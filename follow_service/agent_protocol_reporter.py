"""Agent Protocol Report outbox worker.

This reporter is intentionally independent from Moss copy-trading reporting.
It is disabled by default and only submits rows already persisted in the
agent_protocol_reports outbox.
"""

import asyncio
import json
import logging
import re

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

from . import config as cfg
from . import database as db

logger = logging.getLogger("follow_agent.agent_protocol_reporter")

_AGENT_REPORTS_PATH = "/api/v1/agent-reports"
_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_TERMINAL_STATUS_CODES = {400, 404, 409}
_SUCCESS_STATUS_CODES = {200, 201}
_GO_HTML_ESCAPE = (
    ("&", "\\u0026"),
    ("<", "\\u003c"),
    (">", "\\u003e"),
    (chr(0x2028), "\\u2028"),
    (chr(0x2029), "\\u2029"),
)


def canonical_event_json(event: dict) -> str:
    """Return JSON compatible with Go json.Marshal(map[string]any)."""
    text = json.dumps(event, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    for raw, esc in _GO_HTML_ESCAPE:
        text = text.replace(raw, esc)
    return text


def sign_event(private_key: str, event: dict) -> str:
    """Sign a canonical Agent Protocol event with EIP-191 personal_sign."""
    message = encode_defunct(text=canonical_event_json(event))
    signature = Account.from_key(private_key).sign_message(message).signature.hex()
    return signature if signature.startswith("0x") else f"0x{signature}"


def _normalize_address(value: object) -> str:
    return str(value or "").strip().lower()


def _is_evm_address(value: str) -> bool:
    return bool(_ADDRESS_RE.match(value))


def _int_setting(report_cfg: dict, key: str, default: int) -> int:
    try:
        return int(report_cfg.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _get_valid_report_settings() -> dict | None:
    report_cfg = cfg.get_agent_protocol_report_config()
    if not report_cfg.get("enabled"):
        return None

    base_url = str(report_cfg.get("base_url") or "").strip().rstrip("/")
    agent_address = _normalize_address(report_cfg.get("agent_address"))
    executor_address = _normalize_address(report_cfg.get("executor_address"))
    private_key = str(cfg.get("private_key", "") or "").strip()
    wallet_address = _normalize_address(cfg.get("wallet_address", ""))

    if not base_url:
        logger.warning("Agent protocol reporter skipped: base_url is not set")
        return None
    if not _is_evm_address(agent_address):
        logger.warning("Agent protocol reporter skipped: agent_address must be a 0x EVM address")
        return None
    if not _is_evm_address(executor_address):
        logger.warning("Agent protocol reporter skipped: executor_address must be a 0x EVM address")
        return None
    if not private_key:
        logger.warning("Agent protocol reporter skipped: current service private_key is not set")
        return None

    try:
        signer_address = _normalize_address(Account.from_key(private_key).address)
    except Exception as exc:
        logger.warning("Agent protocol reporter skipped: invalid current service private_key: %s", exc)
        return None

    if executor_address != signer_address:
        logger.warning(
            "Agent protocol reporter skipped: executor_address does not match current service private key address"
        )
        return None
    if wallet_address and wallet_address != signer_address:
        logger.warning(
            "Agent protocol reporter skipped: wallet_address does not match current service private key address"
        )
        return None

    chain_id = report_cfg.get("chain_id")
    try:
        chain_id = int(chain_id)
    except (TypeError, ValueError):
        logger.warning("Agent protocol reporter skipped: chain_id must be an integer")
        return None
    if chain_id <= 0:
        logger.warning("Agent protocol reporter skipped: chain_id must be positive")
        return None

    return {
        "base_url": base_url,
        "agent_address": agent_address,
        "executor_address": executor_address,
        "chain_id": chain_id,
        "private_key": private_key,
        "batch_size": max(1, min(_int_setting(report_cfg, "batch_size", 20), 100)),
        "max_attempts": max(1, _int_setting(report_cfg, "max_attempts", 10)),
    }


def _post_agent_protocol_report(settings: dict, event: dict, signature: str) -> dict:
    body = {
        "agent_address": settings["agent_address"],
        "chain_id": settings["chain_id"],
        "event": event,
        "signature": signature,
    }
    resp = requests.post(
        f"{settings['base_url']}{_AGENT_REPORTS_PATH}",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body, separators=(",", ":"), ensure_ascii=False),
        timeout=10,
    )
    if resp.status_code not in _SUCCESS_STATUS_CODES:
        error = requests.HTTPError(f"{resp.status_code} {resp.text[:500]}")
        error.response = resp
        raise error
    if not resp.content:
        return {}
    return resp.json()


def _mark_report_exception(row: dict, exc: Exception, max_attempts: int) -> None:
    event_id = row["event_id"]
    attempts_after = int(row.get("report_attempts") or 0) + 1
    status_code = None
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status_code = exc.response.status_code

    error = str(exc) or exc.__class__.__name__
    if status_code in _TERMINAL_STATUS_CODES:
        db.mark_agent_protocol_report_dropped(event_id, error)
    elif attempts_after >= max_attempts:
        db.mark_agent_protocol_report_dropped(event_id, error)
    else:
        db.mark_agent_protocol_report_failed(event_id, error)


def _report_pending_once(settings: dict) -> int:
    rows = db.get_pending_agent_protocol_reports(
        limit=settings["batch_size"],
        max_attempts=settings["max_attempts"],
    )
    if not rows:
        return 0

    handled = 0
    for row in rows:
        try:
            event = json.loads(row["event_json"])
            signature = sign_event(settings["private_key"], event)
            _post_agent_protocol_report(settings, event, signature)
        except Exception as exc:
            _mark_report_exception(row, exc, settings["max_attempts"])
            logger.warning("Agent protocol report failed: event_id=%s error=%s", row["event_id"], exc)
        else:
            db.mark_agent_protocol_report_done(row["event_id"])
            handled += 1
            logger.info("Agent protocol report sent: event_id=%s", row["event_id"])
    return handled


def flush_agent_protocol_reports_once(batch_size: int | None = None) -> int:
    """Synchronously flush pending Agent Protocol reports once for CLI flows."""
    settings = _get_valid_report_settings()
    if not settings:
        return 0
    if batch_size is not None:
        settings = dict(settings)
        settings["batch_size"] = max(1, min(int(batch_size), 100))
    return _report_pending_once(settings)


def _request_report_flush(loop: asyncio.AbstractEventLoop, wake_event: asyncio.Event) -> None:
    try:
        loop.call_soon_threadsafe(wake_event.set)
    except RuntimeError:
        pass


async def run_agent_protocol_reporter(stop_event: asyncio.Event) -> None:
    """Run Agent Protocol Report outbox worker when explicitly enabled."""
    settings = _get_valid_report_settings()
    if not settings:
        return

    report_cfg = cfg.get_agent_protocol_report_config()
    interval = max(5, _int_setting(report_cfg, "batch_secs", 30))
    loop = asyncio.get_event_loop()
    wake_event = asyncio.Event()
    listener = lambda: _request_report_flush(loop, wake_event)
    db.register_agent_protocol_report_listener(listener)
    logger.info(
        "Agent protocol reporter started: agent_address=%s chain_id=%s",
        settings["agent_address"],
        settings["chain_id"],
    )

    try:
        while not stop_event.is_set():
            wake_event.clear()
            try:
                await loop.run_in_executor(None, _report_pending_once, settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Agent protocol reporter loop failed: %s", exc)
            if stop_event.is_set():
                break
            try:
                await asyncio.wait_for(wake_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        raise
    finally:
        db.unregister_agent_protocol_report_listener(listener)

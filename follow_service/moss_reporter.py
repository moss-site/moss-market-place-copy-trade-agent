"""Moss copy-trading heartbeat and trade reporting task."""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from . import config as cfg
from . import database as db
from .moss_client import MossClient

logger = logging.getLogger("follow_agent.moss_reporter")

_SCHEMA_VERSION = "2026-05-12"
_SKILL_VERSION = "0.31"
_SOURCE_MAP = {
    "moss": "moss_poller",
    "moss_init": "moss_poller_init",
}
_DONE_REPORT_STATUSES = {"accepted", "duplicate", "done", "ok"}


def _request_trade_report_flush(loop: asyncio.AbstractEventLoop, wake_event: asyncio.Event) -> None:
    """Wake the batch reporter immediately after a new trade is enqueued."""
    try:
        loop.call_soon_threadsafe(wake_event.set)
    except RuntimeError:
        # Event loop already closed during shutdown.
        pass


def _utc_now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_decimal(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            dec = Decimal(stripped)
        except InvalidOperation:
            return None
        return format(dec, "f")
    if isinstance(value, int):
        return str(value)
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return format(dec, "f")


def _to_rfc3339_utc(raw: object) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_address(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text.lower() if text else None


def _normalize_source(source: object) -> str:
    raw = str(source or "").strip()
    return _SOURCE_MAP.get(raw, raw)


def _build_session_id(
    agent_id: str,
    wallet_address: str,
    network: str,
    skill_instance_id: str,
    main_address: str | None = None,
) -> str:
    follower_key = main_address or wallet_address or "unknown"
    seed = f"{network}|{agent_id}|{follower_key}|{skill_instance_id}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"copy_{digest}"


def _build_reporting_client() -> tuple[MossClient, str]:
    """Build a reporting client and stable session id from current config."""
    moss_cfg = cfg.get_moss_source_config()
    base_url = moss_cfg.get("base_url", "")
    agent_id = moss_cfg.get("agent_id", "")
    private_key = cfg.get("private_key", "")
    wallet_address = cfg.get("wallet_address", "")
    main_address = cfg.get("main_address", "")
    builder_address = cfg.get_builder_address()

    if not all([base_url, agent_id, private_key]):
        raise ValueError("Moss reporter config incomplete (need base_url, agent_id, private_key)")

    client = MossClient(
        base_url=base_url,
        agent_id=agent_id,
        private_key=private_key,
        wallet_address=wallet_address,
        builder_address=builder_address,
        main_address=main_address,
    )
    if not client.has_follower_auth():
        raise ValueError("Moss reporter requires private_key for follower auth")

    skill_instance_id = cfg.get_or_create_skill_instance_id()
    session_id = _build_session_id(
        agent_id,
        wallet_address,
        cfg.get_network(),
        skill_instance_id,
        _normalize_address(main_address),
    )
    return client, session_id


def _build_trade_item(row: dict) -> dict:
    size = _format_decimal(row.get("our_size"))
    filled_price = _format_decimal(row.get("filled_price"))
    order_price = _format_decimal(row.get("order_price"))
    executed_notional = None
    if size is not None and filled_price is not None:
        executed_notional = format(abs(Decimal(size)) * Decimal(filled_price), "f")
    elif size is not None and order_price is not None:
        executed_notional = format(abs(Decimal(size)) * Decimal(order_price), "f")
    else:
        executed_notional = _format_decimal(row.get("our_usd"))

    payload = {
        "client_trade_id": row["client_trade_id"],
        "source": _normalize_source(row.get("source")),
        "coin": row["coin"],
        "symbol": row.get("symbol"),
        "side": row["side"],
        "status": row["status"],
        "size": size,
        "client_created_at": _to_rfc3339_utc(row.get("created_at")) or _utc_now_rfc3339(),
        "our_order_id": str(row["our_order_id"]) if row.get("our_order_id") else None,
        "agent_fill_tid": row.get("agent_fill_tid"),
        "agent_event_id": row.get("agent_event_id"),
        "ref_price": _format_decimal(row.get("ref_price")),
        "order_price": order_price,
        "filled_price": filled_price,
        "executed_notional": executed_notional,
        "fee": _format_decimal(row.get("fee")),
        "realized_pnl": _format_decimal(row.get("realized_pnl")),
        "leverage": int(row["leverage"]) if row.get("leverage") is not None else None,
        "agent_account_value": _format_decimal(row.get("agent_account_value")),
        "our_account_value": _format_decimal(row.get("our_account_value")),
        "our_pos_before": _format_decimal(row.get("our_pos_before")),
        "our_pos_after": _format_decimal(row.get("our_pos_after")),
        "error_message": row.get("error_msg"),
    }
    return {k: v for k, v in payload.items() if v is not None}


def _build_trades_batch_body(rows: list[dict], session_id: str) -> dict:
    wallet_address = _normalize_address(cfg.get("wallet_address", "")) or ""
    main_address = _normalize_address(cfg.get("main_address", ""))
    builder_address = _normalize_address(cfg.get_builder_address())
    network = cfg.get_network()
    moss_cfg = cfg.get_moss_source_config()
    agent_id = moss_cfg.get("agent_id", "")

    body = {
        "schema_version": _SCHEMA_VERSION,
        "agent_id": agent_id,
        "network": network,
        "session_id": session_id,
        "wallet_address": wallet_address,
        "skill_instance_id": cfg.get_or_create_skill_instance_id(),
        "skill_version": _SKILL_VERSION,
        "trades": [_build_trade_item(row) for row in rows],
    }
    if main_address:
        body["main_address"] = main_address
    if builder_address:
        body["builder_address"] = builder_address
    return body


def _build_heartbeat_body(session_id: str, status: str) -> dict:
    wallet_address = _normalize_address(cfg.get("wallet_address", "")) or ""
    main_address = _normalize_address(cfg.get("main_address", ""))
    builder_address = _normalize_address(cfg.get_builder_address())
    moss_cfg = cfg.get_moss_source_config()
    body = {
        "schema_version": _SCHEMA_VERSION,
        "session_id": session_id,
        "agent_id": moss_cfg.get("agent_id", ""),
        "network": cfg.get_network(),
        "wallet_address": wallet_address,
        "skill_instance_id": cfg.get_or_create_skill_instance_id(),
        "skill_version": _SKILL_VERSION,
        "follow_ratio": _format_decimal(cfg.get("follow_ratio", 1.0)),
        "status": status,
        "client_time": _utc_now_rfc3339(),
    }
    if main_address:
        body["main_address"] = main_address
    if builder_address:
        body["builder_address"] = builder_address
    return body


def _send_heartbeat(client: MossClient, session_id: str, status: str) -> None:
    body = _build_heartbeat_body(session_id, status)
    client.post_copy_trading_heartbeat(body)
    logger.info("Moss reporter heartbeat sent: session=%s status=%s", session_id, status)


def _report_pending_trades(client: MossClient, session_id: str, batch_size: int) -> None:
    rows = db.get_pending_trade_reports(limit=batch_size)
    if not rows:
        return

    body = _build_trades_batch_body(rows, session_id)
    sent_ids = {row["client_trade_id"] for row in rows}
    logger.info("Moss reporter sending %d trade reports", len(rows))
    try:
        response = client.post_copy_trading_trades_batch(body)
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        for client_trade_id in sent_ids:
            db.mark_trade_report_failed(client_trade_id, error)
        raise

    items = response.get("items") if isinstance(response, dict) else None
    handled_ids: set[str] = set()
    if isinstance(items, list) and items:
        for item in items:
            client_trade_id = item.get("client_trade_id")
            if not client_trade_id:
                continue
            handled_ids.add(client_trade_id)
            item_status = str(item.get("status", "")).lower()
            if item_status in _DONE_REPORT_STATUSES:
                db.mark_trade_report_done(client_trade_id)
            else:
                db.mark_trade_report_failed(
                    client_trade_id,
                    str(item.get("error") or item_status or "rejected"),
                )
    else:
        handled_ids = set(sent_ids)
        for client_trade_id in handled_ids:
            db.mark_trade_report_done(client_trade_id)

    for client_trade_id in sent_ids - handled_ids:
        db.mark_trade_report_failed(client_trade_id, "missing result in response")

    logger.info(
        "Moss reporter trade batch complete: sent=%d handled=%d",
        len(sent_ids),
        len(handled_ids),
    )


def flush_trade_reports_once(batch_size: int | None = None) -> int:
    """Synchronously flush pending trade reports once, used by CLI flows like pause."""
    moss_cfg = cfg.get_moss_source_config()
    if not moss_cfg.get("enabled"):
        return 0

    batch_size = batch_size if batch_size is not None else int(cfg.get("moss_report_batch_size", 100))
    batch_size = min(max(batch_size, 1), 200)
    pending_count = len(db.get_pending_trade_reports(limit=batch_size))
    if pending_count <= 0:
        return 0

    client, session_id = _build_reporting_client()
    try:
        client.register_follower()
    except Exception as exc:
        logger.warning("Moss reporter register_follower failed during flush: %s", exc)
    _report_pending_trades(client, session_id, batch_size)
    return pending_count


async def _heartbeat_loop(stop_event: asyncio.Event, client: MossClient, session_id: str) -> None:
    interval = int(cfg.get("moss_report_heartbeat_secs", 1800))
    interval = max(interval, 10)
    loop = asyncio.get_event_loop()
    while not stop_event.is_set():
        try:
            await loop.run_in_executor(None, _send_heartbeat, client, session_id, "active")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Moss reporter heartbeat failed: %s", exc)
        for _ in range(interval):
            if stop_event.is_set():
                break
            await asyncio.sleep(1)


async def _trade_report_loop(
    stop_event: asyncio.Event,
    client: MossClient,
    session_id: str,
    wake_event: asyncio.Event,
) -> None:
    interval = int(cfg.get("moss_report_batch_secs", 30))
    interval = max(interval, 5)
    batch_size = int(cfg.get("moss_report_batch_size", 100))
    batch_size = min(max(batch_size, 1), 200)
    loop = asyncio.get_event_loop()
    while not stop_event.is_set():
        wake_event.clear()
        try:
            await loop.run_in_executor(None, _report_pending_trades, client, session_id, batch_size)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Moss reporter trade batch failed: %s", exc)
        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=interval)
            if not stop_event.is_set():
                logger.info("Moss reporter immediate flush triggered by new trade")
        except asyncio.TimeoutError:
            pass


async def run_moss_reporter(stop_event: asyncio.Event) -> None:
    """后台执行 heartbeat 和 trade batch 上报。"""
    moss_cfg = cfg.get_moss_source_config()
    if not moss_cfg.get("enabled"):
        logger.info("Moss source not enabled, reporter not started")
        return
    try:
        client, session_id = _build_reporting_client()
    except Exception as exc:
        logger.error("%s", exc)
        return

    loop = asyncio.get_event_loop()
    wake_event = asyncio.Event()
    listener = lambda: _request_trade_report_flush(loop, wake_event)
    db.register_trade_report_listener(listener)
    try:
        await loop.run_in_executor(None, client.register_follower)
    except Exception as exc:
        logger.warning("Moss reporter register_follower failed: %s", exc)

    logger.info("Moss reporter started: session_id=%s", session_id)

    try:
        await asyncio.gather(
            _heartbeat_loop(stop_event, client, session_id),
            _trade_report_loop(stop_event, client, session_id, wake_event),
        )
    except asyncio.CancelledError:
        raise
    finally:
        db.unregister_trade_report_listener(listener)
        try:
            await loop.run_in_executor(None, _send_heartbeat, client, session_id, "stopped")
        except Exception as exc:
            logger.warning("Moss reporter stopped heartbeat failed: %s", exc)

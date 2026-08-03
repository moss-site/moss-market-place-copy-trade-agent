"""
定期查询交易账户的可用余额和账户余额，并写入数据库。
默认每 60 秒执行一次。
"""

import asyncio
import logging
from datetime import datetime, timezone

import requests as _requests
from eth_account import Account

from hyperliquid.info import Info

from . import config as cfg
from . import database as db
from . import hyper_coins
from . import trader
from .moss_client import MossClient
from .symbols import symbol_to_coin

logger = logging.getLogger("follow_agent.balance_tracker")

_MIN_ORDER_USD = 10.0  # Hyperliquid 最小下单金额（与 trader.py 保持一致）


def _fetch_clean_spot_meta(api_url: str) -> dict:
    r = _requests.post(f"{api_url}/info", json={"type": "spotMeta"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    token_count = len(data["tokens"])
    data["universe"] = [
        u for u in data["universe"]
        if all(i < token_count for i in u["tokens"])
    ]
    return data


def _snapshot_balance() -> None:
    api_url = cfg.get("hl_api_url", "https://api.hyperliquid-testnet.xyz")
    private_key = cfg.get("private_key", "")
    if not private_key:
        logger.warning("private_key not configured, skipping balance snapshot")
        return

    wallet = Account.from_key(private_key)
    # 若配置了 main_address，账户归属于 main_address
    account_address = cfg.get("main_address") or wallet.address

    spot_meta = _fetch_clean_spot_meta(api_url)
    info = Info(api_url, skip_ws=True, spot_meta=spot_meta)

    state = info.user_state(account_address)
    margin_summary = state.get("marginSummary", {})
    account_value = float(margin_summary.get("accountValue", 0))
    withdrawable = float(state.get("withdrawable", 0))

    db.record_account_snapshot(account_value=account_value, withdrawable=withdrawable)
    logger.info(
        "Balance snapshot: account_value=%.4f withdrawable=%.4f",
        account_value, withdrawable,
    )
    _check_balance_alert(account_value, withdrawable)


def _check_balance_alert(account_value: float, withdrawable: float) -> None:
    """余额不足告警：每日最多 3 次，相邻 ≥10 分钟。"""
    threshold = float(cfg.get("low_balance_threshold_usd", 10.0))
    threshold = max(_MIN_ORDER_USD, threshold)

    if withdrawable >= threshold:
        return

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    if db.get_today_alert_count("balance_low", today) >= 3:
        return

    last_at = db.get_last_alert_at("balance_low")
    if last_at and (now - last_at).total_seconds() < 600:
        return

    db.record_alert("balance_low", {
        "account_value": account_value,
        "withdrawable": withdrawable,
        "threshold": threshold,
        "main_address": cfg.get("main_address", "") or cfg.get("wallet_address", ""),
        "wallet_address": cfg.get("wallet_address", ""),
    })
    logger.warning(
        "Low balance alert recorded: withdrawable=%.4f < threshold=%.2f",
        withdrawable, threshold,
    )


def _symbol_to_coin(symbol: str, symbol_map: dict) -> str | None:
    """与 moss_ws / moss_poller 一致的 symbol 映射规则。"""
    coin = symbol_to_coin(symbol, symbol_map)
    return hyper_coins.canonicalize_coin(coin) or coin


def _periodic_sltp_check() -> None:
    """
    周期性扫描我方持仓触发止损止盈，与 balance 快照同节奏运行。
    仅在 stop_loss_pct 或 take_profit_pct 启用时才真正工作。
    """
    stop_loss_pct = cfg.get("stop_loss_pct", 0)
    take_profit_pct = cfg.get("take_profit_pct", 0)
    if stop_loss_pct <= 0 and take_profit_pct <= 0:
        return

    moss_cfg = cfg.get_moss_source_config()
    if not moss_cfg.get("enabled"):
        return

    base_url = moss_cfg.get("base_url", "")
    agent_id = moss_cfg.get("agent_id", "")
    private_key = cfg.get("private_key", "")
    if not all([base_url, agent_id, private_key]):
        return

    moss_client = MossClient(
        base_url=base_url,
        agent_id=agent_id,
        private_key=private_key,
        wallet_address=cfg.get("wallet_address", ""),
        main_address=cfg.get("main_address", ""),
        builder_address=cfg.get_builder_address(),
    )

    try:
        raw_positions = moss_client.get_positions()
    except Exception as e:
        logger.warning("SL/TP periodic: fetch Moss positions failed: %s", e)
        return

    symbol_map = moss_cfg.get("symbol_map", {})
    agent_positions: dict = {}
    for p in raw_positions or []:
        coin = _symbol_to_coin(p.get("symbol", ""), symbol_map)
        if not coin:
            continue
        agent_positions[coin] = {
            "size": float(p.get("net_qty", 0)),
            "entry_px": float(p.get("entry_price", 0)),
            "leverage": int(p.get("leverage", 1)),
        }

    trader.check_sl_tp_periodic(agent_id, agent_positions)


async def run_balance_tracker(stop_event: asyncio.Event, interval: int = 60) -> None:
    """每隔 interval 秒执行一次余额快照。"""
    try:
        while not stop_event.is_set():
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, _snapshot_balance)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Balance snapshot error: %s", e)
            for _ in range(interval):
                if stop_event.is_set():
                    break
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("Balance tracker task cancelled, exiting ...")

    logger.info("Balance tracker stopped.")


async def run_sltp_checker(stop_event: asyncio.Event) -> None:
    """每隔 sl_tp_interval 秒执行一次周期性止损止盈扫描。"""
    try:
        while not stop_event.is_set():
            interval = cfg.get("sl_tp_interval", 10)
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, _periodic_sltp_check)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Periodic SL/TP check error: %s", e)
            for _ in range(interval):
                if stop_event.is_set():
                    break
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("SL/TP checker task cancelled, exiting ...")

    logger.info("SL/TP checker stopped.")

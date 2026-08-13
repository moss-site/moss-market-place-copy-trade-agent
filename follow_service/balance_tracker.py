"""
定期查询交易账户的可用余额和账户余额，并写入数据库。
默认每 60 秒执行一次。
"""

import asyncio
import logging
from datetime import datetime, timezone

from . import config as cfg
from . import database as db
from . import hyper_coins
from . import trader
from .moss_client import MossClient
from .symbols import symbol_to_coin

logger = logging.getLogger("follow_agent.balance_tracker")

_MIN_ORDER_USD = 10.0  # Hyperliquid 最小下单金额（与 trader.py 保持一致）


def _snapshot_balance() -> None:
    private_key = cfg.get("private_key", "")
    if not private_key:
        logger.warning("private_key not configured, skipping balance snapshot")
        return

    exchange, info = trader._build_clients()
    # 若配置了 main_address，账户归属于 main_address
    account_address = cfg.get("main_address") or exchange.wallet.address
    moss_cfg = cfg.get_moss_source_config()
    agent_id = str(moss_cfg.get("agent_id") or "")
    try:
        baselines = db.get_baselines(agent_id) if agent_id else {}
    except Exception as e:
        logger.warning("Failed to read active baseline DEXes; using default perps: %s", e)
        baselines = {}
    active_dexes = trader._dexes_for_coins(baselines)

    account_value, withdrawable, _, account_values, withdrawables = (
        trader._get_positions_by_dex(
            info, account_address, dexes=active_dexes
        )
    )

    db.record_account_snapshot(account_value=account_value, withdrawable=withdrawable)
    logger.info(
        "Balance snapshot: account_value=%.4f withdrawable=%.4f dexes=%s",
        account_value, withdrawable, active_dexes,
    )
    for dex in active_dexes:
        _check_balance_alert(
            account_values.get(dex, 0.0),
            withdrawables.get(dex, 0.0),
            dex=dex,
        )


def _check_balance_alert(
    account_value: float,
    withdrawable: float,
    *,
    dex: str = "",
) -> None:
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
        "dex": dex,
        "main_address": cfg.get("main_address", "") or cfg.get("wallet_address", ""),
        "wallet_address": cfg.get("wallet_address", ""),
    })
    logger.warning(
        "Low balance alert recorded: dex=%s withdrawable=%.4f < threshold=%.2f",
        dex or "default", withdrawable, threshold,
    )


def _symbol_to_coin(
    symbol: str,
    symbol_map: dict,
    *,
    hip3_bare_fallback: bool = False,
) -> str | None:
    """与 moss_ws / moss_poller 一致的 symbol 映射规则。"""
    moss_cfg = cfg.get_moss_source_config()
    market_scope = str(moss_cfg.get("market_scope") or "")
    coin = symbol_to_coin(
        symbol,
        symbol_map,
        hyper_coins.get_supported_coins(),
        hip3_bare_fallback=hip3_bare_fallback,
        preferred_dex=market_scope if market_scope and market_scope != "default" else None,
    )
    canonical = hyper_coins.canonicalize_coin(coin) if coin else None
    if not canonical:
        logger.warning("Unknown or unsupported Moss symbol in balance tracker: %s", symbol)
        return None
    return canonical


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
    hip3_bare_fallback = bool(moss_cfg.get("hip3_bare_symbol_fallback", False))
    agent_positions: dict = {}
    for p in raw_positions or []:
        coin = _symbol_to_coin(
            p.get("symbol", ""),
            symbol_map,
            hip3_bare_fallback=hip3_bare_fallback,
        )
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

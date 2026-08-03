"""
交易执行模块 — Coinpilot 仓位对齐模式

核心思路：
- 不直接跟单每笔 fill，而是基于基线快照计算 delta 并对齐仓位
- _do_sync_coin: Moss WS/poller 触发（单币种，即时响应）
- sync_all_positions: 批量对齐（所有币种）
"""

import logging
import math
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import requests as _requests
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from . import config as cfg
from . import database as db
from . import hyper_coins

logger = logging.getLogger("follow_agent.trader")

# 进程内杠杆缓存：{coin: leverage}，避免每次下单都调用 update_leverage
_leverage_cache: dict[str, int] = {}

# 仓位差距小于目标的 1% 时认为已对齐，不重复下单
_SIZE_TOLERANCE = 0.01
# 最小下单金额（USD）
_MIN_ORDER_USD = 10.0

# 仓位缓存：下单成功后记录预期仓位，防止交易所 API 延迟导致 WS/Poller 重复下单
# {coin: (expected_size, timestamp)}，TTL 10 秒
_expected_pos: dict[str, tuple[float, float]] = {}
_EXPECTED_POS_TTL = 10.0

# Per-coin 互斥锁：防止 WS 和 poller 并发对同一 coin 重复下单
_coin_locks: dict[str, threading.Lock] = {}
_coin_locks_mutex = threading.Lock()


def _get_coin_lock(coin: str) -> threading.Lock:
    with _coin_locks_mutex:
        if coin not in _coin_locks:
            _coin_locks[coin] = threading.Lock()
        return _coin_locks[coin]


# ── 共享工具函数 ──────────────────────────────────────────────────────────────

def _fetch_clean_spot_meta(api_url: str) -> dict:
    """获取 spot_meta 并过滤掉 token 索引越界的 universe 条目（测试网已知问题）。"""
    r = _requests.post(f"{api_url}/info", json={"type": "spotMeta"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    token_count = len(data["tokens"])
    data["universe"] = [
        u for u in data["universe"]
        if all(i < token_count for i in u["tokens"])
    ]
    return data


# spot_meta 进程内缓存：内容基本不变，每次事件都重拉浪费 ~0.5s
_spot_meta_cache: dict | None = None
_spot_meta_lock = threading.Lock()


def _get_spot_meta(api_url: str) -> dict:
    """获取 spot_meta，首次调用后缓存于进程内。新增币种需重启服务刷新。"""
    global _spot_meta_cache
    with _spot_meta_lock:
        if _spot_meta_cache is None:
            _spot_meta_cache = _fetch_clean_spot_meta(api_url)
        return _spot_meta_cache


_clients_cache: tuple["Exchange", "Info"] | None = None
_clients_lock = threading.Lock()


def _build_clients() -> tuple[Exchange, Info]:
    """构建 Exchange 和 Info 客户端（整体缓存，避免每次事件重建耗时 10s+）。"""
    global _clients_cache
    with _clients_lock:
        if _clients_cache is not None:
            return _clients_cache

        private_key = cfg.get("private_key", "")
        if not private_key:
            raise ValueError("private_key is not configured")
        api_url = cfg.get("hl_api_url", "https://api.hyperliquid-testnet.xyz")
        main_address = cfg.get("main_address") or None

        wallet = Account.from_key(private_key)
        spot_meta = _get_spot_meta(api_url)
        info = Info(api_url, skip_ws=True, spot_meta=spot_meta)
        meta = info.meta()
        hyper_coins.write_supported_coins_from_meta(meta, api_url=api_url)
        exchange = Exchange(wallet, api_url, meta=meta, account_address=main_address, spot_meta=spot_meta)
        _clients_cache = (exchange, info)
        return exchange, info


def fan_out(tasks: dict[str, Callable]) -> dict:
    """
    并发执行多个无依赖的 REST 调用，返回 {name: result} 字典。

    异常透传：任一任务抛出异常，整体抛出（与串行语义保持一致）。
    每次调用创建/销毁 ThreadPool 本身开销 <1ms，相比 REST 延迟可忽略。
    """
    def _timed(name, fn):
        t0 = _time.time()
        result = fn()
        logger.debug("fan_out [%s]: %.0fms", name, (_time.time() - t0) * 1000)
        return result

    results: dict = {}
    with ThreadPoolExecutor(max_workers=max(len(tasks), 1)) as pool:
        futures = {name: pool.submit(_timed, name, fn) for name, fn in tasks.items()}
        for name, fut in futures.items():
            results[name] = fut.result()
    return results


def _get_positions(info: Info, address: str) -> tuple[float, float, dict]:
    """
    查询账户仓位信息。
    Returns (account_value, withdrawable, positions)
    positions: {coin: {"size": float, "entry_px": float, "leverage": int, "unrealized_pnl": float}}
    size > 0 = 多头，size < 0 = 空头
    """
    state = info.user_state(address)
    account_value = float(state.get("marginSummary", {}).get("accountValue", 0))
    withdrawable = float(state.get("withdrawable", 0))
    positions: dict = {}
    for ap in state.get("assetPositions", []):
        pos = ap.get("position", {})
        coin = pos.get("coin")
        szi = float(pos.get("szi", 0))
        if coin and szi != 0:
            positions[coin] = {
                "size": szi,
                "entry_px": float(pos.get("entryPx") or 0),
                "leverage": int(pos.get("leverage", {}).get("value", 1)),
                "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
            }
    return account_value, withdrawable, positions


def _round_price(px: float, sig_figs: int = 5) -> float:
    """将价格四舍五入到 Hyperliquid 的 5 位有效数字精度规则。"""
    if px <= 0:
        return px
    magnitude = math.floor(math.log10(px))
    factor = 10 ** (sig_figs - 1 - magnitude)
    return round(px * factor) / factor


def _trade_notional(size: float, order_price: float | None = None, filled_price: float | None = None) -> float:
    """Prefer filled notional for executed trades; fall back to order notional otherwise."""
    px = filled_price if filled_price is not None else order_price
    return abs(size) * float(px or 0.0)


def _get_sz_decimals(info: Info, coin: str) -> int:
    """获取币种的交易所 size 精度位数。"""
    asset = info.coin_to_asset.get(coin)
    return info.asset_to_sz_decimals.get(asset, 4) if asset is not None else 4


def _is_coin_tradeable(info: Info, coin: str, source: str = "") -> bool:
    """Apply shared Hyperliquid supported perp coin filters before syncing or opening."""
    log_prefix = f"{source}: " if source else ""
    canonical_coin = hyper_coins.canonicalize_coin(coin, info=info)
    check_coin = canonical_coin or coin

    if cfg.get("perp_only", True):
        asset = info.coin_to_asset.get(check_coin)
        if asset is None or asset >= 10000:
            logger.info("%sSkipping non-perp coin: %s", log_prefix, check_coin)
            return False

    if not canonical_coin:
        logger.info("%sSkipping coin not in Hyperliquid supported coin cache: %s", log_prefix, coin)
        return False

    return True


def _get_follow_ratio() -> float:
    """读取并限制跟单比例到 0~1，避免误配置导致超额跟单。"""
    raw = cfg.get("follow_ratio", 1.0)
    try:
        follow_ratio = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid follow_ratio=%r, using 1.0", raw)
        return 1.0

    if follow_ratio < 0:
        logger.warning("follow_ratio %.4f below 0, clamped to 0", follow_ratio)
        return 0.0
    if follow_ratio > 1:
        logger.warning("follow_ratio %.4f above 1, clamped to 1", follow_ratio)
        return 1.0
    return follow_ratio


def _place_order(
    exchange: Exchange,
    info: Info,
    coin: str,
    is_buy: bool,
    size: float,
    order_price: float,
    leverage: int,
    force: bool = False,
) -> tuple[Optional[str], Optional[float], Optional[float], float]:
    """
    以 IOC 限价单下单。
    Returns (order_id, filled_price, fee, rounded_size)；失败返回 (None, None, None, 0.0)。
    """
    # 更新杠杆（有缓存避免重复调用）
    if _leverage_cache.get(coin) != leverage:
        try:
            _lev_t0 = _time.time()
            exchange.update_leverage(leverage, coin, is_cross=True)
            logger.info("Leverage updated: coin=%s leverage=%sx (%.0fms)", coin, leverage, (_time.time() - _lev_t0) * 1000)
            _leverage_cache[coin] = leverage
        except Exception as e:
            logger.warning("Failed to update leverage for %s: %s", coin, e)

    asset = info.coin_to_asset.get(coin)
    sz_decimals = _get_sz_decimals(info, coin)
    rounded_size = round(size, sz_decimals)

    if rounded_size <= 0:
        logger.warning("Rounded size=0 for %s, skipping", coin)
        return None, None, None, 0.0

    order_value = rounded_size * order_price
    if order_value < _MIN_ORDER_USD and not force:
        logger.warning(
            "Order value $%.2f below minimum $%.2f for %s (size=%.6f price=%.4f), skipping",
            order_value, _MIN_ORDER_USD, coin, rounded_size, order_price,
        )
        return None, None, None, 0.0

    builder = None
    if cfg.is_builder_fee_enabled():
        builder = {"b": cfg.get_builder_address(), "f": cfg.BUILDER_FEE_RATE}

    _order_t0 = _time.time()
    result = exchange.order(
        coin, is_buy, rounded_size, order_price,
        {"limit": {"tif": "Ioc"}},
        builder=builder,
    )
    _order_t1 = _time.time()
    logger.info("_place_order timing [%s]: exchange.order=%.0fms", coin, (_order_t1 - _order_t0) * 1000)

    if result.get("status") == "ok":
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if statuses:
            first = statuses[0]
            if "error" in first:
                logger.error("Order error for %s: %s", coin, first["error"])
                return None, None, None, 0.0
            if "filled" in first:
                oid = str(first["filled"].get("oid", ""))
                filled_price = float(first["filled"].get("avgPx", order_price))
                fee = float(first["filled"].get("totalRawFeeUsdc", 0))
                return oid or None, filled_price, fee, rounded_size
            if "resting" in first:
                oid = str(first["resting"].get("oid", ""))
                logger.warning("IOC order resting for %s (oid=%s), treating as rejected", coin, oid)
                return None, None, None, 0.0
            logger.warning("Unexpected status for %s: %s", coin, first)
            return None, None, None, 0.0
    else:
        logger.error("Order failed for %s: %s", coin, result)

    return None, None, None, 0.0


# ── 核心 delta 对齐逻辑 ───────────────────────────────────────────────────────

def _do_sync_coin(
    exchange: Exchange,
    info: Info,
    coin: str,
    agent_address: str,
    agent_acct_val: float,
    our_acct_val: float,
    agent_positions: dict,
    our_positions: dict,
    baselines: dict,
    mids: dict,
    source: str,
    agent_symbol: Optional[str] = None,
    agent_tx_hash: Optional[str] = None,
    agent_fill_tid: Optional[str] = None,
    agent_event_id: Optional[str] = None,
) -> None:
    """
    对单个 coin 执行 delta 对齐逻辑（内部实现，供 execute_delta_sync 和 sync_all_positions 复用）。
    """
    canonical_coin = hyper_coins.canonicalize_coin(coin, info=info)
    if canonical_coin and canonical_coin != coin:
        logger.info("%s: canonicalized coin %s -> %s", source, coin, canonical_coin)
        if coin in baselines and canonical_coin not in baselines:
            raw_baseline = baselines[coin]
            db.upsert_baseline(
                agent_address=agent_address,
                coin=canonical_coin,
                baseline_agent_size=raw_baseline.get("baseline_agent_size", 0.0),
                our_baseline_size=raw_baseline.get("our_baseline_size", 0.0),
                init_entry_px=raw_baseline.get("init_entry_px", 0.0),
                init_pnl_pct=raw_baseline.get("init_pnl_pct"),
                opened_at_init=raw_baseline.get("opened_at_init", 0),
            )
        coin = canonical_coin
        agent_positions = hyper_coins.canonicalize_positions(agent_positions, info=info)
        our_positions = hyper_coins.canonicalize_positions(our_positions, info=info)
        baselines = hyper_coins.canonicalize_positions(baselines, info=info)

    _t0_sync = _time.time()
    baseline = baselines.get(coin, {})
    baseline_agent_size = baseline.get("baseline_agent_size", 0.0)
    baseline_our_size = baseline.get("our_baseline_size", 0.0)
    agent_leverage = agent_positions[coin].get("leverage", 1) if coin in agent_positions else 1
    agent_symbol = agent_symbol or agent_positions.get(coin, {}).get("symbol")

    if not _is_coin_tradeable(info, coin, source):
        return

    ratio = our_acct_val / agent_acct_val if agent_acct_val > 0 else 0.0
    ratio = ratio * _get_follow_ratio()

    current_agent_size = agent_positions.get(coin, {}).get("size", 0.0)
    current_our_size = our_positions.get(coin, {}).get("size", 0.0)

    # 交易所 API 可能有延迟，用缓存中的预期仓位覆盖
    cached = _expected_pos.get(coin)
    if cached:
        cached_size, cached_ts = cached
        if _time.time() - cached_ts < _EXPECTED_POS_TTL and current_our_size != cached_size:
            logger.info(
                "Position cache hit [%s]: exchange=%.6f cached=%.6f (%.1fs ago)",
                coin, current_our_size, cached_size, _time.time() - cached_ts,
            )
            current_our_size = cached_size

    # ── 仓位占比计算 ──
    ref_price = float(mids.get(coin, 0))
    if ref_price <= 0:
        logger.warning("Cannot get price for %s, skipping", coin)
        return

    agent_pos_pct = (abs(current_agent_size) * ref_price / agent_acct_val * 100) if agent_acct_val > 0 else 0.0
    agent_delta = current_agent_size - baseline_agent_size

    # ── 止损止盈检查（有持仓时，在 delta 对齐前先检查） ──

    stop_loss_pct = cfg.get("stop_loss_pct", 0) / 100.0
    take_profit_pct = cfg.get("take_profit_pct", 0) / 100.0
    our_entry_px = our_positions.get(coin, {}).get("entry_px", 0.0)
    unrealized_pnl = our_positions.get(coin, {}).get("unrealized_pnl", 0.0)

    if current_our_size != 0 and our_entry_px > 0:
        position_value = abs(current_our_size) * our_entry_px
        our_leverage = our_positions.get(coin, {}).get("leverage", 1)
        margin = position_value / our_leverage if our_leverage > 0 else position_value
        pnl_pct = unrealized_pnl / margin if margin > 0 else 0.0

        sl_triggered = stop_loss_pct > 0 and pnl_pct <= -stop_loss_pct
        tp_triggered = take_profit_pct > 0 and pnl_pct >= take_profit_pct

        if sl_triggered or tp_triggered:
            trigger_type = "stop_loss" if sl_triggered else "take_profit"
            logger.info(
                "SL/TP triggered [%s]: %s pnl=%.2f%% threshold=%.2f%%, closing %s",
                trigger_type, coin, pnl_pct * 100,
                (-stop_loss_pct if sl_triggered else take_profit_pct) * 100, coin,
            )
            # 平仓
            is_buy = current_our_size < 0
            close_size = abs(current_our_size)
            slippage = cfg.get("slippage_percent", 1.5) / 100.0
            close_price = _round_price(ref_price * (1 + slippage) if is_buy else ref_price * (1 - slippage))
            oid, filled_price, fee, actual_size = _place_order(exchange, info, coin, is_buy, close_size, close_price, agent_leverage, force=True)

            # 计算已实现盈亏
            realized_pnl = None
            if oid and filled_price:
                if current_our_size > 0:
                    realized_pnl = (filled_price - our_entry_px) * actual_size
                else:
                    realized_pnl = (our_entry_px - filled_price) * actual_size

            if oid:
                _expected_pos[coin] = (0.0, _time.time())

            db.record_trade(
                source=f"moss_{trigger_type}",
                agent_address=agent_address,
                coin=coin,
                side="buy" if is_buy else "sell",
                our_size=actual_size or close_size,
                our_usd=_trade_notional(actual_size or close_size, close_price, filled_price),
                ref_price=ref_price,
                status="filled" if oid else "rejected",
                order_price=close_price,
                filled_price=filled_price,
                entry_price=our_entry_px,
                realized_pnl=realized_pnl,
                fee=fee,
                leverage=agent_leverage,
                our_order_id=oid,
                baseline_agent_size=baseline_agent_size,
                agent_pos_before=current_agent_size,
                agent_delta=agent_delta,
                agent_account_value=agent_acct_val,
                agent_pos_pct=agent_pos_pct,
                our_pos_before=current_our_size,
                our_pos_after=0.0 if oid else current_our_size,
                our_account_value=our_acct_val,
                our_pos_pct=(0.0 if oid else abs(current_our_size) * ref_price / our_acct_val * 100) if our_acct_val > 0 else 0.0,
            )
            # 止损止盈后清除基线（归零）
            if oid:
                db.upsert_baseline(
                    agent_address=agent_address,
                    coin=coin,
                    baseline_agent_size=current_agent_size,
                    our_baseline_size=0.0,
                    init_entry_px=0.0,
                    init_pnl_pct=None,
                    opened_at_init=0,
                )
                logger.info("SL/TP [%s]: baseline reset for %s after %s", trigger_type, coin, trigger_type)
            return

    target_our_size = baseline_our_size + agent_delta * ratio

    # 方向钳制：防止目标方向与基线方向相反
    if baseline_agent_size > 0:
        target_our_size = max(0.0, target_our_size)
    elif baseline_agent_size < 0:
        target_our_size = min(0.0, target_our_size)

    # 统一按交易所精度舍入
    sz_decimals = _get_sz_decimals(info, coin)
    target_our_size = round(target_our_size, sz_decimals)
    our_gap = round(target_our_size - current_our_size, sz_decimals)

    # 已对齐（相对容忍度内）：静默跳过，不写 DB（减少噪音）
    if abs(target_our_size) > 0 and abs(our_gap) <= abs(target_our_size) * _SIZE_TOLERANCE:
        logger.debug(
            "Already aligned: %s our=%.6f target=%.6f gap=%.6f",
            coin, current_our_size, target_our_size, our_gap,
        )
        return

    gap_usd = abs(our_gap) * ref_price
    is_buy = our_gap > 0
    side = "buy" if is_buy else "sell"

    # 完全平仓场景（target=0 且我方有仓位）：跳过最小下单检查，强制平仓
    force_close = (target_our_size == 0 and current_our_size != 0)

    # Gap 存在但低于最小下单金额：记录 skipped（完全平仓场景除外）
    if gap_usd < _MIN_ORDER_USD and not force_close:
        logger.info(
            "Delta sync [%s]: %s %s gap=%.6f gap_usd=$%.2f below $%.0f minimum, skipped",
            source, side, coin, our_gap, gap_usd, _MIN_ORDER_USD,
        )
        db.record_trade(
            source=source,
            agent_address=agent_address,
            coin=coin,
            side=side,
            our_size=abs(our_gap),
            our_usd=gap_usd,
            ref_price=ref_price,
            status="skipped",
            leverage=agent_leverage,
            error_msg=f"gap ${gap_usd:.2f} below minimum ${_MIN_ORDER_USD:.0f}",
            symbol=agent_symbol,
            agent_tx_hash=agent_tx_hash,
            agent_fill_tid=agent_fill_tid,
            agent_event_id=agent_event_id,
            baseline_agent_size=baseline_agent_size,
            agent_pos_before=current_agent_size,
            agent_delta=agent_delta,
            agent_account_value=agent_acct_val,
            agent_pos_pct=agent_pos_pct,
            our_pos_before=current_our_size,
            our_pos_after=current_our_size,
            our_account_value=our_acct_val,
            our_pos_pct=(abs(current_our_size) * ref_price / our_acct_val * 100) if our_acct_val > 0 else 0.0,
        )
        return

    order_size = abs(our_gap)

    slippage = cfg.get("slippage_percent", 1.5) / 100.0
    order_price = _round_price(ref_price * (1 + slippage) if is_buy else ref_price * (1 - slippage))

    logger.info(
        "Delta sync [%s]: %s %s size=%.6f price=%.4f ref=%.4f "
        "agent_baseline=%.6f agent_current=%.6f agent_delta=%.6f "
        "our_baseline=%.6f our_current=%.6f target=%.6f gap=%.6f",
        source, side, coin, order_size, order_price, ref_price,
        baseline_agent_size, current_agent_size, agent_delta,
        baseline_our_size, current_our_size, target_our_size, our_gap,
    )

    _t_before_order = _time.time()
    oid, filled_price, fee, actual_size = _place_order(
        exchange, info, coin, is_buy, order_size, order_price, agent_leverage,
        force=force_close,
    )
    _t_after_order = _time.time()
    logger.info("_do_sync_coin timing [%s]: pre_order=%.0fms place_order=%.0fms", coin, (_t_before_order - _t0_sync) * 1000, (_t_after_order - _t_before_order) * 1000)
    status = "filled" if oid else "rejected"
    signed_actual = actual_size if is_buy else -actual_size
    our_pos_after = round(current_our_size + (signed_actual if oid else 0.0), sz_decimals)

    if oid:
        _expected_pos[coin] = (our_pos_after, _time.time())

    # 计算已实现盈亏（仅平仓/减仓时有意义）
    realized_pnl = None
    our_entry_px = our_positions.get(coin, {}).get("entry_px", 0.0)
    if oid and filled_price and our_entry_px and current_our_size != 0:
        # 判断是否是减仓/平仓（方向与持仓相反）
        is_closing = (current_our_size > 0 and not is_buy) or (current_our_size < 0 and is_buy)
        if is_closing:
            close_size = min(actual_size, abs(current_our_size))
            if current_our_size > 0:
                realized_pnl = (filled_price - our_entry_px) * close_size
            else:
                realized_pnl = (our_entry_px - filled_price) * close_size

    db.record_trade(
        source=source,
        agent_address=agent_address,
        coin=coin,
        side=side,
        our_size=actual_size or order_size,
        our_usd=_trade_notional(actual_size or order_size, order_price, filled_price),
        ref_price=ref_price,
        status=status,
        order_price=order_price,
        filled_price=filled_price,
        leverage=agent_leverage,
        symbol=agent_symbol,
        agent_tx_hash=agent_tx_hash,
        agent_fill_tid=agent_fill_tid,
        agent_event_id=agent_event_id,
        our_order_id=oid,
        baseline_agent_size=baseline_agent_size,
        agent_pos_before=current_agent_size,
        agent_delta=agent_delta,
        agent_account_value=agent_acct_val,
        agent_pos_pct=agent_pos_pct,
        our_pos_before=current_our_size,
        our_pos_after=our_pos_after,
        our_account_value=our_acct_val,
        our_pos_pct=(abs(our_pos_after) * ref_price / our_acct_val * 100) if our_acct_val > 0 else 0.0,
        entry_price=our_entry_px if our_entry_px else None,
        realized_pnl=realized_pnl,
        fee=fee,
    )

    logger.info(
        "Trade recorded [%s]: coin=%s side=%s size=%.6f status=%s oid=%s filled_px=%s "
        "our_pos: %.6f → %.6f pnl=%s fee=%s",
        source, coin, side, order_size, status, oid, filled_price,
        current_our_size, our_pos_after,
        f"{realized_pnl:.4f}" if realized_pnl is not None else "N/A",
        f"{fee:.4f}" if fee is not None else "N/A",
    )


# ── 公开接口 ──────────────────────────────────────────────────────────────────

def close_all_positions() -> list[dict]:
    """
    平掉 HL 上所有持仓。用于暂停跟单时全平仓。
    Returns: 每笔平仓结果列表 [{coin, side, size, status, filled_price, pnl, fee}]
    """
    our_account = cfg.get("main_address") or cfg.get("wallet_address", "")
    if not our_account:
        logger.warning("No account address configured, cannot close positions")
        return []

    exchange, info = _build_clients()
    our_acct_val, _, our_positions = _get_positions(info, our_account)
    mids = info.all_mids()

    if not our_positions:
        logger.info("close_all_positions: no positions to close")
        return []

    results = []
    slippage = cfg.get("slippage_percent", 1.5) / 100.0

    for coin, pos in our_positions.items():
        size = pos["size"]
        entry_px = pos.get("entry_px", 0.0)
        leverage = pos.get("leverage", 1)
        ref_price = float(mids.get(coin, 0))

        if ref_price <= 0 or size == 0:
            continue

        is_buy = size < 0  # 空头需买入平仓，多头需卖出平仓
        close_size = abs(size)
        close_price = _round_price(ref_price * (1 + slippage) if is_buy else ref_price * (1 - slippage))

        logger.info(
            "close_all_positions: closing %s %s size=%.6f price=%.4f",
            coin, "buy" if is_buy else "sell", close_size, close_price,
        )

        oid, filled_price, fee, actual_size = _place_order(exchange, info, coin, is_buy, close_size, close_price, leverage)

        realized_pnl = None
        if oid and filled_price and entry_px:
            if size > 0:
                realized_pnl = (filled_price - entry_px) * actual_size
            else:
                realized_pnl = (entry_px - filled_price) * actual_size

        status = "filled" if oid else "rejected"
        result = {
            "coin": coin, "side": "buy" if is_buy else "sell",
            "size": actual_size or close_size, "status": status,
            "filled_price": filled_price, "pnl": realized_pnl, "fee": fee,
        }
        results.append(result)

        _pos_after_close = 0.0 if oid else size
        if oid:
            _expected_pos[coin] = (0.0, _time.time())
        our_pos_pct = (abs(_pos_after_close) * ref_price / our_acct_val * 100) if our_acct_val > 0 else 0.0

        db.record_trade(
            source="close_all",
            agent_address="",
            coin=coin,
            symbol=db.get_latest_trade_symbol(coin),
            side="buy" if is_buy else "sell",
            our_size=actual_size or close_size,
            our_usd=_trade_notional(actual_size or close_size, close_price, filled_price),
            ref_price=ref_price,
            status=status,
            order_price=close_price,
            filled_price=filled_price,
            entry_price=entry_px,
            realized_pnl=realized_pnl,
            fee=fee,
            leverage=leverage,
            our_order_id=oid,
            our_pos_before=size,
            our_pos_after=0.0 if oid else size,
            our_account_value=our_acct_val,
            our_pos_pct=our_pos_pct,
        )

        logger.info(
            "close_all_positions: %s %s status=%s pnl=%s fee=%s",
            coin, status,
            f"{realized_pnl:.4f}" if realized_pnl is not None else "N/A",
            f"{fee:.4f}" if fee is not None else "N/A",
            oid,
        )

    return results


def execute_delta_sync(
    agent_address: str,
    coin: str,
    source: str = "copy",
    agent_symbol: Optional[str] = None,
    agent_tx_hash: Optional[str] = None,
    agent_fill_tid: Optional[str] = None,
    agent_event_id: Optional[str] = None,
) -> None:
    """
    对单个 coin 执行 delta 对齐。
    Per-coin 互斥锁确保同一 coin 不会被 WS 和 poller 并发重复下单。
    """
    our_account = cfg.get("main_address") or cfg.get("wallet_address", "")
    if not our_account:
        logger.warning("No account address configured, skipping delta sync")
        return

    coin_lock = _get_coin_lock(coin)
    if not coin_lock.acquire(blocking=True, timeout=10):
        logger.warning("execute_delta_sync: %s lock timeout after 10s, skipping [%s]", coin, source)
        return

    try:
        exchange, info = _build_clients()
        agent_acct_val, _, agent_positions = _get_positions(info, agent_address)
        our_acct_val, _, our_positions = _get_positions(info, our_account)
        baselines = db.get_baselines(agent_address)
        mids = info.all_mids()

        if agent_acct_val <= 0:
            logger.warning("Agent account value=0, skipping delta sync for %s", coin)
            return

        _do_sync_coin(
            exchange=exchange,
            info=info,
            coin=coin,
            agent_address=agent_address,
            agent_acct_val=agent_acct_val,
            our_acct_val=our_acct_val,
            agent_positions=agent_positions,
            our_positions=our_positions,
            baselines=baselines,
            mids=mids,
            source=source,
            agent_symbol=agent_symbol,
            agent_tx_hash=agent_tx_hash,
            agent_fill_tid=agent_fill_tid,
            agent_event_id=agent_event_id,
        )
    except Exception as e:
        logger.exception("execute_delta_sync error for %s/%s: %s", agent_address, coin, e)
    finally:
        coin_lock.release()


def check_sl_tp_periodic(agent_address: str, agent_positions: dict) -> None:
    """
    周期性止损止盈扫描（独立于 delta 事件）。

    解决 SL/TP 只在 Agent 新交易时才被触发的问题：balance_tracker 每分钟
    调用一次，即便 Agent 长时间不交易也能按价格变化自动平仓。

    agent_positions: 调用方已查询过的 Moss Agent 仓位，用于重置 baseline_agent_size
    （避免 SL/TP 后下一个事件重新开仓，与 _do_sync_coin 内的 SL/TP 行为一致）
    """
    stop_loss_pct = cfg.get("stop_loss_pct", 0) / 100.0
    take_profit_pct = cfg.get("take_profit_pct", 0) / 100.0
    if stop_loss_pct <= 0 and take_profit_pct <= 0:
        return

    our_account = cfg.get("main_address") or cfg.get("wallet_address", "")
    if not our_account:
        return

    try:
        exchange, info = _build_clients()
        our_acct_val, _, our_positions = _get_positions(info, our_account)
    except Exception as e:
        logger.exception("SL/TP periodic: client setup failed: %s", e)
        return

    if not our_positions:
        return

    mids = info.all_mids()
    slippage = cfg.get("slippage_percent", 1.5) / 100.0

    for coin, pos in list(our_positions.items()):
        our_size = pos.get("size", 0.0)
        our_entry_px = pos.get("entry_px", 0.0)
        unrealized_pnl = pos.get("unrealized_pnl", 0.0)
        leverage = pos.get("leverage", 1)

        if our_size == 0 or our_entry_px <= 0:
            continue

        position_value = abs(our_size) * our_entry_px
        our_leverage = pos.get("leverage", 1)
        margin = position_value / our_leverage if our_leverage > 0 else position_value
        pnl_pct = unrealized_pnl / margin if margin > 0 else 0.0

        sl_triggered = stop_loss_pct > 0 and pnl_pct <= -stop_loss_pct
        tp_triggered = take_profit_pct > 0 and pnl_pct >= take_profit_pct
        if not (sl_triggered or tp_triggered):
            continue

        trigger_type = "stop_loss" if sl_triggered else "take_profit"
        ref_price = float(mids.get(coin, 0))
        if ref_price <= 0:
            logger.warning("SL/TP periodic [%s]: no price for %s, skipping", trigger_type, coin)
            continue

        # non-blocking 获取锁：占用则说明 WS/poller 正在处理，下一分钟再看
        coin_lock = _get_coin_lock(coin)
        if not coin_lock.acquire(blocking=False):
            logger.info("SL/TP periodic [%s]: %s locked, skip this tick", trigger_type, coin)
            continue

        try:
            logger.info(
                "SL/TP periodic [%s]: %s pnl=%.2f%% threshold=%.2f%%, closing",
                trigger_type, coin, pnl_pct * 100,
                (-stop_loss_pct if sl_triggered else take_profit_pct) * 100,
            )

            is_buy = our_size < 0
            close_size = abs(our_size)
            close_price = _round_price(
                ref_price * (1 + slippage) if is_buy else ref_price * (1 - slippage)
            )
            oid, filled_price, fee, actual_size = _place_order(
                exchange, info, coin, is_buy, close_size, close_price, leverage, force=True,
            )

            realized_pnl = None
            if oid and filled_price:
                if our_size > 0:
                    realized_pnl = (filled_price - our_entry_px) * actual_size
                else:
                    realized_pnl = (our_entry_px - filled_price) * actual_size

            our_pos_after = 0.0 if oid else our_size
            if oid:
                _expected_pos[coin] = (0.0, _time.time())
            our_pos_pct = (
                (abs(our_pos_after) * ref_price / our_acct_val * 100)
                if our_acct_val > 0 else 0.0
            )

            db.record_trade(
                source=f"moss_{trigger_type}",
                agent_address=agent_address,
                coin=coin,
                side="buy" if is_buy else "sell",
                our_size=actual_size or close_size,
                our_usd=_trade_notional(actual_size or close_size, close_price, filled_price),
                ref_price=ref_price,
                status="filled" if oid else "rejected",
                order_price=close_price,
                filled_price=filled_price,
                entry_price=our_entry_px,
                realized_pnl=realized_pnl,
                fee=fee,
                leverage=leverage,
                symbol=agent_positions.get(coin, {}).get("symbol"),
                our_order_id=oid,
                our_pos_before=our_size,
                our_pos_after=our_pos_after,
                our_account_value=our_acct_val,
                our_pos_pct=our_pos_pct,
            )

            if oid:
                # baseline_agent_size 更新为 Agent 当前仓位：后续事件 agent_delta=新变化，避免重开
                agent_current = agent_positions.get(coin, {}).get("size", 0.0)
                db.upsert_baseline(
                    agent_address=agent_address,
                    coin=coin,
                    baseline_agent_size=agent_current,
                    our_baseline_size=0.0,
                    init_entry_px=0.0,
                    init_pnl_pct=None,
                    opened_at_init=0,
                )
                logger.info("SL/TP periodic [%s]: baseline reset for %s", trigger_type, coin)
        except Exception as e:
            logger.exception("SL/TP periodic error for %s: %s", coin, e)
        finally:
            coin_lock.release()


def sync_all_positions(agent_address: str, source: str = "align") -> None:
    """
    批量同步所有仓位（一次 API 请求获取全量数据）。
    遍历 baselines 中的所有 coin（包括 Agent 当前无仓位的 coin，以便平仓）。
    """
    our_account = cfg.get("main_address") or cfg.get("wallet_address", "")
    if not our_account:
        logger.warning("No account address configured, skipping sync_all_positions")
        return

    try:
        exchange, info = _build_clients()
        agent_acct_val, _, agent_positions = _get_positions(info, agent_address)
        our_acct_val, _, our_positions = _get_positions(info, our_account)
        baselines = db.get_baselines(agent_address)
        mids = info.all_mids()

        if agent_acct_val <= 0:
            logger.warning("Agent account value=0, skipping sync_all_positions")
            return

        # 需要检查的币种集合：基线中有记录的 + Agent 当前持有的
        all_coins = set(baselines.keys()) | set(agent_positions.keys())

        logger.info(
            "sync_all_positions [%s]: checking %d coins (baseline=%d agent_pos=%d)",
            source, len(all_coins), len(baselines), len(agent_positions),
        )

        for coin in sorted(all_coins):
            coin_lock = _get_coin_lock(coin)
            if not coin_lock.acquire(blocking=False):
                logger.info("sync_all_positions: %s locked, skipping this round", coin)
                continue
            try:
                _do_sync_coin(
                    exchange=exchange,
                    info=info,
                    coin=coin,
                    agent_address=agent_address,
                    agent_acct_val=agent_acct_val,
                    our_acct_val=our_acct_val,
                    agent_positions=agent_positions,
                    our_positions=our_positions,
                    baselines=baselines,
                    mids=mids,
                    source=source,
                )
            except Exception as e:
                logger.exception("sync_all_positions error for coin %s: %s", coin, e)
            finally:
                coin_lock.release()

        logger.info("sync_all_positions [%s] complete", source)

    except Exception as e:
        logger.exception("sync_all_positions fatal error: %s", e)


def get_current_positions(address: str) -> dict:
    """
    查询账户当前持仓信息（供 CLI dashboard 使用）。
    Returns {
        "account_value": float,
        "withdrawable": float,
        "positions": {coin: {"size": float, "entry_px": float, "leverage": int, "unrealized_pnl": float}}
    }
    """
    _, info = _build_clients()
    account_value, withdrawable, positions = _get_positions(info, address)
    return {
        "account_value": account_value,
        "withdrawable": withdrawable,
        "positions": positions,
    }

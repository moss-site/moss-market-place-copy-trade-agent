"""
Moss 信号源轮询模块

通过 REST API 轮询 Moss Agent 的 fills 接口，检测新成交后触发 delta 对齐。

流程:
  启动 → 查 Moss positions 建立基线
       → 每 N 秒轮询 /fills (from_ts 增量)
       → 新 fill → log_event + 查 Moss 仓位 + 调 _do_sync_coin 对齐
"""

import asyncio
import json
import logging
import threading
import time as _time

from . import config as cfg
from . import database as db
from . import hyper_coins
from .moss_client import MossClient
from .trader import (
    _build_clients,
    _do_sync_coin,
    _expected_pos,
    _get_coin_lock,
    _get_follow_ratio,
    _get_positions,
    _get_sz_decimals,
    _is_coin_tradeable,
    _place_order,
    _round_price,
    _trade_notional,
    _MIN_ORDER_USD,
    fan_out,
)
from .symbols import symbol_to_coin

logger = logging.getLogger("follow_agent.moss_poller")


def _get_moss_config() -> dict:
    return cfg.get_moss_source_config()


def _symbol_to_coin(symbol: str) -> str | None:
    """将 Moss symbol 映射为 Hyperliquid coin。"""
    coin = symbol_to_coin(symbol, _get_moss_config().get("symbol_map", {}))
    return hyper_coins.canonicalize_coin(coin) or coin


def _fill_tid_from_fill(fill: dict) -> str:
    """Return a unique raw-event key for a Moss fill."""
    if fill.get("_replay_fill_tid"):
        return str(fill["_replay_fill_tid"])
    fill_id = fill.get("fill_id", "")
    return f"moss_fill_{fill_id}"


def _process_key_from_fill(fill: dict) -> str:
    """Return the order-level processing key shared with WS order events."""
    if fill.get("_replay_process_key"):
        return str(fill["_replay_process_key"])
    order_id = fill.get("order_id") or fill.get("source_order_id")
    if order_id:
        return f"moss_order_{order_id}"
    return _fill_tid_from_fill(fill)


def _normalize_moss_positions(raw_positions: list) -> tuple[dict, float]:
    """
    将 Moss positions 列表转换为 trader.py 需要的格式。

    Returns:
        (positions_dict, account_value)
        positions_dict: {coin: {"size": float, "entry_px": float, "leverage": int, "symbol": str}}
        其中 size 为有符号值（正=多，负=空）
    """
    positions = {}
    for pos in raw_positions:
        symbol = pos.get("symbol", "")
        coin = _symbol_to_coin(symbol)
        if not coin:
            continue
        net_qty = float(pos.get("net_qty", 0))
        positions[coin] = {
            "size": net_qty,
            "entry_px": float(pos.get("entry_price", 0)),
            "leverage": int(pos.get("leverage", 1)),
            "symbol": symbol,
        }
    return positions


def _init_moss_baseline(
    moss_client: MossClient,
    agent_address: str,
    baseline_lock: "threading.Lock | None" = None,
) -> None:
    """
    初始化 Moss Agent 基线。

    持有 baseline_lock 后检查：本轮启动只允许一个通道执行一次 baseline check/init；
    已有基线且我方有仓位则跳过；已有基线但我方仓位为空则强制重建；无基线则查询 Moss 持仓并开仓对齐。
    全流程在锁内进行，防止 WS 与 poller 并发重复初始化开仓（TOCTOU 竞态）。
    """
    our_account = cfg.get("main_address") or cfg.get("wallet_address", "")

    # 直接获取锁（移除了锁外 fast-path，避免 TOCTOU 竞态）
    lock = baseline_lock or threading.Lock()
    with lock:
        if db.has_baseline_init_seen(agent_address):
            logger.info("Moss baseline already checked this run for %s, skipping", agent_address)
            return

        if db.has_baseline(agent_address):
            if our_account:
                try:
                    _, info = _build_clients()
                    _, _, our_positions = _get_positions(info, our_account)
                    if not our_positions:
                        logger.info("Moss baseline exists but our positions are empty — force reinit")
                        db.clear_baselines(agent_address)
                    else:
                        logger.info("Moss baseline already exists for %s (after lock), skipping", agent_address)
                        db.mark_baseline_init_seen(agent_address)
                        return
                except Exception as e:
                    logger.warning("Failed to check positions for Moss auto-reinit: %s", e)
                    return
            else:
                logger.info("Moss baseline already exists for %s, skipping", agent_address)
                db.mark_baseline_init_seen(agent_address)
                return

        logger.info("Initializing Moss baseline for %s ...", agent_address)

        # 查询 Moss Agent 仓位和账户（Follower 签名鉴权）
        moss_positions_raw = moss_client.get_positions()
        moss_account = moss_client.get_account()
        agent_positions = _normalize_moss_positions(moss_positions_raw)
        agent_acct_val = float(moss_account.get("account_value", 0))

        if agent_acct_val <= 0:
            logger.warning("Moss agent account value=0, cannot initialize baseline")
            return

        # 查询我方仓位
        if not our_account:
            logger.warning("No account address configured, skipping Moss baseline")
            return

        exchange, info = _build_clients()
        agent_positions = hyper_coins.canonicalize_positions(agent_positions, info=info)
        our_acct_val, _, our_positions = _get_positions(info, our_account)
        mids = info.all_mids()

        ratio = our_acct_val / agent_acct_val if agent_acct_val > 0 else 0.0
        ratio = ratio * _get_follow_ratio()
        slippage = cfg.get("slippage_percent", 1.5) / 100.0

        logger.info(
            "Moss baseline init: agent_acct=%.2f our_acct=%.2f ratio=%.4f",
            agent_acct_val, our_acct_val, ratio,
        )

        if not agent_positions:
            logger.info("Moss agent has no open positions, baseline initialized as empty")
            db.mark_baseline_init_seen(agent_address)
            return

        init_loss_pct = cfg.get("alignment_loss_pct", 3.0) / 100.0

        for coin, agent_pos in agent_positions.items():
            agent_size = agent_pos["size"]       # signed: +多 -空
            agent_entry = agent_pos["entry_px"]
            agent_leverage = agent_pos["leverage"]

            if not _is_coin_tradeable(info, coin, "moss_init"):
                continue

            current_price = float(mids.get(coin, 0))
            if current_price <= 0:
                logger.warning("Invalid price for %s, skipping", coin)
                continue

            # 计算价格偏离%（不乘杠杆，与 Coinpilot 一致）
            if agent_entry > 0:
                price_deviation_pct = abs(current_price - agent_entry) / agent_entry
            else:
                price_deviation_pct = 0.0

            our_target = agent_size * ratio     # 我方应持有量（signed）
            current_our = our_positions.get(coin, {}).get("size", 0.0)

            # 按交易所精度舍入
            sz_d = _get_sz_decimals(info, coin)
            our_target = round(our_target, sz_d)

            # 价格偏离超过阈值 → 只记基线不开仓，只跟后续增量
            should_open = price_deviation_pct <= init_loss_pct
            our_baseline_size = our_target if should_open else 0.0
            our_gap = round(our_baseline_size - current_our, sz_d)

            db.upsert_baseline(
                agent_address=agent_address,
                coin=coin,
                baseline_agent_size=agent_size,
                our_baseline_size=our_baseline_size,
                init_entry_px=agent_entry,
                init_pnl_pct=price_deviation_pct,
                opened_at_init=1 if should_open else 0,
            )

            logger.info(
                "Moss baseline [%s]: agent_size=%.6f entry=%.4f current=%.4f deviation=%.2f%% "
                "(%s) our_baseline=%.6f our_current=%.6f gap=%.6f",
                coin, agent_size, agent_entry, current_price, price_deviation_pct * 100,
                "OPEN" if should_open else "SKIP",
                our_baseline_size, current_our, our_gap,
            )

            if not should_open:
                logger.info(
                    "Moss baseline [%s]: deviation=%.2f%% > threshold=%.2f%%, skipping init open",
                    coin, price_deviation_pct * 100, init_loss_pct * 100,
                )
                continue

            # 执行初始化开仓
            gap_usd = abs(our_gap) * current_price
            if abs(our_gap) <= 0:
                logger.info("Moss baseline [%s]: gap=0, already aligned", coin)
                continue

            if gap_usd < _MIN_ORDER_USD:
                logger.info(
                    "Moss baseline [%s]: gap too small (gap_usd=$%.2f), skipped", coin, gap_usd,
                )
                _agent_pos_pct = (abs(agent_size) * current_price / agent_acct_val * 100) if agent_acct_val > 0 else 0.0
                _our_pos_pct = (abs(current_our) * current_price / our_acct_val * 100) if our_acct_val > 0 else 0.0
                db.record_trade(
                    source="moss_init",
                    agent_address=agent_address,
                    coin=coin,
                    symbol=agent_positions.get(coin, {}).get("symbol"),
                    side="buy" if our_gap > 0 else "sell",
                    our_size=abs(our_gap),
                    our_usd=gap_usd,
                    ref_price=current_price,
                    status="skipped",
                    leverage=agent_leverage,
                    error_msg=f"gap ${gap_usd:.2f} below minimum ${_MIN_ORDER_USD:.0f}",
                    baseline_agent_size=agent_size,
                    agent_pos_before=agent_size,
                    agent_delta=0.0,
                    agent_account_value=agent_acct_val,
                    agent_pos_pct=_agent_pos_pct,
                    our_pos_before=current_our,
                    our_pos_after=current_our,
                    our_account_value=our_acct_val,
                    our_pos_pct=_our_pos_pct,
                )
                continue

            is_buy = our_gap > 0
            side = "buy" if is_buy else "sell"
            order_price = _round_price(
                current_price * (1 + slippage) if is_buy else current_price * (1 - slippage)
            )

            logger.info(
                "Moss baseline [%s]: placing init %s order: size=%.6f price=%.4f ref=%.4f leverage=%d",
                coin, side, abs(our_gap), order_price, current_price, agent_leverage,
            )

            oid, filled_price, fee, actual_size = _place_order(
                exchange, info, coin, is_buy, abs(our_gap), order_price, agent_leverage,
            )
            status = "filled" if oid else "rejected"
            signed_actual = actual_size if is_buy else -actual_size
            our_pos_after = round(current_our + (signed_actual if oid else 0.0), sz_d)
            if oid:
                _expected_pos[coin] = (our_pos_after, _time.time())

            _agent_pos_pct = (abs(agent_size) * current_price / agent_acct_val * 100) if agent_acct_val > 0 else 0.0
            _our_pos_pct = (abs(our_pos_after) * current_price / our_acct_val * 100) if our_acct_val > 0 else 0.0
            db.record_trade(
                source="moss_init",
                agent_address=agent_address,
                coin=coin,
                symbol=agent_positions.get(coin, {}).get("symbol"),
                side=side,
                our_size=actual_size or abs(our_gap),
                our_usd=_trade_notional(actual_size or abs(our_gap), order_price, filled_price),
                ref_price=current_price,
                status=status,
                order_price=order_price,
                filled_price=filled_price,
                entry_price=filled_price,
                fee=fee,
                leverage=agent_leverage,
                our_order_id=oid,
                baseline_agent_size=agent_size,
                agent_pos_before=agent_size,
                agent_delta=0.0,
                agent_account_value=agent_acct_val,
                agent_pos_pct=_agent_pos_pct,
                our_pos_before=current_our,
                our_pos_after=our_pos_after,
                our_account_value=our_acct_val,
                our_pos_pct=_our_pos_pct,
            )

            logger.info(
                "Moss baseline [%s]: init trade done: side=%s size=%.6f status=%s oid=%s filled_px=%s fee=%s",
                coin, side, abs(our_gap), status, oid, filled_price,
                f"{fee:.4f}" if fee is not None else "N/A",
            )

        logger.info("Moss baseline initialization complete for %s", agent_address)
        db.mark_baseline_init_seen(agent_address)


def _handle_moss_fill(
    fill: dict,
    agent_address: str,
    moss_client: MossClient,
) -> None:
    """
    处理单笔 Moss fill：记录事件 + 查仓位 + delta 对齐。
    在 executor 线程中运行（阻塞式）。
    """
    symbol = fill.get("symbol", "")
    coin = _symbol_to_coin(symbol)
    if not coin:
        logger.warning("Unknown Moss symbol: %s, skipping", symbol)
        return

    fill_id = fill.get("fill_id", "")
    fill_tid = _fill_tid_from_fill(fill)
    process_key = _process_key_from_fill(fill)
    side = fill.get("side", "")
    fill_qty = float(fill.get("qty", 0))
    fill_price = float(fill.get("price", 0))
    is_liquidation = fill.get("is_liquidation", False)

    logger.info(
        "Moss fill detected: %s %s qty=%s price=%s fill_id=%s%s",
        side, coin, fill_qty, fill_price, fill_id,
        " [LIQUIDATION]" if is_liquidation else "",
    )

    # 记录事件
    db.log_event(
        agent_address=agent_address,
        coin=coin,
        raw_payload=json.dumps(fill),
        side=side,
        fill_size=fill_qty,
        fill_price=fill_price,
        tx_hash=None,
        fill_tid=fill_tid,
        process_key=process_key,
        agent_pos_before=None,
        agent_pos_after=None,
    )

    if db.is_synced_process_key(process_key):
        logger.debug("Skipping already-synced Moss order: %s", process_key)
        db.mark_process_key_synced(process_key)
        return

    our_account = cfg.get("main_address") or cfg.get("wallet_address", "")
    if not our_account:
        logger.warning("No account address configured, skipping Moss delta sync")
        return

    try:
        exchange, info = _build_clients()
        coin = hyper_coins.canonicalize_coin(coin, info=info) or coin
    except Exception as e:
        logger.exception("Moss poller: build clients failed: %s", e)
        return

    # 4 个独立 REST 并发拉取：Moss positions + Moss account + HL our state + HL mids
    try:
        results = fan_out({
            "moss_positions": moss_client.get_positions,
            "moss_account": moss_client.get_account,
            "our_state": lambda: _get_positions(info, our_account),
            "mids": info.all_mids,
        })
    except Exception as e:
        logger.exception("Moss poller: parallel fetch failed for %s: %s", coin, e)
        return

    agent_positions = _normalize_moss_positions(results["moss_positions"])
    agent_positions = hyper_coins.canonicalize_positions(agent_positions, info=info)
    agent_acct_val = float(results["moss_account"].get("account_value", 0))
    if agent_acct_val <= 0:
        logger.warning("Moss agent account value=0, skipping delta sync for %s", coin)
        return

    our_acct_val, _, our_positions = results["our_state"]
    mids = results["mids"]
    baselines = db.get_baselines(agent_address)

    agent_size_now = agent_positions.get(coin, {}).get("size", 0.0)

    # Agent 仓位归零 → 基线归零（our_baseline_size=0，触发我方平仓）
    if coin in baselines and abs(agent_size_now) == 0:
        logger.info("Moss agent %s position closed, resetting baseline to zero", coin)
        db.upsert_baseline(
            agent_address=agent_address,
            coin=coin,
            baseline_agent_size=0.0,
            our_baseline_size=0.0,
            init_entry_px=0.0,
            init_pnl_pct=None,
            opened_at_init=0,
        )
        baselines = db.get_baselines(agent_address)

    # 该 coin 还没有基线（新币种）→ 从 0 开始（Agent 重新开仓时自动跟上）
    if coin not in baselines:
        db.upsert_baseline(
            agent_address=agent_address,
            coin=coin,
            baseline_agent_size=0.0,
            our_baseline_size=0.0,
            init_entry_px=0.0,
            init_pnl_pct=None,
            opened_at_init=0,
        )
        baselines = db.get_baselines(agent_address)
        logger.info("Created new Moss baseline for %s: agent=0 our=0", coin)

    # per-coin 互斥锁（blocking 等待，与 WS 通道互斥）
    coin_lock = _get_coin_lock(coin)
    if not coin_lock.acquire(blocking=True, timeout=10):
        logger.warning("Moss poller delta sync: %s lock timeout after 10s, skipping", coin)
        return

    try:
        if db.is_synced_process_key(process_key):
            logger.debug("Skipping already-synced Moss order after lock: %s", process_key)
            db.mark_process_key_synced(process_key)
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
            source="moss",
            agent_symbol=symbol or None,
            agent_tx_hash=None,
            agent_fill_tid=process_key,
            agent_event_id=str(fill.get("event_id") or "") or None,
        )
        db.mark_process_key_synced(process_key)
    except Exception as e:
        logger.exception("Moss delta sync error for %s: %s", coin, e)
    finally:
        coin_lock.release()


def _fill_from_pending_event(row: dict) -> dict | None:
    """把重启前遗留的 pending event 还原成 poller fill 结构。"""
    try:
        raw = json.loads(row.get("raw_payload") or "{}")
    except (json.JSONDecodeError, TypeError):
        return None

    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else None
    fill = dict(payload or raw)
    fill["_replay_fill_tid"] = row.get("fill_tid")
    fill["_replay_process_key"] = row.get("process_key")
    fill_id = fill.get("fill_id")
    if not fill_id and str(row.get("fill_tid", "")).startswith("moss_fill_"):
        fill_id = str(row.get("fill_tid", "")).removeprefix("moss_fill_")
    order_id = fill.get("order_id") or fill.get("source_order_id")
    if not order_id and str(row.get("fill_tid", "")).startswith("moss_order_"):
        order_id = str(row.get("fill_tid", "")).removeprefix("moss_order_")
    if not order_id and str(row.get("process_key", "")).startswith("moss_order_"):
        order_id = str(row.get("process_key", "")).removeprefix("moss_order_")
    if not fill_id or not fill.get("symbol"):
        if not order_id or not fill.get("symbol"):
            return None

    if fill_id:
        fill["fill_id"] = fill_id
    if order_id:
        fill["order_id"] = order_id
    if "qty" not in fill and "fill_qty" in fill:
        fill["qty"] = fill.get("fill_qty")
    if "price" not in fill and "fill_price" in fill:
        fill["price"] = fill.get("fill_price")
    fill.setdefault("created_at", fill.get("createdAt") or raw.get("created_at") or row.get("created_at"))
    return fill


def _replay_pending_moss_fills(moss_client: MossClient, agent_address: str) -> None:
    """服务重启后补处理已落库但未标记 done 的 Moss fill。"""
    pending = db.get_pending_moss_fill_events()
    if not pending:
        return

    logger.info("Replaying %d pending Moss fill events before live polling", len(pending))
    for row in pending:
        fill = _fill_from_pending_event(row)
        if not fill:
            logger.warning("Cannot replay pending Moss fill event: %s", row.get("fill_tid"))
            continue
        _handle_moss_fill(fill, agent_address, moss_client)


async def run_moss_poller(stop_event: asyncio.Event, baseline_lock: "threading.Lock | None" = None) -> None:
    """Moss fills 增量轮询主循环。"""
    moss_cfg = _get_moss_config()
    if not moss_cfg.get("enabled"):
        logger.info("Moss source not enabled, poller not started")
        return

    base_url = moss_cfg.get("base_url", "")
    agent_id = moss_cfg.get("agent_id", "")
    private_key = cfg.get("private_key", "")
    wallet_address = cfg.get("wallet_address", "")
    main_address = cfg.get("main_address", "")
    builder_address = cfg.get_builder_address()

    if not all([base_url, agent_id]):
        logger.error("Moss source config incomplete (need base_url, agent_id)")
        return

    poll_interval = moss_cfg.get("fill_poll_secs", 15)
    agent_address = agent_id

    moss_client = MossClient(
        base_url=base_url, agent_id=agent_id,
        private_key=private_key, wallet_address=wallet_address,
        builder_address=builder_address, main_address=main_address,
    )

    if not moss_client.has_follower_auth():
        logger.error("Moss poller requires private_key for follower auth")
        return

    loop = asyncio.get_event_loop()

    # 自动注册 follower
    try:
        result = await loop.run_in_executor(None, moss_client.register_follower)
        logger.info("Follower registered: %s", result.get("follower_id", "?"))
    except Exception as e:
        logger.error("Follower registration failed: %s", e)
        return

    logger.info("Moss poller starting: agent_id=%s poll_interval=%ds", agent_id, poll_interval)

    # 初始化基线
    try:
        await loop.run_in_executor(None, _init_moss_baseline, moss_client, agent_address, baseline_lock)
    except Exception as e:
        logger.exception("Moss baseline init failed: %s", e)
        return

    # 重启恢复：先补处理上次已落库但未完成 delta sync 的 fill，避免 pending 永久孤立。
    try:
        await loop.run_in_executor(None, _replay_pending_moss_fills, moss_client, agent_address)
    except Exception as e:
        logger.exception("Moss pending fill replay failed: %s", e)

    # 初始化 fill 游标：最多回看最近 20 分钟，避免重启窗口漏单但不回放过久历史。
    lookback_cursor = db.default_moss_fill_cursor()
    pending_fill_ts = db.get_earliest_pending_moss_fill_created_at()
    last_synced_fill_ts = db.get_latest_synced_moss_fill_created_at()
    if pending_fill_ts:
        last_fill_ts = db.moss_fill_cursor_with_overlap(pending_fill_ts)
        logger.info(
            "Moss poller found pending fill cursor: %s (query from %s)",
            pending_fill_ts, last_fill_ts,
        )
    elif last_synced_fill_ts:
        restored_cursor = db.moss_fill_cursor_with_overlap(last_synced_fill_ts)
        last_fill_ts = max(restored_cursor, lookback_cursor)
        logger.info(
            "Moss poller restored fill cursor from last synced fill: %s (query from %s, floor %s)",
            last_synced_fill_ts, last_fill_ts, lookback_cursor,
        )
    else:
        last_fill_ts = lookback_cursor
        logger.info("Moss poller started with lookback fill cursor: %s", last_fill_ts)

    # 轮询循环
    backoff = poll_interval
    while not stop_event.is_set():
        try:
            new_fills = await loop.run_in_executor(
                None, moss_client.get_fills, last_fill_ts, 50
            )

            if new_fills:
                # 按 fill_id 排序（递增）
                new_fills.sort(key=lambda f: int(f.get("fill_id", 0)))

                for fill in new_fills:
                    await loop.run_in_executor(
                        None, _handle_moss_fill, fill, agent_address, moss_client
                    )

                # 更新游标
                last_fill_ts = new_fills[-1].get("created_at", last_fill_ts)

            # 成功后重置退避
            backoff = poll_interval

        except asyncio.CancelledError:
            logger.info("Moss poller task cancelled, exiting ...")
            break
        except Exception as e:
            logger.exception("Moss poll error: %s", e)
            # 指数退避：5 → 10 → 20 → 40 → max 60
            backoff = min(backoff * 2, 60)
            logger.info("Moss poll backoff: %ds", backoff)

        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            logger.info("Moss poller task cancelled during sleep, exiting ...")
            break

    logger.info("Moss poller stopped.")

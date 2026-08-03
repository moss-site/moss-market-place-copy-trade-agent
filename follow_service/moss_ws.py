"""
Moss Source Event WebSocket 消费模块

通过 WebSocket 实时接收 Moss Agent 的 source event，触发 delta 仓位对齐。

流程:
  启动 → bootstrap 获取初始状态 → 建立基线
       → WebSocket 连接 → 收到 ready
       → 实时接收 source_event → 触发 delta 对齐
       → 断线 → 重新 bootstrap + 重连
"""

import asyncio
import json
import logging
import threading
import time as _time

import websockets

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

logger = logging.getLogger("follow_agent.moss_ws")

# 关注的事件类型（会触发 delta 对齐）。position.updated 只作为状态通知，
# order.filled 与 poller fill 通过 order_id/source_order_id 做统一去重。
_TRADE_EVENT_TYPES = {
    "order.filled",
}


def _get_moss_config() -> dict:
    return cfg.get_moss_source_config()


def _symbol_to_coin(symbol: str) -> str | None:
    """将 Moss symbol 映射为 Hyperliquid coin。"""
    coin = symbol_to_coin(symbol, _get_moss_config().get("symbol_map", {}))
    return hyper_coins.canonicalize_coin(coin) or coin


def _event_fill_tid(event: dict) -> str:
    """Return a unique raw-event key for a Moss WS order event."""
    event_id = event.get("event_id", "")
    return f"moss_evt_{event_id}"


def _event_process_key(event: dict) -> str:
    """Return the order-level processing key shared with poller fills."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    source_order_id = payload.get("source_order_id") or payload.get("order_id")
    if source_order_id:
        return f"moss_order_{source_order_id}"
    return _event_fill_tid(event)


def _normalize_moss_positions(raw_positions: list) -> dict:
    """将 Moss positions 列表转换为 trader.py 需要的格式。"""
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


def _init_baseline_from_bootstrap(
    bootstrap: dict,
    agent_address: str,
    moss_client: MossClient,
    baseline_lock: "threading.Lock | None" = None,
) -> None:
    """
    用 bootstrap 数据初始化基线。

    持有 baseline_lock 后检查：本轮启动只允许一个通道执行一次 baseline check/init；
    已有基线且我方有仓位则跳过；已有基线但我方仓位为空则强制重建；无基线则初始化。
    全流程在锁内进行，防止 WS 与 poller 并发重复初始化开仓（TOCTOU 竞态）。
    """
    # 我方仓位检查
    our_account = cfg.get("main_address") or cfg.get("wallet_address", "")
    if not our_account:
        logger.warning("No account address configured, skipping baseline init")
        return

    # 直接获取锁（移除了锁外 fast-path，避免 TOCTOU 竞态）
    lock = baseline_lock or threading.Lock()
    with lock:
        if db.has_baseline_init_seen(agent_address):
            logger.info("Moss WS baseline already checked this run, skipping")
            return

        if db.has_baseline(agent_address):
            try:
                _, info = _build_clients()
                _, _, our_positions = _get_positions(info, our_account)
                if our_positions:
                    logger.info("Moss WS baseline already exists (after lock), skipping")
                    db.mark_baseline_init_seen(agent_address)
                    return
                logger.info("Moss WS baseline exists but our positions empty — force reinit")
                db.clear_baselines(agent_address)
            except Exception as e:
                logger.warning("Failed to check positions for reinit: %s", e)
                return

        logger.info("Initializing baseline from bootstrap for %s ...", agent_address)

        # 从 bootstrap 获取 Agent 仓位和账户
        # 后端 Agent 无持仓时 positions 字段可能返回 null，需兜底为空列表
        positions_raw = bootstrap.get("positions") or []
        agent_positions = _normalize_moss_positions(positions_raw)
        account_state = bootstrap.get("account_state", {})
        agent_acct_val = float(account_state.get("account_value", 0))

        if agent_acct_val <= 0:
            logger.warning("Moss agent account value=0, cannot initialize baseline")
            return

        exchange, info = _build_clients()
        agent_positions = hyper_coins.canonicalize_positions(agent_positions, info=info)
        our_acct_val, _, our_positions = _get_positions(info, our_account)
        mids = info.all_mids()

        ratio = our_acct_val / agent_acct_val if agent_acct_val > 0 else 0.0
        ratio = ratio * _get_follow_ratio()
        slippage = cfg.get("slippage_percent", 1.5) / 100.0
        init_loss_pct = cfg.get("alignment_loss_pct", 3.0) / 100.0

        logger.info(
            "Moss WS baseline init: agent_acct=%.2f our_acct=%.2f ratio=%.4f",
            agent_acct_val, our_acct_val, ratio,
        )

        if not agent_positions:
            logger.info("Moss agent has no open positions, baseline initialized as empty")
            db.mark_baseline_init_seen(agent_address)
            return

        for coin, agent_pos in agent_positions.items():
            agent_size = agent_pos["size"]
            agent_entry = agent_pos["entry_px"]
            agent_leverage = agent_pos["leverage"]

            if not _is_coin_tradeable(info, coin, "moss_ws_init"):
                continue

            current_price = float(mids.get(coin, 0))
            if current_price <= 0:
                logger.warning("Invalid price for %s, skipping", coin)
                continue

            # 价格偏离检查
            if agent_entry > 0:
                price_deviation_pct = abs(current_price - agent_entry) / agent_entry
            else:
                price_deviation_pct = 0.0

            our_target = agent_size * ratio
            current_our = our_positions.get(coin, {}).get("size", 0.0)

            # 按交易所精度舍入
            sz_d = _get_sz_decimals(info, coin)
            our_target = round(our_target, sz_d)

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
                "Moss WS baseline [%s]: agent=%.6f deviation=%.2f%% (%s) our_baseline=%.6f",
                coin, agent_size, price_deviation_pct * 100,
                "OPEN" if should_open else "SKIP", our_baseline_size,
            )

            if not should_open:
                continue

            gap_usd = abs(our_gap) * current_price
            if abs(our_gap) <= 0 or gap_usd < _MIN_ORDER_USD:
                continue

            is_buy = our_gap > 0
            order_price = _round_price(
                current_price * (1 + slippage) if is_buy else current_price * (1 - slippage)
            )

            oid, filled_price, fee, actual_size = _place_order(
                exchange, info, coin, is_buy, abs(our_gap), order_price, agent_leverage,
            )

            _agent_pos_pct = (abs(agent_size) * current_price / agent_acct_val * 100) if agent_acct_val > 0 else 0.0
            signed_actual = actual_size if is_buy else -actual_size
            our_pos_after = round(current_our + (signed_actual if oid else 0.0), sz_d)
            if oid:
                _expected_pos[coin] = (our_pos_after, _time.time())
            _our_pos_pct = (abs(our_pos_after) * current_price / our_acct_val * 100) if our_acct_val > 0 else 0.0

            db.record_trade(
                source="moss_ws_init",
                agent_address=agent_address,
                coin=coin,
                symbol=agent_positions.get(coin, {}).get("symbol"),
                side="buy" if is_buy else "sell",
                our_size=actual_size or abs(our_gap),
                our_usd=_trade_notional(actual_size or abs(our_gap), order_price, filled_price),
                ref_price=current_price,
                status="filled" if oid else "rejected",
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
                "Moss WS baseline [%s]: init %s size=%.6f status=%s filled_px=%s",
                coin, "buy" if is_buy else "sell", abs(our_gap),
                "filled" if oid else "rejected", filled_price,
            )

        logger.info("Moss WS baseline initialization complete for %s", agent_address)
        db.mark_baseline_init_seen(agent_address)


def _handle_source_event(
    event: dict,
    agent_address: str,
    moss_client: MossClient,
) -> None:
    """
    处理单个 source_event：查 Moss 仓位 + delta 对齐。
    在 executor 线程中运行。
    """
    event_type = event.get("event_type", "")
    payload = event.get("payload", {})
    event_seq = event.get("event_sequence", 0)

    # 只处理交易相关事件
    if event_type not in _TRADE_EVENT_TYPES:
        logger.debug("Skipping non-trade event: %s seq=%d", event_type, event_seq)
        return

    # 从 payload 提取 symbol → coin
    symbol = payload.get("symbol", "")
    coin = _symbol_to_coin(symbol)
    if not coin:
        logger.debug("No symbol in event %s seq=%d, skipping", event_type, event_seq)
        return

    event_id = event.get("event_id", "")
    fill_tid = _event_fill_tid(event)
    process_key = _event_process_key(event)

    logger.info(
        "Moss WS event: type=%s coin=%s seq=%d event_id=%s",
        event_type, coin, event_seq, event_id[:16] if event_id else "?",
    )
    _t0 = _time.time()

    # 记录事件
    db.log_event(
        agent_address=agent_address,
        coin=coin,
        raw_payload=json.dumps(event),
        side=payload.get("side"),
        fill_size=float(payload.get("fill_qty", payload.get("qty", 0)) or 0),
        fill_price=float(payload.get("fill_price", payload.get("price", 0)) or 0),
        tx_hash=None,
        fill_tid=fill_tid,
        process_key=process_key,
        agent_pos_before=None,
        agent_pos_after=float(payload.get("net_qty", 0) or 0) if "net_qty" in payload else None,
    )

    if db.is_synced_process_key(process_key):
        logger.debug("Skipping already-synced Moss order: %s", process_key)
        db.mark_process_key_synced(process_key)
        return

    our_account = cfg.get("main_address") or cfg.get("wallet_address", "")
    if not our_account:
        return

    try:
        exchange, info = _build_clients()
        coin = hyper_coins.canonicalize_coin(coin, info=info) or coin
    except Exception as e:
        logger.exception("Moss WS: build clients failed: %s", e)
        return
    _t1 = _time.time()

    # 4 个独立 REST 并发拉取：Moss positions + Moss account + HL our state + HL mids
    try:
        results = fan_out({
            "moss_positions": moss_client.get_positions,
            "moss_account": moss_client.get_account,
            "our_state": lambda: _get_positions(info, our_account),
            "mids": info.all_mids,
        })
    except Exception as e:
        logger.exception("Moss WS: parallel fetch failed for %s: %s", coin, e)
        return
    _t2 = _time.time()
    logger.info(
        "Moss WS timing [%s]: build_clients=%.0fms fan_out=%.0fms",
        coin, (_t1 - _t0) * 1000, (_t2 - _t1) * 1000,
    )

    agent_positions = _normalize_moss_positions(results["moss_positions"])
    agent_positions = hyper_coins.canonicalize_positions(agent_positions, info=info)
    agent_acct_val = float(results["moss_account"].get("account_value", 0))
    if agent_acct_val <= 0:
        logger.warning("Moss agent account value=0, skipping delta sync")
        return

    our_acct_val, _, our_positions = results["our_state"]
    mids = results["mids"]
    baselines = db.get_baselines(agent_address)

    agent_size_now = agent_positions.get(coin, {}).get("size", 0.0)

    # Agent 仓位归零 → 基线归零（our_baseline_size=0，触发我方平仓）
    if coin in baselines and abs(agent_size_now) == 0:
        logger.info("Moss agent %s position closed, resetting baseline to zero", coin)
        db.upsert_baseline(
            agent_address=agent_address, coin=coin,
            baseline_agent_size=0.0, our_baseline_size=0.0,
            init_entry_px=0.0, init_pnl_pct=None, opened_at_init=0,
        )
        baselines = db.get_baselines(agent_address)

    # 新币种 → 基线从 0 开始（Agent 重新开仓时自动跟上）
    if coin not in baselines:
        db.upsert_baseline(
            agent_address=agent_address, coin=coin,
            baseline_agent_size=0.0, our_baseline_size=0.0,
            init_entry_px=0.0, init_pnl_pct=None, opened_at_init=0,
        )
        baselines = db.get_baselines(agent_address)
        logger.info("Created new baseline for %s: agent=0 our=0", coin)

    # per-coin 互斥锁（blocking 等待，避免并发 fill 在锁外被标记为已处理后漏单）
    coin_lock = _get_coin_lock(coin)
    if not coin_lock.acquire(blocking=True, timeout=10):
        logger.warning("Moss WS delta sync: %s lock timeout after 10s, skipping", coin)
        return

    try:
        if db.is_synced_process_key(process_key):
            logger.debug("Skipping already-synced Moss order after lock: %s", process_key)
            db.mark_process_key_synced(process_key)
            return
        _t3 = _time.time()
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
            source="moss_ws",
            agent_symbol=symbol or None,
            agent_tx_hash=None,
            agent_fill_tid=process_key,
            agent_event_id=event_id or None,
        )
        _t4 = _time.time()
        logger.info("Moss WS timing [%s]: _do_sync_coin=%.0fms total=%.0fms", coin, (_t4 - _t3) * 1000, (_t4 - _t0) * 1000)
        db.mark_process_key_synced(process_key)
    except Exception as e:
        logger.exception("Moss WS delta sync error for %s: %s", coin, e)
    finally:
        coin_lock.release()


async def run_moss_ws(stop_event: asyncio.Event, baseline_lock: "threading.Lock | None" = None) -> None:
    """Moss Source Event WebSocket 消费主循环。"""
    moss_cfg = _get_moss_config()
    if not moss_cfg.get("enabled"):
        logger.info("Moss source not enabled, WS consumer not started")
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

    agent_address = agent_id
    moss_client = MossClient(
        base_url=base_url, agent_id=agent_id,
        private_key=private_key, wallet_address=wallet_address,
        builder_address=builder_address, main_address=main_address,
    )
    loop = asyncio.get_event_loop()

    # 自动注册 follower
    if moss_client.has_follower_auth():
        try:
            result = await loop.run_in_executor(None, moss_client.register_follower)
            logger.info("Moss WS: follower registered: %s", result.get("follower_id", "?"))
        except Exception as e:
            logger.warning("Moss WS: follower registration failed: %s", e)

    backoff = 5
    while not stop_event.is_set():
        try:
            # Step 1: bootstrap 获取初始状态
            logger.info("Moss WS: fetching bootstrap for %s ...", agent_id)
            bootstrap = await loop.run_in_executor(None, moss_client.get_bootstrap)
            event_seq = bootstrap.get("event_sequence", 0)
            logger.info(
                "Moss WS: bootstrap OK, event_sequence=%d, positions=%d",
                event_seq, len(bootstrap.get("positions") or []),
            )

            # Step 2: 用 bootstrap 初始化基线
            await loop.run_in_executor(
                None, _init_baseline_from_bootstrap, bootstrap, agent_address, moss_client, baseline_lock,
            )

            # Step 3: 建立 WebSocket 连接
            ws_headers = moss_client.build_ws_headers()
            ws_base = base_url.replace("http://", "ws://").replace("https://", "wss://")
            ws_path = moss_client.get_ws_path()
            ws_url = f"{ws_base}{ws_path}"

            logger.info("Moss WS: connecting to %s ...", ws_url)

            async with websockets.connect(
                ws_url,
                additional_headers=ws_headers,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                logger.info("Moss WS: connected, waiting for ready frame ...")

                # 连接成功，重置退避
                backoff = 5

                # Step 4: 等待 ready 帧
                raw_msg = await ws.recv()
                msg = json.loads(raw_msg)

                if msg.get("type") != "ready":
                    logger.warning("Moss WS: expected ready frame, got: %s", msg.get("type"))
                    continue

                subscribed_ids = msg.get("subscribed_source_account_ids", [])
                logger.info(
                    "Moss WS: ready! server_time=%s subscribed=%s",
                    msg.get("server_time"), subscribed_ids,
                )

                # Step 5: 消费事件流
                async for raw_msg in ws:
                    if stop_event.is_set():
                        break

                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")
                    if msg_type != "source_event":
                        logger.debug("Moss WS: non-event message type=%s", msg_type)
                        continue

                    event = msg.get("event", {})
                    await loop.run_in_executor(
                        None, _handle_source_event, event, agent_address, moss_client,
                    )

        except asyncio.CancelledError:
            logger.info("Moss WS task cancelled, exiting ...")
            break
        except (websockets.ConnectionClosed, OSError) as e:
            if stop_event.is_set():
                break
            logger.warning("Moss WS connection lost (%s), reconnecting in %ds ...", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            if stop_event.is_set():
                break
            logger.exception("Moss WS error: %s, reconnecting in %ds ...", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    logger.info("Moss WS consumer stopped.")

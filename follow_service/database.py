"""
数据库模块 — Coinpilot 仓位对齐模式

表结构：
  agent_baseline    — 基线快照（跟单启动时 Agent 的仓位参考点）
  events            — Moss 原始事件（fill_tid）+ 订单级处理键（process_key）
  trades            — 我方交易记录（含完整上下文）
  account_snapshots — 账户余额快照（每60s）
  alerts            — 告警队列（余额不足等，Bot 轮询消费）
"""

import json as _json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

from . import config as cfg

_baseline_init_seen: set[str] = set()
_baseline_init_seen_lock = threading.Lock()
_trade_report_listeners: list[Callable[[], None]] = []
_trade_report_listeners_lock = threading.Lock()
_agent_protocol_report_listeners: list[Callable[[], None]] = []
_agent_protocol_report_listeners_lock = threading.Lock()


def _db_path() -> str:
    return str(
        Path(cfg.get("db_path", str(cfg.get_instance_dir() / "follow_agent.db"))).expanduser()
    )


@contextmanager
def get_conn():
    Path(_db_path()).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """创建所有表（无迁移，全新启动）。"""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_baseline (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_address       TEXT    NOT NULL,
                coin                TEXT    NOT NULL,
                baseline_agent_size REAL    NOT NULL,
                our_baseline_size   REAL    NOT NULL,
                init_entry_px       REAL,
                init_pnl_pct        REAL,
                opened_at_init      INTEGER NOT NULL DEFAULT 1,
                created_at          TEXT    NOT NULL,
                UNIQUE(agent_address, coin) ON CONFLICT REPLACE
            );

            CREATE TABLE IF NOT EXISTS events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_address  TEXT    NOT NULL,
                coin           TEXT    NOT NULL,
                side           TEXT,
                fill_size      REAL,
                fill_price     REAL,
                tx_hash        TEXT,
                fill_tid       TEXT UNIQUE,
                process_key    TEXT,
                agent_pos_before REAL,
                agent_pos_after  REAL,
                raw_payload    TEXT    NOT NULL,
                created_at     TEXT    NOT NULL,
                sync_status    TEXT    NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS trades (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                source               TEXT    NOT NULL,
                agent_address        TEXT    NOT NULL,
                coin                 TEXT    NOT NULL,
                symbol               TEXT,
                side                 TEXT    NOT NULL,
                baseline_agent_size  REAL,
                agent_pos_before     REAL,
                agent_delta          REAL,
                agent_account_value  REAL,
                agent_pos_pct        REAL,
                our_pos_before       REAL,
                our_pos_after        REAL,
                our_account_value    REAL,
                our_pos_pct          REAL,
                our_size             REAL    NOT NULL,
                our_usd              REAL    NOT NULL,
                ref_price            REAL    NOT NULL,
                order_price          REAL,
                filled_price         REAL,
                entry_price          REAL,
                realized_pnl         REAL,
                fee                  REAL,
                leverage             INTEGER,
                status               TEXT    NOT NULL,
                error_msg            TEXT,
                agent_tx_hash        TEXT,
                agent_fill_tid       TEXT,
                agent_event_id       TEXT,
                our_order_id         TEXT,
                client_trade_id      TEXT,
                created_at           TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trade_reports (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id         INTEGER NOT NULL,
                client_trade_id  TEXT    NOT NULL,
                report_status    TEXT    NOT NULL DEFAULT 'pending',
                report_attempts  INTEGER NOT NULL DEFAULT 0,
                last_reported_at TEXT,
                last_error       TEXT,
                created_at       TEXT    NOT NULL,
                updated_at       TEXT    NOT NULL,
                UNIQUE(trade_id),
                UNIQUE(client_trade_id)
            );
            CREATE INDEX IF NOT EXISTS ix_trade_reports_status_id
                ON trade_reports(report_status, id);

            CREATE TABLE IF NOT EXISTS agent_protocol_reports (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id            INTEGER,
                client_trade_id     TEXT,
                event_id            TEXT    NOT NULL,
                trace_id            TEXT    NOT NULL,
                seq                 INTEGER,
                event_json          TEXT    NOT NULL,
                report_status       TEXT    NOT NULL DEFAULT 'pending',
                report_attempts     INTEGER NOT NULL DEFAULT 0,
                last_reported_at    TEXT,
                last_error          TEXT,
                created_at          TEXT    NOT NULL,
                updated_at          TEXT    NOT NULL,
                UNIQUE(event_id)
            );
            CREATE INDEX IF NOT EXISTS ix_agent_protocol_reports_status_id
                ON agent_protocol_reports(report_status, id);

            CREATE TABLE IF NOT EXISTS account_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                account_value REAL    NOT NULL,
                withdrawable  REAL    NOT NULL,
                created_at    TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                type          TEXT    NOT NULL,
                alert_date    TEXT    NOT NULL,
                created_at    TEXT    NOT NULL,
                payload       TEXT    NOT NULL,
                acknowledged  INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_alerts_date_type ON alerts(alert_date, type);
            CREATE INDEX IF NOT EXISTS ix_alerts_unread   ON alerts(acknowledged, id);
        """)
        # 存量数据库迁移：添加 sync_status 列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）
        try:
            conn.execute(
                "ALTER TABLE events ADD COLUMN sync_status TEXT NOT NULL DEFAULT 'pending'"
            )
        except sqlite3.OperationalError:
            pass  # 列已存在，忽略
        # 存量数据库迁移：添加 process_key 列，用于订单级处理去重
        try:
            conn.execute("ALTER TABLE events ADD COLUMN process_key TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在，忽略
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_process_key_status "
            "ON events(process_key, sync_status)"
        )
        # 存量数据库迁移：trades 表添加 leverage 列
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN leverage INTEGER")
        except sqlite3.OperationalError:
            pass  # 列已存在，忽略
        # 存量数据库迁移：trades 表添加 client_trade_id 列
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN client_trade_id TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在，忽略
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN symbol TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在，忽略
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN agent_event_id TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在，忽略


def register_trade_report_listener(listener: Callable[[], None]) -> None:
    """注册 trade_report 入队监听器，用于唤醒即时上报。"""
    with _trade_report_listeners_lock:
        if listener not in _trade_report_listeners:
            _trade_report_listeners.append(listener)


def unregister_trade_report_listener(listener: Callable[[], None]) -> None:
    with _trade_report_listeners_lock:
        try:
            _trade_report_listeners.remove(listener)
        except ValueError:
            pass


def _notify_trade_report_listeners() -> None:
    with _trade_report_listeners_lock:
        listeners = list(_trade_report_listeners)
    for listener in listeners:
        try:
            listener()
        except Exception:
            pass


def register_agent_protocol_report_listener(listener: Callable[[], None]) -> None:
    """注册 Agent Protocol Report 入队监听器，用于唤醒即时上报。"""
    with _agent_protocol_report_listeners_lock:
        if listener not in _agent_protocol_report_listeners:
            _agent_protocol_report_listeners.append(listener)


def unregister_agent_protocol_report_listener(listener: Callable[[], None]) -> None:
    with _agent_protocol_report_listeners_lock:
        try:
            _agent_protocol_report_listeners.remove(listener)
        except ValueError:
            pass


def _notify_agent_protocol_report_listeners() -> None:
    with _agent_protocol_report_listeners_lock:
        listeners = list(_agent_protocol_report_listeners)
    for listener in listeners:
        try:
            listener()
        except Exception:
            pass


# ── Baseline ──────────────────────────────────────────────────────────────────

def upsert_baseline(
    agent_address: str,
    coin: str,
    baseline_agent_size: float,
    our_baseline_size: float,
    init_entry_px: Optional[float],
    init_pnl_pct: Optional[float],
    opened_at_init: int,
) -> None:
    """插入或替换基线记录（UNIQUE ON CONFLICT REPLACE）。"""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agent_baseline
               (agent_address, coin, baseline_agent_size, our_baseline_size,
                init_entry_px, init_pnl_pct, opened_at_init, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                agent_address, coin, baseline_agent_size, our_baseline_size,
                init_entry_px, init_pnl_pct, opened_at_init,
                datetime.utcnow().isoformat(),
            ),
        )


def get_baselines(agent_address: str) -> dict[str, dict]:
    """返回 {coin: {baseline_agent_size, our_baseline_size, init_entry_px, init_pnl_pct, opened_at_init}}。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_baseline WHERE agent_address=?", (agent_address,)
        ).fetchall()
        return {r["coin"]: dict(r) for r in rows}


def get_baselines_list(agent_address: str) -> list[dict]:
    """返回基线列表（供 CLI 展示）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_baseline WHERE agent_address=? ORDER BY coin",
            (agent_address,),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_baselines(agent_address: str) -> None:
    """删除某 agent 的所有基线记录。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM agent_baseline WHERE agent_address=?", (agent_address,))


def has_baseline(agent_address: str) -> bool:
    """检查某 agent 是否已有基线记录。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM agent_baseline WHERE agent_address=? LIMIT 1", (agent_address,)
        ).fetchone()
        return row is not None


# ── Events ────────────────────────────────────────────────────────────────────

def log_event(
    agent_address: str,
    coin: str,
    raw_payload: str,
    side: Optional[str] = None,
    fill_size: Optional[float] = None,
    fill_price: Optional[float] = None,
    tx_hash: Optional[str] = None,
    fill_tid: Optional[str] = None,
    process_key: Optional[str] = None,
    agent_pos_before: Optional[float] = None,
    agent_pos_after: Optional[float] = None,
) -> None:
    """记录 fill 事件；duplicate fill_tid 会被静默忽略（UNIQUE 约束）。"""
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO events
                   (agent_address, coin, side, fill_size, fill_price, tx_hash,
                    fill_tid, process_key, agent_pos_before, agent_pos_after, raw_payload, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    agent_address, coin, side, fill_size, fill_price, tx_hash,
                    fill_tid, process_key, agent_pos_before, agent_pos_after, raw_payload,
                    datetime.utcnow().isoformat(),
                ),
            )
    except sqlite3.IntegrityError:
        # duplicate fill_tid — keep the original raw event, but backfill the
        # order-level process key so upgraded databases can dedupe old rows.
        if process_key and fill_tid:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE events SET process_key=COALESCE(process_key, ?) WHERE fill_tid=?",
                    (process_key, fill_tid),
                )


def mark_baseline_init_seen(agent_address: str) -> None:
    """标记本进程本轮启动已检查/初始化过该 Agent baseline。"""
    with _baseline_init_seen_lock:
        _baseline_init_seen.add(agent_address)


def has_baseline_init_seen(agent_address: str) -> bool:
    """判断本进程本轮启动是否已检查/初始化过该 Agent baseline。"""
    with _baseline_init_seen_lock:
        return agent_address in _baseline_init_seen


def is_processed_fill_tid(tid: str) -> bool:
    """检查某个 fill_tid 是否已在 events 表中（存在即算，用于 WS 重连去重）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM events WHERE fill_tid=? LIMIT 1", (tid,)
        ).fetchone()
        return row is not None


def is_synced_fill_tid(tid: str) -> bool:
    """检查某个 fill_tid 是否已同步完成（sync_status='done'）。

    供 poller 做跨通道去重：区分「已记录但同步失败/超时（pending）」
    和「已成功同步（done）」，避免 pending 状态的 fill 被 poller 漏掉。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM events WHERE fill_tid=? AND sync_status='done' LIMIT 1", (tid,)
        ).fetchone()
        return row is not None


def is_synced_process_key(process_key: str) -> bool:
    """检查某个订单级处理键是否已有任意事件同步完成。"""
    if not process_key:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM events WHERE process_key=? AND sync_status='done' LIMIT 1",
            (process_key,),
        ).fetchone()
        return row is not None


def mark_fill_tid_synced(tid: str) -> None:
    """将 fill_tid 标记为同步完成（sync_status='done'）。

    在 delta sync 成功执行后调用（WS 和 poller 均需调用）。
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE events SET sync_status='done' WHERE fill_tid=?", (tid,)
        )


def mark_process_key_synced(process_key: str) -> None:
    """将同一订单级处理键下的所有事件标记为同步完成。"""
    if not process_key:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE events SET sync_status='done' WHERE process_key=?", (process_key,)
        )


def get_pending_moss_fill_events(limit: int = 100) -> list[dict]:
    """返回待回填的 Moss fill 事件，供服务重启后补同步。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT *
               FROM events
               WHERE (
                   fill_tid LIKE 'moss_fill_%'
                   OR process_key LIKE 'moss_order_%'
               )
                 AND sync_status='pending'
               ORDER BY id ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def has_recent_synced_event(coin: str, seconds: int = 10) -> bool:
    """检查某个 coin 在最近 N 秒内是否有任意通道成功同步过（sync_status='done'）。"""
    cutoff = (datetime.utcnow() - timedelta(seconds=seconds)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM events WHERE coin=? AND sync_status='done' AND created_at>=? LIMIT 1",
            (coin, cutoff),
        ).fetchone()
        return row is not None


def get_latest_synced_moss_fill_created_at() -> Optional[str]:
    """返回最近已同步 Moss fill 的服务端 created_at，用于 poller 重启恢复游标。"""
    timestamps: list[str] = []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT raw_payload, created_at
               FROM events
               WHERE (
                   fill_tid LIKE 'moss_fill_%'
                   OR process_key LIKE 'moss_order_%'
               )
                 AND sync_status='done'
               ORDER BY id DESC
               LIMIT 1000"""
        ).fetchall()

    for row in rows:
        try:
            payload = _json.loads(row["raw_payload"])
        except (_json.JSONDecodeError, TypeError):
            payload = {}
        nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        ts = (
            payload.get("created_at")
            or payload.get("createdAt")
            or payload.get("timestamp")
            or payload.get("event_time")
            or nested.get("created_at")
            or nested.get("createdAt")
            or nested.get("timestamp")
            or nested.get("event_time")
        )
        if isinstance(ts, str) and ts:
            timestamps.append(ts)

    return max(timestamps) if timestamps else None


def get_earliest_pending_moss_fill_created_at() -> Optional[str]:
    """返回最早 pending Moss fill 的服务端 created_at，用于重启后扩大轮询游标。"""
    timestamps: list[str] = []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT raw_payload, created_at
               FROM events
               WHERE (
                   fill_tid LIKE 'moss_fill_%'
                   OR process_key LIKE 'moss_order_%'
               )
                 AND sync_status='pending'
               ORDER BY id ASC
               LIMIT 1000"""
        ).fetchall()

    for row in rows:
        try:
            payload = _json.loads(row["raw_payload"])
        except (_json.JSONDecodeError, TypeError):
            payload = {}
        nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        ts = (
            payload.get("created_at")
            or payload.get("createdAt")
            or payload.get("timestamp")
            or payload.get("event_time")
            or nested.get("created_at")
            or nested.get("createdAt")
            or nested.get("timestamp")
            or nested.get("event_time")
        )
        if isinstance(ts, str) and ts:
            timestamps.append(ts)

    return min(timestamps) if timestamps else None


def moss_fill_cursor_with_overlap(cursor: str, overlap_seconds: int = 5) -> str:
    """将服务端 cursor 回退几秒，避免同秒多 fill 或 API 边界语义导致漏拉。"""
    try:
        dt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except ValueError:
        return cursor
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc) - timedelta(seconds=overlap_seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def default_moss_fill_cursor(lookback_seconds: int = 1200) -> str:
    """没有历史 fill 游标时，从短 lookback 开始，依赖 fill_tid 精确去重。"""
    return (datetime.now(timezone.utc) - timedelta(seconds=lookback_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ── Trades ────────────────────────────────────────────────────────────────────

def generate_client_trade_id() -> str:
    """生成新的稳定上报幂等键。"""
    return f"ct_{uuid.uuid4().hex}"


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat()


def _utcnow_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_rfc3339_utc(raw: object) -> str:
    if raw is None:
        return _utcnow_rfc3339()
    text = str(raw).strip()
    if not text:
        return _utcnow_rfc3339()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _utcnow_rfc3339()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_dict(data: dict) -> dict:
    return {k: v for k, v in data.items() if v is not None}


def _format_report_value(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _agent_protocol_report_enabled() -> bool:
    report_cfg = cfg.get_agent_protocol_report_config()
    return bool(report_cfg.get("enabled")) and bool(report_cfg.get("submit_trade_results", True))


def _build_agent_protocol_trade_event(trade: dict) -> dict:
    """Build a stable Agent Protocol result event from a persisted trade row."""
    client_trade_id = str(trade["client_trade_id"])
    event_id = f"agent-protocol:trade:{client_trade_id}"
    trace_id = f"agent-protocol:{cfg.get_or_create_skill_instance_id()}"
    status = str(trade.get("status") or "").lower()
    error_msg = str(trade.get("error_msg") or "").strip()
    is_error = status in {"error", "rejected"} or bool(error_msg)
    if is_error:
        text = "Agent 交易服务收到 Moss 事件触发，但本次仓位对齐交易执行失败，已记录错误信息。"
    elif status == "skipped":
        text = "Agent 交易服务收到 Moss 事件触发，当前仓位已满足跟单配置，无需额外下单。"
    else:
        text = "Agent 交易服务收到 Moss 事件触发，已按跟单配置执行仓位对齐交易，并记录本次交易结果。"

    moss_cfg = cfg.get_moss_source_config()
    trade_payload = _clean_dict({
        "client_trade_id": client_trade_id,
        "coin": trade.get("coin"),
        "symbol": trade.get("symbol"),
        "side": trade.get("side"),
        "status": trade.get("status"),
        "size": _format_report_value(trade.get("our_size")),
        "notional": _format_report_value(trade.get("our_usd")),
        "ref_price": _format_report_value(trade.get("ref_price")),
        "order_price": _format_report_value(trade.get("order_price")),
        "filled_price": _format_report_value(trade.get("filled_price")),
        "leverage": _format_report_value(trade.get("leverage")),
        "our_order_id": _format_report_value(trade.get("our_order_id")),
        "realized_pnl": _format_report_value(trade.get("realized_pnl")),
        "fee": _format_report_value(trade.get("fee")),
        "error_message": error_msg or None,
    })
    moss_signal = _clean_dict({
        "agent_id": moss_cfg.get("agent_id"),
        "source": trade.get("source"),
        "agent_fill_tid": _format_report_value(trade.get("agent_fill_tid")),
        "agent_event_id": _format_report_value(trade.get("agent_event_id")),
    })
    payload = {
        "source": "tool",
        "status": "error" if is_error else "ok",
        "tool_request_id": f"trade:{client_trade_id}",
        "text": text,
        "trade_service": _clean_dict({
            "service": "hyper-follow",
            "skill_instance_id": cfg.get_or_create_skill_instance_id(),
            "network": cfg.get_network(),
            "follow_ratio": _format_report_value(cfg.get("follow_ratio", 1.0)),
        }),
        "moss_signal": moss_signal,
        "trade": trade_payload,
    }
    return {
        "schema_version": "agent.collection.v2",
        "trace_id": trace_id,
        "type": "result",
        "event_id": event_id,
        "seq": int(trade["id"]),
        "ts": _to_rfc3339_utc(trade.get("created_at")),
        "payload": payload,
    }


def _enqueue_agent_protocol_report(conn: sqlite3.Connection, trade: dict) -> bool:
    """Insert an Agent Protocol outbox row for a trade if the feature is enabled."""
    if not _agent_protocol_report_enabled():
        return False

    event = _build_agent_protocol_trade_event(trade)
    now = _utcnow_iso()
    try:
        conn.execute(
            """INSERT INTO agent_protocol_reports
               (trade_id, client_trade_id, event_id, trace_id, seq, event_json,
                report_status, report_attempts, last_reported_at, last_error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, ?)""",
            (
                trade["id"],
                trade["client_trade_id"],
                event["event_id"],
                event["trace_id"],
                event["seq"],
                _json.dumps(event, separators=(",", ":"), sort_keys=True, ensure_ascii=False),
                now,
                now,
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def record_trade(
    source: str,
    agent_address: str,
    coin: str,
    side: str,
    our_size: float,
    our_usd: float,
    ref_price: float,
    status: str,
    order_price: Optional[float] = None,
    filled_price: Optional[float] = None,
    entry_price: Optional[float] = None,
    realized_pnl: Optional[float] = None,
    fee: Optional[float] = None,
    leverage: Optional[int] = None,
    error_msg: Optional[str] = None,
    symbol: Optional[str] = None,
    agent_tx_hash: Optional[str] = None,
    agent_fill_tid: Optional[str] = None,
    agent_event_id: Optional[str] = None,
    our_order_id: Optional[str] = None,
    baseline_agent_size: Optional[float] = None,
    agent_pos_before: Optional[float] = None,
    agent_delta: Optional[float] = None,
    agent_account_value: Optional[float] = None,
    agent_pos_pct: Optional[float] = None,
    our_pos_before: Optional[float] = None,
    our_pos_after: Optional[float] = None,
    our_account_value: Optional[float] = None,
    our_pos_pct: Optional[float] = None,
    client_trade_id: Optional[str] = None,
    enqueue_report: bool = True,
) -> int:
    client_trade_id = client_trade_id or generate_client_trade_id()
    created_at = _utcnow_iso()
    should_notify_reporter = False
    should_notify_agent_protocol_reporter = False
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (source, agent_address, coin, symbol, side,
                baseline_agent_size, agent_pos_before, agent_delta, agent_account_value, agent_pos_pct,
                our_pos_before, our_pos_after, our_account_value, our_pos_pct,
                our_size, our_usd, ref_price, order_price, filled_price,
                entry_price, realized_pnl, fee, leverage,
                status, error_msg, agent_tx_hash, agent_fill_tid, agent_event_id, our_order_id, client_trade_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source, agent_address, coin, symbol, side,
                baseline_agent_size, agent_pos_before, agent_delta, agent_account_value, agent_pos_pct,
                our_pos_before, our_pos_after, our_account_value, our_pos_pct,
                our_size, our_usd, ref_price, order_price, filled_price,
                entry_price, realized_pnl, fee, leverage,
                status, error_msg, agent_tx_hash, agent_fill_tid, agent_event_id, our_order_id, client_trade_id,
                created_at,
            ),
        )
        trade_id = cur.lastrowid
        if enqueue_report:
            conn.execute(
                """INSERT INTO trade_reports
                   (trade_id, client_trade_id, report_status, report_attempts, last_reported_at,
                    last_error, created_at, updated_at)
                   VALUES (?, ?, 'pending', 0, NULL, NULL, ?, ?)""",
                (trade_id, client_trade_id, created_at, created_at),
            )
            should_notify_reporter = True
        trade = {
            "id": trade_id,
            "source": source,
            "agent_address": agent_address,
            "coin": coin,
            "symbol": symbol,
            "side": side,
            "our_size": our_size,
            "our_usd": our_usd,
            "ref_price": ref_price,
            "order_price": order_price,
            "filled_price": filled_price,
            "entry_price": entry_price,
            "realized_pnl": realized_pnl,
            "fee": fee,
            "leverage": leverage,
            "status": status,
            "error_msg": error_msg,
            "agent_fill_tid": agent_fill_tid,
            "agent_event_id": agent_event_id,
            "our_order_id": our_order_id,
            "client_trade_id": client_trade_id,
            "created_at": created_at,
        }
        should_notify_agent_protocol_reporter = _enqueue_agent_protocol_report(conn, trade)

    if should_notify_reporter:
        _notify_trade_report_listeners()
    if should_notify_agent_protocol_reporter:
        _notify_agent_protocol_report_listeners()
    return trade_id


def get_trade_by_id(trade_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE id=? LIMIT 1",
            (trade_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def get_latest_trade_symbol(coin: str) -> Optional[str]:
    """返回最近一条该 coin 的非空 symbol，用于无事件路径补全交易对。"""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT symbol
               FROM trades
               WHERE coin=? AND symbol IS NOT NULL AND TRIM(symbol) != ''
               ORDER BY id DESC
               LIMIT 1""",
            (coin,),
        ).fetchone()
        if row is None:
            return None
        return str(row["symbol"])


def get_trade_report_by_trade_id(trade_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trade_reports WHERE trade_id=? LIMIT 1",
            (trade_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def get_pending_trade_reports(limit: int = 100) -> list[dict]:
    """返回待上报交易（pending + failed），按入队顺序扫描。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT
                   tr.id AS report_id,
                   tr.trade_id,
                   tr.client_trade_id,
                   tr.report_status,
                   tr.report_attempts,
                   tr.last_reported_at,
                   tr.last_error,
                   tr.created_at AS report_created_at,
                   tr.updated_at AS report_updated_at,
                   t.*
               FROM trade_reports tr
               JOIN trades t ON t.id = tr.trade_id
               WHERE tr.report_status IN ('pending', 'failed')
               ORDER BY tr.id ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_trade_report_done(client_trade_id: str) -> None:
    now = _utcnow_iso()
    with get_conn() as conn:
        conn.execute(
            """UPDATE trade_reports
               SET report_status='done',
                   last_reported_at=?,
                   last_error=NULL,
                   updated_at=?
               WHERE client_trade_id=?""",
            (now, now, client_trade_id),
        )


def mark_trade_report_failed(client_trade_id: str, error: str) -> None:
    now = _utcnow_iso()
    with get_conn() as conn:
        conn.execute(
            """UPDATE trade_reports
               SET report_status='failed',
                   report_attempts=report_attempts+1,
                   last_error=?,
                   updated_at=?
               WHERE client_trade_id=?""",
            (error, now, client_trade_id),
        )


# ── Agent Protocol Reports ───────────────────────────────────────────────────

def get_agent_protocol_report_by_trade_id(trade_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_protocol_reports WHERE trade_id=? LIMIT 1",
            (trade_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def get_pending_agent_protocol_reports(limit: int = 20, max_attempts: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT *
               FROM agent_protocol_reports
               WHERE report_status IN ('pending', 'failed')
                 AND report_attempts < ?
               ORDER BY id ASC
               LIMIT ?""",
            (max_attempts, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_agent_protocol_report_done(event_id: str) -> None:
    now = _utcnow_iso()
    with get_conn() as conn:
        conn.execute(
            """UPDATE agent_protocol_reports
               SET report_status='done',
                   last_reported_at=?,
                   last_error=NULL,
                   updated_at=?
               WHERE event_id=?""",
            (now, now, event_id),
        )


def mark_agent_protocol_report_failed(event_id: str, error: str) -> None:
    now = _utcnow_iso()
    with get_conn() as conn:
        conn.execute(
            """UPDATE agent_protocol_reports
               SET report_status='failed',
                   report_attempts=report_attempts+1,
                   last_error=?,
                   updated_at=?
               WHERE event_id=?""",
            (error, now, event_id),
        )


def mark_agent_protocol_report_dropped(event_id: str, error: str) -> None:
    now = _utcnow_iso()
    with get_conn() as conn:
        conn.execute(
            """UPDATE agent_protocol_reports
               SET report_status='dropped',
                   report_attempts=report_attempts+1,
                   last_error=?,
                   updated_at=?
               WHERE event_id=?""",
            (error, now, event_id),
        )


def is_our_order(order_id: str) -> bool:
    """检查某个 order_id 是否是我们自己下的单，用于过滤死循环。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM trades WHERE our_order_id=? LIMIT 1", (order_id,)
        ).fetchone()
        return row is not None


def get_trades(
    agent_address: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    with get_conn() as conn:
        if agent_address:
            rows = conn.execute(
                "SELECT * FROM trades WHERE agent_address=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (agent_address, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]


def get_trade_stats() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        filled = conn.execute("SELECT COUNT(*) FROM trades WHERE status='filled'").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM trades WHERE status='rejected'").fetchone()[0]
        errors = conn.execute("SELECT COUNT(*) FROM trades WHERE status='error'").fetchone()[0]
        skipped = conn.execute("SELECT COUNT(*) FROM trades WHERE status='skipped'").fetchone()[0]
        total_usd = conn.execute(
            "SELECT COALESCE(SUM(our_usd),0) FROM trades WHERE status='filled'"
        ).fetchone()[0]
        # 盈亏统计（仅有 realized_pnl 的记录）
        total_pnl = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) FROM trades WHERE realized_pnl IS NOT NULL"
        ).fetchone()[0]
        total_fee = conn.execute(
            "SELECT COALESCE(SUM(fee),0) FROM trades WHERE fee IS NOT NULL"
        ).fetchone()[0]
        # 胜率（realized_pnl > 0 的比例）
        pnl_trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE realized_pnl IS NOT NULL"
        ).fetchone()[0]
        winning = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE realized_pnl IS NOT NULL AND realized_pnl > 0"
        ).fetchone()[0]
        win_rate = round(winning / pnl_trades * 100, 1) if pnl_trades > 0 else 0.0
        # 跟单天数
        first_trade = conn.execute(
            "SELECT MIN(created_at) FROM trades WHERE status='filled'"
        ).fetchone()[0]
        days = 0
        if first_trade:
            from datetime import datetime
            first_dt = datetime.fromisoformat(first_trade)
            days = (datetime.utcnow() - first_dt).days + 1
        return {
            "total": total,
            "filled": filled,
            "rejected": rejected,
            "errors": errors,
            "skipped": skipped,
            "total_usd_traded": round(total_usd, 4),
            "total_realized_pnl": round(total_pnl, 4),
            "total_fee": round(total_fee, 4),
            "win_rate": win_rate,
            "pnl_trades": pnl_trades,
            "winning_trades": winning,
            "trading_days": days,
        }


# ── Account Snapshots ─────────────────────────────────────────────────────────

def record_account_snapshot(account_value: float, withdrawable: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO account_snapshots (account_value, withdrawable, created_at) VALUES (?,?,?)",
            (account_value, withdrawable, datetime.utcnow().isoformat()),
        )


def get_account_snapshots(limit: int = 60) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM account_snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_today_start_balance() -> float:
    """返回今日起始账户净值（今日第一条快照，或昨日最后一条）。"""
    from datetime import timezone
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    cutoff = today_start.isoformat()

    with get_conn() as conn:
        row = conn.execute(
            "SELECT account_value FROM account_snapshots "
            "WHERE created_at >= ? ORDER BY id ASC LIMIT 1",
            (cutoff,),
        ).fetchone()
        if row:
            return float(row[0])

        row = conn.execute(
            "SELECT account_value FROM account_snapshots "
            "WHERE created_at < ? ORDER BY id DESC LIMIT 1",
            (cutoff,),
        ).fetchone()
        return float(row[0]) if row else 0.0


def get_today_stats() -> dict:
    """返回今日交易统计（笔数、PnL、收益率%）。"""
    from datetime import timezone
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    cutoff = today_start.isoformat()

    with get_conn() as conn:
        today_trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status='filled' AND created_at >= ?",
            (cutoff,),
        ).fetchone()[0]

        today_pnl = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM trades "
            "WHERE realized_pnl IS NOT NULL AND created_at >= ?",
            (cutoff,),
        ).fetchone()[0]

    start_balance = get_today_start_balance()
    today_pnl_pct = (
        round(today_pnl / start_balance * 100, 2)
        if start_balance and start_balance > 0
        else 0.0
    )

    return {
        "today_trades": today_trades,
        "today_pnl": round(today_pnl, 4),
        "today_pnl_pct": today_pnl_pct,
    }


def get_today_fee() -> float:
    """返回今日手续费总额。"""
    from datetime import timezone
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    cutoff = today_start.isoformat()

    with get_conn() as conn:
        fee = conn.execute(
            "SELECT COALESCE(SUM(fee), 0) FROM trades WHERE fee IS NOT NULL AND created_at >= ?",
            (cutoff,),
        ).fetchone()[0]
    return round(fee, 4)


def get_recent_trades_with_status(limit: int = 5) -> list[dict]:
    """返回最近 N 笔交易，附加持仓状态（持仓中/已平仓/翻仓）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='filled' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        trades = []
        for r in rows:
            trade = dict(r)
            our_pos_before = trade.get("our_pos_before", 0) or 0
            our_pos_after = trade.get("our_pos_after", 0) or 0
            realized_pnl = trade.get("realized_pnl")

            # 判断交易类型：
            # 1. 有 realized_pnl 且 our_pos_after != 0 → 翻仓（平旧仓+开新仓）
            # 2. 有 realized_pnl 且 our_pos_after == 0 → 纯平仓
            # 3. 无 realized_pnl → 开仓/加仓
            if realized_pnl is not None:
                if abs(our_pos_after) > 0:
                    # 翻仓：仓位方向改变（正负号不同）或从0开始但有realized_pnl
                    if (our_pos_before * our_pos_after < 0) or (abs(our_pos_before) == 0):
                        trade["position_status"] = "翻仓"
                    else:
                        # 减仓但未平完
                        trade["position_status"] = "减仓"
                else:
                    trade["position_status"] = "已平仓"
            else:
                trade["position_status"] = "持仓中"

            trades.append(trade)
        return trades


# ── Alerts ────────────────────────────────────────────────────────────────────

def record_alert(alert_type: str, payload: dict) -> int:
    """写入一条告警，返回 alert id。"""
    now = datetime.utcnow()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (type, alert_date, created_at, payload, acknowledged) "
            "VALUES (?,?,?,?,0)",
            (alert_type, now.strftime("%Y-%m-%d"), now.isoformat(), _json.dumps(payload)),
        )
        return cur.lastrowid


def get_today_alert_count(alert_type: str, date_utc: Optional[str] = None) -> int:
    """返回指定日期（UTC, YYYY-MM-DD）某类型告警的数量，默认今天。"""
    if date_utc is None:
        date_utc = datetime.utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE type=? AND alert_date=?",
            (alert_type, date_utc),
        ).fetchone()
        return int(row[0]) if row else 0


def get_last_alert_at(alert_type: str) -> Optional[datetime]:
    """返回某类型最近一次告警的 UTC 时间（timezone-aware UTC）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT created_at FROM alerts WHERE type=? ORDER BY id DESC LIMIT 1",
            (alert_type,),
        ).fetchone()
        if not row:
            return None
        try:
            last_at = datetime.fromisoformat(row[0])
        except ValueError:
            return None
        if last_at.tzinfo is None:
            return last_at.replace(tzinfo=timezone.utc)
        return last_at.astimezone(timezone.utc)


def get_unread_alerts(limit: int = 100) -> list[dict]:
    """返回未读告警列表（payload 已 JSON 解析）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE acknowledged=0 ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = _json.loads(d["payload"])
            except Exception:
                pass
            out.append(d)
        return out


def get_recent_alerts(limit: int = 50) -> list[dict]:
    """返回最近 N 条告警（含已读，用于诊断）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = _json.loads(d["payload"])
            except Exception:
                pass
            out.append(d)
        return out


def mark_alerts_acknowledged(ids: list[int]) -> int:
    """标记指定 ID 为已读，返回受影响行数。"""
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE alerts SET acknowledged=1 WHERE id IN ({placeholders})",
            ids,
        )
        return cur.rowcount


def mark_all_alerts_acknowledged() -> int:
    """全部标记已读。"""
    with get_conn() as conn:
        cur = conn.execute("UPDATE alerts SET acknowledged=1 WHERE acknowledged=0")
        return cur.rowcount

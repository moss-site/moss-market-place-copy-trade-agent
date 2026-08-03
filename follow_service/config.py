import json
import os
import re
import tempfile
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

# ── 配置文件路径（支持多实例） ─────────────────────────────────────────────────
# 必须通过 --config 显式指定，禁止使用 config.json（无地址后缀）。
_config_path: Path | None = None

_DEFAULT_STATE_DIR = Path.home() / ".hyperliquid-copy-trade"
_CONFIG_NAME_RE = re.compile(r"^config_[0-9a-fA-F]{6}\.json$")
_RESERVED_CONFIG_NAMES = {"config.json", "config_default.json"}
_RUNTIME_STATE_KEYS = {
    "desired_state",
    "watchdog_enabled",
    "maintenance_mode",
    "maintenance_reason",
    "restart_cooldown_secs",
    "max_restarts_per_hour",
    "restart_attempts",
    "last_watchdog_check_at",
    "last_restart_at",
    "last_restart_error",
}

# ── 硬编码全局常量（不可配置） ────────────────────────────────────────────────
TESTNET_BUILDER_ADDRESS = "0x58ee238a5ab9e90d063a7b43d498782664dc5716"
MAINNET_BUILDER_ADDRESS = "0x7a4227ce12Cf0417FFcfcED77CA6A21cF399cEb0"
# Backward-compatible alias for the dev/testnet default.
BUILDER_ADDRESS = TESTNET_BUILDER_ADDRESS
# Hyperliquid SDK builder.f unit is tenths of a basis point: 50 = 5 bps = 0.05%.
BUILDER_FEE_RATE = 50


def get_state_dir() -> Path:
    """返回持久化配置目录，默认放在用户 Home 下，避免随 skill 升级/删除丢失。"""
    env = os.environ.get("FOLLOW_STATE_DIR")
    if env:
        return Path(env).expanduser()
    return _DEFAULT_STATE_DIR


def _expand_path(value: str) -> str:
    return str(Path(value).expanduser())


def _validate_config_path(path: Path) -> Path:
    """拒绝 config.json / config_default.json，要求 config_<6位>.json 命名。"""
    name = path.name
    if name in _RESERVED_CONFIG_NAMES:
        raise SystemExit(
            f"ERROR: 不允许使用 {name}。\n"
            f"请使用 config_<钱包地址后6位>.json 命名（例如 config_f4c4cb.json）。\n"
            f"运行 `python cli.py config wallet-generate` 自动创建。"
        )
    if not _CONFIG_NAME_RE.match(name):
        raise SystemExit(
            f"ERROR: 配置文件命名不规范: {name}\n"
            f"必须使用 config_<钱包地址后6位>.json 格式（例如 config_f4c4cb.json）。"
        )
    return path


def set_config_path(path: str | Path) -> None:
    """设置配置文件路径（供 CLI 和子进程入口调用）。"""
    global _config_path
    _config_path = _validate_config_path(Path(path))


def get_config_path() -> Path:
    """获取当前配置文件路径。必须通过 --config 或环境变量 FOLLOW_CONFIG 指定。"""
    if _config_path is not None:
        return _config_path
    env = os.environ.get("FOLLOW_CONFIG")
    if env:
        return _validate_config_path(Path(env))
    raise SystemExit(
        "ERROR: 未指定配置文件。\n"
        "请使用 --config 参数指定配置文件，例如：\n"
        "  python cli.py --config ~/.hyperliquid-copy-trade/f4c4cb/config_f4c4cb.json service start\n"
        "或设置环境变量 FOLLOW_CONFIG"
    )


def load_config() -> dict:
    with open(get_config_path()) as f:
        return _strip_runtime_state_keys(json.load(f))


def _strip_runtime_state_keys(data: dict) -> dict:
    """Runtime state belongs in service_state.json, never in config_<id>.json."""
    if isinstance(data, dict):
        for key in _RUNTIME_STATE_KEYS:
            data.pop(key, None)
    return data


def save_config(cfg: dict) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = _strip_runtime_state_keys(dict(cfg))
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def get(key: str, default=None):
    return load_config().get(key, default)


class _ConfigLock:
    def __init__(self, path: Path):
        self._path = path.with_suffix(path.suffix + ".lock")
        self._fh = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "w")
        if fcntl is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self._fh

    def __exit__(self, exc_type, exc, tb):
        if self._fh is not None:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
        return False


def update_config(mutator) -> dict:
    """在独占锁内执行读改写，避免并发 config set 丢写。"""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _ConfigLock(path):
        cfg = load_config()
        mutator(cfg)
        _strip_runtime_state_keys(cfg)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp_name, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return cfg


def get_moss_source_config(migrate_bot_id: bool = False) -> dict:
    """返回 moss_source 配置，并兼容旧配置中的 bot_id 字段。"""
    cfg_data = load_config()
    moss_cfg = cfg_data.get("moss_source", {})
    if not isinstance(moss_cfg, dict):
        return {}

    if not moss_cfg.get("agent_id") and moss_cfg.get("bot_id"):
        moss_cfg = dict(moss_cfg)
        moss_cfg["agent_id"] = moss_cfg["bot_id"]
        if migrate_bot_id:
            def _mutate(cfg_data: dict) -> None:
                current = cfg_data.get("moss_source", {})
                if isinstance(current, dict) and not current.get("agent_id") and current.get("bot_id"):
                    current = dict(current)
                    current["agent_id"] = current["bot_id"]
                    cfg_data["moss_source"] = current

            update_config(_mutate)

    return moss_cfg


def get_agent_protocol_report_config() -> dict:
    """返回 Agent Protocol Report 配置；旧配置默认关闭。"""
    report_cfg = load_config().get("agent_protocol_report", {})
    return report_cfg if isinstance(report_cfg, dict) else {}


def get_hyperliquid_auth_config() -> dict:
    """返回 Hyperliquid 授权模式配置；旧配置按 standard + builder fee 处理。"""
    auth_cfg = load_config().get("hyperliquid_auth", {})
    return auth_cfg if isinstance(auth_cfg, dict) else {}


def get_hyperliquid_auth_mode() -> str:
    mode = str(get_hyperliquid_auth_config().get("mode", "standard") or "standard").strip().lower()
    return mode if mode in {"standard", "contract_agent"} else "standard"


def is_contract_agent_auth_mode() -> bool:
    return get_hyperliquid_auth_mode() == "contract_agent"


def is_builder_fee_enabled() -> bool:
    """Builder fee follows the selected Hyperliquid auth mode.

    Standard EOA auth keeps builder fee enabled. Contract-agent auth disables
    builder fee until a contract-compatible builder authorization path exists.
    """
    return get_hyperliquid_auth_mode() == "standard"


def set_value(key: str, value) -> None:
    """设置配置项。支持 'a.b.c' 形式的嵌套路径写入；中间层不存在时自动创建为 dict。"""
    parts = key.split(".")
    if parts[0] in _RUNTIME_STATE_KEYS:
        raise SystemExit(
            f"ERROR: {parts[0]} 是运行态字段，不允许写入配置文件；"
            "请使用 `service watchdog ...` 命令管理自动重启状态。"
        )

    def _mutate(cfg: dict) -> None:
        node = cfg
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                node[p] = nxt
            node = nxt
        node[parts[-1]] = value

    update_config(_mutate)


def get_instance_id() -> str:
    """从 wallet_address 后 6 位生成实例 ID（小写）。

    与 config 文件命名 `config_<6位>.json` 保持一致。
    """
    wallet = get("wallet_address", "")
    if not wallet:
        return "default"
    return wallet[-6:].lower()


def get_instance_dir() -> Path:
    """返回当前实例的运行时目录。"""
    return get_state_dir() / get_instance_id()


def get_network() -> str:
    """根据 Hyperliquid API URL 推断当前网络。"""
    api_url = str(get("hl_api_url", "https://api.hyperliquid-testnet.xyz")).lower()
    if "hyperliquid-testnet" in api_url:
        return "testnet"
    if "api.hyperliquid.xyz" in api_url:
        return "mainnet"
    return "custom"


def get_builder_address() -> str:
    """返回当前网络对应的 Builder Fee 地址。"""
    if get_network() == "mainnet":
        return MAINNET_BUILDER_ADDRESS
    return TESTNET_BUILDER_ADDRESS


def get_or_create_skill_instance_id() -> str:
    """返回稳定 skill 实例 ID；缺失时写回配置。"""
    skill_instance_id = str(get("skill_instance_id", "") or "").strip()
    if skill_instance_id:
        return skill_instance_id

    new_value = f"skill_inst_{uuid.uuid4().hex[:12]}"

    def _mutate(current: dict) -> None:
        current.setdefault("skill_instance_id", new_value)

    updated = update_config(_mutate)
    return str(updated.get("skill_instance_id") or new_value)


def ensure_dirs() -> None:
    """创建运行时目录。"""
    cfg = load_config()

    def _mutate(current: dict) -> None:
        for key in ("log_dir", "db_path", "pid_file"):
            raw = current.get(key, "")
            if raw and raw != _expand_path(raw):
                current[key] = _expand_path(raw)

    cfg = update_config(_mutate)

    Path(cfg["log_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
    Path(cfg["db_path"]).expanduser().parent.mkdir(parents=True, exist_ok=True)
    Path(cfg["pid_file"]).expanduser().parent.mkdir(parents=True, exist_ok=True)

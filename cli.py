#!/usr/bin/env python3
"""
Hyperliquid Copy Trade — command-line interface.

Usage:
    python cli.py [--config <path>] service start
    python cli.py [--config <path>] service stop
    python cli.py [--config <path>] service status

    python cli.py [--config <path>] config show
    python cli.py [--config <path>] config set <key> <value>

    python cli.py [--config <path>] baseline show
    python cli.py [--config <path>] baseline reset

    python cli.py [--config <path>] trades [--limit N] [--agent <address>]
    python cli.py [--config <path>] stats
    python cli.py [--config <path>] balance [--limit N]

Multi-instance:
    python cli.py --config ~/.hyperliquid-copy-trade/f4c4cb/config_f4c4cb.json service start
    python cli.py --config ~/.hyperliquid-copy-trade/ee8867/config_ee8867.json service start
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Allow running from any directory
sys.path.insert(0, str(Path(__file__).parent))

from follow_service import config as cfg
from follow_service import database as db


DEFAULT_LOCAL_VERSION = "0.1.1"
DEFAULT_MAINNET_UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/moss-site/Moss-market-place-copy-trade-agent/main/VERSION.json"
DEFAULT_UPDATE_MANIFEST_URL = os.environ.get("HYPER_FOLLOW_UPDATE_MANIFEST_URL", "")
_EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


# ─── helpers ──────────────────────────────────────────────────────────────────

def _run_service(cmd: str) -> None:
    env = os.environ.copy()
    config_path = cfg.get_config_path()
    env["FOLLOW_CONFIG"] = str(config_path.resolve())
    subprocess.run(
        [sys.executable, "-m", "follow_service.main", cmd,
         "--config", str(config_path.resolve())],
        cwd=Path(__file__).parent,
        env=env,
    )


def _print_table(rows: list[dict], keys: list[str]) -> None:
    if not rows:
        print("(no records)")
        return
    widths = {k: max(len(k), max(len(str(r.get(k, ""))) for r in rows)) for k in keys}
    header = "  ".join(k.ljust(widths[k]) for k in keys)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r.get(k, "")).ljust(widths[k]) for k in keys))


# ─── commands ─────────────────────────────────────────────────────────────────

def _set_service_desired_state(value: str) -> None:
    from follow_service import watchdog

    watchdog.set_desired_state(value)


_RISK_PARAM_KEYS = {
    "follow_ratio",
    "stop_loss_pct",
    "take_profit_pct",
    "moss_source.agent_id",
}


def _arg_value(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    idx = args.index(name)
    if idx + 1 >= len(args):
        return None
    return args[idx + 1]


def _parse_percent_or_ratio(raw: str, *, ratio: bool) -> float:
    text = str(raw).strip()
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1].strip()
    value = float(text)
    if ratio:
        if is_percent or value > 1:
            value = value / 100.0
        if not 0 <= value <= 1:
            raise ValueError("follow ratio must be between 0% and 100%")
        return value
    if value < 0:
        value = abs(value)
    return value


def _risk_confirmation_snapshot(config_data: dict | None = None) -> dict:
    config_data = config_data or cfg.load_config()
    moss_cfg = config_data.get("moss_source", {})
    if not isinstance(moss_cfg, dict):
        moss_cfg = {}
    return {
        "agent_id": moss_cfg.get("agent_id", ""),
        "follow_ratio": config_data.get("follow_ratio", 1.0),
        "stop_loss_pct": config_data.get("stop_loss_pct", 0),
        "take_profit_pct": config_data.get("take_profit_pct", 0),
    }


def _risk_params_are_confirmed(config_data: dict | None = None) -> bool:
    config_data = config_data or cfg.load_config()
    confirmation = config_data.get("risk_params_confirmed")
    if not isinstance(confirmation, dict) or not confirmation.get("confirmed"):
        return False
    expected = _risk_confirmation_snapshot(config_data)
    return all(confirmation.get(k) == v for k, v in expected.items())


def _format_risk_params(config_data: dict | None = None) -> str:
    snap = _risk_confirmation_snapshot(config_data)
    try:
        ratio_pct = float(snap["follow_ratio"]) * 100
        ratio_text = f"{ratio_pct:g}%"
    except (TypeError, ValueError):
        ratio_text = str(snap["follow_ratio"])
    return (
        f"Agent={snap['agent_id'] or '(未设置)'}, "
        f"资金比例={ratio_text}, "
        f"止损={snap['stop_loss_pct']}%, "
        f"止盈={snap['take_profit_pct']}%"
    )


def _require_risk_params_confirmed() -> None:
    config_data = cfg.load_config()
    if _risk_params_are_confirmed(config_data):
        return
    config_path = cfg.get_config_path()
    print("ERROR: 启动跟单前必须先确认资金比例和止盈/止损配置。")
    print(f"当前配置: {_format_risk_params(config_data)}")
    print()
    print("请先在对话中询问用户：")
    print("  1. 您是否需要设置跟单的资金比例？范围 0%~100%，例如 30%、50%、100%。")
    print("  2. 您是否需要设置止损？范围 0%~100%，例如止损 20%。")
    print("  3. 您是否需要设置止盈？范围 0%~300%，例如止盈 20%。")
    print("  注意：止盈/止损按保证金盈亏百分比计算，不是价格涨跌幅；由轮询检查触发，非实时 tick，急速行情下实际盈亏可能超过设定值。")
    print()
    print("按用户回答执行确认命令（必须显式带三个答案）：")
    print(f"  .venv/bin/python cli.py --config {config_path} config confirm-risk --follow-ratio 100% --stop-loss 0 --take-profit 0")
    print("然后再启动服务。")
    raise SystemExit(2)


def cmd_service(args: list[str]) -> None:
    if not args:
        print("Usage: service <start|stop|status|pause|resume|switch|watchdog>")
        return
    subcmd = args[0]
    if subcmd == "watchdog":
        cmd_service_watchdog(args[1:])
    elif subcmd == "pause":
        cmd_service_pause()
    elif subcmd == "resume":
        cmd_service_resume()
    elif subcmd == "switch":
        cmd_service_switch()
    elif subcmd == "start":
        _require_risk_params_confirmed()
        _set_service_desired_state("running")
        _run_service("start")
    elif subcmd == "stop":
        _set_service_desired_state("stopped")
        _run_service("stop")
    else:
        _run_service(subcmd)


def cmd_service_pause() -> None:
    """暂停跟单：全平仓 + 停服务 + 清基线。"""
    _set_service_desired_state("paused")
    from follow_service.trader import close_all_positions
    from follow_service.moss_reporter import flush_trade_reports_once
    from follow_service.agent_protocol_reporter import flush_agent_protocol_reports_once

    db.init_db()

    # 1. 全平仓
    print("正在平仓所有持仓...")
    results = close_all_positions()
    if results:
        for r in results:
            pnl_str = f"pnl=${r['pnl']:.2f}" if r['pnl'] is not None else "pnl=N/A"
            print(f"  {r['coin']} {r['side']} size={r['size']:.6f} status={r['status']} {pnl_str}")
    else:
        print("  无持仓需要平仓")

    # 1.5 先同步 flush 一次 pending trade reports，避免 close_all 后立刻 stop 导致上报长期停在 pending
    try:
        flushed = flush_trade_reports_once()
        if flushed > 0:
            print(f"已立即触发 {flushed} 条待上报交易的同步上报。")
    except Exception as exc:
        print(f"WARNING: pending trade flush failed before stop: {exc}")

    try:
        flushed = flush_agent_protocol_reports_once()
        if flushed > 0:
            print(f"已立即触发 {flushed} 条 Agent Protocol Report 同步上报。")
    except Exception as exc:
        print(f"WARNING: pending Agent Protocol Report flush failed before stop: {exc}")

    # 2. 停服务
    print("正在停止服务...")
    _run_service("stop")

    # 3. 清基线
    moss_cfg = cfg.get_moss_source_config()
    agent_id = moss_cfg.get("agent_id", "")
    if agent_id:
        db.clear_baselines(agent_id)
        print(f"已清除 Moss 基线: {agent_id}")

    print(f"跟单已暂停。使用 'python cli.py --config {cfg.get_config_path()} service resume' 恢复。")


def cmd_service_resume() -> None:
    """恢复跟单：启动服务（自动重建基线）。"""
    _require_risk_params_confirmed()
    _set_service_desired_state("running")
    print("正在恢复跟单...")
    _run_service("start")
    print("服务已启动，基线将自动重建。")


def cmd_service_switch() -> None:
    """切换 Agent：暂停 + 提示配置新 agent_id。"""
    cmd_service_pause()
    print()
    print("请配置新的 Agent:")
    print(f"  方式 1: 编辑 {cfg.get_config_path()} 中 moss_source.agent_id")
    print(f"  方式 2: python cli.py --config {cfg.get_config_path()} config set moss_source '{{...}}'")
    print()
    print(f"配置完成后，运行 'python cli.py --config {cfg.get_config_path()} service resume' 恢复跟单。")


def _watchdog_interval_arg(args: list[str]) -> int:
    if "--interval" in args:
        idx = args.index("--interval")
        if idx + 1 < len(args):
            return int(args[idx + 1])
    return 60


def cmd_service_watchdog(args: list[str]) -> None:
    from follow_service import watchdog

    subcmd = args[0] if args else "status"
    cli_path = Path(__file__).resolve()
    if subcmd == "status":
        print(json.dumps(watchdog.status(), ensure_ascii=False, indent=2))
    elif subcmd == "enable":
        state = watchdog.enable_for_current_service()
        print(json.dumps({"watchdog_enabled": True, "state": state}, ensure_ascii=False, indent=2))
    elif subcmd == "disable":
        state = watchdog.set_watchdog_enabled(False)
        print(json.dumps({"watchdog_enabled": False, "state": state}, ensure_ascii=False, indent=2))
    elif subcmd == "check":
        result = watchdog.watchdog_check(cli_path=cli_path, python_path=sys.executable)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif subcmd == "install":
        interval = _watchdog_interval_arg(args[1:])
        result = watchdog.install(cli_path=cli_path, python_path=sys.executable, interval_secs=interval)
        state = watchdog.enable_for_current_service()
        print(json.dumps({"install": result, "state": state}, ensure_ascii=False, indent=2))
    elif subcmd == "uninstall":
        result = watchdog.uninstall()
        state = watchdog.set_watchdog_enabled(False)
        print(json.dumps({"uninstall": result, "state": state}, ensure_ascii=False, indent=2))
    else:
        print("Usage: service watchdog <install|uninstall|status|enable|disable|check> [--interval N]")


def cmd_check_auth() -> None:
    """校验主账号的 Agent 和 Builder 授权状态。"""
    from follow_service.preflight import check_authorization
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ok = check_authorization(raise_on_fail=False)
    if ok:
        print("授权校验通过：Agent 和 Builder 均已授权。")
    else:
        print("授权校验失败，请查看上方错误信息。")


def _arg_required(args: list[str], *names: str) -> str:
    for name in names:
        value = _arg_value(args, name)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise SystemExit(f"ERROR: missing required argument: {'/'.join(names)}")


def _is_evm_address(value: str) -> bool:
    return bool(_EVM_ADDRESS_RE.match(str(value or "").strip()))


def _require_evm_address(value: str, label: str) -> str:
    value = value.strip()
    if not _is_evm_address(value):
        raise SystemExit(f"ERROR: {label} must be a 0x EVM address")
    return value


def _has_flag(args: list[str], name: str) -> bool:
    return name in args


def cmd_config_init_contract_agent(args: list[str]) -> None:
    """Create a contract-agent config from explicit user-provided fields."""
    executor = _require_evm_address(
        _arg_required(args, "--executor", "--executor-address", "--wallet-address"),
        "executor",
    )
    main_address = _require_evm_address(
        _arg_required(args, "--main", "--main-address", "--agent-address"),
        "main address",
    )
    moss_agent_id = _arg_required(args, "--moss-agent", "--moss-agent-id", "--agent-id")
    if not moss_agent_id.startswith("agt_"):
        raise SystemExit("ERROR: --moss-agent must be an agt_xxx Moss Agent id")

    network = _arg_required(args, "--network").lower()
    if network not in {"testnet", "mainnet"}:
        raise SystemExit("ERROR: --network must be testnet or mainnet")

    raw_follow_ratio = _arg_required(args, "--follow-ratio")
    raw_stop_loss = _arg_required(args, "--stop-loss")
    raw_take_profit = _arg_required(args, "--take-profit")
    try:
        follow_ratio = _parse_percent_or_ratio(raw_follow_ratio, ratio=True)
        stop_loss_pct = _parse_percent_or_ratio(raw_stop_loss, ratio=False)
        take_profit_pct = _parse_percent_or_ratio(raw_take_profit, ratio=False)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"ERROR: invalid risk parameter: {exc}") from exc
    if not 0 <= stop_loss_pct <= 100:
        raise SystemExit("ERROR: --stop-loss must be between 0% and 100%")
    if not 0 <= take_profit_pct <= 300:
        raise SystemExit("ERROR: --take-profit must be between 0% and 300%")

    report_enabled = _has_flag(args, "--enable-agent-protocol-report")
    report_base_url = ""
    report_agent_address = ""
    report_executor_address = ""
    report_chain_id = 8453
    if report_enabled:
        report_base_url = _arg_required(args, "--report-base-url")
        report_agent_address = _require_evm_address(
            _arg_required(args, "--report-agent-address"),
            "report agent address",
        )
        report_executor_address = _require_evm_address(
            _arg_required(args, "--report-executor-address"),
            "report executor address",
        )
        if report_executor_address.lower() != executor.lower():
            raise SystemExit("ERROR: --report-executor-address must equal --executor")
        try:
            report_chain_id = int(_arg_required(args, "--report-chain-id"))
        except ValueError as exc:
            raise SystemExit("ERROR: --report-chain-id must be an integer") from exc
        if report_chain_id <= 0:
            raise SystemExit("ERROR: --report-chain-id must be positive")

    suffix = executor[-6:].lower()
    instance_dir = cfg.get_state_dir() / suffix
    config_path = instance_dir / f"config_{suffix}.json"
    if config_path.exists() and not _has_flag(args, "--force"):
        raise SystemExit(f"ERROR: config already exists: {config_path}\nUse --force to overwrite.")

    template_path = Path(__file__).parent / f"config_default.{network}.json"
    if not template_path.exists():
        raise SystemExit(f"ERROR: template not found: {template_path}")
    with open(template_path) as f:
        config_data = json.load(f)

    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "logs").mkdir(parents=True, exist_ok=True)
    try:
        instance_dir.chmod(0o700)
    except OSError:
        pass

    config_data["private_key"] = ""
    config_data["wallet_address"] = executor
    config_data["main_address"] = main_address
    config_data["follow_ratio"] = follow_ratio
    config_data["stop_loss_pct"] = stop_loss_pct
    config_data["take_profit_pct"] = take_profit_pct
    config_data["log_dir"] = str(instance_dir / "logs")
    config_data["db_path"] = str(instance_dir / "follow_agent.db")
    config_data["pid_file"] = str(instance_dir / "service.pid")
    config_data["hyperliquid_auth"] = {"mode": "contract_agent"}

    moss_cfg = config_data.setdefault("moss_source", {})
    moss_cfg["enabled"] = True
    moss_cfg["agent_id"] = moss_agent_id

    config_data["agent_protocol_report"] = {
        "enabled": report_enabled,
        "base_url": report_base_url,
        "agent_address": report_agent_address,
        "executor_address": report_executor_address,
        "chain_id": report_chain_id,
        "submit_trade_results": True,
        "batch_secs": 30,
        "batch_size": 20,
        "max_attempts": 10,
    }
    config_data["risk_params_confirmed"] = {
        "agent_id": moss_agent_id,
        "follow_ratio": follow_ratio,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "confirmed": True,
        "follow_ratio_answer": raw_follow_ratio,
        "stop_loss_answer": raw_stop_loss,
        "take_profit_answer": raw_take_profit,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(config_path, "w") as f:
        json.dump(config_data, f, indent=2)
    try:
        config_path.chmod(0o600)
    except OSError:
        pass

    print(f"contract-agent 配置已创建：{config_path}")
    print(f"  network       : {network}")
    print(f"  executor      : {executor}")
    print(f"  main_address  : {main_address}")
    print(f"  moss_agent    : {moss_agent_id}")
    print(f"  follow_ratio  : {follow_ratio * 100:g}%")
    print(f"  stop_loss     : {stop_loss_pct:g}%")
    print(f"  take_profit   : {take_profit_pct:g}%")
    print(f"  report        : {'enabled' if report_enabled else 'disabled'}")
    print()
    print("private_key 已故意留空。请用户自己写入配置后再执行：")
    print(f"  .venv/bin/python cli.py --config {config_path} config check-auth")
    print(f"  .venv/bin/python cli.py --config {config_path} service start")


def cmd_wallet_generate() -> None:
    """生成新的以太坊钱包，自动创建 config_<6位>.json 文件。"""
    from eth_account import Account

    acct = Account.create()
    private_key = acct.key.hex()
    wallet_address = acct.address
    suffix = wallet_address[-6:].lower()
    config_name = f"config_{suffix}.json"
    state_dir = cfg.get_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        state_dir.chmod(0o700)
    except OSError:
        pass
    instance_dir = state_dir / suffix
    instance_dir.mkdir(parents=True, exist_ok=True)
    try:
        instance_dir.chmod(0o700)
    except OSError:
        pass
    config_path = instance_dir / config_name

    # 检查文件是否已存在
    if config_path.exists():
        confirm = input(
            f"{config_name} 已存在，覆盖将丢失原有配置。确认覆盖？(yes/N): "
        ).strip().lower()
        if confirm != "yes":
            print("已取消。")
            return

    # 从 config_default.json 加载模板
    default_path = Path(__file__).parent / "config_default.json"
    if not default_path.exists():
        print(f"ERROR: 模板文件不存在: {default_path}")
        return

    with open(default_path) as f:
        config_data = json.load(f)

    # 写入钱包信息
    config_data["private_key"] = private_key
    config_data["wallet_address"] = wallet_address

    # 自动设置实例隔离路径
    config_data["log_dir"] = str(instance_dir / "logs")
    config_data["db_path"] = str(instance_dir / "follow_agent.db")
    config_data["pid_file"] = str(instance_dir / "service.pid")

    with open(config_path, "w") as f:
        json.dump(config_data, f, indent=2)
    try:
        config_path.chmod(0o600)
    except OSError:
        pass

    print(f"新钱包已生成并写入 {config_path}：")
    print(f"  wallet_address : {wallet_address}")
    print(f"  private_key    : {private_key[:6]}...{private_key[-4:]} (已保存)")
    print()
    print("网络配置：")
    print(f"  Hyperliquid API : {config_data['hl_api_url']}")
    print(f"  Moss API        : {config_data['moss_source']['base_url']}")
    print()
    print("后续步骤：")
    print("  1. 用主账号在 Hyperliquid 页面对该地址授权 Agent 和 Builder")
    print(f"  2. 运行 '.venv/bin/python cli.py --config {config_path} config check-auth' 验证授权")
    print(f"  3. 运行 '.venv/bin/python cli.py --config {config_path} moss register' 注册 Follower")
    print("  4. 配置 moss_source 后启动服务")
    print()
    print("授权页面：")
    print(f"  {config_data['hl_authorize_url']}/{wallet_address}")
    if config_data.get("moss_agent_list_url"):
        print("Moss Agent 列表：")
        print(f"  {config_data['moss_agent_list_url']}")


def cmd_config_show() -> None:
    c = cfg.load_config()
    # Mask private key
    display = {**c}
    if display.get("private_key"):
        pk = display["private_key"]
        display["private_key"] = pk[:6] + "..." + pk[-4:] if len(pk) > 10 else "***"
    print(json.dumps(display, indent=2))


def cmd_config_set(args: list[str]) -> None:
    if len(args) < 2:
        print("Usage: config set <key> <value>")
        return
    key, value = args[0], args[1]

    # 优先按 JSON 解析：int/float/bool/null/list/dict 自动得到原生类型；
    # 解析失败（如 0x 开头的地址、纯字符串 private_key 等）则保留为字符串。
    # 覆盖原有的显式数值 coercion（json.loads("3")==3, json.loads("1.5")==1.5）
    # 并修复 dict/list 类型（如 moss_source、allowed_coins）被当字符串存储的 bug。
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        parsed = value

    cfg.set_value(key, parsed)
    if key in _RISK_PARAM_KEYS:
        cfg.set_value("risk_params_confirmed", False)
        print(
            "Risk parameter confirmation cleared; run "
            "`config confirm-risk --follow-ratio <0%~100%> --stop-loss <0%~100%> --take-profit <0%~300%>` "
            "after user confirms all risk settings."
        )
    print(f"Set {key} = {parsed!r}")


def cmd_config_confirm_risk(args: list[str]) -> None:
    raw_follow_ratio = _arg_value(args, "--follow-ratio")
    raw_stop_loss = _arg_value(args, "--stop-loss")
    raw_take_profit = _arg_value(args, "--take-profit")
    if raw_follow_ratio is None or raw_stop_loss is None or raw_take_profit is None:
        raise SystemExit(
            "ERROR: confirm-risk requires explicit user answers for all risk params:\n"
            "  config confirm-risk --follow-ratio <0%~100%> --stop-loss <0%~100%> --take-profit <0%~300%>\n"
            "Required prompts:\n"
            "  1. 资金比例请输入 0%~100%，例如 30%、50%、100%。\n"
            "  2. 止损请输入 0%~100%，例如止损 20%。\n"
            "  3. 止盈请输入 0%~300%，例如止盈 20%。\n"
            "  注意：止盈/止损按保证金盈亏百分比计算，不是价格涨跌幅；由轮询检查触发，非实时 tick，急速行情下实际盈亏可能超过设定值。"
        )
    try:
        follow_ratio = _parse_percent_or_ratio(raw_follow_ratio, ratio=True)
        stop_loss_pct = _parse_percent_or_ratio(raw_stop_loss, ratio=False)
        take_profit_pct = _parse_percent_or_ratio(raw_take_profit, ratio=False)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"ERROR: invalid risk parameter: {exc}") from exc
    if not 0 <= stop_loss_pct <= 100:
        raise SystemExit("ERROR: stop-loss must be between 0% and 100%.")
    if not 0 <= take_profit_pct <= 300:
        raise SystemExit("ERROR: take-profit must be between 0% and 300%.")

    cfg.set_value("follow_ratio", follow_ratio)
    cfg.set_value("stop_loss_pct", stop_loss_pct)
    cfg.set_value("take_profit_pct", take_profit_pct)
    config_data = cfg.load_config()
    snap = _risk_confirmation_snapshot(config_data)
    if not snap.get("agent_id"):
        raise SystemExit("ERROR: moss_source.agent_id is not set; configure the Agent before confirming risk params.")
    confirmation = {
        **snap,
        "confirmed": True,
        "follow_ratio_answer": str(raw_follow_ratio),
        "stop_loss_answer": str(raw_stop_loss),
        "take_profit_answer": str(raw_take_profit),
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    cfg.set_value("risk_params_confirmed", confirmation)
    print("Risk parameters confirmed for service start:")
    print(f"  {_format_risk_params(cfg.load_config())}")


def cmd_baseline_show() -> None:
    """展示当前基线快照状态。"""
    db.init_db()
    moss_cfg = cfg.get_moss_source_config()
    agent_address = moss_cfg.get("agent_id", "")
    if not agent_address:
        print("No moss_source.agent_id configured.")
        return
    rows = db.get_baselines_list(agent_address)
    if not rows:
        print(f"No baseline found for {agent_address}.")
        print("Baseline is initialized on service start.")
        return
    print(f"Baseline for agent: {agent_address}")
    print()
    keys = ["coin", "baseline_agent_size", "our_baseline_size", "init_pnl_pct", "opened_at_init", "created_at"]
    _print_table(rows, keys)


def cmd_baseline_reset() -> None:
    """清除基线，下次服务启动时将重新初始化。"""
    moss_cfg = cfg.get_moss_source_config()
    agent_address = moss_cfg.get("agent_id", "")
    if not agent_address:
        print("No moss_source.agent_id configured.")
        return
    print(f"Clearing baseline for {agent_address}...")
    db.init_db()
    db.clear_baselines(agent_address)
    print("Baseline cleared. It will be re-initialized on next service start.")
    print(
        "Restart the service: "
        f"python cli.py --config {cfg.get_config_path()} service stop && "
        f"python cli.py --config {cfg.get_config_path()} service start"
    )


def cmd_trades(args: list[str]) -> None:
    db.init_db()
    limit = 20
    agent_address = None
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--agent" and i + 1 < len(args):
            agent_address = args[i + 1]
            i += 2
        else:
            i += 1

    trades = db.get_trades(agent_address=agent_address, limit=limit)
    keys = ["id", "source", "coin", "side", "our_size", "our_usd", "ref_price",
            "filled_price", "leverage", "status", "agent_delta", "baseline_agent_size", "created_at"]
    _print_table(trades, keys)


def cmd_stats() -> None:
    db.init_db()

    # Agent 名称
    moss_cfg = cfg.get_moss_source_config()
    agent_name = moss_cfg.get("agent_name", "")
    if not agent_name and moss_cfg.get("agent_id"):
        try:
            from follow_service.moss_client import MossClient
            client = MossClient(
                base_url=moss_cfg.get("base_url", ""),
                agent_id=moss_cfg["agent_id"],
            )
            info = client.get_agent_info()
            agent_data = info.get("bot", {})
            agent_name = agent_data.get("name", "")
            if agent_name:
                ms = dict(moss_cfg)
                ms["agent_name"] = agent_name
                cfg.set_value("moss_source", ms)
        except Exception:
            pass

    if agent_name:
        print(f"  agent: {agent_name} ({moss_cfg.get('agent_id', '')})")
    elif moss_cfg.get("agent_id"):
        print(f"  agent: {moss_cfg['agent_id']}")

    # 全量统计
    stats = db.get_trade_stats()
    print("--- 全量 ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    # 今日统计
    today = db.get_today_stats()
    print("--- 今日 ---")
    for k, v in today.items():
        print(f"  {k}: {v}")


def cmd_dashboard() -> None:
    """展示完整的 Agent 数据看板。"""
    from follow_service.trader import get_current_positions
    import subprocess

    db.init_db()

    # 1. 账户概览
    print("=" * 60)
    print("Agent 数据看板")
    print("=" * 60)

    wallet_address = cfg.get("wallet_address", "")
    main_address = cfg.get("main_address") or wallet_address
    if wallet_address:
        masked = wallet_address[:6] + "..." + wallet_address[-4:]
        print(f"\nAgent 钱包地址: {masked}")

    # 服务状态
    try:
        env = os.environ.copy()
        env["FOLLOW_CONFIG"] = str(cfg.get_config_path().resolve())
        result = subprocess.run(
            [sys.executable, "-m", "follow_service.main", "status",
             "--config", str(cfg.get_config_path().resolve())],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            env=env,
        )
        stdout = result.stdout.lower()
        if result.returncode != 0:
            status = "未知"
        elif "running" in stdout:
            status = "运行中"
        elif "stopped" in stdout:
            status = "已暂停"
        else:
            status = "未知"
    except Exception:
        status = "未知"
    print(f"当前跟单状态: {status}")

    # 账户余额
    try:
        pos_data = get_current_positions(main_address)
        account_value = pos_data["account_value"]
        withdrawable = pos_data["withdrawable"]
        positions = pos_data["positions"]
        print(f"跟单资金总额: {account_value:.2f} USDC")
        print(f"可用余额: {withdrawable:.2f} USDC")
    except Exception as e:
        print(f"无法获取账户余额: {e}")
        account_value = 0
        withdrawable = 0
        positions = {}

    # 2. 今日 P&L
    print("\n" + "-" * 60)
    print("今日 P&L")
    print("-" * 60)

    today_stats = db.get_today_stats()
    today_fee = db.get_today_fee()
    print(f"今日盈亏: {today_stats['today_pnl']:.4f} USDC ({today_stats['today_pnl_pct']:+.2f}%)")
    print(f"今日手续费: {today_fee:.4f} USDC")

    # 已实现 / 未实现盈亏
    total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions.values())
    print(f"已实现盈亏（今日）: {today_stats['today_pnl']:.4f} USDC")
    print(f"未实现盈亏（当前持仓）: {total_unrealized:.4f} USDC")

    # 3. 当前持仓
    if positions:
        print("\n" + "-" * 60)
        print("当前持仓")
        print("-" * 60)
        for coin, pos in positions.items():
            side = "做多" if pos["size"] > 0 else "做空"
            print(f"{coin} {side} | 仓位: {abs(pos['size']):.6f} | 开仓价: {pos['entry_px']:.2f} | 杠杆: {pos['leverage']}x | 未实现盈亏: {pos['unrealized_pnl']:.4f} USDC")
    else:
        print("\n当前无持仓")

    # 4. 近期交易记录（最近 5 条）
    print("\n" + "-" * 60)
    print("近期交易记录（最近 5 条）")
    print("-" * 60)

    recent_trades = db.get_recent_trades_with_status(limit=5)
    if recent_trades:
        for t in recent_trades:
            time_str = t["created_at"][:19].replace("T", " ")
            side_str = "做多" if t["side"] == "buy" else "做空"
            pnl_str = f"{t['realized_pnl']:.4f}" if t.get("realized_pnl") is not None else "N/A"
            leverage_str = f"{t['leverage']}x" if t.get('leverage') else "N/A"
            print(
                f"{time_str} | {t['coin']} {side_str} | "
                f"开仓价: {t['filled_price']:.2f} | 仓位: {t['our_size']:.6f} | "
                f"杠杆: {leverage_str} | 盈亏: {pnl_str} | {t['position_status']}"
            )
    else:
        print("暂无交易记录")

    # 5. 跟单绩效汇总
    print("\n" + "-" * 60)
    print("跟单绩效汇总")
    print("-" * 60)

    stats = db.get_trade_stats()
    print(f"总盈亏: {stats['total_realized_pnl']:.4f} USDC")
    print(f"总胜率: {stats['win_rate']:.1f}%")
    print(f"跟单天数: {stats['trading_days']} 天")
    print(f"累计交易笔数: {stats['filled']} 笔")

    # Agent 信息
    moss_cfg = cfg.get_moss_source_config()
    agent_name = moss_cfg.get("agent_name", "")
    if agent_name:
        print(f"\n当前跟单 Agent: {agent_name}")
    elif moss_cfg.get("agent_id"):
        print(f"\n当前跟单 Agent: {moss_cfg['agent_id']}")

    print("\n" + "=" * 60)


def cmd_balance(args: list[str]) -> None:
    db.init_db()
    limit = 20
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        else:
            i += 1
    rows = db.get_account_snapshots(limit=limit)
    _print_table(rows, ["id", "account_value", "withdrawable", "created_at"])


def cmd_alerts_list(args: list[str]) -> None:
    db.init_db()
    unread_only = "--unread" in args
    as_json = "--json" in args
    limit = 50
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        else:
            i += 1

    if unread_only:
        rows = db.get_unread_alerts()[:limit]
    else:
        rows = db.get_recent_alerts(limit=limit)

    if as_json:
        # payload 已是 JSON 字符串，反序列化为对象
        out = []
        for r in rows:
            item = dict(r)
            if isinstance(item.get("payload"), str):
                try:
                    item["payload"] = json.loads(item["payload"])
                except json.JSONDecodeError:
                    pass
            out.append(item)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    if not rows:
        print("(no alerts)")
        return
    keys = ["id", "type", "alert_date", "acknowledged", "created_at", "payload"]
    _print_table(rows, keys)


def cmd_alerts_ack(args: list[str]) -> None:
    db.init_db()
    if not args:
        print("Usage: alerts ack <id> [<id> ...]")
        return
    ids = [int(x) for x in args]
    n = db.mark_alerts_acknowledged(ids)
    print(f"已标记 {n} 条为已读。")


def cmd_alerts_ack_all() -> None:
    db.init_db()
    n = db.mark_all_alerts_acknowledged()
    print(f"已标记全部 {n} 条为已读。")


def cmd_moss_register() -> None:
    """注册为 Moss follower（钱包签名鉴权）。"""
    from follow_service.moss_client import MossClient

    moss_cfg = cfg.get_moss_source_config()
    base_url = moss_cfg.get("base_url", "")
    agent_id = moss_cfg.get("agent_id", "")
    private_key = cfg.get("private_key", "")
    wallet_address = cfg.get("wallet_address", "")
    main_address = cfg.get("main_address", "")
    builder_address = cfg.get_builder_address()

    if not base_url:
        print("ERROR: moss_source.base_url not configured")
        return
    if not private_key:
        print("ERROR: private_key not configured")
        return

    client = MossClient(
        base_url=base_url, agent_id=agent_id,
        private_key=private_key, wallet_address=wallet_address,
        main_address=main_address,
        builder_address=builder_address,
    )

    print(f"Registering follower: wallet={client._wallet_address} main={client._main_address}")
    try:
        result = client.register_follower()
        print(f"OK: follower_id={result.get('follower_id')} status={result.get('status')}")
    except Exception as e:
        print(f"ERROR: {e}")


# ─── update commands ──────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project_root() -> Path:
    return Path(__file__).parent.resolve()


def _version_file_candidates(root: Path | None = None) -> list[Path]:
    base = root or _project_root()
    return [base / "VERSION.json", base / "follow_service" / "VERSION.json"]


def _version_file_path(root: Path | None = None) -> Path:
    for path in _version_file_candidates(root):
        if path.exists():
            return path
    return _version_file_candidates(root)[-1]


def _coerce_version_metadata(data) -> dict:
    if isinstance(data, dict):
        version = str(data.get("version") or data.get("latest_version") or DEFAULT_LOCAL_VERSION)
        result = dict(data)
        result["version"] = version
        return result
    if isinstance(data, str):
        return {"version": data.strip() or DEFAULT_LOCAL_VERSION}
    return {"version": DEFAULT_LOCAL_VERSION}


def _load_version_metadata(root: Path | None = None) -> dict:
    path = _version_file_path(root)
    try:
        raw = path.read_text().strip()
    except OSError:
        return {"version": DEFAULT_LOCAL_VERSION, "missing_version_file": True}
    try:
        return _coerce_version_metadata(json.loads(raw))
    except json.JSONDecodeError:
        # Backward-compatible with old VERSION.json files that contained only "0.1.1".
        return _coerce_version_metadata(raw)


def _local_version() -> str:
    return str(_load_version_metadata().get("version") or DEFAULT_LOCAL_VERSION)


def _manifest_version(manifest: dict) -> str:
    return str(manifest.get("latest_version") or manifest.get("version") or "")


def _update_state_path() -> Path:
    return cfg.get_instance_dir() / "update_state.json"


def _load_update_state() -> dict:
    current_version = _local_version()
    path = _update_state_path()
    if not path.exists():
        return {"installed_version": current_version, "ignored_versions": []}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"installed_version": current_version, "ignored_versions": []}
        data["installed_version"] = current_version
        data.setdefault("ignored_versions", [])
        return data
    except (OSError, json.JSONDecodeError):
        return {"installed_version": current_version, "ignored_versions": []}


def _save_update_state(state: dict) -> None:
    path = _update_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _find_arg(args: list[str], name: str) -> str | None:
    if name in args:
        idx = args.index(name)
        if idx + 1 < len(args):
            return args[idx + 1]
    return None


def _has_flag(args: list[str], name: str) -> bool:
    return name in args


def _print_start_risk_confirmation_notice() -> None:
    """Remind operators to confirm risk params before restarting after update."""
    try:
        follow_ratio = float(cfg.get("follow_ratio", 1.0))
    except (TypeError, ValueError):
        follow_ratio = 1.0
    stop_loss_pct = cfg.get("stop_loss_pct", 0)
    take_profit_pct = cfg.get("take_profit_pct", 0)
    print("Risk parameters must be confirmed before starting service:")
    print(f"  follow_ratio: {follow_ratio:g} ({follow_ratio * 100:g}%)")
    print(f"  stop_loss_pct: {stop_loss_pct}")
    print(f"  take_profit_pct: {take_profit_pct}")
    print("Ask the user whether to keep or change follow ratio and SL/TP, then run service start.")


def _config_update_manifest_url() -> str:
    try:
        value = cfg.get("update_manifest_url", "")
        if value:
            return str(value)
        network = cfg.get_network()
    except SystemExit:
        return ""
    if network == "mainnet":
        return DEFAULT_MAINNET_UPDATE_MANIFEST_URL
    if network == "testnet":
        packaged_manifest = _project_root() / "dist" / "moss-trading-skill" / "VERSION.json"
        if packaged_manifest.exists():
            return str(packaged_manifest)
        local_manifest = _project_root() / "VERSION.json"
        if local_manifest.exists():
            return str(local_manifest)
    return ""


def _manifest_url(args: list[str]) -> str:
    return _find_arg(args, "--manifest-url") or _config_update_manifest_url() or DEFAULT_UPDATE_MANIFEST_URL


def _load_json_path(path: str | Path) -> dict:
    with open(Path(path).expanduser()) as f:
        return json.load(f)


def _load_manifest(args: list[str]) -> dict:
    manifest_file = _find_arg(args, "--manifest-file")
    if manifest_file:
        return _load_json_path(manifest_file)

    url = _manifest_url(args)
    if not url:
        raise SystemExit(
            "ERROR: update manifest URL is not configured.\n"
            "Pass --manifest-url <url>, set update_manifest_url in config, "
            "or set HYPER_FOLLOW_UPDATE_MANIFEST_URL."
        )

    # Allow local file paths in update_manifest_url for testnet/dev deployments.
    if "://" not in url:
        return _load_json_path(url)

    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_package(package_url: str, expected_sha256: str | None) -> Path:
    tmp = tempfile.NamedTemporaryFile(prefix="hyper-follow-update-", suffix=".tar.gz", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        with urllib.request.urlopen(package_url, timeout=60) as resp, open(tmp_path, "wb") as f:
            shutil.copyfileobj(resp, f)
        if expected_sha256:
            actual = _sha256_file(tmp_path)
            if actual.lower() != expected_sha256.lower():
                raise SystemExit(f"ERROR: package sha256 mismatch: expected {expected_sha256}, got {actual}")
        return tmp_path
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _github_source_archive(package_url: str) -> tuple[str, str | None] | None:
    """Convert GitHub repo/tree URLs into a tarball URL plus optional subdir."""
    parsed = urllib.parse.urlparse(package_url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None

    owner, repo = parts[:2]
    ref = "main"
    subdir = None
    if len(parts) >= 5 and parts[2] == "tree":
        ref = parts[3]
        subdir = "/".join(parts[4:]) or None
    elif len(parts) > 2:
        return None

    archive_url = (
        f"https://codeload.github.com/{urllib.parse.quote(owner)}/"
        f"{urllib.parse.quote(repo)}/tar.gz/{urllib.parse.quote(ref, safe='')}"
    )
    return archive_url, subdir


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract_tar(tar: tarfile.TarFile, dest: Path) -> None:
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest_resolved) + os.sep):
            raise SystemExit(f"ERROR: unsafe path in update package: {member.name}")
    if sys.version_info >= (3, 12):
        tar.extractall(dest, filter="data")
    else:
        tar.extractall(dest)


def _find_update_source_root(root: Path) -> Path:
    root = root.expanduser().resolve()
    candidates = [root]
    if root.is_dir():
        candidates.extend(p.parent for p in root.rglob("cli.py"))
    for c in candidates:
        if (c / "cli.py").exists() and (c / "follow_service").is_dir():
            return c
    raise SystemExit("ERROR: update source does not contain cli.py and follow_service/.")


def _extract_package(package: Path, preferred_subdir: str | None = None) -> Path:
    extract_dir = Path(tempfile.mkdtemp(prefix="hyper-follow-update-src-"))
    with tarfile.open(package, "r:*") as tar:
        _safe_extract_tar(tar, extract_dir)
    if preferred_subdir:
        for top in [extract_dir, *[p for p in extract_dir.iterdir() if p.is_dir()]]:
            candidate = top / preferred_subdir
            if candidate.exists():
                return _find_update_source_root(candidate)
    return _find_update_source_root(extract_dir)


def _resolve_update_source(package_url: str, expected_sha256: str | None = None) -> Path:
    """Resolve package_url to a source tree. Supports tarballs, local dirs, and GitHub tree URLs."""
    parsed = urllib.parse.urlparse(package_url)

    if parsed.scheme in {"", "file"}:
        raw_path = parsed.path if parsed.scheme == "file" else package_url
        path = Path(urllib.parse.unquote(raw_path)).expanduser()
        if path.is_dir():
            return _find_update_source_root(path)
        if path.is_file():
            if expected_sha256:
                actual = _sha256_file(path)
                if actual.lower() != expected_sha256.lower():
                    raise SystemExit(f"ERROR: package sha256 mismatch: expected {expected_sha256}, got {actual}")
            return _extract_package(path)
        raise SystemExit(f"ERROR: package_url path not found: {path}")

    github_source = _github_source_archive(package_url)
    if github_source:
        archive_url, subdir = github_source
        package = _download_package(archive_url, expected_sha256)
        return _extract_package(package, preferred_subdir=subdir)

    package = _download_package(package_url, expected_sha256)
    return _extract_package(package)


def _service_status_text() -> str:
    try:
        config_path = cfg.get_config_path().resolve()
    except SystemExit:
        return "unknown (no --config)"
    env = os.environ.copy()
    env["FOLLOW_CONFIG"] = str(config_path)
    result = subprocess.run(
        [sys.executable, "-m", "follow_service.main", "status", "--config", str(config_path)],
        cwd=Path(__file__).parent,
        env=env,
        capture_output=True,
        text=True,
    )
    return (result.stdout + result.stderr).strip()


def _service_is_running() -> bool:
    return "running" in _service_status_text().lower()


def _service_is_running_quick() -> bool:
    try:
        pid_file = Path(cfg.get("pid_file", "")).expanduser()
        if not pid_file.exists():
            return False
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _wait_for_service_stopped(timeout_secs: int = 30) -> bool:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if not _service_is_running_quick():
            return True
        time.sleep(0.5)
    return not _service_is_running_quick()


def _call_service(cmd: str) -> None:
    config_path = cfg.get_config_path().resolve()
    env = os.environ.copy()
    env["FOLLOW_CONFIG"] = str(config_path)
    result = subprocess.run(
        [sys.executable, "-m", "follow_service.main", cmd, "--config", str(config_path)],
        cwd=Path(__file__).parent,
        env=env,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ERROR: service {cmd} failed with exit code {result.returncode}")


def _current_config_path_or_none() -> Path | None:
    try:
        return cfg.get_config_path().expanduser().resolve()
    except SystemExit:
        return None


def _backup_path(src: Path, backup_root: Path, label: str) -> None:
    if not src.exists():
        return
    dst = backup_root / label
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_copy_ignore)
    else:
        shutil.copy2(src, dst)


def _copy_ignore(_dir: str, names: list[str]) -> set[str]:
    return {name for name in names if name.startswith("._") or name == "__pycache__"}


def _create_update_backup(version: str, service_was_running: bool) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = cfg.get_state_dir() / "backups" / f"update-{ts}"
    project_root = _project_root()
    backup_root.mkdir(parents=True, exist_ok=True)

    for name in [
        "cli.py",
        "VERSION.json",
        "requirements.txt",
        "config_default.json",
        "config_default.mainnet.json",
        "config_default.testnet.json",
        "follow_service/VERSION.json",
    ]:
        _backup_path(project_root / name, backup_root / "code", name)
    _backup_path(project_root / "follow_service", backup_root / "code", "follow_service")
    _backup_path(project_root / ".claude" / "skills" / "hyper-follow" / "SKILL.md", backup_root / "code", ".claude/skills/hyper-follow/SKILL.md")

    config_path = _current_config_path_or_none()
    if config_path:
        _backup_path(config_path, backup_root / "instance", config_path.name)
        try:
            db_path = Path(cfg.get("db_path", "")).expanduser()
            _backup_path(db_path, backup_root / "instance", db_path.name)
        except Exception:
            pass

    meta = {
        "created_at": _utc_now(),
        "from_version": _local_version(),
        "to_version": version,
        "project_root": str(project_root),
        "config_path": str(config_path) if config_path else None,
        "service_was_running": service_was_running,
    }
    with open(backup_root / "backup_meta.json", "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return backup_root


def _copy_update_files(src_root: Path, project_root: Path) -> list[str]:
    changed: list[str] = []
    for name in [
        "cli.py",
        "VERSION.json",
        "requirements.txt",
        "config_default.json",
        "config_default.mainnet.json",
        "config_default.testnet.json",
        "follow_service/VERSION.json",
    ]:
        src = src_root / name
        if src.exists():
            shutil.copy2(src, project_root / name)
            changed.append(name)

    src_follow = src_root / "follow_service"
    if src_follow.is_dir():
        dst_follow = project_root / "follow_service"
        if dst_follow.exists():
            shutil.rmtree(dst_follow)
        shutil.copytree(src_follow, dst_follow, ignore=_copy_ignore)
        changed.append("follow_service/")

    src_skill = src_root / ".claude" / "skills" / "hyper-follow" / "SKILL.md"
    if not src_skill.exists():
        src_skill = src_root / "SKILL.md"
    if src_skill.exists():
        dst_skill = project_root / ".claude" / "skills" / "hyper-follow" / "SKILL.md"
        dst_skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_skill, dst_skill)
        changed.append(".claude/skills/hyper-follow/SKILL.md")

    return changed


def _compile_core(project_root: Path) -> None:
    targets = [project_root / "cli.py"] + [p for p in sorted((project_root / "follow_service").glob("*.py")) if not p.name.startswith("._")]
    result = subprocess.run([sys.executable, "-m", "py_compile", *map(str, targets)], text=True)
    if result.returncode != 0:
        raise SystemExit("ERROR: py_compile failed after update. Run rollback if needed.")


def _latest_backup_dir() -> Path | None:
    root = cfg.get_state_dir() / "backups"
    if not root.exists():
        return None
    backups = sorted([p for p in root.iterdir() if p.is_dir() and p.name.startswith("update-")])
    return backups[-1] if backups else None


def cmd_update_status() -> None:
    state = _load_update_state()
    state["state_path"] = str(_update_state_path())
    state["version_file"] = str(_version_file_path())
    state["version_metadata"] = _load_version_metadata()
    print(json.dumps(state, ensure_ascii=False, indent=2))
    latest = _latest_backup_dir()
    if latest:
        print(f"latest_backup: {latest}")


def cmd_update_check(args: list[str]) -> None:
    manifest = _load_manifest(args)
    latest = _manifest_version(manifest)
    if not latest:
        raise SystemExit("ERROR: manifest missing version/latest_version")

    state = _load_update_state()
    state["last_update_check_at"] = _utc_now()
    state["last_seen_version"] = latest
    _save_update_state(state)

    current = _local_version()
    print(f"current_version: {current}")
    print(f"latest_version:  {latest}")
    if latest == current:
        print("status: up-to-date")
    elif latest in set(state.get("ignored_versions", [])):
        print("status: update ignored")
    else:
        print("status: update available")
    changelog = manifest.get("changelog") or []
    if changelog:
        print("changelog:")
        for item in changelog:
            print(f"  - {item}")


def cmd_update_ignore(args: list[str]) -> None:
    if not args:
        print("Usage: update ignore <version>")
        return
    state = _load_update_state()
    ignored = set(state.get("ignored_versions", []))
    ignored.add(args[0])
    state["ignored_versions"] = sorted(ignored)
    state["updated_at"] = _utc_now()
    _save_update_state(state)
    print(f"ignored_version: {args[0]}")


def cmd_update_apply(args: list[str]) -> None:
    yes = _has_flag(args, "--yes")
    package_arg = _find_arg(args, "--package")
    manifest: dict = {}

    src_root: Path | None = None
    if package_arg:
        package_path = Path(package_arg).expanduser().resolve()
        if not package_path.exists():
            raise SystemExit(f"ERROR: package not found: {package_path}")
        if yes:
            src_root = _find_update_source_root(package_path) if package_path.is_dir() else _extract_package(package_path)
            version = _find_arg(args, "--version") or _load_version_metadata(src_root).get("version", "local-package")
        else:
            version = _find_arg(args, "--version") or "local-package"
    else:
        manifest = _load_manifest(args)
        version = _manifest_version(manifest)
        package_url = str(manifest.get("package_url") or "")
        if not version or not package_url:
            raise SystemExit("ERROR: manifest requires version/latest_version and package_url")

    if not yes:
        print("ERROR: update apply requires explicit --yes after user confirmation.")
        print("This command may stop/restart the service and replace local code files.")
        return

    if not package_arg:
        print(f"Resolving update source for {version} ...")
        src_root = _resolve_update_source(package_url, manifest.get("sha256"))

    service_was_running = False
    has_config = _current_config_path_or_none() is not None
    if has_config:
        service_was_running = _service_is_running()
        print(f"service_before_update: {'running' if service_was_running else 'stopped'}")

    backup_root = _create_update_backup(version, service_was_running)
    print(f"backup_dir: {backup_root}")

    maintenance_enabled = False
    if has_config:
        from follow_service import watchdog

        watchdog.set_maintenance_mode(True, "update apply")
        maintenance_enabled = True

    try:
        if service_was_running:
            print("Stopping service for update (no pause, no close positions) ...")
            _call_service("stop")
            if not _wait_for_service_stopped():
                raise SystemExit("ERROR: service did not stop within 30s; aborting update before replacing code.")

        project_root = _project_root()
        changed = _copy_update_files(src_root, project_root)
        print("updated_files:")
        for name in changed:
            print(f"  - {name}")

        _compile_core(project_root)
        if "requirements.txt" in changed:
            print("NOTICE: requirements.txt changed; install dependencies manually after reviewing them.")

        state = _load_update_state()
        state["installed_version"] = _local_version()
        state["last_applied_version"] = version
        state["last_update_apply_at"] = _utc_now()
        state["last_backup_dir"] = str(backup_root)
        state["last_updated_files"] = changed
        _save_update_state(state)

        if service_was_running:
            print(
                "Service was running before update; leaving it stopped after update. "
                "Confirm follow_ratio/stop_loss_pct/take_profit_pct before starting."
            )
        else:
            print("Service was not running before update; leaving it stopped.")
        if has_config:
            _print_start_risk_confirmation_notice()
        print("Update applied.")
    finally:
        if maintenance_enabled:
            watchdog.set_maintenance_mode(False)


def cmd_update_rollback(args: list[str]) -> None:
    yes = _has_flag(args, "--yes")
    backup_arg = _find_arg(args, "--backup-dir")
    backup_root = Path(backup_arg).expanduser().resolve() if backup_arg else _latest_backup_dir()
    if not backup_root or not backup_root.exists():
        raise SystemExit("ERROR: no update backup found")
    if not yes:
        print(f"Rollback candidate: {backup_root}")
        print("Run again with --yes to restore code/config/db from this backup.")
        return

    has_config = _current_config_path_or_none() is not None
    service_was_running = _service_is_running() if has_config else False
    maintenance_enabled = False
    if has_config:
        from follow_service import watchdog

        watchdog.set_maintenance_mode(True, "update rollback")
        maintenance_enabled = True

    try:
        if service_was_running:
            _call_service("stop")
            if not _wait_for_service_stopped():
                raise SystemExit("ERROR: service did not stop within 30s; aborting rollback before replacing code.")

        project_root = _project_root()
        code_root = backup_root / "code"
        for name in [
            "cli.py",
            "VERSION.json",
            "requirements.txt",
            "config_default.json",
            "config_default.mainnet.json",
            "config_default.testnet.json",
            "follow_service/VERSION.json",
        ]:
            src = code_root / name
            if src.exists():
                shutil.copy2(src, project_root / name)
        if (code_root / "follow_service").is_dir():
            dst = project_root / "follow_service"
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(code_root / "follow_service", dst, ignore=_copy_ignore)
        skill_src = code_root / ".claude" / "skills" / "hyper-follow" / "SKILL.md"
        if skill_src.exists():
            skill_dst = project_root / ".claude" / "skills" / "hyper-follow" / "SKILL.md"
            skill_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(skill_src, skill_dst)

        config_path = _current_config_path_or_none()
        if config_path:
            inst_root = backup_root / "instance"
            cfg_backup = inst_root / config_path.name
            if cfg_backup.exists():
                shutil.copy2(cfg_backup, config_path)
            try:
                db_path = Path(cfg.get("db_path", "")).expanduser()
                db_backup = inst_root / db_path.name
                if db_backup.exists():
                    shutil.copy2(db_backup, db_path)
            except Exception:
                pass

        _compile_core(project_root)
        state = _load_update_state()
        try:
            with open(backup_root / "backup_meta.json") as f:
                json.load(f)
            state["installed_version"] = _local_version()
        except Exception:
            pass
        state["last_rollback_at"] = _utc_now()
        state["last_rollback_dir"] = str(backup_root)
        _save_update_state(state)

        if service_was_running:
            _call_service("start")
        print(f"Rollback restored from {backup_root}")
    finally:
        if maintenance_enabled:
            watchdog.set_maintenance_mode(False)


def cmd_update(args: list[str]) -> None:
    if not args or args[0] == "status":
        cmd_update_status()
    elif args[0] == "check":
        cmd_update_check(args[1:])
    elif args[0] == "apply":
        cmd_update_apply(args[1:])
    elif args[0] == "rollback":
        cmd_update_rollback(args[1:])
    elif args[0] == "ignore":
        cmd_update_ignore(args[1:])
    else:
        print("Usage: update <status|check|apply|rollback|ignore>")


# ─── main dispatcher ──────────────────────────────────────────────────────────

USAGE = """
Hyperliquid Copy Trade CLI

Usage: python cli.py [--config <path>] <command> [args]

Global options:
  --config <path>                       Use a specific config file (multi-instance)

Commands:
  service start                         Start background service
  service stop                          Stop background service
  service status                        Check service status
  service pause                         Pause: close all positions + stop service
  service resume                        Resume: restart service (rebuild baseline)
  service switch                        Switch Agent: pause + prompt for new agent_id
  service watchdog install [--interval N]
                                        Install OS-backed auto-restart watchdog
  service watchdog uninstall            Uninstall auto-restart watchdog
  service watchdog status               Show watchdog desired state and install status
  service watchdog enable|disable       Enable or disable automatic restart
  service watchdog check                Run one watchdog health check
  moss register                         Register as Moss follower (wallet signature)
  alerts list [--unread] [--json] [--limit N]
                                        List balance/system alerts
  alerts ack <id> [<id> ...]            Mark specific alerts as read
  alerts ack-all                        Mark all alerts as read
  config show                           Show current configuration
  config set <key> <value>              Set a config value
  config confirm-risk --follow-ratio R --stop-loss SL --take-profit TP
                                        Confirm follow ratio and SL/TP before start
  config check-auth                     Check Agent and Builder authorization status
  config init-contract-agent --executor ADDR --main ADDR --moss-agent agt_xxx
    --network testnet|mainnet --follow-ratio R --stop-loss SL --take-profit TP
    [--enable-agent-protocol-report --report-agent-address ADDR
     --report-executor-address ADDR --report-base-url URL --report-chain-id N]
                                        Create a contract-agent config; private_key remains empty
  config wallet-generate                Generate a new wallet (private_key + wallet_address)
  baseline show                         Show current baseline snapshot
  baseline reset                        Clear baseline (re-initialized on next start)
  trades [--limit N] [--agent ADDR]       Show trade history
  stats                                 Show aggregate statistics
  dashboard                             Show full Agent dashboard
  balance [--limit N]                   Show account balance snapshots
  update status                          Show local update state
  update check [--manifest-url URL]      Check official update manifest
  update apply [--package PATH] --yes    Apply an update after user confirmation
  update rollback [--backup-dir PATH]    Restore the latest update backup
  update ignore <version>                Ignore a prompted version
"""


def main() -> None:
    argv = sys.argv[1:]

    # 解析全局 --config 参数
    if len(argv) >= 2 and argv[0] == "--config":
        config_path = Path(argv[1])
        if not config_path.exists():
            print(f"ERROR: config file not found: {config_path}")
            sys.exit(1)
        cfg.set_config_path(config_path)
        argv = argv[2:]

    if not argv:
        print(USAGE)
        return

    cmd = argv[0]
    rest = argv[1:]

    if cmd == "service":
        cmd_service(rest)
    elif cmd == "config":
        if not rest:
            cmd_config_show()
        elif rest[0] == "show":
            cmd_config_show()
        elif rest[0] == "set":
            cmd_config_set(rest[1:])
        elif rest[0] == "confirm-risk":
            cmd_config_confirm_risk(rest[1:])
        elif rest[0] == "init-contract-agent":
            cmd_config_init_contract_agent(rest[1:])
        elif rest[0] == "wallet-generate":
            cmd_wallet_generate()
        elif rest[0] == "check-auth":
            cmd_check_auth()
        else:
            print(f"Unknown config subcommand: {rest[0]}")
    elif cmd == "moss":
        if not rest or rest[0] == "register":
            cmd_moss_register()
        else:
            print(f"Unknown moss subcommand: {rest[0]}")
            print("Usage: moss register")
    elif cmd == "alerts":
        if not rest or rest[0] == "list":
            cmd_alerts_list(rest[1:])
        elif rest[0] == "ack":
            cmd_alerts_ack(rest[1:])
        elif rest[0] == "ack-all":
            cmd_alerts_ack_all()
        else:
            print(f"Unknown alerts subcommand: {rest[0]}")
            print("Usage: alerts <list|ack <id>...|ack-all>")
    elif cmd == "baseline":
        if not rest or rest[0] == "show":
            cmd_baseline_show()
        elif rest[0] == "reset":
            cmd_baseline_reset()
        else:
            print(f"Unknown baseline subcommand: {rest[0]}")
            print("Usage: baseline <show|reset>")
    elif cmd == "trades":
        cmd_trades(rest)
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "dashboard":
        cmd_dashboard()
    elif cmd == "balance":
        cmd_balance(rest)
    elif cmd == "update":
        cmd_update(rest)
    else:
        print(USAGE)


if __name__ == "__main__":
    main()

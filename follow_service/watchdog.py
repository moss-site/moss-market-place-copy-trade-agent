"""OS-backed watchdog management for Hyper Follow service instances."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config as cfg

DEFAULT_CHECK_INTERVAL_SECS = 60
DEFAULT_RESTART_COOLDOWN_SECS = 120
DEFAULT_MAX_RESTARTS_PER_HOUR = 10


# ── state helpers ─────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _instance_dir() -> Path:
    return cfg.get_instance_dir()


def state_path() -> Path:
    return _instance_dir() / "service_state.json"


def watchdog_log_path() -> Path:
    return Path(cfg.get("log_dir", str(_instance_dir() / "logs"))).expanduser() / "watchdog.log"


def _default_state() -> dict[str, Any]:
    return {
        "desired_state": "stopped",
        "watchdog_enabled": False,
        "maintenance_mode": False,
        "restart_cooldown_secs": DEFAULT_RESTART_COOLDOWN_SECS,
        "max_restarts_per_hour": DEFAULT_MAX_RESTARTS_PER_HOUR,
        "restart_attempts": [],
        "last_watchdog_check_at": None,
        "last_restart_at": None,
        "last_restart_error": None,
        "updated_at": utc_now(),
    }


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return _default_state()
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_state()
    except (OSError, json.JSONDecodeError):
        return _default_state()

    state = _default_state()
    state.update(data)
    state.setdefault("restart_attempts", [])
    return state


def save_state(state: dict[str, Any]) -> dict[str, Any]:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now()
    with open(path, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return state


def update_state(**updates: Any) -> dict[str, Any]:
    state = load_state()
    state.update(updates)
    return save_state(state)


def set_desired_state(value: str) -> dict[str, Any]:
    if value not in {"running", "stopped", "paused"}:
        raise ValueError(f"invalid desired_state: {value}")
    return update_state(desired_state=value)


def set_watchdog_enabled(enabled: bool) -> dict[str, Any]:
    return update_state(watchdog_enabled=bool(enabled))


def enable_for_current_service() -> dict[str, Any]:
    updates: dict[str, Any] = {"watchdog_enabled": True}
    if is_service_running():
        updates["desired_state"] = "running"
    return update_state(**updates)


def set_maintenance_mode(enabled: bool, reason: str | None = None) -> dict[str, Any]:
    updates: dict[str, Any] = {"maintenance_mode": bool(enabled)}
    if reason is not None:
        updates["maintenance_reason"] = reason if enabled else None
    return update_state(**updates)


def append_log(message: str) -> None:
    path = watchdog_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"{utc_now()} {message}\n")


# ── service checks ────────────────────────────────────────────────────────────

def pid_file() -> Path:
    return Path(cfg.get("pid_file", str(_instance_dir() / "service.pid"))).expanduser()


def read_pid() -> int | None:
    path = pid_file()
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def is_service_running() -> bool:
    pid = read_pid()
    return bool(pid and is_pid_running(pid))


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _recent_restart_attempts(state: dict[str, Any]) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    recent: list[str] = []
    for raw in state.get("restart_attempts", []) or []:
        dt = _parse_utc(str(raw))
        if dt and dt >= cutoff:
            recent.append(str(raw))
    return recent


def restart_rate_limited(state: dict[str, Any]) -> tuple[bool, str | None, list[str]]:
    recent = _recent_restart_attempts(state)
    max_per_hour = int(state.get("max_restarts_per_hour") or DEFAULT_MAX_RESTARTS_PER_HOUR)
    if len(recent) >= max_per_hour:
        return True, f"max_restarts_per_hour reached ({len(recent)}/{max_per_hour})", recent

    cooldown = int(state.get("restart_cooldown_secs") or DEFAULT_RESTART_COOLDOWN_SECS)
    last_dt = _parse_utc(state.get("last_restart_at"))
    if last_dt and (datetime.now(timezone.utc) - last_dt).total_seconds() < cooldown:
        return True, f"restart cooldown active ({cooldown}s)", recent

    return False, None, recent


def run_service_start(cli_path: Path, python_path: str | None = None) -> subprocess.CompletedProcess:
    config_path = cfg.get_config_path().expanduser().resolve()
    py = python_path or sys.executable
    env = os.environ.copy()
    env["FOLLOW_CONFIG"] = str(config_path)
    log_path = watchdog_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log_file:
        return subprocess.run(
            [py, str(cli_path), "--config", str(config_path), "service", "start"],
            cwd=cli_path.parent,
            env=env,
            stdout=log_file,
            stderr=log_file,
            text=True,
        )


def watchdog_check(cli_path: Path, python_path: str | None = None) -> dict[str, Any]:
    state = load_state()
    state["last_watchdog_check_at"] = utc_now()
    status = "noop"
    reason = ""

    if not state.get("watchdog_enabled"):
        reason = "watchdog disabled"
    elif state.get("maintenance_mode"):
        reason = "maintenance mode"
    elif state.get("desired_state") != "running":
        reason = f"desired_state={state.get('desired_state')}"
    elif is_service_running():
        reason = "service running"
    else:
        limited, limited_reason, recent = restart_rate_limited(state)
        state["restart_attempts"] = recent
        if limited:
            reason = limited_reason or "restart rate limited"
            state["last_restart_error"] = reason
        else:
            append_log("service expected running but stopped; restarting")
            result = run_service_start(cli_path=cli_path, python_path=python_path)
            now = utc_now()
            state["restart_attempts"] = [*recent, now]
            state["last_restart_at"] = now
            if result.returncode == 0:
                status = "restarted"
                reason = "service start executed"
                state["last_restart_error"] = None
                append_log("restart succeeded")
            else:
                status = "failed"
                reason = f"service start failed with exit={result.returncode}; see watchdog log"
                state["last_restart_error"] = reason
                append_log(f"restart failed: {reason}")

    save_state(state)
    return {"status": status, "reason": reason, "state": state}


# ── installer paths ───────────────────────────────────────────────────────────

def instance_id() -> str:
    return cfg.get_instance_id()


def label() -> str:
    return f"com.moss.hyper-follow.{instance_id()}.watchdog"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label()}.plist"


def systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def systemd_unit_name() -> str:
    return f"hyper-follow-{instance_id()}-watchdog.service"


def systemd_timer_name() -> str:
    return f"hyper-follow-{instance_id()}-watchdog.timer"


def systemd_service_path() -> Path:
    return systemd_user_dir() / systemd_unit_name()


def systemd_timer_path() -> Path:
    return systemd_user_dir() / systemd_timer_name()


def _program_args(cli_path: Path, python_path: str) -> list[str]:
    return [
        python_path,
        str(cli_path),
        "--config",
        str(cfg.get_config_path().expanduser().resolve()),
        "service",
        "watchdog",
        "check",
    ]


# ── install/uninstall/status ──────────────────────────────────────────────────

def install(cli_path: Path, python_path: str, interval_secs: int = DEFAULT_CHECK_INTERVAL_SECS) -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        return install_launchd(cli_path, python_path, interval_secs)
    if system == "Linux":
        return install_systemd(cli_path, python_path, interval_secs)
    raise RuntimeError(f"watchdog install is not supported on {system}")


def uninstall() -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        return uninstall_launchd()
    if system == "Linux":
        return uninstall_systemd()
    raise RuntimeError(f"watchdog uninstall is not supported on {system}")


def install_launchd(cli_path: Path, python_path: str, interval_secs: int) -> dict[str, Any]:
    plist_path = launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = Path(cfg.get("log_dir", str(_instance_dir() / "logs"))).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": label(),
        "ProgramArguments": _program_args(cli_path, python_path),
        "StartInterval": int(interval_secs),
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir / "watchdog.stdout.log"),
        "StandardErrorPath": str(log_dir / "watchdog.stderr.log"),
        "WorkingDirectory": str(cli_path.parent),
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(payload, f)

    gui = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", gui, str(plist_path)], capture_output=True, text=True)
    boot = subprocess.run(["launchctl", "bootstrap", gui, str(plist_path)], capture_output=True, text=True)
    enable = subprocess.run(["launchctl", "enable", f"{gui}/{label()}"], capture_output=True, text=True)
    if boot.returncode != 0:
        raise RuntimeError((boot.stderr or boot.stdout or "launchctl bootstrap failed").strip())
    if enable.returncode != 0:
        raise RuntimeError((enable.stderr or enable.stdout or "launchctl enable failed").strip())
    append_log(f"launchd watchdog installed: {plist_path}")
    return {"platform": "macos", "installed": True, "path": str(plist_path), "label": label()}


def uninstall_launchd() -> dict[str, Any]:
    plist_path = launchd_plist_path()
    gui = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", gui, str(plist_path)], capture_output=True, text=True)
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass
    append_log("launchd watchdog uninstalled")
    return {"platform": "macos", "installed": False, "path": str(plist_path), "label": label()}


def install_systemd(cli_path: Path, python_path: str, interval_secs: int) -> dict[str, Any]:
    systemd_user_dir().mkdir(parents=True, exist_ok=True)
    service_path = systemd_service_path()
    timer_path = systemd_timer_path()
    args = " ".join(_systemd_quote(arg) for arg in _program_args(cli_path, python_path))
    service_path.write_text(
        "[Unit]\n"
        f"Description=Hyper Follow watchdog check for {instance_id()}\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={cli_path.parent}\n"
        f"ExecStart={args}\n"
    )
    timer_path.write_text(
        "[Unit]\n"
        f"Description=Run Hyper Follow watchdog every minute for {instance_id()}\n\n"
        "[Timer]\n"
        "OnBootSec=30\n"
        f"OnUnitActiveSec={int(interval_secs)}\n"
        "AccuracySec=10\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    reload_result = subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    if reload_result.returncode != 0:
        raise RuntimeError((reload_result.stderr or reload_result.stdout or "systemctl daemon-reload failed").strip())
    enable = subprocess.run(["systemctl", "--user", "enable", "--now", systemd_timer_name()], capture_output=True, text=True)
    if enable.returncode != 0:
        raise RuntimeError((enable.stderr or enable.stdout or "systemctl enable timer failed").strip())
    append_log(f"systemd watchdog installed: {timer_path}")
    return {
        "platform": "linux",
        "installed": True,
        "service_path": str(service_path),
        "timer_path": str(timer_path),
        "timer": systemd_timer_name(),
    }


def uninstall_systemd() -> dict[str, Any]:
    subprocess.run(["systemctl", "--user", "disable", "--now", systemd_timer_name()], capture_output=True, text=True)
    for path in (systemd_service_path(), systemd_timer_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    append_log("systemd watchdog uninstalled")
    return {"platform": "linux", "installed": False, "timer": systemd_timer_name()}


def _systemd_quote(value: str) -> str:
    if all(ch.isalnum() or ch in "/._:=+-" for ch in value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def install_info() -> dict[str, Any]:
    system = platform.system()
    info: dict[str, Any] = {"platform": system, "supported": system in {"Darwin", "Linux"}}
    if system == "Darwin":
        path = launchd_plist_path()
        info.update({"type": "launchd", "path": str(path), "installed": path.exists(), "label": label()})
        result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label()}"], capture_output=True, text=True)
        info["scheduler_loaded"] = result.returncode == 0
    elif system == "Linux":
        service_path = systemd_service_path()
        timer_path = systemd_timer_path()
        info.update({
            "type": "systemd-user",
            "service_path": str(service_path),
            "timer_path": str(timer_path),
            "installed": service_path.exists() and timer_path.exists(),
            "timer": systemd_timer_name(),
        })
        result = subprocess.run(["systemctl", "--user", "is-active", systemd_timer_name()], capture_output=True, text=True)
        info["scheduler_loaded"] = result.returncode == 0
        info["scheduler_state"] = result.stdout.strip() or result.stderr.strip()
    return info


def status() -> dict[str, Any]:
    return {
        "instance_id": instance_id(),
        "config_path": str(cfg.get_config_path().expanduser().resolve()),
        "state_path": str(state_path()),
        "pid_file": str(pid_file()),
        "service_running": is_service_running(),
        "state": load_state(),
        "install": install_info(),
        "log_path": str(watchdog_log_path()),
    }

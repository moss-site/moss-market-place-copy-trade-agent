"""Hyperliquid supported perp coin cache."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

from . import config as cfg

logger = logging.getLogger("follow_agent.hyper_coins")

CACHE_FILENAME = "hyper_supported_coins.json"
DEFAULT_REFRESH_SECS = 600
_REFRESH_FAILURE_COOLDOWN_SECS = 60.0
_refresh_lock = threading.Lock()
_last_refresh_attempt_at: float | None = None
_last_refresh_attempt_key: tuple[str, str] | None = None
_last_refresh_failed = False
_refresh_generation = 0


def get_cache_path() -> Path:
    """Return the per-instance cache path next to config_<id>.json."""
    return cfg.get_config_path().parent / CACHE_FILENAME


def _refresh_secs() -> int:
    try:
        return max(60, int(cfg.get("hyper_coin_refresh_secs", DEFAULT_REFRESH_SECS)))
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_SECS


def _api_url() -> str:
    return cfg.get("hl_api_url", "https://api.hyperliquid-testnet.xyz")


def _extract_perp_coins(meta: dict[str, Any]) -> list[str]:
    coins: list[str] = []
    for item in meta.get("universe", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or item.get("isDelisted"):
            continue
        coins.append(name)
    return sorted(set(coins))


def _read_cache() -> dict[str, Any] | None:
    path = get_cache_path()
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Failed to read Hyper coin cache %s: %s", path, e)
    return None


def _is_cache_fresh(data: dict[str, Any], api_url: str) -> bool:
    if data.get("api_url") != api_url:
        return False
    if "perp_dexs" not in data:
        return False
    try:
        fetched_at = float(data.get("fetched_at", 0))
    except (TypeError, ValueError):
        return False
    refresh_secs = _refresh_secs()
    if data.get("failed_perp_dexs"):
        refresh_secs = min(refresh_secs, int(_REFRESH_FAILURE_COOLDOWN_SECS))
    return time.time() - fetched_at < refresh_secs


def _normalize_perp_dexs(raw_dexs: Any) -> list[str]:
    """Normalize the perpDexs response to SDK dex names (default dex is "")."""
    dexes: list[str] = []
    for item in raw_dexs or []:
        if item is None:
            name = ""
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item).strip()
        if name not in dexes:
            dexes.append(name)
    if "" not in dexes:
        dexes.insert(0, "")
    elif dexes[0] != "":
        dexes.remove("")
        dexes.insert(0, "")
    return dexes


def _fetch_perp_dexs(info, api_url: str) -> list[str]:
    if info is not None and hasattr(info, "perp_dexs"):
        return _normalize_perp_dexs(info.perp_dexs())

    if info is not None:
        return [""]

    r = requests.post(f"{api_url}/info", json={"type": "perpDexs"}, timeout=10)
    r.raise_for_status()
    return _normalize_perp_dexs(r.json())


def _fetch_meta(info, api_url: str, dex: str) -> dict[str, Any]:
    if info is not None and hasattr(info, "meta"):
        try:
            return info.meta(dex=dex)
        except TypeError:
            if dex == "":
                return info.meta()
            raise

    body = {"type": "meta"}
    if dex:
        body["dex"] = dex
    r = requests.post(f"{api_url}/info", json=body, timeout=10)
    r.raise_for_status()
    return r.json()


def _required_perp_dexs() -> set[str]:
    """Return DEXes used by the current Agent baseline; default is always required."""
    required = {""}
    try:
        from . import database as db

        moss_cfg = cfg.get_moss_source_config()
        agent_id = str(moss_cfg.get("agent_id") or "")
        baselines = db.get_baselines(agent_id) if agent_id else {}
        for coin in baselines:
            value = str(coin or "")
            if ":" in value:
                required.add(value.split(":", 1)[0])
    except Exception as e:
        logger.debug("Failed to read required DEXes from baselines: %s", e)
    return required


def write_supported_coins(
    coins: list[str],
    *,
    api_url: str | None = None,
    perp_dexs: list[str] | None = None,
    failed_perp_dexs: list[str] | None = None,
) -> dict[str, Any]:
    """Atomically write the supported perp coin list cache."""
    normalized = []
    for coin in coins:
        if coin is None:
            continue
        value = str(coin).strip()
        if value:
            normalized.append(value)
    payload = {
        "api_url": api_url or _api_url(),
        "fetched_at": time.time(),
        "refresh_secs": _refresh_secs(),
        "coins": sorted(set(normalized)),
        "perp_dexs": _normalize_perp_dexs(perp_dexs or [""]),
        "failed_perp_dexs": sorted(set(failed_perp_dexs or [])),
    }
    path = get_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, path)
    return payload


def refresh_supported_coins(info=None, *, force: bool = False) -> dict[str, Any]:
    """
    Refresh the local Hyperliquid supported coin cache.

    Enumerate perpDexs and merge the default and builder-deployed perp metas.
    If `info` is provided, reuse its methods; otherwise query `/info` directly.
    """
    global _last_refresh_attempt_at, _last_refresh_attempt_key
    global _last_refresh_failed, _refresh_generation

    observed_generation = _refresh_generation
    with _refresh_lock:
        api_url = _api_url()
        attempt_key = (api_url, str(get_cache_path()))
        cached = _read_cache()

        def _fallback() -> dict[str, Any]:
            if cached and cached.get("api_url") == api_url:
                return cached
            return {
                "api_url": api_url,
                "fetched_at": 0,
                "refresh_secs": _refresh_secs(),
                "coins": [],
                "perp_dexs": [""],
            }

        # A caller that waited for another refresh shares that completed result.
        if (
            _refresh_generation != observed_generation
            and _last_refresh_attempt_key == attempt_key
        ):
            return _fallback()

        if cached and not force and _is_cache_fresh(cached, api_url):
            return cached

        now = time.monotonic()
        if (
            _last_refresh_failed
            and _last_refresh_attempt_key == attempt_key
            and _last_refresh_attempt_at is not None
            and now - _last_refresh_attempt_at < _REFRESH_FAILURE_COOLDOWN_SECS
        ):
            return _fallback()

        _last_refresh_attempt_at = now
        _last_refresh_attempt_key = attempt_key
        try:
            discovered_dexs = _fetch_perp_dexs(info, api_url)
            required_dexs = _required_perp_dexs()
            missing_required = sorted(required_dexs - set(discovered_dexs))
            if missing_required:
                raise RuntimeError(
                    f"required perp DEXes are unavailable: {missing_required}"
                )

            coins: list[str] = []
            loaded_dexs: list[str] = []
            failed_dexs: list[str] = []
            for dex in discovered_dexs:
                try:
                    coins.extend(_extract_perp_coins(_fetch_meta(info, api_url, dex)))
                    loaded_dexs.append(dex)
                except Exception as e:
                    if dex in required_dexs:
                        raise RuntimeError(
                            f"required perp DEX metadata unavailable for {dex or 'default'}"
                        ) from e
                    logger.warning(
                        "Skipping unavailable non-required perp DEX metadata: dex=%s error=%s",
                        dex, e,
                    )
                    failed_dexs.append(dex)

            data = write_supported_coins(
                coins,
                api_url=api_url,
                perp_dexs=loaded_dexs,
                failed_perp_dexs=failed_dexs,
            )
        except Exception:
            # Start the cooldown after the (potentially very slow) failed attempt
            # completes, otherwise a 121s enumeration would immediately retry.
            _last_refresh_attempt_at = time.monotonic()
            _last_refresh_failed = True
            _refresh_generation += 1
            raise

        _last_refresh_failed = False
        _refresh_generation += 1
        logger.info(
            "Hyper coin cache refreshed: %d coins across %d perp dexes (failed=%s) -> %s",
            len(data["coins"]), len(loaded_dexs), failed_dexs, get_cache_path(),
        )
        return data


def get_perp_dexs(info=None) -> list[str]:
    """Return cached SDK perp dex names, refreshing legacy/stale caches."""
    api_url = _api_url()
    cached = _read_cache()
    if cached and _is_cache_fresh(cached, api_url) and "perp_dexs" in cached:
        return _normalize_perp_dexs(cached.get("perp_dexs"))

    try:
        data = refresh_supported_coins(info=info, force=True)
        return _normalize_perp_dexs(data.get("perp_dexs"))
    except Exception as e:
        logger.warning("Failed to refresh Hyper perp dex cache: %s", e)
        cached = _read_cache()
        if cached and cached.get("api_url") == api_url and "perp_dexs" in cached:
            return _normalize_perp_dexs(cached.get("perp_dexs"))
        return [""]


def get_supported_coins(info=None) -> set[str]:
    """Return cached supported coins, refreshing when the cache is stale."""
    api_url = _api_url()
    cached = _read_cache()
    if cached and _is_cache_fresh(cached, api_url):
        return set(cached.get("coins") or [])

    try:
        cached = refresh_supported_coins(info=info)
    except Exception as e:
        logger.warning("Failed to refresh Hyper coin cache: %s", e)
        cached = _read_cache()
        if cached and cached.get("api_url") != api_url:
            cached = None
    return set((cached or {}).get("coins") or [])


def canonicalize_coin(coin: str, info=None) -> str | None:
    """Return the exact Hyperliquid coin casing from the supported coin cache."""
    if not coin:
        return None

    raw = str(coin).strip()
    if not raw:
        return None

    supported = get_supported_coins(info=info)
    if raw in supported:
        return raw

    raw_lower = raw.lower()
    for supported_coin in supported:
        if supported_coin.lower() == raw_lower:
            return supported_coin
    return None


def is_supported_coin(coin: str, info=None) -> bool:
    """Return whether `coin` is in the cached Hyperliquid perp universe."""
    return canonicalize_coin(coin, info=info) is not None


def canonicalize_positions(positions: dict, info=None) -> dict:
    """Canonicalize position dict keys to exact Hyperliquid coin names."""
    canonical: dict = {}
    for coin, pos in (positions or {}).items():
        canonical_coin = canonicalize_coin(coin, info=info) or coin
        item = dict(pos) if isinstance(pos, dict) else pos
        canonical[canonical_coin] = item
    return canonical


async def run_hyper_coin_refresher(stop_event: asyncio.Event) -> None:
    """Refresh the Hyperliquid supported coin cache on a fixed interval."""
    try:
        while not stop_event.is_set():
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, refresh_supported_coins)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Hyper coin cache refresh error: %s", e)

            interval = _refresh_secs()
            for _ in range(interval):
                if stop_event.is_set():
                    break
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("Hyper coin refresher task cancelled, exiting ...")

    logger.info("Hyper coin refresher stopped.")

"""Hyperliquid supported perp coin cache."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

from . import config as cfg

logger = logging.getLogger("follow_agent.hyper_coins")

CACHE_FILENAME = "hyper_supported_coins.json"
DEFAULT_REFRESH_SECS = 600


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
    try:
        fetched_at = float(data.get("fetched_at", 0))
    except (TypeError, ValueError):
        return False
    return time.time() - fetched_at < _refresh_secs()


def write_supported_coins(coins: list[str], *, api_url: str | None = None) -> dict[str, Any]:
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
    }
    path = get_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, path)
    return payload


def write_supported_coins_from_meta(meta: dict[str, Any], *, api_url: str | None = None) -> dict[str, Any]:
    """Extract perp coins from Hyperliquid meta and write the cache."""
    return write_supported_coins(_extract_perp_coins(meta), api_url=api_url)


def refresh_supported_coins(info=None, *, force: bool = False) -> dict[str, Any]:
    """
    Refresh the local Hyperliquid supported coin cache.

    If `info` is provided, reuse its `meta()` method; otherwise query the
    Hyperliquid `/info` endpoint directly.
    """
    api_url = _api_url()
    cached = _read_cache()
    if cached and not force and _is_cache_fresh(cached, api_url):
        return cached

    if info is not None and hasattr(info, "meta"):
        meta = info.meta()
    else:
        r = requests.post(f"{api_url}/info", json={"type": "meta"}, timeout=10)
        r.raise_for_status()
        meta = r.json()

    data = write_supported_coins_from_meta(meta, api_url=api_url)
    logger.info("Hyper coin cache refreshed: %d coins -> %s", len(data["coins"]), get_cache_path())
    return data


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

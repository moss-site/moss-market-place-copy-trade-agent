"""Symbol normalization helpers shared by Moss consumers."""

import logging
from collections.abc import Iterable

_QUOTE_SUFFIXES = ("USDT", "USDC")
logger = logging.getLogger("follow_agent.symbols")

# True aliases where Moss uses a different display symbol than Hyperliquid.
# Plain stock tickers are resolved dynamically from the supported coin cache.
_XYZ_PRODUCT_COINS = {
    "S&P500": "SP500",
    "WTIOIL": "CL",
    "SKHYNIX": "SKHX",
}


def known_xyz_coin(symbol: str) -> str | None:
    """Return the platform-confirmed xyz coin for a Moss stock symbol."""
    normalized = str(symbol or "").strip().replace("-", "").replace("/", "")
    upper = normalized.upper()
    for quote in _QUOTE_SUFFIXES:
        if upper.endswith(quote):
            normalized = normalized[:-len(quote)]
            break
    coin = _XYZ_PRODUCT_COINS.get(normalized.upper())
    return f"xyz:{coin}" if coin else None


def _resolve_candidate(
    candidate: str,
    supported_coins: Iterable[str] | None,
    symbol: str,
    *,
    hip3_bare_fallback: bool = False,
    preferred_dex: str | None = None,
    reject_ambiguous: bool = False,
) -> str | None:
    if supported_coins is None:
        return candidate

    supported = [str(coin) for coin in supported_coins]
    candidate_lower = candidate.lower()

    hip3_matches: list[str] = []
    if ":" not in candidate:
        hip3_matches = sorted(
            coin for coin in supported
            if ":" in coin and coin.split(":", 1)[1].lower() == candidate_lower
        )
        preferred = str(preferred_dex or "").strip()
        if preferred:
            preferred_matches = [
                coin for coin in hip3_matches
                if coin.split(":", 1)[0].lower() == preferred.lower()
            ]
            if len(preferred_matches) == 1:
                return preferred_matches[0]

        exact_default = next((coin for coin in supported if coin.lower() == candidate_lower), None)
        if reject_ambiguous and exact_default and hip3_matches:
            logger.warning(
                "Ambiguous Moss symbol %s: bare coin %s matches default and HIP-3 markets %s; skipping",
                symbol, candidate, [exact_default, *hip3_matches],
            )
            return None

    for coin in supported:
        if coin.lower() == candidate_lower:
            return coin

    if ":" in candidate:
        logger.warning(
            "Moss symbol %s resolved to unsupported Hyperliquid coin %s; skipping",
            symbol, candidate,
        )
        return None

    if not hip3_bare_fallback:
        return candidate

    if len(hip3_matches) == 1:
        return hip3_matches[0]
    if len(hip3_matches) > 1:
        logger.warning(
            "Ambiguous Moss symbol %s: bare coin %s matches multiple HIP-3 markets %s; skipping",
            symbol, candidate, hip3_matches,
        )
        return None
    return candidate


def symbol_to_coin(
    symbol: str,
    symbol_map: dict | None = None,
    supported_coins: Iterable[str] | None = None,
    *,
    hip3_bare_fallback: bool = False,
    preferred_dex: str | None = None,
    reject_ambiguous: bool = False,
) -> str | None:
    """Map Moss symbols to Hyperliquid coin names using config plus quote suffix fallback."""
    if not symbol:
        return None

    symbol = str(symbol).strip()
    if not symbol:
        return None

    symbol_map = symbol_map or {}
    # Moss may emit BTC-USDC or BTC/USDT while Hyperliquid perp coins use BTC.
    normalized = symbol.replace("-", "").replace("/", "")
    mapped = symbol_map.get(symbol)
    if mapped is None:
        mapped = symbol_map.get(normalized)

    # Preserve explicit DEX mappings, but repair legacy bare mappings such as
    # MUUSDC -> MU now that the platform classifies these products as xyz.
    if mapped is not None and ":" in str(mapped):
        return _resolve_candidate(
            str(mapped), supported_coins, symbol,
            preferred_dex=preferred_dex,
            reject_ambiguous=reject_ambiguous,
        )
    xyz_coin = known_xyz_coin(symbol)
    if xyz_coin:
        return _resolve_candidate(
            xyz_coin, supported_coins, symbol,
            preferred_dex=preferred_dex,
            reject_ambiguous=reject_ambiguous,
        )
    if mapped is not None:
        return _resolve_candidate(
            str(mapped), supported_coins, symbol,
            hip3_bare_fallback=hip3_bare_fallback,
            preferred_dex=preferred_dex,
            reject_ambiguous=reject_ambiguous,
        )

    if ":" in symbol and not any(normalized.endswith(quote) for quote in _QUOTE_SUFFIXES):
        return _resolve_candidate(
            symbol, supported_coins, symbol,
            preferred_dex=preferred_dex,
            reject_ambiguous=reject_ambiguous,
        )

    for quote in _QUOTE_SUFFIXES:
        if normalized.upper().endswith(quote):
            return _resolve_candidate(
                normalized[:-len(quote)],
                supported_coins,
                symbol,
                hip3_bare_fallback=hip3_bare_fallback,
                preferred_dex=preferred_dex,
                reject_ambiguous=reject_ambiguous,
            )
    return None

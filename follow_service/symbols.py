"""Symbol normalization helpers shared by Moss consumers."""

_QUOTE_SUFFIXES = ("USDT", "USDC")


def symbol_to_coin(symbol: str, symbol_map: dict | None = None) -> str | None:
    """Map Moss symbols to Hyperliquid coin names using config plus quote suffix fallback."""
    if not symbol:
        return None

    symbol = str(symbol).strip()
    if not symbol:
        return None

    symbol_map = symbol_map or {}
    if symbol in symbol_map:
        return symbol_map[symbol]

    # Moss may emit BTC-USDC or BTC/USDT while Hyperliquid perp coins use BTC.
    normalized = symbol.replace("-", "").replace("/", "")
    if normalized in symbol_map:
        return symbol_map[normalized]

    if ":" in symbol and not any(normalized.endswith(quote) for quote in _QUOTE_SUFFIXES):
        return symbol

    for quote in _QUOTE_SUFFIXES:
        if normalized.endswith(quote):
            return normalized[:-len(quote)]
    return None

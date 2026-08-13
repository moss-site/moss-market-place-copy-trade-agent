"""Classify a Moss Agent into the single clearinghouse used for following."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from . import config as cfg
from . import hyper_coins
from .moss_client import MossClient
from .symbols import symbol_to_coin

HISTORY_SAMPLE_SIZE = 20
DEFAULT_SCOPE = "default"
XYZ_SCOPE = "xyz"


class AgentMarketError(RuntimeError):
    """The Agent history cannot safely select one supported clearinghouse."""


@dataclass(frozen=True)
class AgentMarketReport:
    agent_id: str
    market_scope: str
    sample_size: int
    symbols: tuple[str, ...]
    coins: tuple[str, ...]
    checked_at: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["symbols"] = list(self.symbols)
        data["coins"] = list(self.coins)
        return data


def _canonical_supported(coin: str, supported_coins: set[str]) -> str | None:
    lowered = coin.lower()
    return next((item for item in supported_coins if item.lower() == lowered), None)


def classify_fills(
    agent_id: str,
    fills: list[dict],
    *,
    symbol_map: dict,
    supported_coins,
) -> AgentMarketReport:
    """Classify the latest fills; every sampled fill must resolve to one scope."""
    sampled = list(fills or [])[:HISTORY_SAMPLE_SIZE]
    if not sampled:
        raise AgentMarketError("Agent 没有成交历史，无法确认美股/非美股类型")

    supported = {str(coin) for coin in (supported_coins or [])}
    scopes: set[str] = set()
    symbols: set[str] = set()
    coins: set[str] = set()
    for fill in sampled:
        symbol = str(fill.get("symbol") or "").strip()
        if not symbol:
            raise AgentMarketError("成交历史包含空 symbol")
        coin = symbol_to_coin(
            symbol,
            symbol_map,
            supported,
            hip3_bare_fallback=True,
            reject_ambiguous=True,
        )
        canonical = _canonical_supported(coin, supported) if coin else None
        if not canonical:
            raise AgentMarketError(f"无法将历史币种 {symbol} 映射到支持的 Hyperliquid 合约")
        dex = canonical.split(":", 1)[0] if ":" in canonical else ""
        if dex not in {"", XYZ_SCOPE}:
            raise AgentMarketError(
                f"历史币种 {symbol} 属于不支持的 HIP-3 DEX: {dex}；当前只支持 xyz 美股"
            )
        scopes.add(XYZ_SCOPE if dex == XYZ_SCOPE else DEFAULT_SCOPE)
        symbols.add(symbol)
        coins.add(canonical)

    if len(scopes) != 1:
        raise AgentMarketError("最近 20 条成交同时包含美股和非美股，拒绝跟单")

    return AgentMarketReport(
        agent_id=agent_id,
        market_scope=scopes.pop(),
        sample_size=len(sampled),
        symbols=tuple(sorted(symbols)),
        coins=tuple(sorted(coins)),
        checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def inspect_agent(
    agent_id: str | None = None,
    *,
    persist: bool = False,
    refresh_cache: bool = True,
) -> AgentMarketReport:
    """Fetch up to 20 authenticated fills and optionally persist the scope."""
    moss_cfg = cfg.get_moss_source_config()
    target_agent = str(agent_id or moss_cfg.get("agent_id") or "").strip()
    if not target_agent:
        raise AgentMarketError("moss_source.agent_id 未配置")

    if refresh_cache:
        try:
            coin_cache = hyper_coins.refresh_supported_coins(force=True)
            if not coin_cache.get("coins"):
                raise RuntimeError("empty Hyperliquid supported coin list")
        except Exception as exc:
            raise AgentMarketError(f"刷新 Hyperliquid 支持合约列表失败: {exc}") from exc

    client = MossClient(
        base_url=str(moss_cfg.get("base_url") or ""),
        agent_id=target_agent,
        private_key=str(cfg.get("private_key", "")),
        wallet_address=str(cfg.get("wallet_address", "")),
        builder_address=cfg.get_builder_address(),
        main_address=str(cfg.get("main_address", "")),
    )
    if not client.has_follower_auth():
        raise AgentMarketError("缺少 private_key，无法查询 Agent 成交历史")
    try:
        client.register_follower()
        fills = client.get_fills(page_size=HISTORY_SAMPLE_SIZE)
    except Exception as exc:
        raise AgentMarketError(f"查询 Agent 成交历史失败: {exc}") from exc

    report = classify_fills(
        target_agent,
        fills,
        symbol_map=moss_cfg.get("symbol_map", {}),
        supported_coins=_supported_default_xyz_coins(),
    )
    if persist:
        persist_report(report)
    return report


def _supported_default_xyz_coins() -> set[str]:
    """Fetch only the two product-supported universes used for classification."""
    from .preflight import _post_info

    cached = hyper_coins.get_supported_coins()
    if cached:
        return {coin for coin in cached if ":" not in coin or coin.split(":", 1)[0] == XYZ_SCOPE}

    api_url = str(cfg.get("hl_api_url", "https://api.hyperliquid-testnet.xyz"))
    coins: set[str] = set()
    try:
        for dex in ("", XYZ_SCOPE):
            payload = {"type": "meta"}
            if dex:
                payload["dex"] = dex
            meta = _post_info(api_url, payload)
            if not isinstance(meta, dict):
                raise ValueError(f"invalid meta response for {dex or 'default'}")
            for market in meta.get("universe", []) or []:
                if isinstance(market, dict) and market.get("name"):
                    coins.add(str(market["name"]))
    except Exception as exc:
        raise AgentMarketError(f"查询 default/xyz 合约列表失败: {exc}") from exc
    return coins


def persist_report(report: AgentMarketReport, *, set_agent_id: bool = False) -> None:
    """Persist a validated report, optionally selecting that Agent atomically."""
    def _mutate(config_data: dict) -> None:
        current = config_data.setdefault("moss_source", {})
        if set_agent_id:
            current["agent_id"] = report.agent_id
            current["agent_name"] = ""
            config_data["risk_params_confirmed"] = False
        current["market_scope"] = report.market_scope
        current["market_scope_agent_id"] = report.agent_id
        current["market_scope_sample_size"] = report.sample_size
        current["market_scope_symbols"] = list(report.symbols)
        current["market_scope_checked_at"] = report.checked_at

    cfg.update_config(_mutate)


def get_scope_balance(market_scope: str) -> dict[str, float]:
    """Read the target clearinghouse balance selected by Agent history."""
    from .preflight import _post_info

    if market_scope not in {DEFAULT_SCOPE, XYZ_SCOPE}:
        raise AgentMarketError(f"无效 market_scope: {market_scope}")
    account = str(cfg.get("main_address", "") or cfg.get("wallet_address", "")).lower()
    if not account:
        raise AgentMarketError("主账户地址未配置")
    payload = {"type": "clearinghouseState", "user": account}
    if market_scope == XYZ_SCOPE:
        payload["dex"] = XYZ_SCOPE
    try:
        response = _post_info(
            str(cfg.get("hl_api_url", "https://api.hyperliquid-testnet.xyz")),
            payload,
        )
    except Exception as exc:
        raise AgentMarketError(f"查询目标账户余额失败: {exc}") from exc
    state = response if isinstance(response, dict) else {}
    summary = state.get("marginSummary", {})
    summary = summary if isinstance(summary, dict) else {}
    return {
        "account_value": float(summary.get("accountValue") or 0),
        "withdrawable": float(state.get("withdrawable") or 0),
    }


def get_open_positions() -> dict[str, float]:
    """Return non-zero positions across the only two supported scopes."""
    from .preflight import _post_info

    account = str(cfg.get("main_address", "") or cfg.get("wallet_address", "")).lower()
    if not account:
        raise AgentMarketError("主账户地址未配置")
    api_url = str(cfg.get("hl_api_url", "https://api.hyperliquid-testnet.xyz"))
    positions: dict[str, float] = {}
    try:
        for dex in ("", XYZ_SCOPE):
            payload = {"type": "clearinghouseState", "user": account}
            if dex:
                payload["dex"] = dex
            state = _post_info(api_url, payload)
            if not isinstance(state, dict):
                continue
            for asset_position in state.get("assetPositions", []) or []:
                position = asset_position.get("position", {}) if isinstance(asset_position, dict) else {}
                coin = str(position.get("coin") or "")
                size = float(position.get("szi") or 0)
                if coin and size:
                    positions[coin] = size
    except Exception as exc:
        raise AgentMarketError(f"查询切换前持仓失败: {exc}") from exc
    return positions

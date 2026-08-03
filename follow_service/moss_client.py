"""
Moss Source-Core 2.0 REST API 客户端

鉴权模式：Follower 钱包签名（用 private_key 签名，可访问任意 Agent）
"""

import json
import logging
import time
import urllib.parse

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

logger = logging.getLogger("follow_agent.moss_client")

_FOLLOWER_PREFIX = "/api/v2/moss/follower"
_COPY_TRADING_PREFIX = "/api/v2/moss/copy-trading"


def _normalize_address(value: str) -> str:
    return value.strip().lower() if value else ""


class MossClient:
    """Moss REST API 客户端（Follower 钱包签名鉴权）。"""

    def __init__(self, base_url: str, agent_id: str,
                 private_key: str = "", wallet_address: str = "",
                 builder_address: str = "", main_address: str = ""):
        self._base_url = base_url.rstrip("/")
        self._agent_id = agent_id
        self._private_key = private_key
        self._wallet_address = _normalize_address(wallet_address)
        self._builder_address = _normalize_address(builder_address)
        self._main_address = _normalize_address(main_address)
        self._session = requests.Session()

        # 有 private_key 时构建签名账户
        self._account = None
        if private_key:
            self._account = Account.from_key(private_key)
            if not wallet_address:
                self._wallet_address = _normalize_address(self._account.address)

    def has_follower_auth(self) -> bool:
        """是否有 follower 签名能力。"""
        return self._account is not None

    # ── Follower 钱包签名 ────────────────────────────────────────────────

    def _follower_sign(self, method: str, path: str) -> dict:
        """生成 follower 签名头：X-WALLET + X-TS + X-SIGNATURE。"""
        ts = str(int(time.time()))
        message = f"{method}\n{path}\n{ts}"
        msg_hash = encode_defunct(text=message)
        signed = self._account.sign_message(msg_hash)
        return {
            "X-WALLET": self._wallet_address,
            "X-TS": ts,
            "X-SIGNATURE": signed.signature.hex(),
        }

    def _signed_request(self, method: str, full_path: str,
                        params: dict = None, body: dict = None) -> dict:
        """发送签名请求；签名 path 不带 query string。"""
        url = f"{self._base_url}{full_path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(sorted(params.items()))}"

        headers = self._follower_sign(method, full_path)

        if body is not None:
            headers["Content-Type"] = "application/json"
            resp = self._session.request(
                method, url, headers=headers,
                data=json.dumps(body), timeout=10,
            )
        else:
            resp = self._session.request(method, url, headers=headers, timeout=10)

        resp.raise_for_status()
        return resp.json()

    def _follower_request(self, method: str, path: str,
                          params: dict = None, body: dict = None) -> dict:
        """发送 follower 签名请求。"""
        return self._signed_request(method, f"{_FOLLOWER_PREFIX}{path}", params, body)

    def _main_account_params(self, params: dict | None = None) -> dict:
        """为 follower 查询附加主账号地址，供 Moss 校验绑定关系。"""
        merged = dict(params or {})
        if self._main_address:
            merged["main_address"] = self._main_address
        return merged

    # ── Follower 注册 ────────────────────────────────────────────────────

    def register_follower(self) -> dict:
        """
        注册为 follower（钱包签名验证）。
        已注册地址重复调用直接返回现有记录。
        """
        ts = str(int(time.time()))
        message = (
            f"Moss Follower Register\n"
            f"wallet:{self._wallet_address}\n"
            f"main:{self._main_address}\n"
            f"builder:{self._builder_address}\n"
            f"timestamp:{ts}"
        )
        msg_hash = encode_defunct(text=message)
        signed = self._account.sign_message(msg_hash)

        body = {
            "wallet_address": self._wallet_address,
            "main_address": self._main_address,
            "builder_address": self._builder_address,
            "signature": signed.signature.hex(),
            "timestamp": ts,
        }

        url = f"{self._base_url}{_FOLLOWER_PREFIX}/register"
        headers = {"Content-Type": "application/json"}
        resp = self._session.post(url, headers=headers,
                                  data=json.dumps(body), timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ── 查询接口（follower 鉴权）─────────────────────────────────────────

    def get_fills(self, from_ts: str = None, page_size: int = 50) -> list[dict]:
        """查询成交记录（支持增量：from_ts 为 RFC3339 格式）。"""
        params = {"page_size": str(page_size)}
        if from_ts:
            params["from_ts"] = from_ts
        data = self._follower_request(
            "GET", f"/agents/{self._agent_id}/fills", self._main_account_params(params)
        )
        return data.get("items", [])

    def get_positions(self) -> list[dict]:
        """查询 Agent 当前持仓列表。"""
        data = self._follower_request("GET", f"/agents/{self._agent_id}/positions")
        return data.get("items", [])

    def get_account(self) -> dict:
        """查询 Agent 账户状态。"""
        return self._follower_request("GET", f"/agents/{self._agent_id}/account")

    # ── Source Event 接口 ─────────────────────────────────────────────────

    def get_bootstrap(self) -> dict:
        """获取 Agent 初始状态快照。"""
        return self._follower_request(
            "GET", f"/agents/{self._agent_id}/source-events/bootstrap",
            self._main_account_params(),
        )

    def build_ws_headers(self) -> dict:
        """构建 WebSocket 握手用的 follower 签名头。"""
        path = f"{_FOLLOWER_PREFIX}/agents/{self._agent_id}/source-events/ws"
        return self._follower_sign("GET", path)

    def get_ws_path(self) -> str:
        """返回 WebSocket 路径。"""
        path = f"{_FOLLOWER_PREFIX}/agents/{self._agent_id}/source-events/ws"
        params = self._main_account_params()
        if params:
            return f"{path}?{urllib.parse.urlencode(sorted(params.items()))}"
        return path

    # ── 公开查询接口（无需签名）─────────────────────────────────────────

    def get_agent_info(self) -> dict:
        """查询 Agent 元数据（公开 API，无需签名）。"""
        url = f"{self._base_url}/api/v2/moss/trader/realtime/bots/{self._agent_id}"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ── Copy Trading 写接口 ────────────────────────────────────────────────

    def post_copy_trading_heartbeat(self, body: dict) -> dict:
        """上报当前跟单会话 heartbeat。"""
        return self._signed_request(
            "POST",
            f"{_COPY_TRADING_PREFIX}/sessions/heartbeat",
            body=body,
        )

    def post_copy_trading_trades_batch(self, body: dict) -> dict:
        """批量上报本地跟单交易结果。"""
        return self._signed_request(
            "POST",
            f"{_COPY_TRADING_PREFIX}/trades:batch",
            body=body,
        )

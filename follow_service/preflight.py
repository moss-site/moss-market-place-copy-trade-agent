"""
启动前和下单前的授权校验。

检查项：
1. main_address 是否已将 wallet_address 授权为 Agent（userRole/extraAgents）
2. standard 模式：main_address 是否已授权 builder_address（approvedBuilders）
3. contract_agent 模式：仍校验 wallet_address 的 agent 归属，但跳过 builder 授权
"""

import logging
import re
import time

from . import config as cfg

logger = logging.getLogger("follow_agent.preflight")
_EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _post_info(api_url: str, payload: dict) -> list | dict:
    import requests

    r = requests.post(f"{api_url}/info", json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def _is_evm_address(value: str) -> bool:
    return bool(_EVM_ADDRESS_RE.match(str(value or "").strip()))


def _validate_config_completeness(
    *,
    main_address: str,
    wallet_address: str,
    auth_mode: str,
    errors: list[str],
) -> None:
    """Fail before auth calls when required runtime fields are incomplete."""
    api_url = str(cfg.get("hl_api_url", "") or "").strip()
    if not api_url:
        errors.append("hl_api_url 未配置：请先选择 Hyperliquid 网络。")
        logger.error(errors[-1])

    if main_address and not _is_evm_address(main_address):
        errors.append("main_address 格式不正确：必须是 0x 开头的 EVM 地址。")
        logger.error(errors[-1])
    if wallet_address and not _is_evm_address(wallet_address):
        errors.append("wallet_address 格式不正确：必须是 0x 开头的 EVM 地址。")
        logger.error(errors[-1])

    if auth_mode != "contract_agent":
        return

    moss_cfg = cfg.get_moss_source_config(migrate_bot_id=True)
    if not isinstance(moss_cfg, dict) or not moss_cfg.get("enabled"):
        errors.append("moss_source.enabled 未开启：contract-agent 跟单必须配置 Moss Agent。")
        logger.error(errors[-1])
    else:
        if not str(moss_cfg.get("base_url", "") or "").strip():
            errors.append("moss_source.base_url 未配置。")
            logger.error(errors[-1])
        agent_id = str(moss_cfg.get("agent_id", "") or "").strip()
        if not agent_id:
            errors.append("moss_source.agent_id 未配置。")
            logger.error(errors[-1])
        elif not agent_id.startswith("agt_"):
            errors.append("moss_source.agent_id 格式不正确：应为 agt_xxx。")
            logger.error(errors[-1])

    report_cfg = cfg.get_agent_protocol_report_config()
    if not isinstance(report_cfg, dict) or not report_cfg.get("enabled"):
        return

    base_url = str(report_cfg.get("base_url", "") or "").strip()
    agent_address = str(report_cfg.get("agent_address", "") or "").strip()
    executor_address = str(report_cfg.get("executor_address", "") or "").strip()
    if not base_url:
        errors.append("agent_protocol_report.base_url 未配置。")
        logger.error(errors[-1])
    if not _is_evm_address(agent_address):
        errors.append("agent_protocol_report.agent_address 未配置或格式不正确。")
        logger.error(errors[-1])
    if not _is_evm_address(executor_address):
        errors.append("agent_protocol_report.executor_address 未配置或格式不正确。")
        logger.error(errors[-1])
    elif wallet_address and executor_address.lower() != wallet_address.lower():
        errors.append("agent_protocol_report.executor_address 必须等于 wallet_address/executor。")
        logger.error(errors[-1])
    try:
        chain_id = int(report_cfg.get("chain_id"))
        if chain_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("agent_protocol_report.chain_id 必须是正整数。")
        logger.error(errors[-1])


def check_authorization(raise_on_fail: bool = True) -> bool:
    """
    校验主账号授权状态。

    Returns:
        True  — 全部授权通过
        False — 有授权缺失（raise_on_fail=False 时）

    Raises:
        RuntimeError — 授权缺失且 raise_on_fail=True
    """
    api_url = cfg.get("hl_api_url", "https://api.hyperliquid-testnet.xyz")
    main_address = cfg.get("main_address", "").lower()
    wallet_address = cfg.get("wallet_address", "").lower()
    private_key = cfg.get("private_key", "")
    builder_address = cfg.get_builder_address().lower()
    auth_mode = cfg.get_hyperliquid_auth_mode()
    builder_fee_enabled = cfg.is_builder_fee_enabled()

    errors: list[str] = []
    if not main_address:
        msg = "main_address 未配置：请先配置主钱包地址。"
        logger.error(msg)
        errors.append(msg)
    if not wallet_address:
        msg = "wallet_address 未配置：请先生成 Agent Wallet。"
        logger.error(msg)
        errors.append(msg)
    _validate_config_completeness(
        main_address=main_address,
        wallet_address=wallet_address,
        auth_mode=auth_mode,
        errors=errors,
    )
    if errors:
        if raise_on_fail:
            raise RuntimeError("授权校验失败:\n" + "\n".join(f"  - {e}" for e in errors))
        return False

    # ── 0. 私钥地址一致性校验 ─────────────────────────────────────────────
    if private_key and wallet_address:
        try:
            from eth_account import Account

            signer = Account.from_key(private_key).address.lower()
            if signer != wallet_address:
                msg = (
                    f"private_key 地址不匹配：私钥推导地址 {signer} != "
                    f"wallet_address {wallet_address}。"
                )
                logger.error(msg)
                errors.append(msg)
            else:
                logger.info("Wallet key OK: wallet=%s", wallet_address[:10])
        except Exception as e:
            msg = f"private_key 校验失败: {e}"
            logger.error(msg)
            errors.append(msg)
    elif not private_key:
        msg = "private_key 未配置：无法校验交易账号私钥。"
        logger.error(msg)
        errors.append(msg)

    if errors:
        if raise_on_fail:
            raise RuntimeError("授权校验失败:\n" + "\n".join(f"  - {e}" for e in errors))
        return False

    # ── 1. Agent 授权校验 ──────────────────────────────────────────────────
    # 优先用 userRole 查 wallet_address：role=="agent" 且 data.user==main_address
    # 若不满足，再回退查 extraAgents（兼容旧方式追加的 agent）
    if main_address and wallet_address:
        try:
            role_resp = _post_info(api_url, {"type": "userRole", "user": wallet_address})
            authorized = (
                role_resp.get("role") == "agent"
                and role_resp.get("data", {}).get("user", "").lower() == main_address
            )
            if not authorized:
                # 回退：extraAgents
                now_ms = int(time.time() * 1000)
                agents = _post_info(api_url, {"type": "extraAgents", "user": main_address})
                authorized = any(
                    a.get("address", "").lower() == wallet_address
                    and a.get("validUntil", 0) > now_ms
                    for a in agents
                )
            if authorized:
                logger.info("Agent auth OK: main=%s agent=%s mode=%s", main_address[:10], wallet_address[:10], auth_mode)
            else:
                if auth_mode == "contract_agent":
                    msg = (
                        f"Contract-agent 授权未生效：交易地址 {wallet_address} 还不是 "
                        f"{main_address} 的 Hyperliquid agent。请确认 CoreWriter addApiWallet "
                        "已成功，并且 userRole/extraAgents 已同步。"
                    )
                else:
                    msg = (
                        f"Agent 未授权：主账号 {main_address} 尚未授权 {wallet_address} 为 Agent，"
                        "请在 Hyperliquid 页面完成 Agent 授权。"
                    )
                logger.error(msg)
                errors.append(msg)
        except Exception as e:
            msg = f"Agent 授权查询失败: {e}"
            logger.error(msg)
            errors.append(msg)
    elif main_address or wallet_address:
        logger.warning("main_address 或 wallet_address 未配置，跳过 Agent 授权查询")

    # ── 2. Builder 授权校验 ────────────────────────────────────────────────
    if not builder_fee_enabled:
        logger.info("Builder fee disabled by hyperliquid_auth.mode=%s: skipping approvedBuilders preflight", auth_mode)
    elif main_address and builder_address:
        try:
            approved = _post_info(api_url, {"type": "approvedBuilders", "user": main_address})
            authorized = any(b.lower() == builder_address for b in approved)
            if authorized:
                logger.info("Builder auth OK: main=%s builder=%s", main_address[:10], builder_address[:10])
            else:
                msg = (
                    f"Builder 未授权：主账号 {main_address} 尚未授权 builder {builder_address}，"
                    "请在 Hyperliquid 页面完成 Builder 授权。"
                )
                logger.error(msg)
                errors.append(msg)
        except Exception as e:
            msg = f"Builder 授权查询失败: {e}"
            logger.error(msg)
            errors.append(msg)
    else:
        if not builder_address:
            msg = "builder_address 未配置：无法校验 Builder 授权。"
            logger.error(msg)
            errors.append(msg)
        else:
            logger.warning("main_address 未配置，跳过 Builder 授权查询")

    if errors:
        if raise_on_fail:
            raise RuntimeError("授权校验失败:\n" + "\n".join(f"  - {e}" for e in errors))
        return False

    return True

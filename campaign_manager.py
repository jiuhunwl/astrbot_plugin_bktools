"""活动审核发放 CDK 编排器（single-flight / 冷却 / 存储 / 快照）。

编排链路（对齐 design-plugin-campaign.md 第四节，任一环节失败即终止）：

    活动定位 → 启停/窗口判定 → 本地防刷（单次 + 冷却 + single-flight）
    → GitHub star 校验 → 问卷命中校验 → 生成 CDK（retries=0，不自动重试）
    → 写本地记录（只增不删） → 返回聊天文案

安全与隐私：
- fail-closed：网络错误 / 响应结构异常 / 校验器未就绪一律按「服务繁忙」处理，
  **绝不放行**；
- 联系方式仅用于请求体与本地哈希去重，**不明文落日志/聊天**
  （sha256 盐 = api_key 派生的 16 位前缀）；聊天与日志只出掩码；
- 本模块不依赖 astrbot，可独立单测。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from github_star import GitHubStarVerifier, StarCheckResult
from open_api_client import GenerateResult, KEY_TYPE_ADMIN, OpenApiError, OpenApiClient
from survey_adapter import SurveyOutcome, SurveyStatus, SurveyVerifier

logger = logging.getLogger("bktools.campaign")

DEFAULT_STATE_REL_PATH = "campaign_state.json"

# 冷却与并发控制的保守默认值（可在配置覆盖）
DEFAULT_COOLDOWN_SEC = 300
DEFAULT_MAX_BATCH_PER_CHAT_DAY = 20

_BUSY_DETAIL = "服务暂时繁忙，请稍后再试"


class ClaimResult(str, Enum):
    """claim() 的收敛结果码（供 main.py 组装聊天文案）。"""

    GRANTED = "granted"
    ALREADY_CLAIMED = "already_claimed"          # 本地已领过
    COOLDOWN = "cooldown"                        # 冷却中
    IN_PROGRESS = "in_progress"                  # single-flight：上一次还在跑
    CAMPAIGN_NOT_FOUND = "campaign_not_found"
    CAMPAIGN_DISABLED = "campaign_disabled"
    CAMPAIGN_NOT_STARTED = "campaign_not_started"
    CAMPAIGN_EXPIRED = "campaign_expired"
    STAR_NOT_CONFIGURED = "star_not_configured"  # 要求 GitHub 但未提供用户名
    STAR_NOT_STARRED = "star_not_starred"
    STAR_USER_NOT_FOUND = "star_user_not_found"
    SURVEY_NOT_CONFIGURED = "survey_not_configured"
    SURVEY_NOT_MATCH = "survey_not_match"
    SURVEY_UNAVAILABLE = "survey_unavailable"
    KEY_TYPE_FORBIDDEN = "key_type_forbidden"    # opk_ 无生成 scope
    SCOPE_DENIED = "scope_denied"
    RATE_LIMITED = "rate_limited"
    IP_FORBIDDEN = "ip_forbidden"
    GENERATE_FAILED = "generate_failed"          # 5xx / 网络
    CONFIG_ERROR = "config_error"


@dataclass(frozen=True)
class StarRequirement:
    """活动级 GitHub star 要求。enabled=False 时整个校验环节跳过。"""

    enabled: bool = False
    owner: str = ""
    repo: str = ""


@dataclass(frozen=True)
class SurveyRequirement:
    """活动级问卷命中要求。provider 指向 open_api.survey_providers 条目的 type。"""

    enabled: bool = False
    provider: str = ""
    form_id: str = ""  # 覆盖 provider 默认问卷 ID（卷王=问卷 id）
    contact_field_id: str = ""  # 卷王联系方式题 id（缺省回落 provider 配置）
    contact_source: str = "github_login"  # github_login | chat_member


@dataclass(frozen=True)
class CampaignDef:
    """单个活动配置（解析自 _conf_schema.json 的 campaigns 模板列表）。"""

    id: str
    name: str = ""
    enabled: bool = True
    start_at: str = ""
    end_at: str = ""
    type_code: str = ""  # 兑换码类型标识（= bp_redeem_code_type.code）
    per_user_limit: int = 1
    per_ip_limit: int = 0
    star: StarRequirement = field(default_factory=StarRequirement)
    survey: SurveyRequirement = field(default_factory=SurveyRequirement)
    reward_notice: str = ""


@dataclass(frozen=True)
class GrantResult:
    """claim() 的返回。code 仅在 GRANTED 时非空（明文只出现一次）。"""

    result: ClaimResult
    code: str = ""
    batch_id: str = ""
    campaign_id: str = ""
    github_login: str = ""
    code_masked: str = ""
    client_ip: str = ""
    detail: str = ""  # 面向日志的诊断信息；不含联系方式明文


# ---------------------------------------------------------------- 工具


def mask_contact(value: Any) -> str:
    """联系方式掩码：保留前 3 与后 4 位；短值整体打码。"""
    text = str(value or "").strip()
    if not text:
        return "(未提供)"
    if len(text) <= 7:
        return "****"
    return f"{text[:3]}****{text[-4:]}"


def hash_contact(value: Any, salt: str) -> str:
    """联系方式稳定哈希（跨活动去重）：sha256(salt + '|' + value)。"""
    payload = f"{salt}|{str(value or '').strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def salt_from_key(api_key: Any) -> str:
    """由开放 Key 派生 16 位盐（不落明文 Key）。"""
    return hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()[:16]


def mask_code(code: Any) -> str:
    """兑换码掩码：保留前 4 与后 4 位（形如 ABCD-****-M NOP）。"""
    text = str(code or "").strip()
    if not text:
        return "(空)"
    if len(text) <= 8:
        return "****"
    return f"{text[:4]}-****-{text[-4:]}"


def _parse_dt(text: str) -> Optional[datetime]:
    """解析活动窗口/记录时间戳。

    支持 ISO 8601（``2026-01-01T12:00:00+00:00`` / ``Z`` 后缀）与
    ``YYYY-MM-DD[ HH:MM[:SS]]``、``YYYY/MM/DD[ HH:MM[:SS]]``。
    带时区的按真实时区换算到 UTC；不带时区的（运营人员手填的窗口）
    沿用既有约定，按 UTC 解释——保持历史行为不变。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for pattern in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d",
        ):
            try:
                parsed = datetime.strptime(raw, pattern)
            except ValueError:
                continue
            if parsed is not None:
                break
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_campaign_def(raw: Any) -> Optional[CampaignDef]:
    """把 _conf_schema.json 的 campaigns 条目解析为 CampaignDef；非法返回 None。"""
    if not isinstance(raw, dict):
        return None
    campaign_id = str(raw.get("id") or "").strip()
    type_code = str(raw.get("type_code") or raw.get("type_id") or "").strip()
    if not campaign_id or not type_code:
        logger.warning("活动配置缺少 id/type_code，已跳过: %s", dict(list(raw.items())[:4]))
        return None
    star_raw = raw.get("star") or {}
    survey_raw = raw.get("survey") or {}
    return CampaignDef(
        id=campaign_id,
        name=str(raw.get("name") or campaign_id),
        enabled=bool(raw.get("enabled", True)),
        start_at=str(raw.get("start_at") or "").strip(),
        end_at=str(raw.get("end_at") or "").strip(),
        type_code=type_code,
        per_user_limit=max(0, int(raw.get("per_user_limit", 1) or 0)),
        per_ip_limit=max(0, int(raw.get("per_ip_limit", 0) or 0)),
        star=StarRequirement(
            enabled=bool(star_raw.get("enabled", False)),
            owner=str(star_raw.get("owner") or "").strip(),
            repo=str(star_raw.get("repo") or "").strip(),
        ),
        survey=SurveyRequirement(
            enabled=bool(survey_raw.get("enabled", False)),
            provider=str(survey_raw.get("provider") or "").strip().lower(),
            form_id=str(survey_raw.get("form_id") or "").strip(),
            contact_field_id=str(survey_raw.get("contact_field_id") or "").strip(),
            contact_source=str(
                survey_raw.get("contact_source") or "github_login"
            ).strip().lower(),
        ),
        reward_notice=str(raw.get("reward_notice") or "").strip(),
    )


class CampaignStateStore:
    """本地参与记录（JSON 文件，tmp+os.replace 原子写，只增不删）。

    - 所有写操作串行化于单个 ``asyncio.Lock``；
    - 读取做防御性解析，文件损坏不影响运行（回空记录并记日志）；
    - **只增不删**：记录用于审计与本地防刷，删除会造成「重领」。
    """

    def __init__(self, path: str) -> None:
        self._path = str(path or "").strip()
        if not self._path:
            raise ValueError("state_file 不能为空")
        self._lock = asyncio.Lock()

    @property
    def path(self) -> str:
        return self._path

    async def load(self) -> List[Dict[str, Any]]:
        async with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as ex:
            logger.error("活动状态文件读取失败（按空记录处理）: %s", type(ex).__name__)
            return []
        claims = payload.get("claims") if isinstance(payload, dict) else payload
        if not isinstance(claims, list):
            return []
        return [item for item in claims if isinstance(item, dict)]

    async def append_claim(self, record: Dict[str, Any]) -> None:
        """追加一条发放记录（原子写）。"""
        async with self._lock:
            claims = self._read_unlocked()
            claims.append(record)
            self._write_unlocked({"claims": claims})

    def _write_unlocked(self, payload: Dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".campaign_state.", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise


class CampaignManager:
    """活动审核发放编排器。

    依赖全部以构造参数注入，便于单测：
      - ``client``: 开放 API 客户端（生成 CDK）；
      - ``star_verifier``: GitHub star 校验器（可空 = 全部活动不做 star 校验）；
      - ``survey_verifiers``: provider type → 校验器 映射；
      - ``chat_member_provider``: ``chat_key -> 联系方式`` 提取器（contact_source=
        chat_member 时使用；main.py 从 AstrBot 事件里取群成员信息）。
    """

    def __init__(
        self,
        *,
        client: OpenApiClient,
        state_store: CampaignStateStore,
        star_verifier: Optional[GitHubStarVerifier] = None,
        survey_verifiers: Optional[Dict[str, SurveyVerifier]] = None,
        chat_member_provider: Optional[Callable[[Any, str], str]] = None,
        cooldown_sec: int = DEFAULT_COOLDOWN_SEC,
        max_claims_per_chat_day: int = DEFAULT_MAX_BATCH_PER_CHAT_DAY,
    ) -> None:
        if client is None:
            raise ValueError("client 不能为空")
        self._client = client
        self._state = state_store
        self._star = star_verifier
        self._surveys: Dict[str, SurveyVerifier] = dict(survey_verifiers or {})
        self._chat_member_provider = chat_member_provider
        self._cooldown_sec = max(0, int(cooldown_sec))
        self._max_per_chat_day = max(1, int(max_claims_per_chat_day))
        self._campaigns: Dict[str, CampaignDef] = {}
        self._inflight: Dict[str, asyncio.Future] = {}
        self._flight_lock = asyncio.Lock()
        self._salt = salt_from_key(getattr(client, "masked_key", ""))

    # ------------------------------------------------------------- 配置

    def load_campaigns(self, raw_list: Any) -> Dict[str, CampaignDef]:
        """（重）加载活动配置。返回可运行的活动 id → 定义 映射。"""
        campaigns: Dict[str, CampaignDef] = {}
        if isinstance(raw_list, dict):  # 容错：dict 形态按 values 取
            raw_list = list(raw_list.values())
        for raw in raw_list or []:
            definition = build_campaign_def(raw)
            if definition is None:
                continue
            if definition.id in campaigns:
                logger.warning("活动 id 重复，后者覆盖前者: %s", definition.id)
            campaigns[definition.id] = definition
        self._campaigns = campaigns
        return campaigns

    def campaigns(self) -> Dict[str, CampaignDef]:
        return dict(self._campaigns)

    def get(self, campaign_id: str) -> Optional[CampaignDef]:
        return self._campaigns.get(str(campaign_id or "").strip())

    # ------------------------------------------------------------- 门禁

    def check_gate(self, definition: CampaignDef, *, now_ts: float = 0.0) -> ClaimResult:
        """启停 + 时间窗口判定（now_ts 可注入供测试）。"""
        if not definition.enabled:
            return ClaimResult.CAMPAIGN_DISABLED
        timestamp = float(now_ts or 0.0) or time.time()
        start = _parse_dt(definition.start_at)
        if start is not None and datetime.fromtimestamp(timestamp, tz=timezone.utc) < start:
            return ClaimResult.CAMPAIGN_NOT_STARTED
        end = _parse_dt(definition.end_at)
        if end is not None and datetime.fromtimestamp(timestamp, tz=timezone.utc) >= end:
            return ClaimResult.CAMPAIGN_EXPIRED
        return ClaimResult.GRANTED

    # ------------------------------------------------------------- 主流程

    async def claim(
        self,
        chat_key: str,
        campaign_id: str,
        github_login: str,
        *,
        contact_override: str = "",
        chat_ref: str = "",
    ) -> GrantResult:
        """执行一次活动领取。同 (chat_key, campaign_id) 串行化（single-flight）。"""
        key = str(chat_key or "").strip()
        target = str(campaign_id or "").strip()
        login = str(github_login or "").strip()
        if not key or not target:
            return GrantResult(ClaimResult.CONFIG_ERROR, detail="缺少聊天标识或活动 ID")

        async with self._flight_lock:
            pending = self._inflight.get((key, target))
            if pending is not None and not pending.done():
                return GrantResult(
                    ClaimResult.IN_PROGRESS,
                    campaign_id=target,
                    detail="上一次领取仍在处理中",
                )
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._inflight[(key, target)] = future
        try:
            result = await self._claim_inner(key, target, login, contact_override, chat_ref)
            future.set_result(result)
            return result
        except BaseException as ex:  # 兜底：任何异常都收敛为结构化结果
            with contextlib.suppress(BaseException):
                future.set_result(GrantResult(ClaimResult.GENERATE_FAILED))
            logger.error("活动领取异常: %s", type(ex).__name__)
            return GrantResult(
                ClaimResult.GENERATE_FAILED,
                campaign_id=target,
                detail=f"领取流程异常: {type(ex).__name__}",
            )
        finally:
            async with self._flight_lock:
                self._inflight.pop((key, target), None)

    async def _claim_inner(
        self,
        key: str,
        target: str,
        login: str,
        contact_override: str,
        chat_ref: str = "",
    ) -> GrantResult:
        definition = self.get(target)
        if definition is None:
            return GrantResult(ClaimResult.CAMPAIGN_NOT_FOUND, campaign_id=target)

        gate = self.check_gate(definition)
        if gate is not ClaimResult.GRANTED:
            return GrantResult(gate, campaign_id=target)

        claims = await self._state.load()
        local = self._local_guard(claims, key, target)
        if local is not ClaimResult.GRANTED:
            return GrantResult(local, campaign_id=target)

        # 前置门槛：活动要求 star 但没给用户名，直接提示而不浪费一次外部请求
        if definition.star.enabled and not login:
            return GrantResult(
                ClaimResult.STAR_NOT_CONFIGURED, campaign_id=target, github_login=login
            )
        if definition.star.enabled and not (definition.star.owner and definition.star.repo):
            return GrantResult(
                ClaimResult.CONFIG_ERROR,
                campaign_id=target,
                detail="活动开启 star 校验但缺 owner/repo",
            )

        # GitHub star 校验
        if definition.star.enabled and self._star is not None:
            outcome = await self._star.verify(
                login, definition.star.owner, definition.star.repo
            )
            if outcome is StarCheckResult.USER_NOT_FOUND:
                return GrantResult(
                    ClaimResult.STAR_USER_NOT_FOUND,
                    campaign_id=target,
                    github_login=login,
                    detail="GitHub 用户名不存在",
                )
            if outcome is StarCheckResult.NOT_STARRED:
                return GrantResult(
                    ClaimResult.STAR_NOT_STARRED,
                    campaign_id=target,
                    github_login=login,
                    detail=f"未 star {definition.star.owner}/{definition.star.repo}",
                )
            if outcome is StarCheckResult.ERROR:
                return GrantResult(
                    ClaimResult.SURVEY_UNAVAILABLE,
                    campaign_id=target,
                    detail=f"GitHub 校验不可用: {_BUSY_DETAIL}",
                )

        # 问卷命中校验
        if definition.survey.enabled:
            contact = contact_override or self._resolve_contact(
                definition, key, login
            )
            if definition.survey.contact_source == "chat_member" and not contact:
                contact = login  # 取不到聊天身份时回落 GitHub 用户名
            verifier = self._surveys.get(definition.survey.provider)
            if verifier is None:
                return GrantResult(
                    ClaimResult.SURVEY_NOT_CONFIGURED,
                    campaign_id=target,
                    detail=f"问卷 provider 未配置: {definition.survey.provider}",
                )
            survey_outcome: SurveyOutcome = await verifier.verify(
                contact, definition.survey.form_id or None
            )
            if survey_outcome.status is SurveyStatus.NOT_MATCH:
                return GrantResult(
                    ClaimResult.SURVEY_NOT_MATCH,
                    campaign_id=target,
                    detail=f"联系方式 {mask_contact(contact)} 未命中问卷",
                )
            if survey_outcome.status is SurveyStatus.ERROR:
                return GrantResult(
                    ClaimResult.SURVEY_UNAVAILABLE,
                    campaign_id=target,
                    detail=f"问卷校验不可用（{survey_outcome.detail}）",
                )

        # 生成 CDK（唯一非幂等调用；失败不自动重试）
        if self._client.key_type != KEY_TYPE_ADMIN:
            return GrantResult(
                ClaimResult.KEY_TYPE_FORBIDDEN,
                campaign_id=target,
                detail="当前为普通用户 Key（opk_），无 CDK 生成 scope",
            )
        try:
            generated: GenerateResult = await self._client.generate_redeem_codes(
                definition.type_code,
                count=1,
                per_user_limit=definition.per_user_limit,
                per_ip_limit=definition.per_ip_limit,
                campaign_ref=f"bktools:{target}",
            )
        except OpenApiError as ex:
            stage, detail = self._classify_open_error(ex)
            return GrantResult(
                stage,
                campaign_id=target,
                github_login=login,
                client_ip=ex.client_ip,
                detail=f"生成失败: {ex.describe()}",
            )
        if not generated.codes:
            return GrantResult(
                ClaimResult.GENERATE_FAILED,
                campaign_id=target,
                detail="服务端未返回明文码，无法发放",
            )
        code = generated.codes[0]

        # 写本地记录（失败不撤销已生成的码：宁多不可少，后端 per_user_limit 兜底）
        record = {
            "ts": _iso_now(),
            "chat_key": key,
            "chat_ref": str(chat_ref or "")[:64],
            "campaign_id": target,
            "github_login": login,
            "contact_hash": hash_contact(login, self._salt),
            "code_masked": mask_code(code),
            "batch_id": str(generated.batch_id or ""),
        }
        try:
            await self._state.append_claim(record)
        except OSError as ex:
            logger.error("活动记录写入失败（不阻断发放）: %s", type(ex).__name__)

        logger.info(
            "活动发放成功: campaign=%s chat=%s login=%s code=%s batch=%s",
            target,
            key,
            login or "-",
            mask_code(code),
            generated.batch_id or "-",
        )
        return GrantResult(
            ClaimResult.GRANTED,
            code=code,
            batch_id=generated.batch_id,
            campaign_id=target,
            github_login=login,
            code_masked=mask_code(code),
            detail="发放成功",
        )

    # ------------------------------------------------------------- 辅助

    def _resolve_contact(
        self, definition: CampaignDef, key: str, login: str
    ) -> str:
        """按 contact_source 取联系方式（绝不打印明文）。"""
        requirement = definition.survey
        if requirement.contact_source == "chat_member" and self._chat_member_provider:
            try:
                return str(self._chat_member_provider(key, requirement.contact_field_id) or "").strip()
            except (TypeError, ValueError) as ex:
                logger.warning("聊天联系方式提取失败: %s", type(ex).__name__)
        return login

    @staticmethod
    def _classify_open_error(ex: OpenApiError) -> Tuple[ClaimResult, str]:
        """把开放 API 错误码映射到用户可见阶段。"""
        code = str(ex.code or "")
        if code.startswith("openapi."):
            tail = code.split(".", 1)[-1]
            if tail == "rate_limited":
                return ClaimResult.RATE_LIMITED, "频控限制"
            if tail in ("type_not_found", "invalid_request"):
                # 配置错了 type_code（平台端没有这个兑换码类型）：
                # 不是限流也不是故障，重发多少次都不会好。
                return ClaimResult.CONFIG_ERROR, "活动奖励类型配置有误"
            if tail == "ip_forbidden":
                return ClaimResult.IP_FORBIDDEN, "出口 IP 未加白名单"
            if tail == "scope_denied":
                return ClaimResult.SCOPE_DENIED, "Key 未授权该端点"
            return ClaimResult.GENERATE_FAILED, tail
        return ClaimResult.GENERATE_FAILED, code or f"HTTP {ex.status}"

    async def load_claims(self) -> List[Dict[str, Any]]:
        return await self._state.load()

    def _local_guard(
        self, claims: List[Dict[str, Any]], key: str, target: str
    ) -> ClaimResult:
        """本地防刷（弱防线，后端账号/IP 双维度才是硬保证）。

        三层判定，顺序有讲究：
        1. 同聊天用户对**本活动**已领过 → 拒绝（聊天 ID 单次）；
        2. 同聊天用户在冷却窗口内**领过任意活动** → 冷却中（抑制跨活动刷量）；
        3. 同聊天用户当日累计达到总量上限 → 拒绝。
        """
        now = time.time()
        target_claims = 0
        last_any_ts = 0.0
        day_start = now - 86400
        count_today = 0
        for record in claims:
            if str(record.get("chat_key") or "") != key:
                continue
            record_ts = self._to_ts(record.get("ts"))
            if record_ts <= 0:
                continue
            if record_ts >= last_any_ts:
                last_any_ts = record_ts
            if str(record.get("campaign_id") or "") == target:
                target_claims += 1
            if record_ts >= day_start:
                count_today += 1

        if target_claims > 0:
            logger.info(
                "活动重复领取已拦截: chat=%s campaign=%s 已领 %d 次",
                key,
                target,
                target_claims,
            )
            return ClaimResult.ALREADY_CLAIMED
        if self._cooldown_sec > 0 and last_any_ts > 0:
            elapsed = now - last_any_ts
            if elapsed < self._cooldown_sec:
                logger.info(
                    "活动领取冷却中: chat=%s 剩余 %.0fs",
                    key,
                    self._cooldown_sec - elapsed,
                )
                return ClaimResult.COOLDOWN
        if count_today >= self._max_per_chat_day:
            logger.info(
                "活动领取触发当日总量上限: chat=%s count=%d", key, count_today
            )
            return ClaimResult.ALREADY_CLAIMED
        return ClaimResult.GRANTED

    @staticmethod
    def _to_ts(value: Any) -> float:
        raw = str(value or "").strip()
        if not raw:
            return 0.0
        if raw.isdigit():
            return float(raw)
        parsed = _parse_dt(raw)
        if parsed is None:
            return 0.0
        return parsed.timestamp()

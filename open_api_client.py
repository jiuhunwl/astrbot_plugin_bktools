"""系统开放 API 客户端（BugPk 平台 /open-api 平面）。

设计要点（对齐 design-plugin-campaign.md 第三节）：
- 鉴权头仅 ``X-Open-Key``（服务端禁 query/cookie 携带）；
- ``mask_key()`` 永不输出明文：前缀 + **** + 末 4 位（如 ``opka_****abcd``）；
- ``generate_redeem_codes()`` 走 POST /open-api/redeem-codes，
  **retries=0 绝不重试**——生成 CDK 非幂等，重试可能造成重复发码；
- 熔断按 endpoint（路径）粒度，复用 bktools_runtime.RuntimeManager 的约定；
- 失败分类抛 :class:`OpenApiError`：稳定错误码优先取 ``X-Bugpk-Openapi-Code``
  响应头，其次取响应体 ``data.code``（openapi.* 系列错误码）；
- ``can_generate`` 前缀预检：仅 ``opka_`` 具备生成前提——前缀只是友好提示，
  最终以服务端 scope 门（openapi:redeem-code:generate）为准。

本模块不依赖 astrbot，可独立单测。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from bktools_runtime import RuntimeManager

logger = logging.getLogger("bktools.openapi")

# 开放 Key 明文前缀分级：普通 = opk_，管理员作用域 = opka_
KEY_TYPE_NORMAL = "opk_"
KEY_TYPE_ADMIN = "opka_"
_KEY_PREFIXES = (KEY_TYPE_ADMIN, KEY_TYPE_NORMAL)

# 服务端响应体错误信息字段（与响应契约一致：message / error）
_MESSAGE_FIELDS = ("message", "error", "msg")


class OpenApiError(Exception):
    """开放 API 调用失败（含 HTTP 状态码与稳定错误码）。

    - ``status``: HTTP 状态码，网络层失败时为 0；
    - ``code``: 稳定错误码（如 ``openapi.rate_limited``），缺失为空串；
    - ``message``: 面向日志的简述（服务端文案，已尽量精简）；
    - ``client_ip``: ip_forbidden 时服务端回带的出口 IP（可空）。
    """

    def __init__(
        self,
        status: int = 0,
        message: str = "",
        code: str = "",
        client_ip: str = "",
    ) -> None:
        self.status = int(status or 0)
        self.code = str(code or "")
        self.message = str(message or "")
        self.client_ip = str(client_ip or "")
        super().__init__(self.describe())

    def describe(self) -> str:
        head = f"HTTP {self.status}" if self.status else "网络错误"
        parts = [head]
        if self.code:
            parts.append(f"[{self.code}]")
        if self.message:
            parts.append(self.message)
        return " ".join(parts)

    def __str__(self) -> str:  # pragma: no cover - 纯展示
        return self.describe()


@dataclass
class GenerateResult:
    """POST /open-api/redeem-codes 的解析结果。"""

    codes: List[str] = field(default_factory=list)
    batch_id: str = ""
    count: int = 0


def mask_key(key: Any) -> str:
    """开放 Key 脱敏：保留前缀 + **** + 末 4 位，绝不打印明文。

    示例：``opka_1a2b3c4d...wxyz`` → ``opka_****wxyz``。
    """
    raw = str(key or "").strip()
    if not raw:
        return "(未配置)"
    prefix = ""
    for marker in _KEY_PREFIXES:
        if raw.startswith(marker):
            prefix = marker
            raw = raw[len(marker):]
            break
    if len(raw) <= 8:
        return f"{prefix}****"
    return f"{prefix}****{raw[-4:]}"


def normalize_key_type(value: Any) -> str:
    """归一化 key_type；非法值回退 opk_。"""
    raw = str(value or "").strip().lower()
    return KEY_TYPE_ADMIN if raw == KEY_TYPE_ADMIN else KEY_TYPE_NORMAL


class OpenApiClient:
    """系统开放 API 客户端（/open-api 平面，X-Open-Key 鉴权）。

    传输层约定沿用 bktools_runtime.SafeHttpClient：有界响应、按 endpoint
    熔断、仅对网络错误/超时/5xx 重试（生成端点固定 retries=0）。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        key_type: str = KEY_TYPE_NORMAL,
        verify_tls: bool = True,
        *,
        runtime: Optional[RuntimeManager] = None,
        trace_factory: Optional[Callable[[], aiohttp.TraceConfig]] = None,
        timeout_sec: int = 30,
        user_agent: str = "BKtools-OpenApiClient/1.7.0",
        max_response_bytes: int = 1024 * 1024,
        retry_count: int = 2,
        retry_backoff_sec: float = 0.5,
        circuit_threshold: int = 5,
        circuit_recovery_sec: int = 60,
    ) -> None:
        base = str(base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("base_url 不能为空")
        if not base.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http(s):// 开头")
        self._base_url = base
        self._api_key = str(api_key or "").strip()
        if not self._api_key:
            raise ValueError("api_key 不能为空")
        self._key_type = normalize_key_type(key_type)
        self._verify_tls = bool(verify_tls)
        self._runtime = runtime or RuntimeManager()
        self._trace_factory = trace_factory
        self._timeout_sec = max(5, int(timeout_sec))
        self._user_agent = user_agent
        self._max_response_bytes = max(16 * 1024, int(max_response_bytes))
        self._retry_count = max(0, min(int(retry_count), 5))
        self._retry_backoff_sec = max(0.0, min(float(retry_backoff_sec), 10.0))
        self._circuit_threshold = max(1, int(circuit_threshold))
        self._circuit_recovery_sec = max(1, int(circuit_recovery_sec))
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------- 属性

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def key_type(self) -> str:
        return self._key_type

    @property
    def masked_key(self) -> str:
        return mask_key(self._api_key)

    @property
    def can_generate(self) -> bool:
        """前缀预检：opka_ 才具备 CDK 生成前提（最终以服务端 scope 门为准）。"""
        return self._key_type == KEY_TYPE_ADMIN and self._api_key.startswith(KEY_TYPE_ADMIN)

    # ------------------------------------------------------------- 会话

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector: Optional[aiohttp.TCPConnector] = None
            if not self._verify_tls:
                # verify_tls=False：跳过证书校验（自签部署场景）
                connector = aiohttp.TCPConnector(ssl=False)
            kwargs: Dict[str, Any] = {
                "timeout": aiohttp.ClientTimeout(total=self._timeout_sec),
                "headers": {"User-Agent": self._user_agent},
            }
            if connector is not None:
                kwargs["connector"] = connector
            if self._trace_factory is not None:
                kwargs["trace_configs"] = [self._trace_factory()]
            self._session = aiohttp.ClientSession(**kwargs)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------- 端点

    async def get_account(self) -> Dict[str, Any]:
        """GET /open-api/account：本账号基础信息（含 openApiEnabled 门禁字段）。"""
        data = await self._request_json("GET", "/open-api/account", retries=self._retry_count)
        return data if isinstance(data, dict) else {}

    async def generate_redeem_codes(
        self,
        type_code: str,
        count: int = 1,
        per_user_limit: Optional[int] = None,
        per_ip_limit: Optional[int] = None,
        campaign_ref: str = "",
    ) -> GenerateResult:
        """POST /open-api/redeem-codes：生成 CDK。

        🔴 非幂等操作，**retries=0 绝不重试**（重试可能重复发码）。
        服务端响应需携带明文码（data.codes 列表或 data.code 字符串）。

        type_code 是配置里的类型标识（如 ``NEWBIE``）；写全数字时按
        typeId 提交，兼容按自增 ID 配置的存量部署。
        """
        if not self.can_generate:
            raise OpenApiError(
                0,
                "当前为普通用户 Key（opk_），无 CDK 生成 scope，请配置管理员作用域 Key（opka_）",
                code="client.admin_scope_required",
            )
        code = str(type_code or "").strip()
        if not code:
            raise OpenApiError(0, "兑换码类型标识为空", code="client.bad_payload")
        body: Dict[str, Any] = {
            "count": max(1, int(count)),
            "campaignRef": str(campaign_ref or ""),
        }
        if code.isdigit():
            body["typeId"] = int(code)
        else:
            body["typeCode"] = code
        if per_user_limit is not None:
            body["perUserLimit"] = int(per_user_limit)
        if per_ip_limit is not None:
            body["perIpLimit"] = int(per_ip_limit)
        data = await self._request_json(
            "POST", "/open-api/redeem-codes", json_body=body, retries=0
        )
        if not isinstance(data, dict):
            raise OpenApiError(200, "生成端点响应 data 结构异常", code="client.bad_payload")
        codes = _extract_codes(data)
        if not codes:
            # 拿不到明文就视为失败，绝不把批次号当 CDK 发给用户。
            logger.error(
                "生成 CDK 成功但响应缺少明文码（batchId=%s, count=%s）——"
                "请检查服务端 /open-api/redeem-codes 是否返回 codes 字段",
                data.get("batchId"),
                data.get("count"),
            )
            raise OpenApiError(
                200,
                "服务端响应缺少兑换码明文，无法发放",
                code="client.codes_missing",
            )
        return GenerateResult(
            codes=codes,
            batch_id=str(data.get("batchId") or ""),
            count=len(codes),
        )

    # ------------------------------------------------------------- 传输

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        retries: int = 0,
    ) -> Any:
        url = self._base_url + path
        # 熔断按 endpoint（路径）粒度，与解析接口共用 RuntimeManager 约定
        self._runtime.assert_circuit_closed(path)
        attempts = max(1, int(retries) + 1)
        last_error: Optional[BaseException] = None
        last_retryable = False
        for attempt in range(attempts):
            try:
                session = await self._get_session()
                headers = {"X-Open-Key": self._api_key, "Accept": "application/json"}
                async with session.request(
                    method, url, json=json_body, headers=headers, allow_redirects=False
                ) as response:
                    raw = await response.content.read(self._max_response_bytes + 1)
                    if len(raw) > self._max_response_bytes:
                        raise OpenApiError(
                            response.status, "响应超过大小限制", code="client.response_too_large"
                        )
                    text = raw.decode("utf-8", errors="replace")
                    status = int(response.status)
                    if status >= 500:
                        # 5xx：服务端统一错误体 {code, message, error_id}
                        payload = _safe_json(text)
                        message = _first_message(payload) or "服务暂时不可用"
                        raise OpenApiError(status, message, code="")
                    return self._parse_success(status, text, dict(response.headers))
            except asyncio.CancelledError:
                raise
            except OpenApiError as ex:
                # 4xx/契约类错误不重试；5xx 归为可重试
                last_error = ex
                last_retryable = ex.status >= 500
                if not last_retryable or attempt + 1 >= attempts:
                    break
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerTimeoutError,
                asyncio.TimeoutError,
                aiohttp.ClientError,
            ) as ex:
                last_error = ex
                last_retryable = True
                if attempt + 1 >= attempts:
                    break
            if self._retry_backoff_sec > 0:
                await asyncio.sleep(self._retry_backoff_sec * (2**attempt))
        assert last_error is not None
        if last_retryable:
            self._runtime.record_failure(
                path, last_error, self._circuit_threshold, self._circuit_recovery_sec
            )
        if isinstance(last_error, OpenApiError):
            raise last_error
        raise OpenApiError(0, _safe_error_text(last_error), code="client.network_error")

    @staticmethod
    def _parse_success(status: int, text: str, headers: Dict[str, str]) -> Any:
        payload = _safe_json(text)
        if payload is None:
            raise OpenApiError(status, "响应不是有效 JSON", code="client.non_json")
        if not isinstance(payload, dict):
            raise OpenApiError(status, "响应结构异常", code="client.bad_payload")
        # 统一响应契约：{code: 0, data, error, message}
        # 注意 code 是业务码，0 即成功——不能写 `or -1`（0 在 Python 里是 falsy）。
        code = _parse_code(payload.get("code"))
        if code != 0:
            stable = _extract_stable_code(payload, headers)
            raise OpenApiError(
                status,
                _first_message(payload) or "请求被拒绝",
                code=stable,
            )
        return payload.get("data")


# ---------------------------------------------------------------- 助手


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_code(value: Any) -> int:
    """把信封 code 解析为整数；缺失/不可解析一律视为失败码 -1。"""
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        return int(value.strip())
    return -1


def _first_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for field_name in _MESSAGE_FIELDS:
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    return ""


def _extract_stable_code(payload: Dict[str, Any], headers: Dict[str, str]) -> str:
    """稳定错误码：X-Bugpk-Openapi-Code 响应头优先，其次响应体 data.code。"""
    header_value = ""
    for name, value in headers.items():
        if name.lower() == "x-bugpk-openapi-code":
            header_value = str(value or "").strip()
            break
    if header_value:
        return header_value
    data = payload.get("data")
    if isinstance(data, dict):
        code = data.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    return ""


def _extract_codes(data: Dict[str, Any]) -> List[str]:
    """从生成端点响应 data 中提取明文码（codes 列表优先，code 字符串兜底）。"""
    raw_codes = data.get("codes")
    if isinstance(raw_codes, list):
        return [str(item).strip() for item in raw_codes if str(item).strip()]
    single = data.get("code")
    if isinstance(single, str) and single.strip():
        return [single.strip()]
    return []


def _safe_error_text(err: BaseException) -> str:
    text = str(err or "").strip()
    return (text[:200] + "…") if len(text) > 200 else (text or type(err).__name__)

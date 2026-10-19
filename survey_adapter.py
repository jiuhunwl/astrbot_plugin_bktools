"""问卷校验适配器（活动链路的联系方式命中校验）。

两个实现（对齐 design-plugin-campaign.md 3.5 节）：
- :class:`JuanWangSurveyVerifier`：卷王问卷（SurveyKing）公开查询 API，
  ``POST {base_url}{api_prefix}/public/getQueryResult``，``answers``
  非空 = 命中。公开端点无认证，插件不持有任何 SurveyKing 凭据。
- :class:`HttpSurveyVerifier`：http_generic 兜底，endpoint/method/auth/
  form_id/contact 字段映射/成功判定均可配。

fail-closed：网络错误、响应结构异常、字段缺失一律返回 ERROR（服务繁忙），
绝不放行。联系方式值仅用于请求体，不明文落日志。

本模块不依赖 astrbot，可独立单测。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional

import aiohttp

logger = logging.getLogger("bktools.survey")


class SurveyStatus(str, Enum):
    OK = "ok"                    # 命中（已提交过符合条件答卷）
    NOT_MATCH = "not_match"      # 未命中
    ERROR = "error"              # 网络/结构异常（fail-closed，视为服务繁忙）


@dataclass
class SurveyOutcome:
    status: SurveyStatus
    detail: str = ""  # 面向日志的诊断信息；绝不包含联系方式明文

    @property
    def ok(self) -> bool:
        return self.status is SurveyStatus.OK


class SurveyVerifier(ABC):
    """问卷校验抽象基类。"""

    @abstractmethod
    async def verify(
        self, contact_value: str, form_id: Optional[str] = None
    ) -> SurveyOutcome:
        """校验联系方式是否命中问卷。form_id 覆盖 provider 默认问卷 ID。"""


def _json_response_text(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _get_path(obj: Any, path: str) -> Any:
    if obj is None or not path:
        return None
    cur = obj
    for key in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list):
            try:
                idx = int(key)
                cur = cur[idx] if 0 <= idx < len(cur) else None
            except ValueError:
                return None
        else:
            return None
    return cur


def _match_value(value: Any, expected: str) -> bool:
    """判定字段与期望值的比较：布尔归一化为 true/false，大小写不敏感。

    JSON 里 ``true`` 解析成 Python ``True``，``str(True)`` 是 ``"True"``，
    直接比较会漏判——这是线上最常见的配置姿势（success_value="true"）。
    """
    if isinstance(value, bool):
        actual = "true" if value else "false"
    elif value is None:
        actual = ""
    else:
        actual = str(value)
    return actual.strip().casefold() == str(expected or "").strip().casefold()


# ---------------------------------------------------------------- 卷王问卷


class JuanWangSurveyVerifier(SurveyVerifier):
    """卷王问卷（SurveyKing）公开查询 API 专用适配器。

    契约（v1.13 api.prefix=/api，公开端点无需管理凭据）：
      POST {base_url}{api_prefix}/public/getQueryResult
      body: {"id": "<surveyId>", "answer": {"<contact_field_id>": "<值>"}}
      （query_mode=query 时改用 "query" 键传 URL 参数）
      响应: {schema, answers: [...], fieldPermission}
      answers 非空列表 = 命中；空列表 = 未命中；缺失/异常 = ERROR。
    """

    QUERY_BODY_KEYS = {"answer": "answer", "query": "query"}

    def __init__(
        self,
        base_url: str,
        survey_id: str,
        contact_field_id: str,
        *,
        api_prefix: str = "/api",
        query_mode: str = "answer",
        timeout_sec: int = 15,
        user_agent: str = "BKtools-SurveyVerifier/1.7.0",
        trace_factory: Optional[Callable[[], aiohttp.TraceConfig]] = None,
    ) -> None:
        base = str(base_url or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ValueError("卷王问卷 base_url 必须以 http(s):// 开头")
        self._base_url = base
        self._api_prefix = "/" + str(api_prefix or "/api").strip().strip("/")
        self._survey_id = str(survey_id or "").strip()
        self._contact_field_id = str(contact_field_id or "").strip()
        if not self._survey_id:
            raise ValueError("卷王问卷 survey_id 不能为空")
        if not self._contact_field_id:
            raise ValueError("卷王问卷 contact_field_id 不能为空")
        self._body_key = self.QUERY_BODY_KEYS.get(
            str(query_mode or "answer").strip().lower(), "answer"
        )
        self._timeout_sec = max(5, int(timeout_sec))
        self._user_agent = user_agent
        self._trace_factory = trace_factory

    @property
    def endpoint(self) -> str:
        return self._base_url + self._api_prefix + "/public/getQueryResult"

    async def verify(
        self, contact_value: str, form_id: Optional[str] = None
    ) -> SurveyOutcome:
        value = str(contact_value or "").strip()
        if not value:
            return SurveyOutcome(SurveyStatus.ERROR, "联系方式为空")
        survey_id = str(form_id or self._survey_id).strip() or self._survey_id
        body = {
            "id": survey_id,
            self._body_key: {self._contact_field_id: value},
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_sec),
                headers={"User-Agent": self._user_agent},
                trace_configs=[self._trace_factory()] if self._trace_factory else None,
            ) as session:
                async with session.post(self.endpoint, json=body) as response:
                    text = await response.text()
                    if response.status >= 400:
                        return SurveyOutcome(
                            SurveyStatus.ERROR, f"问卷查询 HTTP {response.status}"
                        )
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as ex:
            return SurveyOutcome(SurveyStatus.ERROR, f"问卷查询网络异常: {type(ex).__name__}")
        payload = _json_response_text(text)
        if not isinstance(payload, dict):
            return SurveyOutcome(SurveyStatus.ERROR, "问卷查询响应不是 JSON 对象")
        answers = payload.get("answers")
        if not isinstance(answers, list):
            # 结构异常 fail-closed，绝不放行
            return SurveyOutcome(SurveyStatus.ERROR, "问卷查询响应缺少 answers 列表")
        if answers:
            return SurveyOutcome(SurveyStatus.OK, f"命中（answers={len(answers)} 条）")
        return SurveyOutcome(SurveyStatus.NOT_MATCH, "未查询到匹配答卷")


# ---------------------------------------------------------------- generic 兜底


class HttpSurveyVerifier(SurveyVerifier):
    """http_generic 兜底：请求与成功判定全部可配。

    - endpoint/method：请求地址与方法（GET 时参数拼 query）；
    - auth_type: none | bearer | header（header 名由 auth_header 指定）；
    - form_param / contact_field：问卷 ID 与联系方式在请求中的字段名；
    - extra_params_json：附加固定参数（JSON 对象文本）；
    - success_field/success_value：成功判定（点路径取值后字符串比较）；
      success_field 为空时以 HTTP 2xx 即视为成功。
    """

    def __init__(
        self,
        endpoint: str,
        *,
        method: str = "POST",
        auth_type: str = "none",
        credential: str = "",
        auth_header: str = "X-Api-Key",
        form_param: str = "form_id",
        contact_field: str = "contact",
        extra_params_json: str = "",
        success_field: str = "",
        success_value: str = "true",
        timeout_sec: int = 15,
        user_agent: str = "BKtools-SurveyVerifier/1.7.0",
    ) -> None:
        self._endpoint = str(endpoint or "").strip()
        if not self._endpoint.startswith(("http://", "https://")):
            raise ValueError("http_generic endpoint 必须以 http(s):// 开头")
        self._method = str(method or "POST").strip().upper()
        if self._method not in ("GET", "POST"):
            raise ValueError("http_generic method 仅支持 GET/POST")
        self._auth_type = str(auth_type or "none").strip().lower()
        self._credential = str(credential or "")
        self._auth_header = str(auth_header or "X-Api-Key").strip() or "X-Api-Key"
        self._form_param = str(form_param or "form_id").strip() or "form_id"
        self._contact_field = str(contact_field or "contact").strip() or "contact"
        try:
            extra = json.loads(extra_params_json) if extra_params_json else {}
        except (json.JSONDecodeError, ValueError):
            logger.warning("http_generic extra_params_json 解析失败，已忽略")
            extra = {}
        self._extra_params = extra if isinstance(extra, dict) else {}
        self._success_field = str(success_field or "").strip()
        self._success_value = str(success_value or "").strip()
        self._timeout_sec = max(5, int(timeout_sec))
        self._user_agent = user_agent

    def _headers(self) -> Dict[str, str]:
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        if self._auth_type == "bearer" and self._credential:
            headers["Authorization"] = f"Bearer {self._credential}"
        elif self._auth_type == "header" and self._credential:
            headers[self._auth_header] = self._credential
        return headers

    def _match_success(self, payload: Any) -> bool:
        if not self._success_field:
            return True  # 未配置判定字段：HTTP 2xx 即成功
        value = _get_path(payload, self._success_field)
        return _match_value(value, self._success_value)

    async def verify(
        self, contact_value: str, form_id: Optional[str] = None
    ) -> SurveyOutcome:
        value = str(contact_value or "").strip()
        if not value:
            return SurveyOutcome(SurveyStatus.ERROR, "联系方式为空")
        params: Dict[str, Any] = dict(self._extra_params)
        params[self._form_param] = str(form_id or "").strip()
        params[self._contact_field] = value
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_sec),
            ) as session:
                if self._method == "GET":
                    clean = {k: v for k, v in params.items() if v != ""}
                    async with session.get(
                        self._endpoint, params=clean, headers=self._headers()
                    ) as response:
                        text = await response.text()
                        status = response.status
                else:
                    async with session.post(
                        self._endpoint, json=params, headers=self._headers()
                    ) as response:
                        text = await response.text()
                        status = response.status
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as ex:
            return SurveyOutcome(SurveyStatus.ERROR, f"问卷查询网络异常: {type(ex).__name__}")
        if status >= 400:
            return SurveyOutcome(SurveyStatus.ERROR, f"问卷查询 HTTP {status}")
        payload = _json_response_text(text)
        if self._success_field and payload is None:
            return SurveyOutcome(SurveyStatus.ERROR, "问卷查询响应不是 JSON")
        if self._match_success(payload):
            return SurveyOutcome(SurveyStatus.OK)
        return SurveyOutcome(SurveyStatus.NOT_MATCH, "成功判定字段不匹配")


# ---------------------------------------------------------------- 工厂


_PROVIDER_TYPE_MAP = {
    "juanwang": JuanWangSurveyVerifier,
    "surveyking": JuanWangSurveyVerifier,  # 别名
    "http_generic": HttpSurveyVerifier,
}


def build_survey_verifier(
    provider_cfg: Any,
    *,
    form_id: Optional[str] = None,
    timeout_sec: int = 15,
) -> Optional[SurveyVerifier]:
    """按 open_api.survey_providers 条目构建校验器；配置无效返回 None。

    form_id 仅用于覆盖运行期问卷 ID（卷王用），构建期可传 None。
    """
    if not isinstance(provider_cfg, dict):
        return None
    provider_type = str(provider_cfg.get("type") or "").strip().lower()
    cls = _PROVIDER_TYPE_MAP.get(provider_type)
    if cls is None:
        logger.warning("未知问卷 provider 类型: %s", provider_type)
        return None
    try:
        if cls is JuanWangSurveyVerifier:
            return JuanWangSurveyVerifier(
                str(provider_cfg.get("base_url") or ""),
                str(provider_cfg.get("survey_id") or ""),
                str(provider_cfg.get("contact_field_id") or ""),
                api_prefix=str(provider_cfg.get("api_prefix") or "/api"),
                query_mode=str(provider_cfg.get("query_mode") or "answer"),
                timeout_sec=max(5, int(provider_cfg.get("timeout_sec", timeout_sec) or timeout_sec)),
            )
        return HttpSurveyVerifier(
            str(provider_cfg.get("endpoint") or ""),
            method=str(provider_cfg.get("method") or "POST"),
            auth_type=str(provider_cfg.get("auth_type") or "none"),
            credential=str(provider_cfg.get("credential") or ""),
            auth_header=str(provider_cfg.get("auth_header") or "X-Api-Key"),
            form_param=str(provider_cfg.get("form_param") or "form_id"),
            contact_field=str(provider_cfg.get("contact_field") or "contact"),
            extra_params_json=str(provider_cfg.get("extra_params_json") or ""),
            success_field=str(provider_cfg.get("success_field") or ""),
            success_value=str(provider_cfg.get("success_value") or "true"),
            timeout_sec=max(5, int(provider_cfg.get("timeout_sec", timeout_sec) or timeout_sec)),
        )
    except (TypeError, ValueError) as ex:
        logger.warning("问卷 provider 配置无效（%s）: %s", provider_type, ex)
        return None


# GitHub 用户名合法性与本模块无关，但 star 校验会用到同一套约束，
# 这里提供共享的严格白名单正则（GitHub 官方规则）。
GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

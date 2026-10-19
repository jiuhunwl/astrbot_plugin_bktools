"""开放 API 接入 + 活动发放链路的单测（不依赖 astrbot，不连真实网络）。

运行：
    python -m pytest tests/test_openapi_campaign.py -q

覆盖：
- open_api_client：mask_key / can_generate / X-Open-Key 头 / retries=0 / 错误分类；
- github_star：OK / NOT_STARRED / USER_NOT_FOUND / ERROR 四路；
- survey_adapter：卷王命中、未命中、结构异常 fail-closed、http_generic 可配判定；
- campaign_manager：窗口/启停、聊天单次、冷却、single-flight、发放编排与落盘。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
import time
import types
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import campaign_manager as cm  # noqa: E402
import github_star as gs  # noqa: E402
import open_api_client as oca  # noqa: E402
import survey_adapter as sa  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 假 aiohttp


class FakeResponse:
    """假响应：aiohttp 风格（async CM + status/headers/content.read/text）。"""

    def __init__(self, status: int = 200, body: str = "", headers: dict | None = None) -> None:
        self.status = int(status)
        self.headers = dict(headers or {})
        self._body = body
        self.content = types.SimpleNamespace(read=self._read_content)

    async def _read_content(self, _limit: int = -1) -> bytes:
        return self._body.encode("utf-8")

    async def text(self) -> str:
        return self._body

    async def read(self) -> bytes:
        return self._body.encode("utf-8")

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args) -> bool:
        return False


class FakeSession:
    """记录调用并按 handler(method, url, kwargs) 返回假响应的假会话。"""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.closed = False
        self.calls: list[tuple[str, str, dict]] = []
        self.last_kwargs: dict = {}

    def _call(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        self.last_kwargs = kwargs
        return self._handler(method, url, kwargs)

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        return self._call(method, url, **kwargs)

    def get(self, url: str, **kwargs) -> FakeResponse:
        return self._call("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> FakeResponse:
        return self._call("POST", url, **kwargs)

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def close(self) -> None:
        self.closed = True


def fake_aiohttp(session_factory) -> types.SimpleNamespace:
    """构造最小 aiohttp 替身（ClientError 家族需在模块异常元组里被捕获）。"""
    client_error = type("ClientError", (Exception,), {})
    return types.SimpleNamespace(
        ClientTimeout=type("ClientTimeout", (), {"__init__": lambda s, total=30: setattr(s, "total", total)}),
        ClientError=client_error,
        ClientConnectionError=type("ClientConnectionError", (client_error,), {}),
        ServerTimeoutError=type("ServerTimeoutError", (client_error,), {}),
        TCPConnector=type("TCPConnector", (), {"__init__": lambda s, ssl=True: None}),
        TraceConfig=type("TraceConfig", (), {}),
        ClientSession=lambda *a, **k: session_factory(),
    )


# ---------------------------------------------------------------- open_api_client


class MaskKeyTests(unittest.TestCase):
    def test_mask_keeps_prefix_and_tail(self):
        self.assertEqual(oca.mask_key("opka_1a2b3c4d5e6f7777abcd"), "opka_****abcd")

    def test_normal_prefix(self):
        self.assertEqual(oca.mask_key("opk_abcdefgh1234"), "opk_****1234")

    def test_empty(self):
        self.assertEqual(oca.mask_key(""), "(未配置)")

    def test_short_key_never_leaks(self):
        masked = oca.mask_key("opk_ab12")
        self.assertEqual(masked, "opk_****")
        self.assertNotIn("ab12", masked)

    def test_normalize_key_type(self):
        self.assertEqual(oca.normalize_key_type("opka_"), oca.KEY_TYPE_ADMIN)
        self.assertEqual(oca.normalize_key_type("opk_"), oca.KEY_TYPE_NORMAL)
        self.assertEqual(oca.normalize_key_type("??"), oca.KEY_TYPE_NORMAL)


class OpenApiClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = FakeSession(lambda m, u, k: FakeResponse())
        self.client = oca.OpenApiClient(
            "https://api.example.com/",
            "opka_1a2b3c4d5e6f7777abcd",
            oca.KEY_TYPE_ADMIN,
            verify_tls=True,
            retry_backoff_sec=0,
        )
        # 把会话与异常类型都固定为假实现，杜绝真实网络
        self.client._session = self.session
        self.client._get_session = _resolve_session(self.session)
        oca.aiohttp = fake_aiohttp(lambda: self.session)  # type: ignore[assignment]

    def test_requires_base_url(self):
        with self.assertRaises(ValueError):
            oca.OpenApiClient("", "opk_x")

    def test_requires_api_key(self):
        with self.assertRaises(ValueError):
            oca.OpenApiClient("https://x.example", "")

    def test_rejects_schemeless_base_url(self):
        with self.assertRaises(ValueError):
            oca.OpenApiClient("api.example.com", "opk_x")

    def test_can_generate_by_prefix(self):
        admin = oca.OpenApiClient("https://x.example", "opka_1234567890abcd", oca.KEY_TYPE_ADMIN)
        normal = oca.OpenApiClient("https://x.example", "opk_1234567890abcd", oca.KEY_TYPE_NORMAL)
        self.assertTrue(admin.can_generate)
        self.assertFalse(normal.can_generate)

    def test_generate_rejects_normal_key_without_http(self):
        """🔴 opk_ 在发出任何 HTTP 请求之前就被本地拦下。"""
        client = oca.OpenApiClient("https://x.example", "opk_1234567890abcd", oca.KEY_TYPE_NORMAL)
        client._session = self.session
        client._get_session = _resolve_session(self.session)

        with self.assertRaises(oca.OpenApiError) as caught:
            _run(client.generate_redeem_codes(1))
        self.assertEqual(caught.exception.code, "client.admin_scope_required")
        self.assertEqual(len(self.session.calls), 0)

    def test_generate_does_not_retry_on_5xx(self):
        """🔴 非幂等：生成端点 retries=0，5xx 也不重试。"""
        self.session._handler = lambda m, u, k: FakeResponse(503, '{"code":503,"message":"busy"}')
        with self.assertRaises(oca.OpenApiError):
            _run(self.client.generate_redeem_codes(1))
        self.assertEqual(len(self.session.calls), 1)
        method, url, _ = self.session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.example.com/open-api/redeem-codes")

    def test_generate_success_extracts_plaintext(self):
        payload = {"code": 0, "data": {"batchId": "9", "count": 1, "codes": ["ABCD-EFGH-IJKL-MNOP"]}}
        self.session._handler = lambda m, u, k: FakeResponse(200, json.dumps(payload))

        result = _run(self.client.generate_redeem_codes(
            "NEWBIE", count=1, per_user_limit=1, per_ip_limit=0, campaign_ref="bktools:x"
        ))
        self.assertEqual(result.codes, ["ABCD-EFGH-IJKL-MNOP"])
        self.assertEqual(result.batch_id, "9")
        self.assertEqual(result.count, 1)
        self.assertEqual(self.session.last_kwargs["json"], {
            "typeCode": "NEWBIE", "count": 1, "campaignRef": "bktools:x",
            "perUserLimit": 1, "perIpLimit": 0,
        })

    def test_generate_numeric_type_ref_sends_type_id(self):
        """配置里写数字时按 typeId 提交（兼容按自增 ID 配置的存量部署）。"""
        self.session._handler = lambda m, u, k: FakeResponse(
            200, json.dumps({"code": 0, "data": {"batchId": "1", "count": 1, "codes": ["A"]}})
        )
        _run(self.client.generate_redeem_codes("7", count=1))
        self.assertEqual(self.session.last_kwargs["json"]["typeId"], 7)
        self.assertNotIn("typeCode", self.session.last_kwargs["json"])

    def test_generate_empty_type_ref_rejected(self):
        """类型标识为空必须本地拒绝，不发网络请求。"""
        with self.assertRaises(oca.OpenApiError) as caught:
            _run(self.client.generate_redeem_codes("   "))
        self.assertEqual(caught.exception.code, "client.bad_payload")
        self.assertEqual(len(self.session.calls), 0)

    def test_headers_carry_x_open_key_only(self):
        self.session._handler = lambda m, u, k: FakeResponse(
            200, json.dumps({"code": 0, "data": {"batchId": "1", "count": 1, "codes": ["A"]}})
        )
        _run(self.client.generate_redeem_codes(1))
        headers = self.session.last_kwargs["headers"]
        self.assertIn("X-Open-Key", headers)
        self.assertEqual(headers["X-Open-Key"], "opka_1a2b3c4d5e6f7777abcd")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)

    def test_scope_denied_code_from_header(self):
        self.session._handler = lambda m, u, k: FakeResponse(
            403,
            json.dumps({"code": 403, "message": "该开放 Key 未授权此端点"}),
            headers={"X-Bugpk-Openapi-Code": "openapi.scope_denied"},
        )
        with self.assertRaises(oca.OpenApiError) as caught:
            _run(self.client.generate_redeem_codes(1))
        self.assertEqual(caught.exception.code, "openapi.scope_denied")

    def test_missing_plaintext_is_failure(self):
        """🔴 拿不到明文绝不把批次号当 CDK 发出去。"""
        self.session._handler = lambda m, u, k: FakeResponse(
            200, json.dumps({"code": 0, "data": {"batchId": "5", "count": 1}})
        )
        with self.assertRaises(oca.OpenApiError) as caught:
            _run(self.client.generate_redeem_codes(1))
        self.assertEqual(caught.exception.code, "client.codes_missing")

    def test_business_error_carries_stable_code(self):
        self.session._handler = lambda m, u, k: FakeResponse(
            200, json.dumps({"code": 400, "message": "兑换码类型不存在"})
        )
        with self.assertRaises(oca.OpenApiError) as caught:
            _run(self.client.generate_redeem_codes(99))
        self.assertEqual(caught.exception.status, 200)
        self.assertIn("兑换码类型不存在", caught.exception.message)

    def test_non_json_response_rejected(self):
        self.session._handler = lambda m, u, k: FakeResponse(200, "<html>gateway</html>")
        with self.assertRaises(oca.OpenApiError) as caught:
            _run(self.client.generate_redeem_codes(1))
        self.assertEqual(caught.exception.code, "client.non_json")

    def test_get_account_returns_data(self):
        self.session._handler = lambda m, u, k: FakeResponse(
            200, json.dumps({"code": 0, "data": {"openApiEnabled": True}})
        )
        data = _run(self.client.get_account())
        self.assertTrue(data["openApiEnabled"])


def _resolve_session(session):
    async def _resolve() -> FakeSession:
        return session

    return _resolve


# ---------------------------------------------------------------- github_star


class GitHubStarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = FakeSession(lambda m, u, k: FakeResponse())
        gs.aiohttp = fake_aiohttp(lambda: self.session)  # type: ignore[assignment]
        self.verifier = gs.GitHubStarVerifier("")

    def _status_session(self, statuses: list[int]) -> FakeSession:
        queue = list(statuses)
        session = FakeSession(lambda m, u, k: FakeResponse(queue.pop(0)))
        return session

    def test_ok(self):
        result = _run(self.verifier.verify("alice", "bugpk", "api", session=self._status_session([200, 204])))
        self.assertIs(result, gs.StarCheckResult.OK)

    def test_not_starred(self):
        result = _run(self.verifier.verify("alice", "bugpk", "api", session=self._status_session([200, 404])))
        self.assertIs(result, gs.StarCheckResult.NOT_STARRED)

    def test_user_not_found_short_circuits(self):
        # 注意：GitHub 用户名不允许下划线，用连字符，否则会先被本地正则拦掉
        session = self._status_session([404])
        result = _run(self.verifier.verify("nobody-here", "bugpk", "api", session=session))
        self.assertIs(result, gs.StarCheckResult.USER_NOT_FOUND)
        self.assertEqual(len(session.calls), 1)  # 不浪费第二次请求

    def test_error_on_bad_status(self):
        result = _run(self.verifier.verify("alice", "bugpk", "api", session=self._status_session([200, 403])))
        self.assertIs(result, gs.StarCheckResult.ERROR)

    def test_error_on_missing_args(self):
        session = self._status_session([200, 204])
        result = _run(self.verifier.verify("", "bugpk", "api", session=session))
        self.assertIs(result, gs.StarCheckResult.ERROR)
        self.assertEqual(len(session.calls), 0)

    def test_error_on_invalid_login(self):
        session = self._status_session([200, 204])
        result = _run(self.verifier.verify("bad login!", "bugpk", "api", session=session))
        self.assertIn(result, (gs.StarCheckResult.ERROR, gs.StarCheckResult.USER_NOT_FOUND))
        self.assertEqual(len(session.calls), 0)

    def test_network_error_fails_closed(self):
        session = FakeSession(lambda m, u, k: (_ for _ in ()).throw(gs.aiohttp.ClientConnectionError("boom")))
        result = _run(self.verifier.verify("alice", "bugpk", "api", session=session))
        self.assertIs(result, gs.StarCheckResult.ERROR)

    def test_token_header_set_but_never_logged(self):
        verifier = gs.GitHubStarVerifier("ghp_secret_value")
        headers = verifier._headers()
        self.assertEqual(headers["Authorization"], "Bearer ghp_secret_value")
        self.assertEqual(str(verifier._token).count("ghp_secret_value"), 1)

    def test_no_token_no_auth_header(self):
        self.assertNotIn("Authorization", gs.GitHubStarVerifier("")._headers())


# ---------------------------------------------------------------- survey_adapter


class JuanWangSurveyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = FakeSession(lambda m, u, k: FakeResponse())
        sa.aiohttp = fake_aiohttp(lambda: self.session)  # type: ignore[assignment]

    def _verifier(self, **kwargs):
        base = {"base_url": "https://survey.example.com", "survey_id": "S-100", "contact_field_id": "fld_contact"}
        base.update(kwargs)
        return sa.JuanWangSurveyVerifier(**base)

    def _set_body(self, body: str, status: int = 200) -> None:
        self.session._handler = lambda m, u, k: FakeResponse(status, body)

    def test_hit(self):
        self._set_body(json.dumps({"schema": {}, "answers": [{"id": 1}], "fieldPermission": {}}))
        outcome = _run(self._verifier().verify("13800138000"))
        self.assertIs(outcome.status, sa.SurveyStatus.OK)
        self.assertTrue(outcome.ok)
        method, url, kwargs = self.session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://survey.example.com/api/public/getQueryResult")
        self.assertEqual(kwargs["json"], {"id": "S-100", "answer": {"fld_contact": "13800138000"}})

    def test_miss(self):
        self._set_body(json.dumps({"answers": []}))
        outcome = _run(self._verifier().verify("13800138000"))
        self.assertIs(outcome.status, sa.SurveyStatus.NOT_MATCH)

    def test_missing_answers_fails_closed(self):
        self._set_body(json.dumps({"schema": {}}))
        outcome = _run(self._verifier().verify("13800138000"))
        self.assertIs(outcome.status, sa.SurveyStatus.ERROR)

    def test_non_json_fails_closed(self):
        self._set_body("<html>captcha</html>")
        outcome = _run(self._verifier().verify("13800138000"))
        self.assertIs(outcome.status, sa.SurveyStatus.ERROR)

    def test_http_error_fails_closed(self):
        self._set_body("{}", status=500)
        outcome = _run(self._verifier().verify("13800138000"))
        self.assertIs(outcome.status, sa.SurveyStatus.ERROR)

    def test_network_error_fails_closed(self):
        self.session._handler = lambda m, u, k: (_ for _ in ()).throw(sa.aiohttp.ClientConnectionError("boom"))
        outcome = _run(self._verifier().verify("13800138000"))
        self.assertIs(outcome.status, sa.SurveyStatus.ERROR)

    def test_empty_contact_fails_closed(self):
        outcome = _run(self._verifier().verify("  "))
        self.assertIs(outcome.status, sa.SurveyStatus.ERROR)
        self.assertEqual(len(self.session.calls), 0)

    def test_form_id_override_and_query_mode(self):
        self._set_body(json.dumps({"answers": []}))
        verifier = self._verifier(query_mode="query")
        outcome = _run(verifier.verify("QQ12345", "S-OVERRIDE"))
        self.assertIs(outcome.status, sa.SurveyStatus.NOT_MATCH)
        self.assertEqual(self.session.last_kwargs["json"],
                         {"id": "S-OVERRIDE", "query": {"fld_contact": "QQ12345"}})

    def test_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            sa.JuanWangSurveyVerifier("https://x.example", "", "fld")
        with self.assertRaises(ValueError):
            sa.JuanWangSurveyVerifier("https://x.example", "S-1", "")
        with self.assertRaises(ValueError):
            sa.JuanWangSurveyVerifier("not-a-url", "S-1", "fld")


class HttpGenericSurveyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = FakeSession(lambda m, u, k: FakeResponse())
        sa.aiohttp = fake_aiohttp(lambda: self.session)  # type: ignore[assignment]

    def test_success_field_match(self):
        self.session._handler = lambda m, u, k: FakeResponse(200, '{"data": {"ok": true}}')
        verifier = sa.HttpSurveyVerifier(
            "https://ext.example.com/check", success_field="data.ok", success_value="true",
            contact_field="contact", form_param="form_id",
        )
        outcome = _run(verifier.verify("13800138000", "F-1"))
        self.assertIs(outcome.status, sa.SurveyStatus.OK)
        self.assertEqual(self.session.last_kwargs["json"], {"form_id": "F-1", "contact": "13800138000"})

    def test_success_field_mismatch(self):
        self.session._handler = lambda m, u, k: FakeResponse(200, '{"data": {"ok": false}}')
        verifier = sa.HttpSurveyVerifier(
            "https://ext.example.com/check", success_field="data.ok", success_value="true"
        )
        outcome = _run(verifier.verify("13800138000", "F-1"))
        self.assertIs(outcome.status, sa.SurveyStatus.NOT_MATCH)

    def test_no_success_field_accepts_2xx(self):
        self.session._handler = lambda m, u, k: FakeResponse(200, "anything")
        verifier = sa.HttpSurveyVerifier("https://ext.example.com/check")
        outcome = _run(verifier.verify("13800138000"))
        self.assertIs(outcome.status, sa.SurveyStatus.OK)

    def test_get_uses_query_params(self):
        self.session._handler = lambda m, u, k: FakeResponse(200, "ok")
        verifier = sa.HttpSurveyVerifier("https://ext.example.com/check", method="GET", success_field="")
        outcome = _run(verifier.verify("QQ12345", "F-2"))
        self.assertIs(outcome.status, sa.SurveyStatus.OK)
        method, _, kwargs = self.session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(kwargs["params"], {"form_id": "F-2", "contact": "QQ12345"})

    def test_bearer_auth_header(self):
        self.session._handler = lambda m, u, k: FakeResponse(200, "ok")
        verifier = sa.HttpSurveyVerifier(
            "https://ext.example.com/check", auth_type="bearer", credential="tok123",
            auth_header="X-Token", success_field="",
        )
        _run(verifier.verify("QQ12345"))
        self.assertEqual(self.session.last_kwargs["headers"]["Authorization"], "Bearer tok123")

    def test_http_error_fails_closed(self):
        self.session._handler = lambda m, u, k: FakeResponse(502, "bad gateway")
        verifier = sa.HttpSurveyVerifier("https://ext.example.com/check", success_field="")
        outcome = _run(verifier.verify("QQ12345"))
        self.assertIs(outcome.status, sa.SurveyStatus.ERROR)

    def test_build_factory_maps_types(self):
        self.assertIsNotNone(sa.build_survey_verifier({
            "type": "juanwang", "base_url": "https://s.example.com",
            "survey_id": "S-1", "contact_field_id": "fld",
        }))
        self.assertIsNotNone(sa.build_survey_verifier({
            "type": "surveyking", "base_url": "https://s2.example.com",
            "survey_id": "S-2", "contact_field_id": "fld",
        }))
        self.assertIsNotNone(sa.build_survey_verifier(
            {"type": "http_generic", "endpoint": "https://e.example.com/x"}
        ))
        self.assertIsNone(sa.build_survey_verifier({"type": "unknown"}))
        self.assertIsNone(sa.build_survey_verifier(None))

    def test_build_factory_invalid_config_returns_none(self):
        self.assertIsNone(sa.build_survey_verifier(
            {"type": "juanwang", "base_url": "bad", "survey_id": "", "contact_field_id": ""}
        ))
        self.assertIsNone(sa.build_survey_verifier(
            {"type": "http_generic", "endpoint": "bad"}
        ))


# ---------------------------------------------------------------- campaign_manager


class FakeOpenClient:
    """campaign_manager 依赖注入用的假开放 API 客户端。"""

    def __init__(self, key_type: str = oca.KEY_TYPE_ADMIN, codes: list[str] | None = None,
                 raise_error: oca.OpenApiError | None = None, sleep: float = 0.0) -> None:
        self.key_type = key_type
        self.codes = list(codes) if codes is not None else ["ABCD-EFGH-IJKL-MNOP"]
        self.raise_error = raise_error
        self.sleep = sleep
        self.calls: list[dict] = []
        self.masked_key = "opka_****mnop"

    async def generate_redeem_codes(self, type_code, count=1, per_user_limit=None,
                                    per_ip_limit=None, campaign_ref="") -> oca.GenerateResult:
        self.calls.append({
            "type_code": type_code, "count": count, "per_user_limit": per_user_limit,
            "per_ip_limit": per_ip_limit, "campaign_ref": campaign_ref,
        })
        if self.raise_error is not None:
            raise self.raise_error
        if self.sleep > 0:
            await asyncio.sleep(self.sleep)
        return oca.GenerateResult(codes=self.codes[:count], batch_id="42", count=count)


class FakeStarVerifier:
    def __init__(self, outcome: gs.StarCheckResult) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, str]] = []

    async def verify(self, login, owner, repo, **_kwargs) -> gs.StarCheckResult:
        self.calls.append((str(login), str(owner), str(repo)))
        return self.outcome


class FakeSurveyVerifier:
    def __init__(self, status: sa.SurveyStatus, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        self.calls: list[tuple[str, str | None]] = []

    async def verify(self, contact_value, form_id=None) -> sa.SurveyOutcome:
        self.calls.append((str(contact_value), form_id))
        return sa.SurveyOutcome(self.status, self.detail)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


class CampaignFixtures:
    def __init__(self, state_path: str) -> None:
        self.client = FakeOpenClient()
        self.star = FakeStarVerifier(gs.StarCheckResult.OK)
        self.survey = FakeSurveyVerifier(sa.SurveyStatus.OK)
        self.store = cm.CampaignStateStore(state_path)
        self.manager = cm.CampaignManager(
            client=self.client,
            state_store=self.store,
            star_verifier=self.star,
            survey_verifiers={"juanwang": self.survey},
            cooldown_sec=60,
        )

    def load_default(self, raw_overrides: dict | None = None, **overrides) -> None:
        raw = {
            "id": "welcome",
            "name": "新手礼",
            "enabled": True,
            "type_code": "NEWBIE",
            "per_user_limit": 1,
            "per_ip_limit": 0,
            "star": {"enabled": False, "owner": "bugpk", "repo": "api"},
            "survey": {"enabled": False, "provider": "juanwang"},
        }
        if raw_overrides:
            raw.update(raw_overrides)
        raw.update(overrides)
        self.manager.load_campaigns([raw])


class CampaignGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = CampaignFixtures(str(pathlib.Path(self._tmp.name) / "state.json"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _definition(self, **kwargs):
        base = {"enabled": True, "start_at": "", "end_at": ""}
        base.update(kwargs)
        return cm.CampaignDef(id="x", **base)

    def test_disabled_campaign(self):
        self.assertIs(
            self.fx.manager.check_gate(self._definition(enabled=False)),
            cm.ClaimResult.CAMPAIGN_DISABLED,
        )

    def test_not_started(self):
        self.assertIs(
            self.fx.manager.check_gate(self._definition(start_at="2999-01-01"), now_ts=time.time()),
            cm.ClaimResult.CAMPAIGN_NOT_STARTED,
        )

    def test_expired(self):
        self.assertIs(
            self.fx.manager.check_gate(self._definition(end_at="2000-01-01 00:00:00"), now_ts=time.time()),
            cm.ClaimResult.CAMPAIGN_EXPIRED,
        )

    def test_window_open(self):
        self.assertIs(self.fx.manager.check_gate(self._definition()), cm.ClaimResult.GRANTED)

    def test_missing_definition(self):
        result = _run(self.fx.manager.claim("chat:1:user:1", "nope", "alice"))
        self.assertIs(result.result, cm.ClaimResult.CAMPAIGN_NOT_FOUND)

    def test_requires_chat_key_and_campaign_id(self):
        result = _run(self.fx.manager.claim("", "welcome", "alice"))
        self.assertIs(result.result, cm.ClaimResult.CONFIG_ERROR)
        result = _run(self.fx.manager.claim("chat:1:user:1", "", "alice"))
        self.assertIs(result.result, cm.ClaimResult.CONFIG_ERROR)


class CampaignClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = CampaignFixtures(str(pathlib.Path(self._tmp.name) / "state.json"))
        self.fx.load_default()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_grant_success_and_persist(self):
        result = _run(self.fx.manager.claim("chat:1:user:7", "welcome", "alice"))
        self.assertIs(result.result, cm.ClaimResult.GRANTED)
        self.assertEqual(result.code, "ABCD-EFGH-IJKL-MNOP")
        self.assertEqual(result.batch_id, "42")
        self.assertEqual(result.code_masked, "ABCD-****-MNOP")
        self.assertEqual(result.campaign_id, "welcome")
        self.assertEqual(len(self.fx.client.calls), 1)
        call = self.fx.client.calls[0]
        self.assertEqual(call["campaign_ref"], "bktools:welcome")
        self.assertEqual(call["type_code"], "NEWBIE")
        self.assertEqual(call["count"], 1)
        self.assertEqual(call["per_user_limit"], 1)
        self.assertEqual(call["per_ip_limit"], 0)

        claims = _run(self.fx.store.load())
        self.assertEqual(len(claims), 1)
        record = claims[0]
        self.assertEqual(record["campaign_id"], "welcome")
        self.assertEqual(record["code_masked"], "ABCD-****-MNOP")
        self.assertEqual(record["github_login"], "alice")
        # 🔴 明文码不得落入本地状态文件
        self.assertNotIn("ABCD-EFGH-IJKL-MNOP", json.dumps(record, ensure_ascii=False))

    def test_second_claim_blocked(self):
        first = _run(self.fx.manager.claim("chat:1:user:7", "welcome", "alice"))
        second = _run(self.fx.manager.claim("chat:1:user:7", "welcome", "alice"))
        self.assertIs(first.result, cm.ClaimResult.GRANTED)
        self.assertIs(second.result, cm.ClaimResult.ALREADY_CLAIMED)
        self.assertEqual(len(self.fx.client.calls), 1)  # 未触发第二次生成

    def test_different_chat_can_claim(self):
        _run(self.fx.manager.claim("chat:1:user:7", "welcome", "alice"))
        other = _run(self.fx.manager.claim("chat:1:user:8", "welcome", "bob"))
        self.assertIs(other.result, cm.ClaimResult.GRANTED)

    def test_cooldown_blocks_other_campaign(self):
        _run(self.fx.manager.claim("chat:1:user:7", "welcome", "alice"))
        self.fx.load_default({"id": "second"})
        result = _run(self.fx.manager.claim("chat:1:user:7", "second", "alice"))
        self.assertIs(result.result, cm.ClaimResult.COOLDOWN)
        self.assertEqual(len(self.fx.client.calls), 1)

    def test_star_not_starred_blocks(self):
        self.fx.load_default({"id": "star1", "star": {"enabled": True, "owner": "o", "repo": "r"}})
        self.fx.star = FakeStarVerifier(gs.StarCheckResult.NOT_STARRED)
        self.fx.manager._star = self.fx.star
        result = _run(self.fx.manager.claim("chat:1:user:7", "star1", "alice"))
        self.assertIs(result.result, cm.ClaimResult.STAR_NOT_STARRED)
        self.assertEqual(self.fx.star.calls, [("alice", "o", "r")])
        self.assertEqual(len(self.fx.client.calls), 0)

    def test_star_missing_login(self):
        self.fx.load_default({"id": "star2", "star": {"enabled": True, "owner": "o", "repo": "r"}})
        result = _run(self.fx.manager.claim("chat:1:user:7", "star2", ""))
        self.assertIs(result.result, cm.ClaimResult.STAR_NOT_CONFIGURED)

    def test_star_misconfigured_blocks(self):
        self.fx.load_default({"id": "star3", "star": {"enabled": True, "owner": "o", "repo": ""}})
        result = _run(self.fx.manager.claim("chat:1:user:7", "star3", "alice"))
        self.assertIs(result.result, cm.ClaimResult.CONFIG_ERROR)

    def test_star_user_not_found(self):
        self.fx.load_default({"id": "star4", "star": {"enabled": True, "owner": "o", "repo": "r"}})
        self.fx.star = FakeStarVerifier(gs.StarCheckResult.USER_NOT_FOUND)
        self.fx.manager._star = self.fx.star
        result = _run(self.fx.manager.claim("chat:1:user:7", "star4", "ghost"))
        self.assertIs(result.result, cm.ClaimResult.STAR_USER_NOT_FOUND)

    def test_star_service_error_fails_closed(self):
        self.fx.load_default({"id": "star5", "star": {"enabled": True, "owner": "o", "repo": "r"}})
        self.fx.star = FakeStarVerifier(gs.StarCheckResult.ERROR)
        self.fx.manager._star = self.fx.star
        result = _run(self.fx.manager.claim("chat:1:user:7", "star5", "alice"))
        self.assertIs(result.result, cm.ClaimResult.SURVEY_UNAVAILABLE)
        self.assertEqual(len(self.fx.client.calls), 0)

    def test_survey_not_match_blocks(self):
        self.fx.load_default({"id": "sv1", "survey": {"enabled": True, "provider": "juanwang", "form_id": "F-9"}})
        self.fx.survey = FakeSurveyVerifier(sa.SurveyStatus.NOT_MATCH)
        self.fx.manager._surveys = {"juanwang": self.fx.survey}
        result = _run(self.fx.manager.claim("chat:1:user:7", "sv1", "alice"))
        self.assertIs(result.result, cm.ClaimResult.SURVEY_NOT_MATCH)
        self.assertEqual(self.fx.survey.calls, [("alice", "F-9")])
        self.assertEqual(len(self.fx.client.calls), 0)

    def test_survey_error_fails_closed(self):
        self.fx.load_default({"id": "sv2", "survey": {"enabled": True, "provider": "juanwang"}})
        self.fx.survey = FakeSurveyVerifier(sa.SurveyStatus.ERROR, "网络异常")
        self.fx.manager._surveys = {"juanwang": self.fx.survey}
        result = _run(self.fx.manager.claim("chat:1:user:7", "sv2", "alice"))
        self.assertIs(result.result, cm.ClaimResult.SURVEY_UNAVAILABLE)

    def test_survey_provider_missing(self):
        self.fx.load_default({"id": "sv3", "survey": {"enabled": True, "provider": "missing"}})
        self.fx.manager._surveys = {}
        result = _run(self.fx.manager.claim("chat:1:user:7", "sv3", "alice"))
        self.assertIs(result.result, cm.ClaimResult.SURVEY_NOT_CONFIGURED)

    def test_chat_member_contact_source(self):
        self.fx.load_default({"id": "sv4", "survey": {
            "enabled": True, "provider": "juanwang", "contact_source": "chat_member"}})
        self.fx.manager._chat_member_provider = lambda key, field: "13800138000"
        self.fx.survey = FakeSurveyVerifier(sa.SurveyStatus.OK)
        self.fx.manager._surveys = {"juanwang": self.fx.survey}
        result = _run(self.fx.manager.claim("chat:1:user:7", "sv4", "alice"))
        self.assertIs(result.result, cm.ClaimResult.GRANTED)
        self.assertEqual(self.fx.survey.calls, [("13800138000", None)])

    def test_normal_key_forbidden(self):
        self.fx.client = FakeOpenClient(key_type=oca.KEY_TYPE_NORMAL)
        self.fx.manager._client = self.fx.client
        result = _run(self.fx.manager.claim("chat:1:user:7", "welcome", "alice"))
        self.assertIs(result.result, cm.ClaimResult.KEY_TYPE_FORBIDDEN)
        self.assertEqual(len(self.fx.client.calls), 0)

    def test_scope_denied_mapped(self):
        self.fx.client = FakeOpenClient(raise_error=oca.OpenApiError(
            403, "该开放 Key 未授权此端点", code="openapi.scope_denied"
        ))
        self.fx.manager._client = self.fx.client
        result = _run(self.fx.manager.claim("chat:1:user:7", "welcome", "alice"))
        self.assertIs(result.result, cm.ClaimResult.SCOPE_DENIED)

    def test_rate_limited_mapped(self):
        self.fx.client = FakeOpenClient(raise_error=oca.OpenApiError(
            429, "频控", code="openapi.rate_limited"
        ))
        self.fx.manager._client = self.fx.client
        result = _run(self.fx.manager.claim("chat:1:user:7", "welcome", "alice"))
        self.assertIs(result.result, cm.ClaimResult.RATE_LIMITED)

    def test_ip_forbidden_carries_client_ip(self):
        self.fx.client = FakeOpenClient(raise_error=oca.OpenApiError(
            403, "出口 IP 未加白名单", code="openapi.ip_forbidden", client_ip="203.0.113.9"
        ))
        self.fx.manager._client = self.fx.client
        result = _run(self.fx.manager.claim("chat:1:user:7", "welcome", "alice"))
        self.assertIs(result.result, cm.ClaimResult.IP_FORBIDDEN)
        self.assertEqual(result.client_ip, "203.0.113.9")

    def test_missing_plaintext_not_granted(self):
        self.fx.manager._client = FakeOpenClient(codes=[])
        result = _run(self.fx.manager.claim("chat:1:user:7", "welcome", "alice"))
        self.assertIs(result.result, cm.ClaimResult.GENERATE_FAILED)
        self.assertEqual(_run(self.fx.store.load()), [])  # 失败不落记录

    def test_single_flight_second_call_is_in_progress(self):
        self.fx.manager._client = FakeOpenClient(sleep=0.2)

        async def run_both():
            first_task = asyncio.create_task(
                self.fx.manager.claim("chat:1:user:7", "welcome", "alice")
            )
            # 必须让出控制权：create_task 只是入队，不 await 就让"第二次"
            # 先跑完，顺序完全反转（first 反而变成 IN_PROGRESS）。
            await asyncio.sleep(0.05)
            second = await self.fx.manager.claim("chat:1:user:7", "welcome", "alice")
            first = await first_task
            return first, second

        first, second = _run(run_both())
        self.assertIs(first.result, cm.ClaimResult.GRANTED)
        self.assertIs(second.result, cm.ClaimResult.IN_PROGRESS)
        self.assertEqual(len(self.fx.manager._client.calls), 1)


class CampaignConfigParsingTests(unittest.TestCase):
    def test_valid_definition(self):
        definition = cm.build_campaign_def({
            "id": "c1", "name": "春节", "enabled": False,
            "start_at": "2026-01-01", "end_at": "2026-02-01",
            "type_code": "SPRING", "per_user_limit": 2, "per_ip_limit": 3,
            "star": {"enabled": True, "owner": "o", "repo": "r"},
            "survey": {"enabled": True, "provider": "juanwang", "form_id": "F",
                       "contact_field_id": "fld", "contact_source": "chat_member"},
            "reward_notice": "奖励 100 点数",
        })
        assert definition is not None
        self.assertEqual(definition.per_user_limit, 2)
        self.assertEqual(definition.per_ip_limit, 3)
        self.assertFalse(definition.enabled)
        self.assertTrue(definition.star.enabled)
        self.assertEqual(definition.survey.contact_source, "chat_member")
        self.assertEqual(definition.reward_notice, "奖励 100 点数")

    def test_name_falls_back_to_id(self):
        definition = cm.build_campaign_def({"id": "c9", "type_code": "X"})
        assert definition is not None
        self.assertEqual(definition.name, "c9")

    def test_negative_limits_clamped(self):
        definition = cm.build_campaign_def(
            {"id": "c2", "type_code": "X", "per_user_limit": -5, "per_ip_limit": -1}
        )
        assert definition is not None
        self.assertEqual(definition.per_user_limit, 0)
        self.assertEqual(definition.per_ip_limit, 0)

    def test_missing_id_or_type_rejected(self):
        self.assertIsNone(cm.build_campaign_def({"id": "c1"}))
        self.assertIsNone(cm.build_campaign_def({"type_code": "X"}))
        self.assertIsNone(cm.build_campaign_def(None))
        self.assertIsNone(cm.build_campaign_def([]))
        self.assertIsNone(cm.build_campaign_def({}))

    def test_load_campaigns_dedup_and_skip_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = cm.CampaignStateStore(str(pathlib.Path(tmp) / "state.json"))
            manager = cm.CampaignManager(client=FakeOpenClient(), state_store=store)
            loaded = manager.load_campaigns([
                {"id": "a", "type_code": "A"},
                {"id": "a", "type_code": "A2"},
                {"id": "broken"},
                {"id": "b", "type_code": "B"},
            ])
            self.assertEqual(sorted(loaded.keys()), ["a", "b"])
            self.assertEqual(loaded["a"].type_code, "A2")  # 后者覆盖前者

    def test_load_campaigns_accepts_dict_and_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = cm.CampaignStateStore(str(pathlib.Path(tmp) / "state.json"))
            manager = cm.CampaignManager(client=FakeOpenClient(), state_store=store)
            self.assertEqual(manager.load_campaigns(None), {})
            loaded = manager.load_campaigns({"c": {"id": "c", "type_code": "C"}})
            self.assertEqual(list(loaded.keys()), ["c"])


class PrivacyTests(unittest.TestCase):
    def test_mask_contact(self):
        self.assertEqual(cm.mask_contact("13800138000"), "138****8000")
        self.assertEqual(cm.mask_contact("12345"), "****")
        self.assertEqual(cm.mask_contact(""), "(未提供)")
        self.assertEqual(cm.mask_contact("a@b.co"), "****")

    def test_mask_code(self):
        self.assertEqual(cm.mask_code("ABCD-EFGH-IJKL-MNOP"), "ABCD-****-MNOP")
        self.assertEqual(cm.mask_code(""), "(空)")
        self.assertEqual(cm.mask_code("ABCD"), "****")

    def test_hash_is_stable_and_salted(self):
        first = cm.hash_contact("13800138000", "salt-1")
        second = cm.hash_contact("13800138000", "salt-1")
        third = cm.hash_contact("13800138000", "salt-2")
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertNotIn("13800138000", first)
        self.assertEqual(len(first), 64)

    def test_salt_from_key_never_echoes_key(self):
        salt = cm.salt_from_key("opka_super_secret_value")
        self.assertEqual(len(salt), 16)
        self.assertNotIn("super_secret", salt)

    def test_parse_dt_formats(self):
        self.assertIsNotNone(cm._parse_dt("2026-01-01"))
        self.assertIsNotNone(cm._parse_dt("2026-01-01 08:00:00"))
        self.assertIsNone(cm._parse_dt(""))
        self.assertIsNone(cm._parse_dt("nonsense"))


class StateStoreTests(unittest.TestCase):
    def test_append_and_reload_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "nested" / "state.json")
            store = cm.CampaignStateStore(path)
            self.assertEqual(_run(store.load()), [])
            for index in range(3):
                _run(store.append_claim({"ts": _now_iso(), "n": index}))
            reloaded = cm.CampaignStateStore(path)
            claims = _run(reloaded.load())
            self.assertEqual(len(claims), 3)
            self.assertEqual([claim["n"] for claim in claims], [0, 1, 2])

    def test_corrupt_file_falls_back_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ not json")
            store = cm.CampaignStateStore(path)
            self.assertEqual(_run(store.load()), [])

    def test_non_list_payload_falls_back_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"claims": "oops"}))
            self.assertEqual(_run(cm.CampaignStateStore(path).load()), [])

    def test_requires_path(self):
        with self.assertRaises(ValueError):
            cm.CampaignStateStore("")

    def test_manager_requires_client(self):
        with self.assertRaises(ValueError):
            cm.CampaignManager(client=None, state_store=None)  # type: ignore[arg-type]

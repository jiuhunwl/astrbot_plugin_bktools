from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


class _Filter:
    def command(self, *args, **kwargs):
        return lambda func: func

    def event_message_type(self, *args, **kwargs):
        return lambda func: func


class _Component:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.content = kwargs.get("content", [])

    @classmethod
    def fromURL(cls, url):
        return cls(url)


def _load_main():
    names = (
        "astrbot", "astrbot.api", "astrbot.api.event",
        "astrbot.api.message_components", "astrbot.api.star", "astrbot.core",
        "astrbot.core.star", "astrbot.core.star.filter",
        "astrbot.core.star.filter.event_message_type",
    )
    modules = {name: types.ModuleType(name) for name in names}
    modules["astrbot.api"].logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    modules["astrbot.api.event"].filter = _Filter()
    modules["astrbot.api.event"].AstrMessageEvent = object
    for name in ("Image", "Node", "Nodes", "Plain", "Record", "Video", "File"):
        setattr(modules["astrbot.api.message_components"], name, _Component)
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = type(
        "Star", (), {"__init__": lambda self, context: setattr(self, "context", context)}
    )
    modules["astrbot.api.star"].register = lambda *a, **k: lambda cls: cls
    modules["astrbot.core.star.filter.event_message_type"].EventMessageType = types.SimpleNamespace(ALL="ALL")
    sys.modules.update(modules)
    path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("bktools_json_batch_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


bk = _load_main()


class _Event:
    def __init__(self):
        self.sent = []

    def get_platform_name(self):
        return "aiocqhttp"

    def get_self_id(self):
        return "10000"

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, content):
        return ("chain", content)

    async def send(self, result):
        self.sent.append(result)


# 模拟 aiocqhttp 的异常类型（按类名/消息字符串分类）
class _NetworkError(RuntimeError):
    pass


class _ApiNotAvailable(RuntimeError):
    pass


class _ActionFailed(RuntimeError):
    pass


class _FlakyEvent(_Event):
    """按 error_plan 依次抛错，耗尽后恢复正常发送。"""

    def __init__(self, error_plan):
        super().__init__()
        self.error_plan = list(error_plan)
        self.send_calls = 0

    async def send(self, result):
        self.send_calls += 1
        if self.send_calls <= len(self.error_plan):
            raise self.error_plan[self.send_calls - 1]
        self.sent.append(result)


class JsonBatchOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_json_uses_one_forward_message_without_temp_file(self):
        plugin = bk.BKToolsPlugin(
            object(),
            {"runtime_limits": {"json_text_max_chars": 500, "json_forward_max_nodes": 20}},
        )
        event = _Event()
        payload = {"data": [{"url": f"https://example.com/{i}", "text": "x" * 300} for i in range(37)]}
        with patch.object(bk.tempfile, "mkstemp", side_effect=AssertionError("must not create temp file")):
            sent = await plugin._send_json_result(
                event, payload, ("global", 0), "批量内容超过阈值"
            )
        self.assertTrue(sent)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.sent[0][0], "chain")

    async def test_small_json_remains_one_plain_message(self):
        plugin = bk.BKToolsPlugin(object(), {})
        event = _Event()
        self.assertTrue(
            await plugin._send_json_result(event, {"code": 200}, ("global", 0), "test")
        )
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.sent[0][0], "plain")


class SendReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_classify_send_error(self):
        cls = bk.BKToolsPlugin._classify_send_error
        # 超时（状态不确定）
        self.assertEqual(cls(_NetworkError("WebSocket API call timeout")), "timeout")
        self.assertEqual(cls(asyncio.TimeoutError()), "timeout")
        # 连接类错误（可安全重试）
        self.assertEqual(cls(_NetworkError("HTTP request failed")), "retryable")
        self.assertEqual(cls(_ApiNotAvailable()), "retryable")
        # 平台明确拒绝/本地错误（不重试）
        self.assertEqual(cls(_ActionFailed("bad request")), "rejected")
        self.assertEqual(cls(ValueError("构造失败")), "rejected")

    async def test_packed_send_timeout_falls_back_to_json(self):
        """显式开启 timeout_fallback=json：超时（状态不确定）→ 不重发打包消息 → 降级发送 JSON。"""
        plugin = bk.BKToolsPlugin(
            object(),
            {"send_reliability": {"timeout_fallback": "json", "timeout_grace_sec": 0}},
        )
        event = _FlakyEvent([_NetworkError("WebSocket API call timeout")])
        forward_nodes = [
            bk.Node(name="BKtools", uin=10000, content=[bk.Plain("视频")])
        ]
        payload = {"url": "https://example.com/v.mp4", "code": 200}
        sent = await plugin._send_packed_or_json(
            event, forward_nodes, payload, ("global", 0), "短视频解析结果"
        )
        self.assertTrue(sent)
        # 打包 1 次（超时） + JSON 兜底 1 次（成功）
        self.assertEqual(event.send_calls, 2)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.sent[0][0], "plain")

    async def test_packed_send_timeout_default_no_fallback(self):
        """默认配置（timeout_fallback=none）：超时后不追加任何兜底，避免与平台
        迟到送达的打包消息重复，仅释放去重标记。"""
        plugin = bk.BKToolsPlugin(
            object(),
            {"send_reliability": {"timeout_grace_sec": 0}},
        )
        event = _FlakyEvent([_NetworkError("WebSocket API call timeout")])
        forward_nodes = [
            bk.Node(name="BKtools", uin=10000, content=[bk.Plain("视频")])
        ]
        payload = {"url": "https://example.com/v.mp4"}
        sent = await plugin._send_packed_or_json(
            event, forward_nodes, payload, ("global", 0), "短视频解析结果"
        )
        self.assertFalse(sent)
        # 仅打包 1 次，不追加 JSON 兜底
        self.assertEqual(event.send_calls, 1)
        self.assertEqual(len(event.sent), 0)
        # 去重标记已释放：再次 claim 同一内容应成功
        send_key = plugin._send_key("短视频解析结果", payload)
        self.assertTrue(plugin._runtime_manager.claim_send(send_key))

    async def test_packed_send_retryable_failure_retries_then_success(self):
        """连接类失败按配置重试，成功后不发送兜底。"""
        plugin = bk.BKToolsPlugin(
            object(),
            {"send_reliability": {"retry_max": 2, "retry_backoff_ms": 0}},
        )
        event = _FlakyEvent([_NetworkError("HTTP request failed"), _ApiNotAvailable()])
        forward_nodes = [
            bk.Node(name="BKtools", uin=10000, content=[bk.Plain("视频")])
        ]
        payload = {"url": "https://example.com/v.mp4"}
        sent = await plugin._send_packed_or_json(
            event, forward_nodes, payload, ("global", 0), "短视频解析结果"
        )
        self.assertTrue(sent)
        self.assertEqual(event.send_calls, 3)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.sent[0][0], "chain")

    async def test_packed_send_rejected_no_retry_falls_back(self):
        """平台明确拒绝（ActionFailed）不重试，直接降级 JSON 兜底。"""
        plugin = bk.BKToolsPlugin(object(), {})
        event = _FlakyEvent([_ActionFailed("bad request")])
        forward_nodes = [
            bk.Node(name="BKtools", uin=10000, content=[bk.Plain("视频")])
        ]
        payload = {"url": "https://example.com/v.mp4"}
        sent = await plugin._send_packed_or_json(
            event, forward_nodes, payload, ("global", 0), "短视频解析结果"
        )
        self.assertTrue(sent)
        self.assertEqual(event.send_calls, 2)

    async def test_timeout_none_fallback_releases_claim(self):
        """超时且 timeout_fallback=none：不发送兜底，并释放去重标记允许用户重试。"""
        plugin = bk.BKToolsPlugin(
            object(),
            {"send_reliability": {"timeout_fallback": "none", "timeout_grace_sec": 0}},
        )
        event = _FlakyEvent([_NetworkError("WebSocket API call timeout")])
        forward_nodes = [
            bk.Node(name="BKtools", uin=10000, content=[bk.Plain("视频")])
        ]
        payload = {"url": "https://example.com/v.mp4"}
        sent = await plugin._send_packed_or_json(
            event, forward_nodes, payload, ("global", 0), "短视频解析结果"
        )
        self.assertFalse(sent)
        self.assertEqual(event.send_calls, 1)
        # 去重标记已释放：再次 claim 同一内容应成功
        send_key = plugin._send_key("短视频解析结果", payload)
        self.assertTrue(plugin._runtime_manager.claim_send(send_key))

    async def test_fallback_failure_releases_claim(self):
        """显式开启 json 兜底且兜底也失败：释放去重标记，允许用户稍后重试。"""
        plugin = bk.BKToolsPlugin(
            object(),
            {"send_reliability": {"timeout_fallback": "json", "timeout_grace_sec": 0}},
        )
        event = _FlakyEvent(
            [
                _NetworkError("WebSocket API call timeout"),  # 打包：超时
                _NetworkError("WebSocket API call timeout"),  # JSON 兜底：超时
            ]
        )
        forward_nodes = [
            bk.Node(name="BKtools", uin=10000, content=[bk.Plain("视频")])
        ]
        payload = {"url": "https://example.com/v.mp4"}
        sent = await plugin._send_packed_or_json(
            event, forward_nodes, payload, ("global", 0), "短视频解析结果"
        )
        self.assertFalse(sent)
        send_key = plugin._send_key("短视频解析结果", payload)
        self.assertTrue(plugin._runtime_manager.claim_send(send_key))


class SanitizeAndHeaderTests(unittest.IsolatedAsyncioTestCase):
    def test_sanitize_user_error_hides_url_with_key(self):
        """接口地址含 key 时整段 URL 隐藏，防止泄露到聊天消息。"""
        err = ValueError("请求 https://api.example.com/parse?key=SECRET123 时连接失败")
        text = bk._sanitize_user_error(err)
        self.assertNotIn("SECRET123", text)
        self.assertIn("（接口地址已隐藏）", text)
        self.assertNotIn("api.example.com", text)

    def test_sanitize_user_error_masks_sensitive_query(self):
        """非 URL 场景下对敏感 query 参数打码。"""
        text = bk._sanitize_user_error("参数错误 token=abc123&url=https://v.douyin.com/x/")
        self.assertNotIn("abc123", text)
        self.assertIn("token=***", text)

    def test_sanitize_user_error_handles_non_json(self):
        """接口返回非 JSON 时正文不向用户透传。"""
        self.assertEqual(
            bk._sanitize_user_error(ValueError("非 JSON 响应: <html>secret</html>")),
            "接口返回非 JSON 数据（详情见日志）",
        )

    def test_sanitize_user_error_truncates(self):
        text = bk._sanitize_user_error("x" * 500, limit=50)
        self.assertLessEqual(len(text), 51)

    def test_sanitize_user_error_empty(self):
        self.assertEqual(
            bk._sanitize_user_error(ValueError("")), "未知错误（详情见日志）"
        )

    def test_merged_request_headers_global_and_section(self):
        """节级请求头覆盖全局请求头同名项，其余键合并。"""
        plugin = bk.BKToolsPlugin(
            object(),
            {
                "http": {"headers": {"Authorization": "Bearer global", "Referer": "https://g"}},
                "short_video": {"headers": {"Authorization": "Bearer sv", "X-Custom": "1"}},
            },
        )
        merged = plugin._merged_request_headers("short_video")
        self.assertEqual(merged["Authorization"], "Bearer sv")
        self.assertEqual(merged["Referer"], "https://g")
        self.assertEqual(merged["X-Custom"], "1")

    def test_merged_request_headers_empty(self):
        plugin = bk.BKToolsPlugin(object(), {})
        self.assertEqual(plugin._merged_request_headers("short_video"), {})

    def test_merged_request_headers_json_text(self):
        """headers 以 JSON 文本形式配置（WebUI 文本编辑器）时正确解析并覆盖。"""
        plugin = bk.BKToolsPlugin(
            object(),
            {
                "http": {"headers": '{"Authorization": "Bearer global", "Referer": "https://g"}'},
                "short_video": {"headers": '{"Authorization": "Bearer sv", "X-Custom": "1"}'},
            },
        )
        merged = plugin._merged_request_headers("short_video")
        self.assertEqual(merged["Authorization"], "Bearer sv")
        self.assertEqual(merged["Referer"], "https://g")
        self.assertEqual(merged["X-Custom"], "1")

    def test_merged_request_headers_invalid_json_ignored(self):
        """非法 JSON / 非对象配置被忽略，不抛异常。"""
        plugin = bk.BKToolsPlugin(
            object(),
            {
                "http": {"headers": "{bad json"},
                "short_video": {"headers": '["not", "a", "dict"]'},
            },
        )
        self.assertEqual(plugin._merged_request_headers("short_video"), {})

    async def test_fetch_short_video_passes_merged_headers(self):
        """短视频解析请求携带 全局+节 合并后的自定义请求头。"""
        plugin = bk.BKToolsPlugin(
            object(),
            {
                "http": {"headers": {"Referer": "https://g"}},
                "short_video": {
                    "endpoint": "https://api.example.com/parse",
                    "headers": {"Authorization": "Bearer sv"},
                    "path_code": "code",
                    "success_codes": "200",
                    "path_msg": "msg",
                    "path_data_root": "data",
                },
            },
        )
        plugin._request_json = AsyncMock(
            return_value={"code": 200, "data": {"title": "t"}}
        )
        j, data = await plugin._fetch_short_video("https://v.douyin.com/abc/")
        call_headers = plugin._request_json.call_args.kwargs["headers"]
        self.assertEqual(call_headers["Authorization"], "Bearer sv")
        self.assertEqual(call_headers["Referer"], "https://g")


class DictConfigPreservationPatchTests(unittest.TestCase):
    """AstrBot 框架 dict 配置保留补丁（等价 AstrBotDevs/AstrBot#9512 修复）。

    验证 main.py 中的 _patch_astrbot_dict_config_preservation 补丁逻辑：
    参考值为空 dict 时保留用户键值对，非空参考值行为不变，补丁幂等。
    """

    def _patch_impl(self, refer_conf, conf, path=""):
        """与 main.py 中补丁后 check_config_integrity 逻辑一致（独立于 astrbot 环境）。"""
        has_new = False
        new_conf = {}
        for key, value in refer_conf.items():
            if key not in conf:
                new_conf[key] = value
                has_new = True
            elif conf[key] is None:
                new_conf[key] = value
                has_new = True
            elif isinstance(value, dict):
                if not isinstance(conf[key], dict):
                    new_conf[key] = value
                    has_new = True
                elif not value:
                    new_conf[key] = conf[key]
                else:
                    child_has_new = self._patch_impl(
                        value, conf[key], path + "." + key
                    )
                    new_conf[key] = conf[key]
                    has_new |= child_has_new
            else:
                new_conf[key] = conf[key]
        for key in list(conf.keys()):
            if key not in refer_conf:
                has_new = True
        if list(conf.keys()) != list(new_conf.keys()):
            has_new = True
        conf.clear()
        conf.update(new_conf)
        return has_new

    def _run(self, refer_conf, conf):
        """深拷贝后执行补丁版完整性检查。"""
        import copy

        conf = copy.deepcopy(conf)
        has_new = self._patch_impl(refer_conf, conf)
        return conf, has_new

    def test_dict_item_user_content_preserved(self):
        """dict 类型配置项（参考空 dict）保留用户键值对，不触发多余写回。"""
        refer = {"short_video": {"enable": True, "headers": {}}}
        conf, has_new = self._run(
            refer,
            {
                "short_video": {
                    "enable": True,
                    "headers": {"Authorization": "Bearer xxx"},
                }
            },
        )
        self.assertEqual(
            conf["short_video"]["headers"], {"Authorization": "Bearer xxx"}
        )
        self.assertFalse(has_new)

    def test_nested_dict_item_content_preserved(self):
        """嵌套在 object 内的 dict 项同样保留用户内容。"""
        refer = {
            "nested": {
                "type_obj": {
                    "share_groups": {},
                }
            }
        }
        conf, has_new = self._run(
            refer,
            {
                "nested": {
                    "type_obj": {
                        "share_groups": {"group_a": ["platform:type:id"]},
                    }
                }
            },
        )
        self.assertEqual(
            conf["nested"]["type_obj"]["share_groups"],
            {"group_a": ["platform:type:id"]},
        )
        self.assertFalse(has_new)

    def test_nonempty_refer_dict_still_recurses(self):
        """非空参考值（object 子配置）保持原有行为：缺失项补默认。"""
        refer = {"sub": {"a": 1, "b": 2}}
        conf, has_new = self._run(refer, {"sub": {"a": 1}})
        self.assertEqual(conf["sub"], {"a": 1, "b": 2})
        self.assertTrue(has_new)

    def test_type_mismatch_resets_to_default(self):
        """参考 dict 但用户值非 dict 时，仍重置为默认空 dict（行为不变）。"""
        refer = {"short_video": {"headers": {}}}
        conf, has_new = self._run(
            refer, {"short_video": {"headers": "not-a-dict"}}
        )
        self.assertEqual(conf["short_video"]["headers"], {})
        self.assertTrue(has_new)

    def test_missing_key_gets_default(self):
        """缺失配置项仍补默认值（行为不变）。"""
        refer = {"http": {"headers": {}}, "short_video": {"headers": {}}}
        conf, has_new = self._run(refer, {"short_video": {"headers": {}}})
        self.assertIn("http", conf)
        self.assertEqual(conf["http"]["headers"], {})
        self.assertTrue(has_new)

    def test_default_value_map_dict_injection(self):
        """老版本 DEFAULT_VALUE_MAP 缺 dict 时，补丁注入后 schema 能正常转默认配置。

        模拟 AstrBot v4.25.5（DEFAULT_VALUE_MAP 无 "dict"）：
        注入前 _config_schema_to_default_config 会抛 TypeError；
        注入后应返回空 dict 默认值。
        """
        # 老版本 DEFAULT_VALUE_MAP（无 dict）
        old_map = {
            "int": 0,
            "float": 0.0,
            "bool": False,
            "string": "",
            "text": "",
            "list": [],
            "file": [],
            "object": {},
            "template_list": [],
        }
        self.assertNotIn("dict", old_map)

        # 补丁等价操作：原地注入
        old_map["dict"] = {}

        def _schema_to_default(schema):
            conf = {}

            def _parse(schema, conf):
                for k, v in schema.items():
                    if v["type"] not in old_map:
                        raise TypeError(
                            f"不受支持的配置类型 {v['type']}。支持的类型有：{old_map.keys()}"
                        )
                    default = v.get("default", old_map[v["type"]])
                    if v["type"] == "object":
                        conf[k] = {}
                        _parse(v["items"], conf[k])
                    else:
                        conf[k] = __import__("copy").deepcopy(default)

            _parse(schema, conf)
            return conf

        schema = {
            "http": {
                "type": "object",
                "items": {
                    "headers": {"type": "dict", "default": {}},
                    "timeout_sec": {"type": "int", "default": 45},
                },
            },
            "short_video": {
                "type": "object",
                "items": {"headers": {"type": "dict", "default": {}}},
            },
        }
        default = _schema_to_default(schema)
        self.assertEqual(default["http"]["headers"], {})
        self.assertEqual(default["short_video"]["headers"], {})
        self.assertEqual(default["http"]["timeout_sec"], 45)


class _WechatEvent(_Event):
    def get_platform_name(self):
        return "gewechat"


class WechatPlatformTests(unittest.IsolatedAsyncioTestCase):
    def test_is_wechat_platform(self):
        self.assertTrue(bk.BKToolsPlugin._is_wechat_platform(_WechatEvent()))
        self.assertFalse(bk.BKToolsPlugin._is_wechat_platform(_Event()))

    async def test_packed_wechat_sends_plain_components(self):
        """微信平台打包降级：节点内容逐条作为普通消息发送，不产生合并转发。"""
        plugin = bk.BKToolsPlugin(object(), {})
        event = _WechatEvent()
        forward_nodes = [
            bk.Node(name="BKtools", uin=10000, content=[bk.Plain("标题"), bk.Plain("视频：https://x")]),
            bk.Node(name="BKtools", uin=10000, content=[bk.Plain("第二条")]),
        ]
        sent = await plugin._send_packed_or_json(
            event, forward_nodes, {"code": 200}, ("global", 0), "短视频解析结果"
        )
        self.assertTrue(sent)
        # 3 个组件逐条发送，全部是单组件普通消息，无 Nodes 合并转发
        self.assertEqual(len(event.sent), 3)
        self.assertTrue(all(r[0] == "chain" for r in event.sent))
        self.assertTrue(all(not isinstance(r[1], (list,)) or len(r[1]) == 1 for r in event.sent))

    async def test_packed_non_wechat_keeps_forward(self):
        """非微信平台保持合并转发不变。"""
        plugin = bk.BKToolsPlugin(object(), {})
        event = _Event()
        forward_nodes = [
            bk.Node(name="BKtools", uin=10000, content=[bk.Plain("视频")])
        ]
        sent = await plugin._send_packed_or_json(
            event, forward_nodes, {"code": 200}, ("global", 0), "短视频解析结果"
        )
        self.assertTrue(sent)
        self.assertEqual(len(event.sent), 1)
        # 合并转发：chain 里是 Nodes
        self.assertEqual(event.sent[0][0], "chain")

    async def test_json_long_wechat_splits_plain(self):
        """微信平台长 JSON 分条普通文本发送，不产生 Nodes 合并转发。"""
        plugin = bk.BKToolsPlugin(
            object(),
            {"runtime_limits": {"json_text_max_chars": 200}},
        )
        event = _WechatEvent()
        payload = {
            "data": [{"url": f"https://example.com/{i}", "text": "x" * 100} for i in range(20)]
        }
        sent = await plugin._send_json_result(event, payload, ("global", 0), "test")
        self.assertTrue(sent)
        self.assertGreater(len(event.sent), 1)
        self.assertTrue(all(r[0] == "plain" for r in event.sent))


if __name__ == "__main__":
    unittest.main()

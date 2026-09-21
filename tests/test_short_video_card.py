"""短视频整合卡片、批量打包、错误分类与脱敏的单元测试。

覆盖统一响应契约（video-parser-json-contract）中的 size/size_label、
duration、create_time/publish_time 归一化与展示，以及批量阈值打包行为。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tests"))

from test_json_batch_output import bk, _Event  # noqa: E402


def extract_text(obj):
    """递归提取 mock 组件中的字符串文本。"""
    out = []
    if isinstance(obj, str):
        return [obj]
    for attr in ("args", "content"):
        value = getattr(obj, attr, None)
        if not value:
            continue
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, (list, tuple)):
                for sub in item:
                    out.extend(extract_text(sub))
            elif hasattr(item, "args") or hasattr(item, "content"):
                out.extend(extract_text(item))
    return out


def sample_data(**overrides):
    data = {
        "type": "live",
        "title": "漂亮的御姐有杀气。#短发爽",
        "desc": "漂亮的御姐有杀气。#短发爽",
        "author": {"name": "口水狗狗！", "id": "1", "avatar": ""},
        "cover": "",
        "url": None,
        "quality": "",
        "duration": None,
        "size": 0,
        "size_label": "",
        "create_time": 1700000000,
        "publish_time": "2023-11-15 06:13:20",
        "images": ["https://x.com/1.jpg"],
        "live_photo": [{"image": "https://x.com/l1.jpg", "video": "https://x.com/l1.mp4"}],
        "music": {
            "title": "@我每天都很困创作的原声",
            "author": "我每天都很困",
            "url": "https://cdn.example/music.mp3",
        },
        "extra": {},
    }
    data.update(overrides)
    return data


class ContractFormatTests(unittest.TestCase):
    """契约字段格式化（size_label / duration / publish_time）。"""

    def test_size_label_contract_examples(self):
        self.assertEqual(bk._format_size_label(39245943), "37.43MB")
        self.assertEqual(bk._format_size_label(1048576), "1MB")
        self.assertEqual(bk._format_size_label(0), "")
        self.assertEqual(bk._format_size_label(1024), "1KB")
        self.assertEqual(bk._format_size_label("2048"), "2KB")

    def test_size_label_rejects_invalid(self):
        for bad in (None, "", "abc", -1, float("nan"), float("inf"), True):
            self.assertEqual(bk._format_size_label(bad), "", f"bad={bad!r}")

    def test_duration_format(self):
        self.assertEqual(bk._format_duration(15.25), "00:15")  # 截断小数秒
        self.assertEqual(bk._format_duration(75), "01:15")
        self.assertEqual(bk._format_duration(3661), "1:01:01")
        self.assertEqual(bk._format_duration(0), "")
        self.assertEqual(bk._format_duration(-5), "")
        self.assertEqual(bk._format_duration("bad"), "")

    def test_normalize_unix_seconds(self):
        # 秒保持不变，毫秒按契约除以 1000
        self.assertEqual(bk._normalize_unix_seconds(1700000000), 1700000000)
        self.assertEqual(bk._normalize_unix_seconds(1700000000123), 1700000000)
        self.assertEqual(bk._normalize_unix_seconds(0), 0)
        self.assertEqual(bk._normalize_unix_seconds(None), 0)
        self.assertEqual(bk._normalize_unix_seconds(True), 0)

    def test_publish_time_uses_asia_shanghai(self):
        # 1700000000 = 2023-11-15 06:13:20 Asia/Shanghai (UTC+8)
        self.assertEqual(
            bk._format_publish_time(1700000000, ""), "2023-11-15 06:13:20"
        )
        # 已格式化的接口值优先沿用
        self.assertEqual(
            bk._format_publish_time(0, "2020-01-01 00:00:00"), "2020-01-01 00:00:00"
        )
        # 非法值 → 空串，绝不用当前时间冒充
        self.assertEqual(bk._format_publish_time(0, ""), "")

    def test_collect_tags_dedup_and_priority(self):
        data = sample_data(extra={"tags": ["a", "#b", "a", ""]})
        j = {"data": data}
        self.assertEqual(bk._collect_tags(data, {}, j), ["a", "b"])


class RedactionTests(unittest.TestCase):
    """完整响应发送前的凭据脱敏。"""

    def test_sensitive_keys_masked(self):
        payload = {
            "token": "t",
            "api_key": "k",
            "apiKey": "k2",
            "nested": {"cookie": "c", "accessKey": "a"},
            "list": [{"password": "p"}],
        }
        out = bk._redact_sensitive(payload)
        self.assertEqual(out["token"], "***")
        self.assertEqual(out["api_key"], "***")
        self.assertEqual(out["apiKey"], "***")
        self.assertEqual(out["nested"]["cookie"], "***")
        self.assertEqual(out["nested"]["accessKey"], "***")
        self.assertEqual(out["list"][0]["password"], "***")

    def test_benign_keys_preserved(self):
        payload = {"monkey": "m", "author": "a", "size_label": "1MB", "msg": "ok"}
        out = bk._redact_sensitive(payload)
        self.assertEqual(out["monkey"], "m")
        self.assertEqual(out["author"], "a")
        self.assertEqual(out["size_label"], "1MB")
        self.assertEqual(out["msg"], "ok")


class ParseErrorHintTests(unittest.TestCase):
    """错误分类：接口故障给友好提示，业务失败透传 msg。"""

    def test_http_5xx_friendly_with_upstream_msg(self):
        err = bk.aiohttp.ClientResponseError(
            None, (), status=502,
            message="upstream server error (upstream: 服务暂时不可用)",
        )
        text = bk._describe_parse_error(err)
        self.assertIn("服务暂时不可用", text)  # 透传上游 msg
        self.assertNotIn("502", text)  # 不暴露原始响应码
        self.assertNotIn("message=", text)

    def test_http_429_and_4xx(self):
        err429 = bk.aiohttp.ClientResponseError(None, (), status=429, message="")
        self.assertIn("过于频繁", bk._describe_parse_error(err429))
        err400 = bk.aiohttp.ClientResponseError(None, (), status=400, message="")
        self.assertIn("拒绝", bk._describe_parse_error(err400))

    def test_timeout_and_connection(self):
        self.assertIn("超时", bk._describe_parse_error(asyncio.TimeoutError()))
        self.assertIn(
            "无法连接", bk._describe_parse_error(bk.aiohttp.ClientConnectionError("x"))
        )

    def test_business_value_error_passthrough(self):
        text = bk._describe_parse_error(ValueError("作品不存在或已删除"))
        self.assertEqual(text, "解析失败：作品不存在或已删除")

    def test_business_error_masks_url(self):
        text = bk._describe_parse_error(
            ValueError("bad link http://api.example.com/x?key=SECRET")
        )
        self.assertNotIn("SECRET", text)
        self.assertNotIn("api.example.com", text)

    def test_label_prefix(self):
        self.assertEqual(
            bk._describe_parse_error(ValueError("作品已删除"), "抖音主页解析"),
            "抖音主页解析失败：作品已删除",
        )

    def test_empty_result_hint_passthrough_msg(self):
        j = {"code": 200, "msg": "作品不存在或已删除", "data": {}}
        self.assertIn("作品不存在", bk._empty_result_hint(j, {}))
        # 通用成功文案不作为错误信息
        j2 = {"code": 200, "msg": "解析成功", "data": {}}
        self.assertNotIn("解析成功", bk._empty_result_hint(j2, {}))


class CardFormatTests(unittest.TestCase):
    """整合卡片字段与顺序。"""

    def _card(self, data, config=None):
        plugin = bk.BKToolsPlugin(object(), config or {})
        j = {"code": 200, "msg": "解析成功", "data": data}
        return plugin._format_short_video_text({}, data, j, include_video_line=False)

    def test_live_card_fields(self):
        text = self._card(sample_data())
        for expected in (
            "标题：漂亮的御姐有杀气。#短发爽",
            "类型：实况",
            "作者：口水狗狗！",
            "图集：共 1 张",
            "实况：共 1 个",
            "实况封面：共 1 张",
            "原声：@我每天都很困创作的原声 / 我每天都很困",
            "原声链接：",
        ):
            self.assertIn(expected, text)

    def test_video_card_metadata(self):
        data = sample_data(
            type="video",
            url="https://x.com/v.mp4",
            quality="1080p",
            duration=15.25,
            size=39245943,
            size_label="过期的旧标签",  # 必须按数值重算
            images=[],
            live_photo=[],
            extra={"tags": ["壁纸"]},
        )
        text = self._card(data)
        self.assertIn("类型：视频", text)
        self.assertIn("时长：00:15", text)
        self.assertIn("大小：37.43MB", text)
        self.assertNotIn("过期的旧标签", text)
        self.assertIn("画质：1080p", text)
        self.assertIn("标签：#壁纸", text)
        self.assertIn("发布时间：2023-11-15 06:13:20", text)

    def test_image_card_has_no_video_meta(self):
        data = sample_data(
            type="image", url=None, images=[f"https://x.com/{i}.jpg" for i in range(3)],
            live_photo=[],
        )
        text = self._card(data)
        self.assertIn("类型：图集", text)
        self.assertIn("图集：共 3 张", text)
        self.assertNotIn("大小：", text)
        self.assertNotIn("时长：", text)

    def test_type_inferred_when_missing(self):
        data = sample_data(type="", live_photo=[], images=["https://x.com/a.jpg"])
        self.assertIn("类型：图集", self._card(data))

    def test_show_extended_can_be_disabled(self):
        plugin = bk.BKToolsPlugin(object(), {})
        data = sample_data()
        j = {"code": 200, "data": data}
        text = plugin._format_short_video_text(
            {}, data, j, include_video_line=False, show_extended=False
        )
        self.assertNotIn("标签：", text)
        self.assertNotIn("发布时间：", text)


class BatchOutputTests(unittest.IsolatedAsyncioTestCase):
    """批量阈值 → 完整响应打包消息。"""

    def _plugin(self, batch_cfg):
        return bk.BKToolsPlugin(
            object(),
            {
                "batch_output": batch_cfg,
                "message": {"opening_enable": False},
                # 关闭视频大小探测，避免测试发起真实网络请求
                "video_threshold": {"threshold_enabled": False},
            },
        )

    async def _reply(self, plugin, data):
        j = {"code": 200, "msg": "解析成功", "data": data}
        plugin._fetch_short_video = lambda link: _async((j, data))
        event = _Event()
        await plugin._reply_short_video(event, "https://v.douyin.com/abc/")
        return event

    async def test_threshold_sends_one_packed_message_with_header_and_json(self):
        data = sample_data(images=[f"https://x.com/{i}.jpg" for i in range(5)])
        plugin = self._plugin(
            {"direct_json_enabled": True, "image_count_threshold": 3,
             "live_photo_count_threshold": 10}
        )
        event = await self._reply(plugin, data)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.sent[0][0], "chain")
        texts = extract_text(event.sent[0][1][0])
        joined = "\n".join(texts)
        self.assertIn("标题：", joined)      # 摘要卡片
        self.assertIn('"code":200', joined)  # 完整响应
        self.assertIn('"images"', joined)

    async def test_threshold_none_config_does_not_crash(self):
        data = sample_data(images=[f"https://x.com/{i}.jpg" for i in range(5)])
        plugin = self._plugin(
            {"direct_json_enabled": True, "image_count_threshold": None,
             "live_photo_count_threshold": None}
        )
        event = await self._reply(plugin, data)  # 不应抛 TypeError
        self.assertGreaterEqual(len(event.sent), 1)

    async def test_threshold_string_config_parsed(self):
        data = sample_data(images=[f"https://x.com/{i}.jpg" for i in range(5)])
        plugin = self._plugin(
            {"direct_json_enabled": True, "image_count_threshold": "3",
             "live_photo_count_threshold": "10"}
        )
        event = await self._reply(plugin, data)
        joined = "\n".join(extract_text(event.sent[0][1][0]))
        self.assertIn('"code":200', joined)

    async def test_below_threshold_sends_normal_card(self):
        data = sample_data(images=["https://x.com/1.jpg"])
        plugin = self._plugin(
            {"direct_json_enabled": True, "image_count_threshold": 20,
             "live_photo_count_threshold": 10}
        )
        event = await self._reply(plugin, data)
        joined = "\n".join(extract_text(event.sent[0][1][0]))
        self.assertIn("标题：", joined)
        self.assertNotIn('"code":200', joined)  # 未触发完整响应

    async def test_batch_payload_redacts_credentials(self):
        data = sample_data(extra={"token": "SECRET", "tags": ["t"]})
        plugin = self._plugin(
            {"direct_json_enabled": True, "image_count_threshold": 0,
             "live_photo_count_threshold": 0}
        )
        event = await self._reply(plugin, data)
        joined = "\n".join(extract_text(event.sent[0][1][0]))
        self.assertNotIn("SECRET", joined)
        self.assertIn('"token":"***"', joined)

    async def test_empty_result_passes_through_interface_msg(self):
        plugin = self._plugin({"direct_json_enabled": False})
        j = {"code": 200, "msg": "作品不存在或已删除", "data": {"images": [], "live_photo": []}}
        plugin._fetch_short_video = lambda link: _async((j, j["data"]))
        event = _Event()
        await plugin._reply_short_video(event, "https://v.douyin.com/abc/")
        self.assertEqual(len(event.sent), 1)
        self.assertIn("作品不存在", str(event.sent[0][1]))

    async def test_error_hint_is_friendly(self):
        plugin = self._plugin({"direct_json_enabled": False})

        async def boom(link):
            raise bk.aiohttp.ClientResponseError(
                None, (), status=502,
                message="upstream server error (upstream: 服务暂时不可用)",
            )

        plugin._fetch_short_video = boom
        event = _Event()
        await plugin._reply_short_video(event, "https://v.douyin.com/abc/")
        text = str(event.sent[0][1])
        self.assertIn("服务暂时不可用", text)
        self.assertNotIn("502", text)


def _async(value):
    async def _coro():
        return value

    return _coro()


if __name__ == "__main__":
    unittest.main()

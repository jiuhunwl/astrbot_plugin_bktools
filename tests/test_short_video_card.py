"""短视频整合卡片、批量打包、错误分类与脱敏的单元测试。

覆盖统一响应契约（video-parser-json-contract）中的 size/size_label、
duration、create_time/publish_time 归一化与展示，以及批量阈值打包行为。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

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

    def _plugin(self, batch_cfg, message_cfg=None):
        cfg = {
            "batch_output": batch_cfg,
            "message": {"opening_enable": False},
            # 关闭视频大小探测，避免测试发起真实网络请求
            "video_threshold": {"threshold_enabled": False},
        }
        if message_cfg:
            cfg["message"].update(message_cfg)
        return bk.BKToolsPlugin(object(), cfg)

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
             "live_photo_count_threshold": 10},
            message_cfg={"batch_full_response_mode": "forward"},
        )
        event = await self._reply(plugin, data)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.sent[0][0], "chain")
        texts = extract_text(event.sent[0][1][0])
        joined = "\n".join(texts)
        self.assertIn("标题：", joined)      # 摘要卡片
        self.assertIn('"code":200', joined)  # 完整响应
        self.assertIn('"images"', joined)

    async def test_threshold_default_sends_summary_plus_json_file(self):
        """默认 file 模式：摘要卡片（普通文本）+ JSON 文件（File 组件）。"""
        data = sample_data(images=[f"https://x.com/{i}.jpg" for i in range(5)])
        plugin = self._plugin(
            {"direct_json_enabled": True, "image_count_threshold": 3,
             "live_photo_count_threshold": 10}
        )
        event = await self._reply(plugin, data)
        self.assertEqual(len(event.sent), 2)
        kind0, payload0 = event.sent[0]
        self.assertEqual(kind0, "plain")
        self.assertIn("标题：", payload0)
        kind1, payload1 = event.sent[1]
        self.assertEqual(kind1, "chain")
        (comp,) = payload1
        self.assertEqual(comp.kwargs.get("name", "").startswith("BKtools_解析响应_"), True)
        fpath = comp.kwargs.get("file")
        self.assertTrue(os.path.exists(fpath))
        with open(fpath, "rb") as fh:
            content = json.loads(fh.read().decode("utf-8"))
        self.assertEqual(content["code"], 200)
        self.assertIn("images", content["data"])
        os.remove(fpath)

    async def test_threshold_file_mode_redacts_credentials(self):
        data = sample_data(extra={"token": "SECRET"})
        plugin = self._plugin(
            {"direct_json_enabled": True, "image_count_threshold": 0,
             "live_photo_count_threshold": 0}
        )
        event = await self._reply(plugin, data)
        kind, payload = event.sent[1]
        (comp,) = payload
        with open(comp.kwargs.get("file"), "rb") as fh:
            content = fh.read().decode("utf-8")
        self.assertNotIn("SECRET", content)
        self.assertIn('"token":"***"', content)
        os.remove(comp.kwargs.get("file"))

    async def test_threshold_file_send_failure_falls_back_to_forward(self):
        data = sample_data(images=[f"https://x.com/{i}.jpg" for i in range(5)])
        plugin = self._plugin(
            {"direct_json_enabled": True, "image_count_threshold": 3,
             "live_photo_count_threshold": 10}
        )
        # 仅让「JSON 文件」这一步失败（如 NapCat 读不到本地文件），其余照常成功
        real_dispatch = plugin._dispatch_send

        async def _fail_file_only(event_, result, label, **kw):
            if label.endswith("（JSON 文件）"):
                return "failed"
            return await real_dispatch(event_, result, label, **kw)

        plugin._dispatch_send = _fail_file_only
        event = await self._reply(plugin, data)
        kinds = [k for k, _ in event.sent]
        self.assertEqual(kinds, ["plain", "chain"])
        texts = "\n".join(extract_text(event.sent[-1][1][0]))
        self.assertIn('"code":200', texts)

    async def test_batch_file_oversize_returns_false_without_sending(self):
        """JSON 超过输出限制时，文件方式直接放弃（不发出任何消息），交由打包消息兜底。"""
        data = sample_data(images=["https://x.com/1.jpg"])
        plugin = self._plugin({})
        plugin._runtime_limits_cfg = lambda: {"json_output_max_bytes": 16}
        event = _Event()
        ok = await plugin._send_batch_as_file(
            event, {"code": 200, "data": data}, "摘要", ("t", 1), "测试"
        )
        self.assertFalse(ok)
        self.assertEqual(event.sent, [])

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
             "live_photo_count_threshold": "10"},
            message_cfg={"batch_full_response_mode": "forward"},
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
             "live_photo_count_threshold": 0},
            message_cfg={"batch_full_response_mode": "forward"},
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


class MusicMetaTests(unittest.TestCase):
    """音乐字段清洗：占位符过滤、多路径兜底与可读化转换。"""

    def test_placeholder_values_filtered(self):
        for bad in ("未知艺术家", "未知歌手", "未知", "unknown", "Unknown Artist", "-", "—", "n/a", "none"):
            self.assertEqual(bk._clean_meta(bad), "", bad)
        self.assertEqual(bk._clean_meta(None), "")
        self.assertEqual(bk._clean_meta("   "), "")
        self.assertEqual(bk._clean_meta("周杰伦"), "周杰伦")
        self.assertEqual(bk._clean_meta(0), "0")

    def test_first_meta_skips_empty_and_placeholder(self):
        data = {"albumname": "", "artistsname": "未知艺术家", "name": "夜曲"}
        self.assertEqual(bk._first_meta(data, ("albumname", "name"), {}), "夜曲")
        self.assertEqual(
            bk._first_meta(data, ("artistsname", "artist", "author"), {}), ""
        )

    def test_paths_fall_back_when_configured_path_missing(self):
        # 上游改了字段名：配置的 albumname/artistsname 缺失，仍需兜底到 title/artist
        data = {"title": "来自天堂的魔鬼", "artist": "Zxxy", "url": "https://x/a.m4a"}
        fields = bk._extract_music_fields(
            data, {}, title_p="albumname", author_p="artistsname", audio_p="url"
        )
        self.assertEqual(fields["title"], "来自天堂的魔鬼")
        self.assertEqual(fields["author"], "Zxxy")
        self.assertEqual(fields["audio"], "https://x/a.m4a")

    def test_size_and_bitrate_readable(self):
        self.assertEqual(bk._format_audio_size(487203), "475.78KB")
        self.assertEqual(bk._format_audio_size("26.86 MB"), "26.86 MB")
        self.assertEqual(bk._format_audio_size(""), "")
        self.assertEqual(bk._format_bitrate(129916), "130kbps")
        self.assertEqual(bk._format_bitrate(1119746), "1.12Mbps")
        self.assertEqual(bk._format_bitrate(None), "")

    def test_quality_label_mapping(self):
        self.assertEqual(bk._format_music_quality("higher"), "较高")
        self.assertEqual(bk._format_music_quality("lossless"), "无损")
        self.assertEqual(bk._format_music_quality("未知"), "")
        self.assertEqual(bk._format_music_quality("96k"), "96k")


class MusicCardTests(unittest.TestCase):
    """音乐卡片渲染：平台名、字段顺序与长链接截断。"""

    QISHUI = {
        "url": "https://v11-luna.douyinvod.com/" + "a" * 400,
        "video_meta": {
            "quality": "higher",
            "vtype": "m4a",
            "codec_type": "aac",
            "bitrate": 129916,
            "size": 487203,
        },
        "albumname": "来自天堂的魔鬼",
        "artistsname": "Zxxy（加勒比Music）",
        "artistsmedium_avatar_url": ["https://p3.douyinpic.com/avatar.jpeg"],
    }

    def _fields(self, data, **kw):
        kw.setdefault("title_p", "albumname")
        kw.setdefault("author_p", "artistsname")
        kw.setdefault("audio_p", "url")
        return bk._extract_music_fields(data, {"data": data}, **kw)

    def test_platform_label_is_chinese(self):
        card = bk._format_music_card("qishui", self._fields(self.QISHUI))
        self.assertTrue(card.startswith("平台：汽水音乐\n"))
        self.assertNotIn("qishui", card)
        self.assertIn("平台：QQ音乐", bk._format_music_card("qq", self._fields(self.QISHUI)))
        self.assertIn("平台：酷我音乐", bk._format_music_card("kuwo", self._fields(self.QISHUI)))
        self.assertIn("平台：网易云音乐", bk._format_music_card("netease", self._fields(self.QISHUI)))

    def test_qishui_card_reads_video_meta(self):
        card = bk._format_music_card("qishui", self._fields(self.QISHUI))
        self.assertIn("曲名：来自天堂的魔鬼", card)
        self.assertIn("艺人：Zxxy（加勒比Music）", card)
        self.assertIn("大小：475.78KB", card)
        self.assertIn("音质：M4A · 较高 · 130kbps", card)

    def test_unknown_artist_line_omitted(self):
        data = dict(self.QISHUI, artistsname="未知艺术家")
        card = bk._format_music_card("qishui", self._fields(data))
        self.assertNotIn("未知艺术家", card)
        self.assertNotIn("艺人：", card)

    def test_audio_url_always_full_link(self):
        """直链必须完整展示——截断过的链接复制出去没法用。"""
        card = bk._format_music_card("qishui", self._fields(self.QISHUI))
        audio_line = [ln for ln in card.splitlines() if ln.startswith("音频：")][0]
        self.assertEqual(audio_line, "音频：" + self.QISHUI["url"])
        self.assertNotIn("…", audio_line)

    def test_missing_title_renders_dash(self):
        card = bk._format_music_card("qishui", self._fields({"url": "https://x/a.mp3"}))
        self.assertIn("曲名：—", card)

    def test_karaoke_lyric_is_cleaned(self):
        data = dict(
            self.QISHUI,
            lyric="[1980,1990]<0,260,0>风<260,240,0>扬\n[5740,2430]<0,270,0>谁<280,240,0>执",
        )
        card = bk._format_music_card("qishui", self._fields(data))
        self.assertIn("歌词预览：\n风扬\n谁执", card)
        self.assertNotIn("<0,260,0>", card)
        self.assertNotIn("[1980,1990]", card)

    def test_lyric_preview_is_capped(self):
        data = dict(self.QISHUI, lyric="\n".join(f"第{i}行歌词" for i in range(20)))
        card = bk._format_music_card("qishui", self._fields(data))
        preview = card.split("歌词预览：\n", 1)[1]
        self.assertEqual(len(preview.splitlines()), 6)
        self.assertTrue(preview.endswith("…"))

    def test_lyric_url_rendered_as_link(self):
        data = dict(self.QISHUI, lyric="https://x.com/lrc")
        card = bk._format_music_card("qishui", self._fields(data))
        self.assertIn("歌词链接：https://x.com/lrc", card)
        self.assertNotIn("歌词预览", card)


def _async(value):
    async def _coro():
        return value

    return _coro()


class _EventOnPlatform(_Event):
    """模拟 get_platform_name 返回不同平台的 Event。"""

    def __init__(self, platform):
        super().__init__()
        self._platform = platform

    def get_platform_name(self):
        return self._platform


class AdapterCapsTests(unittest.IsolatedAsyncioTestCase):
    """平台适配器能力：官方机器人/微信自动降级 + 自定义适配器覆盖。"""

    def _plugin(self, message_cfg=None, batch_cfg=None):
        cfg = {"opening_enable": False, "short_video_text_metadata": True}
        cfg.update(message_cfg or {})
        top = {
            "message": cfg,
            "video_threshold": {"threshold_enabled": False},
        }
        if batch_cfg is not None:
            top["batch_output"] = batch_cfg
        return bk.BKToolsPlugin(object(), top)

    def test_builtin_caps_matching(self):
        plugin = self._plugin()
        self.assertTrue(plugin._adapter_caps(_EventOnPlatform("aiocqhttp"))["forward"])
        self.assertTrue(plugin._adapter_caps(_EventOnPlatform("aiocqhttp"))["file"])
        # QQ 官方机器人：不支持合并转发与文件
        caps = plugin._adapter_caps(_EventOnPlatform("qq_official"))
        self.assertFalse(caps["forward"])
        self.assertFalse(caps["file"])
        self.assertEqual(caps["text_chunk_chars"], 1000)
        # NapCat 自定义平台名（特征 token 匹配）
        self.assertTrue(plugin._adapter_caps(_EventOnPlatform("napcat_custom"))["forward"])
        # 微信系
        self.assertFalse(plugin._adapter_caps(_EventOnPlatform("wechatpadpro"))["forward"])
        # 未知平台：沿用旧行为（合并转发）
        caps = plugin._adapter_caps(_EventOnPlatform("some_new_adapter"))
        self.assertTrue(caps["forward"])
        self.assertTrue(caps["file"])

    def test_custom_caps_override(self):
        plugin = self._plugin({
            "custom_adapter_caps": json.dumps(
                {"mybot": {"forward": False, "file": True, "text_chunk_chars": 800}}
            )
        })
        caps = plugin._adapter_caps(_EventOnPlatform("MyBot"))
        self.assertFalse(caps["forward"])
        self.assertTrue(caps["file"])
        self.assertEqual(caps["text_chunk_chars"], 800)

    def test_custom_caps_invalid_json_ignored(self):
        plugin = self._plugin({"custom_adapter_caps": "{not json"})
        # 非法 JSON 不抛错，aiocqhttp 用内置能力
        self.assertTrue(plugin._adapter_caps(_EventOnPlatform("aiocqhttp"))["forward"])

    async def test_json_result_on_official_bot_sends_plain_chunks(self):
        """QQ 官方机器人：长 JSON 不走合并转发，按 1000 字符分片发普通文本。"""
        plugin = self._plugin()
        big = {"code": 200, "data": {"pad": "x" * 5000}}
        event = _EventOnPlatform("qq_official")
        ok = await plugin._send_json_result(event, big, ("t", 0), "测试")
        self.assertTrue(ok)
        kinds = [k for k, _ in event.sent]
        self.assertTrue(kinds and all(k == "plain" for k in kinds))
        self.assertGreater(len(kinds), 1)  # 5000+ 字符被按 1000 字符分片
        joined = "".join(str(t) for _, t in event.sent)
        self.assertIn('"code":200', joined)

    async def test_batch_file_mode_skipped_on_official_bot(self):
        """批量超阈值 + file 模式：官方机器人不支持文件 → 自动降级为分片文本。"""
        data = sample_data(images=[f"https://x.com/{i}.jpg" for i in range(5)])
        plugin = self._plugin(
            batch_cfg={"direct_json_enabled": True, "image_count_threshold": 3,
                       "live_photo_count_threshold": 10}
        )
        j = {"code": 200, "msg": "解析成功", "data": data}
        plugin._fetch_short_video = lambda link: _async((j, data))
        event = _EventOnPlatform("qq_official")
        await plugin._reply_short_video(event, "https://v.douyin.com/abc/")
        kinds = [k for k, _ in event.sent]
        self.assertTrue(kinds)
        self.assertNotIn("chain", kinds)  # 没有任何合并转发
        self.assertTrue(all(k == "plain" for k in kinds))

    async def test_batch_file_mode_still_used_on_napcat(self):
        """回归：NapCat（aiocqhttp）平台批量输出仍走文件模式。"""
        data = sample_data(images=[f"https://x.com/{i}.jpg" for i in range(5)])
        plugin = self._plugin(
            batch_cfg={"direct_json_enabled": True, "image_count_threshold": 3,
                       "live_photo_count_threshold": 10}
        )
        j = {"code": 200, "msg": "解析成功", "data": data}
        plugin._fetch_short_video = lambda link: _async((j, data))
        event = _EventOnPlatform("aiocqhttp")
        await plugin._reply_short_video(event, "https://v.douyin.com/abc/")
        kinds = [k for k, _ in event.sent]
        self.assertEqual(kinds, ["plain", "chain"])  # 摘要 + JSON 文件


class _MDResult:
    """记录 use_markdown 标记的 mock 发送结果。"""

    def __init__(self, kind, text=None, content=None):
        self.kind = kind
        self.text = text
        self.content = content
        self.markdown = None  # 未设置

    def use_markdown(self, use):
        self.markdown = use
        return self


class _MDEvent(_EventOnPlatform):
    """plain_result/chain_result 返回可记录 use_markdown 标记的对象。"""

    def __init__(self, platform):
        super().__init__(platform)
        self.sent = []

    def plain_result(self, text):
        return _MDResult("plain", text=text)

    def chain_result(self, content):
        return _MDResult("chain", content=content)

    async def send(self, result):
        self.sent.append(result)


class MarkdownizeTests(unittest.TestCase):
    """卡片文本 Markdown 化：仅加粗「标签：值」行的标签。"""

    def test_bold_field_labels(self):
        text = "标题：测试视频\n类型：视频\n作者：小明"
        out = bk._markdownize_card_text(text)
        self.assertEqual(
            out,
            "**标题**：测试视频\n**类型**：视频\n**作者**：小明",
        )

    def test_url_value_kept_intact(self):
        text = "视频直链：https://example.com/a.mp4"
        out = bk._markdownize_card_text(text)
        self.assertEqual(out, "**视频直链**：https://example.com/a.mp4")

    def test_json_line_untouched(self):
        text = '{"code":200,"data":{"url":"x"}}'
        self.assertEqual(bk._markdownize_card_text(text), text)

    def test_lines_without_colon_untouched(self):
        text = "作者头像"
        self.assertEqual(bk._markdownize_card_text(text), text)

    def test_ascii_colon_supported(self):
        self.assertEqual(
            bk._markdownize_card_text("size: 12MB"),
            "**size**：12MB",
        )

    def test_empty_value_line_untouched(self):
        self.assertEqual(bk._markdownize_card_text("简介："), "简介：")


class OfficialMarkdownTests(unittest.IsolatedAsyncioTestCase):
    """QQ 官方机器人 Markdown 输出：摘要加粗排版 + JSON 分片强制纯文本。"""

    def _plugin(self, message_cfg=None):
        cfg = {"opening_enable": False, "short_video_text_metadata": True}
        cfg.update(message_cfg or {})
        return bk.BKToolsPlugin(
            object(),
            {"message": cfg, "video_threshold": {"threshold_enabled": False}},
        )

    def test_caps_markdown_flags(self):
        plugin = self._plugin()
        self.assertTrue(plugin._adapter_caps(_EventOnPlatform("qq_official"))["markdown"])
        self.assertFalse(plugin._adapter_caps(_EventOnPlatform("aiocqhttp"))["markdown"])
        # 未知平台默认关闭 Markdown
        self.assertFalse(
            plugin._adapter_caps(_EventOnPlatform("some_new_adapter"))["markdown"]
        )

    def test_custom_caps_markdown_override(self):
        plugin = self._plugin({
            "custom_adapter_caps": json.dumps({"mybot": {"markdown": True}})
        })
        self.assertTrue(plugin._adapter_caps(_EventOnPlatform("MyBot"))["markdown"])

    def test_official_markdown_config_toggle(self):
        plugin = self._plugin()
        self.assertTrue(plugin._official_markdown_enabled(_EventOnPlatform("qq_official")))
        self.assertFalse(plugin._official_markdown_enabled(_EventOnPlatform("aiocqhttp")))
        off = self._plugin({"official_markdown": False})
        self.assertFalse(off._official_markdown_enabled(_EventOnPlatform("qq_official")))

    async def test_official_header_markdownized_and_json_plain(self):
        """官方机器人：摘要卡片 Markdown 排版，JSON 分片强制纯文本。"""
        plugin = self._plugin()
        big = {"code": 200, "data": {"pad": "x" * 5000}}
        event = _MDEvent("qq_official")
        ok = await plugin._send_json_result(
            event, big, ("t", 0), "测试", header_text="标题：测试视频\n类型：视频",
        )
        self.assertTrue(ok)
        self.assertGreaterEqual(len(event.sent), 2)
        first, *rest = event.sent
        self.assertEqual(first.markdown, True)
        self.assertIn("**标题**：测试视频", first.text)
        joined = "".join(str(p.text) for p in rest)
        self.assertIn('"code":200', joined)
        for piece in rest:
            self.assertEqual(piece.markdown, False)

    async def test_official_markdown_disabled_keeps_plain(self):
        """关闭 official_markdown：摘要不做 Markdown 化，全部强制纯文本。"""
        plugin = self._plugin({"official_markdown": False})
        event = _MDEvent("qq_official")
        ok = await plugin._send_json_result(
            event, {"code": 200}, ("t", 0), "测试", header_text="标题：测试视频",
        )
        self.assertTrue(ok)
        self.assertEqual(len(event.sent), 2)
        for piece in event.sent:
            self.assertEqual(piece.markdown, False)
        self.assertNotIn("**标题**", event.sent[0].text)

    async def test_napcat_untouched_by_markdown_logic(self):
        """回归：aiocqhttp 平台不走 Markdown 标记（保持旧纯文本行为）。"""
        plugin = self._plugin()
        event = _MDEvent("aiocqhttp")
        ok = await plugin._send_json_result(
            event, {"code": 200}, ("t", 0), "测试", header_text="标题：测试",
        )
        self.assertTrue(ok)
        for piece in event.sent:
            self.assertIsNone(piece.markdown)

    async def test_button_attached_and_fallback_without_button(self):
        """开启按钮：摘要带按钮发送；无 keyboard 权限失败时降级无按钮重发。"""
        plugin = self._plugin({"official_buttons": True})
        event = _MDEvent("qq_official")

        async def send(result):
            if result.kind == "chain":  # 按钮消息模拟无权限失败
                raise RuntimeError("keyboard not allowed")
            event.sent.append(result)

        event.send = send
        with patch.object(bk, "_BUTTONS_READY", True), \
                patch.object(bk, "Button", _StubButton), \
                patch.object(bk, "ActionRow", _StubActionRow):
            ok = await plugin._send_json_result(
                event, {"code": 200}, ("t", 0), "测试",
                header_text="标题：测试", link_url="https://v.douyin.com/abc/",
            )
        self.assertTrue(ok)
        self.assertEqual(len(event.sent), 2)
        # 第一条是无按钮降级卡片（Markdown），第二条是 JSON 分片
        self.assertEqual(event.sent[0].markdown, True)
        self.assertEqual(event.sent[1].markdown, False)

    async def test_forward_plain_first_card_with_buttons(self):
        """正常解析降级路径：官方机器人首卡片附按钮行（卡片+按钮形态）。"""
        plugin = self._plugin({"official_buttons": True})
        card = bk.Plain()
        card.text = "标题：测试\n视频直链：https://x.com/v.mp4"
        nodes = [
            bk.Node(name="B", uin=10000, content=[card]),
            bk.Node(name="B", uin=10000, content=[bk.Image("https://x.com/1.jpg")]),
        ]
        event = _MDEvent("qq_official")
        with patch.object(bk, "_BUTTONS_READY", True), \
                patch.object(bk, "Button", _StubButton), \
                patch.object(bk, "ActionRow", _StubActionRow):
            sent = await plugin._send_forward_as_plain(
                event, nodes, ("t", 0), "测试降级",
                link_url="https://v.douyin.com/abc",
            )
        self.assertTrue(sent)
        first = event.sent[0]
        self.assertEqual(first.kind, "chain")
        self.assertEqual(first.markdown, True)
        # [Plain 播放器卡片, ActionRow 按钮行]
        self.assertEqual(len(first.content), 2)
        labels = [b.label for b in first.content[1].buttons]
        self.assertEqual(labels[0], "🔗 原始链接")
        self.assertIn("▶ 播放视频", labels)

    async def test_buttons_default_off_no_button_message(self):
        """默认配置不开按钮：只有纯摘要消息，没有 chain 形态按钮消息。"""
        plugin = self._plugin()
        event = _MDEvent("qq_official")
        ok = await plugin._send_json_result(
            event, {"code": 200}, ("t", 0), "测试",
            header_text="标题：测试", link_url="https://v.douyin.com/abc/",
        )
        self.assertTrue(ok)
        self.assertTrue(all(p.kind == "plain" for p in event.sent))

    async def test_forward_as_plain_markdownizes_card_on_official(self):
        """官方机器人降级逐条发送：Plain 卡片被 Markdown 化。"""
        plugin = self._plugin()
        card = bk.Plain()
        card.text = "标题：测试视频"  # 测试桩组件无 text 参数，手动补属性
        nodes = [
            bk.Node(name="BKtools", uin=10000, content=[card]),
            bk.Node(name="BKtools", uin=10000, content=[bk.Image("https://x.com/1.jpg")]),
        ]
        event = _MDEvent("qq_official")
        sent_any = await plugin._send_forward_as_plain(
            event, nodes, ("t", 0), "测试降级"
        )
        self.assertTrue(sent_any)
        self.assertEqual(len(event.sent), 2)
        self.assertIn("**标题**：测试视频", event.sent[0].text)
        self.assertEqual(event.sent[0].markdown, True)


class _StubButton:
    """按钮组件测试桩（模拟 bk.Button 接口）。"""

    class Style:
        PRIMARY = "primary"

    class UrlAction:
        def __init__(self, url):
            self.url = url

    def __init__(self, id, label, action, style=None):
        self.id = id
        self.label = label
        self.action = action
        self.style = style


class _StubActionRow:
    def __init__(self, buttons):
        self.buttons = buttons


class OfficialButtonRowsTests(unittest.TestCase):
    """官方机器人按钮行：多按钮 + 超长 URL 过滤 + 组件缺失停用。"""

    def _plugin(self, message_cfg=None):
        cfg = {"opening_enable": False, "short_video_text_metadata": True}
        cfg.update(message_cfg or {})
        return bk.BKToolsPlugin(
            object(),
            {"message": cfg, "video_threshold": {"threshold_enabled": False}},
        )

    def _rows(self, plugin, card, link):
        with patch.object(bk, "_BUTTONS_READY", True), \
                patch.object(bk, "Button", _StubButton), \
                patch.object(bk, "ActionRow", _StubActionRow):
            return plugin._official_button_rows(card, link)

    def test_link_and_card_urls(self):
        plugin = self._plugin()
        card = "曲名：歌\n音频直链：https://m.example.com/a.mp3\n歌词链接：https://l.example.com/x"
        rows = self._rows(plugin, card, "https://v.douyin.com/abc")
        self.assertEqual(len(rows), 1)
        labels = [b.label for b in rows[0].buttons]
        self.assertEqual(labels[0], "🔗 原始链接")
        self.assertIn("▶ 播放音乐", labels)
        self.assertIn("📄 歌词", labels)
        urls = [b.action.url for b in rows[0].buttons]
        self.assertEqual(urls[0], "https://v.douyin.com/abc")

    def test_oversize_url_skipped(self):
        """超过 1024 字节的直链不能作为按钮（QQ keyboard action data 上限）。"""
        plugin = self._plugin()
        card = "视频直链：https://x.com/" + "a" * 2000
        rows = self._rows(plugin, card, "https://v.douyin.com/abc")
        labels = [b.label for b in rows[0].buttons]
        self.assertEqual(labels, ["🔗 原始链接"])

    def test_components_missing_returns_empty(self):
        """AstrBot 无按钮组件（_BUTTONS_READY False）→ 空列表自动停用。"""
        plugin = self._plugin()
        self.assertEqual(plugin._official_button_rows("标题：x", "https://a.com"), [])

    def test_no_urls_returns_empty(self):
        plugin = self._plugin()
        self.assertEqual(self._rows(plugin, "标题：x", ""), [])

    def test_max_five_buttons(self):
        plugin = self._plugin()
        card = "".join(
            f"链接{i}：https://x.com/{i}\n" for i in range(8)
        )
        rows = self._rows(plugin, card, "https://v.douyin.com/abc")
        self.assertLessEqual(len(rows[0].buttons), 5)


class MusicV2FormatTests(unittest.TestCase):
    """音乐解析统一 JSON 2.0 适配：新字段默认优先、1.0 字段兜底、卡片新行。"""

    V2 = {
        "type": "music",
        "platform": "qishui",
        "id": "7387697402612500481",
        "name": "示例歌曲",
        "singer": "示例歌手",
        "artists": [{"id": "64229713379884", "name": "示例歌手2", "avatar": "https://p3.example.com/a.jpeg"}],
        "album": "示例专辑",
        "cover": "https://p3.example.com/cover.jpeg",
        "duration_ms": 252794,
        "full_duration_ms": 252794,
        "preview": False,
        "lyric": "[00:00.240]示例第一行\n[00:04.000]示例第二行",
        "url": "https://v11-luna.douyinvod.com/xxx",
        "level": "highest",
        "label": "极高音质",
        "quality_type": 3,
        "br": 257694,
        "size": 1930486,
        "size_label": "1.84MB",
        "format": "m4a",
        "codec": "aac",
        "qualities": [
            {"label": "极高音质", "level": "highest", "quality_type": 3,
             "url": "https://x/h.m4a", "size_label": "1.84MB"},
            {"label": "无损音质", "level": "lossless", "quality_type": 4,
             "url": "", "locked": True},
        ],
    }

    def _fields(self, data, **kw):
        return bk._extract_music_fields(data, {"data": data}, **kw)

    def test_v2_fields_extracted_with_new_defaults(self):
        """2.0 新默认路径（name/singer/album/label）直接命中。"""
        f = self._fields(self.V2)
        self.assertEqual(f["title"], "示例歌曲")
        self.assertEqual(f["author"], "示例歌手")
        self.assertEqual(f["album"], "示例专辑")
        self.assertEqual(f["cover"], "https://p3.example.com/cover.jpeg")
        self.assertEqual(f["level"], "极高音质")  # label 原样展示（已是中文）
        self.assertEqual(f["bitrate"], "258kbps")  # 2.0 br（bit/s）
        self.assertEqual(f["size"], "1.84MB")
        self.assertEqual(f["duration"], "04:12")  # full_duration_ms 毫秒换算
        self.assertEqual(f["preview"], "")
        self.assertEqual(f["mtype"], "music")

    def test_v1_fields_fall_back_from_new_defaults(self):
        """回归：1.0 字段（albumname/artistsname/ar_name/al_name/pic）仍可兜底。"""
        v1 = {
            "albumname": "来自天堂的魔鬼",
            "artistsname": "Zxxy",
            "ar_name": "Zxxy",
            "al_name": "同名专辑",
            "pic": "https://p3.douyinpic.com/x.jpeg",
            "url": "https://x/a.m4a",
        }
        # 模拟已升级为新默认路径的配置（旧后端字段全部 miss）
        f = self._fields(v1, title_p="name", author_p="singer", album_p="album",
                         level_p="label")
        self.assertEqual(f["title"], "来自天堂的魔鬼")
        self.assertEqual(f["author"], "Zxxy")
        self.assertEqual(f["album"], "同名专辑")
        self.assertEqual(f["cover"], "https://p3.douyinpic.com/x.jpeg")

    def test_v2_card_lines(self):
        card = bk._format_music_card("qishui", self._fields(self.V2))
        self.assertNotIn("类型：", card)  # music 类型不显示类型行
        self.assertIn("时长：04:12", card)
        self.assertNotIn("试听片段", card)
        self.assertIn("音质：M4A · 极高音质 · 258kbps", card)
        self.assertIn("音质档：极高音质 1.84MB / 无损音质（无权限）", card)
        self.assertIn("歌词预览：\n示例第一行\n示例第二行", card)

    def test_v2_preview_flag(self):
        data = dict(self.V2, preview=True, duration_ms=30000)
        card = bk._format_music_card("qishui", self._fields(data))
        self.assertIn("时长：04:12（试听片段）", card)

    def test_v2_mv_type_line(self):
        data = dict(self.V2, type="mv")
        card = bk._format_music_card("qishui", self._fields(data))
        self.assertIn("类型：MV", card)

    def test_quality_type_label_fallback(self):
        """label/level 均缺失时按 quality_type 统一编号映射中文标签。"""
        data = {
            "name": "歌", "quality_type": 4,
            "url": "https://x/a.flac",
            "qualities": [],
        }
        f = self._fields(data)
        self.assertEqual(f["level"], "无损音质")

    def test_qualities_summary_capped(self):
        qs = [
            {"label": f"档位{i}", "quality_type": i, "url": f"https://x/{i}"}
            for i in range(7)
        ]
        text = bk._summarize_music_qualities(qs)
        self.assertTrue(text.endswith("等共7档"))
        self.assertEqual(text.count(" / "), 4)  # 最多 5 项


class PlayerCardRenderTests(unittest.TestCase):
    """QQ 官方机器人播放器卡片 Markdown 渲染（v1.7.8）。"""

    def test_video_card_header_image_quote(self):
        text = (
            "标题：测试视频\n类型：视频\n作者：BugPk\n时长：01:20\n"
            "大小：1.74MB\n画质：original\n视频直链：https://x.com/v.mp4\n"
            "原始链接：https://v.douyin.com/abc/"
        )
        out = bk._render_player_card_md(
            text, cover_url="https://x.com/cover.jpg", buttons_enabled=True,
        )
        self.assertTrue(out.startswith("## 🎬 测试视频"))
        self.assertIn("![cover #560px #315px](https://x.com/cover.jpg)", out)
        self.assertIn("> 时长 01:20 · 大小 1.74MB · 画质 original", out)
        self.assertIn("**作者**：BugPk", out)
        # 按钮开启时长链接行从正文移除，结尾为分割线
        self.assertNotIn("https://x.com/v.mp4", out)
        self.assertNotIn("https://v.douyin.com/abc/", out)
        self.assertTrue(out.endswith("***"))

    def test_music_card_square_image_keep_urls_without_buttons(self):
        text = (
            "平台：网易云\n曲名：歌\n艺人：A\n时长：04:12（试听片段）\n"
            "音频：https://m.example.com/a.mp3"
        )
        out = bk._render_player_card_md(text, cover_url="https://x.com/c.jpg")
        self.assertIn("## 🎵 歌", out)
        self.assertIn("![cover #300px #300px](https://x.com/c.jpg)", out)
        self.assertIn("**艺人**：A", out)
        self.assertIn("**音频**：https://m.example.com/a.mp3", out)

    def test_no_fields_falls_back_to_markdownize(self):
        text = "自由文本没有字段行"
        self.assertEqual(
            bk._render_player_card_md(text), bk._markdownize_card_text(text)
        )

    def test_long_title_truncated(self):
        text = "标题：" + "长" * 40
        out = bk._render_player_card_md(text)
        header = out.splitlines()[0]
        self.assertTrue(header.startswith("## 🎬 "))
        self.assertTrue(header.endswith("…"))
        # 头部截断到 32 字（加 emoji 前缀后整行不超过 40 字符）
        self.assertLessEqual(len(header), 40)


class PlayerCardSendTests(unittest.IsolatedAsyncioTestCase):
    """播放器卡片发送：封面内嵌跳过重复图片、卡片失败兜底纯文本。"""

    def _plugin(self, message_cfg=None):
        cfg = {"opening_enable": False, "short_video_text_metadata": True}
        cfg.update(message_cfg or {})
        return bk.BKToolsPlugin(
            object(),
            {"message": cfg, "video_threshold": {"threshold_enabled": False}},
        )

    async def test_embedded_cover_image_not_resent(self):
        """封面已内嵌卡片时，不再单独发送同 URL 的 Image 组件。"""
        plugin = self._plugin()

        class _Plain:
            """独立 Plain 类（桩的 Plain/Image 同类，无法区分组件类型）。"""

            def __init__(self, text=""):
                self.text = text

        card = _Plain("标题：测试视频")
        img = bk.Image("https://x.com/1.jpg")
        img.url = "https://x.com/1.jpg"
        nodes = [
            bk.Node(name="B", uin=10000, content=[card]),
            bk.Node(name="B", uin=10000, content=[img]),
        ]
        event = _MDEvent("qq_official")
        with patch.object(bk, "Plain", _Plain):
            sent_any = await plugin._send_forward_as_plain(
                event, nodes, ("t", 0), "测试降级"
            )
        self.assertTrue(sent_any)
        # 仅播放器卡片一条消息；Image 未重复发送
        self.assertEqual(len(event.sent), 1)
        self.assertIn("![cover #560px #315px](https://x.com/1.jpg)", event.sent[0].text)

    async def test_card_failure_falls_back_to_plain_text(self):
        """播放器卡片发送失败（如图片转存失败）时兜底纯文本重发。"""
        plugin = self._plugin()
        card = bk.Plain()
        card.text = "标题：测试视频"
        nodes = [bk.Node(name="B", uin=10000, content=[card])]
        event = _MDEvent("qq_official")

        async def send(result):
            if result.markdown is True:  # Markdown 卡片模拟发送失败
                raise RuntimeError("media transfer failed")
            event.sent.append(result)

        event.send = send
        sent_any = await plugin._send_forward_as_plain(
            event, nodes, ("t", 0), "测试降级"
        )
        self.assertTrue(sent_any)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.sent[0].markdown, None)  # 纯文本兜底
        self.assertIn("标题：测试视频", event.sent[0].text)


if __name__ == "__main__":
    unittest.main()

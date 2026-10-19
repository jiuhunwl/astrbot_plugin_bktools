"""引用消息链接被客户端截断导致解析残链的回归测试。

背景：QQ 引用长文本会在约第 79 字符处截断并追加 "..."，插件曾把
"https://v.douyin.com/WM5..." 去掉尾部省略号后当作完整链接送接口，
导致传参 {"url":["https://v.douyin.com/WM5"]} 解析失败。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tests"))

from test_json_batch_output import bk  # noqa: E402


class ReplySeg:
    """模拟 AstrBot 的 Reply 组件（adapter 会挂上被引用消息的完整原文）。"""

    def __init__(self, message_str: str = "", chain=None):
        self.sender_id = "10086"
        self.sender_nickname = "某人"
        self.message_str = message_str
        self.text = message_str
        self.chain = list(chain or [])


class PlainSeg:
    def __init__(self, text: str):
        self.text = text


class _Obj:
    def __init__(self, message):
        self.message = message


class _Event:
    def __init__(self, message_str: str, message):
        self.message_str = message_str
        self.message_obj = _Obj(message)


class _BrokenObj:
    @property
    def message(self):  # pragma: no cover - 触发异常用
        raise RuntimeError("boom")


SHARE_TEXT = (
    "7.46 复制打开抖音，看看【你有點可愛的图文作品】“让整个京师都知道我”"
    "# 嫁金钗# 田曦薇 # 闫... https://v.douyin.com/WM5qZXqGB04/ 11/03 ygo:/ w@f.oD :4pm"
)
FULL_LINK = "https://v.douyin.com/WM5qZXqGB04/"
# QQ 引用截断：第 79 字符处截断后追加省略号（与用户实测一致）
TRUNCATED_TEXT = SHARE_TEXT[:79] + "..."


class ExtractCandidateTests(unittest.TestCase):
    def test_direct_share_text_still_works(self):
        """回归：直接发分享文案必须照常拿到完整链接。"""
        urls, ok = bk._select_parse_urls(SHARE_TEXT, [])
        self.assertEqual(urls, [FULL_LINK])
        self.assertTrue(ok)

    def test_trailing_ellipsis_marked_as_truncated(self):
        candidates = bk._extract_url_candidates(
            "看看 https://v.douyin.com/WM5qZXqGB04/..."
        )
        self.assertEqual(candidates, [("https://v.douyin.com/WM5qZXqGB04/", True)])

    def test_single_ellipsis_char_also_detected(self):
        candidates = bk._extract_url_candidates("https://v.douyin.com/WM5…")
        self.assertEqual(candidates, [("https://v.douyin.com/WM5", True)])

    def test_trailing_chinese_punct_not_marked_truncated(self):
        """回归：分享文案结尾的中文句号只是标点，不算截断。"""
        candidates = bk._extract_url_candidates("https://v.douyin.com/XYZabcd1/。")
        self.assertEqual(candidates, [("https://v.douyin.com/XYZabcd1/", False)])

    def test_extract_urls_keeps_backward_compatible_shape(self):
        self.assertEqual(bk._extract_urls("a https://x.com/1。 b"), ["https://x.com/1"])


class SelectParseUrlTests(unittest.TestCase):
    def test_quoted_full_text_wins_over_truncated_message_str(self):
        """核心修复：引用消息里 message_str 只有残链，必须用 Reply 里的完整原文。"""
        ev = _Event(TRUNCATED_TEXT, [ReplySeg(SHARE_TEXT)])
        urls, ok = bk._select_parse_urls(
            ev.message_str, bk._quoted_message_texts(ev)
        )
        self.assertEqual(urls[0], FULL_LINK)
        self.assertTrue(ok)
        self.assertNotIn("https://v.douyin.com/WM5", urls)

    def test_only_truncated_link_marks_incomplete(self):
        """引用原文拿不到时，只剩残链 → 标记不完整，让上层给提示而不是白请求。"""
        urls, ok = bk._select_parse_urls(TRUNCATED_TEXT, [])
        self.assertFalse(ok)
        self.assertEqual(urls, ["https://v.douyin.com/WM5"])

    def test_own_complete_link_has_priority_over_quote(self):
        ev = _Event("看这个 https://v.douyin.com/ABCdef12/", [ReplySeg(SHARE_TEXT)])
        urls, ok = bk._select_parse_urls(
            ev.message_str, bk._quoted_message_texts(ev)
        )
        self.assertTrue(ok)
        self.assertEqual(urls[0], "https://v.douyin.com/ABCdef12/")

    def test_dedup_between_message_and_quote(self):
        ev = _Event(SHARE_TEXT, [ReplySeg(SHARE_TEXT)])
        urls, ok = bk._select_parse_urls(
            ev.message_str, bk._quoted_message_texts(ev)
        )
        self.assertEqual(urls, [FULL_LINK])
        self.assertTrue(ok)

    def test_plain_text_without_url(self):
        self.assertEqual(bk._select_parse_urls("哈哈哈", []), ([], False))
        self.assertEqual(bk._select_parse_urls("", []), ([], False))


class QuotedMessageTextTests(unittest.TestCase):
    def test_reads_reply_message_str(self):
        ev = _Event("", [ReplySeg(SHARE_TEXT)])
        self.assertEqual(bk._quoted_message_texts(ev), [SHARE_TEXT])

    def test_reads_plain_segments_inside_reply_chain(self):
        ev = _Event("", [ReplySeg("", chain=[PlainSeg(SHARE_TEXT)])])
        self.assertEqual(bk._quoted_message_texts(ev), [SHARE_TEXT])

    def test_non_reply_components_ignored(self):
        """普通 Plain / 图片等组件不能被误当成引用（避免把聊天内容重复解析）。"""
        ev = _Event("你好", [PlainSeg("这不是引用"), object()])
        self.assertEqual(bk._quoted_message_texts(ev), [])

    def test_reply_without_text_ignored(self):
        ev = _Event("", [ReplySeg("")])
        self.assertEqual(bk._quoted_message_texts(ev), [])

    def test_dedup_and_order(self):
        ev = _Event("", [ReplySeg("AAA"), ReplySeg("AAA"), ReplySeg("BBB")])
        self.assertEqual(bk._quoted_message_texts(ev), ["AAA", "BBB"])

    def test_broken_message_obj_does_not_raise(self):
        ev = _Event("", None)
        ev.message_obj = _BrokenObj()
        self.assertEqual(bk._quoted_message_texts(ev), [])

    def test_missing_message_obj(self):
        ev = _Event("", None)
        del ev.message_obj
        self.assertEqual(bk._quoted_message_texts(ev), [])


if __name__ == "__main__":
    unittest.main()

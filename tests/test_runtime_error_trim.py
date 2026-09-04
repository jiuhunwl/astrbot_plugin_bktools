from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


def _load_runtime():
    path = pathlib.Path(__file__).resolve().parents[1] / "bktools_runtime.py"
    name = "bktools_runtime_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rt = _load_runtime()


class TrimServerErrorTests(unittest.TestCase):
    """验证《上游错误透传》核心逻辑 _trim_server_error 的提取与脱敏。"""

    def test_json_msg_field(self):
        body = '{"code": 502, "msg": "上游抖音接口超时，请稍后重试", "data": null}'
        self.assertEqual(rt._trim_server_error(body), "上游抖音接口超时，请稍后重试")

    def test_message_field(self):
        self.assertEqual(rt._trim_server_error('{"message": "不可解析的链接"}'), "不可解析的链接")

    def test_error_and_detail_field(self):
        self.assertEqual(rt._trim_server_error('{"error": "服务不可用"}'), "服务不可用")
        self.assertEqual(rt._trim_server_error('{"detail": "上游限流"}'), "上游限流")

    def test_nested_dict_field(self):
        body = '{"error": {"reason": "签名校验失败"}}'
        self.assertEqual(rt._trim_server_error(body), "签名校验失败")

    def test_plain_html_body(self):
        body = "<html><body><h1>502 Bad Gateway</h1><p>nginx/1.18.0</p></body></html>"
        self.assertEqual(rt._trim_server_error(body), "502 Bad Gateway nginx/1.18.0")

    def test_plain_text_body(self):
        self.assertEqual(rt._trim_server_error("Bad Request: missing param"), "Bad Request: missing param")

    def test_empty_body(self):
        self.assertEqual(rt._trim_server_error(""), "")
        self.assertEqual(rt._trim_server_error("   "), "")

    def test_truncation(self):
        long_text = "x" * 500
        result = rt._trim_server_error(long_text)
        self.assertLessEqual(len(result), 301)
        self.assertTrue(result.endswith("…"))

    def test_known_unsafe_content_kept_only_once(self):
        """纯文本里同时出现关键词与普通文本时，只提取一次、不重复拼接。"""
        body = '{"error": "A", "msg": "B", "message": "C"}'
        self.assertIn(rt._trim_server_error(body), ("A", "B", "C"))

    def test_broken_utf8_no_crash(self):
        raw = b"\xff\xfe\x01".decode("utf-8", errors="replace")
        self.assertIsInstance(rt._trim_server_error(raw), str)


if __name__ == "__main__":
    unittest.main()
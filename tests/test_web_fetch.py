# -*- coding: utf-8 -*-
"""网页抓取（web_fetch）离线测试。

覆盖：HTML 剥离纯函数（script/style 跳过/块级转换行/空白压缩/标题）/
scheme 白名单 / enabled=false 不注册 / 聊天隔离（allow_from_chat=false 拦
mention/scheduled，放行 manual）/ 本地 http.server 集成（真实抓一次）/
正文截断 / max_bytes 截断 / 审计落盘 / 重定向上限。
不调 LLM；集成用例只连 127.0.0.1 自建临时服务器。
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config_loader, run_context  # noqa: E402
from core import orchestrator  # noqa: E402
from tools.builtin import web_fetch  # noqa: E402

PAGE_HTML = """<!DOCTYPE html>
<html><head><title>测试页面·切石机</title>
<style>.x{color:red}</style>
<script>var evil = "should-not-appear";</script>
</head>
<body>
<h1>切石机</h1>
<p>切石机可以把石头加工成  台阶、楼梯与墙。</p>
<ul><li>圆石</li><li>深板岩圆石</li></ul>
<div>尾部信息</div>
</body></html>
"""


def cfg_with(web=None, **other):
    base = {"permissions": {"file_mode": "full",
                            "roots": {"agent": "."},
                            "default_root": "agent"}}
    if web is not None:
        base["permissions"]["web"] = web
    base.update(other)
    return base


class _Handler(http.server.BaseHTTPRequestHandler):
    """测试服务器：/ 返回 HTML，/json 返回 JSON，/loop 自指重定向，/big 返回大页。"""

    hits = 0   # do_GET 计数（缓存测试用来证明命中没打到服务器）

    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):
        _Handler.hits += 1
        if self.path.startswith("/loop"):
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
            return
        if self.path.startswith("/json"):
            body = json.dumps({"mod": "create", "item": "experience_block"},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        elif self.path.startswith("/big"):
            body = ("<p>" + "长" * 3000 + "</p>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class WebDisabledTest(unittest.TestCase):
    def test_default_enabled(self):
        self.assertTrue(web_fetch.enabled({}))
        self.assertTrue(web_fetch.enabled(cfg_with()))

    def test_explicit_off_no_tools(self):
        self.assertFalse(web_fetch.enabled(cfg_with({"enabled": False})))
        self.assertEqual(web_fetch.make_web_tools(cfg_with({"enabled": False})), [])

    def test_orchestrator_skips_when_off(self):
        reg = orchestrator.build_registry(cfg_with({"enabled": False}))
        self.assertNotIn("web_fetch", reg.names())

    def test_orchestrator_registers_when_on(self):
        reg = orchestrator.build_registry(cfg_with({"enabled": True}))
        self.assertIn("web_fetch", reg.names())

    def test_execute_refuses_when_off(self):
        r = web_fetch.execute(cfg_with({"enabled": False}), "http://127.0.0.1:1/")
        self.assertFalse(r["ok"])
        self.assertIn("未启用", r["reason"])
        self.assertEqual(r["action"], "web_fetch")

    def test_manifest_shape(self):
        (m, fn), = web_fetch.make_web_tools(cfg_with({"enabled": True}))
        self.assertEqual(m["name"], "web_fetch")
        self.assertEqual(m["parameters"]["required"], ["url"])
        self.assertIn("search.mcmod.cn", m["description"])
        self.assertIn("不可信输入", m["description"])


class ExtractTextTest(unittest.TestCase):
    def test_strip_tags_and_skip_script_style(self):
        text, title = web_fetch.extract_text(PAGE_HTML)
        self.assertIn("切石机", text)
        self.assertIn("台阶", text)
        self.assertNotIn("should-not-appear", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("<", text)
        self.assertIn("测试页面", title)

    def test_block_tags_become_lines(self):
        # 块级标签之间保留一个空行（段落分隔），不允许三连空行
        text, _ = web_fetch.extract_text("<div>a</div><div>b</div>")
        self.assertEqual(text.split("\n"), ["a", "", "b"])

    def test_whitespace_collapsed(self):
        text, _ = web_fetch.extract_text("<p>  多个   空白\n\n\t压缩   </p>")
        self.assertEqual(text, "多个 空白 压缩")

    def test_blank_lines_not_stacked(self):
        text, _ = web_fetch.extract_text("<p>a</p><p></p><p></p><p>b</p>")
        self.assertNotIn("\n\n\n", text)
        self.assertIn("a", text)
        self.assertIn("b", text)

    def test_nested_skip_tags(self):
        text, _ = web_fetch.extract_text("<div>前<script>a<b>c</b></script>后</div>")
        self.assertEqual(text, "前 后".replace(" ", ""))

    def test_plain_json_lossless(self):
        body = '{"a": 1, "b": "中文"}'
        text, _ = web_fetch.extract_text(body)
        self.assertIn("中文", text)

    def test_empty_and_none(self):
        self.assertEqual(web_fetch.extract_text("")[0], "")
        self.assertEqual(web_fetch.extract_text(None)[0], "")


class UrlCheckTest(unittest.TestCase):
    def test_file_scheme_rejected(self):
        u, err = web_fetch._check_url("file:///etc/passwd")
        self.assertIsNone(u)
        self.assertIn("http/https", err)

    def test_ftp_scheme_rejected(self):
        u, err = web_fetch._check_url("ftp://example.com/x")
        self.assertIsNone(u)

    def test_no_host_rejected(self):
        u, err = web_fetch._check_url("http://")
        self.assertIsNone(u)
        self.assertIn("主机名", err)

    def test_ok(self):
        u, err = web_fetch._check_url("https://search.mcmod.cn/s?key=切石机")
        self.assertIsNone(err)
        self.assertEqual(u.scheme, "https")


class WebChatIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(config_loader, "AGENT_ROOT", self.tmp.name)
        self._p.start()

    def tearDown(self):
        run_context.set_trigger("manual")
        self._p.stop()
        self.tmp.cleanup()

    def test_mention_blocked_when_disallow(self):
        c = cfg_with({"enabled": True, "allow_from_chat": False})
        run_context.set_trigger("mention")
        r = web_fetch.execute(c, "http://127.0.0.1:1/")
        self.assertFalse(r["ok"])
        self.assertIn("禁止抓网页", r["reason"])

    def test_scheduled_blocked_when_disallow(self):
        c = cfg_with({"enabled": True, "allow_from_chat": False})
        run_context.set_trigger("scheduled")
        self.assertFalse(web_fetch.execute(c, "http://127.0.0.1:1/")["ok"])

    def test_default_allows_chat(self):
        """默认 allow_from_chat=true：聊天触发也放行（只读抓取，危险级低）。"""
        c = cfg_with({"enabled": True})
        run_context.set_trigger("mention")
        # 不断言抓取成功（连不上 1 端口），只断言不被聊天隔离拦下
        r = web_fetch.execute(c, "http://127.0.0.1:1/", timeout=2)
        self.assertNotIn("禁止抓网页", r["reason"])


class WebLiveLocalServerTest(unittest.TestCase):
    """起一个 127.0.0.1 临时 http.server 做真实抓取。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(config_loader, "AGENT_ROOT", self.tmp.name)
        self._p.start()
        run_context.set_trigger("manual")
        self.cfg = cfg_with({"enabled": True, "timeout_sec": 10})

    def tearDown(self):
        run_context.set_trigger("manual")
        self._p.stop()
        self.tmp.cleanup()

    def test_fetch_html(self):
        r = web_fetch.execute(self.cfg, "http://127.0.0.1:%d/" % self.port)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], 200)
        self.assertIn("切石机", r["text"])
        self.assertNotIn("should-not-appear", r["text"])
        self.assertIn("测试页面", r["title"])
        self.assertEqual(r["trigger"], "manual")

    def test_fetch_json_lossless(self):
        r = web_fetch.execute(self.cfg, "http://127.0.0.1:%d/json" % self.port)
        self.assertTrue(r["ok"], r)
        self.assertIn("experience_block", r["text"])

    def test_text_truncated(self):
        r = web_fetch.execute(self.cfg, "http://127.0.0.1:%d/" % self.port,
                              max_chars=20)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["truncated"])
        self.assertLessEqual(len(r["text"]), 20)

    def test_over_bytes_truncated(self):
        c = cfg_with({"enabled": True, "max_bytes": 500})
        r = web_fetch.execute(c, "http://127.0.0.1:%d/big" % self.port)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["over_bytes"])
        self.assertLessEqual(r["bytes"], 500)

    def test_redirect_loop_fails(self):
        r = web_fetch.execute(self.cfg, "http://127.0.0.1:%d/loop" % self.port)
        self.assertFalse(r["ok"])
        self.assertIn("重定向", r["reason"])

    def test_connection_refused_fails_soft(self):
        r = web_fetch.execute(self.cfg, "http://127.0.0.1:1/", timeout=2)
        self.assertFalse(r["ok"])
        self.assertEqual(r["action"], "web_fetch")

    def test_audit_written(self):
        web_fetch.execute(self.cfg, "http://127.0.0.1:%d/" % self.port)
        p = os.path.join(self.tmp.name, "logs", "web_fetch.log")
        self.assertTrue(os.path.exists(p))
        with open(p, encoding="utf-8") as f:
            line = json.loads(f.read().strip().splitlines()[-1])
        self.assertTrue(line["ok"])
        self.assertEqual(line["status"], 200)
        self.assertEqual(line["trigger"], "manual")

    def test_audit_written_on_chat_block(self):
        c = cfg_with({"enabled": True, "allow_from_chat": False})
        run_context.set_trigger("mention")
        web_fetch.execute(c, "http://127.0.0.1:%d/" % self.port)
        p = os.path.join(self.tmp.name, "logs", "web_fetch.log")
        with open(p, encoding="utf-8") as f:
            line = json.loads(f.read().strip().splitlines()[-1])
        self.assertFalse(line["ok"])
        self.assertEqual(line["reason"], "chat-isolation")


class WebCacheTest(unittest.TestCase):
    """同 URL 缓存：TTL 内复用（不打到服务器）、过期重抓、截断语义、失败不缓存。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(config_loader, "AGENT_ROOT", self.tmp.name)
        self._p.start()
        run_context.set_trigger("manual")
        web_fetch._CACHE.clear()
        _Handler.hits = 0
        self.url = "http://127.0.0.1:%d/" % self.port
        self.cfg = cfg_with({"enabled": True, "timeout_sec": 10})

    def tearDown(self):
        run_context.set_trigger("manual")
        web_fetch._CACHE.clear()
        self._p.stop()
        self.tmp.cleanup()

    def test_second_fetch_is_cached(self):
        r1 = web_fetch.execute(self.cfg, self.url)
        self.assertTrue(r1["ok"], r1)
        self.assertNotIn("cached", r1)
        r2 = web_fetch.execute(self.cfg, self.url)
        self.assertTrue(r2["ok"], r2)
        self.assertTrue(r2["cached"])
        self.assertEqual(r2["text"], r1["text"])
        self.assertEqual(r2["title"], r1["title"])
        self.assertIn("不要重复抓", r2["note"])
        self.assertEqual(_Handler.hits, 1, "缓存命中不应再打到服务器")

    def test_cache_hit_smaller_cap_slices(self):
        web_fetch.execute(self.cfg, self.url)                    # 默认 cap 6000
        r = web_fetch.execute(self.cfg, self.url, max_chars=30)
        self.assertTrue(r["cached"])
        self.assertTrue(r["truncated"])
        self.assertLessEqual(len(r["text"]), 30)
        self.assertEqual(_Handler.hits, 1)

    def test_cache_hit_bigger_cap_when_full_text(self):
        """页面未被截断（全文比 cap 小）→ 任意 cap 都可精确复用。"""
        web_fetch.execute(self.cfg, self.url, max_chars=5000)    # 页面约 40 字，不截断
        r = web_fetch.execute(self.cfg, self.url, max_chars=9000)
        self.assertTrue(r["cached"])
        self.assertFalse(r["truncated"])
        self.assertEqual(_Handler.hits, 1)

    def test_ttl_zero_disables_cache(self):
        c = cfg_with({"enabled": True, "cache_ttl": 0})
        web_fetch.execute(c, self.url)
        r = web_fetch.execute(c, self.url)
        self.assertNotIn("cached", r)
        self.assertEqual(_Handler.hits, 2)

    def test_expired_cache_refetches(self):
        c = cfg_with({"enabled": True, "cache_ttl": 1})
        web_fetch.execute(c, self.url)
        # 把缓存时间戳往前拨 10s，模拟过期（比 mock time 更干净）
        with web_fetch._CACHE_LOCK:
            ts, res = web_fetch._CACHE[self.url]
            web_fetch._CACHE[self.url] = (ts - 10, res)
        r = web_fetch.execute(c, self.url)
        self.assertNotIn("cached", r)
        self.assertEqual(_Handler.hits, 2)

    def test_failure_not_cached(self):
        """连不上的 URL 连抓两次都应真实尝试（失败不进缓存）。"""
        c = cfg_with({"enabled": True})
        bad = "http://127.0.0.1:1/"
        r1 = web_fetch.execute(c, bad, timeout=2)
        r2 = web_fetch.execute(c, bad, timeout=2)
        self.assertFalse(r1["ok"])
        self.assertFalse(r2["ok"])
        self.assertNotIn("cached", r2)
        self.assertEqual(web_fetch._CACHE, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)

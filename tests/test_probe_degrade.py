# -*- coding: utf-8 -*-
"""游戏探活降级测试：缓存/离线 fast-fail/恢复探活/daemon 注入降级提示。"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.builtin import mc_bridge


def make_cfg(**mc):
    return {"mc_bridge": dict({"base_url": "http://127.0.0.1:8780"}, **mc)}


class FakeResp:
    def __init__(self, status=200, text='{"status":"ok"}'):
        self.status_code = status
        self.text = text

    def json(self):
        return {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise mc_bridge.requests.HTTPError(str(self.status_code))


class TestGameOnline(unittest.TestCase):
    def setUp(self):
        mc_bridge._probe_cache.update(ok=None, note="", ts=0.0)

    def test_online_ok(self):
        with mock.patch.object(mc_bridge.requests, "get", return_value=FakeResp(200)):
            ok, note = mc_bridge.game_online(make_cfg(), force=True)
        self.assertTrue(ok)
        self.assertIn("在线", note)

    def test_offline_connection_error(self):
        with mock.patch.object(mc_bridge.requests, "get",
                               side_effect=mc_bridge.requests.ConnectionError("refused")):
            ok, note = mc_bridge.game_online(make_cfg(), force=True)
        self.assertFalse(ok)
        self.assertIn("离线", note)

    def test_cache_within_ttl(self):
        cfg = make_cfg(probe_ttl=300)
        with mock.patch.object(mc_bridge.requests, "get",
                               side_effect=mc_bridge.requests.ConnectionError("x")) as g:
            mc_bridge.game_online(cfg, force=True)
            mc_bridge.game_online(cfg)          # 命中缓存
            mc_bridge.game_online(cfg)          # 再命中
        self.assertEqual(g.call_count, 1)       # 只探活一次

    def test_cache_expires(self):
        cfg = make_cfg(probe_ttl=0)             # ttl=0 → 每次都真探
        with mock.patch.object(mc_bridge.requests, "get",
                               side_effect=mc_bridge.requests.ConnectionError("x")) as g:
            mc_bridge.game_online(cfg)
            mc_bridge.game_online(cfg)
        self.assertEqual(g.call_count, 2)


class TestMcToolDegrade(unittest.TestCase):
    def setUp(self):
        mc_bridge._probe_cache.update(ok=False, note="游戏离线（ConnectionError）", ts=9e12)

    def test_state_fast_fail(self):
        tools = mc_bridge.make_mc_tools(make_cfg())
        fns = {m["name"]: fn for m, fn in tools}
        r = fns["mc_state"]()
        self.assertTrue(r.get("offline"))
        self.assertFalse(r.get("sent"))
        self.assertIn("游戏离线", r["error"])

    def test_act_fast_fail_no_http(self):
        """离线时 _act 不应发出任何 HTTP 请求（fast-fail，防超时空等）。"""
        tools = mc_bridge.make_mc_tools(make_cfg())
        fns = {m["name"]: fn for m, fn in tools}
        with mock.patch.object(mc_bridge.requests, "post") as post, \
                mock.patch.object(mc_bridge.requests, "get") as get:
            r = fns["mc_act"](type="ping")
        post.assert_not_called()
        get.assert_not_called()
        self.assertTrue(r.get("offline"))

    def test_batch_and_chat_fast_fail(self):
        tools = mc_bridge.make_mc_tools(make_cfg())
        fns = {m["name"]: fn for m, fn in tools}
        self.assertTrue(fns["mc_batch"](actions=[{"type": "ping"}]).get("offline"))
        self.assertTrue(fns["mc_chat"]().get("offline"))

    def test_recover_after_online(self):
        """恢复在线后 mc_* 正常工作（探活自动刷新）。"""
        mc_bridge._probe_cache.update(ok=None, note="", ts=0.0)
        cfg = make_cfg()
        with mock.patch.object(mc_bridge.requests, "get",
                               return_value=FakeResp(200, '{"last_action":"pong"}')):
            tools = mc_bridge.make_mc_tools(cfg)
            fns = {m["name"]: fn for m, fn in tools}
            r = fns["mc_state"]()
        self.assertNotIn("offline", r)          # 在线时走正常路径


def _stub_daemon(cfg):
    """__new__ 绕过构造器的最小 daemon 桩（只够跑 _run_worker）。"""
    from core import daemon as dm
    d = dm.Daemon.__new__(dm.Daemon)
    d.cfg = cfg
    d.max_rounds = 3
    d.verbose = False
    d.stop_event = None
    d.lock = threading.Lock()
    d.backlog = []
    d.backlog_max = 50
    d.running = True
    d.current_task = "x"
    d.current_reason = "y"
    d.last_run_end = 0.0
    d.log = lambda *a, **k: None
    d.base = "http://127.0.0.1:8780"      # 世界探活用（_run_worker 会先查 /state 的 loaded）
    d._say = lambda *a, **k: None         # 桩里不真发游戏聊天
    d._say = lambda *a, **k: None
    d._save_state = lambda: None
    d.reviewer = dm.Reviewer.__new__(dm.Reviewer)
    d.reviewer.check = lambda *a: None
    return d


class _FakeResp:
    """桩：模拟 /state 返回（daemon 会先探"是否进了世界"，没进世界就跳过运行）。"""

    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def _in_world():
    """patch：客户端在世界里（loaded=true）→ 不触发"未进世界跳过"。"""
    from core import daemon as dm
    return mock.patch.object(dm.requests, "get", return_value=_FakeResp({"loaded": True}))


class TestDaemonDegrade(unittest.TestCase):
    def test_worker_injects_note_when_offline(self):
        """daemon 唤醒运行在离线时给任务注入系统提示。"""
        from core import daemon as dm
        d = _stub_daemon(make_cfg())
        seen = {}

        def fake_run_task(cfg, task, **kw):
            seen["task"] = task
            return "done", {}

        # game_online 在 _run_worker 内部按源模块 import → 必须 patch 源模块
        with _in_world(), \
                mock.patch.object(mc_bridge, "game_online",
                                  return_value=(False, "游戏离线（ConnectionError）")), \
                mock.patch.object(dm, "run_task", side_effect=fake_run_task), \
                mock.patch.object(dm.Reviewer, "reset", lambda self: None):
            d._run_worker("scheduled", "整理记忆", None)
        self.assertIn("游戏离线", seen["task"])
        self.assertIn("整理记忆", seen["task"])

    def test_worker_no_injection_when_online(self):
        from core import daemon as dm
        d = _stub_daemon(make_cfg())
        seen = {}

        def fake_run_task(cfg, task, **kw):
            seen["task"] = task
            return "done", {}

        with _in_world(), \
                mock.patch.object(mc_bridge, "game_online",
                                  return_value=(True, "游戏在线")), \
                mock.patch.object(dm, "run_task", side_effect=fake_run_task), \
                mock.patch.object(dm.Reviewer, "reset", lambda self: None):
            d._run_worker("scheduled", "整理记忆", None)
        self.assertNotIn("系统提示", seen["task"])

    def test_worker_skips_when_not_in_world(self):
        """客户端活着但没进世界（控制面在线、loaded=false）→ 跳过运行，不烧 token。"""
        from core import daemon as dm
        d = _stub_daemon(make_cfg())
        called = []

        def fake_run_task(cfg, task, **kw):
            called.append(task)
            return "done", {}

        with mock.patch.object(dm.requests, "get",
                               return_value=_FakeResp({"loaded": False, "note": "未进入世界"})), \
                mock.patch.object(mc_bridge, "game_online", return_value=(True, "游戏在线")), \
                mock.patch.object(dm, "run_task", side_effect=fake_run_task), \
                mock.patch.object(dm.Reviewer, "reset", lambda self: None):
            d._run_worker("scheduled", "整理记忆", None)
        self.assertEqual(called, [], "未进世界时不应调用 run_task（白烧 token）")


if __name__ == "__main__":
    unittest.main()

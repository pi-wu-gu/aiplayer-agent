# -*- coding: utf-8 -*-
"""daemon 机制离线测试（不调 LLM、不连桥接也能跑核心逻辑）。

覆盖：
1. 提及检测：他人@提及触发 / bot自身发言不触发 / 系统消息不触发 / 额外触发词
2. 唤醒门控：运行中不允许唤醒
3. 审查规则：同一操作连续失败→强制停止；正常记录→放行
4. Agent 钩子：round_hook 返回原因即强制停止思考；stop_event 轮边界终止
5. 积压管理：超上限丢最旧

运行: python tests/test_daemon.py
"""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agent as agent_mod  # noqa: E402
from core.agent import Agent  # noqa: E402
from core.daemon import Daemon, Reviewer, parse_speaker  # noqa: E402


def make_cfg():
    return {
        "defaults": {"provider": "deepseek", "model": "deepseek-flash"},
        "mc_bridge": {"base_url": "http://127.0.0.1:9999"},   # 测试不直连
        "daemon": {"bot_names": ["ExampleBot"], "mention_extra": ["小深"],
                   "review_every": 4, "backlog_max": 5, "review_llm": False},
        "cost_guard": {"enabled": False},
    }


class FakeLLM:
    """替代 llm_client.chat：按脚本返回 tool_calls，最后一次返回纯文本收尾。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, cfg, provider_name=None, model=None, messages=None,
                 tools=None, temperature=None, max_tokens=None, _retry=0):
        self.calls += 1
        item = self.script.pop(0) if self.script else {"content": "收尾"}
        if "tool_calls" in item:
            return {"content": item.get("content"), "tool_calls": item["tool_calls"],
                    "usage": {"total_tokens": 10}, "raw": {}}
        return {"content": item.get("content", "完成"), "tool_calls": None,
                "usage": {"total_tokens": 10}, "raw": {}}


class FakeRegistry:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def manifests(self):
        return []

    def execute(self, name, args):
        self.calls.append((name, args))
        if self.results:
            r = self.results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return "ok"


def make_agent(script=None, results=None, **kw):
    reg = FakeRegistry(results if results is not None else ["ok"])
    ag = Agent("test", "sys", reg, make_cfg(), max_rounds=kw.pop("max_rounds", 8),
               verbose=False, **kw)
    ag._fake_llm = FakeLLM(script or [])
    return ag


def patch_llm(agent):
    agent_mod.llm_client.chat = agent._fake_llm


class TestMention(unittest.TestCase):
    def setUp(self):
        self.d = Daemon.__new__(Daemon)     # 不跑 __init__（避免连桥接/落盘）
        self.d.bot_names = ["ExampleBot"]
        self.d.extra_mentions = ["小深"]

    def test_speaker_parse(self):
        self.assertEqual(parse_speaker("[world] <ExampleBot>: 你好"), "ExampleBot")
        self.assertEqual(parse_speaker("<Lyuan666> hi"), "Lyuan666")
        self.assertIsNone(parse_speaker("liyinyin取得了进度[精准对接]"))

    def test_other_mention_triggers(self):
        self.assertTrue(self.d.is_mention("[world] <Steve>: @ExampleBot 看这个"))
        self.assertTrue(self.d.is_mention("<Lyuan666> ExampleBot 过来"))
        self.assertTrue(self.d.is_mention("<a> 小深 在吗"))

    def test_own_message_never_triggers(self):
        self.assertFalse(self.d.is_mention("[world] <ExampleBot>: [自动] 测试开始"))
        self.assertFalse(self.d.is_mention("<ExampleBot> 我自己说话"))

    def test_plain_message_no_trigger(self):
        self.assertFalse(self.d.is_mention("[world] <ExampleBot>: 今天天气不错"))
        self.assertFalse(self.d.is_mention(""))


class TestWakeGate(unittest.TestCase):
    def setUp(self):
        self.d = Daemon.__new__(Daemon)
        self.d.running = False
        self.d.last_run_end = 0.0
        self.d.run_cooldown = 0.0

    def test_idle_allows(self):
        self.assertTrue(self.d._wake_allowed())

    def test_running_blocks(self):
        self.d.running = True
        self.assertFalse(self.d._wake_allowed())

    def test_cooldown_blocks(self):
        self.d.last_run_end = time.time()
        self.d.run_cooldown = 30
        self.assertFalse(self.d._wake_allowed())


class TestReviewerRules(unittest.TestCase):
    def _reviewer(self, every=4):
        d = Daemon.__new__(Daemon)
        d.review_every = every
        d.review_token_budget = 0
        d.review_llm = False
        d.cfg = make_cfg()
        d.stats = {"review_stops": 0}
        d.log = lambda *a, **k: None
        d.query_health = lambda: 20
        r = Reviewer.__new__(Reviewer)
        r.d = d
        r.cfg = d.cfg
        r.last_review_round = 0
        r.last_review_tokens = 0
        r.jsonl_path = os.devnull
        return r

    def _agent_with_results(self, entries):
        ag = make_agent()
        ag.recent_results.clear()
        for i, (tool, args, ok) in enumerate(entries):
            ag.recent_results.append({"round": i + 1, "tool": tool, "args": args,
                                      "ok": ok, "head": "x", "elapsed": 0.1})
        return ag

    def test_repeat_failure_stops(self):
        r = self._reviewer()
        ag = self._agent_with_results([("mc_act", '{"a":1}', False)] * 3)
        verdict = r.check(4, ag)
        self.assertIsNotNone(verdict)
        self.assertIn("连续3次失败", verdict)

    def test_all_fail_window_stops(self):
        r = self._reviewer()
        ag = self._agent_with_results([("t%d" % i, "{}", False) for i in range(6)])
        verdict = r.check(4, ag)
        self.assertIsNotNone(verdict)

    def test_healthy_passes(self):
        r = self._reviewer()
        ag = self._agent_with_results([("mc_act", '{"a":1}', True)] * 5)
        verdict = r.check(4, ag)
        self.assertIsNone(verdict)

    def test_low_health_stops(self):
        r = self._reviewer()
        r.d.query_health = lambda: 6
        ag = self._agent_with_results([("mc_state", "{}", True)])
        verdict = r.check(4, ag)
        self.assertIsNotNone(verdict)
        self.assertIn("避险红线", verdict)

    def test_not_due_skips(self):
        r = self._reviewer(every=32)
        ag = self._agent_with_results([("mc_act", "{}", False)] * 6)
        self.assertIsNone(r.check(10, ag))       # 10-0 < 32，不审

    def test_counter_resets_per_run(self):
        """修复回归：审查计数不得跨运行累计（否则第2次运行起永远不审查）。"""
        r = self._reviewer(every=32)
        ag = self._agent_with_results([("mc_state", "{}", True)])
        self.assertIsNone(r.check(32, ag))       # 第1次运行第32轮：审（通过）
        self.assertIsNone(r.check(40, ag))       # 同一次运行内未到间隔
        r.reset()                                # 第2次运行开始
        ag2 = self._agent_with_results([("mc_state", "{}", True)])
        self.assertIsNone(r.check(32, ag2))      # 第2次运行第32轮：必须再审一次
        self.assertEqual(r.last_review_round, 32)


class TestAgentForceStop(unittest.TestCase):
    def test_round_hook_stops(self):
        ag = make_agent(script=[{"tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "mc_state", "arguments": "{}"}}]}] * 8)
        patch_llm(ag)
        seen = []

        def hook(round_no, agent):
            seen.append(round_no)
            return "审查: 测试停止" if round_no >= 3 else None

        ag.round_hook = hook
        out = ag.step("测试任务")
        self.assertTrue(out.startswith("(审查强制停止"))
        self.assertIn("测试停止", out)
        self.assertEqual(seen, [1, 2, 3])        # 第3轮结束后即停

    def test_stop_event_terminates(self):
        import threading
        ag = make_agent(script=[{"tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "mc_state", "arguments": "{}"}}]}] * 8)
        patch_llm(ag)
        ev = threading.Event()
        ag.stop_event = ev

        def hook(round_no, agent):
            if round_no >= 2:
                ev.set()
            return None

        ag.round_hook = hook
        out = ag.step("测试任务")
        self.assertEqual(out, "(被外部强制停止思考)")
        self.assertEqual(ag.rounds_used, 3)      # 第2轮末置位，第3轮边界即终止（未思考）


class TestBacklogCap(unittest.TestCase):
    def test_cap_drops_oldest(self):
        d = Daemon.__new__(Daemon)
        d.backlog_max = 5
        d.backlog = []
        for i in range(8):
            d.backlog.append({"id": i, "t": 0, "text": "m%d" % i, "mention": False})
        d.backlog = d.backlog[-d.backlog_max:]
        self.assertEqual(len(d.backlog), 5)
        self.assertEqual(d.backlog[0]["id"], 3)   # 最旧3条被丢


class TestConsoleHotConfig(unittest.TestCase):
    """8770 控制台热改（v0.2.1.24）：
    1. 保存按钮 key 列表与 UI_HTML 输入框一致（回归：曾缺 review_token_budget 输入框
       → $(k).value 抛 TypeError → 整个"保存参数"失效）
    2. 嵌套 cfg 键热改（session_token_budget → cost_guard 段，clamp≥0）
    3. feed 思考事件透传 reasoning + 稳定 uid（UI 折叠状态跨刷新保持）
    4. R4 熔断阈值热改后即时生效（Reviewer 每轮实时读 cfg）
    """

    def _daemon(self, cfg=None):
        d = Daemon.__new__(Daemon)
        d.cfg = cfg if cfg is not None else {"cost_guard": {"session_token_budget": 1000}}
        d.overrides = {}
        return d

    def test_review_token_budget_is_hot_key(self):
        from core.daemon import CONFIG_KEYS
        self.assertIn("review_token_budget", CONFIG_KEYS)
        self.assertIs(CONFIG_KEYS["review_token_budget"], int)

    def test_save_keys_all_have_inputs(self):
        import re
        from core.daemon import UI_HTML
        ids = set(re.findall(r'id="([A-Za-z_0-9]+)"', UI_HTML))
        body = re.search(r"var body = \{\};\s*\[(.*?)\]", UI_HTML, re.S).group(1)
        keys = re.findall(r"'([a-z_]+)'", body)
        self.assertTrue(keys, "未解析到保存键列表")
        missing = [k for k in keys if k not in ids]
        self.assertEqual(missing, [], "保存列表缺少输入框: %s" % missing)

    def test_cfg_key_override_applies_and_clamps(self):
        from core.daemon import CONFIG_CFG_KEYS
        self.assertEqual(CONFIG_CFG_KEYS.get("session_token_budget"),
                         ("cost_guard", "session_token_budget"))
        d = self._daemon()
        changed = d._apply_overrides({"session_token_budget": "5000"}, persist=False)
        self.assertEqual(d.cfg["cost_guard"]["session_token_budget"], 5000)
        self.assertIn("session_token_budget", changed)
        self.assertEqual(d.overrides["session_token_budget"], 5000)
        # 负值 clamp 到 0（0=关闭熔断）
        d._apply_overrides({"session_token_budget": -5}, persist=False)
        self.assertEqual(d.cfg["cost_guard"]["session_token_budget"], 0)

    def test_config_get_includes_cfg_keys(self):
        d = self._daemon()
        out = d.config_get()
        self.assertEqual(out["session_token_budget"], 1000)
        self.assertIn("review_token_budget", out)

    def test_feed_think_event_uid_and_reasoning(self):
        """feed：llm_response 透传 reasoning_head + 稳定 uid。"""
        import json
        import shutil
        import tempfile
        from core import daemon as daemon_mod
        tmp = tempfile.mkdtemp()
        try:
            logdir = os.path.join(tmp, "logs")
            os.makedirs(logdir)
            rec = {"ts": "2026-10-17 12:00:00.000", "rel_s": 1.0,
                   "event": "llm_response", "round_no": 3,
                   "content_head": "思考内容" * 50, "reasoning_head": "推理链" * 50,
                   "tokens": 123, "tool_call_n": 1, "elapsed": 4.5}
            with open(os.path.join(logdir, "behavior_20261017.jsonl"),
                      "w", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            def _feed():
                old = daemon_mod.config_loader.AGENT_ROOT
                daemon_mod.config_loader.AGENT_ROOT = tmp
                try:
                    return self._daemon().feed(10)
                finally:
                    daemon_mod.config_loader.AGENT_ROOT = old

            evs = [e for e in _feed()["events"] if e["event"] == "llm_response"]
            self.assertEqual(len(evs), 1)
            self.assertTrue(evs[0]["uid"])
            self.assertIn("推理链", evs[0]["reasoning"])
            self.assertIn("思考内容", evs[0]["text"])
            # uid 稳定性：重取必须一致（否则 UI 折叠状态每 2s 刷新即丢）
            evs2 = [e for e in _feed()["events"] if e["event"] == "llm_response"]
            self.assertEqual(evs[0]["uid"], evs2[0]["uid"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reviewer_r4_hot_budget(self):
        """R4 熔断阈值热改后即时生效（Reviewer.check 每轮实时读 cfg）。"""
        d = Daemon.__new__(Daemon)
        d.review_every = 4
        d.review_token_budget = 0
        d.review_llm = False
        d.cfg = {"cost_guard": {"enabled": False, "session_token_budget": 100}}
        d.stats = {"review_stops": 0}
        d.log = lambda *a, **k: None
        d.query_health = lambda: 20
        r = Reviewer.__new__(Reviewer)
        r.d = d
        r.cfg = d.cfg
        r.last_review_round = 0
        r.last_review_tokens = 0
        r.jsonl_path = os.devnull
        ag = make_agent()
        ag.total_tokens = 500
        ag.sub_tokens = 0
        verdict = r.check(4, ag)
        self.assertIsNotNone(verdict)
        self.assertIn("超出会话预算", verdict)
        # 热改阈值 → 同一 agent 不再熔断
        d.cfg["cost_guard"]["session_token_budget"] = 100000
        r.last_review_round = 0
        self.assertIsNone(r.check(4, ag))


class TestWorkerStateReset(unittest.TestCase):
    """回归：_run_worker 任何出口都必须复位 running=False（2026-10-17 部署时实测 bug：
    "客户端未进入世界"的提前 return 绕过 finally → daemon 永久卡在 running=True，
    唤醒门控把之后所有唤醒全挡掉，只能重启恢复）。"""

    def _daemon(self):
        import threading
        d = Daemon.__new__(Daemon)
        d.cfg = make_cfg()
        d.max_rounds = 5
        d.base = "http://127.0.0.1:9999"
        d.backlog = []
        d.backlog_max = 10
        d.running = True
        d.current_task = "task"
        d.current_reason = "scheduled"
        d.stop_event = threading.Event()
        d.lock = threading.RLock()
        d.logs = []
        d.log = lambda t: d.logs.append(t)
        d._save_state = lambda: None
        d._say_offline_once = lambda: None
        d.reviewer = types.SimpleNamespace(reset=lambda: None)
        d.stats = {"runs": 0}
        return d

    def test_not_loaded_early_return_resets_state(self):
        from core import daemon as daemon_mod

        class FakeResp:
            def json(self):
                return {"loaded": False, "note": "未进入世界（主菜单）"}

        d = self._daemon()
        orig = daemon_mod.requests.get
        daemon_mod.requests.get = lambda url, timeout=None: FakeResp()
        try:
            d._run_worker("scheduled", "task", [])
        finally:
            daemon_mod.requests.get = orig
        self.assertFalse(d.running)
        self.assertIsNone(d.current_task)
        self.assertIsNone(d.current_reason)
        self.assertTrue(any("跳过" in t for t in d.logs))

    def test_exception_path_resets_state(self):
        from core import daemon as daemon_mod

        class FakeResp:
            def json(self):
                return {"loaded": True}

        d = self._daemon()
        orig_get, orig_run = daemon_mod.requests.get, daemon_mod.run_task
        daemon_mod.requests.get = lambda url, timeout=None: FakeResp()

        def _boom(*a, **k):
            raise RuntimeError("boom")

        daemon_mod.run_task = _boom
        try:
            d._run_worker("mention", "task", [])
        finally:
            daemon_mod.requests.get, daemon_mod.run_task = orig_get, orig_run
        self.assertFalse(d.running)
        self.assertIsNone(d.current_task)
        self.assertTrue(any("运行异常" in t for t in d.logs))


    def test_started_at_not_restored(self):
        """回归：_load_state 不得恢复 started_at（否则 uptime 显示成旧进程累计时长）。"""
        import json
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            state = os.path.join(tmp, "daemon_state.json")
            with open(state, "w", encoding="utf-8") as f:
                json.dump({"cursor": 42, "backlog": [{"id": 1}],
                           "stats": {"runs": 99, "review_stops": 7,
                                     "started_at": 500.0},
                           "last_wake_ts": 1.0, "last_run_end": 2.0,
                           "config": {}}, f)
            d = self._daemon()
            d.state_path = state
            d.stats = {"runs": 5, "mention_runs": 3, "scheduled_runs": 1,
                       "manual_runs": 1, "review_stops": 0,
                       "wake_blocked_running": 0, "started_at": 1000.0}
            d._apply_overrides = lambda data, persist=True: {}
            d._load_state()
            self.assertEqual(d.cursor, 42)
            self.assertEqual(d.stats["runs"], 99)          # 其他统计正常恢复
            self.assertEqual(d.stats["review_stops"], 7)
            self.assertEqual(d.stats["started_at"], 1000.0)  # 保持进程启动值
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""关键词记忆（keyword_memory，2026-10-19）离线测试。

覆盖：
A. 工具模块：开关/增删查改/校验（长短/空/上限/同词追加/追加溢出/fill 三态）/
   持久化/审计日志/lookup 全量
B. daemon 注入：命中注入/未命中不注入/off 不注入/keyword_only 只注关键词/
   上限截断+警告/玩家条目在线注入+离线不注入/命中计数落盘
C. 玩家请求风险审查：mention/scheduled 注入提示、manual 不注入、开关关闭不注入
D. 控制台：CONFIG_KEYS 热更三键 / UI 警告卡片与控件 / 警告队列增清

运行: python tests/test_keyword_memory.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config_loader  # noqa: E402
from core import orchestrator  # noqa: E402
from core.daemon import CONFIG_KEYS, Daemon, UI_HTML  # noqa: E402
from tools.builtin import keyword_memory as km  # noqa: E402


def cfg_with(**daemon):
    base = {"daemon": {"bot_names": ["ExampleBot"], "mention_extra": []}}
    base["daemon"].update(daemon)
    return base


class _RootMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(config_loader, "AGENT_ROOT", self.tmp.name)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()


# ---------------------------------------------------------------- A. 工具模块
class KeywordMemoryToolTest(_RootMixin, unittest.TestCase):
    def test_default_on_and_disabled_refuses(self):
        # 默认开启（与 wake_words 的默认关不同——用户要求开箱即用）
        self.assertEqual(len(km.make_keyword_memory_tools(cfg_with())), 1)
        self.assertEqual(km.make_keyword_memory_tools(
            cfg_with(keyword_memory_enabled=False)), [])
        r = km.execute(cfg_with(keyword_memory_enabled=False), action="list")
        self.assertFalse(r["ok"])
        self.assertIn("未启用", r["reason"])

    def test_orchestrator_skips_when_off(self):
        self.assertNotIn("keyword_memory", orchestrator.build_registry(
            cfg_with(keyword_memory_enabled=False)).names())

    def test_registered_when_on(self):
        self.assertIn("keyword_memory",
                      orchestrator.build_registry(cfg_with(keyword_memory_enabled=True)).names())

    def test_add_list_lookup_del_roundtrip(self):
        c = cfg_with(keyword_memory_enabled=True)
        r = km.execute(c, action="add", keyword="契约书", content="东门口那块，归testplayer管")
        self.assertTrue(r["ok"], r)
        r = km.execute(c, action="add", keyword="ds", content="Deepseek 的简称")
        self.assertTrue(r["ok"], r)
        lst = km.execute(c, action="list")
        self.assertEqual(lst["count"], 2)
        self.assertEqual(lst["max"], 30)
        self.assertEqual(lst["max_inject"], 3)
        self.assertEqual([i["keyword"] for i in lst["items"]], ["契约书", "ds"])
        # lookup 单条
        r = km.execute(c, action="lookup", keyword="ds")
        self.assertTrue(r["ok"])
        self.assertEqual(r["entry"]["content"], "Deepseek 的简称")
        # lookup 全部（含 fill=off 的）
        km.execute(c, action="fill", keyword="ds", fill="off")
        r = km.execute(c, action="lookup")
        self.assertEqual(r["count"], 2)
        self.assertTrue(any(e["keyword"] == "ds" for e in r["entries"]))
        # del
        r = km.execute(c, action="del", keyword="契约书")
        self.assertTrue(r["ok"])
        self.assertEqual(km.execute(c, action="list")["count"], 1)
        r = km.execute(c, action="del", keyword="不存在")
        self.assertFalse(r["ok"])
        # 持久化
        p = km.store_path()
        self.assertTrue(os.path.isfile(p))
        d = json.load(open(p, encoding="utf-8"))
        self.assertEqual([e["keyword"] for e in d["entries"]], ["ds"])

    def test_dup_keyword_appends_not_duplicates(self):
        c = cfg_with(keyword_memory_enabled=True)
        km.execute(c, action="add", keyword="老地方", content="基地二楼")
        r = km.execute(c, action="add", keyword="老地方", content="也在那里存过备用物资")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["action"], "add")
        self.assertIn("追加", r["note"])
        lst = km.execute(c, action="list")
        self.assertEqual(lst["count"], 1)                       # 不重复定义
        self.assertIn("基地二楼", lst["items"][0]["content_head"] or "")
        self.assertIn("也在那里存过备用物资",
                      km.execute(c, action="lookup", keyword="老地方")["entry"]["content"])

    def test_append_overflow_rejected(self):
        c = cfg_with(keyword_memory_enabled=True, keyword_memory_max_content=10)
        km.execute(c, action="add", keyword="甲甲", content="一二三四五六七八九十")
        r = km.execute(c, action="add", keyword="甲甲", content="补")
        self.assertFalse(r["ok"])
        self.assertIn("超过上限", r["reason"])

    def test_validation(self):
        c = cfg_with(keyword_memory_enabled=True, keyword_memory_max_entries=2,
                     keyword_memory_min_len=2)
        self.assertFalse(km.execute(c, action="add", keyword="", content="x")["ok"])
        self.assertFalse(km.execute(c, action="add", keyword="甲", content="单字不行")["ok"])
        self.assertFalse(km.execute(c, action="add", keyword="x" * 41, content="过长")["ok"])
        self.assertFalse(km.execute(c, action="add", keyword="契约", content="")["ok"])
        self.assertFalse(km.execute(c, action="add", keyword="契约", content="x" * 201)["ok"])
        self.assertFalse(km.execute(c, action="add", keyword="契约", content="x",
                                    fill="半满")["ok"])
        self.assertFalse(km.execute(c, action="add", keyword="契约", content="x",
                                    player="p" * 33)["ok"])
        self.assertTrue(km.execute(c, action="add", keyword="契约", content="x")["ok"])
        self.assertTrue(km.execute(c, action="add", keyword="乙乙", content="y")["ok"])
        r = km.execute(c, action="add", keyword="丙丙", content="z")     # 超上限
        self.assertFalse(r["ok"])
        self.assertIn("上限", r["reason"])
        self.assertFalse(km.execute(c, action="bad", keyword="x")["ok"])  # 未知 action

    def test_fill_modes(self):
        c = cfg_with(keyword_memory_enabled=True)
        km.execute(c, action="add", keyword="甲甲", content="内容甲", fill="keyword_only")
        r = km.execute(c, action="fill", keyword="甲甲", fill="off")
        self.assertTrue(r["ok"])
        self.assertEqual(r["fill"], "off")
        r = km.execute(c, action="fill", keyword="甲甲", fill="full")
        self.assertEqual(r["was"], "off")
        self.assertFalse(km.execute(c, action="fill", keyword="没这条", fill="full")["ok"])
        # add 时直接带 fill + player
        r = km.execute(c, action="add", keyword="乙乙", content="内容乙",
                       fill="keyword_only", player="testplayer")
        self.assertTrue(r["ok"])
        e = km.execute(c, action="lookup", keyword="乙乙")["entry"]
        self.assertEqual((e["fill"], e["player"]), ("keyword_only", "testplayer"))

    def test_audit_log_written_and_readable(self):
        c = cfg_with(keyword_memory_enabled=True)
        km.execute(c, action="add", keyword="契约书", content="东门口")
        km.execute(c, action="fill", keyword="契约书", fill="off")
        km.execute(c, action="del", keyword="契约书")
        p = km.audit_path()
        self.assertTrue(os.path.isfile(p))
        txt = open(p, encoding="utf-8").read()
        for act in ("add", "fill", "del"):
            self.assertIn(act, txt)
        self.assertIn("trigger=manual", txt)


# ------------------------------------------------- B/C. daemon 注入与审查
def _daemon_stub(cfg):
    """Daemon.__new__ 替身：只装 _build_task/_keyword_block 需要的属性。"""
    d = Daemon.__new__(Daemon)
    d.cfg = cfg
    d.overrides = {}
    d.backlog = []
    d.backlog_max = 50
    d.console_instructions = []
    d.wake_check_backlog = True
    d.running = False
    d.current_task = None
    d.current_reason = None
    d.cursor = 0
    d.review_every = 32
    d.wake_interval = 300
    d.stats = {"runs": 0, "kw_injects": 0, "warnings": 0, "started_at": time.time()}
    d.lock = threading.RLock()
    d.control_port = 8770
    d.base = "http://127.0.0.1:8780"
    d.log = lambda msg: None
    dcfg = cfg.get("daemon") or {}
    d.kw_enabled = bool(dcfg.get("keyword_memory_enabled", True))
    try:
        d.kw_max_inject = max(1, int(dcfg.get("keyword_memory_max_inject", 3)))
    except (TypeError, ValueError):
        d.kw_max_inject = 3
    d.kw_risk_review = bool(dcfg.get("player_request_risk_review", True))
    d.kw_entries = []
    d._kw_mtime = -1
    d._kw_hits_dirty = False
    d.kw_players_ttl = max(10, int(dcfg.get("keyword_memory_players_ttl", 120)))
    d._kw_players_cache = {"names": None, "ts": 0.0}
    d.warnings = []
    return d


def _chat(text, mention=False, noise=False):
    return {"id": 1, "t": time.time(), "text": text,
            "mention": mention, "noise": noise}


class KeywordInjectionTest(_RootMixin, unittest.TestCase):
    def _mk(self, **daemon):
        daemon.setdefault("keyword_memory_enabled", True)
        cfg = cfg_with(**daemon)
        d = _daemon_stub(cfg)
        # 隔离在线名单查询（不碰 8780）
        d._online_player_names = lambda: []
        return d

    def test_no_entries_no_block(self):
        d = self._mk()
        task = d._build_task("manual", [])
        self.assertNotIn("关键词记忆", task)

    def test_hit_injects_full_content(self):
        d = self._mk()
        km.execute(d.cfg, action="add", keyword="契约书", content="东门口那块归testplayer管")
        task = d._build_task("manual", [_chat("<p> 看看契约书还在不在")])
        self.assertIn("【关键词记忆·自动激活】", task)
        self.assertIn("「契约书」：东门口那块归testplayer管", task)

    def test_no_hit_no_injection(self):
        d = self._mk()
        km.execute(d.cfg, action="add", keyword="契约书", content="东门口")
        task = d._build_task("manual", [_chat("<p> 今天天气不错")])
        self.assertNotIn("关键词记忆·自动激活", task)

    def test_off_never_injected(self):
        d = self._mk()
        km.execute(d.cfg, action="add", keyword="契约书", content="东门口", fill="off")
        task = d._build_task("manual", [_chat("<p> 契约书")])
        self.assertNotIn("关键词记忆·自动激活", task)
        # 但仍可主动访问
        r = km.execute(d.cfg, action="lookup", keyword="契约书")
        self.assertTrue(r["ok"])

    def test_keyword_only_hides_content(self):
        d = self._mk()
        km.execute(d.cfg, action="add", keyword="契约书", content="东门口", fill="keyword_only")
        task = d._build_task("manual", [_chat("<p> 契约书")])
        self.assertIn("「契约书」", task)
        self.assertNotIn("东门口", task)
        self.assertIn("action=lookup", task)

    def test_cap_truncates_and_warns(self):
        d = self._mk(keyword_memory_max_inject=2)
        for i, kw in enumerate(["甲条", "乙条", "丙条", "丁条"]):
            km.execute(d.cfg, action="add", keyword=kw, content="内容%s" % kw)
        task = d._build_task("manual", [_chat("<p> 甲条 乙条 丙条 丁条 全都提到")])
        self.assertIn("「甲条」", task)
        self.assertIn("「乙条」", task)
        self.assertNotIn("「丙条」", task)          # 超上限被截断
        self.assertIn("仅注入前 2 条", task)
        self.assertEqual(len(d.warnings), 1)        # 达到上限即警告
        self.assertEqual(d.warnings[0]["kind"], "关键词记忆激活达上限")
        self.assertEqual(d.stats["warnings"], 1)

    def test_exactly_at_cap_warns(self):
        d = self._mk(keyword_memory_max_inject=2)
        for kw in ["甲条", "乙条"]:
            km.execute(d.cfg, action="add", keyword=kw, content="内容%s" % kw)
        d._build_task("manual", [_chat("<p> 甲条 乙条")])
        self.assertEqual(len(d.warnings), 1)        # 达到上限（含刚好等于）即警告

    def test_player_entry_online_offline(self):
        d = self._mk()
        km.execute(d.cfg, action="add", keyword="老地方", content="基地二楼", player="testplayer")
        # 离线 → 不命中关键词就不注入
        d._online_player_names = lambda: ["someone_else"]
        task = d._build_task("manual", [_chat("<p> 随便逛逛")])
        self.assertNotIn("关键词记忆·自动激活", task)
        # 在线 → 无论命中与否都注入，带玩家名前缀
        d._online_player_names = lambda: ["testplayer"]
        task = d._build_task("manual", [_chat("<p> 随便逛逛")])
        self.assertIn("【testplayer相关·在线】", task)
        self.assertIn("「老地方」：基地二楼", task)
        # 在线 + 命中关键词 → 仍只注入一次（一条条目）
        self.assertEqual(task.count("「老地方」"), 1)
        # 离线但命中关键词 → 普通注入
        d._online_player_names = lambda: []
        task = d._build_task("manual", [_chat("<p> 去老地方")])
        self.assertIn("「老地方」：基地二楼", task)
        self.assertNotIn("相关·在线", task)

    def test_players_ttl_config_and_cache_window(self):
        # 高延迟场景：TTL 可配置且下限 10s；缓存窗口内不重复查询 8780
        from tools.builtin import mc_bridge as _mb
        d = self._mk(keyword_memory_players_ttl=45)
        self.assertEqual(d.kw_players_ttl, 45)
        canned = {"sent": True, "result": {"count": 1,
                                           "players": [{"name": "testplayer", "ping": 99}]}}
        # _mk 替身把 _online_player_names 换成了 lambda，这里恢复真实方法测缓存逻辑
        d._online_player_names = Daemon._online_player_names.__get__(d)
        with mock.patch.object(_mb, "mc_act", return_value=canned) as mp:
            self.assertEqual(d._online_player_names(), ["testplayer"])
            self.assertEqual(d._online_player_names(), ["testplayer"])   # 窗口内走缓存
            self.assertEqual(mp.call_count, 1)
            d._kw_players_cache["ts"] = time.time() - 46              # 缓存过期
            self.assertEqual(d._online_player_names(), ["testplayer"])
            self.assertEqual(mp.call_count, 2)
        # TTL 下限保护
        self.assertEqual(self._mk(keyword_memory_players_ttl=1).kw_players_ttl, 10)

    def test_players_query_failure_degrades_gracefully(self):
        # 高延迟/主线程繁忙导致查询超时 → names=None → 玩家条目退回关键词命中，
        # 且不抛异常、不阻塞任务构建
        d = self._mk()
        km.execute(d.cfg, action="add", keyword="老地方", content="基地二楼", player="testplayer")

        def _boom():
            raise TimeoutError("simulated high-latency timeout")
        d._online_player_names = _boom
        try:
            task = d._build_task("manual", [_chat("<p> 去老地方")])
        except Exception as e:               # 不该抛到调用方
            self.fail("_build_task 不应因在线查询失败而抛异常: %s" % e)
        self.assertIn("「老地方」：基地二楼", task)   # 关键词命中仍注入
        self.assertNotIn("相关·在线", task)

    def test_hits_counted_and_flushed(self):
        d = self._mk()
        km.execute(d.cfg, action="add", keyword="契约书", content="东门口")
        d._build_task("manual", [_chat("<p> 契约书")])
        self.assertEqual(d.kw_entries[0]["hits"], 1)
        self.assertTrue(d._kw_hits_dirty)
        d._flush_kw_hits()
        self.assertFalse(d._kw_hits_dirty)
        e = json.load(open(km.store_path(), encoding="utf-8"))["entries"][0]
        self.assertEqual(e["hits"], 1)
        self.assertTrue(e["last_hit"])
        # 二次 flush 不重复写（无 dirty）
        m1 = os.path.getmtime(km.store_path())
        d._flush_kw_hits()
        self.assertEqual(os.path.getmtime(km.store_path()), m1)

    def test_mtime_reload_picks_up_tool_changes(self):
        d = self._mk()
        d._refresh_keyword_memory()
        self.assertEqual(d.kw_entries, [])
        km.execute(d.cfg, action="add", keyword="契约书", content="东门口")
        d._refresh_keyword_memory()
        self.assertEqual([e["keyword"] for e in d.kw_entries], ["契约书"])

    def test_disabled_daemon_no_injection(self):
        d = self._mk(keyword_memory_enabled=False)
        km.execute(cfg_with(keyword_memory_enabled=True), action="add",
                   keyword="契约书", content="东门口")
        task = d._build_task("manual", [_chat("<p> 契约书")])
        self.assertNotIn("关键词记忆·自动激活", task)


class RiskReviewTest(_RootMixin, unittest.TestCase):
    def _mk(self, **daemon):
        cfg = cfg_with(keyword_memory_enabled=True, **daemon)
        return _daemon_stub(cfg)

    def test_untrusted_triggers_get_review_line(self):
        d = self._mk()
        for reason in ("mention", "scheduled"):
            task = d._build_task(reason, [])
            self.assertIn("【玩家请求风险审查】", task)
            self.assertIn("先审风险", task)

    def test_manual_and_disabled_skip_review_line(self):
        d = self._mk()
        self.assertNotIn("【玩家请求风险审查】", d._build_task("manual", []))
        d2 = self._mk(player_request_risk_review=False)
        self.assertNotIn("【玩家请求风险审查】", d2._build_task("mention", []))


class WarningQueueTest(unittest.TestCase):
    def _mk(self):
        return _daemon_stub(cfg_with())

    def test_warn_snapshot_clear(self):
        d = self._mk()
        d._warn("测试", "第一条")
        d._warn("测试", "第二条")
        snap = d.warnings_snapshot()
        self.assertEqual(len(snap), 2)
        self.assertEqual(snap[0]["text"], "第一条")
        self.assertEqual(d.stats["warnings"], 2)
        n = d.clear_warnings()
        self.assertEqual(n, 2)
        self.assertEqual(d.warnings_snapshot(), [])

    def test_warn_capped_at_50(self):
        d = self._mk()
        for i in range(60):
            d._warn("批量", "第%d条" % i)
        self.assertEqual(len(d.warnings_snapshot()), 50)
        self.assertEqual(d.warnings_snapshot()[0]["text"], "第10条")


# ---------------------------------------------------------------- D. 控制台
class ConsoleWiringTest(unittest.TestCase):
    def test_hot_config_keys(self):
        from core.daemon import CONFIG_KEY_ATTRS
        self.assertIs(CONFIG_KEYS["keyword_memory_enabled"], bool)
        self.assertIs(CONFIG_KEYS["keyword_memory_max_inject"], int)
        self.assertIs(CONFIG_KEYS["player_request_risk_review"], bool)
        self.assertEqual(CONFIG_KEY_ATTRS["keyword_memory_enabled"], "kw_enabled")
        self.assertEqual(CONFIG_KEY_ATTRS["keyword_memory_max_inject"], "kw_max_inject")
        self.assertEqual(CONFIG_KEY_ATTRS["player_request_risk_review"], "kw_risk_review")

    def test_apply_overrides_updates_attrs(self):
        d = _daemon_stub(cfg_with())
        d._save_state = lambda: None
        changed = d._apply_overrides({"keyword_memory_max_inject": 5,
                                      "keyword_memory_enabled": False,
                                      "player_request_risk_review": False})
        self.assertEqual(d.kw_max_inject, 5)
        self.assertFalse(d.kw_enabled)
        self.assertFalse(d.kw_risk_review)
        self.assertIn("keyword_memory_max_inject", changed)

    def test_ui_has_warning_area_and_controls(self):
        for token in ['id="warnCard"', 'id="warnlist"', 'id="warnClear"',
                      'id="keyword_memory_enabled"', 'id="keyword_memory_max_inject"',
                      'id="player_request_risk_review"',
                      "function renderWarnings", "/warnings/clear"]:
            self.assertIn(token, UI_HTML)
        # 保存按钮覆盖新键
        self.assertIn("'keyword_memory_max_inject'", UI_HTML)
        self.assertIn("'keyword_memory_enabled'", UI_HTML)

    def test_status_exposes_warnings_n(self):
        d = _daemon_stub(cfg_with())
        d._warn("t", "x")
        st = d.status()
        self.assertEqual(st["warnings_n"], 1)
        self.assertIn("warnings", st["stats"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

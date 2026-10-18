# -*- coding: utf-8 -*-
"""待办清单（todo_list，2026-10-19）离线测试。

覆盖：
A. 工具模块：开关默认开/关闭拒绝/增删查/去重（同任务不重复登记）/校验
   （空/超长/超上限/未知 action）/del 按 id 与按原文/id 不复用/审计日志
B. daemon 注入：空清单注入纪律行/有清单注入全部条目/关闭不注入/
   上限回显/mtime 重载/待办文本参与关键词扫描（todo 块在 kw 块前）/
   status.todo_n
C. 控制台：CONFIG_KEYS 热更两键 / _apply_overrides 更新属性 / UI 控件与保存清单

运行: python tests/test_todo_list.py
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
from core.daemon import CONFIG_KEYS, CONFIG_KEY_ATTRS, Daemon, UI_HTML  # noqa: E402
from tools.builtin import keyword_memory as km  # noqa: E402
from tools.builtin import todo_list as tl  # noqa: E402


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
class TodoToolTest(_RootMixin, unittest.TestCase):
    def test_default_on_and_disabled_refuses(self):
        self.assertEqual(len(tl.make_todo_tools(cfg_with())), 1)
        self.assertEqual(tl.make_todo_tools(cfg_with(todo_list_enabled=False)), [])
        r = tl.execute(cfg_with(todo_list_enabled=False), action="list")
        self.assertFalse(r["ok"])
        self.assertIn("未启用", r["reason"])

    def test_orchestrator_skips_when_off(self):
        self.assertNotIn("todo_list", orchestrator.build_registry(
            cfg_with(todo_list_enabled=False)).names())

    def test_registered_when_on(self):
        self.assertIn("todo_list",
                      orchestrator.build_registry(cfg_with(todo_list_enabled=True)).names())

    def test_add_list_del_roundtrip(self):
        c = cfg_with(todo_list_enabled=True)
        r = tl.execute(c, action="add", task="去东门口看看契约书")
        self.assertTrue(r["ok"], r)
        first_id = r["added"]
        r = tl.execute(c, action="add", task="统计仓库里的经验块数量")
        self.assertTrue(r["ok"], r)
        lst = tl.execute(c, action="list")
        self.assertEqual(lst["count"], 2)
        self.assertEqual(lst["max"], 20)
        self.assertEqual([i["id"] for i in lst["items"]], [first_id, first_id + 1])
        # del 按 id（int 与字符串都认）
        self.assertTrue(tl.execute(c, action="del", id=first_id)["ok"])
        self.assertEqual(tl.execute(c, action="list")["count"], 1)
        # del 按任务原文
        r = tl.execute(c, action="del", task="统计仓库里的经验块数量")
        self.assertTrue(r["ok"])
        self.assertEqual(r["removed"], first_id + 1)
        self.assertEqual(tl.execute(c, action="list")["count"], 0)
        # del 不存在
        r = tl.execute(c, action="del", id=999)
        self.assertFalse(r["ok"])
        self.assertIn("未找到", r["reason"])
        r = tl.execute(c, action="del", task="没有这条")
        self.assertFalse(r["ok"])

    def test_dup_task_not_duplicated(self):
        c = cfg_with(todo_list_enabled=True)
        r = tl.execute(c, action="add", task="修好东门的桥")
        self.assertTrue(r["ok"], r)
        r2 = tl.execute(c, action="add", task=" 修好东门的桥 ")   # 前后空格 = 同任务
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["existed"], r["added"])
        self.assertIn("不重复登记", r2["note"])
        self.assertEqual(tl.execute(c, action="list")["count"], 1)

    def test_validation(self):
        c = cfg_with(todo_list_enabled=True, todo_list_max=2)
        self.assertFalse(tl.execute(c, action="add", task="")["ok"])
        self.assertFalse(tl.execute(c, action="add", task="   ")["ok"])
        self.assertFalse(tl.execute(c, action="add", task="x" * 201)["ok"])
        self.assertTrue(tl.execute(c, action="add", task="任务甲")["ok"])
        self.assertTrue(tl.execute(c, action="add", task="任务乙")["ok"])
        r = tl.execute(c, action="add", task="任务丙")     # 超上限
        self.assertFalse(r["ok"])
        self.assertIn("上限", r["reason"])
        self.assertFalse(tl.execute(c, action="bad")["ok"])  # 未知 action

    def test_id_not_reused_after_delete(self):
        c = cfg_with(todo_list_enabled=True)
        r1 = tl.execute(c, action="add", task="任务甲")
        tl.execute(c, action="del", id=r1["added"])
        r2 = tl.execute(c, action="add", task="任务乙")
        self.assertEqual(r2["added"], r1["added"] + 1)     # next_id 持久化，不复用
        d = json.load(open(tl.store_path(), encoding="utf-8"))
        self.assertEqual(d["next_id"], r2["added"] + 1)
        self.assertEqual([e["task"] for e in d["entries"]], ["任务乙"])

    def test_del_by_hash_prefixed_id(self):
        c = cfg_with(todo_list_enabled=True)
        r = tl.execute(c, action="add", task="任务甲")
        ok = tl.execute(c, action="del", id="#%d" % r["added"])
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["removed"], r["added"])

    def test_persistence_and_audit(self):
        c = cfg_with(todo_list_enabled=True)
        tl.execute(c, action="add", task="任务甲")
        tl.execute(c, action="del", task="任务甲")
        p = tl.store_path()
        self.assertTrue(os.path.isfile(p))
        d = json.load(open(p, encoding="utf-8"))
        self.assertEqual(d["entries"], [])
        ap = tl.audit_path()
        self.assertTrue(os.path.isfile(ap))
        txt = open(ap, encoding="utf-8").read()
        self.assertIn("add", txt)
        self.assertIn("del", txt)
        self.assertIn("trigger=manual", txt)


# ------------------------------------------------- B. daemon 注入
def _daemon_stub(cfg):
    """Daemon.__new__ 替身：只装 _build_task/_todo_block 需要的属性。"""
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
    d.kw_players_ttl = 120
    d._kw_players_cache = {"names": None, "ts": 0.0}
    d.warnings = []
    # 待办清单属性（与 Daemon.__init__ 同构）
    d.todo_enabled = bool(dcfg.get("todo_list_enabled", True))
    try:
        d.todo_max = max(1, int(dcfg.get("todo_list_max", 20)))
    except (TypeError, ValueError):
        d.todo_max = 20
    d.todo_entries = []
    d._todo_mtime = -1
    return d


class TodoInjectionTest(_RootMixin, unittest.TestCase):
    def _mk(self, **daemon):
        daemon.setdefault("todo_list_enabled", True)
        daemon.setdefault("keyword_memory_enabled", False)   # 隔离：本组只测待办注入
        cfg = cfg_with(**daemon)
        d = _daemon_stub(cfg)
        d._online_player_names = lambda: []
        return d

    def test_empty_list_injects_discipline_line(self):
        d = self._mk()
        task = d._build_task("manual", [])
        self.assertIn("【待办清单】当前为空", task)
        self.assertIn("action=add", task)
        self.assertIn("finish_run", task)

    def test_entries_injected_every_wake(self):
        d = self._mk()
        tl.execute(d.cfg, action="add", task="去东门口看看契约书")
        tl.execute(d.cfg, action="add", task="统计经验块数量")
        for reason in ("manual", "mention", "scheduled"):
            task = d._build_task(reason, [])
            self.assertIn("【待办清单】", task)
            self.assertIn("· #1 去东门口看看契约书", task)
            self.assertIn("· #2 统计经验块数量", task)
            self.assertIn("（共 2 条，上限 20", task)

    def test_disabled_no_injection(self):
        d = self._mk(todo_list_enabled=False)
        tl.execute(cfg_with(todo_list_enabled=True), action="add", task="任务甲")
        task = d._build_task("manual", [])
        self.assertNotIn("【待办清单】", task)

    def test_max_echoed_from_config(self):
        d = self._mk(todo_list_max=5)
        tl.execute(d.cfg, action="add", task="任务甲")
        task = d._build_task("manual", [])
        self.assertIn("（共 1 条，上限 5", task)

    def test_mtime_reload_picks_up_tool_changes(self):
        d = self._mk()
        d._refresh_todo_list()
        self.assertEqual(d.todo_entries, [])
        tl.execute(d.cfg, action="add", task="任务甲")
        d._refresh_todo_list()
        self.assertEqual([e["task"] for e in d.todo_entries], ["任务甲"])
        tl.execute(d.cfg, action="del", id=1)
        d._refresh_todo_list()
        self.assertEqual(d.todo_entries, [])

    def test_todo_text_participates_in_keyword_scan(self):
        # todo 块拼在 kw 块之前 → 待办文本也参与关键词命中扫描（正在做的事命中术语是特性）
        d = self._mk(keyword_memory_enabled=True)
        tl.execute(d.cfg, action="add", task="去东门口看看契约书还在不在")
        km.execute(d.cfg, action="add", keyword="契约书", content="东门口那块归testplayer管")
        task = d._build_task("manual", [])
        self.assertIn("【待办清单】", task)
        self.assertIn("【关键词记忆·自动激活】", task)
        self.assertIn("「契约书」：东门口那块归testplayer管", task)
        # 顺序：待办块在关键词块之前
        self.assertLess(task.index("【待办清单】"), task.index("【关键词记忆·自动激活】"))

    def test_status_exposes_todo_n(self):
        d = self._mk()
        tl.execute(d.cfg, action="add", task="任务甲")
        st = d.status()
        self.assertEqual(st["todo_n"], 1)
        tl.execute(d.cfg, action="del", id=1)
        self.assertEqual(d.status()["todo_n"], 0)


# ---------------------------------------------------------------- C. 控制台
class ConsoleWiringTest(unittest.TestCase):
    def test_hot_config_keys(self):
        self.assertIs(CONFIG_KEYS["todo_list_enabled"], bool)
        self.assertIs(CONFIG_KEYS["todo_list_max"], int)
        self.assertEqual(CONFIG_KEY_ATTRS["todo_list_enabled"], "todo_enabled")
        self.assertEqual(CONFIG_KEY_ATTRS["todo_list_max"], "todo_max")

    def test_apply_overrides_updates_attrs(self):
        d = _daemon_stub(cfg_with())
        d._save_state = lambda: None
        changed = d._apply_overrides({"todo_list_max": 8, "todo_list_enabled": False})
        self.assertEqual(d.todo_max, 8)
        self.assertFalse(d.todo_enabled)
        self.assertIn("todo_list_max", changed)
        # 关闭后 _todo_block 不再注入
        self.assertEqual(d._todo_block(), "")

    def test_ui_has_controls_and_save_lists(self):
        for token in ['id="todo_list_enabled"', 'id="todo_list_max"']:
            self.assertIn(token, UI_HTML)
        self.assertIn("'todo_list_max'", UI_HTML)
        self.assertIn("'todo_list_enabled'", UI_HTML)

    def test_config_get_exposes_keys(self):
        d = _daemon_stub(cfg_with(todo_list_max=7))
        cg = d.config_get()
        self.assertTrue(cg["todo_list_enabled"])
        self.assertEqual(cg["todo_list_max"], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)

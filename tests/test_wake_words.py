# -*- coding: utf-8 -*-
"""AI 自管唤醒词（wake_words）离线测试：开关/增删查/限额去重/持久化/daemon 匹配。"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config_loader  # noqa: E402
from core import orchestrator  # noqa: E402
from tools.builtin import wake_words  # noqa: E402


def cfg_with(**daemon):
    base = {"daemon": {"bot_names": ["ExampleBot"], "mention_extra": []}}
    base["daemon"].update(daemon)
    return base


class WakeWordsToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(config_loader, "AGENT_ROOT", self.tmp.name)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def test_disabled_no_tools_and_refuse(self):
        self.assertEqual(wake_words.make_wake_words_tools(cfg_with()), [])
        self.assertEqual(wake_words.make_wake_words_tools(
            cfg_with(allow_ai_wake_words=False)), [])
        r = wake_words.execute(cfg_with(), action="list")
        self.assertFalse(r["ok"])
        self.assertIn("未启用", r["reason"])

    def test_orchestrator_skips_when_off(self):
        reg = orchestrator.build_registry(cfg_with())
        self.assertNotIn("wake_words", reg.names())

    def test_add_list_remove_roundtrip(self):
        c = cfg_with(allow_ai_wake_words=True)
        self.assertIn("wake_words", orchestrator.build_registry(c).names())
        r = wake_words.execute(c, action="add", word="小深")
        self.assertTrue(r["ok"], r)
        r = wake_words.execute(c, action="add", word="小深")
        self.assertFalse(r["ok"])                      # 去重
        self.assertIn("已存在", r["reason"])
        self.assertTrue(wake_words.execute(c, action="add", word="Test")["ok"])
        r = wake_words.execute(c, action="add", word="test")
        self.assertFalse(r["ok"])                      # 大小写不敏感去重
        self.assertIn("已存在", r["reason"])
        self.assertTrue(wake_words.execute(c, action="remove", word="Test")["ok"])
        lst = wake_words.execute(c, action="list")
        self.assertEqual(lst["words"], ["小深"])
        p = wake_words.store_path()
        self.assertTrue(os.path.isfile(p))             # 持久化
        self.assertEqual(json.load(open(p, encoding="utf-8"))["words"], ["小深"])
        r = wake_words.execute(c, action="remove", word="小深")
        self.assertTrue(r["ok"])
        self.assertEqual(wake_words.execute(c, action="list")["words"], [])

    def test_validation(self):
        c = cfg_with(allow_ai_wake_words=True, ai_wake_words_max=2)
        self.assertFalse(wake_words.execute(c, action="add", word="")["ok"])
        self.assertFalse(wake_words.execute(c, action="add", word="a b")["ok"])   # 空白
        self.assertFalse(wake_words.execute(c, action="add", word="x" * 17)["ok"])  # 过长
        self.assertTrue(wake_words.execute(c, action="add", word="甲")["ok"])
        self.assertTrue(wake_words.execute(c, action="add", word="乙")["ok"])
        r = wake_words.execute(c, action="add", word="丙")               # 超上限
        self.assertFalse(r["ok"])
        self.assertIn("上限", r["reason"])
        self.assertFalse(wake_words.execute(c, action="remove", word="丙")["ok"])  # 不存在
        self.assertFalse(wake_words.execute(c, action="bad", word="x")["ok"])      # 未知 action

    def test_max_default_eight(self):
        c = cfg_with(allow_ai_wake_words=True)
        for i in range(8):
            self.assertTrue(wake_words.execute(c, action="add", word="词%d" % i)["ok"])
        self.assertFalse(wake_words.execute(c, action="add", word="词8")["ok"])


class DaemonMatchTest(unittest.TestCase):
    """daemon.is_mention 对 AI 自管唤醒词的匹配。"""

    def _daemon(self, tmp, allow):
        from core.daemon import Daemon
        d = Daemon.__new__(Daemon)
        d.bot_names = ["ExampleBot"]
        d.extra_mentions = []
        d.allow_ai_wake_words = allow
        d.ai_wake_words = []
        d._ww_mtime = 0.0
        self._p2 = mock.patch.object(config_loader, "AGENT_ROOT", tmp)
        self._p2.start()
        d.ai_wake_words = d._refresh_ai_wake_words()
        return d

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(config_loader, "AGENT_ROOT", self.tmp.name)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def test_match_when_enabled(self):
        d = self._daemon(self.tmp.name, allow=True)
        wake_words.execute(cfg_with(allow_ai_wake_words=True), action="add", word="小深")
        d.ai_wake_words = d._refresh_ai_wake_words()
        self.assertTrue(d.is_mention("小深 在吗"))
        self.assertTrue(d.is_mention("来玩呀 小深"))
        self.assertFalse(d.is_mention("随便说点什么"))
        # 自己发言不触发（防自激）
        self.assertFalse(d.is_mention("<ExampleBot> 小深"))

    def test_ignored_when_disabled(self):
        d = self._daemon(self.tmp.name, allow=False)
        wake_words.execute(cfg_with(allow_ai_wake_words=True), action="add", word="小深")
        d.ai_wake_words = d._refresh_ai_wake_words()
        self.assertEqual(d.ai_wake_words, [])       # 开关关 → 恒空表
        self.assertFalse(d.is_mention("小深 在吗"))

    def test_mtime_reload(self):
        d = self._daemon(self.tmp.name, allow=True)
        self.assertEqual(d.ai_wake_words, [])
        wake_words.execute(cfg_with(allow_ai_wake_words=True), action="add", word="新词")
        d.ai_wake_words = d._refresh_ai_wake_words()   # poll_chat 每轮调用
        self.assertEqual(d.ai_wake_words, ["新词"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

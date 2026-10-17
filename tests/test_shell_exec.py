# -*- coding: utf-8 -*-
"""本机命令行能力（shell_run）离线测试。

覆盖：默认关闭不注册 / 正常执行 / 无 shell 语义 / 白名单（含空名单 fail-closed、路径拒绝）/
超时强杀 / 输出截断 / 聊天隔离（防注入）/ 审计落盘 / 工作目录限制。
不调 LLM、不联网；用的是当前解释器本身作为被执行程序。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config_loader, run_context  # noqa: E402
from core import orchestrator  # noqa: E402
from tools.builtin import shell_exec  # noqa: E402

PY = sys.executable


def cfg_with(shell=None, **other):
    base = {"permissions": {"file_mode": "full",
                            "roots": {"agent": "."},
                            "default_root": "agent"}}
    if shell is not None:
        base["permissions"]["shell"] = shell
    base.update(other)
    return base


class ShellDisabledTest(unittest.TestCase):
    def test_default_off_no_tools(self):
        self.assertFalse(shell_exec.enabled({}))
        self.assertEqual(shell_exec.make_shell_tools({}), [])
        self.assertEqual(shell_exec.make_shell_tools(cfg_with({"enabled": False})), [])

    def test_orchestrator_skips_when_off(self):
        reg = orchestrator.build_registry(cfg_with({"enabled": False}))
        self.assertNotIn("shell_run", reg.names())

    def test_execute_refuses_when_off(self):
        r = shell_exec.execute(cfg_with({"enabled": False}), argv=[PY, "-c", "print(1)"])
        self.assertFalse(r["ok"])
        self.assertIn("未启用", r["reason"])

    def test_notice_says_closed(self):
        txt = "\n".join(shell_exec.startup_notice(cfg_with({"enabled": False})))
        self.assertIn("已关闭", txt)


class ShellExecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(config_loader, "AGENT_ROOT", self.tmp.name)
        self._p.start()
        run_context.set_trigger("manual")
        self.shell = {"enabled": True, "mode": "any", "workdir": ".",
                      "timeout_sec": 10, "max_output": 200, "audit_log": "logs/shell_audit.log"}
        self.cfg = cfg_with(dict(self.shell))

    def tearDown(self):
        run_context.set_trigger("manual")
        self._p.stop()
        self.tmp.cleanup()

    def test_orchestrator_registers_when_on(self):
        reg = orchestrator.build_registry(self.cfg)
        self.assertIn("shell_run", reg.names())

    def test_run_ok(self):
        r = shell_exec.execute(self.cfg, argv=[PY, "-c", "print('hello-shell')"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["rc"], 0)
        self.assertIn("hello-shell", r["stdout"])
        self.assertEqual(r["cwd"], os.path.abspath(self.tmp.name))

    def test_no_shell_semantics(self):
        """&& 必须作为普通参数原样传进去，绝不被解释。"""
        r = shell_exec.execute(self.cfg, argv=[PY, "-c",
                                               "import sys;print('|'.join(sys.argv[1:]))", "a&&b"])
        self.assertTrue(r["ok"], r)
        self.assertIn("a&&b", r["stdout"])
        # cmd 字符串形式同样只做分词，不经 shell
        r2 = shell_exec.execute(self.cfg, cmd='%s -c "import sys;print(\'|\'.join(sys.argv[1:]))" a&&b' % PY)
        self.assertTrue(r2["ok"], r2)
        self.assertIn("a&&b", r2["stdout"])

    def test_timeout_kills(self):
        c = cfg_with(dict(self.shell, timeout_sec=1))
        r = shell_exec.execute(c, argv=[PY, "-c", "import time;time.sleep(5)"])
        self.assertFalse(r["ok"])
        self.assertTrue(r["timed_out"])
        self.assertLess(r["ms"], 4000)

    def test_output_truncated(self):
        c = cfg_with(dict(self.shell, max_output=100))
        r = shell_exec.execute(c, argv=[PY, "-c", "print('x'*5000)"])
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["truncated"])
        self.assertLessEqual(len(r["stdout"]), 100)

    def test_argv_required(self):
        r = shell_exec.execute(self.cfg)
        self.assertFalse(r["ok"])
        self.assertIn("缺少 argv", r["reason"])

    def test_audit_written(self):
        shell_exec.execute(self.cfg, argv=[PY, "-c", "print('audited')"])
        p = os.path.join(self.tmp.name, "logs", "shell_audit.log")
        self.assertTrue(os.path.exists(p))
        line = json.loads(open(p, encoding="utf-8").read().strip().splitlines()[-1])
        self.assertEqual(line["rc"], 0)
        self.assertIn("audited", line["stdout_head"])
        self.assertEqual(line["trigger"], "manual")


class ShellAllowlistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(config_loader, "AGENT_ROOT", self.tmp.name)
        self._p.start()
        run_context.set_trigger("manual")

    def tearDown(self):
        run_context.set_trigger("manual")
        self._p.stop()
        self.tmp.cleanup()

    def test_empty_allowlist_fails_closed(self):
        c = cfg_with({"enabled": True, "mode": "allowlist", "allowlist": []})
        r = shell_exec.execute(c, argv=[PY, "-c", "print(1)"])
        self.assertFalse(r["ok"])
        self.assertIn("白名单为空", r["reason"])

    def test_path_rejected(self):
        c = cfg_with({"enabled": True, "mode": "allowlist", "allowlist": [os.path.basename(PY)]})
        r = shell_exec.execute(c, argv=[PY, "-c", "print(1)"])
        self.assertFalse(r["ok"])
        self.assertIn("裸程序名", r["reason"])

    def test_not_in_allowlist(self):
        # 白名单里放别的程序，被调的是名单外的 → 命中"不在白名单"
        c = cfg_with({"enabled": True, "mode": "allowlist", "allowlist": ["some-other-program"]})
        r = shell_exec.execute(c, argv=["definitely-not-a-real-program"])
        self.assertFalse(r["ok"])
        self.assertIn("不在白名单", r["reason"])

    def test_allowlisted_but_missing(self):
        # 在名单里但 PATH 里不存在 → 命中 which 失败
        c = cfg_with({"enabled": True, "mode": "allowlist", "allowlist": ["definitely-not-a-real-program"]})
        r = shell_exec.execute(c, argv=["definitely-not-a-real-program"])
        self.assertFalse(r["ok"])
        self.assertIn("PATH 里找不到", r["reason"])

    def test_bare_name_allowlisted(self):
        """裸程序名在白名单内 → 放行（用 cmd.exe 这类必然在 PATH 上的程序）。"""
        prog = "cmd.exe" if os.name == "nt" else "sh"
        args = ["/c", "echo", "ok-bare"] if os.name == "nt" else ["-c", "echo ok-bare"]
        c = cfg_with({"enabled": True, "mode": "allowlist", "allowlist": [prog]})
        r = shell_exec.execute(c, argv=[prog] + args)
        self.assertTrue(r["ok"], r)
        self.assertIn("ok-bare", r["stdout"])


class ShellChatIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(config_loader, "AGENT_ROOT", self.tmp.name)
        self._p.start()
        self.cfg = cfg_with({"enabled": True, "mode": "any", "allow_from_chat": False})

    def tearDown(self):
        run_context.set_trigger("manual")
        self._p.stop()
        self.tmp.cleanup()

    def test_chat_trigger_blocked(self):
        run_context.set_trigger("mention")
        r = shell_exec.execute(self.cfg, argv=[PY, "-c", "print(1)"])
        self.assertFalse(r["ok"])
        self.assertIn("禁止调用本机命令行", r["reason"])
        self.assertTrue(run_context.is_untrusted_trigger())

    def test_scheduled_trigger_blocked(self):
        run_context.set_trigger("scheduled")
        self.assertFalse(shell_exec.execute(self.cfg, argv=[PY, "-c", "print(1)"])["ok"])

    def test_manual_trigger_allowed(self):
        run_context.set_trigger("manual")
        self.assertTrue(shell_exec.execute(self.cfg, argv=[PY, "-c", "print(2)"])["ok"])

    def test_chat_allowed_when_explicitly_enabled(self):
        c = cfg_with({"enabled": True, "mode": "any", "allow_from_chat": True})
        run_context.set_trigger("mention")
        self.assertTrue(shell_exec.execute(c, argv=[PY, "-c", "print(3)"])["ok"])

    def test_isolation_logged(self):
        run_context.set_trigger("mention")
        shell_exec.execute(self.cfg, argv=[PY, "-c", "print(1)"])
        p = os.path.join(self.tmp.name, "logs", "shell_audit.log")
        self.assertTrue(os.path.exists(p))
        line = json.loads(open(p, encoding="utf-8").read().strip().splitlines()[-1])
        self.assertEqual(line["reason"], "chat-isolation")


if __name__ == "__main__":
    unittest.main(verbosity=2)

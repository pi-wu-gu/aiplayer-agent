# -*- coding: utf-8 -*-
"""权限表 / 文件访问白名单 离线测试（不连游戏、不调 LLM）。

覆盖：
1. 模式归一与别名（full / readonly / off，含 read/ro/none 等写法）
2. intersect 只能收窄，绝不放宽
3. make_file_tools：off → 无工具；readonly → 仅 file_read；full → 读 + 写
4. 角色 file_mode 收窄（父 full + 子 readonly → readonly；父 readonly + 子 full → 仍 readonly）
5. safe_path：正常相对/绝对路径放行；`..` 穿越、根外绝对路径、符号链接逃逸 一律拒绝
6. build_registry：主 agent 也受权限表约束（这是既往缺口）

运行: python tests/test_permissions.py
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import orchestrator  # noqa: E402
from tools.builtin import file_io  # noqa: E402


def make_cfg(mode, roots, default_root="agent"):
    return {
        "defaults": {"provider": "deepseek", "model": "deepseek-flash"},
        "mc_bridge": {"base_url": "http://127.0.0.1:9999"},   # 测试不直连
        "permissions": {"file_mode": mode, "roots": roots, "default_root": default_root},
    }


class PermModeTest(unittest.TestCase):

    def test_normalize_aliases(self):
        self.assertEqual(file_io.normalize_mode("full"), "full")
        self.assertEqual(file_io.normalize_mode("READONLY"), "readonly")
        self.assertEqual(file_io.normalize_mode("read"), "readonly")
        self.assertEqual(file_io.normalize_mode("ro"), "readonly")
        self.assertEqual(file_io.normalize_mode("off"), "off")
        self.assertEqual(file_io.normalize_mode("none"), "off")
        # 无法识别 → 按 full，保证旧配置行为不变
        self.assertEqual(file_io.normalize_mode("whatever"), "full")
        self.assertEqual(file_io.normalize_mode(None), "full")

    def test_intersect_only_narrows(self):
        self.assertEqual(file_io.intersect("full", "readonly"), "readonly")
        self.assertEqual(file_io.intersect("readonly", "full"), "readonly")
        self.assertEqual(file_io.intersect("off", "full"), "off")
        self.assertEqual(file_io.intersect("full", "full"), "full")
        self.assertEqual(file_io.intersect("readonly", "off"), "off")


class ToolExposureTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiplayer_perm_")
        self.roots = {"agent": os.path.join(self.tmp, "ws"),
                      "shared": os.path.join(self.tmp, "shared")}
        os.makedirs(self.roots["agent"])
        os.makedirs(self.roots["shared"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def names(self, mode, override=None):
        cfg = make_cfg(mode, self.roots)
        return sorted(m["name"] for m, _ in file_io.make_file_tools(cfg, mode_override=override))

    def test_mode_full_exposes_read_and_write(self):
        self.assertEqual(self.names("full"), ["file_read", "file_write"])

    def test_mode_readonly_exposes_read_only(self):
        self.assertEqual(self.names("readonly"), ["file_read"])

    def test_mode_off_exposes_nothing(self):
        self.assertEqual(self.names("off"), [])

    def test_role_mode_can_only_narrow(self):
        # 父 full + 角色 readonly → readonly
        self.assertEqual(self.names("full", "readonly"), ["file_read"])
        # 父 readonly + 角色 full → 仍 readonly（不放宽）
        self.assertEqual(self.names("readonly", "full"), ["file_read"])
        # 父 full + 角色 off → 无工具
        self.assertEqual(self.names("full", "off"), [])


class SafePathTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiplayer_path_")
        self.ws = os.path.join(self.tmp, "ws")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.ws)
        os.makedirs(self.outside)
        self.cfg = make_cfg("full", {"agent": self.ws})
        self.link_ok = False
        try:
            os.symlink(self.outside, os.path.join(self.ws, "escape"))
            self.link_ok = True
        except (OSError, NotImplementedError, AttributeError):
            self.link_ok = False

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_relative_path_ok(self):
        got = file_io.safe_path(self.cfg, os.path.join("docs", "a.md"))
        self.assertTrue(got.startswith(os.path.realpath(self.ws)))

    def test_absolute_inside_ok(self):
        target = os.path.join(self.ws, "x.txt")
        self.assertEqual(file_io.safe_path(self.cfg, target), os.path.realpath(target))

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(ValueError):
            file_io.safe_path(self.cfg, os.path.join("..", "..", "outside", "secret.txt"))

    def test_absolute_outside_rejected(self):
        with self.assertRaises(ValueError):
            file_io.safe_path(self.cfg, os.path.join(self.outside, "secret.txt"))

    def test_symlink_escape_rejected(self):
        if not self.link_ok:
            self.skipTest("当前环境不允许创建符号链接（需管理员/开发者模式）")
        with self.assertRaises(ValueError):
            file_io.safe_path(self.cfg, os.path.join("escape", "secret.txt"))


class RegistryGateTest(unittest.TestCase):
    """主 agent 也走统一装配 —— 权限表对 primary 同样生效。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiplayer_reg_")
        self.cfg = make_cfg("off", {"agent": self.tmp})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_primary_registry_respects_off(self):
        reg = orchestrator.build_registry(self.cfg)
        self.assertNotIn("file_read", reg.names())
        self.assertNotIn("file_write", reg.names())
        # 非文件类工具不受影响
        self.assertIn("mc_state", reg.names())
        self.assertIn("mc_act", reg.names())

    def test_readonly_keeps_read_drops_write(self):
        self.cfg["permissions"]["file_mode"] = "readonly"
        reg = orchestrator.build_registry(self.cfg)
        self.assertIn("file_read", reg.names())
        self.assertNotIn("file_write", reg.names())

    def test_role_asking_for_disabled_tool_is_soft_skipped(self):
        # 父 readonly，角色仍申请 file_write → 静默跳过而不是抛错
        self.cfg["permissions"]["file_mode"] = "readonly"
        reg = orchestrator.build_registry_for(self.cfg, ["file_read", "file_write"], None)
        self.assertIn("file_read", reg.names())
        self.assertNotIn("file_write", reg.names())

    def test_role_asking_for_unknown_tool_raises(self):
        with self.assertRaises(KeyError):
            orchestrator.build_registry_for(self.cfg, ["no_such_tool"], None)


if __name__ == "__main__":
    unittest.main(verbosity=2)

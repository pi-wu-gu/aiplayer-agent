# -*- coding: utf-8 -*-
"""安装根解析覆盖（AIPLAYER_HOME）+ keys.yaml 模板生成测试。"""
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config_loader


class TestAgentRootResolve(unittest.TestCase):
    def test_default_is_code_dir(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIPLAYER_HOME", None)
            importlib.reload(config_loader)
        try:
            expected = os.path.dirname(os.path.dirname(os.path.abspath(config_loader.__file__)))
            self.assertEqual(config_loader.AGENT_ROOT, expected)
        finally:
            importlib.reload(config_loader)  # 恢复真实环境

    def test_env_override_to_real_dir(self):
        d = tempfile.mkdtemp(prefix="aiplayer_home_")
        old = os.environ.get("AIPLAYER_HOME")
        os.environ["AIPLAYER_HOME"] = d
        try:
            importlib.reload(config_loader)
            self.assertEqual(config_loader.AGENT_ROOT, os.path.abspath(d))
        finally:
            if old is None:
                os.environ.pop("AIPLAYER_HOME", None)
            else:
                os.environ["AIPLAYER_HOME"] = old
            importlib.reload(config_loader)

    def test_env_invalid_falls_back(self):
        """指向不存在目录 → 回退代码位置（防御：部署目录被删后仍能启动）。"""
        old = os.environ.get("AIPLAYER_HOME")
        os.environ["AIPLAYER_HOME"] = r"Z:\definitely\not\exist\12345"
        try:
            importlib.reload(config_loader)
            expected = os.path.dirname(os.path.dirname(os.path.abspath(config_loader.__file__)))
            self.assertEqual(config_loader.AGENT_ROOT, expected)
        finally:
            if old is None:
                os.environ.pop("AIPLAYER_HOME", None)
            else:
                os.environ["AIPLAYER_HOME"] = old
            importlib.reload(config_loader)


class TestKeysTemplate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent_keys_test_")
        self._orig_root = config_loader.AGENT_ROOT
        config_loader.AGENT_ROOT = self.tmp
        config_loader.KEYS_PATH = os.path.join(self.tmp, "config", "keys.yaml")

    def tearDown(self):
        config_loader.AGENT_ROOT = self._orig_root
        config_loader.KEYS_PATH = os.path.join(self._orig_root, "config", "keys.yaml")

    def test_template_generated(self):
        self.assertFalse(os.path.exists(config_loader.KEYS_PATH))
        p = config_loader.ensure_keys_template()
        self.assertEqual(p, config_loader.KEYS_PATH)
        text = open(p, encoding="utf-8").read()
        self.assertIn("providers:", text)
        self.assertIn("api_key", text)
        self.assertIn("base_url", text)
        # 幂等：已有文件不覆盖
        open(p, "w", encoding="utf-8").write("# my-custom-content\n")
        config_loader.ensure_keys_template()
        self.assertIn("my-custom-content", open(p, encoding="utf-8").read())

    def test_load_keys_generates_template_then_raises(self):
        with self.assertRaises(FileNotFoundError) as cm:
            config_loader.load_keys()
        self.assertIn("模板", str(cm.exception))
        self.assertTrue(os.path.exists(config_loader.KEYS_PATH))  # 模板已落盘

    def test_real_keys_still_load(self):
        os.makedirs(os.path.dirname(config_loader.KEYS_PATH), exist_ok=True)
        with open(config_loader.KEYS_PATH, "w", encoding="utf-8") as f:
            f.write("providers:\n  - name: t\n    base_url: http://x\n"
                    "    api_key: abc123\nmodel_prices: {}\n")
        providers, prices = config_loader.load_keys()
        self.assertEqual(providers[0]["name"], "t")
        self.assertEqual(prices, {})


if __name__ == "__main__":
    unittest.main()

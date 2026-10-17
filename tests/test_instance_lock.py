# -*- coding: utf-8 -*-
"""实例锁测试：获取/互斥/释放/死亡接管/损坏内容/探测。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.instance_lock import InstanceLock


class TestInstanceLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent_lock_test_")

    def test_acquire_ok_and_info(self):
        lk = InstanceLock(root=self.tmp, kind="daemon")
        ok, msg = lk.acquire()
        self.assertTrue(ok, msg)
        info = lk.holder()
        self.assertEqual(info["kind"], "daemon")
        self.assertEqual(info["pid"], os.getpid())
        self.assertIn("started_at", info)
        lk.release()

    def test_mutual_exclusion(self):
        a = InstanceLock(root=self.tmp, kind="daemon")
        ok, _ = a.acquire()
        self.assertTrue(ok)
        b = InstanceLock(root=self.tmp, kind="daemon")
        ok, msg = b.acquire()
        self.assertFalse(ok)
        self.assertIn(str(os.getpid()), msg)   # 报出持有者 PID
        self.assertIn("只允许一个实例", msg)
        a.release()

    def test_release_then_reacquire(self):
        a = InstanceLock(root=self.tmp, kind="daemon")
        self.assertTrue(a.acquire()[0])
        a.release()
        b = InstanceLock(root=self.tmp, kind="daemon")
        self.assertTrue(b.acquire()[0])
        b.release()

    def test_release_cleans_showcase_file(self):
        lk = InstanceLock(root=self.tmp, kind="daemon")
        lk.acquire()
        lk.release()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "agent.lock")))

    def test_dead_holder_takeover(self):
        """持有者进程死亡 → OS 自动释放锁 → 新实例可接管。"""
        code = (
            "import sys, time;"
            "sys.path.insert(0, r'%s');"
            "from core.instance_lock import InstanceLock;"
            "lk = InstanceLock(root=r'%s', kind='daemon');"
            "ok, _ = lk.acquire();"
            "assert ok;"
            "print('locked', flush=True);"
            "time.sleep(30)"  # 故意不 release 就被杀
            % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), self.tmp)
        )
        p = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            p.stdout.readline()  # 等"locked"
        finally:
            p.kill()
            p.wait()
        lk = InstanceLock(root=self.tmp, kind="daemon")
        ok, msg = lk.acquire()
        self.assertTrue(ok, "持有者死后应可接管: %s" % msg)
        lk.release()

    def test_corrupt_content_still_locked_or_takeover(self):
        """文件内容损坏：无人持锁时应可接管并重写展示牌。"""
        with open(os.path.join(self.tmp, "agent.lock"), "w", encoding="utf-8") as f:
            f.write("not-json{{{")
        lk = InstanceLock(root=self.tmp, kind="daemon")
        ok, msg = lk.acquire()
        self.assertTrue(ok, msg)
        self.assertEqual(lk.holder()["pid"], os.getpid())
        lk.release()

    def test_probe(self):
        lk = InstanceLock(root=self.tmp, kind="daemon")
        running, _ = lk.probe()
        self.assertFalse(running)
        lk.acquire()
        running, msg = lk.probe()
        self.assertTrue(running)
        self.assertIn("daemon", msg)
        lk.release()

    def test_probe_reports_foreign_holder(self):
        """探测能读到别的进程留下的展示牌（不持锁也能读；布局=offset0占位+\n+JSON）。"""
        path = os.path.join(self.tmp, "agent.lock")
        with open(path, "wb") as f:
            f.write(b"0\n" + json.dumps({"pid": 424242, "kind": "daemon",
                                         "started_at": "2026-10-16 17:00:00"}).encode("utf-8"))
        lk = InstanceLock(root=self.tmp, kind="daemon")
        running, msg = lk.probe()
        self.assertTrue(running)
        self.assertIn("424242", msg)

    def test_double_release_safe(self):
        lk = InstanceLock(root=self.tmp, kind="daemon")
        lk.acquire()
        lk.release()
        lk.release()  # 幂等


if __name__ == "__main__":
    unittest.main()

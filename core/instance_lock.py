# -*- coding: utf-8 -*-
"""实例锁：防止同一安装根下出现两个 daemon（双脑）。

原理：agent.lock 文件 + OS 级独占字节锁（Windows msvcrt.locking / POSIX fcntl.flock）。
持有者进程死亡时 OS 自动释放锁 → 第二次 acquire 自然成功并接管（无需陈旧检测逻辑，
锁的存活性由操作系统保证，文件内容只是"展示牌"）。

使用：
    lock = InstanceLock(kind="daemon")
    ok, msg = lock.acquire()
    if not ok:
        print(msg); sys.exit(2)
    try:
        ...  # daemon 主循环
    finally:
        lock.release()

--task 等短生命周期进程**不要**独占锁（需与 daemon 并行），用 probe() 做提示性探测。
"""
import json
import os
import sys
import time

from core import config_loader

LOCK_FILENAME = "agent.lock"


def _lock_file(fd):
    """对 fd 的首字节加独占锁（非阻塞）。成功返回 True。"""
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


def _unlock_file(fd):
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # 释放失败也无妨——进程退出时 OS 会兜底


class InstanceLock:
    """单实例锁。kind 只是展示信息（谁持有），互斥对所有 kind 一视同仁。"""

    def __init__(self, root=None, kind="daemon"):
        self.root = root or config_loader.AGENT_ROOT
        self.kind = kind
        self.path = os.path.join(self.root, LOCK_FILENAME)
        self._fd = None

    # ---------- 获取 / 释放 ----------

    def acquire(self):
        """尝试获取。返回 (ok, message)；失败时 message 含当前持有者信息。"""
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            return False, "实例锁文件不可用（%s）: %s" % (self.path, e)
        if not _lock_file(fd):
            os.close(fd)
            info = self.holder()
            if info:
                return False, ("已有实例在运行（kind=%s PID=%s，自 %s）——同安装根只允许一个实例。\n"
                               "如确认是残留，先结束该进程或删除 %s"
                               % (info.get("kind"), info.get("pid"),
                                  info.get("started_at"), self.path))
            return False, "已有实例在运行（持有者信息不可读）——同安装根只允许一个实例。"
        self._fd = fd
        self._write_info()
        return True, "实例锁已获取（kind=%s PID=%d）" % (self.kind, os.getpid())

    def release(self):
        if self._fd is None:
            return
        _unlock_file(self._fd)
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        try:  # 正常退出时清掉展示牌；异常退出留给 OS 兜底
            info = self.holder()
            if info and info.get("pid") == os.getpid():
                os.remove(self.path)
        except OSError:
            pass

    # ---------- 信息 ----------

    def _write_info(self):
        """★展示牌写在 offset 1 起——Windows 的 msvcrt.locking 连"读"锁字节都会被拒，
        所以锁字节 0、JSON 从 1 开始，别的进程才能从 offset 1 读到持有者信息。"""
        info = {"pid": os.getpid(), "kind": self.kind,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            os.lseek(self._fd, 1, os.SEEK_SET)
            os.ftruncate(self._fd, 1)
            os.write(self._fd, b"\n" + json.dumps(info, ensure_ascii=False).encode("utf-8"))
        except OSError:
            pass  # 展示牌写失败不影响锁本身的有效性

    def holder(self):
        """读当前持有者信息（可能为 None：无文件或内容损坏）。
        ★必须从 offset 1 开始读——锁字节 0 对其他句柄是读取禁区。"""
        try:
            with open(self.path, "rb") as f:
                f.seek(1)
                return json.loads(f.read().decode("utf-8"))
        except (OSError, ValueError):
            return None

    def probe(self):
        """非阻塞探测：返回 (running, message)。供 --task 等并行场景提示用。"""
        if not os.path.exists(self.path):
            return False, "无运行中实例"
        info = self.holder()
        if not info:
            return False, "锁文件存在但信息不可读"
        return True, "实例运行中（kind=%s PID=%s，自 %s）" % (
            info.get("kind"), info.get("pid"), info.get("started_at"))

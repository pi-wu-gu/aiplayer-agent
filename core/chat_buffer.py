# -*- coding: utf-8 -*-
"""游戏聊天栏缓冲区（阶段3.5）。
桥接服务端累积全量聊天（GET /chat），agent 每次对话前只注入"新"消息；
已注入过的消息除非 agent 主动用 mc_chat 获取，否则不会二次进入对话。"""
import json
import os
import time

import requests

from core import config_loader


def _fmt_ts(t):
    try:
        lt = time.localtime(float(t))
    except (TypeError, ValueError):
        return "--:--:--"
    if time.strftime("%Y-%m-%d", lt) == time.strftime("%Y-%m-%d"):
        return time.strftime("%H:%M:%S", lt)          # 今天：只显示时刻，省 token
    return time.strftime("%m-%d %H:%M:%S", lt)        # 跨天：带日期，避免歧义


def format_entries(entries):
    """把聊天条目格式化为注入对话用的多行文本。"""
    return "\n".join("%s %s" % (_fmt_ts(e.get("t", 0)), e.get("text", "")) for e in entries)


class ChatBuffer:
    """agent 侧游标：记录"已注入到对话"的最新消息 id，新消息才注入。"""

    def __init__(self, cfg, cursor_path=None):
        self.base = (cfg.get("mc_bridge", {}).get("base_url")
                     or "http://127.0.0.1:8780").rstrip("/")
        self.cursor_path = cursor_path or os.path.join(
            config_loader.AGENT_ROOT, "logs", "chat_cursor.json")
        self.cursor = self._load_cursor()

    # ---------- 游标持久化 ----------
    def _load_cursor(self):
        try:
            with open(self.cursor_path, "r", encoding="utf-8") as f:
                return int(json.load(f).get("last_id", 0))
        except (OSError, ValueError):
            return 0

    def _save_cursor(self):
        try:
            os.makedirs(os.path.dirname(self.cursor_path), exist_ok=True)
            with open(self.cursor_path, "w", encoding="utf-8") as f:
                json.dump({"last_id": self.cursor, "updated": time.time()}, f)
        except OSError:
            pass

    # ---------- 桥接查询 ----------
    def _fetch(self, since=None, limit=200):
        try:
            r = requests.get(self.base + "/chat",
                             params={"since": since if since is not None else self.cursor,
                                     "limit": limit},
                             timeout=8)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            return {"entries": [], "total": 0, "latest_id": self.cursor}

    # ---------- 对外能力 ----------
    def bootstrap(self, n=8):
        """任务开始：取最近 n 条作为背景上下文返回，游标推到最新（之后的轮次只推新消息）。"""
        d = self._fetch(since=0, limit=max(n, 1))
        entries = d.get("entries") or []
        recent = entries[-n:]
        self.cursor = max(self.cursor, int(d.get("latest_id") or 0))
        self._save_cursor()
        return recent

    def drain(self, limit=15):
        """取回自上次注入以来的新消息并标记为已注入（推进游标）。
        超过 limit 条的旧新消息会被消费但不展示（提示用 mc_chat 补看）。"""
        d = self._fetch()
        entries = d.get("entries") or []
        if not entries:
            return []
        self.cursor = max(self.cursor, int(entries[-1].get("id") or 0))
        self._save_cursor()
        return entries[-limit:]

    def recent(self, n=30):
        """agent 主动获取：最近 n 条全量（不受自动注入去重约束）。"""
        d = self._fetch(since=0, limit=n)
        return d.get("entries") or []

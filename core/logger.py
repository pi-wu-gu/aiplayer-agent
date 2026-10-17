# -*- coding: utf-8 -*-
"""行为日志系统：记录模型行为（工具调用/LLM响应/护栏触发），JSONL+文本双格式，全部带时间戳。"""
import json
import os
import threading
import time

from core import config_loader


class AgentLogger:
    def __init__(self, agent_name="primary", stamp=None):
        self.agent_name = agent_name
        log_dir = os.path.join(config_loader.AGENT_ROOT, "logs")
        os.makedirs(log_dir, exist_ok=True)
        stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
        self.jsonl_path = os.path.join(log_dir, "behavior_%s.jsonl" % stamp)
        self.text_path = os.path.join(log_dir, "behavior_%s.log" % stamp)
        self.t0 = time.time()
        self._lock = threading.Lock()

    def log(self, event, **kw):
        now = time.time()
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + ".%03d" % int((now % 1) * 1000)
        rec = {"ts": ts, "rel_s": round(now - self.t0, 2), "agent": self.agent_name, "event": event}
        rec.update(kw)
        line_head = str(kw.get("result_head") or kw.get("content_head") or "")
        with self._lock:
            try:
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                with open(self.text_path, "a", encoding="utf-8") as f:
                    extra = "".join(" %s=%s" % (k, str(v)[:200]) for k, v in kw.items()
                                    if k not in ("result_head", "content_head"))
                    f.write("[%s +%.1fs] %s %s%s\n" % (
                        ts, rec["rel_s"], event, extra,
                        (" | " + line_head.replace("\n", " ")[:180]) if line_head else ""))
            except OSError:
                pass

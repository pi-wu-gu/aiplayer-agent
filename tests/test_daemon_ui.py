# -*- coding: utf-8 -*-
"""daemon UI 接口测试（/ui /feed /config，stub run_task 零 LLM 成本）。

验证：面板 HTML 可访问且含关键元素；行为流返回事件列表；
参数热更新立即生效+落盘；新实例从状态文件恢复参数。
运行: python tests/test_daemon_ui.py
"""
import json
import os
import sys
import tempfile
import threading
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import daemon as daemon_mod  # noqa: E402
from core import config_loader  # noqa: E402

BRIDGE = "http://127.0.0.1:8780"
PORT = 18771          # 测试用控制端口


def bridge_up():
    try:
        return requests.get(BRIDGE + "/state", timeout=3).json().get("loaded")
    except Exception:
        return False


def main():
    if not bridge_up():
        print("SKIP: 桥接不可达")
        return 0
    cfg = config_loader.full_load()
    state_path = os.path.join(tempfile.gettempdir(), "daemon_ui_test_state.json")
    if os.path.exists(state_path):
        os.remove(state_path)
    cfg["daemon"] = {
        "bot_names": ["muyiyangDeepseek"],
        "poll_interval": 2.0,
        "wake_interval": 3600,
        "review_every": 32,
        "control_port": PORT,
        "say_on_start": False,
        "run_cooldown": 0,
        "state_path": state_path,
    }

    started = threading.Event()
    release = threading.Event()

    def fake_run_task(cfg_, task, max_rounds=None, round_hook=None,
                      stop_event=None, verbose=True):
        started.set()
        release.wait(timeout=10)
        return "(stub)", {"rounds": 1}
    daemon_mod.run_task = fake_run_task

    d = daemon_mod.Daemon(cfg, verbose=False)
    th = threading.Thread(target=d.serve_forever, daemon=True)
    th.start()
    time.sleep(2.0)
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    base = "http://127.0.0.1:%d" % PORT

    # 1) UI 面板
    r = requests.get(base + "/", timeout=5)
    html = r.text
    check("GET / 返回HTML", r.status_code == 200 and "守护进程控制台" in html)
    check("UI含参数表单", all(k in html for k in
          ("poll_interval", "wake_interval", "review_every", "max_rounds_per_run")))
    check("UI含观察面板", "实时观察" in html and "工具调用" in html)
    check("UI含控制按钮", "立即唤醒" in html and "强制停止" in html)
    r2 = requests.get(base + "/ui", timeout=5)
    check("GET /ui 同面板", r2.status_code == 200)

    # 2) 行为流（今日有真实 behavior 日志）
    fd = requests.get(base + "/feed?n=50", timeout=5).json()
    check("GET /feed 返回事件列表", isinstance(fd.get("events"), list))
    check("feed有来源日志", bool(fd.get("source")))
    if fd["events"]:
        kinds = {e.get("kind") for e in fd["events"]}
        check("feed含思考或工具事件", bool(kinds & {"思考", "工具", "停止", "续跑"}))

    # 3) 参数读取
    c = requests.get(base + "/config", timeout=5).json()["config"]
    check("GET /config 含全部参数", all(k in c for k in
          ("poll_interval", "wake_interval", "review_every", "bot_names")))
    check("config初值来自yaml", c["poll_interval"] == 2.0 and c["review_every"] == 32)

    # 4) 热更新
    body = {"poll_interval": 0.8, "wake_interval": 120, "review_every": 16,
            "review_llm": True, "max_rounds_per_run": 40, "run_cooldown": 3,
            "backlog_max": 100, "wake_check_backlog": False,
            "bot_names": ["muyiyangDeepseek", "小深"], "mention_extra": ["帮手"]}
    resp = requests.post(base + "/config", json=body, timeout=5).json()
    check("POST /config 成功", resp.get("ok") is True)
    check("changed含poll_interval", "poll_interval" in resp.get("changed", {}))
    check("热生效: daemon.poll_interval=0.8", d.poll_interval == 0.8)
    check("热生效: daemon.review_every=16", d.review_every == 16)
    check("热生效: review_llm=True", d.review_llm is True)
    check("热生效: bot_names更新", d.bot_names == ["muyiyangDeepseek", "小深"])
    check("热生效: mention_extra更新", d.extra_mentions == ["帮手"])
    check("热生效: max_rounds_per_run→max_rounds=40", d.max_rounds == 40)
    c2 = requests.get(base + "/config", timeout=5).json()["config"]
    check("GET /config 反映新值", c2["poll_interval"] == 0.8 and c2["review_every"] == 16)
    check("GET /config 反映max_rounds_per_run=40", c2["max_rounds_per_run"] == 40)

    # 5) 持久化 + 新实例恢复
    with open(state_path, "r", encoding="utf-8") as f:
        st = json.load(f)
    check("参数已落盘", st.get("config", {}).get("poll_interval") == 0.8)
    d2 = daemon_mod.Daemon(cfg, verbose=False)     # 同 state_path → 恢复
    check("新实例恢复参数", d2.poll_interval == 0.8 and d2.review_every == 16
          and d2.bot_names == ["muyiyangDeepseek", "小深"] and d2.max_rounds == 40)
    check("新实例恢复mention_extra", d2.extra_mentions == ["帮手"])

    # 6) 唤醒/停止按钮走的还是老路由（回归）
    started.clear()
    r3 = requests.post(base + "/wake", timeout=5)
    check("wake 路由正常", r3.status_code == 200 and started.wait(timeout=5))
    release.set()
    time.sleep(1.0)

    d.stop()
    time.sleep(0.8)
    try:
        os.remove(state_path)
    except OSError:
        pass
    print("=" * 40)
    print("ALL PASS" if ok else "HAS FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

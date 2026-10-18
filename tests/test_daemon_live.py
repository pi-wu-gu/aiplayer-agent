# -*- coding: utf-8 -*-
"""daemon 实网集成冒烟（stub 掉 run_task 与一次聊天轮询，零 LLM 成本）。

验证：提及检测入账积压 → 主循环自动触发 mention 运行 → 运行中唤醒被门控(409)
→ /stop 强制停止 → 回 idle → 手动唤醒(无积压) → 停止后无运行 stop=409。
桥接不可达时自动 skip。
运行: python tests/test_daemon_live.py
"""
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
PORT = 18770          # 测试用控制端口（避开正式的8770）


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
    state_path = os.path.join(tempfile.gettempdir(), "daemon_test_state.json")
    if os.path.exists(state_path):
        os.remove(state_path)
    cfg["daemon"] = {
        "bot_names": ["ExampleBot"],
        "poll_interval": 0.5,
        "wake_interval": 3600,          # 测试不依赖定时唤醒
        "review_every": 32,
        "control_port": PORT,
        "say_on_start": False,
        "run_cooldown": 0,
        "backlog_max": 50,
        "state_path": state_path,
    }

    # ---- stub run_task：不调 LLM，只记录任务并模拟运行 ----
    runs = []
    started = threading.Event()
    release = threading.Event()

    # ★注意：这里的签名必须跟 core/runner.run_task 一致（trigger/stop_info 是后加的）。
    #   2026-10-18 曾因只修了 test_daemon_ui.py 的同类 stub、漏了这里，导致本测试长期 FAIL。
    def fake_run_task(cfg_, task, max_rounds=None, round_hook=None,
                      stop_event=None, verbose=True, trigger="manual", stop_info=None):
        runs.append({"task": task, "stop_event": stop_event, "trigger": trigger})
        started.set()
        release.wait(timeout=20)
        return "(stub 收尾)", {"rounds": 3}
    daemon_mod.run_task = fake_run_task

    d = daemon_mod.Daemon(cfg, verbose=True)
    th = threading.Thread(target=d.serve_forever, daemon=True)
    th.start()
    time.sleep(1.5)     # 等首轮轮询（游标种子）
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    st = requests.get("http://127.0.0.1:%d/status" % PORT, timeout=5).json()
    check("status: 初始 idle", st["state"] == "idle")

    # ---- 喂一条@提及 + 一条普通消息（只喂一次，之后恢复真实轮询） ----
    # ★2026-10-18 修正：@提及必须由**别人**发出（<PlayerB>），不能写成 <ExampleBot>——
    #   后者会被 is_mention 判为"bot 自己的发言"而排除（防自激），导致 runs 为空。
    #   这正是新规则"无发言人/自己发言不算提及"该有的行为，测试数据要跟真实场景一致。
    fake_entries = [
        {"id": 9001, "t": time.time(), "text": "[world] <PlayerB>: @ExampleBot 看看经验块"},
        {"id": 9002, "t": time.time(), "text": "[world] <PlayerB>: 今天天气不错"},
        {"id": 9003, "t": time.time(), "text": "[world] <ExampleBot>: [自动] 测试开始: 自己的发言"},
        {"id": 9004, "t": time.time(), "text": "ExampleBot is now sleeping. [1/3]"},
    ]
    real_fetch = d._fetch_chat
    fed = {"done": False}

    def stub_fetch(since, limit=100):
        if not fed["done"]:
            fed["done"] = True
            return {"entries": [e for e in fake_entries if e["id"] > since],
                    "latest_id": fake_entries[-1]["id"]}
        return real_fetch(since, limit)
    d._fetch_chat = stub_fetch

    # ---- 主循环应自动触发 mention 运行 ----
    check("mention: 自动触发运行", started.wait(timeout=8))
    time.sleep(0.3)
    check("mention: 任务含@提及内容", "@提及" in runs[-1]["task"]
          and "看看经验块" in runs[-1]["task"])
    check("mention: 触发源是提及而非自身发言",
          runs[-1]["task"].startswith("【游戏内@提及】"))
    st = requests.get("http://127.0.0.1:%d/status" % PORT, timeout=5).json()
    check("status: running", st["state"] == "running")

    # ---- 运行中唤醒 → 门控拦截 ----
    r2 = requests.post("http://127.0.0.1:%d/wake" % PORT, timeout=5)
    check("wake: 运行中返回409", r2.status_code == 409)
    check("gate: 未产生第二次运行", len(runs) == 1)

    # ---- 强制停止思考 ----
    r3 = requests.post("http://127.0.0.1:%d/stop" % PORT, timeout=5)
    check("stop: 返回200", r3.status_code == 200)
    check("stop: stop_event 已置位", runs[-1]["stop_event"].wait(timeout=5))
    release.set()
    time.sleep(1.5)
    st = requests.get("http://127.0.0.1:%d/status" % PORT, timeout=5).json()
    check("status: 停止后回 idle", st["state"] == "idle")
    check("backlog: 快照已被消费", st["backlog_n"] == 0)
    check("backlog: 无待处理提及", st["mention_pending"] is False)

    # ---- 空闲手动唤醒（无积压）→ 正常运行一次 ----
    started.clear()
    release.clear()
    r5 = requests.post("http://127.0.0.1:%d/wake" % PORT, timeout=5)
    check("wake: 空闲返回200", r5.status_code == 200)
    check("wake: 第二次运行启动", started.wait(timeout=8))
    check("task: 无积压说明", "无积压未读" in runs[-1]["task"])
    release.set()
    time.sleep(1.5)

    # ---- 无运行时 stop → 409 ----
    r4 = requests.post("http://127.0.0.1:%d/stop" % PORT, timeout=5)
    check("stop: 无运行时返回409", r4.status_code == 409)

    d.stop()
    time.sleep(1.0)
    try:
        os.remove(state_path)
    except OSError:
        pass
    print("=" * 40)
    print("ALL PASS" if ok else "HAS FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

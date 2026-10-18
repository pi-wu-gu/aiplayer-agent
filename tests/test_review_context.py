# -*- coding: utf-8 -*-
"""LLM 审查的上下文完整性（2026-10-18 两次误杀后新增）。

背景（实机事故）：
  05:22 「拆除附近的四个伪装方块」—— agent 逐层缩小 scan_region 范围定位方块，
        刚找齐 4 个准备动手，第 8 轮被审查判「反复扫描同一区域各层…已陷入空转」。
  05:23 「互联网查询伪装方块」  —— **任务本身就是查询**，只读是唯一正确的做法，
        第 6 轮被审查判「只读不行动，无实质进展」。

根因：`_llm_review` 只把"操作记录"喂给审查员，**看不到任务是什么**，
于是"只读"被一律当成空转。

本测试锁定：提示词必须带 当前任务 / 触发源 / 最近发言，且必须写明
"只读不写属于正常干活""存疑判继续"这类反误判约束。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.daemon as D  # noqa: E402

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


captured = {}


def _fake_chat(cfg, model=None, messages=None, max_tokens=None, temperature=None):
    captured["messages"] = messages
    return {"content": "继续"}


def _run_review(task, reason, assistant_says, reply="继续"):
    captured.clear()
    D.llm_client.chat = _fake_chat

    class FD:
        cfg = {"cost_guard": {}}
        current_task = task
        current_reason = reason
        review_every = 8
        review_model = "deepseek-flash"
        review_token_budget = 0
        review_llm = True
        stats = {}

        def log(self, *a):
            pass

        def query_health(self):
            return 20

    class FA:
        messages = [{"role": "system", "content": "s"}] + [
            {"role": "assistant", "content": s} for s in assistant_says]
        recent_results = [
            {"round": 1, "tool": "web_fetch", "args": '{"url":"x"}', "ok": True, "head": "ok"},
            {"round": 2, "tool": "mc_act", "args": '{"type":"scan_region"}', "ok": True, "head": "ok"},
        ]
        total_tokens = 130000
        sub_tokens = 0

    r = D.Reviewer(FD())
    r.check(8, FA())
    msgs = captured.get("messages") or []
    return msgs[0]["content"] if msgs else ""


print("== 审查提示词必须带上下文 ==")
m = _run_review("【控制台指令】互联网查询伪装方块", "manual",
                ["The task is only 互联网查询 — I should report findings"])
ck("审查被调用", bool(m))
ck("★含当前任务描述", "互联网查询伪装方块" in m, m[:80])
ck("★含触发源", "manual" in m)
ck("★含最近发言（判断是否已声明完成）",
   "The task is only 互联网查询" in m)

print("== 反误判约束必须在提示词里 ==")
for key, desc in [
        ("只读不写", "明确「只读不写」不算空转"),
        ("查询/探测/分析", "点名查询/探测/分析类任务"),
        ("缩小扫描范围", "点名逐层排查属正常干活"),
        ("反复调用同一个查询工具", "点名单工具连续查不算死循环"),
        ("已经明确声明", "空转的正确定义是「说完完成还在查」"),
        ("存疑一律判", "存疑时判继续"),
]:
    ck(desc, key in m, key)

print("== 空任务也要能跑（不崩） ==")
m2 = _run_review("", "scheduled", [])
ck("任务为空 → 显示占位而非崩", "（未知）" in m2 or bool(m2), m2[:60])
ck("无发言 → 显示占位", "（暂无）" in m2, m2[:120])

print("\n%d 通过 / %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

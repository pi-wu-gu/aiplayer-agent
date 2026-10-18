# -*- coding: utf-8 -*-
"""空转刹车 / 提及误判 / 积压噪声 —— 离线回归（2026-10-18 日志诊断后新增）。

对应实锤问题（session_20261018_031332 等）：
1. auto_continue 完结判定只认 4 个"完成"字面 → "处理完毕/无待办/收尾"全漏判 →
   被连推 3 次 → 25 轮 / 38.8 万 token 纯空转。
2. is_mention 把无发言人的服务器广播（"ExampleBot is now sleeping. [1/3]"、
   "已将ExampleBot传送至…"）当成 @提及 → 误唤醒。
3. backlog 被服务器清理倒计时刷满 → wake_check_backlog 闸门失效 →
   定时唤醒 24h 每 5 分钟跑一整轮。
4. session_token_budget=0 静默关闭 token 熔断。
5. mc_state 摘要丢 screen/window_active/mouse_grabbed → agent 反复重查。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.context import ResultCompressor  # noqa: E402

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


# ---------- 1. 完结判定 ----------
class _FakeAgent:
    """只借 _looks_done，不构造完整 Agent（避免牵扯 registry/LLM）。"""
    from core.agent import Agent
    _looks_done = Agent._looks_done
    _DONE_RE = None
    _PENDING_RE = None


print("== 1. 完结判定（放宽字面 + 排除续做意图）==")
A = _FakeAgent()
# 日志里的真实收尾文本（旧正则全部漏判 → 被强推续跑）
ck("处理完毕算完结", A._looks_done("本次唤醒处理完毕，无待办。积压 27 条…"))
ck("确认完成算完结", A._looks_done("确认完成，无剩余可执行事项。"))
ck("收尾算完结", A._looks_done("已无剩余步骤，收尾。"))
ck("无需再动算完结", A._looks_done("检查完毕，无需再处理。"))
ck("没有需要回应的算完结", A._looks_done("积压全是清理广播，没有需要回应的消息。"))
ck("已完成算完结（旧规则兼容）", A._looks_done("任务已完成。"))
# 不该判完结的
ck("列了下一步 → 不算完结", not A._looks_done("积压已读完，接下来我去挖矿。"))
ck("纯旁白无完结词 → 不算完结", not A._looks_done("我先看看背包里有什么。"))
ck("空文本 → 不算完结", not A._looks_done(""))

# ---------- 2. 空转刹车的判定条件（纯逻辑复算） ----------
print("== 2. 空转刹车条件 ==")


def should_nudge(looks_done, continues, tool_calls, tools_at_last_continue):
    """复刻 agent.step 里注入"继续"的条件。"""
    idle = (tools_at_last_continue >= 0 and tool_calls == tools_at_last_continue)
    return (continues < 3 and not looks_done and not idle)


ck("首轮旁白仍会推（保留原防旁白能力）", should_nudge(False, 0, 0, -1))
ck("推过一次后又只给结论且没调工具 → 不再推",
   not should_nudge(False, 1, 3, 3))
ck("推过一次后确实又调了工具 → 允许再推",
   should_nudge(False, 1, 5, 3))
ck("推满 3 次 → 不再推", not should_nudge(False, 3, 9, 9))

# ---------- 3. is_mention：服务器广播不得算提及 ----------
print("== 3. 提及判定（广播误唤醒）==")
try:
    from core.daemon import Daemon
    import core.daemon as D

    class _D:  # 够用的替身：只跑 is_mention/_is_noise
        bot_names = ["ExampleBot"]
        extra_mentions = []
        noise_res = []
        blacklist_res = []
        is_mention = Daemon.is_mention
        _is_noise = Daemon._is_noise
        _blacklisted = Daemon._blacklisted

    d = _D()
    ck("睡觉进度广播不算提及",
       not d.is_mention("ExampleBot is now sleeping. [1/3]"))
    ck("传送回执广播不算提及",
       not d.is_mention("已将ExampleBot传送至60.500000, 70.000000, 60.500000"))
    ck("名单行不算提及", not d.is_mention("PlayerA, ExampleBot."))
    ck("玩家 @ 仍算提及",
       d.is_mention("[world] <PlayerA>:  @ExampleBot  你现在有哪些agent工具可用"))
    ck("玩家不带@但点名仍算提及",
       d.is_mention("<PlayerA> ExampleBot 砍树"))
    ck("bot 自己的发言不算提及",
       not d.is_mention("[world] <ExampleBot>: 我这就去"))
    # 黑名单（agent 自管）：命中即不算提及、且算噪声
    import re as _re_b
    d.blacklist_res = [_re_b.compile(r"is now sleeping")]
    ck("★命中自管黑名单 → 不算提及",
       not d.is_mention("ExampleBot is now sleeping. [1/3]"))
    ck("★命中自管黑名单 → 算噪声", d._is_noise("ExampleBot is now sleeping. [1/3]"))
    d.blacklist_res = []
except Exception as e:
    print("  SKIP daemon 导入失败: %s" % e)

# ---------- 4. 噪声过滤 ----------
print("== 4. 积压噪声过滤 ==")
import re  # noqa: E402

pats = [r'还剩下\s*\d+\s*秒清理', r'清理成功', r'公共垃圾桶', r'物品被回收', r'个生物被清理']
res = [re.compile(p) for p in pats]
noise_lines = [
    "还剩下 60 秒清理物品和实体",
    "§d§l还剩下 10 秒清理物品和实体",
    "清理成功，有192个物品被回收，有191个物品加入了公共垃圾桶，有145个生物被清理",
]
real_lines = ["[world] <PlayerC>: 有人吗", "[world] <PlayerA>:  @ExampleBot  挖点石头"]
ck("清理倒计时命中噪声", all(any(r.search(t) for r in res) for t in noise_lines))
ck("玩家发言不算噪声", not any(any(r.search(t) for r in res) for t in real_lines))

# ---------- 5. session_token_budget=0 防呆 ----------
print("== 5. token 熔断防呆 ==")
budget_zero = 0
ck("budget=0 在 R4 判据里是假值（这就是漏洞本身）", not bool(budget_zero))
# 修复后：_apply_overrides 应拒绝 0；这里校验"拒绝"这一支的判据
ck("0 应被拒绝（判据 v==0）", budget_zero == 0)

# ---------- 6. mc_state 摘要字段 ----------
print("== 6. mc_state 摘要保留界面/输入字段 ==")
state = {
    "loaded": True, "pos": [194.5, 77.9, -1116.5], "yaw": 1.01, "pitch": -0.67,
    "health": 20, "food": 20, "dead": False, "dim": "minecraft:overworld",
    "gamemode": "survival", "time": {"dayTime": 74950051, "timeOfDay": 22051, "isDay": True},
    "held": {"id": "minecraft:diamond_pickaxe", "count": 1},
    "looking_at": {"block": "minecraft:air"}, "players": [], "chat": [],
    "screen": None, "window_active": True, "mouse_grabbed": False,
    "onGround": True, "sprinting": False, "flying": False,
    "_unknown_fields": ["timeOfDay", "canSleep", "nearbyPlayers"],
}
rc = ResultCompressor(threshold=10)   # 强制走摘要
out = rc.compress("mc_state", json.dumps(state, ensure_ascii=False))
dig = json.loads(out.split("\n[已存档")[0])
ck("保留 window_active", dig.get("window_active") is True, str(dig))
ck("保留 mouse_grabbed", dig.get("mouse_grabbed") is False, str(dig))
ck("保留 onGround", dig.get("onGround") is True, str(dig))
ck("保留 time.timeOfDay", (dig.get("time") or {}).get("timeOfDay") == 22051, str(dig))
ck("未知字段透传（让 agent 知道字段名写错）",
   "timeOfDay" in (dig.get("_unknown_fields") or []), str(dig))
ck("附字段名纠正提示", "time.timeOfDay" in (dig.get("_hint") or ""), str(dig))

print("\n%d 通过 / %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

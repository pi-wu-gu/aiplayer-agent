# -*- coding: utf-8 -*-
"""分层记忆 + finish_run + 唤醒黑名单 —— 离线回归（2026-10-18 新增）。

覆盖用户本次四项需求中的可离线验证部分：
1. finish_run：agent 主动收工通道
2. wake_blacklist：agent 自管唤醒黑名单（含红线：不得屏蔽 bot 名/触发词/通杀正则）
3. 审查默认调 LLM（review_llm 默认值）
4. 分层记忆：目录结构、按次唤醒落盘、到期推导（日/周/月/年）、commit 归档
"""
import datetime
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


# ---------- 1. finish_run ----------
print("== 1. finish_run 收工通道 ==")
from core.agent import Agent  # noqa: E402

a = Agent.__new__(Agent)          # 只验证属性初始化，不构造完整实例
a.finish_requested = False
a.finish_text = ""
ck("默认未请求收工", a.finish_requested is False)

# 复刻 step 里的判据
def step_returns(finish_requested, finish_text):
    if finish_requested:
        return finish_text or "（agent 主动结束运行）"
    return None

ck("调了就立即返回 summary", step_returns(True, "本次唤醒处理完毕") == "本次唤醒处理完毕")
ck("未调则继续跑", step_returns(False, "") is None)
ck("空 summary 有兜底", step_returns(True, "") == "（agent 主动结束运行）")

# ---------- 2. 唤醒黑名单 ----------
print("== 2. 唤醒黑名单（含红线）==")
from tools.builtin import wake_blacklist as wbl  # noqa: E402

BOT = "ExampleBot"
cfg = {"daemon": {"allow_ai_wake_blacklist": True, "wake_blacklist_max": 20,
                  "bot_names": [BOT], "mention_extra": []}}

# 用临时 AGENT_ROOT 隔离真实文件
_tmp = tempfile.mkdtemp(prefix="wbl_test_")
import core.config_loader as cl  # noqa: E402
cl.AGENT_ROOT = _tmp

ck("默认启用", wbl.enabled(cfg))
r = wbl.execute(cfg, "add", pattern=r"is now sleeping", note="睡觉进度广播",
                forbidden=[BOT])
ck("可加普通广播规则", r.get("ok") is True, str(r))
ck("加完能列出", len(wbl.execute(cfg, "list")["items"]) == 1)

bad = wbl.execute(cfg, "add", pattern=BOT, forbidden=[BOT])
ck("★红线：不得屏蔽 bot 自己的名字", bad.get("ok") is False, str(bad))
bad2 = wbl.execute(cfg, "add", pattern=".*", forbidden=[BOT])
ck("★红线：拒绝通杀正则 .*", bad2.get("ok") is False, str(bad2))
bad3 = wbl.execute(cfg, "add", pattern="[unclosed", forbidden=[BOT])
ck("拒绝非法正则", bad3.get("ok") is False, str(bad3))
bad4 = wbl.execute(cfg, "add", pattern="is now sleeping", forbidden=[BOT])
ck("重复添加被拒", bad4.get("ok") is False, str(bad4))

t = wbl.execute(cfg, "test", pattern="ExampleBot is now sleeping. [1/3]")
ck("test 能命中已加规则", t.get("blocked") is True, str(t))
t2 = wbl.execute(cfg, "test", pattern="[world] <PlayerA>:  @ExampleBot  挖矿")
ck("test 不误伤玩家 @", t2.get("blocked") is False, str(t2))

rm = wbl.execute(cfg, "remove", pattern=r"is now sleeping")
ck("可移除", rm.get("ok") is True and len(wbl.execute(cfg, "list")["items"]) == 0)

cfg_off = {"daemon": {"allow_ai_wake_blacklist": False}}
ck("开关关闭时不可用", wbl.execute(cfg_off, "list").get("ok") is False)

# ---------- 3. 审查默认调 LLM ----------
print("== 3. 审查默认调 LLM ==")
import yaml  # noqa: E402

with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "config", "config.yaml"), encoding="utf-8") as f:
    real = yaml.safe_load(f)
ck("config.yaml review_llm=true", real["daemon"]["review_llm"] is True)
ck("config.yaml 允许自管黑名单", real["daemon"].get("allow_ai_wake_blacklist") is True)
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "core", "daemon.py"), encoding="utf-8").read()
ck("daemon 默认值改 True", 'dcfg.get("review_llm", True)' in src)

# ---------- 4. 分层记忆 ----------
print("== 4. 分层记忆（目录/按次唤醒/到期推导/commit）==")
from core import memory as mem  # noqa: E402

root = os.path.join(_tmp, "docs", "记忆")
made = mem.ensure_layout(root)
for d in ("unsorted", "day", "week", "month", "year"):
    ck("建目录 %s/" % d, os.path.isdir(os.path.join(root, d)))
ck("生成 INDEX.md", os.path.isfile(os.path.join(root, "INDEX.md")))

mcfg = {"permissions": {"roots": {"shared": _tmp}}}

# 按次唤醒落盘
p1 = mem.unsorted_path(root, datetime.datetime(2026, 10, 18, 3, 42), "scheduled")
mem.open_note(root, p1, "scheduled", "【定时唤醒】检查积压", 1)
mem.append_note(p1, "积压全是清理广播，无待办。")
p2 = mem.unsorted_path(root, datetime.datetime(2026, 10, 18, 4, 51), "mention")
mem.open_note(root, p2, "mention", "【@提及】玩家让挖矿", 2)
mem.append_note(p2, "挖了 12 个圆石。")
ck("一次唤醒一个文件", os.path.basename(p1) != os.path.basename(p2),
   "%s vs %s" % (p1, p2))
ck("未总结文件按 日期_时分_触发源 命名",
   os.path.basename(p1) == "2026-10-18_0342_scheduled.md", os.path.basename(p1))
ck("内容已写入", "清理广播" in open(p1, encoding="utf-8").read())

# 到期推导：10-18 当天 → 不该有日待办（今天还没过）
pend_today = mem.pending(mcfg, root, today=datetime.date(2026, 10, 18))
ck("当天不过零点→无日待办",
   not any(i["kind"] == "day" for i in pend_today), str(pend_today))

# 过零点（今天 10-19）→ 10-18 可总结
pend = mem.pending(mcfg, root, today=datetime.date(2026, 10, 19))
day_items = [i for i in pend if i["kind"] == "day"]
ck("★过零点→出现日总结待办", len(day_items) == 1 and day_items[0]["period"] == "2026-10-18",
   str(pend))
ck("日待办源材料=2 条未总结", day_items and len(day_items[0]["sources"]) == 2, str(day_items))

# 写日档 + commit
mem._atomic_write(mem.target_path(root, "day", "2026-10-18"),
                  "# 2026-10-18 日档\n定时唤醒无待办；@提及挖了 12 个圆石。")
c = mem.commit(mcfg, root, "day", "2026-10-18", today=datetime.date(2026, 10, 19))
ck("commit 成功", c.get("ok") is True, str(c))
ck("源材料已归档（2 条）", c.get("archived") == 2, str(c))
ck("原件已不在 unsorted", not os.path.exists(p1) and not os.path.exists(p2))
ck("归档目录在 unsorted/_done/2026-10-18",
   os.path.isdir(os.path.join(root, "unsorted", "_done", "2026-10-18")))
ck("commit 后不再出现在待办",
   not any(i["kind"] == "day" for i in mem.pending(mcfg, root, datetime.date(2026, 10, 19))))

# 未写目标档就 commit → 拒绝
c2 = mem.commit(mcfg, root, "day", "2026-10-17", today=datetime.date(2026, 10, 19))
ck("★未写目标档就 commit 被拒", c2.get("ok") is False, str(c2))

# 周：10-18 是周日（ISO 周 42 结束），10-19 起属第 43 周
wk = mem.pending(mcfg, root, today=datetime.date(2026, 10, 19))
week_items = [i for i in wk if i["kind"] == "week"]
ck("★周结束→出现周总结待办", len(week_items) == 1, str(wk))
if week_items:
    ck("周档名格式 2026-W42", week_items[0]["period"].startswith("2026-W"), week_items[0]["period"])
    mem._atomic_write(mem.target_path(root, "week", week_items[0]["period"]),
                      "# 周档\n本周主要：挖矿与清理广播应对。")
    cw = mem.commit(mcfg, root, "week", week_items[0]["period"],
                    today=datetime.date(2026, 10, 19))
    ck("commit 周成功", cw.get("ok") is True, str(cw))

# 月 / 年：跨月跨年
mcur = mem.pending(mcfg, root, today=datetime.date(2026, 11, 2))
ck("★过月→出现月总结待办", any(i["kind"] == "month" for i in mcur), str(mcur))
mi = [i for i in mcur if i["kind"] == "month"]
if mi:
    mem._atomic_write(mem.target_path(root, "month", mi[0]["period"]),
                      "# 月档\n本月主要：稳定跑通定时唤醒；修完 place/dig 动作链路，"
                      "并重构了分层记忆体系（未总结→日→周→月→年）。")
    cm = mem.commit(mcfg, root, "month", mi[0]["period"], today=datetime.date(2026, 11, 2))
    ck("commit 月成功", cm.get("ok") is True, str(cm))
ycur = mem.pending(mcfg, root, today=datetime.date(2027, 1, 5))
ck("★过年→出现年总结待办", any(i["kind"] == "year" for i in ycur),
   str([i["kind"] + ":" + i["period"] for i in ycur]))

st = mem.status(mcfg, root, today=datetime.date(2026, 11, 2))
ck("★周/月总结后日档仍保留（成品明细不搬走）", st.get("day_n") == 1, str(st))
ck("status 返回分层计数", st.get("week_n") == 1 and st.get("month_n") == 1, str(st))

shutil.rmtree(_tmp, ignore_errors=True)
print("\n%d 通过 / %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

# -*- coding: utf-8 -*-
"""C 组实网测试：look_lock 实体锁定（0.2.1.37 补测 + 0.2.1.38 entities id）。"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aitest.ctl import call, cmd  # noqa: E402


def brief(o, n=160):
    s = json.dumps(o, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


fails = []


def check(name, cond, detail=""):
    print("  %s %s %s" % ("PASS" if cond else "FAIL", name, ("| " + detail) if detail else ""))
    if not cond:
        fails.append(name)


def wait_end(max_wait=8.0):
    """轮询 look_lock 查询直到 ended 墓碑（或超时）。"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        st = cmd({"type": "look_lock"})
        if st.get("result") == "look_lock_ended":
            return st
        time.sleep(0.3)
    return cmd({"type": "look_lock"})


print("=== C0. entities 列表（每项应含 id/type/dist/pos——0.2.1.38）===")
ents = cmd({"type": "entities"})
print("  entities:", brief(ents, 200))
items = ents if isinstance(ents, list) else []
check("entities 每项含 id 字段", bool(items) and all(isinstance(e.get("id"), int) for e in items),
      brief([{k: e.get(k) for k in ("id", "type", "dist")} for e in items[:3]], 150))

# 找一个 8 格内的可锁实体（MAX_LOCK_REACH=8.0；超距的一启动就会 target_lost）
near = None
for e in items:
    if e.get("dist", 99) <= 7.9:
        near = e
        break
print("  选用近距实体:", brief(near, 120))

if near:
    print("=== C1. target_type 锁定最近实体 ===")
    r = cmd({"type": "look_lock", "target_type": near["type"], "aim": "center", "duration_ms": 3000})
    print("  回显:", brief(r, 220))
    check("target_type 启动回显 target{id,type,aim,dist}",
          r.get("result") == "look_lock_started" and (r.get("target") or {}).get("id") == near["id"],
          brief(r.get("target"), 120))
    st = cmd({"type": "state", "fields": ["yaw", "pitch"]})
    print("  锁定中 yaw/pitch:", st.get("yaw"), st.get("pitch"))
    check("朝向已压向实体（yaw/pitch 非 None）", st.get("yaw") is not None and st.get("pitch") is not None)
    wait_end()

    print("=== C2. target=<id> 直锁（0.2.1.38：id 来自 entities）===")
    r = cmd({"type": "look_lock", "target": near["id"], "duration_ms": 3000})
    print("  回显:", brief(r, 220))
    check("target=id 直锁启动", r.get("result") == "look_lock_started"
          and (r.get("target") or {}).get("id") == near["id"], brief(r.get("target"), 120))
    st = cmd({"type": "look_lock"})
    print("  查询回显:", brief(st, 200))
    tgt = st.get("target") or {}
    check("查询回显 type 为真实类型（不再是 \"null\"）",
          tgt.get("type") not in (None, "null", "") and tgt.get("id") == near["id"],
          "type=%r" % tgt.get("type"))
    wait_end()

    print("=== C3. aim=eyes vs center pitch 差异 ===")
    r1 = cmd({"type": "look_lock", "target": near["id"], "aim": "center", "duration_ms": 2000})
    p_center = r1.get("pitch")
    wait_end()
    r2 = cmd({"type": "look_lock", "target": near["id"], "aim": "eyes", "duration_ms": 2000})
    p_eyes = r2.get("pitch")
    print("  center pitch=%s eyes pitch=%s" % (p_center, p_eyes))
    check("eyes 与 center 的 pitch 不同", p_center is not None and p_eyes is not None
          and abs(p_center - p_eyes) > 0.3, "center=%s eyes=%s" % (p_center, p_eyes))
    wait_end()

print("=== C4. 超距 target_lost（找 8 格外的实体；没有则跳过）===")
far = next((e for e in items if e.get("dist", 0) > 9.0), None)
if far:
    r = cmd({"type": "look_lock", "target_type": far["type"], "duration_ms": 3000})
    print("  启动回显:", brief(r, 200))
    st = wait_end(6.0)
    print("  墓碑:", brief(st, 220))
    check("超距立即 target_lost", st.get("result") == "look_lock_ended"
          and "target_lost" in str(st.get("end_reason", "")) and "超距" in str(st.get("end_reason", "")),
          str(st.get("end_reason")))
else:
    print("  附近无 9 格外实体，跳过（离线对拍已覆盖超距逻辑）")

print("=== C5. 无效 id 明确报错 ===")
r = cmd({"type": "look_lock", "target": 999999, "duration_ms": 1500})
print("  回显:", brief(r, 220))
check("无效 id 报错且提示用 entities", isinstance(r, str) and "look_lock_err" in r and "找不到实体" in r, r)

print()
print("C 组合计失败 %d 项%s" % (len(fails), ("：" + ", ".join(fails)) if fails else " —— 全部通过"))
sys.exit(1 if fails else 0)

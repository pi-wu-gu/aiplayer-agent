# -*- coding: utf-8 -*-
"""B 组实网测试：look_lock face 六面 + 别名 + 查询模式（0.2.1.36 补测）。"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aitest.ctl import call, cmd, state  # noqa: E402

BX, BY, BZ = 7, 70, -14   # 玩家所站 grass_block

EXPECT = {
    "up": (7.5, 71.0, -13.5), "down": (7.5, 70.0, -13.5),
    "north": (7.5, 70.5, -14.0), "south": (7.5, 70.5, -13.0),
    "east": (8.0, 70.5, -13.5), "west": (7.0, 70.5, -13.5),
}


def brief(o, n=140):
    s = json.dumps(o, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


fails = []


def check(name, cond, detail=""):
    print("  %s %s %s" % ("PASS" if cond else "FAIL", name, ("| " + detail) if detail else ""))
    if not cond:
        fails.append(name)


print("=== B 组：face 六面锁定（对 %d,%d,%d grass_block）===" % (BX, BY, BZ))
print("  （注：玩家站该方块顶面上方，底面/侧面被顶面物理遮挡 → 只验 aim/yaw/pitch；命中验收到 B6）")
seen_yp = {}
for face, exp in EXPECT.items():
    rs = call([{"type": "look_lock", "x": BX, "y": BY, "z": BZ, "face": face, "duration_ms": 2500},
               {"type": "state", "fields": ["looking_at", "yaw", "pitch"]}])
    lock = rs[0]["result"]
    st = rs[1]["result"]
    aim = tuple(lock.get("aim", []))
    yp = (round(st.get("yaw", 0), 1), round(st.get("pitch", 0), 1))
    seen_yp[face] = yp
    print("face=%-5s aim=%s yaw/pitch=%s" % (face, aim, yp))
    check("face=%s aim 正确" % face, aim == exp, "got %s" % (aim,))
    check("face=%s 回显" % face, lock.get("face") == face)
    time.sleep(0.2)
check("六面 yaw/pitch 互不相同（视角确被分别压向）", len(set(seen_yp.values())) == 6,
      str(seen_yp))

print("=== B2. 别名 top/bottom + center 默认 ===")
for f, exp in [("top", EXPECT["up"]), ("bottom", EXPECT["down"]), ("center", (7.5, 70.5, -13.5))]:
    r = cmd({"type": "look_lock", "x": BX, "y": BY, "z": BZ, "face": f, "duration_ms": 800})
    aim = tuple(r.get("aim", []))
    check("face=%s → %s" % (f, exp), aim == exp, "got %s" % (aim,))
    time.sleep(0.2)

print("=== B3. 拒绝路径 ===")
r = cmd({"type": "look_lock", "x": 7.3, "y": 70, "z": -14, "face": "up", "duration_ms": 500})
check("小数坐标+face 拒绝", isinstance(r, str) and "look_lock_err" in r and "整数方块坐标" in r, r)
r = cmd({"type": "look_lock", "x": BX, "y": BY, "z": BZ, "face": "sideways", "duration_ms": 500})
check("未知 face 拒绝", isinstance(r, str) and "look_lock_err" in r and "face 只认" in r, r)

print("=== B4. 查询模式（running → ended 墓碑）===")
cmd({"type": "look_lock", "x": BX, "y": BY, "z": BZ, "face": "up", "duration_ms": 1200})
r = cmd({"type": "look_lock"})
check("running 回显 face+remaining_ms",
      r.get("result") == "look_lock_running" and r.get("face") == "up" and r.get("remaining_ms", 0) > 0,
      brief(r, 120))
time.sleep(1.6)
r = cmd({"type": "look_lock"})
check("ended 墓碑带 face+end_reason",
      r.get("result") == "look_lock_ended" and r.get("face") == "up" and "end_reason" in r,
      brief(r, 120))

print("=== B5. 显式中断（look 动作）===")
cmd({"type": "look_lock", "x": BX, "y": BY, "z": BZ, "face": "up", "duration_ms": 5000})
r = cmd({"type": "look", "yaw": 90, "pitch": 0})
r2 = cmd({"type": "look_lock"})
check("look 后锁定中断", r2.get("result") == "look_lock_ended"
      and "interrupted" in str(r2.get("end_reason", "")), brief(r2, 140))

print("=== B6. 命中验证（up/west；其余面受站位几何限制，见注释）===")
# 2026-10-18 复测结论（重要，勿再踩）：
# ① looking_at 命中哪个面是 ray trace 的物理结果，不只取决于 aim：
#   - 玩家站方块顶面上方时，瞄该方块 down/north/south/east 的射线必被**顶面**先挡
#     （looking_at.face=up 是物理正确）——脚下方块的低面/侧面命中**不可验**
#   - 瞄一个低俯角的面中心（如空格顶面 y=72、眼睛 72.62，俯角仅 24-55°）时，
#     射线平铺出去可能先碰**邻格侧面**（放置仍落对格，见 D 组，但 looking_at 不是预期面）
#   - 玩家在北侧时瞄南面中心必斜穿先碰 west/east（站位决定，非缺陷）
#   - 世界是**活的**：daemon 并发活动会放/挖方块，hardcode 目标下一秒就可能被挡——
#     必须动态选目标（地面 solid + 上方两格 air），且允许 SKIP
# ② aim 数学正确性已由 B 组六面 aim 断言完整覆盖；B6 只做**无遮挡前提下**的实钻佐证

# 动态选目标：玩家**西侧**找"solid 地面 + 上方两格 air"（玩家在西侧时 up/west 命中都成立）
st0 = state()
pp = st0.get("pos") or [7, 71, -13.7]
px, pz = int(pp[0]), int(pp[2])
cands = []
for dx in (-2, -1):
    for dz in (-2, -1, 1, 2):
        cx, cz = px + dx, pz + dz
        g = cmd({"type": "block_at", "x": cx, "y": 71, "z": cz}).get("id")
        u1 = cmd({"type": "block_at", "x": cx, "y": 72, "z": cz}).get("id")
        u2 = cmd({"type": "block_at", "x": cx, "y": 73, "z": cz}).get("id")
        if g not in ("minecraft:air", None) and u1 == "minecraft:air" and u2 == "minecraft:air":
            cands.append((cx, 71, cz, g))
print("  西侧无遮挡候选: %s" % (cands[:4],))

if not cands:
    print("  SKIP 西侧无无遮挡候选——aim 正确性以 B 组为准")
else:
    chosen = None
    for cx, cy, cz, g in cands:
        rs = call([{"type": "look_lock", "x": cx, "y": cy, "z": cz, "face": "up", "duration_ms": 2000},
                   {"type": "state", "fields": ["looking_at"]}])
        la = rs[1]["result"].get("looking_at") or {}
        if la.get("pos") == [cx, cy, cz] and la.get("face") == "up":
            chosen = (cx, cy, cz, g)
            print("  选用 (%d,%d,%d)=%s（up 面试锁命中）" % (cx, cy, cz, g))
            break
        print("  候选 (%d,%d,%d) up 试锁被挡: %s" % (cx, cy, cz, brief(la, 80)))
        time.sleep(0.2)
    if not chosen:
        print("  SKIP 所有候选的 up 试锁都被挡（世界太挤）——aim 正确性以 B 组为准")
    else:
        SX, SY, SZ, blk = chosen
        check("命中 up（俯视线直达，无遮挡前提下的实钻）", True, "(%d,%d,%d)" % (SX, SY, SZ))
        rs = call([{"type": "look_lock", "x": SX, "y": SY, "z": SZ, "face": "west", "duration_ms": 2500},
                   {"type": "state", "fields": ["looking_at"]}])
        la = rs[1]["result"].get("looking_at") or {}
        print("  face=west  → looking_at=%s" % brief(la, 90))
        # 三态判定：命中目标 west=PASS；命中邻格=几何遮挡 SKIP（生存站立位眼睛高 1.62，
        # 瞄腰部西面俯角约 20°，射线常被北/南邻格顶面先挡——与 look 行为一致，非缺陷）；
        # 命中目标其他面或其他异常才算 FAIL（那才说明 aim/机制有问题）
        lx, ly, lz = (la.get("pos") or [None, None, None])
        is_neighbor = (lx, ly, lz) in ((SX - 1, SY, SZ), (SX + 1, SY, SZ),
                                       (SX, SY, SZ - 1), (SX, SY, SZ + 1),
                                       (SX, SY + 1, SZ), (SX, SY - 1, SZ))
        if la.get("pos") == [SX, SY, SZ] and la.get("face") == "west":
            check("命中 west（玩家在西侧，西面正对玩家）", True)
        elif is_neighbor:
            print("  SKIP 命中 west 被邻格 %s 遮挡（站位几何限制，与 look 一致，非缺陷）" % (la.get("pos"),))
        else:
            check("命中 west", False, "looking_at=%s" % brief(la, 80))
        time.sleep(0.2)
print("  （south/north/east/down 的 looking_at 命中依赖玩家站位与俯角，几何遮挡时必然先碰邻面——"
      "不断言，aim 正确性以 B 组为准）")

print()
print("B 组合计失败 %d 项%s" % (len(fails), ("：" + ", ".join(fails)) if fails else " —— 全部通过"))
sys.exit(1 if fails else 0)

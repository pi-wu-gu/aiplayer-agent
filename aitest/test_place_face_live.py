# -*- coding: utf-8 -*-
"""D 组实网测试（0.2.1.39 重写）：place 放置动作（API 路径 useItemOn）。

重写背景（2026-10-17/18 实锤）：旧版用 look_lock face=up + keys key.use——
按键路径在窗口非前台时完全失效（use 依赖的 mc.hitResult 与 looking_at 的
p.pick 自算不是一套）。0.2.1.39 起放置改走 place 动作（发包路径）。

本版用例：
- P0 碰撞箱预检（用户 2026-10-18 提醒）：对玩家自身占位的空气格发 place →
  期望 place_err「目标格与玩家碰撞箱重叠」（回传双方 AABB）
- P1 正常放置：动态选「空气 + 下方实体 + 不压玩家 AABB + 距离可达」的格 →
  place → place_sent → 轮询 place_status 至 place_placed（附 landed）→ block_at 复核
- P2 side= 指定支撑面（侧墙）——无合适候选则 SKIP
- P3 收尾：把落块 dig 回去（尽力而为，清理失败不计失败）
"""
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aitest.ctl import call, cmd  # noqa: E402


def brief(o, n=150):
    s = json.dumps(o, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


fails = []
skips = []


def check(name, cond, detail=""):
    print("  %s %s %s" % ("PASS" if cond else "FAIL", name, ("| " + detail) if detail else ""))
    if not cond:
        fails.append(name)


def skip(name, why):
    print("  SKIP %s | %s" % (name, why))
    skips.append(name)


def state(*fields):
    return cmd({"type": "state", "fields": list(fields)})


def block_id(x, y, z):
    return cmd({"type": "block_at", "x": x, "y": y, "z": z}).get("id")


def held_item():
    st = state("held")
    h = st.get("held") or {}
    return h.get("id"), h.get("count", 0)


def player_box(pos):
    """玩家 AABB（脚底中心 pos，宽 0.6 高 1.8）。返回 (min, max) 两个三元组。"""
    px, py, pz = pos
    return (px - 0.3, py, pz - 0.3), (px + 0.3, py + 1.8, pz + 0.3)


def overlaps_player(bx, by, bz, pos):
    """目标格整格 AABB 是否与玩家碰撞箱相交（与服务端实体碰撞校验同语义）。"""
    (mnx, mny, mnz), (mxx, mxy, mxz) = player_box(pos)
    return mnx < bx + 1 and bx < mxx and mny < by + 1 and by < mxy and mnz < bz + 1 and bz < mxz


def placeable(item_id):
    i = str(item_id or "")
    return any(k in i for k in ("dirt", "cobblestone", "stone", "planks", "andesite",
                                "deepslate", "gravel", "sand", "block"))


def ensure_holding_block():
    """手持可放置方块；不是则从背包选一个（best-effort）。"""
    item, _ = held_item()
    if placeable(item):
        return True
    inv = cmd({"type": "get_inventory"})
    for it in (inv.get("items") or []):
        if 0 <= it.get("slot", -1) <= 8 and placeable(it.get("id")):
            cmd({"type": "select", "slot": it["slot"]})
            time.sleep(0.3)
            item, _ = held_item()
            if placeable(item):
                return True
    return False


def pick_place_target(pos, want_wall=False):
    """动态选放置目标（避开玩家碰撞箱 + 距离可达 + 支撑面非交互）。

    want_wall=False：空气格且下方是地面类实体（place 自动选下方地板支撑）。
    want_wall=True ：空气格且某一水平邻格是地面类实体（place side= 指定该面的反面）。
    返回 (target, side_or_None) 或 (None, None)。

    2026-10-18 环境适配：当前平台 y=77 摆满交互家具（床/背包/工作台/waystone/
    copycat…），结构层是 y=76 平滑石。ground_kw 命中后还要过一遍 bad_kw
    （交互/BE 类），否则会选中 copycat 之类被模组正确拒绝的支撑面。
    """
    px, py, pz = (int(px) for px in pos)
    ground_kw = ("dirt", "grass", "stone", "andesite", "cobblestone", "deepslate",
                 "gravel", "sand", "planks")
    bad_kw = ("copycat", "backpack", "seat", "chair", "sign", "nixie", "bed",
              "waystone", "crate", "barrel", "table", "crank", "furnace",
              "terminal", "lantern", "froglight")
    cands = []
    for dy in (0, 1, 2):  # 先试脚底同层（平台结构层在 y=76，空气在 y=77）
        for r in range(1, 5):
            for dx in range(-r, r + 1):
                for dz in range(-r, r + 1):
                    if max(abs(dx), abs(dz)) != r:
                        continue
                    cands.append((px + dx, py + dy, pz + dz))
    for (tx, ty, tz) in cands:
        if block_id(tx, ty, tz) != "minecraft:air":
            continue
        if overlaps_player(tx, ty, tz, pos):
            continue
        if want_wall:
            for side, (ox, oy, oz) in (("south", (0, 0, -1)), ("north", (0, 0, 1)),
                                       ("east", (-1, 0, 0)), ("west", (1, 0, 0))):
                nb = str(block_id(tx + ox, ty + oy, tz + oz) or "")
                if (nb != "minecraft:air" and any(k in nb for k in ground_kw)
                        and not any(k in nb for k in bad_kw)):
                    dist = math.dist((tx, ty, tz), pos)
                    if dist <= 5.0:
                        return (tx, ty, tz), side
        else:
            below = str(block_id(tx, ty - 1, tz) or "")
            if (below != "minecraft:air" and any(k in below for k in ground_kw)
                    and not any(k in below for k in bad_kw)):
                dist = math.dist((tx + 0.5, ty - 1, tz + 0.5), pos)
                if dist <= 5.0:
                    return (tx, ty, tz), None
    return None, None


print("=== D 组：place 放置动作（0.2.1.39） ===")
st = state("pos", "held", "screen")
print("  玩家: pos=%s held=%s screen=%s" % (st.get("pos"), (st.get("held") or {}).get("id"),
                                            bool(st.get("screen"))))
if st.get("screen"):
    print("  有关界面开着，先 close_screen")
    cmd({"type": "close_screen"})
    time.sleep(0.5)

if not ensure_holding_block():
    print("FAIL 前置：手持非方块且背包里找不到可放置方块——无法测放置")
    sys.exit(1)
item, count = held_item()
print("  手持: %s x%d" % (item, count))
pos = state("pos").get("pos")
print("  脚底: %s" % pos)

# ---------------- P0：碰撞箱预检（用户 2026-10-18 提醒的核心用例） ----------------
print("--- P0：玩家碰撞箱预检 ---")
px, py, pz = pos
p0_target = None
for (bx, by, bz) in ((math.floor(px), math.floor(py), math.floor(pz)),       # 脚
                     (math.floor(px), math.floor(py + 0.9), math.floor(pz)),  # 腰
                     (math.floor(px), math.floor(py + 1.62), math.floor(pz))):  # 头
    if block_id(bx, by, bz) == "minecraft:air":
        p0_target = (bx, by, bz)
        break
if p0_target is None:
    skip("P0 碰撞箱预检", "玩家占位的脚/腰/头三格都不是空气，构造不出重叠目标")
else:
    print("  对玩家自身占位格 %s 发 place（期望被碰撞箱预检拦下）" % (p0_target,))
    r = cmd({"type": "place", "x": p0_target[0], "y": p0_target[1], "z": p0_target[2]})
    txt = json.dumps(r, ensure_ascii=False)
    ok = ("碰撞箱" in txt) and (r.get("ok") is False)
    check("P0 目标格与玩家碰撞箱重叠被拒（带 AABB 数值）", ok, brief(txt, 200))

# ---------------- P1：正常放置 + place_status 复核 ----------------
print("--- P1：正常放置（自动选支撑面） ---")
target, side = pick_place_target(pos)
if target is None:
    skip("P1 正常放置", "附近找不到「空气+下方地面+不压玩家+距离可达」的格")
else:
    tx, ty, tz = target
    print("  目标格: %s（下方=%s，与玩家重叠=%s）" % (target, block_id(tx, ty - 1, tz),
                                                    overlaps_player(tx, ty, tz, pos)))
    r = cmd({"type": "place", "x": tx, "y": ty, "z": tz})
    print("  place:", brief(r, 220))
    sent = (r.get("result") == "place_sent")
    check("place 返回 place_sent", sent, brief(r, 160))
    # 轮询 place_status 至终态（最多 ~4s）
    verdict, landed = None, None
    for _ in range(20):
        time.sleep(0.2)
        ps = cmd({"type": "place_status"})
        res = ps.get("result")
        if res == "place_pending":
            continue
        verdict, landed = res, ps
        break
    print("  place_status:", brief(landed, 220))
    check("place_status 确认 placed", verdict == "place_placed",
          "verdict=%s" % verdict)
    if verdict == "place_placed":
        check("landed 与手持一致", str(landed.get("landed")) == str(item),
              "landed=%s held=%s" % (landed.get("landed"), item))
    time.sleep(0.3)
    b2 = block_id(tx, ty, tz)
    print("  block_at 复核 (%d,%d,%d)=%s" % (tx, ty, tz, b2))
    check("block_at 复核落块", b2 not in ("minecraft:air", None), "block=%s" % b2)
    # ---------------- P3：清理（dig 回去，尽力而为） ----------------
    print("--- P3：清理（dig 回落块，尽力而为） ---")
    if b2 not in ("minecraft:air", None):
        cmd({"type": "select", "slot": 3})  # 切回镐再挖
        time.sleep(0.3)
        dr = cmd({"type": "dig", "x": tx, "y": ty, "z": tz, "timeout_ms": 8000})
        for _ in range(30):
            time.sleep(0.3)
            ds = cmd({"type": "dig_status"})
            if ds.get("result") in ("dig_broken", "dig_timeout", "dig_failed", "dig_cancelled"):
                print("  清理 dig: %s" % brief(ds, 160))
                break
        else:
            print("  清理 dig 未在预期时间内终态（不判失败）")

# ---------------- P2：side= 指定支撑面 ----------------
print("--- P2：side= 指定支撑面（侧墙） ---")
st2 = state("pos")
pos2 = st2.get("pos") or pos
target2, side2 = pick_place_target(pos2, want_wall=True)
if target2 is None:
    # 环境没有侧墙候选时，退而求其次：对地板显式 side=up（仍走显式支撑面代码路径）
    target2, _ = pick_place_target(pos2)
    if target2 is None:
        skip("P2 side= 指定", "附近找不到合适的空气格+地面支撑")
    else:
        side2 = "up"
        print("  无侧墙候选，退化为对地板显式 side=up")
if target2 is not None:
    tx, ty, tz = target2
    print("  目标格: %s side=%s（下方=%s）" % (target2, side2, block_id(tx, ty - 1, tz)))
    if not ensure_holding_block():  # P3 清理切回了镐，先拿回方块
        print("FAIL 前置：P2 前无法拿回可放置方块")
        fails.append("P2 前置：拿回方块")
        target2 = None
if target2 is not None:
    tx, ty, tz = target2
    r = cmd({"type": "place", "x": tx, "y": ty, "z": tz, "side": side2})
    print("  place:", brief(r, 220))
    check("place side= 返回 place_sent", r.get("result") == "place_sent", brief(r, 160))
    verdict = None
    for _ in range(20):
        time.sleep(0.2)
        ps = cmd({"type": "place_status"})
        if ps.get("result") != "place_pending":
            verdict = ps
            break
    print("  place_status:", brief(verdict, 220))
    check("side= 放置确认 placed", verdict and verdict.get("result") == "place_placed",
          "verdict=%s" % (verdict or {}).get("result"))
    if verdict and verdict.get("result") == "place_placed":
        b3 = block_id(tx, ty, tz)
        print("  block_at 复核 (%d,%d,%d)=%s" % (tx, ty, tz, b3))
        check("side= block_at 复核落块", b3 not in ("minecraft:air", None), "block=%s" % b3)
        # 清理：先切回镐（dig 需要合适工具），再 dig
        cmd({"type": "select", "slot": 3})
        time.sleep(0.3)
        cmd({"type": "dig", "x": tx, "y": ty, "z": tz, "timeout_ms": 8000})
        for _ in range(30):
            time.sleep(0.3)
            if cmd({"type": "dig_status"}).get("result") in (
                    "dig_broken", "dig_timeout", "dig_failed", "dig_cancelled"):
                break

print()
print("D 组合计：失败 %d 项，跳过 %d 项%s" % (
    len(fails), len(skips),
    ("；跳过：" + ", ".join(skips)) if skips else ""))
if fails:
    print("失败项：" + ", ".join(fails))
print("结论：" + ("全部通过" if not fails else "存在失败"))
sys.exit(1 if fails else 0)

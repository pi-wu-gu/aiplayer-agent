# -*- coding: utf-8 -*-
"""修复生效实测（2026-10-16）：位移校验 + 未知字段提示 + 事故场景 E2E。

三项验证：
  1. mc_state 传不存在字段（screen）→ 应回 _unknown_fields + _hint（不再静默丢弃）
  2. 正常移动 → 应无 _warn（无误报）
  3. 事故场景 E2E：开物品栏 → move → 应检出零位移并附 _warn 指引 → close_screen → 恢复
"""
import json
import sys
import time

try:/n    from aitest._localpaths import SIDECAR      # 本机路径（不入库）
except ImportError:                              # 未配置 _localpaths → 占位
    SIDECAR = r"aiplayer-agent/0.2.1.12"
sys.path.insert(0, SIDECAR)

import requests  # noqa: E402
import tools.builtin.mc_bridge as mb  # noqa: E402

cfg = {"mc_bridge": {"base_url": "http://127.0.0.1:8780"}}
BASE = "http://127.0.0.1:8780"


def st():
    return requests.get(BASE + "/state", timeout=5).json()


print("== 0. 前置状态 ==")
s = st()
print("pos:", s.get("pos"), "| loaded:", s.get("loaded"),
      "| last_action:", str(s.get("last_action"))[:60])

print("\n== 1. mc_state 未知字段提示 ==")
out = mb.mc_state(cfg, fields=["pos", "screen"])
print(json.dumps(out, ensure_ascii=False)[:400])
assert "_unknown_fields" in out and "screen" in out["_unknown_fields"], \
    "FAIL: 未知字段未报回"
assert "_hint" in out and "get_screen" in out["_hint"], "FAIL: 缺少排查提示"
print("PASS: _unknown_fields + _hint 正常返回")

tools = dict((m["name"], fn) for m, fn in mb.make_mc_tools(cfg))
act = tools["mc_act"]

print("\n== 2. 正常移动：应无 _warn ==")
before = st().get("pos")
out = act(type="move", dir="forward", duration=1000)
print(json.dumps(out, ensure_ascii=False)[:300])
assert out.get("sent") is True, "FAIL: move 未 sent"
assert "_warn" not in out, "FAIL: 正常移动误报 _warn"
after = st().get("pos")
d = mb._pos_dist(before, after)
print("pos %s -> %s (dist %.2f)" % (before, after, d))
assert d > 0.5, "FAIL: 1s 移动位移不足（环境异常？）"
print("PASS: 1s 正常位移不告警")

print("\n== 2b. 短 move（400ms）信息性检查：输入起步延迟边界 ==")
before = st().get("pos")
out = act(type="move", dir="forward", duration=400)
after = st().get("pos")
d = mb._pos_dist(before, after)
warned = "_warn" in out
print("400ms move -> dist %.2f, _warn=%s" % (d, warned))
if warned:
    print("NOTE: 400ms 短 move 踩中起步延迟（已知边界）；按新 _warn 指引换 1000ms 重试：")
    before = st().get("pos")
    out2 = act(type="move", dir="forward", duration=1000)
    after = st().get("pos")
    d2 = mb._pos_dist(before, after)
    print("1000ms 重试 -> dist %.2f, _warn=%s" % (d2, "_warn" in out2))
    assert "_warn" not in out2 and d2 > 0.5, "FAIL: 加长重试仍失败"
    print("PASS: 加长重试恢复（_warn 指引有效）")
else:
    print("PASS: 本次 400ms 也正常（dist %.2f）" % d)

print("\n== 3. 事故场景 E2E：开物品栏 → move 应报 _warn → close_screen ==")
r = act(type="press", key="key.keyboard.e")
print("press E ->", json.dumps(r, ensure_ascii=False)[:200])
time.sleep(1.0)
scr = act(type="get_screen")
res = scr.get("result") if isinstance(scr.get("result"), dict) else {}
print("get_screen keys:", list(res.keys())[:10], "| screen:", res.get("screen"))
has_ui = bool(res.get("slots") or res.get("widgets") or res.get("screen"))
assert has_ui, "FAIL: 按 E 后未检出界面（按键名或环境问题，需人工看）"
print("界面已确认打开")
out = act(type="move", dir="forward", duration=400)
print("move(界面开着) ->", json.dumps(out, ensure_ascii=False)[:600])
assert "_warn" in out and out.get("_displacement") == 0.0, \
    "FAIL: 界面开着时未检出零位移"
assert "get_screen" in out["_warn"] and "close_screen" in out["_warn"], \
    "FAIL: _warn 缺排查指引"
print("PASS: 零位移检出 + _warn 指引完整")
r = act(type="close_screen")
print("close_screen ->", json.dumps(r, ensure_ascii=False)[:200])

print("\n== 4. 关界面后恢复：再 move 应无 _warn ==")
time.sleep(0.5)
out = act(type="move", dir="forward", duration=1000)
assert "_warn" not in out, "FAIL: 关界面后仍误报"
print("PASS: close_screen 后移动恢复，无误报")

print("\n全部通过 ✅")

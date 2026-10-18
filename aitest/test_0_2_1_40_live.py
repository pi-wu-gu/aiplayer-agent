#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""0.2.1.40 实网验收：失败判定修复 + 新 bundle 的四个模块。

跑法（客户端已进游戏后）：
    python aitest/test_0_2_1_40_live.py

覆盖：
  A. 基础：版本 / health / 动作数 / 权限回显
  B. ★失败判定（本次修复的核心）：三种失败形态必须被判 ok=false
       move 缺 dir      → move_err:
       file_read 越界   → file_err:
       不存在的动作名   → unknown:
     对照：一个正常动作必须 ok=true
  C. /stats 计数：上面三次失败应让 command_errors +3（重启后从 0 起算）
  D. bundle 解压：新版本目录内 memory/wake_blacklist/file_ops/approval 四个模块存在
  E. 提示词：实例内 PRIMARY_PROMPT 含新工具与 shared: 语法
  F. 只读动作抽查（不改变世界）
"""
import json
import os
import sys
import time

BASE = "http://127.0.0.1:8780"
AGENT_BASE = "http://127.0.0.1:8770"
SIDECAR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "AI", "game", "aiplayer-agent", "0.2.1.40"))

_ok = 0
_fail = 0


def ck(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  ok   %s" % name)
    else:
        _fail += 1
        print("  FAIL %s %s" % (name, ("| " + str(detail)[:150]) if detail else ""))


def _opener():
    import urllib.request
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path, base=BASE, timeout=40):
    o = _opener()
    return json.loads(o.open(base + path, timeout=timeout).read())


def post(path, body, base=BASE, timeout=45):
    import urllib.request
    o = _opener()
    req = urllib.request.Request(base + path,
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(o.open(req, timeout=timeout).read())


def cmd(action):
    """发单个动作，返回 (ok字段, result字符串)。"""
    r = post("/command", {"commands": [action]})
    res = (r.get("results") or [{}])[0]
    return r, res


def main():
    print("== A. 基础 ==")
    h = get("/health")
    ck("/health ok", h.get("ok") is True, h)
    acts = get("/actions")
    n = acts.get("count")
    perm = acts.get("permissions") or {}
    print("     count=%s  fileMode=%s  gameActionMode=%s  denied=%s"
          % (n, perm.get("fileMode"), perm.get("gameActionMode"), perm.get("deniedCount")))
    ck("动作数=102（full 档，selfToolEnabled=false）", n == 102, n)

    print("== B. ★失败判定（0.2.1.40 核心修复） ==")
    r0 = cmd({"type": "ping"})
    ck("对照组 ping 判 ok=true", r0[1].get("ok") is True, r0[0])

    before = get("/stats")
    err0 = before.get("command_errors", 0)

    r1 = cmd({"type": "move"})                       # 缺 dir → move_err
    ck("move 缺 dir → 外层 ok=false", r1[1].get("ok") is False, r1[1])
    ck("  result 是 move_err 字符串", str(r1[1].get("result", "")).startswith("move_err"), r1[1].get("result"))

    r2 = cmd({"type": "file_read", "path": "../../../windows/win.ini"})
    ck("file_read 越界 → 外层 ok=false", r2[1].get("ok") is False, r2[1])
    # 越界走的是异常路径：结果以 "error: java.lang.IllegalStateException: 路径越界…" 返回，
    # 普通文件错误（文件不存在等）才是 "file_err: …"。两者都应该被判失败，别只认后者。
    _r2 = str(r2[1].get("result", ""))
    ck("  result 是 file_err / error 前缀",
       _r2.startswith("file_err") or _r2.startswith("error:"), _r2[:120])

    r3 = cmd({"type": "no_such_action_xyz"})
    ck("未知动作 → 外层 ok=false", r3[1].get("ok") is False, r3[1])
    ck("  result 是 unknown: 前缀", str(r3[1].get("result", "")).startswith("unknown:"), r3[1].get("result"))

    r4 = cmd({"type": "look", "x": 0, "y": 64, "z": 0})
    ck("对照组 look 判 ok=true", r4[1].get("ok") is True, r4[1])

    print("== C. /stats 计数 ==")
    time.sleep(0.5)
    after = get("/stats")
    err1 = after.get("command_errors", 0)
    print("     before=%s after=%s" % (err0, err1))
    ck("三次失败使 command_errors +3", err1 - err0 >= 3, "%s→%s" % (err0, err1))

    print("== D. bundle 解压到新版本目录 ==")
    ck("实例目录 0.2.1.40 存在", os.path.isdir(SIDECAR), SIDECAR)
    for rel in ("core/memory.py", "core/approval.py",
                "tools/builtin/file_ops.py", "tools/builtin/wake_blacklist.py",
                "agents/roles.yaml", "config/config.yaml"):
        p = os.path.join(SIDECAR, rel)
        ck("  %s" % rel, os.path.isfile(p), p)
    cfgp = os.path.join(SIDECAR, "config", "config.yaml")
    if os.path.isfile(cfgp):
        txt = open(cfgp, encoding="utf-8").read()
        ck("  继承的 roots.shared 不是占位符", "<GAME_DIR>" not in txt,
           "含 <GAME_DIR> 占位符，需手工改成真实路径")
        ck("  roots.shared 指向真实游戏目录", "AI/game/aiplayer" in txt.replace("\\", "/"), "未找到真实路径")

    print("== E. 实例提示词 ==")
    sys.path.insert(0, SIDECAR)
    try:
        import importlib
        import core.runner as R
        importlib.reload(R)
        p = R.PRIMARY_PROMPT
        for k in ("file_grep", "shared:", "wake_blacklist", "need_approval", "17 静态"):
            ck("  提示词含 %s" % k, k in p)
        from tools.builtin.file_io import normalize_mode
        ck("  normalize_mode(False)=off", normalize_mode(False) == "off", normalize_mode(False))
    except Exception as e:
        ck("实例模块可导入", False, "%s: %s" % (type(e).__name__, e))

    print("== F. 只读动作抽查 ==")
    st = get("/state")
    pos = st.get("pos") or [0, 0, 0]
    x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
    probes = [
        ("state", {"type": "state"}),
        ("time", {"type": "time"}),
        ("players", {"type": "players"}),
        ("block_at", {"type": "block_at", "x": x, "y": y - 1, "z": z}),
        ("scan_region", {"type": "scan_region", "x1": x - 1, "y1": y - 1, "z1": z - 1,
                         "x2": x + 1, "y2": y + 1, "z2": z + 1}),
        ("get_inventory", {"type": "get_inventory"}),
        ("entities", {"type": "entities"}),
    ]
    for name, a in probes:
        try:
            _, res = cmd(a)
            ck(name, res.get("ok") is True, res)
        except Exception as e:
            ck(name, False, "%s: %s" % (type(e).__name__, e))

    print("== G. sidecar 控制面 ==")
    try:
        s = get("/status", AGENT_BASE)
        ck("8770 /status 可达", s.get("state") in ("idle", "running"), s.get("state"))
        print("     state=%s runs=%s backlog=%s" % (s.get("state"), (s.get("stats") or {}).get("runs"),
                                                    s.get("backlog_n")))
    except Exception as e:
        ck("8770 /status 可达", False, "%s: %s" % (type(e).__name__, e))

    print()
    print("=" * 56)
    print("%d 通过 / %d 失败" % (_ok, _fail))
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

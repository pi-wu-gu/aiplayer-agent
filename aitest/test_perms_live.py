# -*- coding: utf-8 -*-
"""权限表第五轴 gameActionMode 实机验证：full → safe → observe → full（热切换，不重启客户端）。

用法： python aitest/test_perms_live.py [--wait-ready]
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aitest.ctl import cmd  # noqa: E402

TOML = Path("C:/Users/w0801/PCL2/AI/game/config/aiplayer-common.toml")
KEY = "gameActionMode"


def set_mode(mode):
    """把 [permissions] 段里的 gameActionMode 改成 mode（不存在则插入到 fileMode 之后）。"""
    txt = TOML.read_text(encoding="utf-8")
    pat = re.compile(r'(?m)^\t?' + KEY + r'\s*=\s*"[^"]*"')
    new_line = '\t%s = "%s"  # 实机测试写入' % (KEY, mode)
    if pat.search(txt):
        txt = pat.sub(new_line, txt, count=1)
    else:
        # 插在 fileMode 行之后（[permissions] 段内）
        m = re.search(r'(?m)^\t?fileMode\s*=\s*"[^"]*"\n', txt)
        if not m:
            raise SystemExit("★配置文件里找不到 fileMode，无法定位 [permissions] 段")
        txt = txt[:m.end()] + new_line + "\n" + txt[m.end():]
    TOML.write_text(txt, encoding="utf-8")
    time.sleep(2.5)  # 等 NeoForge 文件监听 + ensurePermissions 重建


def probe(tag, expect_count=None, must_have=(), must_gone=()):
    a = cmd({"type": "actions"})
    perm = a.get("permissions", {})
    groups = a.get("groups", {})
    # 精确取动作名：组内条目形如 "press(key)" / "look（看向坐标/角度）" / "move"
    names = set()
    for entries in groups.values():
        for e in entries:
            head = re.split(r"[（(]", str(e))[0].strip()
            if head:
                names.add(head)
    cnt = a.get("count")
    print("── [%s] count=%s gameActionMode=%s deniedCount=%s"
          % (tag, cnt, perm.get("gameActionMode"), perm.get("deniedCount")))
    for k in ("press", "keys", "click_slot", "cont_button", "screen_click", "move", "jump", "sneak", "look"):
        print("     %-14s %s" % (k, "有" if k in names else "—"))
    if expect_count is not None and cnt != expect_count:
        print("     ⚠ 期望 count=%s 实际=%s" % (expect_count, cnt))
    for k in must_have:
        if k not in names:
            print("     ✗ 应保留却缺失: %s" % k)
    for k in must_gone:
        if k in names:
            print("     ✗ 应摘掉却仍在: %s" % k)
    return a


def try_call(tag, action, **kw):
    r = cmd(action, **kw)
    s = str(r)
    verdict = "被禁(denied)" if s.startswith("denied") else ("可用" if not s.startswith(("unknown", "error")) else s[:60])
    print("── 调用 %-8s → %s | %s" % (tag, verdict, s[:90]))
    return s


def main():
    if "--wait-ready" in sys.argv:
        for _ in range(40):
            try:
                if cmd({"type": "ping"}) == "pong" and _bot_loaded():
                    break
            except Exception:
                pass
            time.sleep(10)

    print("========== 基线（full，默认） ==========")
    probe("full/初始", must_have=("press", "keys", "move"))
    try_call("look", {"type": "look"}, yaw=0, pitch=0)   # 阳性对照：始终可用

    print("\n========== 切到 safe ==========")
    set_mode("safe")
    probe("safe", must_have=("move", "look"), must_gone=("press", "keys", "click_slot", "screen_click"))
    try_call("press", {"type": "press"}, key="key.use")
    try_call("state", {"type": "state"})                 # 阳性对照

    print("\n========== 切到 observe ==========")
    set_mode("observe")
    probe("observe", must_have=("look",), must_gone=("press", "move", "jump", "sneak"))
    try_call("move", {"type": "move"}, dir="forward", duration=100)
    try_call("look", {"type": "look"}, yaw=0, pitch=0)   # 阳性对照

    print("\n========== 还原 full ==========")
    set_mode("full")
    probe("full/还原", must_have=("press", "keys", "move", "click_slot", "screen_click"))
    a = cmd({"type": "actions"})
    print("\n最终 count=%s permissions=%s" % (a.get("count"), a.get("permissions", {})))


def _bot_loaded():
    from aitest.ctl import state
    return bool(state().get("loaded"))


if __name__ == "__main__":
    main()

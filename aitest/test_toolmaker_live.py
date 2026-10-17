# -*- coding: utf-8 -*-
"""自建工具（tool_maker / tool_run）实机验证。

覆盖：默认关闭 → 开启 → 写条目与入口 → 调用/参数替换/各类拒绝 → 权限级联 → 还原。
用法： python aitest/test_toolmaker_live.py [--keep]
"""
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aitest.ctl import cmd  # noqa: E402

TOML = Path("C:/Users/w0801/PCL2/AI/game/config/aiplayer-common.toml")
TOOLS_DIR = Path("C:/Users/w0801/PCL2/AI/game/aiplayer/tools")
INDEX = TOOLS_DIR / "index.json"
PASS, FAIL = [], []


def check(tag, cond, extra=""):
    (PASS if cond else FAIL).append(tag)
    print("  %s %s %s" % ("✓" if cond else "✗", tag, extra))


def set_bool(key, val):
    txt = TOML.read_text(encoding="utf-8")
    pat = re.compile(r"(?m)^\t?" + key + r"\s*=\s*(true|false)")
    assert pat.search(txt), "配置里找不到 " + key
    TOML.write_text(pat.sub("\t%s = %s" % (key, "true" if val else "false"), txt, count=1),
                    encoding="utf-8")
    time.sleep(2.5)


def set_mode(mode):
    txt = TOML.read_text(encoding="utf-8")
    pat = re.compile(r'(?m)^\t?gameActionMode\s*=\s*"[^"]*"')
    TOML.write_text(pat.sub('\tgameActionMode = "%s"' % mode, txt, count=1), encoding="utf-8")
    time.sleep(2.5)


def actions():
    return cmd({"type": "actions"})


def has_action(name):
    a = actions()
    for entries in a.get("groups", {}).values():
        for e in entries:
            if re.split(r"[（(]", str(e))[0].strip() == name:
                return True
    return False


def fwrite(rel, content):
    r = cmd({"type": "file_write"}, path=rel, content=content)
    assert isinstance(r, dict) and r.get("ok"), "file_write 失败: %s → %s" % (rel, str(r)[:200])


def main():
    print("========== 0) 基线：selfToolEnabled=false ==========")
    set_bool("selfToolEnabled", False)
    check("tool_run 不在注册表", not has_action("tool_run"))
    r = cmd({"type": "tool_run"}, name="x")
    check("调用 tool_run 返回 denied", str(r).startswith("denied"), str(r)[:80])
    t = cmd({"type": "tools"})
    check("tools 回显 execution_enabled=false", t.get("execution_enabled") is False)

    print("\n========== 1) 开启 selfToolEnabled ==========")
    set_bool("selfToolEnabled", True)
    check("tool_run 已注册", has_action("tool_run"))
    base = actions()["count"]
    print("     动作数 =", base)

    print("\n========== 2) 写条目与入口（走 file_write，模拟 agent） ==========")
    shutil.rmtree(TOOLS_DIR / "perm_demo", ignore_errors=True)
    for name, steps, args, risk in [
        ("perm_demo", [{"type": "say", "text": "自建工具测试 ${who}"}, {"type": "look", "pitch": 0}], ["who"], "只读"),
        ("uses_press", [{"type": "press", "key": "key.use"}], [], "写世界"),
        ("nested", [{"type": "tool_run", "name": "perm_demo"}], [], "只读"),
        ("bad_action", [{"type": "not_a_real_action"}], [], "只读"),
        ("too_many", [{"type": "say", "text": "x", "repeat": 30}], [], "只读"),
        ("bad_entry", [{"type": "say", "text": "x"}], [], "只读"),
    ]:
        fwrite("tools/%s/main.json" % name, json.dumps({"steps": steps}, ensure_ascii=False))
    idx = {"version": 1, "tools": [
        {"name": n, "entry": "main.json", "args": a, "desc": "测试条目 " + n, "enabled": True, "risk": r}
        for n, a, r in [("perm_demo", ["who"], "只读"), ("uses_press", [], "写世界"),
                        ("nested", [], "只读"), ("bad_action", [], "只读"), ("too_many", [], "只读"),
                        ("bad_entry", [], "只读")]
    ]}
    idx["tools"][5]["entry"] = "../escape.json"
    fwrite("tools/index.json", json.dumps(idx, ensure_ascii=False, indent=1))
    check("index.json 写入成功", True)

    print("\n========== 3) tools 自检回显 ==========")
    t = cmd({"type": "tools"})
    check("execution_enabled=true", t.get("execution_enabled") is True)
    by = {e["name"]: e for e in t.get("entries", [])}
    check("perm_demo 自检 ok", by.get("perm_demo", {}).get("ok") is True)
    check("nested 自检不 ok（禁嵌套）", by.get("nested", {}).get("ok") is False,
          str(by.get("nested", {}).get("issues"))[:80])
    check("bad_entry 自检不 ok（entry 含路径）", by.get("bad_entry", {}).get("ok") is False,
          str(by.get("bad_entry", {}).get("issues"))[:80])

    print("\n========== 4) tool_run 正常调用 + 参数替换 ==========")
    r = cmd({"type": "tool_run"}, name="perm_demo", who="HELLO_ARG")
    check("执行成功 ok=true", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:200])
    check("步骤都执行", r.get("steps_run") == 2)
    tr = " ".join(x.get("result", "") for x in r.get("trace", []))
    check("${who} 被替换", "HELLO_ARG" in tr, tr[:120])

    print("\n========== 5) 各类拒绝 ==========")
    r = cmd({"type": "tool_run"}, name="perm_demo")
    check("缺必填参数被拒", r.get("ok") is False and "缺少必填参数" in str(r.get("problems")), str(r.get("problems"))[:100])
    r = cmd({"type": "tool_run"}, name="nested")
    check("嵌套 tool_run 被拒", r.get("ok") is False and "嵌套" in str(r.get("problems")), str(r.get("problems"))[:100])
    r = cmd({"type": "tool_run"}, name="bad_action")
    check("非法动作名被拒", r.get("ok") is False and "不可用" in str(r.get("problems")), str(r.get("problems"))[:100])
    r = cmd({"type": "tool_run"}, name="too_many")
    check("repeat 超限被拒", r.get("ok") is False and "repeat" in str(r.get("problems")), str(r.get("problems"))[:100])
    r = cmd({"type": "tool_run"}, name="bad_entry")
    check("entry 含路径被拒", r.get("ok") is False and "entry" in str(r.get("problems")), str(r.get("problems"))[:100])
    r = cmd({"type": "tool_run"}, name="not_exist")
    check("不存在的工具报 tool_err", str(r).startswith("tool_err"), str(r)[:100])
    r = cmd({"type": "tool_run"}, name="../../evil")
    check("工具名越界被拒", str(r).startswith("tool_err"), str(r)[:100])

    print("\n========== 6) 权限级联：safe 档下含 press 的宏不可用 ==========")
    set_mode("safe")
    r = cmd({"type": "tool_run"}, name="uses_press")
    check("safe 下 uses_press 被拒（press 不可用）",
          r.get("ok") is False and "不可用" in str(r.get("problems")), str(r.get("problems"))[:120])
    r = cmd({"type": "tool_run"}, name="perm_demo", who="X")
    check("safe 下纯读宏仍可用", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:120])
    set_mode("full")

    print("\n========== 7) 还原 ==========")
    set_bool("selfToolEnabled", False)
    check("tool_run 已摘除", not has_action("tool_run"))
    if "--keep" not in sys.argv:
        fwrite("tools/index.json", '{\n  "version": 1,\n  "tools": []\n}\n')
        for n in ("perm_demo", "uses_press", "nested", "bad_action", "too_many", "bad_entry"):
            shutil.rmtree(TOOLS_DIR / n, ignore_errors=True)
        print("     已清理测试条目与目录")

    print("\n===== 结果：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""通用授权申请体系 + 框架侧完整文件能力 —— 离线回归（2026-10-18 新增）。

覆盖：
1. 授权申请：拦截→排队→批准→单次消费→用完即焚；参数指纹（改参数要重新申请）；
   拒绝 / 过期 / 重复排队不刷屏 / 审计
2. 文件能力：list/stat/grep/mkdir/copy/move；删除的三种模式
   （off 不注册 / approval 需批准 / free 直删）、软删可恢复、保护清单、越界走申请
"""
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


_tmp = tempfile.mkdtemp(prefix="appr_test_")
import core.config_loader as cl  # noqa: E402
cl.AGENT_ROOT = _tmp
os.makedirs(os.path.join(_tmp, "logs"), exist_ok=True)

from core import approval  # noqa: E402
from tools.builtin import file_ops, file_io  # noqa: E402

BASE = {"approval": {"enabled": True, "ttl_sec": 300},
        "permissions": {"file_mode": "full", "destructive_file": "approval",
                        "roots": {"w": _tmp}, "default_root": "w"}}

# ---------- 1. 授权申请 ----------
print("== 1. 授权申请体系 ==")
r1 = approval.require(BASE, "file_delete", {"path": "a.txt"}, reason="删测试文件",
                      risk="high", trigger="manual")
ck("首次调用被拦截并排队", r1.get("need_approval") is True, str(r1))
rid = r1.get("request_id")
ck("返回申请 id", bool(rid), str(r1))
ck("进入待审核队列", any(r["id"] == rid for r in approval.pending(BASE)))

ck("未批准时 check 不通", approval.check(BASE, "file_delete", {"path": "a.txt"}) is False)

# 重复排队不刷屏
r2 = approval.require(BASE, "file_delete", {"path": "a.txt"}, trigger="manual")
ck("同参数重复申请复用同一条", r2.get("request_id") == rid, str(r2))

ok, msg = approval.grant(BASE, rid)
ck("批准成功", ok is True, msg)
ck("批准后 check 通过（并消费令牌）",
   approval.check(BASE, "file_delete", {"path": "a.txt"}) is True)
ck("★单次有效：第二次 check 失效",
   approval.check(BASE, "file_delete", {"path": "a.txt"}) is False)
ck("已消费 → 不在待审核", not any(r["id"] == rid for r in approval.pending(BASE)))

# 参数指纹：改参数要重新申请
r3 = approval.require(BASE, "file_delete", {"path": "b.txt"}, trigger="manual")
ck("★改了参数 → 新申请（旧令牌不匹配）", r3.get("request_id") != rid, str(r3))
ck("用过相同工具但不同参数，check 不通",
   approval.check(BASE, "file_delete", {"path": "b.txt"}) is False)
ok2, _ = approval.deny(BASE, r3["request_id"])
ck("可拒绝", ok2 is True)
ck("拒绝后不再待审核",
   not any(r["id"] == r3["request_id"] for r in approval.pending(BASE)))

# 审计落盘
audit = os.path.join(_tmp, "logs", "approval_audit.log")
ck("审计日志已写", os.path.isfile(audit) and os.path.getsize(audit) > 0)

# 关闭体系 → 直接拒绝不排队
off = {"approval": {"enabled": False}}
r4 = approval.require(off, "file_delete", {"path": "c.txt"})
ck("体系关闭 → 直接拒绝不排队", r4.get("need_approval") is False, str(r4))

# ---------- 2. 文件能力 ----------
print("== 2. 框架侧文件能力 ==")
open(os.path.join(_tmp, "a.txt"), "w", encoding="utf-8").write("line1\nhello world\n")
open(os.path.join(_tmp, "b.txt"), "w", encoding="utf-8").write("nothing here\n")
os.makedirs(os.path.join(_tmp, "sub"), exist_ok=True)
open(os.path.join(_tmp, "sub", "c.txt"), "w", encoding="utf-8").write("hello again\n")

tools = {m["name"]: fn for m, fn in file_ops.make_file_ops_tools(BASE)}
for t in ("file_list", "file_stat", "file_grep", "file_mkdir", "file_copy",
          "file_move", "file_delete", "file_restore"):
    ck("注册 %s" % t, t in tools)

lst = tools["file_list"](".")
ck("file_list 列出条目", lst.get("count", 0) >= 3, str(lst)[:150])
st = tools["file_stat"]("a.txt")
# 注意：Windows 文本模式写 "\n" 会变 "\r\n"，size 比字面多 2 字节，故只校验量级
ck("file_stat 有 size/行数", st.get("lines") == 2 and 18 <= st.get("size", 0) <= 20,
   str(st))
g = tools["file_grep"]("hello")
ck("file_grep 命中 2 处", g.get("hits") == 2, str(g))
ck("file_mkdir 可建多层", tools["file_mkdir"]("x/y/z").get("ok") is True)
cp = tools["file_copy"]("a.txt", "a2.txt")
ck("file_copy 成功", cp.get("ok") is True, str(cp))
ck("file_copy 默认不覆盖", tools["file_copy"]("a.txt", "a2.txt").get("ok") is False)
ck("file_copy overwrite 可覆盖",
   tools["file_copy"]("a.txt", "a2.txt", True).get("ok") is True)
mv = tools["file_move"]("a2.txt", "a3.txt")
ck("file_move 成功", mv.get("ok") is True and os.path.exists(os.path.join(_tmp, "a3.txt")))

# ---------- 3. 删除：approval 模式 ----------
print("== 3. 删除与授权联动 ==")
d1 = tools["file_delete"]("b.txt")
ck("★未批准 → 提交申请而非删除", d1.get("need_approval") is True, str(d1))
ck("文件仍在", os.path.exists(os.path.join(_tmp, "b.txt")))
# 控制台批准
pend = approval.pending(BASE)
hit = [r for r in pend if r["tool"] == "file_delete"]
okg, _ = approval.grant(BASE, hit[0]["id"])
ck("批准该申请", okg is True)
d2 = tools["file_delete"]("b.txt")
ck("★批准后用相同参数重试 → 执行", d2.get("ok") is True, str(d2))
ck("文件已被软删（不在原位）", not os.path.exists(os.path.join(_tmp, "b.txt")))
ck("进了 .trash", os.path.isdir(os.path.join(_tmp, ".trash")), str(d2))
ck("默认软删非 purge", d2.get("mode") == "trash", str(d2))
rs = tools["file_restore"]()
ck("★软删可恢复", rs.get("ok") is True and os.path.exists(os.path.join(_tmp, "b.txt")),
   str(rs))

# 保护清单
prot = {"approval": {"enabled": True},
        "permissions": {"file_mode": "full", "destructive_file": "free",
                        "roots": {"w": _tmp}, "default_root": "w"}}
tf = {m["name"]: fn for m, fn in file_ops.make_file_ops_tools(prot)}
os.makedirs(os.path.join(_tmp, "config"), exist_ok=True)
open(os.path.join(_tmp, "config", "keys.yaml"), "w", encoding="utf-8").write("k")
ck("★保护清单：keys.yaml 拒绝删除",
   tf["file_delete"]("config/keys.yaml").get("ok") is False,
   str(tf["file_delete"]("config/keys.yaml")))
ck("★保护清单：目录不空需 recursive",
   tf["file_delete"]("config", True).get("ok") is False,
   str(tf["file_delete"]("config", True)))

# off 模式 → 工具不注册
offcfg = {"approval": {"enabled": True},
          "permissions": {"file_mode": "full", "destructive_file": "off",
                          "roots": {"w": _tmp}, "default_root": "w"}}
tf2 = {m["name"] for m, _ in file_ops.make_file_ops_tools(offcfg)}
ck("★off 模式不注册 file_delete", "file_delete" not in tf2, str(tf2))

# readonly → 只有只读动作
ro = {"permissions": {"file_mode": "readonly", "roots": {"w": _tmp},
                      "default_root": "w"}}
tf3 = {m["name"] for m, _ in file_ops.make_file_ops_tools(ro)}
ck("★readonly 保留只读（list/stat/grep）",
   {"file_list", "file_stat", "file_grep"} <= tf3, str(tf3))
ck("★readonly 不给写/删", "file_delete" not in tf3 and "file_copy" not in tf3, str(tf3))

# file_mode=off → 全不注册
offall = {"permissions": {"file_mode": "off", "roots": {"w": _tmp}}}
ck("★file_mode=off → 空", file_ops.make_file_ops_tools(offall) == [])

# 越界走申请
try:
    file_io.safe_path(BASE, "../outside.txt")
    ck("白名单外路径确实越界", False)
except ValueError:
    ck("白名单外路径确实越界", True)
real, gate = file_io.safe_path_gated(BASE, "../outside.txt", "file_read",
                                     {"path": "../outside.txt"}, risk="medium")
ck("★越界 → 走授权申请（不是硬拒）",
   gate is not None and gate.get("need_approval") is True, str(gate))

# ---------- 10. 越界删除：批准后重试必须真正执行（2026-10-18 修复回归） ----------
# 背景：原实现把 approval.check 放在 safe_path 成功之后，越界路径永远走不到 →
# 批了也白批，且每次重试都刷新申请（实机复现过）。
print("== 10. 越界删除审批闭环 ==")
outside = os.path.join(tempfile.gettempdir(), "appr_outside_del_target.txt")
with open(outside, "w", encoding="utf-8") as f:
    f.write("delete-me")
r1 = file_ops._delete(BASE, outside, recursive=False, purge=True)
ck("越界删除被拦截并生成申请",
   r1.get("need_approval") is True and r1.get("ok") is False, str(r1))
rid_o = r1.get("request_id")
gok, _ = approval.grant(BASE, rid_o)
ck("控制台批准越界删除", gok is True, str(gok))
r2 = file_ops._delete(BASE, outside, recursive=False, purge=True)
ck("★批准后同参数重试真正执行删除",
   r2.get("ok") is True and not os.path.exists(outside), str(r2))
ck("令牌已消费（状态=consumed）",
   any(r.get("id") == rid_o and r.get("status") == "consumed"
       for r in approval.history(BASE)))
with open(outside, "w", encoding="utf-8") as f:
    f.write("again")
r3 = file_ops._delete(BASE, outside, recursive=False, purge=True)
ck("★单次用完即焚：再删要重新申请", r3.get("need_approval") is True, str(r3))
approval.deny(BASE, r3.get("request_id"))
if os.path.exists(outside):
    os.remove(outside)

# ---------- 11. 敏感文件读写拦截（2026-10-18 新增） ----------
# 威胁模型：聊天是不可信输入，@提及可注入 → file_read 把密钥带进 LLM 上下文 = 外泄；
# approvals.json 可被伪造 grant（args_key 是 sha1，agent 可自算）= 绕过审批体系。
print("== 11. 敏感文件读写拦截 ==")
os.makedirs(os.path.join(_tmp, "config"), exist_ok=True)
keys_path = os.path.join(_tmp, "config", "keys.yaml")
with open(keys_path, "w", encoding="utf-8") as f:
    f.write('providers:\n  - api_key: "sk-FAKE"\n')
iotools = {m["name"]: fn for m, fn in file_io.make_file_tools(BASE)}
rr = iotools["file_read"]("config/keys.yaml")
ck("file_read 拒绝 keys.yaml", rr.get("blocked") is True and rr.get("ok") is False, str(rr))
ww = iotools["file_write"]("config/keys.yaml", "hack")
ck("file_write 拒绝 keys.yaml", ww.get("blocked") is True, str(ww))
ck("写拦截后密钥文件未被改动", "sk-FAKE" in open(keys_path, encoding="utf-8").read())
st1 = file_ops._stat(BASE, "config/keys.yaml")
ck("file_stat 拒绝 keys.yaml", st1.get("blocked") is True, str(st1))
seed = '{"requests": [], "grants": []}'
with open(os.path.join(_tmp, "logs", "approvals.json"), "w", encoding="utf-8") as f:
    f.write(seed)
wa = iotools["file_write"]("logs/approvals.json", '{"grants": [{"used": false}]}')
ck("★file_write 拒绝伪造 approvals.json", wa.get("blocked") is True, str(wa))
ck("伪造写入未生效", open(os.path.join(_tmp, "logs", "approvals.json"),
                          encoding="utf-8").read() == seed)
cp1 = file_ops._copy(BASE, "config/keys.yaml", "leaked.txt")
ck("file_copy 拒绝把 keys.yaml 复制出去", cp1.get("blocked") is True, str(cp1))
ck("复制确实没有发生", not os.path.exists(os.path.join(_tmp, "leaked.txt")))
with open(os.path.join(_tmp, "plain.txt"), "w", encoding="utf-8") as f:
    f.write("hello sk-FAKE world")
gr = file_ops._grep(BASE, "sk-FAKE", path="", glob="*")
ck("★file_grep 目录扫描跳过 keys.yaml（不回显密钥行）",
   all("keys.yaml" not in ln for ln in gr.get("lines", [])) and gr.get("hits", 0) >= 1,
   str(gr.get("lines")))
gr2 = file_ops._grep(BASE, "sk-FAKE", path="config/keys.yaml")
ck("file_grep 直接指定 keys.yaml → 拒绝", gr2.get("blocked") is True, str(gr2))

# ---------- 12. .trash 批次毫秒防撞（2026-10-18 新增） ----------
print("== 12. .trash 批次防撞 ==")
FREE = {"approval": {"enabled": True},
        "permissions": {"file_mode": "full", "destructive_file": "free",
                        "roots": {"w": _tmp}, "default_root": "w"}}
for nm in ("b1.txt", "b2.txt"):
    with open(os.path.join(_tmp, nm), "w", encoding="utf-8") as f:
        f.write("x")
trash_dir = os.path.join(_tmp, ".trash")
before = set(os.listdir(trash_dir)) if os.path.isdir(trash_dir) else set()
d1 = file_ops._delete(FREE, "b1.txt")
d2 = file_ops._delete(FREE, "b2.txt")
ck("free 模式软删 b1/b2 成功", d1.get("ok") is True and d2.get("ok") is True,
   str(d1) + " | " + str(d2))
new = sorted(set(os.listdir(trash_dir)) - before)
ck("★同秒两次软删产生两个独立批次", len(new) == 2, str(new))

shutil.rmtree(_tmp, ignore_errors=True)
print("\n%d 通过 / %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

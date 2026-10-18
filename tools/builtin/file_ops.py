# -*- coding: utf-8 -*-
"""框架侧完整文件能力（2026-10-18 建成）。

★定位（用户 2026-10-18 明确）：
  「框架侧，模组侧之后标注为弃用但保留。确保 agent 框架脱离模组仅丢失游戏干涉功能，
    仍能正常完成其他任务。」

所以框架侧必须能**独立完成全部文件工作**：读写追加列目录（file_io.py 已有）
+ 这里补的 mkdir / stat / copy / move / delete / grep。

安全边界（全部沿用既有地基，不另起炉灶）：
  1. 多根白名单 + realpath（file_io.safe_path）—— 越界一律拒绝
  2. 权限表 fileMode：off → 不注册；readonly → 只注册只读动作（stat/grep）
  3. ★删除走通用授权体系（core/approval）：默认需要控制台**单次批准**
     开关 destructive_file: off | approval(默认) | free
  4. 保护清单：拒绝删 config/、keys.yaml、runtime/、.git/、白名单根本身
  5. 审计：删除落 logs/approval_audit.log（由 approval 模块负责）
"""
import fnmatch
import json
import os
import shutil
import time

from core import approval, config_loader, run_context
from tools.builtin import file_io


# ---------- 保护清单（相对白名单根的路径片段，命中即拒绝删除/移动） ----------
_PROTECTED_PARTS = {"config", "keys.yaml", "runtime", ".git", "agent.lock",
                    "approvals.json", "daemon_state.json"}
_PROTECTED_SUFFIX = (".jar", ".exe", ".dll")


def _cfg_mode(cfg):
    """destructive_file: off | approval | free（默认 approval）。

    ★2026-10-18 修：YAML 1.1 把裸 `off` 解析成布尔 False，而 `False or "approval"`
    会落回默认 `approval` —— 机主以为"已关闭删除"，实际仍可申请（需审批）。
    方向保守但也属配置失效，显式处理布尔。
    """
    raw = ((cfg or {}).get("permissions") or {}).get("destructive_file", "approval")
    if isinstance(raw, bool):
        raw = "off" if raw is False else "free"
    m = str(raw or "approval").strip().lower()
    return m if m in ("off", "approval", "free") else "approval"


def _file_mode(cfg):
    return file_io.normalize_mode(((cfg or {}).get("permissions") or {}).get("file_mode", "full"))


def _is_protected(cfg, real):
    """是否落在保护清单内。"""
    low = real.replace("\\", "/").lower()
    base = os.path.basename(low)
    if base in {p.lower() for p in _PROTECTED_PARTS}:
        return True
    parts = set(low.split("/"))
    if "config" in parts or ".git" in parts or "runtime" in parts:
        return True
    if base.endswith(_PROTECTED_SUFFIX):
        return True
    # 白名单根本身不能删
    for root in file_io.resolve_roots(cfg).values():
        if os.path.normcase(os.path.normpath(real)) == os.path.normcase(os.path.normpath(root)):
            return True
    return False


def _rel(cfg, real):
    """展示用相对路径（取最匹配的白名单根）。"""
    best = None
    for name, root in file_io.resolve_roots(cfg).items():
        try:
            r = os.path.relpath(real, root)
        except ValueError:
            continue
        if best is None or len(r) < len(best[1]):
            best = (name, r)
    if best:
        return "%s:%s" % (best[0], best[1].replace("\\", "/"))
    return real


# ---------- 动作实现 ----------
def _list(cfg, path="", recursive=False, limit=300):
    real = file_io.safe_path(cfg, path)
    if not os.path.isdir(real):
        return {"error": "不是目录: %s" % path}
    out, n = [], 0
    walker = os.walk(real) if recursive else [(real, [], os.listdir(real))]
    for root, dirs, files in walker:
        if n >= limit:
            out.append("…（已达 %d 条上限）" % limit)
            break
        rel_root = os.path.relpath(root, real).replace("\\", "/")
        for name in sorted(dirs):
            out.append(("%s/" % name) if rel_root == "." else "%s/%s/" % (rel_root, name))
            n += 1
        for name in sorted(files):
            p = os.path.join(root, name)
            try:
                sz = os.path.getsize(p)
            except OSError:
                sz = 0
            out.append("%s%s (%d字)" % ("" if rel_root == "." else rel_root + "/", name, sz))
            n += 1
        if not recursive:
            break
    return {"path": path or ".", "recursive": recursive, "count": len(out), "entries": out}


def _mkdir(cfg, path):
    real = file_io.safe_path(cfg, path)
    os.makedirs(real, exist_ok=True)
    return {"ok": True, "path": _rel(cfg, real), "created": True}


def _stat(cfg, path):
    real = file_io.safe_path(cfg, path)
    if file_io.is_sensitive(real):
        return file_io.sensitive_block(real)
    if not os.path.exists(real):
        return {"ok": False, "reason": "不存在: %s" % path}
    st = os.stat(real)
    info = {"path": _rel(cfg, real), "is_dir": os.path.isdir(real),
            "size": st.st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}
    if os.path.isfile(real):
        try:
            with open(real, encoding="utf-8", errors="replace") as f:
                info["lines"] = sum(1 for _ in f)
        except OSError:
            pass
    return {"ok": True, **info}


def _copy(cfg, src, dst, overwrite=False):
    s = file_io.safe_path(cfg, src)
    d = file_io.safe_path(cfg, dst)
    # ★敏感文件双向拦：复制源=外泄（复制成普通文件再读），复制目标=覆盖密钥/审批状态
    if file_io.is_sensitive(s) or file_io.is_sensitive(d):
        return file_io.sensitive_block(s if file_io.is_sensitive(s) else d)
    if not os.path.exists(s):
        return {"ok": False, "reason": "源不存在: %s" % src}
    if os.path.exists(d) and not overwrite:
        return {"ok": False, "reason": "目标已存在：%s（overwrite=true 才覆盖）" % dst}
    os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
    if os.path.isdir(s):
        shutil.copytree(s, d, dirs_exist_ok=bool(overwrite))
    else:
        shutil.copy2(s, d)
    return {"ok": True, "from": _rel(cfg, s), "to": _rel(cfg, d)}


def _move(cfg, src, dst, overwrite=False):
    s = file_io.safe_path(cfg, src)
    d = file_io.safe_path(cfg, dst)
    if not os.path.exists(s):
        return {"ok": False, "reason": "源不存在: %s" % src}
    if _is_protected(cfg, s):
        return {"ok": False, "reason": "源在保护清单内，禁止移动: %s" % _rel(cfg, s)}
    if os.path.exists(d) and not overwrite:
        return {"ok": False, "reason": "目标已存在：%s（overwrite=true 才覆盖）" % dst}
    os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
    shutil.move(s, d)
    return {"ok": True, "from": _rel(cfg, s), "to": _rel(cfg, d)}


def _delete(cfg, path, recursive=False, purge=False):
    """删除。★默认走授权申请（destructive_file=approval）。"""
    mode = _cfg_mode(cfg)
    if mode == "off":
        return {"ok": False, "reason": "删除功能已关闭（permissions.destructive_file=off）"}
    args = {"path": path, "recursive": bool(recursive), "purge": bool(purge)}
    approved_out_of_root = False
    try:
        real = file_io.safe_path(cfg, path)
    except ValueError:
        # ★越界也接入授权体系：让操作者有机会临时放行一次。
        # ★2026-10-18 修（两处）：①令牌消费（approval.check）必须在异常分支内做——
        #   原先只在这里 require，批准后重试依然先抛 ValueError、永远走不到下方的
        #   check，形成"批了也白批"死循环（实测：批准后重试又生成新申请，文件未删）。
        #   ②指纹必须与主流程闸门共用同一个 args（含 recursive/purge）——
        #   第一版修复用 {"path":...} 申请、主流程用全量 args 校验，第二次闸门
        #   永远不匹配，令牌被白白消费。放行后跳过主流程闸门（approved_out_of_root），
        #   但保护清单检查照常生效，"越界+保护路径"组合仍然删不掉。
        if approval.check(cfg, "file_delete", args):
            real = os.path.realpath(str(path or ""))
            approved_out_of_root = True
        else:
            return approval.require(cfg, "file_delete", args,
                                    reason="路径越界，申请临时放行", risk="critical",
                                    how_to="批准后该路径本次放行；路径不在白名单根内，请确认无误。")
    if _is_protected(cfg, real):
        return {"ok": False, "reason": "保护清单内，禁止删除: %s" % _rel(cfg, real)}
    if not os.path.exists(real):
        return {"ok": False, "reason": "不存在: %s" % path}
    if os.path.isdir(real) and not recursive:
        n = len(os.listdir(real))
        if n:
            return {"ok": False, "reason": "目录非空（%d 项）：需 recursive=true" % n}

    if (mode == "approval" and not approved_out_of_root
            and not approval.check(cfg, "file_delete", args)):
        return approval.require(
            cfg, "file_delete", args,
            reason="删除 %s（purge=%s 真删/软删）" % (_rel(cfg, real), purge),
            risk="critical" if purge else "high",
            how_to="控制台批准后，用**完全相同的参数**再调一次 file_delete 即执行。")

    # ---- 执行 ----
    if purge:
        if os.path.isdir(real):
            shutil.rmtree(real)
        else:
            os.remove(real)
        return {"ok": True, "deleted": _rel(cfg, real), "mode": "purge",
                "note": "已真删，不可恢复"}
    # 软删：移到 <根>/.trash/<时间戳>/ 并保持原相对结构
    roots = file_io.resolve_roots(cfg)
    root = None
    for r in roots.values():
        if file_io._inside(r, real):
            root = r
            break
    root = root or os.path.dirname(real)
    rel = os.path.relpath(real, root)
    # ★批次名带毫秒：同秒两次软删并进同一批次会让恢复时两批文件混在一起
    batch = time.strftime("%Y%m%d_%H%M%S") + "_%03d" % int((time.time() % 1) * 1000)
    trash = os.path.join(root, ".trash", batch, rel)
    os.makedirs(os.path.dirname(trash), exist_ok=True)
    shutil.move(real, trash)
    return {"ok": True, "deleted": _rel(cfg, real),
            "trashed_to": _rel(cfg, trash), "mode": "trash",
            "note": "已移入 .trash（可恢复）；purge=true 才是真删"}


def _restore(cfg, name=None):
    """从 .trash 恢复最近一次（或指定名字）删除。"""
    roots = file_io.resolve_roots(cfg)
    cands = []
    for r in roots.values():
        t = os.path.join(r, ".trash")
        if os.path.isdir(t):
            for d in sorted(os.listdir(t), reverse=True):
                cands.append(os.path.join(t, d))
    if not cands:
        return {"ok": False, "reason": "没有可恢复的 .trash 内容"}
    pick = None
    for c in cands:
        if name and name not in c:
            continue
        pick = c
        break
    if not pick:
        return {"ok": False, "reason": "未找到匹配的 .trash 批次: %s" % name}
    moved, failed = [], []
    base_root = None
    for r in roots.values():
        if file_io._inside(r, pick):
            base_root = r
            break
    for root, _dirs, files in os.walk(pick):
        for fn in files:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, pick)
            dst = os.path.join(base_root, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                moved.append(rel)
            except OSError as e:
                failed.append("%s: %s" % (rel, e))
    return {"ok": True, "restored": len(moved), "files": moved[:20], "failed": failed}


def _grep(cfg, pattern, path="", glob="*", max_hits=50, max_line=200):
    real = file_io.safe_path(cfg, path)
    # 直接指定敏感文件 → 拒绝；目录扫描 → 跳过敏感文件（内容会进 LLM 上下文）
    if os.path.isfile(real) and file_io.is_sensitive(real):
        return file_io.sensitive_block(real)
    targets = []
    if os.path.isfile(real):
        targets = [real]
    else:
        for root, dirs, files in os.walk(real):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".trash")]
            for fn in files:
                if fnmatch.fnmatch(fn, glob):
                    fp = os.path.join(root, fn)
                    if not file_io.is_sensitive(fp):
                        targets.append(fp)
            if len(targets) > 2000:
                break
    hits = []
    for t in sorted(targets):
        if len(hits) >= max_hits:
            break
        try:
            with open(t, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if pattern in line:
                        hits.append("%s:%d: %s" % (_rel(cfg, t), i,
                                                   line.strip()[:max_line]))
                        break
        except OSError:
            continue
    return {"ok": True, "pattern": pattern, "scanned": len(targets),
            "hits": len(hits), "lines": hits}


# ---------- 工具装配 ----------
def make_file_ops_tools(cfg):
    """按权限表产出：off → []；readonly → 只读动作；full → 全部（删除另受 destructive_file 管）。"""
    mode = _file_mode(cfg)
    if mode == "off":
        return []
    roots_desc = file_io.describe_roots(cfg)
    default = file_io.default_root_name(cfg)
    out = []

    def add(name, desc, props, required, fn):
        out.append(({"name": name, "description": desc,
                     "parameters": {"type": "object", "required": required,
                                    "properties": props}}, fn))

    add("file_list",
        "列目录（★框架侧，相对路径默认落在 %s；白名单工作区：%s）。"
        "★支持「根名:相对路径」（如 shared:docs/记忆）——与返回值里的格式一致，可照抄。"
        "recursive=true 递归；条目上限 300。" % (default, roots_desc),
        {"path": {"type": "string"}, "recursive": {"type": "boolean"},
         "limit": {"type": "integer"}},
        [], lambda path="", recursive=False, limit=300: _list(cfg, path, recursive, limit))

    add("file_stat",
        "查看文件/目录信息（大小、修改时间、行数、是否目录）。只读，readonly 模式也可用。",
        {"path": {"type": "string"}}, ["path"], lambda path: _stat(cfg, path))

    add("file_grep",
        "在白名单工作区内搜索文本（子串匹配）。返回 文件:行号:内容，默认上限 50 条。"
        "找不文件在哪、某句话写在哪时用它，比逐个 file_read 省 token。",
        {"pattern": {"type": "string"}, "path": {"type": "string"},
         "glob": {"type": "string", "description": "文件名通配，默认 *"},
         "max_hits": {"type": "integer"}},
        ["pattern"],
        lambda pattern, path="", glob="*", max_hits=50: _grep(cfg, pattern, path, glob, max_hits))

    if mode != "full":
        return out

    add("file_mkdir",
        "创建目录（含多级）。写文件会自动建父目录，通常不必显式调用。",
        {"path": {"type": "string"}}, ["path"], lambda path: _mkdir(cfg, path))

    add("file_copy",
        "复制文件/目录（默认不覆盖：目标已存在会拒绝，overwrite=true 才覆盖）。",
        {"src": {"type": "string"}, "dst": {"type": "string"},
         "overwrite": {"type": "boolean"}},
        ["src", "dst"], lambda src, dst, overwrite=False: _copy(cfg, src, dst, overwrite))

    add("file_move",
        "移动/重命名（默认不覆盖）。★保护清单内的源（config/、runtime/、keys.yaml…）禁止移动。",
        {"src": {"type": "string"}, "dst": {"type": "string"},
         "overwrite": {"type": "boolean"}},
        ["src", "dst"], lambda src, dst, overwrite=False: _move(cfg, src, dst, overwrite))

    del_mode = _cfg_mode(cfg)
    if del_mode != "off":
        add("file_delete",
            "删除文件/空目录。★**默认需要操作者在控制台批准（单次有效）**：首次调用会返回"
            "「已提交申请 id=xxx」，批准后**用相同参数再调一次**才真正执行。\n"
            "- purge=false（默认）→ 移入 <工作区>/.trash/<时间戳>/，**可恢复**；"
            "purge=true → 真删，不可恢复。\n"
            "- 目录非空需 recursive=true。\n"
            "- 保护清单（config/、keys.yaml、runtime/、.git/、工作区根本身）一律拒绝。\n"
            "- 路径越界同样走申请（critical 级）。\n"
            "当前模式=%s。" % del_mode,
            {"path": {"type": "string"}, "recursive": {"type": "boolean"},
             "purge": {"type": "boolean"}},
            ["path"],
            lambda path, recursive=False, purge=False: _delete(cfg, path, recursive, purge))

        add("file_restore",
            "从 .trash 恢复最近一次删除（name 可指定批次名片段）。误删后的后悔药。",
            {"name": {"type": "string"}}, [], lambda name=None: _restore(cfg, name))

    return out

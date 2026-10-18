# -*- coding: utf-8 -*-
"""通用授权申请体系（2026-10-18 建成）。

★需求来源（用户原话）：
  「增加删除功能，但默认关闭，可申请单次授权（控制台授权）。
    在控制台添加待审核授权申请，其他危险或越界行为也可以接入这个体系。」

解决的问题：以前高危能力只有两种状态——"直接拒绝"（agent 永远干不了）或"直接放行"（出事不可逆）。
现在是第三条路：**agent 提出单次申请 → 操作者在控制台看到 → 批准这一次 → 用完即焚**。

设计要点：
- **申请（request）**：agent 触发高危动作 → 拦截 → 落一条待审核记录 → 回给 agent
  一个 request_id，告诉它"已提交，等批准"。agent 可以去做别的事，也可以原地重试。
- **批准（grant）**：操作者在控制台点"批准" → 生成一次性令牌，绑定 (工具, 参数指纹, 有效期)。
- **单次消费**：同一 (工具+参数) 再次调用命中令牌 → 放行 → **令牌立即作废**。
- **参数指纹**：参数规范化后的 sha1。改一个字符就匹配不上，防止"批了 A 却执行 B"。
- **通用**：删除只是第一个接入方。路径越界、命令行越白名单等，未来都能走同一套
  （调用方只需 `needs_approval()` / `require()` 两个函数）。

★安全前提：本模块只负责"要不要放行"，不负责"能不能碰"——路径白名单、权限表仍是第一道防线。
"""
import hashlib
import json
import os
import threading
import time

from core import config_loader

_LOCK = threading.RLock()
DEFAULT_TTL = 300          # 批准后有效期（秒）
MAX_PENDING = 50           # 待审核队列上限（超出丢最旧，防刷屏）

RISKS = ("low", "medium", "high", "critical")
_TTL_BY_RISK = {"low": 600, "medium": 300, "high": 180, "critical": 120}


def state_path():
    return os.path.join(config_loader.AGENT_ROOT, "logs", "approvals.json")


def cfg_of(cfg):
    """取 approval 段配置。

    ★2026-10-18 修：该段实际写在 **permissions.approval** 下
    （与 permissions.shell / permissions.web / permissions.destructive_file 同级），
    本函数原先只读**顶层** `cfg["approval"]` → 永远返回 None →
    默认值恰好等于 yaml 里的值，所以"看起来正常"；但一旦把
    `permissions.approval.enabled` 改成 false，**根本不会生效**（关不掉审批体系）。
    顶层写法保留兼容（老配置 / 单元测试直接传顶层）。
    """
    c = cfg or {}
    nested = (c.get("permissions") or {}).get("approval")
    if isinstance(nested, dict):
        return nested
    top = c.get("approval")
    return top if isinstance(top, dict) else {}


def enabled(cfg):
    """总开关。关闭 → 所有需要审批的动作一律直接拒绝（不排队）。"""
    return bool(cfg_of(cfg).get("enabled", True))


def default_ttl(cfg):
    try:
        return max(30, int(cfg_of(cfg).get("ttl_sec", DEFAULT_TTL)))
    except (TypeError, ValueError):
        return DEFAULT_TTL


# ---------- 存储 ----------
def _load():
    try:
        with open(state_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("requests", [])
            d.setdefault("grants", [])
            return d
    except Exception:
        pass
    return {"requests": [], "grants": []}


def _save(d):
    p = state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _looks_like_path(s):
    return ("/" in s) or ("\\" in s) or (len(s) > 1 and s[1] == ":")


def _norm_val(v):
    """路径类参数的归一（2026-10-18 实测修）：

    实例：agent 第一次申请用的 `C:\\Users\\...`，批准后重试时写成 `C://Users//...`
    （双斜杠 / 反斜杠混用），指纹不匹配 → 明明批了却仍被拦，还多生成一条申请。
    所以指纹计算前先把路径统一：分隔符归一、`//` 压缩、Windows 下忽略大小写。
    只对"看起来像路径"的值做归一，避免把普通文本也小写化误合并。
    """
    if isinstance(v, str) and _looks_like_path(v):
        s = v.replace("\\", "/")
        while "//" in s:
            s = s.replace("//", "/")
        return s.lower() if os.name == "nt" else s
    return v


def _args_key(tool, args):
    """(工具, 参数) 指纹：参数按 key 排序后序列化，保证语义相同即指纹相同。"""
    try:
        norm = json.dumps({k: _norm_val(v) for k, v in (args or {}).items()},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        norm = str(args)
    raw = "%s|%s" % (tool, norm)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _nearby_granted(d, tool, args):
    """是否有一份"参数略有不同"的已批准申请？用于给 agent 一个可操作的提示。"""
    key_now = _norm_val((args or {}).get("path"))
    if not key_now:
        return None
    for g in d["grants"]:
        if g.get("used") or g.get("expires", 0) < time.time():
            continue
        if g.get("tool") != tool:
            continue
        for r in d["requests"]:
            if r.get("id") != g.get("id"):
                continue
            if _norm_val((r.get("args") or {}).get("path")) == key_now:
                return r
    return None


def _new_id(d):
    """id = HHMMSS + 请求序号两位。★同秒且序号回绕（历史满 100 条）会撞 id，
    撞了就顺延序号重试——id 在状态文件内必须唯一（grant/deny 都按 id 找记录）。"""
    used = {r.get("id") for r in d["requests"]}
    n = len(d["requests"]) % 100
    while True:
        rid = "%s%02d" % (time.strftime("%H%M%S"), n)
        if rid not in used:
            return rid
        n = (n + 1) % 100


# ---------- 对外：调用方用这两个 ----------
def check(cfg, tool, args):
    """是否已被批准？命中则**消费**令牌（单次有效）并返回 True。"""
    if not enabled(cfg):
        return False
    key = _args_key(tool, args)
    now = time.time()
    with _LOCK:
        d = _load()
        for g in d["grants"]:
            if (g.get("used") or g.get("expires", 0) < now
                    or g.get("tool") != tool or g.get("args_key") != key):
                continue
            g["used"] = True
            g["used_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            # 同步申请记录的状态，便于控制台回溯
            for r in d["requests"]:
                if r.get("id") == g.get("id"):
                    r["status"] = "consumed"
            _save(d)
            return True
    return False


def require(cfg, tool, args, reason="", risk="high", trigger=None,
            how_to="", ttl=None, chain=None):
    """拦截并登记一条待审核申请。返回给 agent 的提示 dict。

    调用方约定：先 `check()` 通过才执行；否则调 `require()` 并把返回值交给 agent。
    """
    from core import run_context
    trig = trigger or run_context.trigger()
    risk = risk if risk in RISKS else "high"
    if not enabled(cfg):
        return {"ok": False, "blocked": True, "need_approval": False,
                "reason": "授权申请体系已关闭（approval.enabled=false），高危动作一律拒绝"}
    key = _args_key(tool, args)
    with _LOCK:
        d = _load()
        # 同一 (工具+参数) 已被拒 → 明确告知拒绝方，别让 agent 盲目重试刷重复申请
        for r in d["requests"]:
            if (r.get("status") == "denied" and r.get("tool") == tool
                    and r.get("args_key") == key):
                return _denied_reply(r)
        # 已在等待？不重复排队，直接复用
        for r in d["requests"]:
            if (r.get("status") == "pending" and r.get("tool") == tool
                    and r.get("args_key") == key):
                return _pending_reply(r, how_to)
        req = {"id": _new_id(d),
               "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "epoch": time.time(),
               "tool": tool,
               "args": _safe_args(args),
               "args_key": key,
               "trigger": trig,
               "agent": run_context.agent_name(),
               "risk": risk,
               "reason": (reason or "")[:200],
               "chain": (chain or "")[:80],
               "status": "pending"}
        d["requests"].append(req)
        # 队列长度控制
        pend = [r for r in d["requests"] if r.get("status") == "pending"]
        if len(pend) > MAX_PENDING:
            drop = {r["id"] for r in pend[:len(pend) - MAX_PENDING]}
            d["requests"] = [r for r in d["requests"] if r["id"] not in drop]
        _save(d)
        _audit("REQUEST", req)
        near = _nearby_granted(d, tool, args)
        return _pending_reply(req, how_to, near)


def _safe_args(args):
    """参数入队前裁剪（避免超长 content 把状态文件撑爆）。"""
    out = {}
    for k, v in (args or {}).items():
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        out[k] = s[:300]
    return out


def _pending_reply(req, how_to, near=None):
    msg = ("该操作需要操作者在控制台批准（单次有效）：申请 %s 已提交，状态=待审核。%s"
           % (req["id"], how_to or "批准后再用**相同参数**重试一次即可执行；改了参数要重新申请。"))
    if near is not None:
        msg += ("\n★提示：已有一份**批准过**的同类申请 %s（同一目标），但你这次的参数与它不完全一致"
                "（路径分隔符/大小写差异已自动归一，若仍不匹配说明参数实质不同）。"
                "请用与申请时完全一致的参数重试，或让操作者批准新申请 %s。"
                % (near.get("id"), req["id"]))
    return {"ok": False, "blocked": True, "need_approval": True,
            "request_id": req["id"],
            "tool": req["tool"], "risk": req["risk"],
            "agent": req.get("agent"),
            "reason": req["reason"],
            "message": msg,
            "nearby_granted": near.get("id") if near else None,
            "hint": "控制台 http://127.0.0.1:%d → 授权申请" % _port()}


def _denied_reply(req):
    """★同 (工具+参数) 已被拒 → 明确告知 agent 拒绝方（2026-10-18 批次1）。

    此前 denied 申请在 require 的复用循环里被无视 → agent 重试会新建重复申请、
    拒绝信息从不流回 agent。现在显式返回，让 agent 换方案（换参数=新申请）。"""
    by = req.get("resolved_by") or "console"
    who = {"console": "操作者（控制台）",
           "system:expire": "系统（申请过期自动清理）",
           }.get(by, by if by.startswith("primary:") else "操作者")
    if by.startswith("primary:"):
        who = "主 agent（%s）" % by.split(":", 1)[1]
    return {"ok": False, "blocked": True, "need_approval": False,
            "rejected": True, "rejected_by": by,
            "request_id": req["id"], "tool": req["tool"], "risk": req.get("risk"),
            "agent": req.get("agent"),
            "reason": req.get("reason"),
            "message": ("★该操作此前已被**%s**拒绝（申请 %s，%s）。不要用相同参数重试——"
                        "要么换方案，要么修改参数后重新申请（新参数=新申请），"
                        "或在汇报中说明此路不通。" % (who, req["id"], req.get("resolved_at") or ""))}


def _port():
    try:
        from core import config_loader as cl
        import yaml
        c = yaml.safe_load(open(os.path.join(cl.AGENT_ROOT, "config", "config.yaml"),
                                encoding="utf-8"))
        return int(((c.get("daemon") or {}).get("control_port", 8770)))
    except Exception:
        return 8770


# ---------- 对外：控制台用 ----------
def pending(cfg, limit=30):
    d = _load()
    out = [r for r in d["requests"] if r.get("status") == "pending"]
    out.sort(key=lambda r: r.get("epoch") or 0, reverse=True)
    return out[:limit]


def history(cfg, limit=30):
    d = _load()
    out = [r for r in d["requests"] if r.get("status") != "pending"]
    out.sort(key=lambda r: r.get("epoch") or 0, reverse=True)
    return out[:limit]


def grant(cfg, req_id, ttl=None, by="console"):
    """批准：生成一次性令牌。返回 (ok, msg)。"""
    with _LOCK:
        d = _load()
        req = next((r for r in d["requests"] if r.get("id") == req_id), None)
        if not req:
            return False, "申请 %s 不存在" % req_id
        if req.get("status") != "pending":
            return False, "申请 %s 已处理（状态=%s）" % (req_id, req.get("status"))
        t = ttl or _TTL_BY_RISK.get(req.get("risk", "high"), default_ttl(cfg))
        d["grants"].append({"id": req_id,
                            "tool": req.get("tool"),
                            "args_key": req.get("args_key"),
                            "issued_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "expires": time.time() + t,
                            "ttl_sec": t,
                            "used": False})
        req["status"] = "granted"
        req["resolved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        req["resolved_by"] = by
        req["ttl_sec"] = t
        _save(d)
        _audit("GRANT", req)
        return True, "已批准 %s（%s），%d 秒内用相同参数重试一次即执行" % (
            req_id, req.get("tool"), t)


def deny(cfg, req_id, by="console"):
    with _LOCK:
        d = _load()
        req = next((r for r in d["requests"] if r.get("id") == req_id), None)
        if not req:
            return False, "申请 %s 不存在" % req_id
        if req.get("status") != "pending":
            return False, "申请 %s 已处理（状态=%s）" % (req_id, req.get("status"))
        req["status"] = "denied"
        req["resolved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        req["resolved_by"] = by
        _save(d)
        _audit("DENY", req)
        return True, "已拒绝 %s" % req_id


def grant_all(cfg, by="console"):
    """批量批准当前所有待审核（谨慎）。返回批准条数。"""
    n = 0
    for r in pending(cfg, limit=MAX_PENDING):
        ok, _ = grant(cfg, r["id"], by=by)
        n += 1 if ok else 0
    return n


def expire_stale(cfg):
    """清理过期未用的令牌（控制台轮询时顺手做）。"""
    now = time.time()
    with _LOCK:
        d = _load()
        changed = False
        for g in d["grants"]:
            if not g.get("used") and g.get("expires", 0) < now:
                g["used"] = True
                g["expired"] = True
                changed = True
                for r in d["requests"]:
                    if r.get("id") == g.get("id") and r.get("status") == "granted":
                        r["status"] = "expired"
                        r["resolved_by"] = "system:expire"
                        r["resolved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if changed:
            _save(d)


def _audit(kind, req):
    """审计落盘（与审批状态分离，便于事后追溯"谁批了什么"）。"""
    try:
        p = os.path.join(config_loader.AGENT_ROOT, "logs", "approval_audit.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("[%s] %s id=%s tool=%s risk=%s trigger=%s args=%s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), kind,
                       req.get("id"), req.get("tool"), req.get("risk"),
                       req.get("trigger"),
                       json.dumps(req.get("args") or {}, ensure_ascii=False)[:300]))
    except OSError:
        pass

# -*- coding: utf-8 -*-
"""待办清单（todo_list，2026-10-19 用户指令）。

用途：给 agent 一个**跨唤醒常驻**的待办清单——daemon 每轮唤醒把当前未完成
任务原样注入任务头（【待办清单】块）。与 keyword_memory 的分工：
  关键词记忆 = 按词触发（任务文本命中关键词才注入），记稳定知识
  待办清单   = 无条件每轮注入，记"还没做完的事"
agent 纪律：收到任务/要求立即 action=add 登记，完成后立即 action=del 移除；
**finish_run 之前清单应为空**（做不完的留在里面并在最终汇报里说明原因）。

★与 wake_words / wake_blacklist / keyword_memory 同构：独立 JSON 文件
  （config/todo.json，AI 专属，不动操作者 config.yaml），daemon 每次唤醒/
  轮询按 mtime 重载，改后即生效。

条目结构（entry dict）：
  id       自增编号（删除不复用——next_id 持久化在文件里）
  task     任务描述（≤200 字；去重按 strip 后的原文，同任务重复 add 不重复登记）
  added_by 添加来源（run_context.trigger：manual/mention/scheduled…）
  at       添加时间（审计）

★设计边界与安全（对照 keyword_memory 2026-10-18 分析结论）：
1. 上下文膨胀：清单有硬上限（默认 20 条，机主可调 daemon.todo_list_max），
   注入内容因此有界；达上限 add 被拒（先 del 再加，或合并相近任务）。
2. 持久化提示词注入通道：玩家可诱导 agent 登记虚假/有害任务。缓解：
   条目记来源（trigger）、长度硬限、数量硬限、操作者可随时手改/删文件；
   配合 player_request_risk_review 开关（见 daemon）：玩家来源的要求先审风险。
3. 所有条目操作（add/del）写入 **agent 可读**的审计日志
   logs/todo_audit.log（file_read 可直接看）。
"""
import json
import os
import time

from core import config_loader, run_context

TOOL_NAME = "todo_list"
DEFAULTS = {"max": 20}
MAX_TASK_LEN = 200
AUDIT_NAME = "logs/todo_audit.log"


def _s(cfg):
    return (cfg.get("daemon") or {})


def enabled(cfg):
    return bool(_s(cfg).get("todo_list_enabled", True))


def _limits(cfg):
    def _g(k):
        try:
            return max(1, int(_s(cfg).get("todo_list_" + k, DEFAULTS[k])))
        except (TypeError, ValueError):
            return DEFAULTS[k]
    return (_g("max"),)


def store_path():
    return os.path.join(config_loader.AGENT_ROOT, "config", "todo.json")


def audit_path():
    return os.path.join(config_loader.AGENT_ROOT, AUDIT_NAME)


def _audit(action, entry, detail=""):
    """所有条目操作写 agent 可读的审计日志（一行一条，追加写）。"""
    line = "[%s] %s agent=%s trigger=%s %s%s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), action,
        run_context.agent_name(), run_context.trigger(),
        json.dumps(entry, ensure_ascii=False)[:200],
        (" | " + detail) if detail else "")
    try:
        os.makedirs(os.path.dirname(audit_path()), exist_ok=True)
        with open(audit_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # 审计失败不阻断操作（工具返回里仍会说明）


def _load():
    """读条目（list[dict]）；文件损坏/不存在 → 空表。字段逐个兜底，容忍旧格式。"""
    try:
        p = store_path()
        if not os.path.isfile(p):
            return []
        d = json.load(open(p, encoding="utf-8"))
        items = d.get("entries") if isinstance(d, dict) else None
        out = []
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            task = str(it.get("task") or "").strip()
            if not task:
                continue
            try:
                iid = int(it.get("id"))
            except (TypeError, ValueError):
                iid = 0
            out.append({"id": iid, "task": task,
                        "added_by": str(it.get("added_by") or "?"),
                        "at": str(it.get("at") or "")})
        return out
    except Exception:
        return []


def _load_next_id():
    """下一个自增 id（持久化在文件 next_id 里 → 删除不复用 id）。"""
    try:
        p = store_path()
        if os.path.isfile(p):
            d = json.load(open(p, encoding="utf-8"))
            if isinstance(d, dict):
                try:
                    return max(1, int(d.get("next_id") or 1))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return 1


def _save(items, trigger, next_id=None):
    # ★next_id 默认"就高不就低"：保留文件里已持久化的 next_id（删除不复用 id），
    #   同时兜底 max(现存 id)+1（旧文件/手改文件没有 next_id 时）。
    if next_id is None:
        next_id = max(_load_next_id(), max([e["id"] for e in items] or [0]) + 1)
    d = {"version": 1,
         "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "updated_by_trigger": trigger,
         "next_id": next_id,
         "entries": items}
    p = store_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _norm(text):
    return str(text or "").strip()


def _find_by_task(items, task):
    """按任务原文精确匹配（strip 后）。"""
    t = _norm(task)
    if not t:
        return None
    for it in items:
        if it["task"] == t:
            return it
    return None


def _find_by_ref(items, item_id, task):
    """del 定位：优先按 id（支持 int / "3" / "#3"），退回按任务原文。"""
    if item_id is not None and str(item_id).strip():
        raw = str(item_id).strip().lstrip("#")
        if raw.isdigit():
            want = int(raw)
            for it in items:
                if it["id"] == want:
                    return it
            if _norm(task):
                return _find_by_task(items, task)
            return None
    return _find_by_task(items, task)


def execute(cfg, action="list", task=None, id=None):
    """list / add / del。返回 dict（ok=False 时 reason 说明原因）。"""
    if not enabled(cfg):
        return {"ok": False,
                "reason": "待办清单未启用（daemon.todo_list_enabled=false）"}
    trigger = run_context.trigger()
    agent = run_context.agent_name()
    items = _load()
    (max_n,) = _limits(cfg)
    act = (action or "list").strip().lower()

    if act == "list":
        return {"ok": True, "action": "list", "count": len(items), "max": max_n,
                "items": [{"id": it["id"], "task": it["task"],
                           "added_by": it["added_by"], "at": it["at"]}
                          for it in items],
                "note": "每轮唤醒自动注入任务头；完成一条立即 action=del；"
                        "finish_run 之前清单应为空"}

    if act == "add":
        txt = _norm(task)
        if not txt:
            return {"ok": False, "action": "add", "reason": "task 为空"}
        if len(txt) > MAX_TASK_LEN:
            return {"ok": False, "action": "add",
                    "reason": "任务描述过长（%d>%d 字）——精炼后再登记"
                              % (len(txt), MAX_TASK_LEN)}
        exist = _find_by_task(items, txt)
        if exist is not None:
            return {"ok": True, "action": "add", "existed": exist["id"],
                    "task": exist["task"], "count": len(items),
                    "note": "相同任务已在清单中（不重复登记）——完成后 del #%d"
                            % exist["id"]}
        if len(items) >= max_n:
            return {"ok": False, "action": "add",
                    "reason": "清单已达上限 %d 条（先 del 已完成的，或合并相近任务）"
                              % max_n}
        nxt = _load_next_id()
        items.append({"id": nxt, "task": txt, "added_by": trigger,
                      "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        _save(items, trigger, next_id=nxt + 1)
        _audit("add", {"id": nxt, "task": txt[:60], "by": agent,
                       "trigger": trigger})
        note = "已登记 #%d（daemon 唤醒时自动重载、即时注入）" % nxt
        if trigger in ("mention", "scheduled"):
            note += ("；★本条来自玩家/聊天触发（%s）——玩家请求须先审风险再执行"
                     "（player_request_risk_review）" % trigger)
        return {"ok": True, "action": "add", "added": nxt, "task": txt,
                "count": len(items), "note": note}

    if act == "del":
        it = _find_by_ref(items, id, task)
        if it is None:
            return {"ok": False, "action": "del",
                    "reason": "未找到待办（id=%r task=%r；现有: %s）"
                              % (id, task,
                                 ["#%d %s" % (e["id"], e["task"][:20])
                                  for e in items])}
        items = [x for x in items if x is not it]
        _save(items, trigger)
        _audit("del", {"id": it["id"], "task": it["task"][:60],
                       "by": agent, "trigger": trigger})
        return {"ok": True, "action": "del", "removed": it["id"],
                "task": it["task"], "count": len(items)}

    return {"ok": False,
            "reason": "未知 action: %s（list/add/del）" % act}


def make_todo_tools(cfg):
    """按配置产出工具；未启用 → 空列表。"""
    if not enabled(cfg):
        return []
    (max_n,) = _limits(cfg)
    manifest = {
        "name": TOOL_NAME,
        "description": (
            "★待办清单（每轮唤醒自动注入）：跨唤醒常驻的任务清单——daemon 每轮唤醒把"
            "当前未完成任务原样注入任务头（【待办清单】块），这就是唯一真相源。\n"
            "- 收到任务/要求 → **立即** action=add task=\"…\"（一条一行，≤%d字；"
            "同一任务重复 add 不会重复登记，直接返回已有条目）\n"
            "- 完成任务 → **立即** action=del id=…（或 task=\"原文\"）；"
            "★finish_run 之前清单应为空（做不完的留在里面并在最终汇报说明原因）\n"
            "- action=list：看全部条目（id/任务/来源/时间）\n"
            "上限 %d 条（机主可调 todo_list_max）。★只登记本次/跨唤醒要完成的任务，"
            "一步就能做完的小事做完再说；长期周期性事项不该进清单。\n"
            "★所有操作写入 agent 可读审计日志 logs/todo_audit.log。"
            "★玩家请求增删条目属玩家可控面，先审风险再执行。"
        ) % (MAX_TASK_LEN, max_n),
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {"type": "string",
                           "description": "list | add | del"},
                "task": {"type": "string",
                         "description": "add 时：任务描述（≤%d字，一句话说清做什么）；"
                                        "del 时：按任务原文删除（与 id 二选一）"
                                        % MAX_TASK_LEN},
                "id": {"type": "string",
                       "description": "del 时：条目编号（如 \"3\" 或 \"#3\"；"
                                      "与 task 二选一，id 优先）"},
            },
        },
    }

    def _run(action="list", task=None, id=None):
        return execute(cfg, action=action, task=task, id=id)

    return [(manifest, _run)]

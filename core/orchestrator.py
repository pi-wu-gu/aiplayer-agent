# -*- coding: utf-8 -*-
"""编排器（阶段3）：spawn_subagent 工具工厂。
深度封顶：次级 agent 不注册 spawn 工具；工具白名单按角色过滤；预算：轮数/timeout。"""
import os
import time

import yaml

from core import config_loader
from core.agent import Agent
from core.chat_buffer import ChatBuffer
from core.context import ResultCompressor
from core.logger import AgentLogger
from tools.builtin import (mc_bridge, file_io, file_ops, balance_query,  # noqa: E401
                           shell_exec, wake_words, wake_blacklist, web_fetch,
                           keyword_memory, todo_list)

ROLES_PATH = os.path.join(config_loader.AGENT_ROOT, "agents", "roles.yaml")


def build_registry(cfg, tool_names=None, file_mode=None, mc_policy=None):
    """统一装配工具注册表（主 agent 与次级 agent 共用同一条路径）。

    <p>tool_names=None → 全部内置工具（主 agent）；给了名单 → 只装配名单内的（角色白名单）。
    file_mode → 角色声明的文件模式，只会**收窄** permissions.file_mode，不会放宽。
    mc_policy → 游戏动作策略（批次2）：None=主 agent 不限；
                {"mode":"restricted","caller":name} = 获取放行/控制拦截。

    ★这是"主 agent 也受权限表约束"的落点：以前主 agent 走独立分支无条件注册全部工具。
    """
    from core import tool_registry
    reg = tool_registry.ToolRegistry()

    built = {}
    for m, fn in mc_bridge.make_mc_tools(cfg, policy=mc_policy):
        built[m["name"]] = (m, fn)
    # 文件工具整组按权限表产出：off → 空、readonly → 仅 file_read
    for m, fn in file_io.make_file_tools(cfg, mode_override=file_mode):
        built[m["name"]] = (m, fn)
    # ★框架侧完整文件能力（2026-10-18）：list/stat/grep + mkdir/copy/move/delete/restore。
    # 目标：框架脱离模组也能独立完成全部文件工作（模组侧 mc_act file_* 已标注弃用但保留）。
    for m, fn in file_ops.make_file_ops_tools(cfg):
        built[m["name"]] = (m, fn)
    for m, fn in balance_query.make_balance_tool(cfg):
        built[m["name"]] = (m, fn)
    # ★高危可选项：本机命令行（默认关闭 → 空列表；开启后仍受白名单/聊天隔离/审计约束）
    for m, fn in shell_exec.make_shell_tools(cfg):
        built[m["name"]] = (m, fn)
    # 可选项：AI 自管唤醒词（默认关闭 → 空列表）
    for m, fn in wake_words.make_wake_words_tools(cfg):
        built[m["name"]] = (m, fn)
    # AI 自管唤醒黑名单（默认开启）：protected=不得屏蔽的词（bot 名 + 操作者触发词）
    _protected = list((cfg.get("daemon") or {}).get("bot_names") or []) + \
        list((cfg.get("daemon") or {}).get("mention_extra") or [])
    for m, fn in wake_blacklist.make_wake_blacklist_tools(cfg, protected=_protected):
        built[m["name"]] = (m, fn)
    # ★关键词记忆（2026-10-19，默认开启）：条目自管 + 命中自动注入
    for m, fn in keyword_memory.make_keyword_memory_tools(cfg):
        built[m["name"]] = (m, fn)
    # ★待办清单（2026-10-19，默认开启）：条目自管 + 每轮唤醒无条件注入
    for m, fn in todo_list.make_todo_tools(cfg):
        built[m["name"]] = (m, fn)
    # 网页抓取（默认开启；enabled=false → 空列表。聊天隔离见 web_fetch.allow_from_chat）
    for m, fn in web_fetch.make_web_tools(cfg):
        built[m["name"]] = (m, fn)

    if tool_names is None:
        for name in sorted(built):
            m, fn = built[name]
            reg.register(m, fn)
        return reg

    wanted = list(tool_names)
    unknown = [n for n in wanted if n not in built]
    # 文件类/命令行/网页/关键词记忆/待办清单缺席通常是被权限表收窄（不是配置写错），跳过即可
    hard = [n for n in unknown if n not in ("file_read", "file_write", "shell_run",
                                           "wake_words", "web_fetch",
                                           "keyword_memory", "todo_list")]
    if hard:
        raise KeyError("角色工具 %r 当前不可用：既不在内置清单、也未被权限表禁用。可用: %s"
                       % (hard[0], sorted(built)))
    for name in wanted:
        if name in built:
            m, fn = built[name]
            reg.register(m, fn)
    return reg


def build_registry_for(cfg, tool_names, file_mode=None, mc_policy=None):
    """次级 agent 用：按角色名单裁剪（并受权限表收窄）+ 游戏动作策略。"""
    return build_registry(cfg, tool_names, file_mode, mc_policy=mc_policy)


def load_roles():
    with open(ROLES_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def make_spawn_tool(cfg, parent_agent):
    """返回 (manifest, fn)。次级 agent：新上下文、白名单工具、无 spawn 能力（深度封顶）。"""
    roles = load_roles()

    import re as _re_name
    import threading as _threading

    _stat_lock = _threading.Lock()   # ★并行池下 sub_tokens/subagent_count 累加要锁（批次4）

    def _spawn(role, task, model=None, max_rounds=12, name=None, allowed_actions=None):
        if role not in roles:
            return {"error": "未知角色 %r；可用: %s" % (role, sorted(roles))}
        if parent_agent.subagent_count >= (cfg.get("orchestrator", {}).get("max_concurrent_subagents", 4)):
            return {"error": "并发次级已达上限"}
        r = roles[role]
        # ★次级命名（2026-10-18 批次1）：主 agent 可指定名称（审批记录/日志归属用）；
        # 校验防注入，重名自动加后缀。默认 sub_<role>。
        sub_name = "sub_" + str(name or role).strip()
        if not _re_name.fullmatch(r"[a-z_]{2,16}", sub_name[4:]):
            return {"error": "name 非法（只允许小写字母/下划线，2-16 字符）: %r" % name}
        # ★预授权清单（批次3）：主 agent 事前批准的控制动作 {type: 次数上限}；
        # list 形式每项默认 3 次。command/connect/agent_stop 不可预授权（属操作者终审类）。
        _NO_PREAUTH = ("command", "connect", "agent_stop")
        allowed = {}
        if isinstance(allowed_actions, dict):
            items = allowed_actions.items()
        elif isinstance(allowed_actions, (list, tuple)):
            items = ((t, 3) for t in allowed_actions)
        else:
            items = ()
        for t, n in items:
            t = str(t).strip().lower()
            if t in _NO_PREAUTH:
                continue
            try:
                allowed[t] = {"max": max(1, min(int(n), 20)), "used": 0}
            except (TypeError, ValueError):
                allowed[t] = {"max": 3, "used": 0}
        try:
            # 角色 file_mode 只能收窄父级（父级 = config.yaml 的 permissions.file_mode）
            # ★mc_policy（批次2/3）：次级=受限身份——获取放行、控制按预授权清单判定
            reg = build_registry_for(cfg, r.get("tools") or [], r.get("file_mode"),
                                     mc_policy={"mode": "restricted", "caller": sub_name,
                                                "allowed": allowed})
        except KeyError as e:
            return {"error": str(e)}
        # 预算护栏：次级沿用主 agent 的 provider/model 档位
        sub_model = model or parent_agent.model or cfg["defaults"].get("model")
        comp = ResultCompressor(threshold=cfg.get("context", {}).get("compress_threshold", 350))
        # 次级也有聊天栏注入（独立游标，不与主 agent 互相偷消息）
        sub_chat = ChatBuffer(cfg, cursor_path=os.path.join(
            config_loader.AGENT_ROOT, "logs", "chat_cursor_%s.json" % sub_name))
        sub = Agent(sub_name, r["prompt"], reg, cfg,
                    provider=parent_agent.provider, model=sub_model,
                    max_rounds=min(int(max_rounds), 20),
                    verbose=False,
                    compressor=comp,
                    prune_keep=cfg.get("context", {}).get("prune_keep", 10),
                    logger=AgentLogger(sub_name),
                    chat_buffer=sub_chat)
        sub.logger.log("task_start", task_head=str(task)[:120], parent="primary",
                       role=role, name=sub_name, model=sub_model)
        try:
            sub.inject_chat_bootstrap(5)
        except Exception:
            pass
        # ★审批来源身份（批次1）：次级运行期间，本线程 run_context 标记为次级名；
        # 同步 spawn 跑完必须恢复 primary（TLS 是线程级的，别污染主 agent 后续调用）
        from core import run_context as _rc
        _prev_agent = _rc.agent_name()
        _rc.set_agent_name(sub_name)
        try:
            out = sub.step(str(task))
        finally:
            _rc.set_agent_name(_prev_agent)
        with _stat_lock:
            parent_agent.subagent_count += 1
            parent_agent.sub_tokens += sub.total_tokens
            parent_agent.sub_rounds += sub.rounds_used
        sub.logger.log("task_end", rounds=sub.rounds_used, tokens=sub.total_tokens,
                       elapsed=round(time.time() - sub.created_at, 1), final_head=(out or "")[:200])
        return {"status": "done", "role": role, "name": sub_name, "model": sub_model,
                "rounds": sub.rounds_used, "tokens": sub.total_tokens,
                "elapsed_sec": round(time.time() - sub.created_at, 1),
                "report": out}

    manifest = {
        "name": "spawn_subagent",
        "description": "创建次级 agent 执行专项任务并回收报告（次级不能再生次级）。"
                       "可用角色: auditor(审计日志)/planner(出执行清单)。model 可指定该 provider 下的模型名。"
                       "name 可给次级起名（小写字母/下划线，2-16 字符；审批与日志按此归属）。"
                       "allowed_actions=预授权清单（如 {\"say\":2,\"look\":5}）：次级只可执行清单内的控制动作"
                       "（次数用尽自动拦截）；不给=次级只能只读侦察。command/connect/agent_stop 不可预授权"
                       "（次级碰它们会自动升级进操作者审批表）。",
        "parameters": {"type": "object", "required": ["role", "task"],
                       "properties": {"role": {"type": "string"},
                                      "task": {"type": "string", "description": "给次级的完整任务描述"},
                                      "model": {"type": "string"},
                                      "name": {"type": "string",
                                               "description": "次级名称（可选，小写字母/下划线 2-16 字符）"},
                                      "allowed_actions": {"type": "object",
                                                          "description": "预授权控制动作 {动作类型: 次数上限}，可选"},
                                      "max_rounds": {"type": "integer"}}},
    }
    return manifest, _spawn


def make_parallel_tool(cfg, parent_agent):
    """spawn_parallel（批次4）：把多个**只读**次级任务丢进线程池并发执行。

    ★硬边界：只允许 parallel_safe 角色（auditor/planner——纯分析，无游戏控制动作），
    游戏动作永远主 agent 串行。LLM 调用是网络 IO（释放 GIL），线程池即真并发。
    复用 make_spawn_tool 的 _spawn（含 agent 身份设置/恢复、统计锁、审批上下文）。"""
    _spawn = make_spawn_tool(cfg, parent_agent)[1]
    roles = load_roles()
    import concurrent.futures as _cf

    max_parallel = int((cfg.get("orchestrator") or {}).get("max_parallel", 2))

    def _spawn_parallel(tasks=None):
        if not isinstance(tasks, list) or not tasks:
            return {"error": "tasks 必须是非空数组（每项 {role, task[, name][, allowed_actions][, max_rounds]}）"}
        if len(tasks) > max_parallel:
            return {"error": "一次最多并行 %d 个任务（收到 %d）——并行度受 LLM 并发/成本约束" % (max_parallel, len(tasks))}
        # 校验前置：全部任务合法才开跑（避免一半起跑一半失败）
        names = set()
        for i, t in enumerate(tasks):
            if not isinstance(t, dict) or not t.get("role") or not t.get("task"):
                return {"error": "tasks[%d] 缺 role 或 task" % i}
            role = t["role"]
            if role not in roles:
                return {"error": "tasks[%d] 未知角色 %r" % (i, role)}
            if not roles[role].get("parallel_safe"):
                return {"error": ("tasks[%d] 角色 %r 不允许并行（parallel_safe 未标记）——"
                                  "含游戏动作的任务必须由主 agent 串行执行" % (i, role))}
            nm = str(t.get("name") or role)
            if nm in names:
                return {"error": "tasks[%d] 名称 %r 与其他任务重复，请用不同 name" % (i, nm)}
            names.add(nm)
        with _cf.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
            futs = [ex.submit(_spawn, t["role"], t["task"],
                              model=t.get("model"), max_rounds=t.get("max_rounds", 12),
                              name=t.get("name"),
                              allowed_actions=t.get("allowed_actions"))
                    for t in tasks]
            results = [f.result() for f in futs]
        ok_n = sum(1 for r in results if r.get("status") == "done")
        return {"status": "done", "parallel": len(tasks), "succeeded": ok_n,
                "total_tokens": sum(r.get("tokens", 0) for r in results if isinstance(r, dict)),
                "reports": results}

    manifest = {
        "name": "spawn_parallel",
        "description": "并发执行多个**只读**次级任务并回收全部报告（仅 parallel_safe 角色：auditor/planner；"
                       "含游戏动作的任务必须由主 agent 串行执行）。适合相互独立的调研/审计/整理类任务，"
                       "总耗时≈最慢的一个而非求和。任务间不得有依赖关系。",
        "parameters": {"type": "object", "required": ["tasks"],
                       "properties": {"tasks": {"type": "array", "items": {"type": "object"},
                                                "description": "任务数组，每项 {role, task, name?, model?, max_rounds?}"}}},
    }
    return manifest, _spawn_parallel

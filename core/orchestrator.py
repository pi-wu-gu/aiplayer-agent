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
                           shell_exec, wake_words, wake_blacklist, web_fetch)

ROLES_PATH = os.path.join(config_loader.AGENT_ROOT, "agents", "roles.yaml")


def build_registry(cfg, tool_names=None, file_mode=None):
    """统一装配工具注册表（主 agent 与次级 agent 共用同一条路径）。

    <p>tool_names=None → 全部内置工具（主 agent）；给了名单 → 只装配名单内的（角色白名单）。
    file_mode → 角色声明的文件模式，只会**收窄** permissions.file_mode，不会放宽。

    ★这是"主 agent 也受权限表约束"的落点：以前主 agent 走独立分支无条件注册全部工具。
    """
    from core import tool_registry
    reg = tool_registry.ToolRegistry()

    built = {}
    for m, fn in mc_bridge.make_mc_tools(cfg):
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
    # 文件类/命令行/网页缺席通常是被权限表收窄（不是配置写错），跳过即可
    hard = [n for n in unknown if n not in ("file_read", "file_write", "shell_run",
                                           "wake_words", "web_fetch")]
    if hard:
        raise KeyError("角色工具 %r 当前不可用：既不在内置清单、也未被权限表禁用。可用: %s"
                       % (hard[0], sorted(built)))
    for name in wanted:
        if name in built:
            m, fn = built[name]
            reg.register(m, fn)
    return reg


def build_registry_for(cfg, tool_names, file_mode=None):
    """次级 agent 用：按角色名单裁剪（并受权限表收窄）。"""
    return build_registry(cfg, tool_names, file_mode)


def load_roles():
    with open(ROLES_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def make_spawn_tool(cfg, parent_agent):
    """返回 (manifest, fn)。次级 agent：新上下文、白名单工具、无 spawn 能力（深度封顶）。"""
    roles = load_roles()

    def _spawn(role, task, model=None, max_rounds=12):
        if role not in roles:
            return {"error": "未知角色 %r；可用: %s" % (role, sorted(roles))}
        if parent_agent.subagent_count >= (cfg.get("orchestrator", {}).get("max_concurrent_subagents", 4)):
            return {"error": "并发次级已达上限"}
        r = roles[role]
        try:
            # 角色 file_mode 只能收窄父级（父级 = config.yaml 的 permissions.file_mode）
            reg = build_registry_for(cfg, r.get("tools") or [], r.get("file_mode"))
        except KeyError as e:
            return {"error": str(e)}
        # 预算护栏：次级沿用主 agent 的 provider/model 档位
        sub_model = model or parent_agent.model or cfg["defaults"].get("model")
        comp = ResultCompressor(threshold=cfg.get("context", {}).get("compress_threshold", 350))
        # 次级也有聊天栏注入（独立游标，不与主 agent 互相偷消息）
        sub_chat = ChatBuffer(cfg, cursor_path=os.path.join(
            config_loader.AGENT_ROOT, "logs", "chat_cursor_sub_%s.json" % role))
        sub = Agent("sub_" + role, r["prompt"], reg, cfg,
                    provider=parent_agent.provider, model=sub_model,
                    max_rounds=min(int(max_rounds), 20),
                    verbose=False,
                    compressor=comp,
                    prune_keep=cfg.get("context", {}).get("prune_keep", 10),
                    logger=AgentLogger("sub_" + role),
                    chat_buffer=sub_chat)
        sub.logger.log("task_start", task_head=str(task)[:120], parent="primary", role=role, model=sub_model)
        try:
            sub.inject_chat_bootstrap(5)
        except Exception:
            pass
        out = sub.step(str(task))
        parent_agent.subagent_count += 1
        parent_agent.sub_tokens += sub.total_tokens
        parent_agent.sub_rounds += sub.rounds_used
        sub.logger.log("task_end", rounds=sub.rounds_used, tokens=sub.total_tokens,
                       elapsed=round(time.time() - sub.created_at, 1), final_head=(out or "")[:200])
        return {"status": "done", "role": role, "model": sub_model,
                "rounds": sub.rounds_used, "tokens": sub.total_tokens,
                "elapsed_sec": round(time.time() - sub.created_at, 1),
                "report": out}

    manifest = {
        "name": "spawn_subagent",
        "description": "创建次级 agent 执行专项任务并回收报告（次级不能再生次级）。"
                       "可用角色: auditor(审计日志)/planner(出执行清单)。model 可指定该 provider 下的模型名。",
        "parameters": {"type": "object", "required": ["role", "task"],
                       "properties": {"role": {"type": "string"},
                                      "task": {"type": "string", "description": "给次级的完整任务描述"},
                                      "model": {"type": "string"},
                                      "max_rounds": {"type": "integer"}}},
    }
    return manifest, _spawn

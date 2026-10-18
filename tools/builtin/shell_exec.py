# -*- coding: utf-8 -*-
"""本机命令行执行 shell_run —— ★高危可选项，**默认关闭**。

放置位置说明：命令行跑在**宿主机器**上，属 agent 框架（本机自用工具）的职责；
模组是"丢进任意 1.21.1 整合包就能用"的可分发组件，不承载 OS 执行面。

四道闸（即便开启也仍然生效）：
  1. **默认关闭**：permissions.shell.enabled=false → 本工具**根本不注册**（模型看不到，不浪费轮次）
  2. **不给 shell**：subprocess(shell=False)，只按 argv 精确执行；
     管道 `|`、重定向 `>`、`&&`、`$(...)` 一律**不会被解释**（它们是普通参数字符串）
  3. **白名单**：mode=allowlist 时 argv[0] 必须是**裸程序名**（不含 `/` `\\`）且在 allowlist 内，
     且能用 shutil.which 解析到；显式路径调用一律拒绝
  4. **聊天隔离**：由游戏聊天/@提及/定时积压触发的运行**默认禁止**调用（allow_from_chat=false）——
     防"提示词注入 → 远程代码执行"（聊天内容来自服务器上的其他玩家，是不可信输入）

另外：工作目录限制在 AGENT_ROOT 内、超时强杀、输出截断、**每次调用都落盘审计**。
"""
import json
import os
import shlex
import shutil
import subprocess
import time

from core import config_loader, run_context

TOOL_NAME = "shell_run"
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_OUTPUT = 8000


def _s(cfg):
    """permissions.shell 段（缺省空 dict）。"""
    return (cfg.get("permissions") or {}).get("shell") or {}


def enabled(cfg):
    return bool(_s(cfg).get("enabled"))


def _audit_path(cfg):
    rel = _s(cfg).get("audit_log") or "logs/shell_audit.log"
    return os.path.join(config_loader.AGENT_ROOT, rel)


def _audit(cfg, entry):
    try:
        p = _audit_path(cfg)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 审计失败不得影响主流程


def startup_notice(cfg):
    """启动时的危险性提示（run.py 在起任务/守护前打印）。返回文本行列表。"""
    s = _s(cfg)
    if not s.get("enabled"):
        return ["[权限表] 本机命令行 shell_run：**已关闭**（默认）。如需启用见 config.yaml 的 permissions.shell"]
    mode = (s.get("mode") or "allowlist").lower()
    allow = s.get("allowlist") or []
    chat = bool(s.get("allow_from_chat", False))
    lines = [
        "",
        "################  高危能力已开启  ################",
        "  permissions.shell.enabled = true —— agent 可以在这台电脑上运行程序",
        "    模式      : %s%s" % (mode, ("  白名单=%s" % allow) if mode == "allowlist" else "（任意程序，最危险）"),
        "    聊天触发  : %s" % ("**也允许**（服务器里任何玩家都可能借此在你的电脑上执行命令！）"
                                if chat else "禁止（allow_from_chat=false，防提示词注入）"),
        "    工作目录  : %s" % _workdir(cfg, None),
        "    超时/截断 : %s 秒 / %s 字符" % (s.get("timeout_sec", DEFAULT_TIMEOUT),
                                            s.get("max_output", DEFAULT_MAX_OUTPUT)),
        "    审计落盘  : %s" % _audit_path(cfg),
        "  建议：只在手动跑任务时临时开启，用完把 enabled 改回 false。",
        "##################################################",
        "",
    ]
    return lines


def _workdir(cfg, workdir):
    """工作目录：必须在 AGENT_ROOT 之内（越界回退到 AGENT_ROOT）。"""
    root = os.path.abspath(config_loader.AGENT_ROOT)
    rel = workdir if workdir else (_s(cfg).get("workdir") or ".")
    p = os.path.abspath(os.path.join(root, rel))
    try:
        if os.path.commonpath([root, p]) != root:
            return root
    except ValueError:      # 不同盘符
        return root
    return p


def _split_cmd(cmd):
    """把 cmd 字符串分词（**不经 shell**）。

    ★Windows 上必须用 posix=False：否则 `C:\\path\\x.exe` 的反斜杠会被当转义吃掉（实测踩过）。
    posix=False 会保留引号，故再手工剥掉 token 两端成对的引号。
    """
    if os.name == "nt":
        toks = shlex.split(cmd, posix=False)
        out = []
        for t in toks:
            if len(t) >= 2 and t[0] == t[-1] and t[0] in ("\"", "'"):
                out.append(t[1:-1])
            else:
                out.append(t)
        return out
    return shlex.split(cmd)


def _resolve_argv(cfg, argv, cmd):
    """把入参整理成 argv 列表。"""
    if argv:
        if isinstance(argv, str):
            argv = _split_cmd(argv)
        lst = [str(x) for x in argv]
    elif cmd:
        # 仅做分词，**不经过 shell**：`|` `>` `&&` `$( )` 都只是普通参数
        lst = _split_cmd(str(cmd))
    else:
        return None, "缺少 argv（或 cmd）；用法 shell_run argv=[\"程序\",\"参数\"…]"
    if not lst:
        return None, "argv 为空"
    if len(lst) > 64:
        return None, "argv 参数过多（>64）"
    return lst, None


def _check_allowlist(cfg, argv):
    """白名单闸。返回 (ok, 解析后的可执行路径, 拒绝原因)。"""
    mode = (_s(cfg).get("mode") or "allowlist").lower()
    if mode == "any":
        return True, argv[0], None
    allow = [str(x).lower() for x in (_s(cfg).get("allowlist") or [])]
    if not allow:
        return False, None, ("白名单为空：mode=allowlist 但 permissions.shell.allowlist 没配任何程序，"
                             "已按最严处理（一律拒绝）。要么加白名单，要么显式设 mode=any")
    prog = argv[0]
    if "/" in prog or "\\" in prog:
        return False, None, ("白名单模式只接受**裸程序名**（依赖 PATH 解析），不接受显式路径: %s" % prog)
    if prog.lower() not in allow:
        # ★NOT_IN_ALLOWLIST: 前缀标记（2026-10-18 P1）：这种情况可走授权申请；
        # 显式路径/空白名单/PATH 找不到是结构性错误，审批也没意义，继续硬拒。
        return False, None, ("NOT_IN_ALLOWLIST:程序 %s 不在白名单内（当前白名单: %s）" % (prog, allow))
    resolved = shutil.which(prog)
    if not resolved:
        return False, None, ("PATH 里找不到程序: %s" % prog)
    return True, resolved, None


def execute(cfg, argv=None, cmd=None, timeout=None, workdir=None):
    """执行一次命令行。返回结果 dict（失败也给 dict，带 ok=false + reason）。"""
    t0 = time.time()
    if not enabled(cfg):
        return {"ok": False, "reason": "本机命令行未启用（permissions.shell.enabled=false）"}

    # 闸 4：聊天隔离（防注入）
    if run_context.is_untrusted_trigger() and not _s(cfg).get("allow_from_chat", False):
        r = {"ok": False, "reason": ("本次运行由游戏聊天/定时积压触发（trigger=%s），"
                                     "按配置禁止调用本机命令行（allow_from_chat=false）。"
                                     "如需放行请显式改配置，并清楚其风险。" % run_context.trigger())}
        _audit(cfg, {"ts": time.time(), "trigger": run_context.trigger(), "argv": argv or cmd,
                     "ok": False, "reason": "chat-isolation"})
        return r

    lst, err = _resolve_argv(cfg, argv, cmd)
    if err:
        return {"ok": False, "reason": err}

    # 闸 3：白名单
    ok, resolved, why = _check_allowlist(cfg, lst)
    if not ok:
        # ★越白名单走授权申请（2026-10-18 P1）：仅 NOT_IN_ALLOWLIST 可申请（critical），
        # 其余结构性错误照旧硬拒。指纹=完整 argv+workdir，重试必须同参数。
        # ★令牌消费放行后必须跳过下方硬拒 return（file_delete 同款坑：check 通过
        # 却继续 return 拒绝——令牌被白白消费且操作不执行）。
        approved = False
        if why and why.startswith("NOT_IN_ALLOWLIST:"):
            from core import approval
            aargs = {"argv": lst, "workdir": str(workdir) if workdir else None}
            if approval.check(cfg, "shell_exec", aargs):
                resolved = shutil.which(lst[0])
                if not resolved:
                    return {"ok": False, "reason": "PATH 里找不到程序: %s" % lst[0], "argv": lst}
                approved = True
            elif approval.enabled(cfg):
                _audit(cfg, {"ts": time.time(), "trigger": run_context.trigger(), "argv": lst,
                             "ok": False, "reason": "allowlist-approval-requested"})
                return approval.require(
                    cfg, "shell_exec", aargs,
                    reason="命令越白名单: %s" % " ".join(str(x) for x in lst)[:80],
                    risk="critical",
                    how_to="控制台「🛡 授权申请」批准后，用**完全相同的参数**再调一次 shell_exec 即执行。")
            else:
                why = why[len("NOT_IN_ALLOWLIST:"):]
        if not approved:
            _audit(cfg, {"ts": time.time(), "trigger": run_context.trigger(), "argv": lst,
                         "ok": False, "reason": "allowlist: " + why})
            return {"ok": False, "reason": why, "argv": lst}

    cwd = _workdir(cfg, workdir)
    if not os.path.isdir(cwd):
        return {"ok": False, "reason": "工作目录不存在: %s" % cwd}

    to = int(timeout or _s(cfg).get("timeout_sec", DEFAULT_TIMEOUT))
    to = max(1, min(to, 600))
    cap = int(_s(cfg).get("max_output", DEFAULT_MAX_OUTPUT))

    try:
        # ★shell=False：argv 直接交给 CreateProcess/execve，绝不经 shell 解析
        p = subprocess.run([resolved] + lst[1:], cwd=cwd, timeout=to,
                           capture_output=True, text=True, errors="replace", shell=False)
        rc, out, serr = p.returncode, p.stdout or "", p.stderr or ""
        timed_out = False
    except subprocess.TimeoutExpired as e:
        rc, timed_out = -1, True
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        serr = "timeout after %ss" % to
    except Exception as e:
        _audit(cfg, {"ts": time.time(), "trigger": run_context.trigger(), "argv": lst,
                     "cwd": cwd, "ok": False, "reason": "exec-error: %s" % e})
        return {"ok": False, "reason": "执行失败: %s" % e, "argv": lst}

    ms = int((time.time() - t0) * 1000)
    trunc = len(out) > cap or len(serr) > cap
    res = {
        "ok": (rc == 0 and not timed_out),
        "argv": lst,
        "resolved": resolved,
        "cwd": cwd,
        "rc": rc,
        "timed_out": timed_out,
        "ms": ms,
        "stdout": out[:cap],
        "stderr": serr[:cap],
        "truncated": trunc,
        "trigger": run_context.trigger(),
    }
    _audit(cfg, {"ts": time.time(), "trigger": run_context.trigger(), "argv": lst, "cwd": cwd,
                 "resolved": resolved, "rc": rc, "ms": ms, "ok": res["ok"],
                 "stdout_head": out[:400], "stderr_head": serr[:400]})
    return res


def make_shell_tools(cfg):
    """按配置产出工具；未启用 → 空列表（工具不注册）。"""
    if not enabled(cfg):
        return []
    s = _s(cfg)
    mode = (s.get("mode") or "allowlist").lower()
    allow = s.get("allowlist") or []

    desc = (
        "★高危：在本机（宿主电脑）执行命令行。默认关闭，当前**已由机主显式开启**。\n"
        "· 只按 argv 精确执行，**没有 shell**：管道/重定向/&&/$( ) 不会被解释（只是普通参数）\n"
        "· 模式=%s%s\n"
        "· 工作目录限 %s；超时 %s 秒；输出截断 %s 字符；每次调用落盘审计\n"
        "· 由游戏聊天/@提及触发的运行%s\n"
        "· 纪律：只做确实必要的事，能一步做完就不要来回试；执行前想清楚这条命令会不会改文件/删东西。"
        % (mode,
           ("，白名单=%s（argv[0] 只能是裸程序名）" % allow) if mode == "allowlist"
           else "（任意程序——机主已接受风险）",
           _workdir(cfg, None), s.get("timeout_sec", DEFAULT_TIMEOUT), s.get("max_output", DEFAULT_MAX_OUTPUT),
           "**也被允许**（注意风险）" if s.get("allow_from_chat", False) else "一律被拒绝（防提示词注入）")
    )
    manifest = {
        "name": TOOL_NAME,
        "description": desc,
        "parameters": {
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"},
                         "description": "程序与参数数组，如 [\"python\",\"-c\",\"print(1)\"]；推荐用这个"},
                "cmd": {"type": "string",
                        "description": "备用写法：命令行字符串（仅按引号分词，不走 shell）"},
                "timeout": {"type": "integer", "description": "超时秒数（默认取配置）"},
                "workdir": {"type": "string", "description": "相对 AGENT_ROOT 的工作目录（默认为配置值）"},
            },
            "required": [],
        },
    }

    def _run(argv=None, cmd=None, timeout=None, workdir=None):
        return execute(cfg, argv=argv, cmd=cmd, timeout=timeout, workdir=workdir)

    return [(manifest, _run)]

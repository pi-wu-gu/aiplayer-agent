# -*- coding: utf-8 -*-
"""运行上下文：本次运行是**谁触发的**。

用途：给"高危工具"做来源隔离 —— 例如本机命令行（shell_run）默认只允许
**操作者手动触发**的运行调用，而由游戏聊天/ @提及 / 定时积压唤醒的运行一律禁止。
这是防"提示词注入 → 远程代码执行"的关键一环：聊天内容是不可信输入。

实现：threading.local（daemon 的每次唤醒运行跑在独立线程里，互不干扰）。
未显式设置时视为 "manual"（= 操作者在终端手动跑）。
"""
import threading

_MANUAL = "manual"
_tls = threading.local()


def set_trigger(kind):
    """设置本次运行的触发源：manual | mention | scheduled | api。"""
    _tls.kind = (kind or _MANUAL).strip().lower()


def trigger():
    """当前运行触发源；未设置 → manual。"""
    return getattr(_tls, "kind", _MANUAL)


def is_untrusted_trigger():
    """触发源是否"来自不可信输入"（游戏聊天/@提及/定时积压处理聊天）。

    ★注意：scheduled 唤醒处理的是聊天积压，其内容同样不可信，故一并计入。
    """
    return trigger() in ("mention", "scheduled")

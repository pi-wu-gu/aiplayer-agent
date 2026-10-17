# -*- coding: utf-8 -*-
"""AI 自管唤醒词（可选，默认关闭）。

用途：允许 agent 自己维护"额外唤醒词"——比如玩家在游戏里说"以后叫我小深就能找到我"，
agent 决定把「小深」加进唤醒词，之后玩家说"小深"就能唤醒它。

★设计边界：
- 词存独立文件 config/wake_words.json（AI 专属，**不动操作者的 config.yaml**），
  daemon 每次聊天轮询自动重载（mtime 变化才重读），加词立即生效、重启保留
- 受 daemon.allow_ai_wake_words 开关约束（默认 false）：关=工具不注册+已存词不生效
- 滥用防护：数量上限（ai_wake_words_max，默认 8）、词长 1-16、去重、
  每次增删记录触发源（trigger）审计——聊天触发的加词意味着玩家在间接控制唤醒行为，
  上限+公告+审计把风险压住；操作者随时可手改/删该文件
"""
import json
import os
import time

from core import config_loader, run_context

TOOL_NAME = "wake_words"
DEFAULT_MAX = 8


def _s(cfg):
    return (cfg.get("daemon") or {})


def enabled(cfg):
    return bool(_s(cfg).get("allow_ai_wake_words"))


def _max_words(cfg):
    try:
        return max(1, int(_s(cfg).get("ai_wake_words_max", DEFAULT_MAX)))
    except (TypeError, ValueError):
        return DEFAULT_MAX


def store_path():
    return os.path.join(config_loader.AGENT_ROOT, "config", "wake_words.json")


def _load():
    """读唤醒词列表；文件损坏/不存在 → 空表。"""
    try:
        p = store_path()
        if not os.path.isfile(p):
            return []
        d = json.load(open(p, encoding="utf-8"))
        words = d.get("words") if isinstance(d, dict) else None
        return [str(w) for w in (words or []) if str(w).strip()]
    except Exception:
        return []


def _save(words, trigger):
    """原子落盘（带审计字段）。"""
    d = {"version": 1,
         "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "updated_by_trigger": trigger,
         "words": words}
    p = store_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _validate(word, words, max_n):
    """返回 (规范化词, None) 或 (None, 错误原因)。"""
    w = (word or "").strip()
    if not w:
        return None, "唤醒词为空"
    if len(w) > 16:
        return None, "唤醒词过长（>16 字符）: %s" % w
    if any(ch.isspace() for ch in w):
        return None, "唤醒词不能含空白字符: %s" % w
    low = w.lower()
    if any(x.lower() == low for x in words):
        return None, "唤醒词已存在: %s" % w
    if len(words) >= max_n:
        return None, "唤醒词数量已达上限 %d（先 remove 再加）" % max_n
    return w, None


def execute(cfg, action="list", word=None):
    """list / add / remove。返回 dict。"""
    if not enabled(cfg):
        return {"ok": False,
                "reason": "AI 自管唤醒词未启用（daemon.allow_ai_wake_words=false）"}
    trigger = run_context.trigger()
    words = _load()
    act = (action or "list").strip().lower()

    if act == "list":
        return {"ok": True, "action": "list", "words": words,
                "max": _max_words(cfg),
                "note": "这些词与 bot_names/mention_extra 并列生效；daemon 每次聊天轮询自动重载"}

    if act == "add":
        w, err = _validate(word, words, _max_words(cfg))
        if err:
            return {"ok": False, "action": "add", "reason": err, "words": words}
        words.append(w)
        _save(words, trigger)
        return {"ok": True, "action": "add", "added": w, "words": words,
                "note": "立即生效（daemon 自动重载）；建议用 say 在游戏里公告"}

    if act == "remove":
        low = (word or "").strip().lower()
        hit = [x for x in words if x.lower() == low]
        if not hit:
            return {"ok": False, "action": "remove",
                    "reason": "未找到: %r（现有: %s）" % (word, words)}
        words = [x for x in words if x.lower() != low]
        _save(words, trigger)
        return {"ok": True, "action": "remove", "removed": hit, "words": words}

    return {"ok": False, "reason": "未知 action: %s（list/add/remove）" % act}


def make_wake_words_tools(cfg):
    """按配置产出工具；未启用 → 空列表（工具不注册）。"""
    if not enabled(cfg):
        return []
    max_n = _max_words(cfg)
    manifest = {
        "name": TOOL_NAME,
        "description": (
            "管理你的额外唤醒词（★仅当机主开启 allow_ai_wake_words 时可用）："
            "被这些词叫到也会唤醒你（与名字 @ 提及并列）。"
            "action=list 看现有词；action=add word=新词（≤16字、无空格、上限 %d 个，"
            "与现有词去重）；action=remove word=词。加词立即生效无需重启；"
            "★建议加完后用 say 在游戏里公告（让玩家知道怎么叫你）；"
            "每次增删都会记录触发来源（审计）。") % max_n,
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {"type": "string",
                           "description": "list | add | remove"},
                "word": {"type": "string", "description": "add/remove 时的唤醒词"},
            },
        },
    }

    def _run(action="list", word=None):
        return execute(cfg, action=action, word=word)

    return [(manifest, _run)]

# -*- coding: utf-8 -*-
"""AI 自管"唤醒黑名单"（默认开启）。

用途：让 agent 自己把**不该唤醒它**的消息屏蔽掉。典型场景（2026-10-18 实锤）：
  - 服务器周期广播："<BOT_NAME> is now sleeping. [1/3]"、"已将X传送至…"、
    "还剩下 60 秒清理物品和实体"、清理成功回执——每 5 分钟几十条，把定时唤醒顶成
    24 小时每 5 分钟跑一整轮（9-25 轮 / 12-38 万 token）
  - 刷屏机器人/广告号
  - 反复触发但永远不用回应的固定话术

与 `daemon.noise_patterns`（操作者配置）的区别：
  - noise_patterns 由操作者在 config.yaml 里写，agent 改不了
  - 本文件由 agent 自己维护，两者**并集生效**：命中任一即视为噪声（不计积压、不算提及）

★设计边界与安全：
- 词存独立文件 config/wake_blacklist.json（AI 专属，不动操作者的 config.yaml），
  daemon 每次聊天轮询按 mtime 重载，加词立即生效、重启保留
- 受 daemon.allow_ai_wake_blacklist 开关约束（默认 true；false → 工具不注册 + 已存词不生效）
- ★红线（防止 agent 把自己关进小黑屋 / 覆盖操作者意图）：
    1. 不得屏蔽 bot 自己的名字（否则 agent 对所有 @ 提及永久失聪）
    2. 不得屏蔽操作者配置的 mention_extra 触发词
    3. 不得屏蔽空串/全匹配正则（.* 、^.*$ 之类会把一切消息全屏蔽）
- 滥用防护：数量上限（wake_blacklist_max，默认 20）、长度上限 120、
  非法正则拒绝、每次增删记录触发源（trigger）审计
"""
import json
import os
import re
import time

from core import config_loader, run_context

TOOL_NAME = "wake_blacklist"
DEFAULT_MAX = 20
MAX_LEN = 120

# 会被判为"通杀"的危险正则（直接拒绝）
_DANGEROUS = ("^.*$", ".*", "^", "$", "^.*", ".*$", "(?s).*", "[\\s\\S]*")


def _s(cfg):
    return (cfg.get("daemon") or {})


def enabled(cfg):
    return bool(_s(cfg).get("allow_ai_wake_blacklist", True))


def _max_items(cfg):
    try:
        return max(1, int(_s(cfg).get("wake_blacklist_max", DEFAULT_MAX)))
    except (TypeError, ValueError):
        return DEFAULT_MAX


def store_path():
    return os.path.join(config_loader.AGENT_ROOT, "config", "wake_blacklist.json")


def _load():
    """读黑名单条目（list[dict]）；文件损坏/不存在 → 空表。"""
    try:
        p = store_path()
        if not os.path.isfile(p):
            return []
        d = json.load(open(p, encoding="utf-8"))
        items = d.get("items") if isinstance(d, dict) else None
        out = []
        for it in (items or []):
            if isinstance(it, dict) and it.get("pattern"):
                out.append({"pattern": str(it["pattern"]),
                            "note": str(it.get("note") or ""),
                            "added_by": str(it.get("added_by") or "?"),
                            "at": str(it.get("at") or "")})
        return out
    except Exception:
        return []


def patterns():
    """只取正则串（daemon 侧用）。"""
    return [it["pattern"] for it in _load() if it.get("pattern")]


def _save(items, trigger):
    d = {"version": 1,
         "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "updated_by_trigger": trigger,
         "items": items}
    p = store_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _validate(pattern, items, max_n, forbidden):
    """返回 (规范化正则, None) 或 (None, 错误原因)。forbidden=禁止屏蔽的词列表。"""
    p = (pattern or "").strip()
    if not p:
        return None, "正则为空"
    if len(p) > MAX_LEN:
        return None, "正则过长（>%d 字符）" % MAX_LEN
    if p in _DANGEROUS:
        return None, "该正则会屏蔽一切消息（通杀），已拒绝: %s" % p
    try:
        rx = re.compile(p)
    except re.error as e:
        return None, "正则非法: %s（%s）" % (p, e)
    # 红线：不得屏蔽 bot 名 / 操作者触发词
    for w in forbidden:
        if not w:
            continue
        try:
            if re.search(p, w, re.IGNORECASE) or re.search(w, p, re.IGNORECASE):
                return None, ("该正则会屏蔽受保护词 %r（bot 名或操作者配置的触发词）——"
                              "不允许，否则你会永久失聪" % w)
        except re.error:
            continue
    if any(it["pattern"] == p for it in items):
        return None, "该正则已存在: %s" % p
    if len(items) >= max_n:
        return None, "黑名单已达上限 %d（先 remove 再加）" % max_n
    return p, None


def execute(cfg, action="list", pattern=None, note=None, forbidden=None):
    """list / add / remove / test。返回 dict。"""
    if not enabled(cfg):
        return {"ok": False,
                "reason": "AI 自管唤醒黑名单未启用（daemon.allow_ai_wake_blacklist=false）"}
    trigger = run_context.trigger()
    items = _load()
    act = (action or "list").strip().lower()

    if act == "list":
        return {"ok": True, "action": "list", "count": len(items), "max": _max_items(cfg),
                "items": items,
                "note": "命中任一条的消息：不算 @提及、不计入待回应积压（mc_chat 仍可查到）"}

    if act == "add":
        p, err = _validate(pattern, items, _max_items(cfg), forbidden or [])
        if err:
            return {"ok": False, "action": "add", "reason": err, "count": len(items)}
        items.append({"pattern": p, "note": str(note or "")[:80],
                      "added_by": trigger, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        _save(items, trigger)
        return {"ok": True, "action": "add", "added": p, "count": len(items),
                "note": "立即生效（daemon 自动重载）"}

    if act == "remove":
        p = (pattern or "").strip()
        hit = [it for it in items if it["pattern"] == p]
        if not hit:
            return {"ok": False, "action": "remove",
                    "reason": "未找到: %r（现有: %s）" % (p, [it["pattern"] for it in items])}
        items = [it for it in items if it["pattern"] != p]
        _save(items, trigger)
        return {"ok": True, "action": "remove", "removed": p, "count": len(items)}

    if act == "test":
        """试匹配：看某条消息会不会被黑名单拦掉（加词前先验证，避免误伤）。"""
        text = str(pattern or "")
        hits = []
        for it in items:
            try:
                if re.search(it["pattern"], text):
                    hits.append(it["pattern"])
            except re.error:
                continue
        return {"ok": True, "action": "test", "text": text[:120],
                "blocked": bool(hits), "matched": hits}

    return {"ok": False, "reason": "未知 action: %s（list/add/remove/test）" % act}


def make_wake_blacklist_tools(cfg, protected=None):
    """按配置产出工具；未启用 → 空列表。protected=禁止屏蔽的词（bot 名 + 操作者触发词）。"""
    if not enabled(cfg):
        return []
    max_n = _max_items(cfg)
    manifest = {
        "name": TOOL_NAME,
        "description": (
            "管理你的**唤醒黑名单**（自己维护，立即生效、重启保留）：命中的消息不再唤醒你、"
            "也不计入待回应积压（mc_chat 照样能查到全文）。用来屏蔽服务器周期广播"
            "（清理倒计时/清理成功/传送回执/睡觉进度…）、刷屏广告号、固定话术——"
            "这些东西会把你 5 分钟叫醒一次，每次白跑十几轮烧 token。"
            "action=list 看现有；action=add pattern=正则 note=备注（≤%d 条，正则≤120字）；"
            "action=remove pattern=正则；action=test pattern=待检测文本（看会不会被拦，加词前先试）。"
            "★红线：不能屏蔽你自己的名字和机主配的触发词（会永久失聪），也不能用 .* 之类通杀正则。"
            "每次增删都记录触发来源（审计）。") % max_n,
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {"type": "string",
                           "description": "list | add | remove | test"},
                "pattern": {"type": "string",
                            "description": "add/remove 用正则；test 用待检测文本"},
                "note": {"type": "string", "description": "add 时的备注（为什么屏蔽）"},
            },
        },
    }

    def _run(action="list", pattern=None, note=None):
        return execute(cfg, action=action, pattern=pattern, note=note,
                       forbidden=protected or [])

    return [(manifest, _run)]

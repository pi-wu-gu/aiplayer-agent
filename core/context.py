# -*- coding: utf-8 -*-
"""上下文消耗优化（阶段2.5）：
1. 结果压缩：大工具结果 → 规则摘要（原始全文存档，agent 可用 tool_result 取回）
2. 历史归档：Agent 里做（老 tool 消息变占位符）
全部规则式摘要，零额外 LLM 调用。

★动作感知派发（2026-10-16 修）：mc_act/mc_batch 的结果形如
{"sent":true,"result":{"action":"get_screen",...}}——动作名在**内层** result 里。
此前 DIGESTERS 只按工具名（mc_act）查表，键永远匹配不上，digester 全是死代码，
61KB 的维度网络 get_screen 只能走通用截断+存档，agent 反复 tool_result 取回又再被压缩，烧轮数。
现在：先按工具名查，失败再解析结果 JSON 拿 action 字段查（内层 result 也递归看）。"""
import json


class ResultCompressor:
    def __init__(self, threshold=350):
        self.base_threshold = threshold
        self.threshold = threshold
        self.spool = {}          # id -> 原始全文
        self._fetched = {}       # id -> 已取回次数（防失控：全文 1 次、开头 1 次、之后拒绝）
        self._n = 0

    def set_usage(self, total_tokens):
        """★按会话累计消耗收紧压缩阈值（v0.2.1.7 框架侧，"压缩按当前消耗量触发"）：
        消耗越大越要省上下文——等价于省钱与省轮数。分档（base=350 时）：
        <8万 token 原档；8-16万 收 1/3；16-28万 收半；≥28万 收到 1/4（下限 80 字）。"""
        t = self.base_threshold
        if total_tokens >= 280_000:
            self.threshold = max(80, t // 4)
        elif total_tokens >= 160_000:
            self.threshold = max(100, t // 2)
        elif total_tokens >= 80_000:
            self.threshold = max(140, t * 2 // 3)
        else:
            self.threshold = t

    # ---------- 对外 ----------
    def compress(self, tool_name, raw_str):
        """返回（可能压缩后的）字符串；大结果存档并附取回指引。"""
        raw = raw_str if isinstance(raw_str, str) else str(raw_str)
        if len(raw) <= self.threshold:
            return raw
        # ★常量型结果不存档（2026-10-16）：actions/tools 这类"查表"结果每次都一样，
        # 存档→tool_result 取回只是多花一轮往返，token 一点没省（实测同一会话取回 2 次）
        if self._action_of(raw) in _NO_ARCHIVE:
            return raw
        dig = self._digest(tool_name, raw)
        self._n += 1
        sid = "r%d" % self._n
        self.spool[sid] = raw
        return "%s\n[已存档r%d(%d字)，通常无需取回]" % (dig, self._n, len(raw))

    def fetch(self, spool_id):
        """取回存档。★同一 id 限取 1 次全文、第 2 次只给开头（石碑任务失控修复）：
        session_214537 里 tool_result 对同一 get_screen 存档取回 4 次（每次 1.3-1.7k 字符），
        全是同一份残缺视图——重复取回=纯烧 token 不产生新信息。"""
        raw = self.spool.get(spool_id)
        if raw is None:
            return {"error": "存档 id %r 不存在；可用: %s" % (spool_id, sorted(self.spool.keys())[-8:])}
        n = self._fetched.get(spool_id, 0) + 1
        self._fetched[spool_id] = n
        if n == 1:
            return {"id": spool_id, "length": len(raw), "content": raw}
        if n == 2:
            return {"id": spool_id, "length": len(raw), "content": raw[:400],
                    "warning": "该存档已取回过（这是第2次，只给开头400字）。你已读过它的大部分内容，"
                               "基于已读信息决策即可；再取不会返回更多。"}
        return {"error": "存档 %s 已取回 %d 次，已停止返回（防失控循环烧token）。"
                         "用已读到的信息继续任务；确需新信息请换动作获取。" % (spool_id, n - 1)}

    # ---------- 规则摘要器 ----------
    def _digest(self, tool_name, raw):
        try:
            fn = DIGESTERS.get(tool_name)
            if fn is None:
                fn = DIGESTERS.get(self._action_of(raw))
            if fn:
                out = fn(raw)
                if out:
                    return out
        except Exception:
            pass
        return self._generic(raw)

    @staticmethod
    def _action_of(raw):
        """从工具结果里提取模组动作名：顶层 action，或内层 result.action
        （mc_act/mc_batch 的返回都是 {"sent":..,"result":{"action":..}}）。"""
        try:
            d = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            return None
        if not isinstance(d, dict):
            return None
        a = d.get("action")
        if isinstance(a, str):
            return a
        inner = d.get("result")
        if isinstance(inner, dict):
            a = inner.get("action")
            if isinstance(a, str):
                return a
        return None

    @staticmethod
    def _generic(raw):
        head = raw[:300]
        tail = raw[-100:] if len(raw) > 500 else ""
        return head + ("…" + tail if tail else "…")

    @staticmethod
    def _j(raw):
        return json.loads(raw) if isinstance(raw, str) else raw


def _unwrap(raw):
    """mc_act/mc_batch 结果 → 内层动作结果 dict（没有内层就返回顶层）。"""
    d = ResultCompressor._j(raw)
    inner = d.get("result") if isinstance(d, dict) else None
    return inner if isinstance(inner, dict) else d


def _dig_mc_state(raw):
    d = ResultCompressor._j(raw)
    keep = {k: d[k] for k in ("loaded", "pos", "yaw", "pitch", "health", "dim", "gamemode",
                              "dead", "looking_at", "last_action", "last_action_age_ms")
            if d.get(k) is not None}
    if d.get("food") is not None:               # 模组侧 food 是数字
        keep["food"] = d.get("food")
    t = d.get("time")
    if isinstance(t, dict):                     # time.dayTime/timeOfDay/isDay
        keep["time"] = {"timeOfDay": t.get("timeOfDay"), "isDay": t.get("isDay")}
    h = d.get("held")
    if isinstance(h, dict):
        keep["held"] = "%s x%s" % (h.get("id"), h.get("count"))
    chat = d.get("chat") or []
    if chat:
        keep["chat_last2"] = chat[-2:]
    if d.get("players"):
        keep["players"] = d["players"][:2]
    # ★补"界面/输入/状态"类字段（2026-10-18）：旧白名单把它们全丢了，agent 每轮都
    # 被迫用 mc_act state/time 重查同一件事（实例 session_20261018_031332 第 1、17 轮
    # 都要 screen/window_active/mouse_grabbed，摘要里一个都没有 → 各多烧一轮）。
    for k in ("screen", "window_active", "mouse_grabbed", "onGround", "sprinting",
              "flying", "vehicle"):
        if d.get(k) is not None:
            keep[k] = d[k]
    eff = d.get("effects")
    if eff:
        keep["effects"] = eff[:4]
    # ★未知字段透传：让 agent 当场看到自己字段名写错了（"timeOfDay/canSleep" 在
    # /state 里根本不存在，正确是 time.timeOfDay；time 动作才有 canSleep），
    # 免得它把"字段被压缩了"当成环境故障再查一遍。
    unk = d.get("_unknown_fields")
    if unk:
        keep["_unknown_fields"] = unk
        keep["_hint"] = "timeOfDay→time.timeOfDay；canSleep 用 mc_act type=time；附近玩家→players"
    return json.dumps(keep, ensure_ascii=False)


def _dig_inventory(raw):
    d = _unwrap(raw)
    items = d.get("items") or []
    if not items and d.get("totalSlots") is None:
        raise ValueError("非库存结构，回退通用摘要")
    # 保留槽位号（select 要用）——紧凑到 "槽=物品 xN" 单行列表
    out = {"totalSlots": d.get("totalSlots"), "selected": d.get("selected"),
           "物品数": len(items)}
    if d.get("screen"):
        out["screen"] = d["screen"]
    out["背包"] = ["槽%s=%s x%s" % (it.get("slot"), str(it.get("id", "?")).replace("minecraft:", ""),
                                    it.get("count")) for it in items]
    if d.get("equip"):
        out["equip"] = d["equip"]
    return json.dumps(out, ensure_ascii=False)


def _dig_get_screen(raw):
    """界面 dump → 紧凑摘要。

    维度网络终端一次返回全部 927 槽（61KB）——只留**非空槽**（含 cx/cy，可直接点）、
    控件精简列表（保留 i/class/cx/cy/text，截断文本）、界面头信息与计数。
    空槽是虚拟槽点了没效果，摘要里本就不该出现。"""
    d = _unwrap(raw)
    if "slots" not in d and "widgets" not in d and "widget_count" not in d:
        raise ValueError("非界面结构，回退通用摘要")
    out = {"action": "get_screen", "screen": d.get("simple") or d.get("class"),
           "title": d.get("title")}
    gz = d.get("gui_size")
    if isinstance(gz, dict):
        out["gui"] = "%sx%s" % (gz.get("w"), gz.get("h"))
    # 控件：全留但裁剪 text；数量通常 <30， cap 40 防爆
    ws = d.get("widgets") or []
    if ws:
        out["widgets"] = [
            {"i": w.get("i"), "cls": w.get("class"), "cx": w.get("cx"), "cy": w.get("cy"),
             "text": (w.get("text") or "")[:24]}
            for w in ws[:40]]
        if len(ws) > 40:
            out["widgets_note"] = "共%d个控件，只列前40" % len(ws)
    # 0.2.1.16 列表条目（石碑选择/服务器列表等）：条目数 + 条目文本——"几条选项"直接可答
    ls = d.get("lists") or []
    if ls:
        out["列表"] = [
            {"条目数": l.get("entry_count"),
             "条目": [(e.get("text") or "")[:30] for e in (l.get("visible") or [])[:60]]}
            for l in ls]
        out["列表_hint"] = "点条目用 screen_click{cx,cy}（可见条目带坐标；条目数=全部含屏幕外）"
    # 槽位：只留非空；cap 80
    slots = d.get("slots") or []
    non_empty = [s for s in slots if s.get("id") and s.get("id") != "air"]
    out["槽位统计"] = {"总槽": len(slots), "非空": len(non_empty)}
    if non_empty:
        out["非空槽"] = [
            {"槽": s.get("index"), "cx": s.get("cx"), "cy": s.get("cy"),
             "id": str(s.get("id", "?")).replace("minecraft:", ""), "n": s.get("count")}
            for s in non_empty[:80]]
        if len(non_empty) > 80:
            out["非空槽_note"] = "共%d个非空槽，只列前80（可用搜索screen_type缩小范围）" % len(non_empty)
    cur = d.get("cursor")
    if isinstance(cur, dict) and cur.get("id") and cur.get("id") != "air":
        out["光标持有"] = "%s x%s" % (cur.get("id"), cur.get("count"))
    if d.get("hovered") is not None:
        out["悬停槽"] = d.get("hovered")
    out["hint"] = d.get("hint") or "点槽位用 screen_click{x=cx,y=cy}；button=0左键"
    return json.dumps(out, ensure_ascii=False)


def _dig_container(raw):
    """get_container → 紧凑摘要（对齐模组侧 compact 语义）。

    ★令牌炸弹治理（2026-10-18 实测）：Sophisticated Backpacks 156 槽全量 dump
    ≈2.3 万字符（每槽 10 字段、空槽也全量序列化）原样进上下文，几条就把会话顶爆。
    只留**非空槽**（index/sec/id/count）、ranges、光标/悬停；truncated 时明确标注
    "还有槽没列"，引导用 find/only 精确查——禁止把截断结果当"容器只有这些"。"""
    d = _unwrap(raw)
    slots = d.get("slots")
    if slots is None and d.get("matches") is None and d.get("ranges") is None:
        raise ValueError("非容器结构，回退通用摘要")
    out = {"action": "get_container", "title": d.get("title"),
           "menu": d.get("menu"), "slot_count": d.get("slot_count")}
    if d.get("find"):
        out["find"] = d["find"]
        ms = d.get("matches") or []
        out["matched"] = d.get("matched", len(ms))
        out["matches"] = [
            {"槽": m.get("index"), "sec": m.get("sec"),
             "id": str(m.get("id", "?")).replace("minecraft:", ""), "n": m.get("count")}
            for m in ms[:60]]
        out["hint"] = d.get("hint") or "点匹配槽用 click_slot{slot=index}"
    else:
        non_empty = [s for s in (slots or []) if s.get("id") and s.get("id") != "air"]
        out["槽位统计"] = {"总槽": d.get("slot_count"), "列出": len(slots or []),
                          "非空": len(non_empty)}
        if d.get("truncated"):
            out["truncated"] = True
            out["_note"] = ("★有槽被上限裁掉未列出——换 find=物品id 精确查或 "
                            "only=container|inventory 分段读，禁止当'容器只有这些'")
        if non_empty:
            out["非空槽"] = [
                {"槽": s.get("index"), "sec": s.get("sec"),
                 "id": str(s.get("id", "?")).replace("minecraft:", ""), "n": s.get("count")}
                for s in non_empty[:80]]
            if len(non_empty) > 80:
                out["非空槽_note"] = "共%d个非空槽，只列前80" % len(non_empty)
        rs = d.get("ranges") or []
        if rs:
            out["ranges"] = [{"sec": r.get("sec"), "from": r.get("from"),
                              "to": r.get("to")} for r in rs]
        cur = d.get("cursor")
        if isinstance(cur, dict) and cur.get("id") and cur.get("id") != "air":
            out["光标持有"] = "%s x%s" % (cur.get("id"), cur.get("count"))
        if d.get("hovered") is not None:
            out["悬停槽"] = d.get("hovered")
        out["hint"] = ("点槽位用 click_slot{slot=index}；★以后读容器先限量："
                       "compact=true / only=container|inventory / max_slots=N / find=物品id")
    return json.dumps(out, ensure_ascii=False)


def _dig_scan_region(raw):
    """scan_region → 聚合摘要。

    ★令牌炸弹治理（2026-10-16 实测）：单条回包 13KB（400 格逐格列举）原样进上下文，
    几条就把会话顶到 20 万预算。按方块类型聚合计数 + 只保留少量"值得注意"的坐标样例；
    需要精确坐标时再用 block_at 单点确认。"""
    d = _unwrap(raw)
    blocks = d.get("blocks")
    if not blocks and d.get("n") is None:
        raise ValueError("非 scan_region 结构，回退通用摘要")
    items = []
    for b in (blocks or []):
        # 形态："x,y,z=id"
        try:
            items.append(str(b).split("=", 1)[1])
        except (IndexError, AttributeError):
            continue
    counts = {}
    for i in items:
        counts[i] = counts.get(i, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    out = {"action": "scan_region", "格数": d.get("n") or len(blocks or []),
           "类型数": len(counts),
           "聚合": ["%s×%d" % (k.replace("minecraft:", ""), v) for k, v in top[:12]]}
    if len(top) > 12:
        out["_note"] = "只列前12种（共%d种）；要精确位置用 block_at" % len(top)
    if d.get("origin"):
        out["origin"] = d.get("origin")
    return json.dumps(out, ensure_ascii=False)


def _dig_mc_chat(raw):
    d = _unwrap(raw)
    lines = d.get("lines") or []
    if not lines and "count" not in d:
        raise ValueError("非聊天结构，回退通用摘要")
    out = {"count": d.get("count"), "total": d.get("total")}
    out["lines"] = [str(l)[:80] for l in lines]
    return json.dumps(out, ensure_ascii=False)


DIGESTERS = {
    "mc_state": _dig_mc_state,
    "mc_chat": _dig_mc_chat,
    "get_inventory": _dig_inventory,
    "cont_info": _dig_container,
    "get_container": _dig_container,
    "get_screen": _dig_get_screen,
    "screen": _dig_get_screen,
    "scan_region": _dig_scan_region,
    "scan": _dig_scan_region,
}

# 常量型结果：不存档（存档只会多一轮 tool_result 往返，省不了 token）
_NO_ARCHIVE = {"actions", "tools", "docs", "online_players", "time", "ping", "web_fetch"}

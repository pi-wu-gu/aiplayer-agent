# -*- coding: utf-8 -*-
"""分层记忆体系（2026-10-18 建成）。

★用户需求原文：
  「将记忆设为"未总结的内容，过零点总结单日，当前周结束总结周，过当月总结月，过当年总结年"，
    放在一个记忆目录下按年、月、日、未总结分类文件夹存放，未总结记忆按单次唤醒存放。」

目录结构（根 = <shared>/docs/记忆/）：
    记忆/
      INDEX.md                    总索引（agent 自己维护）
      unsorted/                   ★未总结：一次唤醒一个文件
        2026-10-18_0342_scheduled.md
        2026-10-18_0451_mention.md
      day/2026-10-18.md           日档（过零点后由未总结汇总而来）
      week/2026-W42.md            周档（本周结束后由日档汇总）
      month/2026-10.md            月档（过月后由周档汇总）
      year/2026.md                年档（过年后由月档汇总）
      unsorted/_done/<period>/    已汇总的未总结原件（归档，不再参与待办）

★职责划分（关键设计）：
  - **框架负责搬运与记账**：建目录、给本次唤醒开文件、判断"该总结谁"、提交后归档原件。
    框架有 OS 权限且不会忘，agent 不该手工挪文件（它连 delete 都没有）。
  - **agent 负责内容**：读未总结 → 写日/周/月/年档 → 调 memory(action=commit) 收工。
    总结走 agent 自己的 LLM 上下文（不额外开一次 LLM 调用，省 token）。

总结时机（纯推导，不需要定时器和常驻状态）：
  日：存在 unsorted 文件的日期 < 今天  → 过零点了，可总结那天
  周：存在 ISO 周 < 当前周 的日档      → 那周结束了
  月：存在月份 < 当前月的周档          → 那月结束了
  年：存在年份 < 当前年的月档          → 那年结束了
已完成项记在 memory_state.json，避免重复总结。
"""
import datetime
import json
import os
import re
import time

from core import config_loader

KINDS = ("day", "week", "month", "year")

# ★period 白名单（2026-10-18 修）：period 由 agent（LLM）传入并直接拼进文件路径，
# 不校验的话 "../x" 之类可以逃出档案目录（读通道=存在性/长度弱探测，搬移路径另有
# items 过滤守住，但输入校验应当在最前面）。每种层一个精确格式。
_PERIOD_RES = {
    "day": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "week": re.compile(r"^\d{4}-W\d{2}$"),
    "month": re.compile(r"^\d{4}-\d{2}$"),
    "year": re.compile(r"^\d{4}$"),
}
_SUBDIR = {"day": "day", "week": "week", "month": "month", "year": "year",
           "unsorted": "unsorted"}

# 未总结文件名：2026-10-18_0342_scheduled.md
_UNSORTED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_([a-z]+)\.md$")


# ---------- 路径 ----------
def memory_root(cfg):
    """记忆根目录 <shared>/docs/记忆/。shared 未配 → None（工具不注册）。"""
    shared = ((cfg.get("permissions") or {}).get("roots") or {}).get("shared") or ""
    if not shared:
        return None
    root = shared if os.path.isabs(shared) else os.path.join(config_loader.AGENT_ROOT, shared)
    return os.path.join(root, "docs", "记忆")


def _sub(root, kind):
    return os.path.join(root, _SUBDIR[kind])


def state_path(cfg):
    return os.path.join(config_loader.AGENT_ROOT, "logs", "memory_state.json")


# ---------- 周期键 ----------
def _iso_week(d):
    y, w, _ = d.isocalendar()
    return "%04d-W%02d" % (y, w)


def period_key(kind, d):
    if kind == "day":
        return d.strftime("%Y-%m-%d")
    if kind == "week":
        return _iso_week(d)
    if kind == "month":
        return d.strftime("%Y-%m")
    return "%04d" % d.year


def target_path(root, kind, period):
    return os.path.join(_sub(root, kind), "%s.md" % period)


def _source_kind(kind):
    """总结 kind 时读的下一层（日←未总结，周←日，月←周，年←月）。"""
    return {"day": "unsorted", "week": "day", "month": "week", "year": "month"}[kind]


# ---------- 初始化 ----------
def ensure_layout(root):
    """建目录骨架（幂等）。返回新建的目录列表。"""
    made = []
    for d in ("unsorted", "day", "week", "month", "year"):
        p = _sub(root, d)
        if not os.path.isdir(p):
            os.makedirs(p, exist_ok=True)
            made.append(d)
    idx = os.path.join(root, "INDEX.md")
    if not os.path.isfile(idx):
        _atomic_write(idx, "# 记忆索引（AI 自己维护）\n\n"
                           "- unsorted/：未总结，一次唤醒一个文件（过零点后汇总成日档）\n"
                           "- day/：日档；week/：周档；month/：月档；year/：年档\n")
        made.append("INDEX.md")
    return made


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------- 未总结：按单次唤醒 ----------
def unsorted_path(root, when=None, trigger="manual", task_head=""):
    """本次唤醒的未总结文件路径（框架在 run_task 开头确定，整个运行期间不变）。"""
    when = when or datetime.datetime.now()
    name = "%s_%s_%s.md" % (when.strftime("%Y-%m-%d"), when.strftime("%H%M"),
                            re.sub(r"[^a-z]", "", (trigger or "manual").lower()) or "manual")
    return os.path.join(_sub(root, "unsorted"), name)


def daily_seq(root):
    """今天第几次唤醒（用于未总结文件头部编号）。"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    return sum(1 for it in _list_unsorted(root) if it["date"] == today) + 1


def open_note(root, path, trigger, task_head, seq):
    """给本次唤醒的未总结文件写头部（元信息），内容由 agent 后续 append。"""
    head = ("# 未总结记忆 · %s 唤醒 #%s\n\n"
            "- 时间：%s\n- 触发源：%s\n- 任务：%s\n\n---\n\n"
            % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), seq,
               datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               trigger, (task_head or "")[:120]))
    _atomic_write(path, head)
    return path


def append_note(path, text):
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + (text or "").strip() + "\n")


# ---------- 待总结推导 ----------
def _load_state(cfg):
    try:
        return json.load(open(state_path(cfg), encoding="utf-8"))
    except Exception:
        return {}


def _save_state(cfg, st):
    p = state_path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _done_set(cfg, kind):
    return set((_load_state(cfg).get("done") or {}).get(kind) or [])


def _list_unsorted(root):
    """返回 [{date, path, name}]，按日期排序。"""
    d = _sub(root, "unsorted")
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        m = _UNSORTED_RE.match(fn)
        if not m:
            continue
        try:
            dt = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        out.append({"date": m.group(1), "date_obj": dt,
                    "name": fn, "path": os.path.join(d, fn)})
    return out


def _list_period_files(root, kind):
    """列出某层已有的档（day/<date>.md 等），返回 [(period, path)]。"""
    d = _sub(root, kind)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".md"):
            out.append((fn[:-3], os.path.join(d, fn)))
    return out


def pending(cfg, root, today=None):
    """推导当前有哪些总结待办。返回 list[{kind, period, sources:[path], hint}]。

    规则：源材料**必须属于已结束的周期**（过零点 / 周已过 / 月已过 / 年已过），
    且该周期没被 commit 过。
    today 可注入（测试用），默认取真实日期。
    """
    today = today or datetime.date.today()
    st = _load_state(cfg)
    done = st.get("done") or {}
    out = []

    # 日：未总结文件的日期 < 今天
    by_date = {}
    for it in _list_unsorted(root):
        if it["date_obj"] < today:
            by_date.setdefault(it["date"], []).append(it["path"])
    for date in sorted(by_date):
        if date in set(done.get("day") or []):
            continue
        out.append({"kind": "day", "period": date, "sources": sorted(by_date[date]),
                    "target": target_path(root, "day", date),
                    "hint": "过零点：把 %d 条未总结汇总成日档" % len(by_date[date])})

    # 周：日档的 ISO 周 < 当前周
    cur_w = _iso_week(today)
    weeks = {}
    for period, path in _list_period_files(root, "day"):
        try:
            d = datetime.date.fromisoformat(period)
        except ValueError:
            continue
        w = _iso_week(d)
        if w < cur_w:
            weeks.setdefault(w, []).append(path)
    for w in sorted(weeks):
        if w in set(done.get("week") or []):
            continue
        out.append({"kind": "week", "period": w, "sources": sorted(weeks[w]),
                    "target": target_path(root, "week", w),
                    "hint": "本周已结束：把 %d 篇日档汇总成周档" % len(weeks[w])})

    # 月：周档所属月份 < 当前月
    cur_m = today.strftime("%Y-%m")
    months = {}
    for period, path in _list_period_files(root, "week"):
        try:
            y, wn = period.split("-W")
            # ISO 周 → 该周周一 → 归属月份（按周一所在月）
            d = datetime.date.fromisocalendar(int(y), int(wn), 1)
        except (ValueError, AttributeError):
            continue
        m = d.strftime("%Y-%m")
        if m < cur_m:
            months.setdefault(m, []).append(path)
    for m in sorted(months):
        if m in set(done.get("month") or []):
            continue
        out.append({"kind": "month", "period": m, "sources": sorted(months[m]),
                    "target": target_path(root, "month", m),
                    "hint": "上月已结束：把 %d 篇周档汇总成月档" % len(months[m])})

    # 年：月档年份 < 当前年
    cur_y = "%04d" % today.year
    years = {}
    for period, path in _list_period_files(root, "month"):
        y = period.split("-")[0]
        if y < cur_y:
            years.setdefault(y, []).append(path)
    for y in sorted(years):
        if y in set(done.get("year") or []):
            continue
        out.append({"kind": "year", "period": y, "sources": sorted(years[y]),
                    "target": target_path(root, "year", y),
                    "hint": "去年已结束：把 %d 篇月档汇总成年档" % len(years[y])})
    return out


# ---------- 提交总结 ----------
def commit(cfg, root, kind, period, today=None):
    """agent 写完目标档后调用：校验目标存在 → 归档源材料 → 记账。

    归档：源材料移到 <源层>/_done/<period>/（如 unsorted/_done/2026-10-18/）。
    today 可注入（测试用），默认取真实日期。
    """
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "reason": "kind 必须是 %s 之一，实得 %r" % (list(KINDS), kind)}
    period = (period or "").strip()
    if not _PERIOD_RES[kind].match(period):
        return {"ok": False, "reason": "period 格式不对：%r（%s 层应为 %s，如 %s）"
                % (period, kind, _PERIOD_RES[kind].pattern,
                   {"day": "2026-10-18", "week": "2026-W42",
                    "month": "2026-10", "year": "2026"}[kind])}

    tgt = target_path(root, kind, period)
    if not os.path.isfile(tgt):
        return {"ok": False, "reason": "目标档还没写：%s（先用 file_append / file_write 写进去再 commit）"
                % _rel(root, tgt)}
    try:
        body = open(tgt, encoding="utf-8").read().strip()
    except OSError:
        body = ""
    if len(body) < 10:
        return {"ok": False, "reason": "目标档内容过短（%d 字符），像是没真正写总结" % len(body)}

    items = [p for p in pending(cfg, root, today)
             if p["kind"] == kind and p["period"] == period]
    if not items:
        # 已 commit 过、或源材料还没到可总结的时点
        if period in _done_set(cfg, kind):
            return {"ok": True, "already": True, "kind": kind, "period": period,
                    "note": "该周期此前已提交过，无需重复"}
        return {"ok": False, "reason": "当前没有待总结的源材料（周期还没结束？或源已被归档）"}

    srcs = items[0]["sources"]
    moved, failed = [], []
    arc_dir = None
    # ★只有 day 层才搬走源材料：unsorted 是"中间态"（内容已并入日档，留着只会重复）。
    # 周/月/年 的源（日档/周档/月档）是**成品明细档**，汇总后必须原样保留，
    # 否则总结完一周就再也查不到那几天的日档了。
    if kind == "day":
        arc_dir = os.path.join(_sub(root, "unsorted"), "_done", period)
        os.makedirs(arc_dir, exist_ok=True)
        for s in srcs:
            try:
                dst = os.path.join(arc_dir, os.path.basename(s))
                if os.path.exists(dst):
                    os.remove(dst)
                os.replace(s, dst)
                moved.append(_rel(root, dst))
            except OSError as e:
                failed.append("%s: %s" % (os.path.basename(s), e))

    st = _load_state(cfg)
    done = st.setdefault("done", {})
    done.setdefault(kind, [])
    if period not in done[kind]:
        done[kind].append(period)
        done[kind].sort()
    st["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_state(cfg, st)

    return {"ok": True, "kind": kind, "period": period,
            "target": _rel(root, tgt), "archived": len(moved),
            "sources_kept": 0 if kind == "day" else len(srcs),
            "failed": failed,
            "note": ("源材料已归档到 %s，该周期不再出现在待办" % _rel(root, arc_dir))
            if kind == "day" else
            ("源明细档（%d 篇）原样保留，仅记账不再重复提醒" % len(srcs))}


def _rel(root, path):
    try:
        return os.path.relpath(path, os.path.dirname(root)).replace("\\", "/")
    except Exception:
        return path


# ---------- 读取源材料（供总结时用） ----------
def read_sources(cfg, root, kind, period, max_chars=6000, today=None):
    items = [p for p in pending(cfg, root, today)
             if p["kind"] == kind and p["period"] == period]
    if not items:
        return {"ok": False, "reason": "没有待总结项：kind=%s period=%s" % (kind, period)}
    parts, total = [], 0
    for s in items[0]["sources"]:
        try:
            txt = open(s, encoding="utf-8").read().strip()
        except OSError:
            continue
        if not txt:
            continue
        chunk = "----- %s -----\n%s" % (os.path.basename(s), txt)
        if total + len(chunk) > max_chars:
            parts.append("…（已截断，剩余用 file_read 逐篇读：%s）"
                         % ", ".join(os.path.basename(x) for x in items[0]["sources"]))
            break
        parts.append(chunk)
        total += len(chunk)
    return {"ok": True, "kind": kind, "period": period,
            "target": _rel(root, items[0]["target"]),
            "n_sources": len(items[0]["sources"]),
            "content": "\n\n".join(parts)}


def status(cfg, root, today=None):
    today = today or datetime.date.today()
    p = pending(cfg, root, today)
    return {"ok": True, "root": root,
            "unsorted_n": len(_list_unsorted(root)),
            "day_n": len(_list_period_files(root, "day")),
            "week_n": len(_list_period_files(root, "week")),
            "month_n": len(_list_period_files(root, "month")),
            "year_n": len(_list_period_files(root, "year")),
            "pending": [{"kind": i["kind"], "period": i["period"],
                         "sources": len(i["sources"]), "hint": i["hint"]} for i in p]}

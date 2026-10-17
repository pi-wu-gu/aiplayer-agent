# -*- coding: utf-8 -*-
"""MC 桥接工具：mc_state（读状态）/ mc_act（发动作+等结果）。阶段2
协议见 AI/docs/操作注意事项.md；控制面 = aiplayer 模组 127.0.0.1:8780（旧 JsMacros 桥 8765 已废弃）。"""
import json
import os
import re
import time

import requests

from core import config_loader

# 服务器聊天过滤：非法字符（如 →←↑↓ 等符号）会直接踢人。白名单净化。
_SAFE_CHAR = re.compile(
    r"[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u00d7\u00f7"
    r"a-zA-Z0-9 "
    r"，。！？：；、（）【】《》“”‘’…—+\-*/%().,:;!?~=<>@#&_|\[\]{}\"']")


def _sanitize_chat(text):
    """→←↑↓≥≤≈ 等替换为安全形式，其余非法字符剔除；截到100字。"""
    text = str(text)
    text = (text.replace("→", "->").replace("←", "<-").replace("↑", "^").replace("↓", "v")
                .replace("≥", ">=").replace("≤", "<=").replace("≈", "~")
                .replace("×", "x").replace("✅", "[OK]").replace("❌", "[X]"))
    text = _SAFE_CHAR.sub("", text)
    return text[:100]


def _base(cfg):
    return cfg.get("mc_bridge", {}).get("base_url", "http://127.0.0.1:8780").rstrip("/")


# ---------- 游戏探活（sidecar 离线降级的地基） ----------
# 游戏客户端关闭后 8780 不存在——mc_*（感知/动作/聊天）全部不可用，但 file_*/shell/
# 记忆/检索等非游戏工具必须照常工作。探活结果缓存 probe_ttl 秒，避免每动作一次探活请求。
_probe_cache = {"ok": None, "note": "", "ts": 0.0}


def game_online(cfg, force=False):
    """探测游戏控制面是否在线。返回 (bool, note)。结果缓存（probe_ttl，默认30s）。"""
    ttl = float(cfg.get("mc_bridge", {}).get("probe_ttl", 30))
    if not force and _probe_cache["ok"] is not None \
            and time.time() - _probe_cache["ts"] < ttl:
        return _probe_cache["ok"], _probe_cache["note"]
    try:
        r = requests.get(_base(cfg) + "/health", timeout=2)
        ok = (r.status_code == 200)
        note = "游戏在线" if ok else "游戏控制面异常(HTTP %s)" % r.status_code
    except requests.RequestException as e:
        ok, note = False, "游戏离线（%s）" % e.__class__.__name__
    _probe_cache.update(ok=ok, note=note, ts=time.time())
    return ok, note


def _offline_result():
    """mc_* 在游戏离线时的统一返回：明确、可 fast-fail、不重试不挂起。"""
    ok, note = _probe_cache["ok"], _probe_cache["note"]
    return {"sent": False, "result": None, "offline": True,
            "error": "游戏离线，mc_* 暂不可用（%s）。感知/动作/聊天类操作需等游戏恢复；"
                     "非游戏工具（file/shell/记忆/检索）不受影响。" % note}


def mc_batch(cfg, actions, timeout=None):
    """批量执行动作序列（一次入队，桥接按序执行）。
    actions: 动作 dict 列表。返回最后一个动作的结果。
    适合连续 click_slot 合成序列——把 LLM 的 N 轮往返压缩成 1 轮。"""
    if not isinstance(actions, list) or not actions:
        return {"sent": False, "error": "actions 必须是非空数组"}
    for i, a in enumerate(actions):
        if not isinstance(a, dict) or not a.get("type"):
            return {"sent": False, "error": "actions[%d] 缺少 type" % i}
    url = _base(cfg) + "/command"
    try:
        prev_la = requests.get(_base(cfg) + "/state", timeout=8).json().get("last_action")
    except requests.RequestException:
        prev_la = None
    r = requests.post(url, json={"commands": actions}, timeout=8)
    r.raise_for_status()
    if not r.json().get("queued"):
        return {"sent": False, "error": "桥接未入队"}
    mc_cfg = cfg.get("mc_bridge", {})
    timeout = timeout or (mc_cfg.get("action_timeout", 20) + 2 * len(actions))  # 批量给更多时间
    interval = mc_cfg.get("action_poll_interval", 1.0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(interval)
        try:
            s = requests.get(_base(cfg) + "/state", timeout=8).json()
            la = s.get("last_action")
            if la and la != prev_la:
                try:
                    return {"sent": True, "executed": len(actions), "result": json.loads(la)}
                except (json.JSONDecodeError, ValueError):
                    return {"sent": True, "executed": len(actions), "result": la}
        except requests.RequestException:
            continue
    return {"sent": True, "executed": len(actions), "result": None,
            "error": "批量执行超时(%ss)，用 cont_info 复核实际状态" % timeout}


def mc_state(cfg, fields=None):
    """读取完整或指定字段的状态。"""
    r = requests.get(_base(cfg) + "/state", timeout=8)
    r.raise_for_status()
    d = r.json()
    if fields:
        out = {k: d.get(k) for k in fields if k in d}
        unknown = [k for k in fields if k not in d]
        if unknown:
            # 2026-10-16 修复：未知字段不再静默丢弃（曾让 agent 查 screen 得 {}，
            # 误判"没有界面"，实为模组 state 无此字段——白烧 87 万 token）
            out["_unknown_fields"] = unknown
            out["_hint"] = ("模组 /state 没有这些字段；现有字段：%s。"
                            "界面查询：state 的 screen 字段直接显示有无界面开着"
                            "（class/title/pause），控件/槽位详情才用 mc_act type=\"get_screen\""
                            % "、".join(d.keys()))
        return out
    # 精简核心字段（完整 state 太长，LLM 不需要 _stats 等）；
    # 字段名与模组 8780 /state 的实际输出对齐（time 内含 isDay/timeOfDay；
    # screen/vehicle 无界面/未骑乘时为字符串 "null"，effects 空时省略）
    keep = ["loaded", "pos", "yaw", "pitch", "health", "food", "dead", "dim", "gamemode",
            "time", "held", "looking_at", "players", "chat",
            "screen", "onGround", "sprinting", "flying", "vehicle", "effects",
            "last_action", "last_action_age_ms"]
    return {k: d.get(k) for k in keep if d.get(k) not in (None, [], {})}


def mc_act(cfg, action, wait=True, timeout=None):
    """发一个动作 JSON 并轮询 last_action 等结果。action 是 dict。
    返回 {'sent':..., 'result':<动作结果对象或文本>}
    注意：桥接 /command 的 body 格式是 {"commands": [动作,...]}，裸对象会被静默丢弃！
    ★ say 文本在此统一净化（auto-say 等所有路径都经过这里，防非法字符踢人）。"""
    action = dict(action)
    if action.get("type") == "say":
        action["text"] = _sanitize_chat(action.get("text", ""))
    url = _base(cfg) + "/command"
    # 记录发送前的 last_action，之后以"变化"判定新结果（比文本匹配可靠）
    try:
        prev_la = requests.get(_base(cfg) + "/state", timeout=8).json().get("last_action")
    except requests.RequestException:
        prev_la = None
    r = requests.post(url, json={"commands": [action]}, timeout=8)
    r.raise_for_status()
    ack = r.json()
    if not ack.get("queued"):
        return {"sent": False, "result": None, "error": "桥接未入队（body 格式或客户端离线）: %s" % ack}
    if not wait:
        return {"sent": True, "result": None}
    mc_cfg = cfg.get("mc_bridge", {})
    timeout = timeout or mc_cfg.get("action_timeout", 20)
    interval = mc_cfg.get("action_poll_interval", 1.0)
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        time.sleep(interval)
        try:
            s = requests.get(_base(cfg) + "/state", timeout=8).json()
            la = s.get("last_action")
            if la and la != prev_la:
                # 客户端 last_action 多为纯文本（如 "keys ✅ ..."），JSON 只是少数——
                # 能解析就解析，解析不了原样返回文本（不再当错误）
                try:
                    return {"sent": True, "result": json.loads(la) if isinstance(la, str) else la}
                except (json.JSONDecodeError, ValueError):
                    return {"sent": True, "result": la}
        except requests.RequestException as e:
            last_err = e
            continue
    return {"sent": True, "result": None, "error": "等待结果超时(%ss) %s" % (timeout, last_err)}


# ---------- 移动类动作位移校验（2026-10-16 "冻结"事故修复） ----------
# 事故：bot 开着物品栏界面 30+ 分钟，原版 MC 界面开着时屏蔽玩家移动输入——
# move/jump/keys 全部返回 sent:true 但位置零变化，agent 收不到任何失败信号，
# 五次唤醒反复重试到 R4 token 熔断，白烧 87 万 token（根因是用户发现的，agent 自查失败）。
# 对策：移动类动作执行后，在按键按住窗口内轮询位置，零位移则附 _warn 指明排查路径。
_MOVE_KEY_MAPPINGS = {"key.forward", "key.back", "key.left", "key.right", "key.jump"}


def _is_movement_action(action):
    """是否移动类动作（位移可校验）。sneak 原地潜行不产生位移，排除。"""
    t = action.get("type")
    if t in ("move", "jump"):
        return True
    if t == "keys":
        keys = action.get("keys") or []
        if isinstance(keys, str):
            keys = [keys]
        return any(k in _MOVE_KEY_MAPPINGS for k in keys)
    return False


def _pos_dist(a, b):
    try:
        return sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)) ** 0.5
    except (TypeError, ValueError):
        return 0.0


def _verify_displacement(cfg, action, pos_before, out):
    """按键窗口内轮询位置；零位移 → out 附 _displacement=0 与 _warn（排查指引）。"""
    t = action.get("type")
    duration = action.get("duration")
    if duration is None:
        duration = {"move": 1000, "keys": 600, "jump": 260}.get(t, 600)
    try:
        duration = max(1, min(int(duration), 60000))
    except (TypeError, ValueError):
        duration = 600
    # 窗口 = duration + 1200ms：2026-10-16 实测移动输入有数百毫秒"起步延迟"
    # （主线程排队/服务器清理事件时更明显，1s 前进常前 0.7s 不动之后才位移），
    # +400ms 曾让 400ms 短 move 踩线误判；封顶 10s 防长 move 堵死调用方
    window = min(duration + 1200, 10000) / 1000.0
    deadline = time.time() + window
    moved = False
    while time.time() < deadline:
        time.sleep(0.15)
        try:
            pos = requests.get(_base(cfg) + "/state", timeout=5).json().get("pos")
        except requests.RequestException:
            continue
        if pos and _pos_dist(pos, pos_before) > 0.05:
            moved = True
            break
    if not moved:
        out["_displacement"] = 0.0
        out["_warn"] = ("位移为0：动作已发出但玩家位置毫无变化。头号根因=有界面开着"
                        "（物品栏/容器/石碑等；原版 MC 界面开着时移动输入被完全屏蔽，"
                        "而 look/press 不受影响所以极难自查）：先 mc_act type=\"get_screen\" 确认，"
                        "有界面就 type=\"close_screen\" 关掉再重试；其余可能：面前有墙"
                        "（block_at 确认）、死亡界面、坐骑、卡实体。"
                        "若 get_screen 无界面且 block_at 前方是空气：可能是客户端主线程繁忙"
                        "造成的输入起步延迟（实测可达数百毫秒，短 move 尤其容易踩坑），"
                        "换 duration>=1000ms 重试一次即可。"
                        "★定位到原因之前禁止连续短重试——每次重试都是真金白银")


# ---------- 容器分区护栏（2026-10-16 混淆事故修复） ----------
# 事故：bot 把"维度网络终端"的分区（玩家区0-35/存储区36+）套到普通容器上，
# 结果 click_slot 22-26 点在**自己的背包**上（普通容器是容器区在前、背包区在后）。
# 对策：容器界面下 click_slot 前查一次 get_container 的 ranges，落在背包区就明确警告。
_zone_cache = {"key": None, "inv_from": None, "title": ""}


def _container_zone(cfg):
    """当前容器界面的分区（按界面标识缓存，避免每次点击都查）。"""
    try:
        st = requests.get(_base(cfg) + "/state", timeout=5).json()
        scr = st.get("screen")
    except requests.RequestException:
        return None
    if not scr or scr == "null":
        _zone_cache["key"] = None
        return None
    key = json.dumps(scr, sort_keys=True, ensure_ascii=False) if isinstance(scr, dict) else str(scr)
    if _zone_cache.get("key") == key:
        return _zone_cache
    try:
        out = mc_act(cfg, {"type": "get_container"})
        d = out.get("result") if isinstance(out, dict) else None
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    inv_from = None
    for r in (d.get("ranges") or []):
        if r.get("sec") == "inventory":
            f = r.get("from")
            inv_from = f if inv_from is None else min(inv_from, f)
    _zone_cache.update(key=key, inv_from=inv_from, title=d.get("title") or "")
    return _zone_cache


def _slot_zone_warning(cfg, slot):
    """click_slot 落在玩家背包区 → 返回警告文本；容器区/查不到 → None。"""
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return None
    z = _container_zone(cfg)
    if not z or z.get("inv_from") is None:
        return None
    if slot < z["inv_from"]:
        return None
    return ("★slot=%d 属于玩家背包区（容器「%s」的容器区是 0..%d）——你点的是自己的物品，"
            "quick=true 会把它移进容器。要取容器里的东西，请用容器区索引；"
            "不确定就先 mc_act type=\"get_container\" 看每槽 section。"
            % (slot, z.get("title") or "容器", z["inv_from"] - 1))


# ---------- 注册用包装（manifest + fn） ----------

# 操作指南文档：环境变量 AGENT_GUIDE_PATH 可覆盖；默认按 PCL2 部署布局相对定位
GUIDE_PATH = os.environ.get(
    "AGENT_GUIDE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))), "AI", "docs", "操作注意事项.md"))
GUIDE_SECTIONS = {
    "槽位": "四大界面槽位映射",
    "工作流": "关键工作流 SOP",
    "红线": "红线与限制",
    "状态": "状态字段速查",
    "故障": "故障排查",
}


def make_mc_tools(cfg):
    def _state(fields=None):
        if not game_online(cfg)[0]:
            return _offline_result()
        return mc_state(cfg, fields)

    def _act(type, **kwargs):
        # 探活闸门：游戏离线时 mc_* 全部 fast-fail（不重试不挂起），非游戏工具不受影响
        if not game_online(cfg)[0]:
            return _offline_result()
        action = dict(kwargs)
        action["type"] = type
        # 参数别名容错：LLM 常把 say 的 text 写成 msg/message，或传成 null
        if type == "say" and not action.get("text"):
            for alt in ("msg", "message", "content"):
                if action.get(alt):
                    action["text"] = action.pop(alt)
                    break
        if type == "say" and not action.get("text"):
            # 别名也救不回来→拒绝执行，绝不往游戏聊天发"undefined"垃圾
            return {"sent": False, "error": "say 缺少 text 参数（LLM 侧修正后重试）"}
        if type == "say":
            action["text"] = _sanitize_chat(action["text"])
        # 参数别名容错（按模组真实动作的高发错法）：
        # screen_click 的坐标在 get_screen 里叫 cx/cy，LLM 极易照抄 → x/y
        if type == "screen_click" and action.get("x") is None:
            for alt in ("cx", "px"):
                if action.get(alt) is not None:
                    action["x"] = action.pop(alt)
                    break
        if type == "screen_click" and action.get("y") is None:
            for alt in ("cy", "py"):
                if action.get(alt) is not None:
                    action["y"] = action.pop(alt)
                    break
        # screen_type 写输入框：EditBox 的方法名是 setValue → value/content 错法
        if type == "screen_type" and not action.get("text"):
            for alt in ("value", "content", "query"):
                if action.get(alt):
                    action["text"] = action.pop(alt)
                    break
        # press 的按键参数：name/binding 错法
        if type == "press" and not action.get("key"):
            for alt in ("name", "binding"):
                if action.get(alt):
                    action["key"] = action.pop(alt)
                    break
        # keys 持续按住：LLM 可能传单个字符串而不是数组
        if type == "keys" and isinstance(action.get("keys"), str):
            action["keys"] = [action["keys"]]
        # command 发服务器命令：text/cmd/message 别名容错（session_214537 实错：cmd 被拒）
        if type == "command" and not action.get("text"):
            for alt in ("cmd", "command_str", "message"):
                if action.get(alt):
                    action["text"] = action.pop(alt)
                    break
        # ★screenshot 框架闸（石碑任务失控修复）：当前 LLM 无视觉能力，截图存了也"看不见"，
        # 对决策零收益纯烧 token——默认拦截；换视觉模型时在 config.yaml mc_bridge.allow_screenshot=true
        if type == "screenshot" and not (cfg.get("mc_bridge") or {}).get("allow_screenshot"):
            return {"sent": False,
                    "error": "screenshot 已被框架拦截：当前模型无视觉能力，看不到截图内容，"
                             "对决策零收益。要看界面内容用 mc_act type=\"get_screen\"；"
                             "换视觉模型后可在 config.yaml 设 mc_bridge.allow_screenshot=true 解锁。"}
        # ★容器槽位分区护栏（2026-10-16 混淆事故）：click_slot 用菜单全局索引，
        # 普通容器=容器区在前、背包区在后（分界随容器大小变）。点在自己背包区上
        # = 动自己的东西（quick=true 快移会把它送进容器）——查一次分区并明确警告。
        zone_warn = (_slot_zone_warning(cfg, action.get("slot"))
                     if type == "click_slot" and action.get("slot") is not None else None)
        # 离线保护：客户端不在世界中时拒绝动作（防任务空转烧token）
        pos_before = None
        if type != "reload_chunks":
            try:
                st = requests.get(_base(cfg) + "/state", timeout=5).json()
                if st.get("loaded") is False:
                    return {"sent": False,
                            "error": "客户端离线(loaded=false，可能在主菜单/重连中)。任务应中止，等待恢复后重试。"}
                pos_before = st.get("pos")
            except requests.RequestException:
                return {"sent": False, "error": "桥接不可达，客户端可能已退出"}
        out = mc_act(cfg, action)
        # 移动类动作：按键窗口内校验位移，零位移附 _warn（界面开着=头号根因）
        if pos_before and _is_movement_action(action) and isinstance(out, dict) and out.get("sent"):
            _verify_displacement(cfg, action, pos_before, out)
        if zone_warn and isinstance(out, dict):
            out["_zone_warn"] = zone_warn
        return out

    def _guide(section=None):
        """读操作指南（全文或按关键词截取相关段落）。"""
        with open(GUIDE_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        if not section:
            return text[:3000] + "\n...（截断，section 参数可取部分：槽位/工作流/红线/状态/故障）"
        # 简易定位：返回包含关键词的标题及其后文
        idx = text.find(section)
        if idx < 0:
            return {"error": "未找到关键词 %r，可用: %s" % (section, list(GUIDE_SECTIONS.values()))}
        nxt = text.find("\n## ", idx + 1)
        return text[idx:nxt if nxt > 0 else len(text)][:3000]

    state_manifest = {
        "name": "mc_state",
        "description": "读取 MC AI 玩家的当前状态：位置/生命/饥饿/时间/手持/准星目标/附近玩家/聊天等。"
                       "fields 可只取部分字段（省略返回核心全量；★不存在的字段会在 _unknown_fields "
                       "报回并附现有字段表，不会静默丢失）。"
                       "★screen 字段直接显示有无界面开着（class/title/pause；界面开着会完全屏蔽移动输入"
                       "——移动零位移先看它），控件/槽位详情用 mc_act type=\"get_screen\"。",
        "parameters": {"type": "object", "properties": {
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "要提取的字段名列表，省略则返回核心全量"}},
        },
    }
    act_manifest = {
        "name": "mc_act",
        "description": "对 MC AI 玩家执行一个动作并等待结果。type 为动作名（★记不准先发 type=\"actions\" 列出全部合法名，猜只会得到 unknown）。"
                       "移动/朝向：look（x,y,z 目标点或 yaw,pitch，直接生效）、move（dir=forward/back/left/right，"
                       "可加 jump/sneak/sprint、duration——★与 look 必须分两次调用；★执行后自动校验位移，"
                       "零位移会返回 _warn：先 get_screen 查界面（界面开着=移动输入被完全屏蔽），"
                       "close_screen 后重试，切勿不查原因连续重试）、turn/press(key=绑定名或物理名)/"
                       "keys(keys=[绑定名...],duration 持续按住)/jump/sneak/select/scroll/mouse_move；"
                       "世界查询：find_block(id,radius 子串|分隔)、block_at(x,y,z)、scan_region、blockstate；"
                       "方块实体：be_nbt(可选 x/y/z 或先 look 对准,可选 key=顶层键)——读任意方块实体已同步"
                       "NBT(SNBT 截断4000)；★辉光管板 bits_n_bobs:nixie_board 的显示文字就在这（据点南侧"
                       " 198,77,-1103 一墙），服务器无 data 命令时的唯一读取途径；"
                       "物品组件：item_nbt(slot 默认手持)——读物品完整数据组件（耐久 damage/max_damage、"
                       "附魔、自定义名，SNBT）；★查耐久/附魔/物品详情用它，不要猜 mc_state 有 durability"
                       " 字段（不存在，实测走过弯路）；★NBT 写不存在客户端→服务器通道（服务器权威），"
                       "sign_edit 是唯一合法写路径；"
                       "告示牌：sign_read(可选 x/y/z 或先 look 对准——读正反两面4行文本/颜色/辉光标志，"
                       "已加载区块内任意距离可读)、sign_edit(lines=[最多4行],可选 x/y/z 或先 look 对准)——"
                       "★告示牌写文本**只能用 sign_edit**（一次动作直接写 4 行，服务端持久化）；"
                       "**禁止**在编辑界面逐键 press 打字（每字符一动作且字符事件不可靠，"
                       "实测三次运行烧穿 150 万 token 全耗在这）——打字类界面同样禁用此法；"
                       "界面：get_screen（控件cx/cy；★列表类界面（石碑选择等）摘要里直接含"
                       "「列表」=条目数+各条目文本与坐标，数选项看它即可，不要 tool_result 反复取全文；"
                       "★维度网络终端会返回全部927槽的大结果，系统会自动压缩成紧凑摘要=只含非空槽+控件，"
                       "直接用摘要里的 cx/cy 点击；★界面开着时 move/keys/jump 全部失效——"
                       "移动零位移先查它，有关着的界面就 close_screen）、screen_click(x,y,button 真鼠标GUI坐标；"
                       "★button=0左键=取物/常规、1=右键、2=中键；从维度网络取物用 button=0)、"
                       "screen_click_widget、"
                       "screen_type(index 或 x,y + text 写输入框)、overlay、close_screen、reload_chunks；"
                       "★聊天界面（ChatScreen）只许 screen_type 打字 + press key.keyboard.enter 发送，"
                       "禁止 screen_click 猜坐标（控件无文本、点了不知道点在哪，实测连点十几次全白搭）；"
                       "容器：get_container（★看容器里有什么只用它：每槽带 section=container|inventory 与 ranges 分区）、"
                       "click_slot(slot,button,shift——原版容器可用，维度网络终端无效；"
                       "★slot 是菜单全局索引：普通容器=容器区在前、背包区在后（分界随容器大小变，看 ranges），"
                       "★维度网络终端才是玩家区0-35/存储区36+；点在背包区=动自己的东西，会收到 _zone_warn)、"
                       "get_inventory（★只返回你自己那36格，界面开着也看不到容器内容）、"
                       "cont_button(id：切石机=配方索引)；"
                       "其他：state/time/say/players(仅附近)/entities/get_inventory/command(cmd)/"
                       "online_players(全服在线Tab名单，按延迟排序)/ping；"
                       "★模组没有 place/dig/goto/net_click/net_search/cont_open/cont_close："
                       "放方块=look 瞄准目标格下方地板顶面中心+keys key.use 250ms；挖方块=look 瞄准+keys key.attack 1000ms；"
                       "开界面=look 瞄准+keys key.use 700ms；关界面=close_screen；"
                       "维度网络=press key.keyboard.o 开终端→screen_type index=8 中文名搜索→screen_click 按 cx/cy 取放；"
                       "★取出三步缺一不可：①空光标 screen_click 存储槽（物品到光标）②再 screen_click 快捷栏空槽"
                       "（玩家区最底行，物品进背包且可被 select 拿起）③才 close_screen——带光标直接关界面物品会退回网络槽白干；"
                       "★中文名搜不到就换更短词（经验块显示名是「经验块」不是「经验方块」）；持物点任意已占用存储槽=按类型入网；"
                       "★可选能力：screenshot —— 截当前画面存文件（★默认被框架拦截：当前模型无视觉"
                       "能力看不到图，调了只会得到拦截说明；读界面一律 get_screen；换视觉模型后可解锁）。"
                       "系统不会自动截图。"
                       "红线：绝不攻击玩家。",
        "parameters": {"type": "object",
                       "required": ["type"],
                       "properties": {"type": {"type": "string", "description": "动作名"}},
                       },
    }
    guide_manifest = None  # mc_guide 已移除：精华入 system prompt，工具化反而诱发元循环空转
    batch_manifest = {
        "name": "mc_batch",
        "description": "批量执行动作序列（一次调用完成多个动作，返回最后结果）。actions 为动作对象数组，"
                       "每个元素形如 {\"type\":\"click_slot\",\"slot\":5,\"button\":1}。"
                       "适合合成放料/取件等连续 click_slot 序列——一回合完成整段操作。执行后用 cont_info 复核。",
        "parameters": {"type": "object", "required": ["actions"],
                       "properties": {"actions": {"type": "array",
                                                  "items": {"type": "object"}}}},
    }

    def _batch(actions):
        if not game_online(cfg)[0]:
            return _offline_result()
        return mc_batch(cfg, actions)

    def _chat(count=30):
        """agent 主动获取游戏聊天栏历史（全量缓冲，不受自动注入去重限制）。"""
        if not game_online(cfg)[0]:
            return _offline_result()
        try:
            count = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            count = 30
        try:
            r = requests.get(_base(cfg) + "/chat", params={"since": 0, "limit": count},
                             timeout=8)
            r.raise_for_status()
            d = r.json()
        except requests.RequestException as e:
            return {"error": "聊天缓冲不可达: %s" % e}
        entries = d.get("entries") or []
        if not entries:
            return {"count": 0, "total": d.get("total", 0), "note": "缓冲区暂无聊天记录"}
        lines = []
        for e in entries:
            ts = time.strftime("%H:%M:%S", time.localtime(e.get("t", 0)))
            lines.append("#%d %s %s" % (e.get("id"), ts, e.get("text", "")))
        return {"count": len(entries), "total": d.get("total"), "lines": lines}

    chat_manifest = {
        "name": "mc_chat",
        "description": "主动获取游戏聊天栏历史消息（全量）。每轮对话前的自动推送只包含未注入的新消息；"
                       "需要更早的聊天记录时用本工具按条数取回。",
        "parameters": {"type": "object",
                       "properties": {"count": {"type": "integer",
                                                "description": "取回最近多少条，默认30，最大100"}}},
    }

    return [
        (state_manifest, _state),
        (act_manifest, _act),
        (batch_manifest, _batch),
        (chat_manifest, _chat),
    ]

# -*- coding: utf-8 -*-
"""受限文件读写工具（权限表版）。

权限表来自 config.yaml 的 permissions 段：

    permissions:
      file_mode: full            # full | readonly | off
      roots:                     # 具名白名单根：文件操作只能落在其中之一
        agent: "."               # 相对路径基于 AGENT_ROOT
        shared: "D:/mc/game/aiplayer"       # 示例：模组侧运行数据目录（按本机游戏目录修改）
      default_root: agent        # 相对路径默认落在哪个根

★两道防线，缺一不可：
  1. **装配期**：off → 本模块不产出任何工具；readonly → 只产出读工具。
     被禁的能力不存在于注册表里，模型看不到也就不会浪费轮次去试。
  2. **调用期**：多根白名单 + realpath 归一 + 包含判定。
     realpath 会一并解开符号链接/junction，因此"工作区里放个链接指向外部"会被判越界。

★收窄语义：角色的 file_mode 只能比父级更严，不能更宽（见 intersect）。
"""
import os
import re
import sys

from core import config_loader

# 模式强度：off < readonly < full
_ORDER = {"off": 0, "readonly": 1, "full": 2}
_ALIASES = {
    "read": "readonly", "readonly": "readonly", "read_only": "readonly", "ro": "readonly",
    "none": "off", "disabled": "off", "off": "off",
    "full": "full", "rw": "full", "write": "full",
}

WINDOWS = sys.platform.startswith("win")


def normalize_mode(value):
    """把用户写的模式名归一为 full / readonly / off。

    ★2026-10-18 修：YAML 1.1 把裸 `off` / `no` / `false` 解析成**布尔 False**，
    而 `str(False or "")` = `""` → 落回默认 `"full"`。于是
    `file_mode: off`（机主想**关掉**文件能力）实际变成 **全开**——权限反转，方向危险。
    现在显式处理布尔：False → off、True → full；其余无法识别仍按 full（兼容旧配置）。
    """
    if isinstance(value, bool):
        return "full" if value else "off"
    return _ALIASES.get(str(value or "").strip().lower(), "full")


def intersect(parent_mode, child_mode):
    """取二者中更严的那个。角色的声明_永不_放宽父级。"""
    a, b = normalize_mode(parent_mode), normalize_mode(child_mode)
    return a if _ORDER[a] <= _ORDER[b] else b


def _perm(cfg):
    p = (cfg or {}).get("permissions") or {}
    return normalize_mode(p.get("file_mode", "full")), p


def resolve_roots(cfg):
    """具名根 → 绝对真实路径。缺省 = 只有 agent 根（= AGENT_ROOT，保持旧行为）。"""
    _, p = _perm(cfg)
    raw = p.get("roots")
    if not isinstance(raw, dict) or not raw:
        raw = {"agent": "."}
    agent_root = os.path.realpath(config_loader.AGENT_ROOT)
    out = {}
    for name, value in raw.items():
        v = str(value)
        out[str(name)] = os.path.realpath(v if os.path.isabs(v) else os.path.join(agent_root, v))
    return out


def default_root_name(cfg):
    roots = resolve_roots(cfg)
    _, p = _perm(cfg)
    name = str(p.get("default_root") or "")
    return name if name in roots else sorted(roots)[0]


def _norm(path):
    return path.lower() if WINDOWS else path


# ---------- 敏感文件拦截（2026-10-18） ----------
# ★为什么在路径层拦而不是单个工具里拦：file_copy 能把密钥复制成普通文件再读，
#   只堵 read 必被绕过。读取/写入的每个入口都过 is_sensitive。
# keys.yaml     = API 密钥（读 → 进 LLM 上下文/日志/外部 API = 外泄；写 → 被诱导换钥）
# approvals.json = 审批令牌状态（写 → 可伪造 grant 绕过审批体系——args_key 是 sha1，
#                  源码就在 agent 手里，完全可以自算）
# config_loader 直读 keys.yaml 不走工具层，框架自身加载不受影响。
SENSITIVE_BASENAMES = {"keys.yaml", "approvals.json"}


def is_sensitive(real):
    """路径是否指向敏感文件（basename 精确匹配，Windows 忽略大小写）。"""
    base = os.path.basename(str(real))
    if WINDOWS:
        base = base.lower()
    return base in SENSITIVE_BASENAMES


def sensitive_block(real):
    """对敏感文件的统一拒绝回复（说明原因，别让 agent 反复试）。"""
    return {"ok": False, "blocked": True,
            "reason": "敏感文件拒绝访问：%s（含密钥/审批状态；防聊天注入把密钥带进上下文）"
                      % os.path.basename(str(real))}


def _inside(root, target):
    root_n, target_n = _norm(root), _norm(target)
    if target_n == root_n:
        return True
    return target_n.startswith(root_n.rstrip("\\/") + os.sep)


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _walks_through_reparse(base, target):
    """★reparse 点逃逸检测（2026-10-18，test_permissions 实测暴露）：
    Windows 无特权环境下 os.symlink 对目录会**退化为 junction**（mount point）——
    此时 os.path.islink() 返回 False、os.path.realpath() **也不解析**，
    realpath 白名单防线被整体绕过（safe_path 把 ws/escape/secret.txt 当成根内路径放行）。
    逐级 lstat 检查 FILE_ATTRIBUTE_REPARSE_POINT 补上这条缝；POSIX 上 realpath
    已解开符号链接，这里 islink 兜底（正常应恒为 False）。"""
    try:
        rel = os.path.relpath(target, base)
    except ValueError:
        return True
    if rel == os.curdir:
        return False
    cur = base
    for part in rel.split(os.sep):
        if part in (os.curdir, ""):
            continue
        if part == os.pardir:
            return True   # target 不应在 base 之外还带 ..（_inside 已保证，双保险）
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except OSError:
            return False  # 中间组件不存在：无链接可解析，按现状放行
        if getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            return True
        if os.path.islink(cur):
            return True
    return False


def _split_root_prefix(raw, roots):
    """解析 `root:相对路径` 写法（如 `shared:docs/记忆`）。返回 (根名, 相对路径) 或 None。

    ★为什么要有（2026-10-18 实测）：file_list/file_stat 的**返回值**是 `shared:docs/记忆/...`
    这种带根名的格式，agent 照着这个格式当**输入**再调一次时却报"不是目录"——
    输出格式和输入语法不一致，害它连试 3 次（相对路径→shared:前缀→绝对路径）。
    """
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(?![/\\])(.*)$", raw)
    if m and m.group(1) in roots:
        return m.group(1), m.group(2)
    return None


def safe_path(cfg, path):
    """把请求路径解析到某个白名单根内；越界抛 ValueError。

    绝对路径必须落在某个白名单根内；相对路径先按 `root:rel` 前缀解析（无前缀则拼 default_root）。
    realpath 同时完成"归一 + 解开符号链接"，因此不会出现链接逃逸。
    """
    roots = resolve_roots(cfg)
    default = default_root_name(cfg)
    raw = str(path or "")
    pref = _split_root_prefix(raw, roots)
    if pref is not None:
        raw = os.path.join(roots[pref[0]], pref[1])
    candidate = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(roots[default], raw))
    for root in roots.values():
        if _inside(root, candidate):
            if _walks_through_reparse(root, candidate):
                raise ValueError("路径经过符号链接/junction（reparse 点），拒绝：%r" % raw)
            return candidate
    raise ValueError("路径越界：只允许以下工作区之内 %s（收到 %r）"
                     % (sorted(roots.items()), raw))


def describe_roots(cfg):
    roots = resolve_roots(cfg)
    return "、".join("%s=%s" % (k, v) for k, v in sorted(roots.items()))


def safe_path_gated(cfg, path, tool, args, risk="high"):
    """白名单判定 + 越界走授权申请（2026-10-18）。

    返回 (real_path, None) 或 (None, 申请提示 dict)。
    越界时不再"一拒了之"：若操作者已在控制台批准过这一次的 (工具+参数)，则本次放行；
    否则登记一条待审核申请。这样"白名单外的正当需求"仍有一条受控通道。
    """
    from core import approval
    try:
        return safe_path(cfg, path), None
    except ValueError as e:
        if approval.check(cfg, tool, args):
            # 已批准 → 本次放行（令牌在 check 内已被消费）
            raw = str(path or "")
            roots = resolve_roots(cfg)
            base = roots.get(default_root_name(cfg), config_loader.AGENT_ROOT)
            return os.path.realpath(raw if os.path.isabs(raw)
                                    else os.path.join(base, raw)), None
        if not approval.enabled(cfg):
            return None, {"ok": False, "blocked": True, "reason": str(e)}
        return None, approval.require(cfg, tool, args, reason=str(e)[:160],
                                      risk=risk,
                                      how_to="批准后可用**相同路径**重试一次（单次放行）。")


def make_file_tools(cfg, mode_override=None):
    """按权限表产出工具清单。

    mode_override：角色声明的 file_mode，只会**收窄**父级模式。
    off → []；readonly → 仅读工具；full → 读 + 写。
    """
    mode, _ = _perm(cfg)
    if mode_override is not None:
        mode = intersect(mode, mode_override)
    if mode == "off":
        return []
    default = default_root_name(cfg)
    roots_desc = describe_roots(cfg)

    def _read(path, max_chars=8000):
        real, gate = safe_path_gated(cfg, path, "file_read", {"path": path}, risk="medium")
        if gate is not None:
            return gate
        if is_sensitive(real):
            return sensitive_block(real)
        if os.path.isdir(real):
            # 目录 → 列条目（名称/类型/大小），供 agent 定位文件
            out = []
            for n in sorted(os.listdir(real)):
                full = os.path.join(real, n)
                out.append("%s%s (%d字)" % (n, "/" if os.path.isdir(full) else "",
                                            os.path.getsize(full) if os.path.isfile(full) else 0))
            return {"path": path, "is_dir": True, "entries": out}
        with open(real, "r", encoding="utf-8") as f:
            data = f.read(max_chars)
        return {"path": path, "content": data,
                "truncated": os.path.getsize(real) > max_chars}

    def _write(path, content, append=False):
        real, gate = safe_path_gated(cfg, path, "file_write", {"path": path}, risk="medium")
        if gate is not None:
            return gate
        if is_sensitive(real):
            return sensitive_block(real)
        os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
        mode_f = "a" if append else "w"
        with open(real, mode_f, encoding="utf-8") as f:
            f.write(content)
        return {"path": path, "bytes": len(content.encode("utf-8")), "appended": append}

    read_manifest = {
        "name": "file_read",
        "description": "读文件（越界拒绝；相对路径默认落在 %s）。白名单工作区：%s。"
                       "路径是目录时返回条目列表。返回前 8000 字符。"
                       "★也支持「根名:相对路径」写法（如 shared:docs/记忆/INDEX.md）——"
                       "和工具返回里看到的格式一致，直接照抄即可。"
                       "★大文件用 max_chars 控制返回长度（默认 8000，超长会被标记 truncated）。"
                       % (default, roots_desc),
        "parameters": {"type": "object", "required": ["path"],
                       "properties": {"path": {"type": "string"},
                                      "max_chars": {"type": "integer",
                                                    "description": "最多返回字符数，默认 8000"}}},
    }
    write_manifest = {
        "name": "file_write",
        "description": "写文件（覆盖或追加；越界拒绝；相对路径默认落在 %s）。白名单工作区：%s。"
                       "当前文件模式 = %s。" % (default, roots_desc, mode),
        "parameters": {"type": "object", "required": ["path", "content"],
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"},
                                      "append": {"type": "boolean"}}},
    }
    tools = [(read_manifest, _read)]
    if mode == "full":
        tools.append((write_manifest, _write))
    return tools

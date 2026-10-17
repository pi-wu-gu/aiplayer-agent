# -*- coding: utf-8 -*-
"""配置加载：config.yaml（全局）+ keys.yaml（密钥）合并与校验。阶段0"""
import os
import re
import copy
import yaml


def resolve_agent_root():
    """安装根解析。优先级：AIPLAYER_HOME 环境变量 > 代码所在目录。

    AIPLAYER_HOME 供模组 sidecar 部署/独立启动注入（指向包含 run.py+core 的目录），
    也支持开发者覆盖迭代；未设置或无效时回退代码位置（现行为，向后兼容）。"""
    home = os.environ.get("AIPLAYER_HOME", "").strip()
    if home and os.path.isdir(home):
        return os.path.abspath(home)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


AGENT_ROOT = resolve_agent_root()
CONFIG_PATH = os.path.join(AGENT_ROOT, "config", "config.yaml")
KEYS_PATH = os.path.join(AGENT_ROOT, "config", "keys.yaml")

ENV_REF = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")

KEYS_TEMPLATE = """\
# keys.yaml —— API 密钥（★此文件绝不能提交/分发/打包）
# 填入真实 api_key 后重试；api_key 也支持 $ENV_VAR 形式引用环境变量
providers:
  - name: deepseek
    base_url: https://api.deepseek.com/v1
    api_key: "在此填入密钥"
    balance_api: https://api.deepseek.com/user/balance

# 模型单价（每百万 token，人民币），用于成本估算；不填则不记账
model_prices: {}
"""


def ensure_keys_template():
    """keys.yaml 不存在时生成模板。返回文件路径（已存在则原样返回）。"""
    if not os.path.exists(KEYS_PATH):
        os.makedirs(os.path.dirname(KEYS_PATH), exist_ok=True)
        with open(KEYS_PATH, "w", encoding="utf-8") as f:
            f.write(KEYS_TEMPLATE)
    return KEYS_PATH


def _resolve_env(value):
    """api_key 支持 $ENV_VAR 引用；未定义时返回空串并标记。"""
    if isinstance(value, str):
        m = ENV_REF.match(value.strip())
        if m:
            return os.environ.get(m.group(1), "")
    return value


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def load_keys():
    if not os.path.exists(KEYS_PATH):
        ensure_keys_template()  # 首次部署（sidecar/独立启动）自动落模板
        raise FileNotFoundError("已生成密钥模板 config/keys.yaml —— 请填入 api_key 后重试")
    with open(KEYS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    providers = []
    for p in data.get("providers", []):
        p = dict(p)
        p["api_key"] = _resolve_env(p.get("api_key", ""))
        if p.get("api_key"):
            providers.append(p)
    prices = data.get("model_prices") or {}
    return providers, prices


def masked(key):
    """日志/展示用脱敏。"""
    if not key:
        return "(空)"
    if len(key) <= 10:
        return key[:3] + "***"
    return key[:6] + "***" + key[-4:]


def full_load():
    cfg = load_config()
    providers, prices = load_keys()
    cfg = copy.deepcopy(cfg)
    cfg["_providers"] = providers
    cfg["_model_prices"] = prices
    if not cfg["defaults"].get("provider"):
        cfg["defaults"]["provider"] = providers[0]["name"] if providers else ""
    return cfg


def get_provider(cfg, name=None):
    """按 name 取 provider；缺省取 defaults.provider；无可用则抛异常。"""
    name = name or cfg["defaults"].get("provider")
    for p in cfg["_providers"]:
        if p["name"] == name:
            return p
    avail = [p["name"] for p in cfg["_providers"]]
    raise KeyError("provider %r 不存在或 key 为空；可用: %s" % (name, avail or "无"))

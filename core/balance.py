# -*- coding: utf-8 -*-
"""余量查询 adapter：deepseek / one-api / moonshot / siliconflow / auto 探测。阶段1"""
import requests


class BalanceError(Exception):
    pass


def _bearer(p):
    return {"Authorization": "Bearer " + p["api_key"]}


def _fmt(amount, currency="CNY"):
    try:
        return "%s %s" % (round(float(amount), 2), currency)
    except (TypeError, ValueError):
        return "unknown"


def q_deepseek(p, timeout):
    r = requests.get(p["base_url"].rstrip("/") + "/user/balance", headers=_bearer(p), timeout=timeout)
    if r.status_code != 200:
        raise BalanceError("HTTP %s" % r.status_code)
    infos = (r.json().get("balance_infos") or [{}])
    return [{"amount": i.get("total_balance"), "currency": i.get("currency", "CNY")} for i in infos]


def q_one_api(p, timeout):
    """one-api/new-api 中转：OpenAI 旧版计费格式。"""
    base = p["base_url"].rstrip("/")
    if "/v1" in base:
        base = base.split("/v1")[0]
    s = requests.get(base + "/v1/dashboard/billing/subscription", headers=_bearer(p), timeout=timeout)
    u = requests.get(base + "/v1/dashboard/billing/usage", headers=_bearer(p), timeout=timeout)
    if s.status_code != 200:
        raise BalanceError("subscription HTTP %s" % s.status_code)
    total = s.json().get("hard_limit_usd")
    used = 0.0
    if u.status_code == 200:
        used = (u.json().get("total_usage") or 0) / 100.0
    return [{"amount": total - used, "currency": "USD", "total": total, "used": used}]


def q_moonshot(p, timeout):
    r = requests.get(p["base_url"].rstrip("/") + "/users/me/balance", headers=_bearer(p), timeout=timeout)
    if r.status_code != 200:
        raise BalanceError("HTTP %s" % r.status_code)
    d = r.json().get("data") or {}
    return [{"amount": d.get("available"), "currency": "CNY",
             "voucher": d.get("voucher"), "cash": d.get("cash_balance")}]


def q_siliconflow(p, timeout):
    base = p["base_url"].rstrip("/")
    if "/v1" in base:
        base = base.split("/v1")[0]
    r = requests.get(base + "/v1/user/info", headers=_bearer(p), timeout=timeout)
    if r.status_code != 200:
        raise BalanceError("HTTP %s" % r.status_code)
    d = r.json().get("data") or {}
    return [{"amount": d.get("balance"), "currency": "CNY", "charge": d.get("chargeBalance")}]


_ADAPTERS = {"deepseek": q_deepseek, "one-api": q_one_api, "moonshot": q_moonshot, "siliconflow": q_siliconflow}


def _detect(p):
    """auto：按域名特征猜协议。"""
    host = p["base_url"].lower()
    if "deepseek" in host:
        return "deepseek"
    if "moonshot" in host:
        return "moonshot"
    if "siliconflow" in host:
        return "siliconflow"
    if "stepfun" in host:
        return "stepfun"
    return "one-api"  # 中转站默认尝试


def q_stepfun(p, timeout):
    """阶跃星辰：无公开余额端点约定；探测常见路径，失败即标不支持。"""
    for path in ("/user/balance", "/users/me/balance", "/v1/user/info"):
        try:
            r = requests.get(p["base_url"].rstrip("/") + path, headers=_bearer(p), timeout=timeout)
            if r.status_code == 200:
                return [{"amount": "见响应", "currency": "?", "raw": r.text[:200]}]
        except requests.RequestException:
            continue
    raise BalanceError("无公开余额端点")


_ADAPTERS["stepfun"] = q_stepfun


def query(p, timeout=15):
    """返回 [{amount, currency, ...}]；不支持时抛 BalanceError。"""
    proto = p.get("balance_api", "auto")
    if proto in ("none", "", None):
        raise BalanceError("该 provider 配置为不支持自动查询")
    if proto == "auto":
        proto = _detect(p)
    fn = _ADAPTERS.get(proto)
    if not fn:
        raise BalanceError("未知协议 %r" % proto)
    return fn(p, timeout)

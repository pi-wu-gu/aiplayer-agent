# -*- coding: utf-8 -*-
"""余量查询工具：balance_query(name)。阶段2"""
from core import balance, config_loader


def make_balance_tool(cfg):
    def _balance_query(name=None):
        p = config_loader.get_provider(cfg, name)
        try:
            return {"provider": p["name"], "balances": balance.query(p)}
        except Exception as e:
            return {"provider": p["name"], "error": str(e),
                    "hint": "该平台可能无公开余额 API，请网页自查"}

    manifest = {
        "name": "balance_query",
        "description": "查询某个 provider 的 API 余量。name 省略时查默认 provider。",
        "parameters": {"type": "object",
                       "properties": {"name": {"type": "string"}}},
    }
    return [(manifest, _balance_query)]

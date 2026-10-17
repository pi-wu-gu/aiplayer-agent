# -*- coding: utf-8 -*-
"""统一 LLM 客户端：OpenAI 兼容 /chat/completions + 用量记账。阶段1"""
import json
import time
import os

import requests

from core import config_loader


class LLMError(Exception):
    pass


def _log_dir(cfg):
    d = os.path.join(config_loader.AGENT_ROOT, cfg.get("llm", {}).get("usage_log", "logs/usage.jsonl"))
    return d if os.path.isabs(d) else os.path.join(config_loader.AGENT_ROOT, d)


def chat(cfg, provider_name=None, model=None, messages=None, tools=None,
         temperature=None, max_tokens=None, _retry=0):
    """单次对话；返回 {'content', 'tool_calls', 'usage', 'raw'}。带 429/超时指数退避。"""
    p = config_loader.get_provider(cfg, provider_name)
    llm_cfg = cfg.get("llm", {})
    model = model or cfg["defaults"].get("model")
    body = {
        "model": model,
        "messages": messages or [],
        "temperature": cfg["defaults"].get("temperature", 0.7) if temperature is None else temperature,
        "max_tokens": max_tokens or cfg["defaults"].get("max_tokens", 2048),
    }
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]

    url = p["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer " + p["api_key"], "Content-Type": "application/json"}
    timeout = llm_cfg.get("request_timeout", 60)
    max_retries = llm_cfg.get("max_retries", 3)

    t0 = time.time()
    try:
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as e:
        if _retry < max_retries:
            time.sleep(2 ** _retry)
            return chat(cfg, provider_name, model, messages, tools, temperature, max_tokens, _retry + 1)
        raise LLMError("请求失败(%s): %s" % (p["name"], e))

    if r.status_code in (429, 500, 502, 503) and _retry < max_retries:
        time.sleep(2 ** _retry)
        return chat(cfg, provider_name, model, messages, tools, temperature, max_tokens, _retry + 1)
    if r.status_code != 200:
        raise LLMError("HTTP %s @%s: %s" % (r.status_code, p["name"], r.text[:300]))

    data = r.json()
    _record_usage(cfg, p["name"], model, data.get("usage") or {}, time.time() - t0)
    _record_call(cfg, p["name"], model, data, time.time() - t0)

    msg = (data.get("choices") or [{}])[0].get("message") or {}
    finish = (data.get("choices") or [{}])[0].get("finish_reason")
    # 空响应保护：content 空且无工具调用（推理吃光额度或异常）→ 一次加倍重试
    if (not msg.get("content") and not msg.get("tool_calls") and _retry < 1):
        return chat(cfg, provider_name, model, messages, tools,
                    temperature, (max_tokens or 2048) * 4, _retry + 1)
    return {
        "content": msg.get("content"),
        # 推理模型（step/deepseek-reasoner 等）的思考过程字段
        "reasoning": msg.get("reasoning_content") or msg.get("reasoning"),
        "tool_calls": msg.get("tool_calls"),
        "usage": data.get("usage") or {},
        "raw": msg,
    }


def _call_log_path(cfg):
    d = cfg.get("llm", {}).get("call_log", "logs/llm_calls.jsonl")
    return d if os.path.isabs(d) else os.path.join(config_loader.AGENT_ROOT, d)


def _record_call(cfg, provider, model, data, elapsed):
    """每次 LLM 调用的完整输出单独落盘，供离线分析（与 usage 账本分开）。"""
    try:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        rec = {
            "t": int(time.time()), "provider": provider, "model": model,
            "finish_reason": choice.get("finish_reason"), "elapsed": round(elapsed, 2),
            "usage": data.get("usage") or {},
            "content": msg.get("content"),
            "reasoning": msg.get("reasoning_content") or msg.get("reasoning"),
            "tool_calls": msg.get("tool_calls"),
        }
        path = _call_log_path(cfg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _record_usage(cfg, provider, model, usage, elapsed):
    # ★缓存字段（2026-10-16 修正）：DeepSeek 报的是 prompt_cache_hit/miss_tokens，
    # 而 OpenAI 风格叫 cached_tokens——以前只记后者，导致账本上缓存命中恒为 0
    # （实测真实命中率 86%，成本被严重误读）。两种命名都记。
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is None:
        hit = usage.get("cached_tokens")
    rec = {"t": int(time.time()), "provider": provider, "model": model,
           "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
           "total_tokens": usage.get("total_tokens"), "elapsed": round(elapsed, 2),
           "cache_hit": hit, "cache_miss": miss}
    try:
        path = _log_dir(cfg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def list_models(cfg, provider_name):
    """GET /models；返回模型 id 列表。"""
    p = config_loader.get_provider(cfg, provider_name)
    url = p["base_url"].rstrip("/") + "/models"
    r = requests.get(url, headers={"Authorization": "Bearer " + p["api_key"]},
                     timeout=cfg.get("llm", {}).get("request_timeout", 60))
    if r.status_code != 200:
        raise LLMError("HTTP %s @%s/models: %s" % (r.status_code, p["name"], r.text[:200]))
    return [m.get("id") for m in (r.json().get("data") or [])]

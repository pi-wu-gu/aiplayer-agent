# -*- coding: utf-8 -*-
"""8780 控制面 CLI（aiplayer 模组 HTTP 控制面）。

用法：
    from aitest.ctl import cmd, call, state
    r = cmd({"type": "screen_click"}, x=141, y=56, button=0)   # 单动作
    rs = call([{"type": "press", "key": "key.keyboard.o"},
               {"type": "get_screen"}])                        # 批量，返回结果列表
    s = state()                                                # GET /state

约定（踩坑总结）：
- POST /command body 必须是 {"commands": [...]}；动作对象用 **type** 字段
- 返回包装：{"queued","ok","failed","results":[{"type","ok","result"}],"last_action"}
  → 真实结果在 results[0].result；mc_act/mc_batch 的 result 里还嵌一层 {"action":...}
- get_screen：终端一次返回全部 927 槽（很大）；槽位 cx/cy 每次重开有 ±4px 漂移，以当次为准
- screen_click 用 x/y（gui 坐标，不是 cx/cy 的槽中心也行——模组会换算 px/py）
- 维度网络取出三步：点存储槽 → 点快捷栏空槽 → close_screen（带光标关界面物品会退回网络槽）
"""
import json
import urllib.request

BASE = "http://127.0.0.1:8780"


def _post(path, payload, timeout=30):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def cmd(action, **params):
    """执行单个动作，返回其 result（去掉外层包装）。"""
    body = dict(action)
    body.update(params)
    r = _post("/command", {"commands": [body]})
    return r["results"][0]["result"]


def call(actions):
    """批量执行，返回 results 列表（每项 {"type","ok","result"}）。"""
    r = _post("/command", {"commands": actions})
    return r.get("results", [])


def state():
    """GET /state 原始状态字典。"""
    return _get("/state")


def screen():
    """get_screen 的内层 result（含 widgets/slots/cursor）。"""
    return cmd({"type": "get_screen"})


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(0)
    action = json.loads(sys.argv[1])
    print(json.dumps(cmd(action), ensure_ascii=False, indent=1))

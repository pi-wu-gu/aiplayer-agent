# -*- coding: utf-8 -*-
"""agent 入口。
--check            校验全部 provider（连通/余量/对话）
--offline          离线自检（不调 LLM，只测工具注册与桥接）
--task "..."       启动主 agent 执行任务（单次）
--role NAME        角色定义（agents/NAME.yaml；缺省用内置 primary）
--max-rounds N     工具循环轮数上限（默认12）
--daemon           持续运行模式：监听聊天@提及+定时唤醒积压+每32轮审查（见 core/daemon.py）"""
import sys
import argparse
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config_loader, llm_client, balance  # noqa: E402
from core import runner  # noqa: E402
from core import daemon as daemon_mod  # noqa: E402
from core.instance_lock import InstanceLock  # noqa: E402
from tools.builtin import mc_bridge, shell_exec  # noqa: E402


def cmd_check(cfg):
    print("=" * 62)
    providers = cfg["_providers"]
    if not providers:
        print("!! 没有可用 provider —— 请编辑 config/keys.yaml 填入 key")
        return 1
    for p in providers:
        print("[provider] %-12s %s  key=%s  balance_api=%s" %
              (p["name"], p["base_url"], config_loader.masked(p["api_key"]), p.get("balance_api")))
        try:
            models = llm_client.list_models(cfg, p["name"])
            shown = models[:6] + (["...共%d个" % len(models)] if len(models) > 6 else [])
            print("  连通: OK   模型(%d): %s" % (len(models), ", ".join(str(m) for m in shown)))
        except Exception as e:
            print("  连通: FAIL  %s" % e)
            continue
        try:
            for b in balance.query(p):
                print("  余量: %s" % b)
        except Exception as e:
            print("  余量: 不支持/失败: %s" % e)
        model = None
        try:
            bad = ("tts", "asr", "audio", "embed", "rerank", "music", "search-image", "realtime")
            model = next((m for m in models if m and not any(b in m.lower() for b in bad)), None)
        except NameError:
            pass
        if p["name"] == cfg["defaults"].get("provider"):
            model = cfg["defaults"].get("model") or model
        try:
            resp = llm_client.chat(cfg, p["name"], model=model,
                                   messages=[{"role": "user", "content": "只回复两个字：在线"}],
                                   max_tokens=512)
            note = "" if resp["content"] else "（content空但reasoning非空=推理模型，正常）"
            print("  对话: OK   回复=%r %s tokens=%s" %
                  (resp["content"], note, resp["usage"].get("total_tokens")))
        except Exception as e:
            print("  对话: FAIL  %s" % e)
        print("-" * 62)
    print("用量账本: logs/usage.jsonl")
    return 0


def cmd_offline(cfg):
    """离线自检：注册表 + MC 桥接连通。"""
    reg = runner.build_primary_registry(cfg)
    print("已注册工具: %s" % reg.names())
    # 参数校验测试
    bad = reg.execute("mc_act", '{"type": 123}')
    print("schema校验(应报错):", bad[:80])
    unknown = reg.execute("no_such_tool", "{}")
    print("未知工具(应报错):", unknown[:80])
    # 桥接连通
    st = reg.execute("mc_state", '{"fields": ["loaded", "pos", "health"]}')
    print("mc_state:", st[:200])
    return 0


def cmd_task(cfg, task, max_rounds=None):
    out, stats = runner.run_task(cfg, task, max_rounds=max_rounds, verbose=True)
    if stats.get("blocked"):
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description="agent 决策层入口")
    ap.add_argument("--check", action="store_true", help="校验全部 provider")
    ap.add_argument("--offline", action="store_true", help="离线自检（不调 LLM）")
    ap.add_argument("--task", help="启动主 agent 执行任务")
    ap.add_argument("--role", default="primary")
    ap.add_argument("--max-rounds", type=int, default=None, help="工具循环轮数上限（默认12）")
    ap.add_argument("--daemon", action="store_true",
                    help="持续运行模式：监听聊天@提及/定时唤醒积压/每N轮审查可强制停止")
    args = ap.parse_args()

    cfg = config_loader.full_load()
    if args.check:
        sys.exit(cmd_check(cfg))
    if args.offline:
        sys.exit(cmd_offline(cfg))
    # ★高危能力提示：本机命令行是否开启，每次起任务/守护前都显式说明
    for line in shell_exec.startup_notice(cfg):
        print(line)
    if args.task:
        # 任务与 daemon 并行是合法工作流，只做提示不阻塞（互斥由 daemon 的实例锁保证）
        running, note = InstanceLock(kind="task").probe()
        if running:
            print("[实例] %s —— 本次任务将并行执行" % note)
        sys.exit(cmd_task(cfg, args.task, max_rounds=args.max_rounds))
    if args.daemon:
        lock = InstanceLock(kind="daemon")
        ok, msg = lock.acquire()
        if not ok:
            print("[实例] " + msg)
            sys.exit(2)
        try:
            sys.exit(daemon_mod.run_daemon(cfg) or 0)
        finally:
            lock.release()
    print("用法: python run.py --check | --offline | --task \"...\" | --daemon")


if __name__ == "__main__":
    main()

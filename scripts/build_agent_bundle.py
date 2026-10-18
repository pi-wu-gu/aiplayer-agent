# -*- coding: utf-8 -*-
"""构建 agent_bundle.zip（sidecar 部署包）：源码 + python-embed 运行时 + wheels。

产物交给模组资源目录（assets/aiplayer/agent_bundle.zip），随 mod jar 分发，
由 AgentSidecar 首次启动解压到 <游戏目录>/aiplayer-agent/<版本>/。

结构（bundle 内统一前缀 agent/）：
  agent/run.py + core/ + tools/ + ...          源码（日常迭代只动这部分）
  agent/runtime/python/                        python-embed 3.13（零依赖：整合包无需装 Python）
    python.exe / python313.dll / python313.zip
    Lib/site-packages/                         wheels 解压即装（requests/pyyaml 及依赖）
    python313._pth                             覆盖：加上 site-packages + import site

★红线：keys.yaml / 日志 / 会话 / 审计 / 缓存绝不入包。

用法：
    python scripts/build_agent_bundle.py            # 打包到模组资源目录
    python scripts/build_agent_bundle.py --out X --skip-runtime   # 只打源码（快速迭代）
"""
import argparse
import os
import shutil
import tempfile
import zipfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # PCL2/agent
VENDOR = os.path.join(SRC, "vendor")
EMBED_ZIP = os.path.join(VENDOR, "python-embed.zip")
WHEELS_DIR = os.path.join(VENDOR, "wheels")
# 输出路径：环境变量 AGENT_BUNDLE_OUT 可覆盖；默认写到仓库根 agent_bundle.zip
DEFAULT_OUT = os.environ.get(
    "AGENT_BUNDLE_OUT",
    os.path.join(SRC, "agent_bundle.zip"))
PREFIX = "agent/"
RT = PREFIX + "runtime/python/"

INCLUDE_FILES = ["run.py", "config/config.yaml"]
INCLUDE_DIRS = ["core", "tools", "agents", "scripts", "docs"]
EXCLUDE_PARTS = {
    "__pycache__", "keys.yaml", ".git", ".workbuddy", "vendor",
    "logs", "session_", "usage.jsonl", "shell_audit.log",
    "denied_access.log", "review_", "chat_cursor", "agent.lock", ".pyc",
    # ★仓库维护脚本，运行时不需要，且**自身含明文敏感词/本机路径**——绝不能进分发包
    #   （2026-10-18 实测：这两个文件被打进 bundle，等于把脱敏词表一起发出去）
    "sanitize_repo.py", "filter_blob_callback.py",
}

# 覆盖 embed 默认的 _pth：站内 site-packages 即用（wheel 直接解压进去，无需 pip）
PTH_CONTENT = "python313.zip\n.\nLib/site-packages\nimport site\n"


def excluded(rel):
    parts = rel.replace("\\", "/").split("/")
    if any(p in EXCLUDE_PARTS for p in parts):
        return True
    return any(x in os.path.basename(rel) for x in EXCLUDE_PARTS)


def add_source(z):
    n = 0
    for f in INCLUDE_FILES:
        src_f = os.path.join(SRC, f)
        if not os.path.exists(src_f):
            # config/config.yaml 是实例配置（不入库）：仓库缺失时回退到模板 example，
            # 打包后仍占位为 config/config.yaml（第三方拿到后填 keys/路径即可用）
            if f == "config/config.yaml":
                src_f = os.path.join(SRC, "config/config.yaml.example")
                print("提示: 未找到实例配置 config/config.yaml，bundle 使用 config.yaml.example 模板")
            else:
                raise SystemExit("缺少必需文件: %s" % f)
        z.write(src_f, PREFIX + f)
        n += 1
    for d in INCLUDE_DIRS:
        base = os.path.join(SRC, d)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if not excluded(os.path.relpath(
                os.path.join(root, x), SRC))]
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), SRC).replace(os.sep, "/")
                if excluded(rel):
                    continue
                z.write(os.path.join(SRC, rel.replace("/", os.sep)), PREFIX + rel)
                n += 1
    return n


def add_runtime(z):
    """python-embed + wheels 解压入包。返回条目数。"""
    if not os.path.exists(EMBED_ZIP):
        raise SystemExit("缺 vendor/python-embed.zip —— 先下载 embeddable 包（见 vendor/README.md）")
    n = 0
    with zipfile.ZipFile(EMBED_ZIP) as ez:
        for info in ez.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            data = ez.read(name)
            if name == "python313._pth":
                data = PTH_CONTENT.encode("utf-8")     # 覆盖 _pth
            z.writestr(RT + name, data)
            n += 1
    for whl in sorted(os.listdir(WHEELS_DIR)):
        if not whl.endswith(".whl"):
            continue
        with zipfile.ZipFile(os.path.join(WHEELS_DIR, whl)) as wz:
            for info in wz.infolist():
                if info.filename.endswith("/"):
                    continue
                z.writestr(RT + "Lib/site-packages/" + info.filename, wz.read(info.filename))
                n += 1
    return n


def build(out, skip_runtime=False):
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        n += add_source(z)
        if not skip_runtime:
            n += add_runtime(z)
    with zipfile.ZipFile(out) as z:
        bad = [i for i in z.namelist() if "keys.yaml" in i]
        has_exe = any(i.endswith("runtime/python/python.exe") for i in z.namelist())
    if bad:
        os.remove(out)
        raise SystemExit("★红线拦截：包内发现 keys.yaml %s，已删除产物" % bad)
    size = os.path.getsize(out)
    print("OK %s\n  条目=%d  %.1f MB  内嵌运行时=%s"
          % (out, n, size / 1048576, "有" if (has_exe and not skip_runtime) else "无"))
    return out


def smoke_test(bundle):
    """本地烟测：解包到临时目录，用内嵌 python 验证 import 链（requests/yaml/项目模块）。"""
    tmp = tempfile.mkdtemp(prefix="agent_bundle_test_")
    try:
        with zipfile.ZipFile(bundle) as z:
            z.extractall(tmp)
        root = os.path.join(tmp, "agent")
        py = os.path.join(root, "runtime", "python", "python.exe")
        if not os.path.exists(py):
            print("smoke: 无内嵌运行时，跳过")
            return
        import subprocess
        code = ("import sys; sys.path.insert(0, r'%s');"
                "import requests, yaml;"
                "from core import config_loader, orchestrator, runner, daemon;"
                "from tools.builtin import mc_bridge, file_io, shell_exec, wake_words, web_fetch,"
                " keyword_memory, todo_list;"
                "print('IMPORTS-OK', sys.version.split()[0])" % root)
        r = subprocess.run([py, "-c", code], env=dict(os.environ), cwd=root,
                           capture_output=True, text=True, timeout=120, errors="replace")
        tail = (r.stdout + r.stderr).strip().splitlines()
        print("smoke exit=%s" % r.returncode)
        for line in tail[-6:]:
            print("  " + line[:150])
        if r.returncode != 0 or "IMPORTS-OK" not in r.stdout:
            raise SystemExit("★烟测失败")
        print("smoke OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--skip-runtime", action="store_true", help="只打源码（快速迭代）")
    ap.add_argument("--no-smoke", action="store_true")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    out = build(a.out, a.skip_runtime)
    if not a.no_smoke:
        smoke_test(out)

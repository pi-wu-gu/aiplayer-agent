# agent —— MC AI 玩家决策层 & 通用多 agent 框架

与 `PCL2/AI/`（MC 客户端桥接系统）同级配套。LLM 通过工具循环驱动：多 provider 密钥管理、余量查询与成本熔断、主从多 agent 协同（次级可选模型）、agent 自研工具沙箱。

## 文档
- 《docs/开发流程.md》—— 五层架构 / 目录规划 / 阶段 0-5 开发路线（每阶段带验收标准）
- 《docs/可行性分析与查漏补缺.md》—— 五项需求逐项可行性 + 15 条查漏清单
- 《../AI/docs/操作注意事项.md》—— MC 桥接协议与红线（agent 层对接依据）

## 快速开始（规划中，随阶段更新）
```
1. cp config/keys.yaml.example config/keys.yaml  # 手动填 key
2. python run.py --check                         # 校验连通性/余量/模型列表
3. python run.py --role primary --task "..."     # 启动主 agent（单次）
4. python run.py --daemon                        # 持续运行模式（见下）
```

## 权限表（config.yaml `permissions:` 节）

装配期生效：**被禁的工具根本不注册**（模型看不到，不浪费轮次），不是"注册后报错"。主 agent 与次级 agent 走同一条装配路径（`core/orchestrator.build_registry`）。

| 配置 | 取值 | 管什么 |
|---|---|---|
| `permissions.file_mode` | `full` / `readonly` / `off` | `file_read` `file_write`（off 时整组不注册） |
| `permissions.roots` | 具名白名单根 | 文件工具的边界；相对路径默认落 `default_root` |
| `permissions.shell` | 见下 | **★高危可选项：本机命令行执行** |

### ★本机命令行 `permissions.shell`（默认关闭）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | **false** | 关 → `shell_run` 不注册；开 → 启动时打印显著危险提示 |
| `mode` | `allowlist` | `allowlist`（白名单）\| `any`（任意程序，最危险） |
| `allowlist` | `[]` | 裸程序名；`mode=allowlist` 且名单为空 → **一律拒绝**（fail-closed） |
| `workdir` | `.` | 相对 AGENT_ROOT；越界回退到 AGENT_ROOT |
| `timeout_sec` / `max_output` | 30 / 8000 | 超时强杀 / 输出截断 |
| `allow_from_chat` | **false** | ★聊天/@提及/定时积压触发的运行是否也允许调用 |
| `audit_log` | `logs/shell_audit.log` | 每次调用落盘（argv/rc/耗时/输出头部） |

**四道闸**：
1. **默认关闭** —— 关掉时工具不注册（模型看不见）
2. **不给 shell** —— `subprocess(shell=False)`，只按 argv 精确执行；`|` `>` `&&` `$( )` **不会被解释**
3. **白名单** —— `argv[0]` 必须是**裸程序名**且经 `shutil.which` 解析到；显式路径一律拒
4. **聊天隔离** —— 聊天触发的运行默认**禁止**调用（`core/run_context.py` 记录触发源）

> ★**为什么必须默认关 + 聊天隔离**：agent 挂在**多人服务器**上，daemon 会因**任何人的 @提及**唤醒它，
> 而聊天内容是**不可信输入**。若放行则等于把"服务器上任何玩家 → 本机任意命令"打开（**提示词注入 → RCE**）。
>
> 安全测试见 `tests/test_shell_exec.py`（21 项：关闭不注册/白名单/禁路径/无 shell 语义/超时/截断/聊天隔离/审计）。

## 持续运行模式（daemon）
```
python run.py --daemon
```
守护进程四大机制（配置见 config.yaml `daemon:` 节）：

| 机制 | 行为 |
|---|---|
| 聊天监听 | 每 `poll_interval` 秒轮询桥接 /chat；**他人**发言 @提及 bot（`muyiyangDeepseek` 或 `mention_extra` 词）→ 立即唤醒运行。bot 自身 say 不触发（防自激） |
| 定时唤醒 | 每 `wake_interval` 秒到点检查**积压未读**；有积压才触发运行，无积压只记录 |
| 周期审查 | agent 每跑 `review_every`（默认32）轮，审查器唤醒一次：规则检查（同操作连续3次失败/连续6次失败/生命<10/token超预算）＋可选 LLM 审查（`review_llm`）。**审查有权强制停止思考**（轮边界生效，游戏内播报原因） |
| 唤醒门控 | 所有唤醒仅在 agent **未运行**时生效：运行中提到→记入积压；定时/手动唤醒→跳过并计数 |

进程结构：主线程=轮询+调度；worker 线程=跑一次 Agent（复用 `core/runner.py`）；
控制API 线程=HTTP。状态（游标/积压/统计）落盘 `logs/daemon_state.json`，重启不丢。

控制 API（默认 127.0.0.1:8770）：
```
GET  /         # Web UI 面板（状态/参数/实时行为流，浏览器直接开）
GET  /status    # idle/running、积压数、待处理提及、统计
GET  /backlog   # 最近50条积压
GET  /feed?n=80 # 实时行为流：思考/工具调用/审查/停止（读最新 behavior 日志）
GET  /config    # 读取全部运行参数
POST /config    # 热更新参数（立即生效+落盘，重启自动恢复）
POST /wake      # 手动唤醒（运行中→409，门控）
POST /stop      # 强制停止当前运行（轮边界生效）
```

UI 面板可热设置：自动检测频率 poll_interval、定时唤醒间隔、审查周期、审查是否调 LLM、
单次轮次上限、运行冷却、积压上限、提及名/额外触发词等；中栏实时滚动展示
模型思考与每次工具调用（成功/失败着色），右栏一键唤醒/强制停止。

日志：`logs/daemon.log`（过程）、`logs/daemon_state.json`（状态）、
`logs/review_日期.jsonl`（每次审查记录：轮号/结论/原因/生命/token）。

测试：`python tests/test_daemon.py`（离线单测16项）、
`python tests/test_daemon_live.py`（实网冒烟15项，stub 掉 LLM 零成本）、
`python tests/test_daemon_ui.py`（UI接口23项断言：面板/行为流/参数热更新/持久化恢复）。

## 当前状态
- [x] 目录骨架 + 配置模板 + 设计文档（阶段 0 产物）
- [x] 阶段 0/1：配置加载 + llm_client（含工具调用格式/重试/用量账本）+ 余量 adapter + `run.py --check`
- [x] 阶段 2：Agent 核心（tool loop）+ tool_registry（schema 校验）+ 内置工具 mc_state/mc_act/file_io/balance_query
- [x] 阶段 2.5 上下文优化：结果压缩+存档（core/context.py，规则式零额外调用）+ 历史归档 + tool_result 取回工具
- [x] 持续运行模式（daemon）：聊天@提及唤醒/定时唤醒积压/每32轮审查可强制停止/唤醒门控（core/daemon.py + core/runner.py，测试30项全过）
- [ ] 阶段 3-5 见《docs/开发流程.md》第三节

## 已验证事实（2026-10-15）
- step（阶跃星辰）key 有效：33 个模型；对话模型用 `step-3.7-flash` / `step-5-preview`（**推理模型**，思考占 token 且在 `reasoning_content` 字段，max_tokens 须给足）；无公开余额端点
- **DeepSeek 14:15 已恢复**：deepseek-flash / deepseek-v4-pro 可用（deepseek-chat 别名仍可用），余量 12.36 CNY；默认 provider 已切回 deepseek-flash
- **★ 桥接 /command 的 body 必须是 `{"commands": [动作]}`**——裸动作对象会被静默丢弃（queued:0）且不报错
- mc_act 结果判定：对比发送前后的 last_action 变化；**last_action 多为纯文本非 JSON**（解析失败原样返回，不再当错误）
- **★ 上下文优化实测**：真实工具结果压缩率 86%（39291→5462 字）；历史归档使长任务 token 从 128万(60轮) 降到可预期 ~15万级；agent 能正确理解"[压缩]已存档"提示并按需取回
- 合成链路全通（木板/工作台/5件木制工具），工作台为 visualworkbench 模组台（合成格内容跨会话保留）
- 用量账本 logs/usage.jsonl；agent 会话日志 logs/session_*.jsonl；测试礼仪（聊天开始说明+结束统计）已入提示词

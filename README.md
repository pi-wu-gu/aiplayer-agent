# agent —— MC AI 玩家决策层 & 通用多 agent 框架

与 `PCL2/AI/`（MC 客户端桥接系统）同级配套。LLM 通过工具循环驱动：多 provider 密钥管理、余量查询与成本熔断、主从多 agent 协同（次级可选模型）、分层记忆、危险操作授权审批、agent 自管唤醒词与唤醒黑名单。

## 文档
- 《docs/开发流程.md》—— 五层架构 / 目录规划 / 阶段 0-5 开发路线（每阶段带验收标准）
- 《docs/参数清单.md》—— `config/config.yaml` 逐键说明 + 热改白名单（★改动后请同步更新）
- 《docs/可行性分析与查漏补缺.md》—— 五项需求逐项可行性 + 15 条查漏清单
- 《docs/runtime/》—— 参考部署快照（游戏目录 `aiplayer/docs/` 为运行时最新版）
- 《../AI/docs/操作注意事项.md》—— MC 桥接协议与红线（agent 层对接依据）

## 快速开始

**前置**：Python **3.13+**（vendor/ 里的预编译 wheel 为 Windows cp313 版；Linux/macOS 需自行安装同名依赖）。
**定位**：本 agent 默认驱动 [aiplayer 模组](https://github.com/pi-wu-gu/aiplayer) 的 `127.0.0.1:8780` 控制面——独立跑源码时需要 MC 客户端（装了模组）在运行。

```
1. 安装依赖（离线 wheel 已附在 vendor/wheels，免外网）：
   pip install --no-index --find-links vendor/wheels requests pyyaml
2. cp config/keys.yaml.example config/keys.yaml    # 手动填真实 api_key（此文件永不提交）
3. 按需修改 config/config.yaml                      # 仓库内为参考实例配置，含本机路径示例
4. python run.py --check                           # 校验连通性/余量/模型列表
5. python run.py --offline                         # 离线一次性提示（不依赖 MC）
   python run.py --role primary --task "..."       # 主 agent 单次任务
   python run.py --daemon                          # 持续运行模式（见下）
```

**打包部署**：`python scripts/build_agent_bundle.py` 把本框架打成 `agent_bundle.zip`
（随模组 jar 分发，模组侧自动解压运行；内嵌运行时仅 Windows）。
Windows 快捷方式：`scripts/启动agent.bat` / `scripts/停止agent.bat`。

> ★**热部署（改 Python 源码不用重打 bundle、不用重启 MC）**：模组按版本目录缓存
> （`<gamedir>/aiplayer-agent/<mod_version>/`），把改动的文件复制进该目录 →
> 杀 `run.py --daemon` 进程 → 手动拉起即可。bundle 变了才需要升 `mod_version`。

## 工具面（19 静态 + 6 动态 + 2 按开关）

| 组 | 工具 |
|---|---|
| 游戏桥接 | `mc_state` `mc_act` `mc_batch` `mc_chat` |
| 文件（框架侧，★首选） | `file_read` `file_write` `file_list` `file_stat` `file_grep` `file_mkdir` `file_copy` `file_move` `file_delete` `file_restore` |
| 网络 / 成本 | `web_fetch` `balance_query` |
| 自管 | `wake_blacklist` `keyword_memory` `todo_list` |
| 动态（`run_task` 内） | `memory` `finish_run` `session_stats` `tool_result` `spawn_subagent` `spawn_parallel` |
| 按开关关闭 | `shell_run`（`permissions.shell.enabled=false`）、`wake_words`（`daemon.allow_ai_wake_words=false`）、`keyword_memory`（`daemon.keyword_memory_enabled=false`）、`todo_list`（`daemon.todo_list_enabled=false`） |

逐项说明见游戏目录 `aiplayer/docs/框架工具.md`。

## 权限表（config.yaml `permissions:` 节）

装配期生效：**被禁的工具根本不注册**（模型看不到，不浪费轮次），不是"注册后报错"。主 agent 与次级 agent 走同一条装配路径（`core/orchestrator.build_registry`）。

| 配置 | 取值 | 管什么 |
|---|---|---|
| `permissions.file_mode` | `full` / `readonly` / `off` | 文件工具整组（off → 全不注册；readonly → 只注册只读那几个） |
| `permissions.roots` | 具名白名单根 | 文件工具的边界；相对路径默认落 `default_root` |
| `permissions.destructive_file` | `off` / **`approval`** / `free` | 删除能力档位（默认申请授权 + 软删可恢复） |
| `permissions.approval` | `enabled` / `ttl_sec` | 授权申请体系总闸与令牌默认有效期 |
| `permissions.web` | `enabled` / `allow_from_chat` / … | 网页抓取开关与限额 |
| `permissions.shell` | 见下 | **★高危可选项：本机命令行执行** |

> ★**配置段位置**：`shell` / `web` / `approval` / `destructive_file` 都在 **`permissions`** 下面，
> 不是顶层。改完务必验证"真的生效"（改一个值看 `/config` 输出有没有变），否则会静默失效。

### 危险操作授权审批（`core/approval.py`）

```
agent 触发高危/越界动作 → 落待审核申请（绑定 工具+参数指纹）
        → 操作者在 8770 控制台「🛡 授权申请」点批准
        → 生成单次有效令牌（TTL 按风险 120-600s）
        → agent 用**相同参数**重试一次 → 执行 → 令牌用完即焚
```

- 改参数/过期/被拒 → 全部失效，需重新申请
- 同参数重复申请会复用同一条，不刷屏
- 参数指纹前会**归一路径**（`\`↔`/`、`//` 压缩、Windows 忽略大小写）
- 审计落 `logs/approval_audit.log`
- 越界路径不再硬拒，改走同一套申请（`file_io.safe_path_gated()`）
- ★接入面扩容（2026-10-18 P0/P1）：`file_copy`/`file_move` 越界（critical）、`mc_act command`（critical）、
  `mc_act connect`/`agent_stop`（medium）、`shell_exec` 越白名单（critical）
- ★审批记录带触发 agent 名（`run_context.agent_name`，primary/sub_xxx）；两级链申请带
  `chain: sub→primary→operator`；被拒申请重试会明确返回 `rejected_by`（不再刷重复申请）

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

## 分层记忆（`core/memory.py`）

```
记忆/
  unsorted/<日期>_<时分>_<触发源>.md   ← 一次唤醒一个文件（未总结）
  day/YYYY-MM-DD.md  week/YYYY-Www.md  month/YYYY-MM.md  year/YYYY.md
  raw/                                 ← 原始记录，永不压缩
```

- **到期推导**（纯推导，不用定时器）：日 < 今天 / 周 < 当前周 / 月 < 当前月 / 年 < 当前年
- **框架搬文件、agent 只写内容**：建目录/开文件/判断到期/归档/记账全归框架；agent 连 delete 都没有
- 顺序固定：`memory(pending)` 读源 → `file_append` 写目标档 → `memory(commit)` 提交
- ★**只有 day 层归档源材料**（unsorted 是中间态）；日/周/月档是成品明细，永远保留
- 总结走 agent 自身上下文，**不额外开 LLM**

## 关键词记忆（`tools/builtin/keyword_memory.py`，2026-10-19）

把术语/约定/教训做成短条目存 `config/keywords.json`，**按关键词触发式注入**任务上下文——
不占用每轮 prompt，只有任务文本里出现关键词时才把条目内容拼进任务头。

| 面 | 行为 |
|---|---|
| 条目 | `{keyword, content, fill, player, hits}`；上限 30 条 / 单条 200 字（`daemon.keyword_memory_max_entries/max_content`） |
| 三态 fill | `full`=注入全文；`keyword_only`=只注入关键词+提示 `action=lookup` 查全文（省上下文的搜索式激活）；`off`=完全不进候选 |
| 注入上限 | 单次运行最多注入 `keyword_memory_max_inject`（默认 3）条；**达到即推控制台「⚠ 警告」区** |
| 玩家专有 | `player` 字段绑定玩家名：该玩家在线时**无条件带上**（前缀【玩家X相关·在线】）；在线名单走 8780 本地队列+TTL 120s 缓存，查询失败自动降级为仅关键词命中 |
| 同词追加 | 同关键词不允许重复定义，`add` 自动把新内容追加到旧条目（合并超限被拒） |
| 风险审查 | `player_request_risk_review`（默认开）：mention/scheduled 任务注入【玩家请求风险审查】提示段 |
| 审计 | 所有条目操作写 `logs/keyword_memory_audit.log`（agent 可读，含 trigger 来源） |

agent 侧五个动作：`list` / `add` / `del` / `lookup`（无关键词=返回全部全文）/ `fill`（改三态）。

## 待办清单（`tools/builtin/todo_list.py`，2026-10-19）

跨唤醒常驻的任务清单：daemon **每轮唤醒**把当前未完成任务原样注入任务头
（【待办清单】块，空清单也注入一行纪律提醒）——与关键词记忆的分工：那边按词触发
记稳定知识，这边无条件注入记"没做完的事"。条目存 `config/todo.json`（AI 自管）。

| 面 | 行为 |
|---|---|
| 登记 | 收到任务/要求 → `action=add task="…"`（≤200 字）；**同任务重复 add 不重复登记**（返回已有条目 id） |
| 移除 | 完成 → `action=del id=…`（或 `task="原文"`，id 支持 `"3"`/`"#3"`）；id 自增不复用 |
| 纪律 | PRIMARY_PROMPT 常驻：**finish_run 之前清单应为空**（做不完的留下并在最终汇报说明原因） |
| 上限 | `daemon.todo_list_max`（默认 20）条；超限 add 被拒（先 del 再加，或合并相近任务） |
| 注入 | 每轮唤醒无条件注入（含 scheduled 空跑）；开关 `daemon.todo_list_enabled`（热改） |
| 风险审查 | 同关键词记忆：玩家来源的增删请求先审风险（`player_request_risk_review`） |
| 审计 | 所有操作写 `logs/todo_audit.log`（agent 可读，含 trigger 来源） |

agent 侧三个动作：`list` / `add` / `del`。`/status` 回显 `todo_n`（当前条数）。

## 持续运行模式（daemon）
```
python run.py --daemon
```
守护进程五大机制（配置见 config.yaml `daemon:` 节）：

| 机制 | 行为 |
|---|---|
| 聊天监听 | 每 `poll_interval` 秒轮询桥接 /chat；**他人**发言 @提及 bot（`bot_names` 或 `mention_extra` 词）→ 立即唤醒。bot 自身 say 不触发（防自激）。★无 `<发言人>` 前缀的服务器广播（"X is now sleeping"/"已将X传送至…"）**不算提及** |
| 定时唤醒 | 每 `wake_interval` 秒到点检查**积压未读**；有积压才触发，无积压只记录。★噪声（清理广播等，见 `noise_patterns`）与 bot 自己的发言**不计入积压** |
| 周期审查 | 每 `review_every` 轮 / `review_token_budget` token 触发一次：规则检查（同操作连续3次失败/连续6次失败/生命<10/token超预算）＋ LLM 审查（`review_llm`）。**审查有权强制停止**（轮边界生效）。★LLM 审查必须带任务上下文，否则会把"只读任务"误判为空转 |
| 唤醒门控 | 所有唤醒仅在 agent **未运行**时生效：运行中提到→记入积压；定时/手动唤醒→跳过并计数 |
| 主动收工 | agent 调 `finish_run` 立即结束；另有完结判定（双向正则）+ **空转刹车**（推了"继续"却一个工具都没调 → 直接收尾）兜底 |

进程结构：主线程=轮询+调度；worker 线程=跑一次 Agent（复用 `core/runner.py`）；
控制API 线程=HTTP。状态（游标/积压/统计/热改配置）落盘 `logs/daemon_state.json`，重启不丢。

控制 API（默认 127.0.0.1:8770）：
```
GET  /             # Web UI 面板（状态/参数/实时行为流/授权申请，浏览器直接开）
GET  /status       # idle/running、积压数、待处理提及、待办数、统计
GET  /backlog      # 最近50条积压（带 noise 标记）
GET  /feed?n=80    # 实时行为流：思考/工具调用/审查/停止（读最新 behavior 日志）
GET  /config       # 读取全部运行参数（含实际生效值）
POST /config       # 热更新参数（立即生效+落盘，重启自动恢复）
POST /wake         # 手动唤醒（运行中→409，门控）
POST /stop         # 强制停止当前运行（轮边界生效）
POST /instruct     # 从控制台下发一条临时任务（等同人工唤醒）
GET  /approvals    # 待审核授权申请 + 审核历史
POST /approvals/grant  # 批准（{id} 或 {"id":"*"} 全批）
POST /approvals/deny   # 拒绝
GET  /warnings     # 警告队列（如"关键词记忆激活达上限"）
POST /warnings/clear   # 清空警告队列
```

UI 面板：左列运行参数热改；中栏实时行为流（**工具调用与深度思考默认折叠、输出默认展开**，
点击可展开且状态跨刷新保持）；右列状态/积压/🛡 授权申请（含审核历史）/⚠ 警告（注入超限等）。

日志：`logs/daemon.log`（过程）、`logs/daemon_state.json`（状态）、
`logs/behavior_*.log`（每轮行为流水）、`logs/session_*.jsonl`（会话全文）、
`logs/review_日期.jsonl`（每次审查记录）、`logs/approval_audit.log`（授权审计）、
`logs/llm_calls.jsonl` + `logs/usage.jsonl`（模型调用与 token 账本）。

## 测试

| 文件 | 覆盖 | 项数 |
|---|---|---|
| `tests/test_daemon.py` | 唤醒门控/积压/审查/热改 | 离线单测 |
| `tests/test_daemon_live.py` | 实网冒烟（stub 掉 LLM，零成本） | 15 |
| `tests/test_daemon_ui.py` | UI 接口：面板/行为流/参数热更新/持久化恢复 | 23 断言 |
| `tests/test_idle_and_noise.py` | 完结判定/空转刹车/噪声过滤/提及判定/黑名单 | 31 |
| `tests/test_memory_and_finish.py` | 分层记忆（note/status/pending/commit/归档）+ `finish_run` | 44 |
| `tests/test_approval_and_fileops.py` | 授权申请（指纹/单次有效/过期）+ 10 个文件工具 | 47 |
| `tests/test_review_context.py` | LLM 审查提示词必须带任务上下文 | 12 |
| `tests/test_permissions.py` | 权限表装配（file_mode / 角色收窄） | 15 |
| `tests/test_shell_exec.py` | shell_run 四道闸 | 21 |
| `tests/test_keyword_memory.py` | 关键词记忆工具五动作/触发注入三态/上限警告/玩家条目/风险审查 | 30 |
| `tests/test_todo_list.py` | 待办清单增删去重/每轮注入/空清单纪律行/上限/mtime 重载/控制台热改 | 20 |
| `tests/test_instance_lock.py` | 单实例锁 | — |

## 当前状态
- [x] 阶段 0/1：配置加载 + llm_client（含工具调用格式/重试/用量账本）+ 余量 adapter + `run.py --check`
- [x] 阶段 2：Agent 核心（tool loop）+ tool_registry（schema 校验）+ 内置工具
- [x] 阶段 2.5 上下文优化：结果压缩+存档（`core/context.py`）+ 历史归档 + `tool_result` 取回
- [x] 阶段 3：多 agent 协同（`spawn_subagent` + roles.yaml + 深度/并发限制）
- [x] 子 agent 权限矩阵 + 并行池（2026-10-18）：次级固定受限身份——游戏获取类放行、
  控制类需 `allowed_actions` 预授权（command/connect/agent_stop 不可预授权，自动升级操作者审批）；
  `spawn_parallel` 并发只读角色（parallel_safe 白名单，游戏动作永远主 agent 串行）；
  spawn 可指定 `name`（小写字母/下划线）
- [x] 持续运行模式（daemon）：五机制齐备，控制台可观测可干预
- [x] 工程加固（2026-10-18）：分层记忆 / 通用授权审批 / 完整文件能力 / 主动收工 / 噪声与黑名单过滤
- [x] 关键词记忆触发式激活（2026-10-19）：条目三态填充 + 注入上限警告 + 玩家专有条目 + 请求风险审查
- [x] 待办清单每轮唤醒注入（2026-10-19）：add/del 自管 + 同任务去重 + finish_run 前应为空纪律 + 空清单也注入
- [ ] 阶段 4-5 见《docs/开发流程.md》第三节

## 已验证事实（滚动更新）
- step（阶跃星辰）key 有效：33 个模型；对话模型用 `step-3.7-flash` / `step-5-preview`（**推理模型**，思考占 token 且在 `reasoning_content` 字段，max_tokens 须给足）；无公开余额端点
- DeepSeek 可用：`deepseek-flash` / `deepseek-v4-pro`（`deepseek-chat` 别名仍可用）；**余额请用 `balance_query` 现查**，别信文档里的历史数字
- **★ 桥接 /command 的 body 必须是 `{"commands": [动作]}`**——裸动作对象会被静默丢弃（queued:0）且不报错
- **★ 模组动作失败是字符串 `<动作>_err: …`**，不是 dict 的 `error` 字段——判定成败必须认这个前缀
- mc_act 结果判定：对比发送前后的 `last_action` 变化；**last_action 多为纯文本非 JSON**（解析失败原样返回，不再当错误）
- **★ 上下文优化实测**：真实工具结果压缩率 86%（39291→5462 字）；历史归档使长任务 token 从 128万(60轮) 降到可预期 ~15万级
- **★ 收工三件套效果**：同类"检查积压"唤醒从 25 轮/388,240 token 降到 **4-5 轮/5.5-7 万 token**（轮数 −84%、token −86%、耗时 −91%）
- 合成链路全通（木板/工作台/5件木制工具），工作台为 visualworkbench 模组台（合成格内容跨会话保留）
- 用量账本 `logs/usage.jsonl`；agent 会话日志 `logs/session_*.jsonl`；测试礼仪（聊天开始说明+结束统计）已入提示词

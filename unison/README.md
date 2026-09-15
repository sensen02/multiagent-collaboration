# Unison · 本地多模型协作工作台

这是《融合方案设计 v4》的首个可运行实现。所有 Agent 使用同一个执行循环；主模型可以直接改代码，也可以自主选择模型并递归委派。

## 启动

需要 Python 3.11+；三方文本合并使用 Git。不需要安装第三方 Python 运行依赖。

```bash
cd unison
python -m unison.server --port 8740
```

浏览器访问 http://127.0.0.1:8740 ，进入 **Unison Console**。也可运行项目中的 `start.sh`。服务仅监听本机。默认数据保存在当前目录 `.unison/`，可用 `--data /absolute/path` 指定位置。

**源码里不含任何 API Key。** 内置默认 provider 的凭据只从环境变量 `UNISON_BUILTIN_API_KEY` 读取（缺省为空），而导入配置时随配置一起提供的 key 优先于它。因此首次运行需要模型凭据时，在控制台顶栏「模型」导入 Codex TOML 或 DSH `settings.yaml` 的 provider 片段，或这样启动：

```bash
UNISON_BUILTIN_API_KEY=<your-key> python3 -m unison.server --port 8740
```

**默认工作区与数据目录分开**：新任务的工作区默认取"数据目录同级、名字带 `-workspace`"的那个目录（`.unison` → `.unison-workspace`），可用 `--workspace-root /absolute/path` 指定。这条规则来自一次真机发现：根工作区曾经就在数据目录里，于是**子任务工作副本会把该目录下所有历史产物一起复制过去**（上一个游戏 `sword-demo` 的中间态成了下一个任务 `ink-duel` 的基线），项目级技能与知识的作用域也跟着悬空。把工作区指到数据目录里仍然允许（在控制台直接填那个目录），但运行时会在运行记录上标 `workspace_warning` 并写一条 `WorkspaceInDataDir` 事件，不静默。

**默认不鉴权**（本机单人使用）：启动时会打印一行警告，此时同机任何进程都能调用全部端点，包括返回明文 API Key 的 `GET /api/models/credentials`。要启用令牌校验加 `--require-auth`，外部程序随后需带 `Authorization: Bearer <token>`（令牌见数据目录 `api_token`）。

## Console

`unison/web/` 是独立的控制台前端，无构建步骤、无第三方前端依赖，由同一个服务直接托管：

- 左侧任务列表：搜索、状态筛选、待回答问题数量；窄屏折叠为抽屉。
- 运行视图按标签组织：**对话 / 总览 / 协作图 / 轨迹 / 文件 / 知识 / 事件**。
  - **对话**：一个 Agent 一页。顶部标签条按所有权树列出**主调度与每个子 AGENT**（各带状态、目标摘要、待答问题数），页内是那个 Agent 的对话流——它当时真正看到与说出的消息：任务简报、收发的消息、每一轮回复，以及被翻成人话的工具调用（"执行命令 `npm test` → 退出码 0"、"委派子任务给 X：…"、"读取文件 a.py"），原始参数与结果收在折叠块里。**任何 Agent 调用 `human_ask`，控制台会自动跳到它的对话页**，问题就地在对话里回答。
  - 总览：任务分布、运行信息、根任务变更报告与最近事件。
  - 协作图：按所有权树展示委派关系，支持按事件游标回放历史协作过程（只重建任务与状态，不重跑工具）。
  - 轨迹：把模型回复、工具参数与结果、任务间消息和人工问答按时间串成可读的协作记录（事件视角，保留原始负载）。
  - 文件：当前差异（带行级高亮）、文件版本历史、内容对象查看、集成冲突与未扫描文件。
  - 知识：按项目检索共享知识，标记来源失效。
  - 事件：原始事件流，可按类型筛选并展开负载。
- 右侧抽屉：任务详情、等待条件、依赖、收件箱、报告、文件变更、有效上下文，以及继续 / 压缩 / 取消 / 补充信息。
- **对话栏**（对话页底部，常驻）：像正常对话一样给 Agent 补一句话，不必再走「修改目标」（后者是切换目标版本，会停止旧任务树）。它复用既有消息协议 `POST /api/message` → `deliver(delivery='wake')`，因此语义是确定的：消息先进收件箱，**当前这一轮（模型回复 + 它请求的工具批次）结束后被读到**；正在等待的任务立刻被唤醒；已交付但仍属当前版本的任务会被重新排队，在同一条对话里继续（不新建版本、不丢历史）；运行处于暂停时，发送会先恢复运行再投递。发给**当前选中的那个 Agent**；若它已被目标切换弃置（旧版本或 superseded），输入栏会改发给当前版本的主调度并说明原因——否则消息投进去没有任何东西会被唤醒。草稿与阅读位置由前端自己留住（SSE 每有新事件就重画整个主区）。
- 顶栏：连接状态、“技能”与“模型”两个入口；控制台只跟随系统深浅色，不提供手动主题切换。
- “技能目录”对话框：按来源根列出本会话可用技能（名称、摘要、适用场景、文件路径），可查看模型加载时逐字看到的内容，并在外部改完技能后一键重扫；坏技能文件会在诊断里列出原因。
- “模型与 Provider”对话框：按 provider 展示接口协议、地址、凭据状态与默认模型，列出该 provider 下所有模型及其上下文 / 输出 / 评分，并提供配置导入、按 provider 扫描模型与评分刷新。
- 视觉语言参考 DeepSeek Harness 的中性冷灰配色与细描边风格；信息架构与交互均为 Unison 自己实现。

## 人类介入：一个问题停下整个运行

`human_ask` 不是"提问的那个任务自己挂起"，而是**整个运行暂停**（`run.status="paused"`、`run.pause_reason="human_question"`），直到这句话被回答。

理由：人是这次协作唯一的输入源。一个问题悬着的时候，其他 Agent 继续烧 token 就是在"还没定的事"上往下做，而回答一到，它们手里的判断全作废——钱花了，返工也跑不掉。所以要停就一起停。

停的方式是**软停**，不是从中间掐断：

| 时刻 | 行为 |
|---|---|
| 提问那一刻 | 提问者转入 `waiting`；运行转入 `paused`；调度器自此不领取任何任务（`current()` 对暂停的运行返回假） |
| 已在跑的那一轮 | **照常做完**（它可能已经写了一半文件，中途掐断只会留下 `TOOL_OUTCOME_UNKNOWN`），随后回到队列；正在进行但还没执行工具的那一轮不写进历史，恢复后重跑 |
| 暂停期间 | 没有 Agent 开始新的一轮；等待中的任务也不会被唤醒（`wake_waiters` 同样看 `current()`） |
| 人类回答 | 该运行下**再没有未答问题**时自动回到 `active`；回答作为消息投给提问者，其余任务从队列继续 |
| 人工点「继续运行」 | 显式覆盖：即使还有问题没答也放行（问题留在列表里） |
| 提问者被取消 / 目标被切换 | 那个问题永远不会有人回答了，暂停随之解除，不会拖着整个运行一起停 |

因此控制台的对话视图会**自动跳到提问的 Agent**，并在页面顶部（以及该 Agent 的对话里）给出回答框；回答后无需再做任何操作。

## 模型接口：统一的 Provider + Models

模型配置只有一份真相源，形状参考 DSH 的 `llm-pi-ai` / `llm-deepseek` 配置：

```text
Provider = { id, displayName, api, baseURL, apiKeyEnv, headers, defaultContextWindow, defaultMaxTokens }
Model    = { id, name, description, contextWindow, maxTokens, inputModalities, role }
```

- `api` 决定唯一的执行路径：`openai-completions` → `/chat/completions`，`openai-responses` → `/responses`。新增一种 wire api 只需在 `unison/providers.py` 登记一次。
- 运行时的扁平 model 记录（`Provider:模型名`）由 `unison/providers.py` 从 provider 投影而来，不再由各处自行拼装。
- 同一 provider 下的模型共享地址、协议与请求头；DSH 片段里的 `defaultContextWindow` / `defaultMaxTokens` 作为缺省值，模型自身字段优先。
- 调用失败统一为 `ModelError`，带稳定 `code`：`AUTH`、`QUOTA`、`RATE_LIMIT`、`INVALID_REQUEST`、`CONTEXT_WINDOW_EXCEEDED`、`TIMEOUT`、`TRANSPORT`、`SERVER`、`EMPTY_RESPONSE`、`MALFORMED_RESPONSE`、`MISSING_CREDENTIAL`、`NO_ADAPTER`；任务错误信息里会带上 code。
- `GET /api/providers` 返回 provider 目录、未归入 provider 的模型（例如离线演示）和当前默认模型；`POST /api/providers/import`（等价于旧的 `/api/config/import`）导入 DSH YAML 或 Codex TOML；`POST /api/providers/scan` 用已保存的 provider 事实（含凭据）重新扫描 `/models` 补齐新模型；`POST /api/providers/verify` 用最小请求逐个验证模型是否真的可调用；`POST /api/providers/default` 设定主调度默认模型。
- **出站请求统一带稳定 `User-Agent`。** 部分网关（含 aaa.bi）对库默认 UA（`Python-urllib/...`）在 `/models` 与 `/responses` 上都直接返回 403，这与凭据是否正确无关。
### 防过热与智能预算（物理额度，不做计费）

真实事故：同一秒内向 `gpt-5.6-sol` 发出两个 25–28 KB 的请求 → 上游 429 + 网关 524，两个任务当场失败。
根因不是提示词，而是运行时缺三样东西：**每桶串行化、可重试的错误分流、用量记账**。

| 机制 | 位置 | 说明 |
|---|---|---|
| **每桶并发闸门** | `unison/limiter.py` | 桶 = provider + 模型；`concurrency` 限制同一时刻在途请求数，**未声明时默认 1**（这一条单独就能消除上面的事故）。超额**排队**而不是失败 |
| **TPM/RPM 额度桶** | 同上 | 滑动窗口累计真实 `usage`；窗口满则等待释放。`tpm`/`rpm` 未声明即不限 |
| **退避重试** | `models.invoke` | `429`/`524`/`520`/`522`/`5xx`/超时按指数退避 + 抖动重试（默认 2 次，尊重 `Retry-After`，上限 120s）；`AUTH`/`MODEL_NOT_FOUND` 等永久错误**不重试** |
| **用量记账** | `model_usage` 记录 | 每次调用落 `tokens/prompt/completion/排队时长/重试次数`，按 run 与 task 聚合；报告与 `usage_report` 工具可查 |
| **模型高低搭配** | `models.downgrade_target` | provider 可声明 `tiers: {heavy: [...], light: [...]}`：子任务**未指定模型**且父为 heavy 时自动降到 light 档，并写 `ModelDowngraded` 事件与 `model_downgraded_from` |

配置形状（都向后兼容，缺省即采取保守默认）：

```python
provider['capacity'] = {'concurrency': 1, 'tpm': 200000, 'rpm': 60, 'queue_timeout': 600, 'window': 60}
provider['tiers']    = {'heavy': ['gpt-5.6-sol'], 'light': ['gpt-5.6-luna', 'gpt-5.6-terra']}
model['capacity']    = {'concurrency': 2}     # 模型级覆盖 provider 默认
model['tier']        = 'heavy'                # 也可直接标在模型上
```

- `GET /api/load`：每桶的在途数、峰值、窗口 tokens/请求数、排队与重试计数、当前额度与来源。
- `usage_report` 工具：模型自己判断"还能不能再委派一轮"时用它，而不是猜。
- **提示词不是刹车**：提示词只影响模型**选择**哪个模型；并发、额度、重试、用量这些发生在 HTTP 层，
  必须由运行时强制。文档与工具的措辞负责解释规则，运行时不依赖模型自觉。

受控对照实验（`tests/test_heat_experiment.py`，假网关对并发请求返回 524）：

| 场景 | 峰值在途 | 上游并发拒绝 | 请求总数 | 结果 |
|---|---|---|---|---|
| `concurrency=4`（改动前） | ≥2 | >0 | >4（重试） | 部分请求重试耗尽后失败 |
| `concurrency=1`（新默认） | **1** | **0** | **4**（无重试） | 全部成功 |

这张表说明两个机制各管一件事：**重试保证任务不死，闸门保证不去打上游**。

### 模型可用性怎么维护（不靠反复烧 token）

可用性曾经只有一条来源：人点「验证可用性」，而那会把 provider 下**每个模型**都真实调一次；
而且结论**没有时间维度**——一次瞬时 400 会让模型永久显示"不可用"，之后它即使被真实调用成功也不会改回来。

现在按"**真实使用就是主证据**"重做：

| 信号 | 成本 | 作用 |
|---|---|---|
| **真实模型调用**（`Models.invoke()` 成功/失败） | 0（本来就要付） | 成功 → `ok (source=call)` 顺延 24 小时；失败按错误码分流 |
| **`/models` 扫描** | 0 模型 token | 某个模型重新出现在广告里时，清掉"不存在/未广告"类的旧结论 |
| **主动探测** | 1 次/模型 | 只补"没有近期证据"的空档；后台 `model-health` 作业每 6 小时最多打 **3** 个 |

失败分流（决定"重试有没有用"）：

| 错误码 | 处置 |
|---|---|
| `AUTH` / `MISSING_CREDENTIAL` / `NO_ADAPTER` | 需要人改配置；`blocked`，**不重试**，改完自动清除 |
| `RATE_LIMIT` / `QUOTA` / `SERVER` / `TIMEOUT` / `TRANSPORT` | 瞬时；单次不改变已有结论，连续 3 次才降级 |
| `INVALID_REQUEST` / `CONTEXT_WINDOW_EXCEEDED` / `EMPTY_RESPONSE` | 与"这段时间能不能用"无关；只记错误 |

因此：

- `GET /api/models/health` 给出每个模型的状态、证据来源与过期时间；控制台显示为
  **可用（真实调用，3 小时前）/ 待复验 / 需处理（AUTH，附该做什么）/ 未验证**四态。
- 「复验过期的模型」按钮只打过期或未验证的模型，并按最久未验证优先排序——刚被真实调用过的模型**一次都不会打**。
- 一次最小探测的失败**不会推翻**未过期的真实调用成功证据，只写一条 `ModelHealthDisagreement` 供人判断。

- **模型目录即声明清单。** Codex `model_catalog_json` 里的每个模型都会被纳入 provider，即使 `/models` 没有广告它：控制台会标出「未广告」，并提供可用性验证结论。
- 扫描只补齐模型，不改写声明或已有的默认模型；上下文 / 输出上限的取值顺序是「已存记录 → 目录元数据 → 声明值 → provider 缺省 → 保守默认」。
- Windows 风格路径在任意平台按同等语义解析：`%USERPROFILE%` 等映射到用户主目录，反斜杠视为路径分隔符，因此 `%userprofile%\.codex\codex-models.json` 在 Linux 上也能读到目录元数据。

内置默认 provider（`OpenAI` → `https://aaa.bi`，`api: openai-responses`，模型 `gpt-5.6-sol`）在启动时自动导入，并在“模型与 Provider”对话框里明确展示，不会被隐藏或改写；它的凭据只来自 `UNISON_BUILTIN_API_KEY`，为空时导入仍然发生，但 `/models` 扫描会因为缺凭据而失败并在控制台的配置提示里显示原因。`gpt-6-astra` 已按目录声明加入并可选，但当前这把 Key 的 group 调用它返回 `404 model_not_found`；该 group 支持后，在控制台点“验证可用性”确认，再点“设为默认”即可切换。内置配置解析失败时不再静默：原因会显示在控制台的配置提示里。

## 开始真实任务

1. 打开顶栏“模型”，粘贴 Codex TOML 或 DSH `settings.yaml` 中的 provider 片段并导入；Unison 自动识别格式与 Provider。
2. Codex 格式读取 `model_provider` 与对应的 `[model_providers.<名称>]`；DSH 格式读取 `agent-default-model` 与 `llm-pi-ai.providers.<route>`。Unison 会请求 Provider 的 `GET /models` 自动补齐全部模型；兼容标准 `data`、顶层 `models` 和顶层 `list` 结构。
3. 点击“刷新评分”可从 [LLM Stats](https://llm-stats.com/) 公开排行榜匹配综合、Coding、Reasoning 和 Agent 评分；结果缓存 24 小时，抓取或匹配失败不会影响模型使用。
4. 创建任务时模型按综合评分降序排列；有评分的优先，无评分显示“未匹配”而不是记为零。
5. 创建任务，填写已有项目目录和目标。根 Agent 直接操作该目录；子任务使用独立工作副本。模型可调用集成工具带回子任务成果。
6. 待回答问题直接显示在工作台。回答后相关任务继续；普通进度不会触发主模型推理。

内置适配器支持标准 Chat Completions 和 OpenAI Responses 工具调用协议。Responses 适配器会在边界转换 `function_call` / `function_call_output`，运行时内部协议保持统一。`disable_response_storage = true` 会向供应商发送 `store: false`，不会关闭 Unison 本地事件与恢复记录。

Codex TOML 导入会创建 `Provider:模型名` 形式的模型记录；`model` 作为新任务默认选择，`review_model` 会写入 Run 上下文，主模型需要独立审查时可用它创建审查子任务。`[features] goals = true` 随 Run 保存；`network_access` 和 WSL 确认字段当前只保留并展示，不改变运行环境。`model_catalog_json` 支持 `%ENV%`、`$ENV` 和 `~` 路径展开；目录不存在时仍导入模型并显示目录错误，存在时会读取上下文窗口、最大输出和说明。

DSH 并不读取 Codex 的 `~/.codex/config.toml`。DSH 标准用户配置来自 `$DSH_HOME/settings.yaml`：自定义路由位于 `llm-pi-ai.providers.<route>`，使用 camelCase 字段 `displayName`、`apiKeyEnv`、`baseURL`，协议字段是 `api: openai-responses` 或 `api: openai-completions`。Unison 的 DSH 导入器会把它们分别转换为内部 Responses / Chat Completions 适配器，并尊重 `agent-default-model`；当前不支持的 `anthropic-messages` 会明确报错而不是静默降级。

首次启动的空数据目录会自动导入内置默认 provider；控制台不再提供离线演示入口（`POST /api/demo` 仍保留给命令行与测试使用，它是确定性脚本，**不是模型推理**）。

## 已实现

- SQLite 持久任务、收件箱与追加事件；一次执行一个模型步骤，再按队列公平调度。
- 主/子统一工具注册表和可替换模型适配器，递归创建子任务。
- any/all 事件等待、消息合批、按 message_id 去重、局部等待环检测、父任务释放执行槽；收件箱超过单批上限时自动续批，未读消息不会被完成状态掩盖。
  **时限完全由模型声明**：`tasks_yield` 不写 `timeout_seconds`/`max_wait_seconds` 就是"只被订阅的事件唤醒"——运行时不补兜底上限、不静默顺延窗口（设计文档 §5.2：无事件时不调用模型轮询）。运行时只把"这个等待的边界在哪"作为一条 `observation` 说明出来。
- **纯文本不是交付**：模型一轮里若没有任何工具调用，就不算表态。此时若收件箱还有未读消息（并发提问常常正好落在"收件箱已清空"到"这一轮结束"之间的推理窗口里）、或自己发出的请求还没人回答、或有子任务未结束，这一轮会被**退回队列**并附上该用哪个工具的指引（`TextRoundDeferred`），任务不会被判负——这是持续服务型角色（出题人、主持人、答疑者）在并发提问下反复暴毙的根因修法。**退回次数不设上限**：这里一度有 `MAX_TEXT_DEFERRALS=2`，连续第 3 轮就抛错判负——那是用计数器代替"这活干完没有"的判断，已移除。
- 需求版本切换：取消旧子树、拒绝旧执行代次、过时问题失效、显式采纳旧成果。
- 人类问答（**一问即暂停整个运行**，见上一节）、插话、显式且幂等的暂停/继续、失败恢复；中断的工具调用不自动重放。
- 本地协作树、任务详情、实际消息/工具记录、事件时间轴回放；SSE 刷新期间到达的新事件会在当前刷新后继续拉取。
- 读写/精确编辑/执行 shell、文件差异、内容寻址版本、归档及恢复到新目录。
- 子工作区三方合并；单进程内按目标工作区串行，提交前检查目标清单未变化并记录 generation；冲突保留两侧内容并产生普通冲突记录。
- 任务报告包含验证状态（已声明验证、失败、未验证、未知）、扫描省略文件、影响和未知项，同时兼容旧的 `verified_claim` 字段。
- SQLite 使用 `PRAGMA user_version` 执行有序 schema 迁移。
- Provider 模型自动发现：用一份连接配置扫描 `/models` 并批量注册模型；导入后也自动扫描，失败时保留显式配置并返回诊断。发现只提供模型 ID，已有的上下文 / 输出上限不会被覆盖。
- 模型性能匹配：低频读取 LLM Stats 公开排行榜，保存来源与详情链接，24 小时缓存，保守拒绝歧义别名；UI 明确标注 “Data by LLM Stats”。
- 模型可用性维护：真实调用回写证据（24 小时 TTL）、失败按码分流、`/models` 扫描旁证、后台限量复验（每次 ≤3 个），`GET /api/models/health` 暴露状态与证据来源。
- 共享知识按项目检索、文件哈希校验、依赖失效；显式 reuse_key 合并同版本重复理解任务。
- 技能（skill）目录：多根发现与同名竞争、frontmatter 校验与诊断、会话目录注入、`skill` 工具按需加载为 `<skill_content>`；声明 `invocation` 契约的技能可**走端口调用**（单次 + 批量，作业持久化与轮询），`skills_list` 暴露调用契约；内置 image-generation 技能可匿名出图、gpu 技能用于探测本机 GPU 与推理栈。
- 有效上下文 compact：模型调用 `context_compact` 后**在本轮工具批次结束时立即生效**（不需要批准、不占额外轮次），同一次调度用压缩后的上下文继续推理；完整日志原文保留，近期工具协议完整，压缩过程中到达的消息不会丢失。
- 模型凭据是可用能力：`models_credentials` 可读取明文 Key 与接口协议，`workspace_shell` 注入 `UNISON_MODEL_*` 环境变量，模型可以在自己写的代码里直接调用模型接口，并在事件里留下 `CredentialsExposed`。
- HTTP API 服务化：外部程序用令牌调用同一组端点（`/api/runs`、`/api/wait`、`/api/task`、`/api/requests`、`/api/request`、`/api/models/credentials`），契约见 [docs/API.md](docs/API.md)。
- 每个请求可重建：`request` 记录保存 provider/模型/协议、系统提示词与工具 schema 的 sha256 与对象引用、输入快照与事件游标；compact 用遮蔽语义并支持把历史还原到压缩之前；中断的工具调用区分 `TOOL_NOT_STARTED` 与 `TOOL_OUTCOME_UNKNOWN`。
- 统一的维护层：所有周期性/按需维护（工作区扫描、评分刷新、对象回收、归档保留、知识整理）注册在同一个表里，由单一调度器串行执行并持久记录状态；`GET /api/maintenance` 可查看，`POST /api/maintenance/run` 可手动触发。破坏性作业默认只预演。
- 没有预算计算、价格表、通知中心、模型权限表或工具审批。**额度闸门不是计费**：它管并发/TPM/RPM 与自身负载，不换算货币成本。

## 数据与行为边界

根模型直接修改选定目录，首次基线包含原有未提交修改。所有模型同等信任；子工作区分开是协作分支机制，不是安全隔离。

文件观察采用两秒扫描和工具边界对账。默认跳过 `.git`、`.unison`、依赖/虚拟环境目录、符号链接及单文件超过 16 MiB 的内容；每个任务返回 `omitted_files`，因此不承诺记录所有瞬时写入或超大文件。扫描期间不调用模型。

归档保存文件对象、事件、报告和相关知识。恢复始终创建新目录，不覆盖当前项目。核心需求改变不会自动回滚已经写入根目录的内容；新根 Agent 获得变更说明后决定如何调整。

模型调用目前使用非流式 HTTP：界面实时显示任务和工具阶段，不能逐 token 显示正在生成的回答。远程 HTTP 调用的取消是尽力而为；已取消调用可能仍在服务端计算。收到的迟到结果不能再驱动旧任务写文件。

compact 的容量估算使用保守的 UTF-8 字节上界，不是供应商精确 tokenizer。极长旧历史使用可检索的原文检查点；完整原文始终保留。压缩会改变部分前缀，缓存效果以服务商实际 usage 为准。主动 compact 在工具批次边界执行：同一轮里若以 `tasks_complete` / `tasks_yield` / `human_ask` 结束，本次请求不生效（此时不会有上下文被改写）。

压缩采用**遮蔽**而不是删除：checkpoint 记录 `shadowed_range`、被遮蔽区间的 `region_ref` 与压缩前的 `history_ref`，因此任何一次请求的输入都能还原出"这次压缩之前"的历史（`GET /api/request?restore=1`）。多次压缩可以逐层还原到最初原文。

“已完成”是任务提交状态，不等于测试通过；报告的验证声明来自模型，应查看实际测试输出。报告另附 shell 执行记录，包含命令、退出码、输出对象和执行结束时的文件清单引用；普通命令成功不自动等于测试通过。文本可合并不代表不存在跨文件语义冲突。

每次模型调用都会留下一条**请求信封**记录（`request`），里面是当时的 provider/模型/协议、系统提示词与其 sha256、工具 schema 与其 sha256、输入快照引用、消息数与输入字节数、以及记录时的全局事件游标 `seq`。系统提示词与工具 schema 按内容寻址去重，所以逐条记录的成本很低。因此这三个问题可以逐字回答，而不是靠模型的自我陈述：

| 问题 | 用哪条记录回答 |
|---|---|
| 通过的是哪一次测试？ | 报告里的 `execution_records`（命令、退出码、输出对象）＋ `verification_status`（模型声明，运行时不给评级） |
| 测试之后代码有没有再变？ | `execution_records[].manifest_ref` 与 `request_headers[].seq`、`/api/events?after=` 对齐 |
| 当时的系统提示词/工具集是什么？ | `GET /api/requests?task_id=` → `GET /api/request?id=&restore=1` 逐字重建 |

中断遗留的工具调用现在区分两种语义：调用表里没有记录 → `TOOL_NOT_STARTED`（工具未开始，可直接重试）；已记录但无结果 → `TOOL_OUTCOME_UNKNOWN`（结果未知，有副作用时不得盲目重放）。两者都会写 `ToolRepairRecorded` 事件。

当前是单进程本机运行时。队列状态与事件同事务提交，但任意外部命令无法承诺恰好执行一次；重启遇到在途工具时标记需要检查，用户或 Agent 核对后继续。不实现分布式调度和自动生成专用冲突调解树。

## 凭据与外部调用

凭据是模型的能力，不是隐藏配置：

- `models_credentials` 工具按需返回明文 `api_key`、`base_url`、wire api 与请求头；每次读取写一条 `CredentialsExposed` 事件，可在事件流里审计。
- `workspace_shell` 启动的子进程带有当前模型信息：`UNISON_MODEL_ID`、`UNISON_MODEL`、`UNISON_MODEL_BASE_URL`、`UNISON_MODEL_API`、`UNISON_MODEL_API_KEY`，以及 `key_env` 指定的同名变量（例如 `DEEPSEEK_API_KEY`）。凭据只存在于子进程环境，不写入工作区文件，因此不会进入文件清单、差异、报告或归档。
- 外部程序使用同一组 HTTP 端点：`POST /api/runs` 提交目标、`GET /api/wait` 阻塞等结果、`GET /api/task` 取报告、`GET /api/models/credentials` 取明文凭据。浏览器控制台走同源请求免令牌；脚本必须带 `Authorization: Bearer <token>`（令牌见数据目录 `api_token`，或用 `--api-token` / `UNISON_TOKEN` 指定）。
- 完整端点、错误码与端到端示例见 [docs/API.md](docs/API.md)。

边界：明文 Key 会进入该任务的模型上下文与其执行记录的可见范围；报告不写凭据，但子进程环境对外部进程可见。同源判定基于 `Origin`/`Host`，只承诺本机单用户场景。需要更严隔离时应自行加反向代理或改部署方式。

## 技能（Skills）

技能是**可复用的任务专用指令**：一段放进目录的 Markdown，加上它自己的脚本与参考资料。
模型在系统提示里只看到**名称与摘要**，任务匹配时才用 `skill` 工具加载全文——
这样几十个技能的正文不会常驻上下文，技能也不会随会话变长而被压缩丢掉。

```text
<根>/skills/<name>/SKILL.md    # 目录包，可带 scripts/、references/、assets/
<根>/<name>.md                 # 扁平技能（name 必须等于文件名）
```

`SKILL.md` 以 YAML frontmatter 开头，`name`（kebab-case，必须与目录名一致）与 `description` 必填：

```markdown
---
name: image-generation
description: 用提示词生成图像并落盘到当前工作区。
when-to-use: 任务需要配图、海报、图标或占位素材时。
metadata:
  note: 只生成文件，不改代码。
---

# 正文
模型的完整指令写在这里；相对路径以技能目录（base directory）为基准解析。
```

- 扫描根按优先级排列：**任务工作区** `.dsh/skills`、`.agents/skills` → 自定义根 → 用户 `~/.dsh/skills`、`~/.agents/skills` → **项目根**及其祖先 → 随程序分发的内置包。同名技能由更靠前的根胜出，落选者写进诊断而不是静默消失。
- 子任务工作副本位于数据目录内，因此任务记录里**显式保存 `project_root`**：副本里的技能根能覆盖项目级技能，而项目自己的技能目录也仍然可见。项目根缺失时回退到最近的 `.git` 祖先。
- 正文与资源始终从磁盘在读时读取，**改技能不需要重启**；`skill-catalog` 维护作业（15 分钟）负责发现新目录、记录目录指纹变化。技能文件的 frontmatter 写错会进诊断。
- `disable-model-invocation: true` 的技能只出现在控制台，模型无法加载。
- 内置技能随程序分发，位于 `unison/skills/`，可被项目或用户技能同名覆盖。

控制台顶栏“技能”按来源列出全部技能，点“查看”能看到模型实际收到的那段 `<skill_content>`；
带调用契约的技能会标出“端口可调”与批量上限。
HTTP 侧对应 `GET /api/skills`、`GET /api/skill`、`POST /api/skills/reload`，见 [docs/API.md](docs/API.md)。

### 端口调用：技能也能像模型一样被调用

技能可以声明**调用契约**，从而像模型调用一样走端口——提交请求、拿 `job`、轮询结果：

```yaml
invocation:
  kind: bridge          # 目前只有 bridge：用独立进程执行
  bridge: scripts/generate.py   # 相对技能目录；必须在技能目录内
  batch: true           # 是否接受一次性多份输入（上限 8）
  timeout: 300          # 硬上限，超过即终止并记为失败
```

桥接协议刻意极小，任何语言都能实现：

```text
stdin  ← {"skill": name, "workspace": path, "base": skill_dir, "args": {...}}   # batch 时 args 为数组
stdout → {"ok": true, "result": {...}}                                          # 或 {"ok": false, "error": "..."}
```

```bash
# 单次
curl -s -X POST http://127.0.0.1:8740/api/skills/call -H "Authorization: Bearer <token>" \
  -H 'Content-Type: application/json' \
  -d '{"skill":"image-generation","workspace":"'"$PWD"'","args":{"prompt":"a red fox in snow"}}'
# 批量：批量的 prompt 就是本技能的 args 列表
curl -s -X POST http://127.0.0.1:8740/api/skills/call -H "Authorization: Bearer <token>" \
  -H 'Content-Type: application/json' \
  -d '{"skill":"image-generation","workspace":"'"$PWD"'","batch":[{"prompt":"fox"},{"prompt":"lake"}]}'
# 轮询（单次与批量同一个端点）
curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8740/api/skill/jobs/<job_id>
```

为什么值得多这一层：`workspace_shell` 是同步阻塞的，一张图 1–3 秒、十张就会占住整个任务循环；
走端口则是"同时提交、各自完成"。模型侧不需要记这些细节——`skills_list` 给出每个技能是否
`invocable`、批量上限与两个端点地址；`skill` 工具在带 `batch` 时直接代为提交，一次动作里
同时拿到指令与调用入口。

边界与取舍：

- 桥接脚本在**独立进程**里跑，因此超时可强制终止、输出不会污染运行时、技能可用任意语言写；
  代价是它继承本机权限——技能与本机代码同等可信（与本项目"所有模型同等信任"的取向一致）。
- 执行失败（桥接报错、退出码非零、输出不是 JSON、超时）是**作业状态 `failed`**，不是 HTTP 错误；
  调用参数不合法才是 `400`。作业状态持久化，重启后仍可查；上次中断的作业标记为 `interrupted` 且**不重放**。
- 并发上限 4 个作业、批量上限 8 份，都是**显式拒绝**而不是静默排队或截断。
- 桥接的产物路径相对 `workspace` 解析，且**拒绝写到工作区之外**——技能目录是随程序分发的源码，
  往里写既不会进任务文件清单，也会污染安装。

### 内置技能：gpu

**先探测再动手**：`scripts/probe.py` 只读地报告显卡与后端（Vulkan / ROCm / CUDA）、资产根里的权重与二进制、独立 venv 的 torch 构建、显存与磁盘余量，并给出结论式判断（"现在能用什么"），而不是丢一堆字段。

它同时提供两类可复核事实（都是踩出来的）：

- **权重结构**：`scripts/weights.py` 只读 safetensors 文件头，报告 tensor 分组、LoRA tensor 占比、缺失部件等结构事实，不输出 `role` / `verdict` 或“能填哪个参数”的角色判词。本机生成后端仍会依据缺失 unet+vae+clip 的结构事实拒绝不完整的主模型。真事故：清单把两个 LoRA 标成了 `sd15-checkpoint`，照清单读就会把 LoRA 喂给 `-m`，换来 200+ 行 `... not in model metadata` 和一堆废图。
- **画面统计量**：`scripts/imagecheck.py` 测量主色及占比、抽样色数、局部对比度、8×8 块均值标准差、重复列占比；不输出 `degenerate` / `verdict`，不自动判定或排除纯色/竖纹。无法测量时报告 `error`，而不是内容质量结论。真事故：命令退出码 0、文件是 PNG、尺寸也对，**但这三条证明不了它不是废图**。

```bash
python3 unison/skills/gpu/scripts/probe.py            # 人读报告
python3 unison/skills/gpu/scripts/probe.py --json     # 机器可读
python3 unison/skills/gpu/scripts/probe.py --weights  # 逐个权重报告文件头结构
python3 unison/skills/gpu/scripts/probe.py --check-output out.png
python3 unison/skills/gpu/scripts/weights.py ~/.cache/unison-gpu/models/sd15/*.safetensors
echo '{"skill":"gpu","workspace":"'"$PWD"'","args":{}}' | python3 unison/skills/gpu/scripts/bridge.py   # 端口调用
```

**资产根**：权重、数据集这类长生命周期产物放在主目录之外的 `~/.cache/unison-gpu`（`UNISON_ASSETS_ROOT` 可覆盖），**不要放数据目录**——`.unison` 是归档来源，且 `objects/` 会被 mark-and-sweep 回收（没有保留期）。技能里把这条写成硬规则，因为把 2G 权重下进工作副本或 `.unison` 是最容易犯且最难发现的错误。

技能只探测，不安装、不下载：ROCm / PyTorch 这类安装动辄数 GB 且改系统，必须先报告"缺什么、要多大空间"再等人确认。

### 关于"技能声明与实现是否一致"

这一度是一整套机制：`unison/skillcheck.py` + `skill-validator` 技能，把 frontmatter 里**声明**的
能力与执行体 `--capabilities` **自报**的能力做对账，产出六类诊断，还顺带用四条正则去抓
文档/脚本/测试里的端口号是否一致、证据文件能不能自证。

**现已整体移除。** 理由是它属于典型的"边界维护"：为了防住一次真实事故
（摘要写了"本机 GPU 后端"而脚本没有这一档），运行时自建了一套官僚机制，用关键词匹配
（`本机|本地|离线|local`）和端口正则去推断"这个技能可不可信"。那是运行时替模型做判断，
而且判据本身脆弱（正则命中不等于能力存在）。

现在只保留**测量**：执行体仍可用 `--capabilities` 自报它实现了什么，技能脚本仍然把
真实数字（权重文件头结构、画面统计量）写在结果里，**但运行时不下判词**。
要不要信一个技能，由模型读正文与实际情况判断。

### 内置技能：image-generation（三个后端）

| 后端 | 凭据 | 说明 |
|---|---|---|
| `pollinations` | 不需要，匿名可用 | 默认后端；一条 GET 即出图。上游匿名档开放的模型会变，`--list-models` 查当前可用项 |
| `huggingface` | 需要 HF token | 走 `router.huggingface.co/hf-inference`；模型 ID 必须形如 `owner/name` |
| `local` | 不需要（本机） | 走 `sd-cli` + 磁盘权重；**离线、可复现、不外传**。开跑前读权重结构、跑完记录画面统计量、并落溯源；不自动判断内容质量 |

```bash
# 云端：模型实际执行的就是这条命令（<base> 是技能返回的 base directory）
python3 "<base>/scripts/generate.py" --prompt "a red fox sleeping in snow, soft morning light" \
  --out ./images/fox.jpg --width 1024 --height 1024 --seed 42
# {"ok":true,"path":"./images/fox.jpg","bytes":26096,"format":"jpeg","width":1024,"height":1024,...}

# 本机：--model 必须是完整 checkpoint；LoRA 走 --lora-dir + prompt 里的 <lora:…>
python3 "<base>/scripts/generate.py" --backend local \
  --model ~/.cache/unison-gpu/models/sd15/GuoFeng3.4.safetensors \
  --prompt "guofeng style, a sword on silk" --out ./assets/sword.png \
  --width 384 --height 512 --steps 6 --seed 74000
```

本机后端的三条实测结论：**`-m` 在完整 checkpoint 上才成功**（文件头里读不到完整 unet+vae+clip 时**直接拒绝执行**，一张都不生成——因为实测那是 200+ 行 `not in model metadata` 和一整批废图）；**漏 `--lora-dir` 不报错、只是风格没生效**（结果里带 `lora_applied` 与 `warning`）；**"命令成功 + PNG + 尺寸对"不等于不是废图**，所以每张生成后都量一遍画面统计量（主色占比、色域数、对比度、8×8 块标准差、重复列占比）写进结果——**但不下判词**，这张图能不能用由模型判断。每批生成落一份 `generation.json`：完整命令行 + 权重 sha256 + 输出 sha256 + 统计量——**元数据由执行体生成，不靠手写**。

本机后端与两个联网后端**在同一个执行体里**（`--backend` 可选 `pollinations` / `huggingface` / `local`），实现只有一份。曾有一个 `local-image-gen` 技能做"薄适配器"，同一个实现在两个技能名下各暴露一次——已删除，名字不是第二份能力。

脚本只用 Python 标准库（本机后端也只多一个技能内的兄弟模块），输出一行 JSON：真实格式与像素尺寸来自**图像头解析**，不是把请求参数回显；失败时同样是一行 JSON 带 `error`，退出码非零。生成的图像就是普通工作区文件，因此自动进入文件版本、差异、报告与归档，也能用 `workspace_integrate` 带回父任务。

边界要说清：**匿名档可用模型由上游决定**（当前 `/image/models` 只返回 `sana`，传其它名字可能失败），`--list-models` 查当前可用项；本机后端的 `check` 只记录测量事实，不含 `degenerate` / `verdict`，也不因统计量高低而拒绝交付。`ok: true` 只表示执行成功，**不等于**人物画对了——统计量不能替代看图，美术结论必须由能看图的人/模型结合需求给出。报告里的 `verified` 只能写你真正核对过的事实。

## 插件

使用 `--plugin your_python_module` 加载提供 `setup(runtime)` 的模块。插件可注册工具和模型适配器，主/子模型共用注册表：

```python
from unison.tools import tool

def setup(runtime):
    async def project_check(task, args):
        return {"workspace": task["workspace"]}
    runtime.register_tool(tool("project_check", "返回当前项目位置"), project_check)
```

模型适配器是 `async adapter(config, messages, tools) -> (assistant_message, usage)`，注册到 `runtime.models.adapters`。本实现借鉴 DSH 的能力组合思路，采用独立 Python 运行循环；没有把 DSH 与另一套 Agent Loop 叠加，也不宣称已兼容 DSH 原生插件。

## 验证

```bash
python3 tests/run_all.py                    # 全部套件（当前 302 个用例）
python3 tests/run_all.py runtime            # 只跑文件名含 runtime 的
for f in unison/web/js/*.js; do node --check "$f"; done
```

**必须用 `tests/run_all.py`，不要用 `unittest discover`。** `discover` 在这里有两个坑：
`tests/` 不是可导入包，而且它只按模式收集——`tests/test_runtime.py` 与 `tests/test_models.py`
长期漏了 `unittest.main()`，于是 `python3 tests/test_runtime.py` 会**静默退出 0**，
一个用例都没跑（运行时最核心的 72 个用例就这样一直没被执行过；`__main__` 块已补上）。
`run_all.py` 逐个文件加载并打印每个文件的用例数，漏跑会立刻显形；
空文件也会被标成 `[EMPTY]` 并以非零退出码结束——"没收集到用例"是缺陷，不是通过。

技能相关测试在 `tests/test_skills.py`（frontmatter、多根竞争、目录注入、`skill` 工具）、`tests/test_image_skill.py`（内置技能脚本的格式识别与失败路径，用假 HTTP 响应，不发真实请求）与 `tests/test_skill_capabilities.py`（本机后端用假 `sd-cli` 跑，不碰真显卡：权重文件头结构读得对不对、LoRA 没生效必须给 warning、低结构输出**仍照常交付并带上统计量**、溯源必须含完整命令行与权重 sha256）。维护层见 `unison/maintenance.py`：作业只声明 `id/interval/dry_run/destructive` 与 `run()`，注册表负责依赖注入、到期判定（失败按指数退避）、状态持久化与事件记录；高频作业保持静默，破坏性作业永不自动执行。

## 维护层

所有"隔一段时间该做的事"都在一处，避免散落在循环里各写各的定时器：

| 作业 | 间隔 | 性质 | 做什么 |
|---|---|---|---|
| `workspace-scan` | 2 秒 | 只读 | 对可观察任务的当前工作区做文件与权限对账（用 stat 指纹跳过未变文件的重哈希） |
| `skill-catalog` | 15 分钟 | 只读 | 重扫技能目录、记录目录指纹变化 |
| `model-health` | 6 小时 | 只读（支持预演，默认真跑） | 限量复验过期/未验证的模型可用性：每次最多 3 个，`blocked` 不重试 |
| `benchmark-refresh` | 24 小时 | 只读 | 刷新模型评分（TTL 缓存） |
| `object-gc` | 7 天 | **破坏性，默认预演** | 内容对象库的 mark & sweep：递归标记可达对象，列出无引用对象与体积 |
| `archive-retention` | 7 天 | **破坏性，默认预演** | 列出归档占用；归档是失败现场的唯一冷备份，删除需显式触发 |
| `knowledge-maintenance` | 6 小时 | 只读 | 按相对阈值（失效占比 ≥30% 或条目 >60）指出哪些项目该整理，不直接调用模型 |

`GET /api/maintenance` 返回每个作业的上次结果、耗时、下次到期与连续失败次数；`POST /api/maintenance/run {"job":"object-gc","dry_run":true}` 手动触发（不传 `job` 则跑所有到期作业）。插件可用 `runtime.maintenance.register(job)` 加入自己的作业。

## 知识层

- **不再自动堆积**：任务完成时不再把报告写进知识库（报告留在 `report` 记录里），只有模型显式调用 `broadcast` 才产生可复用知识。
- **来源分两类**：`scope=project` 按项目根比对，`scope=workspace` 按发布它的子工作区比对；同一文件两处都存在且内容一致时按项目记。这是为了修掉"父任务在自己工作区找不到子任务的来源文件、于是把有效知识全判失效"的问题。
- **依赖可双向传播**：发布会写反向边 `dependents`，文件变化时沿依赖图把受影响的知识一起标失效（写 `KnowledgeInvalidated`）。
- **检索要求全部词命中**（AND），标题命中权重高于正文，按相关度裁剪条数；失效知识默认排最后但仍可见。
- **共享信息层（`scope=run`）**：知识不止存"可复用的项目结论"，也承担**本次运行内的共享信息**——公告、状态、某局的发言。这类条目**不进**后续任务的简报索引、**不跨运行**（下一局搜不到上一局的内容），避免污染项目知识。
- **两条读法要分清**：`knowledge_search` 是**检索**（按相关度排序 + 默认截断），两个人搜同一个词也可能拿到不同切片；`knowledge_read(scope="run", from_seq=0)` 是**按序号枚举**（不过滤、不截断、顺序确定），**所有人读到的逐字节相同**——"看到同一份记录"必须用它。
- **推送而不是抄写**：`broadcast(..., scope="run", notify=[task_id...])` 会把条目**主动推送**给这些任务（收件箱直接可见，不必去搜）。这是"每个子智能都能看到别人的发言"的正确做法：让每个人自己读同一份，而不是给每个人一份手抄本——后者抄一次就产生一个分叉（真机 `run_574484cad16f` 里 C 的任务描述缺了 B 的发言、D 的缺了 C 的发言）。
- **分叉检测**：`usage_report` 与 `GET /api/knowledge?scope=run` 会返回 `divergence`——①`late_injection`：某任务的描述里出现共享条目原文（有人把共享信息抄进去了）；②`unnotified_member`：发过言的人从未收到过别人的任何一条（可见性取决于自己搜）。只报告、不阻断。

## 验证证据

报告的 `verification_status` 是模型**声明**。`verification_evidence` 里只有**事实**：

| 字段 | 含义 |
|---|---|
| `verification_status` | 模型自己的声明（`verified` / `failed` / `not_verified` / `unknown`） |
| `evidence[]` | 模型的每条证据；能对上执行记录的会带上 `execution_ref`、`exit_code`、`timed_out`、`command` 原文与 `output_ref` |
| `command_executions` / `inspections` | 绑定到命令的条数 / 其余（目视、声明）条数 |
| `execution_records` | 本任务一共留下多少条执行记录 |

**运行时不再给证据评等级。** 这里一度有一套 `verified_by_command` / `verified_by_command_weak` /
`verified_by_inspection` / `unverified` 的分级，还额外用正则判断"退出码是否可能被 shell 结构
吞掉"（heredoc、管道、`sed -i`…）、用清单摘要判断"证据是不是跑在交付版本上"，凑不齐就写
`VerificationUnconfirmed` 事件。

那是运行时替人下判断，而且判据脆弱：正则命中不等于检查没通过，"跑过测试"与"测试真的过了"
的区别本来就该由读报告的人结合命令原文、退出码和文件变更自己判断。所以现在运行时只做一件事：
**把模型的证据绑定到真实执行记录，并原样保留命令与退出码**。文件在证据之后又改过这件事，
仍然照常记录在报告的 `files` 里——事实留着，只是不再换算成等级。

程序不强制模型必须跑命令，验证仍由模型自觉（可用 `review_model_id` 开审查子任务）。

## 会话历史的事实源

模型可见的每一条消息都先写进**事件日志**，历史是日志的派生结果（`derive_history()`，与 DSH 的 `deriveMessages` 同构的纯函数）：

- `MessageAppended` 记录消息本身与它的对象引用；
- `ToolResultRecorded` 只记引用，派生时按 `call_id` 插回发起它的 assistant 消息之后（同一批多个结果保持模型顺序）；
- `ToolRepairRecorded` 是崩溃修复的合成结果（`TOOL_NOT_STARTED` / `TOOL_OUTCOME_UNKNOWN`）；
- `ContextCompacted` 记录摘要、`kept_from`（尾部原文从哪一条开始保留）、`region_ref`（被遮蔽的原文）与 `checkpoint_ref`。

压缩在派生里是**全量代入**：每个压缩事件把"这一刻之前累积的历史"替换成"头部两条 + checkpoint + `kept_from` 之后的原文"。`kept_from` 是压缩算法与派生共用的唯一定义，所以两者对同一份历史得到完全相同的结果，不需要任何下标推算。

**任务记录里不再保存历史副本**：只留 `history_messages`（消息数，用于轻量校验）与 `history_ref`（最后一条消息的对象引用）。因此任务记录的大小与对话长度无关，追加消息也不会再重写整段历史；`GET /api/task` 返回的 `history` 是服务端按需派生的结果。

**升级期规则**：升级前创建的任务没有消息事件，仍从其记录里的 `history` 字段读取（不做替换、不改写）；一旦某个任务产生了消息事件，日志就是它唯一的事实源。若派生条数与 `history_messages` 不一致，会写一条 `HistoryDerivationShortfall` 事件——这是派生实现出问题时的报警，不是正常状态。

## 一条可核对的证据链

每份报告、每次请求、每条知识都能互相回指：报告带 `request_headers`（哪次请求、提示词与工具 schema 的哈希、事件游标）与 `execution_records`（命令、退出码、结束时的清单引用），知识带来源的 `scope`/哈希与 `dependents` 反向边。因此"这次测试之后代码有没有再变"、"当时用的系统提示词是什么"都能查，而不必只引用模型的自我陈述。

本测试集覆盖递归单槽、人类问答恢复、事件先到/去重、消息续批、显式暂停恢复、报告字段、SQLite 迁移与工作区 generation、provider 目录结构与默认模型守护、统一调用错误码、迟到模型结果、需求切换、文件冲突、归档恢复、compact 和本地 HTTP 模型协议。近期补充：**对话视图的运行时语义与数据契约**（`human_ask` 暂停整个运行、在飞的一轮交回队列而不是卡在 `running`、并发提问要等最后一个回答才放行、取消提问者即解除暂停、人工"继续运行"是显式覆盖、`/api/transcript` 按 Agent 分组历史并给出 `hold`）、**历史从日志派生且任务记录不再保存历史**（清空记录后仍能逐字重建、重启后以日志为准、压缩后派生结果一致、同一批多工具结果保持模型顺序、记录大小不随对话增长、旧任务仍可读）、主动 compact 的当轮生效与同轮续跑、压缩遮蔽可还原、请求信封可逐字重建、两种中断文案、统一维护层（注册表/退避/预演/GC 标记/stat 短路）、知识来源基准与依赖传播、证据与执行记录的绑定（不再评等级）、凭据工具与 shell 环境注入、外部调用令牌与 `/api/wait` 契约。本测试集使用离线适配器与本地 HTTP 服务，不构成真实模型成功率或缓存节省比例的评测。供应商连接配置及单次可用性检查不等于协作验收。

## 文件结构

```text
unison/
  server.py         本地 HTTP / SSE 与控制台接口
  runtime.py        统一 Agent 循环、队列、消息、目标和知识
  tools.py          所有 Agent 共享的工具协议
  providers.py      DSH 风格 provider + models 目录（唯一真相源）
  models.py         wire api 编解码与统一模型调用层
  config.py         Codex TOML / DSH YAML 导入
  store.py          SQLite 与内容对象
  skills.py         技能发现、frontmatter 解析、调用契约、目录与正文渲染
  skill_runtime.py  技能端口调用：桥接执行、批量、作业持久化与轮询
  skills/           内置技能：gpu（探测/读取权重文件头/输出统计）、image-generation（云端 + local 后端）、
                    pixel-assets（像素化与精灵表）、theed（图像 → 3D 网格）
  workspace.py      文件版本、三方集成、归档恢复
  maintenance.py    统一维护层：作业注册表、调度、预演与对象回收
  demo.py           明确标注的离线机制演示（仅命令行/测试）
  web/
    index.html      控制台外壳
    style.css       设计令牌与组件样式（含浅色 / 深色）
    js/api.js       HTTP 与事件流客户端
    js/ui.js        格式化、模板与提示
    js/views.js     各标签页与模型目录的视图渲染
    js/app.js       状态、路由与交互
    favicon.svg
tests/              运行时、模型协议、provider 目录与服务端鉴权测试
docs/API.md         外部程序调用契约（端点、错误码、示例）
legacy-web/         旧版工作台界面备份，可直接删除
```

前端资源直接由服务托管，无需构建步骤，也没有第三方前端依赖。

## 可靠性补充（2026-09-11）

- 集成写入前保存恢复日志，写入异常时撤销已应用的文件；重启恢复未完成日志。恢复遇到后续用户修改时停止并报告路径，不覆盖该修改。文件提交后的数据库状态、集成事件和来源版本一并提交。
- 已采纳版本按目标任务保存；重复集成只比较该来源后续新增变更，历史初始基线保留。
- 子工作区、文件集成和归档恢复保存普通 Unix 权限位。旧归档没有权限元数据时仍兼容读取。
- 同一数据目录内，未结束运行不能重复占用同一项目或父子目录。此限制不是跨进程或跨数据目录的锁，也不限制外部编辑器。
- 排除目录进入 omitted_files。副本仍不包含依赖目录、Git 元数据、符号链接与超大文件；运行真实项目仍需按项目要求准备环境。
- 完整事件重建、插件生命周期重构、扫描异步化及真实模型对照评测仍未实现。详见 [实施记录](实施记录.md)。

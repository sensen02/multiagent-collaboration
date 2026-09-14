# Unison HTTP API（外部程序调用契约）

Unison 的服务监听 `127.0.0.1`，控制台和外部程序使用同一组端点。本文档只描述**外部程序驱动**所需的稳定子集：提交目标、等待结果、读取报告、读取凭据。控制台额外使用的端点不在此承诺。

## 1. 鉴权

| 运行方式 | 行为 |
|---|---|
| **默认**（`python -m unison.server`，本机单人） | **不鉴权**：同机任何进程都能调用全部端点 |
| `--require-auth` | 要求令牌：`Authorization: Bearer <token>` 或 `X-Unison-Token: <token>` |
| `--require-auth` 下的浏览器控制台 | 同源请求（`Origin` 与 `Host` 一致）免令牌 |

启动时会打印一行警告说明当前处于无鉴权状态。

> **必须知道的后果**：无鉴权时，`GET /api/models/credentials` 会向**同机任何进程**返回明文 API Key。
> 只在你自己独占的本机使用；不要放在共享机器或多用户环境里。同源免令牌也不是进程隔离——
> 本机程序可以伪造 `Origin`。

令牌按以下顺序确定，并且只在服务端比较（不会回显给控制台）：

1. 命令行 `--api-token <token>`
2. 环境变量 `UNISON_TOKEN`
3. 数据目录下的 `api_token` 文件（首次启动自动生成，权限 `0600`）

启用鉴权后，缺少或不匹配返回 `401`：

```json
{"error": "需要 API 令牌：…", "code": "auth_required"}
```

## 2. 错误契约

所有错误都是 JSON，**`code` 稳定**，`error` 是给人看的文案：

| code | HTTP | 含义 |
|---|---|---|
| `auth_required` | 401 | 缺少或错误的令牌 |
| `invalid_request` | 400 | 参数缺失、目标为空、目录被占用、未知 id 等 |
| `internal_error` | 500 | 未预期的服务端故障 |

## 3. 端点

### GET /api/state
全局快照：`runs`、`tasks`（不含历史与清单）、`models`（不含凭据）、`questions`、`app_config`、`cursor`（当前事件游标）、`data_dir`、`default_workspace`。

### POST /api/runs
创建一个目标（run）。

```json
{"goal": "给项目增加导出功能", "workspace": "/abs/path/to/project", "model_id": "OpenAI:gpt-5.6-sol"}
```

返回 run 对象，其中 `root_task` 是要跟踪的任务 id。`model_id` 必须是 `GET /api/state` 中 `models[].id` 之一。

### GET /api/wait?task_id=<id>&timeout=<秒>
阻塞等待任务进入终态或需要人工介入，避免外部程序轮询。

- 终态：`{"task_id","status":"completed|failed|cancelled|superseded","result","error","report_id","timed_out":false}`
- 需要人工：`{"task_id","status":"waiting","wait":{…},"timed_out":false}`（此时应读取 `GET /api/state` 的 `questions` 并用 `POST /api/answer`）
- 窗口用尽仍在执行：`{"…","timed_out":true}`，可再次调用

`timeout` 上限 900 秒；HTTP 侧超时窗口会自动放宽到 `timeout + 60`。

### GET /api/task?id=<id>
单个任务的详情：`task`、`messages`、`reports`、`changes`、`events`（最近 200 条）。报告里包含 `verification_status`、`files`（含 `reason`）、`execution_records`、`omitted_files`、`evidence`、`manifest_ref`。

### GET /api/events?run_id=<id>&after=<seq>&limit=<n>&types=A,B
增量事件流。`after` 用上一次返回的最大 `seq`，即可只取增量；`types` 按事件类型过滤。SSE 版本是 `GET /api/stream?after=<seq>`，只推送最新游标。

### GET /api/knowledge?run_id=<id>&query=<文本>
该项目共享知识；`stale=true` 的记录不能当当前事实使用。

### GET /api/knowledge?run_id=<id>&scope=run&from_seq=<n>&limit=<n>
按**序号顺序枚举**本次运行的共享信息（`scope=project` 则枚举项目知识）。不带 `query`、不按相关度排序、`limit=0` 表示不截断，因此任何调用者读到的都是同一份——核对"所有人是不是看到同一份记录"要用这一条。

返回 `entries`（`seq`/`scope`/`author`/`title`/`content`/`stale`）与 `divergence`：`late_injection`（某任务的描述里出现共享条目原文）与 `unnotified_member`（发过言的人从未收到别人的条目）。两者都只报告、不阻断。

### GET /api/requests?task_id=<id>&run_id=<id>&limit=<n>
某个任务或运行的全部**请求信封**（最近优先）。每次模型调用都会落一条，用来回答"通过的是哪一次请求"。

```json
{"requests":[{"id":"req_…","seq":142,"model_id":"OpenAI:gpt-5.6-sol","context_epoch":0,
              "messages":12,"input_bytes":20480,"tools_count":24,
              "prompt_sha256":"…","tools_sha256":"…","input_ref":"…","prompt_ref":"…","created":1789…}]}
```

- `seq` 是记录时的全局事件游标：拿它可以和 `GET /api/events?after=` 对齐，判断这次请求之后代码有没有再变。
- `input_ref` / `prompt_ref` / `tools_ref` 是内容对象引用；相同的系统提示词与工具 schema 只存一份（按哈希去重）。

### GET /api/request?id=<req_id>&restore=1
按记录**逐字重建**这一次请求，并可选还原压缩前的历史。

```json
{"header":{…},
 "assembled":{"config":{"id":"…","model":"…","base_url":"…","api":"openai-responses"},
              "prompt":"<系统提示词原文>","messages":[…],"tools":[…]} ,
 "restored":[…]}
```

- `assembled` 不依赖任何进程内状态：用 `header` 里的引用即可重建 `config`、`prompt`、`messages`、`tools`。
- `restore=1` 时返回 `restored`：把该次请求的输入**向内解开一层 compact 遮蔽**，得到"压缩之前那一刻的历史"。
  对多次压缩的记录连续调用可逐层还原到最初原文。

### GET /api/maintenance
所有维护作业的统一视图：`id`、说明、间隔、是否预演、是否破坏性、上次结果与耗时、连续失败次数、下次到期。

```json
{"jobs":[{"id":"object-gc","interval":604800,"dry_run":true,"destructive":true,
          "last_summary":"预演：23 个无引用对象（12.4 KB）","last_counts":{"orphans":23},
          "consecutive_errors":0,"next_due":1789250000.0}]}
```

### POST /api/maintenance/run
手动触发维护。`{"job":"object-gc","dry_run":true}` 触发单个；省略 `job` 则执行所有到期作业。
`dry_run` 默认 `true`——破坏性作业（对象回收、归档清理）只有在显式 `dry_run:false` 时才会真正删除。
返回本次结果与刷新后的作业状态。

### GET /api/skills?workspace=<路径>&run_id=<id>
技能目录：模型在本会话能看到的技能（名称、摘要、适用场景、来源根与优先级）。
`workspace` 缺省为数据目录下的 `playground`；给定 `run_id` 时项目根取自该运行的根任务，
因此返回的技能与任务实际加载到的完全一致。

```json
{"workspace":"/srv/app","project_root":"/srv/app","revision":"f423963da1016c21",
 "skills":[{"name":"image-generation","description":"用提示词生成图像并落盘…","source":"bundled",
            "rank":100000,"model_invocable":true,"base":"/opt/unison/unison/skills/image-generation",
            "path":"…/SKILL.md"}],
 "diagnostics":[],"roots":["/srv/app/.dsh/skills","/home/u/.dsh/skills","…"],
 "bundled_dir":"…/unison/skills"}
```

`diagnostics` 列出被拒绝的技能文件（缺少 `description`、布尔写法不认识、同名被更高优先级覆盖等），
每条带 `path` 与可读原因——技能坏了会明确出现在这里，不会静默消失。

### GET /api/skill?name=<技能名>&workspace=<路径>&refresh=1
加载一个技能的完整内容。`content` 与模型调用 `skill` 工具时看到的**逐字一致**（`<skill_content>` 包装）。

```json
{"project_root":"/srv/app",
 "skill":{"name":"image-generation","source":"bundled","rank":100000,"base":"…",
          "when_to_use":"任务需要配图、海报、图标…","content":"# 图像生成…"},
 "content":"<skill_content name=\"image-generation\">…"}
```

技能名不存在或不是合法 kebab-case 时返回 `400 invalid_request`，文案里带上当前可用技能名。

### POST /api/skills/call
**技能端口调用**：把声明了 `invocation` 契约的技能当成可调用的能力，形状与模型调用一致——
提交请求，窗口内完成就返回结果，否则返回 `job` 由调用方轮询。

```json
// 单次
{"skill":"image-generation","workspace":"/srv/app",
 "args":{"prompt":"a red fox in snow","out":"images/fox.jpg","width":1024,"height":1024}}
// 批量（技能必须声明 invocation.batch；上限 8 份，超过明确报错而不是截断）
{"skill":"image-generation","workspace":"/srv/app",
 "batch":[{"prompt":"fox"},{"prompt":"lake","seed":7}]}
// 可选字段：wait（等待秒数，默认 30，上限 900）、timeout（不得超过技能声明的上限）、run_id
```

```json
{"done":true,"poll":null,
 "job":{"id":"skilljob_ab12","name":"image-generation","status":"succeeded","batch":false,"count":1,
        "timeout":300.0,"result":{"path":"images/fox.jpg","format":"jpeg","width":1024,"height":1024},
        "results":null}}
```

- 结果形状由技能自己的桥接脚本决定；运行时只强制 `{"ok": bool}` 骨架，`ok:false` 时 `error` 进作业记录的 `error`。
- 执行失败（桥接报错、退出码非零、输出不是 JSON、超时）是**作业状态 `failed`**，不是 HTTP 错误；
  提交参数不合法（技能不存在、没声明契约、批量超限、args 非对象）才是 `400 invalid_request`。
- 作业状态持久化在 `skill_job` 记录里，进程重启后仍可查询；上次运行中途中断的作业标记为
  `interrupted` 并说明"不会自动重放"，需重新提交。

### GET /api/skill/jobs?limit=<n>
最近的技能作业，最新在前。

### GET /api/skill/jobs/<job_id>
单个作业。`status` 为 `queued` / `running` / `succeeded` / `failed` / `interrupted`。

```json
{"job":{"id":"skilljob_ab12","status":"running","count":2,"created":1789188000.1,"started":1789188000.2,
        "finished":null,"result":null,"results":null,"error":null}}
```

### POST /api/skills/reload
强制重扫技能目录（忽略缓存），用于在外部编辑器里改完技能后立即刷新。
请求体 `{"workspace":"…","run_id":"…"}` 均可省略。返回与 `GET /api/skills` 相同的结构。

### GET /api/models/health
模型可用性维护视图。每个模型给出 `state`（`ok`/`blocked`/`unknown`）、`code`、`reason`、
`remedy`（该做什么）、`source`（证据来自 `call` 真实调用还是 `probe` 主动探测）、
`checked_at` / `expires_at`、`stale`（证据是否已过期）、`needs_probe`。

```json
{"summary":{"total":12,"ok":6,"needs_probe":6,"blocked":6},
 "ok_ttl_seconds":86400,"transient_limit":3,
 "models":[{"id":"OpenAI:gpt-5.6-terra","state":"ok","source":"call","stale":false,
            "checked_at":1789189902.3,"expires_at":1789276302.3,"needs_probe":false,
            "code":null,"reason":"","remedy":"","failures":0}]}
```

维护语义（这是"不靠反复烧 token 验证"的关键）：

- **真实调用是主证据**：任何一次真实模型调用成功都会把状态写成 `ok(source=call)` 并顺延 24 小时；
  失败按错误码分流，不会被一次瞬时故障永久污染。
- **`blocked` 表示需要人处理**（凭据无效、模型不存在、参数被拒），`needs_probe=false`，
  后台探测**不会**重试它；改配置（重新导入 / 扫描 / 保存模型）后标记自动清除。
- **过期语义**：`expires_at` 之后 `stale=true`，才会进入复验队列。

### POST /api/providers/verify
探测模型可用性。**每个模型消耗一次真实调用**，因此默认只探测"已过期或未验证"的模型，
并按最久未验证优先排序。

```json
{"provider_id":"OpenAI","force":false,"limit":3,"model_ids":["gpt-5.6-terra"]}
```

- `force=true`：显式全量复验（人明确要求时）。
- `limit`：本次最多探测几个（`model-health` 后台作业用 3）。
- 返回 `checked` / `reachable` / `skipped`（跳过了哪些、为什么），便于确认为什么没花 token。

### GET /api/load?run_id=<id>
负载与额度视图：每个 provider+模型 桶的在途数、峰值、窗口用量、排队与重试计数、当前额度。

```json
{"buckets":[{"bucket":"OpenAI::gpt-5.6-sol","in_flight":1,"peak_in_flight":1,"queued":3,
             "capacity":{"concurrency":1,"tpm":200000,"rpm":60,"window":60.0,"queue_timeout":600.0},
             "window_tokens":15230,"window_requests":4,"retries":0,"throttled":0,"total_wait_seconds":12.4}],
 "in_flight":1,"queued":3,"retries":0,"throttled":0,
 "usage":{"calls":4,"tokens":15230,"prompt_tokens":14800,"completion_tokens":430,
          "waited_seconds":12.4,"retries":0,"by_model":{"OpenAI:gpt-5.6-sol":{"calls":4,"tokens":15230}}}}
```

`usage` 为可选 `run_id` 的聚合（省略则全局）。用量来自响应里的真实 `usage`；
响应没有 `usage` 时按请求字节保守估算。

### GET /api/models/credentials?model_ids=a,b
**返回明文凭据**，供外部程序与模型在代码中直接调用模型接口。省略 `model_ids` 返回全部模型。

```json
{"models":[{"id":"OpenAI:gpt-5.6-sol","base_url":"https://aaa.bi","api":"openai-responses",
            "api_key":"sk-…","key_env":"","no_key":false,"headers":{},"context_window":272000,"max_output":4096}]}
```

### POST /api/models
新增或更新一个模型记录（控制台"模型与 Provider"对话框使用的路径）。

### POST /api/message · /api/resume · /api/answer · /api/task/cancel · /api/runs/pause · /api/runs/resume · /api/revise · /api/archive · /api/restore · /api/compact
分别用于：给任务发消息并唤醒、恢复失败任务、回答问题、取消任务子树、暂停/继续派发、切换目标版本、归档、从归档恢复到新目录、请求压缩上下文。参数与 `unison/server.py` 的 `dispatch()` 一致。

## 4. 端到端示例

```bash
DATA=~/.unison
TOKEN=$(cat "$DATA/api_token")
BASE=http://127.0.0.1:8740/api

# 1. 创建目标
RUN=$(curl -s -X POST "$BASE/runs" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"goal":"修复 README 中的拼写错误","workspace":"/abs/project","model_id":"OpenAI:gpt-5.6-sol"}')
TASK=$(python3 -c "import json,sys;print(json.loads('''$RUN''')['root_task'])")

# 2. 阻塞等待结果
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/wait?task_id=$TASK&timeout=600"

# 3. 读取报告与变更原因
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/task?id=$TASK" | python3 -m json.tool

# 4. 增量跟随事件
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/events?after=0&types=TaskCompleted,ChangeReportPublished"
```

## 5. 边界

- 仅监听 `127.0.0.1`：本机单用户模型。同源判定基于 `Origin`/`Host`，本机其它进程可伪造该头；需要更强隔离时请改用 `--api-token` 之外的部署方式（反向代理 + 独立鉴权）。
- 外部命令无法承诺恰好执行一次；重启遇到在途工具时任务会被标记为需要检查。
- 报告里的"完成"是提交状态，`verification_status=verified` 才是模型声明的验证结论，两者不能混同。
- 凭据端点返回明文 Key；不要把它写进日志、报告、知识或提交到版本库。

# Rail Dispatch v1 编排辅助工具

本目录只包含测试编排与审计工具；不会修改 `starter/` 或 `evaluator/`。以下命令都只展示用法，不会自行调用模型。四阶段驱动中的 stage0 是初始任务文本，stage1–3 通常对应 evaluator 提供的阶段文本；由操作者明确传入文件。

## 1. 审计代理 `audit_proxy.py`

仅使用 Python 标准库，默认绑定回环地址，并把允许的请求转发到 `https://aaa.bi/v1`：

```bash
python3 harness/audit_proxy.py --port 9011 --audit artifacts/audit.jsonl
```

允许 `/chat/completions`、`/responses`（也兼容客户端带 `/v1/` 前缀）。chat/responses 请求必须是 JSON，且 `model` 必须精确为 `gpt-5.6-sol`；否则返回 403，并在 audit 的 `violation` 字段留下记录。`GET /models` 不访问上游，只在本地返回唯一模型 `gpt-5.6-sol`，避免上游模型目录污染。响应会完整缓冲后原样写回，所以流式 SSE 的内容与事件边界得到保留，但首字节会等到上游流结束。`--upstream` 默认为 HTTPS 生产地址，也接受 HTTP，专供回环假服务离线测试。

代理透传调用方的 Authorization 标头，但绝不把请求标头、API key 或完整 prompt 写进日志。每条 JSONL 包含时间、模型、路径、HTTP 状态、耗时、请求字节数、违规原因，以及普通 JSON/SSE 中尽量提取的 `prompt_tokens`、`completion_tokens`、`total_tokens`、`cache_tokens`。API key 应只通过调用方已有的环境/配置传递，不要写到命令行或产物目录。

完整参数：

```bash
python3 harness/audit_proxy.py --help
```

## 2. DSH SDK 驱动 `dsh_sdk_driver.mjs`

先在 DSH profile 中配置名为 `audit` 的 provider，使其 base URL 指向上述代理，并使其精确模型为 `gpt-5.6-sol`。驱动启动自己的 `dsh --profile sdk` 子进程，以逐行 JSON-RPC 初始化，然后在同一个 session 中严格顺序发送四个 prompt：每次观察到该 session 的 `running`，再等待 `idle`，才发送下一阶段。

```bash
node harness/dsh_sdk_driver.mjs \
  --cwd "$PWD/benchmarks/rail-dispatch-v1/starter" \
  --output artifacts/dsh \
  --max-tokens 32768 \
  prompts/stage0.md evaluator/stages/stage1.md \
  evaluator/stages/stage2.md evaluator/stages/stage3.md
```

产物：

- `notifications.jsonl`：全部 SDK 通知（含接收时间）；
- `summary.json`：session、各阶段 message id/起止/耗时、最终捕获的 assistant 事件、通知中递归提取的 `observedRoutes`/`routeViolations`、shutdown 结果；
- `dsh-stderr.log`：DSH 诊断输出（stdout 专用于 JSON-RPC）。

阶段、RPC 均有独立超时；异常时先 SIGTERM，宽限后 SIGKILL。默认 `--profile sdk`，也可指定隔离的 SDK profile 名；子进程显式继承当前环境。查看完整参数：

```bash
node harness/dsh_sdk_driver.mjs --help
```

## 3. Unison HTTP 驱动 `unison_driver.py`

该脚本连接**已有**服务，不启动服务，也不读取其秘密端点。它按当前 `unison/server.py` 契约调用：

1. `POST /api/runs` 创建 stage0 run；
2. 轮询 `/api/state` 与 `/api/events`，直到 run 完成；
3. 对 stage1–3 依次 `POST /api/revise`，每次等待完成后再进入下一阶段；
4. 每次 revise 都重新读取响应中的 run id；当前实现保持 run id、替换 `root_task`，若未来改为新 root/run 也会自动跟踪。

```bash
UNISON_TOKEN=... python3 harness/unison_driver.py \
  --url http://127.0.0.1:8740 \
  --workspace "$PWD/benchmarks/rail-dispatch-v1/starter" \
  --model-id YOUR_CONFIGURED_MODEL_ID \
  --output artifacts/unison \
  prompts/stage0.md evaluator/stages/stage1.md \
  evaluator/stages/stage2.md evaluator/stages/stage3.md
```

无鉴权的本地服务可省略 token。token 只来自 `--token`/`UNISON_TOKEN`，不会保存到日志。产物为 `responses.jsonl`（所有轮询与变更响应）、`events.jsonl`（去重游标采集的事件）和 `summary.json`（阶段耗时、最终 run/root/task ids）。

```bash
python3 harness/unison_driver.py --help
```

## 4. Usage 汇总 `summarize_usage.py`

```bash
python3 harness/summarize_usage.py artifacts/audit.jsonl
python3 harness/summarize_usage.py --json artifacts/audit.jsonl
```

输出调用数、模型集合、违规数、prompt/completion/total/cache token 总数和缺失 usage 的调用数；损坏的 JSONL 行会告警并单独计数。

## 静态检查

不连接服务、不发网络请求的检查命令：

```bash
python3 -m py_compile harness/audit_proxy.py harness/unison_driver.py harness/summarize_usage.py harness/test_audit_proxy.py
python3 -m unittest harness/test_audit_proxy.py
node --check harness/dsh_sdk_driver.mjs
```

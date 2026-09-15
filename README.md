# Multi-Agent Collaboration

一套**本地多模型协作工作台**的架构设计与可运行实现。

核心取向来自设计文档 §1：**一个 Agent 抽象，一个事件事实源，一组可组合能力；主调度模型与子智能体使用同一套运行循环。**

运行时只保留与任务内容无关的执行不变量——调用格式合法、任务不被重复领取、结果不覆盖较新的版本、取消有效。任务怎么拆、用哪个模型、串行还是并行、怎么验证、什么时候交付，全部由 AI 自己决定；程序不根据"简单/中等/复杂"分派固定流程，也不强制所有任务经过规划者、执行者、审查者。

## 目录

| 路径 | 内容 |
|---|---|
| `融合方案设计.md` | 架构设计 v4（v1–v3 在 `archive/`） |
| `unison/` | 可运行实现：单进程 Python 运行时 + HTTP/SSE 服务 + 控制台前端 |
| `unison/README.md` | 使用说明：模型层、技能、维护层、知识层、边界与取舍 |
| `unison/实施记录.md` | 逐轮改动记录，每条都对应一次真机事故 |
| `unison/docs/API.md` | 外部程序调用的 HTTP 契约 |
| `benchmarks/rail-dispatch-v1/` | 多阶段增量需求基准：starter 项目、评分器、编排与审计工具、unison 与 DSH 两个驱动 |
| `archive/` | 历史设计版本与第六轮"做减法"清理掉的代码 |

## 启动

需要 Python 3.11+；三方文本合并使用 Git；没有第三方 Python 运行依赖。

```bash
cd unison
python3 -m unison.server --port 8740
```

浏览器打开 <http://127.0.0.1:8740> 进入 Unison Console。服务默认只监听本机、默认不鉴权（本机单人使用），加 `--require-auth` 启用令牌校验。默认数据目录是 `unison/.unison/`，新任务的工作区默认取数据目录同级、名字带 `-workspace` 的目录。

### 模型凭据

**源码里没有任何 API Key。** 首次运行需要在控制台顶栏「模型」里导入已有配置——Codex 的 `~/.codex/config.toml` 片段或 DSH 的 `settings.yaml` provider 片段，Unison 会识别格式、导入 Provider 并扫描 `/models` 补齐模型。

内置默认 provider 的凭据只从环境变量读取，缺省为空：

```bash
UNISON_BUILTIN_API_KEY=<your-key> python3 -m unison.server --port 8740
```

导入配置时随配置一起提供的 key 优先于这个环境变量。
**其实我更建议用其他AIagent辅助完成apikey的添加**

## 验证

```bash
cd unison
python3 tests/run_all.py
node --check unison/web/js/app.js   # 前端无构建步骤，语法自检即可
```

当前 **302 个用例全部通过**。请使用 `tests/run_all.py` 而不是 `unittest discover`：后者只按模式收集文件，历史上曾让运行时最核心的 72 个用例静默地一个都没跑。

## 数据与凭据边界

- 数据目录（SQLite、内容寻址对象库、归档）不随仓库分发。
- **所有接入模型同等信任**：没有模型工具白名单、逐模型权限、工具审批或安全策略插件。子工作区分开是为了保存协作分支、避免文件互相覆盖，**不是安全隔离**。
- 模型凭据是可用能力：`models_credentials` 可按需返回明文 Key，`workspace_shell` 会向子进程注入 `UNISON_MODEL_*` 环境变量。需要更严的隔离时应自行加反向代理或改部署方式。
- 当前是单进程本机运行时，不承诺任意外部命令恰好执行一次，也不实现分布式调度。

## 未包含在本仓库

- `papers/`（7 篇论文 PDF）与 `repositories/`（7 个第三方实现，仅供对照参考）留在本地，不随仓库分发——它们不是本项目的一部分，其中多数另有各自的许可证。
- `benchmarks/*/runs/` 下的运行产物（含明文凭据、完整会话与评分中间态）不纳入版本控制；仅保留各阶段契约文本，供基准复现。
- `apikey.txt` 与 `unison/.unison*/` 属于本机凭据与运行数据，已被 `.gitignore` 排除。

## 许可证

[Unlicense](LICENSE) 

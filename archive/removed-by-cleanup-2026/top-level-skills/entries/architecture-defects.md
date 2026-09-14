# 知识条目：sword-demo 暴露出的架构问题（不是游戏的问题）

**样本**：`run_acf011930033`（2026-09-13，5 个任务，26 个 run 里最新的一次）。
**方法**：从 SQLite 事件表与会话记录里重建这次运行，再用本机实测交叉核对。
**结论**：这次运行**成功交付了游戏**，但它同时暴露了 6 个架构缺口——
每一个都能用"另一条同样合法的输入"变成事故。

> **2026-09-13 追加**：缺陷 1、2、6 已完整修复，3、4 部分修复，5 有明确修法但**未动运行时**。
> 每节末尾的"已修"写的是**改了什么**（代码与测试），不是"以后注意"。

## 缺陷 1：能力的**名字**与能力的**实现**对不上，而且没有东西会自动发现这件事

| 位置 | 说法 |
|---|---|
| `image-generation/SKILL.md` 的 description（模型**只能**看到摘要来做选择） | "两个后端：**本机 GPU** 或匿名云服务 Pollinations" |
| 同一个技能的 `scripts/generate.py` | `--backend` 只接受 `pollinations` / `huggingface`，**没有任何本地分支** |
| 实施记录 第 109 行 | 当时为了让模型"选对"，**主动把"本机后端"写进了摘要**——脚本没跟上 |

后果在运行里可见：素材任务要"本机出 30 张立绘"，它只能手写一个 `scripts/generate_portraits.py`
直接调 `sd-cli`——**生图这件事完全没有经过技能层**，也就没有技能层承诺的任何约束。
同一轮里 `skill` 工具只被调用 4 次，且 `gpu/scripts/probe.py` 是用**绝对路径**直接跑的
（`workspace_shell` 里可见），说明"加载技能正文"这条路径也被绕开了。

**架构缺口**：技能是"声明 + 脚本 + 正文"三件套，但运行时不校验**声明的能力是否真的被执行体支持**。
摘要可以描述一个不存在的后端，而且摘要正是模型唯一的决策输入。
**修法**：技能 frontmatter 的 `metadata` 里把"支持的后端/能力"做成枚举，加载时与执行体自报的能力
对账，不一致就写诊断（控制台已经有诊断位，`invocable` 也已经能校验 bridge 存在——这条是同一件事的延伸）。

**已修（2026-09-13）**：本机 GPU 生图成为 `image-generation` 的**真实后端**（`--backend local`，
内含权重角色校验、LoRA 生效检测、输出退化检查、`generation.json` 溯源），
`local-image-gen` 退化为**薄适配器**（不再持有第二份实现）；
新增 `unison/skillcheck.py` 做**能力对账**（frontmatter 声明 vs 执行体 `--capabilities` 自报，六类诊断），
接入 `GET /api/skills` 的 diagnostics 与 `skill-catalog` 作业（不一致写 `SkillCapabilityMismatch` 事件）。
`tests/test_skill_capabilities.py` 把"摘要写 local、实现只有 pollinations"这条**真事故的形状**钉成断言。

## 缺陷 2：清单写下的"角色"被当成事实，而且没人验

`assets/portrait-generation.json` 记录了每条命令（含 `--backend vulkan0`、seed、步数、cfg）——
这一点做得对，值得保留。它缺的是**另外三样**：

- `model_sha256` 是**手写的字面量**，不是运行时算出来的（这次核对是对的，
  但"手写的哈希"和"算出来的哈希"在证据链上不是一回事）；
- 没有记录**二进制版本**（`sd-cli` 自报 `master-859-7f410a3`），换一次构建就复现不了；
- 没有记录影响结果的标志位（见缺陷 6），也没有记录输出文件的哈希。
同一份运行里，清单又把两个 LoRA 标成 `sd15-checkpoint`——下一个 agent 照清单读，
就会把它们喂给 `-m`。**"少一个参数"这一类事故的全部土壤就在这里：元数据是手写声明，
没有任何环节把它与文件/命令的事实对账。**


**修法**：能力型技能产出的元数据必须**由执行体生成**（`local-image-gen` 的 `generation.json`
就是这么做的：完整命令行 + 权重 sha256 + 输出 sha256 + 退化检查数字），并且提供只读的
**复核对账**入口（`weights.py` 判定角色并与清单对照，`imagecheck.py` 判定输出是否退化）。

**已修（2026-09-13）**：`gpu/scripts/weights.py` 按文件头判定角色并与 `manifest.json` 对照
（实测已抓出两个被标成 checkpoint 的 LoRA）；本机后端把它变成**前置硬校验**：
角色不对直接拒绝执行、**一张都不生成**。溯源改由执行体生成；那份手写的
`generate_portraits.py` 已随 `local-image-gen` 改写为薄适配器而消失。

## 缺陷 3：成果搬运绕过了集成层，交付物因此不可回指

游戏运行里的搬运全部是 shell：

```text
[af02d5]  cp /home/sensen/.../.unison/playground/sword-demo/engine.js sword-demo/engine.js
[af02d5]  cp -a .../task_e23c85af02d5/sword-demo/assets sword-demo/
[44d430]  cp /home/sensen/.../playground/sword-demo/engine.js sword-demo/engine.js
[44d430]  cp .../task_e23c85af02d5/sword-demo/assets ... ; cp .../engine.js ...
```

`workspace_integrate` 在这次运行里被调用 6 次，而 shell 里的 `cp`/`mv`/`rsync` **也是 6 次**——
也就是说两条搬运路径**同时活跃在同一批文件上**。代价不是"少了集成事件"，而是**验证对象不可回指**：

- 被 `cp` 进去的文件没有三方合并、没有 generation 检查
  （工具里写了：提交前检查目标清单未变化并记录 generation——`cp` 让这套保护失效）；
- 审查任务 `task_4233db44d430` 把**自己的** sword-demo + 素材任务的 assets + 玩法任务的
  engine.js **拼在同一个工作区**里测（QA 报告自己写了"已从主工作区复制最终冻结 engine.js"），
  于是 PASS 的到底是哪个组合，只有那句自述，没有可核对的引用。

**修法**：这是"机制存在但被绕过"的经典形态。可做的有两件：
①`workspace_shell` 里的写操作（相对工作区的 `cp/mv/rsync/sed -i/apply_patch`）与 `workspace_write`
共用同一套版本记录与 generation 检查——至少不能**静默**绕过（本次 196 次工具调用里
`workspace_write` 只有 13 次，`workspace_shell` 有 62 次，改写引擎的主体是内联
`python3 - <<'PY'` 与 `apply_patch`，都在版本记录之外）；
②交付校验加一条：**通过状态必须绑定到"某个 generation 的清单快照"**，
而不是绑定到"某个工作区当前的样子"。

**已修（部分，2026-09-13）**：生图这条路上不再有"手写脚本绕过一切"的入口——
产物必然带 `generation.json`（命令行 + 权重 sha256 + 输出 sha256）。
但**工作区搬运仍走 shell**：`workspace_shell` 里的 `cp`/内联改写依旧不进版本记录，
缺陷 3 的运行时部分未动。

## 缺陷 4：验证是通过了，但验证的对象不是交付物

- QA 报告写 `Build: engine.js SHA256 afb7bfcb…`——这个哈希**确实**等于最终交付的那份，
  这一点做得对，值得保留。
- 但 `VerificationUnconfirmed` 在这次运行里出现了 4 次（全库 52 次报告里有 36 次），
  说明"声明已验证但拿不出命令证据"是常态而不是例外。
- 更根本的：`verified_by_command` 只要求"本任务有退出码 0 的执行记录"，
  **不要求该执行记录跑在交付物上**。把别人的工作区 `cp` 过来再测试，同样能拿到 `verified_by_command`。

**修法**：验证证据要带**被验证对象的指纹**（文件哈希或清单引用）。
`execution_records[].manifest_ref` 已经存在，把它接进验证判定即可：
证据有效 ⇔ 命令执行时的清单与交付物清单一致。

**已修（部分，2026-09-13）**：新建的能力测试把"证明"变成断言——
角色不对必须拒绝执行**且不调用 sd-cli**、退化输出必须判失败**且保留废图**、
溯源必须含完整命令行与三个哈希。"验证证据绑交付物指纹"仍未接进 `verification_evidence` 的判定。

## 缺陷 5：根是数据目录，项目/技能上下文因此是悬空的

所有 26 个 run 的 `project_root` 都是 `None`。根任务的工作区是
`/home/sensen/.../unison/.unison/playground`——**在运行时的数据目录里面**。
技能发现于是退化成"找最近的 `.git` 祖先"，而这个仓库不是 git 仓库，什么都不返回；
子任务的工作副本又在 `.unison/workspaces/` 下，且被 `workspace-scan` 的默认忽略规则排除
（`.unison` 是默认忽略目录），所以**子任务写的文件不进文件清单**——
`FileVersionRecorded` 全库只有 339 条，而 `workspace_write` 只被调用 18 次：
游戏的主要产物其实是通过 shell 与 `cp` 进出的。

**修法**：把"数据目录"与"项目目录"在语义上分开并强制：
根任务的工作区**不允许**落在数据目录内（或显式接受"这是沙盒"并把它写进技能根与扫描规则里），
否则"项目级技能/项目级知识/文件清单"这三件事都会静默失效。

## 缺陷 6：可复现性没有被当成契约

同一 seed、同一 prompt，在本机实测会因这些设置出**不同的图**：`--fa` / `--diffusion-fa`、
`--rng`、`--tensor-type-rules ^vae\.=f16`、`--vae-tiling`。
而 `portrait-generation.json` 只记了 prompt/seed/尺寸/步数/cfg——
**它记的"可复现"是不完整的**：换一台机器（甚至换一次标志位）就复现不出来，
而看它的人会以为可以复现。

**修法**：能力的溯源字段应当是"**足以重建这次调用**"的最小集合：
完整命令行 + 二进制版本（`sd-cli` 自报 `master-859-7f410a3`）+ 权重 sha256 + 环境标志。
`local-image-gen` 已经把完整命令行与权重 sha256 写进 `generation.json`；
二进制版本也应该一起记。

**已修（2026-09-13）**：本机后端逐条记录**完整命令行**（含 `--backend`，实测它与不写它逐字节同图，
但写进命令才让别人不必重新验证）、权重 sha256、输出 sha256、退化检查数字；
`gpu/references/failure-modes.md` 明确列出哪些开关会改图
（`--fa`/`--diffusion-fa`/`--rng`/`--vae-tiling`/`--tensor-type-rules`，实测）并给出结论：
要可复现就把它们固定进 `extra_args` 并让溯源记下来。二进制版本号仍未自动写入溯源。

---

## 一句话总结（给下一次做同类任务的人）

这次不是"模型不行"：5 个任务全部完成、QA 真的跑了 Playwright、立绘真的出图了。
问题都在**边界**上：**能力的名字、能力的事实、能力的产物，三者之间没有自动对账的环节**。
补的不是提示词，而是三处对账：声明 vs 执行体（缺陷 1）、声明 vs 文件事实（缺陷 2）、
验证 vs 交付物指纹（缺陷 4/6）。

---

# 第二轮（ink-duel 复核）：四个新缺口与它们的修法

`run_0917f0202d86`（ink-duel，2026-09-13）比 sword-demo 那次干净得多：2 个任务、
28 次工具调用、12.5 分钟、素材**走正规集成通道**带回来（`IntegrationCommitted`），
并且主动声明"未做独立视觉评审"。独立复核仍然找到 4 个缺口，现在都已修复。

## 缺口 A：验证证据会过期（本次最严重）

`tests/smoke.py` 只在跑到最后才写 `results.json`。失败时退出码 1、而上一轮的
`passed: true` 还在原地——**"退出码 0"与"证据写着 passed"可能来自两次不同的执行**。
断言还依赖固定 sleep（`press('Space')` 后 70ms），实测 11 次挂 1 次。

**已修**：证据无条件写（含 `exit_code` / `created` / `duration_seconds` / `failure` /
`artifacts` 哈希）；按键改成"轮询 + 重试至超时"（12 次连续运行 0 失败）；
运行时对每条命令证据判定**退出码是否被 shell 吞掉**与**是不是在交付版本上跑的**，
新增 `verified_by_command_weak` 档与 `changed_after_evidence` 列表，控制台直接显示。

## 缺口 B：heredoc 里的断言，运行时看不见

素材任务报告 `verified`，而 `verification_evidence` 是 `verified_by_inspection`、
`command_executions: 0`——它的 SVG 校验确实跑了，但写成 `python3 - <<'PY' ... PY`：
运行时只按命令字符串记账，看不到块内退出码。

**已修**：`exit_code_is_masked()` 识别 heredoc / 管道 / `sed -i` / 多行无 `set -e`，
把这些命令标成"退出码可能被吞掉"，不再计入可用的命令证据。

## 缺口 C：文档的启动路径在实际机器上是坏的

README 写"打开 127.0.0.1:8000"、`serve.py` 默认 8000、`tests/smoke.py` 默认 URL 也是 8000；
而本机 8000 上是**另一个应用**（uvicorn）。逐字复现：`serve.py` → `Address already in use`；
按文档跑测试 → 去断言那个别的应用的标题并失败。原报告只验了 `--port 8793`，
**默认路径从头到尾没被验证过**。

**已修**：三处统一到 8001（并在 README 说明为什么不是 8000）；`serve.py` 端口被占时
**明确失败并提示换端口**，不静默换；`skill-validator` 新增工作区级对账——
`port-mismatch` / `port-undocumented` / `port-occupied`（实测 `/proc/net/tcp` + `ps` 给出占用进程）。

## 缺口 D：根工作区在数据目录里，子副本带上所有历史

`task_135da1794697/` 里带着 `sword-demo/`（含 assets、tests）、`example_*/`、`out/`、`hello.txt`——
因为根任务工作区就是 `.unison/playground`，而 `sword-demo` 也在同一个根下。

**已修**：默认工作区改为数据目录**同级**的 `<数据名>-workspace`（`--workspace-root` 可指定）；
`create_run` 检测工作区是否落在数据目录内，是则在运行上标 `workspace_warning`
并写 `WorkspaceInDataDir` 事件（不阻断，但不静默）。旧的 `.unison/playground` 保持不变，
避免搬动既有交付物。

## 仍然存在的（有意保留）

`sword-demo/README.md` 与 `sword-demo/serve.py` 仍指向 8000，而 8000 在本机被别的应用占用——
现在 `skill-validator` 会把这条报出来（`port-occupied`），但**没有替它改端口**：
那是上一轮的交付物，改它会改变已验收的证据链。要改的话，先说清楚再改。

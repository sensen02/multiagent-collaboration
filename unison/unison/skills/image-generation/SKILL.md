---
name: image-generation
description: 用提示词生成图像并落盘到当前工作区。四个后端，**默认走 FLUX**：匿名云服务 Pollinations（`--model flux`，无需任何 Key，但分辨率封顶 ≈0.59 MP）、Hugging Face（FLUX.1 schnell/dev，需要 HF token）、**ChatGPT 官方 gpt-image（复用本机 Codex 登录态）**，以及**本机 GPU 跑 FLUX.1-schnell Q8（离线、可复现、不外传、无分辨率上限、Apache-2.0 可商用，已装好）**——本机会先读权重文件头（实测：只有 lora_ tensor 的文件填 `-m` 会得到废图；FLUX 是分离式，缺 t5xxl 就在加载阶段失败）、再生成、再把输出的画面统计量一并交付、最后落一份含完整命令行与权重 sha256 的 `generation.json` 溯源。要大图/精确尺寸/不外传就选本机；要 FLUX 风格且不想动本机就选匿名云服务；要求更贴提示词的成图选官方。
when-to-use: 任务需要配图、海报、图标、占位素材、纹理、概念图，或一批角色立绘 / 精灵表时；或已经生成过但图不对（竖条、纯色、风格没生效）需要一条可复现的路径时。
metadata:
  note: 只生成图像与（本机后端的）溯源文件，不修改代码；生成结果的内容质量不会被自动核对。
  backends:
    - chatgpt
    - pollinations
    - huggingface
    - local
invocation:
  kind: bridge
  bridge: scripts/generate.py
  batch: true
  timeout: 300
---

# 图像生成（四个后端，默认 FLUX）

用一段提示词生成图像，写成工作区文件，并把**真实**格式与尺寸报告回来。本技能只做这一件事；
把图像接进代码、写进文档或集成到父工作区，仍按项目的正常流程走。

> **四个后端都在这个执行体里**（`--backend` 可选 `pollinations` / `chatgpt` / `huggingface` / `local`），
> 实现只有这一份：FLUX 后端、官方后端、HF 后端与本机后端平级，不是一个单独的技能。

本技能**可通过端口调用**（`invocation.kind: bridge`），因此既能用下面的命令直接跑，
也能交给运行时执行——**多张图请走端口**：一次性提交全部提示词，运行时不阻塞任务循环。

## 何时用哪个后端

**默认是 FLUX**：不写 `--backend` 就是 `pollinations` + `--model flux`，不需要任何凭据。
想要真正"最强的 FLUX"（FLUX.1 dev 乃至更新的 FLUX.2）需要带凭据的路线，见下表。

| 后端 | 凭据 | 说明 |
|---|---|---|
| `pollinations` | **不需要**，匿名可用 | **默认后端，默认模型 `flux`。** 唯一无需凭据就能拿到 FLUX 的路。**分辨率封顶 ≈0.59 MP**（实测：768×768 或 1024×576，见下），做占位素材、概念图、小尺寸精灵足够，做大图不够。 |
| `huggingface` | 需要 HF token | 走 `router.huggingface.co/hf-inference`，模型 ID 必须形如 `owner/name`；默认 `black-forest-labs/FLUX.1-schnell`（4 步快版），要质量版填 `black-forest-labs/FLUX.1-dev`。**没有 token 会直接报错**（实测无 token 时该端点返回 401），不会静默降级。 |
| `chatgpt` | 复用本机 Codex 的 ChatGPT 登录态 | 官方 `gpt-image`（默认 `gpt-image-2`）。**分辨率与提示词贴合度通常高于匿名 FLUX**（实测给到 1024×1536）。走 `https://chatgpt.com/backend-api/codex/images/generations`，也就是 Codex 内置 `image_gen` 工具用的那条路。不需要 `OPENAI_API_KEY`；凭据只从 `~/.codex/auth.json` **只读**读取（`auth_mode` 必须是 `chatgpt`），不刷新、不回写——刷新令牌是单次有效的，脚本擅自轮换会让 Codex 那边失效。token 过期就报错，跑一次 `codex` 让它自己刷新。 |
| `local` | 不需要（本机） | 走 `sd-cli`（stable-diffusion.cpp）+ 磁盘上的权重。**离线、可复现、不外传、无分辨率上限**，也是唯一能跑任意 FLUX 权重（含 LoRA 风格）的一档。**已装好：FLUX.1-schnell Q8（Apache-2.0，可商用）**。开跑前读取权重结构、跑完记录画面统计量、并落一份溯源；统计量不用于自动判断内容质量或拒绝交付。先跑 `gpu` 技能的 `probe.py` 确认本机栈就绪。 |

需要 token 时按顺序取：`--token` 参数 → 环境变量 `HF_TOKEN`。**不要把 token 写进文件、报告或知识。**

选哪条：**要 FLUX 且要真尺寸 / 要离线 / 要不外传 / 要可复现** → `local`（本机已装好 FLUX.1-schnell，无分辨率上限）；
**要 FLUX 风格但不动本机** → `pollinations --model flux`（免费，但封顶 ≈0.59 MP）；
**要更贴提示词的成图**（立绘、海报、要给人看）→ `chatgpt`；
要 dev 级 FLUX 质量 → `huggingface`（要 token）或把 dev 的 gguf 放进本机 flux 目录（**非商用许可**）。
换了后端要在交付说明里写清换了哪条、为什么，不要悄悄换。

### FLUX 这一档的实测事实（2026-09-14）

1. **匿名档的模型清单是错的，而且错在"少报"。** `/image/models` 只广告 `sana`，
   但 `model=flux` 实际可跑。同 prompt 同 seed 比输出哈希：`flux` 与任何其它名字的结果都**不同**，
   而 `flux-realism` / `sana` / `turbo` 三者**逐字节相同** —— 未识别的名字会**静默回落**到同一个默认模型，
   而那个默认模型就广告成了 `sana`。所以：**只有 `flux` 是真的，其它名字一律当默认处理**。
2. **同 seed 同 prompt 输出可复现**（两次 `flux` 的结果哈希一致），所以 FLUX 这一档是唯一能进"可复现"流程的免费路线。
3. **匿名档有像素预算上限 ≈0.59 MP**：请求 768×768 / 1024×1024 / 1280×1280 / 1536×1536
   都拿回 **768×768**（四个请求的响应字节数完全相同，说明是同一张图被缩放），
   而 1280×720 拿回 **1024×576** —— 768² = 1024×576 = 589,824 px。
   即：**按你给的宽高比缩到预算内**。想要更大的图只能换后端，不能靠调 `--width`。
4. 因此 `--width` / `--height` 只表达**宽高比与上限**，不是承诺；结果里的 `width`/`height` 始终来自图像头解析。

### 官方 `chatgpt` 后端的实测事实（2026-09-14，ChatGPT Plus）

1. **`size` 是建议值，不是契约**：请求 `1024x1024`，三次分别拿回 `1370x1148`、`1254x1254`、`1024x1536`。
   所以结果里的 `width`/`height` 始终来自**图像头解析**，`chatgpt.requested_size` 才是请求值——
   要精确尺寸就自己后处理，不要相信请求参数。
2. **`quality` 也会被上游改写**：请求 `low` 拿回 `low` 也拿回过 `medium`；请求 `high` 拿回过 `low`。
   以响应里的 `chatgpt.quality` 为准，别把请求值写进交付说明。
3. **一次调用约 16–41 秒**。结果里带 `usage`（含图片 token 数）与 `generation_id`，
   所以这一批花了多少是**可核对的数字**，不是估计。

尺寸与质量的合法值：`size` 为 `auto` 或 `WxH`；`quality` 为 `low|medium|high|auto`；
`background` 为 `transparent|opaque|auto`（透明底优先直接向 `gpt-image-2` 要，别默认退回 `gpt-image-1.5`）。

## 基本用法

先解析本技能返回的 `Base directory for this skill:`（下称 `<base>`），所有相对路径都以它为基准。

```bash
# 1) 默认：FLUX（匿名，无需任何凭据）
python3 "<base>/scripts/generate.py" --prompt "a red fox sleeping in snow, soft morning light" --out ./images/fox.jpg

# 2) 要 FLUX 的其它档（需要 HF token）：schnell = 4 步快版，dev = 质量版
python3 "<base>/scripts/generate.py" --backend huggingface --model black-forest-labs/FLUX.1-dev \
  --prompt "isometric city block, warm palette" --out ./images/city.png

# 3) 要更大尺寸或更贴提示词的成图：官方 gpt-image
python3 "<base>/scripts/generate.py" --backend chatgpt --quality high \
  --prompt "a red fox sleeping in snow, soft morning light, photographic" --out ./images/fox-large.png

# 4) 固定宽高比与随机种子（FLUX 同 seed 同 prompt 可复现；注意匿名档会缩到 ≈0.59 MP）
python3 "<base>/scripts/generate.py" --prompt "flat vector app icon of a compass, minimal, centered" \
  --out ./assets/icon.png --width 512 --height 512 --seed 42

# 5) 本机离线出图（完整 checkpoint 填 --model；LoRA 走 --lora-dir + prompt 里的 <lora:…>）
python3 "<base>/scripts/generate.py" --backend local \
  --model /srv/unison-assets/models/sd15/GuoFeng3.4.safetensors \
  --prompt "guofeng style, a sword on silk, misty courtyard" \
  --out ./assets/sword.png --width 384 --height 512 --steps 6 --seed 74000

# 6) 查某个后端能用的"模型"（local 报权重结构事实，chatgpt 报登录态与档位，pollinations 报默认与上限）
python3 "<base>/scripts/generate.py" --list-models --backend local
python3 "<base>/scripts/generate.py" --list-models --backend chatgpt
python3 "<base>/scripts/generate.py" --list-models --backend pollinations
```

`--out` 省略时按提示词自动命名；`--out ./images/` 这样以斜杠结尾则在该目录下自动命名。

### 本机 FLUX：`--model <flux 目录>`（2026-09-14 实测跑通）

本机已经有完整的 FLUX.1-schnell（Apache-2.0，**可商用**），位于
`~/.cache/unison-gpu/models/flux/`（16 GB，**不在** `/srv/unison-assets` —— 那个盘只有 14 GiB 空闲）。

```bash
# 给目录即可：自动在同目录里找 clip_l / t5xxl / ae，挑出扩散模型本体
python3 "<base>/scripts/generate.py" --backend local \
  --model ~/.cache/unison-gpu/models/flux \
  --prompt "an ink-wash chinese landscape, distant mountains in mist, a single pine on a cliff" \
  --out ./assets/landscape.png --width 1024 --height 1024 --seed 4242
```

**FLUX 与 SD 的参数不一样，脚本按模型自动定档，不要沿用 SD 的值**：

| | SD15/SDXL | FLUX schnell | FLUX dev |
|---|---|---|---|
| `-m` / `--diffusion-model` | `-m <完整 checkpoint>` | `--diffusion-model` + 三件套 | 同左 |
| `--steps` | 6–8 | **4** | **20+** |
| `--cfg-scale` | 7.0 | **1.0** | **1.0** |
| `--guidance` | 不用 | **3.5** | 3.5 |
| 负向提示词 | 有效 | **无作用点**（FLUX 没有 CFG），脚本自动不发 `-n` | 同左 |

四条实测事实：

1. **16 GiB 显存装不下全套，必须把文本编码器放 CPU。** 实测总权重 15262 MB
   （text_encoders 2996 + diffusion_model 12106 + vae 160）会**全部**被搬进显存，
   只剩 ~1060 MB，而 VAE 解码要 ~2079 MB，于是 `model manager cannot make enough memory
   available` → `del compute failed`，一张都出不来。脚本对分离式模型的默认后端因此是
   **`diffusion=vulkan0,te=cpu`**：显存降到 12266 MB，正常出图。要用 `--device` 覆盖它，
   得自己承担显存核算。
2. **分离式模型不能填 `-m`。** FLUX 的 gguf 是单组件（只有扩散模型那部分），
   `-m` 是 SD 系列"一个完整 checkpoint"的用法。缺 t5xxl 就是缺文本编码器、缺 ae 就是缺 VAE，
   两者都会在加载阶段失败。脚本在开跑前检查四件套齐不齐，缺哪件直接说哪件。
3. **实测速度**：1024×1024 / 4 步 ≈ **36 s**；768×768 ≈ 26 s（含 ~10 s 模型加载）。
   采样本身约 3.6 it/s。
4. **本地这档没有分辨率上限**，这是它相对匿名 FLUX（封顶 ≈0.59 MP）的主要优势，
   也是相对官方 `chatgpt`（尺寸/质量会被上游改写）的主要优势 —— **只有本机能承诺精确尺寸**。

要更强的 FLUX：把 `--model` 换成 `black-forest-labs/FLUX.1-dev` 的 gguf 即可（放同一目录），
但 **dev 是非商用许可**（`license: other`），商用要单独授权；schnell 是 Apache-2.0。
FLUX 的 LoRA 走 `--lora-dir` + prompt 里的 `<lora:名字:1.0>`，与 SD 相同的写法。

### 本机后端：三条实测结论（都是踩过的）

1. **`--model` 在完整 checkpoint 上才成功**。只有 lora_ tensor 的文件会在加载阶段失败（`not in model metadata`）：
   实测代价是 200+ 行 `VAE/Diffusion model tensor ... not in model metadata`。
   LoRA 的正确用法是 `--lora-dir <目录>` + prompt 里写 `<lora:文件名去后缀:1.0>`。
2. **漏 `--lora-dir` 不会报错**，只会有一行 `WARN can not found lora`，出的是原图。
   所以本脚本会检查日志里有没有 `apply_loras completed`，没有就在结果里给 `warning`——
   看到 warning 就别声称"风格按要求生效了"。
3. **"命令成功 + 文件是 PNG + 尺寸对"不等于不是废图**（竖条、纯色、全灰都满足这三条）。因此结果里会带上画面统计量，供你判断。
   本机后端在统计工具可用时把测量数字写进 `check` 与溯源；工具不可用时给出 `warning`，不会假装量过。
   **统计是事实，不是内容质量判词**：不输出 `degenerate` / `verdict`，也不因纯色、竖纹或统计量高低把产物自动判为失败。

## 多张图：走端口，别在 shell 里串行跑

每张图要 1–3 秒（官方后端 16–31 秒），串行跑十张会占住整个任务循环（`workspace_shell` 是同步阻塞的）。
运行时的技能端点与模型调用同形：**提交请求 → 立即拿到 `job_id` → 轮询结果**。

```bash
# 一次提交多份输入（批量的 prompt 就是本技能的 args 列表）
curl -s -X POST http://127.0.0.1:8740/api/skills/call \
  -H "Authorization: Bearer <api_token>" -H 'Content-Type: application/json' \
  -d '{"skill":"image-generation","workspace":"'"$PWD"'",
       "batch":[{"prompt":"red fox in snow","out":"images/fox.jpg"},
                {"prompt":"blue compass icon","out":"images/icon.jpg","width":512,"height":512}]}'
# → {"id":"skilljob_…","status":"running","count":2,…}   窗口内完成则直接带 results

# 轮询（单次与批量同一个端点）
curl -s -H "Authorization: Bearer <api_token>" http://127.0.0.1:8740/api/skill/jobs/<job_id>
```

- 批量上限 **8 份**，超过会明确报错而不是静默截断；并发由运行时限制，作业状态持久化，重启后仍可查（中断的作业标记为 `interrupted`，不会重放）。
- 单次调用用 `"args": {...}` 而不是 `"batch"`；也可以给调用加 `"wait": 60` 让它等一会儿再返回。
- `args` 里的键与上面的命令行参数一一对应：`prompt` / `out` / `backend` / `model` / `width` / `height` / `seed`；官方后端另有 `size` / `quality` / `background` / `n` / `auth_file` / `turn_id`；本机后端另有 `steps` / `cfg_scale` / `lora_dir` / `lora` / `lora_weight` / `sd_cli` / `extra_args` / `provenance`。
- **一次调用用一个后端**（混用会被拒绝）：一批图的溯源才说得清。
- 桥接协议就是 stdin/stdout 的 JSON，本脚本两种入口共用实现，所以两条路径的结果完全一致。

```bash
# 本机后端批量：一次提交一批素材，并自动落溯源
curl -s -X POST http://127.0.0.1:8740/api/skills/call \
  -H "Authorization: Bearer <api_token>" -H 'Content-Type: application/json' \
  -d '{"skill":"image-generation","workspace":"'"$PWD"'",
       "batch":[{"backend":"local","prompt":"calm male swordsman, indigo robe","seed":74000,
                 "out":"assets/portraits/00.png","width":384,"height":512,"steps":6},
                {"backend":"local","prompt":"alert female saber fighter, crimson robe","seed":74001,
                 "out":"assets/portraits/01.png","width":384,"height":512,"steps":6}]}'
# → 结果里带 local.provenance（默认写在第一张图所在目录的 generation.json）
```

## 输出约定

成功时 stdout 是一行 JSON：

```json
{"ok": true, "path": "./images/fox.jpg", "bytes": 26096, "format": "jpeg",
 "width": 1024, "height": 1024, "backend": "pollinations", "model": null, "seed": 42, "seconds": 3.1}
```

官方后端（`chatgpt`）的结果另带 `chatgpt` 段——**上游实际用的尺寸/质量、用量与请求 id 都在这里**：

```json
{"ok": true, "path": "./images/fox.png", "bytes": 2778024, "format": "png",
 "width": 1536, "height": 1024, "backend": "chatgpt", "seconds": 16.06,
 "chatgpt": {"plan": "plus", "model": "gpt-image-2", "requested_size": "1024x1024",
             "reported_size": "1536x1024", "quality": "low", "output_format": "png",
             "usage": {"input_tokens": 29, "output_tokens": 343, "total_tokens": 372},
             "generation_id": "…", "request_id": "…"}}
```

本机后端的单张结果另带溯源与测量字段（节选；实际尺寸在 `check` 内）：
```json
{"ok": true, "backend": "local", "path": "assets/sword.png",
 "seed": 74000, "seconds": 1.7, "sha256": "…", "model_sha256": "…",
 "check": {"width": 384, "height": 512, "dominant_color": [120, 110, 100],
           "dominant_fraction": 0.12, "distinct_colors": 6300, "local_contrast": 0.08,
           "block_std": 0.15, "repeated_columns": 0.002, "measurements": null},
 "provenance": "assets/generation.json", "warnings": []}
```

`check` 中的主色、主色占比、抽样色数、局部对比度、8×8 块均值标准差、重复列占比
都是测量事实，不是质量评分；`measurements` 当前为 `null`，不表示通过或失败。
`ok: true` 表示执行成功，不表示画面符合提示词；内容是否合用仍需能看图的人或模型结合需求判断。

- `width` / `height` 来自**图像头解析**，不是请求参数回显；与请求不符时以上游实际结果为准。
- 失败时同样是一行 JSON，`ok` 为 `false`，带 `error`，必要时带 `hint`；退出码非零。
  本机后端部分失败时还会带 `partial`——**已经落盘的那几张与它们的溯源仍在里面**，
  如实报告它们，不要说"什么都没生成"。

## 生成之后必须做的事

1. **确认真实产出**：文件已按 `workspace_*` 规则进入任务文件清单——用 `workspace_diff` 或直接读取文件确认它存在、大小合理。
2. **不要声称"已核对内容"**：脚本无法判断图像是否符合需求。要核对就让能看图的模型/人来看；
   报告里的 `evidence` 只写你真正做过的事（例如"文件存在、格式 jpeg、1024x1024"）。
   本机后端额外记录**画面统计量**，不会自动排除纯色/竖纹，也没有“非退化即通过”的结论；
   测量值不能替代看图，更不能证明人物画对了。
3. **注意体积**：图像是二进制大文件，默认不进入文本 diff；需要给父任务带回时用 `workspace_integrate`，
   而不是把二进制内容粘进消息。
4. **失败要换策略**：匿名后端对不支持的模型名会报错，先 `--list-models`，或换更通用的提示词；
   连续失败就如实报告，不要反复重试同一请求刷爆上游配额。
   本机后端被拒绝执行时先看拒绝理由——多数是"把 LoRA 当主模型"（拿 `gpu` 技能的 `weights.py` 复核）。

## 提示词建议

- 明确四件事最有效：**主体 + 风格 + 构图/视角 + 光线或色彩**。
- 文字渲染不可靠：图像模型经常写错字，需要在图上出现准确文字时，先出背景图再叠加排版。
- 需要同系列多张图时固定 `--seed`，只改提示词中的一处变量。

复杂版式的可复用结构见 [`references/prompt-patterns.json`](references/prompt-patterns.json)（相对 `<base>` 解析）。

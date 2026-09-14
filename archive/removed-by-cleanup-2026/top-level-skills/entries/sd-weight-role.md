# 知识条目：权重角色先验，再决定它填哪个参数

**来源**：sword-demo（听雨剑庐）素材任务的真实事故 + 2026-09-13 在本机的复现实验。
**落点**：`unison/unison/skills/gpu/`（判别工具与排查顺序）、`unison/unison/skills/local-image-gen/`（生成时的强制前置校验）。

## 一句话

**"命令跑成功了"和"图是对的"是两件事。** 少一个参数时，本地生图往往**不报错**，
只是把 LoRA 当主模型跑、或者静默忽略 LoRA 风格——两次都能写出一个尺寸正确的 PNG。

## 事故现场

资产根的 `manifest.json` 把两个文件标成 `sd15-checkpoint 像素风`：

| 文件 | 大小 | 文件头事实 |
|---|---|---|
| `PixelArtRedmond15V-PixelArt-PIXARFK.safetensors` | 26 MB | 576 个 `lora_unet_*`，`modelspec.architecture=stable-diffusion-v1/lora` |
| `pixel_art_limbic.safetensors` | 310 MB | 172 个 `transformer.*`，LoRA/adapter |

照清单读，把它们喂给 `-m` 是"最自然"的动作。实测代价：

```text
[ERROR] diffusion_engine.cpp:900  get sd version from file failed
[ERROR] model_manager.cpp:759     VAE tensor 'first_stage_model.encoder...' not in model metadata   （261 行）
[INFO ] main.cpp:919              new_sd_ctx_t failed
```

实施记录里已经记过一次：探针任务用 `-m` 加载 `pixel_art_limbic`（那是 LoRA），
靠 `gpu` 技能里"别把 LoRA 当主模型"那条硬规则爬了出来——**但那条规则只说了"要小心"，
没给"怎么查"**。所以同一个坑在 sword-demo 里又被清单重新挖开。

## 本次补上的东西

1. `gpu/scripts/weights.py`：只读 safetensors 文件头（前 8 字节长度 + JSON），
   给出 `sd15-checkpoint` / `lora-adapter` / `partial-weights` / `unrecognized` 的判定、
   证据（tensor 前缀直方图、`modelspec.architecture`）与**"能填哪个参数"**；
   并与 `manifest.json` 的 `role` 对照，把不一致直接报出来。
2. `gpu/scripts/imagecheck.py`：出图后只读判定是否退化（纯色/竖纹/无结构），
   给出列均值方差、竖纹高频能量、颜色桶数、平坦像素占比。**"文件存在 + PNG + 尺寸对"
   不是它不是废图的证据**，这四个数字才是（虽然也只是必要条件）。
3. `local-image-gen` 技能：把"验权重角色 → 生成 → 核对输出 → 落溯源"绑成一条流水线，
   角色不对时**拒绝执行**（exit 1，不生成任何图片），而不是先跑 30 张再让人从废图里发现。

## 正确的调用形状（实测通过）

```bash
# 主模型：完整 SD1.5 checkpoint（unet 686 + vae 248 + clip 197 个 tensor）
sd-cli -m ~/.cache/unison-gpu/models/sd15/GuoFeng3.4.safetensors \
       --lora-model-dir ~/.cache/unison-gpu/models/sd15 \
       --backend vulkan0 \
       -p "pixel art swordsman <lora:PixelArtRedmond15V-PixelArt-PIXARFK:1.0>" \
       -W 256 -H 256 --steps 6 -o sprite.png
```

- 这个构建**没有** `--lora` 参数（写了会 `unknown argument: --lora`）。
- **漏掉 `--lora-model-dir` 不报错**：日志只有一行
  `[WARN] can not found lora <名字>`，图照出——**是原图**。
  判定"LoRA 是否真的生效"的唯一办法是看日志里有没有 `apply_loras completed`
  （本机实测生效时是 `576/576 LoRA tensors have been applied`）。
- `--backend vulkan0` 在本机与不写它**逐字节同图**（设备 0 就是 RX 9070 XT）——
  写它不是为了修 bug，是为了把"用了哪块卡"固化进命令。

## 可复用的判据（写给下一个 agent）

1. **清单/文档是声明，文件头/日志才是事实。** 两边不一致时以事实为准，并把不一致报出来。
2. 凡是"填哪个参数"的决定，先做一次**只读判别**（`weights.py`），不要靠文件名猜。
3. 凡是"生成成功"的结论，先做一次**只读核对**（`imagecheck.py`），并把数字写进报告；
   不允许用"我看了一眼觉得正常"当证据。
4. **静默失败比报错危险**：报错会让人去查；静默只会让废品流进交付物。
   所以每条生成命令都要有一个"能证明它真的按预期执行了"的日志锚点（如 `apply_loras completed`）。

---
name: gpu
description: 在本机 GPU 上跑推理前，先弄清"这台机器现在能用什么"——探测显卡与后端（Vulkan / ROCm / CUDA）、资产根里的权重与二进制、**逐文件验明权重角色（完整 checkpoint / LoRA / 分部权重，决定它能不能填 `-m`）**、独立 venv 的 torch 构建，并给出确切的调用命令；出图后再把画面的统计量量出来，供你判断结果是否退化。
when-to-use: 任务要在本地 GPU 上做图像/视频生成、放大、批量渲染或任何 GPU 计算；或需要判断"这台机器能不能跑某个模型"、"这个权重文件能不能直接 `-m`"；或生成的图看起来不对（竖条、纯色、全灰、形状都不对）需要定位原因时。
metadata:
  note: 只探测、判别与报告，不安装、不下载、不改动系统。安装是单独一步，需要人确认。
  kind: probe
  provides:
    - gpu_probe
    - weight_role_check
    - output_degeneracy_check
  compute_backends: vulkan, rocm, cuda, cpu
  assets_root: /srv/unison-assets
invocation:
  kind: bridge
  bridge: scripts/bridge.py
  batch: false
  timeout: 120
---

# 本地 GPU

**先探测，再动手。** 这台机器的 GPU 路线与"默认应该装 ROCm + PyTorch"的直觉不同：
实测在用的是 **stable-diffusion.cpp 的 Vulkan 后端**（只链 `libvulkan.so.1`，不需要 Python 张量栈）。
凭印象写 ROCm 安装命令，在这台机器上是白费功夫。

本技能**可通过端口调用**（`invocation.kind: bridge`）：

```bash
echo '{"skill":"gpu","workspace":"'"$PWD"'","base":"'"$PWD"'","args":{}}' | python3 scripts/bridge.py
```

直接运行则给人读的报告（探测是只读的，两种方式都不会改动机器）：

```bash
python3 scripts/probe.py           # 人类可读
python3 scripts/probe.py --json    # 机器可读
```

## 这台机器的事实（2026-09-13 实测，会变——用 probe 重新确认）

| 项 | 实测结果 |
|---|---|
| 独显 | **AMD Radeon RX 9070 XT**（Navi 48 / RDNA4 / `gfx1201`，RADV），16G 级显存 |
| 核显 | AMD Ryzen 9 7900X 的 Raphael——**uma: 1**，默认别用它（会拖慢、且可能被误选） |
| Vulkan 设备自报 | `fp16: 1`、`bf16: 0`、`matrix cores: KHR_coopmat`、`shared memory: 65536` |
| 计算后端 | **Vulkan 可用**（`radeon_icd.x86_64.json` + RADV）；ROCm **未安装**；CUDA 不适用 |
| 推理二进制 | `/srv/unison-assets/bin/sd-cli`、`sd-server`（stable-diffusion.cpp **master-859-7f410a3**，已构建） |
| 可用权重 | 见资产根的 `manifest.json`（sd1.5 国风/像素、Wan2.1 视频、两个放大器）——**清单是声明，文件头才是事实**，用 `weights.py` 复核 |
| Python 张量栈 | 当前解释器**没有** torch；`~/gaussian_splatting_task/venv` 是 `torch 2.14.0+cpu`（**CPU-only**） |

## 实测结论（先说清楚代价，再自己决定）

1. **权重、数据集、大缓存一律放资产根，不要放数据目录。** `.unison` 是**归档来源**，
   而且它的 `objects/` 会被 mark-and-sweep 回收（**没有保留期**）——刚产生但无人引用的对象，
   下一次非预演执行就会被删。资产根在外面，既不归档也不回收：
   `UNISON_ASSETS_ROOT` 可覆盖，默认 `/srv/unison-assets`（`models/` 放权重，`bin/` 放二进制）。
2. **任务工作副本是局部的**：工作区在运行结束/换目标时会被丢弃，别把权重留在里面。
3. **不要自动装东西**。ROCm / PyTorch / vLLM 这类安装动辄数 GB，且会改系统；
   先报告"缺什么、需要多大空间、预期多久"，等人确认再执行。
4. **读一遍权重的文件头，再决定它能填哪个参数**（实测：不先读就填错参数是出废图最省事的路径）：

   ```bash
   python3 scripts/weights.py /srv/unison-assets/models/sd15/*.safetensors   # 人类可读
   python3 scripts/weights.py --json <文件>...                                # 机器可读
   ```

   - 实测 `-m` 只在完整 checkpoint 上成功：`GuoFeng3.4.safetensors` 有 unet 686 + vae 248 + clip 197 个 tensor，所以可以直接 `-m`。
   - **判 LoRA 生效没有，看 `(N / M) LoRA tensors have been applied` 的 N，不要看 `apply_loras completed`**：
     后者在"0 个张量被应用"时照样打印。实测 `pixel_art_limbic` 全是 `(0 / 172)` 且与同 seed 基线
     **逐像素完全一致**（在 SD1.5 上完全空转），而 `PixelArtRedmond15V…` 是 `(576 / 576)`、与基线全图不同。
   - **LoRA 不能进 `-m`，也不能用 `--lora <文件>`**（这个构建根本没有 `--lora` 参数，会报 `unknown argument`）。
     正确姿势是 `--lora-model-dir <目录>` + prompt 里写 `<lora:文件名去后缀:1.0>`。
     实测资产根那两个"像素风"文件**都是 LoRA**（27 MB / 310 MB），却被 `manifest.json` 标成了
     `sd15-checkpoint`——照清单读就会踩坑。详见 [`references/failure-modes.md`](references/failure-modes.md)。

## 最小可用命令（实测过的）

```bash
# 生图（GuoFeng3.4 = SD1.5 写实/国风 checkpoint，完整可用）
/srv/unison-assets/bin/sd-cli \
  -m /srv/unison-assets/models/sd15/GuoFeng3.4.safetensors \
  --lora-model-dir /srv/unison-assets/models/sd15 \
  --backend vulkan0 \
  -p "guofeng style, a sword on silk" -W 512 -H 512 --steps 20 -o out.png

# 想用像素 LoRA 时：主模型仍是上面那个完整 checkpoint，LoRA 只出现在 prompt 里
/srv/unison-assets/bin/sd-cli -m <完整 checkpoint> \
  --lora-model-dir /srv/unison-assets/models/sd15 --backend vulkan0 \
  -p "pixel art swordsman sprite <lora:PixelArtRedmond15V-PixelArt-PIXARFK:1.0>" \
  -W 256 -H 256 --steps 6 -o sprite.png
```

- `--backend vulkan0` 选中独显（Vulkan 设备 0）。实测**不写它也是同一张图**（字节完全相同）：
  本机 Vulkan 设备 0 就是 RX 9070 XT、设备 1 是核显 Raphael，自动选择并没选错。
  写它的价值是**把这件事写进命令**，让别人（和将来的自己）不必重新验证——所以继续写。
- 实测速度：320×320/4 步约 1.8s；512×512/4 步约 2s；加 `--offload-to-cpu` 会翻倍（3.3s），
  显存够就别加。小图快测用 `-W 128 -H 128 --steps 2`。
- `--lora-model-dir` **单独给并不会加载任何 LoRA**（日志里 0 条 lora 记录）；它只是让
  prompt 里的 `<lora:…>` 找得到文件。所以样板命令里带上它是安全的幂等动作。
- **`GGML_VK_DISABLE_COOPMAT=1` 在本机实测是必要开关**（2026-09-13 定位，第一嫌疑）：不设它时 Vulkan coopmat 路径在这张 RDNA4 上算错，出图是**大面积同一块灰 + 窄竖条噪声**（主色占 63–81% 的像素，`128,121,11x`），文件只有 40–160 KB；设了以后同一命令同一 seed 就是正常图（主色 0.001、色数 32078、620 KB）。代价约 +13%（512×512/6 步 2.60s → 2.95s）。**先跑 `imagecheck.py` 拿前后数字再决定**，不要凭感觉加减开关。
- 其它命令（放大、网页版、Wan2.1 视频）见 `references/commands.md`；
  **出废图的排查顺序**见 `references/failure-modes.md`。

## 出图之后：先量数字，再谈内容

"命令退出码 0、文件存在、格式是 PNG、尺寸也对"——这四条**证明不了它不是一张废图**。
竖条、纯色、全灰的退化输出完全满足这四条（实测踩过）。所以生成后加一步只读测量：

```bash
python3 scripts/imagecheck.py out.png            # 单张
python3 scripts/imagecheck.py --json out.png     # 机器可读
python3 scripts/probe.py --check-output out.png  # 并进探测报告
```

它**只给数字**（主色占比、主色 RGB、抽样色数、相邻像素对比度、8×8 块标准差、重复列占比），
**不下结论**：这张图有没有画东西由你判断。文件头附了一组实测参考区间，那只是参考、
不是规则——判断时把几个量合起来看，别只盯一个。数字要进报告；"我看了一眼觉得还行"
不是证据。

## 该用本地还是走外部服务（判据，不是禁令）

两条路都正当。**先探测本地状态，再按下面的条件选，并在交付说明里写清选了哪条、为什么**——
尤其是中途从一条换到另一条时，必须说明，不能悄悄换。

**倾向本地**：

- 任务要求**离线/不外传**（素材敏感、数据不能出本机、明确禁止外部 API）；
- 要求**可复现**（固定 seed + 固定权重 + 固定二进制，结果可重建）；
- 外部服务给不了或不可靠（需要的风格/尺寸/视频能力没有、被限流、连不上）。

**倾向外部**：

- **本地算力已被占用**（`probe.py` 的 `gpu_load` 显示占用率高或显存吃紧）或**排队太久**——
  与其硬等，不如用外部跑掉，但要说明；
- 本地**缺对应的模型**（资产根里没有这类权重，而下载要数 GB、现在没有空间）；
- 外部**质量/能力明显更好**（例如需要写实长视频、文字排版、特定风格），且任务不禁止外部。

```bash
python3 scripts/probe.py --json | python3 -c "import json,sys; print(json.load(sys.stdin)['gpu_load'])"
```

`gpu_load` 里：`saturated` = 占用率≥20% 或显存≥60%（该考虑让路）；`busy_processes` 只表示
**有进程持有计算节点**（桌面常驻程序也会持有，不代表满载，别无脑当成"忙"）。
记住：**持有节点 ≠ 满载**，判据是占用率与显存用量。

## 汇报要求

探测完要回答的是**这台机器现在能做什么**，而不是罗列字段：

- 能用什么后端、哪些权重已就位、二进制在哪；
- **每个想用的权重是哪一类**（完整 checkpoint / LoRA / 分部权重），因此**能填哪个参数**；
  清单与文件头不一致时，以文件头为准并明确报出来；
- 想跑的目标缺什么、要装多大、装到哪（若涉及安装，给出方案而不是直接动手）；
- 显存与磁盘余量（装数 GB 的东西前必须先看盘）；
- 已经出过图的：跑 `imagecheck.py`，把**数字**写进报告，不要只说"看起来正常"。

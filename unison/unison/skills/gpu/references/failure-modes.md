# 出废图的排查顺序（实测记录）

本机：AMD RX 9070 XT（Navi 48 / RDNA4 / gfx1201，RADV）+ Ryzen 9 7900X 核显（Raphael）。
后端：stable-diffusion.cpp `master-859-7f410a3`，`--backend vulkan0`。
资产根：`/srv/unison-assets`（`UNISON_ASSETS_ROOT` 可覆盖）。
下面每条都在这台机器上真的跑过，命令与结论可以直接复用。

## 第零问：coopmat 关了吗（**本机第一嫌疑，2026-09-13 定位**）

**症状**：命令 rc=0、日志正常、PNG 写出来了、尺寸也对，但画面是
**大面积同一块灰**（实测主色占 63–81% 的像素，取值 `128,121,11x`——零潜变量解码出来的那种灰），
其余部分像**窄竖条噪声**，没有主体。文件体积明显偏小（512×512 约 40–160 KB，
正常图 600 KB 量级）。

**根因**：Vulkan 的 **coopmat（KHR_coopmat 矩阵核）路径在这张 RDNA4 卡上算错**。
关掉它，同一命令、同一 seed 立刻出正常图——**这是本机影响最大的一条**。

```bash
export GGML_VK_DISABLE_COOPMAT=1     # 这就是"少的那一个设置"
```

实测对照（`guofeng style, a sword on silk`，6 步）：

| 配置 | 主色占比 | 抽样色数 | 局部对比度 | 8×8 块std | 文件大小 | 判定 |
|---|---|---|---|---|---|---|
| 默认（coopmat 开） | **0.79** | 1 622 | 0.026 | 0.021 | 40 KB | **废图** |
| `GGML_VK_DISABLE_COOPMAT=1` | **0.001** | 32 078 | 0.036 | 0.18 | 620 KB | 正常 |

代价：512×512/6 步 **2.60s → 2.95s**（约 +13%）。**这点代价换回可用的图，必须关。**

**为什么之前没发现**：第一版 `imagecheck.py` 只看"列均值竖纹"和"16 级颜色桶数"，
按列平均恰好把"几乎所有像素都一样"这件事平均掉了，于是把废图判成"未发现退化特征"。
现在的判定改成按**像素本身**取值（主色占比 > 0.40 即废图，见 `imagecheck.py` 的阈值表）。

注意：`GGML_VK_DISABLE_COOPMAT=1` 与文档里其他"别乱加开关"的建议**不冲突**——
它是修复，不是猜测；判断标准是**有没有先跑 `imagecheck.py` 拿到前后数字**。

## 第一问：权重填对参数了吗（第二常见，也最容易骗过人）

**症状**：命令在跑、日志很长、最后**还写出了文件**，但图是废的（竖条、纯色、结构错乱）。
**根因**：把一个**不是完整 checkpoint** 的文件喂给了 `-m`。

实测两种真实故障：

| 文件 | 清单里的声明 | 文件头事实 | 喂 `-m` 的结果 |
|---|---|---|---|
| `models/sd15/PixelArtRedmond15V-PixelArt-PIXARFK.safetensors`（26 MB，576 tensor 全是 `lora_unet_*`，`modelspec.architecture=stable-diffusion-v1/lora`） | `sd15-checkpoint 像素风(PIXARFK)` | **LoRA 适配器** | `get sd version from file failed`，失败 |
| `models/sd15/pixel_art_limbic.safetensors`（310 MB，172 个 `transformer.*`） | `sd15-checkpoint 像素风` | **LoRA / adapter** | 261 行 `VAE tokenizer/diffusion tensor ... not in model metadata`，失败 |

也就是说：**`manifest.json` 的 `role` 字段是声明，不是事实**。资产根里那两个"像素风 checkpoint"
实际都是 LoRA；照清单读就会把它塞进 `-m`，然后拿到一堆 `... not in model metadata`。

```bash
# 先用文件头验明角色（只读，毫秒级）
python3 scripts/weights.py /srv/unison-assets/models/sd15/*.safetensors
python3 scripts/probe.py --weights
```

判别规则（`weights.py` 实现）：`lora_*` 前缀占比 > 50%，或 metadata 写着 `.../lora`，
就是 LoRA；`model.diffusion_model.*` + `first_stage_model.*` + `cond_stage_model.*` 齐全才是完整 SD1.5。

**正确用法**：`-m` 永远是完整 checkpoint，LoRA 只出现在 prompt 里：

```bash
GGML_VK_DISABLE_COOPMAT=1 sd-cli -m /srv/unison-assets/models/sd15/GuoFeng3.4.safetensors \
       --lora-model-dir /srv/unison-assets/models/sd15 \
       --backend vulkan0 \
       -p "pixel art swordsman <lora:PixelArtRedmond15V-PixelArt-PIXARFK:0.9>" \
       -W 512 -H 512 --steps 6 -o sprite.png
```

`--lora-model-dir` **单独给不会加载任何 LoRA**；prompt 里没有 `<lora:…>` 时日志里 0 条 lora 记录。

### 判"LoRA 到底生效没有"：**别信 `apply_loras completed`**

2026-09-13 被一次独立验收 run 抓出两处错，两处都改了：

1. **`apply_loras completed` 在"一个张量都没应用"时照样打印**——这个信号没有判别力。
   真正有信息量的是同一段日志里的计数行
   `(N / M) LoRA tensors have been applied`。判定要取**整段日志里的最大值**，
   因为一条命令会打印多行（分段/卸载重载），只看最后一行会误判。
2. **`(0 / M)` 才是真的没生效**。本机实测两个"像素风 LoRA"：

| LoRA | 计数行 | 与同 seed 基线的像素差 | 结论 |
|---|---|---|---|
| `PixelArtRedmond15V-PixelArt-PIXARFK` | `(0/576) → (576/576) → (0/576)` | 平均差 **22.7**，全图不同 | **确实生效** |
| `pixel_art_limbic` | 全是 `(0/172)` | **0.000，逐像素完全一致** | **没生效**（与本机 SD1.5 不匹配） |

`pixel_art_limbic` 的真实身份是一份 `transformer.*` 前缀的 adapter（172 个张量），
不是 SD1.5 的 `lora_unet_*` 结构——**它在 sd-cli + SD1.5 这条链路上是空转的**。
要坐实"没变"，唯一的办法是**同一 seed 跑一次不带 LoRA 的基线然后逐像素比**。

`local_image.py` 的判定已按上面改：输出 `lora_tensors_applied_max` 与 `lora_tensor_counts`，
`applied_max == 0` 时直接给 warning，不再用弱信号。

注意这个构建**没有 `--lora` 参数**（写了会 `unknown argument: --lora` 并以 rc=1 退出，help 里只有
`--lora-model-dir`）。少了参数不会报"你漏了 LoRA"，只会静默地不加载：
`<lora:…>` 找不到文件时只有一行 `[WARN] can not found lora <名字>`，图照出——
**是原图，不是你要的风格**。这就是"少一个参数"最阴的形态：不失败，只是结果不对。

## 第二问：这个二进制到底认不认这个参数

`sd-cli` 对不认识的长选项会**打印整篇 help**，而且退出码不统一（实测：未知参数 rc=1；
某些加载失败路径 rc=0）。所以：

- **不要用 `sd-cli --help | grep` 的命中与否判断参数是否存在**——命中 help 文本也可能来自报错回显。
- 真正判断方式是看**日志里有没有该参数的生效记录**（例如 LoRA 要看到
  `apply_loras completed` 与 `576/576 LoRA tensors have been applied`）。
- `--no-fa` 在 `master-859` 上**不存在**（只有 `--fa` / `--diffusion-fa`）；写它等于命令没跑成。
- 别把可能打印 help 的输出直接重定向到镜像文件路径：help 文本会被当成"成功产物"留着。

## 第三问：是不是别的后端/精度问题（coopmat 已经在第零问处理）

在这一版上，以下因素**实测都不改变结果**（同一 seed 逐字节相同的 PNG）：

| 变量 | 命令 | 结果 |
|---|---|---|
| 显式选独显 | `--backend vulkan0` | 与**不写**它输出逐字节相同（设备 0 本来就是 RX 9070 XT） |
| 省显存 | `--auto-fit off` | 同一张图（**在 coopmat 开与关两种状态下都测过**） |
| 权重放 RAM | `--offload-to-cpu` | 同一张图，但慢一倍（3.3s vs 1.8s） |
| 只给 LoRA 目录 | `--lora-model-dir <目录>`（prompt 里没有 `<lora:…>`） | 同一张图，日志 0 条 lora 记录 |

以下因素**会让同一 seed 出不同的图**（要做"可复现"就别动它们）：

| 变量 | 命令 | 影响 |
|---|---|---|
| **coopmat** | `GGML_VK_DISABLE_COOPMAT=1` | **决定性**：从废图变成正常图（第零问） |
| flash attention | `--fa` / `--diffusion-fa` | 图变（数值路径不同），未出现废图 |
| 注意力缩放 | `--fa --attn-scale 0.5` | 图变 |
| 随机数源 | `--rng std_default` | 图变 |
| VAE 精度 | `--tensor-type-rules "^vae\.=f16"` | 图变（列方差明显变大） |
| VAE 分块 | `--vae-tiling` | 图变（本机未出现接缝废图） |

**结论（2026-09-13 修正）**：这台 RDNA4 机器的废图**第一嫌疑就是 coopmat 没关**，
第二嫌疑是权重角色与参数不匹配。`--fa` 可以留着。
后端 A/B 的次序是：①`GGML_VK_DISABLE_COOPMAT=1` ②`--backend <模块>=cpu`（实测把 VAE 放 CPU
时**整张图变成纯白**，所以模块级分配也要复核，别以为"拆开更稳"） ③`--backend cpu` 整条 CPU 路线
（能出图但慢，且 `imagecheck` 仍可能报"死平"，务必看数字）。

**每一次都要跑 `imagecheck.py` 留下数字**——这次事故的教训就是"看起来正常"骗过了检查器本身。

## 第四问：结果本身是不是退化输出

```bash
python3 scripts/imagecheck.py out.png
# out.png: 512x512 ch=3 主色占比=0.001 色数=32078 对比度=0.0363 块std=0.18
#   结论：未发现退化特征（这不等于内容正确）
```

判定的四个数字（阈值写在脚本里，改脚本比改文档可靠）：

| 指标 | 废图特征 |
|---|---|
| `dominant_fraction` 主色占比 | **> 0.40** ⇒ 大面积单色（本机正常图 0.001–0.33） |
| `local_contrast` 相邻像素平均差 | < 0.010 ⇒ 死平 |
| `block_std` 8×8 块均值标准差 | < 0.020 ⇒ 没有结构（正常图 0.09–0.28） |
| `distinct_colors` 抽样色数 | < 24 ⇒ 近乎单色（正常图 3000–32000） |
| `repeated_columns` 与左邻列几乎相同的列占比 | > 0.5 且对比度低 ⇒ 竖纹/填充伪影 |

它**不能**替你判断人物画得对不对。在报告里能写的是"文件存在、PNG、512×512、
`imagecheck`：主色占比 0.001、色数 32078、块std 0.18、无退化特征"；
**不能**写"图像内容已核对"——那需要真的看图（本机模型若没有视觉输入，就明说"没有做视觉核对"）。

## 一条可复现的最小验证（约 5 秒）

```bash
S=/srv/unison-assets/bin/sd-cli
M=/srv/unison-assets/models/sd15/GuoFeng3.4.safetensors
$S -m $M --backend vulkan0 -p "guofeng style, a sword on silk" \
   -W 256 -H 256 --steps 4 --seed 74000 -o /tmp/smoke.png
python3 scripts/imagecheck.py /tmp/smoke.png
```

权重的 sha256 可以用资产根 `manifest.json` 里的值核对（实测 `GuoFeng3.4` 与清单一致：
`a83e25fe5b70bad595fe4dd6733ee35f0e3ddf8ed4041ab360f9573556e8b3e6`）。

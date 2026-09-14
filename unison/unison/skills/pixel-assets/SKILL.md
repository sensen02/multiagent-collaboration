---
name: pixel-assets
description: 把生成图变成**真正的像素素材**：指定目标像素网格（如 32×48）降采样、映射到受限调色板（DB16/PICO-8/GameBoy/NES 或自动取色）、去碎点、加描边，输出原生网格图 + 最近邻放大预览 + 可核对的 JSON；还能把逐帧拼成**动画精灵表**并写契约清单（行列顺序、格子尺寸、每帧哈希），以及反向切片核对表是否真按行列排布。扩散模型出的是"像素风插画"，像素大小不匀、边缘抗锯齿、颜色上千——这一步是补上"确定性的像素化"，不该指望模型。
when-to-use: 任务需要游戏用像素素材时（角色帧、图标、道具、瓦片）；已经用像素风 LoRA 出了图但"看起来糊/不够像素"时；需要动画精灵表或有严格行列契约的图集时；像素素材要与现有素材对齐规格（格子尺寸、调色板、透明背景）时。
metadata:
  note: 只做确定性的像素化与拼表/切片，不改颜色风格本身，也不判断美术好不好。
  dependencies: 纯标准库（自带 PNG 编解码，不需要 Pillow）
  outputs:
    - native_grid_png
    - nearest_neighbor_preview_png
    - sprite_sheet_png_and_json
---

# 像素素材：像素化 + 精灵表

## 为什么需要这一步

用像素风 LoRA 出图**不等于**得到像素素材。扩散模型的输出是"像素风插画"：

- 像素**大小不均匀**（同一张图里有的"像素"占 6 个真实像素、有的占 9 个）；
- 边缘**抗锯齿**（每个边界像素都是渐变色，缩放就糊）；
- 颜色**上千种**（受限调色板才是像素画的语言）。

这三件事都是**确定性问题**，用确定性代码解决；指望模型"自己排整齐"是白费力气。
本技能的流程是：源图 → 面积平均降采样到目标网格 → 映射到调色板 → 去碎点 → 可选描边 →
输出原生网格图（1 像素 = 1 像素）与最近邻预览。

## 1. 像素化

```bash
# 角色帧：32×48 格、DB16 调色板、加描边、预览放大 8 倍
python3 "<base>/scripts/pixelize.py" frame.png --grid 32x48 --palette db16 --outline \
    --preview-factor 8 --out-dir px

# 自动取色（median-cut，10 色），不加描边
python3 "<base>/scripts/pixelize.py" icon.png --grid 24x24 --palette auto --colors 10 --out-dir px
```

| 参数 | 说明 |
|---|---|
| `--grid WxH` | **目标像素网格**，这就是"这张图由多少个像素组成"。角色常用 16×24 / 24×32 / 32×48；图标 16×16 / 24×24 |
| `--palette` | `db16`（推荐起点）、`db32`、`pico8`、`gameboy`、`nes`、`bw`、`auto`（median-cut）、`none` |
| `--colors` | `--palette auto` 时的目标色数 |
| `--alpha-threshold` | 认为"透明"的 alpha 阈值（默认 128） |
| `--outline` | 给不透明区域加 1 像素描边（像素画常见做法） |
| `--preview-factor` | 最近邻预览的放大倍数（0 = 不生成） |

输出：`<名>_px{W}x{H}.png`（原生网格）、`<名>_px{W}x{H}_x{N}.png`（预览）、
以及一行 JSON（用了多少色、裁剪比、透明像素数）。

**选网格的经验**：先看源图尺寸。`--grid` 与源图的比例应在 8–20 倍之间
（脚本会把这个比例算进 JSON 的 `scale_ratio`）。比例太小 → 保留太多抗锯齿；
太大 → 细节全丢。384×384 的源图配 32×48 大约是 12×8 倍，实测可用。

## 2. 拼动画精灵表（带契约清单）

```bash
python3 "<base>/scripts/sprite_sheet.py" \
    --frames f1_px32x48.png f2_px32x48.png f3_px32x48.png f4_px32x48.png \
    --rows walk0,walk1,walk2,walk3 --columns right \
    --cell 32x48 --out sheet_walk.png
```

- **帧数必须等于 `行数 × 列数`**，顺序是"行优先"。数目不符会直接报错而不是硬拼
  ——拼错的表在游戏里极难查。
- 尺寸不一致的帧按 `--anchor`（默认 `bottom`，脚底对齐，适合角色）放进格子；
  超出的按锚点裁剪并在 `notes` 里记一笔。
- 同时写 `sheet_walk.sheet.json`：格子尺寸、行列名、每帧来源与 **sha256**、契约说明。
  **代码按这份清单取值，不要靠猜。**

## 3. 反向切片（核对用）

```bash
python3 "<base>/scripts/sprite_sheet.py" --slice sheet_walk.png --rows 4 --columns 1 \
    --out-dir sliced
```

表尺寸不能被行列数整除时会报错——这本身就是有用的信号（说明表不是规则的）。

## 报告里该写什么

写**数字与路径**：网格尺寸、用色数、`scale_ratio`、输出文件名、精灵表的行列与帧数。
不要写"像素画看起来不错"——那需要能看图的人/模型来确认；
本技能与 `gpu` 技能的 `imagecheck.py` 都只保证**形状与结构**，不判断美术质量。

## 与其它技能的衔接

```text
image-generation（--backend local，自带 coopmat 修复）
        ↓ 逐帧出图
pixel-assets（pixelize → sprite_sheet）      ← 本技能
        ↓ 像素素材 / 动画表
theed（单图 → 3D 网格）或直接交给游戏引擎
```

**出图那一步必须先确保图不是废图**：本机 coopmat 未关时产物是"大面积同一块灰 + 窄竖条噪声"，
像素化之后会得到"用色 2 色"的垃圾（实测踩过）。判定用 `gpu` 技能的 `imagecheck.py`。

# 命令参考（本机实测，2026-09-13）

所有路径基于资产根 `/srv/unison-assets`（可用 `UNISON_ASSETS_ROOT` 覆盖）。
二进制：`bin/sd-cli`（单次）、`bin/sd-server`（常驻/网页版）。

## 权重清单

| 用途 | 路径 | 状态 |
|---|---|---|
| SD1.5 国风/写实主模型 | `models/sd15/GuoFeng3.4.safetensors` | 2.1G ✅ 实测出图 |
| SD1.5 像素风 LoRA | `models/sd15/pixel_art_limbic.safetensors` | 310M |
| SD1.5 像素风 LoRA | `models/sd15/PixelArtRedmond15V-PixelArt-PIXARFK.safetensors` | 26M |
| 放大（ESRGAN） | `models/upscalers/4x-UltraSharp.pth`、`RealESRGAN_x4.pth` | 各 64M ⚠️ 未实测 |
| Wan2.1 文生视频 1.3B | `models/wan21/diffusion/wan2.1_t2v_1.3B_bf16.safetensors` | 2.7G ⚠️ 未实测 |
| Wan2.1 VAE | `models/wan21/vae/wan_2.1_vae.safetensors` | 242M ⚠️ 未实测 |
| Wan2.1 文本编码器 | `models/wan21/text_encoder/umt5-xxl-encoder-Q4_K_M.gguf` | 3.5G ⚠️ 未实测 |

每个权重的 sha256 与上游下载地址在资产根的 `manifest.json` / `upstream-downloads.txt`。

## 1. 单张生图（实测通过）

```bash
BIN=/srv/unison-assets/bin/sd-cli
MODELS=/srv/unison-assets/models
$BIN -m $MODELS/sd15/GuoFeng3.4.safetensors \
     --lora-model-dir $MODELS/sd15 \
     --backend vulkan0 \
     -p "国风, 一柄剑置于丝绸之上, 简洁背景" \
     -W 512 -H 512 --steps 20 -o out.png
```

产出 PNG 的尺寸应当是 `-W × -H`，**要核对**（失败时可能留下旧文件或空文件）。

## 2. 快速冒烟（几秒，确认 GPU 通路还活着）

```bash
$BIN -m $MODELS/sd15/GuoFeng3.4.safetensors --backend vulkan0 \
     -p "test" -W 128 -H 128 --steps 2 -o /tmp/smoke.png
```

## 3. 放大（ESRGAN，未实测）

模型用 `models/upscalers/4x-UltraSharp.pth`；`sd-cli` 的放大标志以 `--help` 为准
（不同版本会变），先用小图验证再上批量。

## 4. 网页版常驻服务（未实测）

```bash
/srv/unison-assets/bin/sd-server \
  -m /srv/unison-assets/models/sd15/GuoFeng3.4.safetensors \
  --lora-model-dir /srv/unison-assets/models/sd15 \
  --backend vulkan0 --listen-port 1234
```

浏览器访问 `http://127.0.0.1:1234`。原脚本还传 `--serve-html-path`，指向已被删除的
`stable-diffusion.cpp/examples/server/frontend/dist/index.html`；不传则用内置前端。

## 5. Wan2.1 文生视频（未实测）

三件套齐了但**没实测过**。要点：diffusion / vae / 文本编码器要分别指定
（`--diffusion-model` / `--vae` / 文本编码器参数，以 `sd-cli --help` 为准）；
1.3B + Q4 编码器显存需求不高，但**耗时远大于生图**——先跑 1~2 帧验证通路。

## 6. 查看设备与能力

```bash
vulkaninfo --summary     # 设备名/类型；独显应显示 RADV GFX1201，核显 uma: 1
ls /dev/dri              # renderD128/129 才是计算节点
```

## 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `VAE tensor ... not in model metadata` + `new_sd_ctx_t failed` | 用 `-m` 加载了 LoRA | 改用 `--lora-model-dir` |
| 每步几十秒 | 落到核显（uma: 1）或 `llvmpipe` | 显式 `--backend vulkan0`，用 `vulkaninfo` 核对 |
| 画面异常/崩 | coopmat 在该驱动上不稳 | 试 `GGML_VK_DISABLE_COOPMAT=1` |
| 加载即 OOM | 分辨率/权重太大 | 降分辨率或步数；查 `--max-vram` |
| 找不到模型 | 路径还是旧桌面目录 | 权重在 `/srv/unison-assets/models/` |

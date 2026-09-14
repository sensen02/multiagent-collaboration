---
name: pollinations
description: 基于 Pollinations.AI 免费云端 API 快速生成图像并落盘。无需任何 API Key，支持多种模型（flux, turbo 等），支持自定义宽高、随机种子与去水印。支持 CLI 命令行直出与 Unison 运行时异步桥接调用。
when-to-use: 需要通过免费无鉴权的云端 API 快速生成配图、场景概念图、角色素材、图标或批量纹理占位图时使用。
metadata:
  note: 免费免密云端图像生成（基于 Pollinations.AI）
  backends: [pollinations]
  provides: [cloud_image_gen, free_image_api]
invocation:
  kind: bridge
  bridge: scripts/bridge.py
  batch: true
  timeout: 180
---

# Pollinations 免费云端图像生成技能

本技能封装了 [Pollinations.AI](https://pollinations.ai) 的免费图像生成接口。
零第三方依赖（纯 Python 标准库），不需要配置任何 API Key 或 Token，开箱即用。

---

## 1. 核心特性

- **完全免密**：无需注册或填写 API Token，随时随地开箱即用。
- **高质量模型**：默认支持最新的 `flux` 系列（Flux.1 Schnell）与极速 `turbo` 等模型。
- **真实文件校验**：脚本内建二进制嗅探（`sniff`），自动校验响应头魔数（JPEG / PNG / WebP / GIF），记录真实宽高与体积，不盲目信赖 URL 传参。
- **双模调用**：既能通过命令行直接运行，也能通过 Unison 桥接端口（`/api/skills/call`）提交单次或批量任务，支持任务轮询。

---

## 2. 命令行使用方法

所有路径相对于技能基目录 `<base>`。

### 1) 基本生成

```bash
python3 "<base>/scripts/generate.py" --prompt "a beautiful chinese ink painting of misty mountains and solitary pine tree" --out ./assets/mountains.jpg
```

### 2) 指定尺寸、模型与随机种子

```bash
python3 "<base>/scripts/generate.py" \
  --prompt "cyberpunk street market at midnight, neon reflections, rain, highly detailed" \
  --out ./assets/cyberpunk.png \
  --width 1280 \
  --height 720 \
  --model flux \
  --seed 12345
```

### 3) 查询当前支持的模型列表

```bash
python3 "<base>/scripts/generate.py" --list-models
```

---

## 3. Unison 运行时端口调用（Bridge 模式）

本技能声明了 `invocation.kind: bridge` 与 `batch: true`，可通过 Unison HTTP API 进行单次或批量异步调用。

### 单次请求

```http
POST /api/skills/call
Content-Type: application/json

{
  "skill": "pollinations",
  "workspace": "/home/sensen/Desktop/my-project",
  "args": {
    "prompt": "traditional wuxia swordmaster in black robes standing in bamboo forest",
    "out": "assets/hero.jpg",
    "width": 768,
    "height": 1024,
    "model": "flux"
  }
}
```

### 批量并发请求

一次最多提交 8 份任务（受 `MAX_BATCH` 保护）：

```http
POST /api/skills/call
Content-Type: application/json

{
  "skill": "pollinations",
  "workspace": "/home/sensen/Desktop/my-project",
  "batch": [
    {
      "prompt": "pixel art hero warrior sprite sheet, 8-directional, transparent background",
      "out": "sprites/hero.png",
      "width": 512,
      "height": 512
    },
    {
      "prompt": "pixel art monster boss sprite sheet, dark fantasy style",
      "out": "sprites/boss.png",
      "width": 512,
      "height": 512
    }
  ]
}
```

---

## 4. 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `prompt` | string | **必填** | 画面英文描述，建议包含主体、画风、环境光影等细节 |
| `out` | string | 自动命名 | 输出相对/绝对文件路径；省略时以提示词 slug + 时间戳命名 |
| `width` | int | 1024 | 图像宽度（像素） |
| `height` | int | 1024 | 图像高度（像素） |
| `seed` | int | 随机 | 随机种子，固定数值可复现相近构图 |
| `model` | string | `flux` | 模型名称，可选 `flux`, `turbo`, `flux-realism`, `flux-anime` 等 |
| `timeout` | float | 120.0 | HTTP 超时时间（秒） |

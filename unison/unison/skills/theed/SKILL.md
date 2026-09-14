---
name: theed
description: 用**单张图**重建 3D 网格（GLB/OBJ）并交付，走 TripoSR 前馈重建，**在 AMD RDNA4（A 卡）上真的能跑**——因为把 CUDA-only 的 `torchmcubes` 换成了纯 CPU 的 marching cubes 替身（不改上游一行代码），并在开跑前把输入图的画面统计量一并交付（实测：废图进去会得到废网格，而上游不报错）。要批量/高质量 3D、或需要 3D 高斯泼溅与视频级重建时另找他路。
when-to-use: 任务需要 3D 资产（角色、道具、武器的低模 + 贴图，GLB/OBJ 交付）时；已有 2D 概念图/立绘/精灵想变成可导入引擎的模型时；或"3D 生成在 A 卡上跑不起来"需要一条已验证通路时。
metadata:
  note: 只做图像→网格的重建与文件交付；不做绑定、不做骨骼动画、不保证背面拓扑（单图重建的固有限制）。
  device: cpu（本机唯一确定可用的路径）
  upstream: TripoSR (stabilityai/TripoSR, MIT)
  provides:
    - image_to_mesh
    - cuda_free_3d
invocation:
  kind: bridge
  bridge: scripts/bridge.py
  batch: false
  timeout: 900
---

# 图像 → 3D（A 卡可用）

**先读这段，否则会在同一个地方卡住两次。**

## 为什么 AI 3D 在这台机器上是"依赖问题"而不是"算力问题"

TripoSR 的 `tsr/models/isosurface.py` 第一行是 `from torchmcubes import marching_cubes`。
`torchmcubes` 是**带 CUDA 的 C++ 扩展**，在 ROCm/CPU 环境里 pip 直接装不上
（实测：`Encountered error while generating package metadata`，连 wheel 都构建不出来）。
于是 pipeline 在 **import 阶段**就死——显卡一点没参与。

修法：`scripts/torchmcubes_shim/` 用 `sitecustomize.py` 在**导入路径上**把 `torchmcubes`
换成 `skimage` 的 CPU marching cubes：

```text
scripts/torchmcubes_shim/
  sitecustomize.py    # Python 启动时自动导入；发现真 torchmcubes 不在就把替身注册进 sys.modules
  torchmcubes.py      # 与 torchmcubes.marching_cubes 同形：入 (volume, thresh)，出 (vertices, faces)
```

同形是关键：**顶点用体素索引坐标（0..N-1，不是归一化 0..1）**，这样才能与上游
`v_pos / (self.resolution - 1.0)` 对上；`faces` 是 int64。空表面返回 `(0,3)` 空张量而不是抛错。

**少一个环境变量就等于没修**：`PYTHONPATH` 没带这个目录，症状还是 `ModuleNotFoundError`。
所以 `scripts/run_3d.py` 自己拼环境再调上游——调用者不需要记这件事（脚本还会让 shim
自报是否生效，结果写进返回值的 `torchmcubes` 字段）。

## 用法

```bash
# 只体检输入图（快，先做这一步）
python3 "<base>/scripts/run_3d.py" --image frame.png --check-only

# 重建（CPU；分辨率越低越快）
python3 "<base>/scripts/run_3d.py" --image frame.png --out-dir out \
    --device cpu --mc-resolution 128 --format glb
```

端口调用（推荐给运行时，避免长时间占住任务循环）：

```bash
curl -s -X POST http://127.0.0.1:8740/api/skills/call \
  -H "Authorization: Bearer <api_token>" -H 'Content-Type: application/json' \
  -d '{"skill":"theed","workspace":"'"$PWD"'",
       "args":{"image":"frames/f1.png","out_dir":"threed","mc_resolution":128,"format":"glb"}}'
```

参数：

| 键 | 默认 | 说明 |
|---|---|---|
| `image` | 必填 | 工作区相对路径的**单张**图 |
| `out_dir` | `threed_out` | 网格输出目录 |
| `device` | `cpu` | 本机唯一确定可用的路径；`cuda` 在这张 A 卡上不存在 |
| `mc_resolution` | 128 | marching cubes 分辨率。**这是速度旋钮**：128 已需数分钟，先 64 试通路 |
| `format` | `glb` | `obj` 已实测通过；`glb` 走 xatlas 纹理烘焙，无 GPU 环境可能失败——**先用 obj 验证通路** |
| `foreground_ratio` | 0.85 | 前景占比；主体太小/太大时调它 |

## 输入图的质量会直接决定网格质量（先量数字，再自己决定）

TripoSR 是单图前馈重建：喂一张"大面积纯色 + 窄竖条噪声"的废图（本机 coopmat 未关时的
典型产物），它**不报错**，只是给你一个废网格。所以本技能开跑前调用 gpu 技能的
`imagecheck.py` 量一遍输入图的画面统计量并随结果交付；若数字显示结构很少，第一嫌疑是
`GGML_VK_DISABLE_COOPMAT=1` 没设）。

推荐的完整链路（像素素材 → 3D 参考 → 网格）：

```bash
# 1) 出图：本机后端（自带 coopmat 修复）
python3 "<image-generation skill>/scripts/generate.py" --backend local --device vulkan0 \
  -p "pixel art swordsman, single person, centered, plain background" --seed 42 \
  --out frames/f1.png
# 2) （可选）像素化：拿到像素网格与受限调色板
python3 "<pixel-assets skill>/scripts/pixelize.py" frames/f1.png --grid 32x48 --out-dir px
# 3) 重建
python3 "<base>/scripts/run_3d.py" --image frames/f1.png --out-dir threed --mc-resolution 128
# 4) 报告里写的是：输入体检数字 + 输出网格路径/大小 + 耗时；**不要**写"模型质量已核对"
```

## 两个必须记住的上游坑（都实测踩过）

1. **参数名是 `--pretrained-model-name-or-path`，不是 `--model-path`**。写错会得到
   `unrecognized arguments`，而这条错误只在 stderr 的 usage 里、常被进度条淹没，
   很容易误判成"跑得慢"。本技能脚本已经替调用者拼好。
2. **`--pretrained-model-name-or-path` 只接"目录"，不接单个 `.ckpt` 文件**：
   上游走 huggingface_hub，传文件路径会被当成 repo id 拒绝
   （`Repo id must be in the form 'repo_name' or 'namespace/repo_name'`）。
   目录里必须同时有 `config.yaml` 与 `model.ckpt`——HF 缓存里的 snapshot 目录正好是这样
   （两个文件都是 softlink，**离线可用**），脚本的 `resolve_model()` 会自动挑它，
   并把选中的目录写进返回值的 `model_dir`。

## 已实测的一次完整结果

```text
输入：384×384 像素风角色帧（imagecheck：主色 0.013、色数 8181、块std 0.2511 → 非退化）
输出：out/0/mesh.obj  433,646 字节  顶点 4248  面 8484（带顶点色）
torchmcubes：由技能内 shim 提供（file=.../theed/scripts/torchmcubes_shim/torchmcubes.py，backend=skimage）
```

## 边界（必须写进交付说明）

- **单图重建看不到背面**：背面几何是模型猜的。要背面也靠谱，得用多视角图（不同 seed/角度各出一张，
  逐张重建后对比），或换多视角/视频重建方案。
- **CPU 耗时分段实测**（`mc-resolution 64`，384×384 输入，本机 Ryzen 9 7900X）：
  初始化模型 3.7–17.7s（看是否已在 HF 缓存里）、图像预处理 20–65s（`rembg` 抠图是最大头）、
  前馈推理 **19.2s**、marching cubes **0.33s**、导出 0.02s。**总计约 1–2 分钟/个**。
  想更快只有三条路：把输入图先裁小（预处理是大头）、降 `mc_resolution`、换 ONNX/Vulkan 实现。
- **只出网格**：不做骨骼、不做绑定、不做动画。角色要动起来得另做（像素素材那种程序化骨骼动画反而更实用）。
- **上游是第三方**：`/srv/unison-assets/src/TripoSR` 是 clone 下来的源码，**不要改它**
  （改了会被下次 pull 冲掉）；所有 A 卡适配都在本技能的 shim 里。
- **许可**：TripoSR 是 MIT；权重来自 `stabilityai/TripoSR`。商用前自行核对上游许可。

## 资产根里的相关文件

```text
/srv/unison-assets/
  env/torch-cpu/                 # 自带 torch(CPU) 的独立环境（约 1.5G；不动系统 python）
  src/TripoSR/                   # 上游源码（只读，不改）
  models/threed/triposr/model.ckpt   # 1.02G 权重
  bin/setup-triposr.sh           # 一次性装环境+克隆源码的脚本（幂等）
```

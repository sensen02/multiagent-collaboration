# 这个目录**不会**被 Unison 扫描（先读这段）

你（或任何人）大概会顺手把技能写进 `/home/sensen/Desktop/AI_MultiAgent_Collaboration/skills/`。
**Unison 不会看这里。** 技能发现只认固定的几个根，`<项目根>/skills/` 不在其中。
实测（`unison.skills.roots_for`）当前会话真正被扫的根是：

```text
rank= 100  /home/sensen/Desktop/AI_MultiAgent_Collaboration/.dsh/skills        （工作区级，优先）
rank= 200  /home/sensen/Desktop/AI_MultiAgent_Collaboration/.agents/skills
rank= 500  /home/sensen/.dsh/skills                                            （用户级）
rank= 600  /home/sensen/.agents/skills
rank=100000 /home/sensen/Desktop/AI_MultiAgent_Collaboration/unison/unison/skills  （随程序分发的内置包）
```

`skills/` 这个名字本身没有任何特殊含义——每个根下面才是 `<name>/SKILL.md`。
另外注意：`project_root` 是靠"最近的 `.git` 祖先"找的，而这个仓库**不是** git 仓库，
所以项目级技能根回退成工作区自身，即上面的 rank=100/200。

## 该写到哪里

| 目的 | 位置 | 生效范围 |
|---|---|---|
| 给**这个项目**用（推荐） | `AI_MultiAgent_Collaboration/.agents/skills/<name>/SKILL.md` | 工作区里的所有任务 |
| 给**这台机器所有项目**用 | `~/.agents/skills/<name>/SKILL.md` | 所有会话 |
| 给**任何项目**用（随程序分发） | `unison/unison/skills/<name>/SKILL.md` | 所有会话，会被项目级/用户级同名技能覆盖 |

`SKILL.md` 必须有 YAML frontmatter，`name`（kebab-case，与目录名一致）与 `description` 必填：

```markdown
---
name: my-skill
description: 一句话说明这个技能解决什么问题、什么时候该用它。
when-to-use: 更细的触发条件。
---

# 正文
模型的完整指令。相对路径以技能目录（base directory）为基准解析。
```

改完**不需要重启服务**：正文始终从磁盘读；控制台"技能"对话框里点"重扫"即可看到
（后台 `skill-catalog` 作业每 15 分钟也会自己发现新目录）。
坏技能文件不会让整个目录失效，会出现在控制台诊断里——**别猜，去诊断里看原因**。

## 现在有哪 6 个技能（都在 `unison/unison/skills/`）

| 技能 | 干什么 | 端口可调 |
|---|---|---|
| `gpu` | 探测显卡/后端/权重；**逐文件验明权重角色**（checkpoint / LoRA / 分部权重）；核对出图是否退化 | 是 |
| `image-generation` | 出图：pollinations（联网匿名）、huggingface（需 token）、**local（本机 GPU，自带 coopmat 修复 + 溯源）** | 是（批量 ≤8） |
| `local-image-gen` | `image-generation` 本机后端的**薄适配器**：按"一批素材"的直觉调用（`items` + `out_dir` + `seed_base`） | 是（批量 ≤8） |
| `pixel-assets` | 把生成图变成**真正的像素素材**：目标网格 + 受限调色板 + 描边；拼/切动画精灵表并写契约清单 | 否（纯脚本） |
| `theed` | **图像 → 3D 网格**（GLB/OBJ），A 卡可用（CUDA-only 的 torchmcubes 换成 CPU 替身） | 是 |
| `skill-validator` | 技能**声明**与执行体**自报**能力对账 | 是 |

一条完整素材链路：

```text
image-generation --backend local     →  raw/*.png           （本机出图，自动关 coopmat）
pixel-assets/scripts/pixelize.py     →  *_px32x48.png       （像素化到目标网格）
pixel-assets/scripts/sprite_sheet.py →  sheet.png + .json   （拼动画表，带契约）
theed/scripts/run_3d.py              →  mesh.obj / .glb     （单图重建 3D）
```

## 资产根：`/srv/unison-assets`（2026-09-13 迁移）

`/home` 已用 96%（只剩 19G），新权重一律放 `/` 分区的 `/srv/unison-assets`
（`UNISON_ASSETS_ROOT` 可覆盖）：

```text
/srv/unison-assets/
  bin/            sd-cli、sd-server、fetch-asset.sh、setup-triposr.sh
  models/         sd15（国风主模型 + 2 个像素 LoRA）、sdxl、lora、animatediff、
                  wan21（视频）、upscalers、threed（TripoSR）
  env/torch-cpu/  自带 torch(CPU) 的独立环境（约 1.5G，跑 3D 用；不动系统 python）
  src/TripoSR/    上游源码（只读，不改）
  cache/          下载日志、HF 缓存
```

`gpu/scripts/probe.py` 会自动认这个根（显式 `UNISON_ASSETS_ROOT` > `/srv/unison-assets` > 旧的
`~/.cache/unison-gpu`），`--weights` 会逐个权重打印角色判定与"能不能填 `-m`"。

## 这个目录里现在有什么

`entries/` 是**已经落盘的知识条目**，内容与 `unison/unison/skills/` 下的技能正文一致，
放在这里是为了让人一眼看到"从这次游戏任务里学到了什么"：

- `entries/sd-weight-role.md`——`-m` 只接完整 checkpoint；少一个参数不报错、只是结果不对。
- `entries/architecture-defects.md`——这次 sword-demo 暴露出的架构问题（不是游戏的问题），
  以及**每个缺陷对应的修法**（第一轮修复已完成，见每节末尾的"已修"）。
- `entries/evidence-freshness.md`——ink-duel 复核发现的核心缺口：验证证据会过期，
  `exit_code` 与"证据文件写着 passed"可能不是同一次运行；含三层修法与可复用判据。

工作区级对账（端口一致性、端口是否真被占用、证据文件能否自证）已并入 `skill-validator`：

```bash
python3 -m unison.skillcheck --root unison/skills --workspace <项目目录> --json
```

真正会被模型加载的技能，在 `unison/unison/skills/`（4 个，`python3 -m unison.skillcheck` 对账一致）：

```text
unison/unison/skills/gpu/              # 探测 GPU + 验明权重角色 + 核对出图是否废图（是探测器，不生成）
unison/unison/skills/gpu/scripts/weights.py      # 只读判定 checkpoint / LoRA / 分部权重
unison/unison/skills/gpu/scripts/imagecheck.py   # 只读判定纯色/竖纹等退化输出
unison/unison/skills/gpu/references/failure-modes.md  # 出废图的排查顺序（实测记录）
unison/unison/skills/image-generation/ # 三个后端：pollinations / huggingface / local（本机）
unison/unison/skills/image-generation/scripts/local_image.py  # 本机后端：验角色 → 生成 → 核对 → 溯源
unison/unison/skills/local-image-gen/  # 本机批量的**薄适配器**（名字即意图，实现只有一份）
unison/unison/skills/skill-validator/  # 能力对账：frontmatter 声明 vs 执行体 --capabilities 自报
```

## 一条判据（这次修复的核心）

**声明的能力必须由执行体自证。** 事故的形状是：技能摘要写着"本机 GPU 后端"，
而它的脚本里根本没有这一档——模型按摘要选了它，发现没有离线路径，于是绕开技能层手写命令。

修法不是"下次注意"，而是把它变成可执行检查：

```bash
python3 -m unison.skillcheck --root unison/skills       # 一致 → 退出码 0
# 结果也会出现在 GET /api/skills 的 diagnostics 里，并由 skill-catalog 作业定期检查
```

执行体只要支持 `--capabilities` 打印一行 JSON（`CAPABILITIES` 常量），就能被对账。
六类诊断里最要紧的是 `declared-not-implemented`（摘要承诺了没有的）与
`implemented-not-declared`（做了却没说，能力被白白浪费）。

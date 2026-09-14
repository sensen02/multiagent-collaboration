#!/usr/bin/env python3
"""本机 GPU 生图后端：把 stable-diffusion.cpp 的 sd-cli 接成 image-generation 的一个后端。

**为什么放在这里而不是单独一个技能**：实现只有一份。本机后端与 pollinations /
huggingface 平级，都在 `generate.py` 的 choices 里。

这一层额外承担两件联网后端不需要的事，因为本机路线特有的静默失败都在这里：

1. **开跑前读权重文件头**：实测 `-m` 只在完整 checkpoint 上成功。清单把 LoRA 标成 checkpoint
   这种事发生过，代价是 200+ 行 `... not in model metadata` 和一堆废图。文件头里读不到完整的
   unet+vae+clip 就**直接拒绝**（RuntimeError），一张都不生成——而不是先跑完 30 张再让人从
   废图里发现。
2. **跑完把画面统计量交出来**：命令退出码 0 + 文件是 PNG + 尺寸对，**证明不了它不是废图**。
   这里用 gpu 技能的 `imagecheck` 量出主色占比、对比度、块标准差等数字，写进结果与溯源，
   **但不下判词**——这张图能不能用由你判断。

依赖（缺了就降级，绝不假装量过）：gpu 技能的 `scripts/imagecheck.py` 与 `scripts/weights.py`。
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

def _default_asset_root():
    """默认资产根：显式环境变量 > /srv/unison-assets（2026-09-13 起的主位置）> 旧的 ~/.cache/unison-gpu。

    为什么要有"旧位置"这一档：迁移期两个位置可能都在。**存在性**决定用哪个，
    比写死一个路径更安全——写死会让"文件在旧位置"的场景静默失败。
    """
    override = os.environ.get('UNISON_ASSETS_ROOT')
    if override:
        return Path(override)
    primary = Path('/srv/unison-assets')
    if primary.is_dir():
        return primary
    return Path.home() / '.cache' / 'unison-gpu'


ASSETS_ROOT = _default_asset_root()
DEFAULT_SD_CLI = Path(os.environ.get('SD_CLI') or (ASSETS_ROOT / 'bin' / 'sd-cli'))
DEFAULT_MODEL = ASSETS_ROOT / 'models' / 'sd15' / 'GuoFeng3.4.safetensors'
DEFAULT_NEGATIVE = ('low quality, worst quality, blurry, text, watermark, logo, extra fingers, '
                    'malformed hands, duplicate person, cropped head, modern clothes, photograph')
# FLUX 在 stable-diffusion.cpp 里是**分离式**的：`-m` 那种"一个完整 checkpoint"的用法不适用，
# 必须分别给 --diffusion-model / --clip_l / --t5xxl / --vae。因此 `--model` 指到扩散模型本体时，
# 我们在**同一个目录**里找它的三个同伴——同目录是下载时约定好的布局，不需要额外参数。
FLUX_COMPANIONS = (('clip_l', ('clip_l.safetensors', 'clip_l*.safetensors')),
                   ('t5xxl', ('t5*.gguf', 't5*.safetensors')),
                   ('vae', ('ae.safetensors', 'ae*.safetensors')))
# FLUX 用 distilled guidance，不用 classifier-free guidance：cfg 恒为 1，负向提示词无意义。
# schnell 是 4 步蒸馏版；dev 需要 20 步以上才成型。
FLUX_DEFAULTS = {'schnell': {'steps': 4, 'guidance': 3.5}, 'dev': {'steps': 20, 'guidance': 3.5}}
SLUG = re.compile(r'[^a-zA-Z0-9]+')

# 本技能的目录：vendored 与 gpu 技能都在它的相邻位置。
_SKILL_DIR = Path(__file__).resolve().parent.parent
_SKILLS_ROOT = _SKILL_DIR.parent
for _candidate in (_SKILL_DIR / 'vendored', _SKILLS_ROOT / 'gpu' / 'scripts'):
    if _candidate.is_dir():
        sys.path.insert(0, str(_candidate))
try:
    import imagecheck as _imagecheck                       # noqa: N813
except Exception:                                          # noqa: BLE001
    _imagecheck = None
try:
    import weights as _weights                             # noqa: N813
except Exception:                                          # noqa: BLE001
    _weights = None


def _log(message):
    """进度写 stderr：stdout 只留给桥接/命令行的结果 JSON。"""
    print(message, file=sys.stderr, flush=True)


def slugify(text, limit=40):
    cleaned = SLUG.sub('-', str(text).strip().lower()).strip('-')
    return cleaned[:limit].strip('-') or 'image'


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def default_paths():
    return {'sd_cli': DEFAULT_SD_CLI, 'model': DEFAULT_MODEL, 'assets_root': ASSETS_ROOT}


def asset_roots():
    """权重可能同时存在于两个资产根，两处都要看。

    2026-09-14 实测：主资产根 `/srv/unison-assets` 落在只有 ~14 GiB 空闲的根分区上，
    而 FLUX 全套要 15 GiB 以上——它只能待在主目录那个盘（207 GiB 空闲）的
    `~/.cache/unison-gpu/models/`。所以"只认一个根"会让**明明下好了的权重看不见**。
    """
    roots = []
    for candidate in (Path('/srv/unison-assets'), Path.home() / '.cache' / 'unison-gpu'):
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def discover_parts(model):
    """判断 `--model` 是不是分离式模型，并在同目录里找出它的同伴。

    目前只有 `.gguf` 走这条路（stable-diffusion.cpp 的 gguf 一律是单组件：
    扩散模型、文本编码器分开量化）。返回 None 表示"这不是分离式目标，按老的 `-m` 处理"。

    `--model` 也可以直接指向**目录**（例如 `models/flux/`）：那时在目录里挑扩散模型本体
    ——按名字排除文本编码器，避免把 t5 当成主模型（那正是本技能一开始要防的静默失败）。
    """
    path = Path(model)
    if path.is_dir():
        candidates = [p for p in sorted(path.glob('*.gguf'))
                      if not p.name.lower().startswith(('t5', 'umt5', 'clip'))]
        if not candidates:
            return None
        path = candidates[0]
    if path.suffix.lower() != '.gguf':
        return None
    directory = path.parent

    def find(patterns):
        for pattern in patterns:
            for hit in sorted(directory.glob(pattern)):
                if hit.is_file() and hit != path:
                    return hit
        return None

    parts = {'diffusion': path}
    for key, patterns in FLUX_COMPANIONS:
        parts[key] = find(patterns)
    stem = path.name.lower()
    parts['family'] = 'schnell' if 'schnell' in stem else ('dev' if 'dev' in stem else 'flux')
    return parts


def available_weights():
    """资产根里有哪些权重、各自是什么角色——`--list-models --backend local` 用它。"""
    entries = []
    for root in asset_roots():
        models_dir = root / 'models'
        if not models_dir.is_dir():
            continue
        for path in sorted(models_dir.rglob('*.safetensors')):
            record = {'path': str(path), 'name': path.name, 'bytes': path.stat().st_size,
                      'root': str(root)}
            if _weights is not None:
                try:
                    info = _weights.classify(path)
                    record.update({'architecture': info.get('architecture'),
                                   'tensor_groups': info.get('tensor_groups'),
                                   'lora_tensor_fraction': info.get('lora_tensor_fraction'),
                                   'missing_components': info.get('missing_components'),
                                   'observations': info.get('observations')})
                except Exception as exc:                    # noqa: BLE001
                    record['error'] = f'{type(exc).__name__}:{exc}'
            entries.append(record)
        for path in sorted(models_dir.rglob('*.gguf')):
            # .gguf 不是 safetensors，weights.py 的文件头读取对它不适用：如实说明，不要假装读过。
            entries.append({'path': str(path), 'name': path.name, 'bytes': path.stat().st_size,
                            'root': str(root), 'container': 'gguf',
                            'note': ('gguf 单组件权重：扩散模型要配合同目录的 clip_l / t5xxl / ae，'
                                     '用 --diffusion-model 那一组参数，不能填 `-m`。')})
    return entries


def _path_of(value, default):
    if value in (None, ''):
        return Path(default)
    candidate = Path(str(value)).expanduser()
    return candidate


def _default_backend(parts):
    """分离式模型（FLUX）的默认后端分配：**文本编码器放 CPU**。

    2026-09-14 在 RX 9070 XT（16 GiB）实测：全套权重 15261 MB 会被全部搬进显存
    （text_encoders 2996 + diffusion_model 12106 + vae 160），只剩 ~1060 MB，
    而 VAE 解码要 ~2079 MB —— 于是采样阶段直接
    `model manager cannot make enough memory available` → `del compute failed`。
    把文本编码器放 RAM 后：显存占用降到 12266 MB，采样 14.11s、解码 0.49s，正常出图。
    文本编码器只在开头跑一次，放 CPU 的代价很小；显存不够的代价是**一张都出不来**。
    """
    if parts:
        return 'diffusion=vulkan0,te=cpu'
    return 'vulkan0'


def validate_target(model, lora_dir, loras=(), parts=None):
    """开跑前的硬校验。返回 (problems, warnings, facts)。"""
    problems, warnings, facts = [], [], {}
    model = Path(model)
    if not model.is_file():
        return [f'主模型不存在：{model}'], warnings, facts
    if parts:
        # 分离式（FLUX 的 gguf 布局）：四个部件缺一不可，且**不读 safetensors 文件头**
        # ——gguf 是另一种容器，拿 safetensors 的读法去看它只会得到假的结论。
        missing = [key for key in ('clip_l', 't5xxl', 'vae') if not parts.get(key)]
        facts.update({'layout': 'split', 'family': parts.get('family'),
                      'parts': {k: (str(v) if v else None)
                                for k, v in parts.items() if k != 'family'}})
        if missing:
            problems.append(
                f'{model.name} 是分离式权重（gguf），但同目录缺少部件：{missing}。'
                'stable-diffusion.cpp 跑 FLUX 需要 --diffusion-model + --clip_l + --t5xxl + --vae 四件齐；'
                '缺 t5xxl 就是缺文本编码器，缺 ae 就是缺 VAE，都会在加载阶段失败。'
                '把一个完整 checkpoint 填 `-m` 也不行——那是 SD 系列的用法。')
        return problems, warnings, facts
    if _weights is None:
        warnings.append('weights.py 不可用：跳过权重结构读取（只查了文件存在）')
    else:
        info = _weights.classify(model)
        groups = info.get('tensor_groups') or {}
        complete = bool(groups.get('unet') and groups.get('vae') and groups.get('clip'))
        facts.update({'tensor_groups': groups, 'architecture': info.get('architecture'),
                      'lora_tensor_fraction': info.get('lora_tensor_fraction'),
                      'complete_checkpoint': complete,
                      'observations': info.get('observations')})
        if not complete:
            problems.append(
                f'`-m` 实测需要在完整 checkpoint 上使用，但 {model.name} 的文件头里读不到完整的 '
                f'unet+vae+clip（读到 {groups}）。实际遇到的是加载阶段报 '
                '`... not in model metadata`，输出是废图。'
                '只有 lora_ tensor 的文件请走 --lora-dir + prompt 里的 <lora:名字:1.0>')
    wanted = [x for x in loras if x]
    if wanted:
        directory = Path(lora_dir) if lora_dir else None
        if not directory or not directory.is_dir():
            warnings.append(f'要用 LoRA {wanted} 但没有可用的 --lora-dir（目录不存在）：'
                            'prompt 里的 <lora:…> 会被静默忽略，出的是原图')
        else:
            missing = [name for name in wanted if not list(directory.glob(f'{name}.*'))]
            if missing:
                warnings.append(f'--lora-dir 里找不到 {missing}：同样会被静默忽略')
    return problems, warnings, facts


def build_command(sd_cli, model, prompt, out, width, height, seed, steps, cfg_scale,
                  negative=None, lora=None, lora_weight=1.0, lora_dir=None, backend='vulkan0',
                  extra_args=(), parts=None, guidance=None):
    if parts:
        # 分离式布局：扩散模型、文本编码器、VAE 分三个参数给进去（不是 `-m`）。
        command = [str(sd_cli), '--diffusion-model', str(parts['diffusion'])]
        for key, flag in (('clip_l', '--clip_l'), ('t5xxl', '--t5xxl'), ('vae', '--vae')):
            if parts.get(key):
                command += [flag, str(parts[key])]
    else:
        command = [str(sd_cli), '-m', str(model)]
    if lora_dir:
        command += ['--lora-model-dir', str(lora_dir)]
    if backend:
        command += ['--backend', str(backend)]
    text = prompt
    if lora:
        text = f'{prompt} <lora:{lora}:{lora_weight}>'
    command += ['-p', text]
    if negative:
        command += ['-n', str(negative)]
    command += ['-W', str(width), '-H', str(height), '--steps', str(steps),
                '--cfg-scale', str(cfg_scale), '--seed', str(seed)]
    if guidance is not None:
        command += ['--guidance', str(guidance)]
    command += [str(x) for x in (extra_args or [])]
    command += ['-o', str(out)]
    return command, text


def run_one(item):
    """生成一张。item 是已经归一化过的字典；返回 (result_dict, error_or_None)。"""
    out = Path(item['out'])
    out.parent.mkdir(parents=True, exist_ok=True)
    command, prompt_text = build_command(
        item['sd_cli'], item['model'], item['prompt'], out, item['width'], item['height'],
        item['seed'], item['steps'], item['cfg_scale'], negative=item.get('negative'),
        lora=item.get('lora'), lora_weight=item.get('lora_weight', 1.0),
        lora_dir=item.get('lora_dir'), backend=item.get('backend') or 'vulkan0',
        extra_args=item.get('extra_args') or (), parts=item.get('parts'),
        guidance=item.get('guidance'))
    # 2026-09-13 实测定位：Vulkan coopmat 路径在这张 RDNA4 卡上算错，
    # 出图是"大面积同一块灰 + 窄竖条噪声"（主色占 63–81% 的像素、128,121,11x），
    # 而退出码是 0、文件也在、尺寸也对。代价约 +13%，因此**默认关掉**，
    # 并把这件事写进溯源——否则下一个调用者会以为这是默认行为。
    environment = dict(os.environ)
    backend = str(item.get('backend') or 'vulkan0')
    coopmat_disabled = False
    # 注意判据是"这台机器上有没有用到 vulkan"，不是"字符串是不是以 vulkan 开头"：
    # 分离式模型默认用 `diffusion=vulkan0,te=cpu` 这种复合写法，用 startswith 会漏掉它，
    # 于是 coopmat 的已知算错问题会被静默放回来（RDNA4 上出的是大面积灰块 + 竖条）。
    if 'vulkan' in backend and not item.get('allow_coopmat'):
        environment['GGML_VK_DISABLE_COOPMAT'] = '1'
        coopmat_disabled = True
    record = {'path': str(out), 'prompt': prompt_text, 'seed': item['seed'],
              'width': item['width'], 'height': item['height'], 'steps': item['steps'],
              'cfg_scale': item['cfg_scale'], 'command': command,
              'coopmat_disabled': coopmat_disabled}
    if item.get('parts'):
        record['layout'] = 'split'
        record['family'] = item['parts'].get('family')
        record['guidance'] = item.get('guidance')
        record['parts'] = {k: (str(v) if v else None)
                           for k, v in item['parts'].items() if k != 'family'}
    started = time.time()
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
    record['seconds'] = round(time.time() - started, 3)
    record['returncode'] = proc.returncode
    text = (proc.stdout or b'').decode('utf-8', 'replace')
    record['log_tail'] = '\n'.join(text.strip().splitlines()[-12:])

    if not out.is_file() or out.stat().st_size == 0:
        record['ok'] = False
        return record, ('没有产出文件：常见原因是权重角色不对、参数不存在'
                        '（未知参数会打印整篇 help），或 prompt 里的 LoRA 没找到。'
                        f'最后几行日志：{record["log_tail"][-200:]}')

    record['bytes'] = out.stat().st_size
    record['sha256'] = sha256_file(out)
    record['format'] = 'png'
    # LoRA 是否真的生效，只有日志能证明——**而且要读对那一行**。
    # 实测（2026-09-13，被独立验收 run 抓出来的）：
    #   `apply_loras completed` 在"一个张量都没应用"时**照样打印**，
    #   真正说明问题的是同一段日志里的 `(N / M) LoRA tensors have been applied`。
    #   本机实测：Redmond 库是 `(0 / 576)` 却与基线**全图不同**（确实参与计算），
    #   `pixel_art_limbic` 是 `(0 / 172)` 且与同 seed 基线**逐像素完全一致**（确实没生效）。
    # 所以判定改成读计数 + 与基线比较，不再靠"有没有 completed"这种弱信号。
    if item.get('lora'):
        counts = re.findall(r'\((\d+)\s*/\s*(\d+)\)\s*LoRA tensors have been applied', text)
        record['lora_tensor_counts'] = [[int(a), int(b)] for a, b in counts][-4:]
        # 取整段日志里的**最大值**：同一条命令会打印多行（分段/卸载重载），
        # 实测 Redmond 是 `(0/576) → (576/576) → (0/576)`，只看最后一行会误判成没生效。
        applied = max((int(a) for a, _ in counts), default=None)
        record['lora_applied'] = bool('apply_loras completed' in text)
        record['lora_tensors_applied_max'] = applied
        if not counts:
            record['warning'] = ('日志里没有 `(N / M) LoRA tensors` 计数行：LoRA 很可能没生效，'
                                 '这张图是原模型的风格')
        elif applied == 0:
            record['warning'] = (
                '日志里每一行都是 `(0 / M)`：**没有任何 LoRA 张量被应用**，这个 LoRA 与本机主模型'
                '不匹配、风格不会生效。别把它的产物当成"该 LoRA 的效果"。'
                '要坐实"没变"，用同一 seed 跑一次不带 LoRA 的基线逐像素比。')
    if _imagecheck is not None:
        analysis = _imagecheck.analyse(out)
        # 只报告**测量值**，不下"废图"判词、不因此拒绝交付：
        # 这些数字是模型判断"这张图能不能用"的输入，判词由模型与人来下。
        record['check'] = {'width': analysis.get('width'), 'height': analysis.get('height'),
                           'dominant_color': analysis.get('dominant_color'),
                           'dominant_fraction': analysis.get('dominant_fraction'),
                           'distinct_colors': analysis.get('distinct_colors'),
                           'local_contrast': analysis.get('local_contrast'),
                           'block_std': analysis.get('block_std'),
                           'repeated_columns': analysis.get('repeated_columns'),
                           'measurements': analysis.get('measurements')}
        if (analysis.get('width'), analysis.get('height')) != (item['width'], item['height']):
            record['warning'] = (f'实际尺寸 {analysis.get("width")}x{analysis.get("height")} '
                                 f'与请求 {item["width"]}x{item["height"]} 不符')
    else:
        record['warning'] = 'imagecheck 不可用：只核对了文件存在与大小，没有测量画面统计量'
    if proc.returncode != 0:
        record['ok'] = False
        return record, f'sd-cli 退出码 {proc.returncode}（看上面 command 与 log_tail）'
    record['ok'] = True
    return record, None


def sd_cli_version(sd_cli):
    """问二进制它自己是哪个版本。溯源里没有这一条，"可复现"就少一半——
    换一次构建，同一 seed 也可能不是同一张图。失败就如实返回 None，不编造。"""
    try:
        proc = subprocess.run([str(sd_cli), '--help'], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or b'').decode('utf-8', 'replace')
    for line in text.splitlines():
        if line.lower().startswith('stable-diffusion.cpp version'):
            return line.strip()
    return None


def _normalize_one(options, workspace):
    """把一份 options 归一成一次本机生成所需的全部字段。"""
    prompt = str(options.get('prompt') or '').strip()
    if not prompt:
        raise ValueError('缺少 prompt')
    seed = options.get('seed')
    if seed in (None, ''):
        seed = int(options.get('seed_base') or 0)
    width = max(64, min(int(options.get('width') or 512), 4096))
    height = max(64, min(int(options.get('height') or 512), 4096))
    steps = max(1, min(int(options.get('steps') or 8), 150))
    cfg_scale = float(options.get('cfg_scale') or 7.0)
    out = str(options.get('out') or '').strip()
    if not out:
        out = os.path.join(workspace or '.', 'images', f'{slugify(prompt)}.png')
    elif not os.path.splitext(out)[1]:
        out = f'{out}.png'
    model = _path_of(options.get('model'), DEFAULT_MODEL)
    sd_cli = _path_of(options.get('sd_cli'), DEFAULT_SD_CLI)
    lora_dir = options.get('lora_dir') or None
    parts = discover_parts(model)
    if parts:
        # `--model` 给的是目录时，往下游统一用**扩散模型本体**那个文件：
        # 溯源、校验、报错都指向真正被加载的东西，而不是一个目录名。
        model = Path(parts['diffusion'])
    local = options.get('local') if isinstance(options.get('local'), dict) else {}
    # FLUX 的默认值与 SD 系列不同，而且**不能沿用**：cfg 必须 1（FLUX 用 distilled guidance，
    # 套 7.0 会过曝成一片白），负向提示词没有作用点，步数按 schnell(4) / dev(20) 分档。
    guidance = None
    if parts:
        flavors = FLUX_DEFAULTS.get(parts.get('family'), {})
        if options.get('steps') in (None, ''):
            steps = flavors.get('steps', 4)
        if options.get('cfg_scale') in (None, ''):
            cfg_scale = 1.0
        guidance = float(options.get('guidance') if options.get('guidance') not in (None, '')
                         else flavors.get('guidance', 3.5))
    return {
        'prompt': prompt, 'seed': int(seed), 'width': width, 'height': height, 'steps': steps,
        'cfg_scale': cfg_scale, 'out': out, 'model': model, 'sd_cli': sd_cli,
        'parts': parts, 'guidance': guidance,
        'lora': local.get('lora') or options.get('lora'),
        'lora_weight': local.get('lora_weight', options.get('lora_weight', 1.0)),
        'lora_dir': lora_dir or local.get('lora_dir'),
        'backend': local.get('backend') or options.get('backend_name') or _default_backend(parts),
        # FLUX 没有 classifier-free guidance，负向提示词无处生效——给了只会让人以为起了作用。
        'negative': None if parts else local.get('negative', DEFAULT_NEGATIVE),
        'extra_args': local.get('extra_args') or options.get('extra_args') or (),
    }


def generate_local(items, workspace=None, provenance_path=None):
    """本机后端入口：一次处理 1..N 张（同一进程串行），返回与联网后端同形的结果。

    返回 `{'items': [...], 'provenance': 路径或 None}`；任一张失败时抛 LocalGenerationError，
    但**成功的那几张已经落盘**，异常里带着 `envelope`（含产物与失败原因），不许当没发生。
    `provenance_path` 传 `''` 可以显式关掉溯源；不传则默认写到第一张图的目录下
    （`generation.json`）。
    """
    prepared = [_normalize_one(item if isinstance(item, dict) else {'prompt': str(item)},
                               workspace) for item in items]
    if not prepared:
        raise ValueError('没有要生成的内容')
    head = prepared[0]
    if not Path(head['sd_cli']).is_file():
        raise RuntimeError(f'sd-cli 不存在：{head["sd_cli"]}（先用 gpu 技能的 probe.py 确认本机推理栈）')
    problems, warnings, facts = validate_target(
        head['model'], head['lora_dir'], [item['lora'] for item in prepared],
        parts=head.get('parts'))
    if problems:
        raise RuntimeError('；'.join(problems))

    results, failures = [], []
    for index, item in enumerate(prepared):
        _log(f'[local {index + 1}/{len(prepared)}] seed={item["seed"]} -> {item["out"]}')
        record, error = run_one(item)
        record['index'] = index
        if error and not record.get('error'):
            record['error'] = error               # 失败原因必须留在产物记录里，不能只在异常文本里
        results.append(record)
        if error:
            failures.append(f'#{index}（{record["path"]}）：{error}')
            _log(f'    失败：{error}')
        else:
            check = record.get('check') or {}
            _log(f'    完成 {record["seconds"]}s {check.get("width")}x{check.get("height")} '
                 f'block_std={check.get("block_std")} colors={check.get("distinct_colors")}')

    model_sha = None
    try:
        if Path(head['model']).is_file():
            model_sha = sha256_file(head['model'])
    except OSError:
        model_sha = None
    # 逐张各写各的溯源：批量里每个请求由**独立的子进程**执行，各自只看得见自己那几张。
    # 若都写同一个路径就会互相覆盖（实测踩过）——所以带上 index 前缀区分。
    wanted = [item.get('provenance') for item in prepared]
    provenance_targets = {}
    for index, target in enumerate(wanted):
        if target:
            resolved = str(target)
            if any(other == resolved for other in wanted[:index]):
                stem, suffix = os.path.splitext(resolved)
                resolved = f'{stem}-{index:02d}{suffix}'
            provenance_targets[index] = resolved
    weights_manifest = {
        'path': str(head['model']), 'sha256': model_sha, 'facts': facts,
        'sd_cli': str(head['sd_cli']), 'sd_cli_version': sd_cli_version(head['sd_cli']),
        'backend': 'local',
    }
    envelope = {
        'backend': 'local',
        'model': str(head['model']),
        'model_sha256': model_sha,
        'model_facts': facts,
        'sd_cli': str(head['sd_cli']),
        'sd_cli_version': weights_manifest['sd_cli_version'],
        'items': results,
        'warnings': warnings,
    }
    for record in results:                                 # 与联网后端同形的单张字段
        record.setdefault('format', 'png')
    if len(results) == 1:
        envelope.update({k: v for k, v in results[0].items() if k not in ('index', 'command')})
        envelope['items'] = results
    if provenance_path is None:
        # 默认与产物同目录：溯源必须跟图躺在一起，否则"这批图怎么来的"又只能靠人记。
        provenance_path = os.path.join(os.path.dirname(os.path.abspath(str(results[0]['path']))),
                                       'generation.json')
    if provenance_path:
        provenance = {
            'generator': 'image-generation / 本机后端（sd-cli）',
            'created': time.strftime('%Y-%m-%d %H:%M:%S'),
            'backend': 'local', 'sd_cli': str(head['sd_cli']),
            'sd_cli_version': envelope['sd_cli_version'],
            'model': str(head['model']),
            'model_sha256': model_sha, 'model_facts': facts, 'workspace': workspace,
            'warnings': warnings,
            'items': [{k: v for k, v in record.items() if k != 'log_tail'} for record in results],
        }
        written = []
        for index, target_path in sorted(provenance_targets.items()) if provenance_targets else [(0, provenance_path)]:
            try:
                target = Path(target_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n',
                                  encoding='utf-8')
                written.append(str(target))
            except OSError as exc:
                envelope['provenance_error'] = str(exc)
        if written:
            # 单张时是字符串（调用方最常读这个字段）；多张时是列表（每个请求各一份）。
            envelope['provenance'] = written[0] if len(written) == 1 else written
            envelope['provenance_all'] = written
    if failures:
        envelope['failures'] = failures
        raise LocalGenerationError('；'.join(failures), envelope)
    return envelope


class LocalGenerationError(RuntimeError):
    """带着已完成产物的失败：调用方仍应把 items/provenance 报告出来，而不是当什么都没发生。"""

    def __init__(self, message, envelope):
        super().__init__(message)
        self.envelope = envelope

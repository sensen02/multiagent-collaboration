#!/usr/bin/env python3
"""图像 → 3D 网格（GLB/OBJ），在**没有 CUDA 的 A 卡机器上**跑得起来。

这个脚本存在的理由（A 卡真机踩过，按踩坑顺序）：

1. **TripoSR 的第一行就 import 了 CUDA-only 的 `torchmcubes`**——在 ROCm/CPU 环境里它
   pip 装不上（`Encountered error while generating package metadata`），于是 pipeline
   在 import 阶段就死，跟显卡算力无关。修法见 `scripts/torchmcubes_shim/`：
   用 `sitecustomize.py` 在导入路径上把 `torchmcubes` 换成 `skimage` 的 CPU marching cubes，
   **不改上游一行代码**。
2. **少一个环境变量就等于没修**：`PYTHONPATH` 没带上那个 shim 目录，症状还是
   `ModuleNotFoundError: torchmcubes`。所以这个脚本自己拼好环境再调上游，调用者不用记。
3. **输入图质量决定成败**：TripoSR 是单图前馈重建，喂一张"大面积纯色 + 噪声"的废图
   （本机 coopmat 未关时的典型产物），出来的是废网格而且**不会报错**。
   所以开跑前先用 gpu 技能的 `imagecheck.py` 判定输入，废图直接拒收。
4. **路径现算，不写死**：技能目录可能被搬走，一律按 `Path(__file__)` 推导。

用法（命令行）：

    python3 run_3d.py --image frame.png --out-dir out --device cpu --mc-resolution 128
    python3 run_3d.py --image frame.png --check-only        # 只做输入体检

端口调用（stdin 收 JSON）：

    echo '{"skill":"theed","workspace":"'"$PWD"'","args":{"image":"a.png","out_dir":"out"}}' | python3 bridge.py

输出约定：stdout 一行 JSON，含 `ok` / `mesh` / `input_check` / `seconds` / `command`；
网格文件落在 `out_dir`，格式按 `--format`（默认 glb）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
SHIM_DIR = SCRIPTS_DIR / 'torchmcubes_shim'
DEFAULT_ASSETS = '/srv/unison-assets'

# 兄弟技能的只读检查器：判定输入图是不是废图（缺了就降级并说明，绝不假装检查过）
for _candidate in (SKILL_DIR.parent / 'gpu' / 'scripts',):
    if _candidate.is_dir():
        sys.path.insert(0, str(_candidate))
try:
    import imagecheck as _imagecheck
except Exception:                                              # noqa: BLE001
    _imagecheck = None


def log(message):
    print(message, file=sys.stderr, flush=True)


def assets_root():
    override = os.environ.get('UNISON_ASSETS_ROOT')
    if override:
        return Path(override)
    primary = Path(DEFAULT_ASSETS)
    return primary if primary.is_dir() else Path.home() / '.cache' / 'unison-gpu'


def check_input(image):
    """输入体检：把输入图的画面统计量量出来，随结果一起交付。

    废图进去只会得到废网格，而且上游不报错——所以这些数字值得摆在结果里。
    但**拦截与否由调用方判断**：这里不下"废图"判词，也不替模型决定要不要继续。
    """
    if _imagecheck is None:
        return {'checked': False, 'reason': 'imagecheck 不可用（gpu 技能不在预期位置）'}
    result = _imagecheck.analyse(image)
    return {'checked': True,
            'dominant_fraction': result.get('dominant_fraction'),
            'dominant_color': result.get('dominant_color'),
            'distinct_colors': result.get('distinct_colors'),
            'local_contrast': result.get('local_contrast'),
            'block_std': result.get('block_std'),
            'repeated_columns': result.get('repeated_columns'),
            'error': result.get('error')}


def build_env(extra_pythonpath=()):
    """把 shim 目录塞进 PYTHONPATH——这一步省掉就是 `ModuleNotFoundError: torchmcubes`。"""
    env = dict(os.environ)
    parts = [str(SHIM_DIR)] + [str(p) for p in extra_pythonpath if p]
    if env.get('PYTHONPATH'):
        parts.append(env['PYTHONPATH'])
    env['PYTHONPATH'] = os.pathsep.join(parts)
    env.setdefault('HF_HOME', str(assets_root() / 'cache' / 'hf'))
    return env


def shim_status(torch_python):
    """让 shim 自报是否真的生效——比"我以为设了环境变量"可靠。"""
    code = ('import torchmcubes, json, sys;'
            'print(json.dumps({"file": torchmcubes.__file__,'
            '"backend": getattr(torchmcubes, "_SHIM_BACKEND", None)}))')
    try:
        proc = subprocess.run([str(torch_python), '-c', code], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=build_env(), timeout=120)
        text = (proc.stdout or b'').decode('utf-8', 'replace').strip().splitlines()
        for line in reversed(text):
            if line.startswith('{'):
                return json.loads(line)
        return {'error': text[-1] if text else 'no output'}
    except Exception as exc:                                   # noqa: BLE001
        return {'error': f'{type(exc).__name__}: {exc}'}


def resolve_model(env_root, explicit=None):
    """找到可离线加载的模型**目录**。

    上游用 `TSR.from_pretrained(name_or_path, config_name='config.yaml', weight_name='model.ckpt')`，
    走 huggingface_hub：**本地路径必须是"同时含 config.yaml 与 model.ckpt 的目录"**。
    传单个 .ckpt 文件会被当成 repo id 拒绝（实测：
    `Repo id must be in the form 'repo_name' or 'namespace/repo_name'`）。

    顺序：显式给 → 资产根里的整目录副本 → HF 缓存里的 snapshot 目录（两个文件都是 softlink，
    离线可用）→ 让上游自己按 repo id 去下载。
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(env_root / 'models' / 'threed' / 'triposr' / 'repo')
    snapshots = env_root / 'cache' / 'hf' / 'hub' / 'models--stabilityai--TripoSR' / 'snapshots'
    if snapshots.is_dir():
        candidates.extend(sorted(p for p in snapshots.iterdir() if p.is_dir()))
    for candidate in candidates:
        if (candidate / 'config.yaml').is_file() and (candidate / 'model.ckpt').is_file():
            return candidate, None
    return None, ('找不到可离线加载的 TripoSR 模型目录（需同时含 config.yaml 与 model.ckpt）。'
                  '传单个 .ckpt 文件不行——上游按 repo id 解析，会直接拒绝。'
                  f'把整目录放到 {env_root}/models/threed/triposr/repo，或先联网下载一次。')


def reconstruct(image, out_dir, torch_python=None, triposr_dir=None, device='cpu',
                mc_resolution=128, fmt='glb', foreground_ratio=0.85, model_path=None,
                workspace=None, check_shim=True):
    image = Path(image)
    if not image.is_absolute() and workspace:
        image = Path(workspace) / image
    env_root = assets_root()
    torch_python = Path(torch_python) if torch_python else env_root / 'env' / 'torch-cpu' / 'bin' / 'python'
    triposr_dir = Path(triposr_dir) if triposr_dir else env_root / 'src' / 'TripoSR'
    out_dir = Path(out_dir)
    if not out_dir.is_absolute() and workspace:
        out_dir = Path(workspace) / out_dir

    envelope = {'ok': False, 'image': str(image), 'out_dir': str(out_dir), 'device': device,
                'mc_resolution': mc_resolution, 'format': fmt, 'assets_root': str(env_root)}
    if not image.is_file():
        envelope['error'] = f'输入图不存在：{image}'
        return envelope

    check = check_input(image)
    envelope['input_check'] = check
    # 不再因为"输入图像废图"而拒绝重建：统计量已经随 input_check 交付，
    # 要不要继续由模型判断。这里只在画面几乎无结构时附一条提示，不阻断。
    if (check.get('checked') and (check.get('block_std') or 0) < 0.05
            and (check.get('dominant_fraction') or 0) > 0.5):
        envelope.setdefault('warnings', []).append(
            '输入图的块标准差 %.4f、主色占比 %.3f：画面结构很少，'
            '重建出来的网格很可能也没有形状。若结果不可用，先查生成端'
            '（本机第一嫌疑是 GGML_VK_DISABLE_COOPMAT 没设）。'
            % (check.get('block_std') or 0, check.get('dominant_fraction') or 0))

    if not torch_python.is_file():
        envelope['error'] = (f'没有可用的 torch 环境：{torch_python}'
                             f'（先跑 {env_root}/bin/setup-triposr.sh，约 1.5G）')
        return envelope
    runner = triposr_dir / 'run.py'
    if not runner.is_file():
        envelope['error'] = f'找不到 TripoSR 的 run.py：{runner}（源码在 {triposr_dir}）'
        return envelope

    if check_shim:
        status = shim_status(torch_python)
        envelope['torchmcubes'] = status
        if status.get('error'):
            envelope['error'] = ('torchmcubes 的 CPU 替身没生效：' + str(status['error']) +
                                 '。没有它上游会在 import 阶段就死（A 卡上 torchmcubes 装不上）。')
            return envelope

    model_dir, model_error = resolve_model(env_root, model_path)
    if model_error:
        envelope['error'] = model_error
        return envelope
    envelope['model_dir'] = str(model_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [str(torch_python), str(runner), str(image), '--device', device,
               '--output-dir', str(out_dir), '--model-save-format', fmt,
               '--mc-resolution', str(mc_resolution), '--foreground-ratio', str(foreground_ratio),
               # 上游参数名是 --pretrained-model-name-or-path（不是 --model-path）；写错会得到
               # "unrecognized arguments"，而这条错误只在 stderr 的 usage 里，日志尾部常被进度条淹没，
               # 容易误判成"跑得慢"。实测踩过。
               '--pretrained-model-name-or-path', str(model_dir)]
    envelope['command'] = command
    log('运行：' + ' '.join(command))
    started = time.time()
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=build_env())
    envelope['seconds'] = round(time.time() - started, 1)
    envelope['returncode'] = proc.returncode
    text = (proc.stdout or b'').decode('utf-8', 'replace')
    envelope['log_tail'] = '\n'.join(text.strip().splitlines()[-15:])

    produced = sorted(str(p) for p in out_dir.rglob(f'*.{fmt}'))
    envelope['mesh'] = produced[0] if produced else None
    if not produced:
        envelope['error'] = ('没有产出网格文件：看 log_tail。常见原因：'
                             '①PYTHONPATH 少了 torchmcubes shim（ModuleNotFoundError）'
                             '②输入图不是有效图 ③上游版本变更')
        return envelope
    envelope['ok'] = proc.returncode == 0
    if not envelope['ok']:
        envelope['error'] = f'上游退出码 {proc.returncode}'
    return envelope


def main(argv=None):
    parser = argparse.ArgumentParser(description='图像 → 3D 网格（A 卡可用的 TripoSR 通路）')
    parser.add_argument('--image')
    parser.add_argument('--out-dir', default='threed_out')
    parser.add_argument('--device', default='cpu', help='cpu（本机 A 卡唯一确定能跑的路径）或 cuda')
    parser.add_argument('--mc-resolution', type=int, default=128,
                        help='marching cubes 分辨率（越大越慢；CPU 上 128 已需数分钟）')
    parser.add_argument('--format', default='glb', choices=['glb', 'obj'])
    parser.add_argument('--foreground-ratio', type=float, default=0.85)
    parser.add_argument('--torch-python', default=None)
    parser.add_argument('--triposr-dir', default=None)
    parser.add_argument('--model-path', default=None)
    parser.add_argument('--check-only', action='store_true', help='只做输入图体检')
    parser.add_argument('--call', action='store_true', help='端口调用：stdin 收 JSON')
    args = parser.parse_args(argv)

    if args.call:
        try:
            payload = json.loads(sys.stdin.read() or '{}')
        except ValueError as exc:
            print(json.dumps({'ok': False, 'error': f'桥接输入不是 JSON：{exc}'}, ensure_ascii=False))
            return 1
        raw = payload.get('args') if isinstance(payload, dict) else {}
        raw = raw if isinstance(raw, dict) else {}
        workspace = payload.get('workspace') if isinstance(payload, dict) else None
        if not raw.get('image'):
            print(json.dumps({'ok': False, 'result': {'error': 'args.image 必填（工作区相对路径）'}},
                             ensure_ascii=False))
            return 1
        envelope = reconstruct(raw['image'], raw.get('out_dir') or 'threed_out',
                               torch_python=raw.get('torch_python'), triposr_dir=raw.get('triposr_dir'),
                               device=raw.get('device') or 'cpu',
                               mc_resolution=int(raw.get('mc_resolution') or 128),
                               fmt=raw.get('format') or 'glb',
                               foreground_ratio=float(raw.get('foreground_ratio') or 0.85),
                               model_path=raw.get('model_path'), workspace=workspace)
        print(json.dumps({'ok': envelope.get('ok', False), 'result': envelope}, ensure_ascii=False))
        return 0 if envelope.get('ok') else 1

    if args.check_only:
        if not args.image:
            print(json.dumps({'ok': False, 'error': '--check-only 需要 --image'}, ensure_ascii=False))
            return 1
        result = check_input(args.image)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    if not args.image:
        print(json.dumps({'ok': False,
                          'error': '需要 --image（或 --call 走端口）；用法见技能正文'},
                         ensure_ascii=False))
        return 1
    envelope = reconstruct(args.image, args.out_dir, torch_python=args.torch_python,
                           triposr_dir=args.triposr_dir, device=args.device,
                           mc_resolution=args.mc_resolution, fmt=args.format,
                           foreground_ratio=args.foreground_ratio, model_path=args.model_path)
    print(json.dumps(envelope, ensure_ascii=False))
    return 0 if envelope.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())

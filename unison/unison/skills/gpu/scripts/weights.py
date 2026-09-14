#!/usr/bin/env python3
"""只读地"验明正身"：这个权重文件到底是什么，以及能不能喂给 `-m`。

为什么需要它（真机踩过）：资产根的 `manifest.json` 把两个 LoRA 文件写成了
`sd15-checkpoint 像素风`。照清单读，最自然的动作就是把它们当主模型：

    sd-cli -m .../PixelArtRedmond15V-PixelArt-PIXARFK.safetensors ...

结果是 27 MB 的 LoRA 被塞进 `-m`，报上百行
`VAE/Diffusion model tensor ... not in model metadata`，拿到的图是废图。
**清单是声明，文件头才是事实**；凡是"用什么权重跑"的决定，都要先过一遍这里。

判定只看 safetensors 文件头（前 8 字节长度 + JSON 头），不加载权重，
2.3 GB 的 checkpoint 也是毫秒级。任何解析失败都返回 unknown + 原因，绝不猜。

用法：
    python3 weights.py /srv/unison-assets/models/sd15/*.safetensors
    python3 weights.py --json <文件>...
"""
from __future__ import annotations
import argparse
import json
import os
import struct
import sys
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


HEADER_LIMIT = 64 * 1024 * 1024          # 头不可能比这更大；防畸形文件把内存吃光

# 单个 tensor 名只保留前缀，用前缀直方图判别架构，避免把 1133 个名字全装进内存。
_LORA_PREFIXES = ('lora_unet', 'lora_te', 'lora_te1', 'lora_te2', 'lora_text_encoder',
                  'lora_unet_', 'lora_')
_SD15_UNET = 'model.diffusion_model.'
_SD15_VAE = 'first_stage_model.'
_SD15_CLIP = 'cond_stage_model.'
_SDXL_CLIP = 'conditioner.'          # SDXL 单文件的文本编码器前缀（实测：conditioner.embedders.*）


def read_header(path):
    """返回 (header_dict, error)。只读头部，不读权重数据。"""
    path = Path(path)
    try:
        if not path.is_file():
            return None, 'not-a-file'
        size = path.stat().st_size
        with path.open('rb') as handle:
            raw = handle.read(8)
            if len(raw) != 8:
                return None, 'file-too-short'
            length = struct.unpack('<Q', raw)[0]
            if length <= 0 or length > HEADER_LIMIT or length > size:
                return None, f'implausible-header-length:{length}'
            blob = handle.read(length)
            if len(blob) != length:
                return None, 'truncated-header'
        header = json.loads(blob)
        if not isinstance(header, dict):
            return None, 'header-not-an-object'
        return header, None
    except Exception as exc:                                   # noqa: BLE001
        return None, f'error:{type(exc).__name__}:{exc}'


def classify(path):
    """判别一个 safetensors 的**角色**与**能否当主模型**。"""
    path = Path(path)
    info = {'path': str(path), 'name': path.name,
            'bytes': path.stat().st_size if path.is_file() else None,
            'format': 'safetensors' if path.suffix == '.safetensors' else path.suffix.lstrip('.'),
            'evidence': [], 'observations': [], 'error': None}
    if path.suffix != '.safetensors':
        info['error'] = 'not-safetensors'
        info['evidence'].append('只解析 safetensors；.ckpt/.gguf/.pth 请换对应工具')
        return info

    header, error = read_header(path)
    if error:
        info['error'] = error
        return info

    meta = header.get('__metadata__') or {}
    arch = str((meta.get('modelspec.architecture') or '')).lower()
    ss_module = str((meta.get('ss_network_module') or '')).lower()
    names = [key for key in header if key != '__metadata__']

    groups = {'unet': 0, 'vae': 0, 'clip': 0, 'lora': 0, 'other': 0}
    for name in names:
        # 只在这里做一次前缀判断，name 本身是短字符串，1133 个也不构成负担。
        # 注意：**不能只认 SD1.5 的前缀**。SDXL 单文件用的完全是另一套命名：
        #   model.diffusion_model.*        → UNet（与 SD1.5 同名）
        #   conditioner.embedders.*        → CLIP/text encoder（SD1.5 叫 cond_stage_model.*）
        #   first_stage_model.*            → VAE（与 SD1.5 同名）
        # 第一版只认 SD1.5 前缀，于是把**完整可用的 SDXL** 判成"缺 CLIP、不能当主模型"——
        # 实测被独立验收 run 抓到（日志里 sd-cli 明确 `Version: SDXL` 并成功出图 1024×1024）。
        if name.startswith(_LORA_PREFIXES) or '.lora_' in name:
            groups['lora'] += 1
        elif name.startswith(_SD15_UNET):
            groups['unet'] += 1
        elif name.startswith(_SD15_VAE):
            groups['vae'] += 1
        elif name.startswith(_SD15_CLIP) or name.startswith(_SDXL_CLIP):
            groups['clip'] += 1
        else:
            groups['other'] += 1

    info['tensor_count'] = len(names)
    info['tensor_groups'] = groups
    info['metadata'] = {k: str(v)[:160] for k, v in list(meta.items())[:8]}

    total = max(1, len(names))
    lora_frac = groups['lora'] / total

    # 1) 带 lora_ 前缀的权重
    if arch.startswith('stable-diffusion-v1/lora') or ss_module.endswith('lora') or lora_frac > 0.5:
        info['lora_tensor_fraction'] = round(lora_frac, 3)
        info['evidence'].append(f'{groups["lora"]}/{total} 个 tensor 是 lora_ 前缀')
        if arch:
            info['evidence'].append(f'modelspec.architecture={arch}')
        if ss_module:
            info['evidence'].append(f'ss_network_module={ss_module}')
        info['observations'] = [
            '这个文件里 lora_ 前缀的 tensor 占多数，没有完整的 unet/vae/clip。',
            '实测把只有 lora_ 前缀的文件填进 -m，会报 '
            '`VAE/Diffusion model tensor ... not in model metadata`，输出是废图。',
        ]
        return info

    # 2) 完整 checkpoint：unet + vae + clip 齐活。架构按前缀区分，不要一律叫 sd15。
    if groups['unet'] and groups['vae'] and groups['clip']:
        is_sdxl = any(name.startswith(_SDXL_CLIP) for name in names)
        info['architecture'] = 'sdxl' if is_sdxl else 'sd15'
        info['evidence'].append(f'unet={groups["unet"]} vae={groups["vae"]} clip={groups["clip"]}')
        info['observations'] = [
            ('unet + vae + clip 齐全，是完整的 %s 权重。'
             % ('SDXL' if is_sdxl else 'SD1.5'))]
        if is_sdxl:
            info['observations'].append('SDXL 原生尺寸 1024×1024；配套 LoRA 也必须是 SDXL 的。')
        return info

    # 3) 缺件：说明缺什么，而不是笼统说"坏了"
    missing = [label for label, key in (('UNet', 'unet'), ('VAE', 'vae'), ('CLIP/text encoder', 'clip'))
               if not groups[key]]
    if groups['unet'] or groups['vae'] or groups['clip'] or groups['other']:
        info['missing_components'] = missing
        info['evidence'].append(f'缺 {", ".join(missing)}；tensor 前缀直方图 {groups}')
        info['observations'] = [
            f'缺 {", ".join(missing)}，不是完整 checkpoint。',
            '实测单独用它做 -m 会在加载阶段失败；可按缺什么补什么，'
            '改用 --vae / --diffusion-model / --clip_l 等分部参数。',
        ]
        return info

    info['evidence'].append(f'tensor 前缀直方图 {groups}；metadata 里没有可识别的架构标记')
    info['observations'] = [
        'tensor 前缀与 metadata 都对不上已知的 SD 结构，无法归类。',
        '先用它之前值得看一眼来源与文档；实测中没有它的成功调用记录。',
    ]
    return info


def check_against_manifest(entries, manifest_path=None):
    """把"清单声明"与"文件事实"对照，指出被标错角色的权重。"""
    root = Path(manifest_path).parent if manifest_path else None
    if manifest_path is None or not Path(manifest_path).is_file():
        return None
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    except Exception as exc:                                   # noqa: BLE001
        return {'error': f'manifest 读取失败：{exc}'}
    declared = {}
    for item in manifest.get('models') or []:
        if isinstance(item, dict) and item.get('path'):
            declared[Path(item['path']).name] = str(item.get('role') or '')
    # 只如实并列两列：清单**声明**的角色，与文件头**实际**呈现的结构。
    # 谁对谁错、能不能用，由读这份输出的人判断——这里不写 impact、不下结论。
    comparison = []
    for entry in entries:
        role_declared = declared.get(Path(entry['path']).name)
        if role_declared is None:
            continue
        comparison.append({'name': Path(entry['path']).name,
                           'declared_in_manifest': role_declared,
                           'structure_from_header': struct_summary(entry)})
    return {'manifest': str(manifest_path), 'declared_models': len(declared),
            'comparison': comparison}


def struct_summary(entry):
    """一句话讲清文件头里有什么结构，不换算成角色名。"""
    groups = entry.get('tensor_groups') or {}
    parts = [f'{key}={value}' for key, value in groups.items() if value]
    if entry.get('missing_components'):
        parts.append('缺 ' + ','.join(entry['missing_components']))
    if entry.get('architecture'):
        parts.append('arch=' + entry['architecture'])
    if entry.get('error'):
        parts.append('error=' + str(entry['error']))
    return ' '.join(parts) or '(无结构信息)'


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读读出 safetensors 的结构事实（tensor 前缀、metadata）')
    parser.add_argument('paths', nargs='+')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--manifest', default=None,
                        help='同时并列 manifest.json 的 role 声明与文件头事实（默认尝试资产根的 manifest.json）')
    args = parser.parse_args(argv)

    entries = [classify(p) for p in args.paths]
    manifest_path = args.manifest
    if manifest_path is None:
        candidate = _default_asset_root() / 'manifest.json'
        manifest_path = str(candidate) if candidate.is_file() else None
    comparison = check_against_manifest(entries, manifest_path)

    if args.json:
        print(json.dumps({'ok': True, 'weights': entries, 'manifest_check': comparison},
                         ensure_ascii=False, indent=1))
        return 0

    for entry in entries:
        print(f"\n{entry['name']}")
        if entry.get('bytes') is not None:
            print(f"  {entry['bytes'] / 2**20:.1f} MB   tensors={entry.get('tensor_count')}")
        if entry.get('error'):
            print(f"  错误：{entry['error']}")
        for line in entry.get('evidence') or []:
            print(f"  证据：{line}")
        print(f"  结构：{struct_summary(entry)}")
        for line in entry.get('observations') or []:
            print(f"  观察：{line}")
    if comparison and comparison.get('mismatches'):
        print('\n清单与文件不符（清单是声明，文件头才是事实）：')
        for item in comparison['mismatches']:
            print(f"  {item['name']}: 清单写「{item['declared']}」，实际是 {item['actual']} → {item['impact']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

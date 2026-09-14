#!/usr/bin/env python3
"""本机 GPU 与推理栈探测：只读，不改动任何东西。

设计原则（与技能的说明一致）：

- **只报告事实**。装了/没装、能不能跑，都以实际输出为准，不做"应该是"的推断。
- **绝不下载或安装**。安装是单独一步，需要人确认。
- **失败不致命**：任何一项探测失败都记为 unavailable + 原因，脚本仍然给出完整 JSON。
- **资产根只读**：只列 `/srv/unison-assets` 已有的东西，不写、不清理。

用法：
    python3 probe.py                # 人类可读报告
    python3 probe.py --json         # 机器可读（技能桥接/端口调用用这个）
"""
from __future__ import annotations
import argparse
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
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


ASSET_ROOT = _default_asset_root()
TIMEOUT = 20
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:                                        # 同目录的兄弟模块：验明权重角色、检查出图退化
    import weights as _weights
    import imagecheck as _imagecheck
except Exception as _exc:                   # noqa: BLE001 - 探测脚本不该因为子模块而整体失败
    _weights = _imagecheck = None
    _SIBLING_ERROR = str(_exc)
else:
    _SIBLING_ERROR = None


def run(cmd, timeout=TIMEOUT):
    """跑一条只读命令；返回 (ok, 输出文本)。不抛异常——探测失败本身就是要报告的事实。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, 'not-found'
    except subprocess.TimeoutExpired:
        return False, 'timeout'
    except Exception as exc:                      # noqa: BLE001 - 探测不该让整个脚本失败
        return False, f'error: {exc}'
    text = (proc.stdout or '') + (proc.stderr or '')
    return True, text.strip()


def probe_pci():
    ok, text = run(['lspci'])
    if not ok:
        return {'available': False, 'reason': text}
    lines = [l for l in text.splitlines()
             if any(k in l.lower() for k in ('vga', '3d controller', 'display'))]
    return {'available': True, 'devices': lines}


def probe_drm():
    nodes = sorted(str(p) for p in Path('/dev/dri').glob('*')) if Path('/dev/dri').is_dir() else []
    nodes = [p for p in nodes if Path(p).is_char_device() or Path(p).is_dir()] if nodes else []
    return {'available': bool(nodes), 'nodes': nodes,
            'note': 'renderD* 才是计算节点；card* 是显示节点'}


def probe_nvidia():
    ok, text = run(['nvidia-smi', '--query-gpu=name,memory.total,driver_version',
                    '--format=csv,noheader'])
    if not ok:
        return {'available': False, 'reason': 'nvidia-smi ' + text}
    return {'available': True, 'gpus': [l for l in text.splitlines() if l.strip()]}


def probe_rocm():
    # 不能用 `which`：本机 shell 里 `which 不存在的命令` 返回 0（stderr 才是 1），
    # 于是"探测"把没装的东西报成已装。存在性一律用 shutil.which。
    result = {'installed': False, 'hints': []}
    for path in ('/opt/rocm', '/opt/rocm-6', '/opt/rocm-7', '/opt/rocm-7.14'):
        if Path(path).is_dir():
            result['installed'] = True
            result['hints'].append(path)
    for tool in ('rocminfo', 'rocm-smi', 'hipcc'):
        found = shutil.which(tool)
        if found:
            result['installed'] = True
            result['hints'].append(found)
    return result


def probe_vulkan():
    """Vulkan 是本机实际在用的计算后端（stable-diffusion.cpp 只链 libvulkan）。"""
    result = {'loader': False, 'icds': [], 'devices': [], 'tools': {}}
    for lib in ('libvulkan.so.1', 'libvulkan.so'):
        if any((Path(d) / lib).exists() for d in ('/lib/x86_64-linux-gnu', '/usr/lib/x86_64-linux-gnu',
                                                  '/usr/lib', '/usr/local/lib')):
            result['loader'] = True
            break
    icd_dir = Path('/usr/share/vulkan/icd.d')
    if icd_dir.is_dir():
        result['icds'] = sorted(p.name for p in icd_dir.glob('*.json'))
    for tool in ('vulkaninfo', 'vulkaninfoSDK', 'glxinfo'):
        result['tools'][tool] = bool(shutil.which(tool))
    ok, text = run(['vulkaninfo', '--summary'], timeout=25)
    if ok:
        for line in text.splitlines():
            if 'deviceName' in line or 'GPU' in line:
                result['devices'].append(line.strip())
    return result


def probe_gpu_load():
    """本地算力现在有多忙：显存占用、GPU 占用率、谁占着计算节点、系统负载。

    这些是"现在该用本地还是该走外部"的判据，必须能被模型读到——否则它只能猜。
    amdgpu 驱动把这些直接暴露在 sysfs 上（card*/device/mem_info_vram_*、gpu_busy_percent）。
    """
    result = {'devices': [], 'system_load': {}, 'busy_processes': []}
    try:
        result['system_load'] = {'loadavg': open('/proc/loadavg').read().split()[:3],
                                 'cpus': os.cpu_count()}
    except Exception as exc:                                    # noqa: BLE001
        result['system_load'] = {'error': str(exc)}
    for card in sorted(Path('/sys/class/drm').glob('card[0-9]*')):
        device = card / 'device'
        # card0-DP-1 / card0-HDMI-A-1 / card0-Writeback-1 都是**接口**不是设备——
        # 它们同样有 device/ 目录，只有真设备的 device/drm 才存在。
        if not (device / 'drm').is_dir():
            continue
        entry = {'card': card.name, 'render_node': None}
        for key, name in (('mem_info_vram_used', 'vram_used_bytes'),
                          ('mem_info_vram_total', 'vram_total_bytes'),
                          ('gpu_busy_percent', 'busy_percent')):
            path = device / key
            try:
                entry[name] = int(path.read_text().strip())
            except Exception:                                   # noqa: BLE001
                entry[name] = None
        if entry.get('vram_total_bytes'):
            entry['vram_free_bytes'] = entry['vram_total_bytes'] - (entry.get('vram_used_bytes') or 0)
            entry['vram_used_pct'] = round(100 * (entry.get('vram_used_bytes') or 0) / entry['vram_total_bytes'])
        # 把 card 对上 render 节点（计算走 renderD*，不是 card*）
        try:
            for sibling in sorted((device / 'drm').glob('renderD*')):
                entry['render_node'] = '/dev/dri/' + sibling.name
        except Exception:                                       # noqa: BLE001
            pass
        result['devices'].append(entry)
    # 谁占着 GPU：扫 /proc 的 fd 指向，识别"别的活正在用卡"
    holders = {}
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            for fd in (proc / 'fd').iterdir():
                target = os.readlink(fd)
                if '/dev/dri/renderD' in target:
                    holders.setdefault(target, set()).add(proc.name)
        except Exception:                                       # noqa: BLE001
            continue
    for node, pids in holders.items():
        names = []
        for pid in sorted(pids, key=int)[:6]:
            try:
                names.append(open('/proc/%s/comm' % pid).read().strip())
            except Exception:                                   # noqa: BLE001
                names.append('pid %s' % pid)
        result['busy_processes'].append({'node': node, 'pids': sorted(pids, key=int),
                                         'names': names[:6], 'count': len(pids)})
    # 「有人打开过 render 节点」≠「算力被占满」：桌面环境常驻进程会一直持有它。
    # 真正的判据是占用率与显存用量。
    result['saturated'] = any((d.get('busy_percent') or 0) >= 20 or (d.get('vram_used_pct') or 0) >= 60
                              for d in result['devices'])
    result['idle'] = not result['saturated']
    return result


def probe_python_gpu():
    """当前解释器有没有可用的 GPU 张量栈。区分 cuda / rocm(hip) / cpu-only 三种构建。"""
    info = {'python': sys.version.split()[0]}
    for name in ('torch', 'triton', 'onnxruntime', 'transformers', 'diffusers'):
        if importlib.util.find_spec(name) is None:
            info[name] = None
            continue
        try:
            module = importlib.import_module(name)
            info[name] = getattr(module, '__version__', 'unknown')
            if name == 'torch':
                hip = getattr(getattr(module, 'version', None), 'hip', None)
                info['torch_build'] = f'rocm/{hip}' if hip else (
                    f'cuda/{module.version.cuda}' if getattr(module.version, 'cuda', None) else 'cpu-only')
                try:
                    info['torch_gpu_available'] = bool(module.cuda.is_available())
                    info['torch_device_count'] = int(module.cuda.device_count())
                except Exception as exc:                      # noqa: BLE001
                    info['torch_gpu_available'] = False
                    info['torch_probe_error'] = str(exc)
        except Exception:                                     # noqa: BLE001 - 没装就是没装
            info[name] = None
    return info


def probe_venvs():
    """已知的独立虚拟环境：它们的 torch 构建决定了能不能用 GPU。"""
    found = []
    candidates = sorted(Path.home().glob('*/venv')) + sorted(Path.home().glob('*/*/venv')) \
        + sorted(Path.home().glob('.venvs/*')) + sorted(Path.home().glob('*/.venv'))
    for venv in candidates:
        python = venv / 'bin' / 'python'
        if not python.is_file():
            continue
        ok, text = run([str(python), '-c',
                        'import json,torch;'
                        'print(json.dumps({"torch":torch.__version__,'
                        '"build":("rocm/"+torch.version.hip) if getattr(torch.version,"hip",None) '
                        'else ("cuda/"+str(torch.version.cuda) if torch.version.cuda else "cpu-only"),'
                        '"gpu":bool(torch.cuda.is_available())}))'], timeout=90)
        entry = {'venv': str(venv)}
        if ok and text.startswith('{'):
            entry.update(json.loads(text))
        else:
            entry['note'] = text[:200] or 'torch 不可用'
        found.append(entry)
    return found


def probe_assets():
    """资产根：主目录之外、不归档、不被 GC 的持久位置。只读清单，不写文件。"""
    result = {'root': str(ASSET_ROOT), 'exists': ASSET_ROOT.is_dir(), 'in_repo': False,
              'models': [], 'tools': [], 'total_bytes': 0}
    try:
        result['in_repo'] = '.unison' in ASSET_ROOT.parts or 'AI_MultiAgent_Collaboration' in ASSET_ROOT.parts
    except Exception:                                          # noqa: BLE001
        pass
    if not result['exists']:
        return result
    manifest = ASSET_ROOT / 'manifest.json'
    if manifest.is_file():
        try:
            result['manifest'] = json.loads(manifest.read_text())
        except Exception as exc:                               # noqa: BLE001
            result['manifest_error'] = str(exc)
    for path in sorted((ASSET_ROOT / 'models').rglob('*')) if (ASSET_ROOT / 'models').is_dir() else []:
        if path.is_file():
            size = path.stat().st_size
            result['models'].append({'path': str(path.relative_to(ASSET_ROOT)), 'bytes': size})
            result['total_bytes'] += size
    for path in sorted((ASSET_ROOT / 'bin').glob('*')) if (ASSET_ROOT / 'bin').is_dir() else []:
        if path.is_file():
            result['tools'].append({'path': str(path.relative_to(ASSET_ROOT)),
                                    'bytes': path.stat().st_size,
                                    'executable': os.access(path, os.X_OK)})
    return result


def probe_weight_roles():
    """验明每个权重的**真实角色**，并与 manifest 的声明对照。

    这是"少一个参数就出废图"那类事故的第一道闸：清单说某文件是 sd15 checkpoint，
    文件头却显示它是 LoRA —— 谁把后者喂给 `-m`，谁就会拿到 VAE tensor 报错 + 废图。
    """
    result = {'checked': [], 'mismatches': [], 'error': _SIBLING_ERROR}
    if _weights is None:
        return result
    models_dir = ASSET_ROOT / 'models'
    if not models_dir.is_dir():
        return result
    for path in sorted(models_dir.rglob('*.safetensors')):
        try:
            result['checked'].append(_weights.classify(path))
        except Exception as exc:                                # noqa: BLE001
            result['checked'].append({'path': str(path), 'role': 'unknown',
                                      'error': f'{type(exc).__name__}:{exc}'})
    manifest = ASSET_ROOT / 'manifest.json'
    comparison = _weights.check_against_manifest(result['checked'], str(manifest)) if manifest.is_file() else None
    if comparison:
        result['mismatches'] = comparison.get('mismatches') or []
    return result


def probe_outputs(paths):
    """对调用方给的生成图跑退化检查（纯色/竖纹/废图）。没有给就不做。"""
    if _imagecheck is None:
        return {'error': _SIBLING_ERROR}
    return {'images': [_imagecheck.analyse(p) for p in paths or []]}


def probe_storage():
    out = {}
    for label, path in (('home', Path.home()), ('root', Path('/')), ('assets', ASSET_ROOT)):
        try:
            usage = shutil.disk_usage(path if path.exists() else path.parent)
            out[label] = {'path': str(path), 'total_gb': round(usage.total / 2**30, 1),
                          'free_gb': round(usage.free / 2**30, 1),
                          'used_pct': round(100 * usage.used / usage.total)}
        except Exception as exc:                               # noqa: BLE001
            out[label] = {'path': str(path), 'error': str(exc)}
    return out


def probe_repo_data():
    """提醒：.unison 是**归档来源**且对象会被 GC，不能当存储用。"""
    data = Path.cwd() / '.unison'
    info = {'path': str(data), 'exists': data.is_dir(), 'warning':
            '数据目录是归档来源，objects 还会被 mark-and-sweep 回收（无保留期）：'
            '权重/数据集不要放这里，放到资产根。'}
    return info


def collect(check_outputs=()):
    return {
        'pci': probe_pci(),
        'drm': probe_drm(),
        'nvidia': probe_nvidia(),
        'rocm': probe_rocm(),
        'vulkan': probe_vulkan(),
        'python': probe_python_gpu(),
        'venvs': probe_venvs(),
        'assets': probe_assets(),
        'weight_roles': probe_weight_roles(),
        'outputs': probe_outputs(check_outputs) if check_outputs else None,
        'storage': probe_storage(),
        'gpu_load': probe_gpu_load(),
        'repo_data_dir': probe_repo_data(),
        'verdict': None,
    }


def verdict(report):
    """给出"现在能用什么"的结论，而不是罗列一堆原始字段。"""
    lines = []
    vulkan_ok = report['vulkan']['loader'] and bool(report['vulkan'].get('icds'))
    sd_tools = [t for t in report['assets']['tools']
                if Path(t['path']).name.startswith('sd-') and t.get('executable')]
    torch_gpu = bool(report['python'].get('torch_gpu_available'))
    if sd_tools:
        lines.append('可用：stable-diffusion.cpp（Vulkan）——本地生图/生视频，不需要 Python 张量栈')
    if vulkan_ok:
        lines.append('可用：Vulkan 后端已就绪（ICD: ' + ', '.join(report['vulkan']['icds']) + '）')
    else:
        lines.append('不可用：Vulkan 未就绪（缺 loader 或 ICD）')
    if report['rocm']['installed']:
        lines.append('已安装：ROCm')
    else:
        lines.append('未安装：ROCm（PyTorch/hip 路线目前只找到 cpu-only 构建）')
    if torch_gpu:
        lines.append('可用：当前解释器的 torch 能识别 GPU')
    else:
        build = report['python'].get('torch_build')
        lines.append('不可用：当前解释器 torch ' + (f'是 {build} 构建' if build else '未安装'))
    if report['vulkan'].get('tools', {}).get('vulkaninfo'):
        lines.append('可用：vulkaninfo（可核对设备与 fp16/coopmat 能力）')
    load = report.get('gpu_load') or {}
    busy = load.get('busy_processes') or []
    free_gb = [round((d.get('vram_free_bytes') or 0) / 2**30, 1) for d in load.get('devices', [])
               if d.get('vram_total_bytes')]
    holders = ', '.join(sorted({n for b in busy for n in b['names']}))
    lines.append('本地算力：%s；显存空闲 %s GiB%s%s' % (
        '空闲（占用率与显存都低）' if load.get('idle') else '**正在被占用**（占用率高或显存吃紧，本地任务会排队）',
        '/'.join(str(x) for x in free_gb) or '未知',
        '；持有计算节点的进程：' + holders if holders else '',
        '（持有节点不等于满载，桌面常驻程序也会持有）' if holders and load.get('idle') else ''))
    unavailable = [t for t, ok in report['vulkan'].get('tools', {}).items() if not ok]
    if unavailable:
        lines.append('缺少（不影响 Vulkan 出图，但排查时有用）：' + ', '.join(unavailable))
    roles = report.get('weight_roles') or {}
    loras = [w for w in roles.get('checked') or [] if (w.get('lora_tensor_fraction') or 0) > 0.5]
    partial = [w for w in roles.get('checked') or [] if w.get('missing_components')]
    if loras:
        lines.append('资产根里这些文件以 lora_ tensor 为主（不是完整 unet+vae+clip）：'
                     + ', '.join(w['name'] for w in loras)
                     + '；实测填进 `-m` 会报 not in model metadata。')
    if partial:
        lines.append('这些文件缺部件：'
                     + ', '.join('%s（缺 %s）' % (w['name'], '、'.join(w['missing_components']))
                                 for w in partial)
                     + '；实测单独 `-m` 会在加载阶段失败。')
    for item in roles.get('comparison') or []:
        lines.append('清单与文件头并列：%s 清单写「%s」，文件头结构 %s' % (
            item['name'], item['declared_in_manifest'], item['structure_from_header']))
    return lines


def render(report):
    out = []
    for line in report['verdict']:
        out.append('• ' + line)
    pci = report['pci']
    out.append('')
    out.append('显卡：')
    for line in (pci.get('devices') or ['（lspci 不可用：%s）' % pci.get('reason')]):
        out.append('  ' + line)
    out.append('渲染节点：' + (', '.join(report['drm']['nodes']) or '无'))
    out.append('')
    out.append('资产根 %s：%s，%d 个权重（%.1f GiB），%d 个工具' % (
        report['assets']['root'], '存在' if report['assets']['exists'] else '不存在',
        len(report['assets']['models']), report['assets']['total_bytes'] / 2**30,
        len(report['assets']['tools'])))
    for model in report['assets']['models']:
        out.append('  %-58s %6.0f MB' % (model['path'], model['bytes'] / 2**20))
    out.append('')
    out.append('权重结构（读文件头得到的事实，不是清单声明）：')
    roles = (report.get('weight_roles') or {}).get('checked') or []
    if not roles:
        out.append('  （没有可读的 safetensors）')
    for item in roles:
        out.append('  %-56s %s' % (item.get('name'),
                                   _weights.struct_summary(item) if _weights else ''))
    outputs = (report.get('outputs') or {}).get('images') or []
    if outputs:
        out.append('')
        out.append('出图的画面统计量：')
        for item in outputs:
            dims = '%sx%s' % (item.get('width'), item.get('height')) if item.get('width') else '未判读'
            out.append('  %-40s %-12s 主色占比=%s 色数=%s 对比度=%s 块std=%s' % (
                Path(item['path']).name, dims, item.get('dominant_fraction'),
                item.get('distinct_colors'), item.get('local_contrast'), item.get('block_std')))
    out.append('')
    out.append('独立 venv：')
    for venv in report['venvs'] or [{'venv': '（未发现）'}]:
        out.append('  %s torch=%s build=%s gpu=%s' % (venv.get('venv'), venv.get('torch'),
                                                      venv.get('build'), venv.get('gpu')))
    out.append('')
    out.append('磁盘：')
    for label, info in report['storage'].items():
        if 'error' in info:
            out.append('  %-7s %s' % (label, info['error']))
        else:
            out.append('  %-7s %s 可用 %.1fG / %.1fG（已用 %d%%）' % (
                label, info['path'], info['free_gb'], info['total_gb'], info['used_pct']))
    out.append('')
    out.append('提示：' + report['repo_data_dir']['warning'])
    return '\n'.join(out)


def bridge(payload):
    """技能端口调用协议：stdin 收 {skill, workspace, base, args}，stdout 回 {ok, result}。

    `args.check_outputs` 可以带一组待核对的工作区相对路径（出图后自查废图）。
    """
    args = payload.get('args') if isinstance(payload, dict) else None
    args = args if isinstance(args, dict) else {}
    requested = args.get('check_outputs') or []
    if isinstance(requested, str):
        requested = [requested]
    workspace = Path(payload.get('workspace') or '.') if isinstance(payload, dict) else Path('.')
    paths = []
    for item in requested:
        candidate = Path(item)
        paths.append(str(candidate if candidate.is_absolute() else workspace / candidate))
    report = collect(check_outputs=paths)
    report['verdict'] = verdict(report)
    return {'ok': True, 'result': report}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', action='store_true', help='输出机器可读 JSON')
    parser.add_argument('--weights', action='store_true',
                        help='逐个权重打印角色判定与用法（-m 能不能用）')
    parser.add_argument('--check-output', action='append', default=[], metavar='PNG',
                        help='核对一张生成图是否退化（纯色/竖纹/废图），可重复')
    parser.add_argument('--call', action='store_true',
                        help='按技能端口调用协议执行：stdin 收 JSON，stdout 回 {ok,result}')
    args = parser.parse_args(argv)
    if args.call:
        # 桥接模式**显式**声明，不靠"stdin 有没有数据"猜：在非交互 shell 里 stdin 永远是
        # 一个打开的管道（EOF 时 select 也报可读），猜错就会把直接运行变成端口调用。
        try:
            payload = json.loads(sys.stdin.read() or '{}')
        except ValueError:
            payload = {}
        print(json.dumps(bridge(payload), ensure_ascii=False))
        return 0
    report = collect(check_outputs=args.check_output)
    report['verdict'] = verdict(report)
    if args.weights and not args.json:
        if _weights is None:
            print(f'权重判别不可用：{_SIBLING_ERROR}', file=sys.stderr)
        else:
            print()
            print('── 权重逐个判定 ──')
            _weights.main([str(p) for p in sorted((ASSET_ROOT / 'models').rglob('*.safetensors'))])
            print()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(render(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

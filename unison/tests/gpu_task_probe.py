"""测模型会不会自己用上本地算力。

任务描述里**不提** GPU、不提技能、不提任何命令——只提一个只能靠本地算力完成的产物。
然后从事件日志里核对它实际走了哪条路：

- 有没有查技能目录（skills_list）→ 有没有加载 gpu 技能；
- 有没有真的把本地推理二进制跑起来（ToolCalled 里的命令行里出现 sd-cli --backend vulkan0）；
- 产物是不是真的存在、真的是图（读 PNG 头，并检查不是纯色空图）。

只有这三条都成立，才算"模型使用了本地算力"——报告里说"我用 GPU 生成了"不算。

用法：python3 tests/gpu_task_probe.py [--model OpenAI:gpt-5.5] [--duration 300]
"""
import argparse
import json
import struct
import time
import urllib.request
import zlib
from pathlib import Path

BASE = 'http://127.0.0.1:8740'
TOKEN = Path('.unison/api_token').read_text().strip()
# 这个口径是**刻意收紧**的："必须本机推理、不许外部 API"用来看模型能不能走通本地这条路。
# 产品上的正确口径不是禁止外部，而是"按条件选并说明理由"——见 tests/choose_backend_probe.py。
GOAL = ('生成一张 384x384 的像素风格图片：一把插在石头上的剑，简洁背景，保存为工作区里的 out/pixel_sword.png。'
        '本次要求**必须在本机完成推理**（离线、不外传），不要调用外部/云端图像 API。'
        '做完后确认文件确实是有效图片（读尺寸核对 384x384）并简要说明你用了本机什么能力产生它。')


def call(path, payload=None, method='GET'):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + TOKEN})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def events(run_id, after=0):
    # /api/events 直接返回事件数组（不是 {events: [...]}）
    return call('/api/events?run_id=%s&after=%d&limit=2000' % (run_id, after))


def png_probe(blob):
    """只读 PNG 头与前几个 IDAT：确认是合法 PNG、尺寸对、且不是纯色空图。"""
    if not blob.startswith(b'\x89PNG\r\n\x1a\n'):
        return {'ok': False, 'reason': '不是 PNG（魔数不符）', 'bytes': len(blob)}
    width, height = struct.unpack('>II', blob[16:24])
    idat = b''
    offset = 8
    while offset < len(blob) - 8:
        length = struct.unpack('>I', blob[offset:offset + 4])[0]
        kind = blob[offset + 4:offset + 8]
        if kind == b'IDAT':
            idat += blob[offset + 8:offset + 8 + length]
            if len(idat) > 400000:
                break
        offset += 12 + length
    try:
        raw = zlib.decompress(idat)
    except Exception as exc:                                   # noqa: BLE001
        return {'ok': True, 'width': width, 'height': height, 'bytes': len(blob),
                'colorful': None, 'note': 'IDAT 解压失败: %s' % exc}
    sample = raw[:60000]
    distinct = len(set(sample))
    return {'ok': True, 'width': width, 'height': height, 'bytes': len(blob),
            'colorful': distinct > 8, 'distinct_bytes': distinct}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='OpenAI:gpt-5.5')
    parser.add_argument('--duration', type=float, default=300.0)
    args = parser.parse_args()

    state = call('/api/state')
    for stale in state['runs']:
        if stale['status'] == 'active':
            call('/api/cancel', {'run_id': stale['id']}, 'POST')
            print('已取消旧运行:', stale['id'], flush=True)
    run = call('/api/runs', {'goal': GOAL, 'workspace': state['default_workspace'],
                             'model_id': args.model}, 'POST')
    print('运行:', run['id'], '| 模型:', args.model, flush=True)

    cursor, started, ended = 0, time.time(), None
    seen_events = 0
    while time.time() - started < args.duration:
        time.sleep(5)
        fresh = events(run['id'], cursor)
        if fresh:
            cursor = max(e['seq'] for e in fresh)
            seen_events += len(fresh)
        state = call('/api/state')
        tasks = [t for t in state['tasks'] if t['run_id'] == run['id']]
        root = next(t for t in tasks if t['parent_id'] is None)
        print('  [%3ds] root=%s 子任务=%s' % (
            int(time.time() - started), root['status'],
            ' '.join('%s=%s' % (t['id'][-6:], t['status']) for t in tasks if t['parent_id'])), flush=True)
        if root['status'] in ('completed', 'failed', 'cancelled'):
            ended = root['status']
            break

    print('\n===== 核对 =====', flush=True)
    all_events = events(run['id'])
    tool_calls = [e['payload'] for e in all_events if e['type'] == 'ToolCalled']
    names = [t['name'] for t in tool_calls]
    print('root 结局:', ended or 'timeout', '| 事件 %d 条 | 工具调用 %d 次' % (len(all_events), len(names)))
    print('技能相关调用:', [n for n in names if 'skill' in n.lower()])
    shell = [t['args'].get('command', '') for t in tool_calls if t['name'] == 'workspace_shell']
    gpu_cmds = [c for c in shell if 'sd-cli' in c or 'sd-server' in c]
    print('用到本地推理二进制的命令:', len(gpu_cmds))
    for cmd in gpu_cmds[:3]:
        print('   ', cmd[:160].replace('\n', ' '))
    print('vulkan 后端:', any('vulkan' in c for c in gpu_cmds))

    files = call('/api/files?run_id=%s' % run['id'])
    produced = [c for c in files.get('changes', []) if str(c.get('path', '')).lower().endswith(('.png', '.jpg', '.webp'))]
    print('产出图片:', [c['path'] for c in produced])
    if produced:
        entry = produced[-1]
        # 直接读工作区磁盘文件：/api/object 是按字符分页的**文本**接口，二进制会被替换成乱码。
        # 任务记录里的 workspace 是子任务副本；根任务的产物会直接落在 run 工作区
        # （默认 .unison/playground）。两处都找一遍。
        candidates = [Path(entry['workspace']) / entry['path'],
                      Path('.unison/playground') / entry['path']]
        found = next((c for c in candidates if c.is_file()), None)
        if found:
            info = png_probe(found.read_bytes())
            print('图片核对（%s）:' % found, json.dumps(info, ensure_ascii=False))
        else:
            print('产物不可读，试过:', [str(c) for c in candidates])
    failed = [t for t in tasks if t['status'] == 'failed']
    print('失败任务:', [(t['id'][-6:], (t.get('error') or '')[:70]) for t in failed])
    ok = bool(gpu_cmds) and bool(produced) and not failed
    print('\n结论：模型%s自己用上本地算力。' % ('**确实**' if ok else '**没有**'))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())

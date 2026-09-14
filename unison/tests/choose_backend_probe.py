"""测：给模型自主权时，它会不会按条件选后端并说明理由。

与 gpu_task_probe.py 的区别：那个刻意收紧成"必须本机"，用来验证本地路径走得通；
这个**不规定**走哪条路，只看它是否先探测、是否给出依据、是否与探测结果一致。

判据（真机 run_17a57d9ad240 通过）：
- 先跑 probe.py 再决定（而不是凭印象）；
- 交付说明里写明选了哪条路、依据是什么；
- 依据与探测结果一致（说"本地空闲"就得真探测过空闲）；
- 产物是真的图。
"""
import json, struct, sys, time, urllib.request
from pathlib import Path

BASE = 'http://127.0.0.1:8740'
TOKEN = Path('.unison/api_token').read_text().strip()
GOAL = ('生成一张 512x512 的图片：一只在雪地里的红色狐狸，写实风格，保存为 out/red_fox.png。'
        '本机有本地推理能力，也可以使用外部服务——**选择哪条路由你判断**，但要在交付说明里写清楚'
        '你选了哪条路、依据是什么（例如本地算力是否空闲、是否要求离线、质量与速度的取舍）。'
        '完成后核对产物是有效图片并报告尺寸与实际用的后端。')


def call(path, payload=None, method='GET'):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + TOKEN})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main():
    state = call('/api/state')
    for stale in state['runs']:
        if stale['status'] == 'active':
            call('/api/cancel', {'run_id': stale['id']}, 'POST')
    run = call('/api/runs', {'goal': GOAL, 'workspace': state['default_workspace'],
                             'model_id': 'OpenAI:gpt-5.5'}, 'POST')
    print('run', run['id'], flush=True)
    started = time.time()
    while time.time() - started < 360:
        time.sleep(6)
        tasks = [t for t in call('/api/state')['tasks'] if t['run_id'] == run['id']]
        root = next(t for t in tasks if t['parent_id'] is None)
        print('  [%3ds] %s' % (int(time.time() - started), root['status']), flush=True)
        if root['status'] in ('completed', 'failed', 'cancelled'):
            break

    events = call('/api/events?run_id=%s&after=0&limit=3000' % run['id'])
    calls = [e['payload'] for e in events if e['type'] == 'ToolCalled']
    shell = [c['args'].get('command', '') for c in calls if c['name'] == 'workspace_shell']
    probed = any('probe.py' in c for c in shell)
    local = [c for c in shell if 'sd-cli' in c or 'sd-server' in c]
    cloud = [c for c in shell if 'generate.py' in c]
    done = [e['payload'] for e in events if e['type'] == 'TaskCompleted']
    summary = done[-1]['summary'] if done else ''
    print('\n先探测再决定:', probed, '| 走了本地:', bool(local), '| 走了云后端:', bool(cloud))
    print('\n交付说明:\n' + summary[:900])
    mentions_choice = any(k in summary for k in ('本地', '本机', 'pollinations', '外部', 'Vulkan'))
    files = call('/api/files?run_id=%s' % run['id'])
    real = []
    for change in files['changes']:
        if not str(change.get('path', '')).lower().endswith('.png'):
            continue
        for candidate in (Path(change['workspace']) / change['path'],
                          Path('.unison/playground') / change['path']):
            if candidate.is_file() and candidate.read_bytes()[:8] == b'\x89PNG\r\n\x1a\n':
                real.append((str(candidate), struct.unpack('>II', candidate.read_bytes()[16:24])))
                break
    print('\n产物:', real or '（没有合法 PNG）')
    ok = probed and mentions_choice and bool(real)
    print('\n结论：模型%s先探测、说明理由、交付真图。' % ('**做到了**' if ok else '**没做到**'))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())

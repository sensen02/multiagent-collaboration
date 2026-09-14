"""用真实模型跑一局猜词游戏，并在外部扮演玩家（与 dsh 用户所在的这一侧等价）。

原因：模型 A/B 提问后只会 yield 等待——那个"下一句提问"本来来自人类。脚本在外部
轮询 `/api/state`，发现某个玩家任务在等待/排队时就投一句"该你说了"，其余一律不动：

- 只给玩家打气，**不替玩家出题、也不替 C 回答**（否则就不是测系统而是测脚本）；
- 只在玩家确实挂起（waiting/queued）时才投递，运行中绝不插入；
- 通过端口调用（POST /api/runs、/api/message），与其它外部程序走同一条路。

用法：
    python3 tests/game_driver.py --wake 30 --duration 420
"""
import argparse
import json
import time
import urllib.request
from pathlib import Path

BASE = 'http://127.0.0.1:8740'
UNDERCOVER = ('用内鬼游戏检验多模型协作：4 个子智能，其中 3 个拿到同一个平民词、1 个拿到相近的内鬼词，'
              '每人不知道自己是哪种。轮流用一句话描述自己的词（不能直接说出该词），一轮描述结束后所有人投票投出最可疑的人。'
              '**共享信息必须走共享层，不要抄进任务描述**：每人的描述用 broadcast(scope="run", notify=[其他三人]) 发布，'
              '每个人读公共记录用 knowledge_read(scope="run", from_seq=0)——不要把它复制到任何子任务的 goal 里。'
              '最后汇总谁被投出、内鬼是否被抓住。每次调用尽量简短。')

GOAL = ('用猜词游戏检验多模型协作：让子模型各司其职玩一局"二十问"，控制在 8 轮问答以内。'
        '出题人 C 自选一个常见具体名词作谜底并保密（只给类别/用途级别的粗略指引）；'
        '玩家 A 和玩家 B 轮流出是非题，只能靠"是/否/不清楚"逐步缩小范围，各自独立推理，不要互相抄答案。'
        '所有提问与回答都用 agents_send 直连，不要经过中转。'
        'A 或 B 猜出谜底即结束；最后汇报每轮问答、谁猜中以及协作中出现的问题。'
        '注意：每次调用尽量简短，不要长篇复述。')


TOKEN = Path('.unison/api_token').read_text().strip()


def call(path, payload=None, method='GET'):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN}
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verify(run_id):
    """验收断言：所有成员读到同一份共享记录，且没人把共享信息抄进任务描述。"""
    state = call('/api/state')
    payload = call('/api/knowledge?run_id=%s&scope=run&from_seq=0' % run_id)
    entries = payload['entries']
    print('共享条目: %d 条' % len(entries), flush=True)
    for entry in entries[:12]:
        print('  seq=%s [%s] %s :: %s' % (entry['seq'], (entry.get('author') or '?')[-8:],
                                          entry['title'][:20], (entry['content'] or '')[:55]), flush=True)
    tasks = [t for t in state['tasks'] if t['run_id'] == run_id]
    # 有序枚举本身就该是同一份：同一端点连读多次必须逐字节一致（不存在相关度/截断带来的差异）。
    reads = [json.dumps(call('/api/knowledge?run_id=%s&scope=run&from_seq=0' % run_id)['entries'],
                        ensure_ascii=False, sort_keys=True) for _ in range(3)]
    print('重复读取是否逐字节一致:', len(set(reads)) == 1, flush=True)
    # 每个子任务是否真的被推送过（收件箱里能看到），而不是"谁去搜谁才有"。
    print('共享条目作者:', flush=True)
    for entry in entries:
        print('   seq=%s by %s' % (entry['seq'], (entry.get('author') or '?')[-8:]), flush=True)
    divergence = payload['divergence']
    late = [d for d in divergence if d['kind'] == 'late_injection']
    unnotified = [d for d in divergence if d['kind'] == 'unnotified_member']
    print('late_injection:', len(late), '| unnotified_member:', len(unnotified), flush=True)
    for item in late[:4]:
        print('   抄写:', item['task'][-8:], '相似度', item['similarity'], flush=True)
    failed = [t for t in tasks if t['status'] == 'failed']
    print('失败任务:', [(t['id'][-8:], (t.get('error') or '')[:50]) for t in failed], flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wake', type=float, default=30.0, help='同一个玩家两次打气的最小间隔（秒）')
    parser.add_argument('--duration', type=float, default=420.0, help='整局最长时间（秒）')
    parser.add_argument('--model', default='OpenAI:gpt-5.5')
    parser.add_argument('--game', choices=['twenty', 'undercover'], default='twenty')
    args = parser.parse_args()

    state = call('/api/state')
    workspace = state['default_workspace']
    # 上一局可能因为主调度没走到收束而仍是 active（会占用工作区目录），先清干净。
    for stale in state['runs']:
        if stale['status'] == 'active':
            call('/api/cancel', {'run_id': stale['id']}, 'POST')
            print('已取消未收束的旧运行:', stale['id'], flush=True)
    goal = UNDERCOVER if args.game == 'undercover' else GOAL
    run = call('/api/runs', {'goal': goal, 'workspace': workspace, 'model_id': args.model}, 'POST')
    print('新运行:', run['id'], '| 工作区:', workspace, flush=True)

    played = {}
    ended = None
    started = time.time()
    while time.time() - started < args.duration:
        time.sleep(6)
        try:
            state = call('/api/state')
        except Exception as exc:                       # 服务端短暂不可用不该终止整局
            print('  轮询失败:', exc, flush=True)
            continue
        tasks = {t['id']: t for t in state['tasks']}
        current = [t for t in tasks.values() if t['run_id'] == run['id']]
        if not current:
            continue
        root = tasks[run['root_task']]
        children = [t for t in current if t['parent_id'] == run['root_task']]
        elapsed = int(time.time() - started)
        cells = ' '.join(f"{t['goal'][:8]}={t['status']}" for t in sorted(children, key=lambda x: x['goal']))
        print(f'  [{elapsed:3d}s] root={root["status"]} {cells}', flush=True)
        if root['status'] in ('completed', 'failed', 'cancelled'):
            ended = root['status']
            break
        for task in children:
            # 只给玩家打气；出题人由玩家直接联系，不需要外部推进。
            is_player = '玩家' in task['goal']
            if not is_player or task['status'] not in ('waiting', 'queued'):
                continue
            if time.time() - played.get(task['id'], 0) < args.wake:
                continue
            played[task['id']] = time.time()
            try:
                nudge = ('轮到你了：先 knowledge_read(scope="run", from_seq=0) 读公共记录，'
                         '然后用一句话描述自己的词（broadcast(scope="run", notify=[其他三人])），'
                         '不要把自己的描述直接发给我。'
                         if args.game == 'undercover' else
                         '轮到你了：提出下一个只能回答"是/否/不清楚"的问题，'
                         '用一句 agents_send 直连问出题人 C；如果你已有把握，就直接猜谜底。')
                call('/api/message', {'task_id': task['id'], 'summary': nudge}, 'POST')
                print(f'    → 给 {task["goal"][:10]} 打气', flush=True)
            except Exception as exc:
                print('    打气失败:', exc, flush=True)

    print('\n整局结束，状态:', ended or 'timeout', '| 用时', int(time.time() - started), 's', flush=True)
    verify(run['id'])
    if ended is None:
        # 主调度没能自己收束时，由外部收掉这一局，避免它一直占着工作区。
        try:
            call('/api/cancel', {'run_id': run['id']}, 'POST')
            print('已取消本局（主调度未在时限内收束）', flush=True)
        except Exception as exc:
            print('取消失败:', exc, flush=True)
    print('查看: GET %s/api/state | 事件时间轴在控制台' % BASE)


if __name__ == '__main__':
    main()

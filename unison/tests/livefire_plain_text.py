"""真机复现：把 C 死亡那一次的**真实请求**重放一遍，验证修复。

数据来源是本地运行库 `.unison/state.sqlite3`：
- seq 2357 `ModelCalled`：C 收到 A 的问题后发起的真实请求（含当时逐字的 messages 与 tools 快照）；
- seq 2367 `MessageDelivered`：B 的问题在 C 推理期间到达（就是这一条把 C 打死的）。

做法：在新的临时运行里建一个任务，把它**播种成 C 当时的真实上下文**，
用一个「在模型调用中途投递 B 的问题」的适配器包住真实模型，然后走 `step()`：

- 修复前：`complete()` 撞上那条未读消息 → `TaskFailed: 仍有 1 条未处理消息`；
- 修复后：这一轮被退回队列（`TextRoundDeferred`），任务继续活着并处理收件箱。

这是真机调用（每样本消耗 1-2 次模型配额），不是确定性假模型。
确定性回归在 tests/test_runtime.py::test_plain_text_with_message_arriving_mid_thought_is_not_fatal

用法：python3 tests/livefire_plain_text.py [--request 2357] [--force-text] [--samples 3]
"""
import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unison.runtime import Runtime  # noqa: E402


def recorded_round(store_path, seq):
    """取出当时那次真实请求的逐字内容（messages / tools 快照）。"""
    import sqlite3
    db = sqlite3.connect(store_path)
    db.row_factory = sqlite3.Row
    row = db.execute("select payload from events where type='ModelCalled' and seq=?", (seq,)).fetchone()
    if row is None:
        raise SystemExit('seq %s 不是一次 ModelCalled' % seq)
    request_id = json.loads(row['payload'])['request_id']
    header_row = db.execute("select body from records where kind='request' and id=?", (request_id,)).fetchone()
    if header_row is None:
        raise SystemExit('请求信封已不在记录里')
    header = json.loads(header_row['body'])

    def blob(ref):
        return (Path(store_path).parent / 'objects' / ref).read_bytes()

    return {'header': header,
            'messages': json.loads(blob(header['input_ref'])),
            'tools': json.loads(blob(header['tools_ref']))}


def model_config(store_path, model_id):
    import sqlite3
    db = sqlite3.connect(store_path)
    row = db.execute("select body from records where kind='model' and id=?", (model_id,)).fetchone()
    if row is None:
        raise SystemExit('模型 %s 不在记录里' % model_id)
    return json.loads(row[0])


async def one_sample(args, original, config, model_id):
    """跑一个样本。返回 True 表示任务仍然被打死（修复未生效）。"""
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / 'project').mkdir()
        runtime = Runtime(root / 'data', concurrency=4, batch_seconds=.01)
        runtime.store.put('model', config)
        run = runtime.create_run('真机复现：并发提问下的纯文本答复', str(root / 'project'), model_id)
        parent = runtime.task(run['root_task'])
        created = await runtime.tool_tasks_submit(
            parent, {'goal': '你是持续主持人；玩家会不断提问，请逐个回答。', 'model_id': model_id})
        child = runtime.task(created['task_id'])
        child.update(status='running', epoch=1, history_messages=0)
        runtime.store.put('task', child)
        messages = list(original['messages'])
        if args.force_text:
            messages.append({'role': 'user',
                             'content': '本轮请只输出一个字的纯文本答案（例如 是 或 否），不要调用任何工具，不要解释。'})
        runtime.seed_history(child, messages, source='livefire-replay')
        runtime.store.put('task', runtime.task(child['id']))

        delivered = {'done': False}

        async def adapter(config_, messages_, tools):
            if not delivered['done']:
                # B 的问题正好在 C 推理期间到达——与 seq 2367 一样用 wake 投递。
                delivered['done'] = True
                runtime.deliver(child['id'],
                                '玩家 B 新问题：它通常需要由人直接手持使用吗？请严格只回答 是、否或不清楚。',
                                sender=parent['id'], delivery='wake', topic='question', kind='game')
            return await runtime.models.invoke(config_['api'], config_, messages_, tools or None)

        runtime.models.adapters[config['adapter']] = adapter
        for epoch in range(1, args.rounds + 1):
            fresh = runtime.task(child['id'])
            if fresh['status'] in ('completed', 'failed', 'cancelled'):
                print('  第 %d 轮前任务已终态：%s' % (epoch, fresh['status']))
                break
            fresh.update(status='running', epoch=epoch)
            runtime.store.put('task', fresh)
            await runtime.step(child['id'], epoch)

        fresh = runtime.task(child['id'])
        events = [e['type'] for e in runtime.store.events()]
        failed = [e for e in runtime.store.events() if e['type'] == 'TaskFailed']
        assistant = [e['payload']['message'] for e in runtime.store.events()
                     if e['type'] == 'MessageAppended' and e['task_id'] == child['id']
                     and e['payload'].get('source') == 'assistant']
        tools_used = [e['payload']['name'] for e in runtime.store.events() if e['type'] == 'ToolCalled']
        unread = [m for m in runtime.store.all('message') if m['task_id'] == child['id'] and not m['consumed']]
        replied = [m for m in runtime.store.all('message') if m.get('from_task') == child['id']]
        print('  状态=%s 未读=%d TextRoundDeferred=%d TaskFailed=%d' % (
            fresh['status'], len(unread), events.count('TextRoundDeferred'), len(failed)))
        print('  模型输出：' + json.dumps([m.get('content') for m in assistant], ensure_ascii=False)[:220])
        print('  工具调用：%s | 主动发出的消息：%d 条%s' % (
            tools_used, len(replied),
            '：' + json.dumps([m.get('summary') for m in replied], ensure_ascii=False)[:160] if replied else ''))
        for item in failed:
            print('  失败原因：', item['payload'].get('error'))
        await runtime.stop()
        return bool(failed)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', type=int, default=2357, help='ModelCalled 事件序号')
    parser.add_argument('--store', default='.unison/state.sqlite3')
    parser.add_argument('--force-text', action='store_true',
                        help='追加一条指令，逼真实模型只回纯文本（复现 C 的死法）')
    parser.add_argument('--samples', type=int, default=1)
    parser.add_argument('--rounds', type=int, default=1, help='每个样本连续跑几轮（看能否自行恢复）')
    args = parser.parse_args()

    original = recorded_round(args.store, args.request)
    model_id = original['header']['model_id']
    print('重放请求 %s → 模型 %s，%d 条消息 / %d 个工具' % (
        original['header']['id'], model_id, len(original['messages']), len(original['tools'])))
    if args.force_text:
        print('已追加「只回纯文本」指令（复现 C 的死法）')
    config = model_config(args.store, model_id)

    failures = 0
    for sample in range(args.samples):
        print('\n===== 样本 %d/%d =====' % (sample + 1, args.samples))
        if await one_sample(args, original, config, model_id):
            failures += 1
    print('\n总计：%d 个样本，%d 个仍然被并发到达的消息打死' % (args.samples, failures))
    if not failures:
        print('结论：并发到达的消息没有再把任务打死。')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))

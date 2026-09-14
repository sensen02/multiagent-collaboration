"""共享信息层：作用域、全序、推送，以及"信息分叉"的早期可见性。

判据来自真机 `run_574484cad16f`（内鬼游戏）：主调度没有公共记录可用，只能把发言
复制进各个子任务的 goal——C 的 goal 缺 B 的发言、D 的 goal 缺 C 的发言，
时间轴上给 A 和给 B 的"完整发言"还是两次独立拼装。复制一次就产生一个分叉。
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from unison.runtime import Runtime
from unison.store import dumps, now
from unison.demo import demo_call


class KnowledgeScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / 'project'; self.project.mkdir()
        (self.project / 'README.md').write_text('original\n')
        self.r = Runtime(Path(self.temp.name) / 'data', concurrency=1, batch_seconds=.01)
        self.r.models.adapters['fake'] = demo_call
        self.r.store.put('model', {'id': 'test', 'model': 'test', 'adapter': 'fake',
                                   'base_url': 'test://local', 'no_key': True,
                                   'context_window': 64000, 'max_output': 4096})

    async def asyncTearDown(self):
        await self.r.stop(); self.temp.cleanup()

    def create(self, goal='test goal'):
        run = self.r.create_run(goal, str(self.project), 'test')
        return run, self.r.task(run['root_task'])

    def children(self, run, names):
        return [self.r.create_task(run, name, 'test', self.r.task(run['root_task'])) for name in names]

    async def test_run_scope_entries_are_not_indexed_into_task_briefs(self):
        """run 域条目是本次运行内的一次性信息，不能进后续任务的简报索引。"""
        run, root = self.create()
        await self.r.tool_broadcast(root, {'title': '公告', 'content': '本局发言：它是圆的。',
                                           'scope': 'run'})
        fresh = self.r.create_task(run, 'later task', 'test', root)
        brief = json.loads(next(e['payload']['message']['content'] for e in self.r.store.events(run_id=run['id'])
                                if e['task_id'] == fresh['id'] and e['type'] == 'MessageAppended'
                                and e['payload'].get('source') == 'task-brief'))
        # 正文不进索引，只告诉它"有多少条、怎么读"。
        self.assertNotIn('它是圆的', dumps(brief))
        self.assertTrue(any(isinstance(item, dict) and 'shared_entries' in item
                            for item in brief['knowledge_index']), brief['knowledge_index'])

    async def test_entry_sequence_is_monotonic_and_enumeration_is_ordered(self):
        """同一秒内发布的多条也必须有序：靠 seq，而不是浮点秒 created。"""
        run, root = self.create()
        for text in ('第一条', '第二条', '第三条'):
            self.r.publish_knowledge(root, {'content': text, 'scope': 'run'})
        entries = self.r.knowledge_entries(root, scope='run')
        self.assertEqual([e['content'] for e in entries], ['第一条', '第二条', '第三条'])
        self.assertEqual([e['seq'] for e in entries], sorted(e['seq'] for e in entries))
        # 同一秒（created 相同）也照样有序
        stamps = {round(e['created'], 6) for e in entries}
        self.assertTrue(len(stamps) <= len(entries))

    async def test_notify_fans_out_exactly_once_and_is_terminal(self):
        """推送＝一次扇出：每个目标恰好一份，且是**通知**不是提问（否则又会生成待答请求）。"""
        run, root = self.create()
        a, b = self.children(run, ['A', 'B'])
        entry = await self.r.tool_broadcast(root, {'title': '我的描述', 'content': '它通常是圆的。',
                                                   'scope': 'run', 'notify': [a['id'], b['id']]})
        self.assertEqual(sorted(entry['notified']), sorted([a['id'], b['id']]))
        for task in (a, b):
            copies = [m for m in self.r.store.all('message')
                      if m['task_id'] == task['id'] and m.get('kind') == 'shared-entry']
            self.assertEqual(len(copies), 1, copies)
            self.assertEqual(copies[0]['status'], 'terminal')
            self.assertIn('它通常是圆的', copies[0]['summary'])
            self.assertFalse([x for x in self.r.store.all('answer_pending')
                              if x.get('target') == task['id'] and x.get('status') == 'open'
                              and x.get('kind') == 'shared-entry'])

    async def test_two_members_read_byte_identical_slices(self):
        """所有人按序号读到的必须是同一份（逐字节）。"""
        run, root = self.create()
        a, b, c, d = self.children(run, ['A', 'B', 'C', 'D'])
        for i, who in enumerate((a, b, c, d)):
            self.r.publish_knowledge(who, {'content': f'玩家{i+1}的描述：第{i+1}句。', 'scope': 'run'})
        slices = [dumps(self.r.knowledge_entries(task, scope='run')) for task in (a, b, c, d)]
        self.assertEqual(len(set(slices)), 1)

    async def test_search_is_not_a_reliable_shared_view(self):
        """反例固化：knowledge_search 是检索（相关度排序 + 截断），不能当"同一份记录"用。"""
        run, root = self.create()
        for i in range(25):
            self.r.publish_knowledge(root, {'content': f'第{i}条共享信息', 'scope': 'run'})
        searched = self.r.knowledge_search('共享', root)          # 默认只回 limit 条
        enumerated = self.r.knowledge_entries(root, scope='run')
        self.assertEqual(len(enumerated), 25)
        self.assertLess(len(searched), len(enumerated))
        self.assertTrue(all(e['content'].startswith('第') for e in enumerated))

    async def test_late_injection_is_reported(self):
        """照抄真机形态：把共享条目原文拼进另一个任务的 goal → 必须被报出来。"""
        run, root = self.create()
        await self.r.tool_broadcast(root, {'title': 'A 的发言',
                                           'content': '它通常在城市地下或高架的固定线路上运行，能缓解地面交通压力。',
                                           'scope': 'run'})
        clean = self.r.create_task(run, '你是玩家C。请自己读取公共记录。', 'test', root)
        self.assertEqual([i for i in self.r.knowledge_divergence(run['id'])
                          if i['kind'] == 'late_injection' and i['task'] == clean['id']], [])
        copied = self.r.create_task(
            run, '你是玩家C。当前已公开的玩家A描述是："它通常在城市地下或高架的固定线路上运行，能缓解地面交通压力。" 请发言。',
            'test', root)
        issues = [i for i in self.r.knowledge_divergence(run['id'])
                  if i['kind'] == 'late_injection' and i['task'] == copied['id']]
        self.assertEqual(len(issues), 1, self.r.knowledge_divergence(run['id']))
        self.assertGreaterEqual(issues[0]['similarity'], 0.85)

    async def test_reciprocal_visibility_is_checked_not_universal_push(self):
        """只查"互相可见"：发过言的人必须收到过别人的条目；不要求推送给运行里每个任务。

        真机 `run_96e0fba2bbb0` 里玩家只 notify 其余三名玩家，调度者靠工具结果就能看到——
        那不算分叉。但如果有人只发不收，它的可见性就取决于"自己去搜"，那才是分叉。
        """
        run, root = self.create()
        a, b, c = self.children(run, ['A', 'B', 'C'])
        # A、B 正常互推；C 只发布、从不接收别人的条目。
        await self.r.tool_broadcast(a, {'content': 'A 的描述：它是圆的。', 'scope': 'run',
                                        'notify': [b['id'], c['id']]})
        await self.r.tool_broadcast(b, {'content': 'B 的描述：它很甜。', 'scope': 'run',
                                        'notify': [a['id'], c['id']]})
        await self.r.tool_broadcast(c, {'content': 'C 的描述：它能榨汁。', 'scope': 'run',
                                        'notify': [a['id'], b['id']]})
        issues = self.r.knowledge_divergence(run['id'])
        self.assertEqual([i for i in issues if i['kind'] == 'unnotified_member'], [])
        # 让 C 变成"只发不收"：清掉它收到的两条共享条目，只留它自己发的那条之外的记录
        for m in self.r.store.all('message'):
            if m['task_id'] == c['id'] and m.get('kind') == 'shared-entry':
                m['kind'] = 'shared-entry-seen'; self.r.store.put('message', m)
        issues = self.r.knowledge_divergence(run['id'])
        flagged = [i for i in issues if i['kind'] == 'unnotified_member']
        self.assertEqual([i['task'] for i in flagged], [c['id']], issues)

    async def test_project_scope_behaviour_is_unchanged(self):
        """project 域行为不变：默认范围、要来源、进索引、指纹去重仍然生效。"""
        run, root = self.create()
        first = self.r.publish_knowledge(root, {'title': '协作说明', 'content': '写了 collaboration.md',
                                                'sources': ['README.md']})
        self.assertEqual(first.get('scope'), 'project')
        self.assertTrue(first.get('seq'))
        again = self.r.publish_knowledge(root, {'title': '协作说明', 'content': '写了 collaboration.md',
                                                'sources': ['README.md']})
        self.assertEqual(again['id'], first['id'])            # 指纹去重
        fresh = self.r.create_task(run, 'later', 'test', root)
        brief = json.loads(next(e['payload']['message']['content'] for e in self.r.store.events(run_id=run['id'])
                                if e['task_id'] == fresh['id'] and e['type'] == 'MessageAppended'
                                and e['payload'].get('source') == 'task-brief'))
        ids = [item.get('id') for item in brief['knowledge_index'] if isinstance(item, dict)]
        self.assertIn(first['id'], ids)

    async def test_invalid_scope_is_rejected_loudly(self):
        run, root = self.create()
        with self.assertRaisesRegex(ValueError, 'scope'):
            self.r.publish_knowledge(root, {'content': 'x', 'scope': 'everywhere'})

    async def test_notify_skips_foreign_tasks_with_a_reason(self):
        """推送到不属于本运行版本的任务时明确记账，而不是静默丢弃。"""
        run_a, root_a = self.create('A 局')
        other = self.project.parent / 'other'; other.mkdir()
        run_b = self.r.create_run('B 局', str(other), 'test')
        entry = await self.r.tool_broadcast(root_a, {'content': '公告', 'scope': 'run',
                                                     'notify': [self.r.task(run_b['root_task'])['id']]})
        self.assertEqual(entry['notified'], [])
        self.assertEqual(entry['not_notified'][0]['reason'], '不属于当前运行版本')


if __name__ == '__main__':
    unittest.main()

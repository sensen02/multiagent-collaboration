"""统一维护层：注册表、调度、预演、状态持久化、GC 标记与知识整理阈值。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from unison.maintenance import (ArchiveRetention, Maintenance, MaintenanceJob, ObjectCollector,
                                WorkspaceScan, built_in_jobs)
from unison.runtime import Runtime
from unison.store import dumps


class StubJob(MaintenanceJob):
    id = 'stub'
    description = '测试作业'
    interval = 100
    loud = True

    def __init__(self, result=None, error=None):
        self.result = result or {'summary': 'ok', 'counts': {'n': 1}}
        self.error = error


async def run_stub(job, store, dry_run=False):
    if job.error: raise RuntimeError(job.error)
    return job.result


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = __import__('unison.store', fromlist=['Store']).Store(Path(self.temp.name) / 'data')

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_registry_rejects_duplicates_and_foreign_objects(self):
        maintenance = Maintenance(self.store)
        job = StubJob(); job.run = run_stub.__get__(job)
        maintenance.register(job)
        with self.assertRaisesRegex(ValueError, '重复'):
            maintenance.register(StubJob())
        with self.assertRaisesRegex(ValueError, 'MaintenanceJob'):
            maintenance.register(type('Bad', (), {'id': 'bad'})())

    def test_due_scheduling_backs_off_after_failures(self):
        maintenance = Maintenance(self.store, min_interval=0)
        job = StubJob(error='boom'); job.run = run_stub.__get__(job)
        maintenance.register(job)
        asyncio.run(maintenance.run_job(job))
        state = maintenance.states['stub']
        self.assertEqual(state['consecutive_errors'], 1)
        self.assertIn('boom', state['last_error'])
        # 失败后下次到期时间被推迟（指数退避），不会每 tick 重试。
        self.assertGreater(maintenance.due_at(job), maintenance.states['stub']['last_run'] + job.interval)
        failed = [e for e in self.store.events() if e['type'] == 'MaintenanceFailed']
        self.assertEqual(len(failed), 1)

    def test_success_clears_error_and_persists_state(self):
        maintenance = Maintenance(self.store, min_interval=0)
        job = StubJob(); job.run = run_stub.__get__(job)
        maintenance.register(job)
        asyncio.run(maintenance.tick(force=True))
        saved = self.store.get('maintenance', 'state')['jobs']['stub']
        self.assertEqual(saved['runs'], 1)
        self.assertIsNone(saved['last_error'])
        self.assertEqual(saved['last_counts'], {'n': 1})
        self.assertTrue([e for e in self.store.events() if e['type'] == 'MaintenanceFinished'])
        # 重新构造调度器时状态从 store 恢复。
        reread = Maintenance(self.store)
        self.assertEqual(reread.states['stub']['runs'], 1)

    def test_destructive_job_never_runs_automatically(self):
        class Destructive(StubJob):
            id = 'destructive'
            destructive = True
            dry_run = False
        maintenance = Maintenance(self.store, min_interval=0)
        job = Destructive(); job.run = run_stub.__get__(job)
        maintenance.register(job)
        # 到期也不会自动执行，只能显式触发。
        self.assertEqual(asyncio.run(maintenance.tick()), [])
        self.assertEqual(len(asyncio.run(maintenance.tick(force=True))), 1)

    def test_dry_run_reaches_supporting_jobs_only(self):
        seen = {}
        class Recorder(StubJob):
            id = 'recorder'
            dry_run = True
        async def record(store, dry_run=False):
            seen['dry'] = dry_run
            return {'summary': 'recorded'}
        maintenance = Maintenance(self.store, min_interval=0)
        job = Recorder(); job.run = record
        maintenance.register(job)
        asyncio.run(maintenance.run_job(job, dry_run=True))
        self.assertTrue(seen['dry'])
        self.assertIs(seen['dry'], True)

    def test_runtime_dependency_injection(self):
        runtime = Runtime(Path(self.temp.name) / 'runtime')
        try:
            scan = runtime.maintenance.find('workspace-scan')
            self.assertTrue(hasattr(scan, 'runtime'), '需要运行时的作业应被注入依赖')
            self.assertEqual(len(runtime.maintenance.jobs), len(built_in_jobs()))
        finally:
            runtime.store.close()

    def test_status_reports_schedule_shape(self):
        maintenance = Maintenance(self.store)
        job = StubJob(); job.run = run_stub.__get__(job)
        maintenance.register(job)
        row = maintenance.status()[0]
        for key in ('id', 'description', 'interval', 'destructive', 'next_due', 'runs'):
            self.assertIn(key, row)
        self.assertGreater(row['next_due'], 0)


class ObjectCollectorTests(unittest.TestCase):
    """GC 的 mark 阶段必须看穿"记录 body 是 JSON 字符串"与"对象里引用对象"两层。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        from unison.store import Store
        from unison.workspace import Workspace
        self.store = Store(Path(self.temp.name) / 'data')
        self.workspace = Workspace(self.store)
        self.project = Path(self.temp.name) / 'project'; self.project.mkdir()
        (self.project / 'README.md').write_text('original\n')
        (self.project / 'sub').mkdir(); (self.project / 'sub' / 'a.py').write_text('x=1\n')

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_task_manifest_objects_are_reachable(self):
        task = {'id': 't1', 'run_id': 'r1', 'revision': 1, 'workspace': str(self.project)}
        self.workspace.setup(task)
        self.store.put('task', task)
        objects = set(p.name for p in self.store.objects.iterdir())
        reachable = ObjectCollector.reachable(self.store)
        self.assertEqual(reachable, objects, '任务清单引用的对象必须全部可达')

    def test_orphan_detected_and_only_removed_without_dry_run(self):
        keep = self.store.blob('keep me')
        self.store.put('task', {'id': 't1', 'run_id': 'r1', 'revision': 1, 'manifest': {'a': keep}})
        orphan = self.store.blob('nobody references this')
        collector = ObjectCollector()
        preview = asyncio.run(collector.run(self.store, dry_run=True))
        self.assertEqual(preview['counts']['orphans'], 1)
        self.assertTrue((self.store.objects / orphan).exists(), '预演不得删除任何对象')
        done = asyncio.run(collector.run(self.store, dry_run=False))
        self.assertEqual(done['counts']['removed'], 1)
        self.assertFalse((self.store.objects / orphan).exists())
        self.assertTrue((self.store.objects / keep).exists(), '被引用的对象必须保留')

    def test_nested_object_references_are_followed(self):
        inner = self.store.blob('inner payload')
        outer = self.store.blob(dumps({'note': 'refers to', 'ref': inner}))
        self.store.put('task', {'id': 't1', 'run_id': 'r1', 'revision': 1, 'manifest': {}, 'extra': outer})
        reachable = ObjectCollector.reachable(self.store)
        self.assertIn(inner, reachable, '文本对象内部的引用也要追到')

    def test_archive_retention_is_preview_by_default(self):
        archives = self.store.root / 'archives'
        (archives / 'run_x-r1-abc.tar.gz').write_bytes(b'x' * 32)
        job = ArchiveRetention()
        preview = asyncio.run(job.run(self.store, dry_run=True))
        self.assertEqual(preview['counts']['archives'], 1)
        self.assertTrue((archives / 'run_x-r1-abc.tar.gz').exists())
        removed = asyncio.run(job.run(self.store, dry_run=False))
        self.assertEqual(removed['counts']['removed'], 1)
        self.assertFalse((archives / 'run_x-r1-abc.tar.gz').exists())


class MaintenanceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / 'project'; self.project.mkdir()
        (self.project / 'README.md').write_text('original\n')
        self.r = Runtime(Path(self.temp.name) / 'data', concurrency=1, batch_seconds=.01)
        from unison.demo import demo_call
        self.r.models.adapters['fake'] = demo_call
        self.r.store.put('model', {'id': 'test', 'model': 'test', 'adapter': 'fake', 'base_url': 'test://local',
                                   'no_key': True, 'context_window': 64000, 'max_output': 4096})

    async def asyncTearDown(self):
        await self.r.stop(); self.temp.cleanup()

    async def test_skill_catalog_job_reports_change_once(self):
        """技能目录作业：只读、重扫，目录变化时写一条事件，否则保持静默。"""
        job = self.r.maintenance.find('skill-catalog')
        self.assertIsNotNone(job)
        self.assertEqual(job.interval, 15 * 60)
        self.assertFalse(job.destructive)
        first = await self.r.maintenance.run_job(job)
        # 不写死数量：内置技能包会随版本增加（原先写死 1，加了第二个技能就误报失败）；
        # 这里按实际内置目录算，并逐个核对都出现在目录里。
        bundled = Path(__file__).resolve().parent.parent / 'unison' / 'skills'
        expected = sorted(p.name for p in bundled.iterdir() if (p / 'SKILL.md').is_file())
        # 不断言"总数 == 内置数"：本机完全可能还有用户级技能（`~/.dsh/skills`，例如
        # `windows-share`），那会让这条断言随开发机的家目录变化而失败——测试不该依赖环境。
        self.assertGreaterEqual(first['counts']['skills'], len(expected))
        self.assertEqual(first['counts']['changed'], 0)         # 首次建立指纹不算"变化"
        for name in expected:
            self.assertIn(name, first['catalog'])
        self.assertFalse([e for e in self.r.store.events() if e['type'] == 'SkillCatalogChanged'])
        # 该作业扫描默认工作区（数据目录下的 playground）。把技能放进这个工作区自己的
        # 技能根，重扫就必须发现它——这正是"模型能看到哪些技能"的实际来源。
        target = self.r.store.root / 'playground' / '.dsh' / 'skills' / 'playground-skill'
        target.mkdir(parents=True)
        (target / 'SKILL.md').write_text('---\nname: playground-skill\ndescription: 工作区技能\n---\n正文\n', encoding='utf-8')
        second = await self.r.maintenance.run_job(job)
        self.assertEqual(second['counts']['changed'], 1)
        self.assertEqual(second['counts']['skills'], first['counts']['skills'] + 1)   # 新增的正是工作区技能
        events = [e for e in self.r.store.events() if e['type'] == 'SkillCatalogChanged']
        self.assertEqual(len(events), 1)
        self.assertIn('playground-skill', events[0]['payload']['skills'])

    async def test_scan_job_uses_stat_shortcut(self):
        run = self.r.create_run('scan', str(self.project), 'test')
        task = self.r.task(run['root_task'])
        (self.project / 'later.txt').write_text('added\n')
        result = await self.r.maintenance.run_job(self.r.maintenance.find('workspace-scan'))
        self.assertEqual(result['counts']['changes'], 1)
        fresh = self.r.task(task['id'])
        self.assertIn('later.txt', fresh['manifest'])
        self.assertIn('later.txt', fresh['file_stats'])
        # 未改动时指纹完全复用（同一 mtime/size），不需要重算哈希。
        before = {k: tuple(v[:2]) for k, v in fresh['file_stats'].items()}
        self.r.workspace.reconcile(self.r.task(task['id']))
        after = {k: tuple(v[:2]) for k, v in self.r.task(task['id'])['file_stats'].items()}
        self.assertEqual(before, after)

    async def test_knowledge_job_reports_threshold(self):
        run = self.r.create_run('know', str(self.project), 'test')
        task = self.r.task(run['root_task'])
        self.r.publish_knowledge(task, {'title': 'fact', 'content': 'body', 'sources': ['README.md']})
        result = await self.r.maintenance.run_job(self.r.maintenance.find('knowledge-maintenance'))
        self.assertEqual(result['counts']['projects'], 1)
        self.assertEqual(result['counts']['needing'], 0)   # 1 条有效知识，不到阈值
        (self.project / 'README.md').write_text('changed\n')
        result = await self.r.maintenance.run_job(self.r.maintenance.find('knowledge-maintenance'))
        self.assertEqual(result['counts']['needing'], 1)   # 唯一一条失效 → 100% ≥ 30%
        self.assertIn('失效', ' '.join(result['notes']))

    async def test_scheduler_runs_due_jobs_from_the_loop(self):
        self.r.create_run('loop', str(self.project), 'test')
        await self.r.start()
        for _ in range(200):
            await asyncio.sleep(.05)
            state = self.r.maintenance.states.get('workspace-scan') or {}
            if state.get('runs'): break
        state = self.r.maintenance.states.get('workspace-scan') or {}
        self.assertTrue(state.get('runs'), '运行时循环应驱动到期作业')
        # 高频作业保持静默：不写 MaintenanceFinished 事件。
        self.assertFalse([e for e in self.r.store.events() if e['type'] == 'MaintenanceFinished'
                          and e['payload'].get('job') == 'workspace-scan'])


class KnowledgeLayerTests(unittest.IsolatedAsyncioTestCase):
    """知识层的三个修复：来源基准目录、依赖反向传播、报告不再自动堆积。"""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / 'project'; self.project.mkdir()
        (self.project / 'README.md').write_text('original\n')
        self.r = Runtime(Path(self.temp.name) / 'data', concurrency=1, batch_seconds=.01)
        from unison.demo import demo_call
        self.r.models.adapters['fake'] = demo_call
        self.r.store.put('model', {'id': 'test', 'model': 'test', 'adapter': 'fake', 'base_url': 'test://local',
                                   'no_key': True, 'context_window': 64000, 'max_output': 4096})

    async def asyncTearDown(self):
        await self.r.stop(); self.temp.cleanup()

    def make(self, goal='knowledge'):
        run = self.r.create_run(goal, str(self.project), 'test')
        root = self.r.task(run['root_task'])
        child = self.r.create_task(run, goal + ' child', 'test', root)
        return root, self.r.task(child['id'])

    async def test_child_knowledge_source_is_not_falsely_stale_in_parent(self):
        root, child = self.make()
        (Path(child['workspace']) / 'note.md').write_text('child fact\n')
        item = self.r.publish_knowledge(child, {'title': 'child fact', 'content': 'body', 'sources': ['note.md']})
        self.assertEqual(item['sources'][0]['kind'], 'workspace')
        # 父任务的工作区里没有 note.md，但按发布时记录的类型仍应判定有效。
        result = self.r.knowledge_search('child', root)
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]['stale'], result[0].get('stale_reason'))
        # 子工作区里的文件真的变了，才判失效。
        (Path(child['workspace']) / 'note.md').write_text('edited\n')
        self.assertTrue(self.r.knowledge_search('child', root)[0]['stale'])

    async def test_project_source_is_checked_against_project_root(self):
        root, child = self.make()
        item = self.r.publish_knowledge(child, {'title': 'project fact', 'content': 'body', 'sources': ['README.md']})
        self.assertEqual(item['sources'][0]['kind'], 'project')
        self.assertEqual(item['sources'][0]['path'], 'README.md')
        self.assertFalse(self.r.knowledge_search('project', root)[0]['stale'])
        (self.project / 'README.md').write_text('changed\n')
        self.assertTrue(self.r.knowledge_search('project', root)[0]['stale'])

    async def test_dependency_edges_propagate_both_ways(self):
        root, child = self.make()
        base = self.r.publish_knowledge(child, {'title': 'base', 'content': 'body', 'sources': ['README.md']})
        derived = self.r.publish_knowledge(child, {'title': 'derived', 'content': 'body', 'dependencies': [base['id']]})
        self.assertEqual(self.r.store.get('knowledge', base['id'])['dependents'][0]['id'], derived['id'])
        (self.project / 'README.md').write_text('changed\n')
        # 直接来源变化 → 沿反向边把依赖它的知识一起标失效，并写事件。
        self.r.workspace.invalidate(root, {'README.md'})
        self.assertTrue(self.r.store.get('knowledge', base['id'])['stale'])
        self.assertTrue(self.r.store.get('knowledge', derived['id'])['stale'])
        self.assertEqual(len([e for e in self.r.store.events() if e['type'] == 'KnowledgeInvalidated']), 2)

    async def test_search_requires_all_terms_and_ranks_title_hits_first(self):
        root, child = self.make()
        self.r.publish_knowledge(child, {'title': 'alpha beta', 'content': 'gamma'})
        self.r.publish_knowledge(child, {'title': 'alpha only', 'content': 'delta'})
        self.r.publish_knowledge(child, {'title': 'unrelated', 'content': 'mentions alpha in body'})
        both = self.r.knowledge_search('alpha beta', root)
        self.assertEqual([x['title'] for x in both], ['alpha beta'], '必须全部词命中，而不是命中任一词')
        ranked = self.r.knowledge_search('alpha', root)
        self.assertEqual(ranked[-1]['title'], 'unrelated', '标题命中要排在只有正文命中之前')
        self.assertEqual(len(self.r.knowledge_search('nomatch', root)), 0)
        self.assertEqual(len(self.r.knowledge_search('alpha', root, limit=1)), 1, 'limit 生效')

    async def test_completion_does_not_publish_report_as_knowledge(self):
        run = self.r.create_run('report', str(self.project), 'test')
        task = self.r.task(run['root_task'])
        (self.project / 'new.txt').write_text('x\n')
        self.r.complete(task, {'summary': 'done'})
        reports = self.r.store.all('report')
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['files'][0]['path'], 'new.txt')
        self.assertEqual(self.r.store.all('knowledge'), [], '报告不再自动堆积成知识')

    async def test_evidence_records_facts_without_grading(self):
        """报告如实记录"有没有命令证据"，但不给证据评等级、不发未确认事件。

        原先运行时会把证据分成命令/目视/无证据三档，并在"声明 verified 却没有可用命令证据"时
        写一条 `VerificationUnconfirmed`。那是运行时替人下判断（而且判据里还包含"退出码是否可能
        被 shell 吞掉"这类正则推断）。现在只记录：绑定了哪条执行记录、退出码多少。
        """
        run = self.r.create_run('verify', str(self.project), 'test')
        task = self.r.task(run['root_task'])
        # 没有任何证据：模型自己的声明照常保留，运行时不再补判词。
        self.r.complete(task, {'summary': 'looks fine', 'verified': True})
        report = self.r.store.all('report')[0]
        self.assertEqual(report['verification_status'], 'verified')
        self.assertEqual(report['verification_evidence']['command_executions'], 0)
        self.assertNotIn('verified', report['verification_evidence'])
        self.assertFalse([e for e in self.r.store.events() if e['type'] == 'VerificationUnconfirmed'])
        # 有执行记录时，证据与它绑定，退出码原样保留。
        run2 = self.r.create_run('verify2', str(self.project), 'test')
        task2 = self.r.task(run2['root_task'])
        shell = await self.r.tool_workspace_shell(task2, {'command': 'true'})
        self.r.complete(self.r.task(task2['id']), {'summary': 'tests pass', 'verified': True,
                                                   'evidence': [{'kind': 'command', 'command': 'true'}]})
        latest = [x for x in self.r.store.all('report') if x['task_id'] == task2['id']][0]
        self.assertEqual(latest['verification_evidence']['command_executions'], 1)
        self.assertEqual(latest['verification_evidence']['evidence'][0]['execution_ref'], shell['execution_ref'])
        self.assertEqual(latest['verification_evidence']['evidence'][0]['exit_code'], 0)


if __name__ == '__main__':
    unittest.main()

"""技能端口调用：bridge 契约、单次/批量、作业轮询、中断恢复与拒绝路径。

用临时目录里的假技能与假桥接脚本，因此不依赖内置技能，也不发真实网络请求。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from unison.runtime import Runtime
from unison.skill_runtime import SkillInvocationError, SkillInvoker
from unison.skills import MAX_BATCH, SkillError, parse_invocation

ECHO_BRIDGE = '''#!/usr/bin/env python3
"""测试用桥接脚本：回显输入，并支持用 args.delay 模拟慢调用。"""
import json, sys, time

payload = json.load(sys.stdin)
args = payload.get('args')
delay = (args or {}).get('delay') if isinstance(args, dict) else 0
time.sleep(float(delay or 0))
if isinstance(args, list):
    results = [{'input': item, 'workspace': payload.get('workspace'), 'base': payload.get('base')} for item in args]
    print(json.dumps({'ok': True, 'results': results, 'count': len(results)}))
else:
    print(json.dumps({'ok': True, 'result': {'input': args, 'workspace': payload.get('workspace'),
                                             'base': payload.get('base')}}))
'''

FAIL_BRIDGE = '''#!/usr/bin/env python3
import json, sys
print(json.dumps({'ok': False, 'error': '上游拒绝：模型不可用'}))
'''

CRASH_BRIDGE = '''#!/usr/bin/env python3
import sys
sys.stderr.write('boom: 缺少凭据\\n')
sys.exit(3)
'''

NOT_JSON_BRIDGE = '''#!/usr/bin/env python3
print('这不是 JSON')
'''


def write_skill(root, name, bridge='scripts/run.py', body='# 指令\n', batch=True, source=ECHO_BRIDGE, timeout=30):
    directory = Path(root)/name
    (directory/'scripts').mkdir(parents=True, exist_ok=True)
    (directory/'scripts'/'run.py').write_text(source, encoding='utf-8')
    lines = ['---', f'name: {name}', f'description: {name} 的测试技能']
    if bridge:
        lines += ['invocation:', '  kind: bridge', f'  bridge: {bridge}',
                  f'  batch: {"true" if batch else "false"}', f'  timeout: {timeout}']
    lines.append('---')
    (directory/'SKILL.md').write_text('\n'.join(lines)+'\n'+body, encoding='utf-8')
    return directory


class InvocationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root/'demo'/'SKILL.md'
        self.file.parent.mkdir()
        (self.file.parent/'scripts').mkdir()
        (self.file.parent/'scripts'/'run.py').write_text('pass', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def parse(self, extra):
        return parse_invocation(extra, self.file)

    def test_absent_contract_means_instruction_only(self):
        self.assertIsNone(self.parse({}))
        self.assertIsNone(self.parse({'invocation': {}}))

    def test_numeric_timeout_from_frontmatter_is_a_number(self):
        contract = self.parse({'invocation': {'kind': 'bridge', 'bridge': 'scripts/run.py', 'timeout': 300}})
        self.assertEqual(contract['timeout'], 300.0)
        self.assertFalse(contract['batch'])

    def test_rejects_bad_kind_missing_bridge_and_escape(self):
        with self.assertRaisesRegex(SkillError, 'kind'):
            self.parse({'invocation': {'kind': 'shell', 'bridge': 'scripts/run.py'}})
        with self.assertRaisesRegex(SkillError, '必须提供'):
            self.parse({'invocation': {'kind': 'bridge'}})
        with self.assertRaisesRegex(SkillError, '位于技能目录内'):
            self.parse({'invocation': {'kind': 'bridge', 'bridge': '../outside.py'}})
        with self.assertRaisesRegex(SkillError, '不存在'):
            self.parse({'invocation': {'kind': 'bridge', 'bridge': 'scripts/missing.py'}})
        with self.assertRaisesRegex(SkillError, '必须是正数'):
            self.parse({'invocation': {'kind': 'bridge', 'bridge': 'scripts/run.py', 'timeout': 0}})


class SkillInvokerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.bundled = self.base/'bundled'
        self.bundled.mkdir()
        self.workspace = self.base/'project'
        self.workspace.mkdir()
        self.r = Runtime(self.base/'data', concurrency=1, batch_seconds=.01)
        self.r.skills = __import__('unison.skills', fromlist=['Skills']).Skills(
            home=self.base/'home', bundled=self.bundled)
        self.r.skill_invoker = SkillInvoker(self.r)
        self.invoker = self.r.skill_invoker

    def tearDown(self):
        self.r.store.close()
        self.temp.cleanup()

    async def test_single_call_returns_result_in_window(self):
        write_skill(self.bundled, 'echo-one')
        job = await self.invoker.call('echo-one', args={'prompt': 'hi'}, workspace=str(self.workspace), wait=30)
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(job['result']['input'], {'prompt': 'hi'})
        self.assertEqual(job['result']['workspace'], str(self.workspace))
        self.assertTrue(job['result']['base'].endswith('echo-one'))
        self.assertEqual([e['type'] for e in self.r.store.events() if e['type'].startswith('SkillInvocation')],
                         ['SkillInvocationSubmitted', 'SkillInvocationCompleted'])

    async def test_batch_submits_all_items_and_keeps_order(self):
        write_skill(self.bundled, 'echo-batch')
        job = await self.invoker.call('echo-batch', batch=[{'i': 1}, {'i': 2}, {'i': 3}],
                                      workspace=str(self.workspace), wait=30)
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(job['count'], 3)
        self.assertTrue(job['batch'])
        self.assertEqual([item['input']['i'] for item in job['results']], [1, 2, 3])

    async def test_declared_single_skill_rejects_batch(self):
        write_skill(self.bundled, 'echo-single', batch=False)
        with self.assertRaisesRegex(SkillInvocationError, '不接受批量'):
            self.invoker.submit('echo-single', batch=[{'i': 1}], workspace=str(self.workspace))
        # 单次仍然可用。
        job = await self.invoker.call('echo-single', args={'i': 1}, workspace=str(self.workspace), wait=30)
        self.assertEqual(job['status'], 'succeeded')

    async def test_batch_over_limit_is_rejected_not_truncated(self):
        write_skill(self.bundled, 'echo-limit')
        with self.assertRaisesRegex(SkillInvocationError, f'上限为 {MAX_BATCH}'):
            self.invoker.submit('echo-limit', batch=[{}]*(MAX_BATCH+1), workspace=str(self.workspace))

    async def test_instruction_only_skill_cannot_be_invoked(self):
        write_skill(self.bundled, 'plain-skill', bridge=None)
        with self.assertRaisesRegex(SkillInvocationError, '没有声明 invocation'):
            self.invoker.submit('plain-skill', args={}, workspace=str(self.workspace))

    async def test_unknown_skill_lists_available_names(self):
        write_skill(self.bundled, 'echo-known')
        with self.assertRaisesRegex(SkillInvocationError, 'echo-known'):
            self.invoker.submit('echo-unknown', args={}, workspace=str(self.workspace))

    async def test_bridge_failure_and_crash_are_recorded_as_failed_job(self):
        write_skill(self.bundled, 'failing', source=FAIL_BRIDGE)
        job = await self.invoker.call('failing', args={}, workspace=str(self.workspace), wait=30)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('上游拒绝', job['error'])
        write_skill(self.bundled, 'crashing', source=CRASH_BRIDGE)
        job = await self.invoker.call('crashing', args={}, workspace=str(self.workspace), wait=30)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('退出码 3', job['error'])
        self.assertIn('boom', job['error'])
        failed = [e for e in self.r.store.events() if e['type'] == 'SkillInvocationFailed']
        self.assertEqual(len(failed), 2)

    async def test_non_json_output_is_rejected(self):
        write_skill(self.bundled, 'garbage', source=NOT_JSON_BRIDGE)
        job = await self.invoker.call('garbage', args={}, workspace=str(self.workspace), wait=30)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('不是 JSON', job['error'])

    async def test_slow_job_returns_pending_handle_then_poll_succeeds(self):
        """慢调用不阻塞提交方：窗口内没完成就先拿 job_id，轮询到终态。"""
        write_skill(self.bundled, 'slow-one')
        job = await self.invoker.call('slow-one', args={'delay': 1.2}, workspace=str(self.workspace), wait=0.05)
        self.assertIn(job['status'], {'queued', 'running'})
        for _ in range(100):
            await asyncio.sleep(.05)
            job = self.invoker.job(job['id'])
            if job['status'] in {'succeeded', 'failed', 'interrupted'}:
                break
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(job['result']['input'], {'delay': 1.2})

    async def test_job_timeout_is_enforced(self):
        # 技能声明的 timeout 是硬上限：桥接脚本超时被杀，作业记为失败而不是挂住。
        write_skill(self.bundled, 'hanging', timeout=1, source=ECHO_BRIDGE)
        job = await self.invoker.call('hanging', args={'delay': 30}, workspace=str(self.workspace), wait=30)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('TimeoutExpired', job['error'])

    async def test_restart_marks_inflight_jobs_interrupted_without_replaying(self):
        write_skill(self.bundled, 'echo-one')
        self.r.store.put('skill_job', {'id': 'skilljob_left', 'name': 'echo-one', 'status': 'running',
                                       'created': 1, 'count': 1, 'batch': False})
        self.invoker.recover()
        job = self.invoker.job('skilljob_left')
        self.assertEqual(job['status'], 'interrupted')
        self.assertIn('不会自动重放', job['error'])
        self.assertTrue([e for e in self.r.store.events() if e['type'] == 'SkillJobInterrupted'])

    async def test_jobs_are_listed_newest_first(self):
        write_skill(self.bundled, 'echo-one')
        for index in range(3):
            await self.invoker.call('echo-one', args={'i': index}, workspace=str(self.workspace), wait=30)
        listed = self.invoker.jobs()
        self.assertEqual(len(listed), 3)
        self.assertEqual([item['result']['input']['i'] for item in listed], [2, 1, 0])


class SkillToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """模型侧入口：skill 工具能带批量提交，skills_list 给出调用契约。"""

    async def asyncSetUp(self):
        from unison.demo import demo_call
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = self.base/'project'; self.project.mkdir()
        (self.project/'README.md').write_text('original\n')
        self.bundled = self.base/'bundled'; self.bundled.mkdir()
        self.r = Runtime(self.base/'data', concurrency=1, batch_seconds=.01)
        self.r.models.adapters['fake'] = demo_call
        self.r.store.put('model', {'id': 'test', 'model': 'test', 'adapter': 'fake', 'base_url': 'test://local',
                                   'no_key': True, 'context_window': 64000, 'max_output': 4096})
        self.r.skills = __import__('unison.skills', fromlist=['Skills']).Skills(
            home=self.base/'home', bundled=self.bundled)
        self.r.skill_invoker = SkillInvoker(self.r)
        write_skill(self.bundled, 'echo-one')
        write_skill(self.bundled, 'plain-skill', bridge=None)
        run = self.r.create_run('技能端口测试', str(self.project), 'test')
        self.task = self.r.task(run['root_task'])

    async def asyncTearDown(self):
        await self.r.stop(); self.temp.cleanup()

    async def test_skills_list_exposes_invocation_contract(self):
        result = await self.r.tool_skills_list(self.task, {})
        by_name = {item['name']: item for item in result['skills']}
        self.assertTrue(by_name['echo-one']['invocable'])
        self.assertTrue(by_name['echo-one']['batch'])
        self.assertEqual(by_name['echo-one']['max_batch'], MAX_BATCH)
        self.assertEqual(by_name['echo-one']['invoke_endpoint'], 'POST /api/skills/call')
        self.assertFalse(by_name['plain-skill']['invocable'])
        self.assertIsNone(by_name['plain-skill']['invoke_endpoint'])
        self.assertIn('UNISON_API_TOKEN', result['note'])

    async def test_skill_tool_declares_contract_and_accepts_batch(self):
        loaded = await self.r.tool_skill(self.r.task(self.task['id']), {'name': 'echo-one'})
        self.assertTrue(loaded['invocation']['invocable'])
        self.assertIn('<skill_instructions>', loaded['content'])
        self.assertNotIn('job_id', loaded)          # 不带 batch 时不提交任何东西

        submitted = await self.r.tool_skill(self.r.task(self.task['id']),
                                           {'name': 'echo-one', 'batch': [{'i': 1}, {'i': 2}]})
        self.assertIn('job_id', submitted)
        for _ in range(100):
            await asyncio.sleep(.03)
            job = self.r.skill_invoker.job(submitted['job_id'])
            if job['status'] in {'succeeded', 'failed'}:
                break
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(job['count'], 2)

    async def test_skill_tool_reports_contract_error_for_instruction_only_skill(self):
        result = await self.r.tool_skill(self.r.task(self.task['id']),
                                        {'name': 'plain-skill', 'batch': [{'i': 1}]})
        self.assertIn('batch_error', result)
        self.assertIn('没有声明 invocation', result['batch_error'])

    async def test_shell_environment_exposes_token_only_when_service_injected(self):
        env = self.r.shell_environment(self.task)
        self.assertNotIn('UNISON_API_TOKEN', env)     # 直接用 Runtime 时没有端口可调
        self.r.api_token = 'secret-token'
        self.r.base_url = 'http://127.0.0.1:8740'
        env = self.r.shell_environment(self.task)
        self.assertEqual(env['UNISON_API_TOKEN'], 'secret-token')
        self.assertEqual(env['UNISON_BASE_URL'], 'http://127.0.0.1:8740')


if __name__ == '__main__':
    unittest.main()

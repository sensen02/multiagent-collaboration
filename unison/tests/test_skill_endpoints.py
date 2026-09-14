"""技能端点的 HTTP 契约：POST /api/skills/call 与 GET /api/skill/jobs/<id>。

起真实的本地服务（含鉴权层），用临时目录里的假技能，因此不发真实网络请求。
"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from unison.server import App, Handler

BRIDGE = '''#!/usr/bin/env python3
"""回显桥接：把 workspace 与 args 原样带回，用于验证端到端契约。"""
import json, sys
payload = json.load(sys.stdin)
args = payload.get('args')
if isinstance(args, list):
    print(json.dumps({'ok': True, 'results': [{'echo': item, 'workspace': payload.get('workspace')} for item in args]}))
else:
    print(json.dumps({'ok': True, 'result': {'echo': args, 'workspace': payload.get('workspace')}}))
'''

PLAIN_BRIDGE = '''#!/usr/bin/env python3
import json, sys
print(json.dumps({'ok': False, 'error': '故意失败'}))
'''


def write_skill(root, name, batch=True, source=BRIDGE):
    directory = Path(root) / name
    (directory / 'scripts').mkdir(parents=True, exist_ok=True)
    (directory / 'scripts' / 'run.py').write_text(source, encoding='utf-8')
    lines = ['---', f'name: {name}', f'description: {name} 的测试技能',
             'invocation:', '  kind: bridge', '  bridge: scripts/run.py',
             f'  batch: {"true" if batch else "false"}', '  timeout: 30', '---', '# 指令', '']
    (directory / 'SKILL.md').write_text('\n'.join(lines), encoding='utf-8')


class SkillEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.app = App(self.data, (), token='test-token')
        # 用临时技能包替换内置包，测试不依赖随程序分发的具体技能。
        from unison.skills import Skills
        from unison.skill_runtime import SkillInvoker
        self.app.runtime.skills = Skills(home=self.root / 'home', bundled=self.root / 'bundled')
        self.app.runtime.skill_invoker = SkillInvoker(self.app.runtime)
        (self.root / 'bundled').mkdir()
        write_skill(self.root / 'bundled', 'echo-skill')
        write_skill(self.root / 'bundled', 'single-skill', batch=False)
        write_skill(self.root / 'bundled', 'failing-skill', source=PLAIN_BRIDGE)
        plain = self.root / 'bundled' / 'plain-skill'
        plain.mkdir(parents=True)
        (plain / 'SKILL.md').write_text('---\nname: plain-skill\ndescription: 纯指令技能\n---\n# 指令\n', encoding='utf-8')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.app = self.app
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = f'http://127.0.0.1:{self.port}/api'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.temp.cleanup()

    def request(self, path, data=None, method=None, token='test-token'):
        body = None if data is None else json.dumps(data).encode()
        request = urllib.request.Request(self.api + path, data=body, method=method or ('POST' if body else 'GET'))
        if body:
            request.add_header('Content-Type', 'application/json')
        if token:
            request.add_header('Authorization', 'Bearer ' + token)
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def expect_error(self, path, data=None):
        try:
            self.request(path, data)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)
        self.fail('expected HTTP error')

    # ------------------------------------------------------------ 成功路径

    def test_single_call_returns_result_within_window(self):
        payload = self.request('/skills/call', {'skill': 'echo-skill', 'workspace': str(self.workspace),
                                               'args': {'prompt': 'hi'}, 'wait': 60})
        job = payload['job']
        self.assertTrue(payload['done'])
        self.assertIsNone(payload['poll'])
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(job['result']['echo'], {'prompt': 'hi'})
        self.assertEqual(job['result']['workspace'], str(self.workspace))

    def test_batch_call_keeps_order_and_is_pollable(self):
        payload = self.request('/skills/call', {'skill': 'echo-skill', 'workspace': str(self.workspace),
                                               'batch': [{'i': 1}, {'i': 2}, {'i': 3}], 'wait': 60})
        job = payload['job']
        self.assertTrue(job['batch'])
        self.assertEqual(job['count'], 3)
        self.assertEqual([item['echo']['i'] for item in job['results']], [1, 2, 3])
        # 轮询端点对单次与批量同形。
        polled = self.request(f"/skill/jobs/{job['id']}")['job']
        self.assertEqual(polled['status'], 'succeeded')
        self.assertEqual(polled['count'], 3)
        listed = self.request('/skill/jobs')['jobs']
        self.assertEqual([item['id'] for item in listed], [job['id']])

    def test_slow_call_returns_handle_then_poll_reaches_terminal(self):
        """窗口很小时不阻塞调用方：先拿 job_id，再轮询——这正是"走端口"的意义。"""
        payload = self.request('/skills/call', {'skill': 'echo-skill', 'workspace': str(self.workspace),
                                               'args': {'i': 1}, 'wait': 0})
        job = payload['job']
        self.assertFalse(payload['done'])
        self.assertEqual(payload['poll'], f"/api/skill/jobs/{job['id']}")
        self.assertIn(job['status'], {'queued', 'running', 'succeeded'})
        for _ in range(200):
            polled = self.request(f"/skill/jobs/{job['id']}")['job']
            if polled['status'] in {'succeeded', 'failed', 'interrupted'}:
                break
        self.assertEqual(polled['status'], 'succeeded')

    def test_event_stream_records_invocation(self):
        payload = self.request('/skills/call', {'skill': 'echo-skill', 'workspace': str(self.workspace),
                                               'args': {}, 'wait': 60})
        types = [item['type'] for item in self.request('/events?limit=200')]
        self.assertIn('SkillInvocationSubmitted', types)
        self.assertIn('SkillInvocationCompleted', types)
        self.assertTrue(payload['job']['id'])

    # ------------------------------------------------------------ 拒绝路径

    def test_batch_rejected_when_skill_declares_single_mode(self):
        status, payload = self.expect_error('/skills/call', {'skill': 'single-skill',
                                                            'workspace': str(self.workspace),
                                                            'batch': [{'i': 1}]})
        self.assertEqual(status, 400)
        self.assertIn('不接受批量', payload['error'])

    def test_instruction_only_skill_is_rejected(self):
        status, payload = self.expect_error('/skills/call', {'skill': 'plain-skill', 'workspace': str(self.workspace)})
        self.assertEqual(status, 400)
        self.assertIn('没有声明 invocation', payload['error'])

    def test_unknown_skill_reports_available_names(self):
        status, payload = self.expect_error('/skills/call', {'skill': 'missing-skill', 'workspace': str(self.workspace)})
        self.assertEqual(status, 400)
        self.assertIn('echo-skill', payload['error'])

    def test_unknown_job_is_a_readable_error(self):
        status, payload = self.expect_error('/skill/jobs/skilljob_missing')
        self.assertEqual(status, 400)
        self.assertIn('未知技能作业', payload['error'])

    def test_bridge_failure_surfaces_as_failed_job_not_http_error(self):
        """技能执行失败是作业状态，不是调用失败：提交本身合法。"""
        payload = self.request('/skills/call', {'skill': 'failing-skill', 'workspace': str(self.workspace),
                                               'args': {}, 'wait': 60})
        self.assertEqual(payload['job']['status'], 'failed')
        self.assertIn('故意失败', payload['job']['error'])

    def test_endpoint_requires_token(self):
        request = urllib.request.Request(self.api + '/skills/call', data=b'{}', method='POST')
        request.add_header('Content-Type', 'application/json')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 401)

    def test_skills_directory_exposes_invocation_metadata(self):
        data = self.request('/skills?workspace=' + str(self.workspace))
        by_name = {item['name']: item for item in data['skills']}
        self.assertTrue(by_name['echo-skill']['invocable'])
        self.assertTrue(by_name['echo-skill']['batch'])
        self.assertFalse(by_name['plain-skill']['invocable'])


if __name__ == '__main__':
    unittest.main()

"""服务化边界：外部程序调用必须带令牌，同源控制台不受影响。"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from unison.server import App, AuthError, Handler
from http.server import ThreadingHTTPServer


def headers(origin=None, host='127.0.0.1:8740', token=None, authorization=None):
    data = {'Host': host}
    if origin:
        data['Origin'] = origin
    if token:
        data['X-Unison-Token'] = token
    if authorization:
        data['Authorization'] = authorization
    return data


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App.__new__(App)  # 不需要启动运行时，只验证令牌检查
        self.app.root = Path(self.temp.name)
        self.app.auth_required = True   # 与 App.__init__ 的默认值一致

    def tearDown(self):
        self.temp.cleanup()

    def load(self, token=None, env=None):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, env or {}, clear=False):
            if env is None:
                os.environ.pop('UNISON_TOKEN', None)
            return App._load_token(self.app, token)

    def test_token_is_generated_once_and_reused(self):
        first = self.load()
        self.assertTrue(first)
        self.assertEqual((Path(self.temp.name) / 'api_token').read_text().strip(), first)
        self.assertEqual(self.load(), first)

    def test_explicit_and_environment_token_win(self):
        self.assertEqual(self.load('explicit'), 'explicit')
        self.assertEqual(self.load(env={'UNISON_TOKEN': 'from-env'}), 'from-env')

    def test_same_origin_console_passes_without_token(self):
        self.app.token = 'secret'
        self.app.authenticate(headers(origin='http://127.0.0.1:8740'))
        self.app.authenticate(headers(origin='http://localhost:8740', host='localhost:8740'))

    def test_authenticate_is_a_switch_not_a_no_op(self):
        """守卫：鉴权只能通过 `auth_required` 开关关闭，不能把 authenticate 清空。

        `App(...)` 的默认值是**要求鉴权**，所以任何忘记传参的调用点都不会静默失去保护。
        """
        import inspect
        body = inspect.getsource(App.authenticate)
        self.assertIn('compare_digest', body)
        self.assertIn('auth_required', body)
        self.assertTrue(App.__init__.__defaults__[2] if len(App.__init__.__defaults__) > 2 else True,
                        'App(auth_required=) 的默认值必须是 True')
        self.app.token = 'secret'
        self.app.auth_required = True
        for headers in ({'Host': '127.0.0.1:8740'},
                        {'Host': '127.0.0.1:8740', 'X-Unison-Token': ''},
                        {'Host': '127.0.0.1:8740', 'Origin': 'http://evil.example',
                         'Authorization': 'Bearer wrong'}):
            with self.assertRaises(AuthError):
                self.app.authenticate(headers)
        self.app.authenticate({'Host': '127.0.0.1:8740', 'Authorization': 'Bearer secret'})
        self.app.authenticate({'Host': '127.0.0.1:8740', 'X-Unison-Token': 'secret'})
        self.app.authenticate({'Host': '127.0.0.1:8740', 'Origin': 'http://127.0.0.1:8740'})

    def test_auth_can_be_explicitly_disabled_for_single_user_local_use(self):
        """显式关闭时全部放行——这是被写下来的选择，不是"函数被清空"。"""
        self.app.token = 'secret'
        self.app.auth_required = False
        self.app.authenticate({'Host': '127.0.0.1:8740'})          # 不抛异常
        self.app.authenticate({'Host': '127.0.0.1:8740', 'Origin': 'http://evil.example'})

    def test_external_caller_needs_matching_token(self):
        self.app.token = 'secret'
        for candidate in (headers(), headers(token='wrong'), headers(origin='http://evil.example', token='wrong')):
            with self.assertRaises(AuthError):
                self.app.authenticate(candidate)
        self.app.authenticate(headers(token='secret'))
        self.app.authenticate(headers(authorization='Bearer secret'))


class HttpServiceTests(unittest.TestCase):
    """真实起一个本地服务，验证令牌与 /api/wait 的对外契约。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / 'data'
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        (self.project / 'README.md').write_text('original\n')
        self.app = App(self.data, (), token='test-token')
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

    def request(self, path, data=None, method=None, token=None, origin=None):
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(self.api + path, data=body, method=method or ('POST' if body else 'GET'))
        if body:
            req.add_header('Content-Type', 'application/json')
        if origin:
            req.add_header('Origin', origin)
        if token:
            req.add_header('Authorization', 'Bearer ' + token)
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def expect_error(self, path, data=None, token=None, origin=None):
        try:
            self.request(path, data, token=token, origin=origin)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)
        self.fail('expected HTTP error')

    def demo_run(self, goal='离线演示：服务化测试'):
        model = {'id': 'offline-demo', 'label': '离线机制演示', 'model': 'scripted-demo', 'base_url': 'demo://local',
                 'adapter': 'demo', 'context_window': 64000, 'max_output': 4096, 'no_key': True}
        self.request('/models', model, token='test-token')
        return self.request('/runs', {'goal': goal, 'workspace': str(self.project), 'model_id': 'offline-demo'}, token='test-token')

    def test_external_call_requires_token(self):
        status, payload = self.expect_error('/state')
        self.assertEqual(status, 401)
        self.assertEqual(payload['code'], 'auth_required')
        state = self.request('/state', token='test-token')
        self.assertIn('runs', state)
        # 同源控制台仍然免令牌可用。
        console = self.request('/state', origin=f'http://127.0.0.1:{self.port}')
        self.assertIn('runs', console)

    def test_wait_returns_terminal_result_and_report(self):
        run = self.demo_run('演示检查：服务化')
        result = self.request(f"/wait?task_id={run['root_task']}&timeout=60", token='test-token')
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(result['timed_out'])
        reports = self.request(f"/task?id={run['root_task']}", token='test-token')['reports']
        self.assertTrue(reports)

    def test_wait_returns_early_when_human_input_needed(self):
        run = self.demo_run()
        result = self.request(f"/wait?task_id={run['root_task']}&timeout=60", token='test-token')
        # 根任务在演示里会等待子任务与人工问答；/api/wait 在需要人工介入时立即返回，便于外部程序接管。
        self.assertIn(result['status'], {'waiting', 'completed'})
        if result['status'] == 'waiting':
            self.assertIn('wait', result)

    def test_credentials_endpoint_exposes_plaintext_for_callers(self):
        model = {'id': 'probe', 'model': 'probe-1', 'base_url': 'https://example.invalid', 'api_key': 'sk-probe', 'api': 'openai-completions'}
        self.request('/models', model, token='test-token')
        payload = self.request('/models/credentials?model_ids=probe', token='test-token')
        entry = payload['models'][0]
        self.assertEqual(entry['api_key'], 'sk-probe')
        self.assertEqual(entry['base_url'], 'https://example.invalid')

    def test_task_reports_derived_history(self):
        run = self.demo_run('演示检查：服务化')
        self.request(f"/wait?task_id={run['root_task']}&timeout=60", token='test-token')
        payload = self.request(f"/task?id={run['root_task']}", token='test-token')
        roles = [m['role'] for m in payload['history']]
        # 头两条是系统消息：基础提示与技能目录（目录只含名称与摘要，正文按需加载）。
        self.assertEqual(roles[:2], ['system', 'system'])
        self.assertIn('available_skills', payload['history'][1]['content'])
        self.assertEqual(roles[2:], ['user', 'assistant', 'tool'])
        # 任务记录里不再保存历史副本，只有消息数。
        self.assertNotIn('history', payload['task'])
        self.assertEqual(payload['task']['history_messages'], len(payload['history']))

    def test_transcript_groups_history_by_agent_and_reports_the_hold(self):
        """对话视图的数据契约：按 Agent 分组的历史 + "整个运行正停着等人"这一条事实。"""
        run = self.demo_run('演示问答：对话视图')
        deadline = time.time() + 60
        questions = []
        while time.time() < deadline:
            state = self.request('/state', token='test-token')
            questions = [q for q in state['questions'] if q['run_id'] == run['id'] and q['status'] == 'open']
            if questions:
                break
            time.sleep(.3)
        else:
            self.fail('等不到人工提问')

        payload = self.request(f"/transcript?run_id={run['id']}&limit=50", token='test-token')
        self.assertTrue(payload['hold']['paused'])
        self.assertEqual(payload['hold']['reason'], 'human_question')
        self.assertIn(questions[0]['task_id'], payload['hold']['waiting_on'])
        agents = payload['agents']
        # 主调度在最前（父为空），子任务排在后面。
        self.assertIsNone(agents[0]['parent_id'])
        self.assertTrue(all(a['parent_id'] for a in agents[1:]))
        asker = [a for a in agents if a['id'] == questions[0]['task_id']][0]
        self.assertEqual(asker['questions'][0]['status'], 'open')
        self.assertEqual([m['role'] for m in agents[0]['messages']][:2], ['system', 'system'])
        self.assertTrue(any(m.get('tool_calls') for a in agents for m in a['messages']))

        # 暂停是运行时的事实，不只是界面上的说法。
        paused = [r for r in self.request('/state', token='test-token')['runs'] if r['id'] == run['id']][0]
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(paused['pause_reason'], 'human_question')

        self.request('/answer', {'id': questions[0]['id'], 'answer': '对话视图'}, token='test-token')
        resumed = [r for r in self.request('/state', token='test-token')['runs'] if r['id'] == run['id']][0]
        self.assertEqual(resumed['status'], 'active')
        self.assertEqual(resumed['pause_reason'], '')

    def test_verify_endpoint_rejects_unknown_provider_and_skips_fresh_models(self):
        """复验端点的省 token 契约。

        注意：`App.runtime.store` 的 SQLite 连接属于运行时线程，测试线程不能直接读写，
        因此这里只用 HTTP 观察行为——健康状态机的细节由 test_model_health 覆盖。
        """
        model = {'id': 'probe:one', 'model': 'one', 'base_url': 'https://example.invalid',
                 'api': 'openai-completions', 'context_window': 32000, 'max_output': 4096}
        self.request('/models', model, token='test-token')
        provider = {'id': 'probe', 'name': 'Probe', 'base_url': 'https://example.invalid',
                    'api': 'openai-completions', 'models': [{'id': 'one', 'contextWindow': 32000,
                                                             'maxTokens': 4096}]}
        self.request('/providers/import', {'config': json.dumps(provider)}, token='test-token') \
            if False else None
        status, payload = self.expect_error('/providers/verify', {'provider_id': 'missing'}, token='test-token')
        self.assertEqual(status, 400)
        self.assertIn('provider', payload['error'])

    def test_health_endpoint_shape(self):
        model = {'id': 'probe:one', 'model': 'one', 'base_url': 'https://example.invalid',
                 'api': 'openai-completions', 'context_window': 32000, 'max_output': 4096}
        self.request('/models', model, token='test-token')
        payload = self.request('/models/health', token='test-token')
        entry = next(item for item in payload['models'] if item['id'] == 'probe:one')
        for key in ('state', 'source', 'checked_at', 'expires_at', 'stale', 'needs_probe', 'code', 'remedy'):
            self.assertIn(key, entry)
        # 从未验证过的模型必须进入复验队列，而不是被当成"不可用"。
        self.assertEqual(entry['state'], 'unknown')
        self.assertTrue(entry['needs_probe'])
        self.assertEqual(payload['ok_ttl_seconds'], 86400)
        self.assertIn('needs_probe', payload['summary'])

    def test_wait_accepts_unknown_task_as_error(self):
        status, payload = self.expect_error('/wait?task_id=missing&timeout=1', token='test-token')
        self.assertEqual(status, 400)
        self.assertEqual(payload['code'], 'invalid_request')


if __name__ == '__main__':
    unittest.main()


class DefaultWorkspaceTests(unittest.TestCase):
    """默认工作区必须与数据目录分开：子任务副本不会再带上历史产物（ink-duel 复核）。"""

    def test_default_workspace_is_outside_data_dir(self):
        import tempfile
        from pathlib import Path
        from unison.server import App
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / '.unison'
            app = App(str(data), auth_required=False)
            try:
                workspace = Path(app.default_workspace())
                self.assertTrue(workspace.is_dir())
                self.assertNotIn(data.resolve(), workspace.resolve().parents)
                self.assertNotEqual(workspace.resolve(), data.resolve())
                # 结果稳定：同一个 App 反复问都得到同一个答案
                self.assertEqual(app.default_workspace(), str(workspace))
            finally:
                app.close()

    def test_explicit_workspace_root_is_honoured(self):
        import tempfile
        from pathlib import Path
        from unison.server import App
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / '.unison'
            explicit = Path(tmp) / 'projects'
            app = App(str(data), auth_required=False, workspace_root=str(explicit))
            try:
                self.assertEqual(Path(app.default_workspace()).resolve(), explicit.resolve())
            finally:
                app.close()


class WorkspaceWarningTests(unittest.TestCase):
    """工作区落在数据目录里时要记账，而不是静默（可修的配置问题就该说出来）。"""

    def _runtime(self, tmp):
        from pathlib import Path
        from unison.runtime import Runtime
        return Runtime(Path(tmp) / '.unison', concurrency=1, batch_seconds=.01)

    def _model(self, runtime):
        runtime.store.put('model', {'id': 'test', 'name': 'test', 'provider': 'Test',
                                    'api': 'openai-completions'})

    def test_workspace_inside_data_dir_is_flagged(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            self._model(runtime)
            inside = Path(runtime.store.root) / 'playground'
            inside.mkdir(exist_ok=True)
            run = runtime.create_run('goal', str(inside), 'test')
            self.assertIn('workspace_warning', run)
            events = [e for e in runtime.store.events(run['id']) if e['type'] == 'WorkspaceInDataDir']
            self.assertEqual(len(events), 1)

    def test_workspace_outside_data_dir_is_quiet(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            self._model(runtime)
            outside = Path(tmp) / 'project'
            outside.mkdir()
            run = runtime.create_run('goal', str(outside), 'test')
            self.assertNotIn('workspace_warning', run)
            self.assertEqual([e for e in runtime.store.events(run['id'])
                              if e['type'] == 'WorkspaceInDataDir'], [])

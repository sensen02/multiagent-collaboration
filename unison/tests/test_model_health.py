"""模型可用性维护：真实调用回写、失败分流、过期语义与限量复验。

全部离线：用一个本地 HTTP 服务扮演供应商，通过切换响应状态码驱动真实代码路径
（`Models.invoke()`），因此不消耗任何真实模型 token。
"""
import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from unison.maintenance import MAX_PROBES_PER_RUN, ModelHealthProbe
from unison.models import (HEALTH_BLOCKED, HEALTH_OK, HEALTH_OK_TTL, HEALTH_TRANSIENT_LIMIT,
                           HEALTH_UNKNOWN, Models, health_verdict)
from unison.store import Store


class HealthEndpoint(BaseHTTPRequestHandler):
    """状态码可切换的假供应商；`statuses` 非空时按顺序取（用于连续失败）。"""
    status = 200
    statuses = []
    calls = 0

    def log_message(self, *args):
        pass

    def _next_status(self):
        type(self).calls += 1
        if type(self).statuses:
            return type(self).statuses.pop(0)
        return type(self).status

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        status = self._next_status()
        if status == 200:
            body = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'pong'},
                                            'finish_reason': 'stop'}], 'usage': {}}).encode()
        else:
            body = b'{"error":"upstream"}'
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HealthStateMachineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        HealthEndpoint.status = 200
        HealthEndpoint.statuses = []
        HealthEndpoint.calls = 0
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.models = Models(self.store)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), HealthEndpoint)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {'id': 'probe:fixture', 'model': 'fixture',
                       'base_url': f'http://127.0.0.1:{self.server.server_port}/v1',
                       'api_key': 'fixture-key', 'adapter': 'chat-completions', 'api': 'openai-completions',
                       'context_window': 32000, 'max_output': 4096}
        self.models.save(dict(self.config))

    async def asyncTearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def health(self, model_id='probe:fixture'):
        return self.models.health_of(self.store.get('model', model_id))

    async def call(self):
        return await self.models.invoke('openai-completions', self.store.get('model', 'probe:fixture'),
                                        [{'role': 'user', 'content': 'ping'}], None, timeout=10)

    def events(self, kind):
        return [e for e in self.store.events() if e['type'] == kind]

    # ------------------------------------------------------------ 真实调用回写

    async def test_successful_call_records_ok_evidence_with_ttl(self):
        await self.call()
        health = self.health()
        self.assertEqual(health['state'], HEALTH_OK)
        self.assertEqual(health['source'], 'call')
        self.assertFalse(health['stale'])
        self.assertEqual(health['expires_at'], health['checked_at'] + HEALTH_OK_TTL)
        self.assertEqual(health['failures'], 0)

    async def test_successful_call_clears_previous_blocked_state(self):
        """这正是 gpt-5.6-terra 的场景：旧结论说不可用，真实调用成功必须把它改回来。"""
        model = self.store.get('model', 'probe:fixture')
        model['health'] = {'state': HEALTH_BLOCKED, 'code': 'AUTH', 'reason': '旧凭据', 'remedy': '换 Key',
                           'source': 'probe', 'checked_at': 1, 'expires_at': 2, 'failures': 1, 'history': []}
        self.store.put('model', model)
        await self.call()
        health = self.health()
        self.assertEqual(health['state'], HEALTH_OK)
        self.assertEqual(health['source'], 'call')
        changed = self.events('ModelHealthChanged')
        self.assertTrue(any(e['payload']['from'] == HEALTH_BLOCKED and e['payload']['to'] == HEALTH_OK
                            for e in changed))

    async def test_single_transient_failure_does_not_block(self):
        HealthEndpoint.status = 429
        with self.assertRaises(Exception):
            await self.call()
        health = self.health()
        self.assertNotEqual(health['state'], HEALTH_BLOCKED)
        self.assertEqual(health['code'], 'RATE_LIMIT')
        self.assertEqual(health['failures'], 1)
        self.assertFalse(self.events('ModelHealthChanged'))   # 状态没变就不写事件

    async def test_repeated_transient_failures_eventually_block(self):
        HealthEndpoint.status = 500
        for _ in range(HEALTH_TRANSIENT_LIMIT):
            with self.assertRaises(Exception):
                await self.call()
        health = self.health()
        self.assertEqual(health['state'], HEALTH_BLOCKED)
        self.assertEqual(health['code'], 'SERVER')
        self.assertEqual(health['failures'], HEALTH_TRANSIENT_LIMIT)
        self.assertIn('稍后重试', health['remedy'])

    async def test_auth_failure_blocks_immediately_with_remedy(self):
        HealthEndpoint.status = 401
        with self.assertRaises(Exception):
            await self.call()
        health = self.health()
        self.assertEqual(health['state'], HEALTH_BLOCKED)
        self.assertEqual(health['code'], 'AUTH')
        self.assertIn('API Key', health['remedy'])

    async def test_rate_limit_does_not_override_recent_call_evidence(self):
        await self.call()                      # 先有真实调用成功证据
        HealthEndpoint.status = 429
        with self.assertRaises(Exception):
            await self.call()
        health = self.health()
        self.assertEqual(health['state'], HEALTH_OK)    # 一次限流不足以推翻成功证据
        self.assertEqual(health['code'], 'RATE_LIMIT')  # 但错误被记录

    # ------------------------------------------------------------ 探测与真实证据的优先级

    async def test_probe_failure_does_not_override_recent_successful_call(self):
        await self.call()
        HealthEndpoint.status = 500
        # 探测失败会把错误抛给调用方（作业层负责收成失败结果），但**不能**推翻真实调用成功过的证据。
        with self.assertRaises(Exception):
            await self.models.verify('probe:fixture', timeout=10)
        health = self.health()
        self.assertEqual(health['state'], HEALTH_OK)
        self.assertEqual(health['source'], 'call')
        disagreements = self.events('ModelHealthDisagreement')
        self.assertEqual(len(disagreements), 1)
        self.assertEqual(disagreements[0]['payload']['probe_code'], 'SERVER')

    async def test_probe_success_after_expiry_marks_probe_source(self):
        model = self.store.get('model', 'probe:fixture')
        model['health'] = {'state': HEALTH_OK, 'code': None, 'reason': '', 'remedy': '', 'source': 'call',
                           'checked_at': 1, 'expires_at': 2, 'failures': 0, 'history': []}
        self.store.put('model', model)
        health = await self.models.verify('probe:fixture', timeout=10)
        self.assertEqual(health['state'], HEALTH_OK)
        self.assertEqual(health['source'], 'probe')
        self.assertFalse(health['stale'])

    # ------------------------------------------------------------ 过期与兼容

    def legacy(self, **reach):
        model = self.store.get('model', 'probe:fixture')
        model.pop('health', None)
        model['reachability'] = reach
        self.store.put('model', model)
        return self.health()

    async def test_legacy_reachability_projects_to_stale_health(self):
        """升级前的记录只有 reachability：必须被投影成"已过期"，从而可被复验。"""
        import time
        fresh = time.time() - 60
        health = self.legacy(reachable=False, checked_at=fresh, error='HTTP 429', code='RATE_LIMIT')
        self.assertTrue(health['stale'])
        self.assertTrue(health['legacy'])
        # 新鲜的瞬时失败只能是"未知/待复验"，绝不能被当成永久不可用。
        self.assertEqual(health['state'], HEALTH_UNKNOWN)
        # 旧的瞬时失败同理，仍然是待复验。
        self.assertEqual(self.legacy(reachable=False, checked_at=100, error='HTTP 500',
                                     code='SERVER')['state'], HEALTH_UNKNOWN)

    async def test_legacy_permanent_and_stale_config_errors_need_human(self):
        """凭据类永久错误、以及陈旧的参数/权限类错误都不该被反复探测。"""
        import time
        self.assertEqual(self.legacy(reachable=False, checked_at=100, error='HTTP 401',
                                     code='AUTH')['state'], HEALTH_BLOCKED)
        # 陈旧的 INVALID_REQUEST：通常是权限或模型不存在，重试无用。
        stale = self.legacy(reachable=False, checked_at=100, error='HTTP 404', code='INVALID_REQUEST')
        self.assertEqual(stale['state'], HEALTH_BLOCKED)
        self.assertFalse(stale['needs_probe'] if 'needs_probe' in stale else False)
        # 刚刚发生的 INVALID_REQUEST（可能是参数写错）值得复验。
        recent = self.legacy(reachable=False, checked_at=time.time() - 60, error='HTTP 400',
                             code='INVALID_REQUEST')
        self.assertEqual(recent['state'], HEALTH_UNKNOWN)

    def test_error_code_to_verdict_table_is_total(self):
        for code in ('AUTH', 'MISSING_CREDENTIAL', 'NO_ADAPTER', 'INVALID_REQUEST', 'RATE_LIMIT', 'QUOTA',
                     'SERVER', 'TIMEOUT', 'TRANSPORT', 'MALFORMED_RESPONSE', 'CONTEXT_WINDOW_EXCEEDED',
                     'EMPTY_RESPONSE', 'UNKNOWN'):
            kind, ttl, remedy = health_verdict(code)
            self.assertIn(kind, {'permanent', 'transient', 'neither'})
            self.assertGreater(ttl, 0)
            self.assertTrue(remedy)

    # ------------------------------------------------------------ 配置变更清标记

    async def test_config_change_clears_permanent_blocked_flags(self):
        HealthEndpoint.status = 401
        with self.assertRaises(Exception):
            await self.call()
        self.assertEqual(self.health()['state'], HEALTH_BLOCKED)
        cleared = self.models.clear_blocked()
        self.assertEqual(cleared, ['probe:fixture'])
        health = self.health()
        self.assertEqual(health['state'], HEALTH_UNKNOWN)
        self.assertFalse(health['remedy'])
        self.assertEqual(health['source'], 'config-change')
        self.assertTrue(self.events('ModelHealthCleared'))

    # ------------------------------------------------------------ 限量复验

    async def test_verify_provider_skips_models_with_recent_real_evidence(self):
        """这是省 token 的核心断言：刚被真实调用过的模型，复验按钮**不会**再打它。"""
        self.store.put('provider', {'id': 'probe', 'name': 'Fake', 'base_url': self.config['base_url'],
                                    'api': 'openai-completions', 'adapter': 'chat-completions',
                                    'models': [{'id': 'fixture', 'name': 'Fixture', 'contextWindow': 32000,
                                                'maxTokens': 4096}]})
        await self.call()                                   # 真实调用成功 → 24h 内无需探测
        calls_before = HealthEndpoint.calls
        result = await self.models.verify_provider('probe')
        self.assertEqual(result['checked'], 0)
        self.assertEqual(HealthEndpoint.calls, calls_before)   # 一次调用都没发
        self.assertEqual([item['model'] for item in result['skipped']], ['probe:fixture'])
        self.assertEqual(result['skipped'][0]['source'], 'call')
        self.assertFalse(result['force'])

    async def test_health_view_marks_needs_probe_and_sorts(self):
        view = self.models.health_view()
        entry = next(item for item in view if item['id'] == 'probe:fixture')
        self.assertEqual(entry['state'], HEALTH_UNKNOWN)
        self.assertTrue(entry['needs_probe'])
        self.assertIn('stale', entry)


class ModelHealthJobTests(unittest.IsolatedAsyncioTestCase):
    """维护作业：限量、只打过期项、预演不调用、静默。"""

    async def asyncSetUp(self):
        HealthEndpoint.status = 200
        HealthEndpoint.statuses = []
        HealthEndpoint.calls = 0
        self.temp = tempfile.TemporaryDirectory()
        from pathlib import Path
        from unison.runtime import Runtime
        self.runtime = Runtime(Path(self.temp.name) / 'data', concurrency=1, batch_seconds=.01)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), HealthEndpoint)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        base = f'http://127.0.0.1:{self.server.server_port}/v1'
        for index in range(5):
            self.runtime.models.save({'id': f'probe:m{index}', 'model': 'fixture', 'base_url': base,
                                      'api_key': 'fixture-key', 'adapter': 'chat-completions',
                                      'context_window': 32000, 'max_output': 4096})

    async def asyncTearDown(self):
        self.server.shutdown()
        self.server.server_close()
        await self.runtime.stop()
        self.temp.cleanup()

    def job(self):
        return self.runtime.maintenance.find('model-health')

    async def test_dry_run_lists_plan_without_any_call(self):
        result = await self.runtime.maintenance.run_job(self.job(), dry_run=True)
        self.assertEqual(HealthEndpoint.calls, 0)
        self.assertEqual(result['counts']['planned'], MAX_PROBES_PER_RUN)
        self.assertEqual(result['counts']['pending'], 5)
        self.assertIn('预演', result['summary'])

    async def test_probe_run_is_bounded_and_respects_limit(self):
        result = await self.runtime.maintenance.run_job(self.job())
        self.assertEqual(HealthEndpoint.calls, MAX_PROBES_PER_RUN)   # 5 个待验，本次只打 3 个
        self.assertEqual(result['counts']['probed'], MAX_PROBES_PER_RUN)
        self.assertEqual(result['counts']['reachable'], MAX_PROBES_PER_RUN)
        self.assertIn('仍有 2 个待复验', result['summary'])

    async def test_second_run_skips_models_with_recent_evidence(self):
        await self.runtime.maintenance.run_job(self.job())
        calls_after_first = HealthEndpoint.calls
        view = self.runtime.models.health_view()
        fresh = [item for item in view if item['state'] == 'ok' and not item['stale']]
        self.assertEqual(len(fresh), MAX_PROBES_PER_RUN)
        self.assertTrue(all(not item['needs_probe'] for item in fresh))
        result = await self.runtime.maintenance.run_job(self.job())
        # 只剩没打过的 2 个；本次最多 2 次调用，绝不再打刚验证过的。
        self.assertEqual(HealthEndpoint.calls - calls_after_first, 2)
        self.assertEqual(result['counts']['probed'], 2)

    async def test_blocked_models_are_not_retried(self):
        HealthEndpoint.status = 401
        await self.runtime.maintenance.run_job(self.job())
        blocked = [item for item in self.runtime.models.health_view() if item['state'] == HEALTH_BLOCKED]
        self.assertEqual(len(blocked), MAX_PROBES_PER_RUN)
        self.assertTrue(all(not item['needs_probe'] for item in blocked))
        calls_before = HealthEndpoint.calls
        await self.runtime.maintenance.run_job(self.job())
        # 凭据错误重试无用：后续作业只碰"待复验"的两个，不会重复打已知 AUTH 的模型。
        self.assertEqual(HealthEndpoint.calls - calls_before, 2)

    async def test_silent_job_writes_no_finish_event(self):
        await self.runtime.maintenance.run_job(self.job())
        self.assertFalse([e for e in self.runtime.store.events() if e['type'] == 'MaintenanceFinished'])
        self.assertTrue([e for e in self.runtime.store.events() if e['type'] == 'ModelHealthChanged'])


if __name__ == '__main__':
    unittest.main()

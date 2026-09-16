"""额度闸门：并发串行化、窗口限流、退避重试、用量记账与模型降档。

全部离线：一个可控的假上游（能设定状态码序列与延迟，并统计**同时在途数**）。
判据直接来自真实事故——同一秒向同一模型发出两个大请求导致 429 + 524。
"""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from unison.models import health_verdict
from unison.limiter import (MAX_MODEL_RETRIES, RETRYABLE_CODES, QueueTimeout, RateLimiter,
                            backoff_seconds, bucket_key, capacity_of)
from unison.models import FAILOVER_CODES, ModelError, Models
from unison.store import Store


class FakeUpstream(BaseHTTPRequestHandler):
    """可编程上游：状态码序列 + 人为延迟；记录同时在途请求数的峰值。"""
    statuses = []
    status = 200
    delay = 0.0
    lock = threading.Lock()
    in_flight = 0
    peak_in_flight = 0
    calls = 0
    retry_after = None
    seen = []
    prompt_tokens = 100
    completion_tokens = 20
    truncate = 0        # 声明比实际多写这么多字节 → 客户端读到 EOF，抛 IncompleteRead

    def log_message(self, *args):
        pass

    def do_POST(self):
        with type(self).lock:
            type(self).in_flight += 1
            type(self).calls += 1
            type(self).peak_in_flight = max(type(self).peak_in_flight, type(self).in_flight)
        try:
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            type(self).seen.append({'in_flight': type(self).in_flight, 'headers': dict(self.headers)})
            if type(self).delay:
                time.sleep(type(self).delay)
            with type(self).lock:
                status = type(self).statuses.pop(0) if type(self).statuses else type(self).status
            if status == 200:
                body = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'pong'},
                                                'finish_reason': 'stop'}],
                                   'usage': {'prompt_tokens': type(self).prompt_tokens,
                                             'completion_tokens': type(self).completion_tokens}}).encode()
            else:
                body = b'{"error":"upstream"}'
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            if type(self).retry_after is not None and status != 200:
                self.send_header('Retry-After', str(type(self).retry_after))
            self.send_header('Content-Length', str(len(body) + type(self).truncate))
            self.end_headers()
            self.wfile.write(body)
        finally:
            with type(self).lock:
                type(self).in_flight -= 1


class LimiterHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeUpstream.statuses = []
        FakeUpstream.status = 200
        FakeUpstream.delay = 0.0
        FakeUpstream.in_flight = 0
        FakeUpstream.peak_in_flight = 0
        FakeUpstream.calls = 0
        FakeUpstream.retry_after = None
        FakeUpstream.seen = []
        FakeUpstream.prompt_tokens = 100
        FakeUpstream.completion_tokens = 20
        FakeUpstream.truncate = 0
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeUpstream)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.models = Models(self.store)
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def model(self, model_id='fixture', capacity=None, provider_capacity=None, provider='probe'):
        record = {'id': f'{provider}:{model_id}', 'model': model_id, 'base_url': self.base,
                  'api_key': 'k', 'api': 'openai-completions', 'adapter': 'chat-completions',
                  'provider_id': provider, 'context_window': 32000, 'max_output': 4096}
        if capacity:
            record['capacity'] = capacity
        self.store.put('model', record)
        existing = None
        try:
            existing = self.store.get('provider', provider)
        except ValueError:
            existing = {'id': provider, 'name': provider, 'base_url': self.base, 'api': 'openai-completions',
                        'adapter': 'chat-completions', 'models': []}
        if provider_capacity:
            existing['capacity'] = provider_capacity
        self.store.put('provider', existing)
        return record

    async def call(self, model_id='probe:fixture', run_id=None, task_id=None):
        return await self.models.call(model_id, [{'role': 'user', 'content': 'ping'}], None,
                                      run_id=run_id, task_id=task_id)


class ConcurrencyGateTests(LimiterHarness):
    async def test_same_bucket_never_exceeds_declared_concurrency(self):
        """事故判据：同一秒向同一模型发两个请求，必须串行而不是一起打上游。"""
        self.model(provider_capacity={'concurrency': 1})
        FakeUpstream.delay = 0.2
        await asyncio.gather(self.call(), self.call())
        self.assertEqual(FakeUpstream.peak_in_flight, 1)
        self.assertEqual(FakeUpstream.calls, 2)

    async def test_unknown_capacity_defaults_to_serial(self):
        """未声明额度时最保守：同桶并发 1。这一条单独就能消除那次 429+524。"""
        self.model()
        FakeUpstream.delay = 0.15
        await asyncio.gather(self.call(), self.call(), self.call())
        self.assertEqual(FakeUpstream.peak_in_flight, 1)
        self.assertEqual(capacity_of({'model': 'x'})['concurrency'], 1)

    async def test_model_capacity_overrides_provider_default(self):
        self.model(capacity={'concurrency': 3}, provider_capacity={'concurrency': 1})
        FakeUpstream.delay = 0.2
        await asyncio.gather(*[self.call() for _ in range(3)])
        self.assertEqual(FakeUpstream.peak_in_flight, 3)

    async def test_different_buckets_do_not_block_each_other(self):
        provider = {'capacity': {'concurrency': 1}}
        self.store.put('provider', {'id': 'p1', 'name': 'p1', 'base_url': self.base, **provider})
        self.store.put('provider', {'id': 'p2', 'name': 'p2', 'base_url': self.base, **provider})
        for pid in ('p1', 'p2'):
            self.store.put('model', {'id': f'{pid}:m', 'model': 'm', 'base_url': self.base, 'api_key': 'k',
                                     'api': 'openai-completions', 'adapter': 'chat-completions',
                                     'provider_id': pid, 'context_window': 32000, 'max_output': 4096})
        FakeUpstream.delay = 0.2
        await asyncio.gather(self.call('p1:m'), self.call('p2:m'))
        self.assertEqual(FakeUpstream.peak_in_flight, 2)   # 不同桶可以并行
        self.assertNotEqual(bucket_key({'provider_id': 'p1', 'model': 'm'}),
                            bucket_key({'provider_id': 'p2', 'model': 'm'}))

    async def test_queue_timeout_is_reported_clearly(self):
        self.model(provider_capacity={'concurrency': 1, 'queue_timeout': 0.2})
        FakeUpstream.delay = 1.2
        first = asyncio.ensure_future(self.call())
        await asyncio.sleep(0.05)
        with self.assertRaises(QueueTimeout) as caught:
            await self.call()
        self.assertIn('排队超过', str(caught.exception))
        await first
        self.assertEqual(FakeUpstream.peak_in_flight, 1)


class WindowBudgetTests(LimiterHarness):
    async def test_token_window_throttles_until_it_frees(self):
        """窗口额度用满后应当排队等待，而不是继续打上游。

        第一次调用用掉 180 tokens（真实 usage），窗口上限 200：第二次 20+180 > 200，
        因此必须等到窗口滚出第一次的用量才放行。
        """
        self.model(provider_capacity={'concurrency': 1, 'tpm': 200, 'window': 0.6})
        FakeUpstream.prompt_tokens, FakeUpstream.completion_tokens = 150, 30
        started = time.monotonic()
        await self.call()
        await self.call()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertEqual(FakeUpstream.calls, 2)
        self.assertGreaterEqual(self.models.limiter.snapshot()[0]['throttled'], 1)

    async def test_request_window_is_enforced(self):
        self.model(provider_capacity={'concurrency': 1, 'rpm': 1, 'window': 0.6})
        started = time.monotonic()
        await self.call()
        await self.call()
        self.assertGreaterEqual(time.monotonic() - started, 0.4)

    async def test_usage_is_recorded_with_attribution(self):
        self.model()
        await self.call(run_id='run_x', task_id='task_y')
        usage = self.models.limiter.usage_summary('run_x')
        self.assertEqual(usage['calls'], 1)
        self.assertEqual(usage['tokens'], 120)
        self.assertEqual(usage['prompt_tokens'], 100)
        self.assertEqual(usage['completion_tokens'], 20)
        self.assertIn('probe:fixture', usage['by_model'])
        records = self.store.all('model_usage')
        self.assertEqual(records[0]['task_id'], 'task_y')
        self.assertEqual(self.models.limiter.usage_summary('other')['calls'], 0)

    async def test_snapshot_exposes_load_counters(self):
        self.model(provider_capacity={'concurrency': 1, 'tpm': 1000})
        await self.call()
        snapshot = self.models.limiter.snapshot()
        entry = snapshot[0]
        self.assertEqual(entry['peak_in_flight'], 1)
        self.assertEqual(entry['window_requests'], 1)
        self.assertEqual(entry['window_tokens'], 120)
        self.assertEqual(entry['capacity']['concurrency'], 1)


class RetryTests(LimiterHarness):
    async def test_rate_limit_is_retried_and_succeeds(self):
        self.model()
        FakeUpstream.statuses = [429]
        message, usage = await self.call()
        self.assertEqual(message['content'], 'pong')
        self.assertEqual(FakeUpstream.calls, 2)
        self.assertEqual(self.models.limiter.snapshot()[0]['retries'], 1)

    async def test_upstream_timeout_524_is_retryable(self):
        """事故里的第二个错误码：524 必须可重试，而不是直接杀死任务。"""
        self.model()
        FakeUpstream.statuses = [524] * (1 + MAX_MODEL_RETRIES)
        with self.assertRaises(Exception) as caught:
            await self.call()
        self.assertEqual(getattr(caught.exception, 'code', ''), 'UPSTREAM_TIMEOUT')
        self.assertEqual(FakeUpstream.calls, 1 + MAX_MODEL_RETRIES)
        # 与 429 一样可重试：单独一次 524 不应该让调用失败。
        FakeUpstream.statuses = [524]
        FakeUpstream.calls = 0
        message, _ = await self.call()
        self.assertEqual(message['content'], 'pong')
        self.assertEqual(FakeUpstream.calls, 2)

    async def test_permanent_errors_are_not_retried(self):
        for status in (401, 404):
            FakeUpstream.statuses = []
            FakeUpstream.status = status
            FakeUpstream.calls = 0
            self.model(f'm{status}')
            with self.assertRaises(Exception):
                await self.call(f'probe:m{status}')
            self.assertEqual(FakeUpstream.calls, 1, f'{status} 不应重试')
        FakeUpstream.status = 200

    async def test_retry_after_header_is_respected(self):
        self.model()
        FakeUpstream.statuses = [429]
        FakeUpstream.retry_after = 1
        started = time.monotonic()
        await self.call()
        self.assertGreaterEqual(time.monotonic() - started, 1.0)

    async def test_retry_exhaustion_reports_one_failure_event(self):
        """重试是同一次故障的多次尝试：只应记一次失败，而不是把模型打成 blocked。"""
        self.model()
        FakeUpstream.statuses = [500] * 4
        with self.assertRaises(Exception):
            await self.call()
        self.assertEqual(FakeUpstream.calls, 1 + MAX_MODEL_RETRIES)
        health = self.models.health_of(self.store.get('model', 'probe:fixture'))
        self.assertEqual(health['failures'], 1)
        self.assertNotEqual(health['state'], 'blocked')

    def test_backoff_is_bounded_and_jittered(self):
        samples = [backoff_seconds(0) for _ in range(20)]
        self.assertTrue(all(0 < x <= 0.5 for x in samples), samples)   # 首次退避不超过 base
        self.assertGreater(len(set(samples)), 1)                       # 有抖动，避免同时重试撞车
        self.assertTrue(all(0 < backoff_seconds(n) <= 8.0 for n in range(12)))  # 指数增长但有上限
        self.assertEqual(backoff_seconds(0, retry_after=3), 3.0)   # Retry-After 优先
        self.assertEqual(backoff_seconds(0, retry_after='999'), 120.0)   # 但仍有上限


class TierDowngradeTests(LimiterHarness):
    def provider_with_tiers(self):
        self.store.put('provider', {'id': 'probe', 'name': 'probe', 'base_url': self.base,
                                    'api': 'openai-completions', 'adapter': 'chat-completions',
                                    'tiers': {'heavy': ['big'], 'light': ['small']}, 'models': []})
        for name in ('big', 'small'):
            self.store.put('model', {'id': f'probe:{name}', 'model': name, 'base_url': self.base,
                                     'api_key': 'k', 'api': 'openai-completions',
                                     'adapter': 'chat-completions', 'provider_id': 'probe',
                                     'context_window': 32000, 'max_output': 4096})

    def test_heavy_model_has_a_light_target(self):
        self.provider_with_tiers()
        self.assertEqual(self.models.tiers_of(self.store.get('model', 'probe:big')), 'heavy')
        self.assertEqual(self.models.tiers_of(self.store.get('model', 'probe:small')), 'light')
        self.assertEqual(self.models.downgrade_target('probe:big'), 'probe:small')

    def test_no_downgrade_without_declared_tiers(self):
        self.model('solo')
        self.assertIsNone(self.models.downgrade_target('probe:solo'))
        self.assertEqual(self.models.tiers_of(self.store.get('model', 'probe:solo')), 'standard')

    def test_blocked_light_model_is_skipped(self):
        self.provider_with_tiers()
        small = self.store.get('model', 'probe:small')
        small['health'] = {'state': 'blocked', 'code': 'AUTH', 'checked_at': time.time(), 'expires_at': 0,
                           'failures': 1, 'history': [], 'source': 'probe'}
        self.store.put('model', small)
        self.assertIsNone(self.models.downgrade_target('probe:big'))

    def test_light_model_overrides_via_record_tier(self):
        self.provider_with_tiers()
        big = self.store.get('model', 'probe:big')
        big['tier'] = 'light'
        self.store.put('model', big)
        self.assertIsNone(self.models.downgrade_target('probe:big'))   # 记录级声明优先


class ModelFailoverTargetTests(LimiterHarness):
    """故障转移候选：与降档对称，但方向相反——为活下来往上换，而不是为省钱往下换。"""

    def provider(self, names, **extra):
        record = {'id': 'probe', 'name': 'probe', 'base_url': self.base, 'api': 'openai-completions',
                  'adapter': 'chat-completions', 'models': [{'id': n} for n in names]}
        record.update(extra)
        self.store.put('provider', record)
        for name in names:
            self.store.put('model', {'id': f'probe:{name}', 'model': name, 'base_url': self.base,
                                     'api_key': 'k', 'api': 'openai-completions', 'adapter': 'chat-completions',
                                     'provider_id': 'probe', 'context_window': 32000, 'max_output': 4096})
        return record

    def block(self, model_id, code='RATE_LIMIT'):
        record = self.store.get('model', model_id)
        record['health'] = {'state': 'blocked', 'code': code, 'checked_at': time.time(), 'expires_at': 0,
                            'failures': 3, 'history': [], 'source': 'call'}
        self.store.put('model', record)

    def ids(self, model_id):
        return [(item['id'], item['source']) for item in self.models.failover_targets(model_id)]

    def test_declared_order_wins_and_can_cross_providers(self):
        """显式声明的顺序就是优先级；写全 `provider:模型名` 可以换到另一个 provider。"""
        self.provider(['main', 'backup', 'other'], failover=['backup', 'other'])
        self.assertEqual(self.ids('probe:main'), [('probe:backup', 'declared'), ('probe:other', 'declared')])

    def test_tier_escalation_goes_up_from_light_to_heavy(self):
        """你说的"5.4mini 不行就换比他强一点的"：light → standard → heavy，由弱到强。"""
        self.provider(['small', 'mid', 'big'],
                      tiers={'light': ['small'], 'standard': ['mid'], 'heavy': ['big']})
        self.assertEqual(self.ids('probe:small'),
                         [('probe:mid', 'tier'), ('probe:big', 'tier')])   # 不会往回降到自己
        # 升档排在前面；升完才是"同 provider 的其他模型"兜底。
        # 兜底里**允许出现更弱的模型**是有意的：退一步交付好过判死，而换过什么全程有事件可查。
        self.assertEqual(self.ids('probe:mid'),
                         [('probe:big', 'tier'), ('probe:small', 'provider')])

    def test_without_any_declaration_falls_back_to_same_provider(self):
        """没声明 tiers/failover 时不能变成"没有候选"——否则这个机制永远不触发。"""
        self.provider(['main', 'spare'])
        self.assertEqual(self.ids('probe:main'), [('probe:spare', 'provider')])

    def test_blocked_and_missing_candidates_are_skipped(self):
        """宁可不换，也不要静默换成一个同样用不了的模型。"""
        self.provider(['main', 'dead', 'gone'])
        self.block('probe:dead', 'AUTH')
        self.store.db.execute('DELETE FROM records WHERE kind=? AND id=?', ('model', 'probe:gone'))
        self.assertEqual(self.ids('probe:main'), [])

    def test_model_without_provider_has_no_candidates(self):
        self.model('solo')
        self.assertEqual(self.models.failover_targets('probe:solo'), [])

    def test_failover_codes_cover_model_side_failures_only(self):
        """分流要与 `_VERDICTS` 一致：模型侧的失败才换模型。

        真实事故：gpt-5.4-mini 的 `MODEL_NOT_SUPPORTED`（账户类型不支持）重试永远无用，
        必须换模型；而 AUTH/MISSING_CREDENTIAL 是 provider 级的，同 provider 换模型解决不了。
        """
        self.assertIn('MODEL_NOT_SUPPORTED', FAILOVER_CODES)
        self.assertIn('MODEL_NOT_FOUND', FAILOVER_CODES)
        for code in ('SERVER', 'UPSTREAM_TIMEOUT', 'TIMEOUT', 'TRANSPORT', 'EMPTY_RESPONSE'):
            self.assertIn(code, FAILOVER_CODES)
        for code in ('AUTH', 'MISSING_CREDENTIAL', 'NO_ADAPTER', 'INVALID_REQUEST',
                     'CONTEXT_WINDOW_EXCEEDED'):
            self.assertNotIn(code, FAILOVER_CODES)


class TruncatedResponseTests(LimiterHarness):
    """响应被中途掐断，必须当成**传输问题**，而不是一个没人认识的异常。"""

    async def test_truncated_body_is_classified_as_transport(self):
        """真机 `run_1da778c90c48` 的根任务死于 `IncompleteRead(62776 bytes read)`。

        `IncompleteRead` 既不是 `URLError` 也不是 `OSError`（它是 `HTTPException`），
        所以它**逃过了错误码归类**：不重试、不记健康、不故障转移，任务直接带着一句
        原始异常字符串判死——连"这是传输问题"都看不出来。
        """
        FakeUpstream.truncate = 200
        self.model('fixture')
        with self.assertRaises(ModelError) as caught:
            await self.call()
        self.assertEqual(caught.exception.code, 'TRANSPORT')
        # 归类对了，两件后续处置才会发生：退避重试、以及够格换模型。
        self.assertIn('TRANSPORT', RETRYABLE_CODES)
        self.assertIn('TRANSPORT', FAILOVER_CODES)
        self.assertEqual(FakeUpstream.calls, 1 + MAX_MODEL_RETRIES)   # 重试真的发生了


class RetryableCodeTests(unittest.TestCase):
    def test_empty_and_malformed_responses_are_retryable(self):
        """真机 `run_5b90b2ff368f`：玩家 B 死于一次 `EMPTY_RESPONSE`。

        models.py 的处置表写着"重试"，`RETRYABLE_CODES` 里却没有它——声明与实现不一致，
        网关偶发一次空响应就把任务判死。现在两者一致。
        """
        for code in ('EMPTY_RESPONSE', 'MALFORMED_RESPONSE'):
            self.assertIn(code, RETRYABLE_CODES)
            family, _ttl, advice = health_verdict(code)
            self.assertIn('重试', advice)
            self.assertIn(family, {'transient', 'neither'})

    def test_permanent_errors_are_still_not_retried(self):
        for code in ('AUTH', 'MODEL_NOT_FOUND', 'MISSING_CREDENTIAL'):
            self.assertNotIn(code, RETRYABLE_CODES)


if __name__ == '__main__':
    unittest.main()

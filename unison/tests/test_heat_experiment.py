"""过热的端到端证据：复现"同一秒双并发 → 524"，并证明闸门消除它。

这不是机制单测（那些在 `test_limiter.py`），而是一次**可复现的对照实验**：

- 上游是一个对本桶并发请求直接返回 524 的假网关（模拟 Cloudflare 源站等待超时），
  并记录**同时在途请求数**；
- 对照组：`concurrency=4`（改动前的行为）→ 应当出现并发撞击与失败；
- 实验组：`concurrency=1`（新的默认）→ 峰值在途 1、**0 次 524、0 个任务失败**。

假网关只惩罚"同桶真正同时到达"的请求：第一个请求正常服务，第二个在第一个还没结束时
到达就拿到 524。这样"闸门是否真的把请求串起来"就是可判定的行为差异。
"""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from unison.runtime import Runtime


class PenalizingGateway(BaseHTTPRequestHandler):
    """并发即惩罚的假网关：第 N 个同时到达的请求返回 524。"""
    lock = threading.Lock()
    in_flight = 0
    peak_in_flight = 0
    total = 0
    concurrent_rejections = 0
    delay = 0.35
    concurrency_tolerance = 1     # 允许同时在途几个；超出即 524

    def log_message(self, *args):
        pass

    def do_POST(self):
        with type(self).lock:
            type(self).total += 1
            type(self).in_flight += 1
            type(self).peak_in_flight = max(type(self).peak_in_flight, type(self).in_flight)
            over = type(self).in_flight > type(self).concurrency_tolerance
            if over:
                type(self).concurrent_rejections += 1
        try:
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if over:
                # 上游把请求挂在队列里不响应，网关等不下去 → 524
                body = b'{"error":"origin timeout"}'
                self.send_response(524)
            else:
                time.sleep(type(self).delay)
                body = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'ok'},
                                                'finish_reason': 'stop'}],
                                   'usage': {'prompt_tokens': 40, 'completion_tokens': 5}}).encode()
                self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            with type(self).lock:
                type(self).in_flight -= 1


class HeatExperiment(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        PenalizingGateway.in_flight = 0
        PenalizingGateway.peak_in_flight = 0
        PenalizingGateway.total = 0
        PenalizingGateway.concurrent_rejections = 0
        self.temp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), PenalizingGateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    async def asyncTearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def build(self, concurrency):
        """搭一个隔离运行时：4 个任务全部用同一个重模型（复现事故的"全员一个模型"）。"""
        runtime = Runtime(Path(self.temp.name) / f'data-{concurrency}', concurrency=concurrency,
                          batch_seconds=.01)
        base = f'http://127.0.0.1:{self.server.server_port}/v1'
        runtime.store.put('provider', {'id': 'gw', 'name': 'gateway', 'base_url': base,
                                       'api': 'openai-completions', 'adapter': 'chat-completions',
                                       'capacity': {'concurrency': concurrency, 'queue_timeout': 60},
                                       'models': []})
        runtime.store.put('model', {'id': 'gw:heavy', 'model': 'heavy', 'base_url': base,
                                    'api_key': 'k', 'api': 'openai-completions',
                                    'adapter': 'chat-completions', 'provider_id': 'gw',
                                    'context_window': 32000, 'max_output': 4096,
                                    'base_url': base})
        return runtime

    async def run_four_children(self, runtime):
        """一个根任务 + 4 个子任务，全部打同一个模型——正是事故的形状。"""
        workspace = Path(self.temp.name) / 'project'
        workspace.mkdir(exist_ok=True)
        (workspace / 'README.md').write_text('x\n')

        async def adapter(config, messages, tools):
            return {'role': 'assistant', 'content': 'done'}, {}

        runtime.models.adapters['local'] = adapter
        run = runtime.create_run('并发撞击实验', str(workspace), 'gw:heavy')
        root = runtime.task(run['root_task'])
        children = [runtime.create_task(runtime.run(run['id']), f'child {i}', 'gw:heavy', root)
                    for i in range(4)]
        # 绕过调度器直接并发调用模型：测的就是"同一时刻最多几个在途"。
        tasks = [asyncio.ensure_future(
            runtime.models.call('gw:heavy', [{'role': 'user', 'content': 'ping'}], None,
                                run_id=run['id'], task_id=child['id']))
            for child in children]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results

    async def test_ungated_concurrency_reacts_the_way_the_incident_did(self):
        """对照组（并发 4，即改动前的每桶行为）：请求互相撞击、上游拒绝、额外重试。

        注意任务**不会**因此失败——因为这些拒绝是可重试码，退避重试把任务救了回来。
        这正是"两个机制各管一件事"：重试保证任务不死，闸门保证不去打上游。
        """
        runtime = self.build(4)
        try:
            results = await self.run_four_children(runtime)
            self.assertGreaterEqual(PenalizingGateway.peak_in_flight, 2)      # 真的撞上了
            self.assertGreater(PenalizingGateway.concurrent_rejections, 0)     # 上游因此拒绝
            # 重试确实救回了一部分（有人成功），但**不保证**全部救回：阈值内并发仍有撞击、
            # 重试自身也会撞车，所以它只能减轻、不能预防。
            # 这里只断言确定的量（撞击发生、上游因此拒绝、请求数多于任务数），
            # 不断言"必有请求失败"——那取决于退避抖动与到达时序，会变成随机红灯。
            self.assertTrue([x for x in results if not isinstance(x, Exception)])
            self.assertGreater(PenalizingGateway.total, 4)                     # 多打了几次
            self.assertGreater(runtime.models.limiter.snapshot()[0]['retries'], 0)
            failed=[x for x in results if isinstance(x, Exception)]
            print(f'对照实验：峰值在途 {PenalizingGateway.peak_in_flight}、'
                  f'上游拒绝 {PenalizingGateway.concurrent_rejections} 次、总请求 {PenalizingGateway.total}、'
                  f'重试耗尽 {len(failed)} 个')
        finally:
            await runtime.stop()

    async def test_gate_serializes_and_prevents_the_incident(self):
        """实验组：concurrency=1 时峰值在途 1、0 次并发拒绝、0 个失败。"""
        runtime = self.build(1)
        try:
            results = await self.run_four_children(runtime)
            self.assertEqual(PenalizingGateway.peak_in_flight, 1)
            self.assertFalse([x for x in results if isinstance(x, Exception)])
            self.assertEqual(PenalizingGateway.concurrent_rejections, 0)
            self.assertEqual(PenalizingGateway.total, 4)      # 一次都不用重试
            buckets = runtime.models.limiter.snapshot()
            self.assertEqual(buckets[0]['peak_in_flight'], 1)
            self.assertEqual(buckets[0]['capacity']['concurrency'], 1)
            # 排队是可观测的：4 个请求串行，总等待时间必然大于 0。
            self.assertGreater(buckets[0]['total_wait_seconds'], 0)
            usage = runtime.models.limiter.usage_summary()
            self.assertEqual(usage['calls'], 4)
            self.assertEqual(usage['tokens'], 4 * 45)
        finally:
            await runtime.stop()


if __name__ == '__main__':
    unittest.main()

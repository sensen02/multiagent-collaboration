"""模型调用的物理额度闸门：按桶串行化、按窗口限流、退避重试、用量记账。

**为什么需要它**：调度器只限制"同时跑几个任务"（`concurrency`），却完全不限制
"同时对同一个 provider+模型发几个请求"。实测事故：同一秒内向 `gpt-5.6-sol` 发出两个
25–28 KB 的请求，触发上游 429 + 网关 524，并且因为运行时没有重试，直接杀死了两个任务。

**这一层只做物理额度**，不做业务决策，也**不做计费**：

- `concurrency`：该桶同一时刻最多几个在途请求（未知即 1，最保守）；
- `tpm` / `rpm`：滑动窗口内的 token 与请求数上限（默认不限，由人按账号额度声明）；
- 超额**排队**而不是失败，排队有上限；取消/超时能立刻打断；
- 调用失败后按码退避重试（`Retry-After` 优先），永久错误不重试。

用量的**真实值来自响应里的 `usage`**；没有 usage 时按 `input_bytes // CHARS_PER_TOKEN`
保守估算，避免"第一次请求不知道多大就放行"。
"""
from __future__ import annotations

import asyncio
import json
import random
import time

from .store import now

CHARS_PER_TOKEN = 3          # 保守估算：UTF-8 字节 → token（真实值优先）
DEFAULT_WINDOW = 60.0
DEFAULT_CONCURRENCY = 1      # 未声明额度时最保守：同桶串行
DEFAULT_QUEUE_TIMEOUT = 600.0
MAX_MODEL_RETRIES = 2
BACKOFF_BASE = 0.5
BACKOFF_CAP = 8.0
RETRY_AFTER_CAP = 120.0

# 可重试（瞬时）与不可重试（需要人处理或语义错误）：与 models.health_verdict 的分流一致。
# 值得退避重试的失败：都是"这次没成，但同一个请求再发一次可能成"的类型。
# `EMPTY_RESPONSE`（模型返回空消息）此前不在其中，尽管 models.py 的处置表写着"重试"——
# 于是网关偶发一次空响应就把整个任务判死（真机 `run_5b90b2ff368f` 的玩家 B 就是这么挂的）。
RETRYABLE_CODES = {'RATE_LIMIT', 'UPSTREAM_TIMEOUT', 'SERVER', 'TIMEOUT', 'TRANSPORT',
                   'EMPTY_RESPONSE', 'MALFORMED_RESPONSE'}


class QueueTimeout(RuntimeError):
    """排队等待超过上限：说明额度被占满且迟迟不释放。"""


class BudgetExceeded(RuntimeError):
    """该运行/任务的调用次数或 token 用量已超过配置的硬上限。"""


def bucket_key(config):
    """桶 = provider + 模型。同一把 Key 下不同模型通常有独立的额度，因此按模型分开。"""
    provider = config.get('provider_id') or config.get('base_url') or 'provider'
    model = config.get('model') or config.get('id') or 'model'
    return f'{provider}::{model}'


def capacity_of(config):
    """模型级 capacity 覆盖 provider 级默认；都没声明时给最保守的并发 1。"""
    capacity = {}
    for source in (config.get('provider_capacity'), config.get('capacity')):
        if isinstance(source, dict):
            capacity.update({k: v for k, v in source.items() if v is not None})
    result = {'concurrency': int(capacity.get('concurrency') or DEFAULT_CONCURRENCY),
              'tpm': capacity.get('tpm'), 'rpm': capacity.get('rpm'),
              'window': float(capacity.get('window') or DEFAULT_WINDOW),
              'queue_timeout': float(capacity.get('queue_timeout') or DEFAULT_QUEUE_TIMEOUT)}
    result['concurrency'] = max(1, result['concurrency'])
    return result


class _Bucket:
    """一个 (provider, model) 桶的在途计数与滑动窗口用量。"""

    def __init__(self, key, capacity):
        self.key = key
        self.capacity = capacity
        self.semaphore = asyncio.Semaphore(capacity['concurrency'])
        self.in_flight = 0
        self.peak_in_flight = 0
        self.samples = []          # [(timestamp, tokens)]，窗口内的用量样本
        self.requests = []         # [timestamp]，窗口内的请求时间
        self.queued = 0
        self.total_wait = 0.0
        self.retries = 0
        self.throttled = 0         # 因窗口超限而等待的次数

    def window_usage(self, window, at=None):
        at = time.monotonic() if at is None else at
        cutoff = at - window
        self.samples = [item for item in self.samples if item[0] > cutoff]
        self.requests = [item for item in self.requests if item > cutoff]
        return sum(tokens for _, tokens in self.samples), len(self.requests)

    def add_tokens(self, tokens, at=None):
        at = time.monotonic() if at is None else at
        self.samples.append((at, float(tokens)))

    def headroom_seconds(self, window, tpm, rpm, tokens):
        """还要等多久才可能放行：受最紧的那个约束决定。"""
        at = time.monotonic()
        used_tokens, used_requests = self.window_usage(window, at)
        waits = []
        if tpm:
            if used_tokens + tokens > tpm and self.samples:
                waits.append(max(0.0, self.samples[0][0] + window - at))
        if rpm:
            if used_requests + 1 > rpm and self.requests:
                waits.append(max(0.0, self.requests[0] + window - at))
        return max(waits) if waits else 0.0

    def snapshot(self, at=None):
        at = time.monotonic() if at is None else at
        tokens, requests = self.window_usage(self.capacity['window'], at)
        return {'bucket': self.key, 'in_flight': self.in_flight, 'peak_in_flight': self.peak_in_flight,
                'queued': self.queued, 'capacity': self.capacity,
                'window_seconds': self.capacity['window'], 'window_tokens': tokens, 'window_requests': requests,
                'retries': self.retries, 'throttled': self.throttled, 'total_wait_seconds': round(self.total_wait, 2)}


class RateLimiter:
    """所有真实模型调用都要经过的闸门。进程内单实例，挂在 Models 上。"""

    def __init__(self, store=None):
        self.store = store
        self.buckets = {}

    def capacity_for(self, config, provider=None):
        """合并 provider 级默认与模型级覆盖。

        额度声明在 provider 记录上（一把 Key 一份额度）最自然，模型记录可覆盖个别模型。
        模型记录里没有 provider 的字段，所以这里按 `provider_id` 回查一次。
        """
        provider = provider if provider is not None else self._provider_of(config)
        merged = dict(config)
        if isinstance(provider, dict):
            if isinstance(provider.get('capacity'), dict):
                merged['provider_capacity'] = provider['capacity']
            if provider.get('tiers'):
                merged['provider_tiers'] = provider['tiers']
        return capacity_of(merged)

    def _provider_of(self, config):
        if self.store is None or not config.get('provider_id'):
            return None
        try:
            return self.store.get('provider', config['provider_id'])
        except ValueError:
            return None

    def bucket(self, config):
        key = bucket_key(config)
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = _Bucket(key, self.capacity_for(config))
            self.buckets[key] = bucket
        else:
            # 额度可以在运行中调整（人改配置），每次取用时刷新。
            bucket.capacity = self.capacity_for(config)
        return bucket

    async def acquire(self, config, estimated_tokens=0, deadline=None):
        """取一个调用许可：先等在途名额，再等窗口额度。返回 `_Bucket`。"""
        bucket = self.bucket(config)
        if bucket.semaphore.locked():
            bucket.queued += 1
        started = time.monotonic()
        timeout = bucket.capacity['queue_timeout']
        try:
            await asyncio.wait_for(bucket.semaphore.acquire(), timeout)
        except asyncio.TimeoutError:
            raise QueueTimeout(
                f'等待模型额度超时：{bucket.key} 排队超过 {timeout:.0f}s（并发上限 {bucket.capacity["concurrency"]}）') from None
        finally:
            # 无论拿到还是超时，都不再处于"排队中"。
            bucket.queued = max(0, bucket.queued - 1)
        bucket.in_flight += 1
        bucket.peak_in_flight = max(bucket.peak_in_flight, bucket.in_flight)
        bucket.total_wait += time.monotonic() - started
        # 窗口额度：满则等到窗口释放（同样受排队上限约束）。
        window = bucket.capacity['window']
        tpm, rpm = bucket.capacity['tpm'], bucket.capacity['rpm']
        waited = 0.0
        while True:
            pause = bucket.headroom_seconds(window, tpm, rpm, estimated_tokens)
            if pause <= 0:
                break
            if waited + pause > timeout:
                # 放弃这次调用：必须把在途计数与信号量一起还回去，
                # 否则这个桶会永久少一个并发名额（真机上表现为"越来越慢"）。
                bucket.in_flight = max(0, bucket.in_flight - 1)
                bucket.semaphore.release()
                raise QueueTimeout(f'等待模型额度窗口超时：{bucket.key} 需要再等 {pause:.0f}s（tpm={tpm} rpm={rpm}）')
            bucket.throttled += 1
            await asyncio.sleep(min(pause, 5.0))
            waited += pause
        return bucket

    def release(self, bucket, tokens=0):
        bucket.in_flight = max(0, bucket.in_flight - 1)
        bucket.requests.append(time.monotonic())
        if tokens:
            bucket.add_tokens(tokens)
        bucket.semaphore.release()

    def record_retry(self, bucket):
        bucket.retries += 1

    @staticmethod
    def estimate_tokens(input_bytes):
        return max(1, int(input_bytes) // CHARS_PER_TOKEN)

    @staticmethod
    def tokens_from_usage(usage, fallback_bytes=0):
        """优先用响应里的真实用量；没有就按请求字节保守估算。"""
        usage = usage if isinstance(usage, dict) else {}
        total = usage.get('total_tokens')
        if isinstance(total, (int, float)) and total > 0:
            return int(total)
        prompt = usage.get('prompt_tokens') or usage.get('input_tokens')
        completion = usage.get('completion_tokens') or usage.get('output_tokens')
        if isinstance(prompt, (int, float)) or isinstance(completion, (int, float)):
            return int(prompt or 0) + int(completion or 0)
        return RateLimiter.estimate_tokens(fallback_bytes)

    def snapshot(self):
        return [bucket.snapshot() for bucket in self.buckets.values()]

    def record_usage(self, run_id, task_id, model_id, tokens, prompt_tokens=0, completion_tokens=0,
                     waited=0.0, retries=0):
        """用量落盘：让"预算"成为可查对象，而不只是活在事件里。"""
        if self.store is None:
            return None
        record = {'id': f'usage_{now():.6f}_{random.randrange(1 << 20):x}', 'run_id': run_id,
                  'task_id': task_id, 'model_id': model_id, 'tokens': int(tokens),
                  'prompt_tokens': int(prompt_tokens), 'completion_tokens': int(completion_tokens),
                  'waited_seconds': round(float(waited), 3), 'retries': int(retries), 'created': now()}
        self.store.put('model_usage', record)
        return record

    def usage_summary(self, run_id=None):
        """按运行/模型聚合用量：`GET /api/load` 与 `usage_report` 工具共用。"""
        if self.store is None:
            return {}
        totals = {'calls': 0, 'tokens': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
                  'waited_seconds': 0.0, 'retries': 0, 'by_model': {}}
        for record in self.store.all('model_usage'):
            if run_id is not None and record.get('run_id') != run_id:
                continue
            totals['calls'] += 1
            totals['tokens'] += record.get('tokens', 0)
            totals['prompt_tokens'] += record.get('prompt_tokens', 0)
            totals['completion_tokens'] += record.get('completion_tokens', 0)
            totals['waited_seconds'] += record.get('waited_seconds', 0)
            totals['retries'] += record.get('retries', 0)
            entry = totals['by_model'].setdefault(record.get('model_id') or '?',
                                                  {'calls': 0, 'tokens': 0})
            entry['calls'] += 1
            entry['tokens'] += record.get('tokens', 0)
        totals['waited_seconds'] = round(totals['waited_seconds'], 2)
        return totals


def backoff_seconds(attempt, retry_after=None):
    """指数退避 + 抖动；`Retry-After` 优先，但不超过上限。

    抖动是必须的：没有它，同时被限流的多个任务会在同一时刻一起重试，再次撞满额度。
    """
    if retry_after:
        try:
            return min(float(retry_after), RETRY_AFTER_CAP)
        except (TypeError, ValueError):
            pass
    base = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(0, attempt)))
    return base * (0.5 + random.random() / 2)


def retry_after_of(error):
    """从错误里取 `Retry-After`（秒）：网关限流时通常会带。"""
    value = getattr(error, 'retry_after', None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

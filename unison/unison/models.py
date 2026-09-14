from __future__ import annotations
import asyncio
import json
import os
import re
import urllib.request
import urllib.error
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse
from .store import dumps, now
from .limiter import (MAX_MODEL_RETRIES, RateLimiter, QueueTimeout, RETRYABLE_CODES, backoff_seconds,
                      retry_after_of)
from . import providers


class ModelError(RuntimeError, ValueError):
    """统一模型调用错误：消费方按 code 路由，不解析文案。

    同时继承 RuntimeError 与 ValueError，使既有捕获这两种异常的调用方无需修改；
    新增代码应改用 `error.code`。
    """
    def __init__(self, message, code='UNKNOWN', status=None, retry_after=None):
        super().__init__(message)
        self.code = code
        self.status = status
        # 网关限流时常带 Retry-After：退避重试优先听它，而不是猜一个指数。
        self.retry_after = retry_after


_HTTP_CODES = {
    400: 'INVALID_REQUEST', 401: 'AUTH', 402: 'QUOTA', 403: 'AUTH', 404: 'MODEL_NOT_FOUND',
    408: 'TIMEOUT', 413: 'CONTEXT_WINDOW_EXCEEDED', 422: 'INVALID_REQUEST', 429: 'RATE_LIMIT',
    500: 'SERVER', 502: 'SERVER', 503: 'SERVER', 504: 'TIMEOUT',
    # 反代/网关的等待超时：Cloudflare 520/522/524 表示源站排队太久被切断。
    # 归为可重试的上游超时；否则会作为未分类的 `HTTP_524` 直接杀死任务——
    # 这正是 gpt-5.6-sol 双并发那次的第二个错误码。
    520: 'UPSTREAM_TIMEOUT', 522: 'UPSTREAM_TIMEOUT', 524: 'UPSTREAM_TIMEOUT',
}


def _http_code(status, body=''):
    """HTTP 状态码 + 响应体 → 内部错误码。

    少数情况只有看原文才能分清（实测）：有些模型是被**账户类型**拒绝的，
    状态码同为 400，但重试无用、必须换模型——那是永久问题而不是参数问题。
    """
    code = _HTTP_CODES.get(status, f'HTTP_{status}')
    text = str(body or '').lower()
    if code == 'INVALID_REQUEST' and ('not supported' in text
                                     or 'unsupported_model' in text
                                     or 'model_not_supported' in text):
        return 'MODEL_NOT_SUPPORTED'
    return code


# 只在错误提示里保留上游原话，且绝不回显可能的凭据。
_SECRET_PATTERNS = (
    re.compile(r'sk-[A-Za-z0-9_\-]{8,}'),
    re.compile(r'(?i)bearer\s+[A-Za-z0-9._\-]{8,}'),
)


def _error_body(exc):
    """尽力读出 HTTP 错误响应体（限长，避免把大页面塞进日志）。"""
    try:
        raw = exc.read(8192)
    except Exception:
        return ''
    try:
        return raw.decode('utf-8', 'replace')
    except Exception:
        return ''


def _error_detail(body):
    """从错误响应体里提取一句可读原因，脱敏后拼成 '：<原因>'；取不到就返回空串。"""
    if not body:
        return ''
    detail = ''
    try:
        data = json.loads(body)
    except ValueError:
        detail = body
    else:
        if isinstance(data, dict):
            error = data.get('error')
            if isinstance(error, dict):
                detail = str(error.get('message') or error.get('type') or '')
            elif isinstance(error, str):
                detail = error
            detail = detail or str(data.get('message') or data.get('detail') or '')
    detail = ' '.join(detail.split())[:300]
    for pattern in _SECRET_PATTERNS:
        detail = pattern.sub('[已脱敏]', detail)
    return ('：' + detail) if detail else ''


# ------------------------------------------------------------------ 模型健康
# 可用性维护的核心思想：**真实调用本身就是最强的可用性证据**，因此它的结论必须回写到目录，
# 而主动探测只是"没有近期真实证据时"的补充手段。历史上只有人工点验证会写结论，且结论没有
# 时间维度——一次 400 会让模型永久显示"不可用"，即使它随后被真实调用成功过。

HEALTH_OK = 'ok'
HEALTH_BLOCKED = 'blocked'
HEALTH_UNKNOWN = 'unknown'

# 每个码的处置：permanent=需要人改配置（重试无用）；transient=过一会儿自己会好；
# neither=与"这段时间能否调用"无关（例如单次请求太大），只记错误不动状态。
_VERDICTS = {
    'AUTH': ('permanent', 6 * 3600, '更新 API Key，或确认 key_env 指向的环境变量已设置'),
    'MISSING_CREDENTIAL': ('permanent', 6 * 3600, '为该模型/provider 配置 API Key，或设置 key_env 环境变量'),
    'NO_ADAPTER': ('permanent', 24 * 3600, '在 providers.py 登记该 provider 的 wire api'),
    'MODEL_NOT_FOUND': ('permanent', 24 * 3600, '该模型在此 Key/账户下不存在：确认模型 ID，或从目录声明中移除'),
    # 账户级限制：模型 ID 存在（不在目录里也会返回 400 而不是 404），
    # 但该账户类型不允许调用。上游原话（实测 gpt-5.4/gpt-5.2）：
    # "The 'gpt-5.4' model is not supported when using Codex with a ChatGPT account."
    # 这是**永久**限制，重试无用，必须由人换模型或换账户——原先归为 'neither'，
    # 于是它既不算可用也不算不可用，容易被误当成"参数没调好"反复重试。
    'MODEL_NOT_SUPPORTED': ('permanent', 24 * 3600, '该模型不被当前账户类型支持（例如 Codex + ChatGPT 账户）；换模型或换账户，重试无用'),
    'INVALID_REQUEST': ('neither', 6 * 3600, '确认模型 ID 与请求参数（有些模型需要特定 max_output）'),
    'CONTEXT_WINDOW_EXCEEDED': ('neither', 6 * 3600, '提高 max_output 或使用更大窗口的模型'),
    'EMPTY_RESPONSE': ('neither', 15 * 60, '重试；若持续发生则换模型'),
    'MALFORMED_RESPONSE': ('transient', 30 * 60, '重试；可能是网关瞬时异常'),
    'QUOTA': ('transient', 15 * 60, '账户额度或分组限制；充值或换模型后重试'),
    'RATE_LIMIT': ('transient', 15 * 60, '等待限流窗口结束'),
    'SERVER': ('transient', 30 * 60, '上游 5xx；稍后重试'),
    'UPSTREAM_TIMEOUT': ('transient', 15 * 60, '网关等待上游超时（常见于并发/额度打满）；退避后重试'),
    'TIMEOUT': ('transient', 30 * 60, '上游超时；稍后重试或提高 timeout'),
    'TRANSPORT': ('transient', 30 * 60, '检查网络与 base_url 是否可达'),
    'UNKNOWN': ('neither', 30 * 60, '查看原始错误；必要时重新验证'),
}
HEALTH_OK_TTL = 24 * 3600          # 真实调用成功后 24 小时内不再探测
HEALTH_TRANSIENT_LIMIT = 3         # 连续瞬时失败到这个次数才降级为 blocked
_LEGACY_TTL = 3600
_LEGACY_STALE_ERROR = 6 * 3600   # 旧记录里"参数/权限类"错误多久后视为需要人处理


def health_verdict(code):
    return _VERDICTS.get(str(code or 'UNKNOWN'), _VERDICTS['UNKNOWN'])


def _legacy_health(record):
    """把升级前的 `reachability` 投影为 health：**视为已过期**，因此会被重新验证。"""
    reach = record.get('reachability')
    if not isinstance(reach, dict) or not reach:
        return None
    checked = float(reach.get('checked_at') or 0)
    code = reach.get('code')
    kind = health_verdict(code)[0]
    # 旧记录没有"重试是否有用"的标注，只能按新鲜度保守推断：新鲜的 INVALID_REQUEST
    # （例如刚刚参数写错、恰好上游抖动）值得复验；陈旧的通常就是权限/模型不存在，
    # 重试同样无用，沿用过时 N 小时即视为需要人处理，避免用探测反复烧 token。
    if reach.get('reachable'):
        state = HEALTH_OK
    elif kind == 'permanent' or (kind == 'neither' and _LEGACY_STALE_ERROR > 0
                                 and now() - checked >= _LEGACY_STALE_ERROR):
        state = HEALTH_BLOCKED
    else:
        # 旧记录里的瞬时失败不应当被当成"可用"或"永久不可用"，只能表示"需要复验"。
        state = HEALTH_UNKNOWN
    return {'state': state,
            'code': code, 'reason': reach.get('error') or '', 'remedy': health_verdict(code)[2] if code else '',
            'source': 'probe', 'checked_at': checked, 'expires_at': checked or _LEGACY_TTL,
            'failures': 0 if reach.get('reachable') else 1,
            'history': [{'at': checked, 'code': code, 'source': 'probe'}],
            'legacy': True}


class _WireApi:
    """一种 wire api 的编解码；调用传输由 Models 统一负责。"""
    api = ''
    path = ''

    def url(self, base_url):
        base = str(base_url or '').strip().rstrip('/')
        if not base:
            raise ModelError('provider 缺少 API 地址', 'INVALID_REQUEST')
        return base if base.endswith(self.path) else base + self.path

    def payload(self, config, messages, tools):
        raise NotImplementedError

    def parse(self, body):
        raise NotImplementedError


class OpenAiCompletions(_WireApi):
    api = 'openai-completions'
    path = '/chat/completions'

    def payload(self, config, messages, tools):
        payload = dict(config.get('extra') or {})
        payload.update(model=config['model'], messages=messages,
                       max_tokens=int(config.get('max_output', 4096)), stream=False)
        if tools:
            payload.update(tools=tools, tool_choice='auto')
        return payload

    def parse(self, body):
        try:
            choice = body['choices'][0]
        except (KeyError, IndexError, TypeError):
            raise ModelError('模型响应缺少 choices', 'MALFORMED_RESPONSE') from None
        message = choice.get('message') or {}
        if choice.get('finish_reason') == 'length':
            raise ModelError('模型输出达到长度上限；可提高 max_output 后继续', 'CONTEXT_WINDOW_EXCEEDED')
        if not message.get('content') and not message.get('tool_calls'):
            raise ModelError('模型返回空消息', 'EMPTY_RESPONSE')
        return message, body.get('usage') or {}


class OpenAiResponses(_WireApi):
    api = 'openai-responses'
    path = '/responses'

    def payload(self, config, messages, tools):
        input_items = []
        for message in messages:
            role = message.get('role', 'user')
            if role == 'tool':
                input_items.append({'type': 'function_call_output', 'call_id': message['tool_call_id'],
                                    'output': message.get('content', '')})
                continue
            content = message.get('content')
            if content is not None:
                input_items.append({'role': role, 'content': content})
            for call in message.get('tool_calls', []):
                function = call.get('function') or {}
                input_items.append({'type': 'function_call', 'call_id': call['id'], 'name': function.get('name', ''),
                                    'arguments': function.get('arguments', '{}')})
        response_tools = []
        for item in tools or []:
            function = item.get('function') or {}
            response_tools.append({'type': 'function', 'name': function.get('name'),
                                   'description': function.get('description', ''),
                                   'parameters': function.get('parameters', {'type': 'object', 'properties': {}}),
                                   'strict': False})
        payload = dict(config.get('extra') or {})
        payload.update(model=config['model'], input=input_items,
                       max_output_tokens=int(config.get('max_output', 4096)), stream=False,
                       store=not bool(config.get('disable_response_storage', False)))
        if response_tools:
            payload.update(tools=response_tools, tool_choice='auto')
        return payload

    def parse(self, body):
        if body.get('status') in {'failed', 'cancelled', 'incomplete'}:
            detail = body.get('error') or body.get('incomplete_details') or body['status']
            raise ModelError('Responses API 未完成：' + str(detail), 'INVALID_REQUEST')
        content = []
        calls = []
        for item in body.get('output') or []:
            if item.get('type') == 'function_call':
                calls.append({'id': item.get('call_id') or item.get('id'), 'type': 'function',
                              'function': {'name': item.get('name', ''), 'arguments': item.get('arguments', '{}')}})
            elif item.get('type') == 'message':
                for part in item.get('content') or []:
                    if part.get('type') in {'output_text', 'text'} and part.get('text'):
                        content.append(part['text'])
        text = '\n'.join(content) or body.get('output_text')
        if not text and not calls:
            raise ModelError('模型返回空消息', 'EMPTY_RESPONSE')
        message = {'role': 'assistant', 'content': text}
        if calls:
            message['tool_calls'] = calls
        return message, body.get('usage') or {}


WIRE_APIS = {api.api: api for api in (OpenAiCompletions(), OpenAiResponses())}


def is_event_stream(content_type):
    return 'text/event-stream' in str(content_type or '').lower()


def parse_event_stream(text):
    """把 Responses API 的 SSE 流折回一个「完整响应」对象。

    真机发现（同一 base_url、换 Key 之后）：请求里明明写着 `stream: false`，
    网关仍按 `text/event-stream` 返回，于是只认 JSON 的解析器直接报
    `MALFORMED_RESPONSE`——连本来可用的模型也一起失效。这不是模型的问题，
    是"响应可能以流式到达"这件事没有被处理。

    只取 `response.completed` 里那个完整响应；若网关没发该事件，则用
    `output_item.done` 的事件拼出 output 数组作为退路。
    """
    completed = None
    items = []
    for line in str(text or '').splitlines():
        line = line.strip()
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if not payload or payload == '[DONE]':
            continue
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if kind in {'response.completed', 'response.done', 'response.failed', 'response.incomplete'}:
            response = event.get('response')
            if isinstance(response, dict) and completed is None:
                completed = response
        elif kind in {'response.output_item.done', 'response.output_item.added'}:
            item = event.get('item')
            if isinstance(item, dict) and kind.endswith('.done'):
                items.append(item)
    if completed is None:
        if not items:
            raise ModelError('流式响应里没有完整响应事件', 'MALFORMED_RESPONSE')
        completed = {'status': 'completed', 'output': items}
    elif not completed.get('output') and items:
        completed = dict(completed) | {'output': items}
    return completed


LLM_STATS_URL = 'https://llm-stats.com/'
BENCHMARK_TTL = 24 * 60 * 60
# 稳定的客户端标识：部分网关拒绝库默认 UA。
USER_AGENT = 'Unison/0.1 (+local console)'
_SCORE_FIELDS = {'overall': 'overall', 'llmstats': 'overall', 'reasoning': 'reasoning', 'coding': 'coding', 'agent': 'agent', 'agents': 'agent', 'agentic': 'agent'}


def _normalized_model_id(value):
    """Conservative identifier normalization; deliberately keeps vendor prefixes."""
    value = unquote(str(value or '')).strip().lower()
    value = re.sub(r'^models/', '', value)
    value = re.sub(r'[^a-z0-9._:/+-]+', '-', value)
    return value.strip('-/')


def _score(value):
    match = re.search(r'-?\d+(?:\.\d+)?', str(value or '').replace(',', ''))
    return float(match.group()) if match else None


class _LeaderboardParser(HTMLParser):
    """Small dependency-free parser for the public leaderboard's HTML tables."""
    def __init__(self):
        super().__init__()
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None
        self.link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            self.table = []
        elif tag == 'tr' and self.table is not None:
            self.row = []
        elif tag in {'th', 'td'} and self.row is not None:
            self.cell = {'text': [], 'href': ''}
        elif tag == 'a' and self.cell is not None:
            self.cell['href'] = attrs.get('href', '')

    def handle_data(self, data):
        if self.cell is not None:
            self.cell['text'].append(data)

    def handle_endtag(self, tag):
        if tag in {'th', 'td'} and self.cell is not None:
            self.cell['text'] = ' '.join(''.join(self.cell['text']).split())
            self.row.append(self.cell)
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            if self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == 'table' and self.table is not None:
            if self.table:
                self.tables.append(self.table)
            self.table = None


def _parse_leaderboard(html, source_url):
    parser = _LeaderboardParser()
    parser.feed(html)
    results = []
    for table in parser.tables:
        if len(table) < 2:
            continue
        headers = [re.sub(r'[^a-z]+', '', c['text'].lower()) for c in table[0]]
        model_index = next((i for i, h in enumerate(headers) if h in {'model', 'modelname', 'name'}), None)
        if model_index is None:
            continue
        indexes = {}
        for index, header in enumerate(headers):
            for needle, field in _SCORE_FIELDS.items():
                if needle in header:
                    indexes.setdefault(field, index)
        org_index = next((i for i, h in enumerate(headers) if h in {'org', 'organization', 'provider', 'company'}), None)
        for row in table[1:]:
            if model_index >= len(row):
                continue
            cell = row[model_index]
            name = cell['text'].strip()
            if not name:
                continue
            href = cell.get('href') or ''
            path_parts = [unquote(x) for x in urlparse(href).path.split('/') if x]
            slug = path_parts[-1] if path_parts else name
            item = {'slug': slug, 'name': name, 'org': row[org_index]['text'] if org_index is not None and org_index < len(row) else '',
                    'url': urljoin(source_url, href) if href else source_url}
            for field, index in indexes.items():
                if index < len(row):
                    value = _score(row[index]['text'])
                    if value is not None:
                        item[field] = value
            if any(field in item for field in _SCORE_FIELDS.values()):
                results.append(item)
    return results


class Models:
    """Replaceable model adapter. No routing heuristics or price database."""
    def __init__(self, store):
        self.store = store
        self.adapters = {'chat-completions': self.chat, 'responses': self.responses}
        # 物理额度闸门：所有真实调用都经过它，避免同桶并发把上游打爆。
        self.limiter = RateLimiter(store)

    def list(self):
        result = []
        for m in self.store.all('model'):
            api = providers.api_of(m)
            result.append({k: v for k, v in m.items() if k != 'api_key'} | {
                'api': api or m.get('api', ''),
                'adapter': providers.adapter_of(m),
                'provider_name': m.get('provider_name') or m.get('provider_id') or '内置',
                'health': self.health_of(m),
                'tier': self.tiers_of(m),
                'capacity': self.limiter.capacity_for(m),
                'configured': bool(m.get('api_key') or os.environ.get(m.get('key_env', '')) or m.get('no_key'))})
        return result

    def providers(self):
        """Console-facing provider directory: DSH-shaped providers plus their models."""
        return providers.list_providers(self.store, self)

    # ---------------------------------------------------------------- 健康状态

    def health_of(self, record, at=None):
        """读取模型的健康状态；没有新字段时从旧 `reachability` 投影，并标出是否已过期。"""
        at = now() if at is None else at
        health = record.get('health') if isinstance(record.get('health'), dict) else None
        if not health:
            health = _legacy_health(record)
        if not health:
            return {'state': HEALTH_UNKNOWN, 'code': None, 'reason': '', 'remedy': '',
                    'source': None, 'checked_at': None, 'expires_at': None, 'failures': 0,
                    'history': [], 'stale': True, 'legacy': False}
        result = dict(health)
        expires = result.get('expires_at')
        result['stale'] = bool(expires is None or at >= float(expires))
        # 兼容旧控制台：继续提供 reachability 形状。
        result['reachable'] = result.get('state') == HEALTH_OK
        return result

    def health_view(self):
        """模型健康只读视图：给控制台与外部程序看"哪些需要复验、为什么、证据来自哪"。"""
        items = []
        for model in self.store.all('model'):
            health = self.health_of(model)
            items.append({'id': model['id'], 'provider_id': model.get('provider_id'),
                          'label': model.get('label') or model['id'], **health,
                          'needs_probe': health['stale'] and health['state'] != HEALTH_BLOCKED})
        items.sort(key=lambda item: (item['state'] == HEALTH_OK, not item['stale'], item['id']))
        return items

    def _write_health(self, model_id, health):
        """只更新健康字段：先读回当前记录，避免用调用开始时的快照覆盖别人的改动。"""
        try:
            fresh = self.store.get('model', model_id)
        except ValueError:
            return None
        fresh['health'] = health
        # 旧字段同步维护，控制台与旧数据升级路径都依赖它。
        fresh['reachability'] = {'reachable': health['state'] == HEALTH_OK, 'checked_at': health['checked_at'],
                                 'error': health.get('reason') or None, 'code': health.get('code'),
                                 'source': health.get('source')}
        self.store.put('model', fresh)
        return fresh

    def record_success(self, config, source='call'):
        """真实调用成功 = 最强的可用性证据；顺带清掉历史的瞬时失败计数。"""
        model_id = config.get('id')
        if not model_id:
            return None
        previous = self.health_of(self.store.get('model', model_id) if self._exists(model_id) else config)
        checked = now()
        health = {'state': HEALTH_OK, 'code': None, 'reason': '', 'remedy': '', 'source': source,
                  'checked_at': checked, 'expires_at': checked + HEALTH_OK_TTL, 'failures': 0,
                  'last_failure_at': None,
                  'history': (previous.get('history') or [])[-4:] + [{'at': checked, 'code': None, 'source': source}]}
        self._write_health(model_id, health)
        if previous.get('state') != HEALTH_OK:
            self._event('ModelHealthChanged', {'model': model_id, 'from': previous.get('state'),
                                               'to': HEALTH_OK, 'source': source})
        return health

    def record_failure(self, config, error, source='call'):
        """调用/探测失败：按错误码分流，绝不把瞬时故障当成永久不可用。

        `source='probe'` 时若已有"未过期的真实调用成功"证据，则**不降级状态**：
        一次最小探测的失败不足以推翻真实调用成功过的事实，只记分歧事件供人判断。
        """
        model_id = config.get('id')
        if not model_id:
            return None
        code = getattr(error, 'code', 'UNKNOWN')
        kind, ttl, remedy = health_verdict(code)
        current = self.health_of(self.store.get('model', model_id)) if self._exists(model_id) else \
            self.health_of(config)
        checked = now()
        history = (current.get('history') or [])[-4:] + [{'at': checked, 'code': code, 'source': source}]
        failures = int(current.get('failures') or 0) + 1
        strong_ok = current.get('state') == HEALTH_OK and current.get('source') == 'call' and not current.get('stale')
        claimed = source        # claimed = 当前结论的证据来源，可能是上一次的真实调用
        if source == 'probe' and strong_ok:
            state = HEALTH_OK
            # 一次最小探测失败不足以推翻"真实调用成功过"，只记分歧供人判断。
            claimed = 'call'
            self._event('ModelHealthDisagreement', {'model': model_id, 'evidence': 'call-ok',
                                                    'probe_code': code, 'reason': str(error)})
        elif kind == 'permanent':
            state = HEALTH_BLOCKED
        elif kind == 'transient':
            # 连续瞬时失败才降级；单次限流/超时不改变已有结论。
            if failures >= HEALTH_TRANSIENT_LIMIT:
                state = HEALTH_BLOCKED
            else:
                state = current.get('state', HEALTH_UNKNOWN)
                claimed = current.get('source') or source
        else:
            state = current.get('state', HEALTH_UNKNOWN)
            claimed = current.get('source') or source
        health = {'state': state, 'code': code, 'reason': str(error), 'remedy': remedy, 'source': claimed,
                  'checked_at': checked, 'expires_at': checked + ttl, 'failures': failures,
                  'last_failure_at': checked, 'history': history}
        self._write_health(model_id, health)
        if current.get('state') != state:
            self._event('ModelHealthChanged', {'model': model_id, 'from': current.get('state'),
                                               'to': state, 'code': code, 'source': source})
        return health

    def tiers_of(self, config):
        """模型的档位：优先看模型记录，其次看 provider 的 `tiers` 声明。缺省 standard。"""
        tier = str(config.get('tier') or '').strip()
        if tier:
            return tier
        provider_id = config.get('provider_id')
        if not provider_id:
            return 'standard'
        try:
            provider = self.store.get('provider', provider_id)
        except ValueError:
            return 'standard'
        tiers = provider.get('tiers') or {}
        for name, members in tiers.items():
            if config.get('model') in (members or []):
                return str(name)
        return 'standard'

    def downgrade_target(self, model_id):
        """子任务默认用的轻档模型：父为 heavy 时降一档（可选）。

        只在**有人明确声明** `tiers` 时才降级，且目标必须是存在、可用（未 blocked）的模型；
        否则原样返回——宁可不降级，也不要静默换成一个用不了的模型。
        """
        try:
            config = self.store.get('model', model_id)
        except ValueError:
            return None
        if self.tiers_of(config) != 'heavy':
            return None
        provider_id = config.get('provider_id')
        if not provider_id:
            return None
        try:
            provider = self.store.get('provider', provider_id)
        except ValueError:
            return None
        tiers = provider.get('tiers') or {}
        light = [x for x in (tiers.get('light') or []) if x]
        if not light:
            return None
        # 按目录顺序取第一个"存在且未被判定为需要人处理"的轻档模型。
        for name in light:
            candidate = f'{provider_id}:{name}'
            if not self._exists(candidate):
                continue
            if self.health_of(self.store.get('model', candidate))['state'] == HEALTH_BLOCKED:
                continue
            return candidate
        return None

    def _exists(self, model_id):
        return bool(self.store.db.execute('SELECT 1 FROM records WHERE kind=? AND id=?', ('model', model_id)).fetchone())

    def _event(self, type, payload):
        try:
            self.store.event(type, payload)
        except Exception:      # 事件失败不能影响调用本身
            pass

    def _stamp_source(self, model_id, source, only_if_state_unchanged=False):
        """只改证据来源，不动状态、计数与时间：探测与真实调用写的是同一份结论。

        `only_if_state_unchanged=True` 用于探测失败的分支：若失败没有改变状态
        （即结论仍来自更早的真实调用），就不能把来源标成 probe。
        """
        if not self._exists(model_id):
            return None
        record = self.store.get('model', model_id)
        health = self.health_of(record)
        if not health.get('checked_at'):
            return None
        if only_if_state_unchanged and health.get('source') != 'probe':
            return None
        health = {k: v for k, v in health.items() if k not in {'stale', 'reachable'}}
        health['source'] = source
        history = list(health.get('history') or [])
        if history:
            history[-1] = {**history[-1], 'source': source}
        health['history'] = history[-5:]
        return self._write_health(model_id, health)

    def clear_blocked(self, model_id=None, codes=None):
        """配置变更（改 Key、重新导入、重新扫描）后清掉需要人处理的 blocked。

        这正是"不靠重试修复永久错误"的配套动作：人修了配置，标记必须立刻失效。
        """
        wanted = set(codes or ('AUTH', 'MISSING_CREDENTIAL', 'NO_ADAPTER'))
        changed = []
        for model in self.store.all('model'):
            if model_id is not None and model['id'] != model_id:
                continue
            health = self.health_of(model)
            if health['state'] != HEALTH_BLOCKED or health.get('code') not in wanted:
                continue
            cleared = {**health, 'state': HEALTH_UNKNOWN, 'remedy': '', 'source': 'config-change',
                       'checked_at': now(), 'expires_at': 0,
                       'history': (health.get('history') or [])[-4:] + [{'at': now(), 'code': None,
                                                                        'source': 'config-change'}]}
            self._write_health(model['id'], cleared)
            changed.append(model['id'])
            self._event('ModelHealthCleared', {'model': model['id'], 'was': health.get('code')})
        return changed

    @staticmethod
    def _models_url(base_url):
        base = str(base_url or '').strip().rstrip('/')
        if not base:
            raise ValueError('请填写 provider API 地址')
        return base if base.endswith('/models') else base + '/models'

    @staticmethod
    def _extract_model_ids(body):
        items = body
        if isinstance(body, dict):
            for key in ('data', 'models', 'list'):
                if key in body:
                    items = body[key]
                    break
        if isinstance(items, dict):
            items = [{'id': key} | (value if isinstance(value, dict) else {}) for key, value in items.items()]
        if not isinstance(items, list):
            raise ValueError('模型列表响应必须包含 data/models/list 数组')
        ids = []
        for item in items:
            value = item.get('id') if isinstance(item, dict) else item
            value = str(value or '').strip()
            if value and value not in ids:
                ids.append(value)
        return ids

    def discover(self, provider):
        """Discover an OpenAI-compatible provider and upsert every advertised model."""
        base_url = str(provider.get('base_url') or '').strip().rstrip('/')
        api_key = str(provider.get('api_key') or '')
        provider_id = str(provider.get('provider_id') or provider.get('id') or urlparse(base_url).netloc or 'provider').strip()
        provider_name = str(provider.get('provider_name') or provider.get('name') or provider_id)
        api = providers.api_of(provider) or 'openai-completions'
        if providers.API_ADAPTERS[api] not in self.adapters:
            raise ValueError(f"provider {provider_id!r} 的 api {provider.get('api') or provider.get('adapter')!r} 不受支持")
        configured_headers = provider.get('headers') or {}
        if not isinstance(configured_headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in configured_headers.items()):
            raise ValueError('Provider headers 必须是字符串映射')
        headers = dict(configured_headers)
        headers.setdefault('Accept', 'application/json')
        # 部分网关（含 aaa.bi）会对 Python-urllib 默认 UA 直接返回 403。
        headers.setdefault('User-Agent', USER_AGENT)
        if api_key:
            headers['Authorization'] = 'Bearer ' + api_key
        request = urllib.request.Request(self._models_url(base_url), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'模型发现 HTTP {exc.code}: {exc.reason}') from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise RuntimeError(f'模型发现失败：{exc}') from None
        model_ids = self._extract_model_ids(body)
        if not model_ids:
            raise RuntimeError('模型发现成功，但 provider 未返回任何模型 ID')
        # 已声明/上轮已知的模型优先，其后追加 /models 新发现的；扫描绝不删除已声明模型。
        try:
            previous = self.store.get('provider', provider_id)
        except ValueError:
            previous = {}
        catalog = provider.get('catalog') or {}
        ordered = []
        for entry in list(previous.get('models') or []) + list(provider.get('models') or []):
            value = str(entry.get('id') or '').strip() if isinstance(entry, dict) else ''
            if value and value not in ordered:
                ordered.append(value)
        for model_id in model_ids:
            if model_id not in ordered:
                ordered.append(model_id)
        entries = []
        for model_id in ordered:
            try:
                existing = self.store.get('model', f'{provider_id}:{model_id}')
            except ValueError:
                existing = None
            declared = next((e for e in (previous.get('models') or []) if e.get('id') == model_id), {})
            facts = providers.catalog_metadata(catalog.get(model_id) or {})
            entries.append(dict(facts) | {
                'id': model_id,
                'name': facts.get('name') or declared.get('name') or model_id,
                'contextWindow': (existing or {}).get('context_window') or facts.get('contextWindow')
                                 or declared.get('contextWindow') or provider.get('defaultContextWindow') or 32768,
                'maxTokens': (existing or {}).get('max_output') or facts.get('maxTokens')
                             or declared.get('maxTokens') or provider.get('defaultMaxTokens') or 4096,
                'description': facts.get('description') or declared.get('description')
                               or f'由 {provider_name} /models 自动发现',
                'advertised': model_id in model_ids,
            })
        # 默认模型优先级：显式声明 > 已有 provider 记录 > /models 第一项。
        # 扫描只负责补齐模型，不能改写用户声明的默认模型。
        discovered_ids = {entry['id'] for entry in entries}
        default_model = str(provider.get('default_model') or previous.get('default_model') or '')
        if default_model not in discovered_ids:
            default_model = ''
        saved = providers.save_provider(self.store, self, {
            'id': provider_id, 'name': provider_name, 'base_url': base_url, 'api_key': api_key,
            'key_env': provider.get('key_env', ''), 'no_key': bool(provider.get('no_key', False)),
            'api': providers.api_of(provider), 'adapter': provider.get('adapter') or 'chat-completions',
            'headers': configured_headers, 'models': entries, 'default_model': default_model,
            'source_format': provider.get('source_format') or previous.get('source_format', ''),
            'requires_openai_auth': provider.get('requires_openai_auth', not bool(provider.get('no_key'))),
            'disable_response_storage': bool(provider.get('disable_response_storage', False)),
        })
        # 免费旁证：这次扫描把某个模型列为"已被广告"，则此前基于"未广告/不存在"的
        # blocked 结论可能已经过期，清成待复验（不需要为它花一次探测）。
        newly_advertised = [f"{provider_id}:{entry['id']}" for entry in saved['models']
                            if entry.get('advertised') and entry['id'] in model_ids]
        cleared = self.clear_blocked(codes=('MODEL_NOT_FOUND', 'INVALID_REQUEST'))
        cleared = [model for model in cleared if model in newly_advertised]
        records = [self.store.get('model', f"{provider_id}:{entry['id']}") for entry in saved['models']]
        self.apply_cached_benchmarks()
        return {'provider_id': provider_id, 'models': [{k: v for k, v in record.items() if k != 'api_key'} for record in records],
                'discovered': len(records), 'discovery_error': None, 'health_cleared': cleared}

    def _benchmark_match(self, model, entries):
        wanted = _normalized_model_id(model.get('model'))
        if not wanted:
            return None
        exact = [entry for entry in entries if wanted in {_normalized_model_id(entry.get('slug')), _normalized_model_id(entry.get('name'))}]
        if len(exact) == 1:
            return exact[0]
        # A provider-prefixed wire ID may safely match an unprefixed leaderboard slug only
        # when that leaf alias occurs exactly once in the downloaded leaderboard.
        leaf = wanted.rsplit('/', 1)[-1]
        if leaf == wanted:
            return None
        aliases = [entry for entry in entries if leaf in {_normalized_model_id(entry.get('slug')).rsplit('/', 1)[-1],
                                                          _normalized_model_id(entry.get('name')).rsplit('/', 1)[-1]}]
        return aliases[0] if len(aliases) == 1 else None

    def apply_cached_benchmarks(self, entries=None, matched_at=None):
        if entries is None:
            try:
                cache = self.store.get('benchmark_cache', 'llm-stats')
                entries = cache.get('entries') or []
                matched_at = cache.get('fetched_at')
            except ValueError:
                return 0
        count = 0
        for model in self.store.all('model'):
            entry = self._benchmark_match(model, entries)
            if not entry:
                continue
            scores = {field: entry[field] for field in {'overall', 'reasoning', 'coding', 'agent'} if field in entry}
            model['benchmark'] = scores | {
                'model_slug': entry.get('slug'), 'model_name': entry.get('name'), 'org': entry.get('org', ''),
                'source': 'LLM Stats', 'url': entry.get('url') or LLM_STATS_URL, 'attribution_url': LLM_STATS_URL,
                'matched_at': matched_at or now(),
            }
            self.store.put('model', model)
            count += 1
        return count

    def refresh_benchmarks(self, source_url=LLM_STATS_URL, force=False):
        try:
            cache = self.store.get('benchmark_cache', 'llm-stats')
        except ValueError:
            cache = None
        current = now()
        if cache and not force and current - float(cache.get('fetched_at', 0)) < BENCHMARK_TTL:
            matched = self.apply_cached_benchmarks(cache.get('entries') or [], cache.get('fetched_at'))
            return {'source': 'LLM Stats', 'url': source_url, 'cached': True, 'fetched_at': cache['fetched_at'],
                    'entries': len(cache.get('entries') or []), 'matched': matched, 'refresh_error': None}
        request = urllib.request.Request(source_url, headers={'Accept': 'text/html', 'User-Agent': 'Unison/0.1 (+local display)'} )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                html = response.read().decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
            entries = _parse_leaderboard(html, source_url)
            if not entries:
                raise RuntimeError('公开页面中未找到可识别的排行榜表格')
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            # Existing scores remain available; a failed refresh never disables models.
            return {'source': 'LLM Stats', 'url': source_url, 'cached': bool(cache),
                    'fetched_at': cache.get('fetched_at') if cache else None,
                    'entries': len(cache.get('entries') or []) if cache else 0, 'matched': 0,
                    'refresh_error': f'LLM Stats 抓取失败：{exc}'}
        cache = {'id': 'llm-stats', 'source': 'LLM Stats', 'url': source_url, 'fetched_at': current, 'entries': entries}
        self.store.put('benchmark_cache', cache)
        matched = self.apply_cached_benchmarks(entries, current)
        return {'source': 'LLM Stats', 'url': source_url, 'cached': False, 'fetched_at': current,
                'entries': len(entries), 'matched': matched, 'refresh_error': None}

    async def verify(self, model_id, timeout=90):
        """主动探测：一次最小请求。**每个模型消耗一次真实调用**，因此只该在缺少近期证据时用。

        探测结论写入 `source='probe'`；如果已有"未过期的真实调用成功"证据，失败不会推翻它
        （只记 `ModelHealthDisagreement`）——一次最小请求的失败不足以否定真实调用成功过的事实。
        """
        try:
            config = self.store.get('model', model_id)
        except ValueError:
            raise ModelError(f'未知模型：{model_id}', 'UNKNOWN_MODEL') from None
        api = providers.api_of(config)
        probe = [{'role': 'user', 'content': 'ping'}]
        try:
            # 记账统一由 invoke 负责，来源标成 probe；探测失败不得推翻真实调用的成功证据。
            await self.invoke(api, config, probe, None, timeout=timeout, record_source='probe')
        except Exception:
            # 探测失败但状态仍来自先前的真实调用（`record_failure` 保留了它）时，
            # 证据来源仍然是那次调用，不能改标成 probe。
            self._stamp_source(model_id, 'probe', only_if_state_unchanged=True)
            raise
        self._stamp_source(model_id, 'probe')
        return self.health_of(self.store.get('model', model_id))

    async def verify_provider(self, provider_id, model_ids=None, timeout=90, force=False, limit=None):
        """按需探测 provider 的模型：默认**只打过期或未验证的**，不烧 token 重复验证已知可用的模型。

        - `force=True`：显式全量验证（人明确要求时）。
        - `limit`：本次最多探测几个，按"最久未验证"优先，避免一次点下去打十几个模型。
        返回里带 `skipped`，让控制台能说明"跳过了 N 个已有近期证据的模型"。
        """
        provider = self.store.get('provider', provider_id)
        entries = provider.get('models') or []
        wanted = list(model_ids) if model_ids else [entry['id'] for entry in entries]
        candidates = []
        skipped = []
        for model_key in wanted:
            full_id = model_key if ':' in str(model_key) else f'{provider_id}:{model_key}'
            if not self._exists(full_id):
                continue
            health = self.health_of(self.store.get('model', full_id))
            # 永久错误（凭据/权限/参数）重试无用，必须由人改配置后再验证。
            needs = force or (health['stale'] and health['state'] != HEALTH_BLOCKED)
            (candidates if needs else skipped).append((full_id, health))
        # 最久未验证的优先；从未验证过的排最前。
        candidates.sort(key=lambda pair: pair[1].get('checked_at') or 0)
        if limit is not None:
            skipped.extend(candidates[int(limit):])
            candidates = candidates[:int(limit)]
        results = {}
        for full_id, _health in candidates:
            key = full_id.split(':', 1)[1] if full_id.startswith(provider_id + ':') else full_id
            results[key] = await self.verify(full_id, timeout=timeout)
        reachable = [key for key, item in results.items() if item.get('state') == HEALTH_OK]
        # 把结论写回 provider 条目，控制台无需再读扁平记录即可展示可用性。
        for entry in entries:
            item = results.get(entry['id'])
            if item:
                entry['reachability'] = {'reachable': item.get('state') == HEALTH_OK,
                                         'checked_at': item.get('checked_at'), 'error': item.get('reason') or None,
                                         'code': item.get('code'), 'source': item.get('source')}
        self.store.put('provider', provider)
        return {'provider_id': provider_id, 'checked': len(results), 'reachable': reachable,
                'skipped': [{'model': full_id, 'state': health.get('state'), 'source': health.get('source'),
                             'checked_at': health.get('checked_at')} for full_id, health in skipped],
                'results': results, 'force': bool(force)}

    def save(self, data):
        id = data.get('id','').strip()
        if not id or not data.get('model') or not data.get('base_url'):
            raise ValueError('请填写名称、模型 ID 和 API 地址')
        try:
            old = self.store.get('model',id)
        except ValueError:
            old = {}
        fields = ('id','label','model','base_url','key_env','api_key','no_key','context_window','max_output','adapter','api','extra','description','wire_api','provider_id','provider_name','requires_openai_auth','disable_response_storage','role','headers','default_model')
        m = old | {k:data[k] for k in fields if k in data}
        if not data.get('api_key') and old.get('api_key'):
            m['api_key'] = old['api_key']
        m.setdefault('adapter','chat-completions')
        m.setdefault('context_window',32768)
        m.setdefault('max_output',4096)
        if int(m['context_window']) < 2048 or not 128 <= int(m['max_output']) < int(m['context_window']):
            raise ValueError('输出空间必须小于上下文窗口，且至少 128 token')
        if m['adapter'] not in self.adapters:
            raise ValueError('Unknown adapter')
        # A record created without an explicit api inherits it from its adapter.
        m['api'] = providers.api_of(m) or str(m.get('api') or '')
        self.store.put('model',m)
        return self.list()

    async def call(self, model_id, messages, tools=None, run_id=None, task_id=None):
        """统一入口：model_id → provider api → 已注册 adapter。

        `run_id`/`task_id` 只用于把用量归因到具体运行与任务（`model_usage` 记录），
        不参与请求内容，因此不影响可重建性。
        """
        config = self.store.get('model',model_id)
        adapter = self.adapters.get(str(config.get('adapter') or ''))
        if adapter is None:
            api = providers.api_of(config)
            adapter = self.adapters.get(providers.API_ADAPTERS.get(api, ''))
        if adapter is None:
            raise ModelError(f"模型 {model_id} 没有可用的 provider api 或 adapter", 'NO_ADAPTER')
        # adapter 的公开契约仍是 `(config, messages, tools)`；归因参数只在本进程的
        # 内置实现上传递，插件与测试里的三参数 adapter 不会被多余的 kwargs 打断。
        accepts = getattr(adapter, '_unison_accepts_attribution', None)
        if accepts is None:
            import inspect
            try:
                parameters = inspect.signature(adapter).parameters
                accepts = ('run_id' in parameters) or any(
                    item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())
            except (TypeError, ValueError):
                accepts = False
            try:
                adapter._unison_accepts_attribution = accepts
            except AttributeError:
                pass
        if accepts:
            return await adapter(config,messages,tools,run_id=run_id,task_id=task_id)
        return await adapter(config,messages,tools)

    async def invoke(self, api, config, messages, tools=None, timeout=180, run_id=None, task_id=None,
                     record=True, record_source='call'):
        """唯一的传输路径：额度闸门 → 请求 → HTTP → 解析；失败统一为 ModelError。

        这里同时承担三件事（都属于"防过热"，而不是业务决策）：

        1. **过闸**：同 provider+模型 的在途请求不超过声明的并发；窗口 tokens/请求数超限就排队；
        2. **退避重试**：429/524/5xx/超时按指数退避重试（尊重 `Retry-After`），永久错误立即失败；
        3. **记账**：每次真实调用的用量落 `model_usage`，让用量成为可查对象。
        """
        wire = WIRE_APIS.get(api)
        if wire is None:
            raise ModelError(f'未登记的 wire api：{api}', 'NO_ADAPTER')
        key = config.get('api_key') or os.environ.get(config.get('key_env',''),'')
        if not key and not config.get('no_key'):
            raise ModelError(f"模型 {config.get('id')} 尚未设置 API Key（环境变量或 provider 配置）", 'MISSING_CREDENTIAL')
        url = wire.url(config.get('base_url'))
        payload = wire.payload(config, messages, tools)
        body_bytes = dumps(payload).encode()
        estimated = self.limiter.estimate_tokens(len(body_bytes))

        def request():
            headers = {'Accept': 'application/json', 'User-Agent': USER_AGENT}
            headers.update(config.get('headers') or {})
            headers['Content-Type'] = 'application/json'
            if key:
                headers['Authorization'] = 'Bearer '+key
            req = urllib.request.Request(url,body_bytes,headers)
            try:
                with urllib.request.urlopen(req,timeout=timeout) as response:
                    # 有些网关（实测 aaa.bi 的另一个账号组）即使收到 stream:false，
                    # 仍以 text/event-stream 返回；按 Content-Type 分流，而不是假设 JSON。
                    if is_event_stream(response.headers.get('Content-Type')):
                        body = parse_event_stream(response.read().decode('utf-8','replace'))
                    else:
                        body = json.load(response)
            except urllib.error.HTTPError as e:
                # 不整体持久化响应体（可能回显请求凭据），但**丢弃上游原话是有代价的**：
                # 实测 gpt-5.4 的真实原因是 "not supported when using Codex with a ChatGPT
                # account"，旧实现只留一句 "Model HTTP 400: Bad Request"，于是人和模型都
                # 无从判断该改参数还是该换模型。改为只提取 error.message 一类字段并脱敏。
                # 注意：响应体只能读一次，因此先读出来再决定错误码与提示。
                raw = _error_body(e)
                detail = _error_detail(raw)
                raise ModelError(f'Model HTTP {e.code}: {e.reason}{detail}',
                                 _http_code(e.code, raw), e.code,
                                 retry_after=(e.headers.get('Retry-After') if e.headers else None)) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise ModelError(f'模型连接失败：{e}', 'TRANSPORT') from None
            except ValueError:
                raise ModelError('模型返回了非 JSON 响应', 'MALFORMED_RESPONSE') from None
            return wire.parse(body)

        attempts = 0
        bucket = await self.limiter.acquire(config, estimated)
        waited = 0.0
        try:
            while True:
                attempts += 1
                try:
                    message, usage = await asyncio.to_thread(request)
                except ModelError as error:
                    if error.code in RETRYABLE_CODES and attempts <= MAX_MODEL_RETRIES:
                        pause = backoff_seconds(attempts - 1, retry_after_of(error))
                        self.limiter.record_retry(bucket)
                        self._event('ModelCallRetried', {'model': config.get('id'), 'code': error.code,
                                                         'attempt': attempts, 'pause_seconds': round(pause, 2)})
                        await asyncio.sleep(pause)
                        waited += pause
                        continue
                    # 只有重试耗尽才算一次失败事件：退避重试是同一次故障的多次尝试。
                    if record:
                        self.record_failure(config, error, source=record_source)
                    raise
                except Exception:
                    raise
                tokens = self.limiter.tokens_from_usage(usage, len(body_bytes))
                prompt_tokens = (usage or {}).get('prompt_tokens') or (usage or {}).get('input_tokens') or 0
                completion_tokens = (usage or {}).get('completion_tokens') or (usage or {}).get('output_tokens') or 0
                self.limiter.release(bucket, tokens)
                bucket = None
                # 真实调用成功是最强的可用性证据，顺带把 24 小时内的探测需求清掉。
                if record:
                    self.record_success(config, source=record_source)
                self.limiter.record_usage(run_id, task_id, config.get('id'), tokens,
                                          prompt_tokens, completion_tokens, waited=waited, retries=attempts - 1)
                return message, usage
        finally:
            if bucket is not None:
                self.limiter.release(bucket, 0)

    async def chat(self, config, messages, tools, run_id=None, task_id=None):
        return await self.invoke('openai-completions', config, messages, tools, run_id=run_id, task_id=task_id)

    async def responses(self, config, messages, tools, run_id=None, task_id=None):
        return await self.invoke('openai-responses', config, messages, tools, run_id=run_id, task_id=task_id)

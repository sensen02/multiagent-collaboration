"""Provider 目录：DSH 风格的 provider + models 单一真相源。

设计参考 DSH 的 `llm-pi-ai` / `llm-deepseek` 配置形状：

    provider = {
      id, displayName, api, baseURL, apiKeyEnv, headers,
      models: [ {id, name, description, contextWindow, maxTokens, inputModalities} ],
      defaultContextWindow, defaultMaxTokens
    }

Unison 在此基础上保留一层扁平 model 记录（运行时按 model_id 领取任务），
两者的转换只在本模块发生，避免各处重复解释配置。
"""

from __future__ import annotations

from .store import now

# DSH `api` 与运行时 adapter 的对应关系；新增一种 wire api 只需在这里登记。
API_ADAPTERS = {
    'openai-completions': 'chat-completions',
    'openai-responses': 'responses',
}
ADAPTER_APIS = {adapter: api for api, adapter in API_ADAPTERS.items()}

DEFAULT_CONTEXT_WINDOW = 262144
DEFAULT_MAX_OUTPUT = 32768
MIN_CONTEXT_WINDOW = 2048

_MODEL_FIELDS = ('id', 'name', 'description', 'contextWindow', 'maxTokens', 'inputModalities', 'role')

# 供应商 / Codex 目录条目里常见的字段别名，集中解释一次。
CATALOG_CONTEXT_KEYS = ('contextWindow', 'context_window', 'context_length', 'max_context_tokens', 'max_input_tokens')
CATALOG_OUTPUT_KEYS = ('maxTokens', 'max_output', 'max_output_tokens', 'max_completion_tokens')


def catalog_metadata(entry):
    """从 Codex/OpenAI 风格的目录条目提取 DSH 形状的模型事实。"""
    if not isinstance(entry, dict):
        return {}
    result = {}
    for keys, field in ((CATALOG_CONTEXT_KEYS, 'contextWindow'), (CATALOG_OUTPUT_KEYS, 'maxTokens')):
        for key in keys:
            value = entry.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                result[field] = value
                break
    name = entry.get('display_name') or entry.get('name') or entry.get('id') or entry.get('slug')
    if name:
        result['name'] = str(name)
    if entry.get('description'):
        result['description'] = str(entry['description'])
    # 目录里的 priority 越小越强，用于控制台排序与挑选默认模型。
    if isinstance(entry.get('priority'), int) and not isinstance(entry.get('priority'), bool):
        result['priority'] = entry['priority']
    modalities = entry.get('input_modalities') or entry.get('inputModalities')
    if isinstance(modalities, list):
        result['inputModalities'] = [str(item) for item in modalities if isinstance(item, (str, int, float))]
    return result


def api_of(record):
    """Return the DSH-style api name for a provider or flat model record."""
    api = str(record.get('api') or '').strip()
    if api in API_ADAPTERS:
        return api
    adapter = str(record.get('adapter') or '').strip()
    if adapter in ADAPTER_APIS:
        return ADAPTER_APIS[adapter]
    wire_api = str(record.get('wire_api') or '').strip().lower().replace('-', '_')
    if wire_api in {'responses', 'openai_responses'}:
        return 'openai-responses'
    if wire_api in {'chat_completions', 'chat', 'openai_completions'}:
        return 'openai-completions'
    return ''


def adapter_of(record):
    """Resolve the runtime adapter name, tolerating records without an api field."""
    api = api_of(record)
    if api:
        return API_ADAPTERS[api]
    return str(record.get('adapter') or 'chat-completions')


def _positive_int(value, default):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


def normalize_model(entry, defaults=None):
    """Normalize one DSH-style model entry; ids are required, everything else falls back."""
    defaults = defaults or {}
    model_id = str(entry.get('id') or '').strip()
    if not model_id:
        raise ValueError('模型条目缺少 id')
    context = _positive_int(entry.get('contextWindow'),
                            _positive_int(entry.get('context_window'), defaults.get('contextWindow', DEFAULT_CONTEXT_WINDOW)))
    output = _positive_int(entry.get('maxTokens'),
                           _positive_int(entry.get('max_output'), defaults.get('maxTokens', DEFAULT_MAX_OUTPUT)))
    if context < MIN_CONTEXT_WINDOW:
        context = MIN_CONTEXT_WINDOW
    if output >= context:
        output = max(128, context // 4)
    modalities = entry.get('inputModalities') or entry.get('input_modalities') or []
    if not isinstance(modalities, list):
        modalities = []
    normalized = {
        'id': model_id,
        'name': str(entry.get('name') or model_id),
        'description': str(entry.get('description') or ''),
        'contextWindow': context,
        'maxTokens': output,
        'inputModalities': [str(item) for item in modalities if isinstance(item, (str, int, float))],
        # 记账字段必须原样保留，否则控制台无法判断“供应商没广告”或“上次验证失败”。
        'advertised': entry.get('advertised', True),
    }
    if entry.get('role'):
        normalized['role'] = str(entry['role'])
    if isinstance(entry.get('priority'), int) and not isinstance(entry.get('priority'), bool):
        normalized['priority'] = entry['priority']
    if isinstance(entry.get('reachability'), dict):
        normalized['reachability'] = dict(entry['reachability'])
    return normalized


def provider_record(provider):
    """Validate and canonicalize a provider profile into the stored shape."""
    provider_id = str(provider.get('id') or '').strip()
    if not provider_id:
        raise ValueError('provider 缺少 id')
    base_url = str(provider.get('base_url') or provider.get('baseURL') or '').strip().rstrip('/')
    if not base_url:
        raise ValueError(f'provider {provider_id!r} 缺少 base_url')
    api = api_of(provider) or 'openai-completions'
    headers = provider.get('headers') or {}
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError(f'provider {provider_id!r} 的 headers 必须是字符串映射')
    models = [normalize_model(entry, {'contextWindow': provider.get('defaultContextWindow', DEFAULT_CONTEXT_WINDOW),
                                      'maxTokens': provider.get('defaultMaxTokens', DEFAULT_MAX_OUTPUT)})
              for entry in provider.get('models') or []]
    seen = set()
    for entry in models:
        if entry['id'] in seen:
            raise ValueError(f'provider {provider_id!r} 的模型 id {entry["id"]!r} 重复')
        seen.add(entry['id'])
    default_model = str(provider.get('default_model') or provider.get('model') or '').strip()
    if default_model and default_model not in seen:
        default_model = ''
    if not default_model and models:
        default_model = models[0]['id']
    return {
        'id': provider_id,
        'name': str(provider.get('name') or provider.get('displayName') or provider_id),
        'api': api,
        'adapter': API_ADAPTERS[api],
        'wire_api': provider.get('wire_api') or ('responses' if api == 'openai-responses' else 'chat_completions'),
        'base_url': base_url,
        'key_env': str(provider.get('key_env') or provider.get('apiKeyEnv') or ''),
        'api_key': str(provider.get('api_key') or ''),
        'no_key': bool(provider.get('no_key', not provider.get('apiKeyEnv'))),
        'requires_openai_auth': bool(provider.get('requires_openai_auth', bool(provider.get('apiKeyEnv')))),
        'headers': dict(headers),
        'disable_response_storage': bool(provider.get('disable_response_storage', False)),
        'default_model': default_model,
        'role': str(provider.get('role') or ''),
        'source_format': str(provider.get('source_format') or ''),
        'models': models,
        'updated': now(),
    }


def model_record(provider, entry):
    """Project one provider model entry into the flat runtime model record."""
    model_id = f"{provider['id']}:{entry['id']}"
    return {
        'id': model_id,
        'label': f"{provider['name']} · {entry['name']}",
        'model': entry['id'],
        'base_url': provider['base_url'],
        'api_key': provider.get('api_key', ''),
        'key_env': provider.get('key_env', ''),
        'no_key': provider.get('no_key', False),
        'context_window': entry['contextWindow'],
        'max_output': entry['maxTokens'],
        'adapter': provider['adapter'],
        'api': provider['api'],
        'wire_api': provider['wire_api'],
        'provider_id': provider['id'],
        'provider_name': provider['name'],
        'requires_openai_auth': provider.get('requires_openai_auth', True),
        'headers': dict(provider.get('headers') or {}),
        'disable_response_storage': provider.get('disable_response_storage', False),
        'role': entry.get('role', 'available'),
        'description': entry.get('description') or f"来自 provider {provider['name']}",
        'default_model': entry['id'] == provider.get('default_model'),
    }


def save_provider(store, models, provider):
    """Upsert one provider plus every model record it owns, then return the provider."""
    record = provider_record(provider)
    with store.transaction():
        store.put('provider', record)
        for entry in record['models']:
            models.save(model_record(record, entry))
    return store.get('provider', record['id'])


def models_of(store, provider_id):
    return [m for m in store.all('model') if m.get('provider_id') == provider_id]


def list_providers(store, models=None):
    """Provider directory for the console: provider facts plus their model records."""
    known = {m['id']: m for m in (models.list() if models else store.all('model'))}
    health_of = models.health_of if models is not None else None
    result = []
    for provider in store.all('provider'):
        entries = []
        ordered = sorted(enumerate(provider.get('models') or []),
                         key=lambda pair: (pair[1].get('id') != provider.get('default_model'),
                                           pair[1].get('priority', 10 ** 6), pair[0]))
        for _, entry in ordered:
            record = known.get(f"{provider['id']}:{entry['id']}")
            if record is None:
                continue
            entries.append({'id': entry['id'], 'name': entry['name'], 'description': entry.get('description', ''),
                            'contextWindow': entry['contextWindow'], 'maxTokens': entry['maxTokens'],
                            'inputModalities': entry.get('inputModalities', []), 'role': entry.get('role', 'available'),
                            'priority': entry.get('priority'),
                            'advertised': entry.get('advertised', True),
                            'reachability': entry.get('reachability') or record.get('reachability'),
                            # 可用性的权威来源是扁平记录的 health（真实调用会回写它）；
                            # provider 条目上的 reachability 只是给控制台的兼容副本。
                            'health': health_of(record) if health_of else None,
                            'default': entry['id'] == provider.get('default_model'), 'model': record})
        result.append({
            'id': provider['id'], 'name': provider['name'], 'api': provider['api'], 'adapter': provider['adapter'],
            'base_url': provider['base_url'], 'key_env': provider.get('key_env', ''),
            'configured': bool(provider.get('api_key') or provider.get('key_env') or provider.get('no_key')),
            'no_key': bool(provider.get('no_key')), 'headers': dict(provider.get('headers') or {}),
            'default_model': provider.get('default_model', ''), 'source_format': provider.get('source_format', ''),
            'models': entries, 'model_count': len(entries),
        })
    return result


def unmanaged_models(store, models=None):
    """Models that belong to no provider record, e.g. the offline demo."""
    known = models.list() if models else store.all('model')
    return [m for m in known if not m.get('provider_id')]

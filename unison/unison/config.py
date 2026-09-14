from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

from .store import now
from . import providers


_ENV_PATTERN = re.compile(r"%([^%]+)%")
_SUPPORTED_DSH_APIS = {
    "openai-responses": ("responses", "responses"),
    "openai-completions": ("chat_completions", "chat-completions"),
}
_FORMAT_CODEX = "codex-toml"
_FORMAT_DSH = "dsh-settings-yaml"

# 内置默认 provider 的凭据只从环境变量读取，源码里不放任何 Key：
# 这个仓库是公开的，硬编码一把可用的 Key 等于把它送给所有人。
# 用 `UNISON_BUILTIN_API_KEY=... python -m unison.server` 提供；
# 用户随配置一起导入的 key 优先于它（见 import_provider_config）。
BUILTIN_API_KEY = os.environ.get("UNISON_BUILTIN_API_KEY", "")

BUILTIN_CONFIG_TOML = """model_provider = "OpenAI"
# 主调度默认模型 = 模型目录里 priority 最高、且当前确实可调用的模型。
# 目录 priority=1 的 gpt-6-astra 已作为可选模型加入（见 codex-models.json），
# 但当前这把 Key 的 group 调用它会返回 404 model_not_found；
# 该 group 支持后，把 model 改成 "gpt-6-astra" 即可（控制台也可直接选择）。
model = "gpt-5.6-sol"
review_model = "gpt-5.5"
disable_response_storage = true
model_catalog_json = '%userprofile%\\.codex\\codex-models.json'
network_access = "enabled"
windows_wsl_setup_acknowledged = true

[model_providers.OpenAI]
name = "OpenAI"
base_url = "https://aaa.bi"
wire_api = "responses"
requires_openai_auth = true

[features]
goals = true
"""


def expand_path(value: str) -> Path:
    """Expand POSIX variables and Windows %NAME% variables on every platform.

    Windows 风格的配置（`%userprofile%\\.codex\\...`）在 Linux/macOS 上按同等语义解析：
    `%USERPROFILE%` 等映射到用户主目录，反斜杠视为路径分隔符。
    """
    def replace(match):
        name = match.group(1)
        if name.upper() in {'USERPROFILE', 'HOMEPATH', 'HOME'}:
            return str(Path.home())
        return os.environ.get(name, match.group(0))

    expanded = _ENV_PATTERN.sub(replace, value)
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    if '\\' in expanded:
        expanded = expanded.replace('\\', '/')
    return Path(expanded)


def _catalog_index(data):
    """Index every catalog entry by its id / model / slug, preserving the raw entry."""
    index = {}
    items = data
    if isinstance(data, dict):
        for key in ('models', 'data', 'items'):
            if isinstance(data.get(key), (list, dict)):
                items = data[key]
                break
        else:
            if all(isinstance(v, dict) for v in data.values()):
                items = data
    if isinstance(items, dict):
        for key, value in items.items():
            if isinstance(value, dict):
                index[str(key)] = value
    elif isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ('id', 'model', 'slug', 'name'):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    index.setdefault(value.strip(), item)
    return index


def read_catalog_index(path_value):
    """Read a Codex-style model catalog once and index all entries."""
    if not path_value:
        return {}, None
    path = expand_path(path_value)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}, f'模型目录不存在：{path}'
    except (OSError, ValueError) as exc:
        return {}, f'模型目录读取失败：{exc}'
    return _catalog_index(data), None


def read_catalog(path_value, model_ids):
    if not path_value:
        return {}, None
    index, error = read_catalog_index(path_value)
    return {model_id: index.get(model_id, {}) for model_id in model_ids}, error


def _positive_int(entry, names, default):
    for name in names:
        value = entry.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return default


def _scalar(value):
    value = value.strip()
    if not value:
        return {}
    if value.startswith(("&", "*", "!")) or value in {"|", ">"}:
        raise ValueError("DSH YAML 不支持 anchor、alias、tag 或 block scalar")
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith(("'", '"')):
        try:
            return json.loads(value) if value.startswith('"') else value[1:-1].replace("''", "'")
        except (ValueError, json.JSONDecodeError):
            raise ValueError(f"DSH YAML 字符串引号无效：{value}") from None
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            # YAML flow values used in provider profiles are normally JSON-compatible.
            raise ValueError(f"DSH YAML 行内数组/对象无效：{value}") from None
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", value):
        return float(value)
    return value


def _parse_yaml_subset(text):
    """Parse the mapping/list subset emitted by DSH settings examples, dependency-free."""
    root = {}
    stack = [(-1, root)]
    lines = text.splitlines()
    for number, raw in enumerate(lines, 1):
        if not raw.strip() or raw.lstrip().startswith("#") or raw.strip() == "---":
            continue
        prefix = raw[:len(raw) - len(raw.lstrip())]
        if "\t" in prefix:
            raise ValueError(f"DSH YAML 第 {number} 行不能使用 Tab 缩进")
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.strip()
        if content.startswith(("&", "*", "!")) or content.endswith((" |", " >")):
            raise ValueError(f"DSH YAML 第 {number} 行使用了不支持的 anchor、alias、tag 或 block scalar")
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if content.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"DSH YAML 第 {number} 行的列表缩进无效")
            item = content[2:].strip()
            if ":" in item:
                key, value = item.split(":", 1)
                node = {key.strip(): _scalar(value)}
                parent.append(node)
                stack.append((indent, node))
            else:
                parent.append(_scalar(item))
            continue
        if ":" not in content:
            raise ValueError(f"DSH YAML 第 {number} 行缺少 ':'")
        key, value = content.split(":", 1)
        key = key.strip().strip('"\'')
        if not key:
            raise ValueError(f"DSH YAML 第 {number} 行的键为空")
        if not isinstance(parent, dict):
            raise ValueError(f"DSH YAML 第 {number} 行的映射缩进无效")
        if value.strip():
            parent[key] = _scalar(value)
            continue
        # Infer a list when the next meaningful, more-indented line begins with '-'.
        child = {}
        for following in lines[number:]:
            if not following.strip() or following.lstrip().startswith("#"):
                continue
            next_indent = len(following) - len(following.lstrip(" "))
            if next_indent > indent and following.strip().startswith("- "):
                child = []
            break
        parent[key] = child
        stack.append((indent, child))
    return root


def _parse_document(text):
    try:
        parsed = tomllib.loads(text)
        # Parser success is not enough: simple YAML can also look TOML-like.
        if "model_provider" in parsed or isinstance(parsed.get("model_providers"), dict):
            return _FORMAT_CODEX, parsed
        toml_error = "缺少 Codex model_provider/model_providers"
    except tomllib.TOMLDecodeError as exc:
        toml_error = str(exc)
    try:
        parsed = json.loads(text) if text.lstrip().startswith("{") else _parse_yaml_subset(text)
    except (ValueError, json.JSONDecodeError) as yaml_error:
        raise ValueError(f"无法识别配置：既不是有效 Codex TOML，也不是有效 DSH YAML/JSON。TOML：{toml_error}；DSH：{yaml_error}") from None
    return _FORMAT_DSH, parsed


def _codex_provider(parsed):
    provider_id = parsed.get("model_provider")
    model_id = parsed.get("model")
    review_model = parsed.get("review_model") or model_id
    # 注意：不能把局部变量命名为 providers，否则会遮蔽 providers 模块。
    provider_map = parsed.get("model_providers") or {}
    provider = provider_map.get(provider_id) if isinstance(provider_map, dict) else None
    if not provider_id or not isinstance(provider, dict):
        raise ValueError("Codex TOML：model_provider 必须对应 [model_providers.<名称>] 配置")
    if not model_id:
        raise ValueError("Codex TOML：缺少 model")
    base_url = str(provider.get("base_url") or "").strip()
    if not base_url:
        raise ValueError(f"Codex TOML：provider {provider_id!r} 缺少 base_url")
    wire_api = str(provider.get("wire_api") or "chat_completions").lower().replace("-", "_")
    adapters = {"responses": "responses", "chat_completions": "chat-completions", "chat": "chat-completions"}
    if wire_api not in adapters:
        raise ValueError("Codex TOML：wire_api 目前支持 responses 或 chat_completions")
    requires_auth = bool(provider.get("requires_openai_auth", True))
    catalog_path = parsed.get("model_catalog_json")
    catalog, catalog_error = read_catalog_index(catalog_path)
    model_entries = []
    for current, role in ((model_id, "primary"), (review_model, "review")):
        if any(item["id"] == current for item in model_entries):
            continue
        entry = providers.catalog_metadata(catalog.get(current) or {})
        model_entries.append({
            "id": current,
            "name": entry.get("name") or current,
            "description": entry.get("description") or f"由 Codex TOML 导入的 {role} 模型",
            "context_window": entry.get("contextWindow") or 32768,
            "max_output": entry.get("maxTokens") or 4096,
            "role": role,
        })
    # 目录（catalog）是供应商声明的完整模型清单：全部纳入 provider，
    # 即使 /models 没有广告它，也能在控制台看到、选择并验证可用性。
    for slug, raw in catalog.items():
        if any(item["id"] == slug for item in model_entries):
            continue
        entry = providers.catalog_metadata(raw)
        model_entries.append({
            "id": slug,
            "name": entry.get("name") or slug,
            "description": entry.get("description") or "由 Codex 模型目录声明",
            "context_window": entry.get("contextWindow") or 32768,
            "max_output": entry.get("maxTokens") or 4096,
            "role": "available",
            "priority": entry.get("priority"),
        })
    normalized = {
        "id": str(provider_id), "name": str(provider.get("name") or provider_id),
        "base_url": base_url.rstrip("/"), "wire_api": wire_api, "adapter": adapters[wire_api],
        "api": "openai-responses" if adapters[wire_api] == "responses" else "openai-completions",
        "key_env": str(provider.get("key_env") or "OPENAI_API_KEY"),
        "requires_openai_auth": requires_auth, "no_key": not requires_auth,
        "headers": {}, "models": model_entries,
        # 目录索引随 provider 一起交给发现流程，使 /models 扫描出的模型也能拿到容量与说明。
        "catalog": catalog,
    }
    metadata = {
        "model": model_id, "review_model": review_model, "model_catalog_json": catalog_path or "",
        "catalog_error": catalog_error, "features": parsed.get("features") if isinstance(parsed.get("features"), dict) else {},
        "disable_response_storage": bool(parsed.get("disable_response_storage", False)),
        "network_access": parsed.get("network_access", ""),
        "windows_wsl_setup_acknowledged": bool(parsed.get("windows_wsl_setup_acknowledged", False)),
    }
    return normalized, metadata


def _dsh_section(parsed):
    if isinstance(parsed, list):
        matches = [item.get("config") for item in parsed if isinstance(item, dict) and item.get("name") == "@deepseek-ai/dsh-llm-pi-ai"]
        section = matches[0] if matches else None
    elif isinstance(parsed, dict):
        section = parsed.get("llm-pi-ai", parsed)
        # Also accept the documented Cordis plugin fragment without asking users to extract config.
        if isinstance(parsed.get("plugins"), list):
            matches = [item.get("config") for item in parsed["plugins"] if isinstance(item, dict) and item.get("name") == "@deepseek-ai/dsh-llm-pi-ai"]
            if matches:
                section = matches[0]
    else:
        section = None
    if not isinstance(section, dict) or not isinstance(section.get("providers"), dict):
        raise ValueError("DSH 配置：缺少 llm-pi-ai.providers（或插件 config.providers）")
    return section


def _dsh_provider(parsed):
    provider_map = _dsh_section(parsed)["providers"]
    default = parsed.get("agent-default-model") if isinstance(parsed, dict) else None
    if len(provider_map) != 1:
        names = "、".join(str(name) for name in provider_map) or "无"
        raise ValueError(f"DSH 配置：一次请粘贴一个自定义 provider；当前找到 {len(provider_map)} 个（{names}）")
    provider_id, profile = next(iter(provider_map.items()))
    if not isinstance(profile, dict):
        raise ValueError(f"DSH 配置：provider {provider_id!r} 必须是映射")
    base_url = str(profile.get("baseURL") or "").strip()
    api = str(profile.get("api") or "").strip()
    models = profile.get("models")
    if not base_url:
        raise ValueError(f"DSH 配置：自定义 provider {provider_id!r} 缺少 baseURL")
    if api == "anthropic-messages":
        raise ValueError(f"DSH 配置：provider {provider_id!r} 使用 anthropic-messages；当前后端明确不支持该 API")
    if api not in _SUPPORTED_DSH_APIS:
        supported = "、".join(_SUPPORTED_DSH_APIS)
        raise ValueError(f"DSH 配置：provider {provider_id!r} 的 api={api!r} 无法由 Unison 使用；支持 {supported}")
    if not isinstance(models, list) or not models:
        raise ValueError(f"DSH 配置：自定义 provider {provider_id!r} 需要非空 models 列表")
    normalized_models = []
    seen = set()
    default_context = _positive_int(profile, ("defaultContextWindow",), 262144)
    default_output = _positive_int(profile, ("defaultMaxTokens",), 32768)
    for index, entry in enumerate(models, 1):
        if not isinstance(entry, dict) or not str(entry.get("id") or "").strip():
            raise ValueError(f"DSH 配置：provider {provider_id!r} 的 models 第 {index} 项缺少 id")
        model_id = str(entry["id"]).strip()
        if model_id in seen:
            raise ValueError(f"DSH 配置：provider {provider_id!r} 的模型 id {model_id!r} 重复")
        seen.add(model_id)
        context = _positive_int(entry, ("contextWindow",), default_context)
        output = _positive_int(entry, ("maxTokens",), default_output)
        normalized_models.append({
            "id": model_id, "name": str(entry.get("name") or model_id),
            "description": f"由 DSH llm-pi-ai profile 导入",
            "context_window": context, "max_output": output,
        })
    wire_api, adapter = _SUPPORTED_DSH_APIS[api]
    headers = profile.get("headers") or {}
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError(f"DSH 配置：provider {provider_id!r} 的 headers 必须是字符串映射")
    normalized = {
        "id": str(provider_id), "name": str(profile.get("displayName") or provider_id),
        "base_url": base_url.rstrip("/"), "wire_api": wire_api, "adapter": adapter, "api": api,
        "key_env": str(profile.get("apiKeyEnv") or ""), "requires_openai_auth": bool(profile.get("apiKeyEnv")),
        "no_key": not bool(profile.get("apiKeyEnv")), "headers": dict(headers), "models": normalized_models,
    }
    first = normalized_models[0]["id"]
    selected = first
    if isinstance(default, dict) and str(default.get("provider") or "") == str(provider_id):
        selected = str(default.get("model") or first)
        if selected not in seen:
            raise ValueError(f"DSH 配置：agent-default-model.model={selected!r} 不在 provider {provider_id!r} 的 models 中")
    return normalized, {"model": selected, "review_model": selected, "features": {}, "disable_response_storage": False}


def normalize_provider_config(text):
    """Auto-detect Codex TOML or DSH llm-pi-ai YAML/JSON and return one provider shape."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请粘贴 Codex TOML 或 DSH Provider 配置")
    detected, parsed = _parse_document(text)
    provider, metadata = _codex_provider(parsed) if detected == _FORMAT_CODEX else _dsh_provider(parsed)
    return detected, provider, metadata


def import_provider_config(store, models, text, api_key=""):
    detected, provider, metadata = normalize_provider_config(text)
    # 用户随配置导入的 key 优先；没有才回落到环境变量里的内置凭据。
    effective_api_key = api_key or BUILTIN_API_KEY
    # provider 记录是唯一真相源；扁平 model 记录由 providers 统一投影。
    saved = providers.save_provider(store, models, dict(provider) | {
        'api_key': effective_api_key, 'default_model': metadata['model'],
        'source_format': detected, 'requires_openai_auth': provider["requires_openai_auth"],
        'disable_response_storage': metadata["disable_response_storage"],
    })
    imported = [f"{saved['id']}:{entry['id']}" for entry in saved['models']]
    model_id = f"{provider['id']}:{metadata['model']}"
    review_model_id = f"{provider['id']}:{metadata['review_model']}"
    config = {
        "id": "active", "format": detected, "source_format": detected, "model_provider": provider["id"],
        "model": metadata["model"], "model_id": model_id,
        "review_model": metadata["review_model"], "review_model_id": review_model_id,
        "disable_response_storage": metadata["disable_response_storage"],
        "model_catalog_json": metadata.get("model_catalog_json", ""), "catalog_error": metadata.get("catalog_error"),
        "network_access": metadata.get("network_access", ""),
        "windows_wsl_setup_acknowledged": metadata.get("windows_wsl_setup_acknowledged", False),
        "features": metadata.get("features", {}), "provider": saved, "imported_models": imported, "updated": now(),
    }
    store.put("app_config", config)
    try:
        discovery = scan_provider(models, dict(saved) | {"catalog": provider.get("catalog") or {}},
                                  api_key=effective_api_key)
        config["discovered_models"] = [item["id"] for item in discovery["models"]]
        config["discovery_error"] = None
    except Exception as exc:
        config["discovered_models"] = []
        config["discovery_error"] = str(exc)
    config["updated"] = now()
    store.put("app_config", config)
    return config


def scan_provider(models, provider, api_key=None):
    """扫描 provider 的 /models 并补齐缺失模型；返回发现结果。

    目录（catalog）元数据随 provider 传入，使扫描出的模型也能得到正确的上下文窗口。
    """
    return models.discover({
        "provider_id": provider["id"], "provider_name": provider["name"], "base_url": provider["base_url"],
        "api_key": api_key if api_key is not None else provider.get("api_key", ""),
        "key_env": provider.get("key_env", ""), "no_key": provider.get("no_key", False),
        "requires_openai_auth": provider.get("requires_openai_auth", True),
        "adapter": provider.get("adapter", ""), "api": provider.get("api", ""),
        "wire_api": provider.get("wire_api", ""), "headers": provider.get("headers", {}),
        "disable_response_storage": provider.get("disable_response_storage", False),
        "defaultContextWindow": provider.get("defaultContextWindow"),
        "defaultMaxTokens": provider.get("defaultMaxTokens"),
        "catalog": provider.get("catalog") or {},
    })


def load_builtin_config(store, models):
    """启动时自动加载内置配置。

    失败不再静默：异常向上抛出，由调用方记录并展示，避免“默认模型凭空消失”。
    """
    configs = store.all('app_config')
    if configs:
        return None  # 已有配置，跳过
    return import_provider_config(store, models, BUILTIN_CONFIG_TOML, BUILTIN_API_KEY)


# Backward-compatible Python API; the endpoint now accepts both formats.
def import_codex_config(store, models, text, api_key=""):
    return import_provider_config(store, models, text, api_key)

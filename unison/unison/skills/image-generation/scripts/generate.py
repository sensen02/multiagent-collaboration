#!/usr/bin/env python3
"""按提示词生成图像并落盘，打印一行 JSON 结果。

设计约束（这是技能内置脚本，不是库）：

- **只用标准库**：本项目运行时零第三方依赖，技能脚本也不引入。
- **四个后端**：`pollinations`（匿名可用，无需任何 Key）、`huggingface`（需要 HF token）、
  `chatgpt`（复用本机 Codex 的 ChatGPT 登录态，走官方 `gpt-image-*`）、`local`（本机 GPU）。
- **结果可核对**：校验响应确实是 PNG/JPEG/GIF/WebP，解析出真实宽高，而不是相信请求参数。
- **错误可诊断**：HTTP 错误与后端 JSON 错误都截断成短消息，绝不把整页 HTML 塞进结果。
- **不打印凭据**：token 只从参数或环境变量读取，不出现在输出与 URL 里。

两种入口，**共用同一份实现**：

1. **命令行**（技能正文里教模型用的方式）：
       python3 generate.py --prompt "..." --out ./images/cat.jpg
       python3 generate.py --prompt "..." --out ./x.png --backend huggingface --model black-forest-labs/FLUX.1-schnell
       python3 generate.py --list-models --backend pollinations
2) **桥接**（端口调用，stdin 收 JSON、stdout 回 JSON）：检测到 stdin 有数据即进入桥接模式，
   因此同一条命令既能被模型直接跑，也能被 /api/skills/call 调用：
       echo '{"skill":"image-generation","args":{"prompt":"..."},"workspace":"/srv/app"}' | python3 generate.py
       → {"ok": true, "result": {...}}   或   {"ok": false, "error": "..."}
   `args` 为数组时表示批量（受 invocation.batch 约束），返回 `{"ok":true,"results":[...]}`。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

POLLINATIONS_BASE = 'https://image.pollinations.ai'
POLLINATIONS_MODELS = 'https://image.pollinations.ai/models'
# 匿名档的默认模型：`flux`。实测（2026-09-14，同 prompt 同 seed 比哈希）：
# `flux` 与其它任何名字的输出都不同，而 `flux-realism` / `sana` / `turbo` 三者**逐字节相同**
# —— 说明未识别的模型名会静默回落到同一个默认模型，而 `/models` 只广告 `sana`（= 那个回落值），
# 所以那份清单**低估**了实际可用项。`flux` 是本档唯一能区分的命名模型，且同 seed 可复现。
# 不传 `--model` 时上游用的是别的东西，因此这里显式默认到 flux。
DEFAULT_POLLINATIONS_MODEL = 'flux'
HF_BASE = 'https://router.huggingface.co/hf-inference/models'
# Hugging Face 上的 FLUX：schnell 是蒸馏快版（4 步），dev 是质量版（更强，但更慢、许可不同）。
HF_FLUX_DEFAULT = 'black-forest-labs/FLUX.1-schnell'
HF_FLUX_DEV = 'black-forest-labs/FLUX.1-dev'
# Codex 的 ChatGPT 后端：登录态为 chatgpt 时 base 是 https://chatgpt.com/backend-api/codex，
# 生图端点是它下面的 images/generations（与 images/edits）。
CHATGPT_BASE = 'https://chatgpt.com/backend-api/codex'
CHATGPT_IMAGE_MODEL = 'gpt-image-2'
CHATGPT_AUTH_FILE = os.environ.get('CODEX_HOME', os.path.expanduser('~/.codex')) + '/auth.json'
USER_AGENT = 'Unison/0.1 (skill: image-generation)'
MAX_BYTES = 32 * 1024 * 1024
DEFAULT_TIMEOUT = 120.0

# 本技能**实现上**支持的后端（`--capabilities` 自报，供调用方直接问答）。
CAPABILITIES = {'backends': ['pollinations', 'huggingface', 'chatgpt', 'local'],
                'batch': True,
                'local_requires': ['sd-cli 可执行',
                                   '单文件模型：完整 sd15/sdxl checkpoint（LoRA 不能填 -m）',
                                   '分离式模型：FLUX 的 gguf + 同目录 clip_l / t5xxl / ae 四件齐'],
                'chatgpt_requires': ['本机 Codex 的 ChatGPT 登录态（~/.codex/auth.json，auth_mode=chatgpt）'],
                'local_models_ready': ['flux1-schnell-Q8_0（Apache-2.0，可商用）']}

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import local_image                                     # noqa: N813
except Exception as _local_exc:                            # noqa: BLE001
    local_image = None
    _LOCAL_IMPORT_ERROR = f'{type(_local_exc).__name__}:{_local_exc}'
else:
    _LOCAL_IMPORT_ERROR = None


def _fail(message, **extra):
    """以非零退出码报告失败：状态、原因与建议分开写，便于模型决定下一步。"""
    payload = {'ok': False, 'error': str(message)}
    payload.update({k: v for k, v in extra.items() if v is not None})
    print(json.dumps(payload, ensure_ascii=False))
    return 1


def _short(text, limit=400):
    value = re.sub(r'\s+', ' ', str(text or '')).strip()
    return value[:limit] + ('…' if len(value) > limit else '')


def _slug(text, limit=60):
    value = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]+', '-', str(text or '').strip()).strip('-').lower()
    return (value[:limit] or 'image')


def sniff(data):
    """按魔数识别真实格式，返回 `(格式, 宽, 高)`；无法识别时格式为 None。"""
    if data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) >= 24:
        width, height = struct.unpack('>II', data[16:24])
        return 'png', width, height
    if data[:3] == b'\xff\xd8\xff':
        return 'jpeg', *_jpeg_size(data)
    if data[:6] in {b'GIF87a', b'GIF89a'} and len(data) >= 10:
        width, height = struct.unpack('<HH', data[6:10])
        return 'gif', width, height
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP' and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b'VP8X' and len(data) >= 30:
            width = int.from_bytes(data[24:27], 'little') + 1
            height = int.from_bytes(data[27:30], 'little') + 1
            return 'webp', width, height
        if chunk == b'VP8 ' and len(data) >= 30:
            width = int.from_bytes(data[26:28], 'little') & 0x3FFF
            height = int.from_bytes(data[28:30], 'little') & 0x3FFF
            return 'webp', width, height
        if chunk == b'VP8L' and len(data) >= 25:
            bits = int.from_bytes(data[21:25], 'little')
            return 'webp', (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None, None, None


def _jpeg_size(data):
    """走一遍 JPEG 段，读到 SOFn 才知道真实尺寸。"""
    index = 2
    total = len(data)
    while index + 9 < total:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xD9:
            break
        length = struct.unpack('>H', data[index + 2:index + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            height, width = struct.unpack('>HH', data[index + 5:index + 9])
            return width, height
        index += 2 + length
    return None, None


def _request(url, payload=None, headers=None, timeout=DEFAULT_TIMEOUT):
    request = urllib.request.Request(url, data=payload, headers=headers or {})
    request.add_header('User-Agent', USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
            return body, response.headers.get('Content-Type', '')
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = _short(exc.read(MAX_BYTES), 300)
        except Exception:
            detail = ''
        raise RuntimeError(f'HTTP {exc.code} {exc.reason}' + (f'：{detail}' if detail else '')) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f'连接失败：{exc.reason}') from None
    except TimeoutError:
        raise RuntimeError(f'请求超时（{timeout:.0f}s）') from None


def _request_json(url, payload, headers, timeout):
    """POST 一份 JSON 并解析回 JSON；同时把响应头带回来（生图请求 id 在头里）。

    与 `_request` 分开是因为这里要的是 **JSON 而不是图像字节**，且需要读响应头。
    """
    request = urllib.request.Request(url, data=payload, headers=headers)
    request.add_header('User-Agent', USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES)
            return json.loads(raw.decode('utf-8', 'replace')), response.headers
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = _short(exc.read(MAX_BYTES), 300)
        except Exception:
            detail = ''
        raise RuntimeError(f'HTTP {exc.code} {exc.reason}' + (f'：{detail}' if detail else '')) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f'连接失败：{exc.reason}') from None
    except TimeoutError:
        raise RuntimeError(f'请求超时（{timeout:.0f}s）') from None
    except json.JSONDecodeError:
        raise RuntimeError('生图接口没有返回 JSON') from None


def chatgpt_credentials(path=None):
    """读取本机 Codex 的 ChatGPT 登录态，返回 `(access_token, account_id, plan)`。

    **只读，不刷新、不回写。** 刷新令牌是单次有效的（Codex 自己会轮换它），
    一个第三方脚本擅自刷新而不落盘，会让 Codex 那边下一次刷新被判成 reuse 而失效。
    因此这里过期就明确报错，让 `codex` 自己刷新 —— 它本来就是这份凭据的持有者。
    """
    path = path or CHATGPT_AUTH_FILE
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise RuntimeError(f'找不到 Codex 登录态：{path}（先在 Codex 里登录一次，或改 CODEX_HOME）') from None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'读不了 Codex 登录态 {path}：{exc}') from None
    mode = data.get('auth_mode')
    if mode != 'chatgpt':
        raise RuntimeError(f'Codex 登录态不是 chatgpt（当前 auth_mode={mode!r}）：'
                           '本后端用的是 ChatGPT 订阅后端，不是 API Key 那条路')
    tokens = data.get('tokens') or {}
    token = tokens.get('access_token')
    account = tokens.get('account_id')
    if not token:
        raise RuntimeError(f'{path} 里没有 access_token：重新登录一次 Codex')
    plan = None
    try:                                                   # 解 JWT 只读 claim，不验签（本地凭据，不需要）
        part = token.split('.')[1]
        part += '=' * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part).decode('utf-8', 'replace'))
        auth = claims.get('https://api.openai.com/auth') or {}
        plan = auth.get('chatgpt_plan_type')
        expires = claims.get('exp')
        if expires and expires < time.time():
            raise RuntimeError(f'access_token 已过期（exp={expires}）：跑一次 codex 让它自己刷新 {path}')
    except (IndexError, ValueError, json.JSONDecodeError):
        pass                                               # 不是 JWT 就跳过过期判断，交给上游回答
    return token, account, plan


def chatgpt_generate(options):
    """走官方 ChatGPT 后端生成/编辑图像，返回与其它后端同形的结果。

    请求形状取自 Codex 的实现（`ext/image-generation` + `codex-api/src/endpoint/images.rs`）：
    `POST {base}/images/generations`，体为 `{prompt, model, n, quality, size?, background?}`，
    响应 `{created, data:[{b64_json, generation_id}], size, quality, output_format, usage}`。
    """
    token, account, plan = chatgpt_credentials(options.get('auth_file'))
    model = str(options.get('model') or '').strip() or CHATGPT_IMAGE_MODEL
    body = {'prompt': options['prompt'], 'model': model, 'n': int(options.get('n') or 1)}
    for key in ('quality', 'size', 'background'):
        value = options.get(key)
        if value:
            body[key] = value
    headers = {'Content-Type': 'application/json',
               'Authorization': f'Bearer {token}',
               'originator': 'codex_cli_rs'}
    if account:
        headers['ChatGPT-Account-ID'] = account
    if options.get('turn_id'):
        headers['x-codex-image-turn-id'] = str(options['turn_id'])
    payload, response_headers = _request_json(f'{CHATGPT_BASE}/images/generations',
                                              json.dumps(body).encode(),
                                              headers, options['timeout'])
    items = payload.get('data') or []
    if not items:
        raise RuntimeError(f'生图接口没有返回图像：{_short(json.dumps(payload, ensure_ascii=False), 300)}')
    try:
        raw = base64.b64decode(items[0]['b64_json'])
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f'生图接口返回的 b64_json 无法解码：{exc}') from None
    if len(raw) > MAX_BYTES:
        raise RuntimeError(f'响应超过 {MAX_BYTES // (1024 * 1024)} MiB 上限，已放弃')
    kind, width, height = sniff(raw)
    if not kind:
        raise RuntimeError(f'生图接口返回的不是可识别的图像（{len(raw)} 字节）')
    return {'body': raw, 'format': kind, 'width': width, 'height': height,
            'content_type': f'image/{kind}',
            'chatgpt': {'plan': plan, 'model': model,
                        'requested_size': body.get('size'),
                        'reported_size': payload.get('size'),
                        'quality': payload.get('quality'),
                        'output_format': payload.get('output_format'),
                        'usage': payload.get('usage'),
                        'generation_id': items[0].get('generation_id'),
                        'request_id': response_headers.get('x-codex-imagegen-request-id')}}


def pollinations_url(prompt, width, height, seed, model, extra):
    query = {'width': width, 'height': height, 'nologo': 'true'}
    if seed is not None:
        query['seed'] = seed
    if model:
        query['model'] = model
    query.update(extra or {})
    # prompt 放在路径里（上游的规范形式）；其余参数走 query。
    return f'{POLLINATIONS_BASE}/prompt/{urllib.parse.quote(prompt, safe="")}?' + urllib.parse.urlencode(query)


def generate(**options):
    backend = options['backend']
    prompt = options['prompt']
    timeout = options['timeout']
    if backend == 'pollinations':
        url = pollinations_url(prompt, options['width'], options['height'], options['seed'],
                               options['model'], options['param'])
        body, content_type = _request(url, timeout=timeout)
    elif backend == 'huggingface':
        token = options['token'] or os.environ.get('HF_TOKEN', '')
        if not token:
            raise RuntimeError('Hugging Face 路由需要凭据：请用 --token 或设置 HF_TOKEN 环境变量'
                               '（没有 token 时走 pollinations 的 flux，或 chatgpt 的 gpt-image，都不需要额外凭据）')
        model = options['model']
        if not model or '/' not in model:
            raise RuntimeError('Hugging Face 需要形如 owner/name 的模型 ID，例如 black-forest-labs/FLUX.1-schnell')
        payload = json.dumps({'inputs': prompt, 'parameters': options['param'] or {}}).encode()
        headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {token}',
                   'Accept': 'image/png'}
        body, content_type = _request(f'{HF_BASE}/{model}', payload=payload, headers=headers, timeout=timeout)
    elif backend == 'chatgpt':
        return chatgpt_generate(options)
    else:
        raise RuntimeError(f'未知后端：{backend}')
    if len(body) > MAX_BYTES:
        raise RuntimeError(f'响应超过 {MAX_BYTES // (1024 * 1024)} MiB 上限，已放弃')
    kind, width, height = sniff(body)
    if not kind:
        raise RuntimeError(f'响应不是可识别的图像（Content-Type: {content_type or "未知"}）：{_short(body, 200)}')
    return {'body': body, 'format': kind, 'width': width, 'height': height, 'content_type': content_type}


GO_BACKENDS = ('pollinations', 'huggingface', 'chatgpt')
ALL_BACKENDS = GO_BACKENDS + ('local',)


def render_one(options, workspace=None):
    """一次生成并落盘，返回结果字典；失败抛 RuntimeError/ValueError。

    CLI 与桥接都走这里，因此两条路径的校验、命名与报错完全一致。
    `backend=local` 转到本机后端（`local_image`），它自己有更严的前置校验
    （权重角色、LoRA 是否真的生效、输出是否退化），成功时返回同形结果。
    """
    prompt = str(options.get('prompt') or '').strip()
    if not prompt:
        raise ValueError('缺少 prompt')
    backend = str(options.get('backend') or 'pollinations')
    if backend not in ALL_BACKENDS:
        raise ValueError(f'未知后端：{backend}（本技能实现的有 {", ".join(ALL_BACKENDS)}）')
    if backend == 'local':
        if local_image is None:
            raise RuntimeError(f'本机后端不可用（module import failed: {_LOCAL_IMPORT_ERROR}）')
        envelope = local_image.generate_local(
            [dict(options, backend=None)], workspace=workspace,
            provenance_path=options.get('provenance'))
        return {k: v for k, v in envelope.items() if k != 'items'}
    width = max(64, min(int(options.get('width') or 1024), 4096))
    height = max(64, min(int(options.get('height') or 1024), 4096))
    seed = options.get('seed')
    seed = int(seed) if seed not in (None, '') else None
    timeout = float(options.get('timeout') or DEFAULT_TIMEOUT)
    extension = {'pollinations': 'jpg', 'huggingface': 'png', 'chatgpt': 'png'}[backend]
    # 各后端的默认模型在这里一次定死，请求与结果回报用的是同一个名字。
    model = str(options.get('model') or '').strip()
    if not model:
        model = {'pollinations': DEFAULT_POLLINATIONS_MODEL,
                 'huggingface': HF_FLUX_DEFAULT}.get(backend, '')
    # chatgpt 后端收的是 `size` 字符串（"1024x1024" 或 "auto"），不是宽高两个数；
    # 没显式给 --size 就用 --width/--height 拼一个。上游可能按内容自行调整实际尺寸，
    # 所以结果里的 width/height 始终来自图像头解析，`requested_size` 才是请求值。
    size = str(options.get('size') or '').strip()
    if backend == 'chatgpt' and not size:
        size = f'{width}x{height}'
    out = str(options.get('out') or '').strip()
    if not out:
        out = os.path.join(workspace or '.', 'images', f'{_slug(prompt)}.{extension}')
        os.makedirs(os.path.dirname(out), exist_ok=True)
    elif out.endswith(os.sep) or out.endswith('/'):
        out = os.path.join(out, f'{_slug(prompt)}.{extension}')
    elif not os.path.splitext(out)[1]:
        out = f'{out}.{extension}'
    started = time.time()
    result = generate(backend=backend, prompt=prompt, width=width, height=height, seed=seed,
                      model=model, token=str(options.get('token') or ''),
                      timeout=timeout, param=options.get('param') or {},
                      size=size, quality=options.get('quality'), background=options.get('background'),
                      auth_file=options.get('auth_file'), turn_id=options.get('turn_id'),
                      n=options.get('n'))
    directory = os.path.dirname(os.path.abspath(out))
    os.makedirs(directory, exist_ok=True)
    with open(out, 'wb') as handle:
        handle.write(result['body'])
    envelope = {'path': out, 'bytes': len(result['body']), 'format': result['format'],
                'width': result['width'], 'height': result['height'], 'backend': backend,
                'model': model or None, 'seed': seed, 'seconds': round(time.time() - started, 2),
                'note': '图像内容未经过自动核对；如需确认与需求一致，请查看文件后再决定是否集成。'}
    if result.get('chatgpt'):
        # 如实回报上游的说法（含实际尺寸与用量），但不要把这些当成质量判词。
        envelope['chatgpt'] = result['chatgpt']
    return envelope


def _inside_workspace(path, workspace):
    """桥接调用的产物必须落在工作区内：这是契约，不是建议。

    技能目录是随程序分发的源码，写进去既不会被任务文件清单记录，也会污染安装；
    绝对路径越出工作区同理——调用方给的工作区就是这次调用的落盘范围。
    """
    if not workspace:
        return True
    try:
        return os.path.abspath(path).startswith(os.path.abspath(workspace).rstrip(os.sep) + os.sep)
    except (TypeError, ValueError):
        return False


def bridge(payload):
    """端口调用的桥接入口：读一份 payload，返回一份结果（stdout 只写一行 JSON）。

    与命令行唯一的差别是**路径基准**：桥接的 `out` 相对路径一律相对 `workspace` 解析，
    并拒绝写到工作区之外；命令行保持相对当前目录（人在哪个目录跑就写哪里）。

    `backend=local` 的一批在**同一个进程里串行**跑完，并落一份溯源
    （`<out 所在目录>/generation.json`）；混用后端的批量会被拒绝，
    因为一次「生成这一批」的溯源必须能被一句话说清。
    """
    args = payload.get('args')
    workspace = payload.get('workspace') or os.getcwd()

    def prepare(item):
        options = dict(item) if isinstance(item, dict) else {}
        out = str(options.get('out') or '').strip()
        if out:
            if not os.path.isabs(out):
                out = os.path.join(workspace, out)
            if not _inside_workspace(out, workspace):
                raise ValueError(f'out 必须位于工作区内：{options.get("out")!r} → {out}')
            options['out'] = out
        # 省略 out 时由 render_one 按提示词命名到 workspace/images 下。
        return options

    try:
        items = args if isinstance(args, list) else [args]
        items = [prepare(item) for item in items]
        backends = {str(item.get('backend') or 'pollinations') for item in items}
        if len(backends) > 1:
            raise ValueError(f'一次调用只能用一个后端，当前混用了 {sorted(backends)}：'
                             '请拆成多次调用，否则溯源说不清这一批是怎么来的')

        if backends == {'local'}:
            if local_image is None:
                raise RuntimeError(f'本机后端不可用（module import failed: {_LOCAL_IMPORT_ERROR}）')
            provenance = None
            for item in items:
                candidate = item.get('provenance')
                if candidate:
                    provenance = candidate if os.path.isabs(candidate) else os.path.join(workspace, candidate)
                    break
            try:
                envelope = local_image.generate_local(items, workspace=workspace,
                                                      provenance_path=provenance)
            except local_image.LocalGenerationError as exc:
                return {'ok': False, 'error': str(exc), 'partial': exc.envelope}
            if isinstance(args, list):
                return {'ok': True, 'results': envelope['items'], 'count': len(envelope['items']),
                        'local': {k: v for k, v in envelope.items() if k != 'items'}}
            return {'ok': True, 'result': {k: v for k, v in envelope.items() if k != 'items'}}

        results = [render_one(item, workspace) for item in items]
        if isinstance(args, list):
            return {'ok': True, 'results': results, 'count': len(results)}
        return {'ok': True, 'result': results[0]}
    except (RuntimeError, ValueError, OSError) as exc:
        message = str(exc)
        if 'checkpoint' in message or 'LoRA' in message:
            return {'ok': False, 'error': message,
                    'hint': '先用 gpu 技能的 scripts/weights.py 看文件头判定，再决定它能填 -m 还是只进 <lora:…>'}
        return {'ok': False, 'error': message}


def list_models(backend, timeout):
    if backend == 'local':
        if local_image is None:
            return {'backend': 'local', 'error': f'本机后端不可用：{_LOCAL_IMPORT_ERROR}'}
        paths = local_image.default_paths()
        return {'backend': 'local',
                'sd_cli': str(paths['sd_cli']),
                'sd_cli_present': os.access(paths['sd_cli'], os.X_OK),
                'default_model': str(paths['model']),
                'weights': local_image.available_weights(),
                'note': ('本机后端用 --model 指定完整 checkpoint（不是模型名）；'
                         'role=lora-adapter 的权重不能填 -m，只能进 prompt 的 <lora:名字:1.0>。')}
    if backend == 'chatgpt':
        try:
            _token, account, plan = chatgpt_credentials()
        except RuntimeError as exc:
            return {'backend': 'chatgpt', 'error': str(exc)}
        return {'backend': 'chatgpt',
                'models': [CHATGPT_IMAGE_MODEL, 'gpt-image-1.5'],
                'default_model': CHATGPT_IMAGE_MODEL,
                'plan': plan,
                'account': 'present' if account else 'missing',
                'base': CHATGPT_BASE,
                'note': ('这是 ChatGPT 订阅后端的官方 gpt-image 模型，不是 API Key 那条路；'
                         '等价端点 POST /images/generations 与 /images/edits。'
                         '默认 gpt-image-2；只有明确需要透明背景等旧模型特性时才用 gpt-image-1.5。')}
    if backend != 'pollinations':
        return {'backend': backend, 'note': 'Hugging Face 的模型清单请在 https://huggingface.co/models 按 text-to-image 过滤；本脚本不代为检索。'}
    try:
        body, _ = _request(POLLINATIONS_MODELS, timeout=min(timeout, 30.0))
        data = json.loads(body.decode('utf-8', 'replace'))
    except (RuntimeError, ValueError) as exc:
        return {'backend': backend, 'error': str(exc)}
    names = []
    for item in data if isinstance(data, list) else []:
        names.append(item.get('name') if isinstance(item, dict) else str(item))
    return {'backend': backend, 'models': [x for x in names if x],
            'default_model': DEFAULT_POLLINATIONS_MODEL,
            'note': ('上游这份清单**低估**了实际可用项：实测（同 prompt 同 seed 比输出哈希）'
                     '`flux` 与任何其它名字的结果都不同，而其它未识别的名字会**静默回落**到同一个默认模型'
                     f'（`flux-realism`/`sana`/`turbo` 逐字节相同）。默认因此定为 `{DEFAULT_POLLINATIONS_MODEL}`；'
                     '要更强的 FLUX（dev）或 4 步快版（schnell）请走 huggingface 后端，需要 HF token。')}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='生成图像并落盘（pollinations / huggingface / chatgpt / local 本机 GPU）')
    parser.add_argument('--prompt', help='图像描述；建议英文，包含主体、风格、构图、光线')
    parser.add_argument('--out', help='输出路径；以 / 结尾或省略扩展名时按提示词自动命名')
    parser.add_argument('--backend', default='pollinations', choices=list(ALL_BACKENDS))
    parser.add_argument('--model', default='',
                        help='联网后端的模型名；local 后端则填完整 checkpoint 路径')
    parser.add_argument('--width', type=int, default=1024)
    parser.add_argument('--height', type=int, default=1024)
    parser.add_argument('--seed', type=int)
    parser.add_argument('--token', default='', help='Hugging Face token；也可用 HF_TOKEN 环境变量')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT)
    local = parser.add_argument_group('local 后端专用')
    local.add_argument('--steps', type=int, default=None, help='采样步数；SD15 用 6–8，FLUX schnell 用 4，FLUX dev 用 20+。省略则按模型自动定档')
    local.add_argument('--guidance', type=float, default=None,
                       help='distilled guidance（FLUX 用；默认 3.5）。SD 系列不需要')
    local.add_argument('--cfg-scale', type=float, default=None, help='CFG 强度；SD 系列默认 7.0，FLUX 固定 1.0（省略即按模型定）')
    local.add_argument('--lora-dir', help='LoRA 目录；prompt 里的 <lora:名字:1.0> 靠它才找得到')
    local.add_argument('--lora', help='要应用的 LoRA 文件名（不含扩展名）')
    local.add_argument('--lora-weight', type=float, default=1.0)
    local.add_argument('--negative', default='', help='负向提示词；留空用本机默认那一串')
    local.add_argument('--sd-cli', help='sd-cli 路径；默认 /srv/unison-assets/bin/sd-cli')
    local.add_argument('--device', default=None,
                       help='sd-cli 的 --backend 值。单文件模型默认 vulkan0；'
                            '分离式模型（FLUX）默认 diffusion=vulkan0,te=cpu（16G 卡放不下全套，见 SKILL.md）')
    local.add_argument('--extra-args', nargs=argparse.REMAINDER, default=[],
                       help='原样透传给 sd-cli 的其它参数（放在最后）')
    local.add_argument('--provenance', help='溯源 JSON 的落盘路径；默认 <out 目录>/generation.json')
    chatgpt = parser.add_argument_group('chatgpt 后端专用（官方 gpt-image，走本机 Codex 登录态）')
    chatgpt.add_argument('--size', help='"auto" 或 "WxH"；省略则用 --width/--height 拼。'
                                        '上游可能按内容自行调整实际尺寸，结果里的 width/height 是实测值')
    chatgpt.add_argument('--quality', choices=['low', 'medium', 'high', 'auto'],
                         help='出图质量档；试跑用 low，交付用 high')
    chatgpt.add_argument('--background', choices=['transparent', 'opaque', 'auto'])
    chatgpt.add_argument('--n', type=int, help='一次出几张（当前只落盘第 1 张）')
    chatgpt.add_argument('--auth-file', help='Codex 登录态路径；默认 $CODEX_HOME/auth.json 或 ~/.codex/auth.json')
    chatgpt.add_argument('--turn-id', help='透传 x-codex-image-turn-id（可选的会话标记）')
    parser.add_argument('--list-models', action='store_true')
    parser.add_argument('--capabilities', action='store_true',
                        help='打印本脚本实现上支持的能力（与 SKILL.md 声明对账用）')
    return parser.parse_args(argv)


def main(argv=None):
    # stdin 有内容即视为桥接调用：同一条命令既能被模型直接跑，也能被端口调用。
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                print(json.dumps({'ok': False, 'error': f'桥接输入不是 JSON：{exc}'}, ensure_ascii=False))
                return 1
            print(json.dumps(bridge(payload), ensure_ascii=False))
            return 0
    args = parse_args(argv)
    if args.capabilities:
        print(json.dumps({'skill': 'image-generation', **CAPABILITIES,
                          'local_module': None if local_image is None else 'scripts/local_image.py',
                          'local_error': _LOCAL_IMPORT_ERROR}, ensure_ascii=False))
        return 0
    if args.list_models:
        print(json.dumps(list_models(args.backend, args.timeout), ensure_ascii=False))
        return 0
    if not args.prompt or not str(args.prompt).strip():
        return _fail('缺少 --prompt', hint='用法：python3 generate.py --prompt "..." --out ./images/x.jpg')
    options = {'prompt': args.prompt, 'out': args.out, 'backend': args.backend, 'model': args.model,
               'width': args.width, 'height': args.height, 'seed': args.seed, 'token': args.token,
               'timeout': args.timeout, 'steps': args.steps, 'cfg_scale': args.cfg_scale,
               'guidance': args.guidance,
               'lora_dir': args.lora_dir, 'lora': args.lora, 'lora_weight': args.lora_weight,
               'sd_cli': args.sd_cli, 'provenance': args.provenance,
               'size': args.size, 'quality': args.quality, 'background': args.background,
               'n': args.n, 'auth_file': args.auth_file, 'turn_id': args.turn_id,
               'negative': args.negative or None,
               'extra_args': args.extra_args,
               'local': {'backend': args.device}}
    try:
        result = render_one(options)
    except (RuntimeError, ValueError) as exc:
        return _fail(exc, backend=args.backend, model=args.model or None)
    except OSError as exc:
        return _fail(f'写入失败：{exc}', path=args.out)
    print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())

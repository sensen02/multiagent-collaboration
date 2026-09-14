"""技能（skill）：可复用、按需加载的能力包。

语义与 DSH 的 skill 机制同构，但只实现本项目需要的最小部分：

- **发现**：一个技能是**目录包** `<root>/<name>/SKILL.md`，或**扁平 Markdown** `<root>/<name>.md`；
  只扫描各根目录的**顶层**，不递归发现 `**/SKILL.md`。
- **frontmatter**：`SKILL.md` 以 YAML frontmatter 开头，必填 `name` 与 `description`，
  可选 `when-to-use`（也接受 `whenToUse`）、`metadata`、`disable-model-invocation`、`user-invocable`。
  字段写错即**整条拒绝并给出可读原因**，不静默降级。
- **多根与竞争**：根按 rank 升序扫描，同名技能由 rank 最小者胜出（项目 > 自定义 > 用户 > 内置）。
- **按需加载**：目录只暴露 `name` 与描述；正文只在模型调用 `skill` 工具时读取，
  渲染成与 DSH 一致的 `<skill_content>` 块，并附带资源基目录，供正文里的相对路径解析。

刻意不依赖 PyYAML：本项目运行时零第三方依赖，frontmatter 只支持标量、行内列表与一层缩进映射，
需要更复杂的结构时应改写技能文件，而不是引入解析歧义。
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

NAME_PATTERN = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
FRONTMATTER_PATTERN = re.compile(r'\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)', re.DOTALL)
_INTEGER = re.compile(r'[+-]?\d+')
_NUMBER = re.compile(r'[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?')
# 布尔词表刻意收窄：YAML 1.1 会把裸 `1`/`0` 当布尔，那会让 `timeout: 1` 变成 True
# 并以"必须是正数"这种费解的方式拒绝整条技能。这里 `1`/`0` 一律按整数解析。
_BOOL_TRUE = {'true', 'yes', 'on'}
_BOOL_FALSE = {'false', 'no', 'off'}
_METADATA_LIMIT = 16
_STATE_LIMIT = 512

# 技能可以像模型一样通过端口被调用：声明 `invocation:` 后由 skill_runtime 的桥接器执行。
INVOCATION_KINDS = {'bridge'}
MAX_BATCH = 8
DEFAULT_INVOKE_TIMEOUT = 300.0
MAX_INVOKE_TIMEOUT = 3600.0

# 根优先级：数字小的先被扫描，因此同名时胜出。与 DSH 的 rank 顺序一致。
# 顺序：任务工作区自己的技能根 > 自定义根 > 用户根 > 项目根及其祖先 > 随程序分发的内置包。
PROJECT_DSH = 100
PROJECT_AGENTS = 200
CUSTOM_DEFAULT = 300
CUSTOM_MAX = 499
USER_DSH = 500
USER_AGENTS = 600
ROOT_CHAIN = 1000
BUNDLED = 100000
# 工作区向上级联的上限：足够覆盖任何真实目录深度，同时避免异常路径导致无界遍历。
ROOT_WALK_LIMIT = 32
RANK_NAMES = {
    PROJECT_DSH: 'workspace-dsh',
    PROJECT_AGENTS: 'workspace-agents',
    USER_DSH: 'user-dsh',
    USER_AGENTS: 'user-agents',
    BUNDLED: 'bundled',
}
RANK_HINTS = {
    'workspace-dsh': '任务工作区 .dsh/skills',
    'workspace-agents': '任务工作区 .agents/skills',
    'custom': '自定义根',
    'user-dsh': '用户 ~/.dsh/skills',
    'user-agents': '用户 ~/.agents/skills',
    'bundled': '随程序分发',
}


def rank_label(rank, source):
    """人可读的来源名：项目根级联用同一个标签，避免控制台出现大量重复档位名。"""
    if rank >= ROOT_CHAIN and rank < BUNDLED:
        return 'project-root'
    return source


class SkillError(ValueError):
    """技能文件不可用：带文件位置的可读原因。"""


def is_skill_name(name):
    """公开的技能命名文法：kebab-case，小写字母/数字/连字符。"""
    return bool(name) and len(str(name)) <= 128 and bool(NAME_PATTERN.match(str(name)))


def parse_frontmatter(text):
    """解析 frontmatter，返回 `(字段, 正文)`；没有 frontmatter 时字段为空。"""
    match = FRONTMATTER_PATTERN.match(str(text or '').lstrip('\ufeff'))
    if not match:
        return {}, str(text or '')
    return _parse_yaml_subset(match.group(1)), str(text or '')[match.end():]


def _parse_yaml_subset(block):
    """够用的 YAML 子集：标量、行内列表、一层缩进映射/列表、块标量（`|`）、普通折行续行。

    按缩进逐行归属，而不是维护隐式状态机：取值行、缩进块、续行三种情况分开处理。
    只支持技能 frontmatter 实际会写的形状；更复杂的结构应改写技能文件，而不是猜。
    """
    entries = [line.rstrip() for line in str(block).splitlines()]
    result = {}
    index = 0
    total = len(entries)
    while index < total:
        raw = entries[index]
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            index += 1
            continue
        if ':' not in stripped:
            index += 1
            continue
        name, _, value = stripped.partition(':')
        key = name.strip()
        value = value.strip()
        index += 1
        if not key:
            continue
        if value in {'|', '|-', '|+', '>', '>-', '>+'}:
            # 块标量：吃掉后续所有缩进行（含空行），保留换行。
            block_lines = []
            while index < total and (entries[index].strip() == '' or entries[index][:1] in {' ', '\t'}):
                block_lines.append(entries[index].strip())
                index += 1
            result[key] = '\n'.join(block_lines).strip()
            continue
        if value:
            # 普通标量，可能被后续缩进的续行折行接上。
            parts = [value]
            while index < total and entries[index][:1] in {' ', '\t'} and entries[index].strip():
                continuation = entries[index].strip()
                if continuation.startswith('- ') or ':' in continuation:
                    break
                parts.append(continuation)
                index += 1
            result[key] = _scalar(' '.join(parts))
            continue
        # `key:` 后面为空：接下来要么是缩进映射，要么是缩进列表。
        child = {}
        items = []
        seen_indent = None
        while index < total:
            inner_raw = entries[index]
            inner = inner_raw.strip()
            if inner == '' or inner.startswith('#'):
                index += 1
                continue
            if inner_raw[:1] not in {' ', '\t'}:
                break
            indent = len(inner_raw) - len(inner_raw.lstrip())
            if seen_indent is None:
                seen_indent = indent
            elif indent < seen_indent:
                break
            if inner.startswith('- '):
                items.append(_scalar(inner[2:].strip()))
            elif ':' in inner:
                sub_name, _, sub_value = inner.partition(':')
                child[sub_name.strip()] = _scalar(sub_value.strip())
            else:
                items.append(_scalar(inner))
            index += 1
        if child and items:
            result[key] = child
        elif items:
            result[key] = items
        else:
            result[key] = child
    return result




def _scalar(value):
    """标量归一化：去引号、识别布尔、数字与行内列表。

    引号会**抑制**类型推断（YAML 语义）：`timeout: 300` 是数字，`timeout: "300"` 是字符串。
    不这样做的话，`invocation.timeout` 这类契约字段会以字符串到达校验，最后以"必须是正数"
    这种费解的方式拒绝整条技能。布尔只认 `true/yes/on` 与 `false/no/off`：`1`/`0` 是整数。
    """
    text = str(value or '').strip()
    quoted = len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}
    if quoted:
        text = text[1:-1]
    if text.startswith('[') and text.endswith(']'):
        return [_scalar(item) for item in text[1:-1].split(',') if item.strip()]
    if not quoted and text:
        lowered = text.lower()
        if lowered in _BOOL_TRUE:
            return True
        if lowered in _BOOL_FALSE:
            return False
        if lowered in {'null', '~'}:
            return ''
        if _INTEGER.fullmatch(text):
            try:
                return int(text)
            except ValueError:
                pass
        if _NUMBER.fullmatch(text):
            try:
                return float(text)
            except ValueError:
                pass
    return text


def _boolean(fields, key, default):
    """严格布尔：拼写不认识就报错，绝不静默放行某个调用面。"""
    if key not in fields:
        return default
    value = fields[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _BOOL_TRUE:
            return True
        if lowered in _BOOL_FALSE:
            return False
    raise SkillError(f'{key} 必须是布尔值，当前为 {value!r}')


def _metadata(fields):
    value = fields.get('metadata')
    if isinstance(value, dict):
        return {str(k): v for k, v in list(value.items())[:_METADATA_LIMIT]}
    if isinstance(value, str) and value.strip():
        return {'note': value.strip()}
    return {}


def parse_invocation(fields, skill_file):
    """解析 `invocation:` 契约；没有声明就返回 None（纯指令技能，不可被端口调用）。

    契约写在技能里，运行时不猜：`kind: bridge` 表示"用外部进程执行",
    `bridge` 是相对技能目录的脚本路径，`batch` 声明它是否接受一次性多份输入。
    声明的值不合法就拒绝整条技能——一个说错自己怎么被调用的技能比没有契约更危险。
    """
    raw = fields.get('invocation')
    if raw in (None, '', {}):
        return None
    if not isinstance(raw, dict):
        raise SkillError(f'invocation 必须是映射，当前为 {raw!r}')
    kind = str(raw.get('kind') or '').strip()
    if kind not in INVOCATION_KINDS:
        raise SkillError(f'invocation.kind 必须是 {sorted(INVOCATION_KINDS)} 之一，当前为 {kind!r}')
    bridge = str(raw.get('bridge') or '').strip()
    if not bridge:
        raise SkillError('invocation.kind=bridge 时必须提供 invocation.bridge（相对技能目录的脚本路径）')
    directory = skill_file.parent
    candidate = (directory / bridge).resolve()
    if not candidate.is_relative_to(directory):
        raise SkillError(f'invocation.bridge 必须位于技能目录内：{bridge!r}')
    if not candidate.is_file():
        raise SkillError(f'invocation.bridge 指向的文件不存在：{bridge!r}')
    batch = _boolean({'batch': raw.get('batch')}, 'batch', False) if 'batch' in raw else False
    timeout = raw.get('timeout', DEFAULT_INVOKE_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise SkillError(f'invocation.timeout 必须是正数，当前为 {timeout!r}')
    return {'kind': kind, 'bridge': bridge, 'path': str(candidate), 'batch': bool(batch),
            'timeout': min(float(timeout), MAX_INVOKE_TIMEOUT)}


def project_root_of(workspace):
    """在没有显式项目根时，找最近的含 `.git` 的祖先目录。

    子任务的工作副本位于数据目录里，它的物理祖先跟真实项目毫无关系；因此调用方应当传入
    任务记录里的项目根。这个函数只是兜底，语义与 DSH 的"项目根 = 最近的 .git 祖先"一致。
    """
    if workspace is None:
        return None
    try:
        current = Path(workspace).expanduser().resolve()
    except OSError:
        current = Path(workspace).expanduser().absolute()
    if current.is_file():
        current = current.parent
    node = current
    for _ in range(ROOT_WALK_LIMIT):
        if (node / '.git').exists():
            return node
        parent = node.parent
        if parent == node:
            break
        node = parent
    return None


def roots_for(workspace=None, home=None, custom=(), bundled=None, project_root=None):
    """按 rank 升序给出扫描根：项目侧、然后是自定义、用户、内置。

    项目侧有两级，顺序即优先级：

    1. **工作区自己的技能根**：子任务工作副本里的 `.dsh/skills` 能覆盖项目级技能；
    2. **项目根及其祖先的技能根**：项目根是显式传入的（任务记录里的 `project_root`），
       缺失时回退到最近的 `.git` 祖先，再缺失就退回工作区自身。

    只扫到项目根为止，不继续向上撞系统目录；根目录去重，避免同一路径被扫两次。
    """
    entries = []
    seen = set()

    def add(rank, path, base=None):
        if path is None:
            return
        resolved = Path(path).expanduser()
        try:
            resolved = resolved.resolve()
        except OSError:
            resolved = Path(path).expanduser().absolute()
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        entries.append({'rank': rank, 'source': rank_label(rank, RANK_NAMES.get(rank, str(rank))),
                        'root': resolved, 'base': base})

    def absolute(value):
        try:
            return Path(value).expanduser().resolve()
        except OSError:
            return Path(value).expanduser().absolute()

    home = Path(home).expanduser().resolve() if home is not None else Path.home()
    if bundled is not None:
        add(BUNDLED, bundled)
    workspace_path = absolute(workspace) if workspace is not None else None
    if workspace_path is not None:
        add(PROJECT_DSH, workspace_path / '.dsh' / 'skills', base=workspace_path)
        add(PROJECT_AGENTS, workspace_path / '.agents' / 'skills', base=workspace_path)
    root = absolute(project_root) if project_root else (project_root_of(workspace_path) if workspace_path else None)
    if root is not None and (workspace_path is None or root != workspace_path):
        chain = []
        node = root
        for _ in range(ROOT_WALK_LIMIT):
            chain.append(node)
            parent = node.parent
            if parent == node:
                break
            node = parent
        for depth, base in enumerate(chain):
            add(ROOT_CHAIN + depth * 10, base / '.dsh' / 'skills', base=base)
            add(ROOT_CHAIN + depth * 10 + 5, base / '.agents' / 'skills', base=base)
    for index, custom_root in enumerate(custom or ()):
        add(min(CUSTOM_DEFAULT + index, CUSTOM_MAX), custom_root)
    add(USER_DSH, home / '.dsh' / 'skills')
    add(USER_AGENTS, home / '.agents' / 'skills')
    return sorted(entries, key=lambda item: item['rank'])


def _entry_state(path):
    """根的轻量指纹：只 stat 顶层条目，用于判断目录是否变化。"""
    record = []
    try:
        items = sorted(os.scandir(path), key=lambda item: item.name)[:_STATE_LIMIT]
    except OSError:
        return None
    for item in items:
        try:
            info = item.stat()
            record.append((item.name, item.is_dir(), info.st_mtime_ns, info.st_size))
        except OSError:
            record.append((item.name, item.is_dir(), 0, 0))
    return record


def _load_candidate(spec, directory, skill_file):
    """读取一个候选技能；任何格式问题都返回带位置的错误，而不是抛出中断整个目录。"""
    try:
        raw = skill_file.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f'读取失败：{exc}') from None
    fields, body = parse_frontmatter(raw)
    if not fields:
        raise SkillError('缺少 YAML frontmatter（必须以 --- 开头）')
    name = str(fields.get('name') or '').strip()
    if not name:
        raise SkillError('frontmatter 缺少 name')
    if not is_skill_name(name):
        raise SkillError(f'name 不是合法的 kebab-case：{name!r}')
    if not directory and name != skill_file.stem:
        raise SkillError(f'扁平技能的 name ({name}) 必须与文件名 ({skill_file.stem}) 一致')
    if directory and name != directory.name:
        raise SkillError(f'目录包技能的 name ({name}) 必须与目录名 ({directory.name}) 一致')
    description = str(fields.get('description') or '').strip()
    if not description:
        raise SkillError('frontmatter 缺少 description')
    when_to_use = str(fields.get('when-to-use') or fields.get('whenToUse') or '').strip()
    model_invocable = _boolean(fields, 'disable-model-invocation', False) is False
    user_invocable = _boolean(fields, 'user-invocable', True)
    invocation = parse_invocation(fields, skill_file)
    return {
        'name': name,
        'description': description,
        'when_to_use': when_to_use,
        'model_invocable': model_invocable,
        'user_invocable': user_invocable,
        'rank': spec['rank'],
        'source': spec['source'],
        'root': str(spec['root']),
        'base': str(spec['base']) if spec.get('base') is not None else str(directory or skill_file.parent),
        'path': str(skill_file),
        'content': body.lstrip('\n'),
        'metadata': _metadata(fields),
        'invocation': invocation,
        'invocable': invocation is not None,
        'batch': bool(invocation and invocation['batch']),
    }


def discover(workspace=None, home=None, custom=(), bundled=None, project_root=None):
    """扫描所有根，返回 `(胜出技能列表, 诊断列表)`。

    同名技能按 rank 竞争，只有胜出者进入目录；落选者与坏文件都记入诊断，便于控制台核对。
    """
    winners = {}
    diagnostics = []
    scanned = []
    for spec in roots_for(workspace=workspace, home=home, custom=custom, bundled=bundled,
                          project_root=project_root):
        root = spec['root']
        if not root.is_dir():
            continue
        scanned.append(str(root))
        try:
            items = sorted(os.scandir(root), key=lambda item: item.name)
        except OSError as exc:
            diagnostics.append({'root': str(root), 'source': spec['source'], 'error': f'目录不可读：{exc}'})
            continue
        for item in items:
            name = item.name
            if name.startswith('.'):
                continue
            if item.is_dir():
                skill_file = Path(item.path) / 'SKILL.md'
                if not skill_file.is_file():
                    continue
            elif name.endswith('.md'):
                skill_file = Path(item.path)
            else:
                continue
            try:
                candidate = _load_candidate(spec, Path(item.path) if item.is_dir() else None, skill_file)
            except SkillError as exc:
                diagnostics.append({'path': str(skill_file), 'source': spec['source'], 'error': str(exc)})
                continue
            except Exception as exc:  # 单个坏技能不能让整个目录消失
                diagnostics.append({'path': str(skill_file), 'source': spec['source'], 'error': f'{type(exc).__name__}: {exc}'})
                continue
            current = winners.get(candidate['name'])
            if current is None:
                winners[candidate['name']] = candidate
                continue
            loser = candidate if candidate['rank'] >= current['rank'] else current
            winner = current if loser is candidate else candidate
            winners[candidate['name']] = winner
            diagnostics.append({'path': loser['path'], 'source': loser['source'], 'error':
                                f'同名技能 {candidate["name"]!r} 由更高优先级的 {winner["source"]} 覆盖'})
    ordered = sorted(winners.values(), key=lambda item: item['name'])
    return ordered, diagnostics, scanned


def render_catalog(skills, base_dirs=None):
    """会话目录消息：只含名称与摘要，明确要求先加载再执行。"""
    entries = [f'- `{item["name"]}`: {_escape_text(item["description"])}' for item in skills]
    lines = [
        '<system-reminder>',
        '技能（skill）是一组可复用的任务专用指令。本会话可用技能：',
        '',
        '<available_skills>',
        *entries,
        '</available_skills>',
        '',
    ]
    if entries:
        lines.append('当用户点名某个技能，或任务明显匹配某个技能的描述时，先用 `skill` 工具按精确名称加载它，再开始动作。'
                     '可以一次加载多个相关技能，并完整遵循其指令。这里只有摘要，未加载前不要凭名称猜测技能内容。')
    else:
        lines.append('当前没有可用技能。不要使用更早目录里的技能名。')
    if base_dirs:
        lines.append('技能包按目录提供时，正文里的相对路径都以该技能的 base directory 为基准解析。')
    lines += [
        '技能正文是项目里的普通文件；如果需要新增或修改技能，直接改对应目录下的 SKILL.md（与 scripts/ 资源）。',
        '</system-reminder>',
    ]
    return '\n'.join(lines)


def render_content(skill):
    """与 DSH 一致的 `<skill_content>` 渲染，工具结果与会话注入共用同一形状。"""
    base = skill.get('base')
    if base:
        resources = [f'Base directory for this skill: {_escape_text(base)}',
                     'Resolve relative paths mentioned by this skill against the base directory before using them. '
                     'Load referenced resources only as needed.']
    else:
        resources = [f'Resources for this skill are managed by provider "{_escape_text(skill.get("source", ""))}".',
                     'Load referenced resources only as needed.']
    return '\n'.join([
        f'<skill_content name="{_escape_attr(skill.get("name", ""))}">',
        '<skill_resources>',
        *resources,
        '</skill_resources>',
        '',
        '<skill_instructions>',
        skill.get('content', ''),
        '</skill_instructions>',
        '</skill_content>',
    ])


def _escape_text(value):
    return str(value or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _escape_attr(value):
    return _escape_text(value).replace('"', '&quot;')


def catalog_revision(skills):
    """目录指纹：名称 + 描述 + 来源路径的内容哈希，用于判断"可用技能是否变了"。"""
    material = '\n'.join(f'{item["name"]}\0{item["description"]}\0{item["path"]}' for item in skills)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


class Skills:
    """技能目录：带状态缓存的发现与按需加载。线程内使用，不做跨线程同步。"""

    def __init__(self, home=None, custom=(), bundled=None):
        self.home = home
        self.custom = tuple(custom or ())
        self.bundled = bundled
        self._cache = {}
        self.catalog_revision = ''

    def _key(self, workspace, project_root=None):
        return f'{Path(workspace).expanduser() if workspace else ""}\0{project_root or ""}'

    def _state(self, workspace, project_root=None):
        state = []
        for spec in roots_for(workspace=workspace, home=self.home, custom=self.custom, bundled=self.bundled,
                              project_root=project_root):
            state.append((spec['rank'], str(spec['root']), _entry_state(spec['root'])))
        return state

    def project_root(self, workspace, explicit=None):
        """项目根：优先用调用方传入的（任务记录里的字段），否则回退到最近的 .git 祖先。"""
        if explicit:
            return str(explicit)
        found = project_root_of(workspace)
        return str(found) if found else ''

    def snapshot(self, workspace=None, refresh=False, project_root=None):
        """返回 `{skills, diagnostics, roots, revision}`；目录未变化时复用上次结果。"""
        key = self._key(workspace, project_root)
        state = self._state(workspace, project_root)
        cached = self._cache.get(key)
        if not refresh and cached and cached['state'] == state:
            self.catalog_revision = cached['value']['revision']
            return cached['value']
        skills, diagnostics, scanned = discover(workspace=workspace, home=self.home,
                                                custom=self.custom, bundled=self.bundled,
                                                project_root=project_root)
        value = {'skills': skills, 'diagnostics': diagnostics, 'roots': scanned,
                 'revision': catalog_revision(skills)}
        self._cache[key] = {'state': state, 'value': value}
        self.catalog_revision = value['revision']
        return value

    def catalog(self, workspace=None, refresh=False, project_root=None):
        """面向模型的技能（`disable-model-invocation` 的技能被排除）。"""
        return [item for item in self.snapshot(workspace, refresh, project_root)['skills']
                if item['model_invocable']]

    def load(self, name, workspace=None, refresh=False, project_root=None):
        """按名称加载完整技能，附带正文与资源基目录。"""
        wanted = str(name or '').strip()
        if not is_skill_name(wanted):
            raise SkillError(f'技能名不合法：{wanted!r}；必须是 kebab-case')
        snapshot = self.snapshot(workspace, refresh, project_root)
        for item in snapshot['skills']:
            if item['name'] == wanted:
                if not item['model_invocable']:
                    raise SkillError(f'技能 {wanted} 不允许由模型加载（disable-model-invocation）')
                return item
        available = ', '.join(sorted(item['name'] for item in snapshot['skills'])) or '（无）'
        raise SkillError(f'未找到技能 {wanted}；当前可用：{available}')

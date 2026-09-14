"""技能调用器：把技能当成**可通过端口调用的能力**，形式与模型调用一致。

为什么需要它：技能正文里的脚本通过 `workspace_shell` 调用是同步阻塞的——
一个 20 秒的批量出图请求会占住整个任务循环。技能调用器把同一件事变成
"提交请求 → 立刻拿到 `job_id` → 轮询结果"，与模型调用、`/api/wait` 的形状保持一致：

    提交：POST /api/skills/call   {"skill":"x","args":{…}}            → 立即结果或 job
    批量：POST /api/skills/call   {"skill":"x","batch":[{…},{…}]}     → 同一份契约，多份输入
    轮询：GET  /api/skill/jobs/<id>                                   → 单次与批量同形

桥接协议（`invocation.kind: bridge`）刻意做得极简，任何语言都能实现：

    stdin  ← {"skill": name, "workspace": path, "args": {...}}        # batch 时 args 为数组
    stdout → {"ok": true, "result": {...}}                            # 或 {"ok": false, "error": "..."}

桥接脚本在**独立进程**里执行，因此：输出不会污染运行时、超时可强制终止、
技能可以用任意语言实现。代价是它继承本机权限——技能与本机代码同等可信。

作业状态持久化在 `skill_job` 记录里，因此轮询不依赖内存状态；进程重启后仍可查询
（`running` 的作业在启动时被标记为 `interrupted`，与在途工具调用同一处理原则：不盲目重放）。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

from .skills import MAX_BATCH, SkillError
from .store import uid, now

MAX_CONCURRENT_JOBS = 4
DEFAULT_WAIT = 30.0
MAX_WAIT = 900.0
MAX_OUTPUT = 4 * 1024 * 1024
TERMINAL_JOB_STATUS = {'succeeded', 'failed', 'interrupted'}


class SkillInvocationError(ValueError):
    """调用被拒绝：参数、契约或技能状态问题，属于调用方错误（400）。"""


class SkillInvoker:
    """执行技能桥接脚本并维护作业记录。运行在事件循环线程内，子进程是唯一阻塞点。"""

    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        self._tasks = {}

    # ---------------------------------------------------------------- 查询

    def job(self, job_id):
        try:
            return self.store.get('skill_job', job_id)
        except ValueError:
            raise SkillInvocationError(f'未知技能作业：{job_id}') from None

    def jobs(self, limit=50):
        items = sorted(self.store.all('skill_job'), key=lambda item: item.get('created', 0), reverse=True)
        return items[:limit]

    def pending(self):
        """在途作业；供运行时在关闭时收尾。"""
        return [task for task in self._tasks.values() if not task.done()]

    def recover(self):
        """启动时处理上次运行遗留的作业：结果未知，不重放，明确标记为中断。"""
        for job in self.store.all('skill_job'):
            if job.get('status') in {'queued', 'running'}:
                job.update(status='interrupted', finished=now(),
                           error='上次运行在技能执行中中断；结果未知，不会自动重放。')
                self.store.put('skill_job', job)
                self.store.event('SkillJobInterrupted', {'id': job['id'], 'skill': job['name']},
                                 run_id=job.get('run_id'))

    # ---------------------------------------------------------------- 提交

    def submit(self, name, args=None, batch=None, workspace=None, project_root=None, timeout=None):
        """校验并提交一次调用；返回作业记录（可能已经完成）。"""
        skill = self._resolve(name, workspace, project_root)
        invocation = skill['invocation']
        items = self._normalize_requests(skill, args, batch)
        job_timeout = min(float(timeout or invocation['timeout']), invocation['timeout'])
        job = {'id': uid('skilljob_'), 'name': skill['name'], 'source': skill['source'],
               'workspace': str(workspace or ''), 'bridge': invocation['path'],
               'batch': batch is not None, 'count': len(items), 'status': 'queued',
               'created': now(), 'started': None, 'finished': None,
               'timeout': job_timeout, 'result': None, 'error': None, 'results': None}
        self.store.put('skill_job', job)
        self.store.event('SkillInvocationSubmitted', {'id': job['id'], 'skill': skill['name'],
                                                      'count': len(items), 'batch': job['batch']})
        task = asyncio.ensure_future(self._run(job, skill, items))
        self._tasks[job['id']] = task
        task.add_done_callback(lambda _future, key=job['id']: self._tasks.pop(key, None))
        return job

    async def call(self, name, args=None, batch=None, workspace=None, project_root=None,
                   timeout=None, wait=DEFAULT_WAIT):
        """提交并按需等待；窗口内完成就直接返回结果，否则返回 job 供调用方轮询。"""
        job = self.submit(name, args=args, batch=batch, workspace=workspace,
                          project_root=project_root, timeout=timeout)
        window = max(0.0, min(float(wait or 0), MAX_WAIT))
        if window:
            await self._wait_for(job['id'], window)
        return self.job(job['id'])

    async def _wait_for(self, job_id, window):
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            job = self.job(job_id)
            if job['status'] in TERMINAL_JOB_STATUS:
                return job
            await asyncio.sleep(min(0.2, max(0.02, deadline - time.monotonic())))
        return self.job(job_id)

    def _resolve(self, name, workspace, project_root):
        wanted = str(name or '').strip()
        if not wanted:
            raise SkillInvocationError('缺少 skill 名称')
        try:
            skill = self.runtime.skills.load(wanted, workspace, project_root=project_root)
        except SkillError as exc:
            raise SkillInvocationError(str(exc)) from None
        if not skill['invocable']:
            raise SkillInvocationError(
                f'技能 {wanted} 没有声明 invocation 契约，只能作为指令加载（用 skill 工具），不能通过端口调用')
        if skill['invocation']['kind'] != 'bridge':
            raise SkillInvocationError(f"技能 {wanted} 的 invocation.kind 不受支持：{skill['invocation']['kind']!r}")
        return skill

    @staticmethod
    def _normalize_requests(skill, args, batch):
        """单次与批量归一化为同一份请求列表；批量上限是显式拒绝，不是静默截断。"""
        if batch is None:
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise SkillInvocationError('args 必须是 JSON 对象')
            return [args]
        if not skill['batch']:
            raise SkillInvocationError(f"技能 {skill['name']} 没有声明 invocation.batch，不接受批量输入")
        if not isinstance(batch, list) or not batch:
            raise SkillInvocationError('batch 必须是非空数组')
        if len(batch) > MAX_BATCH:
            raise SkillInvocationError(f'批量上限为 {MAX_BATCH} 份，收到 {len(batch)} 份（请拆成多次调用）')
        for index, item in enumerate(batch):
            if not isinstance(item, dict):
                raise SkillInvocationError(f'batch[{index}] 必须是 JSON 对象')
        return list(batch)

    # ---------------------------------------------------------------- 执行

    async def _run(self, job, skill, items):
        async with self._semaphore:
            job = self.job(job['id'])
            if job['status'] != 'queued':
                return
            job.update(status='running', started=now())
            self.store.put('skill_job', job)
            payload = {'skill': skill['name'], 'workspace': job['workspace'],
                       'base': skill['base'], 'args': items[0] if len(items) == 1 and not job['batch'] else items}
            try:
                raw = await asyncio.to_thread(self._invoke_bridge, skill, payload, job['timeout'])
                results = self._parse_output(skill, raw, job['batch'] or len(items) > 1)
            except Exception as exc:
                job = self.job(job['id'])
                job.update(status='failed', finished=now(), error=f'{type(exc).__name__}: {exc}' if not isinstance(exc, SkillInvocationError) else str(exc))
                self.store.put('skill_job', job)
                self.store.event('SkillInvocationFailed', {'id': job['id'], 'skill': job['name'],
                                                           'error': job['error']})
                return
            job = self.job(job['id'])
            job.update(status='succeeded', finished=now(),
                       results=results if job['batch'] or len(results) > 1 else None,
                       result=results[0] if len(results) == 1 else None)
            self.store.put('skill_job', job)
            self.store.event('SkillInvocationCompleted', {'id': job['id'], 'skill': job['name'],
                                                          'count': len(results)})

    def _invoke_bridge(self, skill, payload, timeout):
        """在独立进程里执行桥接脚本，stdin 传 JSON、stdout 收 JSON。"""
        completed = subprocess.run(
            [sys.executable, skill['invocation']['path']],
            input=json.dumps(payload, ensure_ascii=False).encode(),
            cwd=skill['base'], capture_output=True, timeout=timeout)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or b'').decode('utf-8', 'replace').strip()
            raise SkillInvocationError(
                f"桥接脚本退出码 {completed.returncode}" + (f'：{detail[-600:]}' if detail else ''))
        if len(completed.stdout) > MAX_OUTPUT:
            raise SkillInvocationError(f'桥接脚本输出超过 {MAX_OUTPUT // (1024 * 1024)} MiB 上限')
        text = completed.stdout.decode('utf-8', 'replace').strip()
        if not text:
            raise SkillInvocationError('桥接脚本没有输出 JSON')
        return text

    @staticmethod
    def _parse_output(skill, text, many):
        """只接受 `{"ok": true/false, ...}`，不解析自由文本：契约要能机械判定。"""
        try:
            data = json.loads(text.splitlines()[-1] if '\n' in text else text)
        except ValueError:
            raise SkillInvocationError(f'桥接脚本输出不是 JSON：{text[:200]!r}') from None
        if not isinstance(data, dict) or 'ok' not in data:
            raise SkillInvocationError('桥接脚本输出的 JSON 必须包含 ok 字段')
        if not data['ok']:
            raise SkillInvocationError(str(data.get('error') or '技能执行失败'))
        if 'results' in data and isinstance(data['results'], list):
            return data['results']
        if many and isinstance(data.get('result'), list):
            return data['result']
        return [data.get('result', {k: v for k, v in data.items() if k != 'ok'})]

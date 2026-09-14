from __future__ import annotations
import argparse
import asyncio
import contextlib
import json
import mimetypes
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from .runtime import Runtime, TERMINAL
from .store import dumps, uid, now
from .models import HEALTH_OK_TTL, HEALTH_TRANSIENT_LIMIT
from .skills import SkillError, render_content
from .skill_runtime import SkillInvocationError, DEFAULT_WAIT, MAX_WAIT
from .config import import_provider_config, load_builtin_config, scan_provider
from . import providers

WEB = Path(__file__).resolve().parent/'web'


class AuthError(PermissionError):
    """缺少或错误的外部调用令牌。"""


class App:
    def __init__(self,root,plugins=(),token=None,auth_required=True,workspace_root=None):
        self.loop=asyncio.new_event_loop()
        self.ready=threading.Event()
        self.root=Path(root).expanduser().resolve()
        self.plugins=plugins
        self.workspace_root=str(Path(workspace_root).expanduser().resolve()) if workspace_root else None
        self.token=self._load_token(token)
        # 本机单人使用时可以显式关闭鉴权（`--no-auth`）。默认**要求**鉴权：
        # 让"不需要令牌"成为一个被写下来的选择，而不是把 authenticate 清空。
        self.auth_required=bool(auth_required)
        self.closed=False
        self.thread=threading.Thread(target=self.worker,daemon=True)
        self.startup_error=None
        self.thread.start(); self.ready.wait()
        if self.startup_error: raise RuntimeError('运行时启动失败：'+str(self.startup_error)) from self.startup_error

    def _load_token(self,token):
        """外部程序调用所需的令牌：显式传入 > UNISON_TOKEN > 数据目录 api_token（首次自动生成）。"""
        token=str(token or os.environ.get('UNISON_TOKEN','')).strip()
        if token:
            return token
        path=self.root/'api_token'
        try:
            existing=path.read_text().strip()
        except (OSError, ValueError):
            existing=''
        if existing:
            return existing
        generated=secrets.token_hex(24)
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(generated+'\n')
            path.chmod(0o600)
        except OSError:
            pass
        return generated

    def authenticate(self,headers):
        """鉴权边界：同源控制台免令牌，其它来源必须带令牌；`auth_required=False` 时全部放行。

        同源判定的用意是"本机浏览器打开的控制台不用手填令牌"；它**不是**进程级隔离——
        本机其它程序可以伪造 `Origin`。关闭鉴权意味着**同机任何进程都能调用全部端点**，
        因此默认开启，只在明确的本机单人场景用 `--no-auth` 关掉。
        """
        if not self.auth_required:
            return
        origin=headers.get('Origin')
        if origin and urlparse(origin).netloc==headers.get('Host'):
            return
        supplied=''
        authorization=headers.get('Authorization') or ''
        if authorization.lower().startswith('bearer '):
            supplied=authorization[7:].strip()
        if not supplied:
            supplied=(headers.get('X-Unison-Token') or '').strip()
        if not self.token or not secrets.compare_digest(supplied,self.token):
            raise AuthError('需要 API 令牌：Authorization: Bearer <token>；令牌见数据目录 api_token，或用 --api-token / UNISON_TOKEN 指定')

    def worker(self):
        try:
            self._worker()
        except Exception as exc:
            self.startup_error=exc
            if hasattr(self,'runtime'): self.runtime.store.close()
            self.ready.set()

    def _worker(self):
        asyncio.set_event_loop(self.loop)
        self.runtime=Runtime(self.root)
        from .demo import demo_call
        self.runtime.models.adapters['demo']=demo_call
        for module in self.plugins: self.runtime.load_plugin(module)
        # 启动时自动加载内置配置；失败时保留原因供控制台显示，不再静默隐藏默认模型。
        self.builtin_error=None
        try:
            self.builtin_config=load_builtin_config(self.runtime.store, self.runtime.models)
        except Exception as exc:
            self.builtin_config=None; self.builtin_error=str(exc)
            print(f'内置配置加载失败：{exc}',flush=True)
        self.loop.run_until_complete(self.runtime.start())
        self.ready.set(); self.loop.run_forever()

    def default_workspace(self):
        """新任务的默认工作区。

        真机发现（ink-duel 复核）：默认值曾经取 `<数据目录>/playground`，于是"项目的根"
        落在**运行时的数据目录**里。后果不是美观问题：

        - 子任务工作副本会把根工作区下面**所有**历史产物一起复制过去
          （sword-demo 的中间态跟着 ink-duel 的任务跑，素材任务的基线里全是别的项目）；
        - 技能的项目级根、知识的作用域、外部编辑器的工作位置全部悬空。

        规则：默认工作区必须与数据目录分开。`--workspace-root` 可显式指定；
        否则用数据目录同级目录（好找，且不被归档与 GC 牵连）。
        """
        explicit = getattr(self, 'workspace_root', None)
        if explicit:
            path = Path(explicit).expanduser()
        else:
            data = Path(self.root).expanduser()
            path = (data.parent / (data.name + '-workspace')) if data.name else Path.cwd()
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def call(self,method,path,data=None,query=None,timeout=60,internal=False):
        """internal=True 供服务自身复用（例如 SSE 循环），鉴权已在 HTTP 层完成。"""
        return asyncio.run_coroutine_threadsafe(self.dispatch(method,path,data or {},query or {}),self.loop).result(timeout=timeout)

    def _skill_project_root(self,workspace,run_id=None):
        """项目根取自运行记录（根任务的工作区），保证控制台看到的技能与任务真正加载的一致。"""
        if run_id:
            try:
                run=self.runtime.run(run_id)
                return self.runtime.task_project_root(self.runtime.task(run['root_task']))
            except ValueError:
                pass
        return self.runtime.skills.project_root(workspace)

    async def dispatch(self,method,path,data,q):
        r=self.runtime; s=r.store
        if method=='GET' and path=='/api/state':
            tasks=[{k:v for k,v in t.items() if k not in {'history','base_manifest','manifest'}} for t in s.all('task')]
            configs=s.all('app_config')
            playground=self.default_workspace()
            return {'runs':s.all('run'),'tasks':tasks,'models':r.models.list(),'questions':s.all('question'),
                    'app_config':configs[-1] if configs else None,
                    'cursor':s.cursor(),'data_dir':str(s.root),'default_workspace':playground,
                    'skill_catalog_revision':r.skills.catalog_revision,
                    'skill_count':len(r.skill_snapshot(playground,r.skills.project_root(playground))['skills'])}
        if method=='GET' and path=='/api/skills':
            # 控制台按项目位置查看技能：工作区不同，项目级技能目录就不同。
            workspace=q.get('workspace',[None])[0] or self.default_workspace()
            project_root=self._skill_project_root(workspace,q.get('run_id',[None])[0])
            snapshot=r.skill_snapshot(workspace,project_root)
            diagnostics=list(snapshot['diagnostics'])
            return {'workspace':workspace,'project_root':project_root,
                    'skills':snapshot['skills'],'diagnostics':diagnostics,
                    'roots':snapshot['roots'],'revision':snapshot['revision'],
                    'bundled_dir':str(r.skills.bundled) if r.skills.bundled else ''}
        if method=='GET' and path=='/api/skill/jobs':
            # 作业列表：单次与批量同形，便于外部程序与模型查看自己提交过什么。
            limit=min(200,max(1,int(q.get('limit',[50])[0])))
            return {'jobs':r.skill_invoker.jobs(limit)}
        if method=='GET' and path.startswith('/api/skill/jobs/'):
            job=r.skill_invoker.job(path.rsplit('/',1)[-1])
            return {'job':job}
        if method=='GET' and path=='/api/skill':
            workspace=q.get('workspace',[None])[0] or self.default_workspace()
            refresh=q.get('refresh',[''])[0] not in {'','0','false'}
            project_root=self._skill_project_root(workspace,q.get('run_id',[None])[0])
            try:
                skill=r.skills.load(q['name'][0],workspace,refresh=refresh,project_root=project_root)
            except SkillError as exc:
                raise ValueError(str(exc)) from None
            return {'skill':skill,'content':render_content(skill),'project_root':project_root}
        if method=='GET' and path=='/api/events':
            # Console fetches a bounded window; filtering by type keeps payloads small.
            limit=min(20000,max(1,int(q.get('limit',[1000])[0])))
            events=s.events(q.get('run_id',[None])[0],int(q.get('after',[0])[0]),limit)
            wanted={x for x in (q.get('types',[''])[0] or '').split(',') if x}
            if wanted: events=[e for e in events if e['type'] in wanted]
            return events
        if method=='GET' and path=='/api/replay':
            run_id=q['run_id'][0]; until=int(q['until'][0])
            events=[e for e in s.events(run_id,limit=1000000) if e['seq']<=until]
            tasks={}
            changes={'TaskSubmitted':'queued','TaskClaimed':'running','TaskQueued':'queued','WaitRegistered':'waiting','QuestionRaised':'waiting','TaskWoken':'queued','TaskCompleted':'completed','TaskFailed':'failed','TaskCancelled':'cancelled','TaskSuperseded':'superseded'}
            for event in events:
                id=event['task_id']
                if event['type']=='TaskSubmitted':
                    original=r.task(id); tasks[id]={k:v for k,v in original.items() if k in {'id','goal','parent_id','model_id','revision','created'}}
                if id in tasks and event['type'] in changes: tasks[id]['status']=changes[event['type']]
            return {'tasks':list(tasks.values()),'until':until}
        if method=='GET' and path=='/api/task':
            t=r.task(q['id'][0]); r.workspace.reconcile(t)
            # 历史是日志的派生结果，不来自任务记录（记录里只留 history_messages 计数）。
            return {'task':{k:v for k,v in t.items() if k not in {'base_manifest','manifest'}},
                    'history':r.task_history(t),
                    'messages':[m for m in s.all('message') if m['task_id']==t['id']],
                    'reports':[x for x in s.all('report') if x['task_id']==t['id']],
                    'changes':r.workspace.changes(t),'events':[e for e in s.events(t['run_id'],limit=100000) if e['task_id']==t['id']][-200:]}
        if method=='GET' and path=='/api/files':
            run_id=q['run_id'][0]
            changes=[]; omitted=[]
            for t in s.all('task'):
                if t['run_id']==run_id:
                    r.workspace.reconcile(t)
                    fresh=r.task(t['id'])
                    changes.extend([x|{'task_id':t['id'],'revision':t['revision'],'workspace':t['workspace']} for x in r.workspace.changes(fresh)])
                    omitted.extend({'task_id':t['id'],'workspace':fresh['workspace'],'path':item} for item in fresh.get('omitted_files',[]))
            history=[x for x in s.all('file') if x['run_id']==run_id]
            return {'changes':changes,'omitted_files':omitted,'history':history,'conflicts':[x for x in s.all('conflict') if x['run_id']==run_id]}
        if method=='GET' and path=='/api/knowledge':
            run=r.run(q['run_id'][0]); me=r.task(run['root_task'])
            scope=q.get('scope',[None])[0]
            if q.get('from_seq') or scope:
                # 有序枚举：`knowledge_search` 是按相关度检索并默认截断的，两个人搜同一个词
                # 也可能拿到不同切片；要核对"所有人是不是看到同一份"必须用这一条。
                entries=r.knowledge_entries(me,scope=scope or 'run',
                                            from_seq=int(q.get('from_seq',['0'])[0] or 0),
                                            limit=int(q.get('limit',['0'])[0] or 0))
                return {'scope':scope or 'run','entries':entries,
                        'divergence':r.knowledge_divergence(run['id'])}
            return r.knowledge_search(q.get('query',[''])[0],me)
        if method=='GET' and path=='/api/providers':
            configs=s.all('app_config')
            return {'providers':r.models.providers(),'unmanaged':providers.unmanaged_models(s,r.models),
                    'app_config':configs[-1] if configs else None,'import_error':self.builtin_error}
        if method=='GET' and path=='/api/object':
            text=s.read_blob(q['ref'][0]).decode(errors='replace')
            start=max(0,int(q.get('start',[0])[0])); return {'content':text[start:start+20000],'total':len(text)}
        if method=='GET' and path=='/api/requests':
            # 请求信封：回答"通过的是哪一次请求、当时的系统提示词与工具 schema 是什么"。
            return {'requests':r.request_headers(q.get('task_id',[None])[0],q.get('run_id',[None])[0],
                                                 min(500,max(1,int(q.get('limit',[100])[0]))))}
        if method=='GET' and path=='/api/request':
            header=s.get('request',q['id'][0])
            result={'header':header,'assembled':r.assemble_request(header)}
            if (q.get('restore',[''])[0] or '') not in {'','0','false'}:
                # 把该次请求的输入还原到"压缩之前"的完整历史，用于核对摘要是否丢掉约束。
                result['restored']=r.restore_history(header['input_ref'])
            return result
        if method=='GET' and path=='/api/maintenance':
            # 所有周期性/按需维护的统一视图：上次结果、下次到期、连续失败次数。
            return {'jobs':r.maintenance.status(),'tick_seconds':0.1,
                    'note':'破坏性作业默认只预演；显式触发才真正删除'}
        if method=='GET' and path=='/api/models/health':
            # 模型健康只读视图：谁能用、证据来自真实调用还是探测、什么时候过期、需不需要复验。
            view=r.models.health_view()
            return {'models':view,
                    'summary':{'total':len(view),
                               'ok':len([x for x in view if x['state']=='ok']),
                               'needs_probe':len([x for x in view if x['needs_probe']]),
                               'blocked':len([x for x in view if x['state']=='blocked'])},
                    'ok_ttl_seconds':HEALTH_OK_TTL,'transient_limit':HEALTH_TRANSIENT_LIMIT}
        if method=='GET' and path=='/api/load':
            # 负载与额度视图：现在谁在途、谁在排队、这个窗口用了多少、退避了几次。
            run_id=q.get('run_id',[None])[0]
            buckets=r.models.limiter.snapshot()
            return {'buckets':buckets,
                    'in_flight':sum(b['in_flight'] for b in buckets),
                    'queued':sum(b['queued'] for b in buckets),
                    'retries':sum(b['retries'] for b in buckets),
                    'throttled':sum(b['throttled'] for b in buckets),
                    'usage':r.models.limiter.usage_summary(run_id),
                    'note':'并发/窗口额度按 provider+模型 分桶；未声明额度时同桶并发为 1（最保守）。'}
        if method=='GET' and path=='/api/models/credentials':
            # 供外部程序与模型自行取用：明文凭据。默认控制台不用它，控制台只显示"已配置"。
            wanted=[x for x in (q.get('model_ids',[''])[0] or '').split(',') if x]
            if not wanted:
                wanted=[m['id'] for m in r.models.list()]
            models=[]
            for model_id in wanted:
                config=s.get('model',model_id)
                models.append({'id':config['id'],'model':config.get('model'),'provider_id':config.get('provider_id'),
                               'base_url':config.get('base_url'),'api':providers.api_of(config),
                               'adapter':providers.adapter_of(config),'api_key':r.model_api_key(config),
                               'key_env':config.get('key_env',''),'no_key':bool(config.get('no_key')),
                               'headers':dict(config.get('headers') or {}),
                               'disable_response_storage':bool(config.get('disable_response_storage',False)),
                               'context_window':config.get('context_window'),'max_output':config.get('max_output')})
            return {'models':models,'note':'凭据为明文，仅监听本机；请只在调用时使用。'}
        if method=='GET' and path=='/api/wait':
            task_id=q['task_id'][0]
            timeout=min(900,max(0,float(q.get('timeout',['120'])[0])))
            interval=float(q.get('interval',['0.5'])[0])
            deadline=time.monotonic()+timeout
            while True:
                fresh=r.task(task_id)
                if fresh['status'] in TERMINAL:
                    return {'task_id':task_id,'status':fresh['status'],'result':fresh.get('result'),
                            'error':fresh.get('error'),'report_id':fresh.get('report_id'),'timed_out':False}
                if fresh['status']=='waiting' and fresh.get('wait'):
                    return {'task_id':task_id,'status':'waiting','wait':fresh['wait'],'timed_out':False}
                if time.monotonic()>=deadline:
                    return {'task_id':task_id,'status':fresh['status'],'timed_out':True,
                            'note':'等待窗口结束，任务仍在执行；可再次调用 /api/wait 或轮询 /api/task'}
                await asyncio.sleep(min(interval,max(0.05,deadline-time.monotonic())))
        if method=='POST' and path=='/api/maintenance/run':
            # 手动触发一个（或全部到期的）维护作业。
            # 只有调用方**显式**给出 dry_run 时才覆盖作业策略；否则由作业自己的
            # dry_run_default 决定（模型复验默认真跑，删除类作业默认只列清单）。
            job_id=data.get('job')
            kwargs={'force':True,'job_id':job_id}
            if 'dry_run' in data:
                kwargs['dry_run']=bool(data['dry_run'])
            results=await r.maintenance.tick(**kwargs)
            if job_id and not results: raise ValueError('未知维护作业：'+str(job_id))
            return {'results':results,'jobs':r.maintenance.status()}
        if method=='POST' and path=='/api/skills/call':
            # 技能端口调用：与模型调用同形——提交拿到 job，窗口内完成则直接带结果。
            workspace=str(data.get('workspace') or self.default_workspace())
            run_id=data.get('run_id')
            project_root=self._skill_project_root(workspace,run_id)
            wait=data.get('wait',DEFAULT_WAIT)
            job=await r.skill_invoker.call(data.get('skill'),args=data.get('args'),batch=data.get('batch'),
                                           workspace=workspace,project_root=project_root,
                                           timeout=data.get('timeout'),wait=wait)
            return {'job':job,'done':job['status'] in {'succeeded','failed','interrupted'},
                    'poll':None if job['status'] in {'succeeded','failed','interrupted'} else f"/api/skill/jobs/{job['id']}"}
        if method=='POST' and path=='/api/skills/reload':
            workspace=str(data.get('workspace') or self.default_workspace())
            project_root=self._skill_project_root(workspace,data.get('run_id'))
            snapshot=r.skills.snapshot(workspace,refresh=True,project_root=project_root)
            return {'workspace':workspace,'project_root':project_root,'revision':snapshot['revision'],
                    'skills':snapshot['skills'],'diagnostics':snapshot['diagnostics'],'roots':snapshot['roots']}
        if method=='POST' and path=='/api/models': return r.models.save(data)
        if method=='POST' and path=='/api/models/discover': return r.models.discover(data)
        if method=='POST' and path=='/api/providers/scan':
            # 使用已保存的 provider 事实（含凭据）重新扫描 /models，补齐新增模型。
            provider=s.get('provider',data['provider_id'])
            result=scan_provider(r.models,provider,api_key=provider.get('api_key',''))
            return result | {'provider_id':provider['id'],'default_model':provider.get('default_model','')}
        if method=='POST' and path=='/api/providers/verify':
            # 探针会消耗真实调用，因此默认只打"过期/未验证"的模型，并按最久未验证排序限量执行。
            return await r.models.verify_provider(data['provider_id'],data.get('model_ids'),
                                                  force=bool(data.get('force',False)),
                                                  limit=None if data.get('limit') is None else int(data['limit']))
        if method=='POST' and path=='/api/providers/default':
            # 设定主调度默认模型：provider 记录与 app_config 同时更新。
            provider=s.get('provider',data['provider_id']); model=data['model']
            if not any(entry['id']==model for entry in provider.get('models') or []):
                raise ValueError(f"provider {provider['id']} 没有模型 {model}")
            with s.transaction():
                provider['default_model']=model; s.put('provider',provider)
                for entry in provider['models']:
                    try:
                        record=r.store.get('model',f"{provider['id']}:{entry['id']}")
                    except ValueError:
                        continue
                    record['default_model']=entry['id']==model
                    r.store.put('model',record)
                configs=s.all('app_config')
                if configs:
                    config=configs[-1]
                    if config.get('model_provider')==provider['id']:
                        config.update(model=model,model_id=f"{provider['id']}:{model}",updated=now())
                        s.put('app_config',config)
            return {'provider_id':provider['id'],'default_model':model,'model_id':f"{provider['id']}:{model}"}
        if method=='POST' and path=='/api/benchmarks/refresh': return r.models.refresh_benchmarks(data.get('source_url') or 'https://llm-stats.com/',bool(data.get('force',False)))
        if method=='POST' and path in {'/api/config/import','/api/providers/import'}:
            return import_provider_config(s,r.models,data.get('config',data.get('toml','')),data.get('api_key',''))
        if method=='POST' and path=='/api/runs': return r.create_run(data['goal'],data['workspace'],data['model_id'])
        if method=='POST' and path=='/api/revise': return r.revise(data['run_id'],data['goal'])
        if method=='POST' and path=='/api/answer': return r.answer(data['id'],data['answer'])
        if method=='POST' and path=='/api/message':
            t=r.task(data['task_id'])
            if t['status']=='failed': return r.resume(t['id'],data['summary'])
            return r.deliver(t['id'],data['summary'],delivery='wake',kind='user')
        if method=='POST' and path=='/api/resume': return r.resume(data['task_id'],data.get('message','继续；先检查之前的结果。'))
        if method=='POST' and path=='/api/runs/pause': return r.pause_run(data['run_id'])
        if method=='POST' and path=='/api/runs/resume': return r.resume_run(data['run_id'])
        if method=='POST' and path=='/api/pause': return r.pause_run(data['run_id'])  # compatibility alias; never toggles
        if method=='POST' and path=='/api/task/cancel':
            t=r.task(data['task_id']); r.cancel_task(t['id']); return {'cancelled':t['id']}
        if method=='POST' and path=='/api/cancel':
            run=r.run(data['run_id']); r.cancel_task(run['root_task']); run['status']='cancelled'; s.put('run',run); return run
        if method=='POST' and path=='/api/archive': return r.workspace.archive(r.run(data['run_id']))
        if method=='POST' and path=='/api/restore': return r.workspace.restore_archive(data['archive_path'],data.get('task_id'),data.get('version','after'))
        if method=='POST' and path=='/api/compact':
            t=r.task(data['task_id']); t['compact_requested']=True; s.put('task',t)
            if t['status']=='failed': r.resume(t['id'],'压缩后继续。')
            return {'scheduled':True}
        if method=='POST' and path=='/api/demo':
            model={'id':'offline-demo','label':'离线机制演示','model':'scripted-demo','base_url':'demo://local','adapter':'demo',
                   'context_window':64000,'max_output':4096,'no_key':True,'description':'确定性脚本，仅验证机制，不代表真实模型能力'}
            s.put('model',model)
            folder=Path(self.default_workspace())/uid('example_'); folder.mkdir(parents=True)
            (folder/'README.md').write_text('# 演示项目\n\n这里只包含演示文件，不会修改你的其他项目。\n')
            return r.create_run('离线演示：递归协作、人工问答、文件修改和知识共享',str(folder),'offline-demo')
        raise ValueError('Unknown endpoint')

    def close(self):
        if self.closed: return
        self.closed=True
        if hasattr(self,'runtime'):
            asyncio.run_coroutine_threadsafe(self.runtime.stop(),self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        # 事件循环已停止，显式关闭以释放其文件描述符（测试里会以 ResourceWarning 暴露）。
        if not self.loop.is_running(): self.loop.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args): pass
    def send_json(self,data,status=200):
        body=dumps(data).encode(); self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def send_error_json(self,error):
        """稳定错误契约：同源控制台沿用 error 文案，外部程序可只看 code。"""
        if isinstance(error,AuthError):
            status,code=401,'auth_required'
        elif isinstance(error,(ValueError,KeyError,RuntimeError)):
            status,code=400,'invalid_request'
        else:
            status,code=500,'internal_error'
        self.send_json({'error':str(error),'code':code},status)
    def do_GET(self):
        parsed=urlparse(self.path); q=parse_qs(parsed.query)
        try:
            if parsed.path=='/api/stream':
                self.server.app.authenticate(self.headers)
                self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Cache-Control','no-cache'); self.end_headers()
                cursor=int(q.get('after',[0])[0])
                while True:
                    events=self.server.app.call('GET','/api/events',query={'after':[cursor]},internal=True)
                    if events:
                        cursor=events[-1]['seq']; self.wfile.write(('data: '+dumps({'cursor':cursor})+'\n\n').encode())
                    else: self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush(); time.sleep(.8)
            elif parsed.path.startswith('/api/'):
                self.server.app.authenticate(self.headers)
                # /api/wait 可能长时间阻塞，HTTP 侧超时窗口必须比它更长。
                timeout=min(960,float(q.get('timeout',['120'])[0])+60) if parsed.path=='/api/wait' else 60
                self.send_json(self.server.app.call('GET',parsed.path,query=q,timeout=timeout))
            else:
                relative=parsed.path.lstrip('/') or 'index.html'; path=(WEB/relative).resolve()
                if not path.is_relative_to(WEB.resolve()) or not path.is_file():
                    self.send_error(404); return
                body=path.read_bytes(); self.send_response(200)
                self.send_header('Content-Type',(mimetypes.guess_type(str(path))[0] or 'application/octet-stream')+'; charset=utf-8')
                self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-cache'); self.end_headers(); self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception as e: self.send_error_json(e)
    def do_POST(self):
        try:
            self.server.app.authenticate(self.headers)
            if 'application/json' not in self.headers.get('Content-Type',''): raise ValueError('JSON required')
            data=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
            # 可用性验证要逐模型发起真实请求，给更长的等待窗口。
            tail=urlparse(self.path).path
            # 可用性验证与技能调用都要等真实外部工作，给足够长的窗口。
            timeout=300 if tail.endswith('/verify') else (MAX_WAIT+60 if tail=='/api/skills/call' else 60)
            self.send_json(self.server.app.call('POST',urlparse(self.path).path,data,timeout=timeout))
        except Exception as e: self.send_error_json(e)


def main():
    parser=argparse.ArgumentParser(description='Unison local multi-model harness')
    parser.add_argument('--port',type=int,default=8740)
    parser.add_argument('--data',default=str(Path.cwd()/'.unison'))
    parser.add_argument('--workspace-root',default=None,
                        help='新任务的默认工作区根目录（默认取数据目录同级；'
                             '不要指到数据目录里面，否则子任务副本会带上所有历史产物）')
    parser.add_argument('--plugin',action='append',default=[],help='Import a Python module exposing setup(runtime)')
    parser.add_argument('--api-token',default='',help='外部程序调用令牌；默认复用/生成 <data>/api_token')
    parser.add_argument('--no-auth',dest='no_auth',action='store_true',
                        help='关闭鉴权：同机任何进程都能调用全部端点（本机单人使用）')
    parser.add_argument('--require-auth',dest='require_auth',action='store_true',
                        help='要求外部调用携带令牌（覆盖 --no-auth）')
    args=parser.parse_args()
    if args.no_auth and args.require_auth:
        parser.error('--no-auth 与 --require-auth 不能同时使用')
    # 命令行默认以本机单人方式运行（不鉴权），但这一点会被打印出来；
    # 库内 `App(...)` 的默认值仍是要求鉴权，避免忘记传参时静默失守。
    auth_required=bool(args.require_auth) and not args.no_auth
    app=App(args.data,args.plugin,args.api_token or None,auth_required=auth_required,
            workspace_root=args.workspace_root)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler); server.daemon_threads=True; server.app=app
    print(f'Unison listening on http://127.0.0.1:{args.port}',flush=True)
    print(f'外部调用令牌：{app.token}（也保存在 {app.root/"api_token"}）',flush=True)
    print(f'默认工作区：{app.default_workspace()}',flush=True)
    if not app.auth_required:
        print('警告：未启用鉴权（--require-auth 可开启）。同机任何进程都能调用全部端点，'
              '包括明文凭据端点 /api/models/credentials。',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); app.close()

if __name__=='__main__': main()

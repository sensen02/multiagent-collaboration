"""统一的维护层：所有周期性 / 按需的存储与状态维护都在这里注册、调度与记录。

设计目标（把原先散落的定时逻辑收成一处）：

- **一个注册表**：每个作业声明 `id`、`interval`、是否支持预演、是否具破坏性；
- **一个调度器**：默认**串行**执行，因为所有作业都在同一个 SQLite 写者上工作；
- **一次记录**：作业状态持久化在 `maintenance` 记录里，界面和外部程序都能看到
  "上次跑什么时候、下次什么时候、上次结果、连续失败几次"；
- **预演优先**：支持预演的作业先给清单与体积，不真删；具破坏性且不支持预演的作业
  永不自动执行，只能显式触发。

插件可以 `runtime.maintenance.register(...)` 加入自己的作业，主/子 Agent 共享同一注册表。
"""
from __future__ import annotations

import json
import time
import traceback

from .store import now
from .skills import render_catalog

MAX_BACKOFF = 6 * 3600
# 单次维护作业最多探测几个模型：这是目录维护 token 成本的上限。
MAX_PROBES_PER_RUN = 3


class MaintenanceJob:
    """一个维护作业。`run()` 返回 {'summary','counts','notes','size'}，必须是纯计算或幂等操作。

    作业可能只依赖 store（存储类维护），也可能需要运行时（扫描、评分、知识）：需要运行时的
    作业把 `needs_runtime` 置真，注册表会调用 `build()` 注入依赖——作业自己不关心被谁持有。
    """
    id = ''
    description = ''
    interval = 3600
    initial_delay = 0
    dry_run = False          # **支持** dry_run 参数（先给清单，不真删）
    dry_run_default = None   # 自动调度时默认是否预演；None = 跟随 destructive
    destructive = False      # 会删除数据；不支持预演时永不自动执行
    loud = False             # 每次执行都写事件（高频作业保持静默）
    needs_runtime = False

    def build(self,runtime):
        if not self.needs_runtime: return self
        clone=type(self)()
        clone.runtime=runtime
        for key,value in vars(self).items():
            if key!='runtime': setattr(clone,key,value)
        return clone

    def select(self,job_id):
        return self.id==job_id

    async def run(self,store,dry_run=False):  # pragma: no cover - 抽象
        raise NotImplementedError

    def describe(self,state,due):
        return {'id':self.id,'description':self.description,'interval':self.interval,
                'dry_run':self.dry_run,'destructive':self.destructive,
                'last_run':state.get('last_run'),'last_summary':state.get('last_summary'),
                'last_error':state.get('last_error'),'consecutive_errors':state.get('consecutive_errors',0),
                'runs':state.get('runs',0),'next_due':round(due,3) if due is not None else None}


class Maintenance:
    """单一调度器。tick() 由运行时循环驱动，也可在测试里直接调用。"""
    def __init__(self,store,runtime=None,min_interval=1.0):
        self.store=store
        self.runtime=runtime
        self.jobs=[]
        self.states={}
        self.min_interval=min_interval
        self.started=now()
        self._load()

    # ---------------------------------------------------------------- 注册与状态

    def register(self,job):
        if any(x.id==job.id for x in self.jobs):
            raise ValueError('维护作业 id 重复：'+job.id)
        if not isinstance(job,MaintenanceJob):
            raise ValueError('维护作业必须继承 MaintenanceJob：'+job.id)
        job=job.build(self.runtime) if self.runtime is not None else job
        self.jobs.append(job)
        self.states.setdefault(job.id,{})
        return job

    def _load(self):
        try:
            record=self.store.get('maintenance','state')
        except ValueError:
            return
        self.states={k:dict(v) for k,v in (record.get('jobs') or {}).items()}

    def _save(self):
        self.store.put('maintenance',{'id':'state','jobs':self.states,'updated':now()})

    def due_at(self,job):
        state=self.states.get(job.id,{})
        last=state.get('last_run')
        if last is None:
            return self.started+max(job.initial_delay,max(job.interval,self.min_interval))
        interval=job.interval
        errors=int(state.get('consecutive_errors') or 0)
        if errors:
            interval=min(MAX_BACKOFF,interval*(2**min(errors,6)))
        return last+max(interval,self.min_interval)

    def status(self):
        return [job.describe(self.states.get(job.id,{}),self.due_at(job)) for job in self.jobs]

    def find(self,job_id):
        return next((job for job in self.jobs if job.select(job_id)),None)

    # ---------------------------------------------------------------- 调度

    async def tick(self,force=None,dry_run=False,job_id=None):
        """执行到期的作业并把状态落盘；返回本次执行结果的列表（串行，避免并发写者）。"""
        results=[]
        for job in self.jobs:
            if job_id and not job.select(job_id): continue
            if force is None:
                if self.due_at(job)>now(): continue
            elif not force:
                continue
            if job.destructive and not job.dry_run and force is None:
                continue      # 破坏性且无法预演的作业绝不自动跑
            # dry_run 的解析交给 run_job：调用方显式要求预演时必须生效，
            # 否则用作业声明的默认值（模型复验默认真跑，删除类作业默认只列清单）。
            results.append(await self.run_job(job,dry_requested=dry_run,manual=bool(force is not None or job_id)))
        if results:
            self._save()
        return results

    async def run_job(self,job,dry_requested=False,manual=False,dry_run=None):
        """`dry_run=` 是旧调用点的兼容关键字（等价于 dry_requested）。"""
        if dry_run is not None:
            dry_requested=dry_run
        state=self.states.setdefault(job.id,{})
        if not job.dry_run:
            dry=False
        elif dry_requested:
            dry=True                       # 调用方/调度循环明确要求预演
        elif job.dry_run_default is not None:
            dry=bool(job.dry_run_default)  # 作业声明的默认（例如删除类默认预演）
        else:
            dry=True                       # 兜底：支持预演但没声明默认，按最保守处理（不真删）
        if not manual and job.dry_run_default is None and job.destructive:
            dry=True                       # 破坏性作业在自动调度下必须预演
        started=now()
        task=job.id if hasattr(job,'event_task') else None
        try:
            result=await job.run(self.store,dry_run=dry)
            result=result or {}
            state['last_run']=now()
            state['last_duration']=round(now()-started,3)
            state['last_summary']=result.get('summary') or ''
            state['last_counts']=result.get('counts') or {}
            state['last_error']=None
            state['consecutive_errors']=0
            state['runs']=int(state.get('runs',0))+1
            state['last_dry_run']=bool(dry)
            self._event(job,'MaintenanceFinished',{'dry_run':bool(dry),'manual':manual,
                                                   'summary':state['last_summary'],'counts':state['last_counts'],
                                                   'duration':state['last_duration']},task)
        except Exception as error:
            state['last_run']=now()
            state['last_error']='%s: %s'%(type(error).__name__,error)
            state['consecutive_errors']=int(state.get('consecutive_errors',0))+1
            result={'error':state['last_error'],'trace':traceback.format_exc()[-800:]}
            self._event(job,'MaintenanceFailed',{'dry_run':bool(dry),'manual':manual,'error':state['last_error']},task)
        self.states[job.id]=state
        return {'job':job.id,'dry_run':bool(dry),'manual':manual,**result}

    def _event(self,job,type,payload,task=None):
        """高频作业保持静默（loud=False）时不产生事件，避免污染事件流。"""
        if not job.loud and type=='MaintenanceFinished': return
        try:
            self.store.event(type,{'job':job.id,**payload},task=task)
        except Exception:
            pass


# -------------------------------------------------------------------- 内置作业

class WorkspaceScan(MaintenanceJob):
    """对可观察任务的当前工作区做一次文件对账（含权限位）。"""
    id='workspace-scan'
    description='扫描可观察任务的当前工作区并记录文件版本'
    interval=2
    needs_runtime=True
    initial_delay=2

    async def run(self,store,dry_run=False):
        runtime=self.runtime
        tasks=store.all('task')
        seen=set()
        changed=0
        for task in sorted(tasks,key=lambda x:-x['created']):
            run=runtime.run(task['run_id'])
            # 归档即结案：工作区副本已释放、记录已冻结，不再对它做文件对账。
            if run.get('archived'): continue
            observable=runtime.current(task) or (run.get('root_task')==task['id'] and run['status'] in {'active','completed','paused'})
            if not observable or task['workspace'] in seen or task['id'] in runtime.processes: continue
            seen.add(task['workspace'])
            with store.transaction():
                changed+=len(runtime.workspace.reconcile(task))
        return {'summary':'对账 %d 个工作区，记录 %d 个文件版本'%(len(seen),changed),
                'counts':{'workspaces':len(seen),'changes':changed}}


class SkillCatalogScan(MaintenanceJob):
    """定期重扫技能目录，让新增/修改/删除的技能在不重启的前提下生效。

    只读作业：技能正文始终在加载时从磁盘读，因此这里不需要"刷新缓存"以外的动作——
    它负责在**沙箱外**及时发现目录变化（例如用户在编辑器里新增技能目录），并留下可查的
    目录指纹与模型实际会看到的目录快照。目录没有变化时不写事件，避免污染事件流。
    """
    id='skill-catalog'
    description='重扫技能目录（项目 .dsh/skills、用户目录与内置技能包）'
    interval=15*60
    needs_runtime=True
    initial_delay=5
    loud=False

    async def run(self,store,dry_run=False):
        runtime=self.runtime
        workspace=str(store.root/'playground')
        revision_before=runtime.skills.catalog_revision
        snapshot=runtime.skills.snapshot(workspace,refresh=True,
                                         project_root=runtime.skills.project_root(workspace))
        skills=snapshot['skills']
        diagnostics=list(snapshot['diagnostics'])
        changed=bool(revision_before) and revision_before!=snapshot['revision']
        if changed:
            store.event('SkillCatalogChanged',{'revision':snapshot['revision'],'previous':revision_before,
                                               'skills':[item['name'] for item in skills]})
        return {'summary':'技能 %d 个，诊断 %d 条%s'%(
                    len(skills),len(diagnostics),'，目录已变化' if changed else ''),
                'counts':{'skills':len(skills),'diagnostics':len(diagnostics),
                          'changed':int(changed)},
                'notes':[item.get('error') or item.get('impact') for item in diagnostics[:5]],
                'catalog':render_catalog([item for item in skills if item['model_invocable']])}


class ModelHealthProbe(MaintenanceJob):
    """低频、限量地探测"没有近期证据"的模型可用性。

    这是"不靠烧 token 维护目录"的最后一道保险，而不是主要证据来源——主要证据是真实调用
    （`Models.invoke()` 成功后自动回写）。因此：

    - 只探测**已过期**或**从未验证**的模型；`blocked`（凭据/权限类永久错误）不重试，等配置变更；
    - 每次最多 `MAX_PROBES_PER_RUN` 个，按"最久未验证"优先，一天最多几次调用；
    - 静默作业：没有状态变化就不写事件（失败另有 `MaintenanceFailed`）。
    - `dry_run` 只列出"本次会探测谁"，不发起任何调用。
    """
    id='model-health'
    description='限量复验过期的模型可用性（真实调用是主证据，本作业只补空档）'
    interval=6*3600
    needs_runtime=True
    initial_delay=600      # 启动后先让真实调用积累证据，别急着探测
    loud=False
    dry_run=True             # 支持预演：只报清单
    dry_run_default=False    # 但自动调度时真跑（只读、非破坏性、每次上限 3 个）

    async def run(self,store,dry_run=False):
        runtime=self.runtime
        view=runtime.models.health_view()
        pending=[item for item in view if item['needs_probe']]
        pending.sort(key=lambda item: item.get('checked_at') or 0)
        planned=pending[:MAX_PROBES_PER_RUN]
        if dry_run:
            return {'summary':'预演：%d 个模型待复验，本次会探测 %d 个'%(len(pending),len(planned)),
                    'counts':{'pending':len(pending),'planned':len(planned),'probed':0},
                    'notes':[f"{item['id']}（上次 {item.get('source') or '未验证'}）" for item in planned]}
        probed=[]; failed=[]
        for item in planned:
            try:
                health=await runtime.models.verify(item['id'],timeout=90)
            except Exception as exc:
                failed.append(f"{item['id']}: {exc}")
                continue
            probed.append({'model':item['id'],'state':(health or {}).get('state'),'code':(health or {}).get('code')})
        return {'summary':'复验 %d 个模型：%d 可用，%d 失败；仍有 %d 个待复验'%(
                    len(probed),sum(1 for x in probed if x['state']=='ok'),len([x for x in probed if x['state']!='ok']),
                    max(0,len(pending)-len(planned))),
                'counts':{'pending':len(pending),'planned':len(planned),'probed':len(probed),
                          'reachable':sum(1 for x in probed if x['state']=='ok')},
                'notes':[f"{x['model']} → {x['state']}({x['code']})" for x in probed]+failed}


class BenchmarkRefresh(MaintenanceJob):
    """按 TTL 刷新模型评分；抓取失败不影响任何功能。"""
    id='benchmark-refresh'
    description='刷新 LLM Stats 评分（24 小时缓存）'
    interval=24*3600
    needs_runtime=True
    initial_delay=90

    async def run(self,store,dry_run=False):
        runtime=self.runtime
        result=runtime.models.refresh_benchmarks()
        return {'summary':'评分条目 %s，匹配 %s'%(result.get('entries'),result.get('matched')),
                'counts':{'entries':result.get('entries') or 0,'matched':result.get('matched') or 0},
                'notes':[result['refresh_error']] if result.get('refresh_error') else []}


class ObjectCollector(MaintenanceJob):
    """内容寻址对象库的 mark & sweep。

    引用来源必须**递归**扫描：不仅扫已知字段，还要读出每个对象里的文本，寻找下一层引用
    （工具结果、事件负载、集成日志都会以 32 字节文本对象的形式引用别的对象）。
    删除顺序也重要：对象被归档的 tar.gz 复制过，所以先清对象、再谈归档。
    """
    id='object-gc'
    description='回收无人引用的内容对象（默认预演）'
    interval=7*24*3600
    initial_delay=12*3600
    dry_run=True
    destructive=True

    async def run(self,store,dry_run=True):
        reachable=self.reachable(store)
        on_disk=set(path.name for path in store.objects.iterdir() if path.is_file())
        orphans=sorted(on_disk-reachable)
        size=0
        for name in orphans:
            try: size+=(store.objects/name).stat().st_size
            except OSError: pass
        removed=0
        if not dry_run:
            for name in orphans:
                try: (store.objects/name).unlink(); removed+=1
                except OSError: pass
        return {'summary':('预演：' if dry_run else '已回收 ')+'%d 个无引用对象（%.1f KB），保留 %d 个'%(len(orphans),size/1024,len(on_disk&reachable)),
                'counts':{'objects':len(on_disk),'reachable':len(on_disk&reachable),
                          'orphans':len(orphans),'removed':removed,'bytes':size},
                'notes':orphans[:20]}

    @staticmethod
    def reachable(store):
        """mark 阶段：从所有记录与事件出发，递归标记可达的对象。

        两个必须做对的细节：
        1. 记录 body 是**序列化后的 JSON 字符串**，字符串分支只判断"整串是不是一个哈希"，
           所以必须先解析再递归——否则会漏掉清单里全部引用（误判成活对象）。
        2. 引用可能嵌在任意深度，且文本/JSON 对象里还会引用别的对象，要连追一层。
        """
        objects=set(path.name for path in store.objects.iterdir() if path.is_file())
        found=set()
        def walk(value,depth=0):
            if depth>6: return
            if isinstance(value,str):
                text=value
                if len(value)==64 and value in objects:
                    if value in found: return
                    found.add(value)
                    try: text=store.read_blob(value).decode('utf-8','replace')
                    except (OSError,ValueError): return
                if text[:1] in '[{"' or text[:1].isdigit():
                    try: walk(json.loads(text),depth+1)
                    except ValueError: pass
                return
            if isinstance(value,dict):
                for item in value.values(): walk(item,depth)
            elif isinstance(value,list):
                for item in value: walk(item,depth)
        walk([row['body'] for row in store.db.execute('SELECT body FROM records').fetchall()])
        walk([dict(event) for event in store.events(limit=1000000)])
        return found


class ArchiveRetention(MaintenanceJob):
    """归档是失败现场的唯一冷备份：默认只列出，不删除。"""
    id='archive-retention'
    description='列出归档占用与建议（删除需显式触发）'
    interval=7*24*3600
    initial_delay=6*3600
    dry_run=True
    destructive=True

    async def run(self,store,dry_run=True):
        files=sorted(store.root.glob('archives/*.tar.gz'))
        total=sum(path.stat().st_size for path in files if path.is_file())
        oldest=None
        for path in files:
            try:
                created=path.stat().st_mtime
                oldest=created if oldest is None else min(oldest,created)
            except OSError: pass
        notes=['%s（%.1f KB）'%(path.name,path.stat().st_size/1024) for path in files[:20]]
        removed=0
        if not dry_run:
            for path in files:
                try: path.unlink(); removed+=1
                except OSError: pass
        return {'summary':('%s：%d 个归档，共 %.1f KB'%('预演' if dry_run else '已清理',len(files),total/1024)),
                'counts':{'archives':len(files),'bytes':total,'removed':removed},
                'notes':notes}


class KnowledgeMaintenance(MaintenanceJob):
    """知识层的整理触发：只提交一次整理子任务，不在维护里调用模型。

    触发条件用**相对阈值**而不是时钟：一个项目的知识里失效占比过高、或条数过多时，
    才值得一次模型调用；否则维护零成本地什么都不做。
    """
    id='knowledge-maintenance'
    description='按阈值检查知识层是否需要整理（不直接调用模型）'
    interval=6*3600
    initial_delay=1800
    needs_runtime=True
    stale_ratio=0.3
    max_entries=60

    async def run(self,store,dry_run=False):
        runtime=self.runtime
        groups={}
        for item in store.all('knowledge'):
            groups.setdefault(item.get('project_workspace') or '',[]).append(item)
        needs=[]
        for workspace,items in groups.items():
            if not workspace: continue
            probe={'workspace':workspace,'run_id':None,'revision':None}
            legacy=next((t for t in store.all('task') if t['workspace']==workspace and t.get('run_id')),None)
            if legacy: probe={'workspace':workspace,'run_id':legacy['run_id'],'revision':legacy['revision']}
            stale=sum(1 for item in items if runtime.knowledge_validity(item,probe).get('stale'))
            ratio=stale/len(items) if items else 0
            if ratio>=self.stale_ratio or len(items)>self.max_entries:
                needs.append({'workspace':workspace,'entries':len(items),'stale':stale,'ratio':round(ratio,2)})
        notes=['%s：%d 条中 %d 条失效'%(need['workspace'].split('/')[-1],need['entries'],need['stale']) for need in needs]
        return {'summary':'检查 %d 个项目，%d 个需要整理'%(len(groups),len(needs)),
                'counts':{'projects':len(groups),'needing':len(needs)},
                'notes':notes,'needs':needs}


# -------------------------------------------------------------------- 作业发现

def built_in_jobs():
    return [WorkspaceScan(),SkillCatalogScan(),ModelHealthProbe(),BenchmarkRefresh(),ObjectCollector(),ArchiveRetention(),KnowledgeMaintenance()]

from __future__ import annotations
import asyncio
import contextlib
import hashlib
import json
import os
import signal
import time
from pathlib import Path
from .store import Store, uid, now, dumps
from .models import Models, ModelError, FAILOVER_CODES
from .workspace import Workspace
from .image_info import image_info
from .tools import TOOLS, SYSTEM, schemas_for
from .skills import MAX_BATCH, Skills, SkillError, render_catalog, render_content
from .skill_runtime import SkillInvoker, SkillInvocationError
from .maintenance import Maintenance, built_in_jobs
from . import providers

TERMINAL = {'completed','failed','cancelled','superseded'}

# `_call_model(tools=...)` 的哨兵：省略参数 = 给本任务的完整工具集；
# 显式传 None 才是"这次调用不该带上工具"（压缩就是这样）。
TOOLS_FOR_TASK = object()
MAX_COMPACT_PER_STEP = 3

# 历史派生（derive_history）真正读的事件类型，就这四种。
# 用它过滤是必要的：`ModelReturned` 单条可达 176 KB（上游逐条消息的 usage 归属），
# 在真实运行里占了事件总量的 99%，而派生一行都不看它。
HISTORY_EVENT_TYPES = ('MessageAppended','ToolResultRecorded','ToolRepairRecorded','ContextCompacted')


def describe_error(error):
    """Keep the provider-neutral failure code visible in task errors and the console."""
    if isinstance(error, ModelError):
        return f'模型调用失败（{error.code}）：{error}'
    return str(error)


# 信息分叉检测用的文本归一化：只保留有内容的字符，忽略空白与标点差异——
# 抄进 brief 的原文常常被重新排版过，逐字节比对会漏判。
_PUNCT = set(' \t\r\n，。、；：！？""''（）《》【】,.!?;:()[]{}<>"\'`~@#$%^&*_-+=|\\/')
def _normalize(text):
    return ''.join(ch for ch in str(text or '').lower() if ch not in _PUNCT)


def _best_window_ratio(hay,needle):
    """needle 在 hay 里最好的对齐相似度（0–1）。用于判断"这段文字是不是被抄过来的"。

    先用**包含**判定：抄进任务描述的原文通常一字不改，只是重排版过。否则再做一次带小偏移的
    编辑距离对齐，容忍个别字符被改写。原先用粗步长滑动窗口按位置取样，步长（size//4）
    会直接跨过匹配位置——实测把明明抄全了的文本算成相似度 0。诊断工具自己不可靠比没有更糟。
    """
    size=len(needle)
    if size==0 or len(hay)<size: return 0.0
    if needle in hay: return 1.0
    best=0.0
    for slack in (0,1,2,4):
        window=size+slack
        starts=list(range(max(0,len(hay)-window),-1,-max(1,window//8)))
        for start in starts[:64]:                      # 有界：诊断不该成为开销来源
            segment=hay[start:start+window]
            if not segment: continue
            previous=list(range(len(segment)+1))
            for i,ch in enumerate(needle,1):
                current=[i]
                for j,c2 in enumerate(segment,1):
                    current.append(min(previous[j]+1,current[j-1]+1,previous[j-1]+(ch!=c2)))
                previous=current
            distance=previous[-1]
            ratio=1.0-distance/max(len(needle),len(segment))
            if ratio>best: best=ratio
        if best>=0.95: break
    return best


def _task_brief_text(store,task):
    """任务的 brief 原文（该任务的 `task-brief` 消息），用于检测"有没有抄共享信息"。"""
    run_id=task.get('run_id')
    for event in store.events(run_id=run_id):
        if event.get('task_id')!=task.get('id') or event.get('type')!='MessageAppended': continue
        payload=event.get('payload') or {}
        if payload.get('source')!='task-brief': continue
        message=payload.get('message') or {}
        return str(message.get('content') or '')
    return ''


class Runtime:
    def __init__(self, root, concurrency=4, batch_seconds=.35):
        self.store = Store(root)
        self.models = Models(self.store)
        self.workspace = Workspace(self.store)
        # 技能目录：内置包随程序分发，项目/用户技能由 Skills 按 rank 竞争合并。
        self.skills = Skills(bundled=Path(__file__).resolve().parent/'skills')
        # 技能调用器：把声明了 invocation 契约的技能做成"走端口"的能力，与模型调用同形。
        self.skill_invoker = SkillInvoker(self)
        self.concurrency = concurrency
        self.batch_seconds = batch_seconds
        self.active = {}
        self.processes = {}
        # "派生条数对不上"的报警去重键：见 `task_history`。一次不匹配只报一次。
        self.shortfall_reported = set()
        self.closed = False
        self.handlers = {}
        self._derive_cache=None
        # 所有周期性/按需维护都在这里注册，由同一个调度器串行执行。
        self.maintenance = Maintenance(self.store, runtime=self)
        for job in built_in_jobs():
            self.maintenance.register(job)
        self.service_registry={'store':self.store,'models':self.models,'workspace':self.workspace,
                               'maintenance':self.maintenance,'skills':self.skills,
                               'skill_invoker':self.skill_invoker}
        # Plugins can replace an adapter or register an additional tool without changing this loop.
        self.schemas = list(TOOLS)
        for schema in TOOLS:
            name = schema['function']['name']
            self.handlers[name] = getattr(self,'tool_'+name)

    def load_plugin(self, module):
        import importlib
        plugin=importlib.import_module(module)
        plugin.setup(self)

    def register_tool(self, schema, handler):
        name = schema['function']['name']
        self.schemas = [s for s in self.schemas if s['function']['name'] != name] + [schema]
        self.handlers[name] = handler

    def task(self,id): return self.store.get('task',id)
    def run(self,id): return self.store.get('run',id)

    def task_project_root(self,task):
        """任务所属的项目根：根任务就是它的工作区；子任务是父任务的项目根。

        子任务工作副本位于数据目录内，物理祖先与真实项目无关，因此项目根必须显式记录，
        不能靠路径推断——技能根与"这个任务在改哪个项目"都用它。
        """
        if not task.get('parent_id'):
            return task.get('project_root') or task.get('workspace') or ''
        parent=task.get('project_root')
        if parent:
            return parent
        ancestor=self.store.get('task',task['parent_id'])
        return ancestor.get('project_root') or ancestor.get('workspace') or ''

    def schemas_for_task(self,t):
        """该任务的工具 schema：**全量**。

        不再按 `tools_profile` 预制档位：主模型与子模型、以及任何角色拿到的是同一套工具。
        运行时的职责是提供能力，"这个角色用不用得上"由模型自己判断。
        """
        return list(TOOLS)

    def skill_snapshot(self,workspace,project_root=None):
        """技能发现结果；坏技能文件不抛异常，随诊断一起交给控制台核对。"""
        try:
            return self.skills.snapshot(workspace,project_root=project_root or self.skills.project_root(workspace))
        except Exception as exc:
            return {'skills':[],'diagnostics':[{'path':'','source':'','error':f'{type(exc).__name__}: {exc}'}],
                    'roots':[],'revision':''}

    def skill_catalog(self,workspace,project_root=None):
        """把可用技能写成一条会话目录消息（名称 + 摘要）。"""
        snapshot=self.skill_snapshot(workspace,project_root)
        catalog=[item for item in snapshot['skills'] if item['model_invocable']]
        return render_catalog(catalog,base_dirs=bool(catalog))

    # ------------------------------------------------------------ 会话历史的事实源

    def record_message(self,task,message,source=''):
        """把一条模型可见消息追加进**事件日志**。

        历史完全由日志派生（`derive_history`），任务记录里**不再保存历史副本**：
        只保留 `history_messages`（消息数，用于轻量一致性校验）与 `history_ref`（最后一条）。
        """
        ref=self.store.blob(dumps(message))
        self.store.event('MessageAppended',{'message':message,'ref':ref,'source':source},task)
        task['history_messages']=int(task.get('history_messages',0))+1
        task['history_ref']=ref
        self._derive_cache=None
        return ref

    def seed_history(self,task,messages,source='seed'):
        """成批写入历史（供导入、测试或恢复脚本使用）。

        走与运行时同一条路径写入日志，因此派生历史与缓存副本保持一致；直接改
        `task['history']` 会让日志与缓存分叉，派生结果会覆盖那些改动。
        """
        refs=[self.record_message(task,message,source=source) for message in messages]
        # 历史只在日志里：清掉内存副本，避免调用方随后 put 任务时又把它写回记录。
        task.pop('history',None)
        self.store.put('task',task)
        return refs

    def record_tool_result(self,task,call_id,result):
        """工具结果只在事件里存引用：完整内容本就在对象库与 ToolResult 事件里。"""
        ref=self.store.blob(dumps(result))
        self.store.event('ToolResultRecorded',{'call_id':call_id,'ref':ref,
                                               'truncated':len(dumps(result))>12000},task)
        task['history_messages']=int(task.get('history_messages',0))+1
        self._derive_cache=None
        return ref

    def has_message_log(self,task,events=None):
        """任务是否已经有消息日志。旧任务（升级前）没有，只能回退到记录里的历史字段。"""
        if task.get('history_messages'): return True
        events=self.store.events(task['run_id'],limit=1000000) if events is None else events
        return any(e['task_id']==task['id'] and e['type']=='MessageAppended' for e in events)

    def task_history(self,task,events=None):
        """这次模型调用要读的历史。

        - 有消息日志的任务：**完全由日志派生**（任务记录里没有副本可读）。
        - 旧任务（升级前创建，没有消息日志）：回退到记录里的 `history` 字段。

        `events` 可由调用方传入：一个运行里每个任务的历史都从**同一份事件日志**派生，
        循环调用时只该取一次。取一次就是几百 MB 的差别——某真实运行 5 个任务、151 MB
        事件，逐任务各取一遍要解析 756 MB（5.6 秒），而派生真正需要的只有 0.35 MB。
        """
        if events is None:
            events=self.store.events(task['run_id'],limit=1000000,types=HISTORY_EVENT_TYPES)
        if not self.has_message_log(task,events): return task.get('history') or []
        messages,_=self.derive_history(task,events)
        # 轻量一致性校验：消息数应当与日志记下的条数一致，不一致说明派生缺了东西。
        # 报警**每个 (任务, 实际, 期望) 只写一次**：这个函数是读路径（每次模型调用、每次控制台
        # 派生历史都会经过），在里面无条件写事件，一旦不匹配就变成事件风暴——实测重启打断了一次
        # 工具调用之后，这条报警以约 1.2 条/秒的速度往库里写。事实写一遍就够了。
        expected=task.get('history_messages')
        if isinstance(expected,int) and expected!=len(messages):
            key=(task['id'],len(messages),expected)
            if key not in self.shortfall_reported:
                self.shortfall_reported.add(key)
                self.store.event('HistoryDerivationShortfall',{'derived':len(messages),'expected':expected},task)
        return messages

    def sync_history(self,task):
        """兼容旧调用点：只读地返回"带派生历史"的任务视图。

        历史是日志的纯函数，任务记录里没有副本；因此这里**不改动传入对象**，而是返回一个
        浅拷贝并在其上挂 `history`，避免调用方随后 `store.put` 时把它写回记录。
        """
        if not self.has_message_log(task): return task
        return dict(task,history=self.task_history(task))

    def derive_history(self,task,events):
        """从事件日志派生历史：与 DSH 的 deriveMessages 同构的纯函数。

        两阶段，避免任何下标推算：

        1. **收集**：按事件顺序收集原始消息（`MessageAppended`），工具结果用
           `ToolResultRecorded`/`ToolRepairRecorded` 按 call_id 插回发起它的 assistant 消息之后，
           并在 `ContextCompacted` 处**打一个标记**（此时还没有任何压缩效果）。
        2. **代入**：按事件顺序对标记处施加压缩——把该标记之前累积的消息按压缩当时的定义
           （保留头部两条 + checkpoint + 尾部原文）整体替换。每个标记正好对应一次压缩，
           且代入后标记之前的消息已被替换为压缩后的形态，因此下一次压缩的边界天然正确。

        返回 (messages, linked)；linked=False 表示该任务还没有消息日志（旧任务），
        调用方应回退到缓存副本。
        """
        key=task['id']; cursor=self.store.cursor()
        cached=self._derive_cache
        if cached and cached.get('key')==key and cached.get('cursor')==cursor:
            return cached['messages'],cached['linked']
        entries=[]; pending={}; linked=False; seen=0
        for event in events:
            if event['task_id']!=key: continue
            payload=event['payload'] or {}
            kind=event['type']
            if kind=='MessageAppended':
                message=payload.get('message')
                if not isinstance(message,dict): continue
                linked=True
                seen+=1
                entries.append(message)
                for call in message.get('tool_calls') or []: pending[call.get('id')]=len(entries)
            elif kind=='ToolResultRecorded':
                linked=True
                seen+=1
                self._insert_result(entries,pending,payload.get('call_id'),self._read_ref(payload.get('ref')))
            elif kind=='ToolRepairRecorded':
                linked=True
                for repaired in payload.get('repaired') or []:
                    self._insert_result(entries,pending,repaired.get('call_id'),repaired.get('content') or '')
            elif kind=='ContextCompacted':
                linked=True
                entries.append({'__compaction__':payload,'__position__':seen,
                                '__checkpoint__':self._checkpoint_message(payload)})

        items,_=self.apply_compactions(entries)
        self._derive_cache={'key':key,'cursor':cursor,'messages':items,'linked':linked}
        return items,linked

    @staticmethod
    def apply_compactions(entries):
        """把收集到的消息与压缩标记依次代入，返回最终历史。

        每个标记表示"这一刻发生了一次压缩"：把标记之前累积的消息替换成压缩后的形态
        （头部两条 + checkpoint + 尾部原文）。代入后标记已消费，因此后续标记之前累积的
        天然是压缩后的历史，不需要任何下标推算。
        """
        items=[]; linked=False
        for entry in entries:
            if isinstance(entry,dict) and '__compaction__' in entry:
                linked=True
                checkpoint=entry.get('__checkpoint__') or {'role':'user','content':''}
                payload=entry['__compaction__']
                cut=payload.get('kept_from')
                if not isinstance(cut,int): cut=entry.get('__position__')
                if not isinstance(cut,int): cut=len(items)
                items[:]=items[:2]+[checkpoint]+items[max(2,min(cut,len(items))):]
                continue
            items.append(entry)
        return items,linked

    @staticmethod
    def _insert_result(items,pending,call_id,content):
        """把工具结果插到发起它的 assistant 消息之后（与运行时写缓存的顺序一致）。"""
        if not call_id: return
        anchor=pending.pop(call_id,None)
        if anchor is None: return          # 没有对应调用的结果不进入历史
        position=min(anchor,len(items))
        while position<len(items) and items[position].get('role')=='tool' and items[position].get('tool_call_id')!=call_id:
            position+=1                     # 同一批次里的多个结果保持模型顺序
        items.insert(position,{'role':'tool','tool_call_id':call_id,'content':content})

    def apply_compaction(self,history,cut,checkpoint_message,region):
        """压缩的**全量代入**形式：按缓存里的定义生成压缩后的历史。

        压缩后被遮蔽的区间在历史上被"两条头消息 + checkpoint + 尾部原文"取代。因此从任意
        时点的输入快照出发、依次代入每一次压缩事件，就能得到当时的派生历史——不需要在派生
        过程中猜测下标（影子区间在压缩当时的语义才是准的）。
        """
        start=max(0,min(int(cut),len(history)))
        return history[:2]+[checkpoint_message]+history[start:]

    def _apply_compaction(self,items,payload,region,checkpoint_message):
        cut=payload.get('shadowed_range')
        cut=cut[1] if isinstance(cut,list) and len(cut)==2 and isinstance(cut[1],int) else len(items)
        items[:]=self.apply_compaction(list(items),cut,checkpoint_message,region)

    def _load_region(self,ref):
        if not ref: return None
        try:
            region=json.loads(self.store.read_blob(ref))
        except (OSError,ValueError):
            return None
        return region if isinstance(region,list) else None

    def _checkpoint_message(self,payload):
        checkpoint_ref=payload.get('checkpoint_ref')
        return {'role':'user','content':self._read_ref(checkpoint_ref) if checkpoint_ref else ''}

    def _read_ref(self,ref):
        try:
            return self.store.read_blob(ref).decode('utf-8','replace')
        except (OSError,ValueError):
            return ''

    def current(self, task):
        run = self.run(task['run_id'])
        return task['revision']==run['revision'] and run['status']=='active' and task['status'] not in TERMINAL

    def guard(self, task, epoch):
        fresh = self.task(task['id'])
        if fresh['epoch'] != epoch or not self.current(fresh):
            raise RuntimeError('执行代次或目标已失效；结果仅保留在历史记录')
        return fresh

    def park(self,task,epoch,reason):
        """运行被暂停时，把**已经在飞的那一轮**交回队列。

        暂停不是失效：`current()` 对暂停中的运行返回 False，而代码里多处把这个 False
        当成"目标变了、结果作废"。对暂停必须区别对待，否则在飞的任务会卡在 `running`——
        调度器不领它（`current()` 为假），恢复后也轮不到它（领取要求 `queued`），
        于是整个恢复过程少一个 Agent，且没有任何报错。

        被丢弃的那一轮（模型已返回、工具还没跑）不写进历史：下一轮重跑就是。
        已经跑完工具批次的一轮不在这里处理——它在 `_model_round` 末尾已经回到队列，
        那一轮的工作成果照常保留。
        """
        fresh=self.task(task['id'])
        if fresh['epoch']!=epoch or fresh['status']!='running': return False
        if self.run(fresh['run_id'])['status']!='paused': return False
        fresh['status']='queued'; fresh['not_before']=now()
        self.store.put('task',fresh)
        self.store.event('TaskParked',{'reason':reason},fresh)
        return True

    def backfill_message_log(self,task,cached):
        """把缓存里已有、但日志还没记的消息补进日志（保持顺序）。

        用于两种情形：缓存副本比日志长（历史数据、或调用方成批替换过历史），以及压缩/重写
        历史之前——只有日志与缓存完全对应，"日志即事实源"才不会被随后的派生结果推翻。
        """
        events=self.store.events(task['run_id'],limit=1000000)
        derived,_=self.derive_history(task,events)
        if len(derived)>=len(cached): return 0
        missing=cached[len(derived):]
        for message in missing:
            self.record_message(task,message,source='backfill')
        self._derive_cache=None
        return len(missing)

    def check_workspace_available(self, workspace, own_run=None):
        requested=Path(workspace).expanduser().resolve()
        for existing in self.store.all('run'):
            occupied=Path(existing['workspace']).resolve()
            if existing['id']!=own_run and existing['status'] in {'active','paused','revising'} and (requested.is_relative_to(occupied) or occupied.is_relative_to(requested)):
                raise ValueError('项目目录已被未结束任务占用：'+existing['id'])

    def workspace_inside_data_dir(self,workspace):
        """根工作区是不是落在运行时的数据目录里？是就返回那句话（否则 None）。

        真机发现（ink-duel 复核）：默认工作区曾经是 `<数据目录>/playground`。
        后果不是"不好看"：子任务工作副本会把根工作区下面的**所有**历史产物一起复制过去
        （上一个游戏的中间态成为下一个任务的基线），而 `.unison` 只在拷贝时被忽略,
        根自己就在里面。项目级技能根、知识作用域、外部编辑器位置也一起悬空。

        所以这条**不阻断**，但每次创建运行都会记账，并在控制台与事件里明说：
        它是可修的配置问题，不是"运行时坏了"。
        """
        try:
            ws=Path(workspace).expanduser().resolve()
            data=Path(self.store.root).expanduser().resolve()
        except (OSError,TypeError):
            return None
        if ws==data or data in ws.parents:
            return (f'根工作区 {ws} 在数据目录 {data} 内：子任务工作副本会带上该目录下所有历史产物，'
                    f'项目级技能/知识的作用域也会悬空。用 --workspace-root 指到数据目录之外'
                    f'（控制台"新任务"里也可直接填项目目录）。')
        return None

    def create_run(self, goal, workspace, model_id):
        if not goal.strip(): raise ValueError('目标不能为空')
        self.store.get('model',model_id)
        self.check_workspace_available(workspace)
        configs=self.store.all('app_config')
        app_config=configs[-1] if configs else {}
        run = {'id':uid('run_'),'goal':goal,'revision':1,'workspace':str(Path(workspace).expanduser().resolve()),
               'model_id':model_id,'review_model_id':app_config.get('review_model_id'),
               'features':app_config.get('features',{}),'status':'active','pause_reason':'','created':now()}
        warning=self.workspace_inside_data_dir(run['workspace'])
        if warning: run['workspace_warning']=warning
        with self.store.transaction():
            self.store.put('run',run)
            self.store.put('revision',{'id':run['id']+':1','run_id':run['id'],'revision':1,'goal':goal,'created':now()})
            task = self.create_task(run,goal,model_id)
            run['root_task'] = task['id']; self.store.put('run',run)
            self.store.event('RunCreated',{'goal':goal},run_id=run['id'],revision=1)
            if warning:
                self.store.event('WorkspaceInDataDir',{'workspace':run['workspace'],'warning':warning},
                                 run_id=run['id'],revision=1)
        return run

    def create_task(self, run, goal, model_id, parent=None, dependencies=None, priority=0):
        self.store.get('model',model_id)
        self.ensure_not_archived(run)
        deps = dependencies or []
        for id in deps:
            d = self.task(id)
            if d['run_id']!=run['id'] or d['revision']!=run['revision']:
                raise ValueError('依赖必须属于当前运行版本')
            ancestor=parent
            while ancestor:
                if id==ancestor['id']: raise ValueError('子任务不能依赖自己的祖先任务')
                ancestor=self.task(ancestor['parent_id']) if ancestor.get('parent_id') else None
        task = {'id':uid('task_'),'run_id':run['id'],'parent_id':parent['id'] if parent else None,
                'revision':run['revision'],'goal':goal,'model_id':model_id,'status':'queued','created':now(),
                'workspace':run['workspace'],'epoch':0,'dependencies':deps,'priority':int(priority),
                'context_epoch':0,'inbox_cursor':self.store.cursor(),'wait':None,'not_before':now(),
                'result':None,'compact_requested':False,'last_call_cursor':self.store.cursor()}
        self.workspace.setup(task,parent)
        # 所有 Agent 使用同一套工具：不预制档位、不替模型裁剪能力。
        # 显式记录项目根：子任务的工作副本在数据目录里，仅凭路径无法回到真实项目。
        task['project_root'] = (parent.get('project_root') if parent else None) or task['workspace']
        knowledge = self.knowledge_search('',task)
        # brief 索引只放**项目知识**：`scope='run'` 的共享条目是本次运行内的一次性信息
        # （公告、状态、某局的发言），进索引会污染后续所有任务的简报，下一局还会搜到上一局的内容。
        index = [{'id':k['id'],'title':k['title'],'stale':k['stale']}
                 for k in knowledge if (k.get('scope') or 'project')=='project'][:20]
        shared = self.knowledge_entries(task,scope='run')
        if shared:
            # 运行内共享条目改为**给序号与读取方式**，而不是把正文抄进 brief——
            # 抄进去就等于每个任务各拿一份副本，也就是这次事故的分叉来源。
            index.append({'shared_entries':len(shared),
                          'latest_seq':shared[-1]['seq'],
                          'how_to_read':'用 knowledge_read(scope="run", from_seq=0) 读取同一份有序共享信息'})
        self.record_message(task,{'role':'system','content':SYSTEM},source='system')
        # 技能目录随任务上下文发布一次：只有名称与摘要，正文由 `skill` 工具按需加载。
        self.record_message(task,{'role':'system','content':self.skill_catalog(task['workspace'],task.get('project_root'))},source='skill-catalog')
        self.record_message(task,{'role':'user','content':dumps({
            'task_id':task['id'],'parent_id':task['parent_id'],'goal_revision':task['revision'],
            'goal':goal,'workspace':task['workspace'],'knowledge_index':index,
            'review_model_id':run.get('review_model_id'),'features':run.get('features',{}),
            'note':'主模型可直接操作此工作区，子任务在独立副本。实际文件内容需用工具读取。需要独立审查时，可用 review_model_id 创建审查子任务。'})},source='task-brief')
        self.store.put('task',task)
        self.store.event('TaskSubmitted',{'goal':goal,'parent_id':task['parent_id'],'model':model_id},task)
        return task

    def recover(self):
        """Interrupted tool side effects require inspection, never blind replay."""
        self.workspace.recover_integrations()
        # 技能作业同样不重放：上次运行中途中断的调用只标记，由调用方重新提交。
        self.skill_invoker.recover()
        with self.store.transaction():
            for run in self.store.all('run'):
                # 升级期规则：旧记录没有 pause_reason 字段。若它停在 paused 且确实有未答问题，
                # 就按"等待人类回答"处理（回答到达即放行）；没有未答问题的暂停算人工暂停，不去碰。
                if run['status']!='paused' or run.get('pause_reason'):
                    run.setdefault('pause_reason',''); self.store.put('run',run)
                    continue
                if any(q['run_id']==run['id'] and q['status']=='open' for q in self.store.all('question')):
                    run['pause_reason']='human_question'
                else:
                    run['pause_reason']='manual'
                self.store.put('run',run)
            for t in self.store.all('task'):
                if t['status']=='running':
                    uncertain = self.store.db.execute("SELECT id,name FROM calls WHERE task_id=? AND status='running'",(t['id'],)).fetchall()
                    t['epoch'] += 1
                    if uncertain:
                        t['status']='failed'; t['error']='上次在工具执行中中断，请检查工作区后继续；不会自动重放工具。'
                        t['uncertain_calls']=[dict(x) for x in uncertain]
                    else:
                        t['status']='queued'; t['not_before']=now()
                    self.repair_history(t)
                    self.store.put('task',t)
                    self.store.event('TaskRecovered',{'status':t['status'],'uncertain_calls':t.get('uncertain_calls',[])},t)

    def repair_history(self,t):
        """为崩溃遗留的工具调用补上结果占位，并区分"没开始"与"结果未知"。

        - 调用表里没有这一行：崩溃发生在记录之前，工具从未开始 → `TOOL_NOT_STARTED`，重试是安全的。
        - 调用表里有一行仍为 running：已记录但没落结果 → `TOOL_OUTCOME_UNKNOWN`，有副作用时不得盲目重放。

        修复同时写进事件日志（`ToolRepairRecorded` 带内容），因此派生历史与缓存副本都会包含它。
        """
        pending = {}
        for m in self.task_history(t):
            if m['role']=='assistant':
                for call in m.get('tool_calls',[]): pending[call['id']]=call
            if m['role']=='tool': pending.pop(m['tool_call_id'],None)
        repaired=[]
        for id in pending:
            row=self.store.db.execute('SELECT status FROM calls WHERE id=?',(id,)).fetchone()
            if row is None:
                code,name='TOOL_NOT_STARTED',pending[id]['function']['name']
                content=('TOOL_NOT_STARTED: 这次工具调用在运行时记录它之前就中断了，'
                         '工具没有开始执行。确认当前状态后，如仍需要可以直接重试。')
            else:
                code,name='TOOL_OUTCOME_UNKNOWN',pending[id]['function']['name']
                content=('TOOL_OUTCOME_UNKNOWN: 这次工具调用已经被记录，但没有持久结果，结果未知。'
                         '只读或幂等操作可以重试；可能有副作用的操作必须先核对工作区或已保存日志，'
                         '不要盲目重放。')
            repaired.append({'call_id':id,'name':name,'code':code,'recorded':row is not None,'content':content})
        if repaired:
            self.store.event('ToolRepairRecorded',{'repaired':repaired},t)
            self._derive_cache=None
        return repaired

    async def start(self):
        self.recover()
        self.scheduler = asyncio.create_task(self.loop())

    async def stop(self):
        self.closed = True
        pending=self.skill_invoker.pending()
        if pending: await asyncio.gather(*pending,return_exceptions=True)
        for id in list(self.active): self.interrupt(id)
        if self.active: await asyncio.gather(*list(self.active.values()),return_exceptions=True)
        if hasattr(self,'scheduler'):
            self.scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self.scheduler
        self.store.close()

    async def loop(self):
        while not self.closed:
            try:
                self.wake_waiters()
                tasks = self.store.all('task')
                for t in sorted(tasks,key=lambda t:(-t.get('priority',0),t.get('last_scheduled',t['created']))):
                    if len(self.active)>=self.concurrency: break
                    if t['id'] in self.active or t['status']!='queued' or not self.current(t) or t.get('not_before',0)>now(): continue
                    if t.get('baseline_pending'):
                        old_ids={x['id'] for x in tasks if x['run_id']==t['run_id'] and x['revision']!=t['revision']}
                        if old_ids.intersection(self.active) or old_ids.intersection(self.processes): continue
                        self.workspace.setup(t)
                        t['baseline_pending']=False
                        self.store.put('task',t)
                    deps = [self.task(x) for x in t['dependencies']]
                    if any(d['status'] in {'failed','cancelled','superseded'} for d in deps):
                        self.fail(t,'依赖未成功完成；请调整任务后继续')
                        continue
                    if any(d['status']!='completed' for d in deps): continue
                    with self.store.transaction():
                        t['status']='running'; t['epoch']+=1; t['last_scheduled']=now(); self.store.put('task',t)
                        self.store.event('TaskClaimed',{'epoch':t['epoch']},t)
                    future = asyncio.create_task(self.step(t['id'],t['epoch']))
                    self.active[t['id']]=future
                    future.add_done_callback(lambda f,id=t['id']: self.active.pop(id,None))
                # 所有周期性维护（工作区扫描、评分刷新、对象回收、知识检查……）都在这里统一驱动。
                await self.maintenance.tick()
            except Exception as e:
                self.store.event('RuntimeError',{'error':str(e)})
            await asyncio.sleep(.1)

    def deliver(self, task_id, summary, sender=None, delivery='inbox', topic='', kind='message', refs=None,
                message_id=None, request_id=None, in_reply_to=None, relay_of=None, terminal=False):
        """投递消息，并维护「未答请求」这一等公民。

        消息协议（缺陷 1 的修法）：

        - **发起请求**：带 `request_id`（省略则生成），登记为 open；
        - **转发请求**（`relay_of=<request_id>`）：显式声明"我在替别人转达这个请求"。
          运行时**保留原请求 ID 不变**，只记下被转给了谁（`relayed_to`）并累加 hop。
          这是多跳之后答复仍能绑回最初问题的唯一办法——原实现每次都换新 ID，链路就断了。
        - **答复请求**（`in_reply_to=<request_id>`）：绑定到该请求并置 answered。
          只有"被问的那一方"（`target==sender` 且 kind 与请求不同）才享受兜底绑定；
          并发多问或角色不符时一律标 `unclaimed` 并明确拒绝，**不猜**。
        - **终结通知**（`terminal=True`，目前是作废通知）：**不登记为请求**，也就不要求回执。
          真机 `run_ca4aea2c3b3a` 里"不必再回答它"这句话本身被当成了一个 open 请求，
          于是对方要回应、回应又生成请求、收束时再作废一轮，24 条作废通知互相触发。
          通知的语义是"这件事结束了"，它必须自己也结束。
        """
        t=self.task(task_id)
        # 归档即结案：`wake` 会把一个已完成的任务重新排队、并把 run 拉回 active，
        # 那正是"归档后又被叫起来"，而它的工作区副本已经释放了。消息照旧入库留痕，
        # 但不唤醒。
        if delivery=='wake' and self.run(t['run_id']).get('archived'):
            raise ValueError('运行已归档（结案），不再唤醒它的任务；需要接着做请新建任务')
        dedup_key={'from_task':sender,'task_id':task_id,'kind':kind,'summary':summary}
        # 按内容压制"同一句话又发了一遍"的做法已移除：两次内容相同的消息可能是两次真实意图
        # （催促、重发、确认），判断它们是不是同一件事属于语义判断。去重只保留 message_id
        # 这一条真实不变量（见下方 `already` 分支）。
        relay=self._resolve_relay(relay_of,sender) if relay_of else None
        reply=self._resolve_reply(task_id,sender,in_reply_to,kind=kind)
        if reply is not None and not in_reply_to:
            assert reply.get('target')==sender and reply.get('kind')!=kind, '兜底绑定判据被破坏'
        if relay:
            # 转发不新建请求：沿用原 request_id，并如实记录发起者与经手者。
            entry={'task_id':task_id,'run_id':t['run_id'],'revision':t['revision'],
                   'from_task':sender,'summary':summary,'delivery':delivery,'topic':topic,
                   'kind':('relay:'+kind) if not kind.startswith('relay:') else kind,
                   'refs':refs or [],'created':now(),'consumed':False,'dedup_key':dedup_key,
                   'hop':int(relay.get('hop') or 1)+1,'request_id':relay['id'],'in_reply_to':None,
                   'origin_task':relay.get('requester'),'relayed_by':relay.get('relayer') or sender,
                   'status':'relayed'}
        else:
            # 终结通知（terminal=True）不生成 request_id、不登记为 open：
            # 它是"这件事结束了"，不该再索要一个回执。
            entry={'task_id':task_id,'run_id':t['run_id'],'revision':t['revision'],
                   'from_task':sender,'summary':summary,'delivery':delivery,'topic':topic,'kind':kind,
                   'refs':refs or [],'created':now(),'consumed':False,'dedup_key':dedup_key,
                   'hop':(reply or {}).get('hop') or 1,
                   'request_id':None if (reply or terminal) else (request_id or uid('req_')),
                   'in_reply_to':(reply or {}).get('request_id'),
                   'status':'answered' if reply else ('terminal' if terminal else ('unclaimed' if in_reply_to else 'open'))}
        message=entry | {'id':message_id or uid('msg_')}
        if any(x['id']==message['id'] for x in self.store.all('message')): return message
        with self.store.transaction():
            seq=self.store.event('MessageDelivered',message,t); message['seq']=seq
            self.store.put('message',message)
            if relay_of:
                # 转发：请求本身仍属于原始发起者，只在记录上追加"被转给了谁"。
                # 这样多跳之后答复依然能绑回最初的问题（缺陷 1 的根因就在丢了这条链）。
                self._record_relay(relay,message,sender)
            elif reply:
                self._settle_request(reply,message,t)
            elif in_reply_to:
                self.store.event('UnclaimedReply',{'message':message['id'],'from_task':sender,
                                                   'in_reply_to':in_reply_to,
                                                   'reason':self._unclaimed_reason(in_reply_to)},t)
            elif not terminal:
                record={'id':message['request_id'],'run_id':t['run_id'],'revision':t['revision'],
                        'requester':sender,'target':task_id,'kind':kind,'topic':topic,
                        'summary':str(summary or '')[:200],'status':'open','message_id':message['id'],
                        'created':now(),'hop':1,'relayed_to':[],'answered_by':[task_id]}
                self.store.put('answer_pending',record)
                self.store.event('RequestOpened',{'request_id':record['id'],'requester':sender,
                                                  'target':task_id},t)
            if self.current(t) and t['status']=='waiting' and delivery=='wake':
                self.wake(t,{'type':'message','message_id':message['id']})
            elif t['status']=='completed' and not t.get('superseded') and delivery=='wake' and t['revision']==self.run(t['run_id'])['revision']:
                t['status']='queued'; t['not_before']=now()+self.batch_seconds; self.store.put('task',t)
                run=self.run(t['run_id']); run['status']='active'; self.store.put('run',run)
        return message

    def _record_relay(self,relay,message,sender):
        """登记一次转发：请求仍属于原发起者，只追加"被转给了谁"与经手人。

        这样即使 A → 主持人 → C 转了两手，C 的答复按原 request_id 回绑时，
        运行时仍知道这条请求原本是谁提的（`requester` 不变）。
        """
        record=dict(relay)
        relayed=record.get('relayed_to') or []
        if message['task_id'] not in [item.get('task_id') for item in relayed if isinstance(item,dict)]:
            relayed=relayed+[{'task_id':message['task_id'],'by':sender,'at':now()}]
        record.update(relayed_to=relayed,relayer=sender,hop=int(record.get('hop') or 1)+1)
        # 关键：不要改写 target。target 是「这条请求最初问的是谁」，改写它会丢掉原目标，
        # 使原目标此后的答复也被判成无人认领。该回答的一方另记在 answered_by 里。
        answered=record.get('answered_by') or [record.get('target')]
        if message['task_id'] not in answered:
            answered=answered+[message['task_id']]
        record['answered_by']=answered
        self.store.put('answer_pending',record)
        self.store.event('RequestRelayed',{'request_id':record['id'],'requester':record.get('requester'),
                                           'relayed_by':sender,'to':message['task_id'],
                                           'hop':record['hop']},self.task(message['task_id']))

    def _resolve_relay(self,relay_of,sender):
        """校验"我在转发"的声明：必须是存在且仍 open 的请求，且转发者已见过它。"""
        if not relay_of:
            return None
        try:
            record=self.store.get('answer_pending',relay_of)
        except ValueError:
            raise ValueError(f'relay_of 指向的请求不存在：{relay_of}') from None
        if record.get('status')!='open':
            raise ValueError(f"请求 {relay_of} 当前状态为 {record.get('status')}，不能再转发")
        if sender and record.get('requester')==sender:
            raise ValueError('你自己就是该请求的发起者：直接发给目标即可，不需要声明 relay_of')
        return record

    def _unclaimed_reason(self, in_reply_to):
        try:
            record=self.store.get('answer_pending',in_reply_to)
        except ValueError:
            return f'找不到请求 {in_reply_to}（可能从未登记）'
        return f"请求 {in_reply_to} 的状态是 {record.get('status')}，答复不再被采纳（不要重复发送）"

    def _resolve_reply(self,task_id,sender,in_reply_to,kind=None):
        """绑定答复：显式 in_reply_to 优先；只有唯一 open 请求时才兜底绑定。"""
        records=[r for r in self.store.all('answer_pending')
        # 绑定答复时不能按「投递目标」过滤：答复通常是回给**发起者**的
        # （C 答 A 的问，而记录里的 target 是当初被问的 B），
        # 一旦按 target==task_id 过滤，记录会在绑定前就被丢掉——这正是长期修不好的原因。
                 if r.get('target')==task_id and r.get('status')=='open']
        if in_reply_to:
            open_replies=[r for r in self.store.all('answer_pending')
                          if r.get('status')=='open'
                          and sender in (set(r.get('answered_by') or []) | {r.get('target'), r.get('requester')})]
            match=next((r for r in open_replies if r['id']==in_reply_to),None)
            if match is None:
                return None
            return match
        # 兜底绑定只对「被问的那一方」生效：目标是 task_id、发起者是 sender。
        # 若把「发起者发来的新问题」也算进候选，第二个问题会被误判成对第一个请求的答复
        # （真机上表现为一个问题被标成 answered、新问题却没人记账）。
        if sender is None:
            return None
        # 兜底绑定要同时满足两点，缺一就会错（两种错法都在实测里出现过）：
        #   1) 发送者正是「被问的那一方」（record.target == sender）；
        #   2) 这条消息不是同一类请求——发起者随后再发一个**同类请求**（同样 kind='q'）
        #      时，绝不能把上一条当成它的答复（否则一个问题被标 answered、新问题没人记账）。
        # 答复的 kind 与请求通常不同（如 question/answer），因此这一条能干净地区分两者。
        answers=[r for r in records
                 if sender in (set(r.get('answered_by') or []) | {r.get('target')})
                 and r.get('kind')!=kind]
        return answers[0] | {'bound_by_default':True} if len(answers)==1 else None

    def _settle_request(self,record,message,source_task):
        settled=dict(record)
        settled.update(status='answered',answered_at=now(),answer_message=message['id'])
        self.store.put('answer_pending',settled)
        self.store.event('RequestAnswered',{'request_id':settled['id'],'requester':settled.get('requester'),
                                            'target':settled.get('target'),'answer_message':message['id'],
                                            'bound_by_default':bool(record.get('bound_by_default'))},source_task)

    def open_requests(self,requester=None,target=None):
        """未答请求：`complete()` 的排空检查、`tasks_close` 与 `usage_report` 共用。"""
        result=[]
        for record in self.store.all('answer_pending'):
            if record.get('status')!='open': continue
            if requester is not None and record.get('requester')!=requester: continue
            if target is not None and record.get('target')!=target: continue
            result.append(record)
        return result

    def forfeit_requests(self,requester,request_ids,reason=''):
        """显式放弃未答请求，并通知对端不必再答——避免产生无人认领的幽灵答复。"""
        done=[]
        for request_id in request_ids or []:
            try:
                record=self.store.get('answer_pending',request_id)
            except ValueError:
                continue
            if record.get('requester')!=requester or record.get('status')!='open':
                continue
            record.update(status='forfeited',forfeited_at=now(),
                          reason=reason or '发起方在收束时放弃')
            self.store.put('answer_pending',record)
            done.append(record)
            self.store.event('RequestForfeited',{'request_id':request_id,'reason':record['reason']},
                             self.task(record['target']))
            self.deliver(record['target'],
                         f"请求 {request_id} 已被发起方放弃：{record['reason']}。不必再回答它。",
                         requester,delivery='wake',topic='request:'+request_id,
                         kind='request_forfeited',message_id=uid('msg_'),terminal=True)
        return done

    def wake(self,t,cause):
        if t['status']!='waiting': return
        t.update(status='queued',wait=None,not_before=now()+self.batch_seconds)
        # 记住被唤醒的原因：下一次 yield 据此判断这是"有内容的一轮"还是"纯计时到期"。
        t['wake_cause']=(cause or {}).get('type') or 'unknown'
        t['wake_cause_at']=now()
        self.store.put('task',t)
        self.store.event('TaskWoken',cause,t)
        # Wake reasons are model-visible, even for deadlines with no inbox message.
        msg={'id':uid('msg_'),'task_id':t['id'],'run_id':t['run_id'],'revision':t['revision'],
             'summary':'恢复原因：'+dumps(cause),'kind':'control','delivery':'inbox','topic':'wake',
             'seq':self.store.cursor(),'consumed':False,'created':now(),'from_task':None}
        self.store.put('message',msg)

    def wake_waiters(self):
        """检查所有等待条件，该醒的就叫醒。

        判据有三类（真机 `run_64670df8d7e8` 的死锁把前两类一起暴露了）：

        1. **订阅的任务结束**：`task_ids` 里某个任务进入终态且发生在游标之后；
        2. **订阅主题的消息**：`topics` 里某个主题的新消息；
        3. **订阅任务发来的消息**：`task_ids` 只订阅"任务结束"这件事在协作场景里几乎没用——
           子任务在会话进行中根本不会结束，而它会不断发消息。所以 `task_ids` 同时意味着
           "**这些任务发来的消息也算**"。原先的等待对没有 topics 的订阅者是完全失聪的：
           主调度在等 C/A/B，C 把指引发进了它的收件箱，它却什么都匹配不到。
           只订阅 topics 的等待**不受影响**（没有 topic 的消息依旧不唤醒它），
           因此"进度通知不要 wake"的既有语义保持。

        另外：时限由模型自己声明（`timeout_seconds` / `max_wait_seconds`）。**没有声明时限
        就是"只被订阅的事件唤醒"**，不会自动到期，也不会被运行时补一个兜底上限——
        设计文档 §5.2 的语义是"无事件时不调用模型轮询"。
        """
        with self.store.transaction():
            for t in self.store.all('task'):
                if t['status']!='waiting' or not self.current(t) or not t.get('wait'): continue
                w=t['wait']; matched=[]
                subscribed=list(w.get('task_ids',[]))
                for id in subscribed:
                    other=self.store.db.execute('SELECT body FROM records WHERE kind=? AND id=?',('task',id)).fetchone()
                    if other is None: continue
                    d=json.loads(other['body'])
                    if d['status'] in TERMINAL and d.get('terminal_seq',0)>w['after_cursor']:
                        matched.append('task:'+id)
                for m in self.store.all('message'):
                    if m['task_id']!=t['id'] or m['revision']!=t['revision']: continue
                    if m['consumed'] or m.get('seq',0)<=w['after_cursor']: continue
                    # 过滤类等待（只给 topics）：仍然只有主题能唤醒它。
                    if subscribed:
                        if m.get('topic') in w.get('topics',[]) or m.get('from_task') in subscribed:
                            matched.append('message:'+m['id'])
                    elif m.get('topic') in w.get('topics',[]):
                        matched.append('topic:'+m['topic'])
                required=len(w.get('task_ids',[]))+len(w.get('topics',[]))
                ready=bool(matched) if w.get('mode','any')=='any' else required>0 and len(set(matched))>=required
                # 到期即交还给模型自己决定，不再静默顺延窗口：等多久是模型声明的，
                # 不是运行时替它续的。
                expired=any(x is not None and now()>=float(x)
                            for x in (w.get('deadline'),w.get('max_deadline')))
                if ready or expired:
                    self.wake(t,{'type':'deadline' if expired else 'subscription','matched':list(set(matched))})
                    continue

    def consume_inbox(self,t):
        messages=sorted((m for m in self.store.all('message') if m['task_id']==t['id'] and m['revision']==t['revision'] and not m['consumed']),
                        key=lambda m:(m.get('seq',0),m.get('created',0),m['id']))
        if messages:
            # No summarizing model. Preserve distinct facts; process in bounded batches.
            batch=messages[:30]
            remaining=len(messages)-len(batch)
            # 只把模型用得上的字段给它，并说明每个字段怎么用。
            # 原先是整条 dump：`request_id` 混在十几个字段里，模型既看不出它的用途，
            # 也就无从在转发/答复时把它带下去（缺陷 1 的一半原因在这里）。
            visible=[{'message_id':m['id'],'from_task':m.get('from_task'),'kind':m.get('kind'),
                      'request_id':m.get('request_id'),'in_reply_to':m.get('in_reply_to'),
                      'origin_task':m.get('origin_task'),'relayed_by':m.get('relayed_by'),
                      'hop':m.get('hop'),'summary':m.get('summary')}
                     for m in batch if m.get('kind')!='control']
            # 批次本身保持「可解析的一整段 JSON」：任何附加说明另发一条，否则
            # 依赖解析批次内容的适配器（含 demo）会解析失败。
            self.record_message(t,{'role':'user','content':'收件箱批次：'+dumps(visible)},source='inbox')
            if t.get('inbox_guide_sent'):
                pass
            else:
                self.record_message(t,{'role':'user','content':
                    '消息字段用法：发起请求时记下自己的 request_id；答复别人用 agents_send(in_reply_to=该 request_id)；'
                    '替别人转达某个请求用 agents_send(relay_of=该 request_id)——运行时保留原请求 ID 并记录经手人，'
                    '答复因此能绑回最初的问题。只回一个“是”而不带 in_reply_to，在该目标有多个未答问题时不会被采纳。'},
                    source='inbox-guide')
                t['inbox_guide_sent']=True
                self.store.put('task',t)
            for m in batch:
                m['consumed']=True; self.store.put('message',m)
            t['inbox_cursor']=max(m.get('seq',0) for m in batch)
            t['inbox_remaining']=remaining
            self.store.event('InboxBatchReady',{'ids':[m['id'] for m in batch],'remaining':remaining},t)
        else:
            t['inbox_remaining']=0

    async def step(self,id,epoch):
        """一次任务调度内推进模型轮次。

        主动 compact 在本轮工具批次结束后立即生效，并在同一次调度里用压缩后的上下文
        继续模型调用；因此不需要重新入队，也没有人工批准环节。
        """
        try:
            t=self.task(id)
            with self.store.transaction():
                self.consume_inbox(t)
                t['last_call_cursor']=self.store.cursor()
                self.store.put('task',t)
            # 供模型读取的历史一律来自日志派生结果（恢复后缓存副本可能为空）。
            for _ in range(MAX_COMPACT_PER_STEP+1):
                t=self.task(id)
                if t.get('compact_requested') or self._over_window(t):
                    await self.compact(t,epoch)
                    t=self.guard(t,epoch)
                    if self._over_window(t):
                        raise RuntimeError('有效上下文仍超过模型窗口；请增大窗口或进一步精简目标/工具。原始日志已保留。')
                keep_running=await self._model_round(t,epoch)
                if not keep_running:
                    self.park(self.task(id),epoch,'run-paused')
                    return
                fresh=self.task(id)
                if fresh['epoch']!=epoch or not self.current(fresh) or fresh['status']!='running':
                    self.park(fresh,epoch,'run-paused')
                    return
            raise RuntimeError('单次调度内连续压缩次数过多；请检查目标与工具规模。原始日志已保留。')
        except asyncio.CancelledError:
            pass
        except Exception as e:
            fresh=self.task(id)
            if fresh['epoch']==epoch and fresh['status'] not in TERMINAL:
                # 暂停期间 guard/compact 抛的错不是"这个任务失败了"，是"这轮先别做"。
                if self.park(fresh,epoch,'run-paused'): return
                self.fail(fresh,describe_error(e))

    def _over_window(self,t):
        config=self.store.get('model',t['model_id'])
        history=self.task_history(t)
        size=len(dumps(history).encode())+len(dumps(self.schemas_for_task(t)).encode())
        room=int(config.get('context_window',32768))-int(config.get('max_output',4096))-2048
        return size>room

    async def _call_model(self,t,history,epoch,tools=TOOLS_FOR_TASK):
        """模型调用 + 跨模型故障转移。

        上游异常不该让任务判死：对**模型侧**的失败（这个模型在本账户下用不了、或这次答不了）
        按 `Models.failover_targets` 的顺序换一个模型执行同一件事。每次尝试都留自己的请求信封
        与 `ModelCalled`，换的时候写 `ModelFailover`——换过哪个、为什么换、换成了谁，
        都能从日志逐条复原，**不静默**。

        换成功后**任务继续用新模型**（`model_failover_from` 记住原模型，`model_failover_chain`
        记住整条链）：否则故障期间每一轮都要先交一次注定失败的调用，每个任务每轮都在烧钱。

        候选全部失败时不再换，也不在这里判死——原样抛出，由调用方按既有语义处理。
        凭据、参数、上下文超限这类原因不换模型：它们不是模型侧的原因。

        `tools` 省略时给本任务的完整工具集；**压缩这类"不该调工具"的调用必须显式传 None**，
        否则模型会以为可以调工具，返回一个没有正文的 tool_call，压缩结果就不再是 JSON。
        """
        schemas=self.schemas_for_task(t) if tools is TOOLS_FOR_TASK else tools
        candidates=self.models.failover_targets(t['model_id'])
        while True:
            header=self.record_request(t,history)
            self.store.event('ModelCalled',{'model':t['model_id'],'context_epoch':t['context_epoch'],
                                            'input_ref':header['input_ref'],'request_id':header['id']},task=t)
            try:
                return await self.models.call(t['model_id'],history,schemas,
                                              run_id=t['run_id'],task_id=t['id'])
            except ModelError as error:
                target=next((item for item in candidates if item['id']!=t['model_id']),None)
                fresh=self.task(t['id'])
                if error.code not in FAILOVER_CODES or target is None or fresh['epoch']!=epoch:
                    tried=list(fresh.get('model_failover_chain') or [])
                    if tried:
                        raise ModelError(f'{error}；已尝试故障转移：{" → ".join(tried)} → {fresh["model_id"]}',
                                         error.code) from None
                    raise
                candidates=[item for item in candidates if item['id']!=target['id']]
                failed=fresh['model_id']
                t=fresh
                with self.store.transaction():
                    t.setdefault('model_failover_from',failed)
                    t['model_failover_chain']=list(t.get('model_failover_chain') or [])+[failed]
                    t['model_id']=target['id']
                    self.store.put('task',t)
                    self.store.event('ModelFailover',{'from':failed,'to':target['id'],'code':error.code,
                                                      'source':target['source'],'error':str(error)},t)

    async def _model_round(self,t,epoch):
        """调用一次模型并执行它请求的工具批次。返回 True 表示需要在本轮内压缩后继续。"""
        history=self.task_history(t)
        t['last_model_at']=now()
        self.store.put('task',t)
        msg,usage=await self._call_model(t,history,epoch)
        fresh=self.task(t['id'])
        if fresh['epoch']!=epoch or not self.current(fresh):
            # 暂停中的运行也走这条路（`current()` 为假）：把任务交回队列而不是丢在 running。
            if self.park(fresh,epoch,'run-paused'): return False
            self.store.event('LateModelResult',{'epoch':epoch,'message':msg,'usage':usage},t)
            return False
        t=self.guard(t,epoch)
        with self.store.transaction():
            self.store.event('ModelReturned',{'message':msg,'usage':usage},t)
            clean={k:msg[k] for k in ('role','content','tool_calls','reasoning_content') if k in msg}
            clean['role']='assistant'
            self.record_message(t,clean,source='assistant')
            self.store.put('task',t)
        calls=msg.get('tool_calls',[])
        if not calls:
            fresh=self.task(t['id'])
            if fresh.get('inbox_remaining',0):
                fresh['status']='queued'; fresh['not_before']=now()
                self.store.put('task',fresh)
                self.store.event('TaskQueued',{'reason':'inbox-continuation','remaining':fresh['inbox_remaining']},fresh)
                return False
            # 服务型 agent 的核心冲突（真机事故）：**收件箱清空发生在步骤开始时**，
            # 模型却在思考中；思考这几秒里到达的消息在 `complete()` 之前仍是未读。
            # 旧实现据此抛 ValueError 判负——把「你还有话没答」当成致命错误，
            # 于是持续服务型任务（出题人/主持人）在并发提问下必然暴毙，换替补也一样。
            # 现在的判断：这一轮**没有调用任何工具**，就没资格交付——只要还有未读消息、
            # 自己发出的请求还没人答、或子任务未结束，就回到队列再跑一轮，
            # 由模型自己用 agents_send / tasks_yield / tasks_complete 收尾。
            # 这里**不设连续轮次上限**：退回几次是模型的判断，不是运行时的计数器。
            unsettled=self.unsettled_reasons(t)
            if unsettled:
                self.defer_text_round(t,unsettled,msg.get('content',''))
                return False
            self.complete(t,{'summary':msg.get('content',''),'verification_status':'not_verified'})
            return False
        for call in calls:
            name=call['function']['name']
            try:
                args=json.loads(call['function']['arguments'])
                if not isinstance(args,dict): raise ValueError('工具参数必须为对象')
                fresh=self.guard(t,epoch)
                if fresh['status']!='running': raise ValueError('等待/完成必须放在本轮最后一个工具')
                result=await self.execute(fresh,epoch,call['id'],name,args)
            except asyncio.CancelledError: raise
            except Exception as e:
                result={'error':str(e)}
            fresh=self.task(t['id'])
            if fresh['epoch']!=epoch: return False
            with self.store.transaction():
                self.record_tool_result(fresh,call['id'],result)
                self.store.put('task',fresh)
        fresh=self.task(t['id'])
        if fresh['epoch']!=epoch: return False
        # 主动 compact：工具调用配对已完整落盘，压缩点固定在这里，模型当轮的工具结果留在保留尾部。
        if fresh['status']=='running' and fresh.get('compact_requested'):
            await self.compact(fresh,epoch)
            fresh=self.task(t['id'])
            return fresh['epoch']==epoch and fresh['status']=='running'
        if fresh['status']=='running':
            fresh['status']='queued'; fresh['not_before']=now(); self.store.put('task',fresh)
            self.store.event('TaskQueued',{},fresh)
        return False

    def unsettled_reasons(self,t):
        """这一轮**纯文本**离「可以交付」还差什么；返回人类可读的原因列表。

        与 `complete()` 的守卫共用同一套判据（未读消息 / 未答请求 / 未完成子任务），
        区别只在处理方式：`complete()` 是模型**主动调用**工具后的兜底（此时拒绝是对的，
        模型已经明确表态要交付）；这里是模型**根本没表态**（没有任何工具调用），
        所以不能当成交付尝试去判负，只能让它再跑一轮。
        """
        reasons=[]
        unread=[m for m in self.store.all('message')
                if m['task_id']==t['id'] and m['revision']==t['revision'] and not m['consumed']]
        if unread:
            froms='、'.join(sorted({str(m.get('from_task') or '未知') for m in unread}))
            reasons.append(f'收件箱还有 {len(unread)} 条未读消息（来自 {froms}）——这些是你在思考期间到达的，必须处理')
        outstanding=self.open_requests(requester=t['id'])
        if outstanding:
            listing='、'.join(f"{r['id']}（→{r['target']}）" for r in outstanding)
            reasons.append(f'你自己发出的 {len(outstanding)} 个请求还没人回答：{listing}')
        pending=[x['id'] for x in self.store.all('task')
                 if x['parent_id']==t['id'] and x['revision']==t['revision'] and x['status'] not in TERMINAL]
        if pending:
            reasons.append('未完成的子任务：'+', '.join(pending))
        return reasons

    def defer_text_round(self,t,reasons,text):
        """把「纯文本但还没交付」的一轮退回队列，并明确告诉模型为什么、该用什么工具。

        为什么不直接交付：对持续服务型任务（出题人、主持人、答疑者）来说，
        纯文本就是它的正常产出，把它当成"结项"是把两种语义混为一谈。
        为什么不抛出判负：那是真机事故里 C/C3 的死法——模型没有犯错，
        是运行时在"收件箱已清空"和"又有消息到达"之间做了错误推断。

        退回次数**不设上限**，因此不存在"连续 N 轮就判负"：本轮的事实（没有工具调用、
        待办还在）照常记账，由模型自己决定怎么收尾。
        """
        t['status']='queued'; t['not_before']=now()
        self.store.put('task',t)
        self.store.event('TextRoundDeferred',{'reasons':reasons,'text':str(text or '')[:200]},t)
        hint=''
        inbound=self.open_requests(target=t['id'])
        if inbound:
            first=inbound[0]
            # 真机 `run_5b90b2ff368f`：玩家 B 有两轮都只回纯文本，最后因为这一条被判负。
            # 说明必须**可直接照抄**，而不是再解释一遍概念。
            hint=(f'你手里还有 {len(inbound)} 个等着你回答的问题。请直接照抄这一行来回答第一个：'
                  f'agents_send(task_id="{first["requester"]}", summary="<你的回答>", '
                  f'in_reply_to="{first["id"]}")。'
                  f'若你已经在上面写了答案，把它放进这条调用里；不要再只回一段文字。 ')
        self.deliver(t['id'],
                     '你这一轮只输出了纯文本、没有调用任何工具，因此**没有被当作交付**，也没有发给任何人。'
                     +hint+
                     '待办仍未清理：'+'；'.join(reasons)+'。'
                     '请在本轮用工具明确表态：答复别人用 agents_send(task_id=对方, summary=答复, in_reply_to=对方的 request_id)；'
                     '还需要等消息就用 tasks_yield；确实做完了才用 tasks_complete（提交会自动作废你未答的请求，不会卡住你）。'
                     '只回一句纯文本（例如只回一个“是”）不会送达任何一方。',
                     sender=None,delivery='inbox',topic='text-deferred',kind='control',terminal=True)
        return False

    def record_request(self,t,history=None):
        """为这一次模型请求落一条可重建的信封记录。

        信封只存引用与哈希，不复制正文：`input_ref`/`prompt_ref`/`tools_ref` 都指向内容对象库，
        因此相同的系统提示词与工具 schema 只存一份，而每个请求仍然可以逐字重建。
        `seq` 是记录时的全局事件游标，用于回答"通过的是哪一次请求"。
        """
        config=self.store.get('model',t['model_id'])
        if history is None: history=self.task_history(t)
        prompt=self.summarize_prompt(history)
        input_ref=self.store.blob(dumps(history))
        tools_ref=self.store.blob(dumps(self.schemas))
        header={'id':uid('req_'),'task_id':t['id'],'run_id':t['run_id'],'epoch':t['epoch'],
                'revision':t['revision'],'context_epoch':t['context_epoch'],'seq':self.store.cursor(),
                'model_id':config['id'],'provider_id':config.get('provider_id'),'model':config.get('model'),
                'api':providers.api_of(config),'adapter':providers.adapter_of(config),
                'base_url':config.get('base_url'),'api_key_ref':config.get('api_key') or config.get('key_env',''),
                'reasoning_effort':config.get('extra',{}).get('reasoning_effort') if isinstance(config.get('extra'),dict) else None,
                'max_output':config.get('max_output'),'context_window':config.get('context_window'),
                'disable_response_storage':bool(config.get('disable_response_storage',False)),
                'headers':dict(config.get('headers') or {}),
                'input_ref':input_ref,'input_bytes':len(dumps(history).encode()),'messages':len(history),
                'prompt_ref':self.store.blob(prompt),'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
                'tools_ref':tools_ref,'tools_sha256':hashlib.sha256(dumps(self.schemas).encode()).hexdigest(),
                'tools_count':len(self.schemas),'created':now()}
        self.store.put('request',header)
        return header

    @staticmethod
    def summarize_prompt(history):
        """系统提示词由多条 system 消息拼成：基础提示 + 技能目录。

        记录它才能判断某次请求用的到底是哪版提示词与哪版技能目录；拼接顺序即模型看到的
        顺序，因此 sha256 可以直接用于比对。`assemble_request()` 回放的 `prompt` 是已经
        拼好的字符串，这里对它做幂等处理，使两种调用方式得到同一个结果。
        """
        if isinstance(history,str):
            return history
        parts=[message.get('content') or '' for message in history or [] if message.get('role')=='system']
        return '\n\n'.join(parts)

    def assemble_request(self,header):
        """按记录逐字重建请求信封：{config, messages, tools}，不再依赖任何进程内状态。"""
        config={'id':header['model_id'],'model':header['model'],'base_url':header['base_url'],
                'adapter':header['adapter'],'api':header['api'],'context_window':header['context_window'],
                'max_output':header['max_output'],'headers':header.get('headers') or {},
                'disable_response_storage':header['disable_response_storage']}
        return {'config':config,'prompt':self.store.read_blob(header['prompt_ref']).decode(),
                'messages':json.loads(self.store.read_blob(header['input_ref'])),
                'tools':json.loads(self.store.read_blob(header['tools_ref']))}

    def request_headers(self,task_id=None,run_id=None,limit=50):
        """按最近优先返回请求记录；供报告、控制台与外部程序回答"当时的请求是什么样"。"""
        items=[x for x in self.store.all('request')
               if (task_id is None or x['task_id']==task_id) and (run_id is None or x['run_id']==run_id)]
        return sorted(items,key=lambda x:x['created'],reverse=True)[:limit]

    async def execute(self,t,epoch,call_id,name,args):
        previous=self.store.db.execute('SELECT * FROM calls WHERE id=?',(call_id,)).fetchone()
        if previous:
            if previous['status']=='done': return json.loads(previous['result'])
            raise RuntimeError('该调用状态未知，不会自动重复执行；请先检查')
        if name not in self.handlers: raise ValueError('未知工具：'+name)
        schema=next(s['function']['parameters'] for s in self.schemas if s['function']['name']==name)
        validate(schema,args)
        with self.store.transaction():
            self.store.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?)',(call_id,t['id'],epoch,name,dumps(args),'running',None))
            self.store.event('ToolCalled',{'call_id':call_id,'name':name,'args':args},t)
        try:
            result=await self.handlers[name](t,args)
        except asyncio.CancelledError: raise
        except Exception as e: result={'error':str(e)}
        with self.store.transaction():
            self.store.db.execute("UPDATE calls SET status='done',result=? WHERE id=?",(dumps(result),call_id))
            self.store.event('ToolResult',{'call_id':call_id,'name':name,'result':result},t)
        encoded=dumps(result)
        if len(encoded)>12000:
            return {'output_ref':self.store.blob(encoded),'excerpt':encoded[:6000],
                    'truncated':True,'note':'使用 artifacts_read 按需读取完整工具结果'}
        return result

    def heavy_usage(self,run_id=None):
        """heavy 档的调用数与 token：让"重模型花在哪"可查（缺陷 4 的可观测性）。"""
        totals={'calls':0,'tokens':0}
        for record in self.store.all('model_usage'):
            if run_id is not None and record.get('run_id')!=run_id: continue
            try:
                config=self.store.get('model',record.get('model_id'))
            except ValueError:
                continue
            if self.models.tiers_of(config)!='heavy': continue
            totals['calls']+=1; totals['tokens']+=record.get('tokens',0)
        return totals

    def task_usage(self,task_id):
        """该任务的模型用量（调用数 / tokens / 重试次数）：报告与 `usage_report` 共用。"""
        records=[x for x in self.store.all('model_usage') if x.get('task_id')==task_id]
        return {'calls':len(records),'tokens':sum(x.get('tokens',0) for x in records),
                'retries':sum(x.get('retries',0) for x in records)}

    def fail(self,t,error):
        with self.store.transaction():
            self.repair_history(t)
            t.update(status='failed',error=error)
            t['terminal_seq']=self.store.event('TaskFailed',{'error':error},t)
            self.store.put('task',t)
            if t['parent_id']: self.deliver(t['parent_id'],error,t['id'],'wake','task:'+t['id'],'blocker')

    def verification(self,t,args,verification_status):
        """把模型的验证声明绑定到**实际执行记录**，并把原始事实记下来。

        模型的自由文本（summary / evidence 字符串）是**声明**；只有绑定到执行记录的
        才是**证据**。绑定规则：`execution_ref`/`ref` 指向本任务的执行记录；或
        `command` 与某条执行记录的命令逐字相同；或声明文本里直接包含某条命令原文。

        这里**只记录事实，不下判词**：不做"可信 / 弱 / 过期 / 退出码被掩盖"的分级，
        也不给出 `verified_by_command` 这类结论。退出码、是否超时、执行时的文件清单
        都原样保留，`verification_status` 是模型自己的声明——**判断证据够不够，
        是模型与人的事，不是运行时的事**。
        """
        records=[x for x in self.store.all('execution') if x['task_id']==t['id']]
        by_id={x['id']:x for x in records}
        items=[]
        for entry in args.get('evidence') or []:
            if isinstance(entry,dict):
                items.append(dict(entry))
            elif isinstance(entry,str):
                items.append({'kind':'claim','text':entry})
        for item in items:
            ref=item.get('execution_ref') or item.get('ref')
            record=by_id.get(ref) if isinstance(ref,str) else None
            if record is None and item.get('kind')=='command' and item.get('command'):
                record=next((x for x in records if x['command']==item['command']),None)
            if record is None and item.get('kind')=='claim' and item.get('text'):
                text=item['text']
                record=next((x for x in records if x['command'] in text),None)
            if record is not None:
                item['execution_ref']=record['id']
                item['exit_code']=record['exit_code']
                item['timed_out']=record['timed_out']
                item['manifest_ref']=record['manifest_ref']
                item['output_ref']=record['output_ref']
                item['command']=record['command']
                item['kind']='command'
            else:
                item['bound']=False
        commands=[x for x in items if x['kind']=='command']
        inspections=[x for x in items if x['kind']!='command']
        return {'verification_status':verification_status,
                'evidence':items,
                'command_executions':len(commands),
                'inspections':len(inspections),
                'execution_records':len(records)}

    def complete(self,t,args):
        # 不再因为"还有未完成的子任务"而拒绝交付：交付时机是模型的判断（设计文档 §5）。
        # 子任务未结束这件事照常留在任务记录里，父模型自己看得见。
        #
        # 保留下面这一条：收件箱里还有未读消息时不能交付。这不是业务判断，而是投递语义——
        # 那些消息已经算在它的收件箱里，静默交付等于把它们丢掉。
        unread=[m for m in self.store.all('message') if m['task_id']==t['id'] and m['revision']==t['revision'] and not m['consumed']]
        if unread:
            raise ValueError(f'仍有 {len(unread)} 条未处理消息；请继续处理收件箱后再完成')
        # 排空检查（缺陷 3）：自己发出、还没人回答的请求不能在收束时静默丢下——
        # 否则对方事后补答会变成无人认领的幽灵消息。
        forfeit=args.get('forfeit_requests') or []
        if forfeit:
            self.forfeit_requests(t['id'],forfeit,args.get('forfeit_reason',''))
        outstanding=self.open_requests(requester=t['id'])
        if outstanding:
            # 请求比请求方活得久是**第二个真机缺陷**（`run_8cd95bde07dd`）：玩家猜中后完成任务，
            # 它的问题仍挂在出题人名下为 open，出题人反复尝试答复一个已经不在的对话方
            # （34 次调用大量耗在这里），而请求方自己也因为"还有 4 个请求没人回答"收不了束。
            # 模型已经决定交付了，此刻唯一有意义的动作就是**把这些请求关掉**：
            # 自动作废（带明确原因，对端会收到"不必再答"的通知），并写 `RequestsAutoForfeited`
            # 记账——不静默丢弃，否则对方会继续等一个永远不会来的答复。
            ids=[r['id'] for r in outstanding]
            self.forfeit_requests(t['id'],ids,f'请求方 {t["id"]} 已收束并交付，不必再回答')
            self.store.event('RequestsAutoForfeited',{'request_ids':ids,
                             'reason':'请求方在 tasks_complete 时收束；避免请求比请求方活得久'},t)
        with self.store.transaction():
            self.workspace.reconcile(t,'task-boundary')
            changes=self.workspace.changes(t)
            reasons=args.get('file_reasons') or {}
            verification_status=args.get('verification_status')
            if verification_status is None:
                verification_status='verified' if args.get('verified',False) else 'not_verified'
            if verification_status not in {'verified','failed','not_verified','unknown'}:
                raise ValueError('verification_status 必须为 verified、failed、not_verified 或 unknown')
            verification=self.verification(t,args,verification_status)
            report={'id':uid('report_'),'run_id':t['run_id'],'task_id':t['id'],'revision':t['revision'],
                    'summary':args['summary'],'files':[{'path':c['path'],'before':c['before'],'after':c['after'],
                    'before_mode':c.get('before_mode'),'after_mode':c.get('after_mode'),
                    'reason':reasons.get(c['path'],'未提供原因')} for c in changes],
                    'omitted_files':list(t.get('omitted_files',[])),
                    'impact':args.get('impact'), 'unknowns':args.get('unknowns',[]),
                    'execution_records':[x for x in self.store.all('execution') if x['task_id']==t['id']],
                    'model_usage':self.task_usage(t['id']),
                    'request_headers':[{'id':h['id'],'seq':h['seq'],'model_id':h['model_id'],
                                        'context_epoch':h['context_epoch'],'messages':h['messages'],
                                        'input_bytes':h['input_bytes'],'tools_count':h['tools_count'],
                                        'tools_sha256':h['tools_sha256'],'prompt_sha256':h['prompt_sha256'],
                                        'created':h['created']} for h in self.request_headers(t['id'],limit=200)][::-1],
                    'manifest_ref':self.store.blob(dumps(t['manifest'])),'modes':t.get('modes',{}),
                    # 模型声明与绑定到执行记录的原始证据分开保存，不做分级、不下判词。
                    'evidence':args.get('evidence',[]),'unresolved':args.get('unresolved',[]),
                    'verification_status':verification_status,
                    'verification_evidence':verification,
                    'verified_claim':verification_status=='verified','created':now()}
            self.store.put('report',report)
            self.store.event('ChangeReportPublished',report,t)
            t.update(status='completed',result=args['summary'],report_id=report['id'])
            t['terminal_seq']=self.store.event('TaskCompleted',{'summary':args['summary'],'report_id':report['id']},t)
            self.store.put('task',t)
            # 报告只留在 report 记录里；知识库不再自动堆积"任务报告"条目，
            # 只有模型显式调用 broadcast 才会产生可复用知识。
            if t['parent_id']: self.deliver(t['parent_id'],args['summary'],t['id'],'inbox','task:'+t['id'],'result',[report['id']])
            else:
                run=self.run(t['run_id'])
                pending=[x for x in self.store.all('task') if x['run_id']==run['id'] and x['revision']==run['revision'] and x['id']!=t['id'] and x['status'] not in TERMINAL]
                if not pending:
                    run['status']='completed'; self.store.put('run',run)

    def interrupt(self,id):
        process=self.processes.get(id)
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError): os.killpg(process.pid,signal.SIGKILL)
        future=self.active.get(id)
        if future and future is not asyncio.current_task(): future.cancel()

    def release_task_processes(self,id):
        """释放该任务还欠着的**所有命令**，包括已经脱离父进程的常驻作业。

        为什么不能只靠 `self.processes`：它只记得"此刻正在跑的那一条 shell"。命令一旦
        `setsid`/`nohup ... &` 就脱离了——真机 `run_1da778c90c48` 里 930 世界处理的
        `dataset_runner.py` 被 systemd 收养、自开了新会话，于是取消任务**杀不到它**，
        CPU 与磁盘一直被占着，而看门狗 `while kill -0 <pid>` 还在等一个永远不会被释放的进程。

        判据只能是**环境变量**：进程组、会话、父进程都会在脱离时变掉，而 `UNISON_TASK_ID`
        跨 fork/exec 一路继承。按它清扫，跑多远都找得回来。

        杀法与 `interrupt()` 一致：是进程组组长就杀整组（连带没带标记的子进程），
        否则只杀该进程（组不是它的，动整组会误伤别人——比如服务自己）。
        返回被杀清单，调用方写进事件：**释放了什么必须可核对**。
        """
        marker=f'UNISON_TASK_ID={id}'.encode()
        killed=[]
        for _ in range(2):   # 第二遍收第一遍期间新生的子进程
            found=self._marked_processes(marker)
            if not found: break
            for pid,command in found:
                try:
                    if os.getpgid(pid)==pid: os.killpg(pid,signal.SIGKILL)
                    else: os.kill(pid,signal.SIGKILL)
                except (ProcessLookupError,PermissionError):
                    continue
                killed.append({'pid':pid,'command':command})
        return killed

    @staticmethod
    def _marked_processes(marker):
        """按环境标记找出还活着的进程（pid + 命令行）。读不到的进程跳过，不报错。"""
        found=[]
        for entry in os.listdir('/proc'):
            if not entry.isdigit(): continue
            pid=int(entry)
            if pid==os.getpid(): continue
            try:
                with open(f'/proc/{pid}/environ','rb') as handle:
                    if marker not in handle.read().split(b'\0'): continue
                with open(f'/proc/{pid}/cmdline','rb') as handle:
                    command=handle.read().replace(b'\0',b' ').decode(errors='replace').strip()
            except OSError:
                continue
            found.append((pid,command[:300]))
        return found

    def cancel_task(self,id,status='cancelled'):
        t=self.task(id)
        children=[x for x in self.store.all('task') if x['parent_id']==id]
        for child in children: self.cancel_task(child['id'],status)
        self.interrupt(id)
        # `interrupt()` 只终止**这一轮**（在飞的模型调用与正在跑的那条 shell）。
        # 任务此前留下的常驻/脱离进程不在其中，所以这里再按身份标记清扫一次——
        # "删除这个子模型"必须真的把它占的东西还回来，否则取消只是改了一个状态字段。
        # 注意**只在取消时清扫**：`stop()`/重启走 `interrupt()`，故意不动这些进程，
        # 因为"重启服务"不该顺手杀掉用户特意放出去的持久作业。
        released=self.release_task_processes(id)
        with self.store.transaction():
            t['epoch']+=1; t['wait']=None
            if t['status'] not in TERMINAL: t['status']=status
            if status=='superseded': t['superseded']=True
            t['terminal_seq']=self.store.event('TaskSuperseded' if status=='superseded' else 'TaskCancelled',{},t)
            if released:
                t['released_processes']=released
                self.store.event('TaskProcessesReleased',{'count':len(released),'processes':released},t)
            self.store.put('task',t)
            for q in self.store.all('question'):
                if q['task_id']==id and q['status']=='open':
                    q['status']='obsolete'; self.store.put('question',q)
            # 取消掉提问题的那个人之后，这个问题永远不会被回答了：别让整个运行陪着一起停。
            self.release_human_pause(t['run_id'])

    # 归档门禁：归档是**结案**，不是打包备份——它会停掉这个运行的一切并释放子任务
    # 工作区副本，所以只允许在"这个运行真的停下来了"之后执行。
    ARCHIVABLE_STATES = {'completed','cancelled'}

    def ensure_not_archived(self,run):
        """归档即结案：不再接受新任务、目标变更、回答与恢复。

        释放副本之后继续跑是危险的——子任务的工作区已经不在磁盘上了，
        与其让它带着一个空目录继续，不如把话说明白。
        """
        if run.get('archived'):
            raise ValueError('运行已归档（结案），不再继续；需要接着做请新建任务')
        return run

    def run_archivable(self,run):
        """能不能归档：返回 (是否允许, 不允许的原因)。"""
        if run.get('archived'): return False,'这个运行已经归档过了'
        if run['status'] in self.ARCHIVABLE_STATES: return True,''
        # 主调度模型报错：根任务已 failed，不会再有新调度了，这也是一个可以结案的终态。
        root=self.task(run['root_task']) if run.get('root_task') else None
        if root is not None and root['status']=='failed': return True,''
        return False,('运行还在进行中（%s）：归档会停掉它的全部 AI 与派生进程并释放工作区副本，'
                      '请先停止它或等它结束' % run['status'])

    def archive_run(self,run_id):
        """归档 = 结案，而不是"再存一份"。

        五件事，顺序是有意的：
        1. **门禁**：只有已完成 / 已停止 / 主调度模型报错时才允许（见 `run_archivable`）；
        2. **停掉这个运行的一切**：非终态任务一律 `cancel_task`（它会 SIGKILL 该任务派生的
           进程组），终态任务残留的进程也一并收掉；
        3. **落一份冷备**：`workspace.archive` 先把事件、报告、知识、文件版本与内容对象写进
           tar，因此 `restore_archive` 随时能解到新目录——释放副本之前先有退路；
        4. **释放子任务工作区副本**：删掉 `<数据目录>/workspaces/<task_id>`，磁盘真正回收；
        5. **保留文件修改记录**：`file` 记录、内容对象、报告、事件都不动，"谁在什么时候把哪个
           文件改成了什么"照旧可查，`workspace_diff` 仍能重建差异。

        最后给 run 打上 `archived`，控制台据此把它从活动列表移到"已归档会话"。
        """
        run=self.run(run_id)
        ok,reason=self.run_archivable(run)
        if not ok: raise ValueError(reason)
        tasks=[t for t in self.store.all('task') if t['run_id']==run_id]
        stopped=[]
        for task in tasks:
            if task['status'] in TERMINAL:
                self.interrupt(task['id'])   # 终态任务也可能残留进程
                continue
            self.cancel_task(task['id'])
            stopped.append(task['id'])
        snapshot=self.workspace.archive(run)     # 先留退路，再释放
        released=self.workspace.release(tasks)
        freed=sum(item['bytes'] for item in released)
        files_kept=len([x for x in self.store.all('file') if x['run_id']==run_id])
        with self.store.transaction():
            fresh=self.run(run_id)
            fresh['archived']=now()
            fresh['archive_path']=snapshot['path']
            fresh['archived_stopped']=stopped
            fresh['archived_released']=released
            self.store.put('run',fresh)
            self.store.event('RunArchived',{'archive':snapshot['path'],'stopped':stopped,
                                            'released':[item['task_id'] for item in released],
                                            'freed_bytes':freed,'files_kept':files_kept},
                             run_id=run_id,revision=fresh['revision'])
        return {'run_id':run_id,'archive_path':snapshot['path'],'stopped':stopped,
                'released':released,'freed_bytes':freed,'files_kept':files_kept}

    def revise(self,run_id,goal):
        run=self.ensure_not_archived(self.run(run_id))
        self.check_workspace_available(run['workspace'],run_id)
        if not goal.strip(): raise ValueError('新目标不能为空')
        previous=run['revision']
        with self.store.transaction():
            run['status']='revising'; self.store.put('run',run)
            self.store.event('GoalChangeReceived',{'goal':goal},run_id=run_id,revision=previous)
            root=self.task(run['root_task'])
            self.cancel_task(root['id'],'superseded')
            run.update(goal=goal,revision=previous+1,status='active',pause_reason='')
            self.store.put('run',run)
            self.store.put('revision',{'id':run_id+':'+str(run['revision']),'run_id':run_id,'revision':run['revision'],'goal':goal,'created':now()})
            for k in self.store.all('knowledge'):
                if k.get('run_id')==run_id and k.get('goal_specific'):
                    k.update(stale=True,stale_reason='核心目标已切换'); self.store.put('knowledge',k)
            successor=self.create_task(run,goal,run['model_id'])
            successor['predecessor_id']=root['id']
            successor['baseline_pending']=True
            self.record_message(successor,{'role':'user','content':f'目标从 r{previous} 切换。旧任务已停止，历史任务可通过 tasks_list 查询；有价值的旧成果可显式 workspace_integrate 后重新验证。当前主工作区可能包含旧目标已落盘的修改，请按新目标核对；系统不自动撤销用户文件。'},source='revision')
            self.store.put('task',successor)
            run['root_task']=successor['id']; self.store.put('run',run)
            self.store.event('GoalRevisionActivated',{'previous':previous,'goal':goal,'root_task':successor['id']},run_id=run_id,revision=run['revision'])
        return run

    def answer(self,id,answer):
        q=self.store.get('question',id); t=self.task(q['task_id'])
        run=self.ensure_not_archived(self.run(t['run_id']))
        if q['status']!='open' or t['revision']!=run['revision'] or t['status'] in TERMINAL or run['status']=='cancelled': raise ValueError('问题已回答或属于旧目标')
        with self.store.transaction():
            q.update(status='answered',answer=answer); self.store.put('question',q)
            self.store.event('HumanAnswered',q,t)
            # 先放行再投递：暂停期间 `deliver` 的唤醒会走 `current()` 判断，
            # 运行还没回到 active 的话，回答就送不进那个等待中的任务。
            self.release_human_pause(run['id'])
            self.deliver(t['id'],'用户回答：'+answer,delivery='wake',topic='question:'+id,kind='answer')
        return q

    def resume(self,id,message='继续执行；请先检查已保存的状态。'):
        t=self.task(id); run=self.ensure_not_archived(self.run(t['run_id']))
        if t['revision']!=run['revision'] or t['status'] in {'cancelled','superseded'}: raise ValueError('已弃置或取消的任务不能恢复，请创建新任务')
        self.check_workspace_available(run['workspace'],run['id'])
        with self.store.transaction():
            run['status']='active'; self.store.put('run',run)
            self.repair_history(t); t.update(status='queued',wait=None,error=None,not_before=now()+self.batch_seconds)
            self.store.put('task',t)
            self.deliver(id,message,kind='user')
        return t

    def pause_run(self,id):
        run=self.run(id)
        if run['status']=='paused':
            return run
        if run['status']!='active':
            raise ValueError('只有运行中的 run 可以暂停')
        run['status']='paused'; run['pause_reason']='manual'; self.store.put('run',run)
        for t in self.store.all('task'):
            if t['run_id']==id and t['status']=='running':
                self.interrupt(t['id']); t['epoch']+=1; t['status']='queued'; self.repair_history(t); self.store.put('task',t)
        self.store.event('RunPaused',{'reason':'manual'},run_id=id,revision=run['revision'])
        return run

    def hold_for_human(self,task,question):
        """一个 Agent 向人类提问 = 整个运行停下来等这一句回答。

        为什么是**整个 run** 而不是提问的那一个任务：人类是这次协作唯一的输入源，
        一个问题悬着的时候，其他 Agent 继续烧 token 只会基于"还没定的事"往下做，
        而回答一到，它们手里的判断就作废了。所以要停就一起停。

        停法是**软停**：正在跑的那一轮工具批次照常做完（它可能已经写了一半文件，
        从中间掐断会留下 `TOOL_OUTCOME_UNKNOWN`），然后回到队列；调度器因为
        `run['status']!='active'`（见 `current()`）不再领取任何任务，于是没有 Agent 会开始新一轮。
        回答到达时由 `release_human_pause()` 放行。
        """
        run=self.run(task['run_id'])
        if run['status']!='active':
            # 已经因为别的问题（或人工暂停）停着了，不覆盖既有原因。
            return False
        run['status']='paused'; run['pause_reason']='human_question'; self.store.put('run',run)
        self.store.event('RunPaused',{'reason':'human_question','question_id':question['id'],
                                      'task_id':task['id'],'goal':task.get('goal')},
                         run_id=run['id'],revision=run['revision'])
        return True

    def release_human_pause(self,run_id):
        """人类问题造成的暂停：该运行下再没有未答问题时才解除。

        多个问题可能同时在（并发提问），所以判据是"还剩几个 open"，而不是"刚答的这个"。
        """
        run=self.run(run_id)
        if run.get('pause_reason')!='human_question' or run['status']!='paused':
            return False
        still_open=[q['id'] for q in self.store.all('question')
                    if q['run_id']==run_id and q['status']=='open']
        if still_open:
            return False
        run['status']='active'; run['pause_reason']=''; self.store.put('run',run)
        self.store.event('RunResumed',{'reason':'human-answered'},run_id=run_id,revision=run['revision'])
        return True

    def resume_run(self,id):
        run=self.ensure_not_archived(self.run(id))
        if run['status']=='active':
            return run
        if run['status']!='paused':
            raise ValueError('只有暂停的 run 可以恢复')
        self.check_workspace_available(run['workspace'],run['id'])
        reason=run.get('pause_reason') or 'manual'
        # 人工点"继续运行"是显式覆盖：即使还有问题没答，也按人的意思放行。
        run['status']='active'; run['pause_reason']=''; self.store.put('run',run)
        self.store.event('RunResumed',{'reason':'manual','overrode':reason},run_id=id,revision=run['revision'])
        return run

    async def compact(self,t,epoch):
        original=self.task_history(t)
        ref=self.store.blob(dumps(original))
        # Keep the last two complete assistant/tool exchanges, never split call/result pairs.
        # cut 必须落在首两条消息之后、不超过历史长度，否则会切空或把非消息对象拼进历史。
        # kept_from 是"尾部原文从哪一条开始保留"：压缩算法与日志派生共用这一个定义，
        # 因此两者对同一份历史得到完全相同的结果，不需要任何下标推算。
        starts=[i for i,m in enumerate(original) if m['role']=='assistant']
        kept_from=starts[-2] if len(starts)>=2 else len(original)
        kept_from=max(2,min(kept_from,len(original)))
        cut=kept_from
        old=original[2:kept_from]
        config=self.store.get('model',t['model_id'])
        max_bytes=max(1024,int(config['context_window'])-int(config['max_output'])-4096)
        material=dumps(old)
        if not old:
            # 尾部已经全部是要保留的原文，没有可替换区间；不调用模型，也不改写历史。
            with self.store.transaction():
                fresh=self.guard(t,epoch); fresh['compact_requested']=False; self.store.put('task',fresh)
            return original
        if len(material.encode())>max_bytes:
            # Retrieval checkpoint for pre-existing oversized sessions, explicitly not a complete summary.
            summary={'decisions':[], 'pending':['历史超过单次总结容量；按引用读取原文。'], 'evidence_refs':[ref]}
        else:
            # 走 `_call_model` 而不是直接 `models.call`：压缩也是一次真实的模型调用，同样会遇上
            # 上游 503/超时. 实测真机 `task_b5bf6929c57f` 就是死在**压缩这一次调用**上的 503——
            # 它绕过了故障转移，于是同一条 run 里其他模型好好的，这个任务却被判死
            # （事件日志里那段没有 `ModelCalled`，正是"不是普通轮次"的指纹）。
            # 顺带它还拿到了自己的请求信封，压缩用了什么提示词可以逐字重建。
            msg,usage=await self._call_model(t,[
                {'role':'system','content':'压缩工作上下文，输出 JSON 对象，含 decisions（决定）, pending（未解决）, evidence_refs（来源）。保留事实与未知，不执行工具，不改用户目标。'},
                {'role':'user','content':material}],epoch,None)
            content=msg.get('content','').strip()
            if content.startswith('```'): content=content.split('\n',1)[1].rsplit('```',1)[0]
            summary=json.loads(content)
            if not all(isinstance(summary.get(k),list) for k in ['decisions','pending','evidence_refs']):
                raise ValueError('compact 结果缺少必要字段；原始上下文未替换')
        fresh=self.guard(t,epoch)
        state={'goal':fresh['goal'],'revision':fresh['revision'],'wait':fresh['wait'],'inbox_cursor':fresh['inbox_cursor'],
               'tasks':[{'id':x['id'],'status':x['status'],'goal':x['goal']} for x in self.store.all('task') if x['run_id']==t['run_id']],
               'file_manifest_ref':self.store.blob(dumps(fresh['manifest']))}
        # 遮蔽语义：checkpoint 直接记录被替换的区间内容与原文引用，不依赖任何下标运算，
        # 因此压缩前历史可由 restore_history() 精确还原；原文另在 ContextCompacted 事件里保留为 old_ref。
        checkpoint={'checkpoint':summary,'state':state,
                    'shadowed_range':[2,cut],'kept_from':kept_from,
                    'region_ref':self.store.blob(dumps(old)),'history_ref':ref}
        checkpoint_ref=self.store.blob(dumps(checkpoint))
        fresh['history']=original[:2]+[{'role':'user','content':dumps(checkpoint)}]+original[cut:]
        fresh['compact_requested']=False; fresh['context_epoch']+=1
        with self.store.transaction():
            # checkpoint 由 ContextCompacted 事件唯一记录（内容 + 遮蔽区间 + kept_from）。
            # 不再把它记成一条普通消息：那样派生会在"压缩结果"之后再多出一条 user 消息。
            self.store.put('task',fresh)
            self.store.event('ContextCompacted',{'old_ref':ref,'context_epoch':fresh['context_epoch'],
                                                 'summary':summary,'shadowed_range':[2,cut],
                                                 'checkpoint_ref':checkpoint_ref,
                                                 'region_ref':checkpoint['region_ref'],
                                                 'kept_from':kept_from,'kept_tail':len(original)-kept_from,
                                                 'messages_before':len(original)},fresh)

    @staticmethod
    def checkpoint_payload(message):
        """判断一条消息是不是压缩 checkpoint；是则返回其负载。"""
        if not isinstance(message,dict) or message.get('role')!='user': return None
        try:
            payload=json.loads(message.get('content') or '')
        except (ValueError,TypeError):
            return None
        if isinstance(payload,dict) and isinstance(payload.get('region_ref'),str) and 'shadowed_range' in payload:
            return payload
        return None

    @classmethod
    def splice_checkpoint(cls,messages,region,index):
        """把某一条 checkpoint 消息换回被遮蔽的原文区间，其余消息原样保留。

        与 DSH 的 surface replace 同构：压缩只遮蔽一段区间，原文始终可还原。
        被替换的内容来自 checkpoint 自己记录的 region_ref，不做任何下标推算。
        """
        return messages[:index]+list(region)+messages[index+1:]

    def restore_history(self,history_ref,depth=1):
        """从任意时点的输入快照出发，向内还原压缩前的历史。

        `history_ref` 可以是任意一次请求记录里的 `input_ref`。默认只解开一层遮蔽
        （即"压缩之前那一刻的历史"）；`depth=None` 表示一路还原到最初原文。
        每次调用返回的消息数量单调增加，因此还原过程可逐步核对。
        """
        messages=json.loads(self.store.read_blob(history_ref))
        seen=set()
        while depth is None or depth>0:
            payload=next((p for p in (self.checkpoint_payload(m) for m in messages) if p),None)
            if payload is None or payload['region_ref'] in seen:
                return messages
            seen.add(payload['region_ref'])
            index=messages.index(next(m for m in messages if self.checkpoint_payload(m)))
            messages=self.splice_checkpoint(messages,json.loads(self.store.read_blob(payload['region_ref'])),index)
            if depth is not None: depth-=1
        return messages

    def knowledge_search(self,query,t,limit=20,include_stale=True):
        """检索项目知识。

        - 查询按**全部命中**（AND）过滤，不再"命中任一词就返回全部"；
        - 标题命中权重高于正文，再按"相关度、有效优先、时间"排序；
        - 默认把失效知识排在最后并保留在结果里（模型需要知道"存在但不可当事实用"），
          `include_stale=False` 时只返回有效知识。
        """
        run=self.run(t['run_id']); workspace=run['workspace']
        words=[w for w in query.lower().split() if w]
        scored=[]
        for k in self.store.all('knowledge'):
            if k.get('project_workspace')!=workspace: continue
            title=k['title'].lower(); text=(k['title']+' '+k['content']).lower()
            if words and not all(w in text for w in words): continue
            score=sum(3 for w in words if w in title)+sum(1 for w in words if w in text)
            scored.append((score,self.knowledge_validity(k,t)))
        scored.sort(key=lambda pair:(-pair[0],pair[1].get('stale',False),-pair[1]['created']))
        rows=[item for _,item in scored]
        if not include_stale: rows=[item for item in rows if not item.get('stale')]
        return rows[:limit]

    def knowledge_validity(self,item,t,visited=None):
        item=dict(item)
        visited=set() if visited is None else set(visited)
        if item['id'] in visited:
            return item | {'stale':True,'stale_reason':'知识依赖环'}
        visited.add(item['id'])
        for dep in item.get('dependencies',[]):
            try:
                dependency=self.store.get('knowledge',dep)
            except ValueError:
                item.update(stale=True,stale_reason='依赖的知识已不存在：'+dep); continue
            if self.knowledge_validity(dependency,t,visited).get('stale'):
                item.update(stale=True,stale_reason='依赖知识与当前来源版本不一致')
        for source in item.get('sources',[]):
            # 来源可能属于项目目录，也可能只属于发布它的那个子工作区；按发布时记录的类型比对，
            # 否则父任务会把自己工作区里找不到的来源一律判为失效。
            file=self.source_base(item,t,source)/source['path']
            if not file.is_file():
                item.update(stale=True,stale_reason='来源文件已不存在：'+source['path']); continue
            if self.store.blob(file.read_bytes())!=source['hash']:
                item.update(stale=True,stale_reason='来源文件版本已经变化：'+source['path'])
        if item.get('goal_specific') and (item['run_id']!=t['run_id'] or item['revision']!=t['revision']):
            item.update(stale=True,stale_reason='该结论依赖其他目标版本')
        return item

    def source_base(self,item,t,source):
        """来源文件该按哪个基准目录解析。

        - `scope=project`：项目根，跨任务都按同一个基准比对；
        - `scope=workspace`：发布它的那个工作区（子任务的独立副本）；
        - 旧记录没有 scope：先试当前工作区、再试发布时的工作区、最后试项目根，取第一个存在的。
        """
        kind=(source or {}).get('kind')
        published=(source or {}).get('workspace') or item.get('workspace')
        project=self.run(item['run_id'])['workspace'] if item.get('run_id') else t['workspace']
        if kind=='project': return Path(project)
        if kind=='workspace': return Path(published or t['workspace'])
        for candidate in (t['workspace'],published,project):
            if candidate and (Path(candidate)/source['path']).is_file(): return Path(candidate)
        return Path(t['workspace'])
    def publish_knowledge(self,t,args):
        project=Path(self.run(t['run_id'])['workspace']).resolve()
        project_workspace=str(project)
        sources=[]
        for path in args.get('sources',[]):
            absolute=(Path(t['workspace'])/path).expanduser().resolve()
            if not absolute.is_file(): raise ValueError('知识来源不存在：'+path)
            digest=self.store.blob(absolute.read_bytes())
            relative=str(path)
            # 同一个来源同时存在于项目目录与子任务副本时按**项目**记：这样父与子都会拿项目里的
            # 版本来判断有效性，而不是各自拿自己的副本——否则父任务永远看到"来源不一致"。
            candidate=absolute if absolute.is_relative_to(project) else (project/relative).resolve()
            if candidate.is_file() and candidate.is_relative_to(project) and self.store.blob(candidate.read_bytes())==digest:
                kind,relative='project',str(candidate.relative_to(project))
            else:
                kind='workspace'
            sources.append({'path':relative,'kind':kind,'workspace':t['workspace'],'hash':digest})
        dependencies=args.get('dependencies',[])
        for id in dependencies: self.store.get('knowledge',id)
        scope=str(args.get('scope') or 'project')
        if scope not in {'project','run'}: raise ValueError("scope 必须是 project 或 run")
        title=str(args.get('title') or (args.get('content') or '')[:40] or '共享信息')
        fingerprint=self.store.blob(dumps({'title':title,'sources':sources,'dependencies':dependencies,
                                           'content':args['content'],'scope':scope}))
        for k in self.store.all('knowledge'):
            if k.get('fingerprint')==fingerprint and k['workspace']==t['workspace'] and not k['stale']: return k
        # `seq` 是发布时的全局事件游标：**所有共享条目都要有一个全序**。
        # 原来的排序键是（相关度、是否失效、created），而 created 是浮点秒——
        # 同一秒内的多条顺序不确定，两个人查出来的顺序可能不同，"看到同一份记录"就不成立。
        sequence=self.store.cursor()
        k={'id':uid('knowledge_'),'run_id':t['run_id'],'task_id':t['id'],'revision':t['revision'],
           'project_workspace':project_workspace,'workspace':t['workspace'],
           'title':title,'content':args['content'],'sources':sources,'dependencies':dependencies,
           # 反向边：被依赖的知识要知道谁依赖它，失效才能沿依赖图传播。
           'dependents':[],'goal_specific':bool(args.get('goal_specific',False)),'stale':False,
           'scope':scope,'seq':sequence,
           'created':now(),'fingerprint':fingerprint}
        for dep in dependencies:
            dependency=self.store.get('knowledge',dep)
            edge={'id':k['id'],'published_at':now()}
            if edge['id'] not in [x.get('id') for x in dependency.get('dependents',[])]:
                dependency.setdefault('dependents',[]).append(edge)
                self.store.put('knowledge',dependency)
        bad=[key for key,value in k.items() if isinstance(value,Path)]
        if bad: raise ValueError('知识记录含非 JSON 值：'+','.join(bad))
        self.store.put('knowledge',k)
        self.store.event('KnowledgePublished',{'id':k['id'],'title':k['title'],'scope':scope,'seq':sequence},t)
        if scope=='run': self.store.event('SharedEntryPublished',{'id':k['id'],'title':title,'seq':sequence},t)
        notified,skipped=self.notify_entry(t,k,args.get('notify') or [])
        if notified or skipped:
            k=k | {'notified':notified,'not_notified':skipped}
            self.store.put('knowledge',k)
        return k

    def notify_entry(self,t,entry,targets):
        """把一条共享条目**推送**给指定任务：让每个人自己读同一份，而不是给每个人一份手抄本。

        这是这次事故（`run_574484cad16f`）的正面修法：主调度当时只能靠把发言复制进
        各个子任务的 goal 来"让所有人看到"，复制四次就有四份分叉（C 缺 B、D 缺 C）。
        推送走 `deliver` 的统一投递，因此收件箱、唤醒订阅、事件时间轴、控制台都自动生效。

        `terminal=True`：这是一份**通知**，不是提问——否则接收方会为"收到这条"再生成一个
        待答请求，收束时又被作废，形成通知风暴（第 13 节真机踩过）。
        """
        notified,skipped=[],[]
        for id in targets:
            if not id or id==t['id']: continue
            try: target=self.task(id)
            except ValueError:
                skipped.append({'task_id':id,'reason':'任务不存在'}); continue
            if target['run_id']!=t['run_id'] or target['revision']!=t['revision']:
                skipped.append({'task_id':id,'reason':'不属于当前运行版本'}); continue
            self.deliver(id, f'共享信息（{entry.get("scope")}/{entry.get("seq")}）：{entry["title"]}\n{entry["content"]}',
                         sender=t['id'],delivery='inbox',topic='knowledge:'+entry['id'],kind='shared-entry',
                         refs=[entry['id']],terminal=True)
            notified.append(id)
        if skipped:
            self.store.event('SharedEntryNotifySkipped',{'id':entry['id'],'skipped':skipped},t)
        return notified,skipped

    def knowledge_entries(self,t,scope='run',from_seq=0,limit=0):
        """按 `seq` 顺序枚举共享条目（**全序**，不按相关度）。

        `knowledge_search` 是检索：按关键词 AND 过滤 + 默认截断 20 条 + 按相关度排序，
        因此**两个人搜同一个词也可能拿到不同切片**——"所有人都看到同一份"不能建立在它上面。
        这里不过滤、不按相关度、按 seq 排序（旧记录没有 seq 的用 created 兜底），
        全部调用者得到同一序列化结果。
        """
        run=self.run(t['run_id']); workspace=run['workspace']
        rows=[k for k in self.store.all('knowledge')
              if k.get('project_workspace')==workspace
              and (not scope or (k.get('scope') or 'project')==scope)
              and (k.get('revision')==t['revision'] or (k.get('scope') or 'project')=='project')]
        rows.sort(key=lambda k:(int(k.get('seq') or 0), float(k.get('created') or 0), k['id']))
        if from_seq: rows=[k for k in rows if int(k.get('seq') or 0)>=int(from_seq)]
        if limit: rows=rows[:int(limit)]
        return [{'id':k['id'],'seq':int(k.get('seq') or 0),'scope':k.get('scope') or 'project',
                 'title':k['title'],'content':k['content'],'author':k.get('task_id'),
                 'stale':bool(k.get('stale')),'created':k.get('created')} for k in rows]

    def resolve_path(self,t,path):
        return (Path(t['workspace'])/path).expanduser().resolve()

    async def tool_models_list(self,t,a): return self.models.list()
    def model_api_key(self,config):
        return config.get('api_key') or os.environ.get(config.get('key_env',''),'')
    def model_credentials(self,t,model_id=None):
        """凭据、地址与协议：模型可自行读取并在自己的代码里调用这些接口。"""
        wanted=model_id or t['model_id']
        config=self.store.get('model',wanted)
        key=self.model_api_key(config)
        result={'model_id':config['id'],'model':config.get('model'),'base_url':config.get('base_url'),
                'api':providers.api_of(config),'adapter':config.get('adapter'),
                'api_key':key,'api_key_env':config.get('key_env',''),'no_key':bool(config.get('no_key')),
                'headers':dict(config.get('headers') or {}),
                'disable_response_storage':bool(config.get('disable_response_storage',False)),
                'context_window':config.get('context_window'),'max_output':config.get('max_output'),
                'note':'凭据为明文；可据此直接调用该接口，不要把它写进文件或报告。'}
        with self.store.transaction():
            self.store.event('CredentialsExposed',{'model_id':config['id'],'reason':'models_credentials'},t)
        return result
    async def tool_models_credentials(self,t,a):
        ids=a.get('model_ids') or []
        if not ids: return {'active':self.model_credentials(t,t['model_id'])}
        return {'models':[self.model_credentials(t,x) for x in ids]}
    async def tool_tasks_list(self,t,a):
        return [{k:v for k,v in x.items() if k not in {'history','base_manifest','manifest'}} for x in self.store.all('task') if x['run_id']==t['run_id']]
    async def tool_tasks_submit(self,t,a):
        reuse_key=None
        if a.get('reuse_key'):
            reuse_key=self.store.blob(dumps({'key':a['reuse_key'],'project':self.run(t['run_id'])['workspace'],'manifest':t['manifest'],'revision':t['revision']}))
            for other in self.store.all('task'):
                if other.get('reuse_key')==reuse_key and other['run_id']==t['run_id'] and other['status'] not in {'failed','cancelled','superseded'}:
                    return {'task_id':other['id'],'workspace':other['workspace'],'reused':True}
        # 模型高低搭配：显式指定就尊重；没指定且父模型属于 heavy 档时降到 light 档。
        # 这是"智能预算"里唯一由运行时强制的部分——其余（档位含义、什么时候该省）
        # 交给模型自己判断，运行时只保证不会因为"复制父模型"而全家都用最贵的。
        requested=a.get('model_id')
        chosen=requested or t['model_id']
        downgraded_from=None
        if not requested:
            target=self.models.downgrade_target(chosen)
            if target and target!=chosen:
                downgraded_from=chosen; chosen=target
        # 显式点名重模型不再被拦截：配额是人的策略，写死在运行时里只会挡住模型正确的判断。
        # 这里只统计并如实报告用量，让"花了多少"可见。
        heavy_warning=None
        if self.models.tiers_of(self.store.get('model',chosen))=='heavy':
            used=self.heavy_usage(t['run_id'])['calls']
            heavy_warning=(f'子任务使用了 heavy 档模型 {chosen}（本运行 heavy 调用累计 {used+1} 次）。')
        with self.store.transaction():
            child=self.create_task(self.run(t['run_id']),a['goal'],chosen,t,a.get('dependencies'),a.get('priority',0))
            if downgraded_from:
                child['model_downgraded_from']=downgraded_from
                self.store.put('task',child)
                self.store.event('ModelDowngraded',{'from':downgraded_from,'to':chosen,
                                                    'reason':'子任务未指定模型，父模型为 heavy 档'},child)
            if reuse_key:
                child['reuse_key']=reuse_key; self.store.put('task',child)
            # 预算判断留痕：模型为什么给这个子任务挑这个档位。没有它就只剩"花了多少"，
            # 看不出"该不该花"；人类也无法纠正一个没说出来的判断。
            if str(a.get('cost_note') or '').strip():
                child['cost_note']=str(a['cost_note']).strip()[:500]
                self.store.put('task',child)
                self.store.event('ModelChosenForCost',{'model_id':chosen,'source':'explicit' if requested else 'inherited',
                                                       'note':child['cost_note']},child)
        child_model_source = 'downgraded' if downgraded_from else ('explicit' if requested else 'inherited')
        child['model_source']=child_model_source
        self.store.put('task',child)
        result={'task_id':child['id'],'workspace':child['workspace'],'model_id':chosen,
                'model_source':child_model_source}
        if heavy_warning:
            result['heavy_tier_warning']=heavy_warning
        if downgraded_from:
            result.update(model_id=chosen,model_downgraded_from=downgraded_from,
                          note=f'未指定 model_id，已从 heavy 档 {downgraded_from} 降到 light 档 {chosen}')
        return result
    async def tool_tasks_yield(self,t,a):
        """保存现场、释放执行槽、订阅事件。

        两条硬性拒绝（都是真机跑出来的缺陷，不是假想）：

        1. **不等待自己**：把自身 id 放进订阅列表当"心跳"会让 `reaches()` 立刻判定成封闭环
           （自等待必然可达自己），于是刷出一串 `RunStalled` 却毫无意义；过滤后若没有其它
           可等待目标则明确报错——"等自己"本身就没有语义。
        2. **真正的等待环**仍然拒绝，理由里指出是哪个 id 形成的环。
        """
        raw_ids=a.get('task_ids') or []
        ids=[x for x in raw_ids if x!=t['id']]
        for id in ids:
            other=self.task(id)
            if other['run_id']!=t['run_id'] or other['revision']!=t['revision']: raise ValueError('只能等待当前目标版本任务')
        dropped_self=len(raw_ids)-len(ids)
        topics=list(a.get('topics') or [])
        if dropped_self and not ids and not topics:
            # 只有自身 id：这不是"等待"，是自旋。明确拒绝并说明怎么改。
            return {'error':'不能等待自己：订阅里只有本任务 id，既不会产生事件也不会推进。'
                            '请等待子任务/其它任务，或只给 timeout_seconds 做纯计时等待（到点唤醒一次）。'}

        def reaches(id,visited):
            if id==t['id']: return True
            if id in visited: return False
            visited.add(id); other=self.task(id)
            return any(reaches(x,visited) for x in other.get('dependencies',[])+(other.get('wait') or {}).get('task_ids',[]))
        cycle=[id for id in ids if reaches(id,set())]
        if cycle:
            self.store.event('RunStalled',{'reason':'等待环','task_ids':ids,'cycle':cycle},t)
            return {'error':'此等待形成封闭环（'+', '.join(cycle)+'）。请调整依赖、发消息或请求人类。'}

        after=int(a.get('after_cursor',t['last_call_cursor']))
        if after>self.store.cursor(): raise ValueError('事件游标不能位于未来')
        terminal=[self.task(id) for id in ids if self.task(id)['status'] in TERMINAL]
        if ids and len(terminal)==len(ids) and not topics and all(d.get('terminal_seq',0)<=after for d in terminal):
            return {'waiting':False,'already_terminal':[{'task_id':d['id'],'status':d['status'],'summary':d.get('result')} for d in terminal],
                    'note':'这些任务已结束，无需重复等待；请处理结果或选择其他事件。'}
        window=float(a['timeout_seconds']) if a.get('timeout_seconds') else None
        max_wait=a.get('max_wait_seconds')
        registered=now()
        # 时限完全由模型声明，运行时**不再补一个兜底上限**：
        # 没声明时限就是"只被订阅的事件唤醒"（设计文档 §5.2：无事件时不调用模型轮询）。
        # 想被叫醒就声明 timeout_seconds / max_wait_seconds，或订阅会真正发生的事件。
        max_deadline=registered+float(max_wait) if max_wait else None
        wait={'task_ids':ids,'topics':topics,'mode':a.get('mode','any'),
              'after_cursor':after,'note':a.get('note',''),
              'deadline':registered+window if window else None,
              'registered_at':registered,'window_seconds':window,
              'max_deadline':max_deadline}
        note=''
        with self.store.transaction():
            fresh=self.task(t['id'])
            if dropped_self:
                note='已忽略订阅里的自身 id（等待自己不会产生事件）'
            # 没有声明任何时限时，说清这个等待的边界在哪里——这是描述，不是拦截。
            if not window and not max_wait:
                note=(note+'；' if note else '')+(
                    '未声明 timeout_seconds/max_wait_seconds：只会被订阅的事件唤醒。')
            fresh.update(status='waiting',wait=wait); self.store.put('task',fresh)
            self.store.event('WaitRegistered',wait,fresh); self.wake_waiters()
        # 把等待的边界说给模型听，让它自己判断——运行时不据此拒绝、不替它决定。
        observed=self._wait_observation(wait)
        if observed: note=(note+'；' if note else '')+observed
        result={'waiting':True,'subscription':wait}
        if observed: result['observation']=observed
        if note: result['note']=note
        return result

    def _wait_observation(self,wait):
        """把「这个等待的边界在哪」如实说出来；不预测、不拦截、不改状态。

        这是**描述**，不是判断：运行时遍历一遍订阅对象，把"谁在等什么、谁会来叫醒你"
        这些模型自己不容易看见的事实拼成一句话。它不拒绝登记等待，也不决定该不该继续等。

        两种值得说明的形态（真机 `run_ca4aea2c3b3a` 都出现过）：

        1. **纯计时等待**：没有 task_ids 也没有 topics，除声明的时限外没有事件会唤醒它。
        2. **订阅的任务自己也在等消息**：那些任务不会走向终态，所以"等它们结束"这件事
           不会发生——想被消息唤醒就得订阅 topics。
        """
        if not wait.get('task_ids') and not wait.get('topics'):
            if wait.get('window_seconds'):
                return ('说明：这个等待没有 task_ids 也没有 topics，除你声明的时限外没有事件会唤醒它。'
                        '若你在等某个回复，可以订阅它：tasks_yield(task_ids=[对方任务 id])，'
                        '它发消息或结束时都会叫醒你。')
            return ''
        if wait.get('topics') or not wait.get('task_ids'): return ''
        if wait.get('deadline') or wait.get('window_seconds'): return ''
        for id in wait['task_ids']:
            try: other=self.task(id)
            except ValueError: continue
            if other['status'] in TERMINAL: return ''      # 已经有任务结束，事件可能就在游标后
            if not self.current(other): return ''
            if other['status']!='waiting': return ''       # 还在跑，可能会结束
            other_wait=other.get('wait') or {}
            if other_wait.get('task_ids'): return ''       # 它在等任务结束，可能会推进
            if other_wait.get('deadline') or other_wait.get('window_seconds'): return ''
        return ('说明：订阅的这些任务当前都停在"等消息/等事件"上，不会走向终态；'
                '而这个等待也没有声明时限，所以除了它们发消息，没有事件会唤醒你。'
                '想被消息唤醒就在 tasks_yield 里订阅 topics（例如 ["guidance","answer","question"]）。')

    async def tool_agents_send(self,t,a):
        target=self.task(a['task_id'])
        if target['run_id']!=t['run_id'] or target['revision']!=t['revision']: raise ValueError('消息目标必须属于当前运行版本')
        # 直连任意同版本任务（含兄弟任务）；转发时 hop 自增，便于看出绕了几手。
        # 一律用关键字传参：参数变多后位置传参必然出错（这次就把 in_reply_to
        # 按位置传成了 message_id，答复因此被判成无人认领）。
        message=self.deliver(target['id'],a['summary'],sender=t['id'],delivery=a.get('delivery','inbox'),
                             topic=a.get('topic',''),kind=a.get('kind','message'),
                             refs=a.get('artifact_refs',[]),
                             request_id=a.get('request_id'),in_reply_to=a.get('in_reply_to'),
                             relay_of=a.get('relay_of'))
        result={'message_id':message['id'],'request_id':message.get('request_id'),
                'in_reply_to':message.get('in_reply_to'),'status':message.get('status'),
                'hop':message.get('hop')}
        if message.get('status')=='relayed':
            result['note']=(f"已作为转发送达：原请求 {message['request_id']} 的发起者是 "
                            f"{message.get('origin_task')}，答复请带 in_reply_to={message['request_id']}"
                            f"（这样它才能绑回最初的问题）")
        elif message.get('origin_task'):
            result['origin_task']=message['origin_task']
        if message.get('status')=='answered':
            result['note']='已按 in_reply_to 绑定为答复'
        elif message.get('status')=='unclaimed':
            result['note']=self._unclaimed_reason(a.get('in_reply_to'))
        return result

    async def tool_tasks_close(self,t,a):
        """收束前的排空：不要在对方还有在途请求时强行截断（缺陷 3 的修法）。

        - `drain`：等对方把在途请求答完（或到时限）再收束；
        - `forfeit`：显式作废这些请求并说明原因，对方会收到通知、知道不必再答。
        """
        target=self.task(a['source_task'])
        if target['run_id']!=t['run_id'] or target['revision']!=t['revision']:
            raise ValueError('收束目标必须属于当前运行版本')
        mode=a.get('mode','drain')
        pending=self.open_requests(target=target['id'])
        if mode=='forfeit':
            done=self.forfeit_requests(t['id'],a.get('request_ids') or [r['id'] for r in pending],
                                       a.get('reason',''))
            return {'closed':True,'mode':'forfeit','forfeited':[r['id'] for r in done],
                    'note':'已在收束前作废在途请求；对方会收到不必再答的通知'}
        if mode!='drain':
            raise ValueError("mode 必须是 drain 或 forfeit")
        timeout=max(1.0,min(float(a.get('timeout_seconds') or 120),900.0))
        # 收束预告：告知对方即将收束，请先答完在途请求。
        self.deliver(target['id'],
                     f"即将收束：请先把在途请求答完（当前 {len(pending)} 个未答），再汇报结果。不要再提出新问题。",
                     t['id'],delivery='wake',topic='close-notice',kind='game_close')
        deadline=now()+timeout
        while now()<deadline:
            remaining=self.open_requests(target=target['id'])
            if not remaining: break
            await asyncio.sleep(0.2)
        remaining=self.open_requests(target=target['id'])
        return {'closed':True,'mode':'drain','waited_seconds':round(timeout-(deadline-now()),1),
                'still_open':[{'request_id':r['id'],'requester':r.get('requester'),
                               'summary':(r.get('summary') or '')[:60]} for r in remaining],
                'note':('在途请求已答完' if not remaining else
                        '仍有未答请求；请显式放弃（mode=forfeit）或继续等待，不要在此时截断')}

    async def tool_tasks_cancel(self,t,a):
        """删除一个子 Agent：立即终止它的会话并**释放它占用的全部命令**。

        终止的是这一轮（在飞的模型调用 + 正在跑的那条 shell），释放的是它留下的一切
        ——包括 `setsid`/`nohup` 甩出去的常驻进程，按 `UNISON_TASK_ID` 身份标记清扫。
        历史产物照旧保留（取消不是回滚），所以被删掉的任务仍可在「轨迹」与「文件」里查证。
        """
        self.cancel_task(a['task_id'])
        fresh=self.task(a['task_id'])
        return {'cancelled':a['task_id'],'status':fresh['status'],
                'released_processes':fresh.get('released_processes') or []}
    async def tool_human_ask(self,t,a):
        q={'id':uid('q_'),'task_id':t['id'],'run_id':t['run_id'],'revision':t['revision'],
           'question':a['question'],'options':a.get('options',[]),'status':'open','created':now()}
        with self.store.transaction():
            self.store.put('question',q); self.store.event('QuestionRaised',q,t)
            t.update(status='waiting',wait={'topics':['question:'+q['id']],'task_ids':[], 'after_cursor':self.store.cursor(),'mode':'any'})
            self.store.put('task',t)
            # 向人类提问 = 整个运行暂停，直到这句话被回答（见 hold_for_human）。
            self.hold_for_human(t,q)
        return {'question_id':q['id'],'waiting':True,'run_paused':True,
                'note':'整个运行已暂停：所有 Agent 做完当前这一轮就停手，人类回答后自动继续。'}
    async def tool_workspace_list(self,t,a):
        p=self.resolve_path(t,a.get('path','.'))
        return [{'name':x.name,'directory':x.is_dir()} for x in sorted(p.iterdir()) if x.name not in {'.git','.unison'}][:500]
    async def tool_workspace_read(self,t,a):
        p=self.resolve_path(t,a['path']); content=p.read_text(); lines=content.splitlines()
        start=max(1,a.get('start',1)); limit=min(500,max(1,a.get('limit',200)))
        return {'path':str(p),'start':start,'total_lines':len(lines),'content':'\n'.join(lines[start-1:start-1+limit]),'ref':self.store.blob(content)}
    async def tool_workspace_image_info(self,t,a):
        p=self.resolve_path(t,a['path'])
        return {'path':str(p),**image_info(p.read_bytes())}
    async def tool_workspace_write(self,t,a):
        self.workspace.reconcile(self.task(t['id']))
        p=self.resolve_path(t,a['path']); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(a['content'])
        self.workspace.reconcile(self.task(t['id']),t['id']); return {'written':str(p)}
    async def tool_workspace_edit(self,t,a):
        self.workspace.reconcile(self.task(t['id']))
        p=self.resolve_path(t,a['path']); text=p.read_text()
        if not a['old'] or text.count(a['old'])!=1: raise ValueError('old 必须非空且唯一匹配')
        p.write_text(text.replace(a['old'],a['new'],1)); self.workspace.reconcile(self.task(t['id']),t['id'])
        return {'edited':str(p)}
    def shell_environment(self,t):
        """把当前模型的调用信息注入 shell 环境，让模型能在自己的代码里直接调用模型 API。

        凭据只存在于子进程环境里，不写入工作区文件，因此不会进入文件清单、差异或归档。
        """
        config=self.store.get('model',t['model_id'])
        key=self.model_api_key(config)
        env=dict(os.environ)
        env.update(UNISON_MODEL_ID=config['id'],
                   UNISON_MODEL=config.get('model') or '',
                   UNISON_MODEL_BASE_URL=config.get('base_url') or '',
                   UNISON_MODEL_API=providers.api_of(config) or '',
                   UNISON_MODEL_API_KEY=key,
                   # 身份标记：命令跑到哪里都带着它，因此"这个任务还欠着哪些命令"查得出来。
                   # 环境变量是唯一能跨 fork/exec/`setsid`/`nohup` 保留下来的东西——
                   # 会话 id、父进程、进程组都会在脱离时变掉，只有它不变（见 release_task_processes）。
                   UNISON_TASK_ID=t['id'],
                   UNISON_RUN_ID=t.get('run_id') or '')
        key_env=str(config.get('key_env') or '').strip()
        if key_env and key:
            # 与用户配置同名，脚本按自己的习惯读哪一个都可以。
            env[key_env]=key
        token=getattr(self,'api_token','')
        if token:
            # 技能端点与外部调用共用一份令牌：脚本因此能在不经手明文的情况下调用技能端点。
            # 这两个属性由服务层在启动时注入；直接用 Runtime 的调用方（测试、脚本）没有端口可调。
            env['UNISON_API_TOKEN']=token
            base=getattr(self,'base_url','')
            if base: env['UNISON_BASE_URL']=base
        return env
    async def tool_workspace_shell(self,t,a):
        self.workspace.reconcile(self.task(t['id']))
        timeout=max(1,min(int(a.get('timeout_seconds',120)),3600))
        with tempfile_output(self.store,t['id']) as output:
            process=await asyncio.create_subprocess_exec('/bin/bash','-c',a['command'],cwd=t['workspace'],env=self.shell_environment(t),stdout=output,stderr=asyncio.subprocess.STDOUT,start_new_session=True)
            self.processes[t['id']]=process
            timed_out=False
            try:
                await asyncio.wait_for(process.wait(),timeout)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError): os.killpg(process.pid,signal.SIGKILL)
                await process.wait()
                timed_out=True
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError): os.killpg(process.pid,signal.SIGKILL)
                await process.wait(); raise
            finally:
                self.processes.pop(t['id'],None)
                self.workspace.reconcile(self.task(t['id']),t['id'])
            output.seek(0); content=output.read(); ref=self.store.blob(content)
        fresh=self.task(t['id'])
        execution={'id':uid('execution_'),'task_id':t['id'],'command':a['command'],
                   'exit_code':process.returncode,'timed_out':timed_out,'output_ref':ref,
                   'manifest_ref':self.store.blob(dumps(fresh['manifest'])),'modes':fresh.get('modes',{}),'created':now()}
        self.store.put('execution',execution)
        return {'execution_ref':execution['id'],'manifest_ref':execution['manifest_ref'],'exit_code':process.returncode,'timed_out':timed_out,'output':content.decode(errors='replace')[-8000:], 'output_ref':ref,'truncated':len(content)>8000}
    async def tool_workspace_diff(self,t,a):
        fresh=self.task(t['id']); self.workspace.reconcile(fresh); return self.workspace.changes(fresh)
    async def tool_workspace_integrate(self,t,a):
        source=self.task(a['source_task'])
        if source['id'] in self.processes or source['id'] in self.active: raise ValueError('来源任务仍在运行，请等待其停止后集成')
        if source['run_id']!=t['run_id']: raise ValueError('只能集成同一运行的成果')
        result=self.workspace.integrate(source,self.task(t['id']))
        if source['revision']!=t['revision'] and result['integrated']:
            self.store.event('ArtifactAdopted',{'source_task':source['id'],'old_revision':source['revision'],'requires_validation':True},t)
        return result
    async def tool_workspace_archive(self,t,a): return self.workspace.archive(self.run(t['run_id']))
    async def tool_workspace_restore(self,t,a): return self.workspace.restore_archive(a['archive_path'],a.get('task_id'),a.get('version','after'))
    async def tool_artifacts_read(self,t,a):
        text=self.store.read_blob(a['ref']).decode(errors='replace'); start=max(0,a.get('start',0)); limit=min(20000,max(1,a.get('limit',8000)))
        return {'content':text[start:start+limit],'start':start,'total':len(text)}
    async def tool_knowledge_search(self,t,a):
        rows=self.knowledge_search(a['query'],t)
        scope=a.get('scope')
        if scope: rows=[x for x in rows if (x.get('scope') or 'project')==scope]
        return [{k:v for k,v in x.items() if k!='content'} | {'excerpt':x['content'][:500]} for x in rows[:30]]

    async def tool_knowledge_read(self,t,a):
        """两种读法：按 id 读一条；或按序号顺序枚举本次运行的共享信息。

        枚举这条是"所有人都看到同一份"的载体——它不过滤、不按相关度排序、不默认截断，
        因此任何调用者拿到的都是同一序列化结果。各人自己 `knowledge_search` 是检索，
        排序与截断会让"看到的内容"随人而异。
        """
        if not a.get('id'):
            return {'scope':a.get('scope') or 'run',
                    'entries':self.knowledge_entries(t,scope=a.get('scope') or 'run',
                                                     from_seq=int(a.get('from_seq') or 0),
                                                     limit=int(a.get('limit') or 0))}
        item=self.store.get('knowledge',a['id'])
        return self.knowledge_validity(item,t)

    async def tool_broadcast(self,t,a): return self.publish_knowledge(t,a)
    def skill_summary(self,skill,workspace):
        """技能的可调用契约摘要：模型据此决定是走端口批量调用，还是在 shell 里直接跑。"""
        invocation=skill.get('invocation')
        return {'name':skill['name'],'description':skill['description'],
                'when_to_use':skill['when_to_use'],'source':skill['source'],'rank':skill['rank'],
                'invocable':bool(invocation),'batch':bool(invocation and invocation['batch']),
                'max_batch':MAX_BATCH if invocation else 0,
                'timeout_seconds':invocation['timeout'] if invocation else None,
                'invoke_endpoint':'POST /api/skills/call' if invocation else None,
                'jobs_endpoint':'GET /api/skill/jobs/<id>' if invocation else None,
                'workspace':workspace}

    async def tool_skill(self,t,a):
        """加载技能全文：正文来自当前磁盘内容，因此编辑技能无需重启或版本管理。

        带 `batch` 时同时提交一次端口调用——技能契约与调用入口在同一个动作里给全，
        模型不必先发现"原来还能批量"。
        """
        name=str(a.get('name') or '').strip()
        project_root=self.task_project_root(t)
        try:
            skill=self.skills.load(name,t['workspace'],project_root=project_root)
        except SkillError as exc:
            snapshot=self.skill_snapshot(t['workspace'],project_root)
            available=[self.skill_summary(item,t['workspace']) for item in snapshot['skills'] if item['model_invocable']]
            return {'error':str(exc),'available_skills':available}
        self.store.event('SkillLoaded',{'name':skill['name'],'source':skill['source'],
                                        'path':skill['path'],'rank':skill['rank']},t)
        result={'name':skill['name'],'source':skill['source'],'path':skill['path'],
                'base':skill['base'],'when_to_use':skill['when_to_use'],
                'invocation':self.skill_summary(skill,t['workspace']),
                'content':render_content(skill)}
        batch=a.get('batch')
        if batch is None:
            return result
        try:
            job=self.skill_invoker.submit(skill['name'],batch=batch,workspace=t['workspace'],
                                          project_root=project_root)
        except SkillInvocationError as exc:
            result['batch_error']=str(exc)
            return result
        result['job_id']=job['id']
        result['job_status']=job['status']
        result['poll']=f"GET /api/skill/jobs/{job['id']}"
        self.store.event('SkillBatchSubmitted',{'id':job['id'],'skill':skill['name'],'count':job['count']},t)
        return result

    async def tool_skills_list(self,t,a):
        """技能目录 + 调用契约；只读，不加载正文。"""
        project_root=self.task_project_root(t)
        snapshot=self.skill_snapshot(t['workspace'],project_root)
        return {'workspace':t['workspace'],'project_root':project_root,
                'skills':[self.skill_summary(item,t['workspace']) for item in snapshot['skills']
                          if item['model_invocable']],
                'diagnostics':snapshot['diagnostics'],
                'note':('invocable=true 的技能可通过端口批量调用：POST /api/skills/call 提交，'
                        'GET /api/skill/jobs/<id> 轮询；认证头 Authorization: Bearer $UNISON_API_TOKEN。')}

    def local_compute_view(self):
        """本地算力现在的忙闲：显存占用、GPU 占用率、系统负载。

        "预算"不只指模型调用——本地显卡是被整个 run 共享的稀缺资源。委派前若不知道
        卡上还剩多少显存、有没有别的活在跑，就只能靠猜；猜错的代价是排队或 OOM。
        只读 sysfs/proc，不引入依赖；读不到就如实返回 unavailable，不编造。
        """
        view={'gpus':[],'system_load':{},'note':''}
        try:
            view['system_load']={'loadavg':open('/proc/loadavg').read().split()[:3],'cpus':os.cpu_count()}
        except Exception as e:
            view['system_load']={'error':str(e)}
        try:
            for card in sorted(Path('/sys/class/drm').glob('card[0-9]*')):
                device=card/'device'
                # card0-DP-1 这类是接口不是设备：真设备的 device/drm 才存在。
                if not (device/'drm').is_dir(): continue
                entry={'card':card.name}
                for key,name in (('mem_info_vram_used','vram_used_bytes'),
                                 ('mem_info_vram_total','vram_total_bytes'),
                                 ('gpu_busy_percent','busy_percent')):
                    try: entry[name]=int((device/key).read_text().strip())
                    except Exception: entry[name]=None
                total=entry.get('vram_total_bytes')
                if total:
                    used=entry.get('vram_used_bytes') or 0
                    entry['vram_used_pct']=round(100*used/total)
                    entry['vram_free_mb']=round((total-used)/1048576)
                    # 与 gpu 技能写死的判据保持一致：**持有渲染节点不等于满载**——
                    # 桌面常驻程序会一直持有节点，看占用率与显存才是有效判据。
                    entry['saturated']=bool((entry.get('busy_percent') or 0)>=20
                                            or entry['vram_used_pct']>=60)
                view['gpus'].append(entry)
        except Exception as e:
            view['note']='本地算力读取失败：'+str(e)
        if not view['gpus'] and not view['note']:
            view['note']='未发现可用 GPU 设备；本地算力可能不可用或仅有 CPU（详见 gpu 技能）。'
        if view['gpus']:
            view['note']=('saturated 是判据（占用率≥20% 或 显存≥60%）；'
                          '单看"有没有进程持有渲染节点"会误判——桌面常驻程序一直持有。')
        return view

    async def tool_usage_report(self,t,a):
        """本运行与本任务的模型用量与当前负载：让"预算"成为可观察对象。"""
        limiter=self.models.limiter
        run_usage=limiter.usage_summary(t['run_id'])
        buckets=[b for b in limiter.snapshot()]
        by_tier={}
        for record in self.store.all('model_usage'):
            if record.get('run_id')!=t['run_id']: continue
            try:
                tier=self.models.tiers_of(self.store.get('model',record.get('model_id')))
            except ValueError:
                tier='unknown'
            entry=by_tier.setdefault(tier,{'calls':0,'tokens':0})
            entry['calls']+=1; entry['tokens']+=record.get('tokens',0)
        messages=[m for m in self.store.all('message') if m['run_id']==t['run_id']]
        hops=[m.get('hop') or 1 for m in messages]
        unclaimed=[m for m in messages if m.get('status')=='unclaimed']
        return {'run':run_usage,'this_task':self.task_usage(t['id']),
                'heavy':self.heavy_usage(t['run_id']),'by_tier':by_tier,
                'open_requests':len(self.open_requests(requester=t['id'])),
                'unclaimed_replies':len(unclaimed),
                'average_hop':(round(sum(hops)/len(hops),2) if hops else 0),
                'shared':self.shared_summary(t),
                'divergence':self.knowledge_divergence(t['run_id']),
                'buckets':buckets,'in_flight':sum(b['in_flight'] for b in buckets),
                'queued':sum(b['queued'] for b in buckets),
                # 本地算力与模型调用是同一笔预算的两半：委派前先看这里，别把卡排满。
                'local_compute':self.local_compute_view(),
                'note':('用量按真实响应里的 usage 记账；并发与窗口额度按 provider+模型 分桶，'
                        '未声明额度时同桶并发为 1，因此同模型的并发调用会排队而不是一起打上游。'
                        'local_compute 是本地算力当前忙闲（显存/占用率/系统负载）：本地跑模型前先看它。')}

    def shared_summary(self,t):
        """共享信息层的概况：条目数、最新序号、被推送过谁。"""
        entries=self.knowledge_entries(t,scope='run')
        notified=set()
        for m in self.store.all('message'):
            if m.get('run_id')==t['run_id'] and m.get('kind')=='shared-entry': notified.add(m['task_id'])
        return {'entries':len(entries),'latest_seq':(entries[-1]['seq'] if entries else 0),
                'notified_tasks':sorted(notified)}

    def knowledge_divergence(self,run_id):
        """**只报告、不阻断**：这次事故那一类"信息流分叉"的早期可见性。

        两类问题（都来自真机 `run_574484cad16f` 的形态）：

        1. `late_injection`：某个任务的 brief 里出现了共享条目的原文——说明有人把共享信息
           **复制**进了别人的任务描述。复制一次就产生一个分叉，这正是当时发生的事
           （C 的 goal 缺 B 的发言、D 的 goal 缺 C 的发言）。
        2. `unnotified_member`：共享条目发布时，同一运行里有些任务没被推送——它们的可见性
           取决于"它自己有没有去搜"，而不是"所有人都看到同一份"。
        """
        try: run=self.run(run_id)
        except ValueError: return []
        tasks=[t for t in self.store.all('task') if t['run_id']==run_id and t['revision']==run['revision']]
        entries=[k for k in self.store.all('knowledge')
                 if k.get('run_id')==run_id and (k.get('scope') or 'project')=='run']
        issues=[]
        # `unnotified_member` 的判据要克制：谁"应该"看到一条共享信息取决于作者声明的名单，
        # 运行时并不知道。所以只查**互相可见性**——凡是在这一局里发过言的人（贡献者），
        # 都应当收到过其他人的条目；某个人只在"发"却从没"收"，就是真分叉。
        # （先前按"运行里所有任务"判定，把只看工具结果、不需要推送的调度者误报了一堆。）
        authors={k.get('task_id') for k in entries if k.get('task_id')}
        received={}
        for m in self.store.all('message'):
            if m.get('run_id')!=run_id or m.get('kind')!='shared-entry': continue
            received.setdefault(m['task_id'],set()).add(m.get('topic','').replace('knowledge:',''))
        for task_id in sorted(authors):
            mine={k['id'] for k in entries if k.get('task_id')==task_id}
            others=[k for k in entries if k.get('task_id')!=task_id]
            if len(others)<2: continue
            got=received.get(task_id,set())
            missing=[k['id'] for k in others if k['id'] not in got]
            if len(missing)==len(others):
                issues.append({'kind':'unnotified_member','task':task_id,
                               'note':'该任务发过共享信息，但从未收到过其他人的任何一条：'
                                      '它的可见性取决于自己去搜，而不是"所有人都看到同一份"',
                               'missing':len(missing)})
        # 第二类：共享条目的原文被**抄进别人的任务描述**（这次事故的形态）。
        for entry in entries:
            needle=_normalize(entry.get('content') or '')
            if len(needle)<12: continue
            for task in tasks:
                brief=_task_brief_text(self.store,task)
                if not brief: continue
                hay=_normalize(brief)
                if len(hay)<len(needle): continue
                ratio=_best_window_ratio(hay,needle)
                if ratio>=0.85:
                    issues.append({'kind':'late_injection','entry':entry['id'],'seq':entry.get('seq'),
                                   'task':task['id'],'similarity':round(ratio,3),
                                   'note':'该任务的任务描述里出现共享条目原文：共享信息应当"自己读"，'
                                          '不要在创建任务时把正文抄进去（会分叉且不公平）'})
        return issues

    async def tool_context_compact(self,t,a):
        # 标记后由 step() 在本轮工具批次结束时立即执行；这里只落一个待办位，
        # 因为正在执行的工具调用必须成对落盘后才能改写历史。
        fresh=self.task(t['id']); fresh['compact_requested']=True; self.store.put('task',fresh)
        return {'scheduled':True,'when':'本轮工具批次结束后立即压缩；无需批准，若本轮同时调用 tasks_complete/tasks_yield/human_ask 则不生效'}
    async def tool_goals_activate_revision(self,t,a):
        message=self.store.get('message',a['change_message_id'])
        if message.get('kind')!='user' or message['run_id']!=t['run_id']: raise ValueError('必须引用用户提出的变更消息')
        if t['parent_id']: raise ValueError('只有根任务可切换整个目标版本')
        return self.revise(t['run_id'],a['goal'])
    async def tool_tasks_complete(self,t,a):
        self.complete(self.task(t['id']),a); return {'completed':True}


@contextlib.contextmanager
def tempfile_output(store,id):
    import tempfile
    with tempfile.TemporaryFile() as f:
        yield f


def validate(schema,value,path='arguments'):
    type_=schema.get('type')
    checks={'object':lambda v:isinstance(v,dict),'array':lambda v:isinstance(v,list),
            'string':lambda v:isinstance(v,str),'boolean':lambda v:isinstance(v,bool),
            'integer':lambda v:isinstance(v,int) and not isinstance(v,bool),
            'number':lambda v:isinstance(v,(int,float)) and not isinstance(v,bool)}
    if type_ in checks and not checks[type_](value): raise ValueError(path+': expected '+type_)
    if 'enum' in schema and value not in schema['enum']: raise ValueError(path+': invalid value')
    if type_=='object':
        for key in schema.get('required',[]):
            if key not in value: raise ValueError(path+': missing '+key)
        for key,item in value.items():
            prop=schema.get('properties',{}).get(key)
            if prop: validate(prop,item,path+'.'+key)
            elif schema.get('additionalProperties') is False: raise ValueError(path+': unknown '+key)
    if type_=='array':
        for item in value: validate(schema.get('items',{}),item,path+'[]')

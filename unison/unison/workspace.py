from __future__ import annotations
import difflib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
import threading
from .store import uid, now, dumps

EXCLUDED = {'.git','.unison','node_modules','.venv','venv','__pycache__','.pytest_cache'}
MAX_FILE = 16 * 1024 * 1024
_WORKSPACE_LOCKS = {}
_WORKSPACE_LOCKS_GUARD = threading.Lock()


def workspace_lock(path):
    key = str(Path(path).resolve())
    with _WORKSPACE_LOCKS_GUARD:
        return _WORKSPACE_LOCKS.setdefault(key, threading.RLock())


class Workspace:
    def __init__(self, store):
        self.store = store
        (store.root/'workspaces').mkdir(exist_ok=True)
        (store.root/'archives').mkdir(exist_ok=True)
        # 控制台会把这个目录作为默认项目路径预填，必须先存在。
        (store.root/'playground').mkdir(exist_ok=True)

    def scan(self, path, stats=None, with_stats=False):
        """扫描工作区，返回 (manifest, omissions)；with_stats=True 时返回第三个值 stats。

        `stats` 是上一次扫描的 {相对路径: (mtime_ns, size, hash)}：**大小与修改时间都没变**时
        直接复用旧哈希，避免每 2 秒把整个仓库重读一遍（大仓库下这是主要开销）。
        它只是"跳过重算"的加速器，manifest 里始终是内容哈希。
        """
        root = Path(path).resolve()
        manifest = {}
        omissions = []
        next_stats = {}
        for directory, dirs, files in os.walk(root):
            omissions.extend(str((Path(directory)/d).relative_to(root))+'/' for d in dirs
                             if d in EXCLUDED or (Path(directory)/d).is_symlink() or (Path(directory)/d).resolve() == self.store.root)
            dirs[:] = [d for d in dirs if d not in EXCLUDED and not (Path(directory)/d).is_symlink()
                       and (Path(directory)/d).resolve() != self.store.root]
            for name in sorted(files):
                f = Path(directory)/name
                rel = str(f.relative_to(root))
                try:
                    if f.is_symlink():
                        omissions.append(rel)
                        continue
                    info = f.stat()
                    if info.st_size > MAX_FILE:
                        omissions.append(rel)
                        continue
                    known = (stats or {}).get(rel)
                    if isinstance(known, (list, tuple)) and len(known) == 3 and known[0] == info.st_mtime_ns and known[1] == info.st_size and known[2]:
                        digest = known[2]
                    else:
                        digest = self.store.blob(f.read_bytes())
                    manifest[rel] = digest
                    next_stats[rel] = [info.st_mtime_ns, info.st_size, digest]
                except (OSError, FileNotFoundError):
                    omissions.append(rel)
        return (manifest, omissions, next_stats) if with_stats else (manifest, omissions)

    def setup(self, task, parent=None):
        if parent:
            base, omitted = self.scan(parent['workspace'])
            target = self.store.root/'workspaces'/task['id']
            target.mkdir(exist_ok=True)
            self.restore_manifest(base,target,self.file_modes(parent['workspace'],base))
            task['workspace'] = str(target)
        else:
            target = Path(task['workspace']).expanduser().resolve()
            if not target.is_dir():
                raise ValueError('项目目录不存在')
            task['workspace'] = str(target)
            base, omitted = self.scan(target)
        task['base_modes'] = self.file_modes(target,base)
        task['modes'] = dict(task['base_modes'])
        task['base_manifest'] = base
        task['manifest'] = base
        task['omitted_files'] = omitted
        task['baseline_id'] = self.store.blob(dumps(base))
        # 记录首次扫描的 stat 指纹，后续扫描据此跳过未变文件的重哈希。
        task['file_stats'] = self.scan(target,with_stats=True)[2]

    def file_modes(self, path, manifest):
        return {rel:(Path(path)/rel).stat().st_mode & 0o777 for rel in manifest}

    def restore_manifest(self, manifest, path, modes=None):
        root = Path(path)
        for rel, digest in manifest.items():
            f = root/rel
            f.parent.mkdir(parents=True,exist_ok=True)
            f.write_bytes(self.store.read_blob(digest))
            if modes and rel in modes: f.chmod(modes[rel])

    def reconcile(self, task, actor='external/unknown', call_id=None):
        # 副本已经被归档释放：目录不再存在是**我们自己**造成的，不是"文件被删了"。
        # 少了这一条，被收掉的任务在收尾时（工具的 finally 里还会对账一次）会把整份
        # 副本记成一批删除记录，而那些删除从未发生过——文件修改记录必须是真的。
        if task.get('workspace_released'): return []
        current, omissions, stats = self.scan(task['workspace'],task.get('file_stats'),with_stats=True)
        old = task.get('manifest',{})
        modes=self.file_modes(task['workspace'],current)
        old_modes=task.get('modes',modes)
        changes = []
        for path in sorted(set(current)|set(old)):
            if old.get(path) != current.get(path) or old_modes.get(path)!=modes.get(path):
                record = {'id':uid('f_'),'run_id':task['run_id'],'task_id':task['id'],
                          'revision':task['revision'],'path':path,'before':old.get(path),'after':current.get(path),
                          'before_mode':old_modes.get(path),'after_mode':modes.get(path),
                          'actor':actor,'call_id':call_id,'created':now()}
                self.store.put('file',record)
                self.store.event('FileVersionRecorded',record,task)
                changes.append(record)
        task['modes'] = modes
        task['file_stats'] = stats
        if 'base_modes' not in task:
            task['base_modes']={p:modes[p] for p in task.get('base_manifest',{}) if p in modes}
        task['manifest'] = current
        task['omitted_files'] = omissions
        self.store.put('task',task)
        if changes:
            self.invalidate(task, {x['path'] for x in changes})
        return changes

    def invalidate(self, task, paths):
        """文件变化后把直接来源受影响的知识标失效，并沿依赖图**双向**传播。

        反向边（`dependents`）由 `publish_knowledge` 维护；旧记录没有这个字段时退回到
        按 `dependencies` 前向扫描，行为与之前一致。
        """
        stale = set()
        entries = self.store.all('knowledge')
        for item in entries:
            if item.get('stale'): continue
            sources=item.get('sources',[])
            if item.get('workspace') != task['workspace'] and item.get('project_workspace') != task['workspace']:
                continue
            if any(x['path'] in paths for x in sources):
                stale.add(item['id'])
        changed = True
        while changed:
            changed = False
            for item in entries:
                if item.get('stale') or item['id'] in stale: continue
                edges=set(item.get('dependencies',[]))
                edges.update(x.get('id') for x in item.get('dependents',[]) if isinstance(x,dict))
                if stale.intersection(edges):
                    stale.add(item['id']); changed = True
        for item in entries:
            if item['id'] in stale:
                item.update(stale=True,stale_reason='来源文件或依赖知识已经变化')
                self.store.put('knowledge',item)
                self.store.event('KnowledgeInvalidated',{'id':item['id']},task)

    def diff(self, before, after, path):
        try:
            a = self.store.read_blob(before).decode() if before else ''
            b = self.store.read_blob(after).decode() if after else ''
            return ''.join(difflib.unified_diff(a.splitlines(True),b.splitlines(True),fromfile='a/'+path,tofile='b/'+path))
        except UnicodeDecodeError:
            return '二进制文件变更；前后内容已保存为对象。'

    def changes(self, task):
        current = task.get('manifest',{})
        base = task.get('base_manifest',{})
        return [{'path':p,'before':base.get(p),'after':current.get(p),
                 'before_mode':task.get('base_modes',{}).get(p),'after_mode':task.get('modes',{}).get(p),
                 'diff':self.diff(base.get(p),current.get(p),p)}
                for p in sorted(set(current)|set(base)) if current.get(p)!=base.get(p) or task.get('modes',{}).get(p)!=task.get('base_modes',{}).get(p)]

    def integrate(self, source, target):
        """Prepare merges and commit under a per-workspace lock with a manifest CAS."""
        lock=workspace_lock(target['workspace'])
        with lock:
            return self._integrate_locked(source,target)

    def _integrate_locked(self, source, target):
        self.recover_integrations()
        self.reconcile(source)
        self.reconcile(target)
        previous=source.get('integrated_targets',{}).get(target['id'],{})
        base = previous.get('manifest',source['base_manifest']); incoming = source['manifest']; current = target['manifest']
        expected_manifest_id=self.store.blob(dumps(current))
        proposals = {}; conflicts = []; proposed_modes={}
        base_modes=previous.get('modes',source.get('base_modes',{}))
        for p in incoming:
            bm=base_modes.get(p); im=source.get('modes',{}).get(p,0o644); cm=target.get('modes',{}).get(p)
            proposed_modes[p]=cm if im==bm and cm is not None else im
            if im!=bm and cm!=im:
                if cm not in (None,bm):
                    conflicts.append({'path':p,'reason':'文件权限冲突','base_mode':bm,'incoming_mode':im,'current_mode':cm})
                else: proposals[p]=current.get(p,incoming[p])
        for p in sorted(set(base)|set(incoming)):
            b, i, c = base.get(p), incoming.get(p), current.get(p)
            if b == i or c == i:
                continue
            if c == b:
                proposals[p] = i
                continue
            merged = None
            if b and i and c:
                try:
                    texts = [self.store.read_blob(h).decode() for h in [c,b,i]]
                    with tempfile.TemporaryDirectory() as d:
                        paths = [Path(d)/str(n) for n in range(3)]
                        for f,content in zip(paths,texts): f.write_text(content)
                        proc = subprocess.run(['git','merge-file','-p',*[str(f) for f in paths]],capture_output=True)
                    if proc.returncode == 0:
                        merged = self.store.blob(proc.stdout)
                except (UnicodeDecodeError,FileNotFoundError):
                    pass
            if merged:
                proposals[p] = merged
            else:
                conflicts.append({'path':p,'base':b,'incoming':i,'current':c})
        if conflicts:
            record = {'id':uid('conflict_'),'run_id':target['run_id'],'revision':target['revision'],
                      'source_task':source['id'],'target_task':target['id'],'files':conflicts,'status':'open','created':now()}
            self.store.put('conflict',record)
            self.store.event('ConflictDetected',record,target)
            return {'integrated':False,'conflict':record}
        latest, latest_omissions = self.scan(target['workspace'])
        if self.store.blob(dumps(latest)) != expected_manifest_id or self.file_modes(target['workspace'],latest)!=target.get('modes',{}):
            target['manifest']=latest; target['omitted_files']=latest_omissions; self.store.put('task',target)
            self.store.event('IntegrationAborted',{'source_task':source['id'],'reason':'workspace-changed'},target)
            return {'integrated':False,'retryable':True,'error':'目标工作区在集成准备期间已变化；未写入，请重试'}
        journal={'id':uid('integration_'),'workspace':target['workspace'],'status':'pending',
                 'before':{p:current.get(p) for p in proposals},'after':proposals,
                 'after_modes':{p:proposed_modes.get(p,0o644) if h else None for p,h in proposals.items()},
                 'modes':{p:target.get('modes',{}).get(p,0o644) for p in proposals}}
        self.store.put('integration',journal)
        try:
            for p,h in proposals.items():
                path = Path(target['workspace'])/p
                if h:
                    path.parent.mkdir(parents=True,exist_ok=True)
                    temporary = path.with_name(path.name+'.'+journal['id']+'.unison-tmp')
                    temporary.write_bytes(self.store.read_blob(h))
                    temporary.chmod(proposed_modes.get(p,0o644))
                    os.replace(temporary,path)
                elif path.exists(): path.unlink()
            with self.store.transaction():
                self.reconcile(target,'integration:'+source['id'])
                generation=self.store.db.execute('SELECT generation FROM workspace_generations WHERE workspace=?',(str(Path(target['workspace']).resolve()),)).fetchone()
                next_generation=(generation['generation'] if generation else 0)+1
                self.store.db.execute('INSERT INTO workspace_generations(workspace,generation,manifest_id,updated) VALUES(?,?,?,?) '
                                      'ON CONFLICT(workspace) DO UPDATE SET generation=excluded.generation,manifest_id=excluded.manifest_id,updated=excluded.updated',
                                      (str(Path(target['workspace']).resolve()),next_generation,self.store.blob(dumps(target['manifest'])),now()))
                for conflict in self.store.all('conflict'):
                    if conflict['source_task']==source['id'] and conflict['target_task']==target['id'] and conflict['status']=='open':
                        conflict.update(status='resolved',resolved_at=now())
                        self.store.put('conflict',conflict)
                        self.store.event('ConflictResolved',{'id':conflict['id']},target)
                self.store.event('IntegrationCommitted',{'source_task':source['id'],'paths':list(proposals),'verified':False},target)
                # Keep original task baseline for history. Equal incoming/current is already a no-op.
                journal['status']='committed'; self.store.put('integration',journal)
                source.setdefault('integrated_targets',{})[target['id']]={'manifest':dict(source['manifest']),'modes':dict(source.get('modes',{}))}
                source['last_integrated_manifest'] = dict(source['manifest'])
                self.store.put('task',source)
        except Exception:
            self.rollback_integration(journal)
            self.reconcile(target)
            raise
        return {'integrated':True,'paths':list(proposals),'generation':next_generation,'verification':'尚未验证；请运行相关测试'}

    def rollback_integration(self, journal):
        root=Path(journal['workspace'])
        # Never overwrite a subsequent user edit during recovery.
        for rel, before in journal['before'].items():
            path=root/rel
            actual=self.store.blob(path.read_bytes()) if path.is_file() else None
            actual_mode=path.stat().st_mode & 0o777 if path.is_file() else None
            allowed=((before,journal['modes'][rel] if before else None),
                     (journal['after'][rel],journal.get('after_modes',{}).get(rel,actual_mode)))
            if (actual,actual_mode) not in allowed:
                raise RuntimeError('集成恢复发现后续修改，需要人工检查：'+str(path))
        for rel,before in journal['before'].items():
            path=root/rel
            if before:
                path.parent.mkdir(parents=True,exist_ok=True)
                temporary=path.with_name(path.name+'.'+journal['id']+'.rollback-tmp')
                temporary.write_bytes(self.store.read_blob(before)); temporary.chmod(journal['modes'][rel])
                os.replace(temporary,path)
            elif path.exists(): path.unlink()
            temporary=path.with_name(path.name+'.'+journal['id']+'.unison-tmp')
            if temporary.exists(): temporary.unlink()
        journal['status']='rolled_back'; self.store.put('integration',journal)
        self.store.event('IntegrationRolledBack',{'id':journal['id']})

    def recover_integrations(self):
        for journal in self.store.all('integration'):
            if journal['status']=='pending': self.rollback_integration(journal)

    def archive(self, run):
        tasks = [x for x in self.store.all('task') if x['run_id']==run['id']]
        for task in tasks:
            self.reconcile(task)
        bundle = {'run':run,'tasks':tasks,'events':self.store.events(run['id'],limit=1000000),
                  'files':[x for x in self.store.all('file') if x['run_id']==run['id']],
                  'reports':[x for x in self.store.all('report') if x['run_id']==run['id']]}
        objects = set()
        for task in tasks:
            for key in ['base_manifest','manifest']:
                objects.update(task.get(key,{}).values())
        for entry in bundle['files']:
            objects.update(x for x in [entry['before'],entry['after']] if x)
        # Include referenced objects in tool results and knowledge, not just current files.
        bundle['knowledge'] = [x for x in self.store.all('knowledge') if x.get('run_id')==run['id']]
        def collect(value):
            if isinstance(value,dict):
                for v in value.values(): collect(v)
            elif isinstance(value,list):
                for v in value: collect(v)
            elif isinstance(value,str) and len(value)==64 and (self.store.objects/value).is_file(): objects.add(value)
        collect(bundle)
        destination = self.store.root/'archives'/f"{run['id']}-r{run['revision']}-{uid()}.tar.gz"
        with tempfile.TemporaryDirectory() as d:
            meta = Path(d)/'archive.json'; meta.write_text(dumps(bundle))
            with tarfile.open(destination,'w:gz') as tar:
                tar.add(meta,arcname='archive.json')
                for h in sorted(objects): tar.add(self.store.objects/h,arcname='objects/'+h)
        self.store.event('SnapshotArchived',{'path':str(destination)},run_id=run['id'],revision=run['revision'])
        return {'path':str(destination)}


    def release(self, tasks):
        """释放子任务工作区副本，返回实际删掉的清单与字节数。

        归档之所以能真正省下磁盘，靠的是这一步：`setup()` 给每个子任务建的副本
        `<数据目录>/workspaces/<task_id>` 会一直留在磁盘上，运行结束后再没人用它。

        边界写死在代码里，不做推断：
        - 只删**数据目录工作区根**下面的目录，且目录名必须等于任务 id；
        - 根任务直接操作用户的项目目录，永远不在释放范围内；
        - 文件修改记录不在这里——`file` 记录、内容对象、报告都在库里，删副本不会动它们，
          因此"谁在什么时候把哪个文件改成了什么"照旧可查，`workspace_diff` 也能重建差异。
        """
        root=(self.store.root/'workspaces').resolve()
        released=[]
        for task in tasks:
            value=task.get('workspace')
            if not value: continue
            resolved=Path(value).expanduser().resolve()
            if resolved==root or not resolved.is_relative_to(root): continue
            if resolved.name!=task['id'] or not resolved.is_dir(): continue
            bytes_=sum(f.stat().st_size for f in resolved.rglob('*') if f.is_file())
            shutil.rmtree(resolved)
            # 与删除同一笔写下标记：此后这个任务的任何一次对账都不再把"目录不存在"
            # 当成"文件被删除"（见 reconcile 开头）。被杀掉的任务可能还在收尾。
            # 重新读一次再写，避免用调用方手里的旧副本覆盖掉并发更新。
            fresh=self.store.get('task',task['id'])
            fresh['workspace_released']=now()
            self.store.put('task',fresh)
            released.append({'task_id':task['id'],'path':str(resolved),'bytes':bytes_})
        return released


    def restore_archive(self, archive_path, task_id=None, version='after'):
        with tarfile.open(archive_path,'r:gz') as tar:
            member=tar.getmember('archive.json')
            if member.size>64*1024*1024: raise ValueError('归档元数据过大')
            bundle=json.load(tar.extractfile(member))
            task_id=task_id or bundle['run']['root_task']
            task=next((t for t in bundle['tasks'] if t['id']==task_id),None)
            if not task: raise ValueError('归档中不存在此任务')
            manifest=task['base_manifest' if version=='before' else 'manifest']
            destination=self.store.root/'restored'/uid('workspace_')
            destination.mkdir(parents=True)
            for rel,h in manifest.items():
                path=destination/rel
                if not path.resolve().is_relative_to(destination.resolve()): raise ValueError('归档路径无效')
                if len(h)!=64 or any(c not in '0123456789abcdef' for c in h): raise ValueError('无效内容对象')
                obj=tar.getmember('objects/'+h)
                if obj.size>MAX_FILE: raise ValueError('内容对象过大')
                content=tar.extractfile(obj).read()
                if self.store.blob(content)!=h: raise ValueError('归档内容校验失败')
                path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
                modes=task.get('base_modes' if version=='before' else 'modes',{})
                if rel in modes: path.chmod(modes[rel] & 0o777)
        self.store.event('ArchiveRestored',{'archive':str(archive_path),'workspace':str(destination),'task_id':task_id})
        return {'workspace':str(destination),'task_id':task_id,'version':version}

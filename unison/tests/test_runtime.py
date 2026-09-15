import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unison.runtime import Runtime, TERMINAL
from unison.models import ModelError
from unison.store import dumps, now, uid
from unison.tools import SYSTEM
from unison.demo import demo_call


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.project=Path(self.temp.name)/'project'; self.project.mkdir()
        (self.project/'README.md').write_text('original\n')
        self.r=Runtime(Path(self.temp.name)/'data',concurrency=1,batch_seconds=.01)
        self.r.models.adapters['fake']=demo_call
        self.r.store.put('model',{'id':'test','model':'test','adapter':'fake','base_url':'test://local','no_key':True,'context_window':64000,'max_output':4096})

    @staticmethod
    def brief(messages):
        """任务简报是第一条 user 消息。

        不能按下标取：系统消息条数会随版本变化（基础提示 + 技能目录），下标会漂移；
        这类"按下标猜消息"的写法正是之前晚到结果测试挂死的根因。
        """
        content=next((m.get('content') or '' for m in messages if m.get('role')=='user'),'{}')
        try:
            return json.loads(content)
        except ValueError:
            return {'goal':content}

    async def asyncTearDown(self):
        await self.r.stop(); self.temp.cleanup()

    def create(self,goal='test goal'):
        run=self.r.create_run(goal,str(self.project),'test')
        return run,self.r.task(run['root_task'])

    def children(self,run,names):
        """建若干子任务并返回。`create_task` 要的是 run 记录，不是任务记录。"""
        return [self.r.create_task(run,name,'test',self.r.task(run['root_task'])) for name in names]

    async def wait_until(self,predicate,seconds=10):
        end=asyncio.get_running_loop().time()+seconds
        while asyncio.get_running_loop().time()<end:
            if predicate(): return
            await asyncio.sleep(.03)
        self.fail('timed out: '+dumps([{k:t.get(k) for k in ['id','goal','status','error']} for t in self.r.store.all('task')]))

    async def test_recursive_human_workflow_one_slot(self):
        run,t=self.create('离线演示：测试')
        await self.r.start()
        await self.wait_until(lambda:bool(self.r.store.all('question')))
        q=self.r.store.all('question')[0]
        self.assertEqual(self.r.task(t['id'])['status'],'waiting')
        self.r.answer(q['id'],'验收名称')
        await self.wait_until(lambda:self.r.run(run['id'])['status']=='completed',15)
        self.assertEqual((self.project/'choice.txt').read_text(),'验收名称\n')
        self.assertTrue((self.project/'collaboration.md').is_file())
        self.assertEqual(len(self.r.store.all('task')),4)
        self.assertTrue(self.r.store.all('knowledge'))
        self.assertTrue(self.r.store.all('report'))

    async def test_progress_messages_do_not_wake(self):
        run,t=self.create()
        t['status']='waiting'; t['wait']={'task_ids':[],'topics':['blocker'],'mode':'any','after_cursor':0};self.r.store.put('task',t)
        for i in range(50): self.r.deliver(t['id'],str(i),delivery='inbox',topic='progress')
        self.r.wake_waiters()
        self.assertEqual(self.r.task(t['id'])['status'],'waiting')
        self.assertFalse([e for e in self.r.store.events() if e['type']=='ModelCalled'])

    async def test_explicit_wake_batches_and_deduplicates(self):
        run,t=self.create(); t['status']='waiting'; self.r.store.put('task',t)
        self.r.deliver(t['id'],'blocker',delivery='wake',message_id='same')
        self.r.deliver(t['id'],'blocker',delivery='wake',message_id='same')
        self.r.deliver(t['id'],'second',delivery='wake')
        self.assertEqual(len([e for e in self.r.store.events() if e['type']=='TaskWoken']),1)
        self.assertEqual(self.r.task(t['id'])['status'],'queued')

    async def test_event_arrives_before_wait_registration(self):
        run,t=self.create(); t['status']='running'; self.r.store.put('task',t)
        before=self.r.store.cursor()
        self.r.deliver(t['id'],'already here',topic='protocol')
        await self.r.tool_tasks_yield(t,{'topics':['protocol'],'after_cursor':before})
        self.assertEqual(self.r.task(t['id'])['status'],'queued')

    async def test_wait_cycle_rejected(self):
        run,t=self.create(); child=self.r.create_task(run,'child','test',t)
        child['status']='running';self.r.store.put('task',child)
        await self.r.tool_tasks_yield(child,{'task_ids':[t['id']]})
        result=await self.r.tool_tasks_yield(t,{'task_ids':[child['id']]})
        self.assertIn('error',result)
        self.assertTrue([e for e in self.r.store.events() if e['type']=='RunStalled'])

    async def test_deadline_wakes_only_once(self):
        run,t=self.create(); await self.r.tool_tasks_yield(t,{'timeout_seconds':.001})
        await asyncio.sleep(.01); self.r.wake_waiters();self.r.wake_waiters()
        self.assertEqual(len([e for e in self.r.store.events() if e['type']=='TaskWoken']),1)

    async def test_waiting_on_self_is_rejected_without_stall_spam(self):
        """真机缺陷：子任务把自身 id 放进订阅当心跳，被判成"等待环"并刷了 12 条 RunStalled。"""
        run,t=self.create()
        result=await self.r.tool_tasks_yield(self.r.task(t['id']),{'task_ids':[t['id']],'timeout_seconds':30})
        self.assertIn('error',result)
        self.assertIn('不能等待自己',result['error'])
        self.assertNotIn('stalled',result)
        self.assertFalse([e for e in self.r.store.events() if e['type']=='RunStalled'])
        self.assertEqual(self.r.task(t['id'])['status'],'queued')   # 没有被置为 waiting

    async def test_waiting_on_self_ignored_when_other_targets_present(self):
        run,t=self.create(); child=self.r.create_task(run,'child','test',t)
        result=await self.r.tool_tasks_yield(self.r.task(t['id']),{'task_ids':[t['id'],child['id']],'timeout_seconds':30})
        self.assertTrue(result['waiting'])
        self.assertEqual(result['subscription']['task_ids'],[child['id']])
        self.assertIn('自身 id',result['note'])
        self.assertFalse([e for e in self.r.store.events() if e['type']=='RunStalled'])

    async def test_real_wait_cycle_is_still_rejected(self):
        """真正的环必须继续被拒绝：孤儿任务依赖自己的祖先。"""
        run,a=self.create(); b=self.r.create_task(run,'b','test',a,)
        child=self.r.create_task(run,'c','test',b,)
        # 让 b 等 c，形成 a→b→c→…的封闭等待
        await self.r.tool_tasks_yield(self.r.task(b['id']),{'task_ids':[child['id']],'timeout_seconds':30})
        result=await self.r.tool_tasks_yield(self.r.task(child['id']),{'task_ids':[b['id']],'timeout_seconds':30})
        self.assertIn('error',result)
        self.assertIn('封闭环',result['error'])
        self.assertEqual(len([e for e in self.r.store.events() if e['type']=='RunStalled']),1)

    async def test_declared_deadline_wakes_the_model(self):
        """模型自己声明的时限到了就交还给它——运行时不再静默顺延窗口。

        原先到期会 "静默顺延"（`WaitRenewed`），理由是减少"到期唤醒 → 查表 → 再等待"的空耗。
        但那是运行时替模型决定"继续等"，而且顺延次数还有上限（`MAX_WAIT_RENEWALS=20`）。
        现在时限完全由模型声明：到了就唤醒，让它自己决定继续等、改条件还是收尾。
        """
        run,t=self.create(); child=self.r.create_task(run,'child','test',t)
        result=await self.r.tool_tasks_yield(self.r.task(t['id']),
                                            {'task_ids':[child['id']],'timeout_seconds':.001})
        self.assertTrue(result['waiting'])
        await asyncio.sleep(.01)
        self.r.wake_waiters()
        current=self.r.task(t['id'])
        self.assertEqual(current['status'],'queued')
        self.assertEqual(current.get('wake_cause'),'deadline')
        # 不再有顺延记账
        self.assertEqual([e for e in self.r.store.events() if e['type']=='WaitRenewed'],[])

    async def test_pure_timing_wait_never_auto_renews(self):
        """没有唤醒条件的纯计时等待到期必须交回模型，否则任务会变成只能靠取消结束的睡眠。"""
        run,t=self.create()
        await self.r.tool_tasks_yield(t,{'timeout_seconds':.001})
        await asyncio.sleep(.01); self.r.wake_waiters()
        self.assertEqual(self.r.task(t['id'])['status'],'queued')

    async def test_revision_cancels_descendants_and_old_answers(self):
        run,t=self.create(); child=self.r.create_task(run,'child','test',t)
        await self.r.tool_human_ask(child,{'question':'old question'})
        q=self.r.store.all('question')[0]
        revised=self.r.revise(run['id'],'new goal')
        self.assertEqual(revised['revision'],2)
        self.assertEqual(self.r.task(child['id'])['status'],'superseded')
        with self.assertRaises(ValueError): self.r.answer(q['id'],'late')
        with self.assertRaises(RuntimeError): self.r.guard(t,t['epoch'])
        self.assertTrue(self.r.task(revised['root_task'])['baseline_pending'])

    async def test_late_model_result_cannot_write(self):
        started=asyncio.Event();release=asyncio.Event()
        async def adapter(config,messages,tools):
            goal=self.brief(messages)['goal']
            if goal=='old':
                started.set()
                try: await release.wait()
                except asyncio.CancelledError: await release.wait()
                return {'role':'assistant','tool_calls':[{'id':'late','type':'function','function':{'name':'workspace_write','arguments':dumps({'path':'bad.txt','content':'old'})}}]},{}
            return {'role':'assistant','content':'new done'},{}
        self.r.models.adapters['fake']=adapter
        run,t=self.create('old');await self.r.start();await started.wait()
        revised=self.r.revise(run['id'],'new');release.set()
        await self.wait_until(lambda:self.r.task(revised['root_task'])['status']=='completed')
        self.assertFalse((self.project/'bad.txt').exists())
        self.assertTrue([e for e in self.r.store.events() if e['type']=='LateModelResult'])

    async def test_external_file_changes_invalidate_knowledge(self):
        run,t=self.create()
        k=self.r.publish_knowledge(t,{'title':'README','content':'original','sources':['README.md']})
        dependent=self.r.publish_knowledge(t,{'title':'dependent','content':'inference','dependencies':[k['id']]})
        (self.project/'README.md').write_text('changed\n');self.r.workspace.reconcile(t)
        self.assertTrue(self.r.store.get('knowledge',k['id'])['stale'])
        self.assertTrue(self.r.store.get('knowledge',dependent['id'])['stale'])
        self.assertEqual(self.r.store.all('file')[0]['actor'],'external/unknown')

    async def test_three_way_conflict_preserves_files(self):
        run,t=self.create(); child=self.r.create_task(run,'child','test',t)
        (Path(child['workspace'])/'README.md').write_text('child\n')
        (self.project/'README.md').write_text('parent\n')
        result=self.r.workspace.integrate(child,t)
        self.assertFalse(result['integrated'])
        self.assertEqual((self.project/'README.md').read_text(),'parent\n')
        self.assertEqual((Path(child['workspace'])/'README.md').read_text(),'child\n')

    async def test_integration_is_idempotent_and_preserves_history(self):
        run,t=self.create(); child=self.r.create_task(run,'child','test',t)
        (Path(child['workspace'])/'new.txt').write_text('new')
        self.assertTrue(self.r.workspace.integrate(child,t)['integrated'])
        self.assertEqual(self.r.workspace.integrate(child,self.r.task(t['id']))['paths'],[])
        self.assertEqual(self.r.workspace.changes(self.r.task(child['id']))[0]['path'],'new.txt')

    async def test_shell_output_and_file_history(self):
        run,t=self.create()
        result=await self.r.tool_workspace_shell(t,{'command':"printf 'generated' > output.txt; printf 'ok'"})
        self.assertEqual(result['exit_code'],0);self.assertEqual(result['output'],'ok')
        self.assertEqual(self.r.store.read_blob(result['output_ref']),b'ok')
        self.assertEqual(self.r.store.all('file')[0]['path'],'output.txt')

    async def test_compact_keeps_original_constraints_and_new_inbox(self):
        run,t=self.create('must keep this exact goal')
        t['status']='running';t['epoch']=1
        for i in range(4):
            self.r.seed_history(t,[
                {'role':'assistant','tool_calls':[{'id':str(i),'type':'function','function':{'name':'workspace_list','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':str(i),'content':'[]'}])
        original=list(self.r.task_history(t));self.r.store.put('task',t)
        async def summarizer(config,messages,tools):
            self.r.deliver(t['id'],'arrived during compact',delivery='inbox')
            return {'role':'assistant','content':dumps({'decisions':[],'pending':[],'evidence_refs':[]})},{}
        self.r.models.adapters['fake']=summarizer
        await self.r.compact(t,1);fresh=self.r.task(t['id'])
        self.assertEqual(self.r.task_history(fresh)[:2],original[:2]);self.assertEqual(fresh['context_epoch'],1)
        self.assertTrue(any(not m['consumed'] for m in self.r.store.all('message')))
        ref=[e for e in self.r.store.events() if e['type']=='ContextCompacted'][0]['payload']['old_ref']
        self.assertEqual(json.loads(self.r.store.read_blob(ref)),original)
        pending=set()
        for m in self.r.task_history(fresh):
            for c in m.get('tool_calls',[]): pending.add(c['id'])
            if m['role']=='tool': self.assertIn(m['tool_call_id'],pending); pending.remove(m['tool_call_id'])
        self.assertFalse(pending)

    async def test_bad_compact_does_not_replace_history(self):
        run,t=self.create();t.update(status='running',epoch=1)
        for i in range(3):
            self.r.seed_history(t,[
                {'role':'assistant','tool_calls':[{'id':'b'+str(i),'type':'function','function':{'name':'workspace_list','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'b'+str(i),'content':'[]'}])
        self.r.store.put('task',t)
        async def bad(*a): return {'content':'{"decisions":[]}'},{}
        self.r.models.adapters['fake']=bad
        with self.assertRaises(ValueError):await self.r.compact(t,1)
        self.assertEqual(self.r.task_history(self.r.task(t['id'])),self.r.task_history(t))

    async def test_human_question_pauses_every_agent_until_answered(self):
        """一个 Agent 向人类提问 = **整个运行**停下，直到这句话被回答。

        为什么不是只停提问的那一个：人还没回答时别的 Agent 继续跑，就是基于"还没定的事"
        往下做，回答一到它们手里的判断全作废——既烧钱又制造返工。
        所以这里断言的是"全体停"，而不是"提问者停"。
        """
        run,t=self.create('离线演示：整体暂停')
        await self.r.start()
        await self.wait_until(lambda:bool(self.r.store.all('question')))
        q=self.r.store.all('question')[0]
        fresh=self.r.run(run['id'])
        self.assertEqual(fresh['status'],'paused')
        self.assertEqual(fresh['pause_reason'],'human_question')
        # 在飞的那一轮必须交回队列，不能卡在 running：卡住的任务恢复后不会被领取。
        await self.wait_until(lambda:not [x for x in self.r.store.all('task') if x['status']=='running'])
        await asyncio.sleep(.3)
        self.assertFalse([x for x in self.r.store.all('task') if x['status']=='running'],
                         '暂停期间不应有任何任务处于 running')
        self.r.answer(q['id'],'整体暂停测试')
        self.assertEqual(self.r.run(run['id'])['status'],'active')
        await self.wait_until(lambda:self.r.run(run['id'])['status']=='completed',15)

    async def test_human_pause_lifts_only_after_the_last_open_question(self):
        """并发提问时，答完一个不算完——还剩 open 就继续停着。"""
        run,t=self.create()
        first=self.r.create_task(run,'第一个提问题的','test',t)
        second=self.r.create_task(run,'第二个提问题的','test',t)
        await self.r.tool_human_ask(first,{'question':'first?'})
        await self.r.tool_human_ask(second,{'question':'second?'})
        questions={q['question']:q for q in self.r.store.all('question')}
        self.r.answer(questions['first?']['id'],'a')
        self.assertEqual(self.r.run(run['id'])['status'],'paused','还有问题没答，不该放行')
        self.r.answer(questions['second?']['id'],'b')
        self.assertEqual(self.r.run(run['id'])['status'],'active')
        self.assertEqual(self.r.run(run['id'])['pause_reason'],'')
        # 两个提问者都要回到队列（唤醒是循环里的事，这里显式跑一次）。
        self.r.wake_waiters()
        self.assertEqual(self.r.task(first['id'])['status'],'queued')
        self.assertEqual(self.r.task(second['id'])['status'],'queued')

    async def test_paused_run_parks_inflight_task_instead_of_killing_it(self):
        """暂停时在飞的任务交回队列：既不判负，也不留在 running。"""
        run,t=self.create()
        self.r.pause_run(run['id'])
        running=self.r.task(t['id']); running['status']='running'; self.r.store.put('task',running)
        await self.r.step(running['id'],running['epoch'])
        parked=self.r.task(t['id'])
        self.assertEqual(parked['status'],'queued')
        self.assertIsNone(parked.get('error'))
        self.assertIn('TaskParked',[e['type'] for e in self.r.store.events(run['id'],limit=1000)])

    async def test_manual_resume_overrides_a_human_pause(self):
        """人工点"继续运行"是显式覆盖：即使问题还没答，也按人的意思跑。"""
        run,t=self.create()
        await self.r.tool_human_ask(t,{'question':'还等吗？'})
        self.assertEqual(self.r.run(run['id'])['status'],'paused')
        self.r.resume_run(run['id'])
        fresh=self.r.run(run['id'])
        self.assertEqual(fresh['status'],'active')
        self.assertEqual(fresh['pause_reason'],'')

    async def test_cancelling_the_asker_releases_the_pause(self):
        """提问题的人被取消后，那个问题永远不会有人回答：别让整个运行陪着停。"""
        run,t=self.create()
        asker=self.r.create_task(run,'提问者','test',t)
        await self.r.tool_human_ask(asker,{'question':'还要吗？'})
        self.assertEqual(self.r.run(run['id'])['status'],'paused')
        self.r.cancel_task(asker['id'])
        self.assertEqual(self.r.run(run['id'])['status'],'active')

    async def test_restart_preserves_wait_and_question(self):
        run,t=self.create();await self.r.tool_human_ask(t,{'question':'persist?'})
        self.r.store.close()
        self.r=Runtime(Path(self.temp.name)/'data');self.r.recover()
        q=self.r.store.all('question')[0];self.r.answer(q['id'],'yes')
        self.assertEqual(self.r.task(t['id'])['status'],'queued')

    async def test_uncertain_tool_not_replayed_after_restart(self):
        run,t=self.create();t['status']='running';self.r.store.put('task',t)
        self.r.store.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?)',('call',t['id'],0,'workspace_shell','{}','running',None))
        self.r.recover();fresh=self.r.task(t['id'])
        self.assertEqual(fresh['status'],'failed');self.assertTrue(fresh['uncertain_calls'])

    async def test_archive_has_versions_and_no_model_credentials(self):
        import tarfile
        run,t=self.create();(self.project/'README.md').write_text('new');self.r.workspace.reconcile(t)
        archive=self.r.workspace.archive(run)
        with tarfile.open(archive['path']) as tar:
            data=json.load(tar.extractfile('archive.json'))
            self.assertEqual(data['run']['goal'],run['goal']);self.assertTrue(data['files'])
            self.assertFalse('models' in data)
            self.assertTrue(any(n.startswith('objects/') for n in tar.getnames()))


    async def test_restore_archive_uses_new_directory(self):
        run,t=self.create();(self.project/'README.md').write_text('archived')
        archive=self.r.workspace.archive(run)
        (self.project/'README.md').write_text('later user edit')
        restored=self.r.workspace.restore_archive(archive['path'])
        self.assertEqual((Path(restored['workspace'])/'README.md').read_text(),'archived')
        self.assertEqual((self.project/'README.md').read_text(),'later user edit')

    async def test_shared_knowledge_work_coalesces(self):
        run,t=self.create()
        a=await self.r.tool_tasks_submit(t,{'goal':'Summarize readme','reuse_key':'readme'})
        b=await self.r.tool_tasks_submit(t,{'goal':'Summarize readme','reuse_key':'readme'})
        self.assertEqual(a['task_id'],b['task_id']);self.assertTrue(b['reused'])
        self.assertEqual(len(self.r.store.all('task')),2)

    async def test_default_playground_directory_exists(self):
        # 控制台用默认项目目录预填新建任务表单，它必须在启动后就可用。
        self.assertTrue((self.r.store.root/'playground').is_dir())

    async def test_parent_may_deliver_while_child_still_runs(self):
        """交付时机是模型的判断：还有未完成子任务不再阻止交付。

        原先 `complete()` 会因为"仍有未完成子任务"抛错。那不是执行不变量，而是替模型决定
        "什么时候算做完"——设计文档 §5 把这件事明确交给主模型。子任务未结束的事实仍然
        留在任务记录里，模型自己看得见。
        """
        run,t=self.create();child=self.r.create_task(run,'child','test',t)
        self.r.complete(t,{'summary':'done'})
        self.assertEqual(self.r.task(t['id'])['status'],'completed')
        # 子任务仍是非终态：交付没有把它的状态一并改掉
        self.assertNotIn(self.r.task(child['id'])['status'], TERMINAL)

    async def test_schema_rejects_invalid_argument_before_file_write(self):
        run,t=self.create();t.update(status='running',epoch=1);self.r.store.put('task',t)
        with self.assertRaises(ValueError): await self.r.execute(t,1,'bad','workspace_write',{'path':'new.txt','content':34})
        self.assertFalse((self.project/'new.txt').exists())

    async def test_wait_already_consumed_completion_does_not_deadlock(self):
        run,t=self.create();child=self.r.create_task(run,'child','test',t)
        self.r.complete(child,{'summary':'done'})
        t['last_call_cursor']=self.r.store.cursor();self.r.store.put('task',t)
        result=await self.r.tool_tasks_yield(t,{'task_ids':[child['id']]})
        self.assertFalse(result['waiting'])
        self.assertNotEqual(self.r.task(t['id'])['status'],'waiting')

    async def test_external_edit_recorded_before_model_overwrites(self):
        run,t=self.create();(self.project/'README.md').write_text('user edit')
        await self.r.tool_workspace_write(t,{'path':'README.md','content':'model edit'})
        history=self.r.store.all('file')
        self.assertEqual(len(history),2)
        self.assertEqual(self.r.store.read_blob(history[0]['after']),b'user edit')
        self.assertEqual(self.r.store.read_blob(history[1]['after']),b'model edit')

    async def test_inbox_over_thirty_continues_before_completion(self):
        run,t=self.create(); t.update(status='running',epoch=1); self.r.store.put('task',t)
        for i in range(35): self.r.deliver(t['id'],f'message {i}')
        calls=[]
        async def adapter(config,messages,tools):
            calls.append(messages[-1]['content'])
            return {'role':'assistant','content':'done'},{}
        self.r.models.adapters['fake']=adapter
        await self.r.step(t['id'],1)
        fresh=self.r.task(t['id'])
        self.assertEqual(fresh['status'],'queued')
        self.assertEqual(len([m for m in self.r.store.all('message') if not m['consumed']]),5)
        fresh.update(status='running',epoch=2); self.r.store.put('task',fresh)
        await self.r.step(t['id'],2)
        self.assertEqual(self.r.task(t['id'])['status'],'completed')
        self.assertEqual(len(calls),2)
        self.assertFalse([m for m in self.r.store.all('message') if not m['consumed']])

    async def test_message_from_subscribed_task_wakes_waiter(self):
        """真机死锁回归（run_64670df8d7e8，静止 54 分钟）。

        主调度 `tasks_yield(task_ids=[C,A,B], topics=[])` 等的是"C/A/B **结束**"，
        而它们在会话进行中永远不会结束；C 把指引发进主调度收件箱（delivery=inbox），
        原实现只按 topics 匹配消息，于是主调度永远醒不过来，整个 run 静止。

        修法：`task_ids` 同时意味着"这些任务发来的消息也算事件"。
        """
        run,root=self.create()
        c,=self.children(run,['C'])
        await self.r.tool_tasks_yield(root,{'task_ids':[c['id']],'max_wait_seconds':300})
        self.assertEqual(self.r.task(root['id'])['status'],'waiting')
        # C 给主调度发指引，用的正是默认的 inbox（真实事故里的那一手）。
        self.r.deliver(root['id'],'我选好了谜底，与导航有关',sender=c['id'],
                       delivery='inbox',topic='',kind='guidance')
        self.r.wake_waiters()
        fresh=self.r.task(root['id'])
        self.assertEqual(fresh['status'],'queued')
        woke=[e for e in self.r.store.events() if e['type']=='TaskWoken']
        self.assertTrue(woke and woke[-1]['payload']['matched'],woke)

    async def test_replay_of_stalled_run_topology_now_progresses(self):
        """用 `run_64670df8d7e8` 里**逐字记录**的那套等待条件复现死锁。

        四个任务的订阅（都是当时真值）：
          root: task_ids=[C,A,B], topics=[], mode=any
          C:    task_ids=[], topics=['player-question','guidance']
          A:    task_ids=[], topics=['task_f3dcf310bffd','game']
          B:    task_ids=[], topics=['指引','猜词','问题','玩家 B']
        C 随后用默认的 delivery=inbox 把指引发给 root。原实现下 root 的状态是永久 waiting。
        """
        run,root=self.create()
        c,=self.children(run,['C'])
        a,b=self.children(run,['A','B'])
        await self.r.tool_tasks_yield(c,{'topics':['player-question','guidance'],'max_wait_seconds':30})
        await self.r.tool_tasks_yield(a,{'topics':[root['id'],'game'],'max_wait_seconds':30})
        await self.r.tool_tasks_yield(b,{'topics':['指引','猜词','问题','玩家 B'],'max_wait_seconds':30})
        # 原样的 root 等待（topics=[]、无时限）现在**当场把边界说清楚**：C/A/B 停在等消息上。
        checked=await self.r.tool_tasks_yield(root,{'task_ids':[c['id'],a['id'],b['id']],'topics':[]})
        self.assertTrue(checked['waiting'])
        self.assertIn('observation',checked)
        self.assertEqual(self.r.task(root['id'])['status'],'waiting')
        # C 的真实那一手：delivery=inbox，topic 空。
        self.r.deliver(root['id'],'我已选定谜底并保密：这是一个便携的常见工具，主要与辨认方向或导航有关。',
                       sender=c['id'],kind='guidance',delivery='inbox')
        self.r.wake_waiters()
        self.assertEqual(self.r.task(root['id'])['status'],'queued')
        # 这次静止从未发生：既没有 RunStalled，也没有任何任务事后才发现问题。
        self.assertFalse([e for e in self.r.store.events() if e['type']=='RunStalled'])

    async def test_declared_max_wait_seconds_actually_wakes(self):
        """模型声明的 max_wait_seconds 必须真的参与唤醒判定。

        这个值此前只被续等逻辑用来自我限制，从未参与唤醒，于是 deadline=null 的等待
        成了无界睡眠。现在它直接决定 max_deadline，到点就叫醒模型自己决定下一步。
        """
        run,t=self.create()
        await self.r.tool_tasks_yield(t,{'task_ids':[],'topics':['never-arrives'],'max_wait_seconds':.01})
        wait=self.r.task(t['id'])['wait']
        self.assertTrue(wait['max_deadline'],wait)
        await asyncio.sleep(.02); self.r.wake_waiters()
        self.assertEqual(self.r.task(t['id'])['status'],'queued')

    async def test_wait_without_any_deadline_has_no_ceiling(self):
        """没声明时限 = 只被订阅的事件唤醒：没有兜底计时器，运行时也不补一个。

        原先运行时会给一个 `DEFAULT_WAIT_CEILING=600` 秒的兜底上限，到点把模型叫起来。
        那是用运行时的钟表安排模型的作息。设计文档 §5.2 的语义是"无事件时不调用模型轮询"，
        所以现在没有事件就不唤醒——想被叫醒就自己声明时限，或订阅会真正发生的事件。
        """
        run,t=self.create()
        result=await self.r.tool_tasks_yield(t,{'topics':['never'],'after_cursor':0})
        wait=self.r.task(t['id'])['wait']
        self.assertTrue(result['waiting'])
        self.assertIsNone(wait['deadline'])
        self.assertIsNone(wait['max_deadline'])
        self.assertNotIn('renewals',wait)
        # 说明里讲清楚边界在哪（是描述，不是拦截）
        self.assertIn('只会被订阅的事件唤醒', result.get('note',''))
        # 事件真的到了照样唤醒
        self.r.deliver(t['id'],'来了',sender=None,topic='never')
        self.r.wake_waiters()
        self.assertEqual(self.r.task(t['id'])['status'],'queued')

    async def test_wait_on_tasks_that_only_wait_for_messages_is_described(self):
        """订阅的任务只在等消息时，把这个事实**说出来**——但只描述、不拦截。

        这是 `run_64670df8d7e8` 的真实形态：主调度等 C/A/B **结束**，而 C/A/B 等的是消息。
        原先这里返回的是 `warning`，措辞是"不会走向终态……你只会靠兜底时限醒来"——
        后半句现在不成立了（兜底时限已移除），而且它是**预测**。
        现在返回 `observation`：只说订阅对象当前的状态与"除消息外没有事件会唤醒你"。
        等待照常登记，状态照常是 waiting。
        """
        run,root=self.create()
        c,=self.children(run,['C'])
        await self.r.tool_tasks_yield(c,{'topics':['x'],'max_wait_seconds':300})
        result=await self.r.tool_tasks_yield(root,{'task_ids':[c['id']]})
        self.assertTrue(result['waiting'])
        self.assertIn('observation',result)
        self.assertNotIn('warning',result)
        self.assertIn('停在', result['observation'])
        self.assertEqual(self.r.task(root['id'])['status'],'waiting')

    async def test_idle_session_is_not_woken_on_a_timer(self):
        """三个任务都停在等待上时，运行时**不再按计时器把谁叫起来**。

        原先有一套"全体空闲"唤醒：订阅的任务都停在等待、整个会话没人跑，
        且空闲超过 `ALL_IDLE_GRACE=45` 秒，就把等待者叫起来一次并投递 `all-idle` 说明。
        那是一条按秒表触发的模型调用——用运行时的钟表安排模型的作息。
        现在没有事件就不唤醒（设计文档 §5.2）；"现在没人该动"这个事实可以从 tasks_list
        查出来，不需要运行时定期插话。仍然保留的只有**拓扑上不可能被满足**的等待说明，
        而且它只是把事实说出来，不据此拒绝登记（见下一条测试）。
        """
        run,root=self.create()
        c,a=self.children(run,['C','A'])
        await self.r.tool_tasks_yield(c,{'topics':['x'],'max_wait_seconds':300})
        await self.r.tool_tasks_yield(a,{'topics':['y'],'max_wait_seconds':300})
        await self.r.tool_tasks_yield(root,{'task_ids':[c['id'],a['id']],'max_wait_seconds':300})
        self.r.wake_waiters()
        self.assertEqual(self.r.task(root['id'])['status'],'waiting')
        # 把注册时间往前挪很久，模拟"静了很久"——从前这会触发叫醒。
        fresh=self.r.task(root['id']); fresh['wait']['registered_at']-=100000; self.r.store.put('task',fresh)
        self.r.wake_waiters(); self.r.wake_waiters()
        self.assertEqual(self.r.task(root['id'])['status'],'waiting')
        self.assertEqual([e for e in self.r.store.events() if e['type']=='AllIdleNotice'],[])
        self.assertEqual([m for m in self.r.store.all('message') if m.get('topic')=='all-idle'],[])
        # 但真正的事件仍然唤醒它：C 发消息给 root。
        self.r.deliver(root['id'],'我这边好了',sender=c['id'],topic='x')
        self.r.wake_waiters()
        self.assertEqual(self.r.task(root['id'])['status'],'queued')

    async def test_cost_note_is_recorded_with_the_child(self):
        """预算判断必须留痕：只记"花了多少"看不出"该不该花"。"""
        run,t=self.create()
        out=await self.r.tool_tasks_submit(t,{'goal':'写一个用例','model_id':'test',
                                              'cost_note':'短任务，用当前档即可，不需要再开重模型'})
        child=self.r.task(out['task_id'])
        self.assertEqual(child['cost_note'],'短任务，用当前档即可，不需要再开重模型')
        events=[e for e in self.r.store.events() if e['type']=='ModelChosenForCost']
        self.assertEqual(len(events),1)
        self.assertIn('短任务',events[0]['payload']['note'])

    async def test_usage_report_exposes_local_compute(self):
        """本地算力与模型调用是同一笔预算的两半：委派前要能看到卡有多忙。"""
        run,t=self.create()
        report=await self.r.tool_usage_report(t,{})
        view=report['local_compute']
        self.assertIn('gpus',view)
        self.assertIn('system_load',view)
        self.assertIn('local_compute',report['note'])

    async def test_every_task_gets_the_full_tool_set(self):
        """不再有"只读/精简"档位：主与子、任何角色拿到同一套工具。

        原先 `tools_submit(tools_profile=lean/readonly)` 会按角色裁掉工具，理由是省每次调用的
        固定 schema 开销。但那与系统提示里"所有模型同等工具能力"直接矛盾，而且"这个角色用不用
        得上某个工具"是模型的判断，不是运行时的。
        """
        run,t=self.create()
        child=self.r.create_task(run,'子任务','test',t)
        parent_names=[s['function']['name'] for s in self.r.schemas_for_task(t)]
        child_names=[s['function']['name'] for s in self.r.schemas_for_task(child)]
        self.assertEqual(parent_names, child_names)
        self.assertIn('context_compact', child_names)
        self.assertIn('workspace_write', child_names)
        self.assertIn('workspace_read', child_names)
        self.assertEqual(len(child_names), len(set(child_names)))

    async def test_complete_rejects_unread_inbox(self):
        run,t=self.create(); self.r.deliver(t['id'],'must process')
        with self.assertRaisesRegex(ValueError,'未处理消息'):
            self.r.complete(t,{'summary':'too early'})

    async def test_plain_text_with_message_arriving_mid_thought_is_not_fatal(self):
        """真机事故回归：收件箱在步骤开始时清空，消息在模型思考期间到达。

        出题人 C 收到 A 的问题后开始思考，思考中 B 的问题又投进收件箱；C 输出单字
        "是"（没有工具调用），旧实现据此判定"要交付"，`complete()` 撞上那条未读消息
        抛 ValueError，C 直接判负——换 C2/C3 替补在同样位置以同样方式再死一次。
        """
        run,root=self.create()
        c=self.r.task(self.r.store.all('task')[0]['id'])
        c.update(status='running',epoch=1,parent_id=root['id']); self.r.store.put('task',c)
        a=root['id']
        arrived=[]
        async def adapter(config,messages,tools):
            # 第一次调用（C 推理中）里投递 B 的问题：此时 C 已经消费过收件箱。
            if not any(m.get('role')=='assistant' for m in messages):
                arrived.append(self.r.deliver(c['id'],'玩家 B 提问：需要电力吗？',sender=a,
                                              delivery='wake',topic='question')['id'])
                return {'role':'assistant','content':'是'},{}
            # 被退回后，模型改用工具把答复发出去。
            return {'role':'assistant','content':None,'tool_calls':[
                {'id':'t1','type':'function','function':{'name':'agents_send',
                 'arguments':dumps({'task_id':a,'summary':'是'})}}]},{}
        self.r.models.adapters['fake']=adapter
        await self.r.step(c['id'],1)
        fresh=self.r.task(c['id'])
        self.assertEqual(fresh['status'],'queued')                      # 没有被判负
        self.assertFalse([e for e in self.r.store.events() if e['type']=='TaskFailed'])
        deferred=[e for e in self.r.store.events() if e['type']=='TextRoundDeferred']
        self.assertEqual(len(deferred),1)
        self.assertIn('未读消息',deferred[0]['payload']['reasons'][0])
        # 提醒本身不能变成一条待答请求（真机 run_8cd95bde07dd 里正是这条通知在索要回执）。
        notices=[m for m in self.r.store.all('message') if m.get('kind')=='control']
        self.assertTrue(notices)
        self.assertTrue(all(m['request_id'] is None and m['status']=='terminal' for m in notices),notices)
        # 收件箱里那条到达于思考期间的消息必须被真正处理。
        fresh.update(status='running',epoch=2); self.r.store.put('task',fresh)
        await self.r.step(c['id'],2)
        # 到达于思考期间的那条消息必须真的被读掉（不是靠剩下的控制提示凑数）。
        self.assertEqual(len(arrived),1)
        self.assertTrue(self.r.store.get('message',arrived[0])['consumed'])
        self.assertTrue(any(m.get('from_task')==c['id'] and m.get('summary')=='是'
                            for m in self.r.store.all('message')),
                        '答复必须真的发给对方，而不是停在纯文本里')
        self.assertFalse([e for e in self.r.store.events() if e['type']=='TaskFailed'])

    async def test_plain_text_does_not_abandon_own_open_request(self):
        """服务型任务：自己发出的请求还没人答时，纯文本不是交付。

        旧行为是抛"仍有未答请求"判负；现在退回队列并告知该用 tasks_yield 等或显式放弃。
        """
        run,root=self.create()
        c=self.r.task(self.r.store.all('task')[0]['id'])
        c.update(status='running',epoch=1,parent_id=root['id']); self.r.store.put('task',c)
        self.r.deliver(root['id'],'问题：防雨吗？',sender=c['id'],topic='question')
        async def adapter(config,messages,tools): return {'role':'assistant','content':'好'},{}
        self.r.models.adapters['fake']=adapter
        await self.r.step(c['id'],1)
        fresh=self.r.task(c['id'])
        self.assertEqual(fresh['status'],'queued')
        self.assertFalse([e for e in self.r.store.events() if e['type']=='TaskFailed'])
        reasons=[e for e in self.r.store.events() if e['type']=='TextRoundDeferred'][0]['payload']['reasons']
        self.assertTrue(any('还没人回答' in r for r in reasons),reasons)

    async def test_repeated_plain_text_is_deferred_every_time_and_never_fails(self):
        """纯文本轮次一直退回队列提醒，**但退回次数不设上限、不判负**。

        原先连续第 3 轮就抛 RuntimeError 把任务判负（`MAX_TEXT_DEFERRALS=2`）。
        那是用计数器代替"这活干完没有"的判断；模型没有犯错，不该被运行时判死。
        现在每轮照常退回并说明待办，次数不封顶。
        """
        run,root=self.create()
        c=self.r.task(self.r.store.all('task')[0]['id'])
        c.update(status='running',epoch=1,parent_id=root['id']); self.r.store.put('task',c)
        async def adapter(config,messages,tools): return {'role':'assistant','content':'是'},{}
        self.r.models.adapters['fake']=adapter
        for epoch in (1,2,3,4,5):
            self.r.deliver(c['id'],f'问题 {epoch}',sender=root['id'],topic='question')
            await self.r.step(c['id'],epoch)
            fresh=self.r.task(c['id'])
            if fresh['status'] not in TERMINAL:
                fresh.update(status='running',epoch=epoch+1); self.r.store.put('task',fresh)
        self.assertEqual([e for e in self.r.store.events() if e['type']=='TaskFailed'],[])
        deferred=[e for e in self.r.store.events() if e['type']=='TextRoundDeferred']
        self.assertGreaterEqual(len(deferred),3)
        self.assertIn('task_id',deferred[0])
        self.assertIn('reasons',deferred[0]['payload'])

    async def test_pause_and_resume_run_are_explicit_idempotent(self):
        run,t=self.create()
        self.assertEqual(self.r.pause_run(run['id'])['status'],'paused')
        self.assertEqual(self.r.pause_run(run['id'])['status'],'paused')
        self.assertEqual(self.r.resume_run(run['id'])['status'],'active')
        self.assertEqual(self.r.resume_run(run['id'])['status'],'active')
        types=[e['type'] for e in self.r.store.events(run['id'])]
        self.assertEqual(types.count('RunPaused'),1); self.assertEqual(types.count('RunResumed'),1)

    async def test_report_has_tristate_omissions_and_impact(self):
        run,t=self.create(); (self.project/'large.bin').write_bytes(b'x'*(16*1024*1024+1))
        self.r.complete(t,{'summary':'done','verification_status':'unknown','impact':'API behavior','unknowns':['load']})
        report=self.r.store.all('report')[0]
        self.assertEqual(report['verification_status'],'unknown')
        self.assertEqual(report['omitted_files'],['large.bin'])
        self.assertEqual(report['impact'],'API behavior')
        self.assertFalse(report['verified_claim'])

    async def test_sqlite_schema_migration_version_and_generation(self):
        version=self.r.store.db.execute('PRAGMA user_version').fetchone()[0]
        self.assertGreaterEqual(version,2)
        run,t=self.create(); child=self.r.create_task(run,'child','test',t)
        (Path(child['workspace'])/'new.txt').write_text('new')
        result=self.r.workspace.integrate(child,t)
        self.assertEqual(result['generation'],1)
        row=self.r.store.db.execute('SELECT generation FROM workspace_generations WHERE workspace=?',(str(self.project.resolve()),)).fetchone()
        self.assertEqual(row['generation'],1)

    async def test_run_inherits_imported_review_model_and_features(self):
        from unison.config import import_codex_config
        import_codex_config(self.r.store,self.r.models,'''
model_provider="OpenAI"
model="test"
review_model="review"
[model_providers.OpenAI]
base_url="https://example.test/v1"
wire_api="responses"
requires_openai_auth=false
[features]
goals=true
''')
        run=self.r.create_run('configured',str(self.project),'test')
        task=self.r.task(run['root_task'])
        self.assertEqual(run['review_model_id'],'OpenAI:review');self.assertTrue(run['features']['goals'])
        initial=self.brief(self.r.task_history(task))
        self.assertEqual(initial['review_model_id'],'OpenAI:review')

    async def test_resolved_conflict_is_closed(self):
        run,t=self.create(); child=self.r.create_task(run,'child','test',t)
        (Path(child['workspace'])/'README.md').write_text('child')
        (self.project/'README.md').write_text('parent')
        conflict=self.r.workspace.integrate(child,t)['conflict']
        (Path(child['workspace'])/'README.md').write_text('parent')
        result=self.r.workspace.integrate(self.r.task(child['id']),self.r.task(t['id']))
        self.assertTrue(result['integrated'])
        self.assertEqual(self.r.store.get('conflict',conflict['id'])['status'],'resolved')

    async def test_integration_failure_rolls_back_and_restart_recovers(self):
        from unittest.mock import patch
        import os
        run,t=self.create(); child=self.r.create_task(run,'child','test',t)
        for name in ['a','b']: (Path(child['workspace'])/name).write_text('new')
        replace=os.replace
        calls=[]
        def fail_second(a,b):
            calls.append(a)
            if len(calls)==2: raise OSError('disk failure')
            return replace(a,b)
        with patch('unison.workspace.os.replace',fail_second):
            with self.assertRaises(OSError): self.r.workspace.integrate(child,t)
        self.assertFalse((self.project/'a').exists())
        self.assertFalse((self.project/'b').exists())
        self.assertEqual(self.r.store.all('integration')[0]['status'],'rolled_back')
        journal={'id':'interrupted','workspace':str(self.project),'status':'pending',
                 'before':{'a':None},'after':{'a':self.r.store.blob('new')},'modes':{'a':420}}
        self.r.store.put('integration',journal); (self.project/'a').write_text('new')
        self.r.recover()
        self.assertFalse((self.project/'a').exists())

    async def test_recovery_preserves_later_user_edit(self):
        run,t=self.create()
        self.r.store.put('integration',{'id':'interrupted','workspace':str(self.project),'status':'pending',
            'before':{'a':None},'after':{'a':self.r.store.blob('new')},'modes':{'a':420}})
        (self.project/'a').write_text('user edit')
        with self.assertRaisesRegex(RuntimeError,'后续修改'): self.r.recover()
        self.assertEqual((self.project/'a').read_text(),'user edit')

    async def test_permissions_and_directory_omissions(self):
        script=self.project/'run.sh';script.write_text('#!/bin/sh\nexit 0\n');script.chmod(0o755)
        (self.project/'node_modules').mkdir()
        run,t=self.create();child=self.r.create_task(run,'child','test',t)
        self.assertEqual((Path(child['workspace'])/'run.sh').stat().st_mode & 0o777,0o755)
        self.assertIn('node_modules/',t['omitted_files'])
        restored=self.r.workspace.restore_archive(self.r.workspace.archive(run)['path'])
        self.assertEqual((Path(restored['workspace'])/'run.sh').stat().st_mode & 0o777,0o755)

    async def test_reintegration_does_not_reapply_adopted_delete(self):
        run,t=self.create();child=self.r.create_task(run,'child','test',t)
        (Path(child['workspace'])/'README.md').unlink()
        self.r.workspace.integrate(child,t)
        (self.project/'README.md').write_text('recreated')
        result=self.r.workspace.integrate(self.r.task(child['id']),self.r.task(t['id']))
        self.assertTrue(result['integrated']);self.assertEqual(result['paths'],[])
        self.assertEqual((self.project/'README.md').read_text(),'recreated')

    async def test_overlapping_run_rejected(self):
        self.create()
        with self.assertRaisesRegex(ValueError,'占用'): self.create()
        nested=self.project/'nested';nested.mkdir()
        with self.assertRaisesRegex(ValueError,'占用'): self.r.create_run('nested',str(nested),'test')

    async def test_shell_execution_evidence_bound_to_snapshot(self):
        run,t=self.create()
        result=await self.r.tool_workspace_shell(t,{'command':'exit 1'})
        record=self.r.store.get('execution',result['execution_ref'])
        self.assertEqual(record['exit_code'],1)
        self.r.complete(self.r.task(t['id']),{'summary':'tests failed','verification_status':'failed'})
        report=self.r.store.all('report')[0]
        self.assertEqual(report['verification_status'],'failed')
        self.assertEqual(report['execution_records'][0]['manifest_ref'],report['manifest_ref'])

    async def test_evidence_is_bound_to_execution_without_grading(self):
        """证据与执行记录绑定，但运行时**不下评级**。

        原先运行时把证据分成 `verified_by_command` / `_weak` / `_inspection` / `unverified`，
        还额外判定"退出码是否可能被 shell 结构吞掉"（heredoc、管道、`sed -i`…）以及
        "证据是不是跑在交付版本上"。这些都是**判断**，而且是用正则和清单摘要替模型做的判断：
        设计文档 §13 说验证方式由 AI 选择，"我跑了测试"与"测试真的过了"的区别应当由模型
        与人依据原始事实判断，而不是运行时发一个等级。

        现在运行时只做一件事：把模型给的证据绑定到真实执行记录，并原样保留
        命令原文、退出码、是否超时。
        """
        run,t=self.create()
        command=("python3 - <<'PY'\nfrom pathlib import Path\n"
                 "assert Path('README.md').read_text()=='original\\n'\nPY")
        result=await self.r.tool_workspace_shell(t,{'command':command})
        self.assertEqual(result['exit_code'],0)
        self.r.complete(self.r.task(t['id']),{'summary':'校验通过','verification_status':'verified',
            'evidence':[{'kind':'command','command':command,'execution_ref':result['execution_ref']}]})
        report=self.r.store.all('report')[0]
        evidence=report['verification_evidence']
        self.assertEqual(evidence['verification_status'],'verified')
        self.assertEqual(evidence['command_executions'],1)
        bound=evidence['evidence'][0]
        self.assertEqual(bound['execution_ref'],result['execution_ref'])
        self.assertEqual(bound['exit_code'],0)
        self.assertEqual(bound['command'],command)
        # 没有评级、没有判词、没有"被掩盖"的推断
        for gone in ('verified','unconfirmed','trustworthy_commands','masked_exit_commands',
                     'stale_commands','changed_after_evidence','note','binding_reasons',
                     'exit_code_masked','fresh','stale_reason'):
            self.assertNotIn(gone,evidence,gone)

    async def test_evidence_without_any_execution_is_still_recorded(self):
        """模型声明了证据、本任务却一条执行记录都没有时：只有 `kind:'claim'` 的条目会被记下。

        运行时不据此判负，也不下判词——它只是没有可绑定的执行记录而已。
        """
        run,t=self.create()
        self.r.complete(self.r.task(t['id']),{'summary':'声称跑过','verification_status':'verified',
            'evidence':[{'kind':'claim','text':'我跑过测试'}]})
        evidence=self.r.store.all('report')[0]['verification_evidence']
        self.assertEqual(evidence['command_executions'],0)
        self.assertEqual(evidence['inspections'],1)
        self.assertNotIn('verified',evidence)

    async def test_later_edits_do_not_change_evidence_grade(self):
        """证据跑完之后又改了文件：运行时不再据此降级，只保留原始事实。"""
        run,t=self.create()
        result=await self.r.tool_workspace_shell(t,{'command':'python3 -c "print(1)"'})
        (self.project/'README.md').write_text('edited after the test\n')
        self.r.workspace.reconcile(self.r.task(t['id']))
        self.r.complete(self.r.task(t['id']),{'summary':'测试通过','verification_status':'verified',
            'evidence':[{'kind':'command','command':'python3 -c "print(1)"','execution_ref':result['execution_ref']}]})
        report=self.r.store.all('report')[0]
        # 文件变更仍然照常记录在 report['files'] 里——事实留着，只是不再换算成等级。
        self.assertTrue(any(f['path'].endswith('README.md') for f in report['files']))
        evidence=report['verification_evidence']
        self.assertEqual(evidence['evidence'][0]['execution_ref'],result['execution_ref'])
        self.assertNotIn('verified',evidence)

    async def test_mode_only_change_integrates_and_is_reported(self):
        run,t=self.create();child=self.r.create_task(run,'child','test',t)
        (Path(child['workspace'])/'README.md').chmod(0o755)
        self.assertTrue(self.r.workspace.integrate(child,t)['integrated'])
        self.assertEqual((self.project/'README.md').stat().st_mode & 0o777,0o755)
        self.assertEqual(self.r.workspace.changes(t)[0]['after_mode'],0o755)

    async def test_completed_run_cannot_resume_into_occupied_workspace(self):
        run,t=self.create();self.r.complete(t,{'summary':'done'})
        self.create('second')
        with self.assertRaisesRegex(ValueError,'占用'): self.r.resume(t['id'])
        with self.assertRaisesRegex(ValueError,'占用'): self.r.revise(run['id'],'changed')

    async def test_database_publication_failure_rolls_back_files(self):
        from unittest.mock import patch
        run,t=self.create();child=self.r.create_task(run,'child','test',t)
        (Path(child['workspace'])/'new').write_text('new')
        event=self.r.store.event
        def fail_commit(kind,*args,**kwargs):
            if kind=='IntegrationCommitted': raise RuntimeError('database fault')
            return event(kind,*args,**kwargs)
        with patch.object(self.r.store,'event',fail_commit):
            with self.assertRaisesRegex(RuntimeError,'database fault'): self.r.workspace.integrate(child,t)
        self.assertFalse((self.project/'new').exists())
        self.assertEqual(self.r.store.all('integration')[0]['status'],'rolled_back')

    async def test_active_compact_takes_effect_in_same_round(self):
        """主动 compact 不再等下一轮：本轮工具批次结束后立即压缩，同一轮继续。"""
        run,t=self.create('compact now')
        for i in range(4):
            self.r.seed_history(t,[
                {'role':'assistant','tool_calls':[{'id':'h'+str(i),'type':'function','function':{'name':'workspace_list','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'h'+str(i),'content':'[]'}])
        stable=list(self.r.task_history(t));self.r.store.put('task',t)
        calls=[]
        async def adapter(config,messages,tools):
            calls.append((len(messages),bool(tools)))
            if not tools:
                return {'role':'assistant','content':dumps({'decisions':[],'pending':[],'evidence_refs':[]})},{}
            if len([c for c in calls if c[1]])==1:
                return {'role':'assistant','content':None,'tool_calls':[
                    {'id':'c1','type':'function','function':{'name':'context_compact','arguments':'{}'}}]},{}
            return {'role':'assistant','content':'done','tool_calls':[
                {'id':'c2','type':'function','function':{'name':'tasks_complete','arguments':dumps({'summary':'ok'})}}]},{}
        self.r.models.adapters['fake']=adapter
        await self.r.start()
        await self.wait_until(lambda:self.r.task(t['id'])['status']=='completed')
        fresh=self.r.task(t['id'])
        self.assertEqual(fresh['context_epoch'],1)
        self.assertFalse(fresh['compact_requested'])
        # 同一个调度轮次内：compact 之后紧接着完成，中间没有重新入队等待下一次调度。
        claimed=[e['seq'] for e in self.r.store.events() if e['type']=='TaskClaimed']
        self.assertEqual(len(claimed),1)
        compacted=[e for e in self.r.store.events() if e['type']=='ContextCompacted']
        self.assertEqual(len(compacted),1)
        self.assertGreater(compacted[0]['seq'],claimed[0])
        self.assertFalse([e for e in self.r.store.events() if e['type']=='TaskQueued'])
        # 被替换的原文可从对象库读回，且前两条消息在压缩前后保持一致。
        referenced=json.loads(self.r.store.read_blob(compacted[0]['payload']['old_ref']))
        self.assertEqual(referenced[:2],stable[:2])
        self.assertEqual(self.r.task_history(fresh)[:2],stable[:2])
        # 第一条是本轮模型调用（含技能目录，因此比旧版多一条 system 消息）；
        # 第二条是压缩用的总结调用（不带工具）；第三条是压缩后在同一轮继续的模型调用。
        self.assertEqual(calls,[(11,True),(2,False),(7,True)])

    async def test_compact_deferred_when_round_ends_in_wait(self):
        """同一轮里先 yield/complete 时不压缩，也不静默改写历史。"""
        run,t=self.create('wait instead')
        async def adapter(config,messages,tools):
            return {'role':'assistant','content':None,'tool_calls':[
                {'id':'c1','type':'function','function':{'name':'context_compact','arguments':'{}'}},
                {'id':'c2','type':'function','function':{'name':'tasks_yield',
                    'arguments':dumps({'topics':['child-task']})}}]},{}
        self.r.models.adapters['fake']=adapter
        await self.r.start()
        await self.wait_until(lambda:self.r.task(t['id'])['status']=='waiting')
        fresh=self.r.task(t['id'])
        self.assertTrue(fresh['compact_requested'])
        self.assertEqual(fresh['context_epoch'],0)
        self.assertFalse([e for e in self.r.store.events() if e['type']=='ContextCompacted'])

    async def test_credentials_visible_to_model_and_shell(self):
        """凭据对 AI 可见：工具可读明文 Key，shell 里以环境变量注入，两者都留事件。"""
        model=self.r.store.get('model','test')
        model['api_key']='sk-test-secret';self.r.store.put('model',model)
        run,t=self.create()
        result=await self.r.tool_models_credentials(t,{})
        self.assertEqual(result['active']['api_key'],'sk-test-secret')
        self.assertEqual(result['active']['model_id'],'test')
        self.assertTrue([e for e in self.r.store.events() if e['type']=='CredentialsExposed'])
        other=self.r.store.get('model','test')|{'id':'second','model':'second'}
        self.r.store.put('model',other)
        both=await self.r.tool_models_credentials(t,{'model_ids':['second']})
        self.assertEqual([x['model_id'] for x in both['models']],['second'])
        shell=await self.r.tool_workspace_shell(t,{'command':'printf "%s|%s|%s" "$UNISON_MODEL_ID" "$UNISON_MODEL_BASE_URL" "$UNISON_MODEL_API_KEY"'})
        self.assertEqual(shell['output'],'test|test://local|sk-test-secret')
        # 凭据不落文件：注入只发生在子进程环境里。
        self.assertEqual(list(Path(t['workspace']).iterdir()),[Path(t['workspace'])/'README.md'])

    async def test_request_header_is_a_pure_function_of_the_log(self):
        """每个请求都留一条信封记录：系统提示词与工具 schema 只存一份，但请求可逐字重建。"""
        run,t=self.create('request header')
        async def adapter(config,messages,tools):
            return {'role':'assistant','content':'done','tool_calls':[
                {'id':'c1','type':'function','function':{'name':'tasks_complete','arguments':dumps({'summary':'ok'})}}]},{}
        self.r.models.adapters['fake']=adapter
        await self.r.start()
        await self.wait_until(lambda:self.r.task(t['id'])['status']=='completed')
        headers=self.r.request_headers(t['id'])
        self.assertEqual(len(headers),1)
        header=headers[0]
        assembled=self.r.assemble_request(header)
        import hashlib
        self.assertEqual(header['prompt_sha256'],hashlib.sha256(assembled['prompt'].encode()).hexdigest())
        self.assertEqual(header['tools_sha256'],hashlib.sha256(dumps(self.r.schemas).encode()).hexdigest())
        self.assertEqual(len(assembled['tools']),len(self.r.schemas))
        self.assertEqual(header['tools_count'],len(self.r.schemas))
        # 记录里的输入与那一次 ModelCalled 事件的 input_ref 一致，可回指同一次请求。
        called=[e for e in self.r.store.events() if e['type']=='ModelCalled'][0]
        self.assertEqual(called['payload']['input_ref'],header['input_ref'])
        self.assertEqual(called['payload']['request_id'],header['id'])
        self.assertTrue(assembled['prompt'].startswith(SYSTEM))
        self.assertIn('<available_skills>',assembled['prompt'])
        self.assertEqual([m['role'] for m in assembled['messages']],[ 'system','system','user'])
        # 相同的提示词与工具 schema 只存一份：再记录一次只新增 input_ref。
        before=set(x.name for x in self.r.store.objects.iterdir())
        extra=self.r.record_request(self.r.task(t['id']))
        after=set(x.name for x in self.r.store.objects.iterdir())
        self.assertEqual(after-before,{extra['input_ref']})
        self.assertIn(header['prompt_ref'],after)
        self.assertEqual(extra['prompt_ref'],header['prompt_ref'])
        self.assertEqual(extra['tools_ref'],header['tools_ref'])
        self.assertEqual(extra['prompt_sha256'],header['prompt_sha256'])
        self.assertEqual(len(self.r.request_headers(t['id'])),2)
        report=self.r.store.all('report')[0]
        self.assertEqual(len(report['request_headers']),1)
        self.assertEqual(report['request_headers'][0]['prompt_sha256'],header['prompt_sha256'])

    async def test_restore_history_reverses_shadowing(self):
        """压缩只遮蔽区间：拿任意一次请求的输入即可还原压缩前的完整历史，含多次压缩。"""
        run,t=self.create('shadowing')
        for i in range(4):
            self.r.seed_history(t,[
                {'role':'assistant','tool_calls':[{'id':'s'+str(i),'type':'function','function':{'name':'workspace_list','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'s'+str(i),'content':'[]'}])
        self.r.store.put('task',t)
        async def summarizer(config,messages,tools):
            return {'role':'assistant','content':dumps({'decisions':['d'],'pending':[],'evidence_refs':[]})},{}
        self.r.models.adapters['fake']=summarizer
        before=list(self.r.task_history(self.r.task(t['id'])))
        await self.r.compact(self.r.task(t['id']),t['epoch'])
        once=self.r.task(t['id'])
        once_h=self.r.task_history(once)
        self.assertEqual(once['context_epoch'],1)
        checkpoint=json.loads(once_h[2]['content'])
        # 遮蔽区间 = [首两条之后, 倒数第二个 assistant)，可以按定义直接算出来。
        starts=[i for i,m in enumerate(before) if m['role']=='assistant']
        self.assertEqual(checkpoint['shadowed_range'],[2,starts[-2]])
        self.assertEqual(checkpoint['history_ref'],self.r.store.blob(dumps(before)))
        # 被遮蔽的内容本身也存了引用，还原不依赖任何下标推算。
        self.assertEqual(json.loads(self.r.store.read_blob(checkpoint['region_ref'])),before[2:starts[-2]])
        # 压缩确实把这段区间换成了 checkpoint，尾部原文保留。
        self.assertEqual(len(once_h),2+1+len(before)-starts[-2])
        self.assertEqual(once_h[3:],before[starts[-2]:])
        # 从「压缩之后」的输入出发也能还原出压缩之前的历史。
        self.assertEqual(self.r.restore_history(self.r.store.blob(dumps(once_h))),before)
        # 第二次压缩，并记录 ground truth：完整原文 = 最初原文 + 之后新增的消息。
        appended=[{'role':'assistant','tool_calls':[{'id':'s9','type':'function','function':{'name':'workspace_list','arguments':'{}'}}]},
                  {'role':'tool','tool_call_id':'s9','content':'[]'}]
        for i in range(4):
            appended += [{'role':'assistant','tool_calls':[{'id':'t'+str(i),'type':'function','function':{'name':'workspace_list','arguments':'{}'}}]},
                         {'role':'tool','tool_call_id':'t'+str(i),'content':'[]'}]
        full=before+appended
        # 第二步的输入 = 第一步的产物 + 新增消息；新增消息同样走日志（不能用直接改缓存）。
        once_history=list(once_h)
        second=once_history+appended
        self.r.seed_history(self.r.task(t['id']),appended,source='test')
        self.r.store.put('task',self.r.task(t['id']))
        await self.r.compact(self.r.task(t['id']),t['epoch'])
        twice=self.r.task(t['id'])
        twice_h=self.r.task_history(twice)
        self.assertEqual(twice['context_epoch'],2)
        ref1=self.r.store.blob(dumps(full))
        ref2=self.r.store.blob(dumps(twice_h))
        # 与实现对照：尾部原文逐字保留，遮蔽区间与定义一致（[2, 倒数第二个 assistant)）。
        starts2=[i for i,m in enumerate(second) if m['role']=='assistant']
        self.assertEqual(twice_h[3:],second[starts2[-2]:])
        self.assertEqual(json.loads(twice_h[2]['content'])['shadowed_range'],[2,starts2[-2]])
        # 还原与压缩互为逆运算：解开一层回到那一步压缩之前的输入。
        self.assertEqual(self.r.restore_history(ref2),second)
        # 派生历史与缓存副本在两次压缩后仍然一致。
        derived,_=self.r.derive_history(twice,self.r.store.events(twice['run_id'],limit=1000000))
        self.assertEqual(derived,twice_h)
        self.assertEqual(self.r.restore_history(ref1),full)
        self.assertEqual(self.r.restore_history(ref2,depth=2),full)
        self.assertEqual(self.r.restore_history(ref2,depth=None),full)
        self.assertGreater(len(full),len(twice_h))
        self.assertEqual(self.r.task_history(self.r.task(t['id']))[:2],before[:2])
        # 事件里保留了每一次压缩的原文引用与遮蔽区间。
        compacted=[e['payload'] for e in self.r.store.events() if e['type']=='ContextCompacted']
        self.assertEqual(len(compacted),2)
        self.assertEqual(compacted[0]['shadowed_range'],[2,starts[-2]])
        self.assertGreater(compacted[1]['shadowed_range'][1],compacted[0]['shadowed_range'][0])
        self.assertEqual(json.loads(self.r.store.read_blob(compacted[1]['old_ref'])),second)
        self.assertEqual([x['messages_before'] for x in compacted],[len(before),len(second)])

    async def test_interrupted_tools_distinguish_not_started_from_unknown(self):
        """崩溃遗留的工具调用要区分"没开始"与"结果未知"，不给出同一句模糊文案。"""
        run,t=self.create('repair')
        for i in range(3):
            self.r.seed_history(t,[
                {'role':'assistant','content':None,'tool_calls':[
                    {'id':'ok'+str(i),'type':'function','function':{'name':'workspace_list','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'ok'+str(i),'content':'[]'}])
        self.r.seed_history(t,[{'role':'assistant','content':None,'tool_calls':[
            {'id':'never','type':'function','function':{'name':'workspace_write','arguments':'{}'}},
            {'id':'unknown','type':'function','function':{'name':'workspace_shell','arguments':'{}'}}]}])
        t['status']='running'
        self.r.store.put('task',t)
        # unknown 已记录但仍在 running；never 连调用行都没有。
        self.r.store.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?)',('unknown',t['id'],1,'workspace_shell','{}','running',None))
        self.r.recover()
        fresh=self.r.task(t['id'])
        self.assertEqual(fresh['status'],'failed')
        repaired=[e['payload']['repaired'] for e in self.r.store.events() if e['type']=='ToolRepairRecorded'][0]
        codes={x['call_id']:x['code'] for x in repaired}
        self.assertEqual(codes,{'never':'TOOL_NOT_STARTED','unknown':'TOOL_OUTCOME_UNKNOWN'})
        text=json.dumps(self.r.task_history(fresh)[-2:],ensure_ascii=False)
        self.assertIn('TOOL_NOT_STARTED',text)
        self.assertIn('TOOL_OUTCOME_UNKNOWN',text)
        self.assertIn('不要盲目重放',text)
        # 已正常配对的调用不会被误判为中断。
        self.assertFalse([x for x in repaired if x['call_id'].startswith('ok')])


    async def test_history_is_derived_from_the_log(self):
        """日志是事实源：清掉缓存副本后，历史仍能逐字从事件重建，且与缓存一致。"""
        run,t=self.create('derive')
        async def adapter(config,messages,tools):
            n=len([m for m in messages if m.get('role')=='tool'])
            if n==0:
                return {'role':'assistant','content':'step1','tool_calls':[
                    {'id':'d1','type':'function','function':{'name':'workspace_list','arguments':'{}'}}]},{}
            if n==1:
                return {'role':'assistant','content':'step2','tool_calls':[
                    {'id':'d2','type':'function','function':{'name':'workspace_write','arguments':dumps({'path':'x.txt','content':'hi'})}}]},{}
            return {'role':'assistant','content':'done','tool_calls':[
                {'id':'d3','type':'function','function':{'name':'tasks_complete','arguments':dumps({'summary':'ok'})}}]},{}
        self.r.models.adapters['fake']=adapter
        await self.r.start()
        await self.wait_until(lambda:self.r.task(t['id'])['status']=='completed')
        fresh=self.r.task(t['id'])
        cached=list(self.r.task_history(fresh))
        self.assertEqual([m['role'] for m in cached],
                         ['system','system','user','assistant','tool','assistant','tool','assistant','tool'])
        derived,linked=self.r.derive_history(fresh,self.r.store.events(fresh['run_id'],limit=1000000))
        self.assertTrue(linked)
        self.assertEqual(derived,cached)
        self.assertFalse([e for e in self.r.store.events() if e['type']=='HistoryCacheMismatch'])
        # 清掉缓存副本后仍能逐字重建（模拟旧库或恢复）。
        stripped=self.r.task(t['id']); stripped.pop('history',None); stripped.pop('history_messages',None); self.r.store.put('task',stripped)
        self.r._derive_cache=None
        self.assertEqual(self.r.task_history(self.r.task(t['id'])),cached)
        header=self.r.record_request(self.r.task(t['id']))
        self.assertEqual(json.loads(self.r.store.read_blob(header['input_ref'])),cached)

    async def test_restart_rebuilds_history_from_the_log(self):
        """重启后模型看到的历史来自日志，而不是可能过期的缓存副本。"""
        run,t=self.create('restart')
        async def once(config,messages,tools):
            return {'role':'assistant','content':'answer','tool_calls':[
                {'id':'r1','type':'function','function':{'name':'tasks_complete','arguments':dumps({'summary':'ok'})}}]},{}
        self.r.models.adapters['fake']=once
        await self.r.start()
        await self.wait_until(lambda:self.r.task(t['id'])['status']=='completed')
        expected=list(self.r.task_history(self.r.task(t['id'])))
        self.r.store.close()
        fresh=Runtime(Path(self.temp.name)/'data',concurrency=1,batch_seconds=.01)
        try:
            task=fresh.task(t['id']); task.pop('history',None); task.pop('history_messages',None); fresh.store.put('task',task)
            fresh._derive_cache=None
            self.assertEqual(fresh.task_history(fresh.task(t['id'])),expected)
        finally:
            fresh.store.close()

    async def test_legacy_task_without_message_log_keeps_cache(self):
        """升级期规则：没有消息日志的历史数据保留缓存副本，不被空派生结果覆盖。"""
        run,t=self.create('legacy')
        # 模拟升级前的历史数据：没有 MessageAppended 事件，只有缓存副本。
        self.r.store.db.execute("DELETE FROM events WHERE task_id=? AND type IN ('MessageAppended','ToolResultRecorded')",(t['id'],))
        task=self.r.task(t['id'])
        task['history']=[{'role':'system','content':'old'},{'role':'user','content':'legacy history'}]
        task.pop('history_messages',None)
        self.r.store.put('task',task)
        self.r._derive_cache=None
        self.assertEqual(self.r.task_history(self.r.task(t['id'])),task['history'])
        # 一旦有消息事件，日志就成为事实源；记录里的历史副本被清除。
        current=self.r.task(t['id']); current.pop('history',None)
        self.r.record_message(current,{'role':'assistant','content':'new'},source='test')
        self.r.store.put('task',current)
        self.r._derive_cache=None
        rebuilt=self.r.task_history(self.r.task(t['id']))
        self.assertEqual(rebuilt,[{'role':'assistant','content':'new'}])
        # 记录里不再保存历史：只有消息数用于轻量校验。
        stored=self.r.store.get('task',t['id'])
        self.assertNotIn('history',stored)
        self.assertEqual(stored['history_messages'],1)
        self.assertFalse([e for e in self.r.store.events() if e['type']=='HistoryDerivationShortfall'])

    async def test_parallel_tool_results_keep_model_order(self):
        """同一条 assistant 消息里的多个工具调用，派生结果要与运行时写的顺序一致。"""
        run,t=self.create('parallel')
        async def adapter(config,messages,tools):
            if not [m for m in messages if m.get('role')=='tool']:
                return {'role':'assistant','content':'batch','tool_calls':[
                    {'id':'p1','type':'function','function':{'name':'workspace_list','arguments':'{}'}},
                    {'id':'p2','type':'function','function':{'name':'workspace_read','arguments':dumps({'path':'README.md'})}}]},{}
            return {'role':'assistant','content':'done','tool_calls':[
                {'id':'p3','type':'function','function':{'name':'tasks_complete','arguments':dumps({'summary':'ok'})}}]},{}
        self.r.models.adapters['fake']=adapter
        await self.r.start()
        await self.wait_until(lambda:self.r.task(t['id'])['status']=='completed')
        fresh=self.r.task(t['id'])
        cached=list(self.r.task_history(fresh))
        derived,linked=self.r.derive_history(fresh,self.r.store.events(fresh['run_id'],limit=1000000))
        self.assertTrue(linked)
        self.assertEqual(derived,cached)
        self.assertEqual([m.get('tool_call_id') for m in cached if m['role']=='tool'],['p1','p2','p3'])

    async def test_task_record_does_not_grow_with_history(self):
        """记录里不再保存历史：任务记录大小与消息数无关，只有消息数用于校验。"""
        run,t=self.create('no growth')
        sizes=[]
        for step in range(6):
            self.r.seed_history(t,[
                {'role':'assistant','content':'step '+str(step),'tool_calls':[
                    {'id':'g'+str(step),'type':'function','function':{'name':'workspace_list','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'g'+str(step),'content':dumps({'data':'x'*400})}])
            self.r.store.put('task',t)
            stored=self.r.store.get('task',t['id'])
            self.assertNotIn('history',stored)
            sizes.append(len(dumps(stored).encode()))
        self.assertEqual([m['role'] for m in self.r.task_history(self.r.task(t['id']))][:2],['system','system'])
        self.assertEqual(len(self.r.task_history(self.r.task(t['id']))),3+12)
        # 历史长了 12 条，任务记录只因为 history_messages 这个整数变化而增长几个字节。
        self.assertLess(max(sizes)-min(sizes),64,'任务记录不应随历史线性增长')

    async def test_collaboration_run_keeps_records_free_of_history(self):
        """一次完整的协作运行（含子任务与集成）后，所有任务记录里都不再有历史副本。"""
        run,t=self.create('离线演示：记录不含历史')
        self.r.models.adapters['fake']=demo_call
        await self.r.start()
        await self.wait_until(lambda:bool(self.r.store.all('question')))
        self.r.answer(self.r.store.all('question')[0]['id'],'Unison')
        await self.wait_until(lambda:self.r.run(run['id'])['status']=='completed',15)
        tasks=self.r.store.all('task')
        self.assertTrue(len(tasks)>=4)
        for task in tasks:
            self.assertNotIn('history',task,'任务记录不应携带历史副本')
            self.assertGreater(task.get('history_messages',0),0)
            self.assertGreaterEqual(len(self.r.task_history(task)),task['history_messages'])


    # ---- 跨模型故障转移：上游异常换模型，而不是把任务判死 ----
    #
    # 判据来自真机 `run_1da778c90c48`：子任务 `task_dd2a34733778` 因网关 503
    # （"提示词安全审计暂时不可用"）在两次退避后判死；而**同一条 run 里**，
    # 另一个模型 18 秒后调用成功。上游不可用不是这个任务的错。

    def failover_provider(self, names, **extra):
        """建一个 provider 与它的模型（都用 fake adapter）。"""
        record={'id':'P','name':'P','base_url':'test://local','api':'openai-completions',
                'adapter':'chat-completions','models':[{'id':n} for n in names]}
        record.update(extra)
        self.r.store.put('provider',record)
        for name in names:
            self.r.store.put('model',{'id':f'P:{name}','model':name,'adapter':'fake','base_url':'test://local',
                                      'no_key':True,'provider_id':'P','context_window':64000,'max_output':4096})
        return [f'P:{name}' for name in names]

    @staticmethod
    def done(summary='ok'):
        return {'role':'assistant','content':'done','tool_calls':[
            {'id':'c1','type':'function','function':{'name':'tasks_complete','arguments':dumps({'summary':summary})}}]},{}

    async def test_upstream_error_switches_model_and_the_task_survives(self):
        """第一个模型 503，候选模型正常 → 任务照常完成，换过谁全程有记录。"""
        self.failover_provider(['bad','good'])
        async def adapter(config,messages,tools):
            if config['model']=='bad':
                raise ModelError('Model HTTP 503: Service Unavailable：提示词安全审计暂时不可用','SERVER')
            return self.done()
        self.r.models.adapters['fake']=adapter
        run=self.r.create_run('failover',str(self.project),'P:bad')
        t=self.r.task(run['root_task'])
        await self.r.start()
        await self.wait_until(lambda:self.r.task(t['id'])['status']=='completed')
        fresh=self.r.task(t['id'])
        self.assertEqual(fresh['model_id'],'P:good')
        self.assertEqual(fresh['model_failover_from'],'P:bad')
        self.assertEqual(fresh['model_failover_chain'],['P:bad'])
        events=[e for e in self.r.store.events(fresh['run_id']) if e['type']=='ModelFailover']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['payload']['from'],'P:bad')
        self.assertEqual(events[0]['payload']['to'],'P:good')
        self.assertEqual(events[0]['payload']['code'],'SERVER')
        # 换成功之后**继续用新模型**：不再每一轮都先交一次注定失败的调用。
        self.assertEqual([c['payload']['model'] for c in self.r.store.events(fresh['run_id'])
                          if c['type']=='ModelCalled'], ['P:bad','P:good'])

    async def test_exhausted_candidates_still_fail_with_the_whole_chain(self):
        """候选全部失败时仍然判 failed（本轮的选择），但错误里要看得到换过谁。"""
        self.failover_provider(['bad','also-bad'])
        async def adapter(config,messages,tools):
            raise ModelError(f"Model HTTP 503: {config['model']} 不可用",'SERVER')
        self.r.models.adapters['fake']=adapter
        run=self.r.create_run('failover exhausted',str(self.project),'P:bad')
        t=self.r.task(run['root_task'])
        await self.r.start()
        await self.wait_until(lambda:self.r.task(t['id'])['status']=='failed')
        error=self.r.task(t['id'])['error']
        self.assertIn('SERVER',error)
        self.assertIn('已尝试故障转移',error)
        self.assertIn('P:bad',error)
        self.assertIn('P:also-bad',error)

    async def test_non_model_side_errors_do_not_switch_models(self):
        """凭据/参数类问题换模型没用，不能拿它当遮羞布去掩盖配置错误。"""
        self.failover_provider(['bad','good'])
        async def adapter(config,messages,tools):
            raise ModelError('模型调用失败：Bad Request','INVALID_REQUEST')
        self.r.models.adapters['fake']=adapter
        run=self.r.create_run('no failover',str(self.project),'P:bad')
        t=self.r.task(run['root_task'])
        await self.r.start()
        await self.wait_until(lambda:self.r.task(t['id'])['status']=='failed')
        self.assertEqual(self.r.task(t['id'])['model_id'],'P:bad')
        self.assertFalse([e for e in self.r.store.events(t['run_id']) if e['type']=='ModelFailover'])


if __name__ == '__main__':
    unittest.main()

"""归档 = 结案。

这里钉住的是"归档到底做了什么"，因为最初那版只做到了"再存一份"：`Workspace.archive()`
把事件、报告、知识、文件版本与内容对象打进 tar，然后**什么都不释放**——磁盘一点没变，
`.unison/workspaces/` 下的子任务副本一直堆着，会话也照旧混在活动列表里。

现在归档是五件事：门禁（只在已完成 / 已停止 / 主调度模型报错时）、停掉该 run 的全部 AI
与它们派生的进程、落一份可恢复的冷备、释放子任务工作区副本、并**保留文件修改记录**。
最后一条尤其要钉死：释放副本会让"目录不存在"看起来像"文件被删除"，而那批删除从未发生。
"""
import asyncio
import contextlib
import os
import tempfile
import unittest
from pathlib import Path

from unison.runtime import Runtime
from unison.store import now


class ArchiveRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / 'project'; self.project.mkdir()
        (self.project / 'README.md').write_text('original\n')
        self.r = Runtime(Path(self.temp.name) / 'data', concurrency=1, batch_seconds=.01)
        self.r.models.adapters['fake'] = lambda config, messages, tools: self.fail('不该调用模型')
        self.r.store.put('model', {'id': 'test', 'model': 'test', 'adapter': 'fake',
                                   'base_url': 'test://local', 'no_key': True,
                                   'context_window': 64000, 'max_output': 4096})

    async def asyncTearDown(self):
        await self.r.stop(); self.temp.cleanup()

    def create(self, goal='test goal'):
        run = self.r.create_run(goal, str(self.project), 'test')
        return run, self.r.task(run['root_task'])

    def child_with_copy(self, run, root, name='child task'):
        """建一个子任务并把它的工作区副本真的铺到磁盘上（`setup` 平时由调度循环做）。"""
        child = self.r.create_task(run, name, 'test', root)
        self.r.workspace.setup(child, root)
        self.r.store.put('task', child)
        return self.r.task(child['id'])

    # ---------------------------------------------------------------- 门禁

    async def test_active_run_cannot_be_archived(self):
        """运行还在跑：归档会停掉它并释放副本，所以必须先停下来。"""
        run, _ = self.create()
        self.assertFalse(self.r.run_archivable(self.r.run(run['id']))[0])
        with self.assertRaises(ValueError) as caught:
            self.r.archive_run(run['id'])
        self.assertIn('还在进行中', str(caught.exception))
        self.assertNotIn('archived', self.r.run(run['id']))

    async def test_completed_and_cancelled_runs_can_be_archived(self):
        for status in ('completed', 'cancelled'):
            with self.subTest(status=status):
                run, _ = self.create(goal=f'{status} run')
                fresh = self.r.run(run['id']); fresh['status'] = status; self.r.store.put('run', fresh)
                self.assertTrue(self.r.run_archivable(self.r.run(run['id']))[0])
                result = self.r.archive_run(run['id'])
                self.assertTrue(result['archive_path'].endswith('.tar.gz'))
                archived = self.r.run(run['id'])
                self.assertIn('archived', archived)
                self.assertEqual(archived['archive_path'], result['archive_path'])

    async def test_root_model_failure_is_archivable_even_while_run_status_is_active(self):
        """主调度模型报错：根任务 failed 之后不会再有新调度，这本身就是一个终态。"""
        run, root = self.create()
        self.r.fail(root, '模型调用失败（SERVER）：Model HTTP 502')
        self.assertEqual(self.r.run(run['id'])['status'], 'active')
        self.assertTrue(self.r.run_archivable(self.r.run(run['id']))[0])
        self.r.archive_run(run['id'])
        self.assertIn('archived', self.r.run(run['id']))

    async def test_archiving_twice_is_refused(self):
        run, _ = self.create()
        fresh = self.r.run(run['id']); fresh['status'] = 'completed'; self.r.store.put('run', fresh)
        self.r.archive_run(run['id'])
        with self.assertRaises(ValueError) as caught:
            self.r.archive_run(run['id'])
        self.assertIn('已经归档', str(caught.exception))

    # ------------------------------------------------------- 停止 AI 与进程

    async def test_archive_cancels_every_non_terminal_task_of_the_run(self):
        run, root = self.create()
        waiting = self.r.create_task(run, 'waiter', 'test', root)
        waiting['status'] = 'waiting'; self.r.store.put('task', waiting)
        done = self.r.create_task(run, 'done', 'test', root)
        done['status'] = 'completed'; self.r.store.put('task', done)
        fresh = self.r.run(run['id']); fresh['status'] = 'cancelled'; self.r.store.put('run', fresh)

        result = self.r.archive_run(run['id'])

        # 根任务此时还是 queued，也属于"这个 run 的 AI"，一并停掉。
        self.assertEqual(sorted(result['stopped']), sorted([root['id'], waiting['id']]))
        self.assertEqual(self.r.task(waiting['id'])['status'], 'cancelled')
        self.assertEqual(self.r.task(done['id'])['status'], 'completed')  # 终态不动

    async def test_archive_kills_the_process_group_a_task_spawned(self):
        """归档会停掉 AI **以及它们派生的进程**：留一个 30 秒的 shell 挂在那里就是没结案。"""
        run, root = self.create()
        child = self.child_with_copy(run, root)
        pending = asyncio.create_task(self.r.tool_workspace_shell(child, {'command': 'sleep 30'}))
        try:
            for _ in range(60):
                await asyncio.sleep(.05)
                if child['id'] in self.r.processes: break
            self.assertIn(child['id'], self.r.processes, 'shell 进程没有起来')
            pid = self.r.processes[child['id']].pid
            os.kill(pid, 0)  # 还活着

            fresh = self.r.run(run['id']); fresh['status'] = 'completed'; self.r.store.put('run', fresh)
            self.r.archive_run(run['id'])

            # 工具协程收尾后拿到退出码：-9 就是被 SIGKILL 掉的进程组，不是"自己跑完了"。
            outcome = await asyncio.wait_for(pending, timeout=10)
            self.assertNotIn(child['id'], self.r.processes)
            self.assertEqual(outcome['exit_code'], -9)
            await asyncio.sleep(.2)   # 让事件循环回收子进程，否则 os.kill 看到的是僵尸
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending

    # --------------------------------------------------------- 释放与保留

    async def test_archive_releases_child_workspace_copies(self):
        run, root = self.create()
        child = self.child_with_copy(run, root)
        copy = Path(child['workspace'])
        (copy / 'draft.md').write_text('half-finished\n')
        self.assertTrue(copy.is_dir())

        fresh = self.r.run(run['id']); fresh['status'] = 'completed'; self.r.store.put('run', fresh)
        result = self.r.archive_run(run['id'])

        self.assertFalse(copy.exists())
        self.assertEqual([item['task_id'] for item in result['released']], [child['id']])
        self.assertGreater(result['freed_bytes'], 0)
        self.assertIn('workspace_released', self.r.task(child['id']))

    async def test_archive_never_touches_the_project_directory(self):
        """根任务直接操作用户的项目目录：归档可以删除副本，绝不能删用户的项目。"""
        run, root = self.create()
        (self.project / 'keep.txt').write_text('mine\n')
        fresh = self.r.run(run['id']); fresh['status'] = 'completed'; self.r.store.put('run', fresh)

        result = self.r.archive_run(run['id'])

        self.assertTrue((self.project / 'README.md').is_file())
        self.assertTrue((self.project / 'keep.txt').is_file())
        self.assertEqual(result['released'], [])

    async def test_released_copy_never_recorded_as_file_deletions(self):
        """释放副本之后，任何一次对账都不得把那批"消失"写成删除记录——那些删除没发生过。"""
        run, root = self.create()
        child = self.child_with_copy(run, root)
        copy = Path(child['workspace'])
        (copy / 'draft.md').write_text('half-finished\n')
        recorded = len(self.r.workspace.reconcile(self.r.task(child['id'])))
        self.assertGreater(recorded, 0)
        before = [x for x in self.r.store.all('file') if x['task_id'] == child['id']]

        fresh = self.r.run(run['id']); fresh['status'] = 'completed'; self.r.store.put('run', fresh)
        self.r.archive_run(run['id'])

        # 副本没了，但再对账一次不会新增任何"文件被删除"的记录。
        self.assertEqual(self.r.workspace.reconcile(self.r.task(child['id'])), [])
        after = [x for x in self.r.store.all('file') if x['task_id'] == child['id']]
        self.assertEqual(len(before), len(after))
        self.assertFalse(any(x['after'] is None for x in after))

    async def test_file_modification_records_survive_the_release(self):
        """归档释放磁盘，但"谁在什么时候把哪个文件改成了什么"必须留着，且内容可读。"""
        run, root = self.create()
        child = self.child_with_copy(run, root)
        copy = Path(child['workspace'])
        (copy / 'draft.md').write_text('half-finished\n')
        self.r.workspace.reconcile(self.r.task(child['id']))
        record = next(x for x in self.r.store.all('file')
                      if x['task_id'] == child['id'] and x['path'] == 'draft.md')

        fresh = self.r.run(run['id']); fresh['status'] = 'completed'; self.r.store.put('run', fresh)
        result = self.r.archive_run(run['id'])

        self.assertEqual(result['files_kept'], len([x for x in self.r.store.all('file') if x['run_id'] == run['id']]))
        kept = self.r.store.get('file', record['id'])
        self.assertEqual(kept['path'], 'draft.md')
        self.assertEqual(kept['actor'], 'external/unknown')
        self.assertIsNotNone(kept['after'])
        self.assertEqual(self.r.store.read_blob(kept['after']), b'half-finished\n')

    async def test_released_run_can_still_be_restored_from_its_archive(self):
        """先留退路再释放：副本删掉之后，归档仍然能解回一份完整的工作区。"""
        run, root = self.create()
        child = self.child_with_copy(run, root)
        (Path(child['workspace']) / 'draft.md').write_text('half-finished\n')

        fresh = self.r.run(run['id']); fresh['status'] = 'completed'; self.r.store.put('run', fresh)
        result = self.r.archive_run(run['id'])

        restored = self.r.workspace.restore_archive(result['archive_path'], task_id=child['id'])
        self.assertEqual(Path(restored['workspace']).joinpath('draft.md').read_text(), 'half-finished\n')

    # ----------------------------------------------------------- 归档即结案

    async def test_archived_run_refuses_to_continue(self):
        run, root = self.create()
        child = self.r.create_task(run, 'later', 'test', root)
        self.r.store.put('task', child)
        fresh = self.r.run(run['id']); fresh['status'] = 'completed'; self.r.store.put('run', fresh)
        self.r.archive_run(run['id'])

        with self.assertRaises(ValueError):
            self.r.revise(run['id'], '换个目标')
        with self.assertRaises(ValueError):
            self.r.resume_run(run['id'])
        with self.assertRaises(ValueError):
            self.r.resume(root['id'])
        with self.assertRaises(ValueError):
            self.r.create_task(self.r.run(run['id']), '再派一个', 'test', root)

    async def test_archived_run_is_not_woken_by_a_late_message(self):
        """迟到消息会把已完成的任务重新排队、把 run 拉回 active——归档后这条路必须封死。"""
        run, root = self.create()
        fresh = self.r.run(run['id']); fresh['status'] = 'completed'; self.r.store.put('run', fresh)
        self.r.archive_run(run['id'])
        with self.assertRaises(ValueError):
            self.r.deliver(root['id'], '顺便再改一下', delivery='wake', kind='user')

    async def test_archived_run_is_skipped_by_the_workspace_scan(self):
        """归档即结案：副本已释放、记录已冻结，2 秒一轮的文件对账不该再碰它。"""
        run, root = self.create()
        fresh = self.r.run(run['id']); fresh['status'] = 'completed'; self.r.store.put('run', fresh)
        self.r.archive_run(run['id'])
        job = next(x for x in self.r.maintenance.jobs if x.id == 'workspace-scan')
        summary = await job.run(self.r.store)
        self.assertEqual(summary['counts']['workspaces'], 0)

    async def test_run_archived_event_records_what_was_released(self):
        run, root = self.create()
        child = self.child_with_copy(run, root)
        fresh = self.r.run(run['id']); fresh['status'] = 'completed'; self.r.store.put('run', fresh)
        result = self.r.archive_run(run['id'])

        event = next(e for e in self.r.store.events(run_id=run['id']) if e['type'] == 'RunArchived')
        self.assertEqual(event['payload']['released'], [child['id']])
        self.assertEqual(event['payload']['freed_bytes'], result['freed_bytes'])
        self.assertEqual(event['payload']['archive'], result['archive_path'])
        self.assertIn('files_kept', event['payload'])


if __name__ == '__main__':
    unittest.main()

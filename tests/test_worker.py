"""PR-3 判据（二）：干活端——领活、在独立目录里干、把结论落账并送回飞书。

用假的工作台（``Workbench`` 换成一个返回固定结果的替身）测"账本与工作目录"这一层；
真的跑 ``dsh headless`` 是收尾真机验收的事。

**这一版的核心判据：不假设机器上有 git**（主人 2026-11-19 定：同事机没有 git）。
所以夹具里**故意不建 git 仓库**——如果哪天代码又悄悄依赖 git，这些用例会直接红。
"""

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from bridge import worker as worker_module
from bridge.store import Store
from bridge.workbench import ARTIFACT_LIMIT, Handoff, WorkResult


class FakeWorkbench:
    """替身：不真跑 dsh，只按剧本返回结果，同时记录"被传进来的 cwd"。"""

    def __init__(self, result=None, error=None, writes=None, artifacts=None):
        self.result = result
        self.error = error
        #: 模拟"模型在工作目录里写了东西"：{相对路径: 内容}
        self.writes = dict(writes or {})
        self.artifacts = artifacts
        self.calls = []

    def prepare(self):
        if self.error:
            raise self.error

    def run(self, prompt, *, task_id, requirement, session_id=None, stop=None):
        self.calls.append({'prompt': prompt, 'task_id': task_id, 'cwd': Path(self.cwd),
                           'session_id': session_id})
        for name, content in self.writes.items():
            target = Path(self.cwd) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
        if self.result is not None:
            return self.result
        listed = self.artifacts
        if listed is None:
            listed = [{'path': name, 'bytes': len(content.encode('utf-8'))}
                      for name, content in sorted(self.writes.items())]
        return WorkResult(ok=True, kind='done', text='干完了', session_id='sess-1',
                          duration_seconds=1.5, output_bytes=42, artifacts=list(listed),
                          handoff=None, work_dir=str(self.cwd))


def handoff_for(kind, failure):
    return Handoff(task_id='t', requirement='把表汇总', last_step='调用工具',
                   progress_lines=['读需求'], artifacts=[], workspace='/wt',
                   space='/wt', session_id='sess-1', resume_command='dsh headless',
                   failure=failure)


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        # 刻意**不**建 git 仓库：干活端不许假设机器上有 git。
        self.store = Store(self.home / 'state.sqlite3')
        self.settings = worker_module.worker_config(
            {'worker': {'max_concurrency': 1}}, home=self.home, consumer='dsh-worker')
        self.fake = FakeWorkbench(writes={'out/表.csv': 'a,b\n1,2\n'})
        self.worker = worker_module.Worker(self.store, self.settings,
                                          workbench_factory=self._factory)
        self.worker.register()
        self.now = time.time()

    def _factory(self, space):
        self.fake.cwd = space
        return self.fake

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    # -- 夹具 ---------------------------------------------------------------
    def approved_task(self, card=None, origin_text='把表汇总'):
        self.store.connection.execute(
            "INSERT INTO inbox(id,state,received_at,chat_id,text,raw_json)"
            " VALUES ('om_origin','done',?,'oc_group',?,'{}')", (self.now, origin_text))
        task = self.store.draft_task('oc_group', '把表汇总', self.now, origin_mid='om_origin',
                                     message_id='om_origin', card=card)
        self.store.approve_task(task['id'], approval_mid='om_card', approval_kind='quoted',
                                hints_text='做', now=self.now)
        return task['id']

    def claim_work(self):
        self.worker.register()
        return self.store.claim_events('dsh-worker', time.time(), kind='work')

    # -- 1 干完、产物、账本 --------------------------------------------------
    def test_tick_runs_the_work_in_its_own_space_and_marks_the_task_done(self):
        task_id = self.approved_task()
        summary = self.worker.tick()
        self.assertEqual(summary['result'], 'done')
        task = self.store.get_task(task_id)
        self.assertEqual(task['state'], 'done')
        self.assertEqual(task['executor'], 'dsh-web@' + worker_module.machine_name())
        space = self.home / 'work' / task_id / 'space'
        self.assertEqual(self.fake.calls[0]['cwd'], space,
                         '活必须在自己的独立目录里跑，不是别处')
        self.assertTrue((space / 'out' / '表.csv').is_file())
        self.assertEqual(task['artifacts'],
                         [{'path': 'out/表.csv', 'bytes': len('a,b\n1,2\n'.encode())}])
        events = self.store.connection.execute(
            "SELECT state FROM events WHERE kind='work'").fetchall()
        self.assertEqual([row['state'] for row in events], ['done'], '结论落账后事件要 ack 掉')

    def test_the_space_directory_is_private(self):
        task_id = self.approved_task()
        self.worker.tick()
        mode = stat.S_IMODE((self.home / 'work' / task_id / 'space').stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_a_run_without_artifacts_is_done_and_says_so(self):
        self.fake.writes = {}
        task_id = self.approved_task()
        summary = self.worker.tick()
        self.assertEqual(summary['result'], 'done')
        task = self.store.get_task(task_id)
        self.assertEqual(task['state'], 'done')
        self.assertEqual(task['artifacts'], [])
        self.assertIn('没有产出文件', task['steps'][-1]['note'])

    def test_artifacts_are_relative_paths_with_sizes(self):
        self.fake.writes = {'报告.md': '一二三', 'data/a.csv': 'x' * 2048}
        task_id = self.approved_task()
        self.worker.tick()
        listed = self.store.get_task(task_id)['artifacts']
        paths = [item['path'] for item in listed]
        self.assertIn('报告.md', paths)
        self.assertIn('data/a.csv', paths)
        sizes = {item['path']: item['bytes'] for item in listed}
        self.assertEqual(sizes['data/a.csv'], 2048)

    def test_a_truncated_artifact_list_is_done_and_says_it_is_truncated(self):
        self.fake.writes = {}
        self.fake.artifacts = [{'path': 'f%d.txt' % i, 'bytes': 1}
                               for i in range(ARTIFACT_LIMIT)]
        self.fake.result = WorkResult(ok=True, kind='done', text='干完了', session_id='s',
                                      duration_seconds=1.0, output_bytes=1,
                                      artifacts=self.fake.artifacts,
                                      artifacts_truncated=True, handoff=None,
                                      work_dir=str(self.home))
        task_id = self.approved_task()
        self.assertEqual(self.worker.tick()['result'], 'done')
        note = self.store.get_task(task_id)['steps'][-1]['note']
        self.assertIn('只列前', note)
        self.assertNotIn('没有产出文件', note)

    # -- 2 失败与拦截 --------------------------------------------------------
    def test_lock_conflict_is_blocked_with_a_handoff(self):
        self.fake.result = WorkResult(
            ok=False, kind='lock_conflict', text='那个会话你正开着',
            session_id='sess-1', duration_seconds=3.0, output_bytes=10, artifacts=[],
            handoff=handoff_for('lock_conflict', '会话被占用'), work_dir='/tmp/w')
        task_id = self.approved_task()
        summary = self.worker.tick()
        self.assertEqual(summary['result'], 'blocked')
        task = self.store.get_task(task_id)
        self.assertEqual(task['state'], 'blocked')
        self.assertTrue(task['open_question'], '被拦住时要留下一个问题，人能直接回「执行」')
        self.assertIsNotNone(task['origin_mid'])

    def test_timeout_is_failed_not_blocked(self):
        self.fake.result = WorkResult(
            ok=False, kind='timeout', text='超时了', session_id='sess-1',
            duration_seconds=1800.0, output_bytes=10, artifacts=[],
            handoff=handoff_for('timeout', '超时了'), work_dir='/tmp/w')
        task_id = self.approved_task()
        self.assertEqual(self.worker.tick()['result'], 'failed')
        self.assertEqual(self.store.get_task(task_id)['state'], 'failed')

    def test_a_space_that_cannot_be_created_blocks_instead_of_crashing(self):
        task_id = self.approved_task()
        # 让这件活的工作目录位置被一个普通文件占住：准备目录必然失败，但 home/work 本身正常。
        blocker = self.home / 'work'
        blocker.mkdir(parents=True, exist_ok=True)
        (blocker / task_id).write_text('occupied', encoding='utf-8')
        summary = self.worker.tick()
        self.assertEqual(summary['result'], 'blocked')
        self.assertEqual(summary.get('reason'), 'workdir')
        self.assertEqual(self.store.get_task(task_id)['state'], 'blocked')

    def test_no_work_means_skip(self):
        self.assertEqual(self.worker.tick(), {'skipped': 'no_work'})

    # -- 3 任务包 ------------------------------------------------------------
    def test_the_prompt_carries_requirement_hints_and_the_space(self):
        task_id = self.approved_task(card='【活】把表汇总\n我打算：① 拉数据')
        self.worker.tick()
        prompt = self.fake.calls[0]['prompt']
        self.assertIn('把表汇总', prompt)
        self.assertIn('做', prompt, '主人批卡时说的话要带上')
        self.assertIn(str(self.home / 'work' / task_id / 'space'), prompt)

    def test_the_prompt_forbids_git_and_probing(self):
        self.approved_task()
        self.worker.tick()
        prompt = self.fake.calls[0]['prompt']
        self.assertIn('不要执行任何 git 命令', prompt)
        self.assertIn('不要探测环境', prompt)
        self.assertIn('产物', prompt)

    # -- 4 自检：不依赖 git ---------------------------------------------------
    def test_prepare_creates_the_work_root_and_does_not_need_git(self):
        self.assertFalse((self.home / 'work').exists())
        self.worker.prepare()
        self.assertTrue((self.home / 'work').is_dir())
        self.assertFalse((self.root / '.git').exists())
        self.assertFalse((self.home / '.git').exists())

    def test_prepare_fails_loudly_when_the_work_root_cannot_be_created(self):
        blocker = self.home / 'work'
        blocker.write_text('not a directory', encoding='utf-8')
        with self.assertRaises(RuntimeError) as caught:
            self.worker.prepare()
        self.assertIn('工作目录根', str(caught.exception))

    # -- 5 补发：干完但没发出去的，重领要补发而不是重跑 ----------------------
    def test_a_saved_result_is_resent_without_running_again(self):
        task_id = self.approved_task()
        self.worker.tick()
        self.assertEqual(len(self.fake.calls), 1)
        # 模拟"结论已落盘（result-a1.json）、但 Lead 在落账/ack 之前崩了"：
        # 任务仍是 running，事件被放回 pending。重领时必须补发，而不是再干一遍。
        self.store.connection.execute("UPDATE tasks SET state='running' WHERE id=?", (task_id,))
        self.store.connection.execute(
            "UPDATE events SET state='pending', consumer=NULL, claim_token=NULL, lease_until=NULL"
            " WHERE kind='work'")
        self.store.connection.commit()
        summary = self.worker.tick()
        self.assertTrue(summary.get('recovered'))
        self.assertEqual(len(self.fake.calls), 1, '补发不许再跑一遍')
        self.assertEqual(self.store.get_task(task_id)['state'], 'done')


class NoGitDependencyTests(unittest.TestCase):
    """代码层判据：干活端与工作台里**不许有 git 调用**（同事机上没有 git）。"""

    def test_worker_and_workbench_never_shell_out_to_git(self):
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for name in ('bridge/worker.py', 'bridge/workbench.py'):
            text = (root / name).read_text(encoding='utf-8')
            for needle in ("['git'", '"git"', 'git worktree', 'git commit', 'git status',
                           'commit_worktree', 'worktree_root'):
                if needle in text:
                    offenders.append('%s → %s' % (name, needle))
        self.assertEqual([], offenders, '干活端不假设 git 存在：%s' % offenders)


if __name__ == '__main__':
    unittest.main()

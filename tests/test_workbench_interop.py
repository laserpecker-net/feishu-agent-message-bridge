"""`cmd-interop`（Windows 侧工作台）的判据（`bridge/workbench.py`）。

本机是 mac，跑不了真的 `cmd.exe`，所以这里验的是**构造出来要执行的东西对不对**：
argv（列表传参、Windows 形态、空格与中文）、环境变量（DSH_HOME）、进程树收尾
（先 taskkill 再 killpg）、`launch` 与 `kind` 的搭配、以及 `workbench check` 的行。

**direct 的行为不变**由既有的 `tests/test_workbench.py`、`tests/test_dsh_consumer.py` 守着
（那一套在 mac 上真跑假 CLI，全绿就说明直连路径没被改坏）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import workbench  # noqa: E402


class InteropTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / 'dsh-home'
        self.home.mkdir()
        self.workspace = root / 'space'
        self.workspace.mkdir()
        self.env = mock.patch.dict(os.environ, {'DSH_HOME': str(self.home),
                                                'DEEPSEEK_API_KEY': 'test-key-value'})
        self.env.start()
        self.addCleanup(self.env.stop)
        # 本机是 mac，没有 /mnt/c；这一组用例验的是 argv/环境/进程树，
        # "Windows 可见性"由 VisibilityTests 单独验（那里显式造 True/False）。
        visible = mock.patch('bridge.platform_paths.is_windows_visible', return_value=True)
        visible.start()
        self.addCleanup(visible.stop)

    def bench(self, **overrides):
        block = {'kind': 'dsh-desktop', 'launch': 'cmd-interop',
                 'cli': r'D:\Program Files\DeepSeek\resources\runtime\cli\bin\dsh.cmd',
                 'home': str(self.home), 'workspace': str(self.workspace),
                 'timeout_seconds': 30}
        block.update(overrides.pop('block', {}))
        return workbench.Workbench({'workbench': block}, cwd=str(self.workspace), **overrides)

    # -- argv 构造 ----------------------------------------------------------
    def test_argv_uses_cmd_exe_and_windows_forms(self):
        bench = self.bench()
        argv = bench._cmd_interop_argv('/mnt/c/work/tsk_1/consumer-overlay.yml')
        # 形状：cmd.exe /c <cli> <arg> …（每个参数各占一个元素）
        self.assertEqual(argv[:2], ['cmd.exe', '/c'])
        self.assertEqual(argv[2], r'D:\Program Files\DeepSeek\resources\runtime\cli\bin\dsh.cmd')
        self.assertEqual(argv[3:6], ['headless', '--patch', r'C:\work\tsk_1\consumer-overlay.yml'])
        self.assertEqual(argv[-2:], ['--json', '-'])
        # 真机实测：手写引号会让 cmd 把那串引号当成程序名（不是内部或外部命令）
        for element in argv[2:]:
            self.assertFalse(element.startswith('"'), element)

    def test_argv_keeps_chinese_paths_intact(self):
        bench = self.bench()
        argv = bench._cmd_interop_argv('/mnt/d/我的 项目/overlay.yml')
        # 中文与空格都原样留在**同一个元素**里（引号交给 WSL 互操作加）
        self.assertIn(r'D:\我的 项目\overlay.yml', argv)

    def test_cmd_metacharacters_are_left_alone(self):
        """`&` `^` `(` `)` 不做任何转义：互操作按元素加引号，我们自己加反而坏事（真机实测）。"""
        argv = workbench.interop_command('C:\\x\\a&b.cmd', ['--patch', 'C:\\x\\p^(1)^.yml'])
        self.assertEqual(argv, ['cmd.exe', '/c', 'C:\\x\\a&b.cmd', '--patch', 'C:\\x\\p^(1)^.yml'])
        for element in argv[2:]:
            self.assertNotIn('"', element)

    def test_args_with_spaces_stay_single_elements(self):
        argv = workbench.interop_command(r'D:\Program Files\x\dsh.cmd', ['--say', 'a b'])
        self.assertEqual(argv[2], r'D:\Program Files\x\dsh.cmd')
        self.assertEqual(argv[-1], 'a b')
        self.assertEqual(len(argv), 5, argv)

    def test_interop_command_is_the_only_shape(self):
        """三处调用点必须产出同一种形状：cmd.exe + /c + 单参数命令行。"""
        argv = workbench.taskkill_argv(99)
        self.assertEqual(argv, ['cmd.exe', '/c', 'taskkill', '/T', '/F', '/PID', '99'])

    def test_dir_mode_makes_no_git_assumptions(self):
        # 每个活一个目录：overlay 落在 work/<task>/ 下，cwd 就是那个目录；与 git 无关。
        bench = self.bench()
        argv = bench._cmd_interop_argv('/mnt/c/work/tsk_1/consumer-overlay.yml')
        joined = ' '.join(argv)
        for word in ('worktree', 'git', 'commit'):
            self.assertNotIn(word, joined)

    # -- prepare()：真机踩过的 POSIX 假设 -----------------------------------
    def _map_c_to(self, root):
        """把 `C:\\…` 映射进临时目录，让 home/workspace 的判据能被真实验证。"""
        root = Path(root)

        def win_to_wsl(path, **_kwargs):
            text = str(path)
            if len(text) >= 2 and text[1] == ':':
                rel = text[2:].replace('\\', '/').strip('/')
                return str(root / rel) if rel else str(root)
            raise ValueError('不是 Windows 形态：%s' % text)

        def wsl_to_win(path, **_kwargs):
            text = str(path)
            try:
                rel = Path(text).relative_to(root)
            except ValueError:
                if text.startswith('/mnt/c/'):
                    rel = Path(text[len('/mnt/c/'):])
                else:
                    return text
            win = str(rel).replace('/', '\\')
            return 'C:\\' + win if win else 'C:\\'

        return mock.patch.multiple('bridge.platform_paths',
                                   win_to_wsl=win_to_wsl, wsl_to_win=wsl_to_win)

    def _ok_probe(self, stdout=b'0.2.2-rc.4\n', returncode=0, stderr=b''):
        done = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
        return mock.patch('subprocess.run', return_value=done)

    def test_prepare_probes_through_cmd_exe_instead_of_execing_the_cmd_file(self):
        # 真机：直接 exec `/mnt/c/…/dsh.cmd` → [Errno 8] Exec format error。
        (Path(self.temp.name) / 'Test' / 'home').mkdir(parents=True)
        (Path(self.temp.name) / 'Test' / 'space').mkdir(parents=True)
        with self._map_c_to(self.temp.name), self._ok_probe() as run, \
                mock.patch.object(workbench.Workbench, '_binary', return_value='/mnt/c/x/dsh.cmd'), \
                mock.patch.dict(os.environ, {'DSH_HOME': r'C:\Test\home'}):
            bench = self.bench(block={'home': r'C:\Test\home', 'workspace': r'C:\Test\space'})
            bench.prepare()
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], 'cmd.exe')
        self.assertEqual(argv[1], '/c')
        # 真机结论：每个参数各占一个元素；手写引号会让 cmd 报「不是内部或外部命令」。
        self.assertTrue(argv[2].endswith('dsh.cmd'), argv[2])
        self.assertEqual(argv[3:], ['--version'])
        self.assertEqual('0.2.2-rc.4', bench.cli_version)

    def test_prepare_checks_home_and_workspace_through_the_mapped_path(self):
        (Path(self.temp.name) / 'Test' / 'home').mkdir(parents=True)
        (Path(self.temp.name) / 'Test' / 'space').mkdir(parents=True)
        with self._map_c_to(self.temp.name), self._ok_probe(), \
                mock.patch.object(workbench.Workbench, '_binary', return_value='/mnt/c/x/dsh.cmd'), \
                mock.patch.dict(os.environ, {'DSH_HOME': r'C:\Test\home'}):
            bench = self.bench(block={'home': r'C:\Test\home', 'workspace': r'C:\Test\space'})
            bench.prepare()   # 不抛就是通过
            missing = self.bench(block={'home': r'C:\Test\nope', 'workspace': r'C:\Test\space'})
            with self.assertRaises(workbench.WorkbenchError) as caught:
                with mock.patch.dict(os.environ, {'DSH_HOME': r'C:\Test\nope'}):
                    missing.prepare()
        self.assertIn('home 不存在', str(caught.exception))
        self.assertIn('C:\\Test\\nope', str(caught.exception))

    def test_prepare_treats_trailing_slash_home_as_the_same_place(self):
        (Path(self.temp.name) / 'Test' / 'home').mkdir(parents=True)
        (Path(self.temp.name) / 'Test' / 'space').mkdir(parents=True)
        with self._map_c_to(self.temp.name), self._ok_probe(), \
                mock.patch.object(workbench.Workbench, '_binary', return_value='/mnt/c/x/dsh.cmd'), \
                mock.patch.dict(os.environ, {'DSH_HOME': 'C:\\Test\\home\\'}):
            bench = self.bench(block={'home': r'C:\Test\home', 'workspace': r'C:\Test\space'})
            bench.prepare()

    def test_prepare_rejects_a_truly_different_dsh_home(self):
        (Path(self.temp.name) / 'Test' / 'home').mkdir(parents=True)
        (Path(self.temp.name) / 'Test' / 'space').mkdir(parents=True)
        with self._map_c_to(self.temp.name), self._ok_probe(), \
                mock.patch.object(workbench.Workbench, '_binary', return_value='/mnt/c/x/dsh.cmd'), \
                mock.patch.dict(os.environ, {'DSH_HOME': r'C:\Other\.dsh'}):
            bench = self.bench(block={'home': r'C:\Test\home', 'workspace': r'C:\Test\space'})
            with self.assertRaises(workbench.WorkbenchError) as caught:
                bench.prepare()
        self.assertIn('不一致', str(caught.exception))

    def test_prepare_reports_the_first_stderr_line_when_the_probe_fails(self):
        (Path(self.temp.name) / 'Test' / 'home').mkdir(parents=True)
        (Path(self.temp.name) / 'Test' / 'space').mkdir(parents=True)
        with self._map_c_to(self.temp.name), \
                self._ok_probe(returncode=2, stderr=b'boom: cannot start\nsecond line\n'), \
                mock.patch.object(workbench.Workbench, '_binary', return_value='/mnt/c/x/dsh.cmd'), \
                mock.patch.dict(os.environ, {'DSH_HOME': r'C:\Test\home'}):
            bench = self.bench(block={'home': r'C:\Test\home', 'workspace': r'C:\Test\space'})
            with self.assertRaises(workbench.WorkbenchError) as caught:
                bench.prepare()
        self.assertEqual('cli_unavailable', caught.exception.kind)
        self.assertIn('boom: cannot start', str(caught.exception))
        self.assertNotIn('second line', str(caught.exception))

    # -- 环境 ---------------------------------------------------------------
    def test_env_overrides_set_windows_dsh_home(self):
        bench = self.bench(block={'home': r'C:\Work\Test User\.dsh'})
        overrides = bench._env_overrides()
        self.assertEqual(r'C:\Work\Test User\.dsh', overrides['DSH_HOME'])

    def test_posix_home_is_refused_under_interop(self):
        # 把 POSIX 路径当 DSH_HOME 传给 Windows 子进程，会变成"会话写进没人看得见的地方"。
        bench = self.bench()
        with self.assertRaises(workbench.WorkbenchError) as caught:
            bench._env_overrides()
        self.assertIn('Windows 形态', str(caught.exception))

    def test_direct_mode_has_no_env_overrides(self):
        bench = workbench.Workbench({'workbench': {'launch': 'direct', 'home': str(self.home),
                                                   'workspace': str(self.workspace)}},
                                    cwd=str(self.workspace))
        self.assertEqual({}, bench._env_overrides())

    # -- 进程树收尾 ---------------------------------------------------------
    def test_taskkill_argv_targets_the_tree_forcefully(self):
        argv = workbench.taskkill_argv(4321)
        self.assertEqual(argv, ['cmd.exe', '/c', 'taskkill', '/T', '/F', '/PID', '4321'])

    def test_interop_kill_runs_taskkill_then_kills_the_group(self):
        bench = self.bench()
        process = mock.Mock(pid=4321)
        with mock.patch.object(workbench.subprocess, 'run') as run, \
                mock.patch.object(workbench, 'kill_group') as group:
            workbench.kill_interop(process)
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], workbench.taskkill_argv(4321))
        group.assert_called_once_with(process)

    def test_interop_kill_still_reaps_when_taskkill_fails(self):
        process = mock.Mock(pid=99)
        with mock.patch.object(workbench.subprocess, 'run', side_effect=OSError('no cmd')), \
                mock.patch.object(workbench, 'kill_group') as group:
            workbench.kill_interop(process)
        group.assert_called_once_with(process)

    def test_direct_mode_uses_kill_group_only(self):
        bench = workbench.Workbench({'workbench': {'launch': 'direct', 'home': str(self.home),
                                                   'workspace': str(self.workspace)}},
                                    cwd=str(self.workspace))
        self.assertIs(bench._kill_for(), workbench.kill_group)
        self.assertIs(self.bench()._kill_for(), workbench.kill_interop)

    # -- 搭配与自检 ---------------------------------------------------------
    def test_unknown_launch_is_rejected(self):
        with self.assertRaises(workbench.WorkbenchError) as caught:
            self.bench(block={'launch': 'teleport'}).prepare()
        self.assertIn('workbench.launch', str(caught.exception))

    def test_desktop_requires_interop(self):
        with self.assertRaises(workbench.WorkbenchError) as caught:
            self.bench(block={'launch': 'direct'}).prepare()
        self.assertIn('cmd-interop', str(caught.exception))

    def test_interop_requires_desktop_kind(self):
        with self.assertRaises(workbench.WorkbenchError) as caught:
            self.bench(block={'kind': 'dsh-web'}).prepare()
        self.assertIn('dsh-desktop', str(caught.exception))

    def test_missing_windows_cli_reports_cli_unavailable(self):
        bench = self.bench(block={'cli': r'D:\nowhere\dsh.cmd'})
        with self.assertRaises(workbench.WorkbenchError) as caught:
            bench.prepare()
        self.assertEqual('cli_unavailable', caught.exception.kind)

    def test_describe_prints_the_same_rows_on_both_platforms(self):
        rows = self.bench().describe()
        names = [name for name, _value, _ok in rows]
        self.assertEqual(['role', 'launch', 'kind', 'cli', 'cli_version', 'home', 'home_ok',
                          'workspace', 'workspace_ok', 'work_root', 'mode',
                          'same_as_desktop'], names)
        values = dict((name, value) for name, value, _ok in rows)
        self.assertEqual('cmd-interop', values['launch'])
        self.assertEqual('dir', values['mode'])
        # cli 里的家目录要打码（check 输出会被贴进聊天）
        self.assertNotIn(str(Path.home()), values['cli'])

    def test_describe_shows_windows_home_verbatim_and_masks_the_mac_one(self):
        # interop：home 本来就是 Windows 形态（C:\Users\…），原样显示；
        # direct：mac 的家目录要打码成 ~/…（check 输出会被贴进聊天）。
        interop = dict((name, value) for name, value, _ok in
                       self.bench(block={'home': r'C:\Work\Test User\.dsh'}).describe())
        self.assertEqual(r'C:\Work\Test User\.dsh', interop['home'])
        direct = workbench.Workbench({'workbench': {'launch': 'direct', 'home': str(self.home),
                                                    'workspace': str(self.workspace)}},
                                     cwd=str(self.workspace))
        rows = dict((name, value) for name, value, _ok in direct.describe())
        self.assertNotIn(str(Path.home()), rows['home'])


class RunDirTests(unittest.TestCase):
    """运行现场（overlay / stdout / 交接单）落在哪。

    真机 2026-11-20：某件活派下去 15 秒就死，报「不是 WSL 挂载路径」——因为 overlay 写进了
    ext4 的 bridge home，而 interop 下这个路径要交给 `cmd.exe`，`wsl_to_win` 映射不了。
    interop 的运行目录必须落在 Windows 可见盘上；direct（mac）布局逐字不变。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'home').mkdir()
        (self.root / 'space').mkdir()
        self.env = mock.patch.dict(os.environ, {'DSH_HOME': str(self.root / 'home'),
                                                'DEEPSEEK_API_KEY': 'test-key-value'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _bench(self, *, launch='cmd-interop', space_root='/mnt/c/Work/demo/feishu-work',
               cwd='/mnt/c/Work/demo/feishu-work/tsk_1/space', visible=True):
        block = {'kind': 'dsh-desktop' if launch == 'cmd-interop' else 'dsh-web',
                 'launch': launch,
                 'cli': r'D:\Program Files\DeepSeek\resources\runtime\cli\bin\dsh.cmd',
                 'home': str(self.root / 'home'), 'workspace': str(self.root / 'space')}
        patcher = mock.patch('bridge.platform_paths.is_windows_visible', return_value=visible)
        patcher.start()
        self.addCleanup(patcher.stop)
        return workbench.Workbench({'workbench': block}, cwd=cwd, space_root=space_root)

    def test_interop_run_dir_lives_on_the_windows_visible_root(self):
        bench = self._bench()
        self.assertEqual(str(bench._run_dir('tsk_1')), '/mnt/c/Work/demo/feishu-work/tsk_1/run')

    def test_interop_overlay_handed_to_windows_is_a_windows_path(self):
        bench = self._bench()
        overlay = Path('/mnt/c/Work/demo/feishu-work/tsk_1/run/consumer-overlay.yml')
        argv = bench._cmd_interop_argv(overlay)
        self.assertIn(r'C:\Work\demo\feishu-work\tsk_1\run\consumer-overlay.yml', argv)

    def test_interop_prepare_refuses_when_no_visible_run_root(self):
        bench = self._bench(visible=False)
        with self.assertRaises(workbench.WorkbenchError) as caught:
            bench._run_root()
        message = str(caught.exception)
        self.assertIn('/mnt/', message)
        self.assertIn('worker.work_root', message)
        self.assertEqual(caught.exception.kind, 'cli_unavailable')

    def test_interop_overlay_on_ext4_is_refused_with_actionable_hint(self):
        bench = self._bench()
        bad = Path(str(self.root / 'home' / 'work' / 't1' / 'consumer-overlay.yml'))
        with self.assertRaises(workbench.WorkbenchError) as caught:
            bench._windows_target(bad, 'overlay 路径')
        self.assertIn('Windows 能看到的位置', str(caught.exception))
        self.assertIn('overlay 路径', str(caught.exception))

    def test_direct_run_dir_layout_is_unchanged(self):
        bench = self._bench(launch='direct', space_root=None, cwd=str(self.root / 'space'))
        bench.work_root = self.root / 'bridge-home'
        self.assertEqual(str(bench._run_dir('tsk_1')),
                         str(self.root / 'bridge-home' / 'work' / 'tsk_1'))

class LockConflictMarkerTests(unittest.TestCase):
    def test_marker_is_shared_with_the_mac_path(self):
        # 同一句话、同一判据：两端不许各认各的
        self.assertEqual(workbench.LOCK_CONFLICT_MARKER,
                         'already owned by an active write handle')


class VisibilityTests(unittest.TestCase):
    """interop 的 cwd 必须在 Windows 能看到的位置（真机 2026-11-20：cmd 拒绝 UNC）。

    这一组**不复用** InteropTests 的"可见性=True"补丁：要真判 is_windows_visible。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'home').mkdir()
        (root / 'space').mkdir()
        self.root = root
        self.env = mock.patch.dict(os.environ, {'DSH_HOME': str(root / 'home'),
                                                'DEEPSEEK_API_KEY': 'test-key-value'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _bench(self, *, cwd, launch='cmd-interop'):
        block = {'kind': 'dsh-desktop' if launch == 'cmd-interop' else 'dsh-web',
                 'launch': launch,
                 'cli': r'D:\Program Files\DeepSeek\resources\runtime\cli\bin\dsh.cmd',
                 'home': str(self.root / 'home'), 'workspace': str(self.root / 'space')}
        return workbench.Workbench({'workbench': block}, cwd=cwd)

    def test_space_on_wsl_ext4_is_refused_with_actionable_hint(self):
        # WSL 的 ext4 目录（这里用临时目录模拟）在 Windows 看来是 UNC → 必须明确报错。
        bench = self._bench(cwd=str(self.root / 'space'))
        with mock.patch('bridge.platform_paths.is_windows_visible', return_value=False):
            with self.assertRaises(workbench.WorkbenchError) as caught:
                bench._interop_cwd()
        message = str(caught.exception)
        self.assertIn('Windows 能看到的位置', message)
        self.assertIn('worker.work_root', message)
        self.assertEqual(caught.exception.kind, 'cli_unavailable')

    def test_probe_cwd_prefers_the_space_and_falls_back_to_the_visible_parent(self):
        bench = self._bench(cwd='/mnt/c/work/demo/work/t1/space')
        # space 本身可见 → 直接用 space
        with mock.patch('bridge.platform_paths.is_windows_visible', return_value=True):
            self.assertEqual(str(bench._probe_cwd()), '/mnt/c/work/demo/work/t1/space')
        # space 不可见、home 的上一级可见（C:\Users\<用户>）→ 退到那里
        bench.cwd = Path('/home/demo/work/t1/space')
        bench.home = Path('/mnt/c/work/demo/.dsh')
        with mock.patch('bridge.platform_paths.is_windows_visible',
                        side_effect=lambda p: str(p).startswith('/mnt/c/work')):
            self.assertEqual(str(bench._probe_cwd()), '/mnt/c/work/demo')

    def test_describe_has_a_work_root_row_under_interop(self):
        bench = self._bench(cwd='/mnt/c/work/demo/work/t1/space')
        bench.space_root = Path('/mnt/c/work/demo/work')
        bench.cli_version = '0.2.2-rc.4'
        rows = dict((row[0], row) for row in bench.describe())
        self.assertIn('work_root', rows)
        self.assertIn('work_root', dict((row[0], row) for row in bench.describe()))
        self.assertTrue(rows['work_root'][2], '可见的 work_root 应当 PASS')

    def test_direct_launch_is_unaffected(self):
        path = Path(self.root) / 'space'
        bench = self._bench(cwd=str(path), launch='direct')
        bench.cli = None
        with mock.patch('shutil.which', return_value='/usr/local/bin/dsh'), \
                mock.patch.object(workbench.Workbench, '_probe_version', return_value='0.2.2-rc.4'):
            bench.prepare()
        self.assertEqual(bench._probe_cwd(), None)


class WorkerWorkRootTests(unittest.TestCase):
    """``worker.work_root`` 可配：Windows 上必须指到 /mnt/c/…，默认保持 /work。"""

    def test_default_work_root_is_home_slash_work(self):
        from bridge import worker
        settings = worker.worker_config({'worker': {}}, home='/tmp/bridge-home')
        self.assertEqual(str(settings['work_root']), '/tmp/bridge-home/work')

    def test_work_root_is_configurable(self):
        from bridge import worker
        settings = worker.worker_config({'worker': {'work_root': '/mnt/c/work/demo/feishu-work'}},
                                        home='/tmp/bridge-home')
        self.assertEqual(str(settings['work_root']), '/mnt/c/work/demo/feishu-work')

    def test_space_lives_under_the_configured_root(self):
        from bridge import worker
        self.assertEqual(str(worker.space_path('/mnt/c/work/demo/feishu-work', 'tsk_1')),
                         '/mnt/c/work/demo/feishu-work/tsk_1/space')


if __name__ == '__main__':
    unittest.main()

class SessionCwdComparisonTests(unittest.TestCase):
    """child 报的 cwd 与期望的 cwd：同一个目录的两种写法必须被认成同一个。

    真机现场（2026-11-20，Windows 开发机）：活干完了、文件也写出来了，却因为
    child 报 `C:\\Users\\…\\space`、我们期望 `/mnt/c/Users/…/space` 而被判
    `DSH session workspace mismatch`。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'dsh-home').mkdir()
        (root / 'space').mkdir()
        self.env = mock.patch.dict(os.environ, {'DSH_HOME': str(root / 'dsh-home')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = root

    def _workbench(self, cwd_str):
        return workbench.Workbench({'workbench': {'kind': 'dsh-desktop',
                                                  'launch': 'cmd-interop',
                                                  'home': 'C:\\Users\\example\\.dsh',
                                                  'cli': 'C:\\Users\\example\\dsh.cmd'}},
                                   cwd=cwd_str, work_root='/mnt/c/work')

    def test_windows_form_and_wsl_form_are_the_same_directory(self):
        wb = self._workbench('/mnt/c/Users/example/space')
        same = wb.same_cwd_checker()
        self.assertTrue(same(r'C:\Users\example\space'))
        self.assertTrue(same('C:/Users/example/space'))
        self.assertTrue(same('c:\\users\\example\\space\\'))   # 大小写 + 尾斜杠
        self.assertFalse(same(r'C:\Users\example\other'))
        self.assertFalse(same(''))
        self.assertFalse(same(None))

    def test_unmappable_child_cwd_counts_as_mismatch(self):
        wb = self._workbench('/mnt/c/Users/example/space')
        same = wb.same_cwd_checker()
        self.assertFalse(same(r'\\server\share\space'))   # UNC

    def test_direct_mode_keeps_strict_string_comparison(self):
        wb = workbench.Workbench({'workbench': {'kind': 'dsh-web', 'launch': 'direct'}},
                                 cwd=str(self.root / 'space'))
        self.assertIsNone(wb.same_cwd_checker())

    def test_session_event_uses_the_injected_comparator(self):
        events = workbench.RunEvents('/mnt/c/Users/example/space',
                                     same_cwd=lambda child: child == r'C:\Users\example\space')
        events.feed(json.dumps({'type': 'session', 'sessionId': 's1',
                                'cwd': r'C:\Users\example\space'}))
        self.assertEqual('s1', events.session_id)

    def test_mismatch_error_carries_both_raw_values(self):
        events = workbench.RunEvents('/mnt/c/Users/example/space',
                                     same_cwd=lambda child: False)
        with self.assertRaises(RuntimeError) as caught:
            events.feed(json.dumps({'type': 'session', 'sessionId': 's1',
                                    'cwd': r'C:\Users\example\space'}))
        message = str(caught.exception)
        self.assertIn(r'C:\Users\example\space', message)
        self.assertIn('/mnt/c/Users/example/space', message)

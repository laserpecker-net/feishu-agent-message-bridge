"""干活端适配器（`bridge/workbench.py`）的判据。

假 CLI 按 `dsh headless --json` 的**已实测事件形状**回放，覆盖：正常完成、无 final、
超时/取消、锁冲突、非法事件、空答案、CLI 不存在、凭证缺失、输出超限、overlay 契约、
`prepare()` 的各种坏配置，以及最要紧的一条——**任何非 done 的结局都必须交出完整交接包**
（主人 2026-11-18：杀死以后不能仅仅杀死就完毕，要能接得上）。
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import workbench  # noqa: E402


REPO = Path(__file__).resolve().parents[1]
CONSUMER = REPO / 'dsh-consumer.py'
SECRET = 'sk-' + 'b' * 24


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.dsh_home = root / 'dsh-home'
        self.dsh_home.mkdir()
        self.workspace = root / 'workspace'
        self.workspace.mkdir()
        self.cli = root / 'dsh'
        self.snapshot = root / 'overlay-snapshot'
        self.env = mock.patch.dict(os.environ, {
            'DEEPSEEK_API_KEY': 'test-key-value',
            'DSH_HOME': str(self.dsh_home),
            'WB_SNAPSHOT': str(self.snapshot),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ('FEISHU_CONSUMER_WORKSPACE', 'DSH_PERMISSION_MODE'):
            os.environ.pop(name, None)

    # -- 假 CLI -------------------------------------------------------------
    def fake_cli(self, code):
        # `--version` 是 prepare() 的自检探针：先答掉它，再回放本次要测的事件流。
        preamble = ('import json,os,signal,subprocess,sys,time\n'
                    'if "--version" in sys.argv:\n'
                    '    print("fake-dsh 0.0.0")\n'
                    '    sys.exit(0)\n')
        self.cli.write_text('#!' + sys.executable + '\n' + preamble + code)
        self.cli.chmod(0o700)

    def session(self, cwd=None):
        return ('print(json.dumps({"type":"session","sessionId":"session-wb",'
                '"cwd":%s}),flush=True)\n' % (repr(cwd) if cwd else 'os.getcwd()'))

    def emit(self, *events):
        return ''.join('print(json.dumps(%r),flush=True)\n' % event for event in events)

    def success(self, answer='{"answer":"做完了"}', extra=''):
        return (self.session() + extra
                + self.emit({'type': 'text', 'text': '中间过程'})
                + self.emit({'type': 'status', 'phase': 'step_start', 'text': '读文件'})
                + 'print(json.dumps({"type":"status","phase":"turn_end",'
                  '"reason":{"kind":"completed"}}),flush=True)\n'
                + 'print(json.dumps({"type":"final","text":' + repr(answer) + '}),flush=True)\n')

    def bench(self, **overrides):
        config = {'workbench': {'cli': str(self.cli), 'home': str(self.dsh_home),
                                'workspace': str(self.workspace),
                                'timeout_seconds': overrides.pop('timeout_seconds', 30),
                                'max_output_bytes': overrides.pop('max_output_bytes',
                                                                  workbench.DEFAULT_MAX_OUTPUT_BYTES)}}
        return workbench.Workbench(config, cwd=str(overrides.pop('cwd', self.workspace)), **overrides)

    def run_work(self, **kwargs):
        return self.bench(**{k: v for k, v in kwargs.items()
                             if k in ('timeout', 'timeout_seconds', 'cwd', 'max_output_bytes')}).run(
            '照着做', task_id=kwargs.get('task_id', 'tsk_wb'), requirement='群里那条需求',
            stop=kwargs.get('stop'))

    def handoff_files(self, task_id='tsk_wb'):
        directory = self.dsh_home / 'work' / task_id
        return directory / 'handoff.md', directory / 'handoff.json'

    # -- Policy violation（2026-11-19 真机：干活端跑 `pytest > log` 被判违规）------
    def test_truncating_redirect_is_a_policy_violation_for_the_replier(self):
        # 回话端没有工作树、删了就没了：单个 `>` 截断照旧拦，并单独命名成一类失败。
        self.fake_cli(self.session() + self.emit(
            {'type': 'tool_call', 'callId': 'c1', 'tool': 'bash',
             'input': {'command': 'pytest -q > /tmp/x.log'}}))
        result = self.run_work()
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, 'policy_violation')
        self.assertIn('被沙箱规则拦了', result.text)
        self.assertIn('pytest -q > /tmp/x.log', result.text, '要能看出是哪次调用被拦')
        self.assertIsNotNone(result.handoff)

    def test_the_worker_may_redirect_output_but_still_not_delete(self):
        # 干活端放行的只有"截断型重定向"这一条规则；破坏性命令照旧拦。
        self.fake_cli(self.session() + self.emit(
            {'type': 'tool_call', 'callId': 'c1', 'tool': 'bash',
             'input': {'command': 'rm -rf docs'}}))
        bench = self.bench(allow_truncating_redirect=True)
        result = bench.run('照着做', task_id='tsk_wb2', requirement='群里那条需求')
        self.assertEqual(result.kind, 'policy_violation')
        self.assertIn('rm -rf docs', result.text)

    def test_worker_redirect_passes_the_safety_gate(self):
        # 同一条命令：回话端拦、干活端放行——差别只有"这一条规则"的开关。
        import consumer_safety
        command = 'pytest -q > /tmp/x.log'
        self.assertTrue(consumer_safety.blocked_command(command))
        self.assertFalse(consumer_safety.blocked_command(
            command, allow_truncating_redirect=True))

    # -- 1 正常完成 ---------------------------------------------------------
    def test_success_returns_done_and_answer(self):
        self.fake_cli(self.success())
        result = self.run_work()
        self.assertTrue(result.ok, result.kind)
        self.assertEqual(result.kind, 'done')
        self.assertEqual(result.text, '{"answer":"做完了"}')
        self.assertEqual(result.session_id, 'session-wb')
        self.assertGreater(result.duration_seconds, 0)
        self.assertIsNone(result.handoff, 'done 不需要交接包')
        self.assertFalse(self.handoff_files()[0].exists())

    # -- 2 没有 final -------------------------------------------------------
    def test_plain_text_final_is_accepted_by_the_worker_path(self):
        """干活端的答案是人话，不要求 JSON。

        2026-11-19 真机：worker 的任务包要求"用三到五行说清"，模型照做了，解析器却坚持要
        `{"answer": …}`，于是整件活判失败并给了个谁也看不懂的错。
        """
        self.fake_cli(self.session()
                      + self.emit({'type': 'status', 'phase': 'step_start', 'text': '改 README'})
                      + 'print(json.dumps({"type":"status","phase":"turn_end",'
                        '"reason":{"kind":"completed"}}),flush=True)\n'
                      + 'print(json.dumps({"type":"final","text":"改好了：README 快速开始重写，'
                        '提交 1 次"}),flush=True)\n')
        result = self.run_work()
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.kind, 'done')
        self.assertIn('改好了：README', result.text)

    def test_process_without_final_is_no_final(self):
        self.fake_cli(self.session() + 'time.sleep(0.05)\n')
        result = self.run_work()
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, 'no_final')
        self.assertIsNotNone(result.handoff)

    # -- 3 超时 + 进程组无残留 ---------------------------------------------
    def test_timeout_kills_the_group(self):
        pid_file = Path(self.temp.name) / 'child.pid'
        self.fake_cli('child=subprocess.Popen([sys.executable,"-c",'
                      '"import time;time.sleep(60)"])\n'
                      'open(' + repr(str(pid_file)) + ',"w").write(str(child.pid))\n'
                      'time.sleep(60)\n')
        result = self.run_work(timeout=0.6)
        self.assertEqual(result.kind, 'timeout')
        self.assertIsNotNone(result.handoff)
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 2
        while True:
            probe = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                   capture_output=True, text=True)
            dead = probe.returncode != 0 or probe.stdout.strip().startswith('Z')
            if dead or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        self.assertTrue(dead, probe.stdout)

    # -- 4 取消 -------------------------------------------------------------
    def test_cancel_is_reported_as_timeout_with_handoff(self):
        self.fake_cli(self.session() + 'time.sleep(60)\n')
        stop = threading.Event()
        threading.Timer(0.4, stop.set).start()
        result = self.run_work(stop=stop, timeout=30)
        self.assertEqual(result.kind, 'timeout')
        self.assertIsNotNone(result.handoff)

    # -- 5 锁冲突 -----------------------------------------------------------
    def test_lock_conflict_is_named_not_guessed(self):
        self.fake_cli('print(json.dumps({"type":"error","message":'
                      '"session \\"s1\\" is already owned by an active write handle"}),flush=True)\n'
                      'sys.exit(1)\n')
        result = self.run_work()
        self.assertEqual(result.kind, 'lock_conflict')
        self.assertIn('关掉我再来', result.text)
        self.assertIsNotNone(result.handoff)

    # -- 6 非法事件 ---------------------------------------------------------
    def test_unexpected_event_fails_closed(self):
        self.fake_cli(self.session() + 'print(json.dumps({"type":"telepathy"}),flush=True)\n')
        result = self.run_work()
        self.assertEqual(result.kind, 'error')

    # -- 7 空答案 -----------------------------------------------------------
    def test_empty_answer_is_an_error(self):
        self.fake_cli(self.success(answer=''))
        result = self.run_work()
        self.assertEqual(result.kind, 'error')

    # -- 8 CLI 不存在 -------------------------------------------------------
    def test_missing_cli_is_cli_unavailable_with_handoff(self):
        self.fake_cli(self.success())
        result = self.bench(cli=str(Path(self.temp.name) / 'not-here')).run(
            '做', task_id='tsk_wb', requirement='需求')
        self.assertEqual(result.kind, 'cli_unavailable')
        self.assertIsNotNone(result.handoff)
        self.assertTrue(result.handoff.resume_command)

    # -- 9 凭证缺失 ---------------------------------------------------------
    def test_missing_credential_is_credential_missing_and_scrubbed(self):
        os.environ.pop('DEEPSEEK_API_KEY')
        self.fake_cli(self.success())
        result = self.run_work()
        self.assertEqual(result.kind, 'credential_missing')
        self.assertIsNotNone(result.handoff)
        blob = result.text + json.dumps(result.handoff.as_dict(), ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn('sk-', blob)

    # -- 10 overlay 契约 ----------------------------------------------------
    # -- 凭证路由：环境没 key 但有已保存登录 → stored，且 overlay 必须恢复那三行 ------
    def test_credential_route_falls_back_to_saved_login(self):
        """2026-11-19 真机问题：干活端进程环境里没有 DEEPSEEK_API_KEY。

        回话端能一直正常回消息、干活端却失败，第一嫌疑就是"两条路解析出的凭证不一样"。
        所以把判定本身钉住：没有环境 key、有已保存登录 → `stored`；两样都没有 → None。
        """
        os.environ.pop('DEEPSEEK_API_KEY', None)
        self.assertIsNone(workbench.credential_route(str(self.workspace)))
        (self.dsh_home / '.credentials.yaml').write_text('credentials:\n  token: x\n')
        self.assertEqual(workbench.credential_route(str(self.workspace)), 'stored')

    def test_worker_style_overlay_restores_the_saved_login(self):
        """干活端这条路构造出的 overlay 必须包含恢复登录的三行（否则 headless 拿不到登录态）。"""
        os.environ.pop('DEEPSEEK_API_KEY', None)
        (self.dsh_home / '.credentials.yaml').write_text('credentials:\n  token: x\n')
        bench = self.bench()
        self.assertEqual(bench.credential_route(), 'stored')
        overlay = Path(self.temp.name) / 'worker-overlay.yml'
        workbench.write_overlay(overlay, bench.credential_route())
        text = overlay.read_text(encoding='utf-8')
        for row in ('credentials', 'authorization', 'deepseek-account'):
            self.assertIn('- id: %s\n  disabled: false' % row, text)

    # -- 错误提取：不许把一行日志当成"为什么停" ------------------------------
    def test_error_message_comes_from_the_final_reason_not_a_log_line(self):
        """真机现象：交接单里"为什么停"写成了一行 model listing 日志。

        要求：优先取 `final.reason.error.message`；stderr 里的正常日志不许再冒充错误。
        """
        message = 'llm-deepseek: no API key for provider route "deepseek-official"'
        self.fake_cli(
            'import sys\n'
            'sys.stderr.write("[llm-deepseek-api-key] model listing from '
            'https://api.deepseek.com/models loaded 2 model(s), skipped 0 row(s)\\n")\n'
            'sys.stderr.flush()\n'
            + self.session()
            + 'print(json.dumps({"type":"final","text":"",'
              '"reason":{"kind":"error","error":{"message":%r}}}),flush=True)\n' % message)
        result = self.run_work()
        self.assertFalse(result.ok)
        self.assertIn(message, result.text)
        self.assertNotIn('model listing', result.text)
        self.assertIn(message, result.handoff.failure)

    def test_log_only_stderr_is_not_reported_as_the_error(self):
        """进程非零退出、又没有 final：结论是"机器睡过去了"，不是抄一行日志给用户。"""
        self.fake_cli(
            'import sys\n'
            'sys.stderr.write("[llm-deepseek-api-key] model listing from '
            'https://api.deepseek.com/models loaded 2 model(s), skipped 0 row(s)\\n")\n'
            'sys.exit(9)\n')
        result = self.run_work()
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, 'no_final')
        self.assertIn('机器睡过去了', result.text)
        self.assertNotIn('model listing', result.text)
        self.assertTrue(result.handoff is not None)

    def test_real_error_lines_beat_log_lines_in_stderr(self):
        """stderr 里既有日志又有真错误：挑真错误那一行。"""
        self.fake_cli(
            'import sys\n'
            'sys.stderr.write("[llm-deepseek-api-key] model listing from x loaded 2 model(s)\\n")\n'
            'sys.stderr.write("Error: MISSING_CREDENTIAL: no API key for route deepseek-official\\n")\n'
            'sys.exit(3)\n')
        result = self.run_work()
        self.assertFalse(result.ok)
        self.assertIn('MISSING_CREDENTIAL', result.text)
        self.assertNotIn('model listing', result.text)

    def test_overlay_is_0600_frozen_and_deleted(self):
        code = ('argv=sys.argv[1:]\n'
                'assert argv[0]=="headless", argv\n'
                'assert argv[1]=="--patch", argv\n'
                'open(os.environ["WB_SNAPSHOT"]+".mode","w").write(str(os.stat(argv[2]).st_mode & 0o777))\n'
                'open(os.environ["WB_SNAPSHOT"]+".text","w").write(open(argv[2]).read())\n'
                'open(os.environ["WB_SNAPSHOT"]+".argv","w").write(json.dumps(argv))\n')
        self.fake_cli(code + self.success())
        result = self.run_work()
        self.assertTrue(result.ok, result.kind)
        snapshot = self.snapshot
        self.assertEqual(Path(str(snapshot) + '.mode').read_text(), str(0o600))
        self.assertEqual(Path(str(snapshot) + '.text').read_text(),
                         workbench.overlay_text('env'))
        self.assertEqual(json.loads(Path(str(snapshot) + '.argv').read_text())[-2:], ['--json', '-'])
        # 跑完就删：overlay 只活一次运行
        leftovers = list((self.dsh_home / 'work' / 'tsk_wb').glob(workbench.OVERLAY_NAME))
        self.assertEqual(leftovers, [])

    # -- 11 输出超限 --------------------------------------------------------
    def test_output_over_budget_is_bounded(self):
        self.fake_cli('sys.stdout.write("x"*20000)\nsys.stdout.flush()\ntime.sleep(5)\n')
        result = self.run_work(max_output_bytes=4096)
        self.assertEqual(result.kind, 'error')
        self.assertLessEqual(result.output_bytes, 4096)
        for path in (self.dsh_home / 'work' / 'tsk_wb').glob('*.log'):
            self.assertLessEqual(path.stat().st_size, 4096)

    # -- 12 prepare() 五种坏配置 -------------------------------------------
    def test_prepare_rejects_bad_configurations(self):
        cases = [
            (dict(kind='dsh-desktop'), 'cli_unavailable'),
            (dict(cli=str(Path(self.temp.name) / 'nope')), 'cli_unavailable'),
            (dict(home=str(Path(self.temp.name) / 'no-home')), 'cli_unavailable'),
            (dict(workspace=str(Path(self.temp.name) / 'no-workspace')), 'cli_unavailable'),
        ]
        self.fake_cli(self.success())
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                config = {'workbench': dict({'cli': str(self.cli), 'home': str(self.dsh_home),
                                             'workspace': str(self.workspace)}, **overrides)}
                with self.assertRaises(workbench.WorkbenchError) as caught:
                    workbench.Workbench(config, cwd=str(self.workspace)).prepare()
                self.assertEqual(caught.exception.kind, expected)
        # 第五种：DSH_HOME 与配置不一致
        with mock.patch.dict(os.environ, {'DSH_HOME': str(Path(self.temp.name) / 'other')}):
            with self.assertRaises(workbench.WorkbenchError) as caught:
                self.bench().prepare()
        self.assertEqual(caught.exception.kind, 'cli_unavailable')

    # -- 13 交接包：七种失败都要齐 -----------------------------------------
    def test_every_failure_carries_a_complete_handoff(self):
        scenarios = {
            'lock_conflict': ('print(json.dumps({"type":"error","message":'
                              '"session \\"s1\\" is already owned by an active write handle"}),'
                              'flush=True)\nsys.exit(1)\n', {}),
            'no_final': (self.session() + 'time.sleep(0.05)\n', {}),
            'timeout': (self.session() + 'time.sleep(60)\n', {'timeout': 0.5}),
            'error': (self.session() + 'print(json.dumps({"type":"nope"}),flush=True)\n', {}),
        }
        for expected, (code, kwargs) in scenarios.items():
            with self.subTest(kind=expected):
                self.fake_cli(code)
                kwargs = dict(kwargs, task_id='tsk_%s' % expected)
                result = self.run_work(**kwargs)
                self.assertEqual(result.kind, expected)
                self.assertIsNotNone(result.handoff)
                handoff = result.handoff
                for name in ('task_id', 'requirement', 'last_step', 'progress_lines',
                             'artifacts', 'workspace', 'session_id', 'resume_command',
                             'failure'):
                    self.assertTrue(hasattr(handoff, name), name)
                self.assertEqual(handoff.task_id, 'tsk_%s' % expected)
                self.assertEqual(handoff.requirement, '群里那条需求')
                self.assertTrue(handoff.failure)
                self.assertTrue(handoff.resume_command)
                self.assertEqual(handoff.workspace, str(Path(self.workspace).resolve()))
                markdown, payload = self.handoff_files('tsk_%s' % expected)
                self.assertTrue(markdown.is_file() and payload.is_file())
                self.assertEqual(markdown.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(payload.read_text())['failure'], handoff.failure)

    # -- 13b SIGKILL 之后仍能交接 ------------------------------------------
    def test_too_many_artifacts_truncate_the_list_but_do_not_fail(self):
        """产物太多只截清单，**不判失败**：清单是给人看的，不是把目录倒进群消息。"""
        space = self.dsh_home / 'work' / 'tsk_many' / 'space'
        space.mkdir(parents=True)
        for index in range(workbench.ARTIFACT_LIMIT + 5):
            (space / ('f%03d.txt' % index)).write_text('x')
        self.fake_cli(self.session() + 'time.sleep(0.05)\n')
        result = self.run_work(cwd=space, task_id='tsk_many')
        self.assertEqual(len(result.artifacts), workbench.ARTIFACT_LIMIT)
        self.assertTrue(result.artifacts_truncated, '截断了就要标出来，人得知道只列了一部分')

    def test_handoff_explains_the_unattended_sandbox_policy(self):
        """接手的人必须知道无人值守那次跑的是什么策略——否则会以为命令漏了参数。"""
        handoff = workbench.Handoff(task_id='t1', requirement='r', last_step='s',
                                    progress_lines=[], artifacts=[], workspace='/w',
                                    space=None, session_id='sess', resume_command='dsh headless',
                                    failure='超时了')
        text = handoff.as_markdown()
        self.assertIn('这次是无人值守跑的', text)
        self.assertIn('不问任何人', text)
        self.assertIn('故意没把那套限制带进这条命令', text)
        # 交接单会整份发到私聊：不许出现系统词（approval / policy 这些）
        for word in ('approval', 'policy', 'hints'):
            self.assertNotIn(word, text)

    def test_handoff_survives_sigkill_and_keeps_progress(self):
        pid_file = Path(self.temp.name) / 'stubborn.pid'
        # 子进程忽略 SIGTERM：只有 SIGKILL 能收掉它；同时先把进度写进事件流。
        code = (self.session()
                + 'print(json.dumps({"type":"status","phase":"step_start","text":"读需求"}),flush=True)\n'
                + 'print(json.dumps({"type":"status","phase":"step_start","text":"改代码"}),flush=True)\n'
                + 'child=subprocess.Popen([sys.executable,"-c",'
                  '"import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"])\n'
                + 'open(' + repr(str(pid_file)) + ',"w").write(str(child.pid))\n'
                + 'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                + 'time.sleep(60)\n')
        self.fake_cli(code)
        result = self.run_work(timeout=0.8)
        self.assertEqual(result.kind, 'timeout')
        handoff = result.handoff
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.progress_lines, ['读需求', '改代码'])
        self.assertEqual(handoff.session_id, 'session-wb')
        self.assertIn('--session-id', handoff.resume_command)
        self.assertIn('session-wb', handoff.resume_command)
        markdown = self.handoff_files()[0].read_text()
        self.assertIn('改代码', markdown)
        self.assertIn('接着干', markdown)

    # -- 14 抽取没写第二套 --------------------------------------------------
    def test_consumer_delegates_instead_of_reimplementing(self):
        text = CONSUMER.read_text(encoding='utf-8')
        self.assertNotIn('subprocess.Popen', text)
        # argv 的拼法（'headless'/'--json'）只允许出现在 workbench 里；
        # `profile: 'headless'` 这类元数据值不算重新实现。
        self.assertNotIn("'--json'", text)
        self.assertNotIn('sandbox-policy', text)
        self.assertNotIn('tool-subagent-fork', text)
        self.assertIn('tool-subagent-fork',
                      (REPO / 'bridge' / 'workbench.py').read_text(encoding='utf-8'))
        # 回话端只剩别名：常量必须与 workbench 里的是同一个对象/同一个值。
        import importlib.util

        spec = importlib.util.spec_from_file_location('wb_consumer', CONSUMER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIs(module.ALLOWED_EVENTS, workbench.ALLOWED_EVENTS)
        self.assertIs(module.DISABLED_TOOLS, workbench.DISABLED_TOOLS)
        self.assertEqual(module.OVERLAY_NAME, workbench.OVERLAY_NAME)
        self.assertEqual(module.AGENT_TIMEOUT_SECONDS, workbench.CONSUMER_TIMEOUT_SECONDS)
        self.assertEqual(module.MAX_OUTPUT_BYTES, workbench.DEFAULT_MAX_OUTPUT_BYTES)
        self.assertEqual(len(workbench.RESULT_KINDS), 7)

    # -- 15 产物清单 --------------------------------------------------------
    def test_artifacts_are_reported_relative_to_the_space_with_sizes(self):
        """产物 = 工作目录里的文件（相对路径 + 字节数），不再是单独的 artifacts/ 子目录。"""
        space = self.dsh_home / 'work' / 'tsk_wb' / 'space'
        (space / 'data').mkdir(parents=True)
        (space / 'report.md').write_text('x')
        (space / 'data' / 'a.csv').write_text('a,b\n')
        (space / '.hidden').write_text('不列')
        self.fake_cli(self.session() + 'time.sleep(0.05)\n')
        result = self.run_work(cwd=space)
        self.assertEqual(result.kind, 'no_final')
        listed = {item['path']: item['bytes'] for item in result.artifacts}
        self.assertIn('report.md', listed)
        self.assertIn('data/a.csv', listed)
        self.assertNotIn('.hidden', listed, '隐藏文件不进清单')


if __name__ == '__main__':
    unittest.main()

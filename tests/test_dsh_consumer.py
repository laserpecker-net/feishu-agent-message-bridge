"""Exercise the real adapter against private, synthetic CLI processes.

The fake CLI replays the verified `dsh headless --json` event shapes, so the
tests cover the argv/overlay/env contract, the capability allow/deny decisions
and the failure/timeout/cancel supervision without touching the network or a
real model.
"""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consumer_safety as safety


ADAPTER = Path(__file__).resolve().parents[1] / 'dsh-consumer.py'
#: 回话端的 overlay：沙箱必须是 read-only（角色边界：它只说话、不动手）。
EXPECTED_OVERLAY = """- id: sandbox-policy
  config:
    mode: read-only
- id: approval
  config:
    policy: never
- id: permission
  config:
    presets:
      read-only:
        sandbox: read-only
        approval: ask
      workspace-write:
        sandbox: workspace-write
        approval: ask
      danger-full-access:
        sandbox: danger-full-access
        approval: never
      consumer:
        sandbox: read-only
        approval: never
    defaultPreset: consumer
- id: tool-subagent
  disabled: true
- id: tool-subagent-fork
  disabled: true
- id: tool-subagent-control
  disabled: true
- id: tool-subagent-list-agents
  disabled: true
- id: tool-workflow
  disabled: true
"""
CREDENTIAL_ROWS = """- id: credentials
  disabled: false
- id: authorization
  disabled: false
- id: deepseek-account
  disabled: false
"""
SECRET = 'sk-' + 'a' * 24


class DshConsumerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(ADAPTER.is_file(), 'DSH adapter is required')
        spec = importlib.util.spec_from_file_location('dsh_consumer', ADAPTER)
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.cli = Path(self.temp.name) / 'dsh'
        self.dsh_home = Path(self.temp.name) / 'dsh-home'
        self.dsh_home.mkdir()
        # Headless needs a credential route; point DSH_HOME at a private dir so a
        # developer's real ~/.dsh store or .env cannot make a test pass by accident.
        self.env = mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key-value',
                                                'DSH_HOME': str(self.dsh_home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('FEISHU_CONSUMER_WORKSPACE', None)
        self.payload = {'text': 'private question', 'context': []}
        self.event = {'id': 5, 'claim_token': 'a' * 32}

    def fake_cli(self, code):
        self.cli.write_text('#!' + sys.executable + '\n' +
                            'import os,sys,json,time,signal,subprocess\n' + code)
        self.cli.chmod(0o700)
        return mock.patch.object(self.adapter.shutil, 'which', return_value=str(self.cli))

    def session(self):
        return ('print(json.dumps({"type":"session","sessionId":"session-fake",'
                '"cwd":os.getcwd()}),flush=True)\n')

    def emit(self, *events):
        return ''.join('print(json.dumps(%r),flush=True)\n' % event for event in events)

    def success(self, answer='{"answer":"989"}'):
        return (self.session()
                + self.emit({'type': 'text', 'text': 'WRONG intermediate'})
                + 'print(json.dumps({"type":"status","phase":"turn_end",'
                  '"reason":{"kind":"completed"}}),flush=True)\n'
                + 'print(json.dumps({"type":"final","text":' + repr(answer) + '}),flush=True)\n')

    def run_agent(self, stop=None, home=None):
        raw = self.adapter.build_agent(home or self.home)(
            self.payload, self.event, stop or threading.Event())
        # 适配器只校验、不重写 final（正文与 ask_owner 都要原样交给运行时）；
        # 解析与脱敏的唯一入口是 consumer_safety.finalize_reply。
        return safety.finalize_reply(raw)['answer']

    def receipt(self, home=None):
        runs = list((home or self.home).glob('consumer-agent-runs/dsh/*'))
        self.assertEqual(len(runs), 1)
        return json.loads((runs[0] / 'run.json').read_text()), runs[0]

    def test_entrypoint_uses_dedicated_cli_consumer_identity(self):
        with mock.patch('consumer_runtime.main') as main:
            runpy.run_path(str(ADAPTER), run_name='__main__')
        self.assertTrue(callable(main.call_args.args[0]))
        self.assertEqual(main.call_args.kwargs,
                         {'default_consumer': 'dsh-cli-main', 'default_kind': 'dsh'})

    def test_consumer_identity_can_be_overridden_without_claiming(self):
        with self.fake_cli(''), mock.patch.dict(os.environ, {'PATH': str(self.cli.parent)}):
            result = subprocess.run([sys.executable, str(ADAPTER), '--consumer', 'custom-dsh',
                                     '--home', str(self.home), '--check'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no bridge event was claimed', result.stdout)
        self.assertIn('capabilities', result.stdout)
        self.assertNotIn('test-key-value', result.stdout + result.stderr)
        self.assertFalse(self.home.exists())

    def test_stdin_private_fresh_cwd_and_terminal_json(self):
        code = ('prompt=sys.stdin.read()\n'
                'assert "private question" in prompt\n'
                'argv=sys.argv[1:]\n'
                'assert argv[0]=="headless", argv\n'
                'assert argv[1]=="--patch", argv\n'
                'assert os.path.basename(argv[2])=="consumer-overlay.yml", argv\n'
                'assert argv[3:]==["--json","-"], argv\n'
                'assert os.stat(argv[2]).st_mode & 0o777 == 0o600\n'
                'assert open(argv[2]).read()==' + repr(EXPECTED_OVERLAY) + '\n'
                'assert os.environ["DSH_PERMISSION_MODE"]=="read-only"\n'
                'assert os.stat(os.getcwd()).st_mode & 0o777 == 0o700\n'
                'open("seen-prompt","w").write(prompt)\n') + self.success()
        with self.fake_cli(code):
            self.assertEqual(self.run_agent(), '989')
            self.assertEqual(self.run_agent(), '989')
        runs = list(self.home.glob('consumer-agent-runs/dsh/*'))
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertEqual((run / 'stdout.ndjson').stat().st_mode & 0o777, 0o600)
            receipt = json.loads((run / 'run.json').read_text())
            self.assertEqual(receipt['status'], 'completed')
            self.assertEqual(receipt['session_id'], 'session-fake')
            self.assertEqual(receipt['sandbox_mode'], 'read-only')
            self.assertEqual(receipt['approval_policy'], 'never')
            self.assertEqual(receipt['credential_route'], 'env')
            self.assertNotIn('permission_mode', receipt)
            self.assertEqual(receipt['capabilities'], safety.ROLE_CAPABILITIES['replier'])
            self.assertEqual(receipt['denied_tools'], sorted(safety.DENIED_TOOL_NAMES))
            self.assertEqual(receipt['tool_calls'], 0)
            self.assertEqual(receipt['violations'], [])
            overlay = Path(receipt['overlay'])
            self.assertEqual(overlay, (run / 'consumer-overlay.yml').resolve())
            self.assertEqual(overlay.stat().st_mode & 0o777, 0o600)
            self.assertEqual(overlay.read_text(), EXPECTED_OVERLAY)
        # The overlay is per-run: no global DSH profile or config was touched.
        self.assertEqual(list(self.dsh_home.iterdir()), [])

    def test_workspace_runs_in_workspace(self):
        workspace = Path(self.temp.name) / 'workspace'
        workspace.mkdir()
        code = ('assert os.getcwd() == %r\n' % str(workspace.resolve())) + self.success()
        with self.fake_cli(code), mock.patch.dict(os.environ, {'FEISHU_CONSUMER_WORKSPACE': str(workspace)}):
            self.assertEqual(self.run_agent(), '989')
        receipt, run = self.receipt()
        self.assertEqual(receipt['workspace'], str(workspace.resolve()))
        self.assertEqual(Path(receipt['overlay']).parent, run.resolve())

    def test_stderr_warnings_are_not_fatal(self):
        # `dsh` writes non-fatal diagnostics to stderr; only stdout is the contract.
        code = ('sys.stderr.write("permission: warning only\\n")\n'
                'sys.stderr.flush()\n') + self.success()
        with self.fake_cli(code):
            self.assertEqual(self.run_agent(), '989')
        receipt, run = self.receipt()
        self.assertEqual(receipt['status'], 'completed')
        self.assertIn(b'warning only', (run / 'stderr.log').read_bytes())

    def test_tolerant_json_final_answer(self):
        cases = {
            '```json\n{"answer":"直接回答"}\n```': '直接回答',
            '```\n{"answer":"直接回答"}\n```': '直接回答',
            # Regression: a model that explains before answering could lose the reply.
            '我先查看规则和上下文。{"answer":"直接回答"}': '直接回答',
            '说明在前\n```json\n{"answer":"直接回答"}\n```\n收尾': '直接回答',
        }
        for answer, expected in cases.items():
            with self.subTest(answer=answer), self.fake_cli(self.success(answer)):
                self.assertEqual(self.run_agent(), expected)

    def test_final_answer_reaches_the_runtime_unparsed_so_ask_owner_survives(self):
        raw = ('{"answer":"这条要你拍板","ask_owner":{"question":"要不要调高？",'
               '"needs":"decision"}}')
        with self.fake_cli(self.success(raw)):
            text = self.adapter.build_agent(self.home)(self.payload, self.event, threading.Event())
        # 适配器只校验不重写：运行时才有唯一的解析/脱敏入口，ask_owner 不能被剥掉。
        self.assertEqual(text, raw)
        self.assertEqual(safety.finalize_reply(text)['ask_owner']['needs'], 'decision')

    def test_final_answer_without_json_still_fails(self):
        with self.fake_cli(self.success('只有文字，没有 JSON')):
            with self.assertRaises((RuntimeError, ValueError)):
                self.run_agent()

    def test_ordinary_tool_calls_are_allowed_and_counted(self):
        workspace = Path(self.temp.name) / 'workspace'
        workspace.mkdir()
        tool_events = [
            # Real verified input keys: bash -> command, read/write/edit -> file_path.
            {'type': 'tool_call', 'callId': 'c1', 'tool': 'bash',
             'input': {'command': 'ls -la', 'description': 'list files'}},
            {'type': 'tool_result', 'callId': 'c1', 'status': 'completed', 'result': 'ok'},
            {'type': 'tool_call', 'callId': 'c2', 'tool': 'read', 'input': {'file_path': 'ws/data.txt'}},
            {'type': 'tool_result', 'callId': 'c2', 'status': 'completed', 'result': 'ok'},
            {'type': 'tool_call', 'callId': 'c3', 'tool': 'grep',
             'input': {'pattern': 'DSH', 'file_path': 'ws/data.txt'}},
            {'type': 'tool_result', 'callId': 'c3', 'status': 'completed', 'result': 'ok'},
            {'type': 'tool_call', 'callId': 'c3b', 'tool': 'glob', 'input': {'pattern': '**/*.py'}},
            {'type': 'tool_result', 'callId': 'c3b', 'status': 'completed', 'result': 'ok'},
            {'type': 'tool_call', 'callId': 'c4', 'tool': 'write',
             'input': {'file_path': 'ws/notes.txt', 'content': 'hi'}},
            {'type': 'tool_result', 'callId': 'c4', 'status': 'completed', 'result': 'ok'},
            {'type': 'tool_call', 'callId': 'c5', 'tool': 'edit',
             'input': {'file_path': 'ws/notes.txt', 'old_string': 'a', 'new_string': 'b'}},
            {'type': 'tool_result', 'callId': 'c5', 'status': 'completed', 'result': 'ok'},
            {'type': 'tool_call', 'callId': 'c6', 'tool': 'web_search', 'input': {'query': 'python unittest'}},
            {'type': 'tool_result', 'callId': 'c6', 'status': 'completed', 'result': 'ok'},
            {'type': 'tool_call', 'callId': 'c7', 'tool': 'web_fetch',
             'input': {'url': 'https://example.com/docs'}},
            {'type': 'tool_result', 'callId': 'c7', 'status': 'completed', 'result': 'ok'},
        ]
        code = (self.session()
                + self.emit({'type': 'status', 'phase': 'turn_start'},
                            {'type': 'status', 'phase': 'step_start'})
                + self.emit(*tool_events)
                + self.emit({'type': 'status', 'phase': 'step_end'},
                            {'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}})
                + 'print(json.dumps({"type":"final","text":"{\\"answer\\":\\"done\\"}"}),flush=True)\n')
        with self.fake_cli(code), mock.patch.dict(os.environ, {'FEISHU_CONSUMER_WORKSPACE': str(workspace)}):
            self.assertEqual(self.run_agent(), 'done')
        receipt, _run = self.receipt()
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(receipt['tool_calls'], 8)
        self.assertEqual(receipt['violations'], [])

    def test_denied_tool_calls_kill_the_run_fast(self):
        cases = [
            ({'type': 'tool_call', 'callId': 'c1', 'tool': 'bash',
              'input': {'command': 'rm -rf /tmp/project'}}, 'DestructiveCommand'),
            ({'type': 'tool_call', 'callId': 'c1', 'tool': 'bash',
              'input': {'command': ['rm', '-rf', 'x']}}, 'DestructiveCommand'),
            ({'type': 'tool_call', 'callId': 'c1', 'tool': 'bash',
              'input': {'command': 'printenv DEEPSEEK_API_KEY'}}, 'DestructiveCommand'),
            ({'type': 'tool_call', 'callId': 'c1', 'tool': 'read',
              'input': {'file_path': '/Users/me/.ssh/id_rsa'}}, 'SecretPath'),
            ({'type': 'tool_call', 'callId': 'c1', 'tool': 'read',
              'input': {'file_path': '/tmp/project/.env'}}, 'SecretPath'),
            ({'type': 'tool_call', 'callId': 'c1', 'tool': 'subagent',
              'input': {'goal': 'do it'}}, 'DeniedTool'),
            ({'type': 'tool_call', 'callId': 'c1', 'tool': 'subagent_fork', 'input': {}},
             'DeniedTool'),
            ({'type': 'tool_call', 'callId': 'c1', 'tool': 'workflow', 'input': {}}, 'DeniedTool'),
            ({'type': 'tool_call', 'callId': 'c1', 'tool': 'mcp__server__tool', 'input': {}},
             'DeniedTool'),
            ({'type': 'tool_call', 'callId': 'c1', 'tool': 'write',
              'input': {'type': 'delete', 'path': 'notes.txt'}}, 'DeleteOperation'),
        ]
        for index, (event, reason) in enumerate(cases):
            home = Path(self.temp.name) / ('denied-%d' % index)
            code = self.session() + self.emit(event) + 'time.sleep(30)\n'
            with self.subTest(event=event), self.fake_cli(code):
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, 'tool call policy violation'):
                    self.run_agent(home=home)
                self.assertLess(time.monotonic() - started, 3)
            receipt, _run = self.receipt(home=home)
            self.assertEqual(receipt['status'], 'failed')
            self.assertEqual(receipt['tool_calls'], 1)
            self.assertEqual(receipt['violations'], [reason])
            self.assertNotIn('test-key-value', json.dumps(receipt, ensure_ascii=False))

    def test_error_unknown_and_malformed_events_are_fatal(self):
        cases = [
            ({'type': 'error', 'message': 'secret diagnostic'}, 'reported an error'),
            ({'type': 'system', 'subtype': 'init'}, 'Unexpected DSH event'),
            ({'type': 'tool_call', 'callId': 'c1', 'input': {}}, 'Invalid DSH tool call'),
        ]
        for index, (event, pattern) in enumerate(cases):
            home = Path(self.temp.name) / ('fatal-%d' % index)
            code = self.session() + self.emit(event) + 'time.sleep(30)\n'
            with self.subTest(event=event), self.fake_cli(code):
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, pattern) as error:
                    self.run_agent(home=home)
                self.assertLess(time.monotonic() - started, 3)
            self.assertNotIn('secret diagnostic', str(error.exception))

    def test_answer_redacts_leaked_key_material(self):
        answer = json.dumps({'answer': '这把钥匙是 ' + SECRET}, ensure_ascii=False)
        with self.fake_cli(self.success(answer)):
            self.assertEqual(self.run_agent(), safety.BLOCKED_REPLY)
        receipt, _run = self.receipt()
        self.assertEqual(receipt['status'], 'completed')

    def test_missing_credential_fails_before_creating_anything(self):
        with self.fake_cli(''), mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            with self.assertRaisesRegex(RuntimeError, 'DEEPSEEK_API_KEY') as error:
                self.adapter.build_agent(self.home)
        message = str(error.exception)
        self.assertIn('`dsh`', message)
        self.assertIn('headless ignores the GUI sign-in', message)
        self.assertIn('no bridge event was claimed', message)
        self.assertFalse(self.home.exists())

    def test_stored_signin_is_restored_by_the_overlay(self):
        credentials = self.dsh_home / '.credentials.yaml'
        credentials.write_text('deepseek: {}\n')
        (self.dsh_home / '.env').write_text('DEEPSEEK_API_KEY=lower-precedence\n')
        with self.fake_cli(self.success()), mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            self.assertEqual(self.run_agent(), '989')
        receipt, run = self.receipt()
        self.assertEqual(receipt['credential_route'], 'stored')
        overlay = (run / 'consumer-overlay.yml').resolve()
        self.assertEqual(overlay.read_text(), EXPECTED_OVERLAY + CREDENTIAL_ROWS)

    def test_user_patch_is_a_second_patch_layer_and_is_recorded(self):
        """机器级 provider 补丁：用户 DSH home 里的文件作为第二层 `--patch` 原样叠加。"""
        extra = self.dsh_home / 'feishu-consumer.patch.yml'
        extra.write_text('- id: agent-default-model\n  config:\n    model: m\n')
        code = ('argv = sys.argv[1:]\n'
                'assert argv.count("--patch") == 2, argv\n'
                'assert argv[3:5] == ["--patch", %r], argv\n'
                'assert argv[5:] == ["--json", "-"], argv\n'
                % str(extra)) + self.success()
        with self.fake_cli(code):
            self.assertEqual(self.run_agent(), '989')
        receipt, _run = self.receipt()
        self.assertEqual(receipt['extra_patch'], str(extra))

    def test_no_user_patch_keeps_the_single_patch_argv(self):
        code = ('argv = sys.argv[1:]\n'
                'assert argv.count("--patch") == 1, argv\n'
                'assert argv[3:] == ["--json", "-"], argv\n') + self.success()
        with self.fake_cli(code):
            self.assertEqual(self.run_agent(), '989')
        receipt, _run = self.receipt()
        self.assertIsNone(receipt['extra_patch'])

    def test_user_env_reaches_the_child_but_policy_keys_win(self):
        """机器级 env 文件：内部 CA 的 NODE_EXTRA_CA_CERTS 必须进子进程；策略键不可覆盖。"""
        env_file = self.dsh_home / self.adapter.EXTRA_ENV_NAME
        env_file.write_text('# 内部 CA\n'
                            'NODE_EXTRA_CA_CERTS=/tmp/example-root-ca.crt\n'
                            'export DSH_PERMISSION_MODE=workspace-write\n'
                            'DSH_HOME=/tmp/not-allowed\n')
        code = ('assert os.environ["NODE_EXTRA_CA_CERTS"] == "/tmp/example-root-ca.crt"\n'
                'assert os.environ["DSH_PERMISSION_MODE"] == "read-only"\n') + self.success()
        with self.fake_cli(code):
            self.assertEqual(self.run_agent(), '989')
        receipt, _run = self.receipt()
        self.assertEqual(receipt['extra_env'], ['NODE_EXTRA_CA_CERTS'])

    def test_no_user_env_means_no_machine_overrides(self):
        # 机器级 env 文件不存在时什么都没有：子进程环境只由父进程 + 策略键决定。
        self.assertEqual(self.adapter.extra_env(), {})

    def test_dotenv_in_dsh_home_is_a_credential_route(self):
        (self.dsh_home / '.env').write_text('# local\nDEEPSEEK_API_KEY=from-dsh-home\n')
        with self.fake_cli(self.success()), mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            self.assertEqual(self.run_agent(), '989')
        receipt, run = self.receipt()
        self.assertEqual(receipt['credential_route'], 'dotenv')
        self.assertEqual((run / 'consumer-overlay.yml').resolve().read_text(), EXPECTED_OVERLAY)

    def test_dotenv_in_workspace_is_a_credential_route(self):
        workspace = Path(self.temp.name) / 'workspace'
        workspace.mkdir()
        (workspace / '.env').write_text('export DEEPSEEK_API_KEY=from-workspace\n')
        with self.fake_cli(self.success()), \
             mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': '',
                                          'FEISHU_CONSUMER_WORKSPACE': str(workspace)}):
            self.assertEqual(self.run_agent(), '989')
        receipt, _run = self.receipt()
        self.assertEqual(receipt['credential_route'], 'dotenv')

    def test_dotenv_without_the_key_is_not_a_credential_route(self):
        (self.dsh_home / '.env').write_text('OTHER=1\nDEEPSEEK_API_KEY=\n')
        with self.fake_cli(''), mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            with self.assertRaisesRegex(RuntimeError, 'credential'):
                self.adapter.build_agent(self.home)

    def test_receipt_keeps_the_existing_audit_fields(self):
        with self.fake_cli(self.success()):
            self.run_agent()
        receipt, run = self.receipt()
        for field in ('event_id', 'status', 'session_id', 'profile', 'duration_seconds',
                      'workspace', 'exit_code', 'output_bytes', 'credential_route'):
            self.assertIn(field, receipt)
        self.assertEqual(receipt['event_id'], self.event['id'])
        self.assertEqual(receipt['profile'], 'headless')
        self.assertEqual(receipt['credential_route'], 'env')
        self.assertNotIn('permission_mode', receipt)
        self.assertIsNone(receipt['workspace'])
        self.assertEqual(receipt['overlay'], str((run / 'consumer-overlay.yml').resolve()))

    def test_posix_runtime_is_checked_before_any_claim(self):
        with mock.patch.object(self.adapter, 'require_posix_runtime') as guard, \
             self.fake_cli(''):
            self.assertTrue(callable(self.adapter.build_agent(self.home)))
        guard.assert_called_once_with()

    def test_rejects_incomplete_failed_malformed_and_post_final_output(self):
        cases = [
            'print(json.dumps({"type":"text","text":"not final"}))\n',
            self.success() + 'sys.exit(1)\n',
            'print("not json")\n',
            self.success() + 'print(json.dumps({"type":"text","text":"late"}))\n',
            self.success('{"answer":""}'),
            self.success('{"other":"wrong schema"}'),
            self.success('{broken json'),
            self.success('plain text violates JSON contract'),
            'print(json.dumps({"type":"final","text":"premature"}))\n',
            self.success().replace('"kind":"completed"', '"kind":"cancelled"'),
        ]
        for code in cases:
            with self.subTest(code=code), self.fake_cli(code):
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_agent()

    def test_stdout_and_stderr_output_are_bounded(self):
        for stream in ['sys.stdout', 'sys.stderr']:
            code = stream + '.write("x"*100000)\n' + stream + '.flush()\ntime.sleep(30)\n'
            with self.subTest(stream=stream), self.fake_cli(code), \
                 mock.patch.object(self.adapter, 'MAX_OUTPUT_BYTES', 4096):
                with self.assertRaisesRegex(RuntimeError, 'output limit'):
                    self.run_agent()
        for path in self.home.glob('consumer-agent-runs/dsh/*/*.log'):
            self.assertLessEqual(path.stat().st_size, 4096)

    def test_stop_before_start_does_not_create_run(self):
        stop = threading.Event()
        stop.set()
        with self.fake_cli(self.success()), self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.run_agent(stop)
        self.assertFalse(self.home.exists())

    def test_timeout_and_stop_kill_child_process_group(self):
        for cancel in [False, True]:
            with self.subTest(cancel=cancel):
                pid_file = Path(self.temp.name) / ('child-' + str(cancel))
                code = ('child=subprocess.Popen([sys.executable,"-c",'
                        '"import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"])\n'
                        'open(' + repr(str(pid_file)) + ',"w").write(str(child.pid))\n'
                        'time.sleep(30)\n')
                stop = threading.Event()
                timer = threading.Timer(0.2, stop.set)
                with self.fake_cli(code), mock.patch.object(self.adapter, 'AGENT_TIMEOUT_SECONDS', 0.5):
                    if cancel:
                        timer.start()
                    started = time.monotonic()
                    try:
                        with self.assertRaisesRegex(RuntimeError, 'cancelled' if cancel else 'timed out'):
                            self.run_agent(stop)
                    finally:
                        timer.cancel()
                    self.assertLess(time.monotonic() - started, 3)
                self.assertTrue(pid_file.exists())
                pid = int(pid_file.read_text())
                # A reparented zombie is dead even if the OS has not yet reaped it.
                deadline = time.monotonic() + 1
                while True:
                    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                            capture_output=True, text=True)
                    dead = result.returncode != 0 or result.stdout.strip().startswith('Z')
                    if dead or time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
                self.assertTrue(dead, result.stdout)

    def test_cleanup_failure_preserves_original_generation_error(self):
        code = self.emit({'type': 'tool_call', 'callId': 'c1', 'tool': 'subagent', 'input': {}})
        original = self.adapter._kill_group

        def cleanup(process):
            original(process)
            raise RuntimeError('cleanup failure')

        with self.fake_cli(code), mock.patch.object(self.adapter, '_kill_group', side_effect=cleanup):
            with self.assertRaisesRegex(RuntimeError, 'tool call policy violation'):
                self.run_agent()

    def test_cleanup_reaps_wrapper_even_when_group_signal_fails(self):
        process = mock.Mock(pid=12345)
        with mock.patch.object(self.adapter.os, 'killpg', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.adapter._kill_group(process)
        process.wait.assert_called_once_with(timeout=1)

    def test_missing_binary_fails_before_home_creation(self):
        with mock.patch.object(self.adapter.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'DSH executable'):
                self.adapter.build_agent(self.home)
        self.assertFalse(self.home.exists())


if __name__ == '__main__':
    unittest.main()

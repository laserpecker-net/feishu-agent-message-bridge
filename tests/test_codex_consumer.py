"""Exercise the real Codex adapter against private, synthetic CLI processes.

No real Codex model call is made: every test runs a fake CLI that replays
verified codex-cli 0.147.0 JSONL shapes. Safety assertions are end-to-end: a
denied item must kill the process group within 3 s, record the capability
violation in the run receipt, and never produce an answer.
"""
import importlib.util
import json
import os
import pathlib
import runpy
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from consumer_safety import BLOCKED_REPLY, CAPABILITIES, finalize_reply  # noqa: E402

SPEC = importlib.util.spec_from_file_location('codex_consumer', ROOT / 'codex-consumer.py')
DISABLED_FEATURES = ['plugins', 'hooks', 'multi_agent', 'apps', 'browser_use',
                     'computer_use', 'image_generation']


class CodexConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)
        self.binary = self.home / 'fake-codex'
        auth_home = self.home / 'source-auth'
        auth_home.mkdir(mode=0o700)
        (auth_home / 'auth.json').write_text('{}')
        (auth_home / 'auth.json').chmod(0o600)
        environment = mock.patch.dict(os.environ, {'CODEX_HOME': str(auth_home)})
        environment.start()
        self.addCleanup(environment.stop)

    def fake(self, body):
        self.binary.write_text('#!' + sys.executable + '\n' + body)
        self.binary.chmod(0o700)
        patcher = mock.patch.dict(os.environ, {'CODEX_CONSUMER_BINARY': str(self.binary)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.module.build_agent(self.home)

    def rows(self, *rows):
        return '\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n'

    def success(self, answer='你好'):
        return self.rows(
            {'type': 'thread.started', 'thread_id': 'fake'},
            {'type': 'turn.started'},
            {'type': 'item.completed',
             'item': {'id': 'a', 'type': 'agent_message', 'text': json.dumps({'answer': answer}, ensure_ascii=False)}},
            {'type': 'turn.completed', 'usage': {}},
        )

    def invoke(self, generate, stop=None):
        raw = generate({'message_id': 'm1', 'text': 'private prompt content', 'context': []},
                       {'id': 1, 'claim_token': 'token'}, stop or threading.Event())
        return self.answer_of(raw)

    @staticmethod
    def answer_of(raw):
        # 适配器只校验、不重写 final；解析与脱敏的唯一入口是 finalize_reply。
        return finalize_reply(raw)['answer']

    def runs(self):
        return sorted((self.home / 'codex-runs').iterdir(), key=lambda path: path.stat().st_mtime)

    def last_receipt(self):
        return json.loads((self.runs()[-1] / 'init.json').read_text())

    def denied(self, item, event_type='item.started'):
        """Replay one denied item, assert a fast kill and return the receipt reasons."""
        output = self.rows({'type': event_type, 'item': item})
        # flush=True: the fake must emit the item before sleeping, or the pipe buffers it.
        generate = self.fake('import time\nprint(' + repr(output) + ',end="",flush=True)\ntime.sleep(30)\n')
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'tool call policy'):
            self.invoke(generate)
        self.assertLess(time.monotonic() - started, 3)
        run = self.runs()[-1]
        self.assertFalse((run / 'answer.json').exists())
        return self.last_receipt()['capability_violations']

    def test_success_stdin_isolation_capability_flags_and_receipt(self):
        body = ('import json,os,pathlib,sys\n'
                'p=pathlib.Path.cwd()\n'
                '(p/"seen.json").write_text(json.dumps({"binary":sys.argv[0],"args":sys.argv[1:],'
                '"stdin":sys.stdin.read(),"cwd":str(p),"home":os.environ.get("HOME"),'
                '"codex_home":os.environ.get("CODEX_HOME")}))\n'
                '(p/"seen.json").chmod(0o600)\n'
                'print(' + repr(self.success()) + ',end="")\n')
        generate = self.fake(body)
        self.assertEqual('你好', self.invoke(generate))
        run = self.runs()[-1]
        seen = json.loads((run / 'seen.json').read_text())
        self.assertEqual(str(self.binary), seen['binary'])
        self.assertEqual('-', seen['args'][-1])
        self.assertIn('private prompt content', seen['stdin'])
        self.assertNotIn('private prompt content', ' '.join(seen['args']))
        # Exact argv: sandbox-confined writes, approvals never, shell/edit enabled,
        # delegation/app/browser/computer/image features disabled, web search live.
        expected = ['--ask-for-approval', 'never', 'exec', '--ephemeral', '--skip-git-repo-check',
                    '--ignore-user-config', '--sandbox', 'workspace-write', '--json',
                    '--color', 'never']
        for feature in DISABLED_FEATURES:
            expected += ['--disable', feature]
        expected += ['-c', 'web_search="live"',
                     '-c', 'sandbox_workspace_write.network_access=true',
                     '-c', 'project_doc_max_bytes=0',
                     '--output-schema', str(run / 'schema.json'), '-']
        self.assertEqual(expected, seen['args'])
        self.assertNotIn('shell_tool', seen['args'])
        self.assertNotIn('read-only', seen['args'])
        self.assertEqual(str(run), seen['home'])
        self.assertEqual(str(run / 'codex-home'), seen['codex_home'])
        self.assertTrue((run / 'codex-home/auth.json').is_symlink())
        self.assertEqual(self.home / 'source-auth/auth.json', (run / 'codex-home/auth.json').resolve())
        self.assertEqual(0o700, run.stat().st_mode & 0o777)
        self.assertTrue(all(path.stat().st_mode & 0o077 == 0 for path in run.iterdir()),
                        [(path.name, oct(path.stat().st_mode & 0o777)) for path in run.iterdir()])
        receipt = self.last_receipt()
        self.assertEqual(1, receipt['event_id'])
        self.assertEqual(240, receipt['timeout'])
        self.assertIn('user config ignored', receipt['model_selection'])
        self.assertIsNone(receipt['workspace'])
        self.assertEqual(CAPABILITIES, receipt['capabilities'])
        self.assertEqual('workspace-write', receipt['sandbox_mode'])
        self.assertEqual('never', receipt['approval_policy'])
        self.assertIs(True, receipt['network_access'])
        self.assertEqual(DISABLED_FEATURES, receipt['disabled_features'])
        self.assertEqual([], receipt['capability_violations'])
        self.assertEqual(0, receipt['capability_warnings'])
        self.assertNotIn('--model', seen['args'])
        self.assertNotIn('--ignore-rules', seen['args'])

    def test_success_allows_commands_file_changes_and_web_search(self):
        answer = '读了 README，写了 notes.md'
        output = self.rows(
            {'type': 'thread.started', 'thread_id': 'fake'},
            {'type': 'turn.started'},
            {'type': 'item.started', 'item': {'id': 'r1', 'type': 'reasoning', 'text': 'look first'}},
            {'type': 'item.completed', 'item': {'id': 'r1', 'type': 'reasoning', 'text': 'look first'}},
            {'type': 'item.started', 'item': {'id': 'c1', 'type': 'command_execution',
                                              'command': '/bin/zsh -lc "sed -n 1,5p README.md"',
                                              'status': 'in_progress'}},
            {'type': 'item.completed', 'item': {'id': 'c1', 'type': 'command_execution',
                                                'command': '/bin/zsh -lc "sed -n 1,5p README.md"',
                                                'aggregated_output': 'README heading\n',
                                                'exit_code': 0, 'status': 'completed'}},
            {'type': 'item.completed', 'item': {'id': 'f1', 'type': 'file_change', 'status': 'completed',
                                                'changes': [{'path': 'notes.md', 'kind': 'add'}]}},
            {'type': 'item.completed', 'item': {'id': 'w1', 'type': 'web_search',
                                                'query': 'codex cli release',
                                                'results': [{'url': 'https://example.com/codex'}]}},
            {'type': 'item.completed', 'item': {'id': 't1', 'type': 'todo_list',
                                                'items': [{'text': 'read README', 'done': True}]}},
            {'type': 'item.completed', 'item': {'id': 'p1', 'type': 'plan_update', 'text': 'plan'}},
            {'type': 'item.completed', 'item': {'id': 'a', 'type': 'agent_message',
                                                'text': json.dumps({'answer': answer}, ensure_ascii=False)}},
            {'type': 'turn.completed', 'usage': {}},
        )
        generate = self.fake('print(' + repr(output) + ',end="")\n')
        self.assertEqual(answer, self.invoke(generate))
        receipt = self.last_receipt()
        self.assertEqual([], receipt['capability_violations'])
        self.assertEqual(0, receipt['capability_warnings'])

    def test_nonfatal_error_items_are_counted_not_fatal(self):
        output = self.rows(
            {'type': 'thread.started', 'thread_id': 'fake'},
            {'type': 'turn.started'},
            {'type': 'item.completed', 'item': {'id': 'e1', 'type': 'error',
                                                'message': 'skills/agent-role notice'}},
            {'type': 'item.completed', 'item': {'id': 'e2', 'type': 'error',
                                                'message': 'hooks unavailable'}},
            {'type': 'item.completed', 'item': {'id': 'a', 'type': 'agent_message',
                                                'text': json.dumps({'answer': '收到'}, ensure_ascii=False)}},
            {'type': 'turn.completed', 'usage': {}},
        )
        generate = self.fake('print(' + repr(output) + ',end="")\n')
        self.assertEqual('收到', self.invoke(generate))
        receipt = self.last_receipt()
        self.assertEqual([], receipt['capability_violations'])
        self.assertEqual(2, receipt['capability_warnings'])

    def test_denied_and_unknown_item_types_kill_fast(self):
        cases = [
            ('mcp_tool_call', {'type': 'mcp_tool_call', 'server': 'filesystem', 'tool': 'read_file'}),
            ('collab_agent_tool_call', {'type': 'collab_agent_tool_call', 'agent': 'worker'}),
            ('collab_agent_spawn', {'type': 'collab_agent_spawn', 'role': 'worker'}),
            ('computer_call', {'type': 'computer_call', 'action': 'click'}),
            ('browser_call', {'type': 'browser_call', 'url': 'https://example.com'}),
            ('image_generation', {'type': 'image_generation', 'prompt': 'cat'}),
            ('future_unknown_tool', {'type': 'future_unknown_tool', 'payload': {}}),
        ]
        for item_type, item in cases:
            with self.subTest(item_type=item_type):
                reasons = self.denied(item, event_type='item.completed')
                self.assertEqual(['ItemType:' + item_type], reasons)

    def test_destructive_and_secret_commands_kill_fast(self):
        cases = [
            '/bin/zsh -lc "rm -rf /tmp/x"',
            '/bin/zsh -lc "cat .env"',
            '/bin/zsh -lc "cat ~/.ssh/id_rsa"',
            '/bin/zsh -lc "security find-generic-password -w"',
            '/bin/zsh -lc "git reset --hard origin/main"',
            '/bin/zsh -lc "git checkout ."',
            'bash -c "git checkout ."',
            'cmd.exe /c "del /s /q C:\\tmp\\x"',
            'shred -u notes.md',
        ]
        for command in cases:
            with self.subTest(command=command):
                item = {'type': 'command_execution', 'command': command,
                        'aggregated_output': '', 'exit_code': 0, 'status': 'completed'}
                reasons = self.denied(item, event_type='item.completed')
                self.assertEqual(['DestructiveCommand'], reasons)

    def test_delete_and_secret_path_file_changes_kill_fast(self):
        cases = [
            ({'type': 'file_change', 'status': 'completed',
              'changes': [{'path': 'notes.md', 'kind': 'delete'}]}, 'DeleteOperation'),
            ({'type': 'file_change', 'status': 'completed',
              'changes': [{'path': '.env', 'kind': 'update'}]}, 'SecretPath'),
            ({'type': 'file_change', 'status': 'completed',
              'changes': [{'path': '/Users/someone/.ssh/id_ed25519', 'kind': 'add'}]}, 'SecretPath'),
        ]
        for item, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual([reason], self.denied(item, event_type='item.completed'))

    def test_secret_answer_is_redacted(self):
        answer = '配置前先 export OPENAI_API_KEY=sk-' + 'a1' * 12
        generate = self.fake('print(' + repr(self.success(answer)) + ',end="")\n')
        self.assertEqual(BLOCKED_REPLY, self.invoke(generate))

    def test_fenced_json_answer_is_tolerated(self):
        fenced = '```json\n' + json.dumps({'answer': '直接回答'}, ensure_ascii=False) + '\n```'
        output = self.rows(
            {'type': 'thread.started', 'thread_id': 'fake'},
            {'type': 'turn.started'},
            {'type': 'item.completed', 'item': {'id': 'a', 'type': 'agent_message', 'text': fenced}},
            {'type': 'turn.completed', 'usage': {}},
        )
        generate = self.fake('print(' + repr(output) + ',end="")\n')
        self.assertEqual('直接回答', self.invoke(generate))

    def test_prose_before_the_answer_is_tolerated(self):
        # Regression: a model that explains before answering used to lose the reply.
        prose = '先看下上下文。' + json.dumps({'answer': '直接回答'}, ensure_ascii=False)
        output = self.rows(
            {'type': 'thread.started', 'thread_id': 'fake'},
            {'type': 'turn.started'},
            {'type': 'item.completed', 'item': {'id': 'a', 'type': 'agent_message', 'text': prose}},
            {'type': 'turn.completed', 'usage': {}},
        )
        generate = self.fake('print(' + repr(output) + ',end="")\n')
        self.assertEqual('直接回答', self.invoke(generate))

    def test_answer_json_reaches_the_runtime_unparsed(self):
        raw = ('{"answer":"这条要你拍板","ask_owner":{"question":"要不要调高？",'
               '"needs":"decision"}}')
        output = self.rows(
            {'type': 'thread.started', 'thread_id': 'fake'},
            {'type': 'turn.started'},
            {'type': 'item.completed', 'item': {'id': 'a', 'type': 'agent_message', 'text': raw}},
            {'type': 'turn.completed', 'usage': {}},
        )
        generate = self.fake('print(' + repr(output) + ',end="")\n')
        text = generate({'message_id': 'm1', 'text': 'p', 'context': []},
                        {'id': 1, 'claim_token': 'token'}, threading.Event())
        # 适配器只校验不重写；解析与 ask_owner 归运行时的 finalize_reply。
        self.assertEqual(text, raw)
        self.assertEqual(finalize_reply(text)['ask_owner']['needs'], 'decision')

    def test_answer_without_json_still_fails_closed(self):
        output = self.rows(
            {'type': 'thread.started', 'thread_id': 'fake'},
            {'type': 'turn.started'},
            {'type': 'item.completed', 'item': {'id': 'a', 'type': 'agent_message', 'text': '没有 JSON'}},
            {'type': 'turn.completed', 'usage': {}},
        )
        generate = self.fake('print(' + repr(output) + ',end="")\n')
        with self.assertRaises(RuntimeError):
            self.invoke(generate)

    def test_workspace_uses_cwd_and_records_workspace(self):
        workspace = self.home / 'workspace'
        workspace.mkdir()
        body = ('import json,pathlib,sys\n'
                'p=pathlib.Path.cwd()\n'
                '(p/"seen.json").write_text(json.dumps({"cwd":str(p),"args":sys.argv[1:]}))\n'
                'print(' + repr(self.success()) + ',end="")\n')
        with mock.patch.dict(os.environ, {'FEISHU_CONSUMER_WORKSPACE': str(workspace)}):
            generate = self.fake(body)
            self.assertEqual('你好', self.invoke(generate))
        seen = json.loads((workspace / 'seen.json').read_text())
        self.assertEqual(seen['cwd'], str(workspace.resolve()))
        # Shell/edit capability is enabled; nothing disables the shell tool.
        self.assertNotIn('shell_tool', seen['args'])
        self.assertEqual(['--disable', 'plugins'][0], seen['args'][seen['args'].index('--disable')])
        receipt = self.last_receipt()
        self.assertEqual(str(workspace.resolve()), receipt['workspace'])
        self.assertEqual([], receipt['capability_violations'])

    def test_invalid_workspace_fails_before_claim(self):
        with mock.patch.dict(os.environ, {'FEISHU_CONSUMER_WORKSPACE': 'relative/path'}):
            with self.assertRaisesRegex(RuntimeError, 'absolute'):
                self.module.build_agent(self.home)

    def test_malformed_error_missing_terminal_and_invalid_answer_fail_closed(self):
        cases = [
            ('not json\n', 'Invalid Codex'),
            (json.dumps({'type': 'error', 'message': 'broken'}) + '\n', 'reported an error'),
            (json.dumps({'type': 'turn.failed', 'error': {'message': 'broken'}}) + '\n', 'reported an error'),
            (self.success().rsplit('\n', 2)[0] + '\n', 'terminal'),
            (self.success(''), 'nonempty'),
            (self.success() + json.dumps({'type': 'turn.started'}) + '\n', 'after terminal'),
            (json.dumps({'type': 'mystery.event'}) + '\n', 'Unsupported'),
            (json.dumps({'type': 'item.completed', 'item': 'not-a-dict'}) + '\n', 'Invalid Codex item'),
        ]
        for output, error in cases:
            with self.subTest(error=error, output=output):
                generate = self.fake('print(' + repr(output) + ',end="")\n')
                with self.assertRaisesRegex(RuntimeError, error):
                    self.invoke(generate)

    def test_partial_jsonl_and_final_line_without_newline(self):
        stream = self.module._Stream()
        output = self.success('2').rstrip('\n').encode('utf-8')
        for byte in output:
            stream.feed(bytes([byte]))
        stream.feed(b'', final=True)
        self.assertEqual('2', self.answer_of(stream.result()))

    def test_invalid_binary_fails_before_claim(self):
        with mock.patch.dict(os.environ, {'CODEX_CONSUMER_BINARY': str(self.home)}):
            with self.assertRaisesRegex(RuntimeError, 'absolute executable'):
                self.module.build_agent(self.home)

    def test_missing_auth_fails_before_claim(self):
        self.fake("print('unused')\n")
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(self.home / 'missing-auth')}):
            with self.assertRaisesRegex(RuntimeError, 'auth.json is required'):
                self.module.build_agent(self.home)

    def test_main_uses_distinct_codex_identity(self):
        with mock.patch('consumer_runtime.main') as entry:
            runpy.run_path(str(ROOT / 'codex-consumer.py'), run_name='__main__')
        self.assertEqual({'default_consumer': 'codex-cli-main', 'default_kind': 'codex'}, entry.call_args.kwargs)
        self.assertTrue(callable(entry.call_args.args[0]))

    def test_nonzero_exit_rejects_good_answer(self):
        generate = self.fake('import sys\nprint(' + repr(self.success()) + ',end="")\nsys.exit(7)\n')
        with self.assertRaisesRegex(RuntimeError, 'exit code 7'):
            self.invoke(generate)

    def test_output_bounded_on_both_streams(self):
        for stream in ('stdout', 'stderr'):
            with self.subTest(stream=stream):
                generate = self.fake('import sys\nsys.' + stream + ".write('x'*50000)\n")
                with mock.patch.object(self.module, 'MAX_OUTPUT', 4096):
                    with self.assertRaisesRegex(RuntimeError, 'output limit'):
                        self.invoke(generate)

    def test_preexisting_cancel_does_not_launch(self):
        generate = self.fake("raise AssertionError('should not launch')\n")
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.invoke(generate, stop)
        self.assertFalse((self.home / 'codex-runs').exists())

    def test_cancel_and_timeout_kill_process_group(self):
        # 时间窗口必须比"python 起进程 + 写 pid 文件"宽：0.3s 的窗口在机器忙的时候
        # 连子进程都没起来（判据变成随时间抖动的彩票）。这里给 3 秒，语义不变：
        # 仍然是"取消/超时之后，整棵进程树都被收掉"。
        for mode in ('cancel', 'timeout'):
            with self.subTest(mode=mode):
                generate = self.fake("import os,pathlib,signal,subprocess,sys,time\nchild=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'])\npathlib.Path('child.pid').write_text(str(child.pid))\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(30)\n")
                stop = threading.Event()
                timer = threading.Timer(0.5, stop.set) if mode == 'cancel' else None
                if timer:
                    timer.start()
                    self.addCleanup(timer.cancel)
                with mock.patch.object(self.module, 'TIMEOUT', 3.0):
                    with self.assertRaisesRegex(RuntimeError, 'cancelled' if mode == 'cancel' else 'timed out'):
                        self.invoke(generate, stop)
                runs = sorted((self.home / 'codex-runs').glob('*/child.pid'), key=lambda p: p.stat().st_mtime)
                self.assertTrue(runs, '子进程根本没起来，取消/超时无从验证（是环境太慢，不是产品问题）')
                child_pid = int(runs[-1].read_text())
                # kill(0) may still see an orphan zombie; ps state distinguishes live work.
                import subprocess
                status = subprocess.run(['ps', '-o', 'stat=', '-p', str(child_pid)], capture_output=True, text=True).stdout.strip()
                self.assertTrue(not status or status.startswith('Z'), status)


if __name__ == '__main__':
    unittest.main()

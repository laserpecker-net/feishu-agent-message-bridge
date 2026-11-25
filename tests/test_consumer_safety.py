"""Capability model and local-safety predicates shared by every consumer."""
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consumer_safety as safety


class CapabilityModelTests(unittest.TestCase):
    def test_model_allows_reply_retrieve_execute_and_forbids_damage(self):
        caps = safety.CAPABILITIES
        for allowed in ('reply', 'retrieve', 'execute'):
            self.assertIs(caps[allowed], True, allowed)
        for denied in ('delete', 'destructive', 'secret_access', 'secret_disclosure',
                       'delegate', 'mcp'):
            self.assertIs(caps[denied], False, denied)

    def test_report_is_plain_data_and_hides_no_policy(self):
        report = safety.capabilities_report()
        json.dumps(report)
        self.assertEqual(report['capabilities'], safety.CAPABILITIES)
        self.assertEqual(report['max_answer_chars'], 6000)

    def test_native_windows_is_rejected_before_claiming(self):
        with mock.patch.object(safety.os, 'name', 'nt'), \
                mock.patch.object(safety.sys, 'platform', 'win32'):
            with self.assertRaises(SystemExit) as error:
                safety.require_posix_runtime()
        self.assertIn('WSL2', str(error.exception))

    def test_posix_runtime_is_accepted(self):
        safety.require_posix_runtime()


class DestructiveCommandTests(unittest.TestCase):
    def test_destructive_commands_are_blocked(self):
        commands = [
            'rm -rf build', 'sudo rm -rf /', 'ls; rm -rf y', 'a && rm b', 'x | rm y',
            'rmdir out', 'unlink file', 'shred secrets.doc', 'truncate -s0 data',
            'dd if=/dev/zero of=/dev/disk0', 'mkfs.ext4 /dev/sdb1', 'diskutil eraseDisk x',
            'git clean -fdx', 'git reset --hard HEAD', 'git checkout -- src',
            'git push --force origin main', 'kubectl delete pod x', 'terraform destroy',
            'docker rm -f x', 'docker rmi y', 'docker volume rm z',
            'find . -name "*.log" -delete', 'mysql -e "drop database app"',
            'psql -c "delete from users"', 'echo x > important.txt',
            # Codex reports the shell wrapper verbatim, quotes included.
            "/bin/zsh -lc 'rm -rf build'",
            # Windows/PowerShell equivalents (WSL2 can still touch mounted paths).
            'del /s /q C:\\tmp\\x', 'rd /s /q out', 'Remove-Item -Recurse -Force build',
            'format C:', 'diskpart', 'vssadmin delete shadows', 'shutdown -h now',
            # Regression (review P1): path-prefixed verbs, xargs, find -exec, git
            # restore/checkout-discard, PowerShell without switches and truncation
            # through /dev/null or &>/>&.
            '/bin/rm -rf build', '/usr/bin/rm -rf build', 'xargs rm -rf build',
            'find . -name "*.pyc" -exec rm {} +', 'git restore .', 'git checkout .',
            'Remove-Item notes.md', 'cp /dev/null notes.md', 'tee notes.md </dev/null',
            'echo x &> notes.md', 'echo x >& notes.md', 'git rm notes.md',
            # Review round-3 residual P2: git discard variants and wrapper words.
            'git checkout HEAD -- .', 'git checkout -f .', 'git stash drop', 'git stash clear',
            'git branch -D main', 'git update-ref -d refs/heads/main',
            'busybox rm -rf build', 'coreutils rm -rf build',
            # Review findings: dumping the whole environment is a secret-access path.
            'env', 'env | grep KEY', 'env > /tmp/env.txt', 'printenv',
            # `env` is a transparent prefix: it must not hide the verb after it.
            'env rm -rf build', 'env FOO=1 rm -rf build', 'env -i rm -rf build',
            'xargs env rm -rf x', 'nohup env rm -rf build', 'env FOO=1 printenv', 'env -i',
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(safety.blocked_command(command), command)

    def test_ordinary_read_search_and_build_commands_are_allowed(self):
        commands = [
            'ls -la', 'git status', 'git diff HEAD~1', 'git log --oneline -5',
            'python3 -m unittest discover -s tests -t .', 'npm run build',
            'grep -rn TODO src', "rg foo --glob '*.py'", 'sed -n "1,20p" src/main.py',
            'curl -s https://example.com/api', "python3 -c 'print(1)'",
            'cat README.md', 'echo hello >> log.txt', 'cmd 2>&1 | tee out.log',
            # Regression: a stderr redirect to /dev/null must not look destructive,
            # nor may a `>=` comparison or a `->` arrow inside quoted code.
            'git status --short; echo "---"; ls -d tests 2>/dev/null',
            'command >/dev/null 2>&1', 'python3 -c "print(1>=0)"',
            "python3 -c 'def f() -> int: return 1'",
            # A message argument is data, not a command to execute.
            'git commit -m "fix rm bug"', 'git commit --message="rm cleanup"',
            'git tag -m "delete old tags"', 'git clean -n',
            'git checkout -b feature', 'git checkout main', 'git stash list',
            'git branch -d merged', 'git branch --list',
            # Ordinary uses of the word "env" are data or an assignment prefix, not a dump.
            'grep -rn "env" src', 'env FOO=1 npm test', 'git commit -m "env fix"',
            'env FOO=1 python3 app.py', 'echo env',
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(safety.blocked_command(command), command)

    def test_argv_lists_are_joined_before_checking(self):
        self.assertTrue(safety.blocked_command(['rm', '-rf', 'build']))
        self.assertFalse(safety.blocked_command(['git', 'status']))
        self.assertFalse(safety.blocked_command(['--', '']))
        self.assertFalse(safety.blocked_command(None))
        self.assertFalse(safety.blocked_command(42))


class SecretGuardTests(unittest.TestCase):
    def test_secret_commands_and_relative_paths_are_blocked(self):
        for command in ['cat .env', 'cat ./.env.local', 'cat id_rsa', 'printenv',
                        'env', 'security find-generic-password -a me',
                        'cat ~/.aws/credentials', 'cat ~/.ssh/id_ed25519',
                        'cat /tmp/cert.pem', 'type .env']:
            with self.subTest(command=command):
                self.assertTrue(safety.blocked_command(command), command)

    def test_secret_paths_are_blocked_for_path_fields(self):
        for path in ['.env', '.env.local', '/home/u/.ssh/id_rsa', '/tmp/cert.pem',
                     '/home/u/.aws/credentials', 'deploy/credentials.json',
                     'secrets.yaml', 'id_ed25519', '.netrc', '.git-credentials',
                     '.kube/config', 'terraform.tfstate']:
            with self.subTest(path=path):
                self.assertTrue(safety.blocked_path(path), path)

    def test_ordinary_paths_are_allowed(self):
        for path in ['src/main.py', 'README.md', '.envrc', 'docs/keys.md',
                     '/tmp/build.log', 'notes/credentials.md']:
            with self.subTest(path=path):
                self.assertFalse(safety.blocked_path(path), path)

    def test_leaked_secret_material_is_detected_and_redacted(self):
        leaked = ['key is sk-' + 'a' * 24, 'AKIA' + 'A' * 16,
                  '-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----',
                  'password: hunter2hunter2hunter2hunter2', 'ghp_' + 'a' * 24,
                  'github_pat_' + 'a' * 24, 'xoxb-1234567890-abcdefghij',
                  # Regression (review P0): the key formats actually in use today.
                  'OPENAI_API_KEY=sk-proj-AbCdEf1234567890AbCdEf1234567890',
                  'ANTHROPIC_API_KEY=sk-ant-AbCdEf1234567890AbCdEf1234567890',
                  'key: sk-proj-AbCdEf1234567890AbCdEf1234567890',
                  'AIza' + 'A' * 35, 'aws_secret_access_key = AbCdEf1234567890AbCdEf',
                  'postgres://user:hunter2hunter2@db.example.com/app',
                  'client_secret: AbCdEfGhIjKlMnOpQrStUv',
                  'glpat-' + 'a' * 20, 'sk_live_' + 'a' * 16]
        for text in leaked:
            with self.subTest(text=text):
                self.assertTrue(safety.leaked_secret(text), text)
                self.assertEqual(safety.redact_secret_reply(text), safety.BLOCKED_REPLY)
        for text in ['普通回答', '密码规则见文档', 'token 字段是必填项，长度为 16 以上']:
            with self.subTest(text=text):
                self.assertFalse(safety.leaked_secret(text), text)
                self.assertEqual(safety.redact_secret_reply(text), text)


class ToolPolicyTests(unittest.TestCase):
    def test_delegation_mcp_and_delete_tools_are_denied(self):
        for name in ['Task', 'MCP(server)', 'mcp__github__create_issue', 'subagent',
                     'subagent_fork', 'workflow', 'delete', 'Delete(**)', 'spawn_agent']:
            with self.subTest(name=name):
                self.assertTrue(safety.denied_tool(name), name)

    def test_ordinary_tools_are_allowed(self):
        for name in ['read', 'bash', 'write', 'edit', 'glob', 'grep', 'web_search',
                     'web_fetch', 'job_output', 'command_execution', 'file_change',
                     'agent_message', 'ToolCall', None, '']:
            with self.subTest(name=name):
                self.assertFalse(safety.denied_tool(name), name)

    def test_tool_violation_covers_commands_paths_and_names(self):
        self.assertEqual(safety.tool_violation('bash', {'command': 'rm -rf x'}),
                         'DestructiveCommand')
        self.assertEqual(safety.tool_violation('bash', {'command': 'cat .env'}),
                         'DestructiveCommand')
        self.assertEqual(safety.tool_violation('read', {'path': '/home/u/.ssh/id_rsa'}),
                         'SecretPath')
        self.assertEqual(safety.tool_violation('subagent', {'task': 'x'}), 'DeniedTool')
        self.assertIsNone(safety.tool_violation('bash', {'command': 'git status'}))
        self.assertIsNone(safety.tool_violation('read', {'path': 'src/main.py'}))
        self.assertIsNone(safety.tool_violation('grep', {'pattern': 'TODO'}))
        self.assertEqual(safety.tool_violation('bash', 'rm -rf x'), 'DestructiveCommand')

    def test_delete_operation_detects_file_change_deletes(self):
        self.assertTrue(safety.delete_operation({'type': 'delete'}))
        self.assertTrue(safety.delete_operation({'changes': [{'kind': 'delete', 'path': 'a'}]}))
        self.assertTrue(safety.delete_operation([{'op': 'remove'}]))
        self.assertFalse(safety.delete_operation({'type': 'update', 'path': 'a'}))
        self.assertFalse(safety.delete_operation(None))


class AnswerGuardTests(unittest.TestCase):
    def test_empty_or_non_text_answers_are_rejected(self):
        for value in [None, '', '   ', 3]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safety.finalize_answer(value)

    def test_answer_is_trimmed_bounded_and_redacted(self):
        self.assertEqual(safety.finalize_answer('  hi  '), 'hi')
        self.assertEqual(safety.finalize_answer('x' * safety.MAX_ANSWER_CHARS),
                         'x' * safety.MAX_ANSWER_CHARS)
        with self.assertRaises(ValueError):
            safety.finalize_answer('x' * (safety.MAX_ANSWER_CHARS + 1))
        self.assertEqual(safety.finalize_answer('password: hunter2hunter2hunter2hunter2'),
                         safety.BLOCKED_REPLY)



class WrappedAndMessageArgumentTests(unittest.TestCase):
    """Quoted shell wrappers and -m/--message text must not hide destructive verbs."""

    def test_dangerous_commands_survive_shell_wrappers(self):
        wrapped = [
            ('/bin/zsh -lc "%s"', 'rm -rf build'),
            ('bash -c \'%s\'', 'git checkout .'),
            ('/bin/zsh -lc "%s"', 'git restore .'),
            ('sh -c "%s"', 'git rm notes.md'),
            ('cmd.exe /c "%s"', 'del /s /q C:\\tmp\\x'),
            ('powershell -c "%s"', 'del C:\\tmp\\x'),
            ('wsl.exe %s', 'del /s /q /mnt/c/tmp/x'),
        ]
        for template, command in wrapped:
            text = template % command
            with self.subTest(command=text):
                self.assertTrue(safety.blocked_command(text), text)

    def test_message_arguments_cannot_hide_commands_or_secrets(self):
        blocked = [
            'git commit -m "$(rm -rf build)"',
            'git commit -m "`rm -rf build`"',
            'git commit -m x$(rm -rf y)',
            'git commit -m "OPENAI_API_KEY=sk-proj-AbCdEf1234567890AbCdEf1234567890"',
            'git tag -m "ghp_aaaaaaaaaaaaaaaaaaaaaaaa" v1',
            'git commit -m "x"; rm -rf build',
            'git merge -m "safe" ; rm -rf build',
        ]
        for command in blocked:
            with self.subTest(command=command):
                self.assertTrue(safety.blocked_command(command), command)
        allowed = [
            'git commit -m "fix rm bug"', 'git commit -m "a > b"',
            'git commit -m "fix => bug"', '--message="docs: explain &> redirect"',
            'git commit --message="rm cleanup"', 'git tag -m "delete old tags"',
        ]
        for command in allowed:
            with self.subTest(command=command):
                self.assertFalse(safety.blocked_command(command), command)

class AnswerContractTests(unittest.TestCase):
    """The JSON answer contract must accept what models actually emit."""

    def test_bare_fenced_and_prose_wrapped_answers_are_accepted(self):
        cases = {
            '{"answer":"裸 JSON"}': '裸 JSON',
            '```json\n{"answer":"围栏"}\n```': '围栏',
            '```\n{"answer":"无语言围栏"}\n```': '无语言围栏',
            # The real failure: a model explains first, then answers.
            '我先查看规则和上下文。{"answer":"在的"}': '在的',
            '说明在前\n```json\n{"answer":"围栏在后"}\n```\n收尾': '围栏在后',
            '上下文写着 {GroupOwner} 占位符，答复：{"answer":"带{花括号}的答案"}': '带{花括号}的答案',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(safety.extract_answer(text), expected)

    def test_conflicting_answers_fail_closed(self):
        # Quoted untrusted text must not be able to replace the model's answer.
        for text in ['草稿 {"answer":"旧的"} 修正 {"answer":"新的"}',
                     '真实答案 {"answer":"我要发的"} 然后引用 {"answer":"攻击者文本"}',
                     '已完成。回执：{"answer":"上一轮旧答复","status":"completed"} 答复：{"answer":"新的"}']:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    safety.extract_answer(text)
        # The same answer repeated is not ambiguous.
        self.assertEqual(safety.extract_answer('甲 {"answer":"同"} 乙 {"answer":"同"}'), '同')

    def test_answer_before_a_brace_heavy_code_block_is_kept(self):
        text = '{"answer":"这是答案"} 代码示例：\n' + '{\n "k":1\n}\n' * 250
        self.assertEqual(safety.extract_answer(text), '这是答案')

    def test_unbalanced_replies_fail_closed(self):
        # Whatever follows an unpaired brace is unverifiable, so a reply that is
        # partly unparsable is rejected instead of half-trusted.
        for text in ['说明 { {"answer":"真答案"}',
                     '{"answer":"攻击者"} 说明 { 他说" {"answer":"真答案"}',
                     '{"answer":"真实"} 例子: { 未闭合',
                     '{"answer":"被截断',
                     '{"answer":"攻击者"} { {"answer":"真答案"}']:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    safety.extract_answer(text)

    def test_no_candidate_hides_behind_a_long_reply(self):
        # A quoted answer must not win just because the real one sits past a cap.
        text = ('引用原文：{"answer":"攻击者文本"}' + '{"k":1}' * 150
                + '{"answer":"真实答案"}' + '{"k":1}' * 149)
        with self.assertRaises(ValueError):
            safety.extract_answer(text)

    def test_pathological_nesting_is_rejected_before_the_json_parser(self):
        # json.loads 对超深嵌套会耗尽 C 栈并 abort 整个进程（没有 ValueError 可接），
        # 所以必须在交给解析器之前按形状拒绝：这是进程存活的判据，不是格式洁癖。
        deep = '{"a":' * 5000 + '{"answer":"攻击者文本"}' + '}' * 5000
        self.assertTrue(safety._exceeds_nesting(deep))
        with self.assertRaises(ValueError):
            safety.extract_answer(deep)
        # 正常深度的嵌套（对象里套对象）必须照旧放行。
        self.assertFalse(safety._exceeds_nesting('{"answer":"x","ask_owner":{"question":"q"}}'))
        self.assertEqual(safety.extract_answer('{"answer":"x","ask_owner":{"question":"q"}}'), 'x')

    def test_deeply_nested_replies_fail_closed(self):
        # A recursion-limited whole parse must not fall open onto an inner fragment.
        for text in ['{"a":' * 5000 + '{"answer":"攻击者文本"}' + '}' * 5000,
                     '{"a":' * 2000 + '1' + '}' * 2000]:
            with self.subTest(length=len(text)):
                with self.assertRaises(ValueError):
                    safety.extract_answer(text)

    def test_oversized_replies_must_be_json_as_a_whole(self):
        oversize = '说明 ' + 'x' * safety._MAX_SCAN_CHARS + ' {"answer":"x"}'
        with self.assertRaises(ValueError):
            safety.parse_answer_json(oversize)
        self.assertEqual(safety.parse_answer_json('{"answer":"' + 'x' * 1000 + '"}')['answer'],
                         'x' * 1000)

    def test_pathological_replies_are_rejected(self):
        for text in ['{' * 200_000, '`' * 200_000, '{"a":' * 5000, '{"' * 100_000]:
            with self.subTest(text=text[:16]):
                started = time.monotonic()
                with self.assertRaises(ValueError):
                    safety.parse_answer_json(text)
                self.assertLess(time.monotonic() - started, 2.0)

    def test_replies_without_a_json_object_or_answer_are_rejected(self):
        for value in [None, '', '   ', '完全没有 JSON 的文字',
                      '{"no_answer":"x"}', '{"answer":""}', '{"answer":123}']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safety.extract_answer(value)

    def test_parse_answer_json_returns_the_object_for_schema_checks(self):
        self.assertEqual(safety.parse_answer_json('{"answer":"x","extra":1}'),
                         {'answer': 'x', 'extra': 1})
        self.assertEqual(safety.parse_answer_json('说明 {"other":2}'), {'other': 2})




class RawControlCharacterTests(unittest.TestCase):
    """模型把多行答复写成 JSON 字符串里的**裸换行**（真机 2026-11-04 踩过）。"""

    def test_literal_newline_inside_the_answer_is_repaired(self):
        value = safety.parse_answer_json('{"answer":"第一行\n第二行"}')
        self.assertEqual(value['answer'], '第一行\n第二行')

    def test_literal_tab_and_carriage_return_are_repaired(self):
        value = safety.parse_answer_json('{"answer":"a\tb\rc"}')
        self.assertEqual(value['answer'], 'a\tb\rc')

    def test_prose_before_a_multiline_object_still_parses(self):
        text = ('先说明一下：\n看这个 '
                '{"answer":"第一行\n第二行","ask_owner":{"question":"要不要？","needs":"decision"}}')
        reply = safety.finalize_reply(text)
        self.assertIn('第一行', reply['answer'])
        self.assertEqual(reply['ask_owner']['needs'], 'decision')

    def test_already_escaped_newlines_are_not_double_escaped(self):
        value = safety.parse_answer_json('{"answer":"第一行\\n第二行"}')
        self.assertEqual(value['answer'], '第一行\n第二行')

    def test_control_characters_do_not_soften_structural_checks(self):
        with self.assertRaises(ValueError):
            safety.parse_answer_json('{"answer":"第一行\n第二行"')



class BrokenJsonRepairTests(unittest.TestCase):
    """模型写坏的 JSON 必须能救回来：答案送不出去比格式不干净严重得多。"""

    def test_unescaped_quote_inside_the_answer_is_escaped(self):
        reply = safety.finalize_reply('{"answer":"这已经超出"看"的范围"}')
        self.assertEqual(reply['answer'], '这已经超出"看"的范围')

    def test_unescaped_quotes_do_not_lose_the_escalation(self):
        text = ('{"answer":"超出"看"的范围","ask_owner":{"question":"要不要给"管理员"权限？",'
                '"needs":"decision"}}')
        reply = safety.finalize_reply(text)
        self.assertIn('"看"', reply['answer'])
        self.assertEqual(reply['ask_owner']['needs'], 'decision')
        self.assertIn('管理员', reply['ask_owner']['question'])

    def test_hopeless_json_still_yields_the_answer_and_the_question(self):
        text = '{"answer":"第一段","第二段","ask_owner":{"question":"要不要改？","needs":"decision"}}'
        reply = safety.finalize_reply(text)
        self.assertIn('第一段', reply['answer'])
        self.assertEqual(reply['ask_owner'], {'question': '要不要改？', 'needs': 'decision'})

    def test_text_without_an_answer_is_still_rejected(self):
        with self.assertRaises(ValueError):
            safety.extract_answer('{"question":"没有正文"}')

    def test_salvage_does_not_invent_an_answer_from_prose(self):
        self.assertIsNone(safety._salvage_reply('完全没有 JSON 的一段说明'))


class SkipContractTests(unittest.TestCase):
    """「这条不用回」：只有群消息允许，且必须和回复互斥——回不回只能有一个答案。"""

    def test_group_message_may_skip(self):
        result = safety.finalize_reply(json.dumps({'skip': '对方只是回了一句哦哦'}),
                                       allow_skip=True)
        self.assertEqual(result['skip'], '对方只是回了一句哦哦')
        self.assertIsNone(result['answer'])
        self.assertTrue(result['answer'] is None and result['ask_owner'] is None)

    def test_private_chat_cannot_skip(self):
        with self.assertRaises(ValueError):
            safety.finalize_reply(json.dumps({'skip': '不想回'}), allow_skip=False)

    def test_skip_needs_a_real_reason(self):
        for value in ('', '   ', 42):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safety.finalize_reply(json.dumps({'skip': value}), allow_skip=True)
        with self.assertRaises(ValueError):
            safety.finalize_reply(json.dumps({'skip': '理' * 500}), allow_skip=True)

    def test_skip_and_answer_together_is_ambiguous_and_fails_closed(self):
        for extra in ({'answer': '也回一句'}, {'ask_owner': {'question': '问吗', 'needs': 'fact'}},
                      {'remind': {'at': '2026-11-10 12:00', 'text': '提醒', 'name': '提醒'}}):
            payload = dict(extra, skip='不回')
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):
                    safety.finalize_reply(json.dumps(payload), allow_skip=True)


class EscalationContractTests(unittest.TestCase):
    """`ask_owner` 是"答不了就私聊问本人"的载体：缺省正常，写坏必须失败关闭。"""

    def test_plain_answer_has_no_escalation(self):
        self.assertEqual(safety.finalize_reply('{"answer":"直接答上了"}'),
                         {'answer': '直接答上了', 'ask_owner': None, 'task': None,
                          'task_card': None, 'direct_work': None, 'teach': None,
                          'teach_revert': None, 'remind': None,
                          'remind_cancel': None, 'skip': None, 'dropped': []})

    def test_a_teach_field_that_was_written_but_dropped_is_reported_not_silently_ignored(self):
        # 丢弃本身不能是静默的：运行时据此在回复末尾说"这句没生效"。
        self.assertEqual(safety.finalize_reply(json.dumps(
            {'answer': '好', 'teach': {'kind': 'not-a-kind', 'text': '随便'}}))['dropped'], ['teach'])
        self.assertEqual(safety.finalize_reply(json.dumps(
            {'answer': '好', 'teach_revert': '--last'}))['dropped'], ['teach_revert'])
        self.assertEqual(safety.finalize_reply(json.dumps({'answer': '好'}))['dropped'], [])

    def test_remind_needs_both_an_absolute_time_and_a_body(self):
        good = safety.finalize_reply(json.dumps({'answer': '好', 'remind': {
            'at': '2026-11-06 21:40', 'text': '给张三打电话', 'name': '打电话给张三'}}))['remind']
        self.assertEqual(good, {'at': '2026-11-06 21:40', 'text': '给张三打电话',
                                'name': '打电话给张三'})
        for bad in ({'text': '给张三打电话'}, {'at': '2026-11-06 21:40'},
                    {'at': '', 'text': '给张三打电话'},
                    {'at': 'x' * 60, 'text': '给张三打电话'},
                    {'at': '2026-11-06 21:40', 'text': ''}, 'soon', 7):
            with self.subTest(bad=bad):
                reply = safety.finalize_reply(json.dumps({'answer': '好', 'remind': bad}))
                self.assertIsNone(reply['remind'])
                self.assertIn('remind', reply['dropped'])

    def test_a_bad_remind_name_is_dropped_without_losing_the_reminder(self):
        reply = safety.finalize_reply(json.dumps({'answer': '好', 'remind': {
            'at': '2026-11-06 21:40', 'text': '给张三打电话', 'name': '名' * 40}}))
        self.assertEqual(reply['remind']['text'], '给张三打电话')
        self.assertIsNone(reply['remind']['name'])
        self.assertEqual(reply['dropped'], [])

    def test_remind_cancel_takes_a_name_or_a_number_and_rejects_the_rest(self):
        def cancel(value):
            return safety.finalize_reply(json.dumps({'answer': '好',
                                                     'remind_cancel': value}))['remind_cancel']
        self.assertEqual(cancel('打电话给张三'), '打电话给张三')
        self.assertEqual(cancel(3), '3')
        self.assertIsNone(cancel(''))
        self.assertIsNone(cancel('x' * 60))
        self.assertIsNone(cancel(['a']))

    def test_teach_revert_takes_an_id_last_or_the_entry_name(self):
        # 值会进 argv：认 `t-0007`、`last`，以及清单里那条的名称——回执教本人说的就是
        # 「撤回 汇报先给结论」，模型照回执把名称填进来时必须生效。
        self.assertEqual(safety.normalize_teach_revert(' t-0007 '), 't-0007')
        self.assertEqual(safety.normalize_teach_revert('LAST'), 'last')
        self.assertEqual(safety.normalize_teach_revert('汇报先给结论'), '汇报先给结论')
        for bad in ('../../x', 'a/b', '..', 't-7', '--last', '-r', 'a b', 'a;b', 'x' * 17,
                    '', None, 7, {'id': 't-0007'}):
            with self.subTest(bad=bad):
                self.assertIsNone(safety.normalize_teach_revert(bad))

    def test_the_limits_duplicated_here_still_match_the_bridge_side(self):
        # 消费者不 import bridge（它是独立进程、只依赖 CLI 契约），所以这几个上限只能写死；
        # 写死就要有测试钉住，否则两边各自长大：模型按 16 字给的名字会被 bridge 拒掉。
        from bridge import remind as bridge_remind
        from bridge import teach as bridge_teach
        self.assertEqual(tuple(safety.TEACH_KINDS), tuple(bridge_teach.KINDS))
        self.assertEqual(safety.MAX_TEACH_NAME_CHARS, bridge_teach.MAX_NAME_CHARS)
        # 正文上限允许更严（先挡一道），但不许比 bridge 更松——更松的话这条注定被 CLI 拒。
        self.assertLessEqual(safety.MAX_TEACH_TEXT_CHARS, bridge_teach.MAX_ITEM_CHARS)
        self.assertEqual(safety.MAX_REMIND_TEXT_CHARS, bridge_remind.MAX_TEXT_CHARS)
        self.assertEqual(safety.MAX_REMIND_NAME_CHARS, bridge_remind.MAX_NAME_CHARS)

    def test_escalation_is_parsed_and_normalized(self):
        reply = safety.finalize_reply(json.dumps({
            'answer': '这条要你拍板。',
            'ask_owner': {'question': '  spend_cap\n要不要调高？  ', 'needs': 'decision'}}))
        self.assertEqual(reply['answer'], '这条要你拍板。')
        self.assertEqual(reply['ask_owner'],
                         {'question': 'spend_cap 要不要调高？', 'needs': 'decision'})

    def test_missing_needs_defaults_to_fact(self):
        reply = safety.finalize_reply('{"answer":"x","ask_owner":{"question":"缺什么？"}}')
        self.assertEqual(reply['ask_owner']['needs'], 'fact')

    def test_broken_escalation_fails_closed(self):
        for value in ['{"answer":"x","ask_owner":"spend_cap?"}',
                      '{"answer":"x","ask_owner":{}}',
                      '{"answer":"x","ask_owner":{"question":"   "}}',
                      '{"answer":"x","ask_owner":{"question":"q","needs":"whatever"}}']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safety.finalize_reply(value)

    def test_question_that_is_too_long_is_rejected(self):
        long = 'a' * (safety.MAX_ASK_CHARS + 1)
        with self.assertRaises(ValueError):
            safety.finalize_reply(json.dumps({'answer': 'x', 'ask_owner': {'question': long}}))

    def test_question_with_a_secret_is_dropped_but_the_answer_survives(self):
        reply = safety.finalize_reply(json.dumps({
            'answer': '看到了，先说结论。',
            'ask_owner': {'question': '这个 key ' + 'sk-' + 'a' * 24 + ' 要不要换？'}}))
        self.assertEqual(reply['answer'], '看到了，先说结论。')
        self.assertIsNone(reply['ask_owner'])

    def test_answer_is_still_guarded_like_before(self):
        with self.assertRaises(ValueError):
            safety.finalize_reply('{"ask_owner":{"question":"q"}}')


if __name__ == '__main__':
    unittest.main()

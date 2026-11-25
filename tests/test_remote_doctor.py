import base64
import io
import json
import contextlib
import unittest
from unittest.mock import patch

from tools import remote_doctor


class CommandSizeTests(unittest.TestCase):
    """Windows 的 cmd 命令行上限 8191 字符；-EncodedCommand 是 base64(UTF-16LE)，
    脚本稍微一长就会「命令行太长」，体检直接失败。"""

    def test_windows_command_fits_cmd_limit(self):
        self.assertLess(len(remote_doctor.windows_command()), 7800)


class DecodeTests(unittest.TestCase):
    """远端输出可能不是 UTF-8：中文 Windows 的原生命令走 936 代码页。"""

    def test_utf8_passthrough(self):
        self.assertEqual('a\u4e2d b', remote_doctor._decode('a\u4e2d b'.encode('utf-8')))

    def test_gbk_fallback(self):
        raw = 'WSL \u7248\u672c: 3.0.3.0'.encode('gb18030')
        with self.assertRaises(UnicodeDecodeError):
            raw.decode('utf-8')
        self.assertIn('3.0.3.0', remote_doctor._decode(raw))

    def test_undecodable_bytes_do_not_raise(self):
        raw = b'ok\x85\xff\xfe rest'
        self.assertTrue(remote_doctor._decode(raw).endswith('rest'))

    def test_none_and_str(self):
        self.assertEqual('', remote_doctor._decode(None))
        self.assertEqual('x', remote_doctor._decode('x'))


def sentinel(pairs, prefix='@@'):
    return '\n'.join('%s%s\n%s' % (prefix, name, value) for name, value in pairs)


HEALTHY_WSL_FACTS = sentinel([
    ('uname', 'Linux'), ('kernel', '5.15.153.1-microsoft-standard-WSL2'),
    ('osrelease', 'NAME="Ubuntu"'), ('uid', '1000'), ('user', 'alice'),
    ('wslconf', '[boot]\nsystemd=true'), ('systemd', 'ok'), ('xdg', '/run/user/1000'),
    ('p1', 'systemd'), ('python3', 'Python 3.9.18'), ('git', 'git version 2.43.0'),
    ('wsl_exe', 'present'), ('tailscale', '{"Self":{"DNSName":"wsl-box.tailnet.ts.net"}}'),
])

HEALTHY_WINDOWS_FACTS = sentinel([
    ('os', 'Windows 11 Pro 10.0.26100'), ('ps', 'Desktop 5.1.26100.1'),
    ('sshd', 'Running Automatic'), ('wslVersion', 'WSL version: 2.3.26.0'),
    ('wslList', '* Ubuntu    Running         2'), ('git', 'git version 2.43.0'),
    ('tailscale', '{"Self":{"DNSName":"win-box.tailnet.ts.net"}}'),
    ('wslfacts', HEALTHY_WSL_FACTS.replace('@@', '%%')),
])

BROKEN_WINDOWS_FACTS = sentinel([
    ('os', 'Windows 10 Pro 10.0.19045'), ('ps', 'Desktop 5.1.19041.1'),
    ('sshd', 'Stopped Manual'), ('wslVersion', 'WSL version: 1.0.0.0'),
    ('wslList', '* Legacy    Stopped         1'), ('git', 'missing'),
    ('tailscale', 'missing'),
    ('wslfacts', sentinel([('uid', '0'), ('user', 'root'), ('wslconf', 'missing'),
                           ('systemd', 'fail'), ('xdg', ''), ('p1', 'init'),
                           ('python3', 'missing')], prefix='%%')),
])


class FakeRunner(remote_doctor.Runner):
    def __init__(self, outputs, host='win-box'):
        super().__init__(host)
        self.outputs = outputs
        self.commands = []

    def run(self, remote_command):
        self.commands.append(remote_command)
        for needle, value in self.outputs.items():
            if needle in remote_command:
                if isinstance(value, Exception):
                    raise value
                return value
        raise remote_doctor.RemoteError('no canned output for %r' % remote_command[:60])


class ScriptTests(unittest.TestCase):
    def test_posix_command_round_trips_to_the_script(self):
        command = remote_doctor.posix_command()
        encoded = command.split('printf %s ')[1].split(' | base64 -d')[0]
        self.assertEqual(base64.b64decode(encoded).decode('utf-8'), remote_doctor.POSIX_SCRIPT)

    def test_windows_command_is_utf16le_encoded_powershell(self):
        command = remote_doctor.windows_command()
        self.assertIn('-EncodedCommand', command)
        encoded = command.rsplit(' ', 1)[1]
        script = base64.b64decode(encoded).decode('utf-16-le')
        self.assertIn('@@sshd', script)
        self.assertIn('Get-Service sshd', script)
        embedded = base64.b64encode(remote_doctor.POSIX_SCRIPT.encode('utf-8')).decode('ascii')
        self.assertIn(embedded, script)

    def test_scripts_only_read_state(self):
        for script in (remote_doctor.POSIX_SCRIPT, remote_doctor.WINDOWS_SCRIPT):
            lowered = script.replace('\x60', '').lower()
            for banned in remote_doctor.BANNED:
                self.assertNotIn(banned.lower(), lowered,
                                 '%s 出现在只读脚本里' % banned)

    def test_runner_builds_a_batch_mode_ssh_command(self):
        runner = remote_doctor.Runner('win-box', user='alice', port=2222, key='~/.ssh/id_ed25519',
                                      options=['StrictHostKeyChecking=accept-new'], timeout=7)
        argv = runner.argv('uname -s')
        self.assertEqual(argv[0], 'ssh')
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('ConnectTimeout=7', argv)
        self.assertIn('StrictHostKeyChecking=accept-new', argv)
        self.assertEqual(argv[-2:], ['alice@win-box', 'uname -s'])
        self.assertIn('2222', argv)
        self.assertIn('~/.ssh/id_ed25519', argv)


class ParsingTests(unittest.TestCase):
    def test_parse_blocks_keeps_multiline_values(self):
        blocks = remote_doctor.parse_blocks(sentinel([('a', 'one\ntwo'), ('b', 'x')]))
        self.assertEqual(blocks, {'a': 'one\ntwo', 'b': 'x'})

    def test_wsl_list_parsing_handles_default_marker_and_header(self):
        text = '  NAME      STATE           VERSION\n* Ubuntu    Running         2\n  Debian    Stopped         1\n'
        self.assertEqual(remote_doctor._wsl_version_numbers(text), [('Ubuntu', 2), ('Debian', 1)])

    def test_wslconf_systemd_detection_is_section_scoped(self):
        self.assertTrue(remote_doctor._wslconf_has_systemd('[boot]\nsystemd = true\n'))
        self.assertFalse(remote_doctor._wslconf_has_systemd('[interop]\nsystemd=true\n'))
        self.assertFalse(remote_doctor._wslconf_has_systemd('missing'))
        self.assertFalse(remote_doctor._wslconf_has_systemd('[boot]\nappendWindowsPath=false\n'))


class DetectTests(unittest.TestCase):
    def test_detects_linux_from_uname(self):
        self.assertEqual(remote_doctor.detect_target(FakeRunner({'uname': 'Linux\n'})), 'linux')

    def test_falls_back_to_powershell_for_windows(self):
        runner = FakeRunner({'uname': remote_doctor.RemoteError('fail'),
                             'PSEdition': 'Desktop'})
        self.assertEqual(remote_doctor.detect_target(runner), 'windows')

    def test_unknown_host_raises(self):
        runner = FakeRunner({'uname': remote_doctor.RemoteError('fail'),
                             'PSEdition': remote_doctor.RemoteError('fail')})
        with self.assertRaises(remote_doctor.RemoteError):
            remote_doctor.detect_target(runner)


class VerdictTests(unittest.TestCase):
    def test_healthy_windows_has_no_failure(self):
        facts = remote_doctor.collect(FakeRunner({'powershell': HEALTHY_WINDOWS_FACTS}), 'windows')
        findings = remote_doctor.verdicts('windows', facts)
        failed = [finding for finding in findings if finding['status'] == 'fail']
        self.assertEqual(failed, [], failed)

    def test_broken_windows_reports_actionable_failures(self):
        facts = remote_doctor.collect(FakeRunner({'powershell': BROKEN_WINDOWS_FACTS}), 'windows')
        findings = {finding['id']: finding for finding in remote_doctor.verdicts('windows', facts)}
        self.assertEqual(findings['win.sshd']['status'], 'fail')
        self.assertEqual(findings['win.distro']['status'], 'fail')
        self.assertEqual(findings['win.uid']['status'], 'fail')
        self.assertEqual(findings['win.wslconf']['status'], 'fail')
        self.assertEqual(findings['win.systemd']['status'], 'fail')
        self.assertIn('Set-Service', findings['win.sshd']['hint'])
        self.assertIn('wsl --set-version', findings['win.distro']['hint'])

    def test_wsl_host_checks_systemd_and_xdg(self):
        facts = remote_doctor.collect(FakeRunner({'sh -c': HEALTHY_WSL_FACTS}), 'linux')
        findings = {finding['id']: finding for finding in remote_doctor.verdicts('linux', facts)}
        self.assertEqual(findings['posix.wsl']['status'], 'ok')
        self.assertEqual(findings['posix.systemd']['status'], 'ok')
        self.assertEqual(findings['posix.xdg']['status'], 'ok')
        self.assertEqual(findings['posix.uid']['status'], 'ok')

    def test_missing_xdg_is_only_a_warning(self):
        facts = remote_doctor.collect(FakeRunner({'sh -c': sentinel([
            ('uname', 'Linux'), ('kernel', '5.15.0-microsoft-standard-WSL2'), ('uid', '1000'),
            ('wslconf', '[boot]\nsystemd=true'), ('systemd', 'ok'), ('xdg', ''),
            ('p1', 'systemd'), ('python3', 'Python 3.9.18')])}), 'linux')
        findings = {finding['id']: finding for finding in remote_doctor.verdicts('linux', facts)}
        self.assertEqual(findings['posix.xdg']['status'], 'warn')


class WslTargetTests(unittest.TestCase):
    """`--target wsl`：ssh 落在 Windows 上，必须复用 Windows 采集脚本里的 wsl.exe 子探测。"""

    def test_collect_keeps_tailscale_inside_wsl(self):
        facts = remote_doctor.collect(FakeRunner({'powershell': HEALTHY_WINDOWS_FACTS}), 'windows')
        self.assertIn('wsl-box', facts['wsl']['tailscale'])

    def test_wsl_target_uses_windows_command(self):
        stream = io.StringIO()
        with patch('builtins.print', side_effect=lambda *a, **k: stream.write(' '.join(map(str, a)) + '\n')):
            code = remote_doctor.main(['--host', 'win-box', '--target', 'wsl', '--print-commands'])
        self.assertEqual(code, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload['target'], 'wsl')
        self.assertIn('-EncodedCommand', payload['argv'][-1])

    def test_wsl_target_reports_posix_verdicts(self):
        windows_facts = remote_doctor.collect(FakeRunner({'powershell': HEALTHY_WINDOWS_FACTS}), 'windows')
        stream = io.StringIO()
        with patch.object(remote_doctor, 'collect', return_value=windows_facts), \
                contextlib.redirect_stdout(stream):
            code = remote_doctor.main(['--host', 'win-box', '--target', 'wsl', '--strict'])
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload['target'], 'wsl')
        self.assertEqual(code, 0)
        ids = {finding['id'] for finding in payload['findings']}
        self.assertIn('posix.systemd', ids)
        self.assertNotIn('win.sshd', ids)
        self.assertEqual(payload['facts']['user'], 'alice')

    def test_wsl_target_with_empty_facts_returns_2(self):
        windows_facts = remote_doctor.collect(FakeRunner({'powershell': BROKEN_WINDOWS_FACTS}), 'windows')
        windows_facts['wsl'] = {}
        stream = io.StringIO()
        with patch.object(remote_doctor, 'collect', return_value=windows_facts), \
                contextlib.redirect_stdout(stream):
            code = remote_doctor.main(['--host', 'win-box', '--target', 'wsl'])
        self.assertEqual(code, 2)
        self.assertIn('WSL', json.loads(stream.getvalue())['error'])


class TailscaleSummaryTests(unittest.TestCase):
    def test_summarises_version_backend_ip_dns_and_health(self):
        text = json.dumps({'Version': '1.104.4-t938213198', 'BackendState': 'Running',
                           'Self': {'DNSName': 'box.tailnet.ts.net.', 'TailscaleIPs': ['100.1.2.3']},
                           'Health': []})
        self.assertEqual(remote_doctor._tailscale_summary(text),
                         'v1.104.4 backend=Running ip=100.1.2.3 dns=box.tailnet.ts.net health=0')

    def test_health_problems_are_counted(self):
        text = json.dumps({'BackendState': 'NeedsLogin', 'Health': ['a', 'b']})
        self.assertIn('health=2', remote_doctor._tailscale_summary(text))

    def test_missing_or_garbage_is_empty_or_first_line(self):
        self.assertEqual(remote_doctor._tailscale_summary('missing'), '')
        self.assertEqual(remote_doctor._tailscale_summary(''), '')
        self.assertEqual(remote_doctor._tailscale_summary('not json\nsecond'), 'not json')


class CliTests(unittest.TestCase):
    def test_print_commands_does_not_connect(self):
        stream = io.StringIO()
        with patch('builtins.print', side_effect=lambda *a, **k: stream.write(' '.join(map(str, a)) + '\n')):
            code = remote_doctor.main(['--host', 'win-box', '--target', 'windows', '--print-commands'])
        self.assertEqual(code, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload['target'], 'windows')
        self.assertIn('-EncodedCommand', payload['argv'][-1])

    def test_main_returns_1_when_a_check_fails(self):
        facts = remote_doctor.collect(FakeRunner({'powershell': BROKEN_WINDOWS_FACTS}), 'windows')
        stream = io.StringIO()
        with patch.object(remote_doctor, 'collect', return_value=facts), \
                contextlib.redirect_stdout(stream):
            code = remote_doctor.main(['--host', 'win-box', '--target', 'windows'])
        self.assertEqual(code, 1)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload['host'], 'win-box')
        self.assertGreaterEqual(payload['summary']['fail'], 3)

    def test_ssh_failure_returns_2_with_a_json_error(self):
        stream = io.StringIO()
        with patch.object(remote_doctor, 'collect', side_effect=remote_doctor.RemoteError('connection refused')), \
                contextlib.redirect_stdout(stream):
            code = remote_doctor.main(['--host', 'win-box', '--target', 'windows'])
        self.assertEqual(code, 2)
        payload = json.loads(stream.getvalue())
        self.assertFalse(payload['ok'])
        self.assertIn('connection refused', payload['error'])

    def test_markdown_report_is_written(self):
        import tempfile
        from pathlib import Path
        facts = remote_doctor.collect(FakeRunner({'powershell': HEALTHY_WINDOWS_FACTS}), 'windows')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.md'
            with patch.object(remote_doctor, 'collect', return_value=facts), \
                    contextlib.redirect_stdout(io.StringIO()):
                remote_doctor.main(['--host', 'win-box', '--target', 'windows', '--markdown', str(path)])
            text = path.read_text()
        self.assertIn('# remote-doctor 只读体检', text)
        self.assertIn('| 检查 | 状态 | 事实 | 建议 |', text)
        self.assertIn('只读体检：未在对端写文件', text)


if __name__ == '__main__':
    unittest.main()

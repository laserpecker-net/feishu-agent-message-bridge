import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

from bridge import host


class HostPolicyTests(unittest.TestCase):
    def test_macos_and_linux_homes_are_independent(self):
        with patch.object(host, 'SYSTEM', 'darwin'):
            self.assertEqual(host.default_home().parts[-2:], ('Application Support', 'FeishuPersonalAgent'))
            self.assertEqual(host.service_path(host.LABEL).suffix, '.plist')
            # 优先使用跑 install.py 的那个解释器；显式传入时原样使用。
            self.assertEqual(host.launcher_shebang('/bin/sh'), '#!/bin/sh')
            self.assertEqual(host.launcher_shebang('/nonexistent/python3'), '#!/usr/bin/python3')
            self.assertEqual(host.launcher_shebang(), '#!' + str(Path(host.sys.executable).resolve()))
        with patch.object(host, 'SYSTEM', 'linux'):
            self.assertEqual(host.default_home().name, '.feishu-personal-agent')
            self.assertEqual(host.service_path(host.LABEL).suffix, '.service')
            self.assertEqual(host.launcher_shebang(), '#!/usr/bin/env python3')

    def test_macos_path_is_derived_from_the_installing_user(self):
        with patch.object(host, 'SYSTEM', 'darwin'), \
                patch.object(host, 'current_user', return_value='alice'), \
                patch.object(host.Path, 'home', return_value=Path('/home/alice')):
            path = host.default_path()
            env = host.service_definition(Path('/tmp/home'), Path('/tmp/bin/feishu-bridge'))
        self.assertTrue(path.startswith('/home/alice/.local/bin:'))
        self.assertIn('/home/alice/.npm-global/bin', path)
        # PATH 只能由「当前用户」推出来；漏进 macOS 风格的绝对家目录（/Users/xxx）就算错。
        self.assertNotIn('/Users/', path)
        self.assertEqual(env['EnvironmentVariables']['USER'], 'alice')
        self.assertEqual(env['EnvironmentVariables']['HOME'], '/home/alice')

    def test_current_user_never_falls_back_to_the_repository_author(self):
        with patch.dict(os.environ, {'USER': 'bob'}, clear=False):
            self.assertEqual(host.current_user(), 'bob')
        with patch.dict(os.environ, {'USER': '', 'LOGNAME': ''}, clear=False), \
                patch.object(host.getpass, 'getuser', return_value='carol'):
            self.assertEqual(host.current_user(), 'carol')

    def test_native_windows_is_rejected_with_wsl_pointer(self):
        with patch.object(host, 'SYSTEM', 'windows'):
            with self.assertRaises(SystemExit) as caught:
                host.require_supported()
        self.assertIn('WSL2', str(caught.exception))
        self.assertIn('INSTALL-WINDOWS.md', str(caught.exception))

    def test_unknown_platform_is_rejected(self):
        with patch.object(host, 'SYSTEM', 'freebsd'):
            with self.assertRaises(SystemExit):
                host.require_supported()

    def test_systemd_unit_is_self_contained(self):
        with patch.object(host, 'SYSTEM', 'linux'):
            unit = host.service_definition(Path('/tmp/bridge-home'), Path('/tmp/bin/feishu-bridge'))
        self.assertIn('ExecStart=/tmp/bin/feishu-bridge foreground', unit)
        self.assertIn('WorkingDirectory=/tmp/bridge-home', unit)
        self.assertIn('WantedBy=default.target', unit)
        self.assertIn('Restart=on-failure', unit)
        self.assertNotIn(str(Path.home() / 'Library/LaunchAgents'), unit)

    def test_macos_plist_keeps_program_arguments(self):
        with patch.object(host, 'SYSTEM', 'darwin'):
            payload = host.service_definition(Path('/tmp/home'), Path('/tmp/bin/feishu-bridge'),
                                              label='com.example.probe', args=('status',))
        self.assertEqual(payload['ProgramArguments'], ['/tmp/bin/feishu-bridge', 'status'])
        self.assertEqual(payload['Label'], 'com.example.probe')
        self.assertEqual(payload['EnvironmentVariables']['PATH'], host.default_path())

    def test_linux_unit_resets_restart_limit_and_uses_systemd_logs(self):
        with patch.object(host, 'SYSTEM', 'linux'):
            unit = host.service_definition(Path('/tmp/bridge-home'), Path('/tmp/bin/feishu-bridge'))
        self.assertIn('StartLimitIntervalSec=0', unit)
        self.assertIn('systemd.stdout.log', unit)
        self.assertNotIn('launchd', unit)

    def test_linux_unit_quotes_paths_with_spaces(self):
        with patch.object(host, 'SYSTEM', 'linux'):
            unit = host.service_definition(Path('/tmp/a b'), Path('/tmp/x y/bin'))
        self.assertIn('WorkingDirectory="/tmp/a b"', unit)
        self.assertIn('ExecStart="/tmp/x y/bin" foreground', unit)

    def test_linux_binary_defaults_do_not_require_macos_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'lark-cli'
            binary.write_text('#!/bin/sh\n')
            with patch.object(host, 'SYSTEM', 'linux'), \
                    patch.object(host.Path, 'home', return_value=Path(directory)), \
                    patch.object(host.shutil, 'which', side_effect=lambda name: str(binary) if name == 'lark-cli' else None):
                self.assertEqual(host.default_lark_binary(), str(binary))
                self.assertTrue(host.default_cursor_binary().endswith('cursor-agent'))


class ServiceStateTests(unittest.TestCase):
    """托管层的读写原语：安装器用它判断"升级前在跑吗"、用它重载单元。"""

    def test_macos_state_reports_pid_from_launchctl_print(self):
        done = unittest.mock.Mock(returncode=0, stdout='\tstate = running\n\tpid = 4242\n', stderr='')
        with patch.object(host, 'SYSTEM', 'darwin'), \
                patch.object(host.subprocess, 'run', return_value=done) as run:
            state = host.service_state(host.LABEL)
        self.assertEqual(state, {'loaded': True, 'pid': 4242})
        self.assertEqual(run.call_args[0][0][1], 'print')

    def test_macos_state_is_not_loaded_when_launchctl_does_not_know_the_label(self):
        done = unittest.mock.Mock(returncode=113, stdout='', stderr='Could not find service')
        with patch.object(host, 'SYSTEM', 'darwin'), \
                patch.object(host.subprocess, 'run', return_value=done):
            self.assertEqual(host.service_state(host.LABEL), {'loaded': False, 'pid': None})

    def test_linux_state_prefers_the_user_scope_then_system(self):
        loaded = unittest.mock.Mock(returncode=0, stdout='LoadState=not-found\nActiveState=inactive\nMainPID=0\n')
        system = unittest.mock.Mock(returncode=0, stdout='LoadState=loaded\nActiveState=active\nMainPID=77\n')
        with patch.object(host, 'SYSTEM', 'linux'), \
                patch.object(host.subprocess, 'run', side_effect=[loaded, system]):
            state = host.service_state(host.LABEL)
        self.assertEqual(state['loaded'], True)
        self.assertEqual(state['pid'], 77)
        self.assertEqual(state['scope'], 'system')

    def test_reload_boots_out_then_bootstraps_and_waits_for_a_pid(self):
        calls = []
        state_results = [
            {'loaded': True, 'pid': 1},     # 安装前：已加载 → 需要 bootout
            {'loaded': False, 'pid': None},  # bootout 之后
            {'loaded': True, 'pid': 99},    # bootstrap 之后
        ]

        def fake_run(command, **kwargs):
            calls.append(command)
            return unittest.mock.Mock(returncode=0, stdout='', stderr='')

        with patch.object(host, 'SYSTEM', 'darwin'), \
                patch.object(host, 'service_state', side_effect=state_results), \
                patch.object(host.subprocess, 'run', side_effect=fake_run):
            result = host.reload_service(host.LABEL, '/tmp/x.plist')
        self.assertTrue(result['ok'])
        self.assertEqual(result['pid'], 99)
        self.assertEqual([item[1] for item in calls], ['bootout', 'bootstrap'])

    def test_reload_reports_a_failed_bootstrap_instead_of_pretending(self):
        def fake_run(command, **kwargs):
            return unittest.mock.Mock(returncode=0 if command[1] == 'bootout' else 5,
                                      stdout='', stderr='Bootstrap failed: 5: Input/output error')

        with patch.object(host, 'SYSTEM', 'darwin'), \
                patch.object(host, 'service_state', return_value={'loaded': False, 'pid': None}), \
                patch.object(host.subprocess, 'run', side_effect=fake_run):
            result = host.reload_service(host.LABEL, '/tmp/x.plist')
        self.assertFalse(result['ok'])
        self.assertIn('Input/output error', result['error'])

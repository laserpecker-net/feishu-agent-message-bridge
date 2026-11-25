"""角色边界（回话端只说话 / 干活端才动手）的判据。

事实源：CONTRACTS.md §角色定义。
为什么要有这组用例：回话端两次越界自己动手（2026-11-19 在别的仓库提交、2026-11-20 把
文件写进自己的 workspace），当时只靠提示词，账本里零痕迹。这里的判据盯的是"权限层"，
提示词层只做一致性抽查。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_prompt
import consumer_safety
from bridge import report as report_module
from bridge import workbench


def _overlay_lines(role):
    return workbench.overlay_text('env', role).splitlines()


class SandboxByRoleTests(unittest.TestCase):
    def test_replier_is_read_only_and_worker_is_workspace_write(self):
        self.assertEqual(workbench.sandbox_mode('replier'), 'read-only')
        self.assertEqual(workbench.sandbox_mode('worker'), 'workspace-write')
        self.assertIn('    mode: read-only', _overlay_lines('replier'))
        self.assertIn('    mode: workspace-write', _overlay_lines('worker'))

    def test_two_overlays_differ_only_in_the_sandbox_lines(self):
        replier = _overlay_lines('replier')
        worker = _overlay_lines('worker')
        self.assertEqual(len(replier), len(worker))
        differing = [(a, b) for a, b in zip(replier, worker) if a != b]
        # 只允许 sandbox-policy.mode 与 consumer preset 的 sandbox 两行不同
        self.assertEqual(len(differing), 2, differing)
        self.assertIn(('    mode: read-only', '    mode: workspace-write'), differing)
        self.assertIn(('        sandbox: read-only', '        sandbox: workspace-write'), differing)
        # 禁用工具与审批策略两个角色完全一致
        for line in ('- id: tool-subagent', '- id: tool-workflow'):
            self.assertIn(line, replier)
            self.assertIn(line, worker)

    def test_credential_rows_are_identical_for_both_roles(self):
        stored_replier = workbench.overlay_text('stored', 'replier')
        stored_worker = workbench.overlay_text('stored', 'worker')
        for text in (stored_replier, stored_worker):
            for row in ('- id: credentials', '- id: authorization', '- id: deepseek-account'):
                self.assertIn(row, text)
            self.assertEqual(text.count('disabled: false'), 3)
        with_env = workbench.overlay_text('env', 'replier')
        self.assertNotIn('disabled: false', with_env)

    def test_unknown_role_fails_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            workbench.sandbox_mode('admin')
        with self.assertRaises(ValueError):
            workbench.overlay_text('env', 'nobody')
        with self.assertRaises(ValueError):
            workbench.Workbench(role='admin')

    def test_default_role_keeps_old_callers_working(self):
        # 老调用（不传 role）仍然是干活端语义，避免既有入口静默变只读
        self.assertIn('    mode: workspace-write', workbench.overlay_text('env').splitlines())
        self.assertEqual(workbench.Workbench().role, 'worker')

    def test_workbench_carries_the_role_and_shows_it_in_describe(self):
        replier = workbench.Workbench(role='replier')
        self.assertEqual(replier.role, 'replier')
        rows = dict((row[0], row) for row in replier.describe())
        self.assertIn('role', rows)
        self.assertEqual(rows['role'][1], 'replier')
        self.assertTrue(rows['role'][2])

    def test_write_overlay_uses_the_role(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'overlay.yml'
            workbench.write_overlay(path, 'env', 'replier')
            text = path.read_text(encoding='utf-8')
            self.assertIn('    mode: read-only', text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_consumer_and_worker_declare_their_role_explicitly(self):
        root = Path(__file__).resolve().parents[1]
        consumer = (root / 'dsh-consumer.py').read_text(encoding='utf-8')
        worker = (root / 'bridge/worker.py').read_text(encoding='utf-8')
        self.assertIn("CONSUMER_ROLE = 'replier'", consumer)
        self.assertIn('write_overlay(run_dir / OVERLAY_NAME, credential, CONSUMER_ROLE)', consumer)
        self.assertIn("role='worker'", worker)


class PromptRoleTests(unittest.TestCase):
    def test_replier_prompt_forbids_side_effects(self):
        prompt = agent_prompt.build_prompt({'text': 'q', 'context': []}, '/tmp')
        self.assertIn('只说话，不动手', prompt)
        self.assertIn('一律不要自己动手', prompt)
        self.assertIn('不能写文件', prompt)
        self.assertIn(report_module.ROLE_OFFLOAD_LINE, prompt)

    def test_worker_prompt_does_not_carry_the_replier_section(self):
        prompt = agent_prompt.build_prompt({'text': 'q', 'context': []}, '/tmp', role='worker')
        self.assertNotIn('只说话，不动手', prompt)
        self.assertIn('执行并完成任务', prompt)

    def test_offload_line_is_defined_once(self):
        root = Path(__file__).resolve().parents[1]
        hits = []
        for path in list(root.glob('*.py')) + list((root / 'bridge').glob('*.py')):
            if path.name == 'report.py':
                continue
            text = path.read_text(encoding='utf-8')
            if report_module.ROLE_OFFLOAD_LINE in text:
                hits.append(path.name)
        self.assertEqual(hits, [], '那句人话只该在 report.py 里定义一次')

    def test_replier_capability_model_does_not_execute(self):
        self.assertIs(consumer_safety.ROLE_CAPABILITIES['replier']['execute'], False)
        self.assertIs(consumer_safety.ROLE_CAPABILITIES['worker']['execute'], True)
        report = consumer_safety.capabilities_report('replier')
        self.assertEqual(report['role'], 'replier')
        self.assertIs(report['capabilities']['execute'], False)


if __name__ == '__main__':
    unittest.main()

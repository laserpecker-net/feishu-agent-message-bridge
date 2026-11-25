"""tools/windows_host.py：按人定位同事机（台账 + live 覆盖 + 通道命令）。

对应 docs/windows-hosts.md：台账是「人 → 机器 → 通道」的唯一来源。
这里钉住三件事：匹配能命中（中文/机名/IP）、live 覆盖能覆盖、读不到 live 时降级不报错。
"""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import windows_host

HEADER = ','.join(windows_host.FIELDS)
# 夹具必须是**编造的**：真实台账含同事姓名与飞书 open_id，那份只在本机（.gitignore），
# 测试一旦引用真实数据，个人信息就跟着测试进了仓库。
ROW = (
    '示例同事,示例飞书,ou_example000000000000000000000000,example-win,'
    'example-win.tailxxxx.ts.net,100.64.0.1,winuser,Ubuntu,wsluser,'
    'Windows 11 家庭版,2026-11-05,0000000000000000,2027-05-01,在线,模板机型'
)


def _write(body):
    d = Path(tempfile.mkdtemp())
    p = d / 'hosts.csv'
    p.write_text(body, encoding='utf-8')
    return p


class RegistryTests(unittest.TestCase):
    def test_load_skips_blank_lines_and_keeps_all_fields(self):
        rows = windows_host.load_registry(_write(f'{HEADER}\n{ROW}\n\n'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['magicdns'], 'example-win.tailxxxx.ts.net')
        self.assertEqual(rows[0]['feishu_open_id'], 'ou_example000000000000000000000000')

    def test_bom_file_is_accepted(self):
        p = _write(f'{HEADER}\n{ROW}\n')
        p.write_text(f'\ufeff{HEADER}\n{ROW}\n', encoding='utf-8')
        self.assertEqual(len(windows_host.load_registry(p)), 1)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            windows_host.load_registry(Path(tempfile.mkdtemp()) / 'nope.csv')

    def test_missing_column_raises(self):
        body = f'{HEADER}\n{ROW}\n'.replace(',feishu_open_id', '')
        with self.assertRaises(ValueError):
            windows_host.load_registry(_write(body))


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.rows = windows_host.load_registry(_write(f'{HEADER}\n{ROW}\n'))

    def test_match_by_chinese_name(self):
        self.assertEqual(len(windows_host.match(self.rows, '示例同事')), 1)

    def test_match_by_feishu_name_and_machine_and_ip_and_ssh_user(self):
        for query in ('示例飞书', 'example-win', 'example-win.tailxxxx.ts.net', '100.64.0.1', 'winuser'):
            self.assertEqual(len(windows_host.match(self.rows, query)), 1, query)

    def test_match_is_case_insensitive(self):
        self.assertEqual(len(windows_host.match(self.rows, 'MAGICDNS')), 0)
        self.assertEqual(len(windows_host.match(self.rows, 'EXAMPLE-WIN')), 1)

    def test_unknown_and_empty_query_return_nothing(self):
        self.assertEqual(windows_host.match(self.rows, '不存在的人'), [])
        self.assertEqual(windows_host.match(self.rows, '  '), [])


class ExpiryTests(unittest.TestCase):
    NOW = datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)

    def test_disabled(self):
        self.assertIn('DISABLED', windows_host.describe_expiry('0001-01-01T00:00:00Z', self.NOW))

    def test_absolute_with_days_left(self):
        text = windows_host.describe_expiry('2027-05-01T03:42:35Z', self.NOW)
        self.assertIn('2027-05-01', text)
        self.assertIn('178 天后', text)

    def test_rfc3339_nanoseconds_are_understood(self):
        # Tailscale 的 KeyExpiry 是最多 9 位小数；Python 3.9 的 fromisoformat 只认 3/6 位，
        # 不处理就会把真实到期日显示成「未知」。
        text = windows_host.describe_expiry('2027-05-01T06:55:42.897761943Z', self.NOW)
        self.assertIn('2027-05-01', text)
        self.assertIn('天后', text)

    def test_ledger_word_disabled(self):
        # 台账里手写的 DISABLED（docs/windows-hosts.md §2 的词汇）与 Tailscale 零值 0001-… 同义：
        # 之前只认 0001，登记值 DISABLED 会被 fromisoformat 判成「未知」，等于把最该看清的一列看丢。
        self.assertIn('DISABLED', windows_host.describe_expiry('DISABLED', self.NOW))
        self.assertIn('DISABLED', windows_host.describe_expiry(' disabled ', self.NOW))

    def test_unknown_values(self):
        self.assertEqual(windows_host.describe_expiry('', self.NOW), '未知')
        self.assertEqual(windows_host.describe_expiry('not-a-date', self.NOW), '未知')


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.row = windows_host.load_registry(_write(f'{HEADER}\n{ROW}\n'))[0]

    def test_live_overrides_registry_and_is_labelled(self):
        text = windows_host.render(self.row, {'online': True, 'tailscale_ip': '100.9.9.9',
                                              'key_expiry': '0001-01-01T00:00:00Z'})
        self.assertIn('100.9.9.9', text)
        self.assertIn('在线', text)
        self.assertIn('[live]', text)
        self.assertIn('DISABLED', text)

    def test_without_live_degrades_without_lying(self):
        text = windows_host.render(self.row, {})
        self.assertIn('[登记值]', text)
        self.assertIn('未知（本机读不到 tailscale）', text)

    def test_channels_use_magicdns_and_ssh_user(self):
        text = windows_host.render(self.row, {})
        self.assertIn('tools/bridge-remote.sh --host example-win.tailxxxx.ts.net --user winuser check', text)
        self.assertIn('ssh winuser@example-win.tailxxxx.ts.net', text)
        self.assertIn('wsl.exe -d Ubuntu', text)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.registry = _write(f'{HEADER}\n{ROW}\n')

    def test_hit_exits_zero(self):
        code = windows_host.main(['示例同事', '--registry', str(self.registry), '--no-live'])
        self.assertEqual(code, 0)

    def test_miss_exits_one(self):
        code = windows_host.main(['王五', '--registry', str(self.registry), '--no-live'])
        self.assertEqual(code, 1)

    def test_missing_registry_exits_two(self):
        missing = Path(tempfile.mkdtemp()) / 'nope.csv'
        self.assertEqual(windows_host.main(['示例同事', '--registry', str(missing), '--no-live']), 2)

    def test_no_query_and_no_all_exits_two(self):
        self.assertEqual(windows_host.main(['--registry', str(self.registry), '--no-live']), 2)

    def test_all_exits_zero(self):
        self.assertEqual(windows_host.main(['--all', '--registry', str(self.registry), '--no-live']), 0)


class RepoContractTests(unittest.TestCase):
    """台账是个人信息：数据文件只在本机（.gitignore），仓库里只保留模板。"""

    ROOT = Path(windows_host.ROOT)

    def test_default_registry_path_is_the_local_csv(self):
        self.assertEqual(windows_host.REGISTRY, self.ROOT / 'docs' / 'windows-hosts.csv')

    def test_registry_data_file_is_gitignored(self):
        entries = {
            line.strip()
            for line in (self.ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
        }
        self.assertIn('docs/windows-hosts.csv', entries)

    def test_example_template_has_exactly_the_expected_columns(self):
        path = self.ROOT / 'docs' / 'windows-hosts.example.csv'
        header = path.read_text(encoding='utf-8').splitlines()[0].split(',')
        self.assertEqual(header, windows_host.FIELDS)

    def test_example_template_parses_and_is_obviously_fake(self):
        row = windows_host.load_registry(self.ROOT / 'docs' / 'windows-hosts.example.csv')[0]
        self.assertEqual(row['machine'], 'example-win')
        self.assertIn('example', row['feishu_open_id'])


if __name__ == '__main__':
    unittest.main()

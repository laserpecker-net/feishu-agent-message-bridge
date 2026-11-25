"""WSL ↔ Windows 路径换算的判据（`bridge/platform_paths.py`）。

为什么值得单独一堆用例：Windows 上的 bridge 在 WSL 里、工作台在 Windows 侧，
**同一份文件有两个写法**。换算错了不会立刻报错，只会"文件找不到"——那是最难查的一类错。
所以这里正例要多、报错要明确，且**不许猜**（UNC、`\\wsl$`、未挂载盘符一律拒绝）。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import platform_paths  # noqa: E402


def mounted(*letters):
    return lambda letter: letter.lower() in {item.lower() for item in letters}


class WslToWinTests(unittest.TestCase):
    def test_simple_path(self):
        self.assertEqual(r'C:\work\repo', platform_paths.wsl_to_win('/mnt/c/work/repo'))

    def test_space_and_chinese(self):
        self.assertEqual(r'D:\我的 项目\a b', platform_paths.wsl_to_win('/mnt/d/我的 项目/a b'))

    def test_other_drive(self):
        self.assertEqual(r'E:\x', platform_paths.wsl_to_win('/mnt/e/x'))

    def test_drive_root_keeps_trailing_separator(self):
        self.assertEqual('C:\\', platform_paths.wsl_to_win('/mnt/c'))
        self.assertEqual('C:\\', platform_paths.wsl_to_win('/mnt/c/'))

    def test_lowercase_drive_is_upcased(self):
        self.assertEqual(r'C:\a', platform_paths.wsl_to_win('/mnt/c/a'))

    def test_already_windows_form_is_idempotent(self):
        self.assertEqual(r'C:\a\b', platform_paths.wsl_to_win(r'C:\a\b'))
        self.assertEqual(r'C:\a\b', platform_paths.wsl_to_win('c:/a//b/'))

    def test_trailing_slash_is_dropped(self):
        self.assertEqual(r'C:\a', platform_paths.wsl_to_win('/mnt/c/a/'))

    def test_mixed_separators_collapse(self):
        self.assertEqual(r'C:\a\b', platform_paths.wsl_to_win('/mnt/c//a///b'))

    def test_drive_check_rejects_unmounted_letter(self):
        with self.assertRaises(platform_paths.PathMappingError) as caught:
            platform_paths.wsl_to_win('/mnt/z/x', check_drive=True, drive_checker=mounted('c', 'd'))
        self.assertIn('没有挂载', caught.exception.reason)

    def test_drive_check_passes_for_mounted_letter(self):
        self.assertEqual(r'C:\x', platform_paths.wsl_to_win(
            '/mnt/c/x', check_drive=True, drive_checker=mounted('c')))


class WinToWslTests(unittest.TestCase):
    def test_simple_path(self):
        self.assertEqual('/mnt/c/work/repo', platform_paths.win_to_wsl(r'C:\work\repo'))

    def test_space_and_chinese(self):
        self.assertEqual('/mnt/d/我的 项目/a b', platform_paths.win_to_wsl(r'D:\我的 项目\a b'))

    def test_forward_slashes_accepted(self):
        self.assertEqual('/mnt/c/a/b', platform_paths.win_to_wsl('C:/a/b'))

    def test_drive_root(self):
        self.assertEqual('/mnt/c', platform_paths.win_to_wsl('C:\\'))
        self.assertEqual('/mnt/c', platform_paths.win_to_wsl('C:'))

    def test_already_wsl_form_is_idempotent(self):
        self.assertEqual('/mnt/c/a', platform_paths.win_to_wsl('/mnt/c/a'))
        self.assertEqual('/mnt/c/a', platform_paths.win_to_wsl('/mnt/c//a/'))

    def test_trailing_separator_dropped(self):
        self.assertEqual('/mnt/c/a', platform_paths.win_to_wsl('C:\\a\\'))

    def test_empty_rejected(self):
        with self.assertRaises(platform_paths.PathMappingError):
            platform_paths.win_to_wsl('')

    def test_nul_rejected(self):
        with self.assertRaises(platform_paths.PathMappingError):
            platform_paths.win_to_wsl('C:\\a\x00b')

    def test_relative_path_rejected(self):
        with self.assertRaises(platform_paths.PathMappingError) as caught:
            platform_paths.win_to_wsl('relative/path')
        self.assertIn('不是 Windows 盘符路径', caught.exception.reason)

    def test_drive_letter_must_be_followed_by_separator(self):
        with self.assertRaises(platform_paths.PathMappingError):
            platform_paths.win_to_wsl('Cx\\a')


class RejectedPrefixesTests(unittest.TestCase):
    def test_unc_path_rejected_in_both_directions(self):
        for func in (platform_paths.wsl_to_win, platform_paths.win_to_wsl):
            with self.assertRaises(platform_paths.PathMappingError) as caught:
                func(r'\\server\share\a')
            self.assertIn('UNC', caught.exception.reason)

    def test_wsl_share_rejected(self):
        with self.assertRaises(platform_paths.PathMappingError) as caught:
            platform_paths.win_to_wsl(r'\\wsl$\Ubuntu\home')
        self.assertIn('wsl$', caught.exception.reason)

    def test_device_prefix_rejected(self):
        with self.assertRaises(platform_paths.PathMappingError) as caught:
            platform_paths.win_to_wsl(r'\\?\C:\a')
        self.assertIn('设备路径', caught.exception.reason)

    def test_non_mount_posix_path_rejected(self):
        with self.assertRaises(platform_paths.PathMappingError):
            platform_paths.wsl_to_win('/home/foo/bar')

    def test_drive_check_rejects_unmounted_letter(self):
        with self.assertRaises(platform_paths.PathMappingError) as caught:
            platform_paths.win_to_wsl(r'Z:\x', check_drive=True, drive_checker=mounted('c'))
        self.assertIn('没有挂载', caught.exception.reason)


class NormalizeForTests(unittest.TestCase):
    def test_windows_target(self):
        self.assertEqual(r'C:\a', platform_paths.normalize_for('windows', '/mnt/c/a'))

    def test_wsl_target(self):
        self.assertEqual('/mnt/c/a', platform_paths.normalize_for('wsl', r'C:\a'))

    def test_same_form_passthrough(self):
        self.assertEqual('/mnt/c/a', platform_paths.normalize_for('linux', '/mnt/c/a'))

    def test_unknown_platform_rejected(self):
        with self.assertRaises(platform_paths.PathMappingError):
            platform_paths.normalize_for('beos', r'C:\a')


class LooksLikeTests(unittest.TestCase):
    def test_windows_detection(self):
        self.assertTrue(platform_paths.looks_like_windows_path(r'C:\a'))
        self.assertTrue(platform_paths.looks_like_windows_path(r'\\server\share'))
        self.assertFalse(platform_paths.looks_like_windows_path('/mnt/c/a'))
        self.assertFalse(platform_paths.looks_like_windows_path(''))

    def test_wsl_detection(self):
        self.assertTrue(platform_paths.looks_like_wsl_path('/mnt/c/a'))
        self.assertFalse(platform_paths.looks_like_wsl_path(r'C:\a'))


if __name__ == '__main__':
    unittest.main()

"""仓库脱敏门禁：**进 git 的东西不能带个人信息**（本机可以有真值，但真值必须 gitignored）。

为什么要有这条测试：靠"提交前记得 grep 一下"是守不住的 —— 本次就是文档、测试夹具、归档目录里
各留了几处真名/open_id，直到有人专门扫一遍才发现。判据必须可复跑，所以做成测试：

    python3 -m pytest tests/test_repo_hygiene.py -q

规则：
1. 被 git 跟踪的文件（含 archive/）里不得出现：真实姓名、`ou_`/`oc_`/`cli_` 形式的真实 ID、
   真实 tailnet 名与机名、Tailscale CGNAT 地址、私网地址、某人家的绝对路径、真 key。
2. 本机专用文件必须在 .gitignore 里（台账、发行包产物、证据快照……），否则一次 `git add -A`
   就会把同事的姓名和 open_id 带进提交。

名字用 unicode 转义写在这里，避免"门禁文件自己带个人信息"。
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 三位真实姓名的 unicode 转义（不写字面量：门禁文件自己也不能带真值）
REAL_NAMES = ('\u91d1\u96ea', '\u674e\u91d1\u96ea', '\u5218\u6d77\u5b9d')

FORBIDDEN = [
    ('真实姓名', re.compile('|'.join(REAL_NAMES))),
    ('飞书 open_id', re.compile(r'ou_[0-9a-f]{32}')),
    ('飞书 chat_id', re.compile(r'oc_[0-9a-f]{32}')),
    ('飞书 app_id', re.compile(r'cli_[0-9a-f]{16}')),
    ('tailnet 名', re.compile(r'tail[0-9a-z]{6,}\.ts\.net')),
    ('机器名', re.compile('(?i)' + '5cd' + '5184|apple' + 'macbook|apple' + 'demacbook')),
    ('Tailscale 地址', re.compile(r'\b100\.(?:6[5-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b')),
    ('私网地址', re.compile(r'\b192\.168\.\d{1,3}\.\d{1,3}\b')),
    # 只拦"看起来像真人账户名"的家目录；me/someone/xxx/alice 这类测试占位名放行
    ('某人家的绝对路径', re.compile(r'/Users/(?!example\b|you\b|me\b|someone\b|xxx\b|alice\b|bob\b|user\b|PC\b|<)[A-Za-z0-9._-]+')),
    # 姓名拼音同样算个人信息：它出现在 unit/plist 名里，等于把某个人的名字写进系统命名
    ('作者姓名拼音', re.compile('liu' + 'haibao', re.IGNORECASE)),
    ('同事的账户名', re.compile(r'/home/' + 'pc' + r'\b')),
    # 公开仓库前统一清掉的公司/产品/渠道专名（2026-11-20 主人要求整仓公开）
    # 公开版权方写的是 LaserPecker.net，那一处放行；其余用法照样拦
    ('公司/产品专名', re.compile('laser' + 'pecker' + r'(?!\.net|-net)|' + '激光' + '啄木鸟|' + '钛' + '动',
                                        re.IGNORECASE)),
    ('内部渠道名', re.compile('laser' + 'pecker-ai-channel', re.IGNORECASE)),
    ('开发机账号名', re.compile(r'\b' + 'ai' + 'tv' + r'\b', re.IGNORECASE)),
    ('内部群名', re.compile('销售' + 'AI' + '应用|歪比' + '歪比')),
    ('真的 auth key', re.compile(r'tskey-auth-[A-Za-z0-9]{8,}')),
    ('疑似令牌', re.compile(r'\b(?:ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})')),

]

# 文档里作为**说明**出现的保留/示例值，不构成泄露
ALLOWED_SNIPPETS = {
    '100.64.0.0': 'Tailscale 网段说明（100.64.0.0/10）',
    '100.64.0.1': '台账模板里的示例地址',
    '192.168.': '文档里"私网地址形如 192.168.x.x"的说明',  # 仅当整行是占位描述时放行
        ('Laser' + 'Pecker.net'): 'LICENSE 里必须写的版权方（主人 2026-11-20 决定公开）',
    ('激光' + '啄木鸟'): 'LICENSE 里必须写的版权方（中文名，同上）',
    'xoxb-1234567890-abcdefghij': '测试夹具：一眼假的 Slack token',
    'ghp_aaaaaaaaaaaaaaaaaaaaaaaa': '测试夹具：一眼假的 GitHub token',
}

LOCAL_ONLY_MUST_BE_IGNORED = [
    'docs/windows-hosts.csv',      # 同事机台账：真名 + open_id + MagicDNS
    'dist/windows/',               # 发行包产物（含真实公钥注释、MSI）
    'research/',                   # 证据快照：真实 chat/app id
    'coordination/',               # 本地协作产物
    'reviews/',                    # 本地评审产物
    'acceptance/',                 # 本地验收记录
    'var/',                        # 运行期状态
    '.env',                        # 本机凭据
    'config.json',                 # 本机 bridge 配置（target_open_id 等）
]


def _git(*args):
    return subprocess.run(('git',) + args, cwd=str(REPO_ROOT), capture_output=True, check=True)


def tracked_files():
    out = _git('ls-files', '-z').stdout
    return [REPO_ROOT / name for name in out.decode('utf-8').split('\0') if name]


class NoPersonalDataInTrackedFiles(unittest.TestCase):
    def test_tracked_files_contain_no_personal_identifiers(self):
        offenders = []
        for path in tracked_files():
            try:
                text = path.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            relative = path.relative_to(REPO_ROOT)
            for lineno, line in enumerate(text.splitlines(), 1):
                for label, pattern in FORBIDDEN:
                    match = pattern.search(line)
                    if not match:
                        continue
                    hit = match.group(0)
                    if hit in ALLOWED_SNIPPETS:
                        continue
                    offenders.append('%s:%d %s → %r' % (relative, lineno, label, hit))
        self.assertEqual([], offenders, '被 git 跟踪的文件里出现个人信息：\n' + '\n'.join(offenders))


class LocalOnlyFilesStayLocal(unittest.TestCase):
    def test_local_only_paths_are_gitignored(self):
        missing = []
        for item in LOCAL_ONLY_MUST_BE_IGNORED:
            result = subprocess.run(('git', 'check-ignore', '-q', item), cwd=str(REPO_ROOT))
            if result.returncode != 0:
                missing.append(item)
        self.assertEqual([], missing, '这些本机专用路径没被忽略，一次 git add -A 就会把真值带进提交：%s'
                         % ', '.join(missing))

    def test_local_only_paths_are_documented(self):
        doc = (REPO_ROOT / 'docs' / 'local-only.md').read_text(encoding='utf-8')
        for item in LOCAL_ONLY_MUST_BE_IGNORED:
            self.assertIn(item.rstrip('/'), doc,
                          'docs/local-only.md 里要写清 %r 是什么、为什么只在本机' % item)


class WindowsScriptsKeepBom(unittest.TestCase):
    """给同事装的 Windows 脚本必须是 **UTF-8 with BOM**。

    为什么：那些机器是中文 Windows + PowerShell 5.1，它读 .ps1 是按系统 ANSI（GBK）走的；
    没有 BOM 的文件里只要有一个中文字符串，解析就会在引号处报「字符串缺少终止符」，
    装到一半停住，而且报错位置和真正的原因（编码）看不出关系。本仓真的踩过：
    用编辑器改了一行代码，顺手把 BOM 弄丢了，Windows 上整份脚本解析失败。
    """

    def test_windows_powershell_scripts_start_with_bom(self):
        offenders = []
        for path in tracked_files():
            if path.suffix.lower() != '.ps1':
                continue
            raw = path.read_bytes()
            if raw.startswith(b'\xef\xbb\xbf'):
                continue
            # 纯 ASCII 的文件无论怎么解码都不会走错，不强制加 BOM（加了只是噪音）
            if all(byte < 0x80 for byte in raw):
                continue
            offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual([], offenders, '这些 .ps1 不是 UTF-8 with BOM，中文 Windows 上 PowerShell 5.1 会解析失败：\n'
                         + '\n'.join(offenders))


if __name__ == '__main__':
    unittest.main()

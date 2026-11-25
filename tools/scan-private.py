#!/usr/bin/env python3
"""扫一遍指定目录，按 ``tests/test_repo_hygiene.py`` 的同一份词表判"能不能公开"。

为什么复用那份词表：**一条事实一个答案**。门禁与导出脚本各写一份禁词，
过几个月一定一边改一边忘；这里是直接 import 那份常量。

用法：``python3 tools/scan-private.py [目录，默认当前目录]``
退出码：0 = 干净；1 = 命中（打印前 30 条）。
"""
import importlib.util
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_hygiene_patterns():
    src = REPO_ROOT / 'tests' / 'test_repo_hygiene.py'
    spec = importlib.util.spec_from_file_location('_hygiene', src)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FORBIDDEN, getattr(module, 'ALLOWED_SNIPPETS', {})


def tracked_files(root):
    """在 git 仓库里只看**被跟踪**的文件：本机私有目录（research/ dist/ 台账）本来就不该算。"""
    import subprocess
    try:
        out = subprocess.run(['git', '-C', str(root), 'ls-files'], capture_output=True,
                             text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    return [pathlib.Path(root) / line for line in out.splitlines() if line.strip()]


def scan(root):
    patterns, allowed = load_hygiene_patterns()
    hits = []
    candidates = tracked_files(root)
    if candidates is None:
        candidates = [p for p in pathlib.Path(root).rglob('*') if p.is_file()]
    for path in candidates:
        if not path.is_file() or '/.git/' in str(path):
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            for label, pattern in patterns:
                for found in pattern.findall(line):
                    if found in allowed:
                        continue
                    hits.append('%s:%d %s → %r' % (path, line_no, label, found))
    return hits


def main(argv):
    root = argv[1] if len(argv) > 1 else '.'
    hits = scan(root)
    if hits:
        print('\n'.join(hits[:30]))
        print('敏感词命中 %d 处——**不要公开**' % len(hits))
        return 1
    print('敏感词：0 处（与 test_repo_hygiene 同一份词表）')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

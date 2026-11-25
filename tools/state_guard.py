#!/usr/bin/env python3
"""state-guard：验收/多轮测试前后各存一份状态，收尾按**比对**还原，不按记忆还原。

为什么需要这个工具（2026-11-09 真机事故，见 docs/HISTORY.md §29）：

2026-11-05 19:56 的多轮任务测试收尾时写了 `hold.json`，理由是「恢复原挂起」——它凭记忆
把 16:45 那次「下班」当成原状态，却忽略了同事本人 19:42 已经发过「上班」。结果她那台机器
被挂起 96 小时：零采集、零入队、零回复，4 天里所有 @ 她的消息按挂起语义丢弃，谁都没发现。

「我记得原来是挂起的」这种还原方式，只要记错一次，就是**把别人的服务关掉而无人察觉**。
所以：测之前把状态存成文件，测之后跑一次比对，不一致就以退出码 3 失败。

用法::

    # 测试前（远端 Windows/WSL2 宿主，host 取台账里的 tailnet 名）
    python3 tools/state_guard.py --host <host> --save /tmp/pre.json
    # 测试后
    python3 tools/state_guard.py --host <host> --compare /tmp/pre.json
    # 本机（macOS）
    python3 tools/state_guard.py --local --save /tmp/pre.json

退出码：0 = 与基线一致；3 = 控制状态（mode / release）变了；2 = 目标不可达或用法错误。
`heartbeat_age_seconds` / `installed_at` 这类每时每刻都在变的字段不参与比对。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 参与比对的字段：只有"人做的状态选择"和"跑的哪份代码"。
CONTROL_FIELDS = ('mode', 'installed_release', 'running_release', 'update_pending')

#: 只做展示、不一致也不判失败的字段（每秒都在变）。
NOISE_FIELDS = ('heartbeat_age_seconds', 'installed_at', 'home')


def parse_version_json(text):
    """从 `feishu-bridge status` / `bridge-remote.sh version` 的输出里取出 JSON 对象。

    远端命令偶尔会在前面带一行 ssh banner 或 `bash -s` 的提示，所以不从第一个字符开始解析。
    """
    start = text.find('{')
    while start >= 0:
        try:
            value = json.loads(text[start:])
        except ValueError:
            start = text.find('{', start + 1)
            continue
        if isinstance(value, dict) and 'mode' in value:
            return value
        start = text.find('{', start + 1)
    raise ValueError('输出里没有可解析的 version JSON')


def snapshot(payload):
    """裁成基线文件：只要控制字段，避免把抖动写进基线造成假失败。"""
    return {field: payload.get(field) for field in CONTROL_FIELDS}


def diff(before, after):
    """逐字段比较，返回 [(字段, 之前, 现在)]；顺带回传噪声字段的变化供展示。"""
    changes = []
    for field in CONTROL_FIELDS:
        old, new = before.get(field), after.get(field)
        if old != new:
            changes.append((field, old, new))
    noise = [(field, before.get(field), after.get(field))
             for field in NOISE_FIELDS if before.get(field) != after.get(field)]
    return changes, noise


def collect(local, host, user):
    """读一次当前状态：本机走 `feishu-bridge status`，远端走 `tools/bridge-remote.sh version`。"""
    if local:
        argv = ['feishu-bridge', 'status']
    else:
        argv = ['bash', str(REPO_ROOT / 'tools' / 'bridge-remote.sh'),
                '--host', host, '--user', user, 'version']
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise SystemExit('读取状态失败（退出码 %d）：\n%s' % (result.returncode, result.stdout[-800:]))
    return parse_version_json(result.stdout)


def render(changes, noise):
    lines = []
    for field, old, new in changes:
        lines.append('  %-18s %s  →  %s' % (field, old, new))
    for field, old, new in noise:
        lines.append('  (%s %s → %s，不计入判定)' % (field, old, new))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='验收前后状态护栏：存基线、对比对、不一致 exit 3')
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--local', action='store_true', help='读本机（macOS）的 feishu-bridge status')
    target.add_argument('--host', help='远端宿主（Tailscale 名字或 IP）')
    parser.add_argument('--user', default='PC', help='远端 SSH 用户（默认 PC，仅 --host 用）')
    parser.add_argument('--save', metavar='PATH', help='把当前状态写成基线文件')
    parser.add_argument('--compare', metavar='PATH', help='与基线比对，不一致退出码 3')
    parser.add_argument('--json', action='store_true', help='输出 JSON 而不是表格式差异')
    args = parser.parse_args(argv)
    if not args.local and not args.host:
        parser.error('要么 --local，要么 --host <名字>')

    current = snapshot(collect(args.local, args.host, args.user))

    if args.save:
        Path(args.save).write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n')
        print('已保存基线：%s' % args.save)
        print(json.dumps(current, ensure_ascii=False, indent=2))
        return 0

    if args.compare:
        try:
            before = json.loads(Path(args.compare).read_text())
        except (OSError, ValueError) as error:
            raise SystemExit('读不到基线 %s：%s' % (args.compare, error))
        changes, noise = diff(before, current)
        payload = {'same': not changes,
                   'changes': [{'field': f, 'before': b, 'after': a} for f, b, a in changes],
                   'noise': [{'field': f, 'before': b, 'after': a} for f, b, a in noise],
                   'current': current}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif changes:
            print('状态与基线不一致（这就是测试改了生产状态的证据）：')
            print(render(changes, noise))
            mode_changed = any(field == 'mode' for field, _, _ in changes)
            release_changed = any(field in ('installed_release', 'running_release')
                                  for field, _, _ in changes)
            if mode_changed:
                print('\n挂起状态变了。按基线还原，或明确说明为什么这次改动是有意的：')
                print('  feishu-bridge %s' % ('release' if before.get('mode') == 'running' else 'hold'))
            elif release_changed:
                print('\n只有 release 变了（升级/回滚属预期）；确认这次是有意升级即可，'
                      '不需要 hold/release。')
            else:
                print('\n只有 update_pending 变了：装了新版但进程还没重启，跑 feishu-bridge restart。')
        else:
            print('状态与基线一致。')
            if noise:
                print(render([], noise))
        return 3 if changes else 0

    print(json.dumps(current, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())

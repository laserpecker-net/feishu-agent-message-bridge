#!/usr/bin/env python3
"""按「人」定位同事机：静态台账 + Tailscale live 状态 + 通信通道命令。

台账的唯一来源是 ``docs/windows-hosts.csv``（字段与登记流程见 ``docs/windows-hosts.md``）。
live 状态来自本机 ``tailscale status --json``（只读）。拿不到 live 就降级打印登记值，
不抛异常——"定位不到机器"和"看不到在线状态"是两件事，不能混成一个错误。

用法::

    python3 tools/windows_host.py <名字>            # 按称呼/飞书名/机名/IP 都能匹配
    python3 tools/windows_host.py example-win
    python3 tools/windows_host.py --all
    python3 tools/windows_host.py <名字> --json

退出码：0 命中；1 台账里没有；2 台账缺失/缺列/参数错误。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'docs' / 'windows-hosts.csv'

# 台账列（顺序 = CSV 表头；docs/windows-hosts.md 有逐列说明）
FIELDS = [
    'name',           # 你怎么称呼这位同事，如「示例同事」
    'feishu_user',    # bridge 的 status.json identity.userName，如「示例飞书」
    'feishu_open_id', # 同上 identity.openId（ou_...）
    'machine',        # Windows 计算机名 / Tailscale HostName
    'magicdns',       # <machine>.tailnet.ts.net，ssh 用它，比 IP 稳
    'tailscale_ip',   # 100.x
    'ssh_user',       # Windows OpenSSH 登录用户（管理员账户）
    'wsl_distro',     # WSL 发行版名
    'wsl_user',       # WSL 里的普通用户
    'os',             # Windows 版本 + 机型（一句话）
    'deployed_at',    # 通道/首装日期
    'release',        # bridge 当前 release_id
    'key_expiry',     # Tailscale 节点密钥到期日；DISABLED 表示永不过期
    'status',         # 登记时的状态快照；live 为准
    'notes',          # 这台机器的特殊点（探测结论）
]

def ssh_host(row: dict[str, str]) -> str:
    """ssh 目标主机名：优先台账的 magicdns。

    台账缺 magicdns 时返回 ``<machine>.<tailnet>`` 这种**显式占位**，不猜也不写死本组织的
    tailnet：tailnet 名字是「这台机器在哪张网里」的事实，来源只有台账/live 两处，
    写死在这里就成了第三处，换网时会静默输出一个错的 ssh 目标。
    """
    return row.get('magicdns') or f"{row.get('machine', '')}.<tailnet>"


def load_registry(path: Path | str = REGISTRY) -> list[dict[str, str]]:
    """读台账。缺文件/缺列明确报错，不静默空转。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f'台账不存在：{path}\n'
            '台账是**本机文件**（含同事姓名/飞书 open_id，已在 .gitignore，绝不进 git）。\n'
            f'初始化：cp {path.parent / "windows-hosts.example.csv"} {path}'
            '  然后照 docs/windows-hosts.md §3 登记（示例行是模板，别当真）。'
        )
    with path.open(encoding='utf-8-sig', newline='') as fh:
        reader = csv.DictReader(fh)
        missing = [f for f in FIELDS if f not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f'台账缺列：{missing}（允许顺序不同，不允许少列）')
        rows = [{k: (v or '').strip() for k, v in row.items() if k} for row in reader]
    return [r for r in rows if r.get('name')]


def match(rows: list[dict[str, str]], query: str) -> list[dict[str, str]]:
    """按称呼/飞书名/机名/MagicDNS/IP/SSH 用户/备注做不区分大小写的子串匹配。"""
    q = (query or '').strip().lower()
    if not q:
        return []
    keys = ('name', 'feishu_user', 'machine', 'magicdns', 'tailscale_ip', 'ssh_user', 'notes')
    return [r for r in rows if any(q in (r.get(k) or '').lower() for k in keys)]


def live_status() -> dict[str, dict]:
    """本机 tailscale 的节点快照，键为小写 HostName 与 DNSName。

    任何失败（没装/没登录/JSON 坏了）都返回空 dict：调用方降级成登记值。
    """
    exe = shutil.which('tailscale')
    if not exe:
        return {}
    try:
        proc = subprocess.run(
            [exe, 'status', '--json'], capture_output=True, text=True, timeout=8
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout or '{}')
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}

    nodes = [data.get('Self') or {}] + list((data.get('Peer') or {}).values())
    out: dict[str, dict] = {}
    for node in nodes:
        if not node:
            continue
        ips = node.get('TailscaleIPs') or ([node['TailscaleIP']] if node.get('TailscaleIP') else [])
        rec = {
            'online': bool(node.get('Online')),
            'key_expiry': node.get('KeyExpiry') or '',
            'tailscale_ip': ips[0] if ips else '',
        }
        for key in (node.get('HostName') or '', node.get('DNSName') or ''):
            key = key.rstrip('.').lower()
            if key:
                out[key] = rec
    return out


def describe_expiry(value: str, now: datetime | None = None) -> str:
    """把 Tailscale 的 KeyExpiry 说成人话：DISABLED / 绝对时间 + 剩余天数 / 未知。"""
    if not value:
        return '未知'
    text = str(value).strip()
    # 台账里允许手写 DISABLED（docs/windows-hosts.md §2 规定的词汇），
    # 与 Tailscale 的零值 0001-… 同义。少了这一支，登记值会被显示成「未知」。
    if text.lower() == 'disabled' or text.startswith('0001'):
        return 'DISABLED（永不过期）'
    # Tailscale 给的是 RFC3339Nano（最多 9 位小数），而 Python 3.9 的 fromisoformat 只认 3/6 位，
    # 9 位会抛 ValueError 被当成「未知」——过期时间恰恰是本工具最不能看错的一列。
    normalized = re.sub(r'(\.\d{6})\d+', r'\1', text).replace('Z', '+00:00')
    try:
        when = datetime.fromisoformat(normalized)
    except ValueError:
        return '未知'
    if when.tzinfo is None:  # 台账里手写的纯日期（2027-05-01）按 UTC 处理
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return f'{when.astimezone():%Y-%m-%d %H:%M}（{(when - now).days} 天后）'


def _channels(row: dict[str, str]) -> list[str]:
    host = ssh_host(row)
    user = row.get('ssh_user') or '<ssh_user>'
    distro = row.get('wsl_distro') or 'Ubuntu'
    return [
        f'tools/bridge-remote.sh --host {host} --user {user} check',
        f'tools/bridge-remote.sh --host {host} --user {user} version',
        f'tools/bridge-remote.sh --host {host} --user {user} logs 120',
        f'python3 tools/remote_doctor.py --host {host} --target windows',
        f'ssh {user}@{host}',
        f"ssh {user}@{host} 'wsl.exe -d {distro} -- bash -lc \"feishu-bridge status\"'",
    ]


def render(row: dict[str, str], live: dict | None = None) -> str:
    """一台机器的可读卡片：登记值 + live 覆盖 + 复制即用的通道命令。"""
    live = live or {}
    host = ssh_host(row)
    tail_ip = live.get('tailscale_ip') or row.get('tailscale_ip') or '—'
    online = '在线' if live.get('online') else ('离线' if live else '未知（本机读不到 tailscale）')
    expiry = describe_expiry(live.get('key_expiry') or row.get('key_expiry') or '')
    expiry_src = 'live' if live.get('key_expiry') else '登记值'
    lines = [
        f"{(row.get('name') or '').strip()}（{row.get('feishu_user') or '—'}）",
        f"  机器        {row.get('machine') or '—'}",
        f"  MagicDNS    {host}",
        f"  Tailscale   {tail_ip}   {online}   key expiry {expiry} [{expiry_src}]",
        f"  SSH         {row.get('ssh_user') or '—'}@{host}（Windows OpenSSH，仅公钥）",
        f"  WSL         {row.get('wsl_distro') or '—'} / 用户 {row.get('wsl_user') or '—'}",
        f"  bridge 身份 {row.get('feishu_user') or '—'}（{row.get('feishu_open_id') or '—'}）",
        f"  bridge 版本 {row.get('release') or '—'}（登记值；实际以 version 动作对比 installed/running）",
        f"  部署        {row.get('deployed_at') or '—'}   状态 {row.get('status') or '—'}（登记值；在线/离线只看上面 Tailscale 行）",
        f"  OS          {row.get('os') or '—'}",
    ]
    if row.get('notes'):
        lines.append(f"  特殊点      {row['notes']}")
    lines.append('  通道（复制即用）：')
    lines += [f'    {cmd}' for cmd in _channels(row)]
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='按人/机名定位同事机：台账 + Tailscale live 状态 + 通信通道命令'
    )
    parser.add_argument('query', nargs='?', help='同事称呼 / 飞书名 / 机名 / MagicDNS / IP')
    parser.add_argument('--all', action='store_true', help='列出台账全部机器')
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    parser.add_argument('--registry', default=str(REGISTRY), help='台账路径（默认 docs/windows-hosts.csv）')
    parser.add_argument('--no-live', action='store_true', help='不查本机 tailscale，只用登记值')
    args = parser.parse_args(argv)

    if not args.query and not args.all:
        parser.print_usage(sys.stderr)
        print('要么给一个 query，要么 --all', file=sys.stderr)
        return 2

    try:
        rows = load_registry(args.registry)
    except (OSError, ValueError) as exc:
        print(f'读取台账失败：{exc}', file=sys.stderr)
        return 2

    live = {} if args.no_live else live_status()

    def overlay(row: dict[str, str]) -> dict:
        for key in (row.get('machine') or '', row.get('magicdns') or ''):
            key = key.rstrip('.').lower()
            if key and key in live:
                return live[key]
        return {}

    if args.all:
        if args.json:
            print(json.dumps([{**r, 'live': overlay(r)} for r in rows], ensure_ascii=False, indent=2))
            return 0
        for row in rows:
            rec = overlay(row)
            state = '在线' if rec.get('online') else ('离线' if rec else '—')
            print(f"{row.get('name', ''):<8} {row.get('machine', ''):<22} {state:<4} "
                  f"{describe_expiry(rec.get('key_expiry') or row.get('key_expiry') or '')}")
        return 0

    hits = match(rows, args.query)
    if not hits:
        print(f'台账里没有匹配「{args.query}」的机器（docs/windows-hosts.csv 共 {len(rows)} 台）',
              file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([{**r, 'live': overlay(r)} for r in hits], ensure_ascii=False, indent=2))
    else:
        print('\n\n'.join(render(r, overlay(r)) for r in hits))
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())

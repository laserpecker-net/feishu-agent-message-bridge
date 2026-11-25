"""``python3 -m agentkit``：把"底座能不能用"变成一条可以直接跑的命令。

用法::

    python3 -m agentkit list             # 列出已注册的 AGENT 与能力
    python3 -m agentkit show dsh         # 看某个 AGENT 的能力与策略载体
    python3 -m agentkit platform         # 看当前平台 + Windows 是否被放行
    python3 -m agentkit selftest         # 在**当前这台机器**上真跑一遍平台层

为什么值得存在：现在的仓库里"这台机器能不能跑 codex"要靠读 ``codex-consumer.py --check``
才知道，而那个 ``--check`` 只认识 codex。注册表让这个问题变成一次枚举。
"""

from __future__ import annotations

import json
import sys

from .adapters import get_adapter, list_adapters
from .platform import describe, get_platform
from . import selftest as selftest_mod


def _cmd_list() -> int:
    rows = []
    for name in list_adapters():
        caps = get_adapter(name).capabilities.to_dict()
        rows.append(
            {
                "adapter": name,
                "stream": caps["stream_format"],
                "resume": caps["supports_resume"],
                "tool_events": caps["emits_tool_events"],
                "deny": caps["deny_enforcement"],
                "home": getattr(get_adapter(name), "home_policy", "redirect"),
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def _cmd_show(name: str) -> int:
    adapter = get_adapter(name)
    print(
        json.dumps(
            {
                "name": adapter.name,
                "aliases": list(adapter.aliases),
                "binary": adapter.binary_name,
                "binary_env": adapter.binary_env,
                "home_policy": adapter.home_policy,
                "capabilities": adapter.capabilities.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_platform() -> int:
    info = describe()
    info["adapters"] = list_adapters()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        args = ["list"]
    command, rest = args[0], args[1:]
    if command == "list":
        return _cmd_list()
    if command == "show":
        if not rest:
            print("usage: python3 -m agentkit show <adapter>", file=sys.stderr)
            return 2
        return _cmd_show(rest[0])
    if command == "platform":
        return _cmd_platform()
    if command == "selftest":
        return selftest_mod.main()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

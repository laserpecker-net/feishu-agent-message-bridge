#!/usr/bin/env python3
"""用 agentkit 的适配层跑一个 bridge 消费者（**并行入口，不改既有三个脚本**）。

与 `dsh-consumer.py` / `codex-consumer.py` / `cursor-consumer.py` 的关系：

* 它们各自 700 行，把 argv、环境、流解析、超时、杀进程组、收尾全写在自己文件里；
* 本脚本把这部分换成 `agentkit.driver`，一个参数选 AGENT：

    python3 agentkit-consumer.py --agent dsh    --home ~/.local/share/feishu-agent
    python3 agentkit-consumer.py --agent codex  --home ~/.local/share/feishu-agent --workspace ~/work
    python3 agentkit-consumer.py --agent dsh --check        # 只看能力/二进制，不 register、不 claim

`--consumer` / `--kind` / `--workspace` / `--check` / `--home` 全部由
`consumer_runtime.main()` 解析，本脚本只多认一个 `--agent`（或环境变量
`FEISHU_AGENT_ADAPTER`），所以既有部署的启动参数可以原样搬过来。

注意：`consumer_runtime` 本身要求 POSIX（`flock` 单进程锁）。原生 Windows 上要么按
`WINDOWS-DECISION.md` 拓扑 A 跑在 WSL2 里，要么走拓扑 B 的 HTTP 网关；agentkit 的
Windows 平台层是给"AGENT 进程"用的，不是给 bridge 用的。
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import consumer_runtime  # noqa: E402
from agentkit import driver  # noqa: E402


def main():
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False,
                                     description='agentkit-based bridge consumer')
    parser.add_argument('--agent', default=os.environ.get('FEISHU_AGENT_ADAPTER') or 'dsh',
                        help='agent name registered in agentkit (dsh/codex/cursor/fake)')
    known, rest = parser.parse_known_args()
    # 把剩下的参数原样交回 consumer_runtime.main()（--home/--consumer/--kind/--workspace/--check）
    sys.argv = [sys.argv[0]] + rest

    name = known.agent.strip().lower()
    if '--check' in rest:
        home = None
        for index, token in enumerate(rest):
            if token == '--home' and index + 1 < len(rest):
                home = rest[index + 1]
            elif token.startswith('--home='):
                home = token.split('=', 1)[1]
        try:
            report = driver.describe(name, home=home)
        except Exception as error:  # noqa: BLE001 - --check 的职责就是把问题说出来
            report = {'adapter': name, 'error': '%s: %s' % (type(error).__name__, error)}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))

    kind = driver.ADAPTER_KIND.get(name, 'other')
    consumer_runtime.main(
        # 生产工作区可能是整个 ~/Documents（实测 91 万文件）：产物快照必须走窗口模式，
        # 否则每次 turn 都要全量遍历 26s×2。窗口只影响"产物清单的完整性"，不影响回复。
        driver.build_agent(name, snapshot_window_s=600),
        default_consumer='%s-cli-main' % name,
        default_kind=kind,
    )


if __name__ == '__main__':
    main()

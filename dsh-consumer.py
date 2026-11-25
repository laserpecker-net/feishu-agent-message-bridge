#!/usr/bin/env python3
"""DSH headless adapter: reply, retrieve and complete tasks inside a sandbox.

The bridge event protocol is owned by ``consumer_runtime``; this adapter only
answers one message with one ``dsh headless`` run. The capability model comes
from ``consumer_safety``: reply, retrieve (read/search/web) and execute
(commands, edits) are allowed; delete/destructive commands, secret access,
delegation to subagents and MCP are denied.

**运行机制不在这个文件里**：overlay、NDJSON 契约、超时与进程树收尾、凭证路由
都在 ``bridge/workbench.py`` 一处定义（干活端也要用同一套；两套实现 = 改一处忘一处，
见 ``AGENTS.md`` §0）。本文件只保留"回话端自己的策略"：

  * 一次回话的时间预算是 240 秒（干活的预算是任务级的 1800 秒，见 ``bridge/workbench.py``）；
  * 每轮运行目录放在 ``<home>/consumer-agent-runs/dsh/run-*``，并写一份 ``run.json`` 回执；
  * 它把 final 原文交回 ``consumer_runtime``——解析、脱敏与长度上限的唯一入口是
    ``consumer_safety.finalize_reply``，在这里剥掉那层 JSON 会丢掉 ``ask_owner``
    （2026-11-04 真机踩过：私聊升级因此从没触发）。
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from consumer_safety import (DENIED_TOOL_NAMES, capabilities_report, require_posix_runtime)
from consumer_runtime import main, owner_name_at
from agent_prompt import build_prompt

from bridge import workbench


#: 回话端一次生成的时间预算（秒）。干活端用 ``workbench.TASK_TIMEOUT_SECONDS``。
AGENT_TIMEOUT_SECONDS = workbench.CONSUMER_TIMEOUT_SECONDS
#: 单次输出的字节上限（stdout + stderr）。与干活端共用同一个缺省值。
MAX_OUTPUT_BYTES = workbench.DEFAULT_MAX_OUTPUT_BYTES
# 下面这些名字继续从本模块导出：既有测试与运维脚本按它们定位契约（沙箱策略、overlay 文件名、
# 机器级补丁层）。定义只有一处——在 ``bridge/workbench.py``。
#: 本进程的角色：**回话端只说话**（沙箱 read-only，写不进去）。这是设计上的分工，不是配置。
CONSUMER_ROLE = 'replier'
#: 兼容旧引用：本端实际使用的沙箱策略（= read-only）。
SANDBOX_MODE = workbench.sandbox_mode(CONSUMER_ROLE)
APPROVAL_POLICY = workbench.APPROVAL_POLICY
DISABLED_TOOLS = workbench.DISABLED_TOOLS
OVERLAY_NAME = workbench.OVERLAY_NAME
EXTRA_PATCH_NAME = workbench.EXTRA_PATCH_NAME
EXTRA_ENV_NAME = workbench.EXTRA_ENV_NAME
FORCED_ENV = workbench.FORCED_ENV
ALLOWED_EVENTS = workbench.ALLOWED_EVENTS

overlay_text = workbench.overlay_text
write_overlay = workbench.write_overlay
extra_patch_args = workbench.extra_patch_args
extra_env = workbench.extra_env
workspace_from_env = workbench.workspace_from_env
credential_route = workbench.credential_route
sandbox_mode = workbench.sandbox_mode
# 旧名字继续导出：agentkit 的 legacy 适配器（`agentkit/adapters/legacy.py`）按这些私有名
# 定位契约（overlay 文本、凭证路由、DSH home）。定义仍只有一处——`bridge/workbench.py`。
_overlay_text = workbench.overlay_text
_credential_route = workbench.credential_route
_dsh_home = workbench.dsh_home


def _kill_group(process):
    """收尸进程组。保留本模块的名字：既有测试直接调用替换它。"""
    return workbench.kill_group(process)


def _generate(binary, overlay, run_dir, workdir, prompt, stop_event, receipt, events):
    """跑一次 headless，返回 final 原文。参数显式传入，避免本模块再定义一套默认值。"""
    return workbench.generate_headless(binary, overlay, run_dir, workdir, prompt, stop_event,
                                       receipt, timeout=AGENT_TIMEOUT_SECONDS,
                                       max_output_bytes=MAX_OUTPUT_BYTES,
                                       kill=_kill_group, events=events)


def build_agent(home: Path):
    require_posix_runtime()
    binary = shutil.which('dsh')
    if not binary:
        raise RuntimeError('DSH executable is not available')
    workspace = workspace_from_env()
    credential = credential_route(workspace)
    if credential is None:
        # Headless ignores the GUI sign-in; fail before any event is claimed.
        raise RuntimeError(
            'no headless DSH credential: export DEEPSEEK_API_KEY in the launch environment, '
            "or sign in with `dsh` so $DSH_HOME/.credentials.yaml exists, or add "
            'DEEPSEEK_API_KEY to the .env in $DSH_HOME or the workspace '
            '(headless ignores the GUI sign-in); no bridge event was claimed')

    def generate(payload: dict, event: dict, stop_event: threading.Event) -> str:
        if stop_event.is_set():
            raise RuntimeError('DSH generation cancelled')
        prompt = build_prompt(payload, workspace, timeout_seconds=AGENT_TIMEOUT_SECONDS,
                              owner_name=owner_name_at(home),
                              teachings=payload.get('teachings'), role=CONSUMER_ROLE)
        root = Path(home) / 'consumer-agent-runs' / 'dsh'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        run_dir = Path(tempfile.mkdtemp(prefix='run-', dir=str(root))).resolve()
        receipt = {'event_id': event.get('id'), 'status': 'failed',
                   'profile': 'headless', 'model': 'inherited',
                   'credential_route': credential, 'workspace': workspace,
                   'capabilities': capabilities_report(CONSUMER_ROLE)['capabilities'],
                   'sandbox_mode': sandbox_mode(CONSUMER_ROLE), 'approval_policy': APPROVAL_POLICY,
                   'role': CONSUMER_ROLE,
                   'denied_tools': sorted(DENIED_TOOL_NAMES),
                   'extra_patch': (extra_patch_args() or [None, None])[1],
                   'extra_env': sorted(extra_env()),
                   'overlay': None, 'tool_calls': 0, 'violations': []}
        started = time.monotonic()
        events = workbench.RunEvents(workspace or run_dir)
        try:
            overlay = write_overlay(run_dir / OVERLAY_NAME, credential, CONSUMER_ROLE)
            receipt['overlay'] = overlay
            answer = _generate(binary, overlay, run_dir, workspace or run_dir,
                               prompt, stop_event, receipt, events)
            receipt['status'] = 'completed'
            return answer
        finally:
            receipt['duration_seconds'] = round(time.monotonic() - started, 3)
            with os.fdopen(os.open(str(run_dir / 'run.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
                json.dump(receipt, file, ensure_ascii=False)

    return generate


if __name__ == '__main__':
    main(build_agent, default_consumer='dsh-cli-main', default_kind='dsh')

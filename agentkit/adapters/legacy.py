"""三个既有 AGENT 的 agentkit 适配器（**委托既有模块，不重写策略**）。

设计原则：**策略只有一份**。argv 与流 schema 属于"因 AGENT 而异"的部分，写在这里；
但"什么工具算禁止"必须与 ``consumer_safety`` / ``codex-consumer.py``
完全一致，所以这里直接 import 那几份常量与纯函数，而不是抄一遍。

同步锚点（改动这些地方就必须同时看这里）：

===========================  ==============================================
浏览/编辑/开局 CLI          ``bridge/agent.py:549-554``（argv）
Codex                      ``codex-consumer.py:252-262``（``_command``）
DSH                        ``dsh-consumer.py:238``（argv）、``:67-98``（overlay）
统一安全策略               ``consumer_safety.py:49-231``
===========================  ==============================================
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..types import (
    Capabilities,
    DenyPolicy,
    Outcome,
    Reason,
    StreamEvent,
    TurnRequest,
    TurnStatus,
    TurnStatus as _S,
)
from .base import AgentAdapter, PrepareContext, TurnState
from .registry import register

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from bridge.reply import MAX_OUTPUT  # noqa: E402 - 与 legacy 同一个上限


def _load(name: str, filename: str):
    """按文件名加载（``codex-consumer.py`` 这类带连字符的名字无法直接 import）。"""
    cached = sys.modules.get("agentkit._legacy_" + name)
    if cached is not None:
        return cached
    path = os.path.join(_REPO_ROOT, filename)
    spec = importlib.util.spec_from_file_location("agentkit._legacy_" + name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _safety():
    """``consumer_safety``：全仓唯一的策略来源（纯函数 + 常量，无副作用）。"""
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    import consumer_safety  # noqa: WPS433 - 运行时按需导入，避免测试期路径问题

    return consumer_safety


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------
#: Cursor CLI 的静态权限（headless print 模式没有审批提示，所以先把策略写进 cli.json）。
#: 曾经与内置 agent（`bridge/agent.py`）共用一份；内置 agent 删除后这里是唯一副本。
ALLOW = ['Shell(*)', 'Read(**)', 'Read(/**)', 'Write(**)', 'Write(/**)', 'WebFetch(*)',
         'List(**)', 'List(/**)', 'Glob(**)', 'Glob(/**)', 'Grep(**)', 'Grep(/**)',
         'WebSearch(*)', 'Edit(**)', 'Edit(/**)',
         'ReadLints(**)', 'SemanticSearch(**)', 'NotebookEdit(**)', 'ApplyPatch(*)']
DENY = ['Task(*)', 'Mcp(*:*)', 'Delete(**)', 'Delete(/**)']

@register
class CursorAdapter(AgentAdapter):
    """Cursor CLI（``--print --output-format stream-json``）。

    与已删除的内置 agent（`bridge/agent.py`）逐条对齐的三件事：

    1. **cwd 是私有 run 目录**，工作区只通过 ``--add-dir`` 暴露（相对路径写不到用户项目）；
    2. **静态策略文件** ``<cwd>/.cursor/cli.json`` = 本模块的 ``ALLOW/DENY``；
    3. **终局判定严格**：最后一条必须是 ``type=result`` + ``subtype=success`` + 非 ``is_error``，
       答案必须是 ``{"answer": "..."}`` 形式的 JSON（允许被 fenced/prose 包住）。
    """

    name = "cursor"
    aliases = ("cursor-cli",)
    # 注意：`cursor` 是**编辑器**的 CLI（3.x），agent 的 CLI 是 `cursor-agent`。
    # legacy 的默认值也是后者（``bridge/host.py:102-106``）；写成 `cursor` 会去开编辑器。
    binary_name = "cursor-agent"
    binary_env = "CURSOR_AGENT_BINARY"
    # 实测：`HOME=/tmp/x cursor-agent status` → "Not logged in"；软链 ~/.cursor 与
    # ~/.local/share/cursor-agent 都救不回来（凭据不在这些目录里）。legacy 也从不改 HOME。
    # 所以这个 adapter 的 HOME 必须 inherit —— 否则真实环境第一轮就失败。
    home_policy = "inherit"
    capabilities = Capabilities(
        name="cursor",
        stream_format="stream-json (NDJSON)",
        supports_resume=False,
        supports_model_switch=False,
        emits_tool_events=True,
        emits_usage=False,
        denies_tools=True,
        deny_enforcement="policy-file+stream-backstop",
        notes=("prompt 走 argv 末位；静态策略写在 <run_dir>/.cursor/cli.json，流上另有一层兜底；"
               "HOME 必须为真实 $HOME（登录态在那里，实测重定向即 Not logged in）"),
    )

    @property
    def max_output_bytes(self) -> int:  # type: ignore[override]
        return int(MAX_OUTPUT)

    def resolve_binary(self, platform) -> str:
        """与 legacy 同源：优先 ``cursor-agent``（``bridge/host.py:102-106``）。"""
        override = (os.environ.get(self.binary_env) or "").strip()
        candidates = [override] if override else []
        if not override:
            try:
                from bridge.host import default_cursor_binary

                candidates.append(default_cursor_binary())
            except Exception:  # noqa: BLE001 - 独立使用时没有 bridge 包
                pass
            candidates += ["cursor-agent", "agent"]
        for candidate in candidates:
            if not candidate:
                continue
            if os.path.isabs(candidate):
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return candidate
                continue
            found = platform.which(candidate)
            if found:
                return found
        raise FileNotFoundError(
            "cursor: no `cursor-agent` executable found (set %s); note `cursor` is the "
            "editor CLI and cannot run a headless turn" % self.binary_env
        )

    def preflight(self, req: TurnRequest, ctx: PrepareContext) -> None:
        """写出 Cursor 的静态权限文件 —— 没有它，deny 只剩"事后兜底"。

        原内置 agent 的做法（已删除）：把 ``ALLOW``/``DENY`` 写进
        ``<cwd>/.cursor/cli.json``。agentkit 必须做同样的事，否则同一个 AGENT 换个入口
        就少了静态那一层（CLI 会先执行，我们才在流上看到）。
        """
        if os.path.basename(ctx.binary) == "cursor":
            raise FileNotFoundError(
                "cursor: %s is the editor CLI; point %s at `cursor-agent`"
                % (ctx.binary, self.binary_env)
            )
        cursor_dir = os.path.join(ctx.run_dir, ".cursor")
        os.makedirs(cursor_dir, mode=0o700, exist_ok=True)
        policy_path = os.path.join(cursor_dir, "cli.json")
        policy = {"permissions": {"allow": list(ALLOW), "deny": list(DENY)}}
        with open(policy_path, "w", encoding="utf-8") as handle:
            json.dump(policy, handle, ensure_ascii=False, indent=2)
        platform = getattr(ctx, "platform", None)
        if platform is not None:
            platform.harden_path(policy_path, 0o600)
            platform.harden_path(cursor_dir, 0o700, is_dir=True)

    def cwd_for(self, req: TurnRequest, ctx: PrepareContext) -> str:
        return ctx.run_dir

    def build_argv(self, req: TurnRequest, ctx: PrepareContext) -> Sequence[str]:
        argv: List[str] = [
            ctx.binary,
            "--trust",
            "--force",
            "--sandbox",
            "disabled",
            "--print",
            "--output-format",
            "stream-json",
        ]
        if req.workspace:
            argv += ["--add-dir", req.workspace]
        argv.append(req.prompt)
        return argv

    def deny_violation(self, state: TurnState, event: StreamEvent) -> Optional[str]:
        if event.kind != "tool":
            return None
        safety = _safety()
        arguments = (event.raw or {}).get("arguments")
        reason = safety.tool_violation(event.tool_name or "", arguments)
        if reason is not None:
            return reason
        command = event.tool_command
        if command and safety.blocked_command(command):
            return "DestructiveCommand"
        return None

    def parse_line(self, line: str) -> Optional[StreamEvent]:
        text = line.strip()
        if not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if not isinstance(payload, Mapping):
            return None
        kind = str(payload.get("type") or "")
        subtype = str(payload.get("subtype") or "")
        if kind == "system" and subtype == "init":
            return StreamEvent(kind="init", raw=payload)
        if kind == "tool_call":
            return StreamEvent(
                kind="tool",
                raw=payload,
                tool_name=str(payload.get("name") or payload.get("tool") or ""),
                tool_command=str(payload.get("command") or "") or None,
            )
        if kind in ("assistant", "text"):
            chunk = payload.get("text") or payload.get("content")
            return StreamEvent(kind="text", raw=payload, text=str(chunk or ""))
        if kind == "result":
            return StreamEvent(kind="result", raw=payload)
        if kind in ("error", "failure"):
            return StreamEvent(kind="error", raw=payload)
        return StreamEvent(kind="other", raw=payload)

    def on_event(self, state: TurnState, event: StreamEvent) -> None:
        if event.kind == "init":
            session = (event.raw or {}).get("session_id")
            if isinstance(session, str) and session:
                state.resume_key = session
            model = (event.raw or {}).get("model")
            if isinstance(model, str) and model:
                state.served_model = model
        elif event.kind == "text" and event.text:
            state.add_text(event.text)
        elif event.kind == "error":
            state.extra["error"] = str((event.raw or {}).get("message") or "agent reported an error")
        elif event.kind == "result":
            payload = event.raw or {}
            subtype = str(payload.get("subtype") or "")
            if payload.get("is_error") or subtype != "success":
                state.extra["error"] = str(payload.get("result") or "terminal result was not successful")
                return
            result = payload.get("result")
            if isinstance(result, str):
                state.extra["result_text"] = result
            model = payload.get("model")
            if isinstance(model, str) and model:
                state.served_model = model
            state.extra["terminal_ok"] = True

    def classify(self, state: TurnState) -> Outcome:
        if state.denied_tool:
            return self.make_outcome(state, TurnStatus.FAILED, Reason.DENIED_TOOL)
        fallback_text = state.extra.get("result_text") or (state.text.strip() or None)
        if state.timed_out:
            answer = self._extract(fallback_text) or None
            return self.make_outcome(state, TurnStatus.INCOMPLETE, Reason.TIME_BUDGET, answer)
        if state.extra.get("error"):
            return self.make_outcome(state, TurnStatus.FAILED, Reason.AGENT_ERROR)
        if not state.extra.get("terminal_ok"):
            # legacy：没有成功的 result 就是失败，绝不把半截输出当答案
            return self.make_outcome(state, TurnStatus.FAILED, Reason.NO_ANSWER)
        answer = self._extract(fallback_text)
        if answer:
            return self.make_outcome(state, TurnStatus.COMPLETED, Reason.OK, answer)
        return self.make_outcome(state, TurnStatus.FAILED, Reason.AGENT_ERROR)

    @staticmethod
    def _extract(text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        try:
            answer = _safety().extract_answer(text)
        except ValueError:
            return None
        return (answer or "").strip() or None


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------
@register
class CodexAdapter(AgentAdapter):
    """Codex CLI（``exec --json``）。

    严格度对齐 ``codex-consumer.py:200-236``（``_Stream``）：**未知事件类型、缺
    ``turn.completed``、答案不是恰好 ``{"answer": ...}`` 的 JSON —— 任何一条都失败**。
    宁可失败也不能把半截输出当答案，这是 legacy 用整份测试套件钉住的语义。
    """

    name = "codex"
    aliases = ("codex-cli",)
    binary_name = "codex"
    binary_env = "CODEX_CONSUMER_BINARY"
    capabilities = Capabilities(
        name="codex",
        stream_format="jsonl (`--json`)",
        supports_resume=False,
        supports_model_switch=False,
        emits_tool_events=True,
        emits_usage=True,
        denies_tools=True,
        deny_enforcement="argv(--disable/--sandbox)+stream-backstop",
        notes="prompt 走 stdin；--ephemeral 不持久会话，续跑靠事件 journal 而不是会话",
    )

    @property
    def max_output_bytes(self) -> int:  # type: ignore[override]
        return int(self._module().MAX_OUTPUT)

    def _module(self):
        return _load("codex", "codex-consumer.py")

    def preflight(self, req: TurnRequest, ctx: PrepareContext) -> None:
        module = self._module()
        # 与既有入口一致：登录态从真实 CODEX_HOME 取，运行时指向本次 run 的私有目录
        auth_home = pathlib.Path(
            os.environ.get("CODEX_HOME") or (pathlib.Path.home() / ".codex")
        ).expanduser().resolve()
        auth_file = auth_home / "auth.json"
        if not auth_file.is_file():
            raise FileNotFoundError(
                "codex: %s is required (sign in with the Codex CLI first)" % auth_file
            )
        isolated = pathlib.Path(ctx.run_dir) / "codex-home"
        isolated.mkdir(parents=True, exist_ok=True)
        link = isolated / "auth.json"
        if not link.exists():
            try:
                link.symlink_to(auth_file)
            except OSError:
                shutil.copy2(str(auth_file), str(link))
                os.chmod(str(link), 0o600)
        schema = pathlib.Path(ctx.run_dir) / "schema.json"
        if not schema.exists():
            schema.write_text(json.dumps(module.SCHEMA), encoding="utf-8")
            os.chmod(str(schema), 0o600)

    def build_argv(self, req: TurnRequest, ctx: PrepareContext) -> Sequence[str]:
        # 直接复用既有 ``_command``：flag 集合只有一处真相
        return self._module()._command(ctx.binary, pathlib.Path(ctx.run_dir))

    def prompt_on_stdin(self, req: TurnRequest) -> Optional[str]:
        return req.prompt

    def env_patch(self, req: TurnRequest, ctx: PrepareContext) -> Mapping[str, str]:
        return {"CODEX_HOME": os.path.join(ctx.run_dir, "codex-home")}

    def deny_violation(self, state: TurnState, event: StreamEvent) -> Optional[str]:
        if event.kind != "tool":
            return None
        return self._module()._item_reason(dict(event.raw or {}))

    def parse_line(self, line: str) -> Optional[StreamEvent]:
        text = line.strip()
        if not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if not isinstance(payload, Mapping):
            return None
        module = self._module()
        kind = str(payload.get("type") or "")
        if kind in ("error", "turn.failed") or payload.get("error") or payload.get("is_error"):
            return StreamEvent(kind="error", raw=payload)
        if kind == "turn.completed":
            return StreamEvent(kind="result", raw=payload)
        if kind in ("thread.started", "turn.started"):
            return StreamEvent(kind="init", raw=payload)
        if kind in ("item.started", "item.updated", "item.completed"):
            item = payload.get("item")
            if not isinstance(item, Mapping):
                return StreamEvent(kind="error", raw=payload)
            item_type = module._item_type(item)
            raw: Dict[str, Any] = dict(item)
            raw["__item_type"] = item_type
            raw["__event"] = kind
            if item_type == "error":
                return StreamEvent(kind="other", raw=raw)
            return StreamEvent(kind="tool", raw=raw, tool_name=item_type)
        # 未知事件类型必须 fail-closed（legacy: 'Unsupported Codex stream event'）
        return StreamEvent(kind="error", raw=payload)

    def on_event(self, state: TurnState, event: StreamEvent) -> None:
        if event.kind == "init":
            session = (event.raw or {}).get("thread_id") or (event.raw or {}).get("id")
            if isinstance(session, str) and session:
                state.resume_key = session
        elif event.kind == "tool":
            raw = event.raw or {}
            if raw.get("__event") == "item.completed" and raw.get("__item_type") == "agent_message":
                text = raw.get("text")
                if isinstance(text, str):
                    state.extra["answer_text"] = text
        elif event.kind == "result":
            state.extra["complete"] = True
            usage = (event.raw or {}).get("usage")
            if isinstance(usage, Mapping):
                state.usage = {
                    str(key): int(value)
                    for key, value in usage.items()
                    if isinstance(value, (int, float))
                }
        elif event.kind == "error":
            state.extra["fatal"] = str(
                (event.raw or {}).get("message") or (event.raw or {}).get("error") or "error"
            )

    def classify(self, state: TurnState) -> Outcome:
        if state.denied_tool:
            return self.make_outcome(state, TurnStatus.FAILED, Reason.DENIED_TOOL)
        answer = self._parse_answer(state.extra.get("answer_text"))
        if state.timed_out:
            return self.make_outcome(state, TurnStatus.INCOMPLETE, Reason.TIME_BUDGET, answer)
        if state.extra.get("fatal"):
            return self.make_outcome(state, TurnStatus.FAILED, Reason.AGENT_ERROR)
        if not state.extra.get("complete"):
            # legacy: 'Missing terminal Codex result'
            return self.make_outcome(state, TurnStatus.FAILED, Reason.NO_ANSWER,
                                     answer if False else None)
        if answer:
            return self.make_outcome(state, TurnStatus.COMPLETED, Reason.OK, answer)
        return self.make_outcome(state, TurnStatus.FAILED, Reason.AGENT_ERROR)

    def _parse_answer(self, text: Any) -> Optional[str]:
        """严格版：恰好一个 ``answer`` 键、非空、且不超过上限（对齐 legacy ``result()``）。"""
        if not isinstance(text, str) or not text.strip():
            return None
        module = self._module()
        safety = _safety()
        try:
            value = safety.parse_answer_json(text)
        except ValueError:
            return None
        if not isinstance(value, dict) or set(value) != {"answer"}:
            return None
        answer = value.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return None
        if len(answer.strip()) > int(module.MAX_ANSWER):
            return None
        return answer.strip()


# ---------------------------------------------------------------------------
# DSH
# ---------------------------------------------------------------------------
@register
class DshAdapter(AgentAdapter):
    """DSH CLI（``headless --patch <overlay> --json -``）。

    严格度对齐 ``dsh-consumer.py:128-199``（``_RunEvents``）：``session`` 必须在
    ``status.phase=turn_end(reason.kind=completed)`` 之前，``final`` 必须晚于它且不
    被截断，**未知事件类型一律失败**。少了任何一条，就等于把"没跑完"当成"跑完了"。
    """

    name = "dsh"
    aliases = ("deepseek-harness",)
    binary_name = "dsh"
    binary_env = "DSH_AGENT_BINARY"
    capabilities = Capabilities(
        name="dsh",
        stream_format="json (`--json`, NDJSON)",
        supports_resume=False,
        supports_model_switch=False,
        emits_tool_events=True,
        emits_usage=True,
        denies_tools=True,
        deny_enforcement="overlay(yaml)+env+stream-backstop",
        notes=("prompt 走 stdin；每次 run 一份 overlay（关 5 个工具 + consumer preset）；"
               "家目录用 DSH_HOME 隔离（HOME 重定向对 dsh 无效，已实测）"),
    )

    @property
    def max_output_bytes(self) -> int:  # type: ignore[override]
        return int(self._module().MAX_OUTPUT_BYTES)

    def _module(self):
        return _load("dsh", "dsh-consumer.py")

    def preflight(self, req: TurnRequest, ctx: PrepareContext) -> None:
        module = self._module()
        self._isolate_dsh_home(module, ctx)
        overlay = pathlib.Path(ctx.run_dir) / module.OVERLAY_NAME
        if overlay.exists():
            return
        route = None
        resolver = getattr(module, "_credential_route", None)
        if callable(resolver):
            try:
                route = resolver(req.workspace or ctx.workspace)
            except Exception:  # noqa: BLE001 - 凭据路由失败交给运行时 fail-closed
                route = None
        text = module._overlay_text(route)
        overlay.write_text(text, encoding="utf-8")
        platform = getattr(ctx, "platform", None)
        if platform is not None:
            platform.harden_path(str(overlay), 0o600)
        else:
            os.chmod(str(overlay), 0o600)

    def build_argv(self, req: TurnRequest, ctx: PrepareContext) -> Sequence[str]:
        module = self._module()
        # 机器级 provider 补丁与 legacy 入口同一个来源（`dsh-consumer.extra_patch_args`），
        # 不在这里另写一份判定：两个入口的 argv 必须一模一样。
        return ([ctx.binary, "headless", "--patch",
                 str(pathlib.Path(ctx.run_dir) / module.OVERLAY_NAME)]
                + list(module.extra_patch_args()) + ["--json", "-"])

    def prompt_on_stdin(self, req: TurnRequest) -> Optional[str]:
        return req.prompt

    def env_patch(self, req: TurnRequest, ctx: PrepareContext) -> Mapping[str, str]:
        # 与既有入口一致：读盘默认只读，写操作由 overlay/preset 决定。
        # DSH_HOME 必须显式指向本次 run 的私有目录 —— 见 _isolate_dsh_home 的实测说明。
        # 机器级环境（如内部 CA 的 NODE_EXTRA_CA_CERTS）从同一份文件读，策略键写在后面
        # 强制覆盖，机器配置不能改掉沙箱/私有 home 这两条不变量。
        environment = dict(self._module().extra_env())
        environment.update({
            "DSH_PERMISSION_MODE": "read-only",
            "DSH_HOME": self._private_home(ctx),
        })
        return environment

    @staticmethod
    def _private_home(ctx: PrepareContext) -> str:
        return os.path.join(ctx.run_dir, "dsh-home")

    def _isolate_dsh_home(self, module, ctx: PrepareContext) -> None:
        """把 DSH 的家目录挪进 run 目录，并把凭据"只连不拷"地接进去。

        **为什么不能只靠 HOME 重定向（实测，不是推测）**：``HOME=/tmp/x dsh headless``
        仍然去写**真实用户的家目录**（DSH 用 passwd 条目解析，不看 ``$HOME``）
        （node 的 ``os.userInfo().homedir`` 就是这样）。只有 ``$DSH_HOME`` 能真正改道。
        所以：``DSH_HOME=<run>/dsh-home``，再把真实 ``$DSH_HOME/.credentials.yaml``
        软链进来（符号链接 = 不复制密钥；DSH 刷新 token 时写回真实文件，与 legacy 行为一致）。
        """
        private = pathlib.Path(self._private_home(ctx))
        private.mkdir(parents=True, exist_ok=True)
        platform = getattr(ctx, "platform", None)
        if platform is not None:
            platform.harden_path(str(private), 0o700, is_dir=True)
        real = pathlib.Path(module._dsh_home())
        for name in (".credentials.yaml", ".env"):
            source = real / name
            if not source.is_file():
                continue
            target = private / name
            if target.exists() or target.is_symlink():
                continue
            try:
                target.symlink_to(source)
            except OSError:
                shutil.copy2(str(source), str(target))
                os.chmod(str(target), 0o600)

    def deny_violation(self, state: TurnState, event: StreamEvent) -> Optional[str]:
        if event.kind != "tool":
            return None
        safety = _safety()
        arguments = (event.raw or {}).get("input")
        reason = safety.tool_violation(event.tool_name or "", arguments)
        if reason is None and safety.delete_operation(arguments):
            reason = "DeleteOperation"
        return reason

    def parse_line(self, line: str) -> Optional[StreamEvent]:
        text = line.strip()
        if not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if not isinstance(payload, Mapping):
            return None
        module = self._module()
        kind = str(payload.get("type") or "")
        if kind not in module.ALLOWED_EVENTS:
            # legacy: 'Unexpected DSH event'（fail closed，不静默忽略）
            return StreamEvent(kind="error", raw=payload)
        if kind == "tool_call":
            return StreamEvent(
                kind="tool", raw=payload, tool_name=str(payload.get("tool") or "")
            )
        if kind == "session":
            return StreamEvent(kind="init", raw=payload)
        if kind == "status":
            return StreamEvent(kind="status", raw=payload)
        if kind == "text":
            return StreamEvent(
                kind="text", raw=payload,
                text=str(payload.get("text") or payload.get("content") or ""),
            )
        if kind == "final":
            return StreamEvent(kind="result", raw=payload)
        # thinking / tool_result 是良性事件，legacy 入口对它们**不做动作**（只落盘）：
        # 映射成 note，绝不能落进 status 分支。真机踩过：thinking 带不带 phase 取决于
        # 模型，一旦当成 status 就 `invalid dsh status`，整轮已生成的答案被判失败
        # （2026-11-05，macOS 同事机第一条真实 @ 消息）。
        return StreamEvent(kind="note", raw=payload)

    def on_event(self, state: TurnState, event: StreamEvent) -> None:
        payload = event.raw or {}
        if event.kind == "init":
            session = payload.get("sessionId")
            if not isinstance(session, str) or not session:
                state.extra["fatal"] = "invalid dsh session"
                return
            expected = state.req.workspace
            if payload.get("cwd") != expected:
                state.extra["fatal"] = "dsh session workspace mismatch"
                return
            state.resume_key = session
            state.extra["session"] = session
            return
        if event.kind == "text":
            if state.extra.get("final") is not None:
                state.extra["fatal"] = "dsh event after final"
                return
            if event.text:
                state.add_text(event.text)
            return
        if event.kind == "tool":
            if state.extra.get("complete"):
                state.extra["fatal"] = "unexpected dsh tool call after turn_end"
            return
        if event.kind == "result":
            if not state.extra.get("session") or not state.extra.get("complete") or payload.get("truncated"):
                state.extra["fatal"] = "dsh final without a successful turn"
                return
            state.extra["final"] = payload.get("text")
            return
        if event.kind == "note":
            # thinking / tool_result：legacy 入口只落盘、不做动作，这里同样放行。
            return
        if event.kind == "error":
            state.extra["fatal"] = "unexpected dsh event"
            return
        if event.kind != "status":
            state.extra["fatal"] = "unexpected dsh event"
            return
        # status
        phase = payload.get("phase")
        if state.extra.get("final") is not None:
            state.extra["fatal"] = "dsh event after final"
            return
        if phase == "turn_end":
            reason = payload.get("reason")
            if state.extra.get("complete") or not isinstance(reason, Mapping) or reason.get("kind") != "completed":
                state.extra["fatal"] = "dsh turn did not complete"
                return
            state.extra["complete"] = True
            return
        if phase not in ("turn_start", "step_start", "step_end") or state.extra.get("complete"):
            state.extra["fatal"] = "invalid dsh status"

    def classify(self, state: TurnState) -> Outcome:
        if state.denied_tool:
            return self.make_outcome(state, TurnStatus.FAILED, Reason.DENIED_TOOL)
        answer = self._parse_answer(state.extra.get("final"))
        if state.timed_out:
            return self.make_outcome(state, TurnStatus.INCOMPLETE, Reason.TIME_BUDGET, answer)
        if state.extra.get("fatal"):
            return self.make_outcome(state, TurnStatus.FAILED, Reason.AGENT_ERROR)
        if state.extra.get("final") is None:
            return self.make_outcome(state, TurnStatus.FAILED, Reason.NO_ANSWER)
        if answer:
            return self.make_outcome(state, TurnStatus.COMPLETED, Reason.OK, answer)
        return self.make_outcome(state, TurnStatus.FAILED, Reason.AGENT_ERROR)

    def _parse_answer(self, text: Any) -> Optional[str]:
        """答案只由 ``consumer_safety.finalize_reply`` 解析（正文、脱敏、长度上限一处定义）。"""
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            return _safety().finalize_reply(text)['answer'] or None
        except ValueError:
            return None

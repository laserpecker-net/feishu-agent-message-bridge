#!/usr/bin/env python3
"""Codex text generator; only consumer_runtime may talk to the bridge.

The consumer may reply, retrieve information (read files, search, browse the
web) and execute/complete tasks (run commands, edit and create files) inside a
`workspace-write` sandbox with approvals disabled. Deleting, destructive
commands, secret access and delegation/MCP stay forbidden:

  * the sandbox confines writes to the workspace and network is explicitly on;
  * this adapter's JSONL backstop inspects every streamed item and kills the
    whole process group on the first denied item type, destructive/secret
    command or secret path. It runs after dispatch, so it is a backstop, not a
    pre-execution ban;
  * `consumer_safety.finalize_reply` in `consumer_runtime.process` (the adapter only validates)
    keeps key/password material out of the outbound reply;
  * `agent_prompt.build_prompt` states the same model to the model.

All safety patterns live in ``consumer_safety.py``; this file only wires them to
the Codex event shapes (verified against codex-cli 0.147.0).
"""
import json
import os
import pathlib
import selectors
import shutil
import signal
import subprocess
import threading
import time
import uuid

from consumer_runtime import main, owner_name_at
from agent_prompt import build_prompt
from consumer_safety import (
    MAX_ANSWER_CHARS,
    PATH_KEYS,
    blocked_path,
    capabilities_report,
    delete_operation,
    denied_tool,
    parse_answer_json,
    require_posix_runtime,
    tool_violation,
)

TIMEOUT = 240
MAX_OUTPUT = 2 * 1024 * 1024
MAX_ANSWER = MAX_ANSWER_CHARS
SANDBOX = 'workspace-write'
APPROVAL_POLICY = 'never'
NETWORK_ACCESS = True
# Feature switches verified on codex-cli 0.147.0: no delegation, no app/browser/
# computer/image tools, no hooks or plugins. Shell + file edits stay enabled.
DISABLED_FEATURES = ('plugins', 'hooks', 'multi_agent', 'apps', 'browser_use',
                     'computer_use', 'image_generation')
ALLOWED_ITEM_TYPES = frozenset({
    'reasoning', 'agent_message', 'command_execution', 'file_change',
    'web_search', 'todo_list', 'plan_update',
})
# Item types that describe an action this consumer must never take.
DENIED_ITEM_TYPES = frozenset({
    'mcp_tool_call', 'collab_agent_tool_call', 'computer_call', 'browser_call',
    'image_generation',
})
DENIED_ITEM_PREFIXES = ('mcp', 'collab_agent', 'collabagent', 'subagent',
                        'computer_', 'browser_', 'image_generation')
SCHEMA = {'type': 'object', 'properties': {'answer': {'type': 'string', 'minLength': 1, 'maxLength': MAX_ANSWER}},
          'required': ['answer'], 'additionalProperties': False}


def _private_file(path, mode='wb'):
    return os.fdopen(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), mode)


def _write_json(path, value):
    with _private_file(path, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False)


def _replace_json(path, value):
    """Atomically replace an existing private JSON file (used for the receipt)."""
    temporary = path.with_name(path.name + '.tmp')
    with _private_file(temporary, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))


def _kill_group(process):
    # Kill the group even after the leader exits; descendants can keep its pipes open.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except OSError:
            # macOS can report EPERM for an already terminated process group.
            break
        if sig == signal.SIGTERM:
            time.sleep(0.1)
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _item_type(item):
    value = item.get('type')
    return value.strip().lower() if isinstance(value, str) else ''


def _iter_values(value, keys):
    """Yield string values stored under ``keys`` at any depth (file changes nest)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, str):
                yield item
            else:
                for nested in _iter_values(item, keys):
                    yield nested
    elif isinstance(value, list):
        for item in value:
            for nested in _iter_values(item, keys):
                yield nested


def _denied_item_type(item_type):
    return (item_type in DENIED_ITEM_TYPES
            or item_type.startswith(DENIED_ITEM_PREFIXES)
            or denied_tool(item_type))


def _item_reason(item):
    """Return a short capability-violation reason for one Codex item, or None."""
    item_type = _item_type(item)
    if not item_type:
        return 'InvalidItem'
    if item_type == 'error':
        # Skills/agent-role notices arrive as item errors; they are warnings, not failures.
        return None
    if item_type not in ALLOWED_ITEM_TYPES or _denied_item_type(item_type):
        return 'ItemType:' + item_type
    reason = tool_violation(item_type, item)
    if reason:
        return reason
    if item_type == 'file_change' and delete_operation(item):
        return 'DeleteOperation'
    keys = PATH_KEYS + (('query',) if item_type == 'web_search' else ())
    for value in _iter_values(item, keys):
        if blocked_path(value):
            return 'SecretPath'
    return None


def _answer_payload(text):
    """Parse the schema contract, tolerating a fence or prose around the object.

    The tolerant part lives in ``consumer_safety.parse_answer_json`` so every
    entry point accepts the same shapes; the schema check in ``result()`` stays
    strict (exactly one non-empty `answer` string).
    """
    if not isinstance(text, str):
        return None
    try:
        return parse_answer_json(text)
    except ValueError as error:
        raise RuntimeError('Codex answer is not a JSON object') from error


class _Stream:
    """Incremental JSONL reader with the capability backstop."""

    def __init__(self):
        self.leftover = b''
        self.complete = False
        self.answer = None
        self.warnings = 0
        self.violations = []

    def feed(self, chunk, final=False):
        self.leftover += chunk
        lines = self.leftover.split(b'\n')
        self.leftover = lines.pop()
        if final and self.leftover:
            lines.append(self.leftover)
            self.leftover = b''
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError) as error:
                raise RuntimeError('Invalid Codex stream JSON') from error
            if not isinstance(event, dict):
                raise RuntimeError('Invalid Codex stream event')
            if self.complete:
                raise RuntimeError('Codex emitted data after terminal result')
            self._event(event)

    def _event(self, event):
        kind = event.get('type')
        if kind in ('error', 'turn.failed') or event.get('error') or event.get('is_error'):
            raise RuntimeError('Codex reported an error')
        if kind == 'turn.completed':
            self.complete = True
            return
        if kind in ('thread.started', 'turn.started'):
            return
        if kind not in ('item.started', 'item.updated', 'item.completed'):
            raise RuntimeError('Unsupported Codex stream event')
        item = event.get('item')
        if not isinstance(item, dict):
            raise RuntimeError('Invalid Codex item')
        if _item_type(item) == 'error':
            self.warnings += 1
            return
        reason = _item_reason(item)
        if reason is not None:
            self.violations.append(reason)
            raise RuntimeError('Codex tool call policy violation')
        if kind == 'item.completed' and _item_type(item) == 'agent_message':
            self.answer = item.get('text')

    def result(self):
        if not self.complete:
            raise RuntimeError('Missing terminal Codex result')
        value = _answer_payload(self.answer)
        if (not isinstance(value, dict) or not isinstance(value.get('answer'), str)
                or not value['answer'].strip()):
            raise RuntimeError('Codex answer must be nonempty text')
        if set(value) - {'answer', 'ask_owner'}:
            raise RuntimeError('Codex answer has unexpected fields')
        # 正文和可选的 ask_owner 原样交给运行时：解析/脱敏/长度只有 finalize_reply 一处。
        return self.answer


def _workspace():
    """Validate the optional workspace at build time, before any event is claimed."""
    value = os.environ.get('FEISHU_CONSUMER_WORKSPACE') or None
    if not value:
        return None
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError('FEISHU_CONSUMER_WORKSPACE must be an absolute path')
    if not path.is_dir():
        raise RuntimeError('FEISHU_CONSUMER_WORKSPACE is not a directory: %s' % path)
    return str(path.resolve())


def _command(binary, run):
    command = [str(binary), '--ask-for-approval', APPROVAL_POLICY, 'exec', '--ephemeral',
               '--skip-git-repo-check', '--ignore-user-config',
               '--sandbox', SANDBOX, '--json', '--color', 'never']
    for feature in DISABLED_FEATURES:
        command += ['--disable', feature]
    command += ['-c', 'web_search="live"',
                '-c', 'sandbox_workspace_write.network_access=' + ('true' if NETWORK_ACCESS else 'false'),
                '-c', 'project_doc_max_bytes=0',
                '--output-schema', str(run / 'schema.json'), '-']
    return command


def build_agent(home: pathlib.Path):
    require_posix_runtime()
    workspace = _workspace()
    binary = os.environ.get('CODEX_CONSUMER_BINARY') or shutil.which('codex')
    if not binary or not pathlib.Path(binary).is_absolute() or not pathlib.Path(binary).is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError('An absolute executable Codex CLI path is required')
    home = pathlib.Path(home).resolve()
    auth_home = pathlib.Path(os.environ.get('CODEX_HOME') or pathlib.Path.home() / '.codex').expanduser().resolve()
    auth_file = auth_home / 'auth.json'
    if not auth_file.is_file() or not os.access(str(auth_file), os.R_OK):
        raise RuntimeError('Codex auth.json is required for the isolated generator')

    def generate(payload: dict, event: dict, stop_event: threading.Event) -> str:
        if stop_event.is_set():
            raise RuntimeError('Codex cancelled')
        base = home / 'codex-runs'
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        base.chmod(0o700)
        run = base / uuid.uuid4().hex
        run.mkdir(mode=0o700)
        codex_home = run / 'codex-home'
        codex_home.mkdir(mode=0o700)
        (codex_home / 'auth.json').symlink_to(auth_file)
        # Avoid loading host skills/agents; keep only the existing auth reference.
        environment = dict(os.environ, HOME=str(run), CODEX_HOME=str(codex_home))
        _write_json(run / 'schema.json', SCHEMA)
        prompt = build_prompt(payload, workspace, timeout_seconds=TIMEOUT,
                              owner_name=owner_name_at(home),
                              teachings=payload.get('teachings'))
        with _private_file(run / 'prompt.txt', 'w') as handle:
            handle.write(prompt)
        receipt = {
            'event_id': event.get('id'),
            'timeout': TIMEOUT,
            'model_selection': 'CLI default; user config ignored',
            'workspace': workspace,
            'sandbox_mode': SANDBOX,
            'approval_policy': APPROVAL_POLICY,
            'network_access': NETWORK_ACCESS,
            'capabilities': capabilities_report()['capabilities'],
            'disabled_features': list(DISABLED_FEATURES),
            'capability_violations': [],
        }
        _write_json(run / 'init.json', receipt)
        command = _command(binary, run)
        process = None
        deadline = time.monotonic() + TIMEOUT
        stream = _Stream()
        try:
            with (run / 'prompt.txt').open('rb') as prompt_input, \
                 _private_file(run / 'stdout.ndjson') as stdout, \
                 _private_file(run / 'stderr.txt') as stderr, \
                 selectors.DefaultSelector() as selector:
                if stop_event.is_set():
                    raise RuntimeError('Codex cancelled')
                process = subprocess.Popen(command, cwd=workspace or str(run), stdin=prompt_input,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True, env=environment, umask=0o077)
                for pipe, target in ((process.stdout, stdout), (process.stderr, stderr)):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, target)
                total = 0
                while selector.get_map():
                    if stop_event.is_set():
                        raise RuntimeError('Codex cancelled')
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Codex timed out')
                    for key, _ in selector.select(timeout=0.025):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        total += len(chunk)
                        if total > MAX_OUTPUT:
                            raise RuntimeError('Codex output limit exceeded')
                        key.data.write(chunk)
                        key.data.flush()
                        if key.fileobj is process.stdout:
                            stream.feed(chunk)
                while process.poll() is None:
                    if stop_event.wait(0.025):
                        raise RuntimeError('Codex cancelled')
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Codex timed out')
                if stop_event.is_set():
                    raise RuntimeError('Codex cancelled')
                if process.returncode:
                    raise RuntimeError('Codex exit code ' + str(process.returncode))
                stream.feed(b'', final=True)
                answer = stream.result()
                _write_json(run / 'answer.json', {'answer': answer})
                return answer
        except Exception as error:
            try:
                _write_json(run / 'error.json', {'error': str(error)[:1000]})
            except OSError:
                pass
            raise
        finally:
            if process is not None:
                _kill_group(process)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
            try:
                receipt['capability_violations'] = list(stream.violations)
                receipt['capability_warnings'] = stream.warnings
                _replace_json(run / 'init.json', receipt)
            except OSError:
                pass

    return generate


if __name__ == '__main__':
    main(build_agent, default_consumer='codex-cli-main', default_kind='codex')

"""跨平台可移植性的**结构性**自检（在 macOS 上也能验的部分）。

Windows 没有环境，所以这里不假装"跑过了"，而是把三类"只能在 Windows 上炸"的错误
变成**静态可判定的规则**：

1. POSIX-only 的调用不许出现在平台层之外（否则一定会在某个平台上炸）；
2. adapter 层不许拿到 ``subprocess``（否则 stdin/进程组卫生又开始各写各的）；
3. 已知危险 API（``os.kill(pid, 0)`` 在 Windows 上等于 TerminateProcess）必须有平台门。

这些规则用 AST 判，不看注释、不看字符串，所以文档里写多少遍"killpg"都不影响结论。
"""

import ast
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "agentkit"
sys.path.insert(0, str(ROOT))

#: 只在 POSIX 上有意义、且已经被平台族封装的调用
POSIX_ONLY_ATTRS = {
    "killpg",
    "set_blocking",
    "getpgid",
    "creatpgid",
    "setsid",
    "getuid",
    "geteuid",
    "umask",
    "fork",
    "preexec_fn",
    "sigmask",
    "waitid",
}
#: 只在 POSIX 上存在的模块
POSIX_ONLY_MODULES = {"fcntl", "pwd", "grp", "resource", "termios"}


def python_files(directory=PKG):
    return sorted(path for path in pathlib.Path(directory).rglob("*.py")
                  if "__pycache__" not in path.parts)


def parsed(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _platform_guarded(path):
    """``rollout.py`` 是 macOS/launchd 专用：允许它用 os.getuid，**前提**是它自己拒绝别处运行。"""
    if path.name != "rollout.py":
        return False
    source = path.read_text(encoding="utf-8")
    return 'sys.platform != "darwin"' in source


def test_posix_only_calls_live_only_in_the_posix_platform():
    offenders = {}
    for path in python_files():
        if path.name == "posix.py" or _platform_guarded(path):
            continue
        tree = parsed(path)
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in POSIX_ONLY_ATTRS:
                found.add(node.attr)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in POSIX_ONLY_MODULES:
                        found.add("import " + alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in POSIX_ONLY_MODULES:
                    found.add("from " + node.module)
        if found:
            offenders[str(path.relative_to(ROOT))] = sorted(found)
    assert offenders == {}, offenders


def test_idempotency_never_probes_a_pid_with_os_kill_on_windows():
    """``os.kill(pid, 0)`` 在 Windows 上是 TerminateProcess：必须有平台门。"""
    source = (PKG / "idempotency.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_default_pid_alive"
    )
    guards_nt = False
    for node in ast.walk(function):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute):
            if node.left.attr == "name" and any(
                isinstance(c, ast.Constant) and c.value == "nt" for c in node.comparators
            ):
                guards_nt = True
    assert guards_nt, "Windows 必须提前返回 None，不能走到 os.kill(pid, 0)"


def test_rollout_refuses_to_run_outside_macos(monkeypatch):
    """例外只对"自己会拒绝别处运行"的模块成立（launchd 控制器）。"""
    import sys as _sys

    from agentkit import rollout

    monkeypatch.setattr(_sys, "platform", "linux")
    with pytest.raises(SystemExit) as excinfo:
        rollout.main(["status"])
    assert "macOS" in str(excinfo.value)


def test_windows_platform_does_not_probe_pids(monkeypatch):
    from agentkit.platform.windows import WindowsProcessPlatform

    def boom(*args, **kwargs):  # pragma: no cover - 被调用就是失败
        raise AssertionError("Windows 上不许探测 pid")

    monkeypatch.setattr(os, "kill", boom)
    assert WindowsProcessPlatform().pid_alive(1234) is None


def test_adapters_never_import_subprocess():
    """adapter 层拿不到 subprocess，stdin/进程组卫生就不可能被写漏。"""
    offenders = []
    for path in python_files(PKG / "adapters"):
        tree = parsed(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "subprocess" for alias in node.names
            ):
                offenders.append(path.name)
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                offenders.append(path.name)
    assert offenders == [], offenders


def test_every_adapter_hook_names_a_real_capability_claim():
    """capabilities 的 deny_enforcement 不许空着，也不许写实现不了的词。"""
    from agentkit.adapters import get_adapter

    # 只查内置的四个：其他测试会往全局注册表里塞临时 adapter
    for name in ("cursor", "codex", "dsh", "fake"):
        caps = get_adapter(name).capabilities
        assert caps.deny_enforcement and caps.deny_enforcement != "none", name
        assert caps.stream_format != "unknown", name


def test_windows_family_uses_the_thread_reader_and_posix_the_selector_reader():
    import io

    from agentkit.platform.posix import PosixLineReader, PosixProcessPlatform
    from agentkit.platform.stream import ThreadLineReader
    from agentkit.platform.windows import WindowsProcessPlatform

    class _Proc(object):
        stdout = io.StringIO("")

    # Windows 族必须给线程读取器（管道不可 select）
    reader = WindowsProcessPlatform().line_reader(_Proc())
    assert isinstance(reader, ThreadLineReader)
    reader.close()
    # POSIX 族必须给 selectors 读取器，且这个类只存在于 posix 模块
    assert "PosixLineReader" in PosixProcessPlatform.line_reader.__code__.co_names
    assert PosixLineReader.__module__ == "agentkit.platform.posix"

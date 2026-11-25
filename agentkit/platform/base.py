"""平台层的抽象（Abstract Factory 的产品族接口）。

为什么用抽象工厂而不是散落的 ``if sys.platform``：一次 turn 需要**一整套彼此配套**的
原语（进程创建方式、终止方式、路径约定、编码、文件锁）。这些原语必须来自同一个平台，
否则会出现"用 POSIX 的 killpg 去杀一个 Windows 进程"这类只在别人机器上炸的组合错误。
抽象工厂保证"一整套一起换"。

平台层只做两件事，而这两件事恰好是**最脏且最容易漏**的：
1. 组环境（``prepare_env``）—— HOME / TMPDIR / PWD 重定向 + UTF-8 钉死 + 密钥清洗；
2. 造进程 / 杀进程树（``spawn`` / ``terminate``）。

adapter 拿不到 ``subprocess``，只能拿 ``Invocation`` 交给平台 —— 这样"忘记设
stdin=DEVNULL"或"只杀掉父进程不杀子进程"在结构上就不可能发生。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence

from ..errors import PolicyRefusal

#: 名字命中这些片段的变量，默认**不进**子进程（除非 adapter 显式 allowlist）。
#: 目的与 HarnessRouter 的 relay 同源：真凭据不进 agent 进程，agent 只该拿到占位符。
SECRET_NAME_PATTERN = re.compile(
    r"(API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE[_-]?KEY|SESSION[_-]?KEY|COOKIE)",
    re.IGNORECASE,
)

#: 无论调用方怎么传，都不允许被改写的变量（改写等于把 agent 送出工作区或换掉解释器）。
PROTECTED_VARS = frozenset(
    {
        "HOME",
        "USERPROFILE",
        "PWD",
        "TMPDIR",
        "TMP",
        "TEMP",
        "PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
    }
)

#: 明确剥掉的注入类变量（不剥会导致"同一份代码在不同机器上加载不同库"）。
DROPPED_VARS = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
    }
)


@dataclass(frozen=True)
class WorkspacePaths:
    """一个 workspace 内 agentkit 自己的目录约定（各平台一致）。"""

    workspace: str

    def _join(self, *parts: str) -> str:
        return os.path.join(self.workspace, ".agentkit", *parts)

    @property
    def root(self) -> str:
        return self._join()

    @property
    def home(self) -> str:
        """子进程的 HOME。放在 workspace 内，transcript 才会跟着 workspace 走。"""
        return self._join("home")

    @property
    def tmp(self) -> str:
        return self._join("tmp")

    @property
    def turns(self) -> str:
        """幂等记录目录。"""
        return self._join("turns")

    @property
    def logs(self) -> str:
        return self._join("logs")

    def ensure(self) -> "WorkspacePaths":
        for path in (self.root, self.home, self.tmp, self.turns, self.logs):
            os.makedirs(path, exist_ok=True)
        return self


class ProcessPlatform(ABC):
    """一个操作系统的"进程族"抽象工厂。子类必须提供一整套配套原语。"""

    name = "abstract"

    # ---- 工厂方法 -------------------------------------------------------
    @abstractmethod
    def spawn(self, inv: "InvocationLike") -> subprocess.Popen:
        """创建子进程。实现必须保证：stdin 是 DEVNULL（或等价）、子进程自成进程组。"""

    @abstractmethod
    def terminate(self, proc: subprocess.Popen, grace_s: float = 5.0) -> None:
        """终止**整棵进程树**，不是只杀父进程。

        只杀父进程的后果在 HarnessRouter 里有实测记录：CLI 的 shell 子进程继承 stdout
        管道，父进程一死 reader 仍阻塞，表现为"取消后卡住整个子进程的时长"。
        """

    @abstractmethod
    def line_reader(self, proc: subprocess.Popen):
        """返回该平台的 :class:`~agentkit.platform.stream.LineReader`。

        **必须在平台层**：POSIX 用 ``selectors`` 对管道做非阻塞读，而 Windows 的
        ``selectors`` 只支持 socket（管道会抛 OSError），只能用线程 + 队列。
        这是"一套原语必须来自同一平台"的最强例证。
        """

    # ---- 平台能力 -------------------------------------------------------
    def harden_path(self, path: str, mode: int = 0o600, is_dir: bool = False) -> None:
        """把文件/目录收紧到"只有本人可读"。

        POSIX 靠 mode 位；**Windows 上 mode 位基本不生效**（`os.chmod` 只影响只读标志），
        真正的等价物是 ACL（各平台实现自己覆盖）。默认实现是 chmod，失败不抛 ——
        一个跑在 FAT32/exFAT 上的 workspace 不该让整个 turn 失败。
        """
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    def pid_alive(self, pid: int) -> Optional[bool]:
        """进程是否还活着。返回 None 表示"这个平台判断不了"（交给时间阈值）。"""
        return None

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def is_alive(self, proc: subprocess.Popen) -> bool:
        return proc.poll() is None

    @property
    def supports_process_groups(self) -> bool:
        return True

    def paths(self, workspace: str) -> WorkspacePaths:
        return WorkspacePaths(workspace=workspace)

    # ---- 环境组装（各平台共享 95% 逻辑）--------------------------------
    def prepare_env(
        self,
        base: Optional[Mapping[str, str]] = None,
        workspace: str = "",
        extra: Optional[Mapping[str, str]] = None,
        allow_secrets: Sequence[str] = (),
        deny_overrides: Sequence[str] = (),
        home_mode: str = "redirect",
    ) -> Dict[str, str]:
        """组装子进程环境。三件事：

        1. **重定向**：HOME / TMPDIR / PWD 全部落到 workspace 内。
           * HOME 不重定向 → transcript 落在 workspace 外 → 沙箱回收/换机后 resume 失忆
             （HarnessRouter 实测：两个 CLI 都把 transcript 写在 ``$HOME`` 下）；
           * PWD 不重定向 → Bun 系 CLI 信 ``$PWD`` 胜过 ``getcwd()``，行为按环境漂移；
           * TMPDIR 不重定向 → 产物/临时文件留在宿主机，既收不到也清不掉。
        2. **钉死编码**：Windows 控制台常是 cp936，不钉 UTF-8 会得到乱码或 UnicodeDecodeError。
        3. **清洗凭据**：名字像密钥的变量默认不进子进程；adapter 需要时才显式 allowlist。
        """
        env: Dict[str, str] = {}
        source = dict(os.environ if base is None else base)
        allow = {name.upper() for name in allow_secrets}
        blocked = {name.upper() for name in deny_overrides}

        for key, value in source.items():
            upper = key.upper()
            if key in DROPPED_VARS or upper in DROPPED_VARS:
                continue
            if key in PROTECTED_VARS or upper in PROTECTED_VARS:
                continue  # 受保护的变量由本函数重新写入
            if upper in blocked:
                continue
            if SECRET_NAME_PATTERN.search(key) and upper not in allow:
                continue
            env[key] = value

        paths = self.paths(workspace).ensure() if workspace else None
        if paths is not None:
            if home_mode == "inherit":
                # **实测**：cursor-agent 的登录态就在真实 $HOME 下（`HOME=/tmp/x cursor-agent
                # status` → "Not logged in"），legacy 也从不改它的 HOME（``bridge/agent.py``）。
                # 对这类 AGENT，隔离 HOME 只会让它在真实环境里直接失去凭据 —— 那不是安全，
                # 那是把可用性当成了安全。临时目录仍然重定向，工作区限制靠 cwd/策略文件。
                env["HOME"] = source.get("HOME") or os.path.expanduser("~")
                env["USERPROFILE"] = source.get("USERPROFILE") or env["HOME"]
            else:
                env["HOME"] = paths.home
                env["USERPROFILE"] = paths.home  # Windows 侧同义变量
            env["PWD"] = workspace
            env["TMPDIR"] = paths.tmp
            env["TMP"] = paths.tmp
            env["TEMP"] = paths.tmp
            if home_mode != "inherit":
                # 让子进程自己创建的工作区外路径也收敛回 workspace
                env["XDG_CACHE_HOME"] = os.path.join(paths.home, ".cache")
                env["XDG_CONFIG_HOME"] = os.path.join(paths.home, ".config")
                env["XDG_DATA_HOME"] = os.path.join(paths.home, ".local", "share")

        # 受保护变量里的 PATH 与解释器相关项回填
        if "PATH" in source:
            env["PATH"] = source["PATH"]
        for keep in ("LANG", "LC_ALL", "TERM", "SystemRoot", "COMSPEC", "PATHEXT", "WINDIR"):
            if keep in source and keep not in env:
                env[keep] = source[keep]

        # 编码：三条一起设，因为不同 CLI 读不同的那条
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        if os.name != "nt":
            env.setdefault("LC_ALL", "en_US.UTF-8")
            env.setdefault("LANG", "en_US.UTF-8")
        env.setdefault("NO_COLOR", "1")

        if extra:
            for key, value in extra.items():
                if key in PROTECTED_VARS or key.upper() in PROTECTED_VARS:
                    raise PolicyRefusal(
                        "adapter may not override protected env var %r" % (key,)
                    )
                if key.upper() in blocked:
                    raise PolicyRefusal("adapter may not set %r" % (key,))
                if SECRET_NAME_PATTERN.search(key) and key.upper() not in allow:
                    raise PolicyRefusal(
                        "adapter may not inject secret-shaped env var %r without allow_secrets"
                        % (key,)
                    )
                env[key] = str(value)
        return env


class LockPlatform(ABC):
    """跨进程互斥的抽象工厂（与进程族分开，因为两者的可移植性不同）。

    POSIX 用 ``flock``；原生 Windows 没有等价物，本仓的立场（``WINDOWS-DECISION.md``）
    是把 bridge 本体留在 POSIX，所以 Windows 实现在策略层直接拒绝，而不是假装能锁。
    """

    name = "abstract"

    @abstractmethod
    def acquire(self, path: str) -> object:
        """获取排他锁，返回一个持有者对象（配合 ``release``）。"""

    @abstractmethod
    def release(self, handle: object) -> None:
        ...


#: 平台层接受的调用描述（避免和 types.Invocation 形成循环导入）。
class InvocationLike(object):  # pragma: no cover - 仅用于类型标注
    argv: Sequence[str]
    env: Mapping[str, str]
    cwd: str
    stdin_devnull: bool
    encoding: str

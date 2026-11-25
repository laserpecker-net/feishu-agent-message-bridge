"""注册 / 生效 / 重载 / 停用 agentkit consumer（launchd，macOS）。

职责边界（刻意做小）：

- **写**：`install.py --with-agentkit` 负责把 agentkit 打包进 release 并写 plist；
- **注册**：`register` 只调 ``feishu-bridge events register``，不 claim、不加载进程；
- **生效**：`start` 加载 LaunchAgent（幂等：已加载就不重复 bootstrap）；
- **重载**：`restart` 必须是 bootout+bootstrap —— 只 ``kickstart -k`` 会让进程继续跑
  launchd 缓存的旧 ProgramArguments，也就是 install 换了 release 却还在跑旧代码；
- **停用**：`stop` 卸载并把 plist 挪进 ``disabled-consumers/``，登录时不会被 launchd 拉起。

旧的 Cursor 引擎（`bridge/consumer.py`，label ``io.feishu-agent.cursor-consumer``）
已整体删除，所以这里不再有"接管/回滚到 cursor"的分支：回到上一个版本用
``feishu-bridge rollback --to <release_id>``（release 是自包含的，回滚不依赖任何 plist）。

所有动作都可重入：已加载的 start 不重复 bootstrap，未加载的 stop 不报错。
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

AGENTKIT_LABEL = "io.feishu-agent.agentkit-consumer"
LAUNCHCTL = "/bin/launchctl"
DEFAULT_AGENT = "cursor"
DEFAULT_CONSUMER = "agentkit-cursor"
#: 停用的 consumer 的 plist 不留在 LaunchAgents 里：那里的 plist 带 RunAtLoad/KeepAlive，
#: 登录时会被 launchd 自动拉起，等于把"已经停掉的服务"偷偷恢复。
PARKED_DIR = "disabled-consumers"
LOAD_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# 平台小工具（全部可注入，测试不需要真的 launchctl）
# ---------------------------------------------------------------------------
def _default_run(argv: List[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _domain() -> str:
    return "gui/%s" % os.getuid()


def _service_dir() -> Path:
    return Path.home() / "Library/LaunchAgents"


def plist_path(home: Optional[str] = None) -> Path:
    """plist 固定落在 LaunchAgents；``home`` 只用于旧布局兜底。"""
    override = os.environ.get("AGENTKIT_CONSUMER_PLIST")
    if override:
        return Path(override)
    return _service_dir() / (AGENTKIT_LABEL + ".plist")


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise SystemExit(
            "agentkit-consumerctl 只在 macOS/launchd 上实现；"
            "WSL2/Linux 用 systemd --user（install.py 会写 .service）"
        )


# ---------------------------------------------------------------------------
# launchd
# ---------------------------------------------------------------------------
def launch_status(label: str, run: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    run = run or _default_run
    result = run([LAUNCHCTL, "print", "%s/%s" % (_domain(), label)], 20)
    data: Dict[str, Any] = {"label": label, "loaded": result.returncode == 0}
    if result.returncode:
        data["error"] = (result.stderr or "").strip()[:200]
        return data
    for line in (result.stdout or "").splitlines():
        text = line.strip()
        if text.startswith("pid = "):
            data["pid"] = int(text.split("=", 1)[1].strip() or 0)
        elif text.startswith("state = "):
            data["state"] = text.split("=", 1)[1].strip()
    return data


def _bootstrap(plist: Path, run: Callable[..., Any]) -> None:
    result = run([LAUNCHCTL, "bootstrap", _domain(), str(plist)], 60)
    if result.returncode and "already bootstrapped" not in (result.stderr or ""):
        raise RuntimeError("bootstrap 失败：%s" % (result.stderr or "").strip()[:300])


def _bootout(label: str, run: Callable[..., Any]) -> None:
    result = run([LAUNCHCTL, "bootout", "%s/%s" % (_domain(), label)], 60)
    if result.returncode and "No such process" not in (result.stderr or ""):
        raise RuntimeError("bootout 失败：%s" % (result.stderr or "").strip()[:300])


def _wait_loaded(label: str, loaded: bool, run: Callable[..., Any],
                 timeout_s: Optional[float] = None) -> bool:
    # 默认值在调用时取模块常量（而不是定义时绑定），测试才能把等待压到毫秒级。
    deadline = time.monotonic() + (LOAD_TIMEOUT_S if timeout_s is None else timeout_s)
    while time.monotonic() < deadline:
        if launch_status(label, run)["loaded"] is loaded:
            return True
        time.sleep(0.2)
    return launch_status(label, run)["loaded"] is loaded


# ---------------------------------------------------------------------------
# 读取 plist：consumer 身份的唯一真相
# ---------------------------------------------------------------------------
def read_plist(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise SystemExit(
            "找不到 %s：先运行 `python3 install.py --with-agentkit` 生成它（只写不加载）" % path
        )
    payload = plistlib.loads(path.read_bytes())
    argv = [str(item) for item in payload.get("ProgramArguments", [])]
    options: Dict[str, str] = {}
    index = 1
    while index + 1 < len(argv):
        flag = argv[index]
        if flag.startswith("--"):
            options[flag[2:]] = argv[index + 1]
            index += 2
            continue
        index += 1
    return {
        "label": payload.get("Label"),
        "program": argv[0] if argv else "",
        "script": argv[1] if len(argv) > 1 else "",
        "agent": options.get("agent", DEFAULT_AGENT),
        "consumer": options.get("consumer", DEFAULT_CONSUMER),
        "kind": options.get("kind", "other"),
        "home": options.get("home"),
        "workspace": options.get("workspace"),
        "around": argv,
    }


def _park(plist: Path, home: str) -> Optional[Path]:
    """把停用的 plist 挪出 LaunchAgents（保留内容，回滚时原样挪回）。

    这里对 ``home`` 做硬校验：plist 里读不到 ``--home`` 时宁可报错，也不要在当前目录下
    建出一个叫 ``None/`` 的目录（这是真发生过的：单测里 home 传了 None）。
    """
    root = Path(home) if home else None
    if root is None or not root.is_absolute():
        raise RuntimeError("挪 plist 需要绝对的 home 路径，收到：%r" % (home,))
    parked_dir = root / PARKED_DIR
    parked_dir.mkdir(parents=True, exist_ok=True)
    target = parked_dir / plist.name
    if target.exists():
        target.unlink()
    plist.replace(target)
    return target


def _default_home() -> Optional[Path]:
    """`stop` 之后 `start` 时 plist 已被挪走，此时需要知道 home；从安装包里的 host 模块取兜底值。"""
    try:
        from bridge import host

        return Path(host.default_home())
    except Exception:  # noqa: BLE001 - 拿不到就退回显式 --home
        return None


def _find_parked(name: str, home: Optional[str] = None) -> Optional[Path]:
    """在已知 home、环境变量、安装默认 home 里找被挪走的 plist。"""
    roots = []
    if home:
        roots.append(Path(home))
    env = os.environ.get("FEISHU_BRIDGE_HOME")
    if env:
        roots.append(Path(env))
    fallback = _default_home()
    if fallback is not None:
        roots.append(fallback)
    for root in roots:
        candidate = root / PARKED_DIR / name
        if candidate.is_file():
            return candidate
    return None


def _unpark(parked: Path, home: str) -> Path:
    target = _service_dir() / parked.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    parked.replace(target)
    return target


# ---------------------------------------------------------------------------
# 注册 / 启动 / 停止
# ---------------------------------------------------------------------------
def _bridge_cli(home: str) -> str:
    return str(Path(home) / "bin" / "feishu-bridge")


def register(home: str, consumer: str, kind: str,
             run: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """只注册消费者身份：不 claim、不加载进程。可重复执行（幂等）。"""
    run = run or _default_run
    binary = _bridge_cli(home)
    if not Path(binary).is_file():
        raise SystemExit("找不到 %s；先运行 install.py" % binary)
    result = run([binary, "events", "register", "--consumer", consumer, "--kind", kind], 60)
    if result.returncode:
        raise RuntimeError("注册失败：%s" % (result.stderr or "").strip()[:300])
    return json.loads(result.stdout or "{}")


def start(plist: Path, run: Optional[Callable[..., Any]] = None,
          home: Optional[str] = None) -> Dict[str, Any]:
    """加载 agentkit consumer（幂等：已加载就直接返回 already-loaded）。"""
    run = run or _default_run
    plist = Path(plist)
    steps: List[str] = []
    if not plist.is_file():
        parked = _find_parked(plist.name, home)
        if parked is None:
            raise SystemExit(
                "找不到 %s：先运行 `python3 install.py --with-agentkit` 生成它（只写不加载）" % plist
            )
        plist = _unpark(parked, str(parked.parent.parent))
        steps.append("unpark:%s.plist" % AGENTKIT_LABEL)
    info = read_plist(plist)
    if launch_status(AGENTKIT_LABEL, run)["loaded"]:
        return {"agentkit": launch_status(AGENTKIT_LABEL, run), "steps": ["already-loaded"] + steps,
                "consumer": info["consumer"]}
    _bootstrap(plist, run)
    steps.append("bootstrap:%s" % plist.name)
    kick = run([LAUNCHCTL, "kickstart", "-k", "%s/%s" % (_domain(), AGENTKIT_LABEL)], 60)
    if kick.returncode:
        raise RuntimeError("kickstart 失败：%s" % (kick.stderr or "").strip()[:300])
    steps.append("kickstart:%s" % AGENTKIT_LABEL)
    if not _wait_loaded(AGENTKIT_LABEL, True, run):
        raise RuntimeError("agentkit consumer 没有加载成功；用 `status` 和 "
                           "%s/agentkit-consumer.stderr.log 排查" % info["home"])
    return {"agentkit": launch_status(AGENTKIT_LABEL, run), "steps": steps,
            "consumer": info["consumer"]}


def restart(plist: Optional[Path] = None,
            run: Optional[Callable[..., Any]] = None,
            home: Optional[str] = None) -> Dict[str, Any]:
    """真正重载 agentkit consumer：bootout → bootstrap → kickstart。

    为什么不能只 ``kickstart -k``：kickstart 复用 launchd 里**已缓存**的 ProgramArguments。
    ``install.py`` 把 plist 里的 release 路径改成新 release 之后，kickstart 起来的进程
    仍然指向旧 release —— 现场表现为"plist 里是新路径、进程 argv 里是旧路径"
    （真机踩过，见 docs/HISTORY.md §3.5）。bootout+bootstrap 才会重新读 plist。
    """
    run = run or _default_run
    steps: List[str] = []
    target = Path(plist) if plist else plist_path(home)
    if not target.is_file():
        parked = _find_parked(target.name, home)
        if parked is None:
            raise SystemExit("找不到 %s：先运行 `python3 install.py --with-agentkit`" % target)
        target = _unpark(parked, str(parked.parent.parent))
        steps.append("unpark:%s.plist" % AGENTKIT_LABEL)
    if launch_status(AGENTKIT_LABEL, run)["loaded"]:
        _bootout(AGENTKIT_LABEL, run)
        if not _wait_loaded(AGENTKIT_LABEL, False, run):
            raise RuntimeError("agentkit consumer 没有停下来；未做 bootstrap")
        steps.append("bootout:%s" % AGENTKIT_LABEL)
    _bootstrap(target, run)
    steps.append("bootstrap:%s" % target.name)
    kick = run([LAUNCHCTL, "kickstart", "-k", "%s/%s" % (_domain(), AGENTKIT_LABEL)], 60)
    if kick.returncode:
        raise RuntimeError("kickstart 失败：%s" % (kick.stderr or "").strip()[:300])
    steps.append("kickstart:%s" % AGENTKIT_LABEL)
    loaded = _wait_loaded(AGENTKIT_LABEL, True, run)
    if not loaded:
        raise RuntimeError("agentkit consumer 没有加载成功；用 `status` 排查")
    return {"restarted": True, "steps": steps,
            "agentkit": launch_status(AGENTKIT_LABEL, run)}


def stop(run: Optional[Callable[..., Any]] = None,
         home_arg: Optional[str] = None) -> Dict[str, Any]:
    """卸载 agentkit consumer 并把 plist 挪进 disabled-consumers/（登录不再自启）。"""
    run = run or _default_run
    steps: List[str] = []
    if launch_status(AGENTKIT_LABEL, run)["loaded"]:
        _bootout(AGENTKIT_LABEL, run)
        _wait_loaded(AGENTKIT_LABEL, False, run)
        steps.append("bootout:%s" % AGENTKIT_LABEL)
    else:
        steps.append("already-stopped")
    info = None
    path = plist_path()
    if path.is_file():
        try:
            info = read_plist(path)
        except (OSError, ValueError):
            info = None
    if info and info.get("home"):
        _park(path, info["home"])
        steps.append("park:%s.plist" % AGENTKIT_LABEL)
    return {"steps": steps, "agentkit": launch_status(AGENTKIT_LABEL, run)}


def status(plist: Optional[Path] = None,
           run: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    run = run or _default_run
    path = plist or plist_path()
    data: Dict[str, Any] = {
        "agentkit": launch_status(AGENTKIT_LABEL, run),
        "plist": str(path),
        "plist_present": path.is_file(),
    }
    home = None
    if path.is_file():
        info = read_plist(path)
        home = info.get("home")
        data["parked_agentkit"] = (Path(home) / PARKED_DIR / path.name).is_file() if home else None
        data.update(agent=info["agent"], consumer=info["consumer"], kind=info["kind"],
                    home=info["home"], workspace=info["workspace"],
                    release_script=info["script"])
    return data


def main(argv: Optional[List[str]] = None) -> int:
    _require_macos()
    parser = argparse.ArgumentParser(
        prog="agentkit-consumerctl",
        description="agentkit consumer 的注册/生效/回滚（写 plist 由 install.py 负责）",
    )
    parser.add_argument("--home", default=None, help="bridge home（默认读 plist 里的 --home）")
    parser.add_argument("--plist", default=None, help="agentkit consumer 的 plist 路径")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="launchd 状态 + plist 里的 consumer 身份")
    commands.add_parser("register", help="只注册事件消费者身份（不 claim、不加载）")
    start_parser = commands.add_parser("start", help="加载 LaunchAgent（幂等）")
    start_parser.add_argument("--home", dest="home_sub", default=None,
                              help="同全局 --home（放在子命令后面也认）")
    commands.add_parser("restart", help="真重载：bootout+bootstrap（release 换了新代码后必须用它）")
    stop_parser = commands.add_parser("stop", help="卸载 agentkit consumer 并 park plist")
    stop_parser.add_argument("--home", dest="home_sub", default=None,
                             help="同全局 --home（放在子命令后面也认）")
    args = parser.parse_args(argv)
    home = args.home or getattr(args, "home_sub", None)
    path = Path(args.plist) if args.plist else plist_path(home)
    if args.command == "status":
        print(json.dumps(status(path), ensure_ascii=False, indent=2))
        return 0
    if args.command == "register":
        info = read_plist(path)
        home = home or info["home"]
        print(json.dumps(register(home, info["consumer"], info["kind"]),
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == "restart":
        print(json.dumps(restart(path, home=home), ensure_ascii=False, indent=2))
        return 0
    if args.command == "start":
        print(json.dumps(start(path, home=home), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(stop(home_arg=home), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

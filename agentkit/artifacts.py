"""产物（artifact）判定：不信 agent 自报，比工作区快照。

为什么不让 adapter 上报"我生成了 a.txt"：agent 的自我描述和事实之间没有强制关系
（它可能没写、写失败、或写到工作区外）。HarnessRouter 的做法是从 git 工作区推导
produced files；这里用更朴素也更可移植的方式：**turn 前后对 workspace 做文件快照并求差**。

好处：
* 与 agent 无关 —— 换 CLI 不用改；
* 与操作系统无关 —— 只要 ``os.scandir`` 能遍历；
* 可测 —— 快照与差集都是纯函数。

刻意跳过 ``.agentkit/`` 自身目录与 ``.git/``（后者变化不代表产物）。``node_modules`` 之类
不特殊处理：是否忽略由调用方的 ``ignore`` 决定，避免内置一份永远不完整的黑名单。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

DEFAULT_IGNORED_DIRS = (".agentkit", ".git")
DEFAULT_IGNORED_SUFFIXES = (".pyc", ".pyo", ".swp", ".tmp~")

#: 单文件超过这个大小只在快照里记 size，不参与 mtime 抖动判定（大文件被 touch 不算产出）
LARGE_FILE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class FileState:
    size: int
    mtime_ns: int


@dataclass
class Snapshot:
    root: str
    files: Dict[str, FileState] = field(default_factory=dict)
    #: 撞上 ``max_files`` 提前返回 —— 此时 ``diff()`` 是**不完整的**，必须往外报，不能装作"没产物"
    truncated: bool = False
    #: 只统计"最近窗口内变动"的文件（大工作区用；见 ``snapshot(since=...)``）
    windowed: bool = False

    def diff(self, before: "Snapshot") -> "SnapshotDiff":
        created: List[str] = []
        modified: List[str] = []
        for path, state in self.files.items():
            old = before.files.get(path)
            if old is None:
                created.append(path)
            elif old.size != state.size or old.mtime_ns != state.mtime_ns:
                modified.append(path)
        deleted = [p for p in before.files if p not in self.files]
        return SnapshotDiff(
            created=sorted(created), modified=sorted(modified), deleted=sorted(deleted)
        )

    def fingerprint(self) -> str:
        """稳定指纹，用于日志与测试断言（不追求密码学强度）。"""
        import hashlib

        digest = hashlib.sha256()
        for path in sorted(self.files):
            state = self.files[path]
            digest.update(("%s:%d:%d\n" % (path, state.size, state.mtime_ns)).encode("utf-8"))
        return digest.hexdigest()[:16]


@dataclass
class SnapshotDiff:
    created: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)

    @property
    def produced(self) -> List[str]:
        """本 turn "产出"的文件：新建 + 修改（删除单列，因为删除不是产物）。"""
        return sorted(set(self.created) | set(self.modified))

    def is_empty(self) -> bool:
        return not (self.created or self.modified or self.deleted)

    def to_dict(self) -> Dict[str, List[str]]:
        return {
            "created": list(self.created),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
        }


def snapshot(
    root: str,
    ignore_dirs: Iterable[str] = DEFAULT_IGNORED_DIRS,
    ignore_suffixes: Iterable[str] = DEFAULT_IGNORED_SUFFIXES,
    max_files: int = 20000,
    since: Optional[float] = None,
) -> Snapshot:
    """遍历 ``root`` 生成快照。

    * ``max_files``：超过即停止，并把 ``truncated`` 置真 —— **调用方必须把这件事报出去**。
      静默返回"空产物"比不报更糟：下游会以为 agent 什么都没写。
    * ``since``（墙钟秒）：只保留 mtime 不早于它的文件，并且**剪掉 mtime 更早的目录**
      （新建/删除子项一定会刷新父目录 mtime）。实测工作区有 91 万文件时，全量遍历要 26s，
      剪枝后 <0.01s。代价要说清楚：**原地修改一个很老的目录里的文件会被漏掉**
      （它的父目录 mtime 没变，我们不会下去）。所以这是"大工作区的 best-effort"，
      ``TurnRunner`` 会在 outcome 的 ``fallbacks`` 里标 ``artifacts_windowed``。
    """
    ignored_dirs: Set[str] = set(ignore_dirs or ())
    ignored_suffixes: Tuple[str, ...] = tuple(ignore_suffixes or ())
    snap = Snapshot(root=os.path.abspath(root), windowed=since is not None)
    if not os.path.isdir(root):
        return snap

    count = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if is_dir:
                if entry.name in ignored_dirs:
                    continue
                if since is not None and stat.st_mtime < since:
                    continue  # 目录本身没被动过 → 里面的新建/删除不可能发生
                stack.append(entry.path)
                continue
            if ignored_suffixes and entry.name.endswith(ignored_suffixes):
                continue
            if since is not None and stat.st_mtime < since:
                continue
            snap.files[os.path.relpath(entry.path, root)] = FileState(
                size=stat.st_size, mtime_ns=stat.st_mtime_ns
            )
            count += 1
            if count >= max_files:
                snap.truncated = True
                return snap
    return snap


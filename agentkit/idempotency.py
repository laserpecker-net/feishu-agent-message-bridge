"""turn 级幂等：原子占位 + 等第一次的结果（**不是**返回 409 让调用方自己猜）。

语义直接取自 UHP ``tasks.md`` §6 的判决：

* 同一个 key 的重复提交**必须**返回第一次的结果；
* 第一次还在跑时**必须等**它，而不是返回"冲突"或"部分结果"；
* 理由：agent 任务又贵又有副作用，重试跑两遍比慢一点糟糕得多。

实现上只用一个原语：``O_CREAT | O_EXCL`` 的原子创建（与 HarnessRouter 用
``control_store.create_item`` 做 CAS 是同一种做法）。竞态赢者写 ``running`` 并开工，
输者读到 ``running`` 就去等终态 —— 没有内存锁、没有 Redis，跨进程也成立。

崩溃恢复不是靠猜：记录里有 ``pid`` 与 ``started_at``，只有"进程已死"或"超过
``stale_after_s``"才允许接管，避免把正在跑的 turn 重跑一遍。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .errors import TurnInProgress
from .types import Outcome, TurnStatus

RUNNING = "running"
DONE = "done"


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


@dataclass
class TurnRecord:
    turn_key: str
    state: str
    owner: str = ""
    pid: int = 0
    started_at: float = 0.0
    outcome: Optional[Dict[str, Any]] = None

    @property
    def terminal(self) -> bool:
        return self.state == DONE and bool(self.outcome) and (
            (self.outcome or {}).get("status") in TurnStatus.TERMINAL
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_key": self.turn_key,
            "state": self.state,
            "owner": self.owner,
            "pid": self.pid,
            "started_at": self.started_at,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TurnRecord":
        return cls(
            turn_key=data.get("turn_key", ""),
            state=data.get("state", RUNNING),
            owner=data.get("owner", ""),
            pid=int(data.get("pid") or 0),
            started_at=float(data.get("started_at") or 0.0),
            outcome=data.get("outcome"),
        )


class TurnStore(object):
    """一组 turn 记录的目录（默认 ``<workspace>/.agentkit/turns``）。"""

    def __init__(self, directory: str, pid_alive=None):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        # 可注入的活性判断：平台层是唯一的真相来源（Windows 上没有 os.kill(pid, 0)）
        self._pid_alive = pid_alive or _default_pid_alive

    # ---- 路径 -----------------------------------------------------------
    def path_for(self, turn_key: str) -> str:
        return os.path.join(self.directory, _digest(turn_key) + ".json")

    # ---- 读写 -----------------------------------------------------------
    def _read(self, turn_key: str) -> Optional[TurnRecord]:
        path = self.path_for(turn_key)
        try:
            with open(path, "rb") as handle:
                data = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return TurnRecord.from_dict(data)

    def _write_atomic(self, record: TurnRecord) -> None:
        path = self.path_for(record.turn_key)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        payload = json.dumps(record.to_dict(), ensure_ascii=False).encode("utf-8")
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _create_exclusive(self, record: TurnRecord) -> bool:
        path = self.path_for(record.turn_key)
        payload = json.dumps(record.to_dict(), ensure_ascii=False).encode("utf-8")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return True

    # ---- 协议 -----------------------------------------------------------
    def begin(
        self,
        turn_key: str,
        owner: str = "",
        stale_after_s: float = 3600.0,
        allow_takeover: bool = True,
    ) -> "BeginResult":
        """尝试占位。

        返回 ``BeginResult(kind='new')`` 表示"你来跑"；
        ``kind='replay'`` 表示"已有终态，直接用"；
        ``kind='busy'`` 表示"别人在跑，请等终态"。
        """
        record = TurnRecord(
            turn_key=turn_key,
            state=RUNNING,
            owner=owner,
            pid=os.getpid(),
            started_at=time.time(),
        )
        if self._create_exclusive(record):
            return BeginResult(kind="new", record=record)

        existing = self._read(turn_key)
        if existing is None:
            # 文件存在但读不出来（半写/损坏）：既然拿不到事实，就按忙处理，让调用方等
            return BeginResult(kind="busy", record=None)
        if existing.terminal:
            return BeginResult(kind="replay", record=existing)

        if allow_takeover and self._is_stale(existing, stale_after_s):
            existing.owner = owner
            existing.pid = os.getpid()
            existing.started_at = time.time()
            self._write_atomic(existing)
            return BeginResult(kind="new", record=existing)

        return BeginResult(kind="busy", record=existing)

    def _is_stale(self, record: TurnRecord, stale_after_s: float) -> bool:
        age = time.time() - record.started_at
        if record.started_at and age > stale_after_s:
            return True
        if record.pid and record.pid != os.getpid():
            alive = self._pid_alive(record.pid)
            if alive is False:
                return True
        return False

    def finish(self, turn_key: str, outcome: Outcome) -> None:
        record = TurnRecord(
            turn_key=turn_key,
            state=DONE,
            owner=outcome.adapter,
            pid=os.getpid(),
            started_at=0.0,
            outcome=outcome.to_dict(),
        )
        self._write_atomic(record)

    def release(self, turn_key: str) -> None:
        """放弃占位（执行前就失败时用），让重试可以重新开工。"""
        path = self.path_for(turn_key)
        record = self._read(turn_key)
        if record is not None and record.terminal:
            return  # 终态是事实，不允许被撤销
        try:
            os.unlink(path)
        except OSError:
            pass

    def await_terminal(
        self, turn_key: str, timeout_s: float = 120.0, poll_s: float = 0.2
    ) -> Optional[Outcome]:
        """等第一次的结果。超时返回 None —— 由调用方决定抛 :class:`TurnInProgress`。"""
        deadline = time.time() + max(0.0, timeout_s)
        while True:
            record = self._read(turn_key)
            if record is not None and record.terminal:
                outcome = Outcome.from_dict(record.outcome or {})
                outcome.replayed = True
                outcome.turn_key = turn_key
                return outcome
            if time.time() >= deadline:
                return None
            time.sleep(poll_s)

    def replay(self, turn_key: str) -> Optional[Outcome]:
        record = self._read(turn_key)
        if record is None or not record.terminal:
            return None
        outcome = Outcome.from_dict(record.outcome or {})
        outcome.replayed = True
        outcome.turn_key = turn_key
        return outcome

    def require_terminal(
        self, turn_key: str, timeout_s: float = 120.0, poll_s: float = 0.2
    ) -> Outcome:
        outcome = self.await_terminal(turn_key, timeout_s=timeout_s, poll_s=poll_s)
        if outcome is None:
            raise TurnInProgress(turn_key, self.path_for(turn_key))
        return outcome


@dataclass
class BeginResult:
    kind: str  # 'new' | 'replay' | 'busy'
    record: Optional[TurnRecord] = None


def _default_pid_alive(pid: int) -> Optional[bool]:
    """默认活性探测。

    **Windows 上必须直接返回 None**：CPython 的 ``os.kill`` 在 Windows 只支持
    ``CTRL_C_EVENT`` / ``CTRL_BREAK_EVENT``，**其他任何信号值都会调用 TerminateProcess**
    —— 也就是说 ``os.kill(pid, 0)`` 在 Windows 上不是探活，是"把那个进程杀掉"。
    拿一条陈旧记录里的 pid 去探活，等于随机杀掉别人的进程。宁可返回 None
    （让 TurnStore 退回时间阈值判断），也不能这么干。
    """
    """POSIX 上可靠；Windows 上返回 None（未知），交给时间阈值判定。"""
    if pid <= 0:
        return None
    if os.name == "nt":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True

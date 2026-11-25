"""会话指针存储：把"同一个对话的下一轮该接哪个 session"落盘。

**为什么需要它**：续跑不是把 ``resume_key`` 塞进 argv 就完事 —— 那个 key 必须**跨进程**
记住，否则 consumer 重启一次、沙箱回收一次，多轮对话就断在这里。本仓现在没有任何
resume 断言（全仓 grep 不到），所以这一层是从零补的。

设计取舍：

* 一个 key 一个文件（``<ws>/.agentkit/sessions/<hash>.json``，0600），不做数据库 ——
  consumer 是单进程 + 单 workspace，文件足够，且坏了只影响一个对话；
* 文件名用 **sha256 前缀**而不是原始对话 ID：对话 ID 来自外部事件，直接拼路径就可能
  被 ``../`` 之类的输入穿越出去；
* 每轮成功后**覆盖**而不是追加：只有最新 session 有意义，历史 session 留着只会让
  "该接哪个"变成一次猜测。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass
class SessionRecord:
    """一个对话的续跑指针。"""

    key: str
    resume_key: Optional[str] = None
    adapter: str = ""
    turns: int = 0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, object]) -> "SessionRecord":
        return SessionRecord(
            key=str(data.get("key") or ""),
            resume_key=(str(data["resume_key"]) if data.get("resume_key") else None),
            adapter=str(data.get("adapter") or ""),
            turns=int(data.get("turns") or 0),
            updated_at=float(data.get("updated_at") or 0.0),
        )


class SessionStore(object):
    """目录化的会话指针存储。所有写都是"临时文件 + rename"，断电只会丢最后一轮。"""

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(str(root)))

    def _ensure(self) -> None:
        if not os.path.isdir(self.root):
            os.makedirs(self.root, mode=0o700, exist_ok=True)

    def path_for(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:32]
        return os.path.join(self.root, digest + ".json")

    def get(self, key: str) -> Optional[SessionRecord]:
        path = self.path_for(key)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        record = SessionRecord.from_dict(data)
        # 文件里的 key 校验一下：hash 碰撞极不可能，但被手工改过的文件很常见
        return record if record.key == key else None

    def set(self, key: str, resume_key: Optional[str], adapter: str = "") -> SessionRecord:
        self._ensure()
        previous = self.get(key)
        record = SessionRecord(
            key=key,
            resume_key=resume_key,
            adapter=adapter or (previous.adapter if previous else ""),
            turns=(previous.turns if previous else 0) + 1,
        )
        self._atomic_write(key, record)
        return record

    def clear(self, key: str) -> bool:
        """丢掉指针（会话被拒/过期时调用），返回是否真的删掉了东西。"""
        path = self.path_for(key)
        try:
            os.unlink(path)
            return True
        except OSError:
            return False

    def list_keys(self) -> List[str]:
        try:
            names = [n for n in os.listdir(self.root) if n.endswith(".json")]
        except OSError:
            return []
        keys: List[str] = []
        for name in names:
            try:
                with open(os.path.join(self.root, name), "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                keys.append(str(data.get("key") or ""))
            except (OSError, ValueError):
                continue
        return sorted(key for key in keys if key)

    def _atomic_write(self, key: str, record: SessionRecord) -> None:
        path = self.path_for(key)
        tmp = path + ".tmp.%d" % os.getpid()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record.to_dict(), handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

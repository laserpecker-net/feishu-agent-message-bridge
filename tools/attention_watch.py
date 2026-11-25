#!/usr/bin/env python3
"""只读巡检 feishu-bridge 的 attention 事件，把没人看的 needs_attention 显性化。

背景：bridge 在消息超时/外部消费者失败时会把 inbox 置 needs_attention 并发布
kind=attention 事件。但 CONTRACTS.md 规定 attention 留给人工 ack、不允许自动消费，
而常驻消费者只 claim `--kind message`，所以 attention 队列长期无人可见。

本脚本**不 claim、不 ack、不写 bridge 状态目录**，只做三件事：
  1. `feishu-bridge events list --state pending` 读出待处理的 attention 事件；
  2. 对没通知过的事件发一条 macOS 通知（同一事件只通知一次）；
  3. 追加一行到 --log 指定的本地日志，供事后翻查。

状态与日志默认落在 bridge home（`$FEISHU_BRIDGE_HOME`，与 install.py 同一个目录），
不写仓库工作区：re-clone / `git clean` 不会丢状态、也不会把消息摘要留在源码树里。
通知走 `osascript`，因此本工具仅在 macOS 上有通知能力；其它平台只写日志。

用法：
  python3 tools/attention_watch.py            # 正常巡检（静默，只在有新事件时通知）
  python3 tools/attention_watch.py --dry-run  # 只打印，不通知、不落盘
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# 密钥判定只有一份权威实现（CONTRACTS.md「单一权威实现」），巡检不另建正则。
sys.path.insert(0, str(REPO_ROOT))
from consumer_safety import leaked_secret  # noqa: E402

DEFAULT_HOME = Path(os.environ.get("FEISHU_BRIDGE_HOME")
                    or Path.home() / "Library" / "Application Support" / "FeishuPersonalAgent")
DEFAULT_STATE = DEFAULT_HOME / "attention-watch-state.json"
DEFAULT_LOG = DEFAULT_HOME / "attention-watch.log"


def bridge_binary():
    for candidate in (
        Path.home() / ".local" / "bin" / "feishu-bridge",
        DEFAULT_HOME / "bin" / "feishu-bridge",
    ):
        if candidate.exists():
            return str(candidate)
    return "feishu-bridge"


def list_pending(home):
    proc = subprocess.run(
        [bridge_binary(), "--home", str(home), "events", "list", "--state", "pending", "--limit", "100"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "events list failed").strip()[:500])
    return json.loads(proc.stdout).get("events") or []


def message_text(home, message_id):
    """只读查 inbox 正文，用于通知里给出人话摘要；失败一律返回空串。"""
    db = home / "state.sqlite3"
    if not db.exists():
        return ""
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        try:
            row = con.execute("SELECT text FROM inbox WHERE id=?", (message_id,)).fetchone()
        finally:
            con.close()
    except Exception:
        return ""
    return (row[0] or "").strip() if row else ""


def sanitize(text, limit=80):
    text = " ".join(text.split())
    if leaked_secret(text):
        return "[正文疑似含敏感信息，已省略]"
    return text[:limit] + ("…" if len(text) > limit else "")


def notify(title, body):
    script = "display notification %s with title %s" % (_osa(body), _osa(title))
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    except Exception:
        pass


def _osa(text):
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def load_state(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"notified": {}}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%s.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description="Read-only attention event watcher")
    parser.add_argument("--home", default=os.environ.get("FEISHU_BRIDGE_HOME", str(DEFAULT_HOME)))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    home = Path(args.home).expanduser()
    state_path = Path(args.state).expanduser()
    log_path = Path(args.log).expanduser()

    try:
        events = list_pending(home)
    except Exception as error:
        print("attention-watch: skipped (%s)" % str(error)[:200])
        return 0

    attention = [e for e in events if e.get("kind") == "attention"]
    if not attention:
        print("attention-watch: 0 pending attention")
        return 0

    state = load_state(state_path)
    notified = state.setdefault("notified", {})
    fresh = [e for e in attention if str(e.get("id")) not in notified]
    print("attention-watch: %d pending, %d new" % (len(attention), len(fresh)))
    if not fresh:
        return 0

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    for event in sorted(fresh, key=lambda e: e.get("id") or 0):
        payload = event.get("payload") or {}
        message_id = payload.get("message_id") or "?"
        error = payload.get("error") or "needs_attention"
        snippet = sanitize(message_text(home, message_id))
        line = "%s event=%s message_id=%s error=%s text=%s" % (now, event.get("id"), message_id, error, snippet or "-")
        lines.append(line)
        if not args.dry_run:
            notified[str(event.get("id"))] = {"message_id": message_id, "error": error, "notified_at": now}

    if args.dry_run:
        print("\n".join(lines))
        return 0

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # 日志含消息摘要，保持与 bridge 其它文件一致的 0600。
        if log_path.exists():
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass
        else:
            log_path.touch(mode=0o600)
        with log_path.open("a") as stream:
            stream.write("\n".join(lines) + "\n")
    except Exception as error:
        print("attention-watch: log write failed (%s)" % str(error)[:200])

    newest = fresh[-1]
    payload = newest.get("payload") or {}
    body = "%d 条待人工处理｜event %s: %s" % (
        len(fresh), newest.get("id"), sanitize(message_text(home, payload.get("message_id") or ""), 60) or payload.get("error") or "")
    notify("feishu-bridge 有消息没回出去", body)
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

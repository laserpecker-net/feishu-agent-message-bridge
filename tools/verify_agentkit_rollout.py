#!/usr/bin/env python3
"""agentkit 接管状态的验收脚本：一条命令跑完"检查 / 冷启动 / 收发"。

为什么需要它：接管线上 consumer 之后，"看起来正常"很容易骗人 —— 进程在、心跳在，
但重启后可能双开、release 可能和代码不一致、产物清单可能因为工作区太大而静默为空。
这个脚本把这几件事变成**可复跑的判定**，而不是每次靠人回忆。

用法：

    python3 tools/verify_agentkit_rollout.py check                  # 只读体检
    python3 tools/verify_agentkit_rollout.py check --json
    python3 tools/verify_agentkit_rollout.py coldstart --dry-run   # 只打印计划，不动服务
    python3 tools/verify_agentkit_rollout.py coldstart             # 全部 bootout 再按登录方式 bootstrap
    python3 tools/verify_agentkit_rollout.py send --text "自检：只回 ok"

`coldstart` 模拟的是**登录**：launchd 会 bootstrap ``~/Library/LaunchAgents`` 里的每个 plist。
它等价于"重启后自启动"的机制验证（物理重启还会顺带验证内核/驱动，这里不涉及）。
注意它会短暂停掉 bridge（几秒），期间到达的飞书消息在重新启动后由 backfill 补拉。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentkit import rollout  # noqa: E402
from bridge import host  # noqa: E402

BRIDGE_LABEL = "io.feishu-agent.bridge"
ATTENTION_LABEL = "io.feishu-agent.attention-watch"
CONSUMERS = (rollout.AGENTKIT_LABEL,)
#: 引擎 A 已退役的 label：只要它还在 LaunchAgents 里，登录就会反复启动失败的进程。
RETIRED_CONSUMER_LABEL = "io.feishu-agent.cursor-consumer"


def _run(argv, timeout=120):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _launchctl(*args, timeout=120):
    return _run(["/bin/launchctl"] + list(args), timeout)


def _launchd(name):
    result = _launchctl("print", "%s/%s" % (rollout._domain(), name), timeout=30)
    return result.returncode == 0


def _bridge(*args):
    result = _run([str(Path(host.default_home()) / "bin" / "feishu-bridge")] + list(args), timeout=60)
    try:
        return json.loads(result.stdout or "{}")
    except ValueError:
        return {"error": (result.stdout or result.stderr or "").strip()[:300]}


def audit_layout(home=None):
    """纯函数：从 plist 布局推出"应该有几个 consumer 在跑"，不碰 launchd。"""
    home = Path(home or host.default_home())
    service_dir = rollout._service_dir()
    active = [name for name in CONSUMERS if (service_dir / (name + ".plist")).is_file()]
    parked_dir = home / rollout.PARKED_DIR
    parked = [p.stem for p in sorted(parked_dir.glob("*.plist"))] if parked_dir.is_dir() else []
    retired = [name for name in (RETIRED_CONSUMER_LABEL,)
               if (service_dir / (name + ".plist")).is_file()]
    return {
        "active_consumer_plists": active,
        "parked_consumer_plists": parked,
        "retired_consumer_plists": retired,
        # 登录时 launchd 会拉起每个 plist：多于一个就是"重启双开"的雷；
        # 退役引擎的 plist 还在 = 重启后有一个进程必然启动失败，同样判不安全。
        "reboot_safe": len(active) == 1 and not retired,
    }


def _release_audit(home):
    receipt_path = Path(home) / "installation.json"
    if not receipt_path.is_file():
        return {"ok": False, "reason": "installation.json 不存在"}
    receipt = json.loads(receipt_path.read_text())
    agentkit = receipt.get("agentkit")
    if not agentkit:
        return {"ok": False, "reason": "receipt 里没有 agentkit 块（安装时没带 --with-agentkit）"}
    release = Path(agentkit["release"])
    checked, problems = 0, []
    for rel, digest in agentkit.get("release_files", {}).items():
        path = release / rel
        if not path.is_file():
            problems.append("%s: release 里缺失" % rel)
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append("%s: 与 receipt 不符" % rel)
        # 二进制读取：`git cat-file` 的输出直接参与 sha256，不能走 text 模式
        blob = subprocess.run(["git", "cat-file", "-p", "HEAD:" + rel],
                              capture_output=True, timeout=30)
        if blob.returncode == 0 and hashlib.sha256(blob.stdout).hexdigest() != digest:
            problems.append("%s: 与 git HEAD 不符" % rel)
        checked += 1
    return {"ok": not problems, "release": str(release), "files": checked, "problems": problems}


def check(home=None, as_json=False):
    home = Path(home or host.default_home())
    report = {"home": str(home), "checks": {}}
    checks = report["checks"]

    checks["release"] = _release_audit(home)
    checks["layout"] = audit_layout(home)
    checks["launchd"] = {name: _launchd(name) for name in (BRIDGE_LABEL, ATTENTION_LABEL) + CONSUMERS}
    status = _bridge("status")
    checks["bridge"] = {
        "ok": status.get("running_release") == status.get("installed_release") is not None,
        "running_release": status.get("running_release"),
        "installed_release": status.get("installed_release"),
        "sending_enabled": status.get("sending_enabled"),
    }
    events = _bridge("events", "status")
    consumers = {c["name"]: c for c in events.get("events", {}).get("consumers", [])}
    states = events.get("events", {}).get("states", {})
    active_name = (audit_layout(home)["active_consumer_plists"] or [""])[0]
    loaded_consumers = [name for name in CONSUMERS if checks["launchd"].get(name)]
    checks["events"] = {
        "ok": states.get("pending", 0) == 0 and states.get("leased", 0) == 0 and len(loaded_consumers) == 1,
        "states": states,
        "loaded_consumers": loaded_consumers,
        "active_consumer_plist": active_name,
        "consumers": {name: {k: c.get(k) for k in ("claims", "acks", "fails")}
                      for name, c in consumers.items()},
    }
    failures = [name for name, item in checks.items()
                if isinstance(item, dict) and item.get("ok") is False]
    if not checks["layout"]["reboot_safe"]:
        failures.append("layout(reboot_safe)")
    report["ok"] = not failures
    report["failures"] = failures
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("home: %s" % report["home"])
        print("release: %s 文件 %s 个，问题：%s"
              % (checks["release"].get("release"), checks["release"].get("files"),
                 checks["release"].get("problems") or "无"))
        print("plist 布局：active=%s parked=%s → 重启安全=%s"
              % (checks["layout"]["active_consumer_plists"],
                 checks["layout"]["parked_consumer_plists"], checks["layout"]["reboot_safe"]))
        print("launchd: %s" % checks["launchd"])
        print("bridge: %s" % checks["bridge"])
        print("events: states=%s loaded=%s consumers=%s"
              % (checks["events"]["states"], checks["events"]["loaded_consumers"],
                 checks["events"]["consumers"]))
        print("结论: %s%s" % ("通过 ✅" if report["ok"] else "不通过 ❌",
                             "" if report["ok"] else " 失败项=%s" % report["failures"]))
    return 0 if report["ok"] else 1


def _wait(predicate, timeout_s, interval=0.3):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def coldstart(dry_run=False, home=None):
    """模拟登录：bootout 所有 feishu 服务 → bootstrap ~/Library/LaunchAgents 里的每个 plist。"""
    home = Path(home or host.default_home())
    plists = sorted(rollout._service_dir().glob("io.feishu-agent-*.plist"))
    labels = [plistlib.loads(p.read_bytes()).get("Label") for p in plists]
    plan = {"bootout": [label for label in labels if _launchd(label)],
            "bootstrap": [str(p) for p in plists]}
    if dry_run:
        print(json.dumps({"dry_run": True, **plan}, ensure_ascii=False, indent=2))
        return 0
    if not plists:
        raise SystemExit("~/Library/LaunchAgents 里没有 feishu 服务，无法冷启动")

    started = time.monotonic()
    for label in plan["bootout"]:
        _launchctl("bootout", "%s/%s" % (rollout._domain(), label), timeout=60)
    _wait(lambda: not any(_launchd(label) for label in labels), 30)

    for plist in plists:  # 登录时 launchd 做的就是这件事，顺序无关
        result = _launchctl("bootstrap", rollout._domain(), str(plist), timeout=60)
        if result.returncode:
            print("bootstrap 失败 %s: %s" % (plist.name, (result.stderr or "").strip()), file=sys.stderr)
            return 1
    elapsed = time.monotonic() - started
    _wait(lambda: _launchd(BRIDGE_LABEL) and _launchd(rollout.AGENTKIT_LABEL), 30)
    # bridge 起来后要给它几秒做首次扫描，健康度才有意义
    _wait(lambda: (_bridge("status").get("running_release") or "") != "", 30)
    for _ in range(40):
        state = _bridge("status")
        if state.get("running_release"):
            break
        time.sleep(0.5)
    print(json.dumps({
        "coldstart_seconds": round(elapsed, 1),
        "bootstrapped": [p.name for p in plists],
        "launchd": {label: _launchd(label) for label in labels},
        "layout": audit_layout(home),
    }, ensure_ascii=False, indent=2))
    return check(home)


def send(text, chat_id=None, timeout_s=300, home=None):
    home = Path(home or host.default_home())
    config = json.loads((home / "config.json").read_text())
    chat_id = chat_id or (config.get("self_chat_ids") or [None])[0]
    if not chat_id:
        raise SystemExit("没有 self_chat_ids，请显式传 --chat-id")
    target = config["target_open_id"]
    body = json.dumps({"text": '<at user_id="%s"></at> %s' % (target, text)}, ensure_ascii=False)
    before = {c["name"]: c.get("acks", 0) for c in
              _bridge("events", "status").get("events", {}).get("consumers", [])}
    result = _run([config["lark_binary"], "im", "+messages-send", "--as", "user",
                   "--chat-id", chat_id, "--msg-type", "text", "--content", body], timeout=60)
    if result.returncode:
        raise SystemExit("发送失败：%s" % (result.stderr or result.stdout)[:300])
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        consumers = {c["name"]: c.get("acks", 0) for c in
                     _bridge("events", "status").get("events", {}).get("consumers", [])}
        if any(consumers.get(name, 0) > before.get(name, 0) for name in consumers):
            break
        time.sleep(3)
    workspace = Path(config.get("consumer_workspace") or home)
    runs = sorted((workspace / ".agentkit" / "runs").glob("*/run.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not runs:
        print("事件已送达，但没找到 run 记录")
        return 0
    record = json.loads(runs[0].read_text())
    outcome = record.get("outcome", {})
    print(json.dumps({"run": runs[0].parent.name, "status": outcome.get("status"),
                      "answer": outcome.get("answer"), "artifacts": outcome.get("artifacts"),
                      "fallbacks": outcome.get("fallbacks"), "duration_s": outcome.get("duration_s")},
                     ensure_ascii=False, indent=2))
    return 0 if outcome.get("status") == "completed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="agentkit 接管状态验收（检查/冷启动/收发）")
    parser.add_argument("--home", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    check_parser = sub.add_parser("check", help="只读体检：release/布局/launchd/bridge/事件")
    check_parser.add_argument("--json", action="store_true")
    cold = sub.add_parser("coldstart", help="模拟登录自启动（会短暂停服务）")
    cold.add_argument("--dry-run", action="store_true")
    sender = sub.add_parser("send", help="发一条真实消息并等它被消费")
    sender.add_argument("--text", required=True)
    sender.add_argument("--chat-id", default=None)
    sender.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    if args.command == "check":
        return check(args.home, args.json)
    if args.command == "coldstart":
        return coldstart(args.dry_run, args.home)
    return send(args.text, args.chat_id, args.timeout, args.home)


if __name__ == "__main__":
    raise SystemExit(main())

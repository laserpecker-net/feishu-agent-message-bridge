# Windows 拓扑决策（唯一权威）

> **本文件是 Windows 拓扑的唯一定义处。** 其它文档不再各自给推荐，只链接到这里：
> `INSTALL-WINDOWS.md`、`dist/wsl2/README.md`、`README.md`、`CONTRACTS.md`、`skills/feishu-bridge-consumer-remote/SKILL.md`。
>
> 日期：2026-10-28（2026-11-03 更新）。形态 A 已在真实 Windows 11 机器（WSL 3.0.3.0，Ubuntu 22.04）上端到端跑通：bridge + DSH 常驻消费者在线 12h+、零重启、回复读回通过。仍标 ⚠需真机 的条目要在装新机时逐条复核。

---

## 0. 两条受支持形态，先选一条再动手

| | **形态 A（默认，推荐）** | **形态 B** |
|---|---|---|
| bridge 跑在哪 | **这台 Windows 的 WSL2 Ubuntu 里** | **另一台机器**（Mac，或另一个 WSL2 实例） |
| Windows 侧做什么 | ① DSH/Codex 装进 WSL2 直接消费（最省事）<br>② 或原生 Windows 会话经 `wsl.exe` / `http://localhost:8766` 消费 | 原生 Windows **只做消费者**：HTTP 网关 + `skills/feishu-bridge-consumer-remote` |
| 前置条件 | 管理员 PowerShell、WSL2、约 5GB 磁盘、自己的飞书账号 | 一台已在跑 bridge 的 Mac + 网络可达（SSH 隧道优先） |
| 典型场景 | 只有这一台 Windows，要独立完成飞书收发 | 已有一台 Mac 常开跑 bridge，Windows 只是再加一个消费者 |
| 安装入口 | [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md) | [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md)「形态 B」节 + [remote skill](skills/feishu-bridge-consumer-remote/SKILL.md) |
| 消费者沙箱 | WSL2 内 = bwrap/landlock（POSIX，与 macOS 同构） | 原生 Windows 会话 = 只有会话自身工具策略，**无适配器级沙箱** |
| 谁来发飞书消息 | WSL2 里的 bridge sender | 另一台机器上的 bridge sender |

**形态 A 内部的三条消费者路径**（`wsl.exe` 包装 / DSH 装进 WSL2 / HTTP 网关）只在 [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md) §6 写一份，本文件不重复——单一事实源。

---

## 1. 不要做的 6 件事

> 接入层（Tailscale / SSH 隧道 / 堡垒机）不是拓扑：它只解决「网络能不能到」，换不掉形态 A/B 的选择，也不能把 bridge 装到原生 Windows 上。远程取证用只读的 `tools/remote_doctor.py`，不要用"能 SSH 上去"当作可以改机器的理由。

做了任何一条，你的部署就不再是本仓支持的形态，后续问题我们不负责：

1. **遇到失败先报告，不要绕过或自建方案。** 装到一半报错时，先确认是不是「已知脚本缺陷」（见 [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md) §9）：是的话按那节给的正确修法改一行再重跑；不是的话把原始报错贴出来问人。**改脚本、换架构、自己写守护/中转，都会让问题从"已知缺陷"变成"没人见过的新故障"。**
2. **不要把 bridge 本体移植到原生 Windows。** 它依赖 POSIX 文件锁（`flock`）、进程组信号（`killpg`）、用户级服务管理（systemd）；`consumer_runtime.require_posix_runtime()` 会在任何 claim 之前直接拒绝。移植代价的历史评估见 `docs/HISTORY.md`。
3. **不要自写中转 / 代理 / 轮询脚本代替 `feishu-bridge events`。** 事件协议（租约、`claim_token`、至少一次 + `event_id` 幂等、不可变 outbox、回复去重）都在 CLI 与网关里；自己再包一层等于另造一套协议。
4. **不要跨机共享 SQLite（`state.sqlite3`，WAL 模式）。** 放到 SMB / 网络盘 / 两台机器同时打开，多写者会损坏数据库且不可回滚。跨机只用 HTTP 网关或 SSH（`CONTRACTS.md`「远程消费」节）。
5. **不要在 Windows 侧调 lark-cli 发消息。** 飞书收发的唯一出口是 bridge 的 sender 线程（确定性 `reply_uuid`、日配额、幂等、outbox 校验都在它里面）。Windows 侧只 claim 事件、只提交 reply/ack/fail。
6. **不要声称两条形态的沙箱等价。** 形态 A 的 WSL2 常驻入口有 bwrap/landlock 沙箱 + 流式兜底 + 出站脱敏；形态 B 的原生 Windows 会话只有会话自身策略和 skill prompt。两边的强制级别不同，写进文档或汇报都要区分。

---

## 2. 形态 A：WSL2 全内（默认）

- bridge、lark-cli、SQLite、sender 全部在 WSL2 Ubuntu 里；服务用 systemd `--user` 管理。
- 数据目录 `~/.feishu-personal-agent`，仓库**必须在 ext4 家目录**（`/mnt/c` 下会被 `setup.sh` 拒绝）。
- systemd 配置在**发行版**的 `/etc/wsl.conf` 的 `[boot]`，不是 `%USERPROFILE%\.wslconfig`。
- 该 Windows 上其它 DSH/Codex 会话（原生 or WSL2 内）都是**消费者**，选择见 INSTALL-WINDOWS.md §6。

## 3. 形态 B：原生 Windows 只做消费者

- bridge 在另一台机器；Windows 侧永远不装 `install.py`、不跑 `feishu-bridge start|stop|restart`、不写 bridge 状态文件。
- 只能走两件事：HTTP 网关 `/v1/events/*`，或 `ssh` 调对端 `feishu-bridge events` CLI（`skills/feishu-bridge-consumer-remote/SKILL.md`）。
- 网关 token 泄露 → 在对端 `feishu-bridge events token --rotate`；**轮换后必须重启 `events serve` 才生效**（运行中的网关保留启动时读入的旧 token）。
- 默认 `events serve` 只绑 `127.0.0.1`；跨机优先 SSH 隧道，`--allow-remote` + 端口转发属最后手段，禁止直接暴露公网。

---

## 4. 变更流程

要先改这里的形态定义（新增第三条拓扑、把 bridge 搬进原生 Windows 等），走仓库评审：写清动机、影响文件、真机验证计划。**未经批准就先按形态 A 或 B 落地，不要自行发明第三条。**

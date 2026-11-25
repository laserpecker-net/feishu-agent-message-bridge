# 飞书个人助手（Feishu Personal Assistant）

用**你自己的飞书账号**收发消息：群里 @ 你的事件由本机会话里的 AI 接住并回复；需要动手的活，
先起一张卡片等你点头，再交给**干活端**在独立目录里做，结果带回群里。

> **这是"个人账号自动化"工具，不是飞书官方机器人。** 使用前请确认这符合你所在组织的合规要求；
> 它读写的是你自己的会话，凭据只落在本机（`0600`）。

---

## 它解决什么问题

在飞书里被 @ 的时候，人往往**不在电脑前**，或者回答一句要跳三个地方查资料。
这个桥把"被 @ 的消息"变成三件可以自动化的事：

1. **回话**：AI 读懂消息、查资料、给出回答，挂在原消息下面（带引用）
2. **派活要点头**：要动东西的活（改文件、跑命令、提交、发消息），先起卡等你批准
3. **有人真的去干**：批准后由干活端在**独立目录**里执行，产物清单回群里

桥自己**没有大脑**：它只负责采集、排队、发出去。回话与干活是两个职责不同的消费者进程。

---

## 三个角色（代码里就是这样分工的）

| 角色 | 进程 | 职责 | 沙箱 |
|---|---|---|---|
| 桥 | `feishu-bridge foreground` | 采集消息 → 生成事件 → 排队出站 → 读回校验 | —— |
| 回话端 | `dsh-consumer.py`（`dsh-main`） | 答题、起草任务卡、发回执、**只读** | `read-only` |
| 干活端 | `dsh-worker.py`（`dsh-worker`） | 领批准过的活，在 `work/<task>/space/` 里干活、交产物 | `workspace-write` |

- 事件按角色路由：`message`/`attention` 只给回话端，`work` 只给干活端；抢错会直接报错
- 回话端**写不进去**（沙箱只读），所以它不会"顺手把文件改了"
- 干活端**不替你做决定**：只领已经批准（或本人直派）的活

---

## 关键特性

- **出站唯一出口**：所有发出去的话都先落一张 outbox 行，发送后回读校验；核对不上就停在待人工，不重发
- **任务卡与审批**：四档批准（引用回复 / 唯一待批 / `执行` 命令 / 本人直派），有 TTL、有审计三件套
- **一次一个目录**：每个活一个独立目录，产物是文件清单；**不假设 git**（同事机器上没有 git 也能干活）
- **失败必交接**：超时 / 被沙箱拦 / 机器睡过去，都给交接单（需求原文、卡在哪一步、产物、续跑命令）
- **慢答复有回执**：15 秒还没答完先说「收到，我先看一下」，180 秒报进度
- **挂起开关**：`下班` / `上班`，挂起期间零采集零回复，重启保持
- **两端等价**：macOS（launchd）与 Windows（WSL2 + systemd）同一份代码，只有平台实现差异

---

## 快速开始

### macOS

```bash
git clone https://github.com/laserpecker-net/feishu-agent-message-bridge.git
cd feishu-agent-message-bridge
python3 install.py --verify            # 构建 release + 起服务 + 跑 doctor
feishu-bridge status                   # running_release / mode
feishu-bridge doctor --assert; echo rc=$?
```

前置：`lark-cli`（`npm i -g @larksuite/cli`）已登录你的飞书账号；AI 引擎（`dsh`）可用。
完整步骤见 [INSTALL-MACOS.md](INSTALL-MACOS.md)。

### Windows（WSL2 全内，推荐）

拓扑决策见 **[WINDOWS-DECISION.md](WINDOWS-DECISION.md)**（只支持 WSL2 全内，不支持原生 Windows 跑桥）。

```powershell
# 在解压后的发行包里（dist/wsl2 打包生成）
install.cmd            # 自动装依赖 + 在 WSL 里装 bridge + 两个消费者
```

装完应看到：

```
bridge       active
dsh-main     live      ← 回话端
dsh-worker   live      ← 干活端
```

细节（退出码、阶段、离线安装、故障排查）见 [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md) 与 [dist/wsl2/README.md](dist/wsl2/README.md)。

---

## 目录结构

```
bridge/          桥本体（采集/事件/出站队列/任务账本/网关/命令）
dsh-consumer.py  回话端入口
dsh-worker.py    干活端入口
agentkit/        把"起进程/读流/超时/杀进程组"收成平台层（多引擎适配）
install.py       安装与升级（release 快照 + 服务定义 + 激活）
dist/wsl2/       Windows(WSL2) 发行包与安装脚本
tools/           迁移、体检、导出公开快照等一次性工具
docs/            设计、历史与运维文档
```

---

## 配置

单一配置：`config.json`（在 bridge home 下）。常用的几项：

| 键 | 含义 |
|---|---|
| `target_open_id` | 你本人的 open_id（只服务本人） |
| `self_chat_ids` | 允许「本人发的消息也处理」的会话白名单 |
| `signature` | 回复末尾的署名 |
| `reply_to_bots` | 是否回复其他机器人（默认 `false`，避免 AI 互刷） |
| `workbench` | 干活端用哪个工作台（`direct` / `cmd-interop`，CLI 路径与 home） |
| `worker.work_root` | 干活目录根（Windows 上要放在 `/mnt/c/...`，即 `C:\...`） |

命令与说明书：私聊发 `help` 会返回完整使用说明；常用命令 `状态` / `上班` / `下班` / `执行` / `清单` / `撤回`。

---

## 验收与运维

```bash
feishu-bridge status                # 进程、release、采集延迟、错误
feishu-bridge doctor --assert       # 服务/心跳/token/release/worker/积压/消费者，fail 时退出码 1
feishu-bridge consumer status       # 谁在领事件
feishu-bridge hold -r "下班"        # 挂起（零采集零回复）
feishu-bridge release               # 恢复
```

Windows 上额外的只读复核：`sudo bash dist/wsl2/wsl/check-consumers.sh --user <用户>`（六条判据）。

---

## 安全与隐私

- 凭据、会话库、日志**只在本机**；`config.json` / 凭据文件权限 `0600`
- 只处理 **@ 本人**的消息；私聊默认不采集，除非显式加入 `self_chat_ids`
- 回复一律带署名，可区分"人说的"和"助手说的"
- 仓库里带脱敏门禁（`pytest tests/test_repo_hygiene.py`），提交前会拦住真实 open_id / 内网地址 / 机器名
- 公开仓库是由 `tools/export-public.sh` 生成的**单提交快照**，不含私有运维台账与历史

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/GUIDE-BEGINNER.md](docs/GUIDE-BEGINNER.md) | **第一次装的人从这里开始**：申请飞书自建应用 → 配 App ID/Secret → 装桥 → 配事件订阅 → 怎么用 |
| [CONTRACTS.md](CONTRACTS.md) | 数据模型、事件与命令的唯一事实源 |
| [AGENTS.md](AGENTS.md) | 改这个仓库的规矩（根治优先、一条事实一个答案、判据可复跑） |
| [docs/HISTORY.md](docs/HISTORY.md) | 踩过的坑与当时的判据（含真实事故复盘） |
| [AGENTKIT.md](AGENTKIT.md) | 多引擎适配层设计 |
| [CONSUMER-TEMPLATE.md](CONSUMER-TEMPLATE.md) | 接入自己的 Agent 时的能力模型与协议 |
| [docs/local-only.md](docs/local-only.md) | 哪些文件只在本机、不进 git |

---

## 许可

[MIT](LICENSE) © 2026 LaserPecker.net / 激光啄木鸟

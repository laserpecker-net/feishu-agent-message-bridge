# AGENTKIT：AGENT × 操作系统的底层抽象

> 这份文档回答两个问题：**(1)** 这层抽象用的是哪些设计模式、为什么不是"一个工厂"；
> **(2)** 它把哪些反复踩过的坑变成了"结构上不可能犯"。
>
> 【事实】本文的每个"现状问题"都带 `文件:行`；凡是我没亲自验证过的行号，只写文件不写行。

---

## 1. 一句话结论

**不是一个模式，是四个模式各管一轴**：

| 轴 | 变化的东西 | 模式 | 落点 |
|---|---|---|---|
| AGENT | argv、流 schema、答案提取、静态权限载体 | **Adapter（适配器）** | `agentkit/adapters/base.py` + `legacy.py` |
| OS | 起进程、杀进程组、读管道、加锁、环境隔离 | **Abstract Factory（抽象工厂）** | `agentkit/platform/{base,posix,windows}.py` |
| 选择 | "这次用哪个 agent / 哪个平台" | **Factory Method + Registry（注册表）** | `agentkit/adapters/registry.py`、`platform/__init__.py` |
| 一次 turn 的固定流程 | 幂等 → 组环境 → 起进程 → 扫流 → 兜底 → 快照 → 收尾 | **Template Method（模板方法）** | `agentkit/turn.py` |

为什么不能用"一个工厂"解决：**工厂解决"创建"，而这四个轴的变化频率和变化原因完全不同**。
把"用哪个 AGENT"和"在哪个 OS 上跑"塞进同一个工厂，工厂会得到 `2×N` 个产品类
（`PosixCodex`、`WindowsCodex`、`PosixDsh`…），加一个 OS 就要改 N 次 —— 这是
"工厂爆炸"，也正是本仓现在三份 `_kill_group` 的根源。

必须区分的两件事：

* **Adapter** 管"**协议翻译**"（一个 AGENT 的方言 ↔ 我们的归一化事件）；
* **Abstract Factory** 管"**原语成套**"（一套 `spawn/terminate/line_reader/lock/prepare_env`
  必须来自同一个 OS，**不许跨平台混搭**）。

后者是硬约束，不是风格问题：Windows 的 `selectors` **只支持 socket、不支持管道**，
所以"边跑边读、还要带超时"在 Windows 上只能用线程 + 队列。一旦允许"POSIX 的读取器 +
Windows 的进程组"这种混搭，错误会以 `OSError` 的形式在运行时随机出现。

---

## 2. 结构

```
                     ┌──────────────────────────────────────────┐
   调用方            │  TurnRunner.run(req)  ← Template Method  │  turn.py
   （consumer /      │  ①校验收参 ②幂等占位 ③组环境 ④写prompt   │
     CLI / 测试）    │  ⑤快照before ⑥spawn ⑦扫流+deny ⑧超时兜底  │
                     │  ⑨快照diff ⑩判定 ⑪诚实字段 ⑫落盘+幂等收尾│
                     └───────────┬──────────────────┬───────────┘
                                 │                  │
                    ┌────────────▼───────┐   ┌──────▼─────────────────┐
                    │  AgentAdapter      │   │  Platform（抽象工厂）  │
                    │  Adapter 模式      │   │  一族原语必须同源      │
                    ├────────────────────┤   ├────────────────────────┤
                    │ build_argv         │   │ spawn / terminate      │
                    │ prompt_on_stdin    │   │ line_reader            │
                    │ env_patch          │   │ prepare_env            │
                    │ parse_line         │   │ lock / pid_alive       │
                    │ deny_violation     │   │ paths / which          │
                    │ classify           │   └───────┬────────┬───────┘
                    │ finalize_on_timeout│           │        │
                    └─────────┬──────────┘   ┌────────▼──┐  ┌──▼─────────────┐
                              │              │ Posix 族  │  │ Windows 族     │
              ┌───────────────┼──────────┐   │ selectors │  │ 线程+队列      │
        ┌─────▼─────┐ ┌───────▼────┐ ┌───▼─┐ │ killpg    │  │ taskkill /T    │
        │ Cursor    │ │ Codex      │ │ DSH │ │ flock     │  │ 锁：明确拒绝   │
        └───────────┘ └────────────┘ └─────┘ └───────────┘  └────────────────┘
              ▲              ▲          ▲
              └──────────────┴──────────┘  Registry：@register("name") + get_adapter()
```

三条"不许越界"的规则：

1. **adapter 拿不到 `subprocess`**。它只能返回 argv/env/prompt，起进程由平台做。
   于是"忘了 `stdin=DEVNULL`"这种事故**结构上不可能发生** —— 平台没有这个开关可忘。
2. **adapter 不许放宽策略**。`DenyPolicy` 只能收窄；`prepare_env` 对 `HOME/PWD/TMPDIR/PATH`
   等受保护变量直接抛 `PolicyRefusal`。这就是 UHP 的 "narrowing is safe, widening is escalation"。
3. **平台原语不许跨族混用**。`get_platform()` 返回的是一整套；`get_lock_platform()`
   在 Windows 上直接抛 `PolicyRefusal`（见 §5）。

---

## 3. 这层抽象"优化"掉了什么（对照本仓现状）

### 3.1 两份 `_kill_group` → 一份

【事实】`codex-consumer.py:91-100`、`dsh-consumer.py:202-216`
是同一段逻辑的两个手抄本（TERM → sleep → KILL）。
【现状】统一在 `agentkit/platform/posix.py` 的 `terminate()`：**先杀组再等**，
`stopped_early` 时立刻 TERM，超时再 SIGKILL，并对"进程已自己退出"的 `ProcessLookupError` 静默。

### 3.2 三份 workspace 校验 → 一份

【事实】`codex-consumer.py:239-249`、`dsh-consumer.py:294-304`、
`consumer_runtime.py:62-71` 各有一份"必须绝对路径 + 必须是目录"。
【现状】`TurnRunner._validate_workspace()`，并且**在任何副作用之前**失败
（先校验、后幂等占位、再建目录）。

### 3.3 环境隔离：从"两套半"到一套

【事实】
* `codex-consumer.py:289` 只改 `HOME` / `CODEX_HOME`，`TMPDIR`、`PWD` 仍是父进程的；
* `dsh-consumer.py:236` 只加 `DSH_PERMISSION_MODE`。

【现状】`platform.prepare_env()` 一次性做完 6 件事（`tests/test_agentkit_platform.py` 全覆盖）：

| 动作 | 为什么 | 漏了会怎样 |
|---|---|---|
| `HOME/USERPROFILE` → `<ws>/.agentkit/home` | 会话记录必须跟着工作区走 | agent 的 transcript 留在真实 `~` 里，换机/回收后续跑 = 失忆 |
| `TMPDIR/TMP/TEMP` → `<ws>/.agentkit/tmp` | 临时文件属于本次 turn | 沙箱清理把别人的临时产物删掉 |
| `PWD` → 工作区 | POSIX `getcwd` 与 `$PWD` 不一致时，脚本按错的目录展开相对路径 | "明明文件在那里"式幽灵 bug |
| 钉 UTF-8（`PYTHONIOENCODING`/`PYTHONUTF8`/`LC_ALL`） | Windows 控制台默认 cp936 | 中文答案变成 `?????` |
| 丢弃 `LD_PRELOAD`/`DYLD_*`/`PYTHONHOME` | 加载器注入面 | 父进程的注入变量污染子进程 |
| 剥离"名字像密钥"的变量（可显式 allowlist） | 真凭据不该出现在 agent 环境里 | agent 进程 `env` 里就能读到 key |

最后一条对应 HarnessRouter 那条最贵的教训：**relay 要让真 key 不出现在子进程 env 里** ——
需要凭据的 CLI 应该用它自己的登录态文件（codex 的 `auth.json`、dsh 的 stored signin），
而不是把 key 塞进环境变量。

### 3.4 幂等：进程边界两侧都要做

【事实】本仓现在的幂等只在"平台侧 claim + `claim_token` + `event_id`"这一侧
（`bridge/consumer.py` 的 claim/reply/ack 状态机）。
【现状】`agentkit/idempotency.py` 在**子进程这一侧**再补一层：`TurnStore.begin()`
用 `O_CREAT|O_EXCL` 占位，三种结果 `new / replay / busy`：

* `replay` → 直接返回第一次的结果（**不是** 409，也不是重跑）；
* `busy` → **等**，等到终态就返回它；等不到才抛 `TurnInProgress`；
* 只有在"pid 已死 或 超过 `stale_after_s`"时才允许接管。

这正是"wait, don't 409"：重复提交的语义是**第一次的结果**，而不是第二个错误。

### 3.5 产物：快照求差，不信自报

【事实】三个 adapter 的产物判定各自为政（cursor 落 `result.json`，codex 落 receipt，
dsh 落 `run.json`），没有"这个 turn 到底产出了哪些文件"的统一答案。
【现状】`agentkit/artifacts.py`：turn 前 `snapshot()`（跳过 `.git`/`.agentkit`，
上限 20000 文件），turn 后求差，`Outcome.artifacts` 是**相对路径列表**。

### 3.6 `incomplete` 必须诚实

【现状】两条不变式由底座强制，adapter 无法绕过：

1. `status == incomplete` ⇒ `incomplete_reasons` 非空（`turn.py` 里补 `outcome.reason`）；
2. 超时兜底出来的答案（`finalize_on_timeout`）一定带 `fallback_answer` 标记，
   且**永远不会**被标成 `completed`。

【事实】Codex/DSH 入口超时直接 raise，没有"用已产出的文本再跑一次"的兜底；这个能力从来没统一过。
现在它是 `AgentAdapter.finalize_on_timeout()` 这个钩子，谁想用谁实现，骨架负责打标记。

### 3.7 请求字段不许"静默忽略"

【现状】`Outcome.ignored` 列出"请求里给了、但这个 adapter 做不到"的字段
（例：`model`、`narrow_to`）。测试 `test_unsupported_request_fields_are_reported_not_silently_dropped`
钉住这条：**静默忽略的字段与已生效的字段，在下游看起来一模一样**。

### 3.8 一套 `LineReader` 吃掉两份逐行切分

【事实】`codex-consumer.py:182-201`、`dsh-consumer.py:143-199`
两份"半个行/最后一行没换行/EOF"的手工状态机。
【现状】`platform/stream.py`：`next_line(timeout)` 只返回 `LINE / TIMEOUT / EOF` 三种结果，
POSIX 用 `selectors + os.read`（**刻意不用** `readline()`，避免文本缓冲与 selectors 打架），
Windows 用线程 + 队列 + 哨兵。

---

## 4. 怎么接进来（已经接好了）

**既有三个入口一行都没动**，但"接进来"这件事本身已经落地：新增一个并行入口
[`agentkit-consumer.py`](agentkit-consumer.py)，用 `--agent` 选 AGENT，其余参数
（`--home` / `--consumer` / `--kind` / `--workspace` / `--check`）全部原样交给
`consumer_runtime.main()` 解析：

```bash
# 旧：python3 dsh-consumer.py   --home ~/.local/share/feishu-agent
# 新：python3 agentkit-consumer.py --agent dsh --home ~/.local/share/feishu-agent

python3 agentkit-consumer.py --agent dsh --check      # 只报告能力/二进制，不 register、不 claim
```

驱动层只有一层薄适配（[`agentkit/driver.py`](agentkit/driver.py)），它实现的正是
`consumer_runtime` 唯一依赖的契约：

```python
build(mark_home) -> generate(payload, event, stop_event) -> str   # 非空答案，否则抛异常
```

三个要点：

1. **提示词复用 `agent_prompt.build_prompt`**，不在 driver 里另起一套 —— 否则 DSH/Codex/
   新入口会各自漂移出一套行为模型。
2. **幂等键 = 事件 ID + 本次 `claim_token`**。带 token 是刻意的：同一个事件换 token 重新
   领取，是"上次领取过期了"，属于**合法第二次尝试**，不该被重放；只有"同一 token 再来
   一次"（进程崩溃后重放）才必须返回第一次的结果。
3. **`incomplete` 当失败**：半截答案不许发到飞书。`generate` 只在 `completed` 时返回文本，
   其余一律抛 `AgentRunFailed`，由 `consumer_runtime` 转成 `fail --error AgentFailed`。

### 多轮续跑（原来完全缺失）

[`agentkit/session.py`](agentkit/session.py) 把"这个对话下一轮该接哪个 session"落盘到
`<ws>/.agentkit/sessions/<sha256>.json`（0600，原子写）。`driver` 的每轮流程是：

* 会话指针存在 → 作为 `resume_key` 传给 adapter；
* 成功 → 用 outcome 里的新 key 覆盖指针；
* **续跑被拒**（会话过期/被清理）→ `TurnRunner` 自动**换新会话重试一次**，
  并把 `attempts=2`、`fallbacks=['resume_fallback']` 留在结果里。

第 3 条是重点：会话过期是常态（换机、清了 `~/.codex`、沙箱回收），对用户来说"这次没答上"
不可接受，丢掉上下文重来明显好过失败 —— 但**不能悄悄做**，否则"上下文丢过一次"在日志里
永远查不到。

### 迁移顺序（仍然建议，每步可独立回滚）

1. **先切 DSH**：argv 最干净、overlay 已固化，`tests/test_dsh_consumer.py` 的 25 个用例
   可直接当行为清单对照。
2. **再切 Codex**：`--ephemeral` 不可续跑，幂等只能靠事件 journal —— 与 `turn_key` 语义
   正好对齐，风险最低。
3. **最后切 Cursor**：它在 agentkit 里已有 workspace/策略文件的特例，要先对齐
   `finalize_on_timeout` 的行为。
4. **`bridge/` 不需要动**：它是平台侧（claim/ack/reply/journal/flock），agentkit 是子进程侧，
   两者的边界就是 `TurnRequest` / `Outcome` 这一对值对象。

新增一个 AGENT 的最小成本（照 [`agentkit/adapters/fake.py`](agentkit/adapters/fake.py) 抄）：

```python
@register
class MyAgent(AgentAdapter):
    name = "myagent"
    binary_name = "myagent"
    capabilities = Capabilities(name="myagent", stream_format="jsonl", ...)
    def build_argv(self, req, ctx): return [ctx.binary, "--json", "-"]
    def prompt_on_stdin(self, req): return req.prompt
    def parse_line(self, line): ...      # 一行 → StreamEvent
    def classify(self, state): ...       # 累积状态 → Outcome
```

**0 行**监督循环、**0 行** kill、**0 行**超时逻辑、**0 行**产物收集。

## 5. 与 `WINDOWS-DECISION.md` 的关系（这条最容易被绕过）

`WINDOWS-DECISION.md` 是唯一权威：bridge 保持 POSIX（`flock`/`killpg`/systemd），
只支持拓扑 A（全部在 WSL2 里）和拓扑 B（原生 Windows 只作**消费者**，走 HTTP 网关/SSH）。

agentkit 因此**不偷偷在 Windows 上跑 bridge**，而是把这个边界做成可检查的代码：

* `get_platform()` 在 `os.name == "nt"` 时**默认抛 `UnsupportedPlatform`**，
  报错文本里直接引用 `WINDOWS-DECISION.md`；
* 要启用原生 Windows，必须显式设 `FEISHU_AGENTKIT_NATIVE_WINDOWS=1`
  —— 这是"改拓扑"，需要人来做决定，不能被代码顺手打开；
* `WindowsLockPlatform.acquire()` **永远抛 `PolicyRefusal`**：Windows 没有 `flock`
  的等价物能保证"同一 workspace 只有一个 consumer"这个不变式，
  与其做一个"大部分时候对"的锁，不如明确说不。

也就是说：**能跑 AGENT ≠ 能跑 bridge**。前者 agentkit 支持（`CREATE_NEW_PROCESS_GROUP`
+ `taskkill /T` + 线程读取器），后者仍然只能在 WSL2/拓扑 B 下跑。

---

## 6. 运行与验证

```bash
python3 -m agentkit list        # 已注册的 AGENT 及能力
python3 -m agentkit show dsh    # 某个 AGENT 的能力声明与二进制解析方式
python3 -m agentkit platform    # 当前平台、进程组支持、原生 Windows 是否放行
python3 -m agentkit selftest    # 在**当前这台机器**上真跑一遍平台层（见下）
```

`selftest` 做四件只有真机才能回答的事：起进程并验证 HOME 被重定向、按
`line / timeout / eof` 三态读流、`terminate` 是否连孙进程一起收、文件锁
（Windows 上它应当**按设计拒绝**，拒绝也算通过）。一次输出就是这台机器的体检报告：

```
{"platform": "posix", "checks": [{"name": "spawn+env", "ok": true, ...}], "ok": true}
```

测试分工（100 个用例）：

| 文件 | 覆盖 | 关键断言 |
|---|---|---|
| [`test_agentkit_platform.py`](tests/test_agentkit_platform.py) | 抽象工厂/环境/流/杀进程 | HOME·USERPROFILE·TMPDIR·PWD 重定向、密钥剥离、受保护变量拒绝、`LINE/TIMEOUT/EOF` 三态、进程组连孙进程一起收、selftest 自身通过 |
| [`test_agentkit_turn.py`](tests/test_agentkit_turn.py) | 五个契约场景 + 幂等 + 诚实字段 | 首轮/续跑/字段忽略申报/产物来自快照/回收后产物仍在；超时与超输出必 `incomplete`；deny 必杀；同 `turn_key` 返回第一次结果；**输出上限按字节不按字符**；续跑被拒自动降级且留痕 |
| [`test_agentkit_registry.py`](tests/test_agentkit_registry.py) | 注册表 + **与既有策略的一致性** | cursor 的 deny 与 `consumer_safety.tool_violation` 同结论；codex 的 argv 与 `codex-consumer._command` 逐字节相等 |
| [`test_agentkit_driver.py`](tests/test_agentkit_driver.py) | 接进运行时的契约 + 会话指针 | 失败必抛（运行时才能 fail 掉事件）、空答案不外发、同 token 只跑一次（`ran.log` 只有一行）、换 token 会重跑、续跑被拒降级、`--check` 不 claim 不建状态目录 |
| [`test_agentkit_legacy_e2e.py`](tests/test_agentkit_legacy_e2e.py) | **三个真实 adapter 的端到端** | 假 CLI 重放真实 JSONL：codex 的 `thread.started → agent_message → turn.completed`、cursor 的 `system.init → result`、dsh 的 `session → status.turn_end → final`；deny 事件 8 秒内击杀；缺终结事件/未知事件/多键答案一律 fail-closed；cursor 策略文件落在 run 目录且工作区无 `.cursor` |
| [`test_agentkit_portability.py`](tests/test_agentkit_portability.py) | 跨平台**结构规则**（AST 扫描） | POSIX-only 调用只许出现在 `platform/posix.py`；adapter 层拿不到 `subprocess`；`_default_pid_alive` 必须有 nt 门；Windows 族必须给线程读取器 |
| [`test_agentkit_windows.py`](tests/test_agentkit_windows.py) | Windows 族（在 macOS 上可验的部分） | `CREATE_NEW_PROCESS_GROUP` 标志、stdin 走 prompt 文件、`taskkill /T` → `/T /F` 的**顺序**、已死进程不再发命令、锁按设计拒绝、`SystemRoot/COMSPEC/PATHEXT` 保留而 `USERPROFILE` 重定向 |

registry 那一行是这次改造的"安全闸"：**允许重构，不允许改变安全结论**。

## 7. 剩下的缺口（诚实清单）

1. **既有三个入口仍是默认路径**。`agentkit-consumer.py` 与它们并行，切换需要人工决定
   （按 §4 顺序），并且每步都要跑对应的 `tests/test_*_consumer.py` 回归。这是有意的：
   在跑着真实飞书流量的机器上，默认路径不该被一次提交改掉。
2. **续跑机制已就绪，但三个真实 CLI 都没有续跑参数**：codex 用 `--ephemeral`、dsh/cursor
   的 argv 里都没有 `--resume`（这点是**事实**，全仓也没有任何 resume 断言）。所以今天
   只有 `fake` 走得到这条路径；机制的价值是"下一个带 `--resume` 的 agent 直接可用"，
   以及"请求里给了 `resume_key` 但 adapter 不支持时必须如实申报 `ignored`"。
3. **Windows 仍是"代码路径已验证，真机未跑"**。注入式测试覆盖了标志与命令顺序，但
   `taskkill /T` 对真实 GUI 子进程的效果、`icacls` 收紧是否生效、线程读取器在高吞吐下的
   表现，只有在 Windows 上 `python3 -m agentkit selftest` 跑一次才算数。我没有 Windows
   机器，不假装跑过 —— 逐条依据与风险见 §8.2。
4. **`agentkit` 尚未落 `attempt.json` 式 journal**，只有 `runs/<uuid>/run.json` + `.agentkit/turns/`
   幂等记录；要完全对齐 bridge 的 journal 语义还需要一层映射。
5. **Windows 侧的单行上限依赖 `readline()` 自身行为**：POSIX 读取器把单行上限做成了硬切
   （超过 8 MiB 就强制切出一段交给字节预算，见 `stream.py`），Windows 的线程读取器用的是
   `readline()`，无法在中途打断一行 —— 这是两个实现之间唯一剩下的语义差异，已在
   `selftest` 之外单独记录。

## 8. 自我验证（这一节是"我到底验过什么"的账本）

原则：**能跑的跑到底，不能跑的给出静态判定依据并标注"未验证"**。三类结论分开写：

- 【实跑】本机能验证、已经跑过的；
- 【静态】本机不能验证，但依据 API 语义/代码结构可以判定的；
- 【未验】本机确实无法验证，只能等真机。

### 8.1 实跑清单

| 验什么 | 怎么验 | 结果 |
|---|---|---|
| agentkit 全量用例 | `python3 -m pytest tests/test_agentkit_*.py -q` | **100 passed** |
| 全仓回归 | `python3 -m pytest tests/ -q` | **562 passed, 2 failed**。两个失败是沙箱禁止 `ps`（`PermissionError: 'ps'`，`tests/test_codex_consumer.py` / `tests/test_dsh_consumer.py`），与本次改动无关：`git status` 里 `bridge/`、三个 `*-consumer.py` 一字未改 |
| 三个真实 adapter 端到端 | `tests/test_agentkit_legacy_e2e.py`：用 `#!<当前解释器>` 的假 CLI 重放真实 JSONL | 15 passed，含"deny 事件 8 秒内击杀""缺 `turn.completed` 不许算成功""DSH `final` 必须在 `turn_end` 之后" |
| 平台自检 | `python3 -m agentkit selftest` | `ok: true`（spawn+env / stream:line / terminate:tree / lock 四项全过） |
| 入口自检（真实 CLI） | `python3 agentkit-consumer.py --agent {dsh,codex,cursor} --check` | 三者 `binary_ok=true`，路径分别是 npx 里的 `dsh`、`/opt/homebrew/bin/codex`、`~/.local/bin/cursor-agent` |
| argv 与真实 CLI 对账 | `codex exec --help`、`cursor agent --help`、`dsh headless --help` | 我们用到的 flag 全部存在：codex `--disable/--sandbox/--output-schema/--json/--ephemeral/--skip-git-repo-check`；cursor `--print/--output-format/--force/--trust/--sandbox/--add-dir`；dsh `headless --patch/--json/-` |
| fd 不泄漏 | spawn 5 次、比对 `/dev/fd` 计数 | 增量 ≤ 1（修掉了每 turn 漏一个句柄的 bug） |
| 巨型行截断 | 8 MiB 无换行输入 | 硬切后交给字节预算，不吞内存 |
| 假 CLI 写入位置 | cursor 用例断言 `.cursor/cli.json` 在 run 目录、工作区无 `.cursor` | 通过（策略文件不污染用户仓库） |
| 答案出站脱敏 | fake agent 返回含 `sk-ant-...` 的答案 | 整条替换为固定文案，并留 `secret_redacted` 痕迹（清洗在底座，adapter 漏不掉） |
| 锁的真实争用 | `test_posix_lock_really_excludes_another_process`：本进程持锁，**另起子进程**去抢 | 通过（子进程拿到 `TimeoutError`，说明 `flock` 真的跨进程排他，而不是同进程自欺） |

### 8.2 Windows：本机没有环境，所以逐条给"凭什么这么写"

| 断言 | 依据 | 状态 |
|---|---|---|
| `os.kill(pid, 0)` 在 Windows 上不是探活，而是 **TerminateProcess** | CPython `os.kill` 文档：除 `CTRL_C_EVENT`/`CTRL_BREAK_EVENT` 外任何信号值都直接杀进程 | 【静态】已加门：`agentkit/idempotency.py` 的 `_default_pid_alive` 先判 `os.name == "nt"` 返回 `None`；`WindowsProcessPlatform.pid_alive()` 直接返回 `None`（`platform/windows.py`），并有用例断言"调用即失败" |
| 管道在 Windows 上不可 `select`，`selectors` 只支持 socket | CPython `selectors` 文档（Windows 只有 `SelectSelector`） | 【静态】结构分流：Windows 族给 `ThreadLineReader`，POSIX 族给 `PosixLineReader`；后者在 Windows 上**实例化即抛**（结构性防混搭）；`test_agentkit_portability.py` 用 AST 保证 POSIX-only 调用只出现在 `platform/posix.py` |
| `creationflags=CREATE_NEW_PROCESS_GROUP` 只在 Windows 有效 | `subprocess` 文档：`creationflags` 仅 Windows，POSIX 传了会 `TypeError` | 【静态】只在 `platform/windows.py` 传；POSIX 用 `start_new_session=True`。两个标志都不许跨族出现（同上 AST 规则） |
| `taskkill /T` 需要 System32 兜底 | Windows 组件位置；服务账号的 PATH 可能不含 System32 | 【静态】`taskkill_command()` 先找 `%SystemRoot%\System32\taskkill.exe`，找不到才退回裸 `taskkill` |
| 0600 在 Windows 上不构成权限 | NTFS 是 ACL 模型，`os.chmod` 只动只读位 | 【静态】平台层新增 `harden_path()`：POSIX 走 `chmod`，Windows 走 `icacls <path> /inheritance:r /grant:r <USER>:F`；**拿不到用户名时只收继承、绝不退化成"所有人可读"**（prompt 文件里有用户消息）；ACL 失败不阻断 turn（FAT32/exFAT） |
| `HOME` 重定向在 Windows 上靠 `USERPROFILE` | Windows 工具读 `USERPROFILE`，POSIX 工具读 `HOME` | 【静态】`prepare_env` 两个都改，并且保留 `SystemRoot/COMSPEC/PATHEXT/WINDIR`（少了它们连子进程都起不来） |
| 真机行为：进程树是否真的收干净、ACL 是否真的生效、线程读取器在高吞吐下是否丢行 | 只能真机跑 | 【未验】需要一次 `python3 -m agentkit selftest` + 一个真实 turn |

### 8.3 这轮自查**改掉的**六个问题（都是"看代码才发现的"，不是猜的）

1. **Windows 上会误杀进程**（严重）：`_default_pid_alive` 用 `os.kill(pid, 0)` 探活，
   在 Windows 上等于 `TerminateProcess` —— 拿陈旧记录里的 pid 去"探活"，就是随机杀别人的进程。
   已加 nt 门 + 用例锁死。
2. **每 turn 漏一个 fd**：prompt 走文件时 `open(...,'rb')` 之后没关，子进程已 dup 自己的句柄。
   已改成 `try/finally` 关闭，并加了 `/dev/fd` 计数用例。
3. **Cursor 的 deny 少了一层**：`capabilities` 声明了 `policy-file+stream-backstop`，但实现里
   **没有写** `<cwd>/.cursor/cli.json`（这正是策略文件该做的静态那一层）。
   已补 `preflight` 写同源策略文件，并用 `cwd_for()` 把 cwd 放回私有 run 目录 ——
   否则策略文件会写进用户仓库，等于用污染换安全。
4. **Cursor 的二进制解析指错**：默认 `cursor` 是**编辑器** CLI（本机 `cursor --help` 全是
   `--diff/--merge/--wait` 这类窗口参数，agent 子命令才叫 `cursor agent`）。写成 `cursor` 会去开
   编辑器而不是跑 agent。已改 `cursor-agent`（复用 `bridge/host.py` 的 `default_cursor_binary()`），
   并在 `preflight` 里对"指到编辑器"直接报错。
5. **Codex / DSH 的严格度低于 legacy**：未知事件被静默忽略、DSH 缺 `status.turn_end` 也接受、
   codex 答案允许多余键。已按 `codex-consumer.py:200-236`（`_Stream`）和
   `dsh-consumer.py:128-199`（`_RunEvents`）重写状态机：未知事件 fail-closed、缺终结事件失败、
   答案必须恰好 `{"answer": ...}` / 经 `extract_answer` 且不超上限。
6. **DSH 的凭据隔离假设是错的**（见 8.4）：`HOME` 重定向对它无效，已改成 `DSH_HOME` 隔离 +
   凭据符号链接。

### 8.4 两个"用实测纠正设计假设"的实验

**实验一：DSH 不认 `HOME`，只认 `DSH_HOME`**【实跑】

```
$ HOME=/tmp/probe/h2 dsh headless --help
Error: EPERM: operation not permitted, open '~/.dsh/profiles/headless/cordis.yml'
$ DSH_HOME=/tmp/probe/h1 dsh headless --help
Usage: dsh --profile headless [options] [task...]      # 并在 /tmp/probe/h1 下建出 profiles/ storages/
$ HOME=/tmp/probe/h3 node -e 'console.log(require("os").homedir(), process.env.HOME)'
/tmp/probe/h3 /tmp/probe/h3
```

结论（事实）：node 自己认 `$HOME`，但 **DSH 用 passwd 条目解析家目录**（`os.userInfo()` 那条路），
所以给它 `HOME=/tmp/...` 它照样去写 `~/.dsh`。含义：**"统一重定向 HOME"对遵循 `$HOME` 的工具
有效，对 DSH 无效**。已改成 `DSH_HOME=<run>/dsh-home`，并把真实 `$DSH_HOME/.credentials.yaml`
**符号链接**进去（不复制密钥；DSH 刷新 token 时写回真实文件，与 legacy 行为一致）。

**实验二：`cursor` 是编辑器，`cursor-agent` 才是 agent**【实跑】

```
$ cursor --help            # Cursor 3.27.22：--diff/--merge/--add/--goto/--new-window ...（编辑器）
$ cursor agent --help      # -p, --print / --output-format / --force / --trust / --sandbox / --add-dir
$ cursor-agent --version   # 2026.10.22-27dcf5f
```

含义：`binary_name` 写错不会报错，只会**静默地开一个窗口**并返回空输出 —— 所以 `--check` 的
`binary_ok` 必须看"解析到哪个二进制"，不能只看"能不能找到"。

### 8.5 明确**没有**验证的

- ~~没有发起真实模型调用~~ → **已推翻（2026-11-01，见 §9）**：`cursor` 适配器在真机上跑通了
  两轮真实 `cursor-agent` 调用（65.9s / 53.8s，Grok 4.7），回复已落到飞书。
  但 `codex` / `dsh` 两个适配器**仍然只有假 CLI 回放**，没有真实模型轮。
- **Windows 真机零运行**：§8.2 的每一行都是"代码层可判定"，不是"跑过"。
- `attempt.json` 式 journal 映射仍未做（§7.4）。
- 三个真实 CLI 都没有 `--resume`，续跑路径只有 `fake` 走到（§7.2）。

## 9. 发布 / 注册 / 生效 / 真实环境测试（2026-11-01，已在本机真机完成）

这一节记的是"线上到底发生了什么"，不是设计意图。四个动作被刻意拆开，每一步都可逆。

> **读法提醒（2026-11-03 校订）**：§9 里的 release id（`a0fbf88d3eade509`）与 pid 是**当时那次实录**的，
> 之后又发过两次版，**不要照抄**。当前事实以命令输出为准：
> `feishu-bridge status | grep running_release`、`launchctl print gui/$(id -u)/io.feishu-agent.agentkit-consumer`。
> 截至 2026-11-04：**macOS 与 Windows/WSL2 主机的线上消费者都是 DSH**（`dsh-main`，
> 见 README 的「现在这台机器靠谁回复」表；macOS 由 `install.py --with-dsh-consumer` 托管，
> WSL2 由 `dist/wsl2/wsl/install-dsh-consumer.sh` 托管）。`agentkit-consumer.py` 是**可选并行入口**，
> 是否在跑只以 `feishu-bridge consumer status` 的输出为准，不在文档里写死。**旧的 Cursor 引擎
> （`bridge/consumer.py`）及其 plist、`--take-over` / `--restore-cursor` 全部已删除**（见
> [docs/HISTORY.md](docs/HISTORY.md)）；下面表格是 2026-11-01 那次实录，命令列已按当前代码更新。


| 动作 | 命令 | 做了什么 | 不可逆风险 |
|---|---|---|---|
| 发布 | `python3 install.py --with-agentkit --agentkit-agent cursor --agentkit-consumer agentkit-cursor --agentkit-workspace ~/Documents` | 把 `agentkit/` + 6 个根级运行时文件打进**当前 release**（`releases/a0fbf88d3eade509/`，27 个文件写进 `installation.json` 的 sha256 账），并写 `io.feishu-agent.agentkit-consumer.plist`（**只写不加载**） | 无：不改运行进程，`bridge/` 一个字节没动，release id 不变 |
| 注册 | `python3 agentkit-consumerctl.py register` | `feishu-bridge events register --consumer agentkit-cursor --kind other`：只建身份，不 claim | 无 |
| 生效 | `python3 agentkit-consumerctl.py start` | 幂等加载 LaunchAgent（已加载就返回 `already-loaded`） | 有（从此由它领消息） |
| 重载 | `python3 agentkit-consumerctl.py restart` | **bootout → bootstrap → kickstart**。`install.py` 改了 plist 里的 release 路径后，只 `kickstart -k` 会继续跑 launchd 缓存的旧参数 | 无（事件是持久队列；重载窗口内的事件重新领取） |
| 停用 | `python3 agentkit-consumerctl.py stop` | `bootout` 并把 plist 挪进 `<home>/disabled-consumers/`，登录不再自启 | 无 |
| 回滚版本 | `feishu-bridge rollback --to <release_id>` → `feishu-bridge restart` → `agentkit-consumerctl.py restart` | release 是自包含的：回到上一版不依赖任何 plist | 无 |

`agentkit-consumerctl.py` **随 release 分发**（`releases/<id>/agentkit-consumerctl.py`，可执行），
装好的机器上直接能跑，不需要仓库副本。

**为什么要挪 plist**：`~/Library/LaunchAgents` 里的 plist 是 `RunAtLoad + KeepAlive`，登录时
launchd 会把它们全部拉起 —— 那一格里出现两个 consumer plist，就等于"重启后两个进程抢同一批事件"。
所以 `stop` 把文件挪进 `<home>/disabled-consumers/`，`start` 需要时再挪回；`tools/verify_agentkit_rollout.py`
的 `audit_layout()` 会把"多于一个 plist"和"退役引擎的 plist 残留"都判成 `reboot_safe: false`。

### 9.1 真实环境证据（两条真实飞书消息，完整链路）

消息由 owner 身份发进自测群 `oc_example000000000000000000000000`（成员：1 人 + 1 机器人），
`<at user_id="ou_c746...">` 命中 `mentioned()`，chat 在 `self_chat_ids` 白名单里：

| # | 事件 | 领取→完成 | run 目录 | 模型 / 耗时 | 飞书回执 |
|---|---|---|---|---|---|
| 1 | `evt-32-eaa7e99c33b0f10ec12febf1` | `leased`→`done`，`claims=1 acks=1 fails=0` | `Documents/.agentkit/runs/833d91cfaa1a4ba2a900886527cf498d` | `Grok 4.7 256K High Fast` / 65.867s | 回复 `om_x100b6140b0c2bcacb0b687e05e1cfdd`（22:42，`reply_to` 指向原消息） |
| 2 | `evt-33-54493f3ac01be5a7fd9d46fe` | `done`，`claims=2 acks=2 fails=0` | `Documents/.agentkit/runs/4ba60a4d850f46239fc3296a74c6280d` | 同上 / 53.808s | 22:45 回复，`reply_to=om_x100b61414c66bca4cf59d65b61c9322` |
| 3 | `evt-34-fd54e6c56159304b1adafcce` | `done`，`claims=3 acks=3 fails=0` | `Documents/.agentkit/runs/b4b952ba0b28458e88759a43d863e6b6` | 同上 / 22.896s | 22:48 回复 `ok 2026-11-01 22:48:28 CST`，`reply_to=om_x100b61415feb00a0ce4899931067dc7` |

第二条的答案里模型自己报出了 run 目录名 `4ba60a4d850f46239fc3296a74c6280d` —— 和磁盘上的目录
一致，说明 `cwd`/工作区/artifact 路径在真实进程里是对的，不是"测试环境里凑出来的"。

> **升级已有部署时**：`install.py --with-agentkit` 只写 plist，不重载已加载的 consumer，`kickstart -k` 也会按旧配置起进程。要让 consumer 换到新 release，必须 `bootout` + `bootstrap`；判据是进程 argv 里的 release 目录（见 [docs/HISTORY.md](docs/HISTORY.md) §3.5）。

回滚也真的跑了一遍（不是读代码推断）：当时的 `stop --restore-cursor` → cursor consumer `pid 8157`
`healthy: true`；再 `start --take-over` → agentkit 重新 loaded（**这两个命令已随引擎 A 删除**，
保留这段只是为了说明"当年那条路真的通"，不代表现在还能跑）。**三轮**双向切换（第二轮带
plist 挪位逻辑）后 `running_release` 始终是 `a0fbf88d3eade509`，bridge 无重启、事件
`dead` 数没有增加（3 条都是既有的历史死信）。第三条消息正是在"plist 归位 + 重启安全"改造
之后发的，用来排除"改坏了还能回话只是因为进程没重启"这种假象。

### 9.2 这轮上线暴露并修掉的两个真问题

1. **cursor 的登录态在 `$HOME` 里**。原来底座统一把 `HOME` 收进 workspace，`cursor-agent`
   立刻 `Not logged in`；实测软链 `~/.cursor`、`~/.local/share/cursor-agent`、只拷
   `agent-cli-state.json`/`cli-config.json` **全部救不回来**。结论：给 adapter 加一个
   `home_policy`（默认 `redirect`，`cursor` = `inherit`），并且只允许"凭据不可复制"的 AGENT 用，
   必须在 `capabilities.notes` 写明理由。legacy 也从不改 HOME —— 这是**对齐现状**，不是放宽。
   `list`/`show` 现在会打印 `home` 策略，审计时看得见。
2. **失败时 `error` 是空的**。真实沙箱里 `cursor-agent` 报
   `Error: EPERM ... mkdir '~/.cursor/projects/...'`（裸文本，不是 JSON 行），解析器按设计忽略
   非 JSON，结果只剩 `failed/no_answer`，排障只能猜。现在：`FAILED` 且 `error` 为空时，把原始
   tail 的最后几行写进 `error`。

3. **重启会双开 consumer**（`RunAtLoad + KeepAlive` 的 plist 留在 `LaunchAgents` 里）。
   现在停用方的 plist 会被挪到 `<home>/disabled-consumers/`，回滚时挪回。这个改造顺手暴露了
   一个单测自己的 bug：`home=None` 时会在当前目录建出 `None/` 目录（`git status` 里能看到），
   所以 `_park()` 现在对非绝对路径直接报错。
   **还有一个更隐蔽的坑**：`install.py` 每次升级都会重写 cursor 的 plist，等于把刚做的
   "停用"撤销一半（文件又回到 `LaunchAgents`，下次登录就双开）。现在 `install.py` 写 plist
   前先看 parked 副本在不在：在就写到 parked 位置（升级不改 "谁在领消息"），
   而 `start` 的 `already-loaded` 分支也会顺手把被写回的 plist 再挪走。两条都有回归测试。
   **第四个坑是测试自己**：`_default_home()` 让单测能摸到真实安装目录，一次全量测试真的把线上
   parked 的 cursor plist 挪进了临时目录。现在单测里 `_default_home()` 被 autouse fixture
   关掉（`monkeypatch`），需要 home 的用例必须显式传 —— 测试不许有对真实环境的隐式写权限。

### 9.3 本机当前状态

- consumer：`agentkit-cursor`（kind `other`），LaunchAgent `io.feishu-agent.agentkit-consumer`
  **在** `~/Library/LaunchAgents/` 里，`pid 11679`，轮询心跳正常（`last_seen_at` 每 ~11s 前进）。
- 旧的 cursor consumer：**引擎与 plist 都已删除**（`install.py` 升级时会清掉遗留的
  `io.feishu-agent.cursor-consumer.plist` 和 parked 副本），不再作为回滚路径。
- bridge：`running_release == installed_release == a0fbf88d3eade509`，`sending_enabled: true`。
- 备份（可回滚）：`~/Library/Application Support/FeishuPersonalAgent/backups/agentkit-rollout-20261101-223804/`
  （`config.json`、`installation.json`、当时的 plist）。
- 测试：`python3 -m pytest tests/ -q` → **605 passed**（含 rollout 19 例、install-agentkit 9 例、
  HOME 策略 3 例）。

### 9.4 仍然**没有**验证的（诚实清单）

- `codex` / `dsh` 适配器**没有**真实模型轮：只做了假 CLI 回放 + `--help` 核对。
- Windows 真机仍然零运行（§8.2 全是静态判定）。
- 长时间稳定性：只观察了 ~12 分钟、3 个真实事件；没有验证连续失败重试、租约过期回收、
  进程被杀后的恢复在真实流量下的表现。
- **真机重启**没有执行（那会打断你的机器）：重启安全性是"按 launchd 语义构造 + 单测钉住"
  （plist 只剩一个在 LaunchAgents 里），不是"重启过一次看到只有一个 consumer"。
- 真实回复的"内容质量"取决于模型和 prompt（`agent_prompt.py`），本轮只验了协议与链路正确。

### 9.5 从已提交的代码部署（2026-11-01 23:13）

前几轮都是"边改边装"，这一轮改成"先提交、再从干净树部署"，用来证明部署物可复现：

1. 提交 `2bc7c7b`（36 个文件：`agentkit/` 23 个、2 个入口脚本、10 个测试、3 个文档、`install.py`），
   提交后 `git status` 干净；
2. 从 HEAD 干净树跑 `install.py --with-agentkit`：release 里 **27 个文件与 `git cat-file HEAD:<path>`
   逐字节一致**（receipt、release、HEAD 三方 sha256 相等）；
3. `agentkit-consumerctl.py restart` 让进程真正吃到这份代码：`pid 26966`（当时的实现是
   `kickstart -k`；**2026-11-03 起改为 bootout+bootstrap**，因为 kickstart 会复用缓存的旧路径）；
4. 再发一条真实飞书消息验收：`evt-35-40d5d94c84489f75b16a55e5`、run
   `9838d4b8cffb470ab325d1f589968cfb`、26.998s、`Grok 4.7 256K High Fast`，
   23:13 回复 `ok 2026-11-01 23:13:42 CST`（`reply_to=om_x100b61412191b8a8cf45d4ee3cc1db9`），
   `claims=4 acks=4 fails=0`；
5. 部署后 plist 布局仍然只有"一个在 LaunchAgents"：`agentkit-consumer.plist` 在
   `~/Library/LaunchAgents/`，`cursor-consumer.plist` 在 `disabled-consumers/`。

这条路径（提交 → 安装 → 重启 → 一条真实消息）就是以后发版该走的顺序；「先提交再 install」
（保证 `installation.json` 里记的 sha256 能在 git 里对上）对 agentkit 同样成立，见 [docs/HISTORY.md](docs/HISTORY.md)。

### 9.6 完整测试流程（2026-11-02 07:13–07:28，本机真跑）

按"关机 → 登录 → 收发 → 干活 → 回滚 → 双开核查"的顺序跑了一遍，可复跑的执行器是
`tools/verify_agentkit_rollout.py`（`check` / `coldstart` / `send` 三个子命令）。

| 环节 | 做法 | 结果 |
|---|---|---|
| 冷启动（模拟关机） | 三个服务全部 `bootout` 到 not loaded | ✅ 全部停干净 |
| 登录自启动 | 对 `~/Library/LaunchAgents/*.plist` 逐个 `bootstrap`（= launchd 登录行为） | ✅ 6.1s 内 bridge/agentkit/attention 都 loaded，`cursor` 仍 not loaded |
| 冷启动后健康 | `feishu-bridge status` + 事件心跳 | ✅ release 一致、sending 开启、`last_seen` 3.9s、stderr 0 字节 |
| 收发 ①（问答） | 真实消息，问 pid/事件序号 | ✅ evt-36，62.2s，回复带 pid 82148 与 turn_key |
| 收发 ②（任务） | 让 agent 建 `agentkit-task-20261102/hello.txt` | ✅ 磁盘上真有文件（41 字节），内容带时间戳，回复给出路径/大小/内容 |
| 收发 ③（任务+产物） | 建 `report.md` 三行 | ✅ `artifacts=['agentkit-task-20261102/report.md']`，`fallbacks=['artifacts_windowed']` |
| 回滚到旧路径（**历史，命令已删除**） | `stop --restore-cursor` | ✅ 当时 `cursor-main` pid 90529 healthy，事件 39 由它处理并回复"cursor-main 在回"——这条能力随引擎 A 一起退役，现在回滚版本用 `feishu-bridge rollback` |
| 再接管（**历史，命令已删除**） | `start --take-over` | ✅ 当时 agentkit pid 92367，cursor 再被停用并挪走；现在 `start` 幂等加载即可 |
| 双开核查 | LaunchAgents 里只有一个 consumer plist | ✅ `reboot_safe: true` |
| 去重核查 | `outbox` 按原消息分组 | ✅ 49 条回复 / 49 个原消息，全部 `reply_verified`，无重复 |
| release 一致性 | 27 文件 sha256 = receipt = `git cat-file HEAD:<path>` | ✅ |

**期间发现并修掉的真问题**：工作区是 `~/Documents`（实测 918,699 个文件），
快照 20000 文件撞顶后 `artifacts` **静默返回空** —— agent 明明写了 `hello.txt`，run 记录
却是 `[]`。修法见 `agentkit/artifacts.py`：窗口模式（目录 mtime 剪枝，26s → <0.01s）+ 
`truncated` 标记，并在 `outcome.fallbacks` 里写 `artifacts_windowed` / `artifacts_truncated`。
这条是真机才暴露的，单测和"看起来正常"都发现不了。

**仍未做**：物理重启（会中断当前会话）。登录自启动的机制已按 launchd 语义验证，
物理重启后建议直接跑：

    python3 tools/verify_agentkit_rollout.py check

期望输出 `结论: 通过 ✅`，且 `loaded` 里只有一个 consumer。

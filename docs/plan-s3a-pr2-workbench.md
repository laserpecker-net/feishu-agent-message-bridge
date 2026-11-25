# PR-2 设计稿：`Workbench(direct)` 干活端适配器

> 状态：**已实现（PR-2，2026-11-18）**。实现见 `bridge/workbench.py`，判据见 `tests/test_workbench.py`。编排见 [plan-s3a-worker.md](plan-s3a-worker.md) PR-2；平台与失败语义见
> [design-b-loop.md](design-b-loop.md) §6、§9。本文只说 PR-2 这一块，不碰 PR-3 的事件接线。

---

## 1. 这个 PR 要解决什么

干活端拿到一件活之后，需要有人**真的把 `dsh headless` 跑起来**：给它一份任务说明、一个会话 id、
一个能写的目录，让它干，然后把「干成了 / 干不成 / 被锁住了 / 机器睡过去了」判清楚交回来。

PR-2 只做这一层**执行适配器**。不产生事件、不回飞书、不改任务状态——那些是 PR-3/PR-4。

## 2. 最重要的一条设计决策：**不许写第二个 headless 运行器**

`dsh-consumer.py`（458 行）里已经有一整套成熟实现：

| 已有能力 | 在哪 |
|---|---|
| 每轮 0600 `--patch` overlay（沙箱策略 / 关掉子代理工具 / 凭证路由） | `_overlay_text`、`_write_overlay` |
| NDJSON 事件契约与「不容忍部分成功」的解析 | `_RunEvents`、`ALLOWED_EVENTS` |
| `start_new_session=True` + 超时杀进程组 | `_generate`、`_kill_group` |
| 输出上限 `MAX_OUTPUT_BYTES = 2 MiB` | 常量 |
| 凭证路由（`DEEPSEEK_API_KEY` vs 已保存登录）+ 明确失败文案 | `_credential_route` |
| 超时 `AGENT_TIMEOUT_SECONDS = 240` | 常量 |

**所以 PR-2 的形状是「抽取 + 加一个面向任务的入口」，不是新写一套**：

```
bridge/workbench.py       ← 新：把上面这些搬进来，对外一个 Workbench
dsh-consumer.py           ← 改：删掉自己那份，改成调用 Workbench（行为逐字不变）
tests/                    ← 现有 consumer 测试全部继续跑，作为"行为没变"的判据
```

判定「没写第二套」的硬判据（写进测试）：

- `dsh-consumer.py` 里不再出现 `subprocess.Popen` / `headless` / overlay 文本字面量
- overlay 文本与现在**逐字相同**（golden 对比，钉死）
- `ALLOWED_EVENTS` / `MAX_OUTPUT_BYTES` / `DISABLED_TOOLS` 只有一处定义

## 3. 对外接口

```python
@dataclass(frozen=True)
class WorkResult:
    ok: bool
    kind: str            # 'done' | 'lock_conflict' | 'timeout' | 'no_final'
                         # | 'credential_missing' | 'cli_unavailable' | 'error'
    text: str            # 成功：final 的 answer；失败：给人看的一句人话
    session_id: str
    duration_seconds: float
    output_bytes: int
    artifacts: list[str] # 干活产物（相对业务目录的路径）
    handoff: "Handoff | None"  # 非 done 时**必须有**：见 §6.5，PR-4 拿它发交接单

class Workbench:
    def __init__(self, config, *, cli=None, home=None, workspace=None, timeout=None)
    def prepare(self) -> None     # 启动自检；不通过抛 WorkbenchError(kind=...)
    def run(self, prompt: str, *, session_id: str | None = None,
            stop: threading.Event | None = None) -> WorkResult
```

- `run()` **不抛异常**：所有失败都落到 `WorkResult.ok=False` + `kind`，调用方（PR-4）只按 `kind` 决定
  任务状态与人话文案。只有"参数错/内部 bug"才抛。
- `stop` 事件用于取消（主人 `下班` 或任务被叫停）：命中就杀进程组，`kind='timeout'` 复用同一套收尾。

## 4. 配置块 `workbench`（每台机器一次）

```jsonc
{
  "workbench": {
    "kind": "dsh-web",              // 或 "dsh-desktop"（S3b）
    "cli": "~/.npm-global/bin/dsh", // 可执行文件；不给就用 PATH 里的 dsh
    "home": "~/.dsh",               // DSH_HOME；不给就用当前 env
    "workspace": "<这个活的工作目录>",   // 每个活一个独立目录，见编排 §7
    "timeout_seconds": 1800,        // 任务级超时（不是回话那 240 秒）
    "credential_overlay": "auto",   // auto | stored | env
    "max_output_bytes": 2097152
  }
}
```

缺省值**只在这个模块定义一次**（`dsh-consumer.py` 改成从 `bridge/workbench.py` 取，不许两处各写一份）。
`prepare()` 逐项校验：`cli` 存在且可执行、`cli --version` 能答、`home` 存在且 `DSH_HOME` 与之一致、
`workspace` 可写、凭证路由能解析、`kind` 与当前平台匹配。

## 5. 失败分类（一张表定死，PR-4 照着写回群文案）

| 现场 | `kind` | 判定依据 | 给人看的话 |
|---|---|---|---|
| 正常跑完 | `done` | NDJSON 里有 `final` 且 answer 非空 | （结果正文） |
| 会话被占 | `lock_conflict` | stdout/stderr 出现 `already owned by an active write handle` | 「这个会话你正开着，我不敢动；关掉我再来」 |
| 超时 | `timeout` | 到 `timeout_seconds` 还没 `final` | 「超时了，卡在 <最后一步>」 |
| 进程消失（睡眠/被杀） | `no_final` | 进程退出但**没有 `final`** | 「机器睡过去了，我没干完」 |
| 凭证不可用 | `credential_missing` | 起跑前 `_credential_route` 解析不出 | 「我这边的登录好像过期了」 |
| CLI 起不来 | `cli_unavailable` | `Popen` 抛 `FileNotFoundError` / `prepare` 没过 | 「我这边工具起不来，先别派活」 |
| 其它 | `error` | 退出码非 0、事件不合法、answer 空 | 「干到一半出错了：<脱敏后的第一行>」 |

**不做的事**：不重试、不换会话硬闯、不把 stderr 全文贴给群里（只贴脱敏后的一句话）。

## 6. 进程与文件纪律

- 子进程 `start_new_session=True`；取消/超时先 `SIGTERM` 到进程组，5 秒后 `SIGKILL`，再 `wait()` 回收
- **每个活的运行目录**：`<bridge home>/work/<task_id>/`（不用 `/tmp`——Windows 重启会清空 `/tmp`，设计稿 §6.1 有实测）
  - `overlay.yml`（0600，跑完即删）
  - `output.ndjson`（原始事件流，便于事后复盘，按 `artifact_retention_days` 清理）
  - `artifacts/`（干活产物）
- 日志只写 `task_id` + `kind` + 耗时 + 输出字节数；**不写凭证、不写 overlay 内容、不写完整错误堆栈**
- 同一 `session_id` 并发调用：由 dsh 自己的写句柄锁拦下（`lock_conflict`），Workbench 不自己做互斥

## 6.5 交接包：杀死之后必须交得出去

主人 2026-11-18 的原话：**"杀死以后，一定要通知给人类前台……不能仅仅杀死就完毕了，要能接得上；
把任务需求、当前进度、输出物、工作空间等，要给人类做一个交接。"**

所以 PR-2 的合同改成：**任何非 `done` 的结局，都必须带一份完整交接包**；只杀不交 = 失败。

```python
@dataclass(frozen=True)
class Handoff:
    task_id: str
    requirement: str        # 任务原话（群里那条 + 主人批卡时补的话）
    last_step: str          # 卡在哪一步（取事件流里最后一个 status/step_start 的文字）
    progress_lines: list[str]  # 已经做过的事（事件流摘要，最多 10 行）
    artifacts: list[str]    # 已经产出的文件（相对业务目录）
    workspace: str          # 业务目录（这台机器上的绝对路径）
    space: str | None       # 本次的工作目录（人可以直接进去看产物）
    session_id: str         # dsh 会话 id：可以续跑
    resume_command: str     # 「接着干」的可粘贴命令（dsh 续跑该会话）
    failure: str            # 一句人话：为什么停（超时 / 机器睡过去 / 锁冲突 / 凭证）
```

- **落盘两处**：`<bridge home>/work/<task_id>/handoff.md`（人看的）+ `handoff.json`（机器看的）
- **谁来发**：PR-4 负责把它发到私聊（并回群一句摘要）；PR-2 只负责"造得出来、内容齐全"
- **判据**：每个非 `done` 用例都断言 `handoff` 非空且 9 个字段都有值；用 SIGKILL 杀掉的用例同样要能交出
  （事件流是边跑边落盘的，所以杀之前已经写下的进度不会丢）
- `resume_command` 用会话 id 续跑（设计稿 §6.1 实测：被打断的会话文件完好，可续）

## 7. 测试（假 CLI，`tests/test_workbench.py`，约 17 条）

| # | 用例 | 判据 |
|---|---|---|
| 1 | 正常跑完 | `done`、text 是 final 内容、耗时>0 |
| 2 | 没有 `final`（进程正常退出） | `no_final` |
| 3 | 超时 | `timeout`，且进程组内**无残留**（`pgrep -P` 空） |
| 4 | 取消（`stop.set()`） | `timeout`，同上无残留 |
| 5 | 锁冲突文案 | `lock_conflict` |
| 6 | 非法事件类型 | `error`（不容忍部分成功） |
| 7 | 空 answer | `error` |
| 8 | CLI 不存在 | `cli_unavailable` |
| 9 | 凭证缺失 | `credential_missing`，**错误文案里不含任何 key 片段** |
| 10 | overlay | 文件存在时 0600、内容与 golden 逐字相同、跑完被删 |
| 11 | 输出超上限 | 截断到 `max_output_bytes`，`output_bytes` 如实记录 |
| 12 | `prepare()` 各项校验 | 5 种不良配置各一条失败用例 |
| 13 | **交接包齐全** | 7 种失败各一条：`handoff` 非空、9 字段有值、`handoff.md`/`handoff.json` 存在 |
| 13b | **SIGKILL 后仍能交接** | 直接杀进程组 → 进度来自已落盘的事件流，`handoff` 仍完整 |
| 14 | 抽取没写第二套 | `dsh-consumer.py` 里零命中 `Popen`/`headless`/overlay 字面量；三个常量只有一处定义 |
| 15 | 现有 consumer 测试 | 全绿（行为不变的判据） |

## 8. 不做什么（防止范围膨胀）

- 不产生 `work` 事件、不回飞书、不动 `tasks`（PR-3/PR-4）
- 不写 Windows 的 `cmd-interop`（S3b），只留 `launch` 字段
- 不碰浏览器（浏览器独占性留到 S3b）
- 不引入新依赖（只用 stdlib + 现有 transport）

## 9. 风险

| 风险 | 处置 |
|---|---|
| 抽取时悄悄改变 consumer 行为 | 现有 consumer 测试全绿 + overlay golden + 逐字常量对比，三条一起 |
| 干活跑太久烧 token | 任务级 `timeout_seconds` 默认 1800，超时先杀进程组再判定 |
| 产物太大撑爆磁盘 | `artifacts/` 按 `artifact_retention_days` 清理；单次输出上限 2 MiB |
| 凭证泄漏到日志 | 错误信息过一遍脱敏（复用现有 transport 的脱敏函数），测试断言"文案不含 key 片段" |

## 10. 估时

抽取与测试 **1 天**（原估 1 天，抽取比新写更慢但只留一套）。

## 11. 三件由我定的事（主人 2026-11-18：「你结合系统和设计去评估」）

1. **任务级超时**：`timeout_seconds = 1800`。到点**杀进程组**，但**必须交交接包**（§6.5）——
   需求原话、卡在哪一步、做过什么、产物、工作空间、可续跑的会话 id 与命令。不交包 = 这个 PR 没做完。
2. **`workspace` 的语义**：它只是"这是哪个项目"的声明（默认本仓库），**实际 `cwd` 由调用方传入**，
   S3a 里就是每个活自己的独立目录 `work/<task_id>/space/`。这样同一个 Workbench 能给任何项目干活，
   而"在哪个目录跑"永远是显式的参数，不靠猜。
3. **凭证路由**：`auto` —— 环境里有 `DEEPSEEK_API_KEY` 就用它；没有就用主人已保存的 dsh 登录
   （`credentials`/`authorization`/`deepseek-account` 三行 `disabled: false`）。
   理由：headless 默认关闭已保存登录（§6.1 实测），而干活要产出**归属主人**的产物，用本人身份最不容易出岔子。
   两条路都解析不出来 → `credential_missing`，并把"下一步怎么做"写进 `handoff.failure`。

## 12. 收工记（PR-2 已实现，2026-11-18）

- `bridge/workbench.py`（741 行）：`Workbench` / `WorkResult` / `Handoff`；overlay 文本 golden 冻结
- `dsh-consumer.py` 从 458 行缩到 **128 行**，运行器部分删掉改成调用——**没有第二套**
- 交接单 §6.5 已落地：7 种失败各有一条"交接包齐全"用例，外加一条 SIGKILL 后仍能交出（进度来自已落盘事件流）
- **续跑命令故意不带沙箱 overlay**：无人值守那次是 `workspace-write` + `approval=never`（全程不问人）；
  人接手时该按自己的策略跑。交接单里现在自己写明这一点（用例钉住），免得接手的人以为命令漏了参数
- 两个实现坑已写进 HISTORY：macOS `/var` → `/private/var` 符号链接导致 workspace 判定失败；
  同一件活重派时 `O_EXCL` 变 `FileExistsError` → 改成把上一次现场挪进 `attempts/<时间戳>/`，不覆盖也不删

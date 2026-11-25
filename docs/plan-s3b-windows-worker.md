# S3b 设计稿（v3）：**一个 bridge agent，两套操作系统适配，功能必须完全一样**

> 状态：**设计稿，等主人 review**。
> 原则 2（主人 2026-11-19 第三次纠正，**这条最要紧**）：**除了我们这台开发机，别人的机器上没有 git。**
> 所以干活端**只有一种形态**（没有 A/B 之分）：
>
> **每个活一个独立目录 `work/<task_id>/`；产物是文件清单（路径 + 大小）；没有 git、没有工作树、没有提交。**
>
> 回执写「产物：`…/result.csv`（N KB）」，**任何地方都不出现 commit hash**；
> 不假设 git 存在、不因为"没有 git"拒活、也不静默——`workbench check` 只报一个 `mode=dir`。
>
> 原则 1（主人 2026-11-19 两次强调）：**Windows 和 mac 上的 bridge agent 是同一个产品、完全相同的功能，
> 只为适配不同操作系统而实现不同。** 不允许出现"这台上能做、那台上做不了"的功能差异；
> 允许的只有**实现方式**不同，且每一处差异都要在 CONTRACTS 里列出并配同一条判据。

---

## 0. 这条原则怎么落到判据上

1. **功能清单两端逐项对齐**：§1 那张表里每一行，mac 与 Windows 都要有，且用同一条判据验
2. **同一句命令、同一句回话**：命令词汇（`help`/`上班`/`下班`/`状态`/`执行`/`记住：`/`撤回`/`清单`）、
   卡片格式、批准方式、回执文案、交接单结构、失败人话——两端**同一份代码、同一份文案**
3. **判据可对照**：`feishu-bridge doctor --assert` 两端都 rc=0；`pytest` 两端跑同一套；
   `workbench check` 两端输出**同样的行**（值不同是正常的：平台、路径、CLI 版本）
4. **能力缺失必须明说**：真存在平台做不到的事（例如 `ego-browser` 没有 Windows 版），
   处理方式是"**两端一致的失败语义**"——不会就诚实说不会、并给替代路径，**不许静默不支持**

## 1. 功能对等清单（S3b 的验收就是这张表逐行打勾）

| # | 功能 | mac 上 | Windows 上（本切片目标） | 同一判据 |
|---|---|---|---|---|
| 1 | 群里 @ 回话 | 回话端 claim → dsh → reply | 同（WSL 里跑同一个 consumer） | 群里出现 `reply_verified` 行 |
| 2 | 私聊命令（8 条） | `commands` 精确匹配 | 同（同一份 `bridge/commands.py`） | 逐条命令命中 + 近似词回引导 |
| 3 | 说明书 `help` | 发全文（可切条） | 同 | 收到说明书 + 零系统词 |
| 4 | 任务卡 | 五段人话 + 代码写死动作行 | 同 | 卡片逐段断言 |
| 5 | 三档批准 + `owner_direct` | 引用/唯一待批/命令/主人直派 | 同 | 四种 `approval_kind` 各一条用例 |
| 6 | `work` 事件 + 干活端 | `dsh-worker` + 独立目录 + 产物清单 | **同一形态**（本切片核心） | 各自跑一件自检活：产物目录里有文件 + 群里回执带路径（**无 hash**） |
| 7 | 结果回写 | 群里带引用 + 私聊交接单 | 同 | `say.parent_id` 指向原消息 |
| 8 | 失败语义 | 九种 `kind` → 状态+人话 | 同（九种都覆盖，含 Windows 两种新的） | 每种各一条用例 + 交接单齐全 |
| 9 | 慢响应回执 | 15 秒应一声 / 180 秒报进度 | 同 | 秒答不发；慢答两条 |
| 10 | 待人工项与关单 | `attention list/close` | 同 | 关单后 `attention_clear=ok` |
| 11 | 出站唯一出口 | outbox v11 | 同 | `test_single_exit` 全绿 |
| 12 | 安装/升级/回滚 | `install.py` + launchd | **同一份 `install.py` + systemd** | `--verify` rc=0；`running_release` 正确 |
| 13 | 体检 | `doctor --assert` | 同 | rc=0，九项全 ok |
| 14 | 浏览器（ego） | 有 | **平台无此能力** | 两端一致的"诚实回话"机制（见 §4） |

## 2. 只允许这六处实现不同（其余必须同一份代码）

| # | 关注点 | mac | Windows | 判据 |
|---|---|---|---|---|
| 1 | 服务管理 | launchd 用户单元 | **systemd 系统单元（WSL2 内）** | `systemctl status` active；重启才换代码 |
| 2 | 工作台启动 | `direct`（`dsh web`/headless 同 home） | **`cmd-interop`**：**桌面版是主形态**（同事日常都用桌面版），`cmd.exe /c "<安装目录>\resources\runtime\cli\bin\dsh.cmd"`；只有在**没有桌面版**的机器上才退到 WSL 的 `dsh`（`dsh-web` 形态） | `workbench check` 明说用的哪份 + 版本 + 与桌面是否同一 home |
| 3 | 路径 | POSIX | **`/mnt/c/…` ↔ `C:\…` 双向映射**，UNC/盘符不存在报错 | 映射单测正反例 + argv 列表传参（空格/中文） |
| 4 | 进程树 | `killpg` | **`taskkill /T /F` + `killpg` 双保险** | 取消后 `tasklist`/`ps` 零残留 |
| 5 | 凭证/会话 home | `~/.dsh` | **`C:\Users\<用户>\.dsh`**（同一套 overlay 三行） | `workbench check` 报 home 与是否同一套 |
| 6 | 运行目录 | `~/Library/Application Support/…` | WSL 的 `~/.feishu-personal-agent`（**不放 `/tmp`**，重启会清） | 运行现场能被找到（`result-a1.json` 等） |
| 7 | **隔离形态** | 独立目录 `work/<task_id>/` + 产物清单 | **同**（同一份代码） | `workbench check` 打印 `mode=dir`；回执与账本只写产物路径 |

## 3. 要做的事（顺序即依赖）

1. **把当前 release 装到 Windows 那台**（含 S3a 全部功能），`install.py --verify` → `doctor --assert`
2. **`cmd-interop` 启动器 + 路径映射**（纯函数、先单测：`/mnt/c` ↔ `C:`、空格、中文、UNC 报错）
3. **干活端在 Windows 跑通**（独立目录 + 产物清单 + 结果回写；与 mac 同一份 `worker.py`/`workbench.py`）
4. **两种 Windows 专属失败**：`interop_unavailable`（cmd 调不起）、`path_mapping_error`（路径认不出）→ `blocked` + 人话
5. **两端判据对照表**：写一个 `tools/parity-check.md`（或 doctest 式脚本），把 §1 十四行逐条列出"怎么验、两端输出"

## 4. 平台能力缺失怎么处理（两端一致，不许静默）

`ego-browser` 没有 Windows/WSL 版。按"功能完全一致"的原则，处理方式是：

- mac：能开浏览器干活 → 正常执行
- Windows：**接到浏览器类活时，明确回"这台机器没有浏览器能力，要么换 mac 那台，要么我给你换成只读方案"**，
  并落 `blocked` + 交接单（不是失败、不是假装在做）
- CONTRACTS 里写成"平台能力矩阵"一行，这是**意图内**的差异，不是功能缺失

## 5. 真机验收（两端各一次，输出可对照）

1. `install.py --verify` → `doctor --assert` rc=0
2. `dsh-worker.py --check` → `workbench check` 九行 PASS（含 CLI 路径/版本/home）
3. 群里 @ 发一件自检活 → 批准 → 断言：`events.consumer`、`work/<task>/` 里真有产物文件、群里带引用回执且**没有 hash**
4. 三种失败：开着会话再派（`blocked`）、`dsh.cmd` 改名（`interop_unavailable`）、非浏览器平台接浏览器活（`blocked` + 明说）
5. `pytest -q`：两端同一套；平台相关用例若跳过，必须**列出来并说明为什么**

## 6. 估时

**2–3 天**：装新版 + systemd 复核（0.5）、`cmd-interop` 与路径（1）、真机验收与踩坑（1）、对照表与文档（0.5）。

## 7. 风险

| 风险 | 处置 |
|---|---|
| 桌面版 home 与 WSL `dsh` 不是一套 | `workbench check` 打印"与桌面同一 home？"，不一致直接 `blocked` |
| 桌面版独占 `profiles/desktop` | 不写它自己的 profile，只用 overlay（§10.2 实测可合并） |
| 中文/空格路径 | argv 列表传参 + 单测 |
| **机器上没有 git** | 本来就不依赖 git；`workbench check` 只报 `mode=dir`；**绝不因此拒活** |
| 杀毒拦 spawn | `workbench check` 加"起一个短命 dsh 进程"探针 |
| WSL 重启清 `/tmp` | 运行目录/overlay/工作树全放持久路径 |
| 两端 node/dsh 版本漂移 | 验收第一条并列打印两端版本，不一致直接 FAIL |

## 8. 不做

- 不在原生 Windows Python 里跑桥（设计稿 §4：原生不支持，必须 WSL2）
- 不做桌面版 GUI 自动化（不点按钮）
- 不做两端状态同步（两台独立机器，各自一本账）
- 不为了"看起来一致"而给 Windows 造一个假的浏览器能力
- **不给任何机器装 git、不做工作树、不做提交**（干活端唯一的隔离就是"每个活一个目录"）
- 回执里不出现 commit hash（没有提交这回事）

## 9. 待主人确认 / 已澄清

1. **工作台主形态（已澄清）**：同事日常用**桌面版**，所以 Windows 侧默认 `kind=dsh-desktop`、
   CLI 取桌面版自带 `dsh.cmd`、home 取 `C:\Users\<用户>\.dsh`；WSL 的 `dsh` 只作为**没有桌面版时**的兜底。
   `workbench check` 必须打印"用的是哪份 + 版本 + 与桌面是否同一 home"。
2. **第一台做哪台**：建议开发机（不影响别人）→ **待你点头**。
3. **提交作者（已澄清，不是一个新角色）**：提交是 git 自己的 author 字段，取**那台机器的 git 配置**，
   没有"Windows 侧作者"这种角色。事实：mac 上干活端提交的作者就是本机配置
   （形如 `<本机 git config 里的用户名> <…@users.noreply.github.com>`）；Windows 上同理，会是那台机 `git config` 里的身份。
   "哪台机器干的"这件事由账本的 `executor` 字段记录（`dsh-web@<机器名>`），不需要在作者字段上做文章。
   如果以后希望两端作者统一，改那台机器的 `git config` 即可，**不动代码**。

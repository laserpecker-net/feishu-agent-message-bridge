# 只在本机、不进 git 的东西（真值放这里，仓库只放占位）

> 规则一句话：**仓库里可以是占位符，真值必须在本机且被 `.gitignore` 挡住。**
> 这条规则由测试守：`python3 -m pytest tests/test_repo_hygiene.py -q`
> （扫描**所有被 git 跟踪的文件**，出现真实姓名 / open_id / chat_id / app_id / tailnet 名 /
> 机名 / Tailscale 或私网地址 / 某人家目录 / 真 key，就失败）。

## 为什么需要这份文档

本机确实需要一些"真值"：同事叫什么、她那台机器的 MagicDNS 名、她账号的 open_id、
某台机器上一次性 auth key 的记录……这些**不能进版本库**（会随 clone、zip、镜像扩散），
但也不能"靠记忆"——记错机名就会把命令发到别人机器上。所以：

- **真值**：放本机的 CSV / 环境变量 / 配置里，路径写在下面的表里；
- **仓库**：只放模板（`*.example.*`）与"怎么填"的说明；
- 文档里需要举例时，一律用占位符：`<名字>`、`<机名>`、`<tailnet>`、`ou_example…`、`100.64.0.1`。

## 本机专用文件一览

| 路径 | 里面是什么真值 | 为什么不能进 git | 怎么建 / 怎么恢复 |
|---|---|---|---|
| `docs/windows-hosts.csv` | 同事称呼、飞书 open_id、机名/MagicDNS、Tailscale IP、SSH 用户 | 姓名 + 账号 ID + 内网地址 | `cp docs/windows-hosts.example.csv docs/windows-hosts.csv` 后照 [windows-hosts.md](windows-hosts.md) §3 登记 |
| `dist/windows/` | 离线 SSH 一键包产物（内含真实公钥注释、微软 MSI 二进制） | 公钥注释含「用户@主机名」，MSI 是 6.9 MB 二进制 | `bash tools/windows/ssh-bootstrap/build-package.sh`（见该目录 NOTES.md） |
| `research/` | 平台事实快照（真实 app_id / chat_id / 抓包结论） | 真实应用与群 ID | 需要时按 [../AGENTS.md](../AGENTS.md) 的取证方式重跑，结论再写进正文 |
| `coordination/` | 多 agent 协作的中间产物、审查快照 | 常含真值与半成品结论 | 用完即删；不进仓库避免被当现行文档 |
| `reviews/` | 本地评审报告 | 同上 | 同上 |
| `acceptance/` | 真机验收记录（含真实地址与输出） | 含内网地址/账号 | 需要长期留的证据转写进 `docs/HISTORY.md`（脱敏后） |
| `var/` | bridge 运行期状态（队列、锁、临时文件） | 运行数据 | 由程序自己创建，删掉会自动重建 |
| `.env` / `.env.*` | 本机凭据（token） | 凭据 | 参照 `.env.example`（若有）或文档里的环境变量说明 |
| `config.json` | 本机 bridge 配置（`target_open_id`、署名等） | 含账号 open_id | `python3 install.py` 生成；**它属于安装产物，不属于源码** |

> `docs/windows-hosts.csv` 是这张表里最关键的一项：`AGENTS.md` §5 要求"动某位同事的机器前先
> `python3 tools/windows_host.py <名字>` 定位"，那条命令读的就是这个文件。**它丢了**就照 example
> 重建一遍（不确定的字段留空，工具会标"未知"而不是瞎猜）。

## 提交前的两条自检

```bash
python3 -m pytest tests/test_repo_hygiene.py -q     # 门禁：跟踪文件里有没有真值
git status --porcelain                              # 别把上面表里的路径 add 进去
```

`.gitignore` 里每一项都对应上表的一行；如果新增了"只在本机"的文件，**先加 ignore，再写文档**，
最后把路径补进 `tests/test_repo_hygiene.py` 的 `LOCAL_ONLY_MUST_BE_IGNORED`——三处一起改，
否则下一次有人 `git add -A` 就会把真值带进提交。

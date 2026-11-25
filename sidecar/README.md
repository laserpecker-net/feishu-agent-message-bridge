# sidecar：as_bot 的长连接收消息侧

## 它做什么

```
飞书 --WS--> sidecar/index.js --POST 127.0.0.1/v1/ingest--> 桥（与 as_user 相同的下游）
```

只做三件事：**建长连接、把 `im.message.receive_v1` 原样转发、断线重连**。
谁该不该回、怎么回，全在桥里；sidecar 不做业务判断。

## 为什么不用 webhook

webhook 要求一个**公网可达的 HTTPS 入口**（域名 + 证书 + 签名校验），内网/家用机器基本开不出来。
长连接不需要任何入站端口——这也是多数飞书机器人的做法。

## 安装

**装桥时自动完成**：`install.py --identity-mode bot` 会把 `index.js`/`package.json` 复制到
`<bridge home>/identity-sidecar-app/` 并在那里 `npm install --omit=dev`（幂等，跨 release 复用），
缺 `npm` 或装依赖失败会在写单元**之前**报错并给下一步——不允许出现"单元装好了、进程一启动就崩"。

手工排查时等价于：

```bash
cd "$HOME/Library/Application Support/FeishuPersonalAgent/identity-sidecar-app"
npm install --omit=dev     # 只装 @larksuiteoapi/node-sdk
node index.js              # 前台跑一次看日志
```

依赖为什么不在 release 里：release 必须是**不变副本**，而 node_modules 是运行时产物；
另一个原因是 ESM 的 `import` 只从脚本所在目录往上找 `node_modules`，所以 app 与依赖必须同目录。

## 运行（由桥自动拉起，一般不用手工跑）

```bash
FEISHU_APP_ID=cli_xxx \
FEISHU_APP_SECRET=xxx \
FEISHU_INGEST_URL=http://127.0.0.1:8787/v1/ingest \
FEISHU_INGEST_TOKEN_FILE="$HOME/Library/Application Support/FeishuPersonalAgent/gateway-token" \
node sidecar/index.js
```

- `FEISHU_INGEST_URL` **只接受 127.0.0.1 / ::1**（脚本自己会拒绝其它 host 并退出码 2）
- token 轮换后会自动重读 token 文件（401 时重试一次）
- 桥侧另有一条对账路径（`BotIdentity.collect`）：断线窗口按时间窗补，不依赖 sidecar 队列

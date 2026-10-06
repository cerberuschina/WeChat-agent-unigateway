# 把一个 agent 接进网关 —— 每种 agent 一条命令

> 结论先说：**网关那一侧的动作对所有 agent 都一样**（登记 agent → 开一条进来的路 →
> 重启 → 验证），不一样的只有 agent 自己那一侧。所以这里是一个引擎 + 每种 agent 一个
> profile：`python join.py <agent>` 就是那个 agent 的「一键接入脚本」。

```
python join.py --list          # 现成的 profile 有哪些
python join.py workbuddy       # 登记 + 写它的配置 + 重启网关 + 验证
python join.py --status        # 现在接进来了谁
```

## 一、先认清：agent 只有三种进来的姿势

| 姿势 | 谁去找谁 | 什么时候用 | 写进配置的 `type` |
|---|---|---|---|
| **网关调它** | 网关发消息给它、等它回 | 它有 CLI（`claude -p`）、有 A2A 端点、或有个 HTTP 接口 | `exec` / `a2a` / `http` |
| **它来拉（能扫码）** | 它自己取码、长轮询 | 它**能改自己的微信 base-url**（Hermes 的 `WEIXIN_BASE_URL`、本仓库的 `clients/ilink_agent_client.py`） | `virtual` + `auto_approve` |
| **它来拉（不能扫码）** | 同上，但码得我们给它 | 它取码地址写死在腾讯，但**读自己配置里的 base-url**（WorkBuddy 桌面版） | `virtual` + `accept_tokens` |

第三种的 token 由网关签发、落在 `data/<名字>-token.txt`，`gateway.json` 里只记一行
`"accept_tokens": {"workbuddy": "file:data/workbuddy-token.txt"}` —— **密钥不进配置文件**，
和 `account.json` 一个待遇（`.gitignore` 已挡住整个 `data/`）。

## 二、一张表：谁用什么命令

| agent | 一条命令 | 它会怎么被接进来 |
|---|---|---|
| **WorkBuddy** | `python join.py workbuddy` | token 接入；顺带写 `~/.workbuddy/settings.json` 的 `claw.channels.weixinClawBot`（`baseUrl` → 网关） |
| **Hermes**（微信渠道） | `python join.py hermes` | 二维码接入；它把 base-url 指到网关，自己取码（已在 `auto_approve` 里） |
| **Claude Code** | `python join.py claude` | 默认走 a2a-bridge 的 `http://127.0.0.1:8799` |
| **Claude Code（直接跑）** | `python join.py claude --mode exec` | `claude -p {text}`，stdout 就是回复 |
| **CodeBuddy CLI** | `python join.py codebuddy` | `codebuddy -p {text}`（没装会有提示） |
| **任何 iLink 客户端** | `python join.py ilink` | 让它连 `127.0.0.1:18500` 自己取码 |
| **只有桌面版的 agent** | `python join.py cdp --url bridges/profiles/<x>.json` | CDP 桥进它的输入框（见 `BACKENDS-WORKBUDDY.md`） |
| **自定义 CLI** | `python join.py mycli --mode exec --command "my-cli --ask {text}" --prefix m` | exec |
| **自定义 A2A** | `python join.py myagent --mode a2a --url http://127.0.0.1:9000` | a2a |
| **自定义 HTTP** | `python join.py myapi --mode http --url … --body-template '{"q":"{text}"}' --reply-path reply` | http |

命令跑完，在微信里就能这样用（`prefix` 就是那个字母）：

```
/w 帮我把这个报错的栈追一下          # 这一条发给 WorkBuddy
/use w                              # 这个会话以后都给 WorkBuddy
/who  /agents  /help                # 网关自己回
```

## 三、它到底改了什么

跑 `python join.py workbuddy --dry-run` 会先打印「这次要改什么」，一条不多一条不少：

1. `gateway.json` → `agents.workbuddy`（`type` / `label` / `prefix`）——**先备份**
   （`gateway.json.bak-join-<时间戳>`），再原子替换；
2. `gateway.json` → `virtual.accept_tokens.workbuddy = "file:data/workbuddy-token.txt"`
   （token 文件第一次会生成，0600）；
3. agent 那一侧（有 profile 才有）：例如 WorkBuddy 的渠道配置；
4. 重启网关（`--no-restart` 可以跳过）——找到监听虚拟端口那个进程，停掉，用同一个
   python 重新拉起来（脱离当前终端，日志写 `data/gateway.log`），再探一次 `/health`；
5. 最后打印一遍 `--status`。

写得对不对当场就知道：脚本会拿 `load_config()` 把改完的文件读回来（配置有错立刻报），
再问一次网关 `/health` 和 `/admin/binds`。

## 四、随手加一种 agent

改 `agent_gateway/join.py`：写个 `_build_xxx(plan, world, opts)` 再
`_register(Profile(name="xxx", …))` 就完了 —— 登记、token、重启、验证全是共用的。
具体某个产品怎么「指过来」，照 `BACKENDS-WORKBUDDY.md` 的办法先手工跑通一次，
再把它固化成 profile 里的一个适配器。

## 五、排错

| 现象 | 先看哪儿 |
|---|---|
| 网关没起来 | `data/gateway.log`；`--status` 会直说「没在跑」 |
| 微信里 `/w` 说「还没接入」 | 对方没连上来：WorkBuddy 看它自己的日志有没有 `[WeixinClawBotPlugin] status change connected`；iLink 客户端看它有没有取码成功 |
| 消息进来没人回 | 一个 bot 身份**只能有一个进程在长轮询**：网关在轮询真微信，agent 只许连虚拟服务（`127.0.0.1:18500`），别让它也去连腾讯 |
| token 接入连不上 | `gateway.json` 的 `virtual.accept_tokens` 路径对不对（相对路径按**配置文件目录**解析）；token 文件内容与对方配置里的是否一致 |
| 对方日志里刷 `ret=-14` / 重新弹出二维码 | token 已经和网关对不上了（网关重启后换了 token 文件、或你改过它）。**别扫它弹的码**——它取码写死在腾讯，一扫就退回腾讯绑定、把网关这份配置覆盖掉；改成 `python join.py <agent>` 重跑一遍即可 |
| 换了台机器 | `data/` 不在 git 里：token 文件、`account.json` 都要自己带过去（或者重新 `join.py`） |

## 六、安全

- 虚拟服务只绑 `127.0.0.1`；**不**在 `auto_approve` 里的接入一律要人工批准
  （`/bind/<qrcode>` 或 `POST /admin/approve`）——这条门是防「本机任何程序给自己要一个微信身份」的。
- 签发给 agent 的 token 等价于「以这个 agent 的身份收发**这一个微信**的消息」，别外传；
  吊销 = 从 `accept_tokens` 删掉那一行 + 重启网关（对方会拿到 `ret=-14`，和真 iLink 行为一致）。
- `join.py` 只写自己认得的两处（`gateway.json`、已知产品的配置），每次都先备份。

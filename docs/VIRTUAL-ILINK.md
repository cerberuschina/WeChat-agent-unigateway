# 虚拟 iLink —— 让 agent 以为自己在直接连微信

> 核心想法：**网关伪装成腾讯的 iLink 服务端**。agent 照常扫码、照常长轮询"微信"，
> 但它连的是本机网关；网关发它一个**虚拟微信号**，对外只用一个真微信。

## 为什么这么做（而不是给每个 agent 写一个适配器）

| 做法 | 代价 |
|---|---|
| 每个 agent 写一个后端适配器（A2A / HTTP / CLI） | 每个 agent 一套超时、一套鉴权、一套错误翻译；agent 的"在想"和"回话"被拆成两次调用 |
| **虚拟 iLink（本文件）** | agent **零改动**：它本来就有微信接入能力，只是把 base_url 指过来 |

而且消息流变成"agent 自己拉、自己回"——网关不需要等 agent、不需要知道它跑多久、
不需要替它保管上下文。网关只做两件事：**收**（真微信 → 某个 agent 的队列）与
**发**（agent 的 sendmessage → 真微信）。

## 形状

```
真微信（一个号）
   │  ClawBot / iLink
   ▼
┌───────────── agent-gateway ─────────────┐
│  真实 iLink 客户端（唯一持有真凭证）        │
│      │  收消息                            │
│      ▼                                    │
│  router（/c /h /w + /use）                 │
│      │                                     │
│      ▼                                     │
│  虚拟 iLink 服务端  http://127.0.0.1:18500  │
│   ├─ get_bot_qrcode    ← agent A 要码      │
│   ├─ get_qrcode_status → 虚拟身份+token     │
│   ├─ getupdates        → agent A 的消息队列 │
│   └─ sendmessage       → 转发进真微信       │
└───────────────────────────────────────────┘
   ▲                        ▲
   │ base_url 指过来         │ base_url 指过来
 Hermes 微信渠道          Claude / WorkBuddy 的微信接入
```

## 让 agent 连过来（不改它的代码）

### 情况 A：它**已经绑过微信**（例如 Hermes 的 weixin 渠道）

配一行就行：

```jsonc
"virtual": {
  "enabled": true,
  "reuse_real_token_for": "hermes",
  "reuse_token_env": "HERMES_WEIXIN_TOKEN"   // 或者 reuse_token_file: "C:/path/token.txt"
}
```

网关会**沿用那个真 token**：agent 那边什么都不用动（连 token 都不用重取），
只要把它的微信 base_url 指过来。token 本身不写进配置——从环境变量或文件读；
两个都不给时退回"网关自己那个 token"（只有 agent 与网关共用同一个 bot 身份时才正确）。

> ⚠️ 实测过的坑：Hermes 的 `gateway/platforms/weixin.py` 里，
> **消息收发**用的是可覆盖的 `base_url`（`WEIXIN_BASE_URL` 环境变量），
> 但**扫码登录那两步写死了 `ILINK_BASE_URL`**（取码 / 查状态都直接打腾讯）。
> 所以它**没法重新扫一个"我们发的码"**——这种情况必须走 token 复用，
> 而不是指望它扫我们的码。别的客户端也可能有同样的写死，先看它取码的 URL 从哪来。

### 情况 B：它还没绑

把它的微信 base_url 指到 `http://127.0.0.1:18500`，它就会**正常弹二维码**——
只是这个码由网关签发（`qrcode_img_content` 指向 `/bind/<qrcode>`）。

| agent | 怎么指过来 |
|---|---|
| Hermes（weixin 渠道，已绑定） | 环境变量 `WEIXIN_BASE_URL=http://127.0.0.1:18500` + `reuse_real_token_for` |
| 其他 iLink / ClawBot 风格客户端 | 同类 base-url / host 覆盖；配置里有 `ilinkai.weixin.qq.com` 字样的就是它 |

### 两种情况都会发生的事

agent 拿到的身份形状与真的一模一样：
`ilink_bot_id = virt-<name>xxxxxx@im.bot`、`bot_token`（网关签发）、
`baseurl`（指向网关自己）。它之后 `getupdates` / `sendmessage` 全程以为在跟微信说话。

想亲眼看一遍：`python examples/virtual_loop_demo.py`

### agent 侧：用现成的客户端（起步最快）

`clients/ilink_agent_client.py` 就是一个**普通的 iLink 客户端**：扫码拿虚拟号 →
长轮询 → 每条消息跑一条命令 → 把命令的输出发回去。

```bash
python clients/ilink_agent_client.py --name claude \
    --base-url http://127.0.0.1:18500 \
    --runner "claude -p {text}" --cwd K:/ClaudeWork
```

| 参数 | 作用 |
|---|---|
| `--runner` | 命令模板；`{text}` 替换成消息内容（作为**一个**参数，不怕引号/换行） |
| `--stdin` | 改成把消息喂给命令的标准输入 |
| `--once` | 处理一条就退出（测试用） |
| `--timeout` | 单条消息的处理上限（默认 900s） |
| `--creds` | 虚拟身份与游标存哪儿（默认 `data/virtual-client-<name>.json`） |
| `--session-store` | 每个联系人一条会话线（默认存在凭证旁边），配合 `{session}` 做多轮 |
| `--reuse-token` | 直接用已经持有的真 token（对应上面的情况 A） |

**多轮任务怎么接着聊**：runner 只要在输出里打印一行 `##SESSION:<id>`，客户端会把这行
从回复里摘掉、按联系人存下来，下一次用 `{session}` 传回去：

```bash
--runner "claude -p {text} --resume {session}"
```

不这么做的话，每条消息都是**全新会话**——你说"开始 T2"，它不知道 T2 是什么
（第一版就是这样：`--max-turns 1 --allowed Read`，任务类消息还会因为一轮用光而空手而归）。

**等久了会看到什么**：网关在你派活之后一直按着「正在输入」，客户端每 45 秒发一句
「⏳ 还在跑（已 X 分 YY 秒）」（`--progress-every`，0 = 关掉）；agent 跑完才发正文。
单条消息的等待上限默认 **1 小时**（`--timeout`），长任务不会再被 15 分钟砍掉。

微信**不支持编辑已发出的消息**（真适配器里写着 `SUPPORTS_MESSAGE_EDITING = False`），
所以做不到逐字流式；能保证的是「有反馈、不丢结果」。

**Markdown 谁渲染**：网关。agent 直接发 Markdown 就行，网关在发送前把它渲染成微信
能读的样子：`**粗**`→`粗`、标题→`【标题】`、表格去掉虚线行、链接保留地址、
**代码块原样保留**；再按 1800 字上限在段落边界切条，代码围栏不会被切一半
（超长围栏会分行重开，每一条都是完整代码）。

实测过的完整链路（Claude Code 作答 2.6 秒）：

```
真微信 ─► 网关 ─► 虚拟队列 ─► 客户端长轮询 ─► claude -p ─► sendmessage ─► 网关 ─► 真微信
```

游标跟着凭证落盘，重启不重放旧消息；身份失效（网关重启过）会自动重新扫码。

> 写测试脚本时注意：网关的去重是**跨重启**的——同一条测试消息（同 message_id 或同内容）
> 第二次会被当成重复丢掉。每次换个 message_id，或者清掉 data_dir。

> **另一种（不推荐）做法**：hosts 文件把 `ilinkai.weixin.qq.com` 指到 127.0.0.1，
> 再给本机装一张自签证书做 TLS 中间人。它能"无配置"拦截所有客户端，但：
> ① 影响**全机**——包括网关自己要用的那个真实凭证，容易自锁；
> ② 要往系统信任库里装证书；
> ③ 微信升级证书策略时会整片失败。
> 想要那种效果，等到真需要再说；现在用 base_url 覆盖就够了。

## 批准流程（谁会拿到虚拟号）

1. agent 启动 → 调 `get_bot_qrcode` → 网关生成一条**待批准**记录；
   `GET http://127.0.0.1:18500/admin/binds` 能看到它（没有名字）。
2. 你在 `http://127.0.0.1:18500/bind/<qrcode>` 上给它**起个名字**并批准
   （名字就是以后路由用的 `/c`、`/h` 那个名字）。也可以 `POST /admin/approve`。
3. 它下一次轮询 `get_qrcode_status` 就拿到 `confirmed` + 虚拟号
   （形如 `virt-claudea24473@im.bot`）+ token + `baseurl`（指向网关自己）。
4. 之后它 `getupdates` 拿微信消息、`sendmessage` 回微信——**全程以为在跟微信说话**。

**预先授权的写法**：`gateway.json` 里

```jsonc
"virtual": { "enabled": true, "port": 18500, "auto_approve": ["hermes", "claude"] }
```

`auto_approve` 里的名字会在网关启动时就预建并批准好；对应 agent 的扫码流程会自动
拿到它的身份，不需要人工点批准。**没在里面**的 agent 一律要人工批一次——这是防
"本机任何程序都能给自己要一个微信身份"的那道门。

## 路由：一条真微信怎么分给多个 agent

真微信那边只有**一个联系人**（一个真 bot 身份 = 一个会话）。所以进来的消息还是要靠
规则分派：

- 直接说话 → 默认 agent；
- `/c 内容` → 只这一条给 claude；
- `/use claude` → 这个会话以后都给 claude；
- `/agents` `/who` `/help` → 网关自己回。

分派给虚拟 agent 时，网关只是把消息**放进它的队列**，然后在微信里回一句
"已转给 X，算完就回。"——agent 什么时候算完，它自己 `sendmessage` 回来，网关再发出去。

## 现在不支持 / 会明确报错的地方

| 项 | 现状 |
|---|---|
| 图片 / 语音 / 文件 | 不支持：`getuploadurl` 直接返回错误，`sendmessage` 只接受文本（**明确报错，不静默丢**） |
| 群聊 | 不支持（iLink 的 bot 身份通常收不到群消息） |
| 多真微信号 | 不支持：网关只持有一个真身份（这正是它的意义） |
| 虚拟身份的持久化 | 绑定目前**只在进程内存**里：重启网关，agent 需要重新走一次扫码（它的 token 失效会得到 `ret=-14`，行为与真实 iLink 一致） |

## 安全边界

- 虚拟服务只绑 `127.0.0.1`；本机任何程序都能向它要一个身份，**所以用 auto_approve 之外
  的一切绑定都要人工批准**。
- 虚拟 token 只在本机流转；真凭证仍然只在 `data/account.json`（600 权限）。
- 网关不会把虚拟身份的任何东西发往腾讯。

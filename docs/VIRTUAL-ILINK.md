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

绝大多数客户端都认一个 base-url 覆盖：

| agent | 怎么指过来 |
|---|---|
| Hermes（weixin 渠道） | 环境变量 `WEIXIN_BASE_URL=http://127.0.0.1:18500` |
| 其他 iLink / ClawBot 风格客户端 | 同类 base-url / host 覆盖；有 `ilinkai.weixin.qq.com` 字样的配置就是它 |

设好之后，agent 那边会**正常弹二维码**——只是这个码由网关签发。

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

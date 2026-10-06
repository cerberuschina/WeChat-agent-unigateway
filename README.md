# agent-gateway

**一个微信，接住本机所有 agent。**

> A tiny local gateway that owns **one** WeChat (iLink / ClawBot) binding and
> fans it out to **N** agents — Claude Code, Hermes, CodeBuddy/WorkBuddy, or
> anything that speaks A2A, HTTP, or a command line. Stdlib only, no
> dependencies, single process, localhost only.

---

## 它解决什么问题

要给一个 agent 接上微信，通常得给它一个微信号、在微信里装 **ClawBot 插件**、扫码绑定 ——
三个 agent 就是三个微信、三台手机、三次绑定。

这个网关把那一层收成一个：**只绑一个微信**，网关自己持有那个 bot 身份，
往里分给本机的多个 agent。微信上看到的是一个联系人，背后是一整个机房的 agent。

```
                        ┌──────────────── agent-gateway（本进程） ────────────────┐
微信（一个号）           │                                                        │
  └ ClawBot 插件 ──长轮询──►  ilink.py  ──►  router.py  ──► backends.py          │
       ▲                  │   getupdates       /c /h /w         │                  │
       │                  │   游标 + context_token              │                  │
       └─── sendmessage ──┤                                    ├──► A2A  :8800  Hermes
                          │                                    ├──► A2A  :8799  Claude Code
                          │                                    └──► exec  ……    WorkBuddy / 任意 CLI
                          └────────────────────────────────────────────────────────┘
```

关键点：**同一个 bot 身份只能有一个进程在长轮询**。所以网关是那个唯一进程，
后面的 agent 谁都不碰微信。

## 两种接法

**① 虚拟 iLink（推荐，agent 零改动）** —— 网关伪装成腾讯的 iLink 服务端，
agent 照常扫码、照常长轮询，但对面是网关；网关给它一个**虚拟微信号**。

```
微信 ──► 网关（持有唯一真凭证）──► 虚拟 iLink 服务端 :18500
                                      ▲            ▲
                     WEIXIN_BASE_URL─┘            └─ base_url 指过来
                     (Hermes 微信渠道)                (Claude / WorkBuddy 的微信接入)
```

Agent 那边什么都不用改，只要把微信的 base-url 指向 `http://127.0.0.1:18500`
（Hermes 是环境变量 `WEIXIN_BASE_URL`）。两种接入方式：

- **它已经绑过微信**（Hermes 微信渠道就是这种：它的扫码 URL 写死在腾讯那边，没法重绑）：
  配 `"virtual": {"reuse_real_token_for": "hermes"}`，它拿原来的 token 直接被网关接住。
- **它还没绑**：它那边正常弹二维码 → 你在 `/bind/<qrcode>` 上批准 → 它拿到网关签发的
  虚拟号；不想人工点就把它写进 `"auto_approve": ["claude"]`。

想亲眼看一遍：`python examples/virtual_loop_demo.py`（真客户端 + 虚拟服务端，不联网）。
详见 **[docs/VIRTUAL-ILINK.md](docs/VIRTUAL-ILINK.md)**。

**② 直接后端（a2a / http / exec）** —— 网关主动去调 agent，等它返回。

```
微信 ──► 网关 ──┬─► A2A   http://127.0.0.1:8800
                ├─► A2A   http://127.0.0.1:8799
                └─► exec  某条命令行（stdout 就是回复）
```

想用哪种，取决于 agent 自己有什么：**有微信接入能力 → 用①；只有接口/命令行 → 用②。**
两种可以混着配（`agents` 里每个 agent 的 `type` 决定）。

## 特点

- **零依赖**：只用 Python 标准库（`urllib` + 线程），Python 3.9+ 直接跑。
- **一个入口，多后端**：`a2a` / `http` / `exec` 三种适配器，覆盖本机绝大多数形态。
- **先应答，后出结果**：agent 慢，微信不能干等 —— 收到消息先回一句"已转给 X"，
  算完再把结果发回来。
- **同一个会话不乱序**：每个会话串行执行，回复不会前后颠倒。
- **状态在磁盘**：长轮询游标、`context_token`、会话的 sticky agent、去重窗口都落盘，重启不丢消息、不重复转发。
- **两条去重**：`message_id` + 内容指纹（上游会用新 id 重发同样的文本）。

## 快速开始

```bash
# 1) 拿代码（无依赖，不需要 pip install）
git clone https://github.com/cerberuschina/WeChat-agent-unigateway.git
cd WeChat-agent-unigateway

# 2) 绑定一个微信（微信里装好 ClawBot 插件，扫码）
python login.py                 # 凭证写入 data/account.json（已在 .gitignore 里）

# 3) 配置你的 agent
cp gateway.example.json gateway.json    # 改 agents 那一段

# 4) 无副作用地验证路由（不碰微信，不调远端）
python -m agent_gateway --dry-run --once

# 5) 跑起来
python -m agent_gateway --config gateway.json
```

跑起来之后，在微信里对这个联系人说话：

| 你发 | 会发生什么 |
|---|---|
| `帮我看下这个报错` | 发给**当前** agent（默认 hermes） |
| `/c 修一下那个测试` | 这一条只发给 claude（一次性，不改默认） |
| `/use claude` | 这个会话以后都发给 claude |
| `/who` `/agents` `/help` | 看当前 / 看名单 / 看用法 |

## 配置

```jsonc
{
  "account":   { "account_id": "", "token": "", "base_url": "https://ilinkai.weixin.qq.com" },
  "data_dir":  "data",              // 游标、context_token、会话状态都在这
  "default_agent": "hermes",

  "agents": {
    "hermes":   { "type": "a2a",  "label": "Hermes",      "prefix": "h", "url": "http://127.0.0.1:8800", "timeout": 900 },
    "claude":   { "type": "a2a",  "label": "Claude Code", "prefix": "c", "url": "http://127.0.0.1:8799", "timeout": 900 },
    "workbuddy":{ "type": "exec", "label": "WorkBuddy",   "prefix": "w", "enabled": false,
                  "command": ["workbuddy", "--prompt", "{text}"], "timeout": 600 }
  },

  "delivery": { "max_chars_per_message": 1200, "ack": true,
                "ack_template": "已转给 {label}，算完就回。" },
  "access":   { "allowed_users": [] }   // 空 = 谁都能用（单机常见）；填了就只认这些 from_user_id
}
```

`account` 留空时会去读 `data/account.json`（`login.py` 写的那个）。想换绑：删掉它重新扫码。

### 三种后端

| type | 用途 | 必填 |
|---|---|---|
| `a2a` | 任何 A2A 端点（`message/send` JSON-RPC）。[a2a-bridge](https://github.com/) 把 Claude Code / Hermes 包成的就是这个 | `url`，可选 `token` |
| `http` | 一个普通 JSON 接口：把消息渲染进 `body_template`，从 `reply_path` 读回复 | `url`，`body_template`，`reply_path` |
| `exec` | 本地命令行；回复 = stdout。`{text}` 会替换进 argv，没有占位符就把消息写进 stdin | `command`（数组） |

## 与 a2a-bridge 的关系

本项目**不自带** agent 服务端 —— 它只负责"一个微信入口 + 路由"。后端由你自己提供：

- 已经有把 agent 包成 **A2A 端点**的东西（常见做法：一个小服务把 `claude -p` /
  `hermes chat -q` 各自包成 `message/send` 端点，各占一个本地端口）→ 直接填 `url`；
- 只有 HTTP 接口 → 用 `http` 后端；
- 只有命令行 → 用 `exec` 后端；
- 只有桌面应用（没有 CLI/API）→ 用 `bridges/electron_cdp.py`（见 `docs/BACKENDS-WORKBUDDY.md`）。

## 安全与注意事项

- **凭证只在本机**：`data/account.json` 是 bot token，权限 600，`.gitignore` 已挡住。
  它等价于"以这个 bot 的身份收发消息"，不要外传、不要提交。
- **只监听本地**：网关不开放任何入站端口（iLink 是长轮询，出站即可）。
- **一个身份一个进程**：别让网关和别的程序（比如 Hermes 自己的 weixin 渠道）同时轮询同一个 bot 身份，
  那会互相抢消息。要切换入口时，先停一边。
- **`access.allowed_users`**：默认谁给这个 bot 发消息都会被转给 agent。多用户环境请填白名单。
- **能力边界**：iLink 的 bot 身份一般只收 **私聊**；普通微信群消息通常收不到（这是 iLink 侧的限制，不是网关的）。
  目前只处理**文字**消息。
- **合规**：个人微信的自动化存在账号风险，请自行评估；本项目只是把官方 iLink Bot API 用起来，不绕过任何限制。

## 测试

```bash
python -m unittest discover -s tests -t .     # 203 项，全部离线（不联网、不碰微信）
```

测试覆盖：路由语法（含命令与 agent 前缀冲突）、A2A 回复提取、exec 后端（argv/stdin/超时/非零退出）、
iLink 解析与错误映射、**干跑模式下的端到端**（一条微信消息从去重 → 路由 → 后端/虚拟队列 → 回复）、
**虚拟 iLink 的完整登录闭环**（HTTP 层：取码 → 待批准 → 批准 → 拿虚拟身份 → 带 token 调用）、
**真 token 复用**（已绑过的 agent 不改 token 就能被接住，且它的身份不会被别人扫码领走）、
以及 CDP 桥的选择器档案与 JS 片段。

## 目录

```
agent_gateway/
  ilink.py         真实 iLink 客户端（长轮询 / 发送 / 扫码 / 游标与 context_token）
  virtual_ilink.py 虚拟 iLink 服务端（伪装成腾讯那侧，给 agent 发虚拟微信号）
  router.py        路由规则（纯函数，好测）
  backends.py      a2a / http / exec 三种直接后端
  store.py         会话 sticky agent + 去重窗口
  gateway.py       主循环：收 → 路由 → 派发（虚拟队列 或 直接后端）→ 回
login.py           扫码绑定这个网关自己的真微信号
clients/ilink_agent_client.py   把任意 agent 挂到微信上的客户端（扫码 → 长轮询 → 跑命令 → 回话）
examples/virtual_loop_demo.py   一条命令跑完"虚拟 iLink"全流程（真客户端，不联网）
bridges/           可选桥：bridges/electron_cdp.py 用 CDP 驱动只有桌面版的 agent
docs/VIRTUAL-ILINK.md       虚拟 iLink 的设计、接法、批准流程、边界
docs/REMOTE-AGENTS.md       非本机 agent 接入：bind_key、上传/下载、安全边界
docs/PROTOCOL.md            iLink 协议实测要点
docs/BACKENDS-WORKBUDDY.md  WorkBuddy 实测结论 + 两条接法
docs/SWITCH.md              把微信切到网关的步骤（含回滚）
tests/             203 项离线测试
```

## 已知限制 / Roadmap

- [x] 图片 / 文件 双向（`getuploadurl` + AES-128-ECB + CDN；自带纯 Python AES，靠 NIST 向量保证正确性）
- [ ] 语音 / 视频：协议分支已写，未实测
- [ ] 群聊（取决于 iLink 是否给这个身份下发群事件）
- [ ] 一条消息并发问多个 agent（`/all`）与结果汇总
- [ ] WorkBuddy 之类的桌面应用适配（只有本地 API / 无 CLI 的场景）
- [ ] 虚拟身份落盘：现在虚拟号只在内存里，网关重启后 agent 要重新扫码（它的 token 会收到
      `ret=-14`，与真实 iLink 的失效行为一致）
- [ ] 开机自启脚本（Windows Startup / systemd unit）

## License

MIT

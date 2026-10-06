# 把 WorkBuddy 接成后端 —— 现状与做法

> 结论先说：**WorkBuddy Desktop 没有可用的本地 API**，但可以用两种方式接进网关。
> 官方本地接口只有一个"探活"端点，接入靠 `exec` 后端 + 一个桥接脚本。

## 一、实测到的东西（WorkBuddy Desktop 5.6.2）

### 1. 探活端点：`GET http://127.0.0.1:18488/workbuddy/probe` ✅

```json
{"ok":true,"app":"workbuddy-desktop","version":"5.6.2","platform":"win32"}
```

端口段是 `18488 / 18489 / 18490`（多个实例顺位）。**路径白名单**：只接
`GET /workbuddy/probe` 与 `OPTIONS`，**其余一律 404** —— 这是它自己在代码注释里
写明的设计意图："避免误把这里当成业务 RPC 入口"。

网关可以用它做两件事：判断 WorkBuddy 在不在、拿到版本号（所以
`gateway.json` 里可以把它当一个"可探测的存在"，而不是可调用的 API）。

### 2. 不能当接口用的东西（查过，别踩）

| 端口 | 是什么 | 结论 |
|---|---|---|
| `18488/18489/18490` | 探活服务器 | 只有 `/workbuddy/probe`，别的 404 |
| `10672 / 10673` | 内部服务（`X-Request-Id`，JSON） | 未文档化，返回 `{"error":"not_found"}` |
| 动态端口（本次是 `2487`） | **CellJS 运行时**（Express，`X-Cell-Trace-ID`） | 需要认证：`{"error":{"code":"AUTH_REQUIRED"}}`；且端口每次启动都变 |

也就是说：它内部确实有 agent 运行时和 RPC 层（`wsRpc`），但**都是给渲染进程用的**，
拿来做外部集成等于逆向一个每次升级都可能变的私有协议 —— 不做。

### 3. 它自己的 Claw 通道

asar 里能看到 WorkBuddy 自己也有 **Claw** 相关设置（含"元宝派通道"开关）。
如果哪天它官方支持"接一个机器人"，那应该优先用它；在那之前，走下面两条。

## 二、怎么接（两条路，任选）

### 路 A（推荐、最稳）：它有没有官方 CLI？

WorkBuddy 是 CodeBuddy 家族的桌面版；如果装了 CodeBuddy 的命令行版本，那就跟
Claude Code 一样是个 `exec` 后端，零适配：

```json
"workbuddy": {
  "type": "exec", "label": "WorkBuddy", "prefix": "w",
  "command": ["codebuddy", "-p", "{text}"], "timeout": 900
}
```

（把 `codebuddy` 换成实际的命令名；`--help` 确认一眼参数。）

### 路 B（通用兜底）：CDP 桥接 —— 任何 Electron 应用都能这么接

以调试端口启动应用，然后用 Chrome DevTools Protocol 往输入框里打字、把回复读回来：

```bash
# 关掉 WorkBuddy，然后：
"D:\Program Files\WorkBuddy\WorkBuddy.exe" --remote-debugging-port=9222

pip install websocket-client          # 只有这个可选桥需要
python bridges/electron_cdp.py --profile bridges/profiles/workbuddy.json --list
```

把 `bridges/profiles/workbuddy.json` 里的选择器对着真实界面调一次（里面写了怎么调），
然后接进网关：

```json
"workbuddy": {
  "type": "exec", "label": "WorkBuddy", "prefix": "w",
  "command": ["python", "bridges/electron_cdp.py",
              "--profile", "bridges/profiles/workbuddy.json", "--text", "{text}"],
  "timeout": 900
}
```

**这条路的好处**：不动它任何私有协议，升级了最多改一个选择器；
**代价**：需要 WorkBuddy 开着、且开着调试端口（等于开着开发者工具的口子，仅本机可连）。

## 三、给网关的动作清单（做完打勾）

- [x] 决定走 A 还是 B —— **都不用**：5.6.2 里它自己带了一个 weixin claw 插件，走「虚拟
      iLink + 网关签发 token」更省事（下面第四节）
- [x] 走 A：确认 `codebuddy`（或实际命令）存在，填进 `gateway.json`
- [ ] 走 B：`pip install websocket-client`；带 `--remote-debugging-port=9222` 重启 WorkBuddy；
      调 `profiles/workbuddy.json` 的三个选择器；`--text 你好` 跑通
- [x] `enabled: true`，`python -m agent_gateway --dry-run --once` 看路由
- [ ] 微信里 `/w 你好` 试一发

## 四、实测补充（2026-10-06，5.6.2）：它自己就是个 ClawBot 客户端

「微信助理」那一屏不是腾讯的什么专有接口，就是 **iLink / ClawBot**：asar 里
`packages/workbuddy-server/src/claw/plugins/weixin/` 有完整实现，取码走的正是
`GET {apiBaseUrl}/ilink/bot/get_bot_qrcode?bot_type=3`，扫码后按
`get_qrcode_status` 拿 `bot_token` —— 和本网关 `login.py`、`clients/ilink_agent_client.py`
是同一套协议。

两个关键事实：

1. **取码 URL 写死**：`ClawService.startWeixinQrLogin()` 调的是无参
   `startWeixinQrLogin(apiBaseUrl = DEFAULT_API_BASE_URL)`（`https://ilinkai.weixin.qq.com`）。
   → 它**永远扫不到我们发的码**，只能「它先绑真微信、我们再接管」或者「我们直接发它一个身份」。
2. **收发 URL 可覆盖**：`WeixinClawBotConfigAdapter.resolveAccount()` 里
   `baseUrl = rawConfig.baseUrl || DEFAULT`，之后每次调用都拼
   `${credential.apiBaseUrl}/ilink/bot/…`，Authorization 是 `Bearer <botToken>`。
   → 只要把渠道配置里的 `baseUrl` 指到网关，它就以普通客户端身份连上来了。

渠道配置在 **`~/.workbuddy/settings.json`** 的 `claw.channels.weixinClawBot`
（`botToken / accountId / channelId / userId / baseUrl`；`claw.users.<uid>.channels` 是
按用户分的那一份，`claw.channels` 是 legacy，未登录/未认领时按 legacy 算）。

于是接法只有一条命令：

```bash
python join.py workbuddy        # 登记 agent + 发 token + 写上面那份配置 + 重启网关
```

它拿到的虚拟号形如 `virt-workbuddy…@im.bot`，之后微信里的 `/w 你好` 就归它。想撤：从
`gateway.json` 的 `virtual.accept_tokens` 删掉那一行、重启网关（它会收到 `ret=-14`）。

> ⚠️ 别在 WorkBuddy 界面里点「连接/扫码」：它的码是向腾讯要的，扫完会把我们写进去的
> 那份渠道配置覆盖成腾讯绑定。要重连就跑一次 `python join.py workbuddy`。


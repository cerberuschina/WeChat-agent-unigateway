# Mac 接入端（本地工具 → studio 网关）

2026-10-26 搭好。**微信入口仍是 studio 的 Docker 网关**，Mac 这边作为它的一个远程 agent 接入：
消息从微信进 studio 网关，按前缀路由到 Mac，由本机工具跑完再原路回微信。

```
微信（bot dd191a869fe5@im.bot，只有 studio 在长轮询）
  ↓
studio Docker 网关  agent-gateway（config/gateway.json）
  ├─ claude       → A2A 127.0.0.1:8799
  ├─ hermes       → A2A 127.0.0.1:8800
  ├─ mac-hermes   → 虚拟 iLink（token）→ Mac 客户端 → 本机 hermes   ✅ 在跑
  └─ mac-claude   → 虚拟 iLink（token）→ Mac 客户端 → 本机 claude   ✅ 在跑（走 DeepSeek 供应商）
```

## studio 侧改了什么

`/home/cerberusubuntu/agent-gateway/config/gateway.json`（备份：`gateway.json.bak-macjoin-*`）：

```jsonc
"agents": {
  "mac-claude": {"type": "virtual", "label": "Mac Claude", "prefix": "mc"},
  "mac-hermes": {"type": "virtual", "label": "Mac Hermes", "prefix": "mh"}
},
"virtual": {
  "public_url": "http://100.95.111.58:18500",
  "accept_tokens": {
    "mac-claude": "file:/app/data/mac-claude-token.txt",
    "mac-hermes": "file:/app/data/mac-hermes-token.txt"
  }
}
```

- token 文件在 studio 的 `~/agent-gateway/data/mac-*-token.txt`（mount 进容器是 `/app/data/...`）。
  路径必须写 **容器内路径**；写 `data/...` 会被解析成 `/config/data/...`（不存在的目录）。
- 走 token 而不是扫码，是因为扫码会把「随便哪个待领取的绑定」发给客户端，多 agent 时会拿错身份。

## Mac 侧

- 客户端：仓库里自带的 `clients/ilink_agent_client.py`，长轮询 studio 的虚拟 iLink。
- 常驻：两个 LaunchAgent（KeepAlive，登录即起）
  - `com.cerberus.mac-agent-hermes` → 日志 `data/mac-hermes.log`，身份 `data/virtual-client-mac-hermes.json`
  - `com.cerberus.mac-agent-claude` → 跑 `scripts/run-mac-claude-client.sh`，日志 `data/mac-claude.log`
- runner：
  - hermes：`/Users/mac/.local/bin/hermes -m deepseek-v4-pro -z {text}`
    （hermes 默认模型是 anthropic/claude-opus-4.6，DeepSeek 供应商不认，必须带 `-m`）
  - claude：`--session-command`（stream-json 长会话），env 由启动脚本从 CC Switch 读

```bash
# 看状态 / 重启 / 停
launchctl print gui/$(id -u)/com.cerberus.mac-agent-hermes | grep -E "state =|pid ="
launchctl kickstart -k gui/$(id -u)/com.cerberus.mac-agent-hermes
launchctl bootout   gui/$(id -u)/com.cerberus.mac-agent-hermes
tail -f ~/ClaudeWork/WeChat-agent-unigateway/data/mac-hermes.log
```

## 微信里怎么用

| 发什么 | 谁来做 |
|---|---|
| `你好` | studio 的 claude（默认 agent，行为没变） |
| `/mh 看一下这个报错` | Mac 上的 hermes |
| `/use mh` | 这个会话以后都走 Mac hermes |
| `/mc …` | Mac 的 claude（DeepSeek 供应商，多轮会话） |

## mac-claude 的认证方式

本机 `claude` CLI 本身**没登录**（`claude -p` 报 `Not logged in`，keychain 里也没有凭证；
桌面 App 用的是宿主注入的认证，不传给 CLI）。现在借 **CC Switch 里那条 DeepSeek 供应商**：

- 启动脚本 `scripts/run-mac-claude-client.sh` 每次启动时从 `~/.cc-switch/cc-switch.db`
  读 `app_type='claude-desktop' and name='DeepSeek'` 那条的 `ANTHROPIC_BASE_URL` /
  `ANTHROPIC_AUTH_TOKEN`，并设 `ANTHROPIC_MODEL=deepseek-chat`（DeepSeek 的 Anthropic 兼容端点
  只认它自己的模型名），**key 不落盘到任何脚本或 plist**。
- 用 `--session-command`（claude 的 stream-json 长会话），同一个微信会话里能续上下文，
  已实测两轮对话记得住上文。
- CC Switch 里换供应商或 key 之后，重启 LaunchAgent 即可生效：
  `launchctl kickstart -k gui/$(id -u)/com.cerberus.mac-agent-claude`

```bash
# 日志
tail -f ~/ClaudeWork/WeChat-agent-unigateway/data/mac-claude.log
```

## 回滚

```bash
launchctl bootout gui/$(id -u)/com.cerberus.mac-agent-hermes
ssh studio 'sudo cp ~/agent-gateway/config/gateway.json.bak-macjoin-<时间戳> \
                   ~/agent-gateway/config/gateway.json && sudo docker restart agent-gateway'
```

Mac 上残留的正向切换文件已经停用：`gateway.front-not-in-use.json`、`data/account.json.front-backup`
（那是「Mac 当前置网关」的实验，**别启动**，否则会和 studio 抢同一个微信身份）。

## 注意

- 一个微信 bot 身份只能有一个进程长轮询 —— 现在就是 studio，Mac 侧只连虚拟 iLink，不碰腾讯。
- studio 网关重启后，虚拟绑定从 `data/virtual/binds.json` 恢复，Mac 客户端的 token 继续有效；
  客户端若拿到 `ret=-14` 会自己重登，token 模式下无需人工。
- 客户端在 LaunchAgent 里跑，PATH 只有 plist 里写的那几段，**runner 里用绝对路径**。

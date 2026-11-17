# 统一记忆（使用说明）

设计见 [MEMORY-DESIGN.md](MEMORY-DESIGN.md)。这是 P1 的用法。

一句话：**记忆属于聊天，不属于设备或 agent。** 同一个联系人换一台设备、换一个
agent，读到的都是同一份记忆；每个联系人互相隔离，另有一层所有人可读的全局公共层。

## 打开

```json
"memory": {
  "enabled": true,
  "inject": {"enabled": true, "max_chars": 800, "recent_turns": 4, "facts": 6},
  "global_writers": ["claude@win59"]
}
```

重启网关生效。默认 `enabled: false`，关着的时候行为和以前完全一样。

## 它自动做什么

每条消息（命令和保活除外）发出前，网关会在前面拼一段不超过 800 字的摘要：

```
【记忆｜本聊天】
任务：把网关部署到新 VPS（doing，m-3a91）
最近：
- 用户：把 studio 和 win59 的网关都升级
- hermes@mac-air：已推送 4a2bf87
相关事实：
- 部署目录 /opt/wechat-gateway（m-1c02）
- [全局] 偏好中文回答（m-0f11）
【消息】继续
```

同时自动记录：

- **L0 对话**：用户消息和 agent 回复各一条，截断 500 字，默认 30 天过期；
- **L1 事实**：命中「记住 / 以后都 / 不要 / 必须 / 地址是 / 部署在 …」这类明确说法
  的回复自动记一条长期事实；
- **脱敏**：`sk-…`、`ghp_…`、`AKIA…`、`Bearer …`、私钥块会被替换成 `[REDACTED]`
  后才落盘。

## 微信里的命令

| 命令 | 作用 |
|---|---|
| `/remember 内容` | 记成长期事实（本聊天） |
| `/remember --global 内容` | 记进全局公共层，所有联系人都看得到 |
| `/task 内容` | 记一条进行中的任务 |
| `/task done` / `/task blocked` / `/task doing` | 改最近一条任务的状态 |
| `/recall 关键词` | 从本聊天 + 全局层里找 |
| `/forget m-1c02` 或 `/forget 关键词` | 删除 |
| `/memory` | 概览：各层多少条、任务状态 |
| `/memory reload` | 把 Markdown 里手改的内容导回来 |

这些命令都由网关直接回答，不会打扰 agent。

## 给 agent 的检索 API

用设备自己的虚拟 token 认证（`Authorization: Bearer <token>`）：

```bash
# 搜索（只返回该设备服务过的聊天 + 全局层）
curl -s http://127.0.0.1:18500/memory/search \
  -H "Authorization: Bearer $AGW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"peer":"o9cq…@im.wechat","q":"部署目录","k":6}'

# 写一条事实
curl -s http://127.0.0.1:18500/memory/write -H "Authorization: Bearer $AGW_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"peer":"o9cq…@im.wechat","text":"VPS 是 1.2.3.4","tags":["deploy"]}'

# 任务
curl -s http://127.0.0.1:18500/memory/task -H "Authorization: Bearer $AGW_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"peer":"o9cq…@im.wechat","text":"等 VPS 到位","status":"doing"}'

# 统计
curl -s "http://127.0.0.1:18500/memory/stats?peer=o9cq…@im.wechat" \
  -H "Authorization: Bearer $AGW_TOKEN"
```

- `peer` 必须在该设备服务过的聊天里（网关按它处理过的消息记着），否则只返回全局层；
- `scope:"global"` 的写入只有 `memory.global_writers` 里的设备可以做；
- 客户端每轮会把 `AGW_TOKEN` / `AGW_PEER` 放进环境变量（审批钩子已经在用），
  hook/tool 直接读即可。

## 文件在哪，能不能手改

```
data/memory/
├── memory.db                 # SQLite（0600），检索的真源
├── salt.txt                  # 联系人哈希用的盐（0600）
├── global/{facts,tasks,profile}.md
└── peers/<hash>/{facts,tasks,profile}.md
```

- `facts.md` / `tasks.md` 是网关生成的镜像，**可以在后面手加 `- 一行事实`**，
  下次 `/memory reload` 或网关重启时以 `user` 身份导入；
- `profile.md`（画像 L3）不会被网关覆盖，可以自由编辑，内容会参与注入；
- 想看原始对话（L0）：直接查 SQLite，或 `/recall`。

## 隐私与关闭

- 默认 30 天清掉 L0；`memory.l0_ttl_days` 可调，0 = 不过期。
- `/forget` 或直接删 `data/memory/` 目录即可清空。
- `memory.enabled=false` + 重启 = 完全回到旧行为；已写的数据不会自动删。
- `memory.redact_patterns` 可追加自定义正则（比如内部工单号）。

## P1 的边界

- 事实抽取是规则式的（明确说法才记）；LLM 抽取、embedding/RAG 在 P2。
- 检索用 FTS5（中文退回二元组 LIKE），没有向量检索。
- 没有 MCP 工具/控制台记忆面板（P2）；agent 目前通过 HTTP API 或自动注入使用。
- TencentDB-Agent-Memory 后端在 P3。

## 排错

- **消息里没有注入块**：`memory.enabled` 是否为 true、`inject.enabled`、
  `max_chars` 是否 > 0；第一条消息本来就没有可注入的内容。
- **`/remember` 说有记但 `/recall` 找不到**：关键词太新或太短；用 `/memory` 看条数，
  或直接看 `facts.md`。
- **agent API 返回 `peer is not served by this device`**：设备还没收到过这个聊天的
  消息（重启网关后 `_peer_bind` 只恢复已记录的对应关系），先让这个聊天发一条消息。
- **记忆出错会不会影响微信收发**：不会，所有记忆异常只记日志。

# 网关联邦：hub 转发给子网关，而不是每个 agent 跑 iLink 客户端

> 状态：设计稿（2026-11-05）。未实现。

## 为什么要这一层

现在的形态是「云电脑 hub + 每台机器每个 agent 一个 iLink 客户端」：

```
hub (云电脑, 持有微信身份)
 ├── iLink ← claude 客户端 (studio)
 ├── iLink ← hermes 客户端 (studio)
 ├── iLink ← claude 客户端 (win59)
 └── iLink ← mac-* 客户端 (Mac)
```

它解决了多实例粘住和故障转移，代价是每台机器都要常驻一个客户端进程，而且要跟着
hub 的协议版本走。

联邦把「一个 agent 一个客户端」换成「一台机器一个网关入口」：

```
hub (云电脑, 持有微信身份)
 ├── gateway → studio:18700   (studio 自己调用本地 a2a / exec)
 ├── gateway → win59:18700    (win59 自己调用本地 a2a)
 └── iLink   ← mac-*          (Mac 仍走 iLink，见"取舍")
```

子网关掌握自己的 agent 清单、会话和工作目录；hub 只负责把一条消息转过去、把
结果拿回来。

## 协议（v1，文本消息）

```
POST {sub}/federation/send
Authorization: Bearer <token>
Content-Type: application/json

{"agent": "claude", "text": "帮我看下这个报错", "peer": "<微信会话 id>",
 "context_id": "wx:<微信会话 id>", "source": "hub"}

200 {"reply": "……", "context_id": "wx:…", "elapsed_ms": 1234}
4xx/5xx {"error": {"code": "unknown_agent", "message": "…"}}
```

```
GET {sub}/federation/health
200 {"ok": true, "agents": ["claude", "hermes"], "version": "…"}
```

约定：

- 子网关只接受 `type ∈ {a2a, http, exec}` 的 agent；`virtual`（iLink）返回
  `409 virtual_agent_not_federated`，并提示"该 agent 需要用 iLink 客户端接入"。
- `peer` 由 hub 原样透传，子网关可据此做按聊天的会话粘住（a2a 的 `contextId`
  默认就用 `context_id`）。
- 第一版**同步**返回，`timeout` 由 hub 侧 agent 配置决定（默认 900s）。

## hub 侧改动

`agent_gateway/backends.py`：

```python
if agent.type == "gateway":
    return call_gateway(agent, text, context_id=context_id)
```

`call_gateway()` 复用现有的 `_post_json()`（同一个 urllib + 超时 + 错误翻译），
把 `agent.remote_agent` 填进请求体的 `agent` 字段，返回 `reply`。

配置：

```jsonc
"agents": {
  "studio-claude": {
    "type": "gateway",
    "label": "Studio Claude",
    "prefix": "sc",
    "url": "http://100.95.111.58:18700/federation/send",
    "token": "<与子网关一致的随机串>",
    "remote_agent": "claude",
    "timeout": 900
  }
}
```

`agent_gateway/config.py`：`AgentConfig` 增加 `remote_agent: str = ""`，
类型校验允许 `gateway`，并强制 `url` 与 `token` 非空。

## 子网关侧改动

新增 `agent_gateway/federation.py`，**独立进程**，读同一份 `gateway.json`：

```bash
python -m agent_gateway.federation --config gateway.json
```

- 用 `http.server.ThreadingHTTPServer`（和 `dashboard.py` 一个模式，纯标准库）。
- 只注册 `/federation/health` 与 `/federation/send` 两个路由。
- token 用 `hmac.compare_digest` 比较；没配 token 时拒绝启动（不提供"无鉴权"模式）。
- 调 agent 直接复用 `backends.call_agent()`，和主网关走同一套适配器。
- **不需要微信身份**，也不启动主网关的主循环——这是它能跑在 studio（网关已停、
  没有 bot 可登录）上的前提。

配置（子网关自己的 `gateway.json`）：

```jsonc
"federation": {
  "enabled": true,
  "host": "100.95.111.58",
  "port": 18700,
  "token": "<随机串>",
  "allowed_agents": ["claude", "hermes"]
}
```

`allowed_agents` 为空 = 该配置里所有非 virtual 的 agent 都可被调用。

## 部署

- **studio**：systemd 用户服务 `federation.service`，监听 tailnet 地址；
  本机 a2a 服务（8799 / 8800）保持 `127.0.0.1` 不变——联邦服务就是本机调用者，
  外部依赖只剩一个端口。
- **win59**：计划任务常驻（与 `Unigateway_Watchdog` 同一套路），监听
  `100.73.56.113`；Windows 防火墙只放行 tailnet 段。
- **hub（云电脑）**：`gateway.json` 增加 `gateway` 类型的 agent；先与现有 iLink
  并存灰度，跑通再删对应的 iLink 客户端与 systemd 单元。

## 取舍（要认下来）

| 维度 | iLink 客户端 | 联邦 |
|---|---|---|
| 依赖 | 每台每 agent 一个进程 | 每台一个进程 |
| 协议跟随 | 客户端要跟 hub 版本 | 只有 `/federation/*` 一个契约 |
| 多实例粘住 / 故障转移 | 原生支持 | **丢失**（hub 视角一 agent 一 URL） |
| `/new`、`/stop` | 落到 agent 自己 | 需子网关实现透传（v1 不支持） |
| 媒体（图片/文件） | 支持（走上传/下载） | v1 只传文本 |
| 鉴权 | token（每 agent 一个） | token（每子网关一个） |

结论：**联邦适合"这台机器上的 agent 就归这台机器管"，不适合"同一个 agent 在多台
机器上互为备份"。** 所以建议：

- `studio-claude`、`studio-hermes`、`win59-claude`：走联邦；
- `mac-claude`、`mac-hermes`：保留 iLink（Mac 上没有网关，只有客户端）；
- 如果之后还要跨机器的 `claude` 冗余，在 hub 层加"多后端 + 故障转移"（第二版），
  或者用一个逻辑 agent 多个 gateway 后端 + 优先级。

## 里程碑

- **M1**：`federation.py` 服务端 + `gateway` 后端 + token 鉴权 + 健康检查 + 单测
  （覆盖：无 token 拒绝、错 token 拒绝、未知 agent 404、virtual agent 409、正常转发）。
- **M2**：部署到 studio，hub 增加 `studio-claude`/`studio-hermes`，与 iLink 并存跑
  一周观察。
- **M3**：迁移 win59（`win59-claude`），评估 iLink 是否只留 Mac。
- **M4（可选）**：多设备故障转移、媒体转发、`/new` 透传。

## 安全

- 绑定 tailnet 地址（`100.64.0.0/10`），不监听 `0.0.0.0`，不开公网。
- token 必填，`hmac.compare_digest` 比较，日志里不出现明文。
- 子网关只暴露 `/federation/*`，其余路径 404。

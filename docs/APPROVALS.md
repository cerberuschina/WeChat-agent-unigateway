# 放行卡：让 agent 在手机上问你一句

无人值守的 agent 最怕的不是做错，是**卡在"需要你批准"上**：`claude -p` 没人可问，
于是要么给它全权，要么它一轮跑完什么都没干（观察过一次 41 轮 / $1.18 零产出的）。

放行卡把那个死路变成一个能回答的问题：

```
agent ──POST /agent/approval──▶ 网关 ──▶ 真微信
   ▲                              │        ⏸️ 「claude」要执行一条命令，等你一句话：
   │                              │        git status --short
   └──GET /agent/approval/<id>/wait◀┘        回 /approve k7 放行，/reject k7 拒绝。
                                            （180 秒不回就算拒绝。）
```

## 三条规矩

1. **沉默 = 拒绝。** 每张卡都有 TTL（`delivery.approval_ttl_seconds`，默认 180 秒），
   过期就是 `expired`，永远不等于"可以"。
2. **一卡一答。** 答复是终局：再回一次只会告诉你"已经答过了"，不会翻盘。
3. **没身份就问不动人。** 请求要带虚拟身份的 token；没有 token 的请求连消息都不发。

## 命令（用户侧，手打）

| 输入 | 意思 |
|------|------|
| `/approve <编号>` | 放行；编号可以省（= 最新那张卡） |
| `/reject <编号>` | 拒绝 |
| `/yes` `/ok` `/no` | 同上，图快 |
| `/help` | 列表里有这一行 |

答案会被回复一句回执（`✅ k7 已放行` / `⛔ k7 已拒绝` / `⌛️ k7 超时作废`），
所以"我到底点上了没有"不用猜。

## agent 侧（三行就够）

```python
from clients.ilink_agent_client import ask_approval
verdict = ask_approval(client, peer, "想跑 npm test", detail="npm test -- --runInBand")
if verdict["decision"] == "allow":
    ...   # 放行
```

`ask_approval` 内部就是那两个接口：POST 建卡（返回 `id`），再 GET `/wait` 挂着等。
任何 agent（不一定是 Claude）都能用——这是网关的能力，不是某个 agent 的。

## Claude Code 接进来：PreToolUse 钩子

`hooks/claude_approval_hook.py` 把"要执行命令"这一步变成一张放行卡。装在
`~/.claude/settings.json`：

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash|PowerShell",
       "hooks": [{"type": "command",
                  "command": "python K:/hermesWork/hooks/claude_approval_hook.py"}]}
    ]
  }
}
```

它靠客户端导出的环境变量找网关（`AGW_APPROVAL_URL` / `AGW_TOKEN` / `AGW_PEER`，
由 `export_approval_env` 在每条消息跑 agent 之前设好）：

* 有这三个变量 → 命令**先问人**，微信回了才跑；
* 没有（你自己在终端开的会话）→ **弃权**，交给普通的权限提示，绝不因为钩子坏掉而拦你；
* 够不到网关 / 等不到答复 / 自己崩了 → **拒绝**，并说明原因。钩子永远不会是
  "没批准就跑了"的原因。

只对命令类工具发问（默认 `Bash,PowerShell,shell,run_command`，可用 `AGW_ASK_TOOLS` 改）；
读取、编辑照旧走原有权限规则，不打扰。

## 接口

| 方法 | 路径 | 谁用 | 说明 |
|------|------|------|------|
| POST | `/agent/approval` | agent | `{peer,title,detail,kind,ttl}` → `{ret:0,id,ttl}`；`peer` 省略时问"最后跟你说话的那个人" |
| GET | `/agent/approval/<id>` | agent | 看这张卡的状态（`pending/allow/deny/expired`） |
| GET | `/agent/approval/<id>/wait?timeout=90` | agent | 挂着等答复（和 iLink 的长轮询一个习惯） |
| — | 你回的 `/approve` `/reject` | 人 | 走路由器 → 网关兑现 → 给 agent 答复 + 给你回执 |

控制台（`http://127.0.0.1:18600/api/state`）的 `gateway.approvals` 里能看到所有卡，
包括已经答过的那些——排障时先看这里。

## 配置

```json
{ "delivery": { "approval_ttl_seconds": 180 } }
```

## 测过什么

`tests/test_approvals.py`（18 项）＋ `tests/test_approval_hook.py`（6 项）：
纯 broker 逻辑（超时=拒绝、答复终局、按 peer 认领最新那张）、两个接口（问 / 等 /
无 token 被拒 / 关掉 approvals 时的干净拒绝）、路由器命令、网关接线（发卡 + 回执 +
没人可问时立刻拒绝不刷屏），以及钩子的六种处境（放行 / 拒绝 / 超时 / 不关它的事 /
没环境弃权 / 够不到网关）。

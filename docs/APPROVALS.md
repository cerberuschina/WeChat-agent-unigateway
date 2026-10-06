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

## 四条规矩

1. **沉默 = 拒绝。** 每张卡都有 TTL（`delivery.approval_ttl_seconds`，默认 180 秒），
   过期就是 `expired`，永远不等于"可以"。
2. **一卡一答。** 答复是终局：再回一次只会告诉你"已经答过了"，不会翻盘。
3. **没身份就问不动人。** 请求要带虚拟身份的 token；没有 token 的请求连消息都不发。
4. **放行是宽的那个方向，所以它必须有边。** 唯一一句"全放行"是 `/always`，
   它被两道边夹着：到点自动失效，且用户下一句话就把它收回。

## 命令（用户侧，手打）

| 输入 | 意思 |
|------|------|
| `/approve <编号>` | 放行；编号可以省（= 最新那张卡） |
| `/reject <编号>` | 拒绝 |
| `/yes` `/ok` `/no` | 同上，图快 |
| `/always` | **本轮全放行**：此刻挂着的全放行，本轮之后的新卡也不再问 |
| `/help` | 列表里有这一行 |

答案会被回复一句回执（`✅ k7 已放行` / `⛔ k7 已拒绝` / `⌛️ k7 超时作废`），
所以"我到底点上了没有"不用猜。

## `/always`：这一轮别再问我了

一轮活干到一半，agent 连着问五件事，逐条回 `/approve` 很烦。`/always` 一次说清：

```
/always  →  ✅ 这一轮全放行（一次放掉了 k7、2m4）。
            30 分钟内 agent 再要点头的事我直接放行，不再打断你；你下一句话一到就自动收回。
```

它管两件事：**此刻挂着的那几张卡**一起放行，**本轮之后的新卡**建的时候就放行——
后者连微信都不发（省额度，也正是"不再打断你"的意思）。

收口有三道：

* 用户**下一句话**（`/approve` `/reject` `/always` 本身不算）—— 一到就收回；
* `delivery.always_window_seconds`（默认 1800 秒）—— 到点自动失效；
* 只对**这个联系人**生效，不串到别人身上。

为什么不干脆做成"一直允许"：见规矩 1。`/always` 之所以可以，是因为它是**说出口的一句话**；
也正因为如此，它得像话一样会结束。每次自动放行都留着痕迹（`decided_by: always` +
`reason`），翻控制台的 `gateway.approvals` 就知道是谁、什么时候松的口。

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
{ "delivery": { "approval_ttl_seconds": 180, "always_window_seconds": 1800 } }
```

## 测过什么

`tests/test_approvals.py`（27 项）＋ `tests/test_approval_hook.py`（6 项）：
纯 broker 逻辑（超时=拒绝、答复终局、按 peer 认领最新那张、`/always` 的放行/过期/不串人/收回）、
两个接口（问 / 等 / 无 token 被拒 / 关掉 approvals 时的干净拒绝）、路由器命令、
网关接线（发卡 + 回执 + 没人可问时立刻拒绝不刷屏 + `/always` 之后不再发卡），
以及钩子的六种处境（放行 / 拒绝 / 超时 / 不关它的事 / 没环境弃权 / 够不到网关）。

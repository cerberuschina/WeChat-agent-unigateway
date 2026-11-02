# 统一记忆（Gateway Memory）设计

状态：设计稿，2026-11-02。目标分支 `multi_device`。尚未实现。

## 1. 为什么要它

网关现在是"消息路由器"：消息转给哪台设备、由哪个 agent 回答，都是无状态的。
于是同一个聊天里：

- 换一台设备（`hermes@mac-air` → `hermes@win59`）或换一个 agent（hermes → claude），
  对方不知道刚才聊了什么、做到哪一步；
- "上次说的那个部署地址"、"你之前定过：不要动 K 盘"这类长期事实，只能靠人再讲一遍；
- 任务中断（`/stop`、设备关机、agent 崩溃）以后没有地方记录停在哪。

统一记忆要做的，是让**记忆属于聊天，而不是属于某台设备或某个 agent**。微信身份
本来就只有一份在网关手里，记忆放在同一层是最自然的位置。

不变量：

1. 记忆按**联系人**隔离；另有一层所有联系人都能读的**全局公共层**（主人偏好、公共事实）。
2. 设备/agent 只是读写者：同一聊天换设备、换 agent，读到的是同一份记忆。
3. 网关是记忆的唯一写入者（自动记录），agent 可以主动写（工具/命令），人可以在
   Markdown 里直接改。
4. 默认零依赖（标准库 SQLite + Markdown）；高级检索可接 RAG；可整体换成
   腾讯开源的 TencentDB-Agent-Memory（见 §7）。

## 2. 记忆模型

借用 TencentDB-Agent-Memory 的分层命名，和现有生态对齐：

| 层 | 内容 | 写入者 | 例子 |
|---|---|---|---|
| L0 对话 | 原始的一来一回（截断/脱敏后） | 网关自动 | "用户：把网关发到 studio；Claude：已更新并重启" |
| L1 事实 | 原子事实、偏好、约束、决定 | 网关抽取 / agent / 人 | "部署目录 = /home/cerberusubuntu/agent-gateway" |
| L2 场景 | 某个项目/话题的归纳块 | agent / 人 / 定期归纳 | "多设备网关：正在做多实例路由，VPS 未到" |
| L3 画像 | 长期稳定的偏好与工作方式 | 人（Markdown）/ 定期归纳 | "偏好中文回答；动手前先 git fetch" |
| 任务 | 进行中的任务、步骤、状态 | agent 工具 / 人 | "状态=进行中，下一步=部署到新 VPS" |

每条记忆：

```
id            # 短 id（人可输入，如 m-7f3a）
scope         # peer:<sender-hash> | global
layer         # l0|l1|l2|l3|task
kind          # fact|preference|decision|event|task|summary
text          # 正文（≤ 1000 字；长内容截断并保留来源）
tags          # 自由标签 ["deploy","studio"]
source        # auto|agent|user|import
agent         # 哪个 agent 写的（可空）
instance      # 哪个设备实例写的（可空，如 mac-air）
created_at / updated_at / expires_at
supersedes    # 被这条事实推翻的旧 id（冲突处理）
fingerprint   # sha1(scope+normalized text)，去重用
```

## 3. 写入路径

### 3.1 自动记录（网关）

接点就是现有的 `Gateway._record("in"/"out", peer, agent, text)` 和 `_send()`：

- 每个**完整回合**结束时写一条 L0：用户消息 + agent 最终回复（各截断到 500 字；
  超过部分只留摘要标记）。命令类（`/approve`、`/stop`、保活）不记。
- L1 抽取默认用**规则**（零依赖）：命中"我叫/我偏好/以后/记住/地址/密码/路径/
  不要/必须"等模式，或显式 `/remember`；配置了 `memory.extractor.url` 时改走
  LLM（OpenAI 兼容 `/chat/completions`，例如 DeepSeek）抽取事实 JSON，失败回退规则。
- 任务进度：`/new`、`/stop`、客户端上报的"一轮结束"都可以更新 `task` 层；
  agent 通过工具显式更新优先。
- 写入前**脱敏**：`sk-…`、`ghp_…`、`AKIA…`、`Bearer …`、`-----BEGIN … KEY-----`、
  以及配置里 `memory.redact_patterns` 追加的规则；命中的内容替换为 `[REDACTED]`，
  原文不落盘。
- 保留期：L0 默认 30 天（`memory.l0_ttl_days`），L1/L2/L3 默认永久；
  `/forget` 和 `memory.retention` 可覆盖。

### 3.2 显式写入

微信命令（经 `router.py` 新增）：

| 命令 | 作用 |
|---|---|
| `/remember <内容>` | 写 L1；`/remember --global <内容>` 写全局层（需在 `memory.global_writers` 里） |
| `/task <内容>` / `/task done` | 写/关闭任务，记在 peer 作用域 |
| `/recall <关键词>` | 检索并回一段结果（不带 agent） |
| `/forget <id|关键词>` | 删除/失效，回复删了什么 |
| `/memory` | 记忆概览：事实条数、任务状态、最近 L2 |

agent 工具（HTTP API，见 §4.3）：

```
POST /memory/write   {text, layer?, tags?, scope?="peer", kind?, supersedes?}
POST /memory/task    {text, status:"doing|done|blocked", step?}
POST /memory/forget  {id? | query?}
```

### 3.3 人改 Markdown

`data/memory/peers/<sender-hash>/profile.md`（L3）和 `facts.md`（L1）、
`tasks.md`（任务）是 SQLite 的**镜像**：

- 网关每次写入后按防抖（2 s）重写对应 Markdown；文件头带 `<!-- hash: … -->`。
- 人改了 Markdown 且 hash 不符：启动时（以及 `/memory reload`）导入，按行号/短 id
  合并；冲突以 Markdown 为准并记一条日志（人改的优先）。
- 全局层在 `data/memory/global/`，同样结构。
- L0 不镜像（量大、隐私高），只保留在 SQLite，`/export` 可导出。

## 4. 读取路径

### 4.1 自动注入（每条消息）

分发前（`_dispatch_virtual`、以及 `backends.call_agent` 三条直连路径）构造一段
**Memory Brief**，预算默认 800 字符（`memory.inject.max_chars`），拼在消息前面：

```
【记忆｜本聊天】
任务：正在把网关部署到新 VPS；下一步=写 compose（m-3a91）
最近：用户要把 studio 和 win59 的网关都升级；已推送 4a2bf87（10 分钟前）
相关事实：
- 部署目录 /opt/wechat-gateway（全局，m-1c02）
- 偏好中文回答、动手前先 git fetch（全局，m-0f11）
【消息】<原始内容>
```

- 命令、保活、审批类消息**不注入**（省 token，也不污染会话）。
- 检索：本聊天最近 N 条 L0 摘要 + 按消息关键词检索 L1/L2 + 全局 L3 固定几条；
  按"相关度 × 新近度"排序后在预算内截断。
- 同一轮多次发送只注入一次；客户端会把注入块当作普通上下文。
- 注入块带 `【记忆】` 前缀，网关在记录 L0 时会剥离，避免记忆自我复制。

### 4.2 检索 API（给 agent）

```
GET  /memory/search?q=…&scope=peer|global|all&k=8&budget=1200
     → {items:[{id,layer,text,tags,score,updated_at}], brief?}
GET  /memory/stats
```

认证复用虚拟 token（`Authorization: Bearer <token>`）；`peer` 参数必填，
服务端校验它在该 token 绑定实例**服务过的聊天**里（`VirtualBind.last_peer` 与
`_peer_bind` 的历史集合），否则只返回全局层——防止一个联系人的设备读别人。
`scope=global` 只读；写全局需额外权限（见 §5）。

### 4.3 agent 接入

- **MCP（推荐）**：仓库提供 `scripts/memory_mcp.py`，stdio MCP server，暴露
  `memory_search`、`memory_write`、`memory_task`、`memory_forget`；从
  `AGW_*` 环境变量取地址/token/peer（客户端每轮已 `export_approval_env`，再补
  `AGW_MEMORY_URL`）。Claude Code 用 `.mcp.json` 挂上；Hermes 按其 tool 配置挂；
  WorkBuddy 走 HTTP 工具配置。
- **HTTP**：没有 MCP 的 agent 直接调 §4.2/§3.2 的接口。
- **零改动 agent**：只靠 §4.1 自动注入也能工作，只是不能主动检索/记事实。

## 5. 隔离与权限

| 角色 | 能读 | 能写 |
|---|---|---|
| 某聊天的设备（token） | 该聊天桶 + 全局层 | 该聊天桶；任务 |
| 全局写手（`memory.global_writers` 里的设备名） | 全部 | 全局层 |
| WeChat 用户 `/remember --global` | 其聊天桶 + 全局 | 全局（受 `access.allowed_users` 约束） |
| 人（SSH/控制台） | 全部 | 全部（直接改 Markdown / SQLite） |

- 联系人 → `peer_id` 用 `sha1(sender + memory.salt)[:16]`；日志和文件名不出现微信号。
- 设备信任模型与现有网关一致：合法 token 即可信；跨联系人的读被 §4.2 的 peer
  校验收敛到"自己服务过的聊天"。
- 全局层写入要显式（命令带 `--global` 或专门设备），避免某个 agent 的猜测污染
  所有人看得到的偏好。
- 记忆投毒/过期：`supersedes` + `expires_at` + `/forget`；L1 抽取默认只接受
  "陈述句 + 命中规则"的内容，agent 主动写入需带 `source=agent` 便于审计。

## 6. 存储

### 6.1 内置后端（默认，零依赖）

`data/memory/memory.db`（SQLite，WAL，0600）：

```sql
CREATE TABLE items(
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, layer TEXT NOT NULL,
  kind TEXT, text TEXT NOT NULL, tags TEXT, source TEXT,
  agent TEXT, instance TEXT, fingerprint TEXT,
  created_at REAL, updated_at REAL, expires_at REAL, supersedes TEXT);
CREATE INDEX items_scope ON items(scope, layer);
CREATE UNIQUE INDEX items_fp ON items(scope, fingerprint);
CREATE VIRTUAL TABLE items_fts USING fts5(text, tags, content='items', content_rowid='rowid');
```

- **RAG**：第一阶段用 FTS5/BM25；`memory.rag.enabled` 打开后，写入时调用配置的
  OpenAI 兼容 `/embeddings`（URL/key/model 可配，例如 DeepSeek 或本机 Ollama），
  向量存 `items_vec`（BLOB）；检索 BM25 与向量各取 top-k，用 RRF（k=60）融合，
  再加新近度权重。没有配置 embedding 时自动退回纯 FTS，不发生失败。
- Markdown 镜像见 §3.3；`/export` 生成一份可读的整包。

### 6.2 后端抽象

```python
class MemoryBackend(Protocol):
    def write(self, item) -> Item: ...
    def search(self, query, *, scopes, layers, k, budget) -> list[Item]: ...
    def forget(self, *, id=None, query=None, scope) -> int: ...
    def stats(self) -> dict: ...
```

`BuiltinBackend`（§6.1）与 `TencentDBBackend`（§7）两个实现；`memory.backend`
选一个。Markdown 镜像、注入、权限都在网关层，换后端不影响它们。

## 7. 接 TencentDB-Agent-Memory（可选后端）

选择它而不是同门的另外两个：

- **TencentDB-Agent-Memory**（MIT，Node ≥ 22，Docker）：L0-L3 分层、BM25+向量+RRF、
  `memory-proxy` 支持把 Claude Code / Codex / CodeBuddy / WorkBuddy / Hermes /
  OpenClaw 的 base URL 指过去自动注入，`/v3/tools/list|call` 给 agent 工具，
  hub 有 owner/visibility/version/ACL。和本设计的分层、隔离、注入方式一一对应。
- **Hy Memory 2.0**：以专用模型（30B-A3B）为核心，需要额外推理资源，适合作为
  L1 抽取器而不是网关级存储。
- **WeKnora**：文档 RAG 平台，偏知识库，不是"聊天记忆"。

接入方式（`memory.backend="tencentdb"`）：

```json
"memory": {
  "backend": "tencentdb",
  "tencentdb": {
    "base_url": "http://127.0.0.1:8125",
    "token": "file:/app/data/memory-token.txt",
    "model_proxy": "http://127.0.0.1:8126",
    "team": "my-gateway"
  }
}
```

| 本设计 | TencentDB 里 |
|---|---|
| `scope=peer:<hash>` | 每个联系人一个 owner/workspace，visibility=private |
| `scope=global` | team 可见的共享资产（visibility=team） |
| L0/L1/L2/L3/task | 对应 Conversation/Atom/Scenario/Persona + Skill/Task 块 |
| 自动注入 | 网关侧仍自己注入（策略可控），或把 agent 的模型 base URL 指到 memory-proxy（多一层自动注入，二选一） |
| Markdown 镜像 | 继续由网关导出（TencentDB 不保证人可改） |

部署：在同一台云主机加一个 compose 服务（Node 镜像），`start-all.sh` 或官方
Docker 方式起 core/hub/proxy；数据卷持久化；只监听 127.0.0.1/tailnet。
适配器只依赖它的 HTTP API，失败时**降级到 builtin 并告警**，消息链路不断。

## 8. 配置（新增 `memory` 段）

```json
"memory": {
  "enabled": true,
  "backend": "builtin",
  "data_dir": "/app/data/memory",
  "salt": "file:/app/data/memory-salt.txt",
  "l0_ttl_days": 30,
  "inject": {"enabled": true, "max_chars": 800, "recent_turns": 4, "facts": 6},
  "extractor": {"url": "", "token": "", "model": ""},
  "rag": {"enabled": false, "embeddings_url": "", "embeddings_model": "", "rrf_k": 60},
  "global_writers": ["claude@win59"],
  "redact_patterns": []
}
```

- `enabled=false` 时完全是旧行为（方便回滚）。
- `salt` 不存在时首次启动生成并 0600 写入；换 salt 等于换一套匿名 id。
- 失败策略：记忆任何环节异常只记日志，**绝不阻断消息收发**（同现有媒体/审批的
  原则）。

## 9. 代码接点

| 文件 | 改动 |
|---|---|
| `agent_gateway/memory.py`（新） | 数据模型、BuiltinBackend、Markdown 镜像、Brief 构造、脱敏、抽取 |
| `agent_gateway/memory_tencentdb.py`（新，P3） | HTTP 适配器，实现 MemoryBackend |
| `agent_gateway/config.py` | `MemoryConfig` + 解析/校验 |
| `agent_gateway/gateway.py` | `self.memory` 初始化；`_record`/`_send` 挂自动记录；`_dispatch_virtual` 与 `backends.call_agent` 前注入 Brief；`/stop`、`/new` 更新任务 |
| `agent_gateway/router.py` | `/remember` `/recall` `/forget` `/memory` `/task`，`help_text` 同步 |
| `agent_gateway/virtual_ilink.py` | `/memory/search|write|task|forget|stats` 路由；token→bind 的 peer 校验 |
| `clients/ilink_agent_client.py` | `export_approval_env` 补 `AGW_MEMORY_URL/AGW_MEMORY_TOKEN` |
| `scripts/memory_mcp.py`（新） | stdio MCP server：search/write/task/forget |
| `agent_gateway/dashboard.py` | 记忆面板：往来记忆数、任务、全局事实（只读 + reload） |
| `docs/MEMORY.md` | 使用手册（本文件是设计稿，实现后拆分） |

## 10. 分阶段

**P1（内置，零依赖，先可用）**
自动 L0 记录与脱敏、规则 L1 抽取、Markdown 镜像、Brief 自动注入、`/remember`
`/recall` `/forget` `/memory` `/task`、HTTP `/memory/*`、peer/global 隔离、测试。

**P2（RAG + 工具）**
FTS5 混合 embedding/RRF、`scripts/memory_mcp.py`、控制台记忆面板、L2 定期归纳
（同一聊天的 L0 摘要压缩）、导出/导入。

**P3（TencentDB-Agent-Memory 后端）**
适配器 + compose 服务 + 降级策略 + 迁移工具（builtin → tencentdb）。

每阶段都保持 `memory.enabled=false` 时行为与现在完全一致。

## 11. 验证

- 单元：脱敏、指纹去重、supersedes、FTS 检索、RRF 融合、Markdown 往返、
  peer 越权读被拒、全局写权限。
- 集成：同一聊天在 `hermes@mac-air` 回答后在 `claude@win59` 追问，注入块里能
  看到上一轮摘要；`/remember` 后换 agent 仍能召回；L0 过期后不再注入。
- 故障：记忆库损坏/后端宕机/embedding 超时 → 消息照常收发，日志告警。
- 真实部署：云主机 compose 起 TencentDB，跑 P3 冒烟：写一条 / 读一条 / 停服务
  看降级。

## 12. 风险与取舍

| 风险 | 处理 |
|---|---|
| Token 开销（每条消息多 800 字） | 预算可调；命令/保活不注入；Brief 只有命中项 |
| 记忆污染（agent 乱写） | 全局层写权限收敛；`source=agent` 可审计；`supersedes`/`/forget` |
| 隐私（微信内容落盘） | L0 保留期、脱敏、peer 哈希、SQLite/Markdown 0600、`/forget` |
| 记忆自我强化（把注入内容又记一遍） | 写 L0 时剥离 `【记忆】` 块；fingerprint 去重 |
| 换后端语义漂移 | 网关持有注入/隔离/镜像，后端只做存取；契约测试跑两套实现 |
| 设备信任 | 与现状一致：设备互信；跨联系人读靠 peer 校验。多租户不做 |

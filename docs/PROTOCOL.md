# iLink Bot API — 实测要点

本文件记录 `agent_gateway/ilink.py` 依赖的**线上事实**。它不是官方文档，
而是把"能跑通的实现"里用到的部分抽出来，方便后来人核对与排错。
如果你发现某条与官方行为不一致，以实测为准并在这里更正。

- Base URL：`https://ilinkai.weixin.qq.com`
- 所有业务接口：`POST /ilink/bot/<action>`（QR 那两个是 GET）
- 返回：JSON，**HTTP 200 也可能带业务错误**，必须看 `ret` / `errcode`

## 请求头

```
Content-Type: application/json
AuthorizationType: ilink_bot_token
Authorization: Bearer <bot_token>          # 登录后才有
X-WECHAT-UIN: <base64(随机 32 位整数的十进制串)>   # 每次请求新生成
iLink-App-Id: bot
iLink-App-ClientVersion: 131584            # (2<<16)|(2<<8)
Content-Length: <body bytes>
```

body 除了业务字段，还要带 `base_info.channel_version`（当前 `"2.2.0"`）。

## 端点

| action | 作用 | 关键字段 |
|---|---|---|
| `ilink/bot/get_bot_qrcode?bot_type=3` | 取登录二维码（GET，无需 token） | 返回 `qrcode`、`qrcode_img_content` |
| `ilink/bot/get_qrcode_status?qrcode=<值>` | 轮询扫码状态（GET） | `status`：`wait` / `scaned` / `scaned_but_redirect` / `expired` / `confirmed` |
| `ilink/bot/getupdates` | **长轮询**收消息（≈35s 挂起） | 请求 `{get_updates_buf}` → 返回 `{msgs, get_updates_buf}` |
| `ilink/bot/sendmessage` | 发消息 | `{msg: {...}}` |
| `ilink/bot/sendtyping` | "正在输入" | `{ilink_user_id, typing_ticket, state}`（1 开始 / 2 结束） |
| `ilink/bot/getconfig` | 取会话配置（含 `typing_ticket`） | `{ilink_user_id, context_token?}` |

扫码确认后返回的是**长期凭证**：

```json
{ "ilink_bot_id": "xxxxxxxx@im.bot", "bot_token": "…",
  "baseurl": "https://…", "ilink_user_id": "…" }
```

`scaned_but_redirect` 时要用返回的 `redirect_host` 换 base url 继续轮询 —— 这是分线路由，不换会一直 `wait`。

## 收消息

```jsonc
// 请求
{ "get_updates_buf": "<上次的游标，空串第一次>", "base_info": {...} }

// 返回（节选）
{ "ret": 0,
  "get_updates_buf": "<新游标>",
  "msgs": [ {
      "from_user_id": "…",           // 谁发的（= 回消息时的 to）
      "to_user_id": "…",
      "message_id": "…",             // 去重用
      "context_token": "…",          // ★ 回消息必须带上它
      "item_list": [ { "type": 1, "text_item": { "text": "你好" } } ]
  } ] }
```

- **游标**：`get_updates_buf` 当成不透明偏移量，原样回传；要落盘，否则重启会重放或丢消息。
- **`context_token`**：每次收到消息都带，**发消息时必须回传同一个**，否则回复绑不到那个会话。
  要按 peer 持久化（本项目存在 `data/ilink/context-tokens.json`）。
- **`item_list` 类型**：`1` 文本、`2` 图片、`3` 语音、`4` 文件、`5` 视频。
  本项目只处理 `1`；其余类型目前只回一句"只认文字"。
- 会收到自己发的消息（`from_user_id == 自己的 account_id`），要跳过。

## 发消息

```jsonc
{ "msg": {
    "from_user_id": "",              // bot 身份留空
    "to_user_id": "<对方 from_user_id>",
    "client_id": "<自己生成的去重 id>",
    "message_type": 2,               // 2 = bot 消息
    "message_state": 2,              // 2 = 完整消息
    "context_token": "<收到的那一个>",
    "item_list": [ { "type": 1, "text_item": { "text": "回复内容" } } ]
} }
```

## 错误码

| ret / errcode | 含义 | 怎么办 |
|---|---|---|
| `0` | 正常 | —— |
| `-14` | 会话失效 / 未登录 | **重新扫码**（本项目直接退出并提示） |
| `-2` | iLink 频率限制（也可能伪装成过期会话） | 退避重试，不要立刻重登 |
| 其他非 0 | 业务错误 | 看 `errmsg`；保留原始返回便于排查 |

注意：`getupdates` 长轮询到点是**正常返回**（空 `msgs`），不是错误，别当异常处理。

## 媒体（尚未接入本项目）

外向：`ilink/bot/getuploadurl` 取上传地址 → 上传 AES-128 密文 → 拿到
`encrypt_query_param` → 组装 `item_list` 发送。
内向：item 里带 `media.encrypt_query_param` / `aes_key`，从
`https://novac2c.cdn.weixin.qq.com/c2c/download?...` 下载后本地解密。

安全提示：下载地址要做**域名白名单**校验（只允许微信 CDN 域），否则就是一个 SSRF 口子。

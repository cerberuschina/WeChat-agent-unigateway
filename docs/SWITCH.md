# 切换到新网关 —— 下午回家照着做（含回滚）

> 目标：微信上**只留一个入口**，后面是 hermes / claude / workbuddy。
> 原则：**先跑通，再切换**；任何一步不对都能一分钟退回原状。

## 0. 现在的状态

- 你现在的微信通道是 **Hermes 自己的 weixin 渠道**（就是正在跟你说话的这个），它**正在轮询**那个 bot 身份。
- 网关一旦启动并绑定同一个身份，**两边会抢消息**。所以顺序必须是：先停 Hermes 的 weixin，再起网关。
- 网关代码在 `K:\hermesWork`，未接管任何东西。

## 1. 先跑通（不碰微信，5 分钟）

```bash
cd K:\hermesWork
python -m unittest discover -s tests -t .        # 应当全绿
copy gateway.example.json gateway.json           # 然后改 agents 那段
python -m agent_gateway --config gateway.json --dry-run --once
```

`--dry-run` 不发任何 iLink 请求，只把路由结果打出来。这一步用来确认配置读得对。

## 2. 绑定微信（扫码）

```bash
python login.py                # 打印二维码 URL 与 qrcode 值
```

- 微信里**必须装好 ClawBot 插件**；扫码的是**要拿来当唯一入口的那个号**。
- 想不打扰现有的：先用另一个号扫，跑通了再决定要不要换。
- 凭证写到 `data/account.json`（权限 600，`.gitignore` 已挡）。**换绑就是删掉它重扫。**

## 3. 切入口（关键一步）

1. **停掉 Hermes 的 weixin 渠道**（不然两边抢消息）。做完这一步，微信上暂时联系不到 Hermes —— 这是预期的。
2. 起网关：
   ```bash
   python -m agent_gateway --config gateway.json
   ```
   看到 `gateway up: account=… agents=…` 就成了。
3. 微信里对这个联系人说话：
   - `你好` → 默认 agent（hermes）
   - `/c 帮我看看这个报错` → claude
   - `/use claude` → 以后都走 claude
   - `/agents` `/who` `/help`

## 4. 忘了 / 不对了 —— 回滚

```bash
# 停网关：Ctrl+C（或关掉那个窗口）
# 重新打开 Hermes 的 weixin 渠道（把它启回去即可）
```

网关是**独立进程、独立目录、独立凭证文件**，停掉它不会影响 Hermes 的任何状态；
Hermes 的 weixin 渠道启回去，一切跟今天一模一样。

## 5. 这一轮的已知边界

| 事项 | 现状 |
|---|---|
| 纯文字 | ✅ |
| 图片 / 语音 / 文件 | ❌ 还没接（`docs/PROTOCOL.md` 里写了链路，roadmap 上） |
| 群聊 | ❌ iLink 的 bot 身份一般收不到群消息 |
| claude / hermes 后端 | ✅ 现成的 A2A 端点（8799 / 8800），需要 a2a-bridge 在跑 |
| workbuddy 后端 | ⚠️ 见 `docs/BACKENDS-WORKBUDDY.md`：它没有可用本地 API，走 CLI 或 CDP 桥 |
| 开机自启 | ❌ 还没写（roadmap） |

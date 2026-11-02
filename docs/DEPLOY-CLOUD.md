# 云主机 + 多设备：一台网关，多台机器上的 agent

目标：**微信身份只在一台云主机上**（一个 bot 只能有一个进程长轮询），Mac、
Windows 桌面、笔记本各自跑自己的 `hermes` / `claude` / `workbuddy` 实例，通过
Tailscale、WireGuard 或 SSH 隧道接进来；三条路同时配好，哪条通用哪条。

```
        微信 ──(出站长轮询，无需入站端口)──►  云主机 agent-gateway
                                                  │ 虚拟 iLink :18500
                          ┌───────────────────────┼───────────────────────┐
                    Tailscale 100.64/10      WireGuard 10.0.0.0/8      SSH 隧道 127.0.0.1
                          │                       │                       │
                mac-air: hermes@mac-air   win59: claude@win59     任意设备: workbuddy@…
```

同一个 agent 可以在任意多台设备上各跑一个实例（`hermes@mac-air`、
`hermes@win59`），网关按聊天粘住一台，掉线时转到另一台。

---

## 1. 云主机准备

- Linux（Ubuntu/Debian 即可），装 Docker + compose 插件；项目本身零依赖。
- 目录布局（和容器内路径一致）：

```
/opt/wechat-gateway/
├── docker-compose.cloud.yml     # 仓库里这份
├── Dockerfile
├── config/gateway.json          # 从 gateway.cloud.example.json 复制后修改
└── data/                        # account.json、virtual/binds.json、token 文件、媒体
```

- 出站要能到 `ilinkai.weixin.qq.com` 和 `novac2c.cdn.weixin.qq.com`；**入站不需要
  对公网开任何端口**。
- 防火墙只放设备到 18500 的路（host 网络下由宿主机防火墙把关）：

```bash
sudo ufw allow 10701/tcp                                # ssh，按你自己的端口改
sudo ufw allow in on tailscale0 to any port 18500 proto tcp
sudo ufw allow in on wg0        to any port 18500 proto tcp
sudo ufw deny 18500/tcp                                 # 公网网卡一律拒绝
sudo ufw deny 18600/tcp                                 # 控制台只走本机/隧道
```

> 注意：容器用 `network_mode: host`，端口就是宿主机的端口，ufw 规则直接有效；
> 换成 bridge + `ports:` 的话 Docker 会绕过 ufw（走 DOCKER 链），要自己另加规则。

## 2. 三条线路（可以同时配）

### Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --accept-dns=false --hostname=wx-gateway   # 不动 DNS/默认路由
tailscale ip -4                                              # 例如 100.95.111.58
```

设备访问 `http://100.95.111.58:18500`。tailnet ACL 里把设备限到该端口。

### WireGuard（自建，兜底）

```ini
# /etc/wireguard/wg0.conf（云主机）
[Interface]
Address = 10.8.0.1/24
ListenPort = 51820
PrivateKey = ...

[Peer]                      # mac-air
PublicKey = ...
AllowedIPs = 10.8.0.2/32

[Peer]                      # win59
PublicKey = ...
AllowedIPs = 10.8.0.3/32
```

设备访问 `http://10.8.0.1:18500`。`AllowedIPs` 只路由内网段，不要抢占设备默认路由。

### SSH 隧道（不开 VPN 时的最后一招）

```bash
ssh -N -L 18500:127.0.0.1:18500 ubuntu@wx-gateway
# 设备上客户端用 http://127.0.0.1:18500
```

隧道本身是加密的；缺点是断线要重连。**媒体交换**注意：走 `127.0.0.1` 时客户端
会以为网关在同一台机器上（共享路径），图片/文件会失效——需要媒体就优先用
VPN 地址或对网关用主机名，别用回环地址。

## 3. 配置与首个微信登录

```bash
cp gateway.cloud.example.json config/gateway.json
# 改这几处：account（留空，后面扫码写入 data/account.json）、
#           virtual.public_url（设备实际用的地址）、
#           virtual.allow_cidrs（Tailscale 100.64.0.0/10 / WireGuard 10.0.0.0/8）

sudo docker compose -f docker-compose.cloud.yml up -d --build
sudo docker logs -f agent-gateway          # 启动后提示"还没有绑定微信"
sudo docker compose -f docker-compose.cloud.yml run --rm gateway \
     python login.py --png /app/data/qr.png
```

把 `data/qr.png` 拿出来扫码（`sudo docker cp` 或 `docker compose cp`）。凭据写进
`data/account.json`，容器重启后直接沿用。**同一时间只能有一个进程持有这个身份**：
旧机器上的网关必须先停掉，否则两边互相抢长轮询。

## 4. 给每台设备签发 token

```bash
# 在宿主机上跑（token 文件进 ./data，配置里写容器内的 /app/data/）
python3 scripts/mint_device_token.py hermes mac-air --config config/gateway.json \
        --data-dir data --token-prefix /app/data/
python3 scripts/mint_device_token.py claude win59  --config config/gateway.json \
        --data-dir data --token-prefix /app/data/
# 撤销：
python3 scripts/mint_device_token.py claude win59 --remove --config config/gateway.json \
        --data-dir data
sudo docker compose -f docker-compose.cloud.yml restart gateway
```

它写 `data/<agent>-<device>-token.txt`（0600），并把
`virtual.accept_tokens["<agent>@<device>"]` 指向它。同一个 agent 想多接几台设备，
就多执行几次、设备名不同即可。

## 5. 设备上的客户端

```bash
python clients/ilink_agent_client.py \
  --name hermes --device mac-air \
  --base-url "http://100.95.111.58:18500,http://10.8.0.1:18500,http://127.0.0.1:18500" \
  --creds data/virtual-client-hermes-mac-air.json \
  --reuse-token "$(cat data/hermes-mac-air-token.txt)" \
  --runner "hermes -m deepseek-v4-pro -z {text}"
```

- `--base-url` 可以给一串（逗号分隔）：客户端先探 TCP，用第一条通的；连续两次
  失败会自动换下一条，并把有效地址写回 creds，下次启动优先用它。
  环境变量 `ILINK_GATEWAY_URL` 也可以给这串地址。
- `--device` 只影响默认 creds 文件名和日志；**身份由 token 决定**（网关侧
  `agent@device`）。同一台机器跑两个同类实例时，务必给不同的 `--device` 和 token。
- 常驻用 systemd（Linux）：

```ini
# /etc/systemd/system/wx-agent@.service
[Unit]
Description=WeChat agent client (%i)
After=network-online.target

[Service]
User=agent
WorkingDirectory=/opt/wx-agent
ExecStart=/usr/bin/python3 clients/ilink_agent_client.py --name hermes --device %i \
  --base-url "http://100.95.111.58:18500,http://10.8.0.1:18500,http://127.0.0.1:18500" \
  --reuse-token ${TOKEN} --runner "hermes -m deepseek-v4-pro -z {text}"
Restart=always
RestartSec=5
EnvironmentFile=/etc/wx-agent/%i.env

[Install]
WantedBy=multi-user.target
```

Windows 桌面用计划任务 + `wscript` 隐藏启动（参考仓库 `watchdog/` 里的写法），
macOS 用 LaunchAgent。

## 6. 同一个 agent 多实例时，消息怎么走

- **粘住**：哪台设备回了这条聊天，下一条还投给它（记录在 `data/virtual/binds.json`
  的 `peers` 里，网关重启也保留）。
- **故障转移**：粘住的那台不在线（90 秒内没长轮询）就投给在线的另一台，日志里
  记一条 `failing over to …`。
- **全都离线**：消息排进那台记住的设备队列，它连回来立刻收到；确实一台都没接过
  入的 agent 会直接回用户"还没接入"。
- 控制台 `agents[].instances` 里每台设备一行（在线/离线、队列、最后活动）。
- 路由前缀（`/h`、`/c`、`/w`）仍按 agent 名字选**类型**，实例在类型内部自动选；
  `/use hermes` 换的是类型，不是某台设备。

## 7. 安全边界

- 传输加密交给 Tailscale / WireGuard / SSH；网关本身只讲 HTTP（故意没有内建 TLS）。
  不要在没有 VPN 的情况下把 18500 暴露到公网。
- 每个设备一个 token；`virtual.accept_tokens` 就是设备清单，删一行 + 重启 = 撤销。
- `admin_cidrs` 保持 loopback：批准、`/admin/binds`、`/health`、控制台只在本机。
  看控制台：`ssh -L 18600:127.0.0.1:18600 ubuntu@wx-gateway`。
- `allow_cidrs` 只写 VPN 网段（和本机回环）；改了记得把 `127.0.0.1/32`、`::1/128`
  留在里面，否则本机自己也会被拒。
- `data/virtual/binds.json` 从 2026-11-02 起写成 0600（里面是全部虚拟 token）。
- 信任模型：连进同一网关的设备**默认互信**（审批接口、媒体接口只校验"是合法
  token"，不区分是哪台设备）。只给自己的设备发 token，不要做多租户。

## 8. 启动后自查

```bash
sudo docker ps                                    # Up (healthy)
sudo docker logs agent-gateway | tail -20         # 无持续 ret=-1/-14；getupdates 正常
curl -s http://127.0.0.1:18600/api/state | python3 -m json.tool | head -40
# 设备侧：客户端日志出现 "服务端 http://…（备用 N 条线路）" 并持续长轮询
```

在微信里发一句「保活」，任何一个在线实例都可以先用网关直接答（`/h` 等前缀
路由到具体 agent）。发一条真正的任务，确认控制台 `traffic` 里出现
`微信 → <agent>`，并且是预期的那台设备（`instances` 的队列/最后活动变化）。

## 9. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 网关日志 `getupdates: ret=-14` 后退出（码 3） | 微信身份失效 | 重新 `login.py` 扫码 |
| `getupdates: ret=-1` 连续出现 | 游标不属于这个身份（例如从别的部署带过来的 `vcur-*`） | 停容器，清空 `data/ilink/get_updates_buf.json` 为 `{"buf": ""}`，再启动 |
| 客户端连不上、报 502 | 系统代理拦截了 tailnet/wg 地址 | 已内置：`100.64/10`、`10/8` 等私有地址不走代理 |
| 图片/文件双向往返失败 | 客户端用 `127.0.0.1` 走隧道，被判成"同机" | 改用 VPN 地址或主机名 |
| 控制台打不开 | 只绑了 127.0.0.1 | `ssh -L 18600:127.0.0.1:18600` |
| 重启后约 1 分钟才绑上端口 | 旧版本没有 SO_REUSEADDR（2026-11-01 已修） | 升级后只会发生在从旧版升级的那一次 |

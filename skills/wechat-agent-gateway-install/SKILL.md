---
name: wechat-agent-gateway-install
description: Download and install the open-source WeChat agent gateway (cerberuschina/WeChat-agent-unigateway) from GitHub, verify it with an offline smoke test, bind a WeChat bot by QR, connect a local CLI agent (Claude Code, Hermes, Codex, or any command), run it, and debug the first message end to end. Use when the user wants an AI agent reachable from WeChat, wants to self-host a WeChat bot gateway, or is troubleshooting a gateway that receives nothing.
license: MIT
---

# WeChat agent gateway — install & verify

You are installing **WeChat-agent-unigateway**: a small, dependency-free Python
gateway that owns one WeChat bot identity and routes each incoming chat message to
one of the user's local agents (a CLI, an A2A/HTTP service, or a "virtual iLink"
client). It is plain stdlib — no `pip install` is required.

- Repo: `https://github.com/cerberuschina/WeChat-agent-unigateway`
- Runtime: Python **3.9+**, git. Works on macOS, Linux, Windows.
- Default ports: `18500` (virtual iLink / agent API), `18600` (console).

Work through the steps in order. Do not skip the offline smoke test: it proves the
checkout runs *before* anything touches WeChat.

## 0. Ask first (two questions)

1. **Where should it live?** Suggest `~/WeChat-agent-unigateway` (macOS/Linux) or
   `%USERPROFILE%\WeChat-agent-unigateway`.
2. **Which agent answers the messages?** Get an exact command:
   - Claude Code: `claude -p {text}` (or a `--session-command` stream-json line)
   - Hermes: `hermes -m <model> -z {text}`
   - Anything else: the command that takes the message on stdin or as `{text}`.

Also confirm the user has a **WeChat bot identity that can be bound through iLink**
(the README refers to the ClawBot plugin in WeChat). Without that, everything can
be installed and verified offline, but no message will flow.

## 1. Clone and verify the checkout

```bash
git clone --depth 1 https://github.com/cerberuschina/WeChat-agent-unigateway.git ~/WeChat-agent-unigateway
cd ~/WeChat-agent-unigateway
```

If git/network is unavailable, download the GitHub zip and unpack it; the rest is
identical.

Run the skill's doctor (this file's directory is the skill root):

```bash
python3 <skill-dir>/scripts/doctor.py --repo ~/WeChat-agent-unigateway --skip-demo
```

It checks Python/git, the checkout, ports 18500/18600, and the config. Fix every
`FAIL` before continuing. (`WARN` about a missing `gateway.json` / `account.json` is
expected at this point.)

Then run the offline end-to-end demo — no WeChat, no network:

```bash
python3 examples/virtual_loop_demo.py
```

Expected: it prints ①–⑤ and ends with `全程：agent 只把 base_url 指过来…`, exit code 0.
If this fails, stop and fix the environment; do not continue to WeChat.

## 2. Configure `gateway.json`

```bash
cp gateway.example.json gateway.json
```

Pick **one** of the two agent modes below, then edit `gateway.json`.

### Mode A — `exec` agent (fastest; no second process)

The gateway runs the command for each message and sends its stdout back.

```json
{
  "default_agent": "claude",
  "agents": {
    "claude": {
      "type": "exec",
      "label": "Claude Code",
      "prefix": "c",
      "command": ["claude", "-p", "{text}"],
      "timeout": 900
    }
  },
  "virtual": {"enabled": false}
}
```

Use an absolute path for the CLI if it is not on the gateway's `PATH`.

### Mode B — `virtual` agent (recommended for long-lived sessions)

The agent owns its own session and pulls messages with its own long-poll; this is
what makes `/new`, `/sessions`, `/resume`, `/stop`, and steering work.

`gateway.json`:

```json
{
  "default_agent": "claude",
  "agents": {
    "claude": {"type": "virtual", "label": "Claude Code", "prefix": "c"}
  },
  "virtual": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 18500,
    "auto_approve": ["claude"]
  }
}
```

Then start the client (separate terminal / service) after step 4:

```bash
python3 clients/ilink_agent_client.py \
  --name claude \
  --base-url http://127.0.0.1:18500 \
  --runner "claude -p {text}"
```

`auto_approve: ["claude"]` lets this local client claim its identity without an
approval click. On a shared/remote machine, remove `auto_approve` and approve the
bind from `http://127.0.0.1:18600/` instead.

Validate the config:

```bash
python3 <skill-dir>/scripts/doctor.py --repo ~/WeChat-agent-unigateway
```

## 3. Bind the WeChat bot (one identity, one poller)

```bash
python3 login.py                 # QR printed as a URL/value
# or: python3 login.py --png data/qr.png     (needs `pip install segno`)
```

Scan/open the QR with the WeChat account that owns the bot; credentials are written
to `data/account.json` (keep it `0600`, never commit it).

**Rule:** only one process may long-poll a bot identity. Stop any other gateway or
WeChat bot integration using the same identity first, or messages will split.

## 4. Start the gateway

```bash
python3 -m agent_gateway --config gateway.json
```

Healthy startup looks like:

```
gateway up: account=… base=https://ilinkai.weixin.qq.com agents=claude
虚拟 iLink 已就绪：http://127.0.0.1:18500
控制台：http://127.0.0.1:18600/
```

There is no inbound port to open: iLink is an outbound long poll.

## 5. First message (end-to-end check)

From the bound WeChat account, send to the bot:

1. `保活` (or `/ilink`) — the gateway answers directly, without the agent. If this
   works, the WeChat side is alive.
2. `你好，报一下你是什么 agent` — should reach the configured agent and get a reply.
3. `/help` — command list; `/who` — current agent; `/agents` — roster.

For Mode B, check the client log shows a long poll and an answer; for Mode A, check
`data/`-adjacent gateway output for `dispatch -> claude`.

Definition of done: a real WeChat message produces a real agent answer, and
`http://127.0.0.1:18600/` shows the conversation.

## 6. Keep it running

- macOS: a LaunchAgent; Linux: a systemd unit; Windows: Task Scheduler with a
  hidden `wscript` launcher (the repo's `watchdog/` directory has a working example).
- Restart policy: `unless-stopped` / `Restart=always`. The gateway exits with code 3
  when the WeChat identity expires — that is intended (re-run `login.py`).
- Optional, on the `multi_device` branch only: cloud deployment, several devices per
  agent, and unified memory — see `docs/DEPLOY-CLOUD.md` and `docs/MEMORY.md`.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `iLink 会话已失效（ret=-14）` and exit 3 | bot identity expired / bound elsewhere | re-run `python3 login.py`; ensure no other process polls the same bot |
| `getupdates: ret=-1` repeatedly | the saved cursor belongs to another identity (e.g. a copied `data/`) | stop the gateway; write `{"buf": ""}` to `data/ilink/get_updates_buf.json`; start again |
| Gateway starts, WeChat gets no reply | no agent connected (virtual mode) or the command failed (exec mode) | `/agents`, `/who`; check for `dispatch -> …`; start the client or fix the command |
| `Address already in use` on 18500/18600 | another gateway/console is running | stop it; one identity one poller |
| Long spins of `ret=-1`/`-2` | old version without the error-code fix | update to the latest `main` |
| Dashboard 403 | console is loopback-only by default | open it from the gateway host, or use an SSH tunnel |
| Media/images fail over an SSH tunnel | client sees `127.0.0.1` and treats the gateway as local | use a LAN/VPN address (not loopback) in `--base-url` |
| Works in terminal, fails as a service | `PATH`/env differences | use absolute CLI paths; pass required env in the unit |
| `tests/test_start_stack.py` fails on Python 3.9 | that test needs 3.10+ | runtime is fine; use 3.10+ to run the full suite |

## 8. Safety

- `data/account.json` is the bot credential: `0600`, backed up privately, never
  pasted into chat or committed.
- Do not expose 18500/18600 to the internet. For remote agents use a VPN or an SSH
  tunnel (`docs/REMOTE-AGENTS.md`, `docs/DEPLOY-CLOUD.md`).
- Keep approvals on: an agent that can run commands should ask before dangerous
  work (`docs/APPROVALS.md`).
- Uninstall: stop the process; delete the checkout. Credentials live only under
  `data/`.

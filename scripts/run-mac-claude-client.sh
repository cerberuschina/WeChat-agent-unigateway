#!/bin/zsh
# Mac 接入端：把本机 claude 接成 studio 网关的 mac-claude agent。
# 供应商凭证不落在本文件里——每次启动从 CC Switch 的库里读（DeepSeek 那条）。
set -euo pipefail

REPO="/Users/mac/ClaudeWork/WeChat-agent-unigateway"
CLAUDE="/Users/mac/.local/bin/claude"
BASE_URL="http://100.95.111.58:18500"
TOKEN="$(cat "$REPO/data/mac-claude-token.txt")"

eval "$(python3 - <<'PY'
import sqlite3, json, os, shlex
db = os.path.expanduser("~/.cc-switch/cc-switch.db")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
row = con.execute(
    "select settings_config from providers where app_type='claude-desktop' and name='DeepSeek'"
).fetchone()
if not row:
    raise SystemExit("CC Switch 里找不到 DeepSeek 供应商")
env = json.loads(row[0])["env"]
print(f"export ANTHROPIC_BASE_URL={shlex.quote(env['ANTHROPIC_BASE_URL'])}")
print(f"export ANTHROPIC_AUTH_TOKEN={shlex.quote(env['ANTHROPIC_AUTH_TOKEN'])}")
print("export ANTHROPIC_MODEL=deepseek-chat")
PY
)"

exec /usr/bin/python3 -u "$REPO/clients/ilink_agent_client.py" \
  --name mac-claude \
  --base-url "$BASE_URL" \
  --reuse-token "$TOKEN" \
  --creds "$REPO/data/virtual-client-mac-claude.json" \
  --cwd /Users/mac/ClaudeWork \
  --session-command "$CLAUDE -p --input-format stream-json --output-format stream-json --verbose --permission-mode acceptEdits --add-dir $REPO/data/media"

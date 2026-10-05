#!/usr/bin/env bash
# Start agent-gateway on Linux/macOS.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f gateway.json ]]; then
  echo "[x] gateway.json not found. Copy gateway.example.json to gateway.json and edit it." >&2
  exit 1
fi

exec python3 -m agent_gateway --config gateway.json

#!/usr/bin/env python
"""Claude Code PreToolUse hook — ask the human on WeChat *before* running a command.

An unattended `claude -p` cannot answer a permission prompt, so today it either
gets blanket rights or dies doing nothing. This hook turns the prompt into a
message: the gateway puts "it wants to run <cmd>" on the phone, the user replies
``/approve <id>``, and the verdict comes back here as the hook's decision.

Wire it up in ``~/.claude/settings.json`` (project or user scope):

    "hooks": {
      "PreToolUse": [
        {"matcher": "Bash|PowerShell",
         "hooks": [{"type": "command",
                    "command": "python C:/path/to/hooks/claude_approval_hook.py"}]}
      ]
    }

Three env vars must be present — the iLink client exports them for its runner:

    AGW_APPROVAL_URL   http://127.0.0.1:18500   (the gateway)
    AGW_TOKEN          the agent's virtual identity token
    AGW_PEER           the WeChat chat to ask (optional; the gateway falls back
                       to the last person who wrote to the agent)

Anything other than a clear "allow" is a **deny**: no answer, a timeout, a
broken gateway or a crash all mean the command does not run. A hook must never be
the reason something happened without permission.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Only commands get a human question; reads and edits are cheap and reversible
# (and are governed by the ordinary permission rules). Override with AGW_ASK_TOOLS.
DEFAULT_ASK = ("Bash", "PowerShell", "shell", "run_command")


def _decision(allow: bool, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allow else "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))


def _post(url: str, token: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never a proxy
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _get(url: str, token: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0                       # not an event we understand: leave it alone

    tool = str(event.get("tool_name") or "")
    wanted = tuple(t.strip() for t in
                   (os.environ.get("AGW_ASK_TOOLS") or ",".join(DEFAULT_ASK)).split(",")
                   if t.strip())
    if tool not in wanted:
        return 0                       # no opinion: the normal rules decide

    base = (os.environ.get("AGW_APPROVAL_URL") or "").rstrip("/")
    token = os.environ.get("AGW_TOKEN") or ""
    if not base or not token:
        # Not a session we were wired into (an interactive run the user started, say):
        # defer to the ordinary permission flow instead of denying — a hook must never
        # break sessions that never opted into it.
        return 0

    payload = event.get("tool_input") or {}
    if isinstance(payload, dict):
        detail = payload.get("command") or payload.get("file_path") or json.dumps(
            payload, ensure_ascii=False)[:1000]
    else:
        detail = str(payload)[:1000]

    ttl = float(os.environ.get("AGW_APPROVAL_TTL") or 180)
    try:
        started = _post(f"{base}/agent/approval", token, {
            "peer": os.environ.get("AGW_PEER") or "",
            "title": f"想执行：{str(detail).strip()[:160]}",
            "detail": str(detail)[:1500],
            "kind": "command",
            "ttl": ttl,
        }, timeout=min(30.0, ttl))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _decision(False, f"问不到网关（{exc}），按拒绝处理。")
        return 0

    approval_id = str(started.get("id") or "")
    if not approval_id:
        _decision(False, f"网关没有给出编号：{started}")
        return 0

    try:
        answer = _get(f"{base}/agent/approval/{approval_id}/wait"
                      f"?timeout={int(min(ttl + 15, 3600))}", token, timeout=ttl + 30)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _decision(False, f"等不到答复（{exc}），按拒绝处理。")
        return 0

    approval = answer.get("approval") or {}
    decision = str(approval.get("decision") or "expired")
    if decision == "allow":
        _decision(True, f"用户在微信放行了（{approval_id}）。")
    elif decision == "deny":
        _decision(False, f"用户在微信拒绝了（{approval_id}）：{approval.get('reason') or ''}")
    else:
        _decision(False, f"{approval_id} 超时没答复，按拒绝处理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

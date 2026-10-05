"""Agent adapters: how the gateway actually talks to one backend.

Three shapes cover almost everything on a local machine:

``a2a``   A2A JSON-RPC 2.0 (``message/send``) — this is what the a2a-bridge
          exposes for Claude Code and Hermes, and what any A2A peer speaks.
``http``  A plain JSON endpoint: the message is rendered into a body template
          and the reply is read back from a dotted path.
``exec``  A local command line; the reply is its stdout. Useful for CLIs that
          have no server (and for wiring up anything that only ships a binary).
"""
from __future__ import annotations

import json
import shlex
import subprocess
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List


class BackendError(RuntimeError):
    """A backend could not answer. The message is safe to show the user."""


def _dotted(data: Any, path: str) -> Any:
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _join_parts(parts: Any) -> str:
    out: List[str] = []
    for part in parts or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
        elif isinstance(part, str):
            out.append(part)
    return "\n".join(out).strip()


def extract_a2a_reply(payload: Dict[str, Any]) -> str:
    """Pull the agent's answer out of an A2A response.

    A2A agents return a Task; the answer may sit in ``artifacts[*].parts`` (the
    usual case, including the a2a-bridge) or in ``status.message.parts`` when a
    task ended with a message instead of artifacts.
    """
    if isinstance(payload.get("error"), dict):
        raise BackendError(f"A2A error {payload['error'].get('code')}: {payload['error'].get('message')}")

    result = payload.get("result") if "result" in payload else payload
    if not isinstance(result, dict):
        raise BackendError("A2A response has no result object")

    chunks: List[str] = []
    for artifact in result.get("artifacts") or []:
        if isinstance(artifact, dict):
            text = _join_parts(artifact.get("parts"))
            if text:
                chunks.append(text)
    if not chunks:
        status = result.get("status") or {}
        message = status.get("message") or {}
        text = _join_parts(message.get("parts"))
        if text:
            chunks.append(text)
        state = str(status.get("state") or "")
        if not chunks and state in {"failed", "canceled", "rejected", "unknown"}:
            raise BackendError(f"A2A task ended as {state}")
    if not chunks:
        raise BackendError("A2A 返回里没有文本（artifacts 与 status.message 都是空的）")
    return "\n\n".join(chunks)


def _post_json(url: str, payload: Dict[str, Any], *, headers: Dict[str, str],
               token: str = "", timeout: float = 900.0) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json", **headers}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
        raise BackendError(f"HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BackendError(f"连不上（{exc}）") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackendError(f"返回的不是 JSON：{raw[:200]}") from exc
    if not isinstance(data, dict):
        raise BackendError("返回的不是对象")
    return data


def call_a2a(agent, text: str, *, context_id: str = "") -> str:
    message: Dict[str, Any] = {
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
    }
    if context_id:
        message["contextId"] = context_id
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {"message": message},
    }
    data = _post_json(agent.url, payload, headers=dict(agent.headers or {}),
                      token=agent.token, timeout=agent.timeout)
    return extract_a2a_reply(data)


def call_http(agent, text: str) -> str:
    if agent.body_template:
        body = json.loads(json.dumps(agent.body_template).replace("{text}", text))
    else:
        body = {"text": text}
    data = _post_json(agent.url, body, headers=dict(agent.headers or {}),
                      token=agent.token, timeout=agent.timeout)
    reply = _dotted(data, agent.reply_path)
    if isinstance(reply, str):
        return reply
    if reply is None:
        raise BackendError(f"在返回里找不到 {agent.reply_path or '<root>'} 的文本")
    return json.dumps(reply, ensure_ascii=False)


def call_exec(agent, text: str) -> str:
    argv = [part.replace("{text}", text) for part in agent.command]
    uses_stdin = not any("{text}" in part for part in agent.command)
    try:
        completed = subprocess.run(
            argv,
            input=text if uses_stdin else None,
            cwd=agent.cwd or None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=agent.timeout,
        )
    except FileNotFoundError as exc:
        raise BackendError(f"找不到命令 {agent.command[0]!r}（{exc}）") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"命令超时（>{agent.timeout:.0f}s）") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:300]
        raise BackendError(f"命令退出码 {completed.returncode}：{detail}")
    return (completed.stdout or "").strip()


def call_agent(agent, text: str, *, context_id: str = "") -> str:
    """Dispatch one message to one agent and return its reply text."""
    if not agent.enabled:
        raise BackendError(f"agent '{agent.name}' 是停用状态")
    if agent.type == "a2a":
        return call_a2a(agent, text, context_id=context_id)
    if agent.type == "http":
        return call_http(agent, text)
    if agent.type == "exec":
        return call_exec(agent, text)
    raise BackendError(f"未知 backend 类型：{agent.type}")


def describe_command(argv: List[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)

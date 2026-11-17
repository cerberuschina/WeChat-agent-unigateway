"""Federation entry: let another gateway run this machine's agents.

This is the *server* half of ``backends.call_gateway`` (see
``docs/FEDERATION.md``). It is deliberately standalone: it reads the same
``gateway.json`` as the main gateway but never logs into WeChat, so it can run
on a machine whose gateway is stopped, or on a box that has no bot identity at
all — all it needs is the local backends (a2a / http / exec).

    python -m agent_gateway.federation --config gateway.json
    python -m agent_gateway.federation --config gateway.json --host 100.95.111.58 --port 18700

Endpoints (everything else is 404):

    GET  /federation/health  -> {"ok": true, "agents": [...], "version": "..."}
    POST /federation/send    -> {"reply": "..."} | {"error": {"code", "message"}}

Both require ``Authorization: Bearer <federation.token>``. There is no
"no-auth" mode on purpose: an unauthenticated endpoint that runs arbitrary
agent commands is a remote shell with extra steps.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from . import backends
from .config import AgentConfig, Config, ConfigError, load_config

log = logging.getLogger("agent_gateway.federation")

MAX_BODY = 4 * 1024 * 1024  # a text message, not a file upload


class FederationError(Exception):
    """An error that maps to one HTTP status for the caller."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class FederationSettings:
    def __init__(self, *, host: str, port: int, token: str,
                 allowed_agents: List[str]):
        self.host = host
        self.port = port
        self.token = token
        self.allowed_agents = [a.lower() for a in allowed_agents]


def load_federation_settings(raw: Dict[str, Any]) -> FederationSettings:
    """Pull the ``federation`` block out of a gateway.json dict."""
    block = raw.get("federation") or {}
    if not isinstance(block, dict):
        raise ConfigError("'federation' must be an object")
    if not block.get("enabled"):
        raise ConfigError("federation is not enabled in this config")
    token = str(block.get("token") or "").strip()
    if not token:
        raise ConfigError("federation.token is required (no unauthenticated mode)")
    allowed = block.get("allowed_agents") or []
    if not isinstance(allowed, list) or not all(isinstance(a, str) for a in allowed):
        raise ConfigError("federation.allowed_agents must be a list of agent names")
    return FederationSettings(
        host=str(block.get("host") or "127.0.0.1"),
        port=int(block.get("port") or 18700),
        token=token,
        allowed_agents=allowed,
    )


def _pick_agent(cfg: Config, settings: FederationSettings, name: str) -> AgentConfig:
    agent = cfg.agents.get((name or "").strip().lower())
    if agent is None:
        raise FederationError(404, "unknown_agent",
                              f"这台机器上没有名为 {name!r} 的 agent")
    if agent.type == "virtual":
        raise FederationError(
            409, "virtual_agent_not_federated",
            f"agent {agent.name!r} 是 iLink（virtual）类型，需要用客户端接入，"
            f"不能通过 federation 调用")
    if settings.allowed_agents and agent.name not in settings.allowed_agents:
        raise FederationError(403, "agent_not_allowed",
                              f"agent {agent.name!r} 不在 allowed_agents 里")
    if not agent.enabled:
        raise FederationError(503, "agent_disabled", f"agent {agent.name!r} 已停用")
    return agent


def handle_send(cfg: Config, settings: FederationSettings, body: Dict[str, Any]) -> Dict[str, Any]:
    name = body.get("agent")
    text = body.get("text")
    if not isinstance(name, str) or not name.strip():
        raise FederationError(400, "bad_request", "缺少 agent")
    if not isinstance(text, str) or not text.strip():
        raise FederationError(400, "bad_request", "缺少 text")
    agent = _pick_agent(cfg, settings, name)
    context_id = body.get("context_id")
    if not isinstance(context_id, str):
        context_id = ""
    started = time.monotonic()
    try:
        reply = backends.call_agent(agent, text, context_id=context_id)
    except backends.BackendError as exc:
        raise FederationError(502, "backend_error", str(exc)) from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log.info("federation: %s answered in %d ms (peer=%s)",
             agent.name, elapsed_ms, body.get("peer") or context_id or "-")
    return {"reply": reply, "agent": agent.name,
            "context_id": context_id, "elapsed_ms": elapsed_ms}


def _make_handler(cfg: Config, settings: FederationSettings):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-gateway-federation/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            log.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return False
            offered = header[len("Bearer "):].strip()
            return hmac.compare_digest(offered, settings.token)

        def _reject(self, status: int, code: str, message: str) -> None:
            self._send_json(status, {"error": {"code": code, "message": message}})

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/federation/health":
                self._reject(404, "not_found", "只提供 /federation/health 与 /federation/send")
                return
            if not self._authorized():
                self._reject(401, "unauthorized", "缺少或错误的 token")
                return
            agents = [a.name for a in cfg.agents.values()
                      if a.enabled and a.type != "virtual"
                      and (not settings.allowed_agents or a.name in settings.allowed_agents)]
            self._send_json(200, {"ok": True, "agents": agents,
                                  "version": "1"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/federation/send":
                self._reject(404, "not_found", "只提供 /federation/health 与 /federation/send")
                return
            if not self._authorized():
                self._reject(401, "unauthorized", "缺少或错误的 token")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._reject(413, "bad_body", "请求体为空或过大")
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._reject(400, "bad_json", "请求体不是合法 JSON")
                return
            if not isinstance(body, dict):
                self._reject(400, "bad_json", "请求体必须是对象")
                return
            try:
                self._send_json(200, handle_send(cfg, settings, body))
            except FederationError as exc:
                self._reject(exc.status, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - never leak a traceback to the hub
                log.exception("federation send failed")
                self._reject(500, "internal_error", f"{type(exc).__name__}: {exc}")

    return Handler


def build_server(cfg: Config, settings: FederationSettings,
                 *, host: Optional[str] = None, port: Optional[int] = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(
        (host or settings.host, port if port is not None else settings.port),
        _make_handler(cfg, settings),
    )
    server.daemon_threads = True
    return server


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_gateway.federation",
        description="把本机的 agent 通过 /federation/* 暴露给另一台网关")
    parser.add_argument("--config", default="gateway.json", help="gateway.json 路径")
    parser.add_argument("--host", default="", help="覆盖 federation.host")
    parser.add_argument("--port", type=int, default=None, help="覆盖 federation.port")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    raw = json.loads(open(args.config, encoding="utf-8-sig").read())
    settings = load_federation_settings(raw)

    server = build_server(cfg, settings, host=args.host or None, port=args.port)
    host, port = server.server_address[:2]
    log.info("federation listening on http://%s:%s (agents: %s)",
             host, port, ", ".join(a.name for a in cfg.agents.values()
                                   if a.enabled and a.type != "virtual") or "<none>")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

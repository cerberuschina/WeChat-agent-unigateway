"""Virtual iLink server — the gateway pretends to be Tencent's WeChat bot API.

Why this exists (and why it beats wiring agents over A2A):

* An agent that already speaks **iLink** (Hermes' weixin channel, any ClawBot
  -style integration) needs **zero code changes** to be multiplexed: point its
  base URL at this server and it will QR-login, long-poll and send exactly as it
  would against ``ilinkai.weixin.qq.com``.
* No second WeChat account: the real identity stays with the gateway. Every
  agent gets a **virtual** bot identity (``virt-<name>@im.bot``) minted here.
* No A2A/HTTP/CLI adapter per agent, no per-agent timeouts: the agent pulls its
  own messages with its own long-poll and pushes its answers back. The gateway
  is just the wall socket.

How an agent gets connected
---------------------------
1. The agent starts its normal WeChat login and asks for a QR
   (``GET ilink/bot/get_bot_qrcode``) — but its base URL is this server.
2. This server answers with its **own** QR value and shows it to the operator
   (``GET /admin/binds``); the operator approves it (``POST /admin/approve``)
   or the server auto-approves per config.
3. ``GET ilink/bot/get_qrcode_status`` then returns ``confirmed`` with the
   virtual ``ilink_bot_id`` + ``bot_token`` + a ``baseurl`` pointing back here.
4. The agent long-polls ``getupdates`` (fed by the real WeChat side through
   :meth:`deliver`) and posts answers with ``sendmessage`` (forwarded by
   :meth:`on_outbound` to the real WeChat).

Redirecting an agent (no MITM, no certs): most clients honour a base-URL
override, e.g. Hermes' weixin adapter reads ``WEIXIN_BASE_URL`` — set it to
``http://127.0.0.1:<port>``. See docs/VIRTUAL-ILINK.md for the hosts-file
alternative and why it is riskier.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import media

RET_OK = 0
ERR_SESSION_EXPIRED = -14
ERR_RATE_LIMIT = -2
ERR_BAD_REQUEST = -501007

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18500
LONG_POLL_SECONDS = 30.0
BIND_TTL_SECONDS = 600.0


def virtual_account_id(name: str) -> str:
    """A bot id shaped like Tencent's, so agents treat it as normal."""
    safe = "".join(ch if ch.isalnum() else "-" for ch in (name or "agent").lower()).strip("-")
    return f"virt-{safe or 'agent'}{secrets.token_hex(3)}@im.bot"


class VirtualBind:
    """One agent's virtual WeChat identity."""

    def __init__(self, name: str, *, qrcode: str = "", note: str = ""):
        self.name = name
        self.note = note
        self.qrcode = qrcode or secrets.token_hex(16)
        self.account_id = virtual_account_id(name)
        self.token = secrets.token_urlsafe(24)
        self.created_at = time.time()
        self.confirmed_at: Optional[float] = None
        self.cursor = "vcur-0"
        self.cursor_seq = 0
        self.last_peer = ""
        # Set once this bind's qrcode has been handed to an agent, so a second
        # agent asking for a QR never receives the same credentials.
        self.qr_issued = False
        self.lock = threading.Condition()
        self.queue: deque = deque()
        self.context_tokens: Dict[str, str] = {}
        self.last_seen = time.time()

    # -- lifecycle -------------------------------------------------------
    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None

    @property
    def expired(self) -> bool:
        return not self.confirmed and (time.time() - self.created_at) > BIND_TTL_SECONDS

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "note": self.note,
            "qrcode": self.qrcode,
            "account_id": self.account_id,
            "confirmed": self.confirmed,
            "created_at": self.created_at,
            "queue": len(self.queue),
            "cursor_seq": self.cursor_seq,
        }

    # -- message pump ----------------------------------------------------
    def deliver(self, *, text: str, peer: str, message_id: str = "",
                context_token: str = "") -> None:
        """Hand one real-WeChat message to this agent (called by the router)."""
        if not text.strip():
            return
        with self.lock:
            self.cursor_seq += 1
            self.cursor = f"vcur-{self.cursor_seq}"
            self.last_peer = peer
            if context_token:
                self.context_tokens[peer] = context_token
            self.queue.append({
                "from_user_id": peer,
                "to_user_id": self.account_id,
                "message_id": message_id or f"vmsg-{secrets.token_hex(6)}",
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            })
            self.lock.notify_all()

    def drain(self, cursor: str, timeout: float) -> Tuple[str, List[Dict[str, Any]]]:
        """Long-poll: wait for messages, then return everything queued."""
        deadline = time.monotonic() + max(timeout, 0.0)
        with self.lock:
            while not self.queue and time.monotonic() < deadline:
                self.lock.wait(timeout=min(1.0, max(deadline - time.monotonic(), 0.05)))
            self.last_seen = time.time()
            messages = list(self.queue)
            self.queue.clear()
            if messages:
                # The agent gets a fresh cursor so it can be resumed safely.
                self.cursor = f"vcur-{self.cursor_seq}-r{secrets.token_hex(2)}"
            return self.cursor, messages


class _AddressAwareServer(ThreadingHTTPServer):
    """HTTP server that can listen on IPv6 (and dual-stack when bound to ``::``).

    ``ThreadingHTTPServer`` inherits ``AF_INET``, so an IPv6 host used to fail at
    bind time. ``IPV6_V6ONLY=0`` lets a ``::`` bind accept IPv4-mapped clients as
    well; if the platform refuses that, we stay IPv6-only rather than crash.
    """

    dual_stack = True

    def __init__(self, address, handler, family=None):
        import socket
        if family is not None:
            self.address_family = family      # must be set before the socket exists
        super().__init__(address, handler)

    def server_bind(self) -> None:
        import socket
        if self.address_family == socket.AF_INET6 and self.dual_stack:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (AttributeError, OSError):
                pass
        super().server_bind()


def _make_server(host: str, port: int, handler) -> "_AddressAwareServer":
    """Pick the address family from the host, so ``::`` and ``::1`` just work."""
    import socket
    if ":" in (host or ""):
        return _AddressAwareServer((host, port, 0, 0), handler, socket.AF_INET6)
    return _AddressAwareServer((host, port), handler, socket.AF_INET)


class VirtualILinkServer:
    """HTTP server that mimics the iLink bot API for local agents."""

    def __init__(self, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 data_dir: str | Path = "data/virtual",
                 auto_approve: Optional[List[str]] = None,
                 accept_tokens: Optional[Dict[str, str]] = None,
                 on_outbound: Optional[Callable[[VirtualBind, str], None]] = None,
                 on_outbound_items: Optional[Callable[[VirtualBind, List[Dict[str, Any]]], None]] = None,
                 on_typing: Optional[Callable[[VirtualBind, int], None]] = None,
                 bind_key: str = "", public_url: str = "",
                 allow_cidrs: Optional[List[str]] = None,
                 admin_cidrs: Optional[List[str]] = None,
                 on_log: Optional[Callable[[str], None]] = None):
        self.host = host
        self.port = port
        self.dir = Path(data_dir).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.auto_approve = [n.lower() for n in (auto_approve or [])]
        # real token -> agent name (see config.VirtualConfig.reuse_real_token_for)
        self.accept_tokens = {t: n for t, n in (accept_tokens or {}).items() if t and n}
        self.on_outbound = on_outbound
        # Media a virtual agent wants delivered: the gateway turns these into real
        # iLink items (upload + encrypt) on its own way out.
        self.on_outbound_items = on_outbound_items
        # Heartbeats from an agent ("still working") — the gateway holds the real
        # typing indicator; nothing is sent to WeChat for these.
        self.on_typing = on_typing
        # Remote agents: a pre-shared bind key, and the address they should use to
        # fetch media they cannot read from this machine's disk.
        self.bind_key = bind_key or ""
        self.public_url = (public_url or "").rstrip("/")
        self.allow_cidrs = [str(c) for c in (allow_cidrs or [])]
        # 默认只有本机能批准：公网暴露时，"人工批准"只有在这一条成立时才有意义。
        self.admin_cidrs = [str(c) for c in (admin_cidrs or ["127.0.0.1/32", "::1/128"])]
        self.uploads: Dict[str, Dict[str, Any]] = {}
        self.upload_dir = self.dir / "uploads"
        self._log = on_log or (lambda _m: None)
        self._lock = threading.RLock()
        self._binds: Dict[str, VirtualBind] = {}   # qrcode -> bind
        self._by_token: Dict[str, VirtualBind] = {}
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- state -----------------------------------------------------------
    def binds(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [b.as_dict() for b in self._binds.values() if not b.expired]

    def agent_names(self) -> List[str]:
        with self._lock:
            return [b.name for b in self._binds.values() if b.confirmed and not b.expired]

    def bind_named(self, name: str) -> Optional[VirtualBind]:
        wanted = name.lower()
        with self._lock:
            for bind in self._binds.values():
                if bind.name.lower() == wanted and bind.confirmed and not bind.expired:
                    return bind
        return None

    def request_bind(self, name: str = "", note: str = "") -> VirtualBind:
        bind = VirtualBind(name or "agent", note=note)
        with self._lock:
            self._binds[bind.qrcode] = bind
        self._log(f"virtual: pending bind {bind.name} qrcode={bind.qrcode[:8]}…")
        if bind.name.lower() in self.auto_approve:
            self.approve(bind.qrcode)
        return bind

    def approve(self, qrcode: str, *, name: str = "") -> Optional[VirtualBind]:
        with self._lock:
            bind = self._binds.get(qrcode)
            if not bind:
                return None
            if name and name != bind.name:
                # Re-minting on every approve would silently change the identity
                # the agent already received, so only do it on a real rename.
                bind.name = name
                bind.account_id = virtual_account_id(name)
            if not bind.confirmed:
                bind.confirmed_at = time.time()
                self._by_token[bind.token] = bind
        self._log(f"virtual: approved {bind.name} as {bind.account_id}")
        return bind

    def reject(self, qrcode: str) -> bool:
        with self._lock:
            bind = self._binds.pop(qrcode, None)
            if bind:
                self._by_token.pop(bind.token, None)
            return bind is not None

    def ensure_bind(self, name: str, *, issued: bool = True) -> VirtualBind:
        """Get (or create) a confirmed identity for a named agent.

        ``issued=True`` means "never hand this qrcode out to a fresh agent": used
        for identities that are reserved for a specific client (auto-approved
        agents and token-reuse agents).
        """
        bind = self.bind_named(name)
        if bind:
            return bind
        bind = self.request_bind(name)
        if not bind.confirmed:
            self.approve(bind.qrcode, name=name)
        bind.qr_issued = issued
        return bind

    def _bind_for_token(self, token: str) -> Optional[VirtualBind]:
        if not token:
            return None
        with self._lock:
            bind = self._by_token.get(token)
        if bind:
            return bind
        name = self.accept_tokens.get(token)
        if not name:
            return None
        # A client that is already bound to the real WeChat (its QR path may be
        # hard-wired to Tencent) keeps working through us: the token it already
        # holds is an alias for its virtual identity.
        self._log(f"virtual: real token reused for agent {name}")
        return self.ensure_bind(name, issued=True)

    # -- routing helpers -------------------------------------------------
    def deliver(self, agent: str, *, text: str, peer: str, message_id: str = "",
                context_token: str = "") -> bool:
        bind = self.bind_named(agent)
        if not bind:
            return False
        bind.deliver(text=text, peer=peer, message_id=message_id, context_token=context_token)
        self._log(f"virtual: -> {bind.name} {text[:60]!r}")
        return True

    def wait_for_bind(self, name: str, timeout: float = 120.0) -> Optional[VirtualBind]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            bind = self.bind_named(name)
            if bind:
                return bind
            time.sleep(0.5)
        return None

    # -- http ------------------------------------------------------------
    def start(self) -> Tuple[str, int]:
        handler = _make_handler(self)
        self._httpd = _make_server(self.host, self.port, handler)   # IPv6-aware
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        # Pre-authorise the listed agents: their identity exists from the start,
        # and their own QR login picks up the credentials (see pending_qr).
        # auto-approved agents may claim theirs through the QR flow; token-reuse
        # agents must not, so their qrcode is never handed out.
        for name in self.auto_approve:
            self.ensure_bind(name, issued=False)
        for name in self.accept_tokens.values():
            self.ensure_bind(name, issued=True)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="virtual-ilink",
                                        daemon=True)
        self._thread.start()
        self._log(f"virtual iLink listening on http://{self.host}:{self.port}")
        return self.host, self.port

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def base_url(self) -> str:
        # An IPv6 literal needs brackets or the URL is unusable ("http://::1:18500").
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"http://{host}:{self.port}"

    # -- endpoint bodies (also directly unit-testable) -------------------
    # -- access policy (public exposure) ---------------------------------
    def _ip_in(self, ip: str, cidrs: List[str]) -> bool:
        """CIDR match that also accepts a bare address (``::1`` == ``::1/128``)."""
        import ipaddress
        try:
            address = ipaddress.ip_address((ip or "").strip("[]"))
        except ValueError:
            return False
        for entry in cidrs or []:
            try:
                network = ipaddress.ip_network(str(entry).strip(), strict=False)
            except ValueError:
                continue
            if address in network:
                return True
        return False

    def may_admin(self, ip: str) -> bool:
        """Approving and listing binds is for admin networks only (default: localhost).

        Without this, exposing the port publicly would let a stranger approve their
        own bind — the operator's approval only means something if the operator is
        the only one who can press it.
        """
        return self._ip_in(ip, self.admin_cidrs)

    def may_use_api(self, ip: str) -> bool:
        """Agent-facing endpoints: allow-listed when configured.

        Empty ``allow_cidrs`` means "no network restriction" — binding still needs
        the pre-shared key, an identity token, and a human approval.
        """
        if not self.allow_cidrs:
            return True
        return self._ip_in(ip, self.allow_cidrs)

    def policy_hint(self) -> str:
        if not self.allow_cidrs:
            return ("未设置 allow_cidrs：能连到这个端口的机器都可以请求接入"
                    "（仍需 bind_key + 人工批准）。公网暴露请写明白名单。")
        return f"白名单：{', '.join(self.allow_cidrs)}"

    # -- remote (non-local) agents ---------------------------------------
    def public_base(self) -> str:
        """The address agents should use to reach us (``public_url`` wins)."""
        return self.public_url or self.base_url()

    def check_bind_key(self, key: str) -> bool:
        """A remote agent must know the pre-shared key.

        With no key configured the QR endpoint stays effectively local-only: only
        names in ``auto_approve`` get identities without an operator clicking
        approve.
        """
        if not self.bind_key:
            return True
        return secrets.compare_digest(str(key or ""), self.bind_key)

    def save_blob(self, name: str, data: bytes) -> str:
        """Keep bytes uploaded by a remote agent and return an opaque blob id."""
        blob_id = secrets.token_hex(8)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        safe = Path(name or "file.bin").name or "file.bin"
        target = self.upload_dir / f"{blob_id}-{safe}"
        target.write_bytes(data)
        self.uploads[blob_id] = {"name": safe, "size": len(data), "path": target}
        self._log(f"remote: stored {safe} ({len(data)} bytes) as blob {blob_id}")
        return blob_id

    def blob_path(self, reference: str) -> Optional[Path]:
        """Resolve ``blob:<id>`` (or a bare id) to the file we stored."""
        blob_id = reference.split(":", 1)[1] if reference.startswith("blob:") else reference
        entry = self.uploads.get(blob_id) or {}
        path = entry.get("path")
        return Path(path) if path and Path(path).is_file() else None

    def media_url(self, folder: str, name: str) -> str:
        """Where an agent that is *not* on this machine fetches a stored file."""
        return (f"{self.public_base()}/media/"
                f"{urllib.parse.quote(folder, safe='')}/{urllib.parse.quote(name, safe='')}")

    def read_media(self, rest: str) -> Optional[Tuple[str, bytes]]:
        """Serve one stored inbound file to an authenticated agent.

        ``rest`` is ``<peer-folder>/<filename>`` as handed out by :meth:`media_url`;
        anything that escapes the media root is refused rather than served.
        """
        root = (self.dir.parent / "media").resolve()
        try:
            target = (root / urllib.parse.unquote(rest)).resolve()
        except (OSError, ValueError):
            return None
        if root not in target.parents or not target.is_file():
            self._log(f"remote: refused media request {rest!r}")
            return None
        return target.name, target.read_bytes()

    def pending_qr(self) -> VirtualBind:
        """Pick which qrcode an agent gets.

        A **pre-authorised** agent (listed in ``virtual.auto_approve``) that has
        not picked up its credentials yet receives *its* bind, so its normal
        login flow completes with no operator involved. Any other agent gets a
        fresh pending bind, which the operator approves on the bind page.
        """
        with self._lock:
            for bind in self._binds.values():
                if bind.confirmed and not bind.qr_issued and not bind.expired:
                    bind.qr_issued = True
                    self._log(f"virtual: handing the credentials of {bind.name} to an agent")
                    return bind
        bind = self.request_bind()
        bind.qr_issued = True
        return bind

    def ep_qrcode(self, key: str = "") -> Dict[str, Any]:
        if not self.check_bind_key(key):
            self._log("virtual: refused a QR request without the bind key")
            return {"ret": ERR_BAD_REQUEST,
                    "errmsg": "远程接入需要 bind_key：请在取码请求里带上 ?key=…"}
        bind = self.pending_qr()
        return {"ret": RET_OK, "qrcode": bind.qrcode,
                "qrcode_img_content": f"{self.base_url()}/bind/{bind.qrcode}"}

    def ep_qrcode_status(self, qrcode: str) -> Dict[str, Any]:
        with self._lock:
            bind = self._binds.get(qrcode)
        if not bind:
            return {"ret": RET_OK, "status": "expired"}
        if bind.expired:
            return {"ret": RET_OK, "status": "expired"}
        if not bind.confirmed:
            return {"ret": RET_OK, "status": "wait"}
        return {"ret": RET_OK, "status": "confirmed", "ilink_bot_id": bind.account_id,
                "bot_token": bind.token, "baseurl": self.base_url(), "ilink_user_id": bind.name}

    def ep_getupdates(self, bind: VirtualBind, payload: Dict[str, Any],
                      timeout: float = LONG_POLL_SECONDS) -> Dict[str, Any]:
        cursor, messages = bind.drain(str(payload.get("get_updates_buf") or ""), timeout)
        return {"ret": RET_OK, "msgs": messages, "get_updates_buf": cursor}

    def ep_sendmessage(self, bind: VirtualBind, payload: Dict[str, Any]) -> Dict[str, Any]:
        msg = payload.get("msg") or {}
        text = ""
        media_items: List[Dict[str, Any]] = []
        for item in msg.get("item_list") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == 1:
                text += str((item.get("text_item") or {}).get("text") or "")
            elif media.parse_media_item(item):
                media_items.append(item)

        handled = False
        if text.strip() and self.on_outbound:
            self.on_outbound(bind, text)
            handled = True
        if media_items and self.on_outbound_items:
            self.on_outbound_items(bind, media_items)
            handled = True
        if not handled:
            return {"ret": ERR_BAD_REQUEST,
                    "errmsg": "empty message: no text and no usable media item"}
        return {"ret": RET_OK, "msg_id": f"vout-{secrets.token_hex(6)}"}


def _make_handler(server: VirtualILinkServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "VirtualILink/0.1"

        def log_message(self, *_args) -> None:  # silence the default stderr spam
            return

        # -- helpers ----------------------------------------------------
        def _send(self, payload: Dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, body: str, status: int = 200) -> None:
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _json_body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
            except json.JSONDecodeError:
                return {}

        def _token(self) -> str:
            auth = self.headers.get("Authorization") or ""
            return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        # -- routes -----------------------------------------------------
        def _deny(self, admin: bool) -> bool:
            """Answer 403 (and return True) when this client may not use the route.

            Admin routes (approval, bind list, health) default to localhost only;
            agent routes honour ``allow_cidrs`` when it is configured.
            """
            ip = (self.client_address or ("?",))[0]
            if (server.may_admin(ip) if admin else server.may_use_api(ip)):
                return False
            server._log(f"refused {'admin' if admin else 'agent'} request from {ip}")
            self._send({"ok": False, "error": "forbidden",
                        "hint": "管理接口默认只允许本机；公网接入请配置 allow_cidrs / admin_cidrs"},
                       status=403)
            return True

        def do_GET(self) -> None:  # noqa: N802
            path, _, query = self.path.partition("?")
            params = dict(urllib.parse.parse_qsl(query))

            if path.startswith(("/admin/", "/bind/")) or path in ("/", "/health"):
                if self._deny(admin=True):
                    return
            elif self._deny(admin=False):
                return

            if path == "/ilink/bot/get_bot_qrcode":
                key = params.get("key") or self.headers.get("X-Bind-Key") or ""
                server._log(f"virtual: 取码请求来自 {self.client_address[0]}")
                self._send(server.ep_qrcode(key))
                return
            if path == "/ilink/bot/get_qrcode_status":
                self._send(server.ep_qrcode_status(params.get("qrcode", "")))
                return
            if path.startswith("/media/"):
                # 远程 agent 在这里取回网关替他落盘的附件（用虚拟身份 token 认证）
                if not server._bind_for_token(self._token()):
                    self._send({"ret": ERR_SESSION_EXPIRED, "errmsg": "需要虚拟身份的 token"},
                               status=401)
                    return
                served = server.read_media(path[len("/media/"):])
                if not served:
                    self._send({"ok": False, "error": "Not Found"}, status=404)
                    return
                name, body = served
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("X-File-Name", urllib.parse.quote(name))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/admin/binds":
                self._send({"ok": True, "binds": server.binds()})
                return
            if path.startswith("/bind/"):
                qrcode = path[len("/bind/"):]
                self._text(_bind_page(qrcode))
                return
            if path == "/admin/approve":  # convenience for curl/operator
                bind = server.approve(params.get("qrcode", ""), name=params.get("name", ""))
                self._send({"ok": bool(bind), "bind": bind.as_dict() if bind else None})
                return
            if path == "/admin/reject":
                self._send({"ok": server.reject(params.get("qrcode", ""))})
                return
            if path in ("/", "/health"):
                self._send({"ok": True, "service": "virtual-ilink",
                            "binds": [b["name"] for b in server.binds() if b["confirmed"]]})
                return
            self._send({"ok": False, "error": "Not Found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            path, _, query = self.path.partition("?")
            params = dict(urllib.parse.parse_qsl(query))

            if path.startswith("/admin/"):
                if self._deny(admin=True):
                    return
            elif self._deny(admin=False):
                return

            if path == "/upload":
                # 远程 agent 把文件字节推过来（它的路径在这边没有意义）
                if not server._bind_for_token(self._token()):
                    self._send({"ret": ERR_SESSION_EXPIRED, "errmsg": "需要虚拟身份的 token"},
                               status=401)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(length) if length else b""
                if not data:
                    self._send({"ret": ERR_BAD_REQUEST, "errmsg": "文件内容为空"})
                    return
                name = urllib.parse.unquote(self.headers.get("X-File-Name") or "file.bin")
                blob = server.save_blob(name, data)
                self._send({"ret": RET_OK, "blob": f"blob:{blob}",
                            "name": Path(name).name, "size": len(data)})
                return

            payload = self._json_body()

            if path == "/ilink/bot/get_bot_qrcode":
                self._send(server.ep_qrcode())
                return
            if path == "/ilink/bot/get_qrcode_status":
                self._send(server.ep_qrcode_status(params.get("qrcode", "")))
                return

            if path in ("/admin/approve", "/admin/reject"):
                if path.endswith("approve"):
                    bind = server.approve(str(payload.get("qrcode") or ""),
                                          name=str(payload.get("name") or ""))
                    self._send({"ok": bool(bind), "bind": bind.as_dict() if bind else None})
                else:
                    self._send({"ok": server.reject(str(payload.get("qrcode") or ""))})
                return

            # every /ilink/bot/* call below needs a virtual token
            bind = server._bind_for_token(self._token())
            if not bind or not bind.confirmed:
                self._send({"ret": ERR_SESSION_EXPIRED, "errmsg": "virtual: unknown or unconfirmed token"})
                return

            if path == "/ilink/bot/getupdates":
                self._send(server.ep_getupdates(bind, payload))
                return
            if path == "/ilink/bot/sendmessage":
                self._send(server.ep_sendmessage(bind, payload))
                return
            if path == "/ilink/bot/sendtyping":
                # A heartbeat, not a message: state 1 = 还在干活，2 = 收工。
                try:
                    state = int(payload.get("state"))
                except (TypeError, ValueError):
                    state = 1
                if server.on_typing:
                    server.on_typing(bind, state)
                self._send({"ret": RET_OK})
                return
            if path == "/ilink/bot/getconfig":
                self._send({"ret": RET_OK, "typing_ticket": ""})
                return
            if path == "/ilink/bot/getuploadurl":
                self._send({"ret": ERR_BAD_REQUEST,
                            "errmsg": "virtual gateway: media upload not supported yet"})
                return
            self._send({"ret": ERR_BAD_REQUEST, "errmsg": f"virtual gateway: no such endpoint {path}"})

    return Handler


def _bind_page(qrcode: str) -> str:
    """Minimal operator page: an agent asked to connect, approve or reject it."""
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>虚拟微信 · 批准接入</title>
<style>
 body{{font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:40px;max-width:640px}}
 code{{background:#f2f2f2;padding:2px 6px;border-radius:4px}}
 button{{font-size:15px;padding:8px 18px;margin-right:10px;cursor:pointer}}
 .row{{margin:18px 0}} .hint{{color:#666;font-size:13px}}
</style>
<h2>有一个 agent 想接入微信</h2>
<p>扫码请求：<code>{qrcode}</code></p>
<div class="row">
  <label>给它起个名字（就是以后路由用的名字，例如 hermes / claude）：<br>
  <input id="name" value="" placeholder="agent 名字" style="font-size:15px;padding:6px 8px;width:240px"></label>
</div>
<div class="row">
  <button onclick="approve()">批准接入</button>
  <button onclick="reject()">拒绝</button>
</div>
<p class="hint">批准后这个 agent 会拿到一个<strong>虚拟微信号</strong>，它以为自己在跟微信说话，实际对面是这个网关。</p>
<script>
async function approve() {{
  const name = document.getElementById('name').value.trim();
  const r = await fetch('/admin/approve', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{qrcode: '{qrcode}', name}})}});
  const d = await r.json();
  document.body.innerHTML = d.ok
    ? '<h2>已批准</h2><p><code>' + (d.bind.account_id || '') + '</code></p>'
    : '<h2>批准失败</h2>';
}}
async function reject() {{
  await fetch('/admin/reject', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{qrcode: '{qrcode}'}})}});
  document.body.innerHTML = '<h2>已拒绝</h2>';
}}
</script></html>"""

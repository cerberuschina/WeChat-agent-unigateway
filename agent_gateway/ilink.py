"""Minimal client for Tencent's **iLink Bot API** (personal WeChat bots).

This is the wire layer only — no routing, no agent logic. It is deliberately
dependency-free (stdlib ``urllib`` + threads) so the gateway runs anywhere
Python 3.9+ runs.

Wire notes (all verified against a working adapter, 2026-10):

* Base URL ``https://ilinkai.weixin.qq.com``; every call is a POST (or GET for
  the QR endpoints) to ``ilink/bot/<action>`` returning JSON.
* Auth is a bearer token plus ``AuthorizationType: ilink_bot_token`` and a
  random ``X-WECHAT-UIN`` header. The token is minted once by QR login and is
  long-lived; re-login only when the server answers ``ret = -14``.
* Inbound messages arrive from **long polling** ``getupdates`` (35 s hold).
  The response carries ``msgs`` plus a ``get_updates_buf`` cursor that must be
  replayed on the next call — treat it like an opaque offset.
* Every send needs the peer's ``context_token`` (delivered on each inbound
  message) — it is what binds a reply to the conversation that produced it.
* One bot identity = one WeChat contact. Exactly one process may poll it.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8)

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

ITEM_TEXT = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
TYPING_START, TYPING_STOP = 1, 2

ERR_SESSION_EXPIRED = -14
ERR_RATE_LIMIT = -2


class ILinkError(RuntimeError):
    """Any non-OK answer from iLink."""

    def __init__(self, message: str, *, ret: Optional[int] = None,
                 errcode: Optional[int] = None, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.ret = ret
        self.errcode = errcode
        self.payload = payload or {}

    @property
    def code(self) -> Optional[int]:
        return self.errcode if self.errcode is not None else self.ret


class SessionExpired(ILinkError):
    """``ret = -14`` — the token is gone; run the QR login again."""


class RateLimited(ILinkError):
    """``ret = -2`` — iLink is throttling; back off and retry."""


def _uin_header() -> str:
    value = int.from_bytes(secrets.token_bytes(4), "big")
    return base64.b64encode(str(value).encode("ascii")).decode("ascii")


def _headers(token: Optional[str], body_len: int = 0) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(body_len),
        "X-WECHAT-UIN": _uin_header(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _raise_for_status(payload: Dict[str, Any], context: str) -> Dict[str, Any]:
    ret = payload.get("ret")
    errcode = payload.get("errcode")
    errmsg = str(payload.get("errmsg") or payload.get("msg") or "")
    bad = (isinstance(ret, int) and ret != 0) or (isinstance(errcode, int) and errcode != 0)
    if not bad:
        return payload
    detail = f"{context}: ret={ret} errcode={errcode} errmsg={errmsg or '-'}"
    if errcode == ERR_SESSION_EXPIRED or ret == ERR_SESSION_EXPIRED:
        raise SessionExpired(detail, ret=ret, errcode=errcode, payload=payload)
    if errcode == ERR_RATE_LIMIT or ret == ERR_RATE_LIMIT:
        raise RateLimited(detail, ret=ret, errcode=errcode, payload=payload)
    raise ILinkError(detail, ret=ret, errcode=errcode, payload=payload)


def _is_private_host(host: str) -> bool:
    """Loopback / LAN / link-local / ULA — a proxy in the middle is wrong there."""
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost":
        return True
    if host == "::1" or host.startswith(("127.", "10.", "192.168.", "169.254.", "::ffff:127.")):
        return True
    if ":" in host and host.startswith(("fe80:", "fc", "fd")):   # IPv6 link-local / ULA
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


def _opener_for(url: str):
    """A no-proxy opener for local/LAN targets, ``None`` (default) otherwise.

    Traffic to Tencent should keep honouring the system proxy — that is how it
    reaches the internet here. An agent talking to a gateway on this machine or on
    the LAN must not: a proxy there answers 502 instead of connecting.
    """
    if _is_private_host(urllib.parse.urlsplit(url).hostname or ""):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return None


def _request(method: str, base_url: str, endpoint: str, *, token: Optional[str] = None,
             payload: Optional[Dict[str, Any]] = None, timeout_ms: int = API_TIMEOUT_MS,
             extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    body: Optional[bytes] = None
    if payload is not None:
        body = json.dumps({**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = _headers(token, len(body) if body else 0)
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    timeout = max(timeout_ms / 1000.0, 1.0)
    opener = _opener_for(url)
    try:
        if opener is None:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        else:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise ILinkError(f"HTTP {exc.code} on {endpoint}: {raw[:200]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ILinkError(f"network error on {endpoint}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ILinkError(f"{endpoint} returned non-JSON: {raw[:200]}") from exc
    if not isinstance(data, dict):
        raise ILinkError(f"{endpoint} returned {type(data).__name__}, expected object")
    return data


# --------------------------------------------------------------------------
# QR login (no token needed)
# --------------------------------------------------------------------------
def fetch_qr(bot_type: str = "3", base_url: str = BASE_URL,
             bind_key: str = "") -> Tuple[str, str]:
    """Return ``(qrcode_value, qrcode_url)`` for the login QR.

    ``bind_key`` goes through as a query parameter: a *virtual* gateway reachable
    from other machines requires the pre-shared key before it hands out an
    identity (a real iLink server just ignores the extra parameter). Falls back to
    ``ILINK_BIND_KEY`` in the environment so a client can set it once.
    """
    import os
    # QR login itself sets no token; the shared key is the only credential here.
    bind_key = bind_key or os.environ.get("ILINK_BIND_KEY", "")
    endpoint = f"{EP_GET_BOT_QR}?bot_type={urllib.parse.quote(bot_type)}"
    if bind_key:
        endpoint += f"&key={urllib.parse.quote(bind_key)}"
    data = _request("GET", base_url, endpoint,
                    timeout_ms=QR_TIMEOUT_MS,
                    extra_headers={"iLink-App-Id": ILINK_APP_ID,
                                   "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)})
    _raise_for_status(data, "get_bot_qrcode")
    value = str(data.get("qrcode") or data.get("qrcode_value") or "")
    url = str(data.get("qrcode_img_content") or data.get("qrcode_url") or "")
    return value, url


def qr_status(qrcode: str, base_url: str = BASE_URL) -> Dict[str, Any]:
    data = _request("GET", base_url, f"{EP_GET_QR_STATUS}?qrcode={urllib.parse.quote(qrcode)}",
                    timeout_ms=QR_TIMEOUT_MS,
                    extra_headers={"iLink-App-Id": ILINK_APP_ID,
                                   "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)})
    return _raise_for_status(data, "get_qrcode_status")


def qr_login(*, bot_type: str = "3", timeout_seconds: int = 480,
             base_url: str = BASE_URL, max_refreshes: int = 3,
             on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> Optional[Dict[str, str]]:
    """Blocking QR login. Returns creds dict on success, ``None`` on timeout.

    The QR expires in ~2 minutes, so on ``expired`` a fresh one is fetched
    (``max_refreshes`` times) and reported through ``on_event`` again.

    ``on_event`` gets ``("qr", {"qrcode":..., "url":...})`` once per QR and
    ``("status", {...})`` for each poll result, so a CLI can render progress.
    """
    qrcode, url = fetch_qr(bot_type, base_url)
    if not qrcode:
        return None
    if on_event:
        on_event("qr", {"qrcode": qrcode, "url": url})
    deadline = time.monotonic() + timeout_seconds
    current_base = base_url
    refreshes = 0
    while time.monotonic() < deadline:
        try:
            status = qr_status(qrcode, current_base)
        except ILinkError:
            time.sleep(1.0)
            continue
        state = str(status.get("status") or "wait")
        if on_event:
            on_event("status", status)
        if state == "scaned_but_redirect" and status.get("redirect_host"):
            current_base = f"https://{status['redirect_host']}"
        elif state == "expired":
            refreshes += 1
            if refreshes >= max_refreshes:
                return None
            qrcode, url = fetch_qr(bot_type, current_base)
            if on_event:
                on_event("qr", {"qrcode": qrcode, "url": url})
        elif state == "confirmed":
            account_id = str(status.get("ilink_bot_id") or "")
            token = str(status.get("bot_token") or "")
            if not account_id or not token:
                return None
            return {
                "account_id": account_id,
                "token": token,
                "base_url": str(status.get("baseurl") or base_url),
                "user_id": str(status.get("ilink_user_id") or ""),
            }
        time.sleep(1.0)
    return None


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
class ILinkClient:
    """Thread-safe iLink client bound to one bot identity."""

    def __init__(self, account_id: str, token: str, *, base_url: str = BASE_URL,
                 data_dir: str | os.PathLike[str] = "data",
                 on_log: Optional[Callable[[str], None]] = None):
        self.account_id = account_id
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._log = on_log or (lambda _msg: None)
        self._send_lock = threading.Lock()
        self._context_tokens: Dict[str, str] = {}
        self._restore()

    # -- persistence -----------------------------------------------------
    @property
    def _context_path(self) -> Path:
        return self.data_dir / "context-tokens.json"

    @property
    def _cursor_path(self) -> Path:
        return self.data_dir / "get_updates_buf.json"

    def _restore(self) -> None:
        try:
            if self._context_path.exists():
                data = json.loads(self._context_path.read_text(encoding="utf-8-sig"))
                self._context_tokens = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
        except (OSError, json.JSONDecodeError):
            self._context_tokens = {}

    def context_token(self, peer: str) -> Optional[str]:
        return self._context_tokens.get(peer)

    def remember_context_token(self, peer: str, token: str) -> None:
        if not token:
            return
        self._context_tokens[peer] = token
        try:
            tmp = self._context_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._context_tokens, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._context_path)
        except OSError as exc:  # pragma: no cover
            self._log(f"warn: cannot persist context tokens: {exc}")

    def load_cursor(self) -> str:
        try:
            if self._cursor_path.exists():
                data = json.loads(self._cursor_path.read_text(encoding="utf-8-sig"))
                return str(data.get("buf") or "")
        except (OSError, json.JSONDecodeError):
            pass
        return ""

    def save_cursor(self, buf: str) -> None:
        try:
            tmp = self._cursor_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"buf": buf}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._cursor_path)
        except OSError as exc:  # pragma: no cover
            self._log(f"warn: cannot persist cursor: {exc}")

    # -- calls -----------------------------------------------------------
    def get_updates(self, cursor: str) -> Dict[str, Any]:
        """One long-poll round. Rate limits are swallowed into an empty batch."""
        try:
            return _request("POST", self.base_url, EP_GET_UPDATES, token=self.token,
                            payload={"get_updates_buf": cursor}, timeout_ms=LONG_POLL_TIMEOUT_MS)
        except RateLimited:
            return {"ret": 0, "msgs": [], "get_updates_buf": cursor}
        except ILinkError as exc:
            if "network error" in str(exc) or "timed out" in str(exc):
                return {"ret": 0, "msgs": [], "get_updates_buf": cursor}
            raise

    def send_text(self, to: str, text: str, *, context_token: Optional[str] = None,
                  client_id: Optional[str] = None) -> Dict[str, Any]:
        if not text or not text.strip():
            raise ValueError("send_text: text must not be empty")
        message: Dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": client_id or f"gw-{secrets.token_hex(8)}",
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        with self._send_lock:
            return _raise_for_status(
                _request("POST", self.base_url, EP_SEND_MESSAGE, token=self.token,
                         payload={"msg": message}, timeout_ms=API_TIMEOUT_MS),
                "sendmessage")

    def send_typing(self, to: str, state: int, *, typing_ticket: str,
                    context_token: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"ilink_user_id": to, "typing_ticket": typing_ticket,
                                   "state": state}
        if context_token:
            payload["context_token"] = context_token
        return _request("POST", self.base_url, EP_SEND_TYPING, token=self.token,
                        payload=payload, timeout_ms=CONFIG_TIMEOUT_MS)

    def get_config(self, user_id: str, *, context_token: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"ilink_user_id": user_id}
        if context_token:
            payload["context_token"] = context_token
        return _raise_for_status(
            _request("POST", self.base_url, EP_GET_CONFIG, token=self.token,
                     payload=payload, timeout_ms=CONFIG_TIMEOUT_MS),
            "getconfig")


# --------------------------------------------------------------------------
# Inbound helpers
# --------------------------------------------------------------------------
def message_text(message: Dict[str, Any]) -> str:
    """Flatten ``item_list`` into plain text (text items only)."""
    chunks: List[str] = []
    for item in message.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == ITEM_TEXT:
            text_item = item.get("text_item") or {}
            text = text_item.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
    return "\n".join(chunks).strip()


def sender_of(message: Dict[str, Any]) -> str:
    return str(message.get("from_user_id") or "").strip()


def split_text(text: str, max_chars: int = 1200) -> List[str]:
    """Split long replies on blank lines, then hard-wrap, so WeChat gets
    several readable messages instead of one truncated one."""
    text = (text or "").strip()
    if not text:
        return []
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    parts: List[str] = []
    buffer = ""
    for block in text.split("\n\n"):
        candidate = f"{buffer}\n\n{block}" if buffer else block
        if len(candidate) <= max_chars:
            buffer = candidate
            continue
        if buffer:
            parts.append(buffer)
            buffer = ""
        while len(block) > max_chars:
            cut = block.rfind("\n", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        buffer = block
    if buffer:
        parts.append(buffer)
    return [p for p in parts if p.strip()]

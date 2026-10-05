"""The gateway itself: poll iLink once, fan out to N agents, reply to one chat.

Design notes that matter in practice (learned the hard way):

* **One poller.** A bot identity may only be long-polled by one process. The
  gateway is that process; agents behind it never touch iLink.
* **Ack first, answer later.** Agents take seconds to minutes. The chat gets an
  immediate "已转给 X…" and the real answer later, otherwise WeChat just sits
  there and the user resends.
* **Per-chat ordering.** Messages from the same peer are handled by a worker
  pool but serialized per chat, so replies never arrive out of order.
* **Dedup twice.** By ``message_id`` and by content fingerprint — the upstream
  re-sends identical text under fresh ids.
* **Cursor on disk.** ``get_updates_buf`` is persisted so a restart does not
  replay or drop messages.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Optional

from . import backends, ilink, router
from .config import Config, ConfigError, load_account, load_config, save_account
from .store import StateStore
from .virtual_ilink import VirtualILinkServer

log = logging.getLogger("agent_gateway")

ACCOUNT_FILE = "account.json"


class Gateway:
    def __init__(self, cfg: Config, *, dry_run: bool = False,
                 account_file: Optional[Path] = None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.account_file = Path(account_file) if account_file else (cfg.data_dir / ACCOUNT_FILE)
        self.store = StateStore(cfg.data_dir / "state")
        self.client: Optional[ilink.ILinkClient] = None
        self._chat_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")
        self._typing_tickets: Dict[str, str] = {}
        self.virtual: Optional[VirtualILinkServer] = None

    # -- account ---------------------------------------------------------
    def _resolve_account(self) -> bool:
        if self.cfg.account.configured:
            self.client = ilink.ILinkClient(self.cfg.account.account_id, self.cfg.account.token,
                                            base_url=self.cfg.account.base_url,
                                            data_dir=self.cfg.data_dir / "ilink",
                                            on_log=log.warning)
            return True
        saved = load_account(self.account_file)
        if saved and saved.get("account_id") and saved.get("token"):
            self.client = ilink.ILinkClient(str(saved["account_id"]), str(saved["token"]),
                                            base_url=str(saved.get("base_url") or ilink.BASE_URL),
                                            data_dir=self.cfg.data_dir / "ilink",
                                            on_log=log.warning)
            log.info("using saved account %s", _safe(saved["account_id"]))
            return True
        return False

    def login(self, *, timeout_seconds: int = 480) -> bool:
        """QR login, then persist creds for later runs."""
        def on_event(kind: str, payload: dict) -> None:
            if kind == "qr":
                print(f"\n用微信扫码绑定这个 bot（用它当网关的唯一入口）：\n{payload.get('url') or ''}\n"
                      f"qrcode={payload.get('qrcode')}\n", flush=True)
            else:
                status = str(payload.get("status") or "")
                if status and status != "wait":
                    print(f"[qr] {status}", flush=True)

        creds = ilink.qr_login(timeout_seconds=timeout_seconds, on_event=on_event)
        if not creds:
            print("登录失败或超时。", file=sys.stderr)
            return False
        save_account(self.account_file, account_id=creds["account_id"], token=creds["token"],
                     base_url=creds["base_url"], user_id=creds.get("user_id", ""))
        print(f"绑定成功：account_id={_safe(creds['account_id'])} → {self.account_file}")
        self.cfg.account.account_id = creds["account_id"]
        self.cfg.account.token = creds["token"]
        self.cfg.account.base_url = creds["base_url"]
        return True

    # -- helpers ---------------------------------------------------------
    def _chat_lock(self, chat_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._chat_locks.get(chat_id)
            if lock is None:
                lock = threading.Lock()
                self._chat_locks[chat_id] = lock
            return lock

    def _send(self, chat_id: str, text: str) -> None:
        if self.dry_run or self.client is None:
            log.info("[dry-run] -> %s: %s", _safe(chat_id), text[:200].replace("\n", " / "))
            print(f"[dry-run] -> {_safe(chat_id)}: {text}")
            return
        context_token = self.client.context_token(chat_id)
        for chunk in ilink.split_text(text, self.cfg.delivery.max_chars_per_message):
            try:
                self.client.send_text(chat_id, chunk, context_token=context_token)
            except ilink.SessionExpired:
                raise
            except ilink.ILinkError as exc:
                log.warning("send failed to %s: %s", _safe(chat_id), exc)
                time.sleep(1.0)
                try:
                    self.client.send_text(chat_id, chunk, context_token=context_token)
                except ilink.ILinkError as retry_exc:
                    log.error("send failed twice to %s: %s", _safe(chat_id), retry_exc)
                    raise

    def _typing(self, chat_id: str, state: int) -> None:
        if self.dry_run or self.client is None:
            return
        ticket = self._typing_tickets.get(chat_id)
        if not ticket:
            try:
                cfg = self.client.get_config(chat_id, context_token=self.client.context_token(chat_id))
                ticket = str(cfg.get("typing_ticket") or "")
            except ilink.ILinkError:
                ticket = ""
            if ticket:
                self._typing_tickets[chat_id] = ticket
        if not ticket:
            return
        try:
            self.client.send_typing(chat_id, state, typing_ticket=ticket,
                                    context_token=self.client.context_token(chat_id))
        except ilink.ILinkError:
            pass

    # -- message handling ------------------------------------------------
    def handle(self, message: dict) -> None:
        sender = ilink.sender_of(message)
        if not sender or (self.client and sender == self.client.account_id):
            return
        message_id = str(message.get("message_id") or "").strip()
        text = ilink.message_text(message)
        if message_id and self.store.is_duplicate(f"id:{message_id}"):
            log.debug("duplicate message_id %s", _safe(message_id))
            return
        if text and self.store.is_duplicate(f"text:{sender}:{self.store.fingerprint(text)}"):
            log.debug("duplicate content from %s", _safe(sender))
            return
        if not text:
            if not self.dry_run:
                self._send(sender, "现在只认文字消息（图片/语音还没接）。")
            return
        if not self.cfg.access.allows(sender):
            log.info("ignored message from unauthorized %s", _safe(sender))
            return

        context_token = str(message.get("context_token") or "").strip()
        if context_token and self.client and not self.dry_run:
            self.client.remember_context_token(sender, context_token)

        decision = router.route(text, self.cfg, sticky=self.store.sticky(sender))
        if decision.set_sticky:
            self.store.set_sticky(sender, decision.set_sticky)
        if decision.kind == "ignore":
            return
        if decision.kind == "reply":
            if not self.dry_run:
                self._send(sender, decision.text)
            else:
                print(f"[dry-run] {suggestion_prefix(decision)} {decision.text}")
            return

        agent = self.cfg.agent(decision.agent)
        if not agent:
            self._send(sender, f"路由到了一个不存在的 agent：{decision.agent}")
            return
        log.info("dispatch -> %s (%s) from %s", agent.name, decision.note, _safe(sender))

        # A "virtual" agent does not get called: it pulls its own messages with
        # its own iLink long-poll (it thinks it is talking to WeChat). We just
        # drop the message into its queue and wait for it to answer.
        if agent.type == "virtual":
            self._dispatch_virtual(agent, decision.text, sender, message_id, context_token)
            return

        with self._chat_lock(sender):
            if self.cfg.delivery.ack and not self.dry_run:
                self._send(sender, self.cfg.delivery.ack_template.format(label=agent.display))
            self._typing(sender, ilink.TYPING_START)
            try:
                reply = backends.call_agent(agent, decision.text, context_id=f"wx:{sender}")
            except backends.BackendError as exc:
                self._send(sender, self.cfg.delivery.error_template.format(label=agent.display, error=exc))
                return
            except ilink.SessionExpired:
                raise
            except Exception as exc:  # noqa: BLE001 - never kill the poll loop
                log.exception("backend %s crashed", agent.name)
                self._send(sender, self.cfg.delivery.error_template.format(label=agent.display, error=exc))
                return
            finally:
                self._typing(sender, ilink.TYPING_STOP)
            if not reply.strip():
                reply = f"「{agent.display}」没有返回任何内容。"
            self._send(sender, reply)

    def _dispatch_virtual(self, agent, text: str, sender: str, message_id: str,
                          context_token: str) -> None:
        if not self.virtual:
            self._send(sender, f"「{agent.display}」是虚拟接入的，但这个网关没开 virtual 模式"
                               f"（gateway.json 的 virtual.enabled）。")
            return
        delivered = self.virtual.deliver(agent.name, text=text, peer=sender,
                                        message_id=message_id, context_token=context_token)
        if not delivered:
            self._send(sender, f"「{agent.display}」还没接入：让它在自己那边发起一次微信扫码登录，"
                               f"然后在这里批准 → {self.virtual.base_url()}/admin/binds")
            return
        if self.cfg.delivery.ack and not self.dry_run:
            self._send(sender, self.cfg.delivery.ack_template.format(label=agent.display))

    # -- poll loop -------------------------------------------------------
    def run(self, *, once: bool = False) -> int:
        if not self._resolve_account():
            print("还没有绑定微信：先跑 `python login.py`（或 `python -m agent_gateway --login`）。",
                  file=sys.stderr)
            return 2
        assert self.client is not None
        cursor = self.client.load_cursor()
        log.info("gateway up: account=%s base=%s agents=%s",
                 _safe(self.client.account_id), self.client.base_url,
                 ",".join(a.name for a in self.cfg.enabled_agents()))
        self._start_virtual()
        failures = 0
        while not self._stop.is_set():
            try:
                response = self.client.get_updates(cursor)
            except ilink.SessionExpired:
                print("iLink 会话已失效（ret=-14），需要重新扫码：python login.py", file=sys.stderr)
                return 3
            except ilink.ILinkError as exc:
                failures += 1
                log.warning("getupdates failed (%d): %s", failures, exc)
                time.sleep(min(30, 2 ** min(failures, 5)))
                continue
            failures = 0
            new_cursor = str(response.get("get_updates_buf") or "")
            if new_cursor and new_cursor != cursor:
                cursor = new_cursor
                self.client.save_cursor(cursor)
            for message in response.get("msgs") or []:
                if not isinstance(message, dict):
                    continue
                self._pool.submit(self._safe_handle, message)
            if once:
                break
        return 0

    # -- virtual iLink (agents connect to us, not to Tencent) -------------
    def _start_virtual(self) -> None:
        if not self.cfg.virtual.enabled:
            return
        self.virtual = VirtualILinkServer(
            host=self.cfg.virtual.host,
            port=self.cfg.virtual.port,
            data_dir=self.cfg.data_dir / "virtual",
            auto_approve=self.cfg.virtual.auto_approve,
            on_outbound=self._forward_to_wechat,
            on_log=log.info,
        )
        host, port = self.virtual.start()
        # Pre-authorise the agents the operator listed in virtual.auto_approve:
        # they get a virtual identity now, and pick up the credentials through
        # their own QR login (which this server answers).
        for name in self.cfg.virtual.auto_approve:
            if not self.virtual.bind_named(name):
                bind = self.virtual.request_bind(name)
                self.virtual.approve(bind.qrcode, name=name)
        print(f"\n虚拟 iLink 已就绪：{self.virtual.base_url()}")
        print("  把 agent 的微信 base_url 指到这里，它就等于接上了微信（例：Hermes 用 WEIXIN_BASE_URL）")
        print(f"  待批准的接入：http://{host}:{port}/admin/binds\n")
        for agent in self.cfg.enabled_agents():
            if agent.type != "virtual":
                continue
            bind = self.virtual.bind_named(agent.name)
            state = f"已接入 {bind.account_id}" if bind else "还没接入（等它在自己那边发起扫码登录）"
            print(f"  · {agent.name:<10} {state}")

    def _forward_to_wechat(self, bind, text: str) -> None:
        """A virtual agent answered: push it out through the real WeChat identity."""
        peer = bind.last_peer
        if not peer:
            log.warning("virtual agent %s answered %r before any peer was known", bind.name, text[:60])
            return
        log.info("virtual: %s -> wechat %s (%d chars)", bind.name, _safe(peer), len(text))
        self._send(peer, text)

    def _safe_handle(self, message: dict) -> None:
        try:
            self.handle(message)
        except ilink.SessionExpired:
            self._stop.set()
        except Exception:  # noqa: BLE001
            log.exception("inbound handling failed")

    def stop(self, *_args) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        if self.virtual:
            self.virtual.stop()
        self._pool.shutdown(wait=False)
        self.store.flush()


def suggestion_prefix(decision: router.Decision) -> str:
    return f"[{decision.note}]"


def _safe(value: Optional[str], keep: int = 8) -> str:
    text = str(value or "")
    return text if len(text) <= keep else f"{text[:keep]}…"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-gateway", description=__doc__.splitlines()[0])
    parser.add_argument("-c", "--config", default="gateway.json", help="path to gateway.json")
    parser.add_argument("--login", action="store_true", help="QR login and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="route and print instead of talking to WeChat (no iLink calls)")
    parser.add_argument("--once", action="store_true", help="process one long-poll batch, then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    gateway = Gateway(cfg, dry_run=args.dry_run)
    signal.signal(signal.SIGINT, gateway.stop)
    try:
        if args.login:
            return 0 if gateway.login() else 1
        return gateway.run(once=args.once)
    finally:
        gateway.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

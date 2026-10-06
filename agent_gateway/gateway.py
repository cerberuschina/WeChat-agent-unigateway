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

from . import backends, ilink, ilink_media, markdown, media, router
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
        self._typing_loops: Dict[str, threading.Event] = {}
        # WeChat lets a bot send only ~10 messages before the user replies again,
        # so count them per peer and keep the tail for the next turn.
        self._turn_counts: Dict[str, int] = {}
        self._pending_output: Dict[str, str] = {}
        # Console: a message ring, a log ring and the little HTTP server itself.
        self.traffic = None
        self.log_ring = None
        self.dashboard = None
        self.virtual: Optional[VirtualILinkServer] = None
        # Questions an agent needs a human to answer. The broker lives here so the
        # phone, the console and the router all look at one list; silence expires
        # to *deny*, because an unanswered question is not permission.
        from .approvals import ApprovalBroker
        self.approvals = ApprovalBroker(ttl=float(cfg.delivery.approval_ttl_seconds))
        # The chat an agent's question goes to when the agent does not name one:
        # kept on disk, because a question must still have a destination after a
        # restart (the phone that wrote to us last is the one to ask).
        self._last_peer: str = self.store.last_peer()

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
        budget = max(1, int(self.cfg.delivery.max_messages_per_turn))
        left = max(0, budget - self._turn_counts.get(chat_id, 0))
        # WeChat allows a bot only ~10 messages before the user replies. A progress
        # note must never eat the slots the actual answer needs.
        if text.lstrip().startswith("⏳") and left <= max(1, int(self.cfg.delivery.reserve_for_answer)):
            log.info("dropped a progress note for %s: %d slot(s) left this turn", _safe(chat_id), left)
            return
        context_token = self.client.context_token(chat_id)
        self._record("out", chat_id, "", text)
        # WeChat renders no Markdown and refuses oversized items, so rendering and
        # splitting belong to the channel layer — the agent sends what it means.
        chunks = markdown.prepare(text, self.cfg.delivery.max_chars_per_message)
        if len(chunks) > left:
            if left <= 1:
                self._queue_pending(chat_id, text)
                log.warning("no budget left for %s (%d chars queued to the next turn)",
                            _safe(chat_id), len(text))
                return
            head, rest = chunks[:left - 1], chunks[left - 1:]
            tail = "\n\n".join(rest)
            self._queue_pending(chat_id, tail)
            chunks = head + [f"（还差 {len(tail)} 字没发完，回我一句我接着发）"]
            log.info("trimmed an answer for %s to fit the per-turn message budget", _safe(chat_id))
        for chunk in chunks:
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
            self._turn_counts[chat_id] = self._turn_counts.get(chat_id, 0) + 1

    def _queue_pending(self, chat_id: str, text: str) -> None:
        """Keep what did not fit this turn; the peer's next message releases it."""
        existing = self._pending_output.get(chat_id, "")
        self._pending_output[chat_id] = f"{existing}\n\n{text}".strip() if existing else text

    def _take_pending(self, chat_id: str) -> str:
        return self._pending_output.pop(chat_id, "")

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

    def _start_typing(self, chat_id: str, *, max_seconds: float = 1800.0) -> None:
        """Keep 「正在输入」 alive while a virtual agent works.

        WeChat cannot edit a sent message (the real adapter sets
        ``SUPPORTS_MESSAGE_EDITING = False``), so there is no true streaming on
        this channel. What we can honestly do is hold the typing indicator for
        the whole run and let the agent send its own progress notes.
        """
        if self.dry_run or self.client is None:
            return
        self._stop_typing(chat_id, quiet=True)
        stop = threading.Event()
        self._typing_loops[chat_id] = stop
        deadline = time.monotonic() + max_seconds

        def loop() -> None:
            while not stop.is_set() and time.monotonic() < deadline:
                self._typing(chat_id, ilink.TYPING_START)
                # WeChat's indicator has a short life and the API does not say how
                # long. The user's own observation pinned it: the light shows right
                # after their message arrives and is already gone by the time the run
                # gets going — so re-arming on the old 45s cadence left it off most
                # of the time. Beat often; each beat is one API call and zero messages.
                stop.wait(8.0)
            self._typing(chat_id, ilink.TYPING_STOP)

        threading.Thread(target=loop, name=f"typing-{chat_id[-6:]}", daemon=True).start()

    def _stop_typing(self, chat_id: str, *, quiet: bool = False) -> None:
        stop = self._typing_loops.pop(chat_id, None)
        if stop is None:
            return
        stop.set()
        if not quiet:
            self._typing(chat_id, ilink.TYPING_STOP)

    def _agent_typing(self, bind, state: int) -> None:
        """An agent heartbeat: keep 「正在输入」 up while it works.

        Heartbeats never become WeChat messages — that is the point, since WeChat
        only allows a bot ~10 messages before the user replies again.
        """
        peer = bind.last_peer
        if not peer:
            return
        if state == ilink.TYPING_STOP:
            self._stop_typing(peer)
        else:
            self._start_typing(peer)      # 幂等：已在跑就重置那 30 分钟的上限

    # -- message handling ------------------------------------------------
    def _ingest_media(self, message: dict, sender: str) -> str:
        """Download inbound media, keep it locally, describe it for the agent.

        The agent receives plain text with the local path, so *every* kind of
        backend can use the attachment without speaking iLink media itself.
        """
        parts: list[str] = []
        folder = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in sender)[:48] or "peer"
        labels = {media.ITEM_IMAGE: "图片", media.ITEM_FILE: "文件",
                  media.ITEM_VIDEO: "视频", media.ITEM_VOICE: "语音"}
        for item in message.get("item_list") or []:
            info = media.parse_media_item(item)
            if not info:
                continue
            label = labels.get(info["item_type"], "媒体")
            name = info["filename"] or f"{label}-{int(time.time())}"
            if self.dry_run:
                parts.append(f"[{label}] {name}（dry-run：没有下载）")
                continue
            if self.client is None:
                parts.append(f"[{label}] {name}（网关还没绑定微信，下载不了）")
                continue
            try:
                target = ilink_media.download_media(self.client, info,
                                                    Path(self.cfg.data_dir) / "media" / folder,
                                                    name=name)
            except Exception as exc:  # noqa: BLE001 - a bad attachment must not kill the turn
                log.warning("media download failed from %s: %s", _safe(sender), exc)
                parts.append(f"[{label}] {name} 下载失败：{exc}")
                continue
            size = target.stat().st_size
            log.info("media from %s -> %s (%d bytes)", _safe(sender), target, size)
            lines = [f"[{label}] {target.name}（{size} 字节）",
                     f"本地路径（网关所在机器）：{target}"]
            if self.virtual:
                # A remote agent cannot read that path; give it a way to fetch the file.
                lines.append(f"下载地址：{self.virtual.media_url(folder, target.name)}"
                             f"（带你的 token 作 Bearer 认证）")
            parts.append("\n".join(lines))
        return "\n\n".join(parts).strip()

    def handle(self, message: dict) -> None:
        sender = ilink.sender_of(message)
        if not sender or (self.client and sender == self.client.account_id):
            return
        message_id = str(message.get("message_id") or "").strip()
        self._last_peer = sender
        self.store.set_last_peer(sender)
        text = ilink.message_text(message)
        if not text:
            text = self._ingest_media(message, sender)   # 图片/文件：落盘后把路径交给 agent
        if message_id and self.store.is_duplicate(f"id:{message_id}"):
            log.debug("duplicate message_id %s", _safe(message_id))
            return
        if text and self.store.is_duplicate(f"text:{sender}:{self.store.fingerprint(text)}"):
            log.debug("duplicate content from %s", _safe(sender))
            return
        # A message from the peer opens a fresh budget (WeChat's per-turn cap), and
        # whatever the previous turn could not deliver goes out first.
        self._turn_counts.pop(sender, None)
        pending = self._take_pending(sender)
        if pending:
            self._send(sender, pending)
        if not text:
            if not self.dry_run:
                self._send(sender, "这条消息里没有我能读的内容（文字、图片、文件都行）。")
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
        # 用户又开口了 = 上一轮结束了：「本轮全放行」到此收回。
        # 回答放行卡的那几条不算新的一轮，否则 /always 一句话就把自己关掉了。
        if decision.kind not in ("approve", "reject", "always"):
            self.approvals.clear_auto(sender)
        if decision.kind == "ignore":
            return
        if decision.kind == "reply":
            if not self.dry_run:
                self._send(sender, decision.text)
            else:
                print(f"[dry-run] {suggestion_prefix(decision)} {decision.text}")
            return

        if decision.kind in ("approve", "reject", "always"):
            self._resolve_approval(sender, decision)
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
        self._record("in", sender, agent.name, text)
        if self.cfg.delivery.ack and not self.dry_run:
            self._send(sender, self.cfg.delivery.ack_template.format(label=agent.display))
        # The agent answers whenever it is done; hold 「正在输入」 until then.
        self._start_typing(sender)

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
        reused = self.cfg.virtual.reuse_real_token_for
        self.virtual = VirtualILinkServer(
            host=self.cfg.virtual.host,
            port=self.cfg.virtual.port,
            data_dir=self.cfg.data_dir / "virtual",
            auto_approve=self.cfg.virtual.auto_approve,
            accept_tokens=self.cfg.virtual.reuse_tokens(self.cfg.account.token),
            on_outbound=self._forward_to_wechat,
            on_outbound_items=self._forward_media_to_wechat,
            on_typing=self._agent_typing,
            approvals=self.approvals,
            on_approval=self._approval_asked,
            bind_key=self.cfg.virtual.bind_key,
            public_url=self.cfg.virtual.public_url,
            allow_cidrs=self.cfg.virtual.allow_cidrs,
            admin_cidrs=self.cfg.virtual.admin_cidrs,
            on_log=log.info,
        )
        host, port = self.virtual.start()
        print(f"\n虚拟 iLink 已就绪：{self.virtual.base_url()}")
        print("  把 agent 的微信 base_url 指到这里，它就等于接上了微信（例：Hermes 用 WEIXIN_BASE_URL）")
        print(f"  待批准的接入：{self.virtual.base_url()}/admin/binds\n")
        print(f"  管理接口（批准/列表）允许的来源：{', '.join(self.virtual.admin_cidrs)}")
        if self.cfg.virtual.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"  ⚠️ 监听 {self.cfg.virtual.host}：{self.virtual.policy_hint()}\n")
        if reused:
            print(f"  已复用真 token 的 agent：{reused}（它原来的微信绑定不用改，直接指过来即可）\n")
        for agent in self.cfg.enabled_agents():
            if agent.type != "virtual":
                continue
            bind = self.virtual.bind_named(agent.name)
            state = f"已接入 {bind.account_id}" if bind else "还没接入（等它在自己那边发起扫码登录）"
            print(f"  · {agent.name:<10} {state}")
        self._start_dashboard()

    def _forward_to_wechat(self, bind, text: str) -> None:
        """A virtual agent answered: push it out through the real WeChat identity."""
        peer = bind.last_peer
        if not peer:
            log.warning("virtual agent %s answered %r before any peer was known", bind.name, text[:60])
            return
        self._stop_typing(peer)
        log.info("virtual: %s -> wechat %s (%d chars)", bind.name, _safe(peer), len(text))
        self._send(peer, text)

    def _budget_left(self, chat_id: str) -> int:
        return max(0, max(1, int(self.cfg.delivery.max_messages_per_turn))
                   - self._turn_counts.get(chat_id, 0))

    def _send_media_path(self, chat_id: str, path: Path) -> None:
        """Upload one local file to WeChat's CDN and send it as an attachment."""
        if self.dry_run or self.client is None:
            log.info("[dry-run] 附件 -> %s: %s", _safe(chat_id), path)
            print(f"[dry-run] 附件 -> {_safe(chat_id)}: {path}")
            return
        if self._budget_left(chat_id) <= 0:
            # No new state on purpose: the file is still on disk, and the note tells
            # the peer why nothing arrived.
            self._queue_pending(chat_id, f"（这一轮消息额度用完了，附件 {path.name} 没发出去）")
            log.warning("no message budget left for %s; attachment %s not sent", _safe(chat_id), path)
            return
        try:
            ilink_media.send_file(self.client, chat_id, path,
                                  context_token=self.client.context_token(chat_id))
            self._turn_counts[chat_id] = self._turn_counts.get(chat_id, 0) + 1
            log.info("attachment -> %s: %s (%d bytes)", _safe(chat_id), path.name, path.stat().st_size)
        except ilink.ILinkError as exc:
            log.error("attachment to %s failed: %s", _safe(chat_id), exc)

    # -- console (dashboard) ---------------------------------------------
    def _record(self, kind: str, peer: str, agent: str = "", text: str = "") -> None:
        """Keep the recent message flow so the console can show it live."""
        if self.traffic is not None:
            self.traffic.add(kind, peer, agent, text)

    def snapshot(self) -> dict:
        """Everything the console page shows, as one JSON-able dict."""
        agents = []
        for agent in self.cfg.enabled_agents():
            # Only a virtual agent has a bind; an a2a/http/exec agent is simply reachable.
            bind = (self.virtual.bind_named(agent.name)
                    if self.virtual and agent.type == "virtual" else None)
            agents.append({
                "name": agent.name, "label": agent.label, "type": agent.type,
                "bound": bool(bind), "prefix": agent.prefix,
                "account_id": bind.account_id if bind else "",
                "queue": len(bind.queue) if bind else 0,
                "last_seen": bind.last_seen if bind else 0,
            })
        virtual: dict = {"enabled": self.cfg.virtual.enabled}
        if self.virtual:
            binds = self.virtual.binds()
            virtual.update({
                "host": self.virtual.host, "port": self.virtual.port,
                "bind_key": bool(self.cfg.virtual.bind_key),
                "allow_cidrs": self.virtual.allow_cidrs,
                "admin_cidrs": self.virtual.admin_cidrs,
                "pending": [b for b in binds if not b.get("confirmed")],
                "binds": binds,
            })
        return {
            "running": True,
            "account": _safe(self.client.account_id if self.client else self.cfg.account.account_id),
            "approvals": self.approvals.snapshot(),
            "base_url": self.cfg.account.base_url,
            "virtual_url": self.virtual.base_url() if self.virtual else "",
            "default_agent": self.cfg.default_agent,
            "dry_run": self.dry_run,
            "agents": agents,
            "virtual": virtual,
            "delivery": {
                "max_messages_per_turn": self.cfg.delivery.max_messages_per_turn,
                "reserve_for_answer": self.cfg.delivery.reserve_for_answer,
                "max_chars_per_message": self.cfg.delivery.max_chars_per_message,
                "per_peer": {peer: {"used": used, "left": self._budget_left(peer)}
                             for peer, used in list(self._turn_counts.items())},
            },
            "pending_output": {peer: len(text) for peer, text in self._pending_output.items()},
        }

    def _start_dashboard(self) -> None:
        """Start the operator console (its own thread; loopback by default)."""
        if not self.cfg.dashboard.enabled:
            return
        from .dashboard import Dashboard, LogRing, TrafficRing

        self.log_ring = LogRing()
        self.log_ring.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("agent_gateway").addHandler(self.log_ring)
        self.traffic = TrafficRing()
        self.dashboard = Dashboard(
            snapshot=self.snapshot,
            approve=lambda qrcode, name: (self.virtual.approve(qrcode, name=name)
                                          if self.virtual else None),
            reject=lambda qrcode: (self.virtual.reject(qrcode) if self.virtual else False),
            may_admin=lambda ip: (self.virtual.may_admin(ip) if self.virtual
                                  else ip in ("127.0.0.1", "::1")),
            logs=self.log_ring, traffic=self.traffic,
            host=self.cfg.dashboard.host, port=self.cfg.dashboard.port,
            on_log=log.info,
        )
        try:
            self.dashboard.start()
        except OSError as exc:
            # The console is a nicety; a busy port must never take the gateway down.
            log.warning("console did not start (port %s busy?): %s", self.cfg.dashboard.port, exc)
            print(f"\n⚠️ 控制台没起来（端口 {self.cfg.dashboard.port} 被占用？）：{exc}")
            self.dashboard = None
            return
        print(f"\n控制台：{self.dashboard.base_url()}（浏览器打开；默认只允许本机访问）")

    # -- approvals: the agent asks, the human answers on the phone ------------
    def _approval_asked(self, bind, approval) -> None:
        """Put an agent's question on the phone, then leave it open.

        The agent is blocked in its own long poll, so this must be one message the
        user can act on — id, what it wants, and how long it waits.
        """
        # With /always on, the card was released the moment it was created — there
        # is nothing to ask, and a WeChat message saved is one more for the answer.
        if not approval.open:
            log.info("approval %s already released (allow-all), not asking %s",
                     _safe(approval.id), _safe(approval.peer or self._last_peer))
            return
        peer = approval.peer or self._last_peer
        if not peer:
            from .approvals import DENY
            self.approvals.resolve(approval.id, DENY, by="gateway",
                                   reason="没有可以问的联系人")
            log.warning("approval %s from %s has no peer to ask", _safe(approval.id), bind.name)
            return
        log.info("approval %s -> asking %s", _safe(approval.id), _safe(peer))
        self._send(peer, self.approvals.describe(approval))

    def _resolve_approval(self, sender: str, decision) -> None:
        """``/approve [id]``、``/reject [id]``、``/always`` 从手机上来。"""
        from .approvals import ALLOW, DENY, PENDING

        if decision.kind == "always":
            self._allow_all(sender)
            return

        wanted = (decision.text or "").strip().lstrip("/")
        approval = self.approvals.get(wanted) if wanted else self.approvals.newest_pending(sender)
        if approval is None:
            self._send(sender, f"没有这个编号（或已过期）：{wanted}" if wanted
                       else "现在没有等你点头的事。")
            return
        if approval.decision != PENDING:
            self._send(sender, self.approvals.verdict_text(approval))   # answered twice
            return
        self.approvals.resolve(approval.id,
                               ALLOW if decision.kind == "approve" else DENY,
                               by=sender)
        decided = self.approvals.get(approval.id) or approval
        log.info("approval %s -> %s (by %s)", _safe(decided.id), decided.decision, _safe(sender))
        self._send(sender, self.approvals.verdict_text(decided))

    def _allow_all(self, sender: str) -> None:
        """``/always``：这一轮剩下的问答不再逐条问。

        它是**用户明确说的一句话**，不是沉默——这正是它敢一次全放行的理由。
        但放行是宽的那个方向，所以收回的机制必须实在：用户下一句话就收回
        （``_handle_message`` 里的 ``clear_auto``），另有 always_window_seconds 兜底。
        """
        window = float(self.cfg.delivery.always_window_seconds)
        released = self.approvals.allow_all(sender, window=window, by=sender)
        log.info("allow-all by %s: released %s, window %.0fs", _safe(sender),
                 [a.id for a in released], window)
        head = "✅ 这一轮全放行"
        if released:
            head += "（一次放掉了 " + "、".join(a.id for a in released) + "）"
        minutes = max(1, int(round(window / 60)))
        self._send(sender, f"{head}。\n"
                           f"{minutes} 分钟内 agent 再要点头的事我直接放行，不再打断你；"
                           f"你下一句话一到就自动收回。")

    def _forward_media_to_wechat(self, bind, items: list) -> None:
        """A virtual agent wants to send files: the gateway does the real upload.

        The agent marks a local file as ``localpath:<abs>``; the bytes leave this
        machine only encrypted, on their way to WeChat's CDN.
        """
        peer = bind.last_peer
        if not peer:
            log.warning("virtual agent %s sent media before any peer was known", bind.name)
            return
        self._stop_typing(peer)
        for item in items:
            info = media.parse_media_item(item)
            if not info:
                continue
            source = info["encrypt_query_param"]
            if source.startswith("localpath:"):
                path = Path(source[len("localpath:"):])
            elif source.startswith("blob:"):
                # A remote agent uploaded the bytes to us; keep the accounting honest.
                path = self.virtual.blob_path(source) if self.virtual else None
                if path is None:
                    log.warning("virtual: %s sent blob %s the gateway no longer has",
                                bind.name, source[:24])
                    continue
            else:
                log.warning("virtual: %s sent media this gateway cannot read (%s)",
                            bind.name, source[:48])
                continue
            if not path.is_file():
                log.warning("virtual: %s pointed at a missing file %s", bind.name, path)
                continue
            log.info("virtual: %s -> wechat %s attachment %s", bind.name, _safe(peer), path.name)
            self._send_media_path(peer, path)

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
        if self.dashboard:
            self.dashboard.stop()
            self.dashboard = None
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

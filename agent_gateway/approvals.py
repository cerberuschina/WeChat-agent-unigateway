"""Asking the human, from inside an agent run.

A coding agent that needs a decision mid-task has no way to ask: on a phone-only
setup it dies with "requires approval" and finishes having done nothing. This
broker turns that dead end into a message the user can answer from WeChat —
``/approve <id>`` — while the agent waits on its long poll.

Design notes
* ids are short and speakable; the user retypes them on a phone keyboard.
* every request has a TTL and **expires to deny**: silence must never mean yes.
* pure logic — no HTTP, no WeChat — so both halves stay testable offline.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

PENDING = "pending"
ALLOW = "allow"
DENY = "deny"
EXPIRED = "expired"

# No 0/o/1/l/i: these ids get read off a phone screen and typed back by hand.
ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"

TITLES = {
    "command": "要执行一条命令",
    "edit": "要改一个文件",
    "write": "要新建一个文件",
    "fetch": "要联网取东西",
    "other": "要做一步需要你点头的事",
}


def short_id(counter: int) -> str:
    """Deterministic, speakable id (`k7`, `2m4`…) from a running counter."""
    out = ""
    n = max(0, counter)
    while True:
        out = ALPHABET[n % len(ALPHABET)] + out
        n //= len(ALPHABET)
        if n == 0:
            return out


@dataclass
class Approval:
    """One question waiting for the user."""

    id: str
    agent: str
    peer: str
    title: str
    detail: str = ""
    kind: str = "command"
    decision: str = PENDING
    reason: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    decided_at: float = 0.0
    decided_by: str = ""

    @property
    def open(self) -> bool:
        return self.decision == PENDING

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "agent": self.agent,
            "peer": self.peer,
            "title": self.title,
            "detail": self.detail,
            "kind": self.kind,
            "decision": self.decision,
            "reason": self.reason,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
        }


class ApprovalBroker:
    """Thread-safe registry of pending questions."""

    def __init__(self, *, ttl: float = 180.0, clock: Callable[[], float] = time.time,
                 max_history: int = 500) -> None:
        self.ttl = float(ttl)
        self._clock = clock
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._items: Dict[str, Approval] = {}
        self._order: List[str] = []
        self._counter = 0
        self._max_history = max_history

    # ---------------------------------------------------------------- writing

    def request(self, *, agent: str, peer: str, title: str, detail: str = "",
                kind: str = "command", ttl: Optional[float] = None) -> Approval:
        now = self._clock()
        with self._cv:
            self._counter += 1
            approval = Approval(
                id=short_id(self._counter), agent=agent, peer=peer, title=title,
                detail=detail, kind=kind, created_at=now,
                expires_at=now + float(ttl if ttl is not None else self.ttl),
            )
            while approval.id in self._items:          # counter wraps after a restart
                self._counter += 1
                approval = Approval(
                    id=short_id(self._counter), agent=agent, peer=peer, title=title,
                    detail=detail, kind=kind, created_at=now,
                    expires_at=now + float(ttl if ttl is not None else self.ttl),
                )
            self._items[approval.id] = approval
            self._order.append(approval.id)
            while len(self._order) > self._max_history:
                self._items.pop(self._order.pop(0), None)
            return approval

    def resolve(self, approval_id: str, decision: str, *, by: str = "user",
                reason: str = "") -> Optional[Approval]:
        """Record a verdict. Returns None when the id is unknown or already closed."""
        if decision not in (ALLOW, DENY):
            raise ValueError(f"decision must be {ALLOW!r} or {DENY!r}, not {decision!r}")
        with self._cv:
            approval = self._items.get((approval_id or "").strip().lstrip("/").lower())
            if approval is None or not approval.open:
                return None
            approval.decision = decision
            approval.reason = reason
            approval.decided_at = self._clock()
            approval.decided_by = by
            self._cv.notify_all()
            return approval

    def expire(self) -> List[Approval]:
        """Close everything past its TTL — as a *deny*, never an implicit yes."""
        now = self._clock()
        closed: List[Approval] = []
        with self._cv:
            for approval in self._items.values():
                if approval.open and approval.expires_at and now >= approval.expires_at:
                    approval.decision = EXPIRED
                    approval.reason = "超时没回，当成拒绝"
                    approval.decided_at = now
                    approval.decided_by = "timeout"
                    closed.append(approval)
            if closed:
                self._cv.notify_all()
        return closed

    # ---------------------------------------------------------------- reading

    def get(self, approval_id: str) -> Optional[Approval]:
        self.expire()
        with self._lock:
            return self._items.get((approval_id or "").strip().lstrip("/").lower())

    def pending(self, peer: Optional[str] = None) -> List[Approval]:
        self.expire()
        with self._lock:
            return [a for a in self._items.values()
                    if a.open and (peer is None or a.peer == peer)]

    def newest_pending(self, peer: Optional[str] = None) -> Optional[Approval]:
        """For a bare ``/approve`` with no id: the question this peer saw last."""
        items = self.pending(peer)
        return max(items, key=lambda a: a.created_at) if items else None

    def wait(self, approval_id: str, timeout: Optional[float] = None) -> Optional[Approval]:
        """Block until the question is answered (or the wait times out)."""
        deadline = None if timeout is None else self._clock() + float(timeout)
        with self._cv:
            while True:
                self.expire()
                approval = self._items.get((approval_id or "").strip().lstrip("/").lower())
                if approval is None or not approval.open:
                    return approval
                remaining = None if deadline is None else deadline - self._clock()
                if remaining is not None and remaining <= 0:
                    return approval
                self._cv.wait(min(remaining, 1.0) if remaining is not None else 1.0)

    # -------------------------------------------------------------- rendering

    def describe(self, approval: Approval) -> str:
        """The question as the user sees it on the phone."""
        head = TITLES.get(approval.kind, TITLES["other"])
        left = max(0, int(round(approval.expires_at - self._clock())))
        lines = [f"⏸️ 「{approval.agent}」{head}，等你一句话：", ""]
        if approval.title:
            lines.append(approval.title.strip())
        if approval.detail:
            body = approval.detail.strip()
            if len(body) > 1200:
                body = body[:1200] + "\n…（截断）"
            lines += ["", "```", body, "```"]
        lines += ["", f"回 /approve {approval.id} 放行，/reject {approval.id} 拒绝。",
                  f"（{left} 秒不回就算拒绝，我不会自己往下做。）"]
        return "\n".join(lines)

    def verdict_text(self, approval: Approval) -> str:
        """What the user gets back after answering (so the tap feels acknowledged)."""
        if approval.decision == ALLOW:
            return f"✅ {approval.id} 已放行，{approval.agent} 继续了。"
        if approval.decision == DENY:
            return f"⛔ {approval.id} 已拒绝，{approval.agent} 会换个做法或停下。"
        if approval.decision == EXPIRED:
            return f"⌛️ {approval.id} 超时作废（当成拒绝）。"
        return f"…{approval.id} 还没回。"

    def snapshot(self, limit: int = 20) -> List[Dict[str, object]]:
        self.expire()
        with self._lock:
            ids = self._order[-limit:]
            return [self._items[i].as_dict() for i in reversed(ids) if i in self._items]

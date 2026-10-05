"""Small state store: per-chat sticky agent + inbound dedup.

JSON on disk, one file each, atomic writes. Nothing here needs a database and
the whole thing is a few kilobytes.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        pass
    return default


class StateStore:
    """Sticky agent per chat + a TTL cache for duplicate inbound messages."""

    def __init__(self, data_dir: str | os.PathLike[str], *, dedup_ttl: float = 300.0):
        self.dir = Path(data_dir).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        self._sticky_path = self.dir / "chats.json"
        self._dedup_path = self.dir / "dedup.json"
        self._dedup_ttl = dedup_ttl
        self._lock = threading.Lock()
        self._sticky: Dict[str, str] = _read_json(self._sticky_path, {}) or {}
        self._seen: Dict[str, float] = {k: float(v) for k, v in (_read_json(self._dedup_path, {}) or {}).items()}

    # -- sticky agent ----------------------------------------------------
    def sticky(self, chat_id: str) -> Optional[str]:
        return self._sticky.get(chat_id)

    def set_sticky(self, chat_id: str, agent: str) -> None:
        with self._lock:
            self._sticky[chat_id] = agent
            _atomic_write(self._sticky_path, self._sticky)

    # -- dedup -----------------------------------------------------------
    @staticmethod
    def fingerprint(*parts: str) -> str:
        return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()

    def is_duplicate(self, key: str) -> bool:
        """True when this key was seen inside the TTL window."""
        now = time.time()
        with self._lock:
            self._prune(now)
            if key in self._seen:
                return True
            self._seen[key] = now
            # Persist sparingly: dedup is best-effort across restarts.
            if len(self._seen) % 20 == 0:
                _atomic_write(self._dedup_path, self._seen)
            return False

    def _prune(self, now: float) -> None:
        expired = [k for k, ts in self._seen.items() if now - ts > self._dedup_ttl]
        for key in expired:
            self._seen.pop(key, None)

    def flush(self) -> None:
        with self._lock:
            _atomic_write(self._dedup_path, self._seen)

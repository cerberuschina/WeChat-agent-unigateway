"""统一记忆：属于"聊天"而不是设备或 agent 的长期记忆。

设计见 ``docs/MEMORY-DESIGN.md``。这里实现 P1：

* L0 原始对话（截断、脱敏、按天过期）
* L1 事实 / L2 场景 / L3 画像 / 任务
* 每联系人一份（``scope=peer:<hash>``），外加全局公共层（``scope=global``）
* SQLite + FTS5 检索（没有 FTS5 时退回 LIKE），可选 embedding 留给 P2
* Markdown 镜像：``data/memory/peers/<hash>/*.md``，可读、画像可直接改
* 注入用的一段 Memory Brief

原则：记忆出任何问题都只记日志，不让消息链路失败。
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

L0_TRUNCATE = 500
MAX_ITEM_CHARS = 1000
DEFAULT_MAX_CHARS = 800
MEMORY_HEADER = "【记忆｜本聊天】"
MESSAGE_HEADER = "【消息】"

LAYER_LABELS = {"l0": "对话", "l1": "事实", "l2": "场景", "l3": "画像", "task": "任务"}

# 明确的长期信息才抽成事实；普通闲聊只进 L0，交给检索的新近度排。
_FACT_MARKERS = ("记住", "以后都", "以后要", "以后别", "别忘了", "我叫", "我是",
                 "我的偏好", "偏好是", "不要", "必须", "地址是", "密码是", "路径是",
                 "用户名是", "部署在", "默认用")

_REDACTIONS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{10,}"), "[REDACTED]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer [REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED PRIVATE KEY]"),
]


def now() -> float:
    return time.time()


def age_text(ts: float) -> str:
    delta = max(0.0, now() - float(ts or 0))
    if delta < 90:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400 * 2:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def redact(text: str, extra_patterns: Sequence[str] = ()) -> str:
    out = text or ""
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    for pattern in extra_patterns or ():
        try:
            out = re.sub(pattern, "[REDACTED]", out)
        except re.error:
            continue
    return out


def extract_facts(text: str) -> List[str]:
    """规则抽取：一句话里有明确的"长期"标记才记，问句不记。"""
    line = (text or "").strip()
    if not line or line.startswith(("/", "／", "!")):
        return []
    if line.endswith(("?", "？")):
        return []
    if not 6 <= len(line) <= 300:
        return []
    if not any(marker in line for marker in _FACT_MARKERS):
        return []
    return [line]


def peer_id(sender: str, salt: str) -> str:
    digest = hashlib.sha1(f"{salt}\x1f{sender}".encode("utf-8")).hexdigest()
    return digest[:16]


def _fingerprint(scope: str, text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    return hashlib.sha1(f"{scope}\x1f{normalized}".encode("utf-8")).hexdigest()


@dataclass
class MemoryItem:
    id: str
    scope: str
    layer: str
    kind: str = ""
    text: str = ""
    tags: List[str] = field(default_factory=list)
    source: str = "auto"
    agent: str = ""
    instance: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: Optional[float] = None
    supersedes: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "scope": self.scope, "layer": self.layer, "kind": self.kind,
            "text": self.text, "tags": list(self.tags), "source": self.source,
            "agent": self.agent, "instance": self.instance,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "expires_at": self.expires_at, "supersedes": self.supersedes,
        }


class BuiltinStore:
    """SQLite + FTS5。一条连接 + 一把锁：网关的并发度不需要池。"""

    def __init__(self, path: Path, *, l0_ttl_days: float = 30.0,
                 redact_patterns: Sequence[str] = (), log=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.l0_ttl_days = float(l0_ttl_days or 0)
        self.redact_patterns = list(redact_patterns or ())
        self._log = log or (lambda _m: None)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=3000")
        self._fts = False
        self._create_schema()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self.prune()

    # -- schema ----------------------------------------------------------
    def _create_schema(self) -> None:
        with self._lock, self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS items(
                  id TEXT PRIMARY KEY,
                  scope TEXT NOT NULL,
                  layer TEXT NOT NULL,
                  kind TEXT DEFAULT '',
                  text TEXT NOT NULL,
                  tags TEXT DEFAULT '',
                  source TEXT DEFAULT 'auto',
                  agent TEXT DEFAULT '',
                  instance TEXT DEFAULT '',
                  fingerprint TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  expires_at REAL,
                  supersedes TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS items_scope ON items(scope, layer);
                CREATE UNIQUE INDEX IF NOT EXISTS items_fp ON items(scope, fingerprint)
                  WHERE fingerprint IS NOT NULL;
            """)
            for statement in (
                "CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5("
                "text, tags, content='items', content_rowid='rowid', tokenize='trigram')",
                "CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5("
                "text, tags, content='items', content_rowid='rowid')",
            ):
                try:
                    self._db.execute(statement)
                except sqlite3.OperationalError:
                    continue
                self._fts = True
                break
            if self._fts:
                try:
                    self._db.executescript("""
                        CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
                          INSERT INTO items_fts(rowid, text, tags)
                          VALUES (new.rowid, new.text, new.tags);
                        END;
                        CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
                          INSERT INTO items_fts(items_fts, rowid, text, tags)
                          VALUES ('delete', old.rowid, old.text, old.tags);
                        END;
                        CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
                          INSERT INTO items_fts(items_fts, rowid, text, tags)
                          VALUES ('delete', old.rowid, old.text, old.tags);
                          INSERT INTO items_fts(rowid, text, tags)
                          VALUES (new.rowid, new.text, new.tags);
                        END;
                    """)
                except sqlite3.OperationalError as exc:   # pragma: no cover
                    self._log(f"memory: FTS 触发器没建起来：{exc}")
                    self._fts = False

    # -- write -----------------------------------------------------------
    def write(self, *, scope: str, layer: str, text: str, kind: str = "",
              tags: Sequence[str] = (), source: str = "auto",
              agent: str = "", instance: str = "",
              expires_at: Optional[float] = None, supersedes: str = "") -> Optional[MemoryItem]:
        clean = redact((text or "").strip(), self.redact_patterns)[:MAX_ITEM_CHARS]
        if not clean:
            return None
        fp = None if layer == "l0" else _fingerprint(scope, clean)
        stamp = now()
        if expires_at is None and layer == "l0" and self.l0_ttl_days > 0:
            expires_at = stamp + self.l0_ttl_days * 86400
        tags_text = ",".join(sorted({str(t).strip() for t in tags if str(t).strip()}))
        with self._lock, self._db:
            item_id = f"m-{secrets.token_hex(3)}"
            try:
                self._db.execute(
                    "INSERT INTO items(id, scope, layer, kind, text, tags, source, agent,"
                    " instance, fingerprint, created_at, updated_at, expires_at, supersedes)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item_id, scope, layer, kind, clean, tags_text, source, agent, instance,
                     fp, stamp, stamp, expires_at, supersedes))
            except sqlite3.IntegrityError:
                row = self._db.execute(
                    "SELECT * FROM items WHERE scope=? AND fingerprint=?", (scope, fp)).fetchone()
                if row is None:
                    return None
                self._db.execute("UPDATE items SET updated_at=? WHERE id=?", (stamp, row["id"]))
                if layer == "task" and tags_text:
                    self._db.execute("UPDATE items SET tags=?, updated_at=? WHERE id=?",
                                     (tags_text, stamp, row["id"]))
                    data = dict(row)
                    data["tags"] = tags_text
                    data["updated_at"] = stamp
                    return self._to_item(data)
                data = dict(row)
                data["updated_at"] = stamp
                return self._to_item(data)
        return MemoryItem(id=item_id, scope=scope, layer=layer, kind=kind, text=clean,
                          tags=[t for t in tags_text.split(",") if t], source=source,
                          agent=agent, instance=instance, created_at=stamp,
                          updated_at=stamp, expires_at=expires_at, supersedes=supersedes)

    @staticmethod
    def _to_item(row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=str(row["id"]), scope=str(row["scope"]), layer=str(row["layer"]),
            kind=str(row["kind"] or ""), text=str(row["text"] or ""),
            tags=[t for t in str(row["tags"] or "").split(",") if t],
            source=str(row["source"] or ""), agent=str(row["agent"] or ""),
            instance=str(row["instance"] or ""),
            created_at=float(row["created_at"] or 0), updated_at=float(row["updated_at"] or 0),
            expires_at=row["expires_at"], supersedes=str(row["supersedes"] or ""))

    # -- read ------------------------------------------------------------
    @staticmethod
    def _like_terms(query: str) -> List[str]:
        """关键词拆法：中文按二元组，英文按空白分词。"""
        query = (query or "").strip()
        if not query:
            return []
        if any("\u4e00" <= ch <= "\u9fff" for ch in query):
            chars = [ch for ch in re.sub(r"\s+", "", query) if "\u4e00" <= ch <= "\u9fff"]
            grams = {a + b for a, b in zip(chars, chars[1:])}
            return sorted(grams) or [query]
        return [t for t in re.split(r"\s+", query) if t]

    def search(self, query: str, scopes: Sequence[str], *, layers: Optional[Sequence[str]] = None,
               k: int = 8, budget: int = 1200) -> List[MemoryItem]:
        scopes = [s for s in scopes if s]
        if not scopes:
            return []
        where = [f"i.scope IN ({','.join('?' * len(scopes))})"]
        args: List[Any] = list(scopes)
        if layers:
            where.append(f"i.layer IN ({','.join('?' * len(layers))})")
            args.extend(layers)
        where.append("(i.expires_at IS NULL OR i.expires_at > ?)")
        args.append(now())
        query = (query or "").strip()
        rows: List[sqlite3.Row] = []
        with self._lock:
            if query and self._fts:
                terms = [t for t in re.split(r"\s+", query) if t][:6]
                match = " OR ".join(f'"{t.replace(chr(34), "")}"' for t in terms)
                try:
                    rows = self._db.execute(
                        "SELECT i.*, bm25(items_fts) AS rank FROM items_fts"
                        " JOIN items i ON i.rowid = items_fts.rowid"
                        f" WHERE items_fts MATCH ? AND {' AND '.join(where)}"
                        " ORDER BY rank LIMIT ?", [match, *args, max(k * 3, k)]).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                # FTS 对中文长句很挑（trigram 要 3 字以上且是子串），退回二元组 LIKE。
                terms = self._like_terms(query) if query else []
                clause = list(where)
                like_args: List[Any] = []
                if terms:
                    clause.append("(" + " OR ".join(["i.text LIKE ?"] * len(terms)) + ")")
                    like_args = [f"%{t}%" for t in terms]
                rows = self._db.execute(
                    f"SELECT i.* FROM items i WHERE {' AND '.join(clause)}"
                    " ORDER BY i.updated_at DESC LIMIT ?",
                    [*args, *like_args, max(k * 3, k) if query else k]).fetchall()
        items = [self._to_item(row) for row in rows]
        if query and items:
            terms = self._like_terms(query)
            items.sort(key=lambda it: (-sum(t in it.text for t in terms), -it.updated_at))
        out: List[MemoryItem] = []
        used = 0
        for item in items:
            if used + len(item.text) > budget and out:
                break
            out.append(item)
            used += len(item.text)
            if len(out) >= k:
                break
        return out

    def recent(self, scope: str, *, n: int = 4, layers: Sequence[str] = ("l0",)) -> List[MemoryItem]:
        placeholders = ",".join("?" * len(layers))
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM items WHERE scope=? AND layer IN (" + placeholders + ")"
                " AND (expires_at IS NULL OR expires_at > ?)"
                " ORDER BY created_at DESC LIMIT ?", [scope, *layers, now(), n]).fetchall()
        return [self._to_item(row) for row in rows]

    def latest_tasks(self, scope: str, *, n: int = 2) -> List[MemoryItem]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM items WHERE scope=? AND layer='task'"
                " ORDER BY updated_at DESC LIMIT ?", (scope, n)).fetchall()
        return [self._to_item(row) for row in rows]

    def list_items(self, scope: str, *, layer: Optional[str] = None,
                   limit: int = 200) -> List[MemoryItem]:
        sql = "SELECT * FROM items WHERE scope=?"
        args: List[Any] = [scope]
        if layer:
            sql += " AND layer=?"
            args.append(layer)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._to_item(row) for row in rows]

    # -- delete ----------------------------------------------------------
    def forget(self, *, scopes: Sequence[str], item_id: str = "",
               query: str = "") -> int:
        scopes = [s for s in scopes if s]
        if not scopes:
            return 0
        if item_id:
            with self._lock, self._db:
                cur = self._db.execute(
                    "DELETE FROM items WHERE id=? AND scope IN (" +
                    ",".join("?" * len(scopes)) + ")", [item_id, *scopes])
                return cur.rowcount
        if not query:
            return 0
        targets = self.search(query, scopes, k=50, budget=100000)
        if not targets:
            return 0
        ids = [it.id for it in targets]
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM items WHERE id IN (" + ",".join("?" * len(ids)) + ")", ids)
            return cur.rowcount

    def prune(self) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM items WHERE layer='l0' AND expires_at IS NOT NULL AND expires_at < ?",
                (now(),))
            return cur.rowcount

    def stats(self, scopes: Sequence[str]) -> Dict[str, Any]:
        scopes = [s for s in scopes if s]
        if not scopes:
            return {"total": 0, "by_layer": {}}
        marks = ",".join("?" * len(scopes))
        with self._lock:
            rows = self._db.execute(
                f"SELECT layer, COUNT(*) AS n FROM items WHERE scope IN ({marks}) GROUP BY layer",
                scopes).fetchall()
            tasks = self._db.execute(
                f"SELECT COUNT(*) AS n FROM items WHERE scope IN ({marks}) AND layer='task'",
                scopes).fetchone()["n"]
        by_layer = {str(row["layer"]): int(row["n"]) for row in rows}
        return {"total": sum(by_layer.values()), "by_layer": by_layer, "tasks": int(tasks)}

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass


class MemoryHub:
    """配置 + 作用域 + 注入 + 命令 + Markdown 镜像，包住 BuiltinStore。"""

    def __init__(self, cfg, base_data_dir, *, log=None):
        self.cfg = cfg
        if log is None:
            self._log = lambda _m: None
        elif callable(log):
            self._log = log
        else:                       # a logging.Logger
            self._log = log.info
        self.enabled = bool(getattr(cfg, "enabled", False))
        if not self.enabled:
            self.store = None
            return
        data_dir = Path(getattr(cfg, "data_dir", "") or (Path(base_data_dir) / "memory"))
        if not data_dir.is_absolute():
            data_dir = Path(base_data_dir) / data_dir
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.salt = self._load_salt(getattr(cfg, "salt", ""))
        self.store = BuiltinStore(self.dir / "memory.db",
                                  l0_ttl_days=getattr(cfg, "l0_ttl_days", 30.0),
                                  redact_patterns=getattr(cfg, "redact_patterns", ()),
                                  log=self._log)
        self._import_all()
        self._log(f"memory: ready at {self.dir} (backend={getattr(cfg, 'backend', 'builtin')})")

    # -- init helpers ----------------------------------------------------
    def _load_salt(self, spec: str) -> str:
        spec = str(spec or "").strip()
        if spec.startswith("file:"):
            path = Path(spec[len("file:"):]).expanduser()
            if not path.is_absolute():
                path = self.dir / path
        elif spec:
            return spec
        else:
            path = self.dir / "salt.txt"
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
        salt = secrets.token_hex(16)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(salt + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return salt

    def scope_for(self, sender: str) -> str:
        return f"peer:{peer_id(sender, self.salt)}" if sender else ""

    def close(self) -> None:
        if self.store:
            self.store.close()

    # -- writing ---------------------------------------------------------
    def record_inbound(self, sender: str, agent: str, instance: str, text: str) -> None:
        self._record_l0(sender, agent, instance, text, kind="user")
        if not sender:
            return
        for fact in extract_facts(text):
            self.write_fact(sender, fact, source="auto", agent=agent, instance=instance,
                            tags=("auto-extract",))

    def record_reply(self, sender: str, agent: str, instance: str, text: str) -> None:
        self._record_l0(sender, agent, instance, text, kind="agent")
        if not sender:
            return
        for fact in extract_facts(text):
            self.write_fact(sender, fact, source="auto", agent=agent, instance=instance,
                            tags=("auto-extract",))

    def _record_l0(self, sender: str, agent: str, instance: str, text: str, *, kind: str) -> None:
        if not self.enabled or not sender or not self.store:
            return
        body = (text or "").strip()
        if body.startswith(MEMORY_HEADER):     # 注入块自己不该被记回来
            return
        try:
            self.store.write(scope=self.scope_for(sender), layer="l0", kind=kind,
                             text=body[:L0_TRUNCATE], source="auto",
                             agent=agent, instance=instance)
        except Exception as exc:  # noqa: BLE001 - 记忆失败不能拖垮消息
            self._log(f"memory: L0 write failed: {exc}")

    def write_fact(self, sender: str, text: str, *, source: str = "user", agent: str = "",
                   instance: str = "", tags: Sequence[str] = (),
                   global_: bool = False, layer: str = "l1", kind: str = "") -> Optional[MemoryItem]:
        if not self.enabled or not self.store:
            return None
        scope = "global" if global_ else self.scope_for(sender)
        if not scope:
            return None
        try:
            item = self.store.write(scope=scope, layer=layer, kind=kind or ("global" if global_ else ""),
                                    text=text, tags=tags, source=source,
                                    agent=agent, instance=instance)
        except Exception as exc:  # noqa: BLE001
            self._log(f"memory: write failed: {exc}")
            return None
        if item:
            self._mirror(sender, global_=global_)
        return item

    def write_task(self, sender: str, text: str, *, status: str = "doing",
                   source: str = "user", agent: str = "", instance: str = "") -> Optional[MemoryItem]:
        item = self.write_fact(sender, text, source=source, agent=agent, instance=instance,
                               tags=(f"status:{status}",), layer="task", kind="task")
        return item

    # -- reading / injection ---------------------------------------------
    def build_brief(self, sender: str, query: str = "") -> str:
        if not self.enabled or not self.store or not sender:
            return ""
        if not bool(getattr(self.cfg, "inject_enabled", True)):
            return ""
        budget = int(getattr(self.cfg, "inject_max_chars", DEFAULT_MAX_CHARS) or 0)
        if budget <= 0:
            return ""
        scope = self.scope_for(sender)
        out: List[str] = [MEMORY_HEADER]
        used = len(MEMORY_HEADER)

        def add(block: List[str]) -> bool:
            nonlocal used
            text = "\n".join(block)
            if used + len(text) > budget:
                return False
            out.extend(block)
            used += len(text) + 1
            return True

        tasks = [t for t in self.store.latest_tasks(scope, n=6)
                 if "status:done" not in t.tags][:2]
        if tasks:
            block = []
            for task in tasks:
                status = next((t.split(":", 1)[1] for t in task.tags
                               if t.startswith("status:")), "doing")
                block.append(f"任务：{task.text[:160]}（{status}，{task.id}）")
            add(block)

        recent = self.store.recent(scope, n=int(getattr(self.cfg, "inject_recent_turns", 4) or 0))
        if recent:
            block = ["最近："]
            for item in reversed(recent):
                who = "用户" if item.kind == "user" else (item.agent or "agent")
                if item.instance:
                    who += f"@{item.instance}"
                line = f"- {who}：{item.text[:160]}"
                if used + len("\n".join(block + [line])) > budget - 120:
                    break
                block.append(line)
            if len(block) > 1:
                add(block)

        remaining = max(0, budget - used - 20)
        if remaining > 40:
            facts = self.store.search(query or "", [scope], layers=("l1", "l2", "l3"),
                                      k=int(getattr(self.cfg, "inject_facts", 6) or 0),
                                      budget=remaining)
            globals_ = self.store.search(query or "", ["global"], layers=("l1", "l2", "l3"),
                                         k=3, budget=remaining)
            block = ["相关事实："]
            for item in facts:
                block.append(f"- {item.text[:160]}（{item.id}）")
            for item in globals_:
                block.append(f"- [全局] {item.text[:160]}（{item.id}）")
            if len(block) > 1:
                add(block)
        if len(out) <= 1:
            return ""
        return "\n".join(out)

    def inject(self, sender: str, text: str) -> str:
        brief = self.build_brief(sender, text)
        if not brief:
            return text
        return f"{brief}\n{MESSAGE_HEADER}{text}"

    # -- commands --------------------------------------------------------
    def overview(self, sender: str) -> str:
        if not self.enabled or not self.store:
            return "记忆没有开启（gateway.json 的 memory.enabled）。"
        scope = self.scope_for(sender)
        stats = self.store.stats([scope, "global"])
        by = stats["by_layer"]
        lines = ["记忆概览：",
                 f"· 本聊天：对话 {by.get('l0', 0)} 条、事实 {by.get('l1', 0)} 条、"
                 f"场景 {by.get('l2', 0)} 条、画像 {by.get('l3', 0)} 条",
                 f"· 任务：{by.get('task', 0)} 条"]
        tasks = self.store.latest_tasks(scope, n=3)
        for task in tasks:
            status = next((t.split(":", 1)[1] for t in task.tags
                           if t.startswith("status:")), "doing")
            lines.append(f"  - {task.text[:120]}（{status}，{task.id}）")
        glob = self.store.stats(["global"])
        lines.append(f"· 全局公共层：{glob['total']} 条（事实 {glob['by_layer'].get('l1', 0)}）")
        lines.append("命令：/remember、/task、/recall 关键词、/forget id、/memory reload")
        return "\n".join(lines)

    def recall(self, sender: str, query: str) -> str:
        if not self.enabled or not self.store:
            return "记忆没有开启。"
        if not query.strip():
            return "用法：/recall 关键词"
        items = self.store.search(query, [self.scope_for(sender), "global"],
                                  k=6, budget=1500)
        if not items:
            return f"没找到和「{query}」有关的记忆。"
        lines = [f"和「{query}」有关的记忆："]
        for item in items:
            scope = "全局" if item.scope == "global" else LAYER_LABELS.get(item.layer, item.layer)
            lines.append(f"· [{item.id}|{scope}] {item.text[:180]}")
        return "\n".join(lines)

    def remember(self, sender: str, text: str, *, global_: bool = False) -> str:
        if not self.enabled:
            return "记忆没有开启。"
        body = text.strip()
        if not body:
            return "用法：/remember [--global] 要记住的内容"
        item = self.write_fact(sender, body, source="user", global_=global_)
        if not item:
            return "这条没记下来（内容为空或写入失败，看网关日志）。"
        where = "全局公共层" if global_ else "本聊天"
        return f"已记下（{where}，{item.id}）：{body[:100]}"

    def forget(self, sender: str, key: str) -> str:
        if not self.enabled or not self.store:
            return "记忆没有开启。"
        key = key.strip()
        if not key:
            return "用法：/forget <记忆 id 或关键词>"
        scopes = [self.scope_for(sender), "global"]
        if key.startswith("m-"):
            removed = self.store.forget(scopes=scopes, item_id=key)
        else:
            removed = self.store.forget(scopes=scopes, query=key)
        self._mirror(sender)
        if not removed:
            return f"没找到要删的记忆：{key}"
        return f"已删除 {removed} 条记忆（{key}）。"

    def task_command(self, sender: str, arg: str) -> str:
        if not self.enabled:
            return "记忆没有开启。"
        body = arg.strip()
        if not body:
            return "用法：/task 内容 ｜ /task done 或 /task blocked"
        lowered = body.lower()
        status = None
        for word, value in (("done", "done"), ("完成", "done"), ("blocked", "blocked"),
                            ("卡住", "blocked"), ("doing", "doing"), ("进行", "doing")):
            if lowered == word or lowered.startswith(word + " "):
                status = value
                body = body[len(word):].strip()
                break
        scope = self.scope_for(sender)
        if status and not body:
            tasks = self.store.latest_tasks(scope, n=1)
            if not tasks:
                return "现在没有进行中的任务。"
            item = tasks[0]
            updated = self.store.write(scope=scope, layer="task", kind="task",
                                       text=item.text, tags=(f"status:{status}",),
                                       source="user")
            self._mirror(sender)
            label = {"done": "已完成", "blocked": "已标记卡住", "doing": "进行中"}[status]
            return f"{label}（{updated.id if updated else item.id}）：{item.text[:100]}"
        item = self.write_task(sender, body, status=status or "doing")
        if not item:
            return "任务没记下来（看网关日志）。"
        return f"任务已记录（{item.id}）：{body[:100]}"

    def reload(self, sender: str) -> str:
        if not self.enabled:
            return "记忆没有开启。"
        imported = self._import_all(scope=self.scope_for(sender))
        return f"已从 Markdown 导入 {imported} 条（人改的优先）。" if imported else \
            "Markdown 没有新的改动。"

    # -- agent API -------------------------------------------------------
    def api(self, path: str, *, allowed_peer: bool, can_global: bool,
            payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled or not self.store:
            return {"ret": -1, "errmsg": "memory is disabled"}
        peer = str(payload.get("peer") or "").strip()
        scope = self.scope_for(peer) if (peer and allowed_peer) else ""
        scopes = ([scope] if scope else []) + (["global"] if can_global or path == "search" else [])
        if path == "stats":
            return {"ret": 0, "stats": self.store.stats([scope, "global"] if scope else ["global"])}
        if path == "search":
            query = str(payload.get("q") or payload.get("query") or "")
            scopes = ([scope] if scope else []) + ["global"]
            items = self.store.search(query, scopes,
                                      k=int(payload.get("k") or 8),
                                      budget=int(payload.get("budget") or 1200))
            return {"ret": 0, "items": [i.as_dict() for i in items]}
        if path == "write":
            text = str(payload.get("text") or "")
            global_ = bool(payload.get("scope") == "global")
            if global_ and not can_global:
                return {"ret": -1, "errmsg": "this device may not write the global layer"}
            if not scope and not global_:
                return {"ret": -1, "errmsg": "peer is not served by this device"}
            item = self.write_fact(peer, text, source="agent",
                                   agent=str(payload.get("agent") or ""),
                                   instance=str(payload.get("instance") or ""),
                                   tags=payload.get("tags") or (),
                                   global_=global_,
                                   layer=str(payload.get("layer") or "l1"))
            return {"ret": 0, "item": item.as_dict() if item else None}
        if path == "task":
            if not scope:
                return {"ret": -1, "errmsg": "peer is not served by this device"}
            item = self.write_task(peer, str(payload.get("text") or ""),
                                   status=str(payload.get("status") or "doing"),
                                   source="agent", agent=str(payload.get("agent") or ""),
                                   instance=str(payload.get("instance") or ""))
            return {"ret": 0, "item": item.as_dict() if item else None}
        if path == "forget":
            if not scope:
                return {"ret": -1, "errmsg": "peer is not served by this device"}
            target = str(payload.get("id") or "")
            removed = (self.store.forget(scopes=[scope], item_id=target) if target
                       else self.store.forget(scopes=[scope], query=str(payload.get("query") or "")))
            self._mirror(peer)
            return {"ret": 0, "removed": removed}
        return {"ret": -1, "errmsg": f"unknown memory API: {path}"}

    # -- markdown mirror -------------------------------------------------
    def _peer_dir(self, sender: str) -> Path:
        return self.dir / "peers" / (peer_id(sender, self.salt) if sender else "unknown")

    def _mirror(self, sender: str, *, global_: bool = False) -> None:
        if not self.enabled or not self.store:
            return
        try:
            if global_:
                self._write_md(self.dir / "global", "global")
            if sender:
                self._write_md(self._peer_dir(sender), self.scope_for(sender))
                # 全局层变了也顺带刷新一次，保持两边一致
                self._write_md(self.dir / "global", "global")
        except OSError as exc:
            self._log(f"memory: mirror failed: {exc}")

    def _write_md(self, folder: Path, scope: str) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        facts = self.store.list_items(scope, layer="l1")
        tasks = self.store.list_items(scope, layer="task")
        profile = folder / "profile.md"
        if not profile.exists():
            profile.write_text(
                "# 画像（L3）\n\n"
                "这里的内容会被原样注入到每条消息前面；一行一条，以 `- ` 开头。\n\n"
                "- （示例，删掉这一行）偏好中文回答\n", encoding="utf-8")
        (folder / "facts.md").write_text(self._render_md("事实（L1）", facts), encoding="utf-8")
        (folder / "tasks.md").write_text(self._render_md("任务", tasks), encoding="utf-8")

    @staticmethod
    def _render_md(title: str, items: Sequence[MemoryItem]) -> str:
        lines = [f"# {title}", "", "<!-- 由网关生成；手动加的一行会以 user 身份导入。 -->", ""]
        for item in items:
            tags = f"  ({', '.join(item.tags)})" if item.tags else ""
            lines.append(f"- [{item.id}] {item.text}{tags}")
        return "\n".join(lines) + "\n"

    def _import_all(self, *, scope: str = "") -> int:
        """从 Markdown 导回人工新增的行。镜像行带 [m-xxxx]，跳过。"""
        imported = 0
        roots: List[Tuple[Path, str]] = []
        if scope == "global" or not scope:
            roots.append((self.dir / "global", "global"))
        if scope and scope != "global":
            roots.append((self._peer_dir_for_scope(scope), scope))
        elif not scope:
            peers_dir = self.dir / "peers"
            if peers_dir.is_dir():
                for child in peers_dir.iterdir():
                    if child.is_dir():
                        roots.append((child, f"peer:{child.name}"))
        for folder, target_scope in roots:
            imported += self._import_folder(folder, target_scope)
        return imported

    def _peer_dir_for_scope(self, scope: str) -> Path:
        return self.dir / "peers" / scope.split(":", 1)[-1]

    def _import_folder(self, folder: Path, scope: str) -> int:
        imported = 0
        for name, layer in (("facts.md", "l1"), ("profile.md", "l3"), ("tasks.md", "task")):
            path = folder / name
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                stripped = line.strip()
                if not stripped.startswith("- ") or stripped.startswith("- ["):
                    continue
                text = stripped[2:].strip()
                if not text or text.startswith("（示例"):
                    continue
                existing = _fingerprint(scope, redact(text, self.store.redact_patterns))
                with self.store._lock:
                    row = self.store._db.execute(
                        "SELECT 1 FROM items WHERE scope=? AND fingerprint=?",
                        (scope, existing)).fetchone()
                if row:
                    continue
                self.store.write(scope=scope, layer=layer,
                                 kind="task" if layer == "task" else "",
                                 text=text, source="user")
                imported += 1
        return imported

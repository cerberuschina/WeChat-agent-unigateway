#!/usr/bin/env python
"""Agent-side iLink client — put *any* agent on WeChat through the virtual gateway.

This is the other half of `agent_gateway.virtual_ilink`: the gateway pretends to
be Tencent, this pretends to be a normal WeChat integration. Between the two,
the agent itself needs to know nothing about WeChat.

    python clients/ilink_agent_client.py --name claude \
        --runner "claude -p {text}" --base-url http://127.0.0.1:18500

Flow: QR-login against the gateway (it mints a virtual identity) → long-poll
`getupdates` → for every message run the agent command → `sendmessage` the
answer back → the gateway forwards it to the real WeChat.

Useful flags
    --login-only     bind and print the identity, then exit
    --once           handle one message and exit (good for tests)
    --stdin          feed the message to the command on stdin instead of {text}
    --timeout N      per-message timeout for the agent command (default 900s)
    --creds PATH     where the virtual identity + cursor live (default data/virtual-client-<name>.json)
    --reuse-token T  start with a token the gateway already knows (put it in the
                     config as ``virtual.accept_tokens.<name>: file:...``). Without
                     this the client's identity lives only in the gateway's memory,
                     so restarting the gateway drops it off its long poll and the
                     client exits with ret=-14.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_gateway import ilink, ilink_media, media  # noqa: E402
from clients.stream_session import (ClaudeStreamSession, SessionDead,  # noqa: E402
                                    TurnTimeout)

PLACEHOLDER = "{text}"
SESSION_PLACEHOLDER = "{session}"
SESSION_MARK = "##SESSION:"

# 整条消息只有这样才算换会话的命令（网关把它原样送进来，见 agent_gateway/gateway.py）。
NEW_SESSION_WORDS = frozenset({"/new", "/reset"})
LIST_SESSIONS_WORD = "/sessions"
RESUME_SESSION_WORD = "/resume"

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def parse_session_command(text: str) -> tuple[str, str]:
    """把会话类命令拆成 ``(命令, 参数)``；不是这类命令就返回 ``("", "")``。

    命令只有整条消息的第一个词才算（和网关的规则一致），全角「／」也认。
    """
    parts = (text or "").strip().split(None, 1)
    if not parts:
        return "", ""
    head = parts[0].lower().replace("／", "/")
    argument = parts[1].strip() if len(parts) > 1 else ""
    if head in NEW_SESSION_WORDS:
        return "new", argument
    if head == LIST_SESSIONS_WORD:
        return "sessions", argument
    if head == RESUME_SESSION_WORD:
        return "resume", argument
    return "", ""


def is_new_session_command(text: str) -> bool:
    """True when the message opens with ``/new`` (``/reset`` also works).

    Trailing words are ignored here; Hermes uses them as the new session's title.
    """
    return parse_session_command(text)[0] == "new"


# ---------------------------------------------------------------------- 会话目录
# claude 把每个工作目录的 transcript 放在 ``~/.claude/projects/<目录名>/<会话 id>.jsonl``，
# 目录名 = 工作目录路径里每个非字母数字字符都换成 ``-``（``E:\\traeWork\\all`` →
# ``E--traeWork-all``）。``claude --resume <id>`` 认的就是这些 id。
def claude_projects_root() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    base = Path(configured) if configured else Path.home() / ".claude"
    return base / "projects"


def project_dir_for(cwd: str | Path) -> Path:
    target = Path(cwd or Path.home()).expanduser().resolve()
    return claude_projects_root() / re.sub(r"[^A-Za-z0-9]", "-", str(target))


class KnownSession:
    """一条能接着聊的会话：id、最近活动时间、第一句用户消息（当标题）。"""

    def __init__(self, id: str, updated: float, title: str = ""):
        self.id = id
        self.updated = updated
        self.title = title


def _message_text(obj: dict) -> str:
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(block.get("text") or "") for block in content
                        if isinstance(block, dict) and block.get("type") == "text")
    return ""


# 这些不是用户真说的话：命令包装、注入的系统提醒、被打断占位。
_BORING_PREFIXES = ("<", "Caveat:", "[Request interrupted")


def session_title(path: Path, max_lines: int = 400) -> str:
    """从 transcript 里挑一句像样的话当标题（摘要行优先，否则第一句用户消息）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for count, line in enumerate(handle):
                if count >= max_lines:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "summary" and str(obj.get("summary") or "").strip():
                    return " ".join(str(obj["summary"]).split())[:60]
                if obj.get("type") != "user" or obj.get("isMeta") or obj.get("isSidechain"):
                    continue
                text = " ".join(_message_text(obj).split())
                if not text or text.startswith(_BORING_PREFIXES):
                    continue
                return text[:60]
    except OSError:
        return ""
    return ""


def list_claude_sessions(cwd: str | Path, *, limit: int = 10,
                         with_titles: bool = True) -> list[KnownSession]:
    """这个工作目录下最近的会话，最新的排前面。"""
    directory = project_dir_for(cwd)
    if not directory.is_dir():
        return []
    found: list[tuple[float, Path]] = []
    for path in directory.glob("*.jsonl"):
        if not UUID_RE.match(path.stem):
            continue                      # 子 agent 的旁支文件、别的碎片，不能 --resume
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [KnownSession(path.stem, updated, session_title(path) if with_titles else "")
            for updated, path in found[:limit]]


def _ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def format_session_list(rows: list[KnownSession], current_id: str = "",
                        *, now: float | None = None) -> str:
    if not rows:
        return "这个工作目录还没有能接着聊的会话。直接说话就是一条新会话。"
    now = now if now is not None else time.time()
    lines = ["能接着聊的会话（最近的在前，✅ = 现在这条）："]
    for index, row in enumerate(rows, 1):
        mark = "✅ " if row.id == current_id else ""
        when = time.strftime("%m-%d %H:%M", time.localtime(row.updated))
        title = row.title or "（无标题）"
        lines.append(f"{index}. {mark}{title} · {when}（{_ago(now - row.updated)}）· {row.id[:8]}")
    lines += ["", "发 /resume 编号 切过去（也可以 /resume <id 前 8 位>）。"
                  "/new 是开一条全新的，不断旧会话。"]
    return "\n".join(lines)


def resolve_session(query: str, *, cwd: str | Path,
                    catalog_ids: list[str] | None = None) -> tuple[str, str]:
    """``/resume`` 的参数 → 会话 id。编号按上一次 /sessions 给的那份列表算，稳定。"""
    wanted = (query or "").strip()
    if not wanted:
        return "", ("用法：/resume <编号>（先发 /sessions 看列表），"
                    "也可以直接给会话 id 的前几位。")
    if wanted.isdigit():
        index = int(wanted)
        ids = list(catalog_ids or [])
        if 1 <= index <= len(ids):
            return ids[index - 1], ""
        return "", f"没有第 {index} 条。先发 /sessions 看现在的列表。"
    lowered = wanted.lower()
    if not re.fullmatch(r"[0-9a-f-]{4,36}", lowered):
        return "", "要编号（先发 /sessions），或会话 id 的前几位（十六进制和短横线）。"
    matches = [row.id for row in list_claude_sessions(cwd, limit=500, with_titles=False)
               if row.id.lower().startswith(lowered)]
    if len(matches) == 1:
        return matches[0], ""
    if not matches:
        return "", f"没找到 id 以 {wanted} 开头的会话。"
    return "", f"{wanted} 能对上 {len(matches)} 条，多写几位。"


def catalog_path_for(sessions_path: Path) -> Path:
    """``/sessions`` 给过的列表（编号 → id）要落盘，下次 /resume 编号才对得上。"""
    return sessions_path.with_name(sessions_path.stem + ".catalog.json")


def load_catalog(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_catalog(path: Path, catalog: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def extract_session(answer: str) -> tuple[str, str]:
    """Pull a runner's ``##SESSION:<id>`` marker out of its output.

    A runner that supports multi-turn work prints the marker so the next message
    from the same peer can resume that conversation instead of starting cold.
    """
    session = ""
    kept: list[str] = []
    for line in answer.splitlines():
        if line.strip().startswith(SESSION_MARK):
            session = line.strip()[len(SESSION_MARK):].strip()
        else:
            kept.append(line)
    return "\n".join(kept).strip(), session


def load_sessions(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_sessions(path: Path, sessions: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")


class RunnerStopped(RuntimeError):
    """The user typed /stop while this run was in flight."""


def _terminate(proc: subprocess.Popen) -> None:
    """Kill the agent *and* whatever it spawned.

    The runner is usually a wrapper (``cc_runner.py`` → ``claude.exe`` → its own
    tools), so killing only the direct child would leave the real work running.
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.kill()
    except Exception:  # noqa: BLE001 - stopping is best effort, never fatal
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def run_agent(argv: list[str], text: str, *, use_stdin: bool, timeout: float, cwd: str,
              session: str = "", stop: "threading.Event | None" = None) -> str:
    """Run the agent command once and return its answer.

    ``stop`` is watched while the child runs, so a ``/stop`` from the phone ends the
    work instead of waiting out ``timeout`` (an hour by default).
    """
    if use_stdin:
        command = argv
        stdin = text
    else:
        command = [text if part == PLACEHOLDER else session if part == SESSION_PLACEHOLDER else part
                   for part in argv]
        if command == argv and PLACEHOLDER not in argv:
            command = argv + [text]          # no placeholder: append the text
        stdin = None
    proc = subprocess.Popen(command, stdin=subprocess.PIPE if stdin is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", cwd=cwd or None,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    watcher = None
    if stop is not None:
        def watch() -> None:
            while proc.poll() is None:
                if stop.is_set():
                    _terminate(proc)
                    return
                time.sleep(0.3)

        watcher = threading.Thread(target=watch, name="runner-stop", daemon=True)
        watcher.start()
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate(proc)
        proc.communicate()
        raise
    if stop is not None and stop.is_set():
        raise RunnerStopped("（收到 /stop，这次没跑完）")
    out = (out or "").strip()
    err = (err or "").strip()
    if proc.returncode != 0 and not out:
        raise RuntimeError(f"退出码 {proc.returncode}：{err[:300] or '（没有输出）'}")
    if err and not out:
        return err
    return out


def heartbeat(client, sender: str, message: dict, state: int) -> None:
    """Tell the gateway the agent is still alive.

    This goes to the *gateway*, never to WeChat: the gateway owns the real typing
    indicator, so a heartbeat costs zero messages out of the per-turn budget.
    """
    if not hasattr(client, "send_typing"):
        return
    try:
        client.send_typing(sender, state, typing_ticket="",
                           context_token=message.get("context_token"))
    except Exception as exc:  # noqa: BLE001 - a heartbeat must never break the run
        log(f"（心跳发不出去：{exc}）")


class PeerSession:
    """一个联系人一条长命会话：能接着聊，也能在它跑的时候插话。

    为什么不是"一条消息一个进程"：那样用户中途说的话只能排在后面（甚至丢），而且每轮
    都要 cold start。这里把消息投进队列、由这个线程顺序处理 —— 正在跑的时候再来的话，
    直接写进同一个会话（当前这一步一结束就生效，见 clients/stream_session.py）。
    """

    def __init__(self, peer: str, argv: list[str], *, cwd: str, env: dict, idle: float,
                 client, sessions: dict, sessions_path: Path, max_chars: int = 0):
        self.peer = peer
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.idle = idle
        self.client = client
        self.sessions = sessions
        self.sessions_path = sessions_path
        self.max_chars = max_chars
        self.queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.session = None
        self.pending = 0
        self.started_at = 0.0
        self._stop = threading.Event()
        # /new 的请求：不能在这里直接拆会话（session 只归 _run 那个线程碰），记下来等它收手。
        self._reset_message: "dict | None" = None
        # /resume 同理：切到一个已知会话，也要等手里这一轮跑完再动手。
        self._resume_target = ""
        self._resume_message: "dict | None" = None
        self._thread = threading.Thread(target=self._run, name=f"peer-{peer[-6:]}", daemon=True)

    # -- 外面只调这两个 --------------------------------------------------
    def submit(self, text: str, message: dict) -> None:
        self.queue.put((text, message))
        if not self._thread.is_alive():
            self._thread.start()

    def reset(self, message: dict | None = None) -> None:
        """换新会话：等手里这条跑完就关掉旧会话、忘掉它的 id。"""
        self._reset_message = message or {}
        if not self._thread.is_alive():
            self._thread.start()

    def resume(self, session_id: str, message: dict | None = None) -> None:
        """切到已知会话：等手里这条跑完，关掉当前进程，下一条用 --resume 接上它。"""
        self._resume_target = session_id
        self._resume_message = message or {}
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- 内部 -----------------------------------------------------------
    def _open(self) -> None:
        command = list(self.argv)
        known = self.sessions.get(self.peer) or ""
        if known:
            command += ["--resume", known]        # 客户端重启后接着上次的会话
        made = ClaudeStreamSession(command, cwd=self.cwd, env=self.env, log=log)
        made.start()
        self.session = made
        self.started_at = time.time()

    def _close(self) -> None:
        if self.session is not None:
            self.session.stop()
            self.session = None

    def _reply(self, answer: str, message: dict) -> None:
        answer, media_paths = extract_media_marks(answer)
        if not answer.strip() and not media_paths:
            answer = "（这次没有输出）"
        chunks = [answer] if self.max_chars <= 0 else ilink.split_text(answer, self.max_chars)
        for chunk in chunks:
            self.client.send_text(self.peer, chunk, context_token=message.get("context_token"))
        for path in media_paths:
            send_local_media(self.client, self.peer, path, message)

    def _apply_reset(self) -> None:
        """关掉旧会话、忘掉它的 id —— 下一条消息就是一个全新的上下文。"""
        message = self._reset_message or {}
        self._reset_message = None
        self._close()
        self.sessions.pop(self.peer, None)
        save_sessions(self.sessions_path, self.sessions)
        log(f"（{self.peer[-8:]} 换新会话：旧的关掉，下一条从零开始）")
        self._reply("✅ 好，新会话，前面的上下文不带上了。", message)

    def _apply_resume(self) -> None:
        """切到已知会话：关掉当前进程，记下 id，下一条 _open 会 --resume 接上它。"""
        target = self._resume_target
        self._resume_target = ""
        message = self._resume_message or {}
        self._resume_message = None
        live_id = self.session.session_id if self.session is not None else ""
        if target and target == live_id:
            self._reply("已经在这个会话里了，下一条就接着它。", message)
            return
        self._close()
        self.sessions[self.peer] = target
        save_sessions(self.sessions_path, self.sessions)
        title = session_title(project_dir_for(self.cwd) / f"{target}.jsonl")
        label = f"{title} · " if title else ""
        log(f"（{self.peer[-8:]} 切到会话 {target[:8]}…，下一条 --resume 接上）")
        self._reply(f"✅ 切到「{label}{target[:8]}…」，下一条接着那个会话聊。", message)

    def _run(self) -> None:
        while not self._stop.is_set():
            # 换会话只在手里没活时做：正在跑的那一轮照常收尾，别把它的答案吞掉。
            if self._reset_message is not None and not self.pending:
                try:
                    self._apply_reset()
                except Exception as exc:  # noqa: BLE001 - 这条线程不能死，也不能无限重试
                    self._reset_message = None
                    log(f"（换会话没做成：{exc}）")
                continue
            if self._resume_message is not None and not self.pending:
                try:
                    self._apply_resume()
                except Exception as exc:  # noqa: BLE001
                    self._resume_message = None
                    self._resume_target = ""
                    log(f"（切会话没做成：{exc}）")
                continue
            item = None
            try:
                item = self.queue.get(timeout=0.3)
            except queue.Empty:
                pass
            if item is not None:
                text, message = item
                try:
                    if self.session is None or not self.session.alive:
                        self._open()
                    # 正在跑也照样写进去：它会在当前这一步结束后立刻成为下一轮
                    self.session.send(text)
                    self.pending += 1
                    if self.pending == 1:
                        heartbeat(self.client, self.peer, message, ilink.TYPING_START)
                except Exception as exc:  # noqa: BLE001 - 会话这边出事不能拖垮轮询
                    log(f"（会话用不了：{exc}）")
                    self._reply(f"（会话用不了：{exc}）", message)
                    self._close()
                    self.pending = 0
                    continue
            if not self.pending:
                continue
            try:
                turn = self.session.turn(timeout=0.3)
            except TurnTimeout:
                continue
            except SessionDead as exc:
                log(f"（会话断了：{exc}）")
                self._close()
                self._reply(f"（会话断了，下一条我会重开：{exc}）", item[1] if item else {})
                self.pending = 0
                heartbeat(self.client, self.peer, item[1] if item else {}, ilink.TYPING_STOP)
                continue
            self.pending = max(0, self.pending - 1)
            if turn.session_id and turn.session_id != self.sessions.get(self.peer):
                self.sessions[self.peer] = turn.session_id
                save_sessions(self.sessions_path, self.sessions)
            self._reply(turn.text, item[1] if item else {})
            if not self.pending:
                heartbeat(self.client, self.peer, item[1] if item else {}, ilink.TYPING_STOP)
                if self.idle and time.time() - self.started_at > self.idle:
                    log("（会话空闲够久，收工；下次用 --resume 接着聊）")
                    self._close()


def run_with_progress(runner: list[str], text: str, args, session: str, sender: str,
                      client, message: dict) -> str:
    """Run the agent, keeping the peer's 「正在输入」 alive while it works.

    WeChat cannot edit a sent message, and it lets a bot send only ~10 messages
    before the user replies — so progress *messages* are a budget leak. Default is
    therefore a heartbeat: the agent says "still working", the gateway holds the
    typing indicator, and nothing is sent to the real WeChat. ``--progress-notes``
    opts back into visible notes (which do spend the budget).
    """
    holder: dict = {}

    def work() -> None:
        try:
            holder["answer"] = run_agent(runner, text, use_stdin=args.stdin,
                                         timeout=args.timeout, cwd=args.cwd, session=session,
                                         stop=stop)
        except RunnerStopped as stopped_note:
            holder["answer"] = str(stopped_note)
        except subprocess.TimeoutExpired:
            holder["answer"] = f"（跑了 {args.timeout:.0f} 秒还没完，这次先放弃）"
        except Exception as exc:  # noqa: BLE001 - the agent must never kill the loop
            holder["answer"] = f"（跑挂了：{exc}）"

    def watch_for_stop() -> None:
        """每两秒问一次网关：用户有没有在本轮里发 /stop。"""
        while worker.is_alive() and not stop.is_set():
            if stop_requested(client, sender):
                log("收到 /stop，停下这次运行。")
                stop.set()
                return
            time.sleep(2.0)

    stop = threading.Event()
    worker = threading.Thread(target=work, name="runner", daemon=True)
    watcher = threading.Thread(target=watch_for_stop, name="runner-stop-watch", daemon=True)
    started = time.time()
    worker.start()
    watcher.start()
    hb = max(float(args.progress_every), 0.0)
    notes = getattr(args, "progress_notes", False)
    delay = hb
    beating = False
    # 立刻打第一拍：微信的「正在输入」自己有寿命，等满一个周期再发的话，
    # 每轮开头那几十秒看起来跟死了没区别。心跳是零微信开销的，早点发不亏。
    if hb and not notes:
        heartbeat(client, sender, message, ilink.TYPING_START)
        beating = True
    while worker.is_alive():
        worker.join(timeout=delay or 5.0)
        if not worker.is_alive():
            break
        if not delay:
            continue
        elapsed = time.time() - started
        if notes:
            note = (f"⏳ 还在跑（已 {int(elapsed // 60)} 分 {int(elapsed % 60):02d} 秒），"
                    f"完事我把结果发上来。")
            try:
                client.send_text(sender, note, context_token=message.get("context_token"))
                delay = min(delay * 2, 600.0)   # 退避：进度是花微信额度的
            except Exception as exc:  # noqa: BLE001
                log(f"（进度发不出去：{exc}）")
        else:
            heartbeat(client, sender, message, ilink.TYPING_START)
            beating = True
    if beating:
        heartbeat(client, sender, message, ilink.TYPING_STOP)
    return holder.get("answer", "（没有结果）")


def do_login(base_url: str, name: str, *, png: str = "", max_refreshes: int = 30,
             timeout: int = 480) -> dict:
    def on_event(kind: str, payload: dict) -> None:
        if kind == "qr":
            url = str(payload.get("url") or "")
            print("\n请批准这个 agent 接入（网关的批准页打开它给的链接即可）：", flush=True)
            if url:
                print(f"  {url}", flush=True)
            print(f"  qrcode = {payload.get('qrcode')}", flush=True)
            if png and url:
                try:
                    import segno  # type: ignore
                    segno.make(url, error="m").save(png, scale=6, border=2, kind="png")
                    print(f"  二维码图片：{png}", flush=True)
                except ImportError:
                    print("  （写 PNG 失败：pip install segno）", flush=True)
            print("  （如果这个 agent 已在网关的 auto_approve 里，会立刻通过）", flush=True)
        else:
            status = str(payload.get("status") or "")
            if status == "scaned":
                log("已扫码，等确认…")
            elif status == "expired":
                log("二维码过期，换一张…")

    creds = ilink.qr_login(base_url=base_url, timeout_seconds=timeout,
                           max_refreshes=max_refreshes, on_event=on_event)
    if not creds:
        raise SystemExit("登录失败：网关没给身份（超时？端口不对？）")
    log(f"拿到虚拟微信号：{creds['account_id']}（服务端 {creds['base_url']}）")
    return {"account_id": creds["account_id"], "token": creds["token"],
            "base_url": creds["base_url"], "cursor": "", "name": name}


def save_creds(path: Path, creds: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_creds(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def handle_session_command(command: str, argument: str, *, sender: str, message: dict,
                           client, sessions: dict, sessions_path: Path, peer_sessions: dict,
                           cwd: str, session_mode: bool, runner_has_session: bool) -> None:
    """在客户端这一侧执行 /new、/sessions、/resume —— 会话只归这里管，网关只转交。

    ``session_mode`` 是长命会话模式（--session-command）；``runner_has_session``
    表示一条消息一个进程的模板里带 ``{session}``，能靠 --resume 接着上次的聊。
    """

    def reply(text: str) -> None:
        client.send_text(sender, text, context_token=message.get("context_token"))

    if command == "new":
        # 长命会话模式交给那个联系人的线程（别在别的线程里拆它的 session）；
        # runner 模式没有常驻会话，忘掉 session id 就够了。
        worker = peer_sessions.get(sender)
        if worker is not None:
            worker.reset(message)
        else:
            sessions.pop(sender, None)
            save_sessions(sessions_path, sessions)
            log(f"（{sender[-8:]} 换新会话：忘掉 session id）")
            reply("✅ 好，新会话，前面的上下文不带上了。")
        return

    if command == "sessions":
        rows = list_claude_sessions(cwd)
        catalog_path = catalog_path_for(sessions_path)
        catalog = load_catalog(catalog_path)
        # 编号只对这次给用户看的那份列表有效，所以把它存下来，/resume 编号才有据可依。
        catalog[sender] = {"ids": [row.id for row in rows], "at": time.time()}
        save_catalog(catalog_path, catalog)
        reply(format_session_list(rows, sessions.get(sender, "")))
        return

    # /resume
    if not session_mode and not runner_has_session:
        reply("这个客户端一条消息起一个进程，模板里也没写 {session}，切会话用不上；"
              "要能切，让 agent 用长命会话模式（--session-command），"
              "或者 runner 模板里带上 {session}。")
        return
    catalog_ids = list((load_catalog(catalog_path_for(sessions_path)).get(sender) or {})
                       .get("ids") or [])
    target, problem = resolve_session(argument, cwd=cwd, catalog_ids=catalog_ids)
    if not target:
        reply(problem)
        return
    worker = peer_sessions.get(sender)
    if worker is not None:
        worker.resume(target, message)          # 它等这一轮跑完再关旧会话，别在这里抢
        return
    sessions[sender] = target
    save_sessions(sessions_path, sessions)
    title = session_title(project_dir_for(cwd) / f"{target}.jsonl")
    label = f"{title} · " if title else ""
    log(f"（{sender[-8:]} 切到会话 {target[:8]}…，下一条接上）")
    reply(f"✅ 切到「{label}{target[:8]}…」，下一条接着那个会话聊。")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an agent on WeChat via the virtual gateway")
    parser.add_argument("--name", default="agent", help="this agent's name (must match the gateway config)")
    parser.add_argument("--base-url", default="http://127.0.0.1:18500",
                        help="the gateway's virtual iLink server")
    parser.add_argument("--runner", default="", help=f"command template, e.g. 'claude -p {PLACEHOLDER}'")
    parser.add_argument("--stdin", action="store_true", help="pipe the message into the command instead")
    parser.add_argument("--cwd", default="", help="working directory for the agent command")
    parser.add_argument("--timeout", type=float, default=3600.0,
                        help="per-message timeout (seconds); a long agent run must not be cut short")
    parser.add_argument("--max-chars", type=int, default=0,
                        help="0 = send the answer whole and let the gateway render/split for WeChat")
    parser.add_argument("--progress-every", type=float, default=45.0,
                        help="heartbeat every N seconds while the agent works (0 = off); "
                             "heartbeats go to the gateway and cost no WeChat messages")
    parser.add_argument("--session-command", default="",
                        help="长命会话模式：这条命令起一个双向流的 agent 进程（例如 "
                             "\"claude -p --input-format stream-json --output-format stream-json "
                             "--verbose\"），给了它就不要 --runner；同一个会话多轮共用，"
                             "而且用户在它跑的时候插话会被立刻收下（当前这一步结束就生效）")
    parser.add_argument("--session-idle", type=float, default=1800.0,
                        help="会话空闲多久收工（秒）；下次启动用已知的 session id --resume 接着聊")
    parser.add_argument("--progress-notes", action="store_true",
                        help="send visible 「还在跑」 notes instead of heartbeats "
                             "(these DO spend WeChat's ~10 messages per turn)")
    parser.add_argument("--creds", default="", help="where to keep the virtual identity")
    parser.add_argument("--session-store", default="",
                        help="where to keep per-peer session ids (default: next to --creds); "
                             "use {session} in --runner to resume the conversation")
    parser.add_argument("--login-only", action="store_true")
    parser.add_argument("--once", action="store_true", help="handle a single message then exit")
    parser.add_argument("--max-refreshes", type=int, default=30)
    parser.add_argument("--login-timeout", type=int, default=480)
    parser.add_argument("--png", default="", help="write the QR to this PNG (needs segno)")
    parser.add_argument("--reuse-token", default="",
                        help="a token the agent already holds (see virtual.reuse_real_token_for)")
    parser.add_argument("--bind-key", default="",
                        help="pre-shared key a remote gateway requires before it hands out "
                             "an identity (also via the ILINK_BIND_KEY environment variable)")
    args = parser.parse_args(argv)

    creds_path = Path(args.creds) if args.creds else Path("data") / f"virtual-client-{args.name}.json"
    if args.bind_key:
        # The QR request is built deep inside ilink.fetch_qr; the env is how the
        # key travels without threading it through every call.
        os.environ["ILINK_BIND_KEY"] = args.bind_key
    creds = load_creds(creds_path)

    if args.reuse_token:
        # The agent is already bound to the real WeChat: keep that token, the
        # gateway accepts it as this agent's alias.
        creds = {"account_id": f"reuse-{args.name}", "token": args.reuse_token,
                 "base_url": args.base_url, "cursor": "", "name": args.name}
        save_creds(creds_path, creds)

    if not creds:
        creds = do_login(args.base_url, args.name, png=args.png,
                         max_refreshes=args.max_refreshes, timeout=args.login_timeout)
        save_creds(creds_path, creds)

    client = ilink.ILinkClient(creds["account_id"], creds["token"], base_url=creds["base_url"])
    log(f"身份 {creds['account_id']}，服务端 {creds['base_url']}，凭证 {creds_path}")

    try:                                     # a stale token must re-login, not spin
        client.get_config(creds["account_id"])
    except ilink.SessionExpired:
        log("旧身份已失效（网关重启会这样），重新扫码…")
        creds = do_login(args.base_url, args.name, png=args.png,
                         max_refreshes=args.max_refreshes, timeout=args.login_timeout)
        save_creds(creds_path, creds)
        client = ilink.ILinkClient(creds["account_id"], creds["token"], base_url=creds["base_url"])
    except ilink.ILinkError as exc:
        log(f"（探测身份时出问题，先继续：{exc}）")

    if args.login_only:
        print(json.dumps({k: creds[k] for k in ("account_id", "base_url", "name")},
                         ensure_ascii=False, indent=2))
        return 0

    if not args.runner and not args.session_command:
        raise SystemExit("要么给 --runner（一条消息一个进程），要么给 --session-command"
                         "（长命会话，能边跑边插话），要么只做 --login-only。")
    # Windows paths in a --runner template would have their backslashes eaten by
    # posix-style splitting ("C:\x\y" -> "C:xy"), so normalise them to slashes.
    runner = shlex.split(args.runner.replace("\\", "/"))
    log(f"用命令处理消息：{' '.join(runner)}" + ("  (stdin)" if args.stdin else ""))
    sessions_path = (Path(args.session_store) if args.session_store
                     else creds_path.with_suffix(".sessions.json"))
    sessions = load_sessions(sessions_path)

    cursor = str(creds.get("cursor") or "")
    handled = 0
    session_command = shlex.split(args.session_command) if args.session_command else []
    peer_sessions: dict = {}
    while True:
        started = time.monotonic()
        try:
            response = client.get_updates(cursor)
        except ilink.SessionExpired:
            log("身份失效（ret=-14），退出；下次启动会重新扫码。")
            return 3
        except ilink.RateLimited:
            time.sleep(2)
            continue
        except ilink.ILinkError as exc:
            log(f"取消息失败：{exc}（2 秒后重试）")
            time.sleep(2)
            continue
        except Exception as exc:  # noqa: BLE001 - 网关重启会把长轮询掐断
            # 今天咬过一次：我重启网关，客户端一个 ConnectionResetError 就退出了，
            # 微信那条链路跟着死。连接断了是重启的正常副作用，重试就行，不该退出。
            log(f"和网关的连接断了（{exc}），5 秒后重连。")
            time.sleep(5)
            continue

        new_cursor = str(response.get("get_updates_buf") or "")
        if new_cursor and new_cursor != cursor:
            cursor = new_cursor
            creds["cursor"] = cursor
            save_creds(creds_path, creds)
        if not response.get("msgs") and time.monotonic() - started < 1.0:
            time.sleep(1)   # 秒回的空批次不是长轮询，别对网关死循环

        for message in response.get("msgs") or []:
            text = ilink.message_text(message)
            sender = ilink.sender_of(message)
            if text.strip() and not is_local_gateway(getattr(client, "base_url", "")):
                # 网关在别的机器上：把「下载地址」换成本机真能读到的文件
                text = materialize_media(text, client, Path("data") / "inbox")
            if not text.strip():
                log("收到一条没有内容的消息，跳过。")
                continue
            log(f"← {sender}: {text[:80]}")
            started = time.time()
            session = sessions.get(sender, "")
            export_approval_env(client, sender)   # 工具钩子靠这几个变量找回网关
            command, argument = parse_session_command(text)
            if command:
                # /new、/sessions、/resume：网关把命令原样送进来，真正管事的是这里。
                # 这条命令出什么岔子都不能把长轮询带走（客户端没人看着重启）。
                try:
                    handle_session_command(
                        command, argument, sender=sender, message=message, client=client,
                        sessions=sessions, sessions_path=sessions_path,
                        peer_sessions=peer_sessions, cwd=args.cwd,
                        session_mode=bool(session_command),
                        runner_has_session=(not args.stdin
                                            and SESSION_PLACEHOLDER in runner))
                except Exception as exc:  # noqa: BLE001
                    log(f"（会话命令 /{command} 没处理成：{exc}）")
                    try:
                        client.send_text(sender, f"（这条 /{command} 没处理成：{exc}）",
                                         context_token=message.get("context_token"))
                    except Exception:  # noqa: BLE001
                        pass
                continue
            if session_command:
                # 长命会话模式：这条消息不在这里等 —— 交给那个联系人的线程，它才收得住
                # "边跑边插"（在这里等的话，插话只能排在后面）。
                worker = peer_sessions.get(sender)
                if worker is None:
                    worker = PeerSession(sender, session_command, cwd=args.cwd,
                                         env=os.environ.copy(), idle=args.session_idle,
                                         client=client, sessions=sessions,
                                         sessions_path=sessions_path, max_chars=args.max_chars)
                    peer_sessions[sender] = worker
                    log(f"（给 {sender[-8:]} 开一条长命会话）")
                worker.submit(text, message)
                continue
            answer = run_with_progress(runner, text, args, session, sender, client, message)
            answer, new_session = extract_session(answer)
            answer, media_paths = extract_media_marks(answer)
            if new_session and new_session != session:
                sessions[sender] = new_session
                save_sessions(sessions_path, sessions)
                log(f"（会话已记下，下一条接着聊：{new_session[:8]}…）")
            if not answer.strip():
                answer = "（这次没有输出）"
            log(f"→ {answer[:80]}（{time.time() - started:.1f}s）")
            # 0 = hand the whole answer to the gateway, which renders Markdown for
            # WeChat and splits it into bubbles (it knows the channel's limits).
            chunks = [answer] if args.max_chars <= 0 else ilink.split_text(answer, args.max_chars)
            for chunk in chunks:
                client.send_text(sender, chunk, context_token=message.get("context_token"))
            for path in media_paths:
                send_local_media(client, sender, path, message)
            handled += 1
            if args.once:
                log("--once：处理完这条就退出。")
                return 0


def extract_media_marks(answer: str) -> tuple[str, list[str]]:
    """Pull ``##FILE:<path>`` / ``##IMAGE:<path>`` lines out of the runner's output.

    The runner is what knows it produced an artifact; it marks the path, and this
    client turns the mark into a real attachment.
    """
    paths: list[str] = []
    kept: list[str] = []
    for line in answer.splitlines():
        stripped = line.strip()
        if stripped.startswith(("##FILE:", "##IMAGE:")):
            path = stripped.split(":", 1)[1].strip()
            if path:
                paths.append(path)
                continue
        kept.append(line)
    return "\n".join(kept).strip(), paths


def is_local_gateway(base_url: str) -> bool:
    """True when the gateway runs on this very machine (paths are then shared)."""
    import urllib.parse as _urlparse

    host = (_urlparse.urlsplit(base_url or "").hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}


def _no_proxy_opener():
    """An opener that ignores HTTP(S)_PROXY.

    The gateway is on this machine or on the LAN; a corporate/system proxy in the
    middle would turn those requests into 502s.
    """
    import urllib.request as _urlrequest

    return _urlrequest.build_opener(_urlrequest.ProxyHandler({}))


def stop_requested(client, peer: str) -> bool:
    """Ask the gateway whether the user typed ``/stop`` for this chat.

    Polled while a run is in flight: the work happens in *this* process, so nobody
    else can end it. Any failure means "no stop" — a check must never break a run.
    """
    import urllib.parse as _urlparse
    import urllib.request

    base = str(getattr(client, "base_url", "") or "").rstrip("/")
    token = str(getattr(client, "token", "") or "")
    if not base:
        return False
    url = f"{base}/agent/stop?peer={_urlparse.quote(peer or '')}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with _no_proxy_opener().open(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001 - never let a stop check kill the run
        return False
    return bool(payload.get("stop"))


def ask_approval(client, peer: str, title: str, detail: str = "", *, kind: str = "command",
                 ttl: float = 180, wait: float = 0, timeout: float = 30) -> dict:
    """Ask the human through the gateway and (by default) wait for the answer.

    This is the one call an agent needs to stop dying on "requires approval": the
    gateway puts the question on the phone, the user answers there, and the
    verdict comes back as ``{"decision": "allow"|"deny"|"expired", ...}``.
    """
    import urllib.request

    base = str(getattr(client, "base_url", "") or "").rstrip("/")
    token = str(getattr(client, "token", "") or "")
    request = urllib.request.Request(
        f"{base}/agent/approval",
        data=json.dumps({"peer": peer, "title": title, "detail": detail,
                         "kind": kind, "ttl": ttl}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with _no_proxy_opener().open(request, timeout=timeout) as response:
        started = json.loads(response.read().decode("utf-8") or "{}")
    approval_id = str(started.get("id") or "")
    if not approval_id:
        raise RuntimeError(f"网关没有受理这次放行请求：{started}")
    waiting = float(wait or 0) or (float(ttl) + 20)
    query = urllib.request.Request(
        f"{base}/agent/approval/{approval_id}/wait?timeout={int(waiting)}",
        headers={"Authorization": f"Bearer {token}"})
    with _no_proxy_opener().open(query, timeout=waiting + 30) as response:
        answered = json.loads(response.read().decode("utf-8") or "{}")
    return dict(answered.get("approval") or {})


def export_approval_env(client, peer: str, ttl: float = 180) -> None:
    """Publish the gateway coordinates for anything the runner spawns.

    A tool hook (see ``hooks/claude_approval_hook.py``) runs as a child process and
    cannot ask *us* for the token, so it inherits these instead.
    """
    os.environ["AGW_APPROVAL_URL"] = str(getattr(client, "base_url", "") or "")
    os.environ["AGW_TOKEN"] = str(getattr(client, "token", "") or "")
    os.environ["AGW_PEER"] = peer or ""
    os.environ["AGW_APPROVAL_TTL"] = str(int(ttl))


def upload_blob(client, name: str, data: bytes) -> str:
    """Push file bytes to the gateway; returns a ``blob:<id>`` reference.

    A remote agent cannot hand over a path — its filesystem is not the gateway's.
    """
    import urllib.parse as _urlparse
    import urllib.request

    url = f"{str(client.base_url).rstrip('/')}/upload"
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {client.token}",
                 "X-File-Name": _urlparse.quote(name),
                 "Content-Type": "application/octet-stream"})
    with _no_proxy_opener().open(request, timeout=300) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if int(payload.get("ret", -1)) != 0 or not payload.get("blob"):
        raise RuntimeError(f"网关拒绝了上传：{payload}")
    return str(payload["blob"])


def fetch_remote_media(client, url: str, out_dir: Path) -> Path:
    """Download one media file the gateway stored (remote agents only)."""
    import urllib.parse as _urlparse
    import urllib.request

    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {client.token}"})
    with _no_proxy_opener().open(request, timeout=300) as response:
        data = response.read()
        name = _urlparse.unquote(response.headers.get("X-File-Name") or "media.bin")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / Path(name).name
    target.write_bytes(data)
    return target


def materialize_media(text: str, client, out_dir: Path) -> str:
    """Turn the gateway's 「下载地址：…」 lines into real local paths.

    The gateway sits on another machine, so the path it printed means nothing
    here; we fetch the bytes with our own token and hand the agent a usable path.
    """
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("下载地址："):
            url = stripped[len("下载地址："):].strip()
            try:
                target = fetch_remote_media(client, url, out_dir)
                lines.append(f"本地路径：{target}")
                continue
            except Exception as exc:  # noqa: BLE001 - keep the URL so the agent can try
                log(f"（取不到远端文件 {url}：{exc}）")
        lines.append(line)
    return "\n".join(lines)


def send_local_media(client, sender: str, path: str, message: dict) -> None:
    """Hand a file to the gateway, which encrypts and uploads it.

    Same machine → the gateway reads it itself (``localpath:``). Different machine
    → we POST the bytes to the gateway's ``/upload`` first (``blob:``), because our
    path means nothing over there.
    """
    try:
        absolute = os.path.abspath(path)
        if not os.path.isfile(absolute):
            log(f"（{path} 不存在，跳过）")
            return
        reference = f"localpath:{absolute}"
        if not is_local_gateway(getattr(client, "base_url", "")):
            with open(absolute, "rb") as handle:
                reference = upload_blob(client, os.path.basename(absolute), handle.read())
        item = media.build_media_item(
            media.item_type_for(absolute),
            encrypt_query_param=reference,
            aes_key=bytes(16), filename=os.path.basename(absolute),
            plaintext_size=os.path.getsize(absolute), ciphertext_size=0)
        ilink_media.send_items(client, sender, [item],
                               context_token=message.get("context_token"))
        log(f"（已交给网关发送附件：{os.path.basename(absolute)}）")
    except Exception as exc:  # noqa: BLE001 - an attachment must not break the reply loop
        log(f"（附件发不出去 {path}：{exc}）")


if __name__ == "__main__":
    raise SystemExit(main())

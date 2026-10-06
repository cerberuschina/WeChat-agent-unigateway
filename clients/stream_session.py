"""一个长命的 agent 进程：多轮共用同一个会话，中途可以插话。

为什么需要它：一条消息起一个进程的话，用户中途说的话只能排队（甚至丢），而且每一轮
都要 cold start。这里 stdin 一直开着 —— 后来的消息会被收下，并在当前这一步结束后
立刻生效；要它马上改做别的，就发一次 interrupt 控制请求（会话不丢，接着聊）。

协议要点（Claude Code 2.1.281 实测，不是猜的）：

* 启动：``claude -p --input-format stream-json --output-format stream-json --verbose``
* 输入：stdin 上一行一个 JSON：``{"type":"user","message":{"role":"user",
  "content":[{"type":"text","text":"..."}]}}``
* 输出：stdout 上一行一个 JSON，我们只关心 ``assistant``（正文）和 ``result``
  （一轮结束，带 ``session_id`` / ``stop_reason``）；中间会夹很多 ``system`` 事件。
* 运行中插话**不会**进到当前这一轮，而是在当前轮结束后立刻成为下一轮 —— 这是 CLI
  的边界，所以想立刻改向就配 interrupt：``{"type":"control_request","request_id":"…",
  "request":{"subtype":"interrupt"}}``（实测有 case 分支，返回 control_response）。
* stdin 一旦关掉，这个会话就结束了：stream-json 输入要求 stdin 全程可读。
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class Turn:
    """一轮的结果，外加它是怎么结束的。"""

    text: str = ""
    session_id: str = ""
    stop_reason: str = ""
    cost_usd: float = 0.0
    interrupted: bool = False
    raw: Dict = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.text.strip()


class SessionDead(RuntimeError):
    """进程没了（崩了、被杀了）：调用方该考虑重开会话。"""


class TurnTimeout(SessionDead):
    """只是「这一段还没等到结果」——会话还活着，接着等就行。"""

    def __init__(self, seconds: float):
        super().__init__(f"等了 {seconds:.1f} 秒还没有结果")
        self.seconds = seconds


class ClaudeStreamSession:
    def __init__(self, command: List[str], *, cwd: str = "", env: Optional[Dict[str, str]] = None,
                 log: Callable[[str], None] = lambda _m: None) -> None:
        self.command = list(command)
        self.cwd = cwd or None
        self.env = env
        self.log = log
        self.session_id = ""
        self.started_at = 0.0
        self._proc: Optional[subprocess.Popen] = None
        self._turns: "queue.Queue[Turn]" = queue.Queue()
        self._text: List[str] = []          # 当前轮累积的 assistant 正文
        self._events = 0
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        self._proc = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, encoding="utf-8", errors="replace",
                                      bufsize=1, cwd=self.cwd, env=self.env)
        self.started_at = time.time()
        threading.Thread(target=self._read_stdout, name="session-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="session-err", daemon=True).start()
        self.log(f"会话已起（pid {self._proc.pid}）")

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:  # noqa: BLE001 - 关不上就直接杀
                pass
        time.sleep(0.2)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None

    # ------------------------------------------------------------------- 发话
    def send(self, text: str) -> None:
        """把一轮用户消息推进会话（正在跑也没关系：它会在当前轮后立刻生效）。"""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise SessionDead("会话已经结束，需要重开一个")
            payload = {"type": "user",
                       "message": {"role": "user",
                                   "content": [{"type": "text", "text": text}]}}
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            self._text.clear()
        self.log(f"→ 送进会话：{text[:60]}")

    def interrupt(self, *, request_id: str = "") -> str:
        """让当前这一轮停下（会话保留，可以接着聊）。返回 request_id。"""
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise SessionDead("会话已经结束，没得打断")
            rid = request_id or f"int-{int(time.time() * 1000)}"
            payload = {"type": "control_request", "request_id": rid,
                       "request": {"subtype": "interrupt"}}
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        self.log("发了一次 interrupt")
        return rid

    # ------------------------------------------------------------------- 收话
    def turn(self, timeout: float = 3600.0) -> Turn:
        """等一轮结束。超时或进程没了都抛 SessionDead，让调用方决定怎么办。"""
        deadline = time.monotonic() + timeout
        while True:
            if not self.alive and self._turns.empty():
                raise SessionDead("会话进程已经不在了")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TurnTimeout(timeout)
            try:
                return self._turns.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue

    def ask(self, text: str, timeout: float = 3600.0) -> Turn:
        self.send(text)
        return self.turn(timeout=timeout)

    # ------------------------------------------------------------------ 读线程
    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                self.log(f"（看不懂的一行：{line[:120]}）")
                continue
            self._events += 1
            kind = event.get("type")
            if kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        self._text.append(str(block["text"]))
            elif kind == "result":
                if event.get("session_id"):
                    self.session_id = str(event["session_id"])
                text = str(event.get("result") or "").strip() or "\n".join(self._text).strip()
                stop = str(event.get("stop_reason") or "")
                self._turns.put(Turn(text=text, session_id=self.session_id, stop_reason=stop,
                                     cost_usd=float(event.get("total_cost_usd") or 0.0),
                                     interrupted=stop in ("interrupted", "max_turns"),
                                     raw=event))
                self._text.clear()
            elif kind not in ("system",):
                self.log(f"（事件 {kind}）")
        self.log("会话的标准输出关了")

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            line = line.strip()
            if line:
                self.log(f"[stderr] {line[:200]}")

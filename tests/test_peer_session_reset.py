"""``/new`` 在客户端这一侧：换会话 = 关掉旧会话 + 忘掉 ``--resume`` 的 id。

这一段跑的是真的 PeerSession 线程（只把 claude 进程换成一个假的流式会话），所以
线程里的时序、队列、``pending`` 计数都跟线上一致：能证明 /new 之后下一条消息确实
从零开始，而不是接着旧会话聊。
"""
from __future__ import annotations

import importlib.util
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1] / "clients" / "ilink_agent_client.py"
spec = importlib.util.spec_from_file_location("ilink_agent_client_reset", CLIENT)
assert spec and spec.loader
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)

PEER = "wx-peer-1234"


class _Turn:
    def __init__(self, text: str, session_id: str = ""):
        self.text = text
        self.session_id = session_id


class _FakeStreamSession:
    """假的 claude 流式会话：记下启动命令，收到话就回一句。"""

    opened: list = []

    def __init__(self, command, *, cwd, env, log):
        _FakeStreamSession.opened.append(list(command))
        self.alive = True
        self._turns: "queue.Queue" = queue.Queue()

    def start(self) -> None:
        return None

    def send(self, text: str) -> None:
        self._turns.put(_Turn(f"答：{text}", "sess-new"))

    def turn(self, timeout: float = 0.3):
        try:
            return self._turns.get(timeout=timeout)
        except queue.Empty:
            raise client.TurnTimeout() from None

    def stop(self) -> None:
        self.alive = False


class _FakeClient:
    def __init__(self):
        self.sent: list = []

    def send_text(self, sender: str, text: str, context_token=None) -> None:
        self.sent.append((sender, text))


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.real_session = client.ClaudeStreamSession
        client.ClaudeStreamSession = _FakeStreamSession
        self.addCleanup(setattr, client, "ClaudeStreamSession", self.real_session)
        _FakeStreamSession.opened = []
        self.fake = _FakeClient()
        self.sessions_path = self.tmp / "sessions.json"
        self.sessions = {PEER: "old-session-id"}
        client.save_sessions(self.sessions_path, self.sessions)

    def make_worker(self) -> "client.PeerSession":
        worker = client.PeerSession(
            PEER, ["claude", "-p", "--input-format", "stream-json"],
            cwd="", env={}, idle=0, client=self.fake, sessions=self.sessions,
            sessions_path=self.sessions_path, max_chars=0)
        self.addCleanup(worker.stop)
        return worker


class PeerSessionResetTests(_Base):
    def test_the_first_turn_resumes_the_known_session(self):
        worker = self.make_worker()
        worker.submit("你好", {"context_token": "ctx"})
        self.assertTrue(_wait_until(lambda: _FakeStreamSession.opened))
        self.assertIn("--resume", _FakeStreamSession.opened[0])
        self.assertIn("old-session-id", _FakeStreamSession.opened[0])

    def test_new_drops_the_resume_id_and_starts_cold(self):
        worker = self.make_worker()
        worker.submit("第一句", {"context_token": "ctx"})
        self.assertTrue(_wait_until(lambda: any("答：第一句" in t for _p, t in self.fake.sent)))

        worker.reset({"context_token": "ctx"})
        self.assertTrue(_wait_until(lambda: PEER not in worker.sessions),
                        "换会话后必须忘掉旧的 session id")
        self.assertEqual(client.load_sessions(self.sessions_path), {},
                         "落盘的会话表也要清掉，否则重启又接着旧会话聊")
        self.assertTrue(any(t.startswith("✅") for _p, t in self.fake.sent),
                        "得回一句确认，不然用户不知道换没换")

        opened_before = len(_FakeStreamSession.opened)
        worker.submit("第二句", {"context_token": "ctx"})
        self.assertTrue(_wait_until(lambda: len(_FakeStreamSession.opened) > opened_before),
                        "换会话后该重开一条")
        self.assertNotIn("--resume", _FakeStreamSession.opened[-1],
                         "新会话不该带上旧的 --resume")


if __name__ == "__main__":
    unittest.main()

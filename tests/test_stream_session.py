"""长命会话（clients/stream_session.py）：一个进程多轮，中途可以插话。

真的 claude 进程在测试里跑不起（要凭证、要钱、慢），所以这里用
tests/fakes/fake_stream_agent.py 说同一套协议；协议本身是拿真 claude 实测过的
（见模块 docstring）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clients.stream_session import ClaudeStreamSession, SessionDead   # noqa: E402

FAKE = ROOT / "tests" / "fakes" / "fake_stream_agent.py"


def session(*extra: str, tmp: Path | None = None, record: Path | None = None):
    command = [sys.executable, "-u", str(FAKE), *extra]
    if record is not None:
        command += ["--record", str(record)]
    made = ClaudeStreamSession(command, cwd=str(tmp or ROOT))
    made.start()
    return made


class SessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.sessions: list = []

    def tearDown(self):
        for made in self.sessions:
            made.stop()
        self._tmp.cleanup()

    def make(self, *extra: str, record: Path | None = None) -> ClaudeStreamSession:
        made = session(*extra, tmp=self.tmp, record=record)
        self.sessions.append(made)
        return made

    def test_one_process_serves_several_turns(self):
        made = self.make()
        first = made.ask("一", timeout=30)
        second = made.ask("二", timeout=30)
        self.assertEqual(first.text, "回复：一（第 1 轮）")
        self.assertEqual(second.text, "回复：二（第 2 轮）", "同一个进程该接着聊")
        self.assertEqual(first.session_id, second.session_id)
        self.assertEqual(made.session_id, "fake-session-1")

    def test_a_message_sent_while_it_works_lands_as_the_next_turn(self):
        """这就是我们能做到的 steer：话立刻收下，当前这一步一结束就用上。"""
        made = self.make("--delay", "1.0")
        made.send("先做这个")
        time.sleep(0.2)
        made.send("改做那个")                      # 正在跑的时候插话
        first = made.turn(timeout=30)
        second = made.turn(timeout=30)
        self.assertEqual(first.text, "回复：先做这个（第 1 轮）")
        self.assertEqual(second.text, "回复：改做那个（第 2 轮）",
                         "插进去的话不该被丢掉，也不该混进上一轮")

    def test_interrupt_is_a_control_request(self):
        record = self.tmp / "events.jsonl"
        made = self.make(record=record)
        made.send("慢慢来")
        rid = made.interrupt()
        turn = made.turn(timeout=30)
        self.assertTrue(turn.text, "打断不该把结果弄丢（这里是假进程，照答）")

        deadline = time.time() + 5
        sent = []
        while time.time() < deadline:
            if record.exists():
                sent = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()]
                if any(e.get("type") == "control_request" for e in sent):
                    break
            time.sleep(0.1)
        interrupts = [e for e in sent if e.get("type") == "control_request"]
        self.assertEqual(len(interrupts), 1, sent)
        self.assertEqual(interrupts[0]["request"]["subtype"], "interrupt")
        self.assertEqual(interrupts[0]["request_id"], rid)

    def test_sending_after_the_agent_exited_is_an_error(self):
        made = self.make("--die-after", "1")
        made.ask("一句", timeout=30)
        deadline = time.time() + 5
        while made.alive and time.time() < deadline:
            time.sleep(0.1)
        with self.assertRaises(SessionDead):
            made.ask("还有一句", timeout=5)

    def test_a_silent_agent_raises_instead_of_hanging(self):
        made = self.make("--silent")
        made.send("在吗")
        with self.assertRaises(SessionDead) as caught:
            made.turn(timeout=0.6)
        self.assertIn("没有结果", str(caught.exception))

    def test_stop_ends_the_process(self):
        made = self.make()
        made.ask("一句", timeout=30)
        made.stop()
        self.assertFalse(made.alive)


if __name__ == "__main__":
    unittest.main()

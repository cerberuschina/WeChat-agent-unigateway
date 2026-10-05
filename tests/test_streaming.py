"""Feedback while an agent works: typing indicator, progress notes, rendering.

WeChat cannot edit a message once sent, so there is no true token streaming on
this channel. These tests pin what we *can* do: hold the typing indicator for the
whole run, post periodic progress notes from the agent side, and make sure every
outbound answer is rendered for WeChat before it leaves the gateway.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_gateway import ilink
from agent_gateway.config import load_config
from agent_gateway.gateway import Gateway

CLIENT = Path(__file__).resolve().parents[1] / "clients" / "ilink_agent_client.py"
spec = importlib.util.spec_from_file_location("ilink_agent_client", CLIENT)
assert spec and spec.loader
client_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_mod)

PY = sys.executable


class FakeClient:
    """Stands in for the real iLink client: records what would be sent."""

    def __init__(self):
        self.typing = []
        self.sent = []
        self.account_id = "bot@im.bot"

    def context_token(self, _chat_id):
        return "ctx"

    def get_config(self, _chat_id, *, context_token=None):
        return {"typing_ticket": "ticket-1"}

    def send_typing(self, chat_id, state, *, typing_ticket="", context_token=None):
        self.typing.append((chat_id, state, typing_ticket))

    def send_text(self, chat_id, text, *, context_token=None):
        self.sent.append((chat_id, text))


def virtual_gateway(tmp: Path, *, auto_approve=("claude",)) -> Gateway:
    path = tmp / "cfg.json"
    path.write_text(json.dumps({
        "data_dir": str(tmp / "data"),
        "account": {"account_id": "acct", "token": "tok"},
        "default_agent": "claude",
        "virtual": {"enabled": True, "port": 0, "auto_approve": list(auto_approve)},
        "agents": {"claude": {"type": "virtual", "label": "Claude Code"}},
    }, ensure_ascii=False), encoding="utf-8")
    gateway = Gateway(load_config(path), dry_run=True)
    gateway.dry_run = False                 # exercise the send paths, with a fake client
    gateway.client = FakeClient()
    gateway._start_virtual()
    return gateway


class TypingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.gateway = virtual_gateway(self.tmp)

    def tearDown(self):
        self.gateway.shutdown()
        self._tmp.cleanup()

    def test_typing_starts_for_the_run_and_stops_at_the_end(self):
        self.gateway._start_typing("wx-user")
        time.sleep(0.15)
        self.assertTrue(any(state == ilink.TYPING_START for _c, state, _t in self.gateway.client.typing))

        self.gateway._stop_typing("wx-user")
        self.assertEqual(self.gateway.client.typing[-1][1], ilink.TYPING_STOP)
        self.assertFalse(self.gateway._typing_loops, "停止后不该还留着线程记录")

    def test_a_second_run_does_not_leave_two_loops_behind(self):
        self.gateway._start_typing("wx-user")
        self.gateway._start_typing("wx-user")
        self.assertEqual(len(self.gateway._typing_loops), 1)

    def test_the_agents_answer_stops_the_indicator_and_is_rendered(self):
        bind = self.gateway.virtual.bind_named("claude")
        # realistic order: the message arrives (that is how the bind learns the peer)
        self.gateway.virtual.deliver("claude", text="帮我看下", peer="wx-user", context_token="ctx")
        self.gateway._start_typing("wx-user")
        time.sleep(0.1)

        self.gateway._forward_to_wechat(bind, "# 结果\n\n**重点**：`x=1`\n\n- 一\n- 二")

        self.assertNotIn("wx-user", self.gateway._typing_loops)
        chat, text = self.gateway.client.sent[-1]
        self.assertEqual(chat, "wx-user")
        self.assertEqual(text, "【结果】\n\n重点：x=1\n\n· 一\n· 二")


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.gateway = virtual_gateway(Path(self._tmp.name))

    def tearDown(self):
        self.gateway.shutdown()
        self._tmp.cleanup()

    def test_long_answers_are_split_before_they_leave(self):
        self.gateway._send("wx-user", "段落。\n\n" + "长" * 3000)
        sent = [text for _chat, text in self.gateway.client.sent]
        self.assertGreater(len(sent), 1)
        for text in sent:
            self.assertLessEqual(len(text), self.gateway.cfg.delivery.max_chars_per_message)

    def test_code_blocks_reach_wechat_intact(self):
        self.gateway._send("wx-user", "```python\nx = a ** b\n```")
        self.assertIn("x = a ** b", self.gateway.client.sent[-1][1])


class ProgressNoteTests(unittest.TestCase):
    """The agent side must say something while a long run is going on."""

    class Args:
        stdin = False
        cwd = ""
        timeout = 30.0

        def __init__(self, every):
            self.progress_every = every

    def test_progress_notes_are_sent_while_the_runner_works(self):
        sent = []

        class Sink:
            def send_text(self, chat_id, text, *, context_token=None):
                sent.append(text)

        runner = [PY, "-c", "import time; time.sleep(1.2); print('干完了')"]
        answer = client_mod.run_with_progress(runner, "任务", self.Args(0.3), "", "wx-user",
                                              Sink(), {"context_token": "ctx"})
        self.assertEqual(answer, "干完了")
        self.assertTrue(any("还在跑" in text for text in sent), sent)

    def test_progress_can_be_disabled(self):
        sent = []

        class Sink:
            def send_text(self, chat_id, text, *, context_token=None):
                sent.append(text)

        runner = [PY, "-c", "import time; time.sleep(0.6); print('ok')"]
        client_mod.run_with_progress(runner, "任务", self.Args(0), "", "wx-user",
                                     Sink(), {})
        self.assertEqual(sent, [])

    def test_a_crashing_runner_is_reported_as_text(self):
        class Sink:
            def send_text(self, chat_id, text, *, context_token=None):
                pass

        runner = [PY, "-c", "raise SystemExit(2)"]
        answer = client_mod.run_with_progress(runner, "任务", self.Args(0), "", "wx-user",
                                              Sink(), {})
        self.assertIn("跑挂了", answer)


if __name__ == "__main__":
    unittest.main()

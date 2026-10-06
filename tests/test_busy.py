"""繁忙模式：上一条还在跑，又来一条 —— 排队还是顶掉它（Hermes 叫 busy_input_mode）。

两种模式各测一遍，外加 /queue 这个显式覆盖（"这条排后面，别打断"）。
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

from agent_gateway import router                              # noqa: E402
from agent_gateway.config import ConfigError, load_config     # noqa: E402

PEER = "wx-user"


class _FakeVirtual:
    """只实现网关在这条路径上用到的部分，并记下写了什么。"""

    def __init__(self):
        self.stops: dict = {}
        self.delivered: list = []

    def request_stop(self, peer: str, ttl: float = 120.0) -> bool:
        if not peer:
            return False
        self.stops[peer] = time.time() + ttl
        return True

    def take_stop(self, peer: str) -> bool:
        return bool(self.stops.pop(peer, 0.0) > time.time())

    def deliver(self, agent: str, *, text: str, peer: str, message_id: str = "",
                context_token: str = "") -> bool:
        self.delivered.append((agent, text, peer))
        return True

    def base_url(self) -> str:
        return "http://127.0.0.1:18500"

    def stop(self) -> None:      # gateway.shutdown() 会调它
        return None


def write_config(tmp: Path, **delivery) -> Path:
    cfg = {
        "data_dir": str(tmp / "data"),
        "account": {"account_id": "acct", "token": "tok"},
        "default_agent": "claude",
        "virtual": {"enabled": True, "port": 0, "auto_approve": ["claude"]},
        "dashboard": {"enabled": False},
        "delivery": delivery,
        "agents": {"claude": {"type": "virtual", "label": "Claude Code"}},
    }
    path = tmp / "cfg.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


class BusyModeConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_default_is_to_queue(self):
        self.assertEqual(load_config(write_config(self.tmp)).delivery.busy_mode, "queue")

    def test_interrupt_is_allowed(self):
        cfg = load_config(write_config(self.tmp, busy_mode="interrupt"))
        self.assertEqual(cfg.delivery.busy_mode, "interrupt")

    def test_a_typo_is_refused_loudly(self):
        with self.assertRaises(ConfigError):
            load_config(write_config(self.tmp, busy_mode="stear"))

    def test_steer_is_refused_with_a_reason(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(write_config(self.tmp, busy_mode="steer"))
        self.assertIn("CLI", str(caught.exception), "得说清为什么不做，别让人以为漏了")


class BusyModeGatewayTests(unittest.TestCase):
    def make(self, **delivery):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from agent_gateway.gateway import Gateway

        self.sent: list = []
        self.gateway = Gateway(load_config(write_config(Path(self._tmp.name), **delivery)),
                               dry_run=True)
        self.addCleanup(self.gateway.shutdown)
        self.gateway.dry_run = False
        self.gateway.virtual = _FakeVirtual()
        self.gateway._send = lambda chat_id, text, **_kw: self.sent.append(text)
        return self.gateway

    def say(self, text: str) -> None:
        self.gateway.handle({"from_user_id": PEER, "message_id": f"m-{time.time()}",
                             "item_list": [{"type": 1, "text_item": {"text": text}}]})

    def fall_busy(self) -> None:
        """让网关以为这个会话有活在跑（真跑时那个 typing 循环在转）。"""
        self.gateway._typing_loops[PEER] = threading.Event()

    def test_queue_mode_lets_the_running_task_finish(self):
        self.make(busy_mode="queue")
        self.fall_busy()
        self.say("顺带再看一个")
        self.assertEqual(self.gateway.virtual.stops, {}, "排队模式不该打断")
        self.assertEqual([t for _a, t, _p in self.gateway.virtual.delivered], ["顺带再看一个"])

    def test_interrupt_mode_stops_the_running_task_and_takes_over(self):
        self.make(busy_mode="interrupt")
        self.fall_busy()
        self.gateway.virtual.deliver("claude", text="上一件", peer=PEER)
        self.gateway._typing_loops[PEER] = threading.Event()

        self.say("先做这个")
        self.assertTrue(self.gateway.virtual.take_stop(PEER), "该给正在跑的那次留个 /stop 记号")
        self.assertTrue(any("先停下" in text for text in self.sent), self.sent)
        self.assertIn("先做这个", [t for _a, t, _p in self.gateway.virtual.delivered],
                      "打断了也要把新消息交过去，不然它就丢了")

    def test_interrupt_mode_does_not_stop_anything_when_nothing_runs(self):
        self.make(busy_mode="interrupt")
        self.say("闲着的时候来一条")
        self.assertEqual(self.gateway.virtual.stops, {})

    def test_slash_queue_overrides_interrupt(self):
        self.make(busy_mode="interrupt")
        self.fall_busy()
        self.say("/queue 这条排后面")
        self.assertEqual(self.gateway.virtual.stops, {}, "/queue 就是明说别打断")
        self.assertIn("这条排后面", [t for _a, t, _p in self.gateway.virtual.delivered])

    def test_slash_queue_without_text_explains_the_current_mode(self):
        self.make(busy_mode="interrupt")
        self.say("/queue")
        self.assertIn("interrupt", self.sent[-1])

    def test_the_router_marks_an_explicit_queue_request(self):
        self.make(busy_mode="interrupt")
        decision = router.route("/queue 帮我看下这个", self.gateway.cfg)
        self.assertEqual(decision.kind, "dispatch")
        self.assertEqual(decision.metadata.get("busy"), "queue")
        self.assertEqual(decision.text, "帮我看下这个")

    def test_help_mentions_both_the_mode_and_the_command(self):
        self.make()
        text = router.help_text(self.gateway.cfg, None)
        self.assertIn("/queue", text)


if __name__ == "__main__":
    unittest.main()

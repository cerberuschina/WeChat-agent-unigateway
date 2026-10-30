"""``/new``：网关把命令原样交给当前 agent，由 agent 自己换会话。

会话本体（Hermes 自己的 session、claude 客户端 ``--resume`` 的那个 id）活在 agent
进程里，网关改不了也不该假装能改——它只负责转交，以及在办不到时说清楚为什么。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_gateway.config import load_config

PEER = "wx-user"


class _FakeVirtual:
    """只实现网关在 /new 这条路上用到的部分，并记下写了什么。"""

    def __init__(self):
        self.delivered: list = []

    def deliver(self, agent: str, *, text: str, peer: str, message_id: str = "",
                context_token: str = "") -> bool:
        self.delivered.append((agent, text, peer))
        return True

    def request_stop(self, peer: str, ttl: float = 120.0) -> bool:
        return True

    def take_stop(self, peer: str) -> bool:
        return False

    def base_url(self) -> str:
        return "http://127.0.0.1:18500"

    def stop(self) -> None:      # gateway.shutdown() 会调它
        return None


def write_config(tmp: Path, agents: dict) -> Path:
    cfg = {
        "data_dir": str(tmp / "data"),
        "account": {"account_id": "acct", "token": "tok"},
        "default_agent": "claude",
        "virtual": {"enabled": True, "port": 0, "auto_approve": ["claude"]},
        "dashboard": {"enabled": False},
        "agents": agents,
    }
    path = tmp / "cfg.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


class NewSessionGatewayTests(unittest.TestCase):
    def make(self, agents=None):
        from agent_gateway.gateway import Gateway

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sent: list = []
        cfg = load_config(write_config(
            Path(self._tmp.name),
            agents or {"claude": {"type": "virtual", "label": "Claude Code"}}))
        self.gateway = Gateway(cfg, dry_run=True)
        self.addCleanup(self.gateway.shutdown)
        self.gateway.dry_run = False          # 但不会真的发微信：_send 被下面替换掉
        self.gateway.virtual = _FakeVirtual()
        self.gateway._send = lambda chat_id, text, **_kw: self.sent.append(text)
        return self.gateway

    def say(self, text: str) -> None:
        self.gateway.handle({"from_user_id": PEER, "message_id": f"m-{time.time()}",
                             "context_token": "ctx",
                             "item_list": [{"type": 1, "text_item": {"text": text}}]})

    def test_new_is_handed_to_the_current_agent(self):
        self.make()
        self.say("/new")
        self.assertEqual(self.gateway.virtual.delivered, [("claude", "/new", PEER)])
        # 确认是 agent 自己回的那一句（Hermes / claude 客户端都会回），网关不要补第二句
        self.assertEqual(self.sent, [], "网关不该再追加一句确认")

    def test_a_title_is_kept(self):
        self.make()
        self.say("/new 修那个 bug")
        self.assertEqual(self.gateway.virtual.delivered[0][1], "/new 修那个 bug")

    def test_reset_is_the_same_command(self):
        self.make()
        self.say("/reset")
        self.assertEqual(self.gateway.virtual.delivered[0][1], "/reset")

    def test_sessions_is_handed_to_the_current_agent(self):
        self.make()
        self.say("/sessions")
        self.assertEqual(self.gateway.virtual.delivered, [("claude", "/sessions", PEER)])

    def test_resume_keeps_the_number(self):
        self.make()
        self.say("/resume 3")
        self.assertEqual(self.gateway.virtual.delivered, [("claude", "/resume 3", PEER)])

    def test_a_fullwidth_slash_is_normalised_on_the_way_in(self):
        self.make()
        self.say("／resume 2")
        self.assertEqual(self.gateway.virtual.delivered[0][1], "/resume 2")

    def test_sessions_refusal_names_the_right_verb(self):
        self.make({"claude": {"type": "exec", "label": "Claude Code",
                              "command": ["echo", "ok"]}})
        self.say("/sessions")
        self.assertTrue(any("列会话" in text for text in self.sent), self.sent)

    def test_new_follows_the_sticky_agent(self):
        self.make({"claude": {"type": "virtual", "label": "Claude Code"},
                   "hermes": {"type": "virtual", "label": "Hermes"}})
        self.gateway.store.set_sticky(PEER, "hermes")
        self.say("/new")
        self.assertEqual(self.gateway.virtual.delivered, [("hermes", "/new", PEER)])

    def test_a_per_call_backend_is_told_why_it_cannot(self):
        """a2a/exec 后端是一条一条调出来的，没有会话可以换——别装作换了。"""
        self.make({"claude": {"type": "exec", "label": "Claude Code",
                              "command": ["echo", "ok"]}})
        self.say("/new")
        self.assertEqual(self.gateway.virtual.delivered, [])
        self.assertTrue(any("虚拟 iLink" in text for text in self.sent), self.sent)

    def test_an_unbound_agent_says_how_to_join(self):
        self.make()
        self.gateway.virtual.deliver = lambda *a, **kw: False
        self.say("/new")
        self.assertTrue(any("还没接入" in text for text in self.sent), self.sent)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

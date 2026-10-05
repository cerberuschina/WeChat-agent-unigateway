"""Routing rules — the part that must never be wrong, tested without any I/O."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_gateway.config import ConfigError, load_config
from agent_gateway.router import help_text, route


def make_cfg(tmp: Path, agents=None, **over):
    payload = {
        "default_agent": "hermes",
        "data_dir": str(tmp / "data"),
        "agents": agents or {
            "hermes": {"type": "exec", "label": "Hermes", "prefix": "h", "command": ["echo", "ok"]},
            "claude": {"type": "exec", "label": "Claude Code", "prefix": "c", "command": ["echo", "ok"]},
            "workbuddy": {"type": "exec", "label": "WorkBuddy", "prefix": "w", "enabled": False,
                          "command": ["echo", "ok"]},
        },
    }
    payload.update(over)
    path = tmp / "gateway.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return load_config(path)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plain_message_goes_to_default_agent(self):
        d = route("帮我看下这个报错", self.cfg)
        self.assertEqual(d.kind, "dispatch")
        self.assertEqual(d.agent, "hermes")
        self.assertEqual(d.text, "帮我看下这个报错")

    def test_sticky_agent_wins_over_default(self):
        d = route("接着聊", self.cfg, sticky="claude")
        self.assertEqual(d.agent, "claude")

    def test_prefix_one_shot(self):
        d = route("/c 修一下那个测试", self.cfg)
        self.assertEqual((d.kind, d.agent, d.text), ("dispatch", "claude", "修一下那个测试"))
        # one-shot: it must not change the sticky agent
        self.assertIsNone(d.set_sticky)

    def test_prefix_without_body_asks_for_one(self):
        d = route("/c", self.cfg)
        self.assertEqual(d.kind, "reply")
        self.assertIn("Claude Code", d.text)

    def test_use_switches_and_reports(self):
        d = route("/use claude", self.cfg)
        self.assertEqual(d.kind, "reply")
        self.assertEqual(d.set_sticky, "claude")

    def test_use_accepts_prefix_or_name(self):
        self.assertEqual(route("/use c", self.cfg).set_sticky, "claude")
        self.assertEqual(route("/use Claude", self.cfg).set_sticky, "claude")

    def test_use_unknown_agent_lists_the_roster(self):
        d = route("/use nope", self.cfg)
        self.assertEqual(d.kind, "reply")
        self.assertIn("hermes", d.text)
        self.assertIsNone(d.set_sticky)

    def test_disabled_agent_is_not_routable(self):
        d = route("/w 帮我跑一下", self.cfg)
        self.assertEqual(d.kind, "reply")  # workbuddy is disabled -> treated as unknown
        d2 = route("/use workbuddy", self.cfg)
        self.assertIsNone(d2.set_sticky)

    def test_who_reports_current(self):
        d = route("/who", self.cfg, sticky="claude")
        self.assertIn("Claude Code", d.text)
        self.assertIn("claude", d.text)

    def test_agents_lists_roster(self):
        d = route("/agents", self.cfg)
        self.assertIn("Hermes", d.text)
        self.assertIn("Claude Code", d.text)
        self.assertNotIn("WorkBuddy", d.text)  # disabled

    def test_help_is_readable(self):
        text = help_text(self.cfg, None)
        self.assertIn("/use", text)
        self.assertIn("/c", text)

    def test_unknown_command_does_not_become_a_prompt(self):
        d = route("/doesnotexist hello", self.cfg)
        self.assertEqual(d.kind, "reply")
        self.assertIn("不认识", d.text)

    def test_empty_text_is_ignored(self):
        self.assertEqual(route("   ", self.cfg).kind, "ignore")

    def test_command_name_beats_agent_prefix(self):
        """An agent whose prefix collides with a command must not hijack it."""
        cfg = make_cfg(self.tmp, agents={
            "sneaky": {"type": "exec", "label": "Sneaky", "prefix": "help", "command": ["echo", "ok"]},
            "hermes": {"type": "exec", "label": "Hermes", "prefix": "h", "command": ["echo", "ok"]},
        })
        d = route("/help", cfg)
        self.assertEqual(d.kind, "reply")
        self.assertIn("网关", d.text)

    def test_fullwidth_slash_is_understood(self):
        d = route("／c 你好", self.cfg)
        self.assertEqual((d.kind, d.agent), ("dispatch", "claude"))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_is_a_clear_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.tmp / "nope.json")
        self.assertIn("gateway.example.json", str(ctx.exception))

    def test_unknown_agent_key_is_rejected(self):
        path = self.tmp / "bad.json"
        path.write_text(json.dumps({
            "agents": {"a": {"type": "exec", "command": ["echo"], "typo": 1}},
        }), encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("typo", str(ctx.exception))

    def test_default_agent_must_exist(self):
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, default_agent="ghost")

    def test_exec_requires_a_command(self):
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, agents={"a": {"type": "exec"}})


if __name__ == "__main__":
    unittest.main()

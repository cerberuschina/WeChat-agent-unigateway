"""End-to-end in dry-run: an inbound WeChat message must reach the right agent.

Dry-run mode keeps every iLink call out of the picture, so the whole path
(dedup -> access -> route -> backend -> delivery) is exercised locally with an
``exec`` agent that just prints. That is also how you debug routing without
touching your WeChat.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_gateway.config import load_config
from agent_gateway.gateway import Gateway


def message(text: str, *, user: str = "u1", mid: str = "m1") -> dict:
    return {
        "from_user_id": user,
        "message_id": mid,
        "context_token": "ctx",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        cfg_path = self.tmp / "gateway.json"
        cfg_path.write_text(json.dumps({
            "data_dir": str(self.tmp / "data"),
            "default_agent": "hermes",
            "agents": {
                "hermes": {"type": "exec", "label": "Hermes", "prefix": "h",
                           "command": [sys.executable, "-c",
                                       "import sys;print('HERMES 收到：' + sys.argv[1])", "{text}"]},
                "claude": {"type": "exec", "label": "Claude Code", "prefix": "c",
                           "command": [sys.executable, "-c",
                                       "import sys;print('CLAUDE 收到：' + sys.argv[1])", "{text}"]},
            },
        }, ensure_ascii=False), encoding="utf-8")
        self.cfg = load_config(cfg_path)
        self.gateway = Gateway(self.cfg, dry_run=True)

    def tearDown(self):
        self.gateway.shutdown()
        self._tmp.cleanup()

    def run_message(self, text: str, **kw) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.gateway.handle(message(text, **kw))
        return buffer.getvalue()

    def test_default_agent_answers(self):
        out = self.run_message("帮我看看")
        self.assertIn("HERMES 收到：帮我看看", out)

    def test_prefix_routes_to_the_other_agent(self):
        self.assertIn("CLAUDE 收到：修一下", self.run_message("/c 修一下"))

    def test_use_then_plain_message_stays_on_that_agent(self):
        self.run_message("/use claude", mid="m-use")
        out = self.run_message("接着聊", mid="m-next")
        self.assertIn("CLAUDE 收到：接着聊", out)

    def test_one_shot_prefix_does_not_change_the_sticky_agent(self):
        self.run_message("/c 只看这一条", mid="m-c")
        out = self.run_message("回到默认", mid="m-h")
        self.assertIn("HERMES 收到：回到默认", out)

    def test_duplicate_message_id_is_processed_once(self):
        first = self.run_message("同一句", mid="dup")
        second = self.run_message("同一句", mid="dup")
        self.assertIn("HERMES 收到", first)
        self.assertEqual(second.strip(), "")

    def test_duplicate_content_under_a_new_id_is_also_dropped(self):
        self.run_message("重复的内容", mid="a")
        second = self.run_message("重复的内容", mid="b")
        self.assertEqual(second.strip(), "")

    def test_commands_are_answered_without_calling_an_agent(self):
        out = self.run_message("/agents", mid="m-agents")
        self.assertIn("Hermes", out)
        self.assertNotIn("收到", out)

    def test_unauthorized_sender_is_ignored(self):
        cfg_path = self.tmp / "restricted.json"
        cfg_path.write_text(json.dumps({
            "data_dir": str(self.tmp / "data2"),
            "agents": {"hermes": {"type": "exec", "label": "Hermes",
                                  "command": [sys.executable, "-c", "print('不该被调用')"]}},
            "access": {"allowed_users": ["someone-else"]},
        }, ensure_ascii=False), encoding="utf-8")
        gateway = Gateway(load_config(cfg_path), dry_run=True)
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                gateway.handle(message("你好"))
            self.assertEqual(buffer.getvalue().strip(), "")
        finally:
            gateway.shutdown()

    def test_an_image_message_reaches_the_agent_as_text(self):
        """媒体不再是"只认文字"的拒绝——它变成一条带路径的文本消息交给 agent。"""
        gateway = Gateway(self.cfg, dry_run=False)
        gateway.client = None  # dry-run flag off, but no iLink client: _send logs only
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                gateway.handle({"from_user_id": "u1", "message_id": "m-img",
                                "item_list": [{"type": 2, "image_item": {}}]})
            output = buffer.getvalue()
            self.assertIn("[图片]", output)
            self.assertIn("下载不了", output, "没有客户端时要说清楚，而不是假装下载过")
        finally:
            gateway.shutdown()

    def test_a_message_with_nothing_readable_gets_a_polite_reply(self):
        gateway = Gateway(self.cfg, dry_run=False)
        gateway.client = None
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                gateway.handle({"from_user_id": "u1", "message_id": "m-empty",
                                "item_list": [{"type": 9, "unknown_item": {}}]})
            self.assertIn("没有我能读的内容", buffer.getvalue())
        finally:
            gateway.shutdown()


class EntryPointTests(unittest.TestCase):
    """The command the README tells people to run must actually run."""

    def test_python_m_agent_gateway_help_works(self):
        import pathlib
        import subprocess
        root = pathlib.Path(__file__).resolve().parent.parent
        proc = subprocess.run([sys.executable, "-m", "agent_gateway", "--help"],
                              cwd=root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gateway.json", proc.stdout)

    def test_missing_config_says_what_to_do(self):
        import pathlib
        import subprocess
        root = pathlib.Path(__file__).resolve().parent.parent
        proc = subprocess.run([sys.executable, "-m", "agent_gateway", "-c", "no-such-file.json"],
                              cwd=root, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("gateway.example.json", proc.stderr)


if __name__ == "__main__":
    unittest.main()

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

from unittest import mock

from agent_gateway import gateway as gateway_module, ilink
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

    def test_the_same_text_is_taken_again_once_the_short_window_passes(self):
        self.run_message("继续", mid="c1")
        for key in list(self.gateway.store._seen):
            if key.startswith("text:"):
                self.gateway.store._seen[key] -= gateway_module.CONTENT_DEDUP_SECONDS + 1
        self.assertIn("HERMES 收到：继续", self.run_message("继续", mid="c2"))

    def test_a_repeated_command_is_not_dropped_as_duplicate_content(self):
        first = self.run_message("/agents", mid="ag1")
        second = self.run_message("/agents", mid="ag2")
        self.assertIn("Hermes", first)
        self.assertIn("Hermes", second)

    def test_a_replayed_command_with_the_same_id_is_still_dropped(self):
        self.run_message("/agents", mid="ag-same")
        self.assertEqual(self.run_message("/agents", mid="ag-same").strip(), "")

    def test_a_second_keepalive_is_answered(self):
        first = self.run_message("保活", mid="k1")
        second = self.run_message("保活", mid="k2")
        self.assertTrue(first.strip())
        self.assertTrue(second.strip())

    def test_commands_are_answered_without_calling_an_agent(self):
        out = self.run_message("/agents", mid="m-agents")
        self.assertIn("Hermes", out)
        self.assertNotIn("收到", out)

    def test_keepalive_is_answered_by_the_gateway_not_the_agent(self):
        out = self.run_message("保活", mid="m-ka")
        self.assertIn("已重置ilink限制数，除掉本条消息，当前剩余9条信息余量", out)
        self.assertNotIn("收到", out)

    def test_ilink_command_is_the_same_keepalive(self):
        out = self.run_message("/ilink", mid="m-ilink")
        self.assertIn("当前剩余9条信息余量", out)
        self.assertNotIn("收到", out)

    def test_keepalive_reports_what_is_left_after_itself(self):
        # 上一轮没发完的内容会先补发、先占额度，保活报的是它之后还剩几条。
        self.gateway._turn_counts["u1"] = 8
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.gateway._send_keepalive("u1")
        self.assertIn("当前剩余1条信息余量", buffer.getvalue())

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


class PollLoopTests(unittest.TestCase):
    """``Gateway.run`` must act on what ``getupdates`` raises, not spin past it."""

    setUp = DryRunTests.setUp
    tearDown = DryRunTests.tearDown

    def _run_with(self, client) -> int:
        self.gateway._resolve_account = lambda: setattr(self.gateway, "client", client) or True
        self.gateway._start_virtual = lambda: None
        with contextlib.redirect_stderr(io.StringIO()):
            return self.gateway.run()

    def test_an_expired_session_exits_with_code_3(self):
        class _Expired:
            account_id, base_url = "bot@im.bot", "http://x"

            def load_cursor(self):
                return ""

            def get_updates(self, cursor):
                raise ilink.SessionExpired("getupdates: ret=-14", ret=-14)

        self.assertEqual(self._run_with(_Expired()), 3)

    def test_a_rate_limit_backs_off_before_polling_again(self):
        gateway = self.gateway
        sleeps = []

        class _Throttled:
            account_id, base_url = "bot@im.bot", "http://x"
            calls = 0

            def load_cursor(self):
                return ""

            def get_updates(self, cursor):
                self.calls += 1
                if self.calls == 1:
                    raise ilink.RateLimited("getupdates: ret=-2", ret=-2)
                gateway._stop.set()
                return {"ret": 0, "msgs": []}

        with mock.patch.object(gateway_module.time, "sleep", sleeps.append):
            self.assertEqual(self._run_with(_Throttled()), 0)
        self.assertEqual(sleeps[0], gateway_module.RATE_LIMIT_BACKOFF_SECONDS)

    def test_an_instant_empty_answer_is_not_polled_again_at_once(self):
        gateway = self.gateway
        sleeps = []

        class _Instant:
            account_id, base_url = "bot@im.bot", "http://x"
            calls = 0

            def load_cursor(self):
                return ""

            def get_updates(self, cursor):
                self.calls += 1
                if self.calls == 3:
                    gateway._stop.set()
                return {"ret": 0, "msgs": []}

        with mock.patch.object(gateway_module.time, "sleep", sleeps.append):
            self._run_with(_Instant())
        self.assertEqual(sleeps, [gateway_module.MIN_EMPTY_POLL_SECONDS] * 3)


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

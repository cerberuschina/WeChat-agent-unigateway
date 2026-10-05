"""The agent-side client (clients/ilink_agent_client.py).

Only the parts that can be tested without an agent installed: how the message
becomes a command line, and how the reply is produced. The transport itself is
covered by test_virtual_ilink.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1] / "clients" / "ilink_agent_client.py"

spec = importlib.util.spec_from_file_location("ilink_agent_client", CLIENT)
assert spec and spec.loader
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)

PY = sys.executable


def echo_runner(script: str) -> list[str]:
    return [PY, "-c", script]


class RunnerTests(unittest.TestCase):
    def test_text_is_substituted_as_one_argument(self):
        answer = client.run_agent(
            echo_runner("import sys; print('收到:' + sys.argv[1])"),
            "帮我看下 这个 空指针",
            use_stdin=False, timeout=30, cwd="")
        self.assertEqual(answer, "收到:帮我看下 这个 空指针")

    def test_stdin_mode_keeps_the_command_untouched(self):
        answer = client.run_agent(
            echo_runner("import sys; print('stdin:' + sys.stdin.read())"),
            "多行\n内容", use_stdin=True, timeout=30, cwd="")
        self.assertEqual(answer, "stdin:多行\n内容")

    def test_a_command_without_a_placeholder_gets_the_text_appended(self):
        answer = client.run_agent(
            echo_runner("import sys; print(sys.argv[-1])"),
            "尾巴", use_stdin=False, timeout=30, cwd="")
        self.assertEqual(answer, "尾巴")

    def test_failure_is_reported_as_text_not_an_exception(self):
        with self.assertRaises(RuntimeError):
            client.run_agent(echo_runner("import sys; sys.stderr.write('炸了'); sys.exit(3)"),
                             "x", use_stdin=False, timeout=30, cwd="")

    def test_timeout_is_a_timeout(self):
        import subprocess

        with self.assertRaises(subprocess.TimeoutExpired):
            client.run_agent(echo_runner("import time; time.sleep(30)"),
                             "x", use_stdin=False, timeout=1, cwd="")


class SessionTests(unittest.TestCase):
    """A runner can keep one conversation per peer via the ##SESSION: marker."""

    def test_marker_is_stripped_and_returned(self):
        answer, session = client.extract_session("好的，我看下\n##SESSION:abc-123\n")
        self.assertEqual(answer, "好的，我看下")
        self.assertEqual(session, "abc-123")

    def test_output_without_a_marker_is_untouched(self):
        answer, session = client.extract_session("没有标记")
        self.assertEqual(answer, "没有标记")
        self.assertEqual(session, "")

    def test_session_is_substituted_into_the_command(self):
        answer = client.run_agent(
            [PY, "-c", "import sys; print(sys.argv[1] or '(空)')", "{session}"],
            "x", use_stdin=False, timeout=30, cwd="", session="sess-9")
        self.assertEqual(answer, "sess-9")

    def test_empty_session_still_produces_one_argument(self):
        answer = client.run_agent(
            [PY, "-c", "import sys; print(len(sys.argv), repr(sys.argv[1]))", "{session}"],
            "x", use_stdin=False, timeout=30, cwd="", session="")
        self.assertEqual(answer, "2 ''")

    def test_sessions_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            client.save_sessions(path, {"peer-a": "sess-1"})
            self.assertEqual(client.load_sessions(path), {"peer-a": "sess-1"})
            self.assertEqual(client.load_sessions(Path(tmp) / "missing.json"), {})


class CredsTests(unittest.TestCase):
    def test_creds_round_trip_keeps_the_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "creds.json"
            client.save_creds(path, {"account_id": "virt-x@im.bot", "token": "t",
                                     "base_url": "http://127.0.0.1:1", "cursor": "vcur-3"})
            loaded = client.load_creds(path)
            assert loaded is not None
            self.assertEqual(loaded["cursor"], "vcur-3")

    def test_a_broken_creds_file_is_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(client.load_creds(path))


class CommandLineTests(unittest.TestCase):
    """The runner template must survive Windows paths (regression: shlex ate them)."""

    def test_windows_backslashes_survive_the_template(self):
        import shlex

        template = r"C:\Python314\python.exe C:\tools\cc_runner.py {text}"
        parts = shlex.split(template.replace("\\", "/"))
        self.assertEqual(parts[0], "C:/Python314/python.exe")
        self.assertEqual(parts[1], "C:/tools/cc_runner.py")
        self.assertEqual(parts[2], "{text}")


if __name__ == "__main__":
    unittest.main()

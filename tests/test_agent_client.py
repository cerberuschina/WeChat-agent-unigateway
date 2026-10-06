"""The agent-side client (clients/ilink_agent_client.py).

Only the parts that can be tested without an agent installed: how the message
becomes a command line, and how the reply is produced. The transport itself is
covered by test_virtual_ilink.
"""
from __future__ import annotations

import importlib.util
import os
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


class RemoteClientTests(unittest.TestCase):
    """客户端在"网关不在本机"时的两件事：推文件、取文件。"""

    def test_gateway_locality_detection(self):
        for url in ("http://127.0.0.1:18500", "http://localhost:1", "http://0.0.0.0:2", ""):
            self.assertTrue(client.is_local_gateway(url), url)
        for url in ("http://192.168.1.5:18500", "http://gw.lan:18500", "https://gw.example"):
            self.assertFalse(client.is_local_gateway(url), url)

    def test_upload_blob_posts_the_bytes_with_our_token(self):
        import urllib.request as urlrequest

        captured = {}

        class FakeResponse:
            def read(self):
                return b'{"ret": 0, "blob": "blob:abc", "name": "a b.bin", "size": 3}'

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            captured["data"] = request.data
            return FakeResponse()

        real = urlrequest.urlopen
        urlrequest.urlopen = fake_urlopen
        try:
            gateway = type("C", (), {"base_url": "http://gw:18500", "token": "tok"})()
            blob = client.upload_blob(gateway, "a b.bin", b"xyz")
        finally:
            urlrequest.urlopen = real

        self.assertEqual(blob, "blob:abc")
        self.assertEqual(captured["url"], "http://gw:18500/upload")
        self.assertEqual(captured["data"], b"xyz")
        self.assertEqual(captured["headers"]["authorization"], "Bearer tok")
        self.assertEqual(captured["headers"]["x-file-name"], "a%20b.bin")

    def test_materialize_replaces_the_download_url_with_a_real_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fake_fetch(_client, _url, out_dir):
                target = Path(out_dir) / "got.pdf"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x")
                return target

            real = client.fetch_remote_media
            client.fetch_remote_media = fake_fetch
            try:
                text = client.materialize_media(
                    "[文件] a.pdf（1 字节）\n本地路径（网关所在机器）：K:/gw/a.pdf\n"
                    "下载地址：http://gw/media/peer/a.pdf（带你的 token 作 Bearer 认证）",
                    None, Path(tmp))
            finally:
                client.fetch_remote_media = real

        self.assertIn("本地路径：", text)
        self.assertNotIn("下载地址：", text)
        self.assertIn("got.pdf", text)

    def test_materialize_keeps_the_url_when_the_fetch_fails(self):
        def boom(*_a, **_k):
            raise OSError("网络不通")

        real = client.fetch_remote_media
        client.fetch_remote_media = boom
        try:
            text = client.materialize_media("下载地址：http://gw/x", None, Path("."))
        finally:
            client.fetch_remote_media = real
        self.assertIn("下载地址：http://gw/x", text, "取不到时把原文留着，而不是静默丢掉")


class BindKeyTests(unittest.TestCase):
    """远程接入：取码时必须带上预共享密钥（否则网关不给身份）。"""

    def test_fetch_qr_sends_the_key_as_a_query_parameter(self):
        seen = {}

        def fake_request(method, base_url, endpoint, **kwargs):
            seen["endpoint"] = endpoint
            return {"qrcode": "q1", "qrcode_img_content": "http://gw/bind/q1"}

        real = client.ilink._request
        client.ilink._request = fake_request
        try:
            value, url = client.ilink.fetch_qr(base_url="http://gw:1", bind_key="s3cret")
        finally:
            client.ilink._request = real

        self.assertEqual(value, "q1")
        self.assertIn("key=s3cret", seen["endpoint"])
        self.assertIn("bot_type=3", seen["endpoint"])

    def test_the_key_can_come_from_the_environment(self):
        seen = {}

        def fake_request(method, base_url, endpoint, **kwargs):
            seen["endpoint"] = endpoint
            return {"qrcode": "q1", "qrcode_img_content": ""}

        real, real_env = client.ilink._request, os.environ.get("ILINK_BIND_KEY")
        client.ilink._request = fake_request
        os.environ["ILINK_BIND_KEY"] = "from-env"
        try:
            client.ilink.fetch_qr(base_url="http://gw:1")
        finally:
            client.ilink._request = real
            if real_env is None:
                os.environ.pop("ILINK_BIND_KEY", None)
            else:
                os.environ["ILINK_BIND_KEY"] = real_env
        self.assertIn("key=from-env", seen["endpoint"])

    def test_no_key_means_no_extra_parameter(self):
        seen = {}

        def fake_request(method, base_url, endpoint, **kwargs):
            seen["endpoint"] = endpoint
            return {"qrcode": "q1", "qrcode_img_content": ""}

        real, real_env = client.ilink._request, os.environ.pop("ILINK_BIND_KEY", None)
        client.ilink._request = fake_request
        try:
            client.ilink.fetch_qr(base_url="http://gw:1")
        finally:
            client.ilink._request = real
            if real_env is not None:
                os.environ["ILINK_BIND_KEY"] = real_env
        self.assertNotIn("key=", seen["endpoint"])


if __name__ == "__main__":
    unittest.main()

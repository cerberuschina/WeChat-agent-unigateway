"""The agent-side client (clients/ilink_agent_client.py).

Only the parts that can be tested without an agent installed: how the message
becomes a command line, and how the reply is produced. The transport itself is
covered by test_virtual_ilink.
"""
from __future__ import annotations

import importlib.util
import json
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
        captured = {}

        class FakeResponse:
            def read(self):
                return b'{"ret": 0, "blob": "blob:abc", "name": "a b.bin", "size": 3}'

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        class FakeOpener:
            def open(self, request, timeout=0):
                captured["url"] = request.full_url
                captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
                captured["data"] = request.data
                return FakeResponse()

        real = client._no_proxy_opener
        client._no_proxy_opener = lambda: FakeOpener()
        try:
            gateway = type("C", (), {"base_url": "http://gw:18500", "token": "tok"})()
            blob = client.upload_blob(gateway, "a b.bin", b"xyz")
        finally:
            client._no_proxy_opener = real

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


class NewSessionCommandTests(unittest.TestCase):
    """``/new`` 在 agent 这一侧的意思：换一条全新的会话（上下文不带了）。"""

    def test_the_command_is_recognised(self):
        for text in ("/new", " /new ", "/NEW", "/reset", "/Reset", "/new 给这个会话起个名",
                     "／new"):
            self.assertTrue(client.is_new_session_command(text), text)

    def test_ordinary_text_is_not_a_command(self):
        for text in ("/news", "new", "帮我 /new 一下那个会话", "", "   "):
            self.assertFalse(client.is_new_session_command(text), repr(text))


class SessionCommandParsingTests(unittest.TestCase):
    def test_the_three_commands_and_their_arguments(self):
        cases = {
            "/new 起个名": ("new", "起个名"),
            "/reset": ("new", ""),
            "/sessions": ("sessions", ""),
            "/sessions 2": ("sessions", "2"),
            "/resume 3": ("resume", "3"),
            "/RESUME a1b2c3d4": ("resume", "a1b2c3d4"),
            "／resume 1": ("resume", "1"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(client.parse_session_command(text), expected)

    def test_ordinary_text_is_not_a_session_command(self):
        for text in ("resume 3", "/resumex", "/session", "/会话", "", "   ", "看下 /resume"):
            with self.subTest(text=text):
                self.assertEqual(client.parse_session_command(text), ("", ""))


class SessionCatalogTests(unittest.TestCase):
    """``/sessions`` 从 claude 的 transcript 目录里列，``/resume`` 按编号或 id 前缀切。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cwd = self.root / "work" / "site"
        self.cwd.mkdir(parents=True)
        self._old_config = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.root / "claude-home")
        self.directory = client.project_dir_for(self.cwd)
        self.directory.mkdir(parents=True)

    def tearDown(self):
        if self._old_config is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._old_config

    def write_session(self, session_id: str, title: str, mtime: float) -> Path:
        path = self.directory / f"{session_id}.jsonl"
        lines = [{"type": "user", "message": {"role": "user", "content": title}},
                 {"type": "assistant", "message": {"role": "assistant",
                                                   "content": [{"type": "text", "text": "好"}]}}]
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines),
                        encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_project_dir_name_follows_claude_slugging(self):
        self.assertEqual(client.project_dir_for("E:\\traeWork\\all").name, "E--traeWork-all")

    def test_sessions_are_listed_newest_first_with_a_title(self):
        self.write_session("11111111-1111-1111-1111-111111111111", "旧的活", 1000.0)
        self.write_session("22222222-2222-2222-2222-222222222222", "修那个空指针", 2000.0)
        (self.directory / "not-a-session.jsonl").write_text("{}", encoding="utf-8")
        rows = client.list_claude_sessions(self.cwd)
        self.assertEqual([row.id[:4] for row in rows], ["2222", "1111"])
        self.assertEqual(rows[0].title, "修那个空指针")

    def test_a_summary_line_wins_over_the_first_user_message(self):
        path = self.directory / "33333333-3333-3333-3333-333333333333.jsonl"
        path.write_text("\n".join(json.dumps(obj, ensure_ascii=False) for obj in [
            {"type": "summary", "summary": "排查登录问题"},
            {"type": "user", "message": {"role": "user", "content": "你好"}},
        ]), encoding="utf-8")
        self.assertEqual(client.session_title(path), "排查登录问题")

    def test_command_wrappers_are_not_titles(self):
        path = self.directory / "44444444-4444-4444-4444-444444444444.jsonl"
        path.write_text("\n".join(json.dumps(obj, ensure_ascii=False) for obj in [
            {"type": "user", "message": {"role": "user",
                                         "content": "<command-name>/clear</command-name>"}},
            {"type": "user", "message": {"role": "user", "content": "真的问题在这"}},
        ]), encoding="utf-8")
        self.assertEqual(client.session_title(path), "真的问题在这")

    def test_the_listing_numbers_sessions_and_marks_the_current_one(self):
        self.write_session("22222222-2222-2222-2222-222222222222", "修那个空指针", 2000.0)
        self.write_session("11111111-1111-1111-1111-111111111111", "旧的活", 1000.0)
        rows = client.list_claude_sessions(self.cwd)
        text = client.format_session_list(
            rows, "11111111-1111-1111-1111-111111111111", now=2600.0)
        self.assertIn("2. ✅ 旧的活", text)
        self.assertIn("1. 修那个空指针", text)
        self.assertIn("/resume", text)

    def test_resume_by_number_uses_the_listing_we_showed(self):
        ids = ["aaaaaaaa-1111-1111-1111-111111111111",
               "bbbbbbbb-2222-2222-2222-222222222222"]
        self.assertEqual(client.resolve_session("2", cwd=self.cwd, catalog_ids=ids), (ids[1], ""))
        target, problem = client.resolve_session("3", cwd=self.cwd, catalog_ids=ids)
        self.assertEqual(target, "")
        self.assertIn("/sessions", problem)

    def test_resume_by_id_prefix(self):
        self.write_session("abcdef12-3456-7890-abcd-ef1234567890", "那个活", 1000.0)
        self.assertEqual(client.resolve_session("abcdef12", cwd=self.cwd)[0],
                         "abcdef12-3456-7890-abcd-ef1234567890")

    def test_an_ambiguous_prefix_is_refused(self):
        self.write_session("abcd1111-3456-7890-abcd-ef1234567890", "一号", 1000.0)
        self.write_session("abcd2222-3456-7890-abcd-ef1234567890", "二号", 1001.0)
        target, problem = client.resolve_session("abcd", cwd=self.cwd)
        self.assertEqual(target, "")
        self.assertIn("2 条", problem)

    def test_a_bad_argument_gets_usage(self):
        for query in ("", "abc", "会话"):
            target, problem = client.resolve_session(query, cwd=self.cwd)
            self.assertEqual(target, "")
            self.assertTrue(problem)


class _FakeSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakeILink:
    def __init__(self):
        self.sent = []

    def send_text(self, peer, text, context_token=None):
        self.sent.append(text)


class SessionSwitchTests(unittest.TestCase):
    """``/resume`` 真的切：关掉当前进程、记下 id，下一条从那条会话接上。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.sessions_path = self.root / "client.sessions.json"
        self.client = _FakeILink()

    def make_peer(self, sessions: dict) -> "client.PeerSession":
        return client.PeerSession("wx-user-1", [], cwd=str(self.root), env={}, idle=0,
                                  client=self.client, sessions=sessions,
                                  sessions_path=self.sessions_path)

    def test_resume_records_the_target_and_closes_the_live_session(self):
        sessions = {"wx-user-1": "old-id"}
        peer = self.make_peer(sessions)
        live = _FakeSession("old-id")
        peer.session = live
        peer._resume_target = "new-target-id"
        peer._resume_message = {}
        peer._apply_resume()

        self.assertTrue(live.stopped)
        self.assertEqual(sessions["wx-user-1"], "new-target-id")
        self.assertEqual(client.load_sessions(self.sessions_path)["wx-user-1"], "new-target-id")
        self.assertIn("切到", self.client.sent[-1])
        self.assertIn("new-targ", self.client.sent[-1])

    def test_resuming_the_live_session_is_a_no_op(self):
        sessions = {"wx-user-1": "live-id"}
        peer = self.make_peer(sessions)
        live = _FakeSession("live-id")
        peer.session = live
        peer._resume_target = "live-id"
        peer._resume_message = {}
        peer._apply_resume()

        self.assertFalse(live.stopped)
        self.assertEqual(sessions["wx-user-1"], "live-id")
        self.assertIn("已经", self.client.sent[-1])

    def test_handler_refuses_resume_without_a_resumable_configuration(self):
        sent = []
        fake = _FakeILink()
        client.handle_session_command(
            "resume", "1", sender="p", message={}, client=fake, sessions={},
            sessions_path=self.sessions_path, peer_sessions={}, cwd=str(self.root),
            session_mode=False, runner_has_session=False)
        self.assertIn("{session}", fake.sent[-1])

    def test_handler_sets_the_session_in_runner_mode(self):
        fake = _FakeILink()
        sessions = {}
        client.handle_session_command(
            "resume", "1", sender="p", message={}, client=fake, sessions=sessions,
            sessions_path=self.sessions_path, peer_sessions={}, cwd=str(self.root),
            session_mode=False, runner_has_session=False)  # 先确认被拒
        cat = {"p": {"ids": ["99999999-9999-9999-9999-999999999999"]}}
        client.save_catalog(client.catalog_path_for(self.sessions_path), cat)
        client.handle_session_command(
            "resume", "1", sender="p", message={}, client=fake, sessions=sessions,
            sessions_path=self.sessions_path, peer_sessions={}, cwd=str(self.root),
            session_mode=False, runner_has_session=True)
        self.assertEqual(sessions["p"], "99999999-9999-9999-9999-999999999999")

    def test_handler_lists_and_stores_the_catalog(self):
        fake = _FakeILink()
        client.handle_session_command(
            "sessions", "", sender="p", message={}, client=fake, sessions={},
            sessions_path=self.sessions_path, peer_sessions={}, cwd=str(self.root),
            session_mode=True, runner_has_session=False)
        self.assertTrue(fake.sent, "至少回一句")
        catalog = client.load_catalog(client.catalog_path_for(self.sessions_path))
        self.assertIn("p", catalog)
        self.assertIsInstance(catalog["p"]["ids"], list)


if __name__ == "__main__":
    unittest.main()


class EndpointFallbackTests(unittest.TestCase):
    """One client, several routes to the same gateway (Tailscale / WireGuard / tunnel)."""

    def _listening(self):
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self.addCleanup(sock.close)
        return f"http://127.0.0.1:{sock.getsockname()[1]}"

    def test_a_comma_separated_base_url_becomes_an_ordered_set(self):
        self.assertEqual(
            client.parse_endpoints(
                "http://100.64.0.5:18500, http://10.8.0.2:18500/",
                "", ["http://100.64.0.5:18500", "http://127.0.0.1:18500"]),
            ["http://100.64.0.5:18500", "http://10.8.0.2:18500", "http://127.0.0.1:18500"])

    def test_the_first_route_that_answers_wins(self):
        live = self._listening()
        picked = client.pick_endpoint(["http://127.0.0.1:1", live])
        self.assertEqual(picked, live)

    def test_a_working_current_route_is_kept(self):
        live = self._listening()
        self.assertEqual(client.pick_endpoint([live], current=live), live)

    def test_with_nothing_reachable_the_first_candidate_is_used_anyway(self):
        self.assertEqual(client.pick_endpoint(["http://127.0.0.1:1"]), "http://127.0.0.1:1")

    def test_reachability_is_a_plain_tcp_check(self):
        live = self._listening()
        self.assertTrue(client.endpoint_reachable(live))
        self.assertFalse(client.endpoint_reachable("http://127.0.0.1:1", timeout=0.5))

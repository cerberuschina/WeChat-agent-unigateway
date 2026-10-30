"""start_agent_stack: 跨机器自动发现的那几个纯函数（不启动任何进程）。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import start_agent_stack as stack


class ConfigTests(unittest.TestCase):
    def _write(self, tmp: Path, payload: dict) -> Path:
        path = tmp / "gateway.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_ports_come_from_the_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = stack.load_cfg(self._write(Path(tmp), {
                "data_dir": str(Path(tmp) / "data"),
                "virtual": {"host": "127.0.0.1", "port": 19500},
                "dashboard": {"port": 19600},
            }))
            self.assertEqual(stack.virtual_port(cfg), 19500)
            self.assertEqual(stack.virtual_base(cfg), "http://127.0.0.1:19500")
            self.assertEqual(stack.dashboard_url(cfg), "http://127.0.0.1:19600")
            self.assertEqual(stack.data_dir(cfg), Path(tmp) / "data")

    def test_ports_fall_back_when_the_config_is_silent(self):
        self.assertEqual(stack.virtual_port({}), 18500)
        self.assertEqual(stack.dashboard_port({}), 18600)
        self.assertEqual(stack.virtual_base({}), "http://127.0.0.1:18500")

    def test_a_broken_config_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "gateway.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertEqual(stack.load_cfg(bad), {})
            self.assertEqual(stack.data_dir({}), stack.REPO / "data")

    def test_relative_data_dir_resolves_against_the_repo(self):
        self.assertEqual(stack.data_dir({"data_dir": "state"}), stack.REPO / "state")


class EnvAndDiscoveryTests(unittest.TestCase):
    def test_read_env_strips_quotes_and_skips_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cc.env"
            path.write_text("# 注释\nANTHROPIC_AUTH_TOKEN='tok-1'\n"
                            'export ANTHROPIC_BASE_URL="https://x"\n\nBROKEN\n',
                            encoding="utf-8")
            env = stack.read_env(path)
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "tok-1")
            self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://x")
            self.assertNotIn("BROKEN", env)

    def test_creds_prefer_the_environment(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "from-env"}, clear=False):
            creds, src = stack.find_creds(home=Path(tempfile.gettempdir()) / "no-such-home")
            self.assertEqual(creds.get("ANTHROPIC_AUTH_TOKEN"), "from-env")
            self.assertIsNone(src)

    def test_creds_fall_back_to_cc_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".a2a-bridge").mkdir()
            (home / ".a2a-bridge" / "cc.env").write_text("ANTHROPIC_AUTH_TOKEN=from-file\n",
                                                         encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
                os.environ.pop("ANTHROPIC_API_KEY", None)
                creds, src = stack.find_creds(home=home)
            self.assertEqual(creds.get("ANTHROPIC_AUTH_TOKEN"), "from-file")
            self.assertEqual(src, home / ".a2a-bridge" / "cc.env")

    def test_newest_version_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for version in ("2.0.9", "2.0.14", "1.9.30"):
                exe = root / version / "claude.exe"
                exe.parent.mkdir(parents=True)
                exe.write_text("", encoding="utf-8")
                paths.append(exe)
            self.assertEqual(stack.pick_newest(paths), root / "2.0.14" / "claude.exe")

    def test_claude_is_found_in_the_users_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            exe = home / ".local" / "bin" / ("claude.exe" if stack.IS_WIN else "claude")
            exe.parent.mkdir(parents=True)
            exe.write_text("", encoding="utf-8")
            with mock.patch("start_agent_stack.shutil.which", return_value=None):
                found = stack.find_claude_exe(home=home, localappdata=str(home / "nope"))
            self.assertEqual(found, exe)


class HostAuthScrubTests(unittest.TestCase):
    """从 Claude Code 桌面会话里启动时，别把「宿主代管认证」的标记带给 claude。"""

    def test_a_plain_terminal_env_is_left_alone(self):
        env = {"ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_BASE_URL": "https://gw"}
        self.assertEqual(stack.scrub_host_auth(dict(env)), env)

    def test_desktop_markers_and_the_host_proxy_are_dropped(self):
        env = {
            "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
            "CLAUDE_CODE_HOST_AUTH_ENV_VAR": "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH": "1",
            "CLAUDE_CODE_ENTRYPOINT": "claude-desktop-3p",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:15721/claude-desktop",
            "PATH": "/usr/bin",
        }
        cleaned = stack.scrub_host_auth(dict(env))
        for key in stack.HOST_AUTH_MARKERS:
            self.assertNotIn(key, cleaned)
        self.assertNotIn("ANTHROPIC_BASE_URL", cleaned)
        self.assertEqual(cleaned["PATH"], "/usr/bin")

    def test_start_client_hands_the_scrubbed_env_to_the_client(self):
        captured = {}

        class FakeProc:
            pid = 4321

            def poll(self):
                return None

        def fake_popen(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return FakeProc()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1"},
                                 clear=False), \
                    mock.patch("start_agent_stack.subprocess.Popen", side_effect=fake_popen), \
                    mock.patch("start_agent_stack.time.sleep"):
                pid, why = stack.start_client({"data_dir": tmp}, Path("claude"), {}, Path(tmp))
        self.assertEqual((pid, why), (4321, ""))
        self.assertNotIn("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", captured["env"])


class ProcessTests(unittest.TestCase):
    def test_pid_alive_knows_itself(self):
        self.assertTrue(stack.pid_alive(os.getpid()))

    def test_pid_alive_rejects_nonsense(self):
        self.assertFalse(stack.pid_alive(0))
        self.assertFalse(stack.pid_alive(-1))

    def test_client_pid_honours_the_pid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "client-claude.pid").write_text(str(os.getpid()), encoding="utf-8")
            pid, src = stack.client_pid({"data_dir": tmp})
            self.assertEqual(pid, os.getpid())
            self.assertEqual(src, "client-claude.pid")

    def test_client_pid_ignores_a_stale_pid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "client-claude.pid").write_text("999998", encoding="utf-8")
            with mock.patch("start_agent_stack._scan_client_pid", return_value=None):
                pid, _src = stack.client_pid({"data_dir": tmp})
            self.assertIsNone(pid)


if __name__ == "__main__":
    unittest.main()

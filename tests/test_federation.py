"""Federation: the sub-gateway server and the hub-side ``gateway`` backend."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_gateway.backends import BackendError, call_agent
from agent_gateway.config import AgentConfig, ConfigError, load_config
from agent_gateway.federation import (
    FederationSettings,
    build_server,
    load_federation_settings,
)

TOKEN = "s3cret-token"


def _config(agents, federation=None):
    raw = {
        "data_dir": "data",
        "default_agent": next(iter(agents)),
        "agents": agents,
        "virtual": {"enabled": True},
        "federation": federation or {
            "enabled": True, "host": "127.0.0.1", "port": 0,
            "token": TOKEN, "allowed_agents": [],
        },
    }
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "gateway.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return tmp, load_config(path), raw


def _echo_agent():
    return {
        "type": "exec",
        "label": "Echo",
        "prefix": "e",
        "command": [sys.executable, "-c",
                    "import sys; print('echo:' + sys.stdin.read().strip())"],
    }


class SettingsTests(unittest.TestCase):
    def test_disabled_is_an_error(self):
        with self.assertRaises(ConfigError):
            load_federation_settings({"federation": {"enabled": False, "token": TOKEN}})

    def test_missing_token_is_an_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_federation_settings({"federation": {"enabled": True}})
        self.assertIn("token", str(ctx.exception))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.cfg, self.raw = _config({
            "echo": _echo_agent(),
            "virt": {"type": "virtual", "label": "Virtual", "prefix": "v"},
            "other": _echo_agent(),
        })
        self.settings = load_federation_settings(self.raw)
        self.server = build_server(self.cfg, self.settings, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _post(self, body, token=TOKEN):
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/federation/send",
            data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path="/federation/health", token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_lists_non_virtual_agents(self):
        status, body = self._get()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("echo", body["agents"])
        self.assertNotIn("virt", body["agents"])

    def test_health_requires_token(self):
        status, body = self._get(token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_send_round_trips_through_the_backend(self):
        status, body = self._post({"agent": "echo", "text": "你好", "peer": "u1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["reply"], "echo:你好")
        self.assertEqual(body["agent"], "echo")

    def test_unknown_agent_is_404(self):
        status, body = self._post({"agent": "nope", "text": "x"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "unknown_agent")

    def test_virtual_agent_is_refused(self):
        status, body = self._post({"agent": "virt", "text": "x"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "virtual_agent_not_federated")

    def test_missing_text_is_400(self):
        status, body = self._post({"agent": "echo"})
        self.assertEqual(status, 400)

    def test_wrong_token_is_401(self):
        status, body = self._post({"agent": "echo", "text": "x"}, token="wrong")
        self.assertEqual(status, 401)

    def test_unknown_path_is_404(self):
        status, _ = self._get("/nope")
        self.assertEqual(status, 404)

    def test_allowed_agents_limits_the_surface(self):
        tmp, cfg, raw = _config(
            {"echo": _echo_agent(), "other": _echo_agent()},
            {"enabled": True, "host": "127.0.0.1", "port": 0,
             "token": TOKEN, "allowed_agents": ["echo"]})
        try:
            settings = load_federation_settings(raw)
            server = build_server(cfg, settings, host="127.0.0.1", port=0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/federation/send",
                data=json.dumps({"agent": "other", "text": "x"}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {TOKEN}"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 403)
            server.shutdown()
            server.server_close()
        finally:
            tmp.cleanup()


class HubBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.cfg, self.raw = _config({"echo": _echo_agent()})
        self.settings = load_federation_settings(self.raw)
        self.server = build_server(self.cfg, self.settings, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _hub_agent(self, token=TOKEN):
        return AgentConfig(
            name="studio-echo", type="gateway", label="Studio Echo", prefix="se",
            url=f"http://127.0.0.1:{self.port}/federation/send",
            token=token, remote_agent="echo", timeout=15)

    def test_gateway_backend_returns_the_sub_gateway_reply(self):
        reply = call_agent(self._hub_agent(), "帮我看看", context_id="wx:user-1")
        self.assertEqual(reply, "echo:帮我看看")

    def test_bad_token_becomes_a_backend_error(self):
        with self.assertRaises(BackendError):
            call_agent(self._hub_agent(token="wrong"), "x")

    def test_unknown_remote_agent_is_a_backend_error(self):
        agent = self._hub_agent()
        agent.remote_agent = "nope"
        with self.assertRaises(BackendError) as ctx:
            call_agent(agent, "x")
        self.assertIn("unknown_agent", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

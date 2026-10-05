"""Virtual iLink: agents believe they are talking to WeChat, but talk to us.

Covers both layers: the endpoint bodies (fast, direct) and the real HTTP surface
(the agent's actual path: GET the QR, poll the status, then long-poll updates).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from agent_gateway.config import ConfigError, load_config
from agent_gateway.gateway import Gateway
from agent_gateway.virtual_ilink import VirtualILinkServer

TOKEN_HEADERS = {"Content-Type": "application/json"}


def post(url: str, payload: dict, token: str = "") -> dict:
    headers = dict(TOKEN_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


class BindFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = VirtualILinkServer(data_dir=Path(self._tmp.name))

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def test_qr_then_wait_then_approved(self):
        qr = self.server.ep_qrcode()
        self.assertEqual(qr["ret"], 0)
        self.assertTrue(qr["qrcode"])
        self.assertIn(qr["qrcode"], qr["qrcode_img_content"], "二维码要指向我们自己的批准页")

        self.assertEqual(self.server.ep_qrcode_status(qr["qrcode"])["status"], "wait")

        bind = self.server.approve(qr["qrcode"], name="hermes")
        confirmed = self.server.ep_qrcode_status(qr["qrcode"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertTrue(confirmed["ilink_bot_id"].startswith("virt-hermes"))
        self.assertTrue(confirmed["ilink_bot_id"].endswith("@im.bot"), "形状要跟真的一样")
        self.assertEqual(confirmed["bot_token"], bind.token)
        self.assertTrue(confirmed["baseurl"].startswith("http://127.0.0.1"))

    def test_unknown_qrcode_is_expired_not_an_error(self):
        self.assertEqual(self.server.ep_qrcode_status("nope")["status"], "expired")

    def test_rejected_bind_disappears(self):
        qr = self.server.ep_qrcode()
        self.assertTrue(self.server.reject(qr["qrcode"]))
        self.assertEqual(self.server.ep_qrcode_status(qr["qrcode"])["status"], "expired")

    def test_auto_approve_skips_the_operator(self):
        server = VirtualILinkServer(data_dir=Path(self._tmp.name) / "auto", auto_approve=["claude"])
        try:
            bind = server.request_bind("claude")
            self.assertTrue(bind.confirmed)
            self.assertEqual(server.agent_names(), ["claude"])
        finally:
            server.stop()

    def test_token_lookup(self):
        bind = self.server.request_bind("x")
        self.server.approve(bind.qrcode)
        self.assertIs(self.server._bind_for_token(bind.token), bind)
        self.assertIsNone(self.server._bind_for_token("bogus"))


class MessageFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sent = []
        self.server = VirtualILinkServer(data_dir=Path(self._tmp.name),
                                        auto_approve=["claude"],
                                        on_outbound=lambda bind, text: self.sent.append((bind.name, text)))
        self.bind = self.server.request_bind("claude")

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def test_getupdates_is_empty_without_a_message(self):
        cursor, messages = self.bind.drain("", timeout=0.2)
        self.assertEqual(messages, [])
        self.assertTrue(cursor)

    def test_delivered_message_looks_like_wechat(self):
        self.assertTrue(self.server.deliver("claude", text="你好", peer="wx-user",
                                            message_id="m1", context_token="ctx"))
        _, messages = self.bind.drain("", timeout=0.2)
        self.assertEqual(len(messages), 1)
        msg = messages[0]
        self.assertEqual(msg["from_user_id"], "wx-user")
        self.assertEqual(msg["context_token"], "ctx")
        self.assertEqual(msg["item_list"][0]["text_item"]["text"], "你好")
        self.assertEqual(msg["to_user_id"], self.bind.account_id)

    def test_queue_is_drained_not_replayed(self):
        self.server.deliver("claude", text="一次", peer="wx-user")
        self.assertEqual(len(self.bind.drain("", 0.1)[1]), 1)
        self.assertEqual(len(self.bind.drain("", 0.1)[1]), 0, "同一条不该被投两次")

    def test_deliver_to_an_unknown_agent_is_refused(self):
        self.assertFalse(self.server.deliver("nobody", text="hi", peer="wx-user"))

    def test_sendmessage_forwards_to_the_real_wechat(self):
        result = self.server.ep_sendmessage(self.bind, {"msg": {"item_list": [
            {"type": 1, "text_item": {"text": "回答"}}]}})
        self.assertEqual(result["ret"], 0)
        self.assertEqual(self.sent, [("claude", "回答")])

    def test_empty_or_media_only_send_is_rejected_not_swallowed(self):
        result = self.server.ep_sendmessage(self.bind, {"msg": {"item_list": [
            {"type": 2, "image_item": {}}]}})
        self.assertNotEqual(result["ret"], 0, "媒体还不支持，要明确报错而不是静默丢掉")


class HttpSurfaceTests(unittest.TestCase):
    """The real path an agent takes: HTTP, not direct method calls."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = VirtualILinkServer(data_dir=Path(self._tmp.name), port=0)
        self.host, self.port = self.server.start()
        self.base = f"http://{self.host}:{self.port}"

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def test_agent_can_walk_the_whole_login_over_http(self):
        qr = get(f"{self.base}/ilink/bot/get_bot_qrcode?bot_type=3")
        self.assertEqual(qr["ret"], 0)
        self.assertEqual(get(f"{self.base}/ilink/bot/get_qrcode_status?qrcode={qr['qrcode']}")["status"], "wait")

        approved = post(f"{self.base}/admin/approve", {"qrcode": qr["qrcode"], "name": "hermes"})
        self.assertTrue(approved["ok"])

        done = get(f"{self.base}/ilink/bot/get_qrcode_status?qrcode={qr['qrcode']}")
        self.assertEqual(done["status"], "confirmed")
        token = done["bot_token"]

        # an authenticated call works, an anonymous one looks like an expired session
        self.assertEqual(post(f"{self.base}/ilink/bot/sendtyping", {}, token=token)["ret"], 0)
        self.assertEqual(post(f"{self.base}/ilink/bot/sendtyping", {})["ret"], -14)

    def test_health_lists_confirmed_agents(self):
        self.server.request_bind("claude")
        self.server.approve(self.server.binds()[0]["qrcode"])
        health = get(f"{self.base}/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["binds"], ["claude"])


class WiringTests(unittest.TestCase):
    """Config + gateway wiring: no A2A call is involved for a virtual agent."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _config(self, *, enabled: bool, agent_type: str = "virtual") -> Path:
        path = self.tmp / f"cfg-{agent_type}-{int(enabled)}.json"
        path.write_text(json.dumps({
            "data_dir": str(self.tmp / "data"),
            "default_agent": "claude",
            "virtual": {"enabled": enabled, "port": 0, "auto_approve": ["claude"]},
            "agents": {"claude": {"type": agent_type, "label": "Claude Code", "prefix": "c",
                                  **({"command": [sys.executable, "-c", "print('x')"]}
                                     if agent_type == "exec" else {})}},
        }, ensure_ascii=False), encoding="utf-8")
        return path

    def test_virtual_agent_without_virtual_mode_is_a_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._config(enabled=False))
        self.assertIn("virtual.enabled", str(ctx.exception))

    def test_dispatch_reaches_the_virtual_queue_without_calling_any_backend(self):
        cfg = load_config(self._config(enabled=True))
        gateway = Gateway(cfg, dry_run=True)
        try:
            gateway._start_virtual()
            self.assertIsNotNone(gateway.virtual)
            bind = gateway.virtual.bind_named("claude")
            self.assertIsNotNone(bind, "auto_approve 应当直接放行")

            gateway.handle({"from_user_id": "wx-user", "message_id": "m1",
                            "context_token": "ctx",
                            "item_list": [{"type": 1, "text_item": {"text": "帮我看下"}}]})

            _, messages = bind.drain("", timeout=0.2)
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["item_list"][0]["text_item"]["text"], "帮我看下")
        finally:
            gateway.shutdown()

    def test_agent_that_has_not_bound_yet_gets_a_readable_reply(self):
        cfg = load_config(self._config(enabled=True))
        cfg.virtual.auto_approve = []
        gateway = Gateway(cfg, dry_run=True)
        try:
            gateway._start_virtual()
            gateway.handle({"from_user_id": "wx-user", "message_id": "m1",
                            "item_list": [{"type": 1, "text_item": {"text": "在吗"}}]})
            # dry-run prints instead of sending; no crash and no bind is the point
            self.assertIsNone(gateway.virtual.bind_named("claude"))
        finally:
            gateway.shutdown()


if __name__ == "__main__":
    unittest.main()

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


class BindPersistenceTests(unittest.TestCase):
    """A gateway restart must not cost the agent another scan.

    The agent keeps the token it was handed; the whole point of writing binds to
    disk is that ``_bind_for_token`` still recognises it afterwards.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _server(self, **kwargs) -> VirtualILinkServer:
        return VirtualILinkServer(data_dir=self.dir, **kwargs)

    def test_identity_and_token_survive_a_restart(self):
        first = self._server()
        try:
            bind = first.ensure_bind("hermes", issued=True)
            account_id, token = bind.account_id, bind.token
        finally:
            first.stop()

        second = self._server()
        try:
            restored = second.bind_named("hermes")
            self.assertIsNotNone(restored, "重启后身份应当还在")
            self.assertEqual(restored.account_id, account_id)
            self.assertEqual(restored.token, token)
            self.assertIs(second._bind_for_token(token), restored,
                          "agent 手里的 token 必须仍然认得出它")
            self.assertFalse(restored.expired)
        finally:
            second.stop()

    def test_unapproved_bind_is_not_restored(self):
        first = self._server()
        try:
            first.request_bind("stranger")
        finally:
            first.stop()

        second = self._server()
        try:
            self.assertIsNone(second.bind_named("stranger"), "没批准过的接入不该复活")
            self.assertEqual(second.agent_names(), [])
        finally:
            second.stop()

    def test_cursor_and_context_token_come_back(self):
        first = self._server(auto_approve=["claude"])
        try:
            first.ensure_bind("claude", issued=True)
            first.deliver("claude", text="hi", peer="wx-1", context_token="ctx-1")
            first.bind_named("claude").drain("", timeout=0.2)
            seq = first.bind_named("claude").cursor_seq
        finally:
            first.stop()

        second = self._server(auto_approve=["claude"])
        try:
            restored = second.bind_named("claude")
            self.assertEqual(restored.cursor_seq, seq)
            self.assertEqual(restored.context_tokens.get("wx-1"), "ctx-1")
        finally:
            second.stop()

    def test_restored_identity_is_not_handed_to_a_new_agent(self):
        first = self._server(auto_approve=["claude"])
        try:
            first.ensure_bind("claude", issued=True)
        finally:
            first.stop()

        second = self._server(auto_approve=["claude"])
        try:
            fresh = second.ep_qrcode()["qrcode"]
            self.assertEqual(second.ep_qrcode_status(fresh)["status"], "wait",
                             "已发出的身份不能再发给另一个 agent")
            self.assertNotEqual(second.bind_named("claude").qrcode, fresh)
        finally:
            second.stop()

    def test_rejected_bind_does_not_come_back(self):
        first = self._server()
        try:
            bind = first.request_bind("gone")
            first.approve(bind.qrcode)
            first.reject(bind.qrcode)
        finally:
            first.stop()

        second = self._server()
        try:
            self.assertIsNone(second.bind_named("gone"))
        finally:
            second.stop()


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

    def test_an_empty_send_is_rejected_not_swallowed(self):
        result = self.server.ep_sendmessage(self.bind, {"msg": {"item_list": []}})
        self.assertNotEqual(result["ret"], 0, "空消息要明确报错，而不是静默丢掉")

    def test_media_items_go_to_the_items_callback(self):
        from agent_gateway import media as media_mod

        got = []
        self.server.on_outbound_items = lambda bind, items: got.append((bind.name, items))
        item = media_mod.build_media_item(media_mod.ITEM_FILE, encrypt_query_param="localpath:C:/x/a.pdf",
                                          aes_key=bytes(16), filename="a.pdf",
                                          plaintext_size=10, ciphertext_size=16)
        result = self.server.ep_sendmessage(self.bind, {"msg": {"item_list": [item]}})
        self.assertEqual(result["ret"], 0)
        self.assertEqual(got[0][0], "claude")
        self.assertEqual(got[0][1][0]["file_item"]["file_name"], "a.pdf")

    def test_a_text_and_a_file_in_one_message_reach_both_callbacks(self):
        from agent_gateway import media as media_mod

        texts, files = [], []
        self.server.on_outbound_items = lambda bind, items: files.append(items)
        item = media_mod.build_media_item(media_mod.ITEM_IMAGE, encrypt_query_param="localpath:C:/x/a.png",
                                          aes_key=bytes(16), filename="a.png",
                                          plaintext_size=10, ciphertext_size=16)
        result = self.server.ep_sendmessage(self.bind, {"msg": {"item_list": [
            {"type": 1, "text_item": {"text": "看这个图"}}, item]}})
        self.assertEqual(result["ret"], 0)
        self.assertEqual(self.sent, [("claude", "看这个图")])
        self.assertEqual(files[0][0]["image_item"]["media"]["encrypt_query_param"],
                         "localpath:C:/x/a.png")


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

    def test_a_heartbeat_hits_the_typing_callback_and_sends_no_message(self):
        """心跳必须是"不产生微信消息"的路径——否则它就是在偷额度。"""
        seen = []
        self.server.on_typing = lambda bind, state: seen.append((bind.name, state))
        qr = get(f"{self.base}/ilink/bot/get_bot_qrcode?bot_type=3")
        post(f"{self.base}/admin/approve", {"qrcode": qr["qrcode"], "name": "hermes"})
        token = get(f"{self.base}/ilink/bot/get_qrcode_status?qrcode={qr['qrcode']}")["bot_token"]

        result = post(f"{self.base}/ilink/bot/sendtyping",
                      {"state": 1, "ilink_user_id": "wx-user"}, token=token)

        self.assertEqual(result["ret"], 0)
        self.assertEqual(seen, [("hermes", 1)])

    def test_a_heartbeat_with_a_bogus_state_still_counts_as_working(self):
        seen = []
        self.server.on_typing = lambda bind, state: seen.append(state)
        self.server.request_bind("claude")
        bind = self.server.approve(self.server.binds()[0]["qrcode"], name="claude")
        result = post(f"{self.base}/ilink/bot/sendtyping", {"state": "?"}, token=bind.token)
        self.assertEqual(result["ret"], 0)
        self.assertEqual(seen, [1])


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


class TokenReuseTests(unittest.TestCase):
    """An agent already bound to the real WeChat keeps working through us.

    Hermes' weixin channel reads WEIXIN_BASE_URL for its message calls, but its
    QR-login URL is hard-wired to Tencent — so it cannot re-bind through the
    gateway. It must be able to keep the token it holds.
    """

    REAL = "real-token-from-tenant"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = VirtualILinkServer(data_dir=Path(self._tmp.name), port=0,
                                         accept_tokens={self.REAL: "hermes"})
        self.host, self.port = self.server.start()
        self.base = f"http://{self.host}:{self.port}"

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def test_real_token_resolves_to_a_named_virtual_identity(self):
        bind = self.server._bind_for_token(self.REAL)
        self.assertIsNotNone(bind)
        self.assertEqual(bind.name, "hermes")
        self.assertTrue(bind.confirmed)
        self.assertEqual(self.server.agent_names(), ["hermes"])

    def test_an_unknown_token_is_still_refused(self):
        self.assertIsNone(self.server._bind_for_token("someone-elses-token"))
        self.assertEqual(post(f"{self.base}/ilink/bot/sendtyping", {} , token="bogus")["ret"], -14)

    def test_http_calls_with_the_real_token_are_served(self):
        self.assertEqual(post(f"{self.base}/ilink/bot/sendtyping", {}, token=self.REAL)["ret"], 0)

    def test_reuse_bind_is_never_handed_out_as_a_qr(self):
        reuse = self.server._bind_for_token(self.REAL)
        fresh = self.server.ep_qrcode()
        self.assertNotEqual(fresh["qrcode"], reuse.qrcode,
                            "复用真 token 的身份不能被别人扫码领走")

    def test_messages_reach_the_reusing_agent_and_answers_come_back(self):
        out = []
        self.server.on_outbound = lambda bind, text: out.append((bind.name, text))
        bind = self.server._bind_for_token(self.REAL)
        self.server.deliver("hermes", text="在吗", peer="wx-owner", context_token="ctx")
        _, messages = bind.drain("", 0.2)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.server.ep_sendmessage(bind, {"msg": {"item_list": [
            {"type": 1, "text_item": {"text": "在"}}]}})["ret"], 0)
        self.assertEqual(out, [("hermes", "在")])


class ReuseWiringTests(unittest.TestCase):
    def test_gateway_wires_the_real_token_into_the_virtual_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text(json.dumps({
                "data_dir": str(Path(tmp) / "data"),
                "account": {"account_id": "acct", "token": "the-real-token"},
                "default_agent": "hermes",
                "virtual": {"enabled": True, "port": 0, "reuse_real_token_for": "hermes"},
                "agents": {"hermes": {"type": "virtual", "label": "Hermes"}},
            }, ensure_ascii=False), encoding="utf-8")
            cfg = load_config(path)
            gateway = Gateway(cfg, dry_run=True)
            try:
                gateway._start_virtual()
                bind = gateway.virtual._bind_for_token("the-real-token")
                self.assertIsNotNone(bind, "网关应当把真 token 交给虚拟层复用")
                self.assertEqual(bind.name, "hermes")
            finally:
                gateway.shutdown()

    def test_naming_an_unknown_agent_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text(json.dumps({
                "data_dir": str(Path(tmp) / "data"),
                "virtual": {"enabled": True, "reuse_real_token_for": "nobody"},
                "agents": {"hermes": {"type": "virtual"}},
            }, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("reuse_real_token_for", str(ctx.exception))


class RemoteAgentTests(unittest.TestCase):
    """非本机 agent：预共享密钥取码 + 上传/下载媒体（不共享文件系统）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.server = VirtualILinkServer(data_dir=self.tmp / "data" / "virtual", port=0,
                                         bind_key="s3cret",
                                         public_url="http://10.0.0.5:18500")
        self.host, self.port = self.server.start()
        self.base = f"http://{self.host}:{self.port}"
        qr = get(f"{self.base}/ilink/bot/get_bot_qrcode?key=s3cret")
        post(f"{self.base}/admin/approve", {"qrcode": qr["qrcode"], "name": "claude"})
        self.token = get(f"{self.base}/ilink/bot/get_qrcode_status?qrcode={qr['qrcode']}")["bot_token"]

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    @staticmethod
    def post_raw(url, data, token="", filename=""):
        headers = {"Content-Type": "application/octet-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if filename:
            headers["X-File-Name"] = urllib.parse.quote(filename)
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:      # 401 是预期答案之一
            body = exc.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"ret": -1, "http": exc.code, "body": body}

    @staticmethod
    def get_bytes(url, token=""):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_qr_without_the_bind_key_is_refused(self):
        result = get(f"{self.base}/ilink/bot/get_bot_qrcode")
        self.assertNotEqual(result["ret"], 0, "没有密钥就不该发码")
        self.assertIn("bind_key", str(result.get("errmsg", "")))

    def test_public_base_prefers_public_url(self):
        self.assertEqual(self.server.public_base(), "http://10.0.0.5:18500")
        self.assertTrue(self.server.media_url("peer", "a.pdf").startswith("http://10.0.0.5:18500/media/"))

    def test_a_remote_agent_can_push_bytes_and_get_a_blob(self):
        data = "远端文件内容".encode() * 3
        payload = self.post_raw(f"{self.base}/upload", data, token=self.token, filename="报表.pdf")
        self.assertEqual(payload["ret"], 0)
        self.assertTrue(payload["blob"].startswith("blob:"))
        self.assertEqual(payload["size"], len(data))
        path = self.server.blob_path(payload["blob"])
        self.assertIsNotNone(path)
        self.assertEqual(path.read_bytes(), data)
        self.assertTrue(path.name.endswith("报表.pdf"))

    def test_upload_requires_a_valid_token(self):
        self.assertNotEqual(self.post_raw(f"{self.base}/upload", b"x")["ret"], 0)
        self.assertNotEqual(self.post_raw(f"{self.base}/upload", b"x", token="bogus")["ret"], 0)

    def test_an_empty_upload_is_refused(self):
        self.assertNotEqual(self.post_raw(f"{self.base}/upload", b"", token=self.token)["ret"], 0)

    def test_an_unknown_blob_is_not_resolved(self):
        self.assertIsNone(self.server.blob_path("blob:deadbeef"))

    def test_media_download_serves_a_stored_file_to_the_owner(self):
        folder = self.tmp / "data" / "media" / "peer"
        folder.mkdir(parents=True)
        (folder / "a.pdf").write_bytes(b"stored-bytes")
        status, body = self.get_bytes(f"{self.base}/media/peer/a.pdf", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"stored-bytes")

    def test_media_download_requires_a_token(self):
        status, _body = self.get_bytes(f"{self.base}/media/peer/a.pdf")
        self.assertEqual(status, 401)

    def test_media_download_refuses_to_escape_the_media_root(self):
        outside = self.tmp / "secret.txt"
        outside.write_bytes(b"nope")
        self.assertIsNone(self.server.read_media("../secret.txt"))
        self.assertIsNone(self.server.read_media("peer/../../secret.txt"))


class PortCollisionTests(unittest.TestCase):
    def test_two_virtual_servers_cannot_share_a_port(self):
        first = VirtualILinkServer(data_dir=Path(tempfile.mkdtemp()), port=0)
        first.start()
        try:
            again = VirtualILinkServer(data_dir=Path(tempfile.mkdtemp()), port=first.port)
            with self.assertRaises(OSError):
                again.start()
        finally:
            first.stop()


if __name__ == "__main__":
    unittest.main()

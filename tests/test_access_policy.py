"""公网暴露的准入模型：人工批准 + 白名单。

批准接口默认只对本机开放——否则把端口暴露到公网，陌生人可以自己批准自己，
"人工批准"就成了一句空话。agent 侧接口则按 allow_cidrs 白名单放行（未配置时不限来源，
但仍要 bind_key、身份 token 和人工批准）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_gateway.virtual_ilink import VirtualILinkServer


def opener():
    """Loopback must not go through the machine's HTTP proxy."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url: str, token: str = "") -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener().open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


class PolicyUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = VirtualILinkServer(data_dir=Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_admin_defaults_to_localhost_only(self):
        self.assertTrue(self.server.may_admin("127.0.0.1"))
        self.assertTrue(self.server.may_admin("::1"))
        self.assertFalse(self.server.may_admin("192.168.1.9"))
        self.assertFalse(self.server.may_admin("240e:3b7::5"))

    def test_admin_can_be_widened_but_only_explicitly(self):
        other = VirtualILinkServer(data_dir=Path(self._tmp.name) / "b",
                                   admin_cidrs=["10.0.0.0/8", "240e:3b7:1a2b::/48"])
        self.assertTrue(other.may_admin("10.1.2.3"))
        self.assertTrue(other.may_admin("240e:3b7:1a2b::9"))
        self.assertFalse(other.may_admin("127.0.0.1"), "给了白名单就不再隐含本机")

    def test_api_is_unrestricted_until_a_whitelist_is_set(self):
        self.assertTrue(self.server.may_use_api("240e:3b7::5"))
        scoped = VirtualILinkServer(data_dir=Path(self._tmp.name) / "c",
                                    allow_cidrs=["240e:3b7:1a2b::/48", "192.168.1.0/24"])
        self.assertTrue(scoped.may_use_api("240e:3b7:1a2b::9"))
        self.assertTrue(scoped.may_use_api("192.168.1.44"))
        self.assertFalse(scoped.may_use_api("240e:dead::1"))
        self.assertFalse(scoped.may_use_api("127.0.0.1"), "白名单之外一律不放行，本机也一样")

    def test_bare_addresses_and_brackets_are_accepted(self):
        self.assertTrue(self.server._ip_in("::1", ["::1"]))
        self.assertTrue(self.server._ip_in("[::1]", ["::1/128"]))
        self.assertTrue(self.server._ip_in("10.0.0.1", ["10.0.0.1"]))

    def test_garbage_never_matches(self):
        self.assertFalse(self.server._ip_in("", ["127.0.0.1/32"]))
        self.assertFalse(self.server._ip_in("not-an-ip", ["127.0.0.1/32"]))
        self.assertFalse(self.server._ip_in("127.0.0.1", ["nonsense/99"]))

    def test_policy_hint_says_what_is_missing(self):
        self.assertIn("allow_cidrs", self.server.policy_hint())
        scoped = VirtualILinkServer(data_dir=Path(self._tmp.name) / "d",
                                    allow_cidrs=["192.168.1.0/24"])
        self.assertIn("192.168.1.0/24", scoped.policy_hint())


class EnforcedOverHttpTests(unittest.TestCase):
    """白名单/管理员网段要真的在 HTTP 层拦住，而不只是写在文档里。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def start(self, **kwargs) -> VirtualILinkServer:
        server = VirtualILinkServer(data_dir=self.tmp / "data" / "virtual", port=0, **kwargs)
        server.start()
        return server

    def test_localhost_may_reach_the_admin_page_by_default(self):
        server = self.start()
        try:
            status, payload = fetch(f"{server.base_url()}/admin/binds")
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
        finally:
            server.stop()

    def test_a_narrowed_admin_list_locks_localhost_out(self):
        server = self.start(admin_cidrs=["10.0.0.0/8"])
        try:
            for path in ("/admin/binds", "/bind/whatever", "/health"):
                status, payload = fetch(f"{server.base_url()}{path}")
                self.assertEqual(status, 403, path)
                self.assertIn("allow_cidrs", str(payload.get("hint", "")))
        finally:
            server.stop()

    def test_the_api_whitelist_blocks_unlisted_callers(self):
        server = self.start(allow_cidrs=["10.0.0.0/8"])
        try:
            status, _payload = fetch(f"{server.base_url()}/ilink/bot/get_bot_qrcode?key=x")
            self.assertEqual(status, 403)
        finally:
            server.stop()

    def test_the_api_whitelist_admits_listed_callers(self):
        server = self.start(allow_cidrs=["127.0.0.0/8", "::1/128"])
        try:
            status, payload = fetch(f"{server.base_url()}/ilink/bot/get_bot_qrcode")
            self.assertEqual(status, 200)
            self.assertEqual(payload["ret"], 0)
        finally:
            server.stop()

    def test_approval_still_requires_the_bind_key_even_when_allowed(self):
        server = self.start(bind_key="s3cret")
        try:
            status, payload = fetch(f"{server.base_url()}/ilink/bot/get_bot_qrcode")
            self.assertEqual(status, 200)
            self.assertNotEqual(payload["ret"], 0, "没有 bind_key 就不该发码")
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()

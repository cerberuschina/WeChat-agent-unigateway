"""IPv6: the URL shaping, and a real bind/serve over ::1.

The server must not assume IPv4: ``ThreadingHTTPServer`` is AF_INET by default,
so ``host: "::"`` used to fail at bind time, and URL formatting used to produce
``http://::1:18500`` (unusable). Both are covered here, including a live request
over the IPv6 loopback when the machine has one.
"""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_gateway.virtual_ilink import VirtualILinkServer


def has_ipv6() -> bool:
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        probe.bind(("::1", 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class UrlShapeTests(unittest.TestCase):
    def test_ipv6_loopback_gets_brackets(self):
        server = VirtualILinkServer(host="::1", port=18500, data_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(server.base_url(), "http://[::1]:18500")

    def test_a_full_ipv6_address_gets_brackets(self):
        server = VirtualILinkServer(host="240e:3b7:1a2b::5", port=18500,
                                    data_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(server.base_url(), "http://[240e:3b7:1a2b::5]:18500")

    def test_ipv4_is_left_alone(self):
        server = VirtualILinkServer(host="192.168.1.5", port=18500,
                                    data_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(server.base_url(), "http://192.168.1.5:18500")

    def test_an_already_bracketed_host_is_not_double_wrapped(self):
        server = VirtualILinkServer(host="[::1]", port=18500, data_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(server.base_url(), "http://[::1]:18500")

    def test_public_url_wins_for_media_links(self):
        server = VirtualILinkServer(host="::1", port=18500, public_url="http://[240e::9]:18500",
                                    data_dir=Path(tempfile.mkdtemp()))
        self.assertTrue(server.media_url("peer", "a.pdf").startswith("http://[240e::9]:18500/media/"))
        self.assertEqual(server.public_base(), "http://[240e::9]:18500")


@unittest.skipUnless(has_ipv6(), "这台机器没有可用的 IPv6 回环")
class LiveIpv6Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = VirtualILinkServer(host="::1", port=0, data_dir=Path(self._tmp.name))
        self.host, self.port = self.server.start()
        self.base = self.server.base_url()

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def _open(self, url: str):
        """Loopback must not go through an HTTP proxy (the host machine has one)."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(url, timeout=5)

    def test_it_really_serves_over_ipv6(self):
        with self._open(f"{self.base}/health") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "virtual-ilink")

    def test_the_bind_address_is_an_ipv6_socket(self):
        self.assertEqual(self.server._httpd.address_family, socket.AF_INET6)
        self.assertEqual(self.server._httpd.server_address[1], self.port)

    def test_a_login_flow_works_over_ipv6(self):
        with self._open(f"{self.base}/ilink/bot/get_bot_qrcode") as response:
            qr = json.loads(response.read().decode("utf-8"))
        self.assertEqual(qr["ret"], 0)
        self.assertIn("[::1]", qr["qrcode_img_content"], "批准页链接也要是能点的 IPv6 URL")

    def test_ipv4_loopback_is_not_reachable_on_an_ipv6_only_bind(self):
        """::1 只监听 IPv6；要同时收 IPv4 得绑 "::"（防火墙/双栈见文档）。"""
        try:
            self._open(f"http://127.0.0.1:{self.port}/health").close()
        except (urllib.error.URLError, ConnectionError, OSError):
            return
        self.skipTest("这台机器的 ::1 同时收了 IPv4（双栈），不算失败")


class ProxyBypassTests(unittest.TestCase):
    """局域网/本机目标不能走系统代理（否则会得到 502，而不是连接错误）。"""

    def test_private_targets_are_recognised(self):
        from agent_gateway.ilink import _is_private_host

        self.assertTrue(_is_private_host("127.0.0.1"))
        self.assertTrue(_is_private_host("localhost"))
        self.assertTrue(_is_private_host("::1"))
        self.assertTrue(_is_private_host("[::1]"))
        self.assertTrue(_is_private_host("192.168.1.5"))
        self.assertTrue(_is_private_host("10.0.0.7"))
        self.assertTrue(_is_private_host("172.16.0.1"))
        self.assertTrue(_is_private_host("172.31.255.254"))
        self.assertTrue(_is_private_host("169.254.10.1"))
        self.assertTrue(_is_private_host("fe80::1"))
        self.assertTrue(_is_private_host("fd00::1"))

    def test_public_targets_keep_the_default_opener(self):
        from agent_gateway.ilink import _is_private_host, _opener_for

        self.assertFalse(_is_private_host("ilinkai.weixin.qq.com"))
        self.assertFalse(_is_private_host("172.32.0.1"))
        self.assertFalse(_is_private_host("240e:3b7::5"))
        self.assertIsNone(_opener_for("https://ilinkai.weixin.qq.com/ilink/bot/getupdates"))
        self.assertIsNotNone(_opener_for("http://192.168.1.5:18500/health"))
        self.assertIsNotNone(_opener_for("http://[::1]:18500/health"))


if __name__ == "__main__":
    unittest.main()

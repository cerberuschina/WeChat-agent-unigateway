"""/stop：手机上一句话，停掉正在跑的活。

四层各测一遍：路由器认这个命令、网关留记号并回话、虚拟服务端把记号交给 agent（只给一次）、
客户端真的把子进程杀掉（这条是真的起进程、真的杀）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_gateway import router                                              # noqa: E402
from agent_gateway.virtual_ilink import VirtualILinkServer                    # noqa: E402

CLIENT = ROOT / "clients" / "ilink_agent_client.py"
spec = importlib.util.spec_from_file_location("ilink_agent_client", CLIENT)
assert spec and spec.loader
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class StubClient:
    """只带客户端协议需要的那两个属性。"""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token


def get(url: str, token: str = "") -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


class StopEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = VirtualILinkServer(data_dir=Path(self._tmp.name), port=0,
                                         accept_tokens={"tok-claude": "claude"})
        self.server.start()
        self.base = self.server.base_url().rstrip("/")

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def test_one_stop_is_delivered_once(self):
        self.assertTrue(self.server.request_stop("wx-user"))
        self.assertTrue(self.server.take_stop("wx-user"), "agent 该看到这次喊停")
        self.assertFalse(self.server.take_stop("wx-user"), "看过一次就该清掉，别追着杀下一个任务")

    def test_a_stale_stop_expires(self):
        self.server.request_stop("wx-user", ttl=-1)      # 已经过期
        self.assertFalse(self.server.take_stop("wx-user"))

    def test_an_empty_peer_has_nobody_to_stop(self):
        self.assertFalse(self.server.request_stop(""))

    def test_the_agent_asks_over_http_and_only_once(self):
        stub = StubClient(self.base, "tok-claude")
        self.assertFalse(client.stop_requested(stub, "wx-user"))

        self.server.request_stop("wx-user")
        self.assertTrue(client.stop_requested(stub, "wx-user"))
        self.assertFalse(client.stop_requested(stub, "wx-user"))

    def test_a_stranger_cannot_ask(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            get(f"{self.base}/agent/stop?peer=wx-user", token="nope")
        self.assertEqual(caught.exception.code, 401)


class ClientKillTests(unittest.TestCase):
    """真的起一个长进程，然后真的因为 /stop 被杀掉。"""

    def test_stop_kills_the_running_agent(self):
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        started = time.time()
        with self.assertRaises(client.RunnerStopped):
            client.run_agent([sys.executable, "-c", "import time; time.sleep(60)"], "",
                             use_stdin=False, timeout=120, cwd="", stop=stop)
        self.assertLess(time.time() - started, 20, "不该等满 timeout")

    def test_without_stop_the_answer_comes_back_as_before(self):
        answer = client.run_agent([sys.executable, "-c", "print('ok')"], "",
                                  use_stdin=False, timeout=60, cwd="")
        self.assertEqual(answer.strip(), "ok")


CFG = {
    "data_dir": "",
    "account": {"account_id": "acct", "token": "tok"},
    "default_agent": "claude",
    "virtual": {"enabled": True, "port": 0, "auto_approve": ["claude"]},
    "dashboard": {"enabled": False},
    "agents": {"claude": {"type": "virtual", "label": "Claude Code"}},
}


class GatewayStopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        cfg = dict(CFG)
        cfg["data_dir"] = str(tmp / "data")
        path = tmp / "cfg.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

        from agent_gateway.config import load_config
        from agent_gateway.gateway import Gateway

        self.sent: list = []
        self.gateway = Gateway(load_config(path), dry_run=True)
        self.gateway.dry_run = False
        self.gateway._send = lambda chat_id, text, **_kw: self.sent.append(text)
        self.cfg = self.gateway.cfg

    def tearDown(self):
        self.gateway.virtual = None          # 假对象没有人家的 shutdown
        self.gateway.shutdown()
        self._tmp.cleanup()

    def say(self, text: str) -> None:
        self.gateway.handle({"from_user_id": "wx-user", "message_id": f"m-{time.time()}",
                             "item_list": [{"type": 1, "text_item": {"text": text}}]})

    def test_the_router_knows_stop(self):
        decision = router.route("/stop", self.cfg)
        self.assertEqual(decision.kind, "stop")
        self.assertIn("/stop", router.help_text(self.cfg, None))

    def test_stop_is_never_forwarded_to_the_agent(self):
        # 它自己是控制指令：转发给 agent 就变成一句普通的话了。
        self.assertEqual(router.route("/stop", self.cfg).agent, "")

    def test_stop_leaves_a_flag_the_client_can_pick_up(self):
        self.gateway.virtual = self.gateway.virtual or _FakeVirtual()
        self.say("/stop")
        self.assertTrue(any("停" in text for text in self.sent), self.sent)
        self.assertTrue(self.gateway.virtual.take_stop("wx-user"), "记号要留给客户端收")

    def test_the_acknowledgement_tells_the_truth_about_what_is_running(self):
        self.gateway.virtual = _FakeVirtual()
        self.say("/stop")                                  # 没有任务在跑
        self.assertIn("没看到有任务在跑", self.sent[-1])

        # 网关按内容去重，所以第二句得说点不一样的（真人也一样，会多打几个字）
        self.gateway._typing_loops["wx-user"] = threading.Event()   # 假装有活在跑
        self.say("/stop 停一下")
        self.assertIn("已经喊停", self.sent[-1])

    def test_without_a_virtual_server_it_says_so(self):
        self.gateway.virtual = None
        self.say("/stop")
        self.assertIn("没有东西可停", self.sent[-1])


class _FakeVirtual:
    """只实现网关用到的两个方法（免得为一条命令起整个 HTTP 服务）。"""

    def __init__(self):
        self.stops: dict = {}

    def request_stop(self, peer: str, ttl: float = 120.0) -> bool:
        if not peer:
            return False
        self.stops[peer] = time.time() + ttl
        return True

    def take_stop(self, peer: str) -> bool:
        return bool(self.stops.pop(peer, 0.0) > time.time())


if __name__ == "__main__":
    unittest.main()

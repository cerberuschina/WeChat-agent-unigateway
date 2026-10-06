"""控制台：状态快照、两个写动作（批准/拒绝）、日志/消息环、访问门。"""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_gateway.dashboard import Dashboard, LogRing, TrafficRing


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url: str) -> tuple[int, str]:
    try:
        with opener().open(url, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def post(url: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with opener().open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


class FakeBind:
    def __init__(self, name: str, qrcode: str):
        self.name = name
        self.qrcode = qrcode

    def as_dict(self):
        return {"name": self.name, "qrcode": self.qrcode, "account_id": f"virt-{self.name}@im.bot"}


class RingTests(unittest.TestCase):
    def test_log_ring_keeps_the_tail(self):
        ring = LogRing(capacity=3)
        ring.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("ring-test")
        logger.addHandler(ring)
        logger.setLevel(logging.INFO)
        for i in range(5):
            logger.info("line %d", i)
        logger.removeHandler(ring)
        tail = ring.tail()
        self.assertEqual([item["text"] for item in tail], ["line 2", "line 3", "line 4"])

    def test_traffic_ring_is_newest_first_and_bounded(self):
        ring = TrafficRing(capacity=2)
        ring.add("in", "peer", "claude", "一")
        ring.add("out", "peer", "claude", "二")
        ring.add("in", "peer", "claude", "三")
        tail = ring.tail()
        self.assertEqual([item["text"] for item in tail], ["三", "二"])
        self.assertEqual(tail[0]["kind"], "in")


class DashboardHttpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.calls = []

        def snapshot():
            return {"running": True, "account": "77f33f61…", "virtual_url": "http://127.0.0.1:18500",
                    "agents": [{"name": "claude", "label": "Claude Code", "type": "virtual",
                                "bound": True, "account_id": "virt-claude@im.bot", "queue": 0}],
                    "virtual": {"host": "127.0.0.1", "port": 18500, "bind_key": "k",
                                "admin_cidrs": ["127.0.0.1/32"], "pending": [
                                    {"qrcode": "q1", "name": "agent", "client_ip": "10.1.2.3",
                                     "created_at": 1_700_000_000}]},
                    "delivery": {"max_messages_per_turn": 10, "reserve_for_answer": 2,
                                 "per_peer": {"wx-user": {"used": 3, "left": 7}}}}

        def approve(qrcode, name):
            self.calls.append(("approve", qrcode, name))
            return FakeBind(name or "agent", qrcode)

        def reject(qrcode):
            self.calls.append(("reject", qrcode, ""))
            return True

        self.dashboard = Dashboard(snapshot=snapshot, approve=approve, reject=reject,
                                   may_admin=lambda ip: True, port=0)
        self.dashboard.start()
        self.base = self.dashboard.base_url().rstrip("/")

    def tearDown(self):
        self.dashboard.stop()
        self._tmp.cleanup()

    def test_the_page_renders_the_console(self):
        status, html = get(f"{self.base}/")
        self.assertEqual(status, 200)
        for marker in ("agent-gateway 控制台", "待批准的接入", "每轮消息额度", "最近消息", "/api/state"):
            self.assertIn(marker, html, marker)

    def test_state_carries_gateway_traffic_and_logs(self):
        self.dashboard.traffic.add("in", "wx-user", "claude", "你好")
        status, body = get(f"{self.base}/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["gateway"]["running"])
        self.assertEqual(payload["traffic"][0]["text"], "你好")
        self.assertIn("agents", payload["gateway"])
        self.assertIn("logs", payload)

    def test_approve_and_reject_reach_the_gateway(self):
        status, payload = post(f"{self.base}/api/approve", {"qrcode": "q1", "name": "claude"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["bind"]["account_id"], "virt-claude@im.bot")

        status, payload = post(f"{self.base}/api/reject", {"qrcode": "q2"})
        self.assertTrue(payload["ok"])
        self.assertEqual(self.calls, [("approve", "q1", "claude"), ("reject", "q2", "")])

    def test_unknown_routes_are_not_silent(self):
        status, _ = get(f"{self.base}/api/nope")
        self.assertEqual(status, 404)
        status, _ = post(f"{self.base}/api/nope", {})
        self.assertEqual(status, 404)

    def test_broken_json_is_a_400(self):
        request = urllib.request.Request(f"{self.base}/api/approve", data=b"{oops",
                                         method="POST", headers={"Content-Type": "application/json"})
        try:
            with opener().open(request, timeout=5) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 400)


class DashboardAccessTests(unittest.TestCase):
    """控制台不能因为把端口暴露出去就跟着裸奔。"""

    def test_a_non_admin_caller_is_refused(self):
        dashboard = Dashboard(snapshot=lambda: {}, approve=lambda *a: None,
                              reject=lambda *a: None, may_admin=lambda ip: False, port=0)
        dashboard.start()
        try:
            base = dashboard.base_url().rstrip("/")
            for path in ("/", "/api/state"):
                status, body = get(f"{base}{path}")
                self.assertEqual(status, 403, path)
                self.assertIn("admin_cidrs", body)
        finally:
            dashboard.stop()

    def test_a_non_admin_cannot_approve(self):
        dashboard = Dashboard(snapshot=lambda: {}, approve=lambda *a: FakeBind("x", "y"),
                              reject=lambda *a: True, may_admin=lambda ip: False, port=0)
        dashboard.start()
        try:
            status, payload = post(f"{dashboard.base_url().rstrip('/')}/api/approve",
                                   {"qrcode": "q1"})
            self.assertEqual(status, 403)
            self.assertFalse(payload["ok"])
        finally:
            dashboard.stop()


class GatewayConsoleTests(unittest.TestCase):
    """控制台接在网关上：快照要反映真实的绑定与额度，环形缓冲要真的记东西。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        path = self.tmp / "cfg.json"
        path.write_text(json.dumps({
            "data_dir": str(self.tmp / "data"),
            "account": {"account_id": "acct", "token": "tok"},
            "default_agent": "claude",
            "virtual": {"enabled": True, "port": 0, "auto_approve": ["claude"]},
            "dashboard": {"enabled": True, "port": 0},
            "agents": {"claude": {"type": "virtual", "label": "Claude Code"}},
        }, ensure_ascii=False), encoding="utf-8")
        from agent_gateway.config import load_config
        from agent_gateway.gateway import Gateway

        class FakeClient:
            account_id = "bot@im.bot"

            def context_token(self, _chat_id):
                return "ctx"

            def get_config(self, *_a, **_k):
                return {"typing_ticket": ""}

            def send_text(self, _chat_id, _text, *, context_token=None):
                return {"ret": 0}

        self.gateway = Gateway(load_config(path), dry_run=True)
        self.gateway.dry_run = False          # 出站要走真代码路径才会进消息环
        self.gateway.client = FakeClient()
        self.gateway._start_virtual()

    def tearDown(self):
        self.gateway.shutdown()
        self._tmp.cleanup()

    def test_the_console_starts_with_the_gateway(self):
        self.assertIsNotNone(self.gateway.dashboard)
        status, html = get(self.gateway.dashboard.base_url().rstrip("/") + "/")
        self.assertEqual(status, 200)
        self.assertIn("agent-gateway 控制台", html)

    def test_the_snapshot_reflects_bindings_and_budget(self):
        state = self.gateway.snapshot()
        self.assertTrue(state["running"])
        self.assertEqual([a["name"] for a in state["agents"]], ["claude"])
        self.assertTrue(state["agents"][0]["bound"], "auto_approve 的 agent 应当已接入")
        self.assertEqual(state["delivery"]["max_messages_per_turn"], 10)
        self.assertEqual(state["virtual"]["port"], self.gateway.virtual.port)

    def test_messages_show_up_in_the_traffic_ring(self):
        self.gateway.handle({"from_user_id": "wx-user", "message_id": "m-1",
                             "item_list": [{"type": 1, "text_item": {"text": "帮我看看"}}]})
        self.gateway.virtual.deliver("claude", text="帮我看看", peer="wx-user")
        self.gateway._forward_to_wechat(self.gateway.virtual.bind_named("claude"), "看完了")

        traffic = self.gateway.traffic.tail()
        kinds = [item["kind"] for item in traffic]
        self.assertIn("in", kinds, traffic)
        self.assertIn("out", kinds, traffic)
        self.assertIn("帮我看看", [item["text"] for item in traffic])

    def test_the_page_state_endpoint_serves_what_the_page_expects(self):
        """页面 JS 读的字段必须真的在快照里——缺一个就是空白面板。"""
        self.gateway.traffic.add("in", "wx-user", "claude", "你好")
        status, body = get(self.gateway.dashboard.base_url().rstrip("/") + "/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)

        self.assertEqual(sorted(payload), ["gateway", "logs", "now", "traffic"])
        gateway = payload["gateway"]
        for key in ("running", "account", "base_url", "virtual_url", "default_agent",
                    "dry_run", "agents", "virtual", "delivery", "pending_output"):
            self.assertIn(key, gateway, key)
        for key in ("host", "port", "bind_key", "allow_cidrs", "admin_cidrs", "pending", "binds"):
            self.assertIn(key, gateway["virtual"], key)
        for key in ("max_messages_per_turn", "reserve_for_answer", "per_peer"):
            self.assertIn(key, gateway["delivery"], key)
        for key in ("name", "label", "type", "bound", "account_id", "queue", "last_seen"):
            self.assertIn(key, gateway["agents"][0], key)
        self.assertEqual(payload["traffic"][0]["kind"], "in")
        self.assertTrue(isinstance(payload["logs"], list))

    def test_the_page_html_carries_the_render_hooks(self):
        """页面自己得带渲染函数与刷新循环，否则服务端再对也是死页面。"""
        status, html = get(self.gateway.dashboard.base_url().rstrip("/") + "/")
        self.assertEqual(status, 200)
        for hook in ("function render(", "function decide(", "setInterval(refresh", "/api/state",
                     "/api/approve", "/api/reject", "id=\"agents\"", "id=\"pending\"", "id=\"budget\""):
            self.assertIn(hook, html, hook)


class PortCollisionTests(unittest.TestCase):
    """端口不能被两个进程悄悄共享（Windows 上默认的 SO_REUSEADDR 会允许）。"""

    def test_a_second_dashboard_on_the_same_port_fails_loudly(self):
        first = Dashboard(snapshot=lambda: {}, approve=lambda *a: None, reject=lambda *a: None,
                          may_admin=lambda ip: True, port=0)
        first.start()
        try:
            again = Dashboard(snapshot=lambda: {}, approve=lambda *a: None, reject=lambda *a: None,
                              may_admin=lambda ip: True, port=first.port)
            with self.assertRaises(OSError):
                again.start()
        finally:
            first.stop()


class DashboardOptionalTests(unittest.TestCase):
    """控制台是锦上添花：端口被占用时网关必须照常干活。"""

    def test_a_busy_console_port_does_not_take_the_gateway_down(self):
        blocker = Dashboard(snapshot=lambda: {}, approve=lambda *a: None,
                            reject=lambda *a: None, may_admin=lambda ip: True, port=0)
        blocker.start()
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "cfg.json"
            path.write_text(json.dumps({
                "data_dir": str(Path(tmp.name) / "data"),
                "account": {"account_id": "acct", "token": "tok"},
                "default_agent": "claude",
                "virtual": {"enabled": True, "port": 0, "auto_approve": ["claude"]},
                "dashboard": {"enabled": True, "port": blocker.port},
                "agents": {"claude": {"type": "virtual", "label": "Claude Code"}},
            }, ensure_ascii=False), encoding="utf-8")

            from agent_gateway.config import load_config
            from agent_gateway.gateway import Gateway

            gateway = Gateway(load_config(path), dry_run=True)
            gateway.client = None
            gateway._start_virtual()          # 端口冲突不该抛出去
            try:
                self.assertIsNone(gateway.dashboard, "起不来就别假装起来了")
                self.assertIsNotNone(gateway.virtual, "虚拟服务必须照常起来")
                self.assertTrue(gateway.snapshot()["running"])
            finally:
                gateway.shutdown()
        finally:
            blocker.stop()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()

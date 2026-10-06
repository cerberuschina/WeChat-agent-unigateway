"""放行卡：agent 问、人在手机上答。

三层各测一遍：broker 的纯逻辑（超时=拒绝）、虚拟服务端的两个接口（问 + 等），
以及网关把它接进微信命令（/approve → 解释 → 给 agent 答复）。
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_gateway import router
from agent_gateway.approvals import (ALLOW, DENY, EXPIRED, PENDING, ApprovalBroker,
                                     short_id)
from agent_gateway.virtual_ilink import VirtualILinkServer


def post(url: str, payload: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def get(url: str, token: str = "") -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.broker = ApprovalBroker(ttl=60, clock=lambda: self.now[0])

    def ask(self, **kw):
        kw.setdefault("agent", "claude")
        kw.setdefault("peer", "wx-user")
        kw.setdefault("title", "想跑 npm test")
        return self.broker.request(**kw)

    def test_ids_stay_typable_on_a_phone(self):
        ids = [short_id(n) for n in range(0, 40)]
        self.assertEqual(ids[0], "2")
        for value in ids:
            self.assertFalse(set(value) & set("01loi"), value)
        self.assertEqual(len(set(ids)), len(ids), "编号不能重复")

    def test_the_question_carries_the_id_the_command_and_the_deadline(self):
        approval = self.ask(detail="npm test -- --runInBand")
        text = self.broker.describe(approval)
        self.assertIn(approval.id, text)
        self.assertIn("/approve", text)
        self.assertIn("npm test", text, "用户要看清放行的是哪条命令")
        self.assertIn("60 秒", text, "得说清多久不回就作废")

    def test_silence_expires_to_deny_never_to_allow(self):
        approval = self.ask(ttl=30)
        self.now[0] += 31
        self.broker.expire()
        self.assertEqual(self.broker.get(approval.id).decision, EXPIRED)
        waited = self.broker.wait(approval.id, timeout=1)
        self.assertEqual(waited.decision, EXPIRED)
        self.assertIn("超时", self.broker.verdict_text(waited))

    def test_a_verdict_is_final(self):
        approval = self.ask()
        self.assertIsNotNone(self.broker.resolve(approval.id, ALLOW, by="wx-user"))
        self.assertIsNone(self.broker.resolve(approval.id, DENY, by="wx-user"),
                          "已经答过的不能再翻盘")
        self.assertEqual(self.broker.get(approval.id).decision, ALLOW)

    def test_only_allow_and_deny_are_verdicts(self):
        approval = self.ask()
        with self.assertRaises(ValueError):
            self.broker.resolve(approval.id, "maybe")

    def test_wait_returns_as_soon_as_the_human_answers(self):
        approval = self.ask()
        threading.Timer(0.15, self.broker.resolve, args=(approval.id, DENY)).start()
        started = time.time()
        answer = self.broker.wait(approval.id, timeout=5)
        self.assertEqual(answer.decision, DENY)
        self.assertLess(time.time() - started, 4, "不该等到超时")

    def test_unknown_id_is_not_an_error(self):
        self.assertIsNone(self.broker.get("zzzz"))
        self.assertIsNone(self.broker.resolve("zzzz", ALLOW))
        self.assertIsNone(self.broker.newest_pending("wx-user"))

    def test_a_bare_approve_only_sees_this_peers_question(self):
        mine = self.ask()
        self.ask(peer="someone-else", title="别人的事")
        newest = self.broker.newest_pending("wx-user")
        self.assertEqual(newest.id, mine.id)

    # -- /always: 本轮全放行 ------------------------------------------------

    def test_always_releases_everything_already_waiting(self):
        first, second = self.ask(title="跑测试"), self.ask(title="改文件")
        released = self.broker.allow_all("wx-user", window=600, by="wx-user")
        self.assertEqual({a.id for a in released}, {first.id, second.id},
                         "一次要说清放掉了哪几张，不能只放最新那张")
        self.assertEqual(self.broker.get(first.id).decision, ALLOW)
        self.assertEqual(self.broker.get(second.id).decided_by, "wx-user")

    def test_always_then_new_questions_are_answered_without_asking(self):
        self.broker.allow_all("wx-user", window=600, by="wx-user")
        later = self.ask(title="又一件事")
        self.assertEqual(later.decision, ALLOW, "开着 /always 就不该再挂起一张卡")
        self.assertEqual(later.decided_by, "always")
        self.assertIn("/always", later.reason, "自动放行的得留下是谁松的口")

    def test_always_expires_back_to_asking(self):
        self.broker.allow_all("wx-user", window=600, by="wx-user")
        self.now[0] += 601
        self.assertEqual(self.ask(title="窗口过了之后").decision, PENDING,
                         "窗口一过就得重新问人——自动放行不能永不过期")

    def test_always_does_not_leak_to_another_peer(self):
        self.broker.allow_all("wx-user", window=600, by="wx-user")
        self.assertEqual(self.ask(peer="someone-else", title="别人的事").decision, PENDING)

    def test_clearing_always_puts_the_questions_back(self):
        self.broker.allow_all("wx-user", window=600, by="wx-user")
        self.broker.clear_auto("wx-user")
        self.assertEqual(self.ask(title="他又开口之后").decision, PENDING,
                         "用户再说话就该收回，哪怕窗口没到")


class ApprovalEndpointTests(unittest.TestCase):
    """虚拟服务端的 /agent/approval：问一句，然后挂着等答复。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.asked: list = []
        self.broker = ApprovalBroker(ttl=20)
        self.server = VirtualILinkServer(
            data_dir=Path(self._tmp.name), port=0,
            accept_tokens={"tok-claude": "claude"},
            approvals=self.broker,
            on_approval=lambda bind, approval: self.asked.append((bind.name, approval.id)))
        self.server.start()
        self.base = self.server.base_url().rstrip("/")

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def test_an_agent_asks_and_then_collects_the_verdict(self):
        started = post(f"{self.base}/agent/approval",
                       {"peer": "wx-user", "title": "想跑 git status",
                        "detail": "git status --short", "kind": "command"},
                       token="tok-claude")
        approval_id = started["id"]
        self.assertEqual(self.asked, [("claude", approval_id)], "网关得被告知去问人")
        self.assertEqual(get(f"{self.base}/agent/approval/{approval_id}",
                             token="tok-claude")["approval"]["decision"], PENDING)

        self.broker.resolve(approval_id, ALLOW, by="wx-user")     # 相当于用户回 /approve
        answered = get(f"{self.base}/agent/approval/{approval_id}", token="tok-claude")
        self.assertEqual(answered["approval"]["decision"], ALLOW)
        self.assertEqual(answered["approval"]["decided_by"], "wx-user")

    def test_wait_blocks_until_the_answer_arrives(self):
        approval_id = post(f"{self.base}/agent/approval",
                           {"title": "想改一个文件"}, token="tok-claude")["id"]
        seen = {}

        def collect():
            seen["answer"] = get(
                f"{self.base}/agent/approval/{approval_id}/wait?timeout=10", token="tok-claude")

        worker = threading.Thread(target=collect, daemon=True)
        started = time.time()
        worker.start()
        time.sleep(0.3)
        self.assertTrue(worker.is_alive(), "还没人答，它就该挂着")
        self.broker.resolve(approval_id, DENY, by="wx-user")
        worker.join(timeout=10)
        self.assertEqual(seen["answer"]["approval"]["decision"], DENY)
        self.assertLess(time.time() - started, 9)

    def test_a_stranger_without_a_token_cannot_ask(self):
        # iLink 的习惯是错误放在 body 里（ret != 0），不是 HTTP 状态码。
        answer = post(f"{self.base}/agent/approval", {"title": "让我来"}, token="nope")
        self.assertNotEqual(answer.get("ret"), 0, answer)
        self.assertEqual(self.asked, [], "没身份就不该惊动人")

    def test_ask_without_a_broker_is_a_clean_refusal(self):
        server = VirtualILinkServer(data_dir=Path(self._tmp.name) / "plain", port=0,
                                    accept_tokens={"tok-claude": "claude"})
        server.start()
        try:
            answer = post(f"{server.base_url().rstrip('/')}/agent/approval",
                          {"title": "试试"}, token="tok-claude")
            self.assertNotEqual(answer.get("ret"), 0)
            self.assertIn("approvals", answer.get("errmsg", ""))
        finally:
            server.stop()


CFG = {
    "data_dir": "",           # filled in setUp
    "account": {"account_id": "acct", "token": "tok"},
    "default_agent": "claude",
    "virtual": {"enabled": True, "port": 0, "auto_approve": ["claude"]},
    "dashboard": {"enabled": False},
    "agents": {"claude": {"type": "virtual", "label": "Claude Code"}},
}


class FakeClient:
    account_id = "bot@im.bot"

    def context_token(self, _chat_id):
        return "ctx"

    def get_config(self, *_a, **_k):
        return {"typing_ticket": ""}

    def send_text(self, *_a, **_k):
        return {"ret": 0}


class GatewayApprovalTests(unittest.TestCase):
    """/approve 是路由器认的命令，网关负责兑现并把话回给用户。"""

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
        self.gateway.client = FakeClient()

        def capture(chat_id, text, **_kw):
            self.sent.append(text)

        self.gateway._send = capture
        self.cfg = self.gateway.cfg

    def tearDown(self):
        self.gateway.shutdown()
        self._tmp.cleanup()

    def test_the_router_knows_approve_and_reject(self):
        decision = router.route("/approve k7", self.cfg)
        self.assertEqual((decision.kind, decision.text), ("approve", "k7"))
        self.assertEqual(router.route("/reject", self.cfg).kind, "reject")
        self.assertEqual(router.route("/no 2m4", self.cfg).kind, "reject")
        self.assertIn("/approve", router.help_text(self.cfg, None))

    def test_the_router_knows_always(self):
        self.assertEqual(router.route("/always", self.cfg).kind, "always")
        self.assertIn("/always", router.help_text(self.cfg, None))

    def test_always_releases_the_open_card_and_then_stops_asking(self):
        self.gateway._last_peer = "wx-user"
        waiting = self.gateway.approvals.request(agent="claude", peer="wx-user",
                                                 title="想跑 npm test")

        self.gateway.handle({"from_user_id": "wx-user", "message_id": "m-a1",
                             "item_list": [{"type": 1, "text_item": {"text": "/always"}}]})
        self.assertEqual(self.gateway.approvals.get(waiting.id).decision, ALLOW)
        self.assertTrue(any("全放行" in text for text in self.sent), self.sent)

        # 下一张卡：不问人，也不发「等你一句话」那条
        sent_before = len(self.sent)
        later = self.gateway.approvals.request(agent="claude", peer="wx-user",
                                               title="又想改文件")
        self.gateway._approval_asked(types.SimpleNamespace(name="claude"), later)
        self.assertEqual(later.decision, ALLOW)
        self.assertEqual(len(self.sent), sent_before, "开着 /always 就不该再打扰人")

    def test_saying_anything_else_takes_the_allow_all_back(self):
        self.gateway.handle({"from_user_id": "wx-user", "message_id": "m-a2",
                             "item_list": [{"type": 1, "text_item": {"text": "/always"}}]})
        self.assertGreater(self.gateway.approvals.auto_allow_until("wx-user"), 0)

        self.gateway.handle({"from_user_id": "wx-user", "message_id": "m-a3",
                             "item_list": [{"type": 1, "text_item": {"text": "接着干"}}]})
        self.assertEqual(self.gateway.approvals.auto_allow_until("wx-user"), 0,
                         "用户又开口 = 上一轮结束了")

    def test_the_question_reaches_the_chat_and_the_answer_releases_the_agent(self):
        self.gateway._last_peer = "wx-user"        # 上一条消息是谁发的
        approval = self.gateway.approvals.request(agent="claude", peer="",
                                                 title="想跑 npm test",
                                                 detail="npm test")
        self.gateway._approval_asked(types.SimpleNamespace(name="claude"), approval)
        self.assertTrue(any(text.startswith("⏸️") and "/approve" in text for text in self.sent),
                        self.sent)
        self.assertIn(approval.id, self.sent[-1])
        self.assertIn("想跑 npm test", self.sent[-1])

        self.gateway.handle({"from_user_id": "wx-user", "message_id": "m-1",
                             "item_list": [{"type": 1,
                                            "text_item": {"text": f"/approve {approval.id}"}}]})
        self.assertEqual(self.gateway.approvals.get(approval.id).decision, ALLOW)
        self.assertTrue(any("已放行" in text for text in self.sent), self.sent)

    def test_rejecting_closes_it_without_releasing_anyone(self):
        approval = self.gateway.approvals.request(agent="claude", peer="wx-user",
                                                 title="想删库")
        self.gateway.handle({"from_user_id": "wx-user", "message_id": "m-2",
                             "item_list": [{"type": 1,
                                            "text_item": {"text": f"/reject {approval.id}"}}]})
        self.assertEqual(self.gateway.approvals.get(approval.id).decision, DENY)
        self.assertTrue(any("已拒绝" in text for text in self.sent), self.sent)

    def test_approving_when_nothing_is_pending_is_said_plainly(self):
        self.gateway.handle({"from_user_id": "wx-user", "message_id": "m-3",
                             "item_list": [{"type": 1, "text_item": {"text": "/approve"}}]})
        self.assertTrue(any("没有等你点头的事" in text for text in self.sent), self.sent)

    def test_a_question_with_no_one_to_ask_is_denied_immediately(self):
        approval = self.gateway.approvals.request(agent="claude", peer="", title="没人可问")
        self.gateway._last_peer = ""
        self.gateway._approval_asked(types.SimpleNamespace(name="claude"), approval)
        self.assertEqual(self.gateway.approvals.get(approval.id).decision, DENY)
        self.assertEqual(self.sent, [], "没人可问就别发消息")

    def test_the_console_snapshot_shows_pending_questions(self):
        self.gateway.approvals.request(agent="claude", peer="wx-user", title="等它")
        self.assertEqual(self.gateway.snapshot()["approvals"][0]["title"], "等它")

    def test_the_last_peer_survives_a_restart(self):
        """重启之后 agent 的问题也得有地方可去。"""
        self.gateway.store.set_last_peer("wx-user")

        from agent_gateway.config import load_config
        from agent_gateway.gateway import Gateway

        revived = Gateway(load_config(Path(self._tmp.name) / "cfg.json"), dry_run=True)
        try:
            self.assertEqual(revived._last_peer, "wx-user")
        finally:
            revived.shutdown()


if __name__ == "__main__":
    unittest.main()

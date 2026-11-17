"""统一记忆：存储、脱敏、抽取、注入、命令、Markdown 镜像、隔离。"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_gateway import memory
from agent_gateway.config import ConfigError, MemoryConfig, load_config
from agent_gateway.gateway import Gateway
from agent_gateway.virtual_ilink import VirtualILinkServer


def hub(tmp: Path, **kw) -> memory.MemoryHub:
    cfg = MemoryConfig(enabled=True, **kw)
    return memory.MemoryHub(cfg, tmp, log=lambda _m: None)


class RedactionTests(unittest.TestCase):
    def test_secrets_are_masked(self):
        text = memory.redact("key sk-abcdefghijklmnop 和 Bearer abcdefghijklmnopqrstuv")
        self.assertNotIn("sk-abcdefghijklmnop", text)
        self.assertNotIn("abcdefghijklmnopqrstuv", text)
        self.assertIn("[REDACTED]", text)

    def test_private_keys_are_masked(self):
        text = memory.redact("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
        self.assertEqual(text, "[REDACTED PRIVATE KEY]")

    def test_custom_patterns_are_masked(self):
        self.assertEqual(memory.redact("口令 hunter2", [r"hunter2"]), "口令 [REDACTED]")

    def test_ordinary_text_is_untouched(self):
        text = "部署目录 /opt/wechat-gateway，IP 100.95.111.58"
        self.assertEqual(memory.redact(text), text)


class ExtractionTests(unittest.TestCase):
    def test_a_stated_preference_becomes_a_fact(self):
        self.assertEqual(memory.extract_facts("以后都用中文回答我"), ["以后都用中文回答我"])

    def test_questions_commands_and_chatter_do_not(self):
        self.assertEqual(memory.extract_facts("以后都用中文吗?"), [])
        self.assertEqual(memory.extract_facts("/remember 以后都用中文"), [])
        self.assertEqual(memory.extract_facts("今天天气不错"), [])

    def test_overlong_text_is_not_turned_into_a_fact(self):
        self.assertEqual(memory.extract_facts("记住 " + "x" * 400), [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = memory.BuiltinStore(Path(self._tmp.name) / "m.db", l0_ttl_days=30)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_same_fact_twice_is_one_item(self):
        first = self.store.write(scope="peer:a", layer="l1", text="部署目录是 /opt/gw")
        second = self.store.write(scope="peer:a", layer="l1", text="部署目录是 /opt/gw")
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.store.stats(["peer:a"])["by_layer"], {"l1": 1})

    def test_raw_turns_repeat_and_expire(self):
        self.store.write(scope="peer:a", layer="l0", text="继续")
        self.store.write(scope="peer:a", layer="l0", text="继续")
        self.assertEqual(self.store.stats(["peer:a"])["by_layer"], {"l0": 2})
        with self.store._lock, self.store._db:
            self.store._db.execute("UPDATE items SET expires_at=? WHERE layer='l0'",
                                   (time.time() - 1,))
        self.assertEqual(self.store.prune(), 2)
        self.assertEqual(self.store.stats(["peer:a"])["total"], 0)

    def test_search_finds_a_chinese_substring_and_respects_scope(self):
        self.store.write(scope="peer:a", layer="l1", text="部署目录是 /opt/wechat-gateway")
        self.store.write(scope="peer:b", layer="l1", text="部署目录是 /srv/other")
        hits = self.store.search("wechat", ["peer:a"])
        self.assertEqual([h.text for h in hits], ["部署目录是 /opt/wechat-gateway"])
        self.assertEqual(self.store.search("部署目录", ["peer:b"])[0].text, "部署目录是 /srv/other")

    def test_forget_by_id_and_by_query(self):
        item = self.store.write(scope="peer:a", layer="l1", text="密码是 1234")
        self.store.write(scope="peer:a", layer="l1", text="偏好中文")
        self.assertEqual(self.store.forget(scopes=["peer:a"], item_id=item.id), 1)
        self.assertEqual(self.store.forget(scopes=["peer:a"], query="偏好"), 1)
        self.assertEqual(self.store.stats(["peer:a"])["total"], 0)

    def test_budget_limits_what_search_returns(self):
        for i in range(5):
            self.store.write(scope="peer:a", layer="l1", text=f"事实{i} " + "x" * 50)
        hits = self.store.search("事实", ["peer:a"], k=10, budget=120)
        self.assertLessEqual(sum(len(h.text) for h in hits), 120)


class HubTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.hub = hub(self.dir)
        self.peer = "wx-user-1"

    def tearDown(self):
        self.hub.close()
        self._tmp.cleanup()

    def test_turns_and_replies_land_in_l0_and_auto_facts(self):
        self.hub.record_inbound(self.peer, "hermes", "mac-air", "把网关发到 studio")
        self.hub.record_reply(self.peer, "hermes", "mac-air", "已更新并重启")
        self.hub.record_reply(self.peer, "hermes", "mac-air", "以后都先 git fetch")
        scope = self.hub.scope_for(self.peer)
        stats = self.hub.store.stats([scope])
        self.assertEqual(stats["by_layer"].get("l0"), 3, "一条用户消息 + 两条回答")
        self.assertEqual(stats["by_layer"].get("l1"), 1, "带「以后都」的回答抽成事实")

    def test_brief_and_inject_carry_recent_context_and_facts(self):
        self.hub.record_inbound(self.peer, "hermes", "mac-air", "第一句")
        self.hub.record_reply(self.peer, "hermes", "mac-air", "答复第一句")
        self.hub.write_fact(self.peer, "部署目录是 /opt/wechat-gateway")
        brief = self.hub.build_brief(self.peer, "部署目录在哪")
        self.assertIn(memory.MEMORY_HEADER, brief)
        self.assertIn("答复第一句", brief)
        self.assertIn("/opt/wechat-gateway", brief)
        injected = self.hub.inject(self.peer, "继续")
        self.assertTrue(injected.startswith(memory.MEMORY_HEADER))
        self.assertTrue(injected.endswith(f"{memory.MESSAGE_HEADER}继续"))

    def test_another_chat_does_not_see_this_one(self):
        self.hub.write_fact("someone-else", "密码是 1234")
        self.assertNotIn("1234", self.hub.build_brief(self.peer, "密码"))

    def test_global_layer_is_read_by_everyone(self):
        self.hub.write_fact(self.peer, "偏好中文回答", global_=True)
        self.assertIn("偏好中文回答", self.hub.build_brief("another-peer", ""))

    def test_commands_remember_recall_forget_and_overview(self):
        self.assertIn("已记下", self.hub.remember(self.peer, "部署目录是 /opt/gw"))
        self.assertIn("/opt/gw", self.hub.recall(self.peer, "部署目录"))
        self.assertIn("已删除", self.hub.forget(self.peer, "部署目录"))
        self.assertIn("没找到", self.hub.recall(self.peer, "部署目录"))
        self.assertIn("记忆概览", self.hub.overview(self.peer))

    def test_task_command_tracks_and_closes(self):
        self.assertIn("任务已记录", self.hub.task_command(self.peer, "把网关部署到 VPS"))
        self.assertIn("任务：", self.hub.build_brief(self.peer, ""))
        reply = self.hub.task_command(self.peer, "done")
        self.assertIn("已完成", reply)
        self.assertIn("status:done", self.hub.store.latest_tasks(
            self.hub.scope_for(self.peer), n=1)[0].tags)

    def test_markdown_mirror_and_import(self):
        item = self.hub.write_fact(self.peer, "部署目录是 /opt/gw")
        folder = self.hub._peer_dir(self.peer)
        facts = (folder / "facts.md").read_text(encoding="utf-8")
        self.assertIn(item.id, facts)
        self.assertTrue((folder / "profile.md").exists())
        with (folder / "facts.md").open("a", encoding="utf-8") as fh:
            fh.write("- 人手动加的一条事实\n")
        self.assertIn("导入 1 条", self.hub.reload(self.peer))
        self.assertIn("人手动加的一条事实", self.hub.recall(self.peer, "手动"))

    def test_api_peer_scope_and_global_permission(self):
        self.hub.write_fact(self.peer, "本聊天的事实：部署在 studio")
        self.hub.write_fact(self.peer, "全局事实：偏好中文", global_=True)
        ok = self.hub.api("search", allowed_peer=True, can_global=False,
                          payload={"peer": self.peer, "q": "事实"})
        self.assertEqual(ok["ret"], 0)
        texts = [i["text"] for i in ok["items"]]
        self.assertTrue(any("studio" in t for t in texts))
        self.assertTrue(any("偏好中文" in t for t in texts))

        denied = self.hub.api("search", allowed_peer=False, can_global=False,
                              payload={"peer": self.peer, "q": "studio"})
        self.assertFalse(any("studio" in i["text"] for i in denied["items"]),
                         "没服务过这个聊天的设备读不到它")

        write_global = self.hub.api("write", allowed_peer=False, can_global=False,
                                    payload={"peer": self.peer, "scope": "global",
                                             "text": "偷偷写全局"})
        self.assertEqual(write_global["ret"], -1)
        allowed = self.hub.api("write", allowed_peer=False, can_global=True,
                               payload={"peer": self.peer, "scope": "global",
                                        "text": "授权设备写全局"})
        self.assertEqual(allowed["ret"], 0)


class ConfigTests(unittest.TestCase):
    def _load(self, memory_raw):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "cfg.json"
        path.write_text(json.dumps({
            "virtual": {"enabled": True},
            "agents": {"hermes": {"type": "virtual"}},
            "memory": memory_raw,
        }), encoding="utf-8")
        return load_config(path)

    def test_memory_is_off_by_default(self):
        self.assertFalse(self._load({}).memory.enabled)

    def test_inject_knobs_are_read(self):
        cfg = self._load({"enabled": True, "inject": {"max_chars": 500, "facts": 3},
                          "global_writers": ["claude@win59"]})
        self.assertTrue(cfg.memory.enabled)
        self.assertEqual(cfg.memory.inject_max_chars, 500)
        self.assertEqual(cfg.memory.global_writers, ["claude@win59"])

    def test_unimplemented_backends_are_refused_loudly(self):
        with self.assertRaises(ConfigError):
            self._load({"enabled": True, "backend": "tencentdb"})
        with self.assertRaises(ConfigError):
            self._load({"enabled": True, "rag": {"enabled": True}})


class GatewayMemoryTests(unittest.TestCase):
    """网关层面：自动记录、注入、命令回话。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        path = tmp / "cfg.json"
        path.write_text(json.dumps({
            "data_dir": str(tmp / "data"),
            "default_agent": "hermes",
            "virtual": {"enabled": True, "port": 0, "auto_approve": ["hermes"]},
            "agents": {"hermes": {"type": "virtual", "label": "Hermes", "prefix": "h"}},
            "memory": {"enabled": True, "inject": {"max_chars": 1200}},
        }, ensure_ascii=False), encoding="utf-8")
        self.cfg = load_config(path)
        self.gateway = Gateway(self.cfg, dry_run=True)
        self.sent = []
        self.gateway._send = lambda chat_id, text, **_kw: self.sent.append(text)
        self.gateway._start_virtual()

    def tearDown(self):
        self.gateway.shutdown()
        self._tmp.cleanup()

    def say(self, text: str, mid: str) -> None:
        self.gateway.handle({"from_user_id": "wx-user", "message_id": mid,
                             "context_token": "ctx",
                             "item_list": [{"type": 1, "text_item": {"text": text}}]})

    def test_a_second_message_carries_the_first_turn(self):
        self.say("第一句", "m1")
        bind = self.gateway.virtual.bind_named("hermes")
        _, first = bind.drain("", 0.1)
        self.assertNotIn(memory.MEMORY_HEADER, first[0]["item_list"][0]["text_item"]["text"],
                         "第一条还没有可注入的记忆")
        self.gateway._forward_to_wechat(bind, "答复第一句")

        self.say("第二句", "m2")
        _, second = bind.drain("", 0.1)
        text = second[0]["item_list"][0]["text_item"]["text"]
        self.assertIn(memory.MEMORY_HEADER, text)
        self.assertIn("答复第一句", text)
        self.assertTrue(text.endswith("【消息】第二句"))

    def test_memory_commands_answer_without_reaching_the_agent(self):
        self.say("/remember 部署目录是 /opt/gateway", "m-r1")
        self.assertIn("已记下", self.sent[-1])
        self.say("/recall 部署目录", "m-r2")
        self.assertIn("/opt/gateway", self.sent[-1])
        self.say("/memory", "m-r3")
        self.assertIn("记忆概览", self.sent[-1])
        bind = self.gateway.virtual.bind_named("hermes")
        self.assertEqual(len(bind.queue), 0, "记忆命令不该投给 agent")

    def test_memory_off_is_the_old_behaviour(self):
        cfg = load_config(self.cfg.source)
        cfg.memory.enabled = False
        gateway = Gateway(cfg, dry_run=True)
        try:
            self.assertIsNone(gateway.memory)
        finally:
            gateway.shutdown()


class MemoryEndpointTests(unittest.TestCase):
    """GET/POST /memory/* 走虚拟 token；没 token 一律 401 / ret=-14。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.seen = []
        self.server = VirtualILinkServer(
            data_dir=Path(self._tmp.name), port=0,
            accept_tokens={"tok": "hermes@mac-air"},
            on_memory=lambda bind, path, payload: (
                self.seen.append((bind.name, bind.instance, path, payload))
                or {"ret": 0, "path": path}))
        host, port = self.server.start()
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def _post(self, payload, token=""):
        import urllib.request
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + "/memory/search",
                                         data=json.dumps(payload).encode("utf-8"),
                                         headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_a_token_gets_its_call_through_with_identity(self):
        result = self._post({"peer": "wx-1", "q": "部署"}, token="tok")
        self.assertEqual(result["ret"], 0)
        self.assertEqual(self.seen[0][:3], ("hermes", "mac-air", "search"))
        self.assertEqual(self.seen[0][3]["peer"], "wx-1")

    def test_no_token_is_refused(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post({"peer": "wx-1"}, token="")
        self.assertEqual(caught.exception.code, 401)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

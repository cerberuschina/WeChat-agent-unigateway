"""Attachments through the gateway, both directions — without touching WeChat.

Inbound: a media message from the peer must land on disk and reach the agent as
text with the local path (so every backend can use it).
Outbound: a virtual agent marks a local file and the gateway uploads it.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_gateway import ilink, ilink_media, media
from agent_gateway.config import load_config
from agent_gateway.gateway import Gateway


class FakeClient:
    def __init__(self):
        self.account_id = "bot@im.bot"
        self.sent = []

    def context_token(self, _chat_id):
        return "ctx"

    def send_text(self, chat_id, text, *, context_token=None):
        self.sent.append((chat_id, text))
        return {"ret": 0}

    def get_config(self, *_a, **_k):
        return {"typing_ticket": ""}


def build_gateway(tmp: Path, *, dry_run: bool = False) -> Gateway:
    path = tmp / "cfg.json"
    path.write_text(json.dumps({
        "data_dir": str(tmp / "data"),
        "account": {"account_id": "acct", "token": "tok"},
        "default_agent": "claude",
        "virtual": {"enabled": True, "port": 0, "auto_approve": ["claude"]},
        "agents": {"claude": {"type": "virtual", "label": "Claude Code"}},
    }, ensure_ascii=False), encoding="utf-8")
    gateway = Gateway(load_config(path), dry_run=dry_run)
    gateway.client = FakeClient()
    gateway._start_virtual()
    return gateway


def file_item(name: str = "报表.pdf", query: str = "EQP-1") -> dict:
    return media.build_media_item(media.ITEM_FILE, encrypt_query_param=query,
                                  aes_key=bytes(16), filename=name,
                                  plaintext_size=11, ciphertext_size=16)


class InboundMediaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.gateway = build_gateway(self.tmp)
        self.saved = []
        self._real_download = ilink_media.download_media

        def fake_download(client, info, dest_dir, *, name=""):
            target = Path(dest_dir) / (name or "x.bin")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"payload-11")
            self.saved.append((client, info, target))
            return target

        ilink_media.download_media = fake_download

    def tearDown(self):
        ilink_media.download_media = self._real_download
        self.gateway.shutdown()
        self._tmp.cleanup()

    def test_a_file_message_becomes_text_with_a_local_path(self):
        self.gateway.handle({"from_user_id": "wx-user", "message_id": "m-1",
                             "item_list": [file_item()]})

        bind = self.gateway.virtual.bind_named("claude")
        _, messages = bind.drain("", timeout=0.2)
        self.assertEqual(len(messages), 1, "媒体消息也要进 agent 的队列")
        text = messages[0]["item_list"][0]["text_item"]["text"]
        self.assertIn("[文件]", text)
        self.assertIn("报表.pdf", text)
        self.assertIn("本地路径", text)
        self.assertIn("下载地址", text, "远程 agent 要有办法取到这个文件")
        self.assertTrue(self.saved, "应当调用下载")
        self.assertIn(str(self.saved[0][2]), text)

    def test_the_downloaded_item_keeps_the_original_encrypt_param(self):
        self.gateway.handle({"from_user_id": "wx-user", "message_id": "m-2",
                             "item_list": [file_item(query="EQP-777")]})
        self.assertEqual(self.saved[0][1]["encrypt_query_param"], "EQP-777")

    def test_a_download_failure_is_reported_but_does_not_kill_the_turn(self):
        def boom(*_a, **_k):
            raise ilink.ILinkError("CDN 下载 HTTP 403")

        ilink_media.download_media = boom
        self.gateway.handle({"from_user_id": "wx-user", "message_id": "m-3",
                             "item_list": [file_item()]})
        bind = self.gateway.virtual.bind_named("claude")
        _, messages = bind.drain("", timeout=0.2)
        text = messages[0]["item_list"][0]["text_item"]["text"]
        self.assertIn("下载失败", text)
        self.assertIn("403", text)

    def test_dry_run_describes_instead_of_downloading(self):
        gateway = build_gateway(self.tmp, dry_run=True)
        try:
            self.assertEqual(ilink_media.download_media.__name__, "fake_download")
            text = gateway._ingest_media({"item_list": [file_item()]}, "wx-user")
            self.assertIn("dry-run", text)
        finally:
            gateway.shutdown()


class OutboundMediaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.gateway = build_gateway(self.tmp)
        self.sent_files = []
        self._real_send_file = ilink_media.send_file

        def fake_send_file(client, to, path, *, context_token=None, caption=""):
            self.sent_files.append((to, Path(path), context_token))
            return {"ret": 0}

        ilink_media.send_file = fake_send_file
        self.file = self.tmp / "报告.pdf"
        self.file.write_bytes(b"pdf-bytes")

    def tearDown(self):
        ilink_media.send_file = self._real_send_file
        self.gateway.shutdown()
        self._tmp.cleanup()

    def bind(self):
        bind = self.gateway.virtual.bind_named("claude")
        self.gateway.virtual.deliver("claude", text="先来条消息", peer="wx-user")
        return bind

    def item_for(self, path: Path) -> dict:
        size = path.stat().st_size if path.exists() else 0
        return media.build_media_item(media.ITEM_FILE,
                                      encrypt_query_param=f"localpath:{path}",
                                      aes_key=bytes(16), filename=path.name,
                                      plaintext_size=size, ciphertext_size=0)

    def test_a_marked_file_is_uploaded_and_sent(self):
        bind = self.bind()
        self.gateway._forward_media_to_wechat(bind, [self.item_for(self.file)])
        self.assertEqual(len(self.sent_files), 1)
        self.assertEqual(self.sent_files[0][0], "wx-user")
        self.assertEqual(self.sent_files[0][1].name, "报告.pdf")
        self.assertEqual(self.gateway._turn_counts["wx-user"], 1, "附件也占额度")

    def test_a_missing_file_is_refused_loudly(self):
        bind = self.bind()
        self.gateway._forward_media_to_wechat(bind, [self.item_for(self.tmp / "nope.pdf")])
        self.assertEqual(self.sent_files, [])

    def test_a_foreign_media_item_is_not_guessed_at(self):
        bind = self.bind()
        self.gateway._forward_media_to_wechat(bind, [file_item(query="EQP-from-elsewhere")])
        self.assertEqual(self.sent_files, [])

    def test_no_budget_means_no_attachment_and_a_note(self):
        bind = self.bind()
        self.gateway._turn_counts["wx-user"] = self.gateway.cfg.delivery.max_messages_per_turn
        self.gateway._forward_media_to_wechat(bind, [self.item_for(self.file)])
        self.assertEqual(self.sent_files, [], "额度用完不能硬发")
        self.assertIn("额度用完", self.gateway._pending_output["wx-user"])

    def test_media_before_any_message_has_no_peer_to_send_to(self):
        bind = self.gateway.virtual.bind_named("claude")     # no deliver() → no peer
        self.gateway._forward_media_to_wechat(bind, [self.item_for(self.file)])
        self.assertEqual(self.sent_files, [])


if __name__ == "__main__":
    unittest.main()

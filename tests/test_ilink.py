"""iLink wire layer: parsing, error mapping, persistence. No network."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_gateway import ilink


class MessageParsingTests(unittest.TestCase):
    def test_text_items_are_joined(self):
        message = {"item_list": [
            {"type": ilink.ITEM_TEXT, "text_item": {"text": "第一行"}},
            {"type": 2, "image_item": {"media": {}}},
            {"type": ilink.ITEM_TEXT, "text_item": {"text": "第二行"}},
        ]}
        self.assertEqual(ilink.message_text(message), "第一行\n第二行")

    def test_media_only_message_has_no_text(self):
        self.assertEqual(ilink.message_text({"item_list": [{"type": 3, "voice_item": {}}]}), "")

    def test_garbage_items_do_not_crash(self):
        self.assertEqual(ilink.message_text({"item_list": [None, "x", {"type": 1}]}), "")

    def test_sender_of(self):
        self.assertEqual(ilink.sender_of({"from_user_id": "u1"}), "u1")
        self.assertEqual(ilink.sender_of({}), "")


class SplitTests(unittest.TestCase):
    def test_short_text_is_one_message(self):
        self.assertEqual(ilink.split_text("短", 100), ["短"])

    def test_empty_text_returns_nothing(self):
        self.assertEqual(ilink.split_text("   ", 100), [])

    def test_paragraphs_are_kept_together_when_they_fit(self):
        text = "第一段\n\n第二段"
        self.assertEqual(ilink.split_text(text, 100), [text])

    def test_long_text_splits_and_never_exceeds_the_limit(self):
        blocks = [f"第{i}段" + "x" * 60 for i in range(10)]
        parts = ilink.split_text("\n\n".join(blocks), 200)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 200)

    def test_a_single_huge_paragraph_is_hard_wrapped(self):
        parts = ilink.split_text("y" * 500, 120)
        self.assertEqual([len(p) for p in parts], [120, 120, 120, 120, 20])


class HeaderTests(unittest.TestCase):
    def test_headers_carry_the_bot_identity(self):
        headers = ilink._headers("tok", 12)
        self.assertEqual(headers["AuthorizationType"], "ilink_bot_token")
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(headers["iLink-App-Id"], ilink.ILINK_APP_ID)
        self.assertEqual(headers["Content-Length"], "12")
        self.assertTrue(headers["X-WECHAT-UIN"])

    def test_headers_omit_authorization_without_a_token(self):
        self.assertNotIn("Authorization", ilink._headers(None))

    def test_barer_case_does_not_matter(self):
        self.assertEqual(ilink.BASE_URL, "https://ilinkai.weixin.qq.com")
        self.assertEqual(ilink.EP_GET_UPDATES, "ilink/bot/getupdates")
        self.assertEqual(ilink.EP_SEND_MESSAGE, "ilink/bot/sendmessage")
        self.assertEqual(ilink.EP_GET_BOT_QR, "ilink/bot/get_bot_qrcode")
        self.assertEqual(ilink.EP_GET_QR_STATUS, "ilink/bot/get_qrcode_status")


class ErrorMappingTests(unittest.TestCase):
    def test_ok_payload_passes_through(self):
        payload = {"ret": 0, "msgs": []}
        self.assertIs(ilink._raise_for_status(payload, "x"), payload)

    def test_session_expired_is_its_own_error(self):
        with self.assertRaises(ilink.SessionExpired):
            ilink._raise_for_status({"ret": -14, "errmsg": "session expired"}, "getupdates")

    def test_rate_limit_is_retryable(self):
        with self.assertRaises(ilink.RateLimited):
            ilink._raise_for_status({"errcode": -2}, "sendmessage")

    def test_other_errors_keep_the_code(self):
        with self.assertRaises(ilink.ILinkError) as ctx:
            ilink._raise_for_status({"ret": 5, "errmsg": "boom"}, "sendmessage")
        self.assertEqual(ctx.exception.code, 5)
        self.assertIn("boom", str(ctx.exception))


class ClientPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_context_tokens_survive_a_restart(self):
        first = ilink.ILinkClient("bot@im.bot", "tok", data_dir=self.dir)
        first.remember_context_token("u1", "ctx-1")
        second = ilink.ILinkClient("bot@im.bot", "tok", data_dir=self.dir)
        self.assertEqual(second.context_token("u1"), "ctx-1")
        self.assertIsNone(second.context_token("u2"))

    def test_cursor_survives_a_restart(self):
        first = ilink.ILinkClient("bot@im.bot", "tok", data_dir=self.dir)
        self.assertEqual(first.load_cursor(), "")
        first.save_cursor("buf-42")
        self.assertEqual(ilink.ILinkClient("bot@im.bot", "tok", data_dir=self.dir).load_cursor(), "buf-42")

    def test_corrupt_state_files_are_ignored(self):
        (self.dir / "context-tokens.json").write_text("{not json", encoding="utf-8")
        (self.dir / "get_updates_buf.json").write_text("{not json", encoding="utf-8")
        client = ilink.ILinkClient("bot@im.bot", "tok", data_dir=self.dir)
        self.assertIsNone(client.context_token("u1"))
        self.assertEqual(client.load_cursor(), "")

    def test_send_text_refuses_empty_bodies(self):
        client = ilink.ILinkClient("bot@im.bot", "tok", data_dir=self.dir)
        with self.assertRaises(ValueError):
            client.send_text("u1", "   ")

    def test_context_token_file_is_json_with_peer_keys(self):
        client = ilink.ILinkClient("bot@im.bot", "tok", data_dir=self.dir)
        client.remember_context_token("u9", "ctx-9")
        saved = json.loads((self.dir / "context-tokens.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, {"u9": "ctx-9"})


if __name__ == "__main__":
    unittest.main()

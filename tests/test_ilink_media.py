"""Upload/download envelope against a fake client — no WeChat, no CDN, no network."""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_gateway import ilink, ilink_media, media


class FakeClient:
    """Records protocol calls; the CDN is faked at the ``_http_bytes`` boundary."""

    def __init__(self, upload_response=None, download_body=b"", download_headers=None):
        self.base_url = "http://127.0.0.1:18500"
        self.token = "tok"
        self.sent_texts = []
        self.requests = []
        self.upload_response = upload_response or {"upload_param": "UP1"}
        self.download_body = download_body
        self.download_headers = download_headers or {}
        self.uploads = {}

    def send_text(self, to, text, *, context_token=None):
        self.sent_texts.append((to, text))
        return {"ret": 0}


class TransportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.client = FakeClient()
        self.uploads = {}

        def fake_request(method, base_url, endpoint, *, token=None, payload=None, timeout_ms=0):
            self.client.requests.append((endpoint, payload))
            if endpoint == ilink_media.EP_GET_UPLOAD_URL:
                return self.client.upload_response
            if endpoint == ilink.EP_SEND_MESSAGE:
                self.client.last_message = payload["msg"]
                return {"ret": 0, "msg_id": "m-1"}
            raise AssertionError(f"unexpected endpoint {endpoint}")

        def fake_http(method, url, *, data=None, headers=None, timeout=0):
            self.uploads[url] = data
            if method == "POST":
                return 200, {"x-encrypted-param": "EQP-42"}, b""
            return 200, self.client.download_headers, self.client.download_body

        self._real_request = ilink._request
        self._real_http = ilink_media._http_bytes
        ilink._request = fake_request
        ilink_media._http_bytes = fake_http

    def tearDown(self):
        ilink._request = self._real_request
        ilink_media._http_bytes = self._real_http
        self._tmp.cleanup()

    # -- upload ----------------------------------------------------------
    def test_upload_sends_the_encrypted_bytes_and_returns_a_file_item(self):
        path = self.tmp / "报表.pdf"
        path.write_bytes(b"hello pdf" * 10)

        item = ilink_media.upload_media(self.client, "wx-user", path)

        endpoint, payload = self.client.requests[0]
        self.assertEqual(endpoint, ilink_media.EP_GET_UPLOAD_URL)
        self.assertEqual(payload["media_type"], media.MEDIA_FILE)
        self.assertEqual(payload["rawsize"], 90)
        self.assertEqual(payload["filesize"], 96)
        self.assertEqual(payload["rawfilemd5"], hashlib.md5(b"hello pdf" * 10).hexdigest())
        self.assertEqual(len(payload["aeskey"]), 32)

        posted = list(self.uploads.values())[0]
        self.assertEqual(len(posted) % 16, 0, "上传的是补过填充的密文")
        key = bytes.fromhex(payload["aeskey"])
        self.assertEqual(media.aes128_ecb_decrypt(posted, key), b"hello pdf" * 10)

        self.assertEqual(item["type"], media.ITEM_FILE)
        self.assertEqual(item["file_item"]["file_name"], "报表.pdf")
        self.assertEqual(item["file_item"]["len"], "90")
        self.assertEqual(item["file_item"]["media"]["encrypt_query_param"], "EQP-42")
        self.assertEqual(media.parse_aes_key(item["file_item"]["media"]["aes_key"]), key)

    def test_a_png_becomes_an_image_item(self):
        path = self.tmp / "shot.png"
        path.write_bytes(b"\x89PNG" + b"0" * 20)
        item = ilink_media.upload_media(self.client, "wx-user", path)
        self.assertEqual(item["type"], media.ITEM_IMAGE)
        self.assertIn("mid_size", item["image_item"])

    def test_upload_full_url_is_preferred_over_the_constructed_one(self):
        self.client.upload_response = {"upload_full_url": "https://cdn.example/up?sig=1"}
        path = self.tmp / "a.bin"
        path.write_bytes(b"x")
        ilink_media.upload_media(self.client, "wx-user", path)
        self.assertIn("cdn.example/up?sig=1", list(self.uploads)[0])

    def test_missing_upload_url_is_an_error_not_a_silent_failure(self):
        self.client.upload_response = {}
        path = self.tmp / "a.bin"
        path.write_bytes(b"x")
        with self.assertRaises(ilink.ILinkError):
            ilink_media.upload_media(self.client, "wx-user", path)

    def test_a_cdn_upload_without_the_header_is_an_error(self):
        def fake_http(method, url, *, data=None, headers=None, timeout=0):
            return 200, {}, b"no param here"

        ilink_media._http_bytes = fake_http
        path = self.tmp / "a.bin"
        path.write_bytes(b"x")
        with self.assertRaises(ilink.ILinkError) as ctx:
            ilink_media.upload_media(self.client, "wx-user", path)
        self.assertIn("x-encrypted-param", str(ctx.exception))

    def test_send_file_sends_caption_then_the_item(self):
        path = self.tmp / "a.txt"
        path.write_bytes(b"data")
        ilink_media.send_file(self.client, "wx-user", path, context_token="ctx", caption="给你")
        self.assertEqual(self.client.sent_texts, [("wx-user", "给你")])
        msg = self.client.last_message
        self.assertEqual(msg["item_list"][0]["type"], media.ITEM_FILE)
        self.assertEqual(msg["context_token"], "ctx")
        self.assertTrue(msg["client_id"].startswith("gw-"))

    # -- download --------------------------------------------------------
    def test_download_decrypts_into_a_file(self):
        key = bytes(range(16))
        plaintext = b"this is the file body"
        self.client.download_body = media.aes128_ecb_encrypt(plaintext, key)
        info = {"encrypt_query_param": "EQP-9",
                "aes_key": base64.b64encode(key.hex().encode()).decode()}

        target = ilink_media.download_media(self.client, info, self.tmp / "media", name="got.bin")

        self.assertEqual(target.read_bytes(), plaintext)
        self.assertEqual(target.name, "got.bin")

    def test_download_without_a_key_keeps_the_bytes_as_is(self):
        self.client.download_body = b"plain bytes"
        target = ilink_media.download_media(self.client, {"encrypt_query_param": "E"},
                                            self.tmp / "m", name="p.bin")
        self.assertEqual(target.read_bytes(), b"plain bytes")

    def test_download_falls_back_to_full_url(self):
        self.client.download_body = b"raw"
        target = ilink_media.download_media(self.client, {"full_url": "https://cdn/x"},
                                            self.tmp / "m", name="u.bin")
        self.assertEqual(target.read_bytes(), b"raw")

    def test_a_media_item_without_any_location_is_rejected(self):
        with self.assertRaises(ilink.ILinkError):
            ilink_media.download_media(self.client, {}, self.tmp, name="x.bin")


class EndToEndEnvelopeTests(unittest.TestCase):
    """Upload → item → (simulated receiver) download must give back the same bytes."""

    def test_round_trip_through_the_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            original = tmp / "原文.txt"
            payload = "中文内容 with mixed ASCII".encode() * 3
            original.write_bytes(payload)

            sent = {}

            class Client:
                base_url = "http://x"
                token = "t"

                def send_text(self, *a, **k):
                    return {"ret": 0}

            client = Client()

            def fake_request(*a, **k):
                return {"upload_param": "P"}

            def fake_http(method, url, *, data=None, headers=None, timeout=0):
                if method == "POST":
                    sent["ciphertext"] = data
                    return 200, {"x-encrypted-param": "EQP"}, b""
                return 200, {}, sent["ciphertext"]

            real_request, real_http = ilink._request, ilink_media._http_bytes
            ilink._request, ilink_media._http_bytes = fake_request, fake_http
            try:
                item = ilink_media.upload_media(client, "wx", original)
                info = media.parse_media_item(item)
                assert info is not None
                out = ilink_media.download_media(client, info, tmp / "in", name="back.txt")
            finally:
                ilink._request, ilink_media._http_bytes = real_request, real_http

            self.assertEqual(out.read_bytes(), payload, "上传再下载必须字节一致")


if __name__ == "__main__":
    unittest.main()

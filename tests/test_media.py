"""Media envelope: AES-128-ECB (pure Python, checked against the NIST vectors),
the item shapes iLink expects, and the key encoding that trips people up."""
from __future__ import annotations

import base64
import unittest

from agent_gateway import media as md


class AesTests(unittest.TestCase):
    """The bundled AES must be *the* AES, not an approximation of it."""

    def test_fips_197_appendix_b_vector(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.assertEqual(md._pure_ecb(key, plain).hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")

    def test_nist_sp_800_38a_ecb_vector(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"
                              "ae2d8a571e03ac9c9eb76fac45af8e51")
        expected = ("3ad77bb40d7a3660a89ecaf32466ef97"
                    "f5d3d58503b9699de785895a96fdbaaf")
        self.assertEqual(md._pure_ecb(key, plain).hex(), expected)

    def test_pkcs7_round_trip_and_rejects_bad_padding(self):
        for size in (0, 1, 15, 16, 17, 1000):
            data = bytes(range(256))[:size] if size <= 256 else b"x" * size
            self.assertEqual(md.pkcs7_unpad(md.pkcs7_pad(data)), data)
        with self.assertRaises(ValueError):
            md.pkcs7_unpad(b"1234567890123456")          # 无填充
        with self.assertRaises(ValueError):
            md.pkcs7_unpad(b"123456789012345x")          # 长度不对

    def test_encrypt_decrypt_round_trip_through_the_public_api(self):
        key = bytes(range(16))
        for payload in (b"", b"a", b"x" * 16, b"y" * 1234):
            cipher = md.aes128_ecb_encrypt(payload, key)
            self.assertEqual(len(cipher) % 16, 0)
            self.assertEqual(md.aes128_ecb_decrypt(cipher, key), payload)

    def test_bundled_matches_cryptography_when_available(self):
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            self.skipTest("cryptography 没装（正常，自带实现仍然可用）")
        key, data = bytes(range(16)), b"hello world" * 5
        cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        reference = cipher.update(md.pkcs7_pad(data)) + cipher.finalize()
        self.assertEqual(md._pure_ecb(key, md.pkcs7_pad(data)), reference)


class KeyEncodingTests(unittest.TestCase):
    def test_api_key_is_base64_of_the_hex_string(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        encoded = md.aes_key_for_api(key)
        self.assertEqual(base64.b64decode(encoded).decode(), key.hex())
        self.assertEqual(md.parse_aes_key(encoded), key)

    def test_parse_tolerates_a_raw_hex_key(self):
        key = bytes.fromhex("0f0e0d0c0b0a09080706050403020100")
        self.assertEqual(md.parse_aes_key(key.hex()), key)


class ItemShapeTests(unittest.TestCase):
    def test_file_item_matches_the_protocol(self):
        key = bytes(range(16))
        item = md.build_media_item(md.ITEM_FILE, encrypt_query_param="QP", aes_key=key,
                                   filename=r"C:\tmp\report.pdf", plaintext_size=1234,
                                   ciphertext_size=1248)
        self.assertEqual(item["type"], 4)
        self.assertEqual(item["file_item"]["file_name"], "report.pdf")
        self.assertEqual(item["file_item"]["len"], "1234")
        self.assertEqual(item["file_item"]["media"]["encrypt_type"], 1)
        self.assertEqual(md.parse_aes_key(item["file_item"]["media"]["aes_key"]), key)

    def test_image_item_carries_the_ciphertext_size(self):
        item = md.build_media_item(md.ITEM_IMAGE, encrypt_query_param="QP",
                                   aes_key=bytes(16), filename="a.png",
                                   plaintext_size=100, ciphertext_size=112)
        self.assertEqual(item["type"], 2)
        self.assertEqual(item["image_item"]["mid_size"], 112)

    def test_rejects_a_non_media_type(self):
        with self.assertRaises(ValueError):
            md.build_media_item(md.ITEM_TEXT, encrypt_query_param="x", aes_key=bytes(16),
                                filename="f", plaintext_size=1, ciphertext_size=16)

    def test_parse_round_trips_a_built_item(self):
        item = md.build_media_item(md.ITEM_FILE, encrypt_query_param="QP",
                                   aes_key=bytes(range(16)), filename="a.zip",
                                   plaintext_size=9, ciphertext_size=16)
        parsed = md.parse_media_item(item)
        assert parsed is not None
        self.assertEqual(parsed["encrypt_query_param"], "QP")
        self.assertEqual(parsed["filename"], "a.zip")
        self.assertEqual(parsed["item_type"], md.ITEM_FILE)

    def test_parse_ignores_text_items(self):
        self.assertIsNone(md.parse_media_item({"type": 1, "text_item": {"text": "hi"}}))

    def test_item_type_follows_the_extension(self):
        self.assertEqual(md.item_type_for("a.PNG"), md.ITEM_IMAGE)
        self.assertEqual(md.item_type_for("b.MP4"), md.ITEM_VIDEO)
        self.assertEqual(md.item_type_for("c.silk"), md.ITEM_VOICE)
        self.assertEqual(md.item_type_for("d.pdf"), md.ITEM_FILE)
        self.assertEqual(md.item_type_for("noext"), md.ITEM_FILE)


class UploadPlanTests(unittest.TestCase):
    def test_plan_pads_to_the_block_size_and_hashes_the_plaintext(self):
        import hashlib
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(b"x" * 20)
            plan = md.file_upload_plan(path)
            self.assertEqual(plan["rawsize"], 20)
            self.assertEqual(plan["filesize"], 32, "补齐到 16 的整数倍")
            self.assertEqual(plan["rawfilemd5"], hashlib.md5(b"x" * 20).hexdigest())
            self.assertEqual(plan["media_type"], md.MEDIA_FILE)
            self.assertEqual(len(plan["key"]), 16)
            self.assertEqual(len(plan["filekey"]), 32)


if __name__ == "__main__":
    unittest.main()

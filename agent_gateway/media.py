"""iLink media: AES-128-ECB envelope + item shapes for files and images.

Where this sits
--------------
``getuploadurl`` hands us an upload URL; the bytes we POST must be the file
encrypted with a fresh AES-128 key (ECB, PKCS7); the CDN answers with an
``x-encrypted-param`` header, and *that* value plus the key (as base64 of its hex
string — not base64 of the raw bytes, or images arrive as grey boxes) goes into
the message item:

    {"type": 4, "file_item": {"media": {"encrypt_query_param": …, "aes_key": …,
                                        "encrypt_type": 1},
                              "file_name": "report.pdf", "len": "12345"}}

Inbound is the mirror image: ``media.encrypt_query_param`` + ``media.aes_key``,
downloaded from ``{cdn}/download?encrypted_query_param=…`` and decrypted.

Why a bundled AES
-----------------
The gateway advertises itself as dependency-free, and the interpreter that runs
it here has no ``cryptography``. So: use ``cryptography`` when it is importable
(it is audited and fast) and fall back to the small pure-Python implementation
below. Both are checked against the NIST vectors in the tests, and against each
other when the library is present.

This is a transport envelope, not a security boundary — the key travels in the
same request as the ciphertext. Nothing here should be reused for secrets at rest.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
MEDIA_IMAGE, MEDIA_VIDEO, MEDIA_FILE, MEDIA_VOICE = 1, 2, 3, 4

MEDIA_TYPE_BY_ITEM = {ITEM_IMAGE: MEDIA_IMAGE, ITEM_FILE: MEDIA_FILE,
                      ITEM_VIDEO: MEDIA_VIDEO, ITEM_VOICE: MEDIA_VOICE}
ITEM_KEY = {ITEM_IMAGE: "image_item", ITEM_VOICE: "voice_item",
            ITEM_FILE: "file_item", ITEM_VIDEO: "video_item"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
VOICE_EXTS = {".silk", ".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}

BLOCK = 16


# --------------------------------------------------------------------------- #
# PKCS7 + AES-128-ECB
# --------------------------------------------------------------------------- #
def pkcs7_pad(data: bytes, block: int = BLOCK) -> bytes:
    pad = block - (len(data) % block) or block
    return data + bytes([pad]) * pad


def pkcs7_unpad(data: bytes, block: int = BLOCK) -> bytes:
    if not data or len(data) % block:
        raise ValueError("不是完整的分组长度")
    pad = data[-1]
    if not 1 <= pad <= block or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS7 填充不对")
    return data[:-pad]


def _gf_mul(a: int, b: int) -> int:
    product = 0
    for _ in range(8):
        if b & 1:
            product ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= 0x1B
        b >>= 1
    return product


def _make_sbox() -> list[int]:
    """S-box from its definition (GF(2^8) inverse + affine transform)."""
    table = []
    for value in range(256):
        inv = 0 if value == 0 else next(b for b in range(1, 256) if _gf_mul(value, b) == 1)
        s = inv
        for _ in range(4):
            s = ((s << 1) | (s >> 7)) & 0xFF
            inv ^= s
        table.append(inv ^ 0x63)
    return table


SBOX = _make_sbox()
INV_SBOX = [SBOX.index(i) for i in range(256)]


def _expand_key(key: bytes) -> list[bytes]:
    nk = len(key) // 4
    nr = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = [SBOX[b] for b in temp[1:] + temp[:1]]
            temp[0] ^= rcon
            rcon = ((rcon << 1) ^ 0x1B) & 0xFF if rcon & 0x80 else rcon << 1
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [bytes(b for word in words[4 * r:4 * r + 4] for b in word) for r in range(nr + 1)]


def _shift_rows(state: list[int], inverse: bool = False) -> list[int]:
    out = list(state)
    for row in range(1, 4):
        values = [state[4 * col + row] for col in range(4)]
        values = values[-row:] + values[:-row] if inverse else values[row:] + values[:row]
        for col in range(4):
            out[4 * col + row] = values[col]
    return out


def _mix_columns(state: list[int], inverse: bool = False) -> list[int]:
    out = [0] * BLOCK
    for col in range(4):
        a = state[4 * col:4 * col + 4]
        if not inverse:
            out[4 * col + 0] = _gf_mul(a[0], 2) ^ _gf_mul(a[1], 3) ^ a[2] ^ a[3]
            out[4 * col + 1] = a[0] ^ _gf_mul(a[1], 2) ^ _gf_mul(a[2], 3) ^ a[3]
            out[4 * col + 2] = a[0] ^ a[1] ^ _gf_mul(a[2], 2) ^ _gf_mul(a[3], 3)
            out[4 * col + 3] = _gf_mul(a[0], 3) ^ a[1] ^ a[2] ^ _gf_mul(a[3], 2)
        else:
            out[4 * col + 0] = (_gf_mul(a[0], 14) ^ _gf_mul(a[1], 11)
                                ^ _gf_mul(a[2], 13) ^ _gf_mul(a[3], 9))
            out[4 * col + 1] = (_gf_mul(a[0], 9) ^ _gf_mul(a[1], 14)
                                ^ _gf_mul(a[2], 11) ^ _gf_mul(a[3], 13))
            out[4 * col + 2] = (_gf_mul(a[0], 13) ^ _gf_mul(a[1], 9)
                                ^ _gf_mul(a[2], 14) ^ _gf_mul(a[3], 11))
            out[4 * col + 3] = (_gf_mul(a[0], 11) ^ _gf_mul(a[1], 13)
                                ^ _gf_mul(a[2], 9) ^ _gf_mul(a[3], 14))
    return out


def _encrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    state = [b ^ k for b, k in zip(block, round_keys[0])]
    for rnd in range(1, len(round_keys) - 1):
        state = _mix_columns(_shift_rows([SBOX[b] for b in state]))
        state = [s ^ k for s, k in zip(state, round_keys[rnd])]
    state = _shift_rows([SBOX[b] for b in state])
    return bytes(s ^ k for s, k in zip(state, round_keys[-1]))


def _decrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    state = [b ^ k for b, k in zip(block, round_keys[-1])]
    for rnd in range(len(round_keys) - 2, 0, -1):
        state = [INV_SBOX[b] for b in _shift_rows(state, inverse=True)]
        state = [s ^ k for s, k in zip(state, round_keys[rnd])]
        state = _mix_columns(state, inverse=True)
    state = [INV_SBOX[b] for b in _shift_rows(state, inverse=True)]
    return bytes(s ^ k for s, k in zip(state, round_keys[0]))


def _pure_ecb(key: bytes, data: bytes, decrypt: bool = False) -> bytes:
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥")
    if len(data) % BLOCK:
        raise ValueError("密文/明文必须是 16 字节的整数倍")
    round_keys = _expand_key(key)
    run = _decrypt_block if decrypt else _encrypt_block
    return b"".join(run(data[i:i + BLOCK], round_keys) for i in range(0, len(data), BLOCK))


def _library_ecb(key: bytes, data: bytes, decrypt: bool = False):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    ctx = cipher.decryptor() if decrypt else cipher.encryptor()
    return ctx.update(data) + ctx.finalize()


def aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    padded = pkcs7_pad(plaintext)
    try:
        return _library_ecb(key, padded)
    except ImportError:
        return _pure_ecb(key, padded)


def aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    try:
        raw = _library_ecb(key, ciphertext, decrypt=True)
    except ImportError:
        raw = _pure_ecb(key, ciphertext, decrypt=True)
    return pkcs7_unpad(raw)


def aes_key_for_api(key: bytes) -> str:
    """iLink wants base64 of the *hex string*, not base64 of the raw key."""
    return base64.b64encode(key.hex().encode("ascii")).decode("ascii")


def parse_aes_key(encoded: str) -> bytes:
    """Reverse of :func:`aes_key_for_api`, tolerating a raw hex string."""
    if not encoded:
        raise ValueError("缺少 aes_key")
    try:
        decoded = base64.b64decode(encoded)
    except Exception:  # noqa: BLE001
        decoded = b""
    text = decoded.decode("ascii", "ignore").strip()
    if len(text) == 32:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    text = encoded.strip()
    if len(text) == 32:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    if len(decoded) == 16:
        return decoded
    raise ValueError(f"aes_key 形式不认识：{encoded[:24]!r}")


# --------------------------------------------------------------------------- #
# items
# --------------------------------------------------------------------------- #
def item_type_for(path: str | Path) -> int:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return ITEM_IMAGE
    if ext in VIDEO_EXTS:
        return ITEM_VIDEO
    if ext in VOICE_EXTS:
        return ITEM_VOICE
    return ITEM_FILE


def build_media_item(item_type: int, *, encrypt_query_param: str, aes_key: bytes,
                     filename: str, plaintext_size: int, ciphertext_size: int) -> Dict[str, Any]:
    """The outbound item shape iLink expects (see the module docstring)."""
    if item_type not in ITEM_KEY:
        raise ValueError(f"不是媒体类型：{item_type}")
    media = {"encrypt_query_param": encrypt_query_param,
             "aes_key": aes_key_for_api(aes_key), "encrypt_type": 1}
    if item_type == ITEM_IMAGE:
        body = {"mid_size": ciphertext_size}
    elif item_type == ITEM_FILE:
        body = {"file_name": Path(filename).name, "len": str(plaintext_size)}
    elif item_type == ITEM_VIDEO:
        body = {"video_size": ciphertext_size, "play_length": 0}
    else:
        body = {"playtime": 0}
    return {"type": item_type, ITEM_KEY[item_type]: {"media": media, **body}}


def parse_media_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pull ``{item_type, item, media, filename, size}`` out of an inbound item."""
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    key = ITEM_KEY.get(item_type or 0)
    if not key:
        return None
    body = item.get(key) or {}
    media = body.get("media") or {}
    return {"item_type": item_type, "key": key, "body": body, "media": media,
            "filename": str(body.get("file_name") or ""),
            "size": body.get("len") or body.get("mid_size") or 0,
            "encrypt_query_param": str(media.get("encrypt_query_param") or ""),
            "aes_key": str(media.get("aes_key") or ""),
            "full_url": str(media.get("full_url") or "")}


def file_upload_plan(path: str | Path) -> Dict[str, Any]:
    """Everything ``getuploadurl`` needs, plus the key to encrypt with."""
    data = Path(path).read_bytes()
    key = secrets.token_bytes(16)
    return {"path": Path(path), "plaintext": data, "key": key,
            "filekey": secrets.token_hex(16),
            "rawsize": len(data), "filesize": ((len(data) + 15) // 16) * 16,
            "rawfilemd5": hashlib.md5(data).hexdigest(),
            "media_type": MEDIA_TYPE_BY_ITEM[item_type_for(path)]}

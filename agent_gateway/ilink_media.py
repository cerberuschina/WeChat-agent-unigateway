"""Media over iLink: upload/download as free functions on top of a client.

Kept out of :mod:`agent_gateway.ilink` on purpose — that module is the protocol
core and stays small; this one is the envelope around it:

    getuploadurl → POST the AES-128-ECB ciphertext to the CDN → take the
    ``x-encrypted-param`` response header → that value goes into the item we send.

Inbound is the mirror image: the item carries ``encrypt_query_param`` + ``aes_key``,
we fetch ``{cdn}/download?encrypted_query_param=…`` and decrypt.

Everything here takes the client as an argument (``client.base_url`` / ``.token``),
so a fake client is enough to test the whole flow without touching WeChat.
"""
from __future__ import annotations

import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import ilink, media

EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
CDN_TIMEOUT_S = 120.0


def cdn_base(client: Any) -> str:
    return str(getattr(client, "cdn_base_url", "") or CDN_BASE_URL).rstrip("/")


def _http_bytes(method: str, url: str, *, data: Optional[bytes] = None,
                headers: Optional[Dict[str, str]] = None,
                timeout: float = 60.0) -> Tuple[int, Dict[str, str], bytes]:
    """A request that also needs the response **headers** (the CDN returns the
    upload token in ``x-encrypted-param``), and that may carry binary bodies."""
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (response.status,
                    {k.lower(): v for k, v in response.headers.items()},
                    response.read())
    except urllib.error.HTTPError as exc:
        return (exc.code,
                {k.lower(): v for k, v in (exc.headers or {}).items()},
                exc.read())
    except urllib.error.URLError as exc:
        raise ilink.ILinkError(f"网络错误（{method} {url.split('?')[0]}）：{exc.reason}") from exc


def upload_media(client: Any, to: str, path: str | Path) -> Dict[str, Any]:
    """Encrypt a local file, push it to the CDN, return the ready-to-send item."""
    plan = media.file_upload_plan(path)
    response = ilink._raise_for_status(
        ilink._request("POST", client.base_url, EP_GET_UPLOAD_URL, token=client.token,
                       payload={"filekey": plan["filekey"], "media_type": plan["media_type"],
                                "to_user_id": to, "rawsize": plan["rawsize"],
                                "rawfilemd5": plan["rawfilemd5"], "filesize": plan["filesize"],
                                "no_need_thumb": True, "aeskey": plan["key"].hex()},
                       timeout_ms=ilink.API_TIMEOUT_MS),
        "getuploadurl")

    upload_param = str(response.get("upload_param") or "")
    upload_url = str(response.get("upload_full_url") or "")
    if not upload_url and upload_param:
        upload_url = (f"{cdn_base(client)}/upload?"
                      f"encrypted_query_param={urllib.parse.quote(upload_param, safe='')}"
                      f"&filekey={urllib.parse.quote(plan['filekey'], safe='')}")
    if not upload_url:
        raise ilink.ILinkError(f"getUploadUrl 既没给 upload_param 也没给 upload_full_url：{response}")

    ciphertext = media.aes128_ecb_encrypt(plan["plaintext"], plan["key"])
    encrypted_query_param = _upload_ciphertext(upload_url, ciphertext)
    return media.build_media_item(media.item_type_for(path),
                                  encrypt_query_param=encrypted_query_param,
                                  aes_key=plan["key"], filename=plan["path"].name,
                                  plaintext_size=plan["rawsize"],
                                  ciphertext_size=len(ciphertext))


def _upload_ciphertext(upload_url: str, ciphertext: bytes) -> str:
    status, headers, body = _http_bytes("POST", upload_url, data=ciphertext,
                                        headers={"Content-Type": "application/octet-stream"},
                                        timeout=CDN_TIMEOUT_S)
    param = headers.get("x-encrypted-param") or ""
    if status == 200 and param:
        return param
    detail = body[:200].decode("utf-8", "replace")
    raise ilink.ILinkError(
        f"CDN 上传缺少 x-encrypted-param 头：{detail}" if status == 200
        else f"CDN 上传 HTTP {status}：{detail}")


def send_items(client: Any, to: str, item_list: List[Dict[str, Any]], *,
               context_token: Optional[str] = None) -> Dict[str, Any]:
    """Send an arbitrary item list (text, image, file, …)."""
    message: Dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": f"gw-{secrets.token_hex(8)}",
        "message_type": ilink.MSG_TYPE_BOT,
        "message_state": ilink.MSG_STATE_FINISH,
        "item_list": item_list,
    }
    if context_token:
        message["context_token"] = context_token
    return ilink._raise_for_status(
        ilink._request("POST", client.base_url, ilink.EP_SEND_MESSAGE, token=client.token,
                       payload={"msg": message}, timeout_ms=ilink.API_TIMEOUT_MS),
        "sendmessage")


def send_file(client: Any, to: str, path: str | Path, *,
              context_token: Optional[str] = None, caption: str = "") -> Dict[str, Any]:
    """Upload a local file and send it (optionally after a caption)."""
    item = upload_media(client, to, path)
    if caption:
        client.send_text(to, caption, context_token=context_token)
    return send_items(client, to, [item], context_token=context_token)


def download_media(client: Any, media_info: Dict[str, Any], dest_dir: str | Path, *,
                   name: str = "") -> Path:
    """Download + decrypt one inbound media item into ``dest_dir``."""
    encrypted = str(media_info.get("encrypt_query_param") or "")
    full_url = str(media_info.get("full_url") or "")
    if encrypted:
        url = (f"{cdn_base(client)}/download?"
               f"encrypted_query_param={urllib.parse.quote(encrypted, safe='')}")
    elif full_url:
        url = full_url
    else:
        raise ilink.ILinkError("这条媒体消息既没有 encrypt_query_param 也没有 full_url")

    status, _headers, body = _http_bytes("GET", url, timeout=CDN_TIMEOUT_S)
    if status != 200:
        raise ilink.ILinkError(f"CDN 下载 HTTP {status}")
    key_b64 = str(media_info.get("aes_key") or "")
    data = media.aes128_ecb_decrypt(body, media.parse_aes_key(key_b64)) if key_b64 else body

    target = Path(dest_dir) / (name or f"media-{secrets.token_hex(6)}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target

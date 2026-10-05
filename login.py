#!/usr/bin/env python
"""QR login: bind one WeChat to this gateway.

    python login.py                 # writes data/account.json
    python login.py -c my.json      # same config the gateway uses

The QR is printed as a URL *and* as a raw value. Open the URL, or hand the
value to any QR renderer — no third-party library required (pass
``--render`` if you have ``qrcode`` installed and want ASCII art in-terminal).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_gateway import ilink  # noqa: E402
from agent_gateway.config import ConfigError, load_config, save_account  # noqa: E402


def _render_ascii(value: str) -> bool:
    try:
        import qrcode  # type: ignore
    except ImportError:
        return False
    qr = qrcode.QRCode(border=1)
    qr.add_data(value)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    return True


def _write_png(value: str, path: str) -> bool:
    """Write the QR as a PNG (segno is pure-Python, no Pillow needed).

    Why PNG at all: a user may have to bind the bot from a phone — they can send
    the image to themselves and long-press → "识别图中二维码".
    """
    try:
        import segno  # type: ignore
    except ImportError:
        return False
    try:
        qr = segno.make(value, error="m")
        qr.save(path, scale=6, border=2, kind="png")
        return True
    except Exception:  # noqa: BLE001
        return False


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bind a WeChat account to agent-gateway")
    parser.add_argument("-c", "--config", default="gateway.json")
    parser.add_argument("--bot-type", default="3")
    parser.add_argument("--timeout", type=int, default=480)
    parser.add_argument("--render", action="store_true", help="print an ASCII QR (needs `qrcode`)")
    parser.add_argument("--png", default="", help="write each QR to this PNG path (needs `segno`)")
    parser.add_argument("--max-refreshes", type=int, default=30,
                        help="the QR dies after ~2 min; how many times to fetch a fresh one (default 30)")
    parser.add_argument("--out", default="", help="where to store creds (default: <data_dir>/account.json)")
    args = parser.parse_args(argv)

    data_dir = Path("data")
    try:
        cfg = load_config(args.config)
        data_dir = cfg.data_dir
    except ConfigError as exc:
        print(f"（读不到配置，改用默认 data/ 目录：{exc}）", file=sys.stderr)

    account_path = Path(args.out) if args.out else (data_dir / "account.json")

    def on_event(kind: str, payload: dict) -> None:
        if kind == "qr":
            value = str(payload.get("qrcode") or "")
            url = str(payload.get("url") or "")
            print("\n请用微信扫这个码（或在微信里打开链接），把它绑成网关的唯一入口：", flush=True)
            if url:
                print(f"  {url}", flush=True)
            print(f"  qrcode = {value}", flush=True)
            if args.png:
                if _write_png(url or value, args.png):
                    print(f"  二维码图片已写到 {args.png}", flush=True)
                else:
                    print("  （写 PNG 失败：pip install segno）", flush=True)
            if args.render and _render_ascii(url or value):
                print()
        else:
            status = str(payload.get("status") or "")
            if status == "scaned":
                print("已扫码，请在微信里确认…", flush=True)
            elif status == "expired":
                print("二维码过期，正在刷新…", flush=True)

    creds = ilink.qr_login(bot_type=args.bot_type, timeout_seconds=args.timeout,
                           max_refreshes=args.max_refreshes, on_event=on_event)
    if not creds:
        print("登录失败或超时。", file=sys.stderr)
        return 1

    save_account(account_path, account_id=creds["account_id"], token=creds["token"],
                 base_url=creds["base_url"], user_id=creds.get("user_id", ""))
    print(f"\n绑定成功，凭证已写入 {account_path}")
    print("下一步：python -m agent_gateway --config gateway.json")
    print("（别把 account.json 提交到 git —— .gitignore 已经挡了。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

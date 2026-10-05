#!/usr/bin/env python
"""Agent-side iLink client — put *any* agent on WeChat through the virtual gateway.

This is the other half of `agent_gateway.virtual_ilink`: the gateway pretends to
be Tencent, this pretends to be a normal WeChat integration. Between the two,
the agent itself needs to know nothing about WeChat.

    python clients/ilink_agent_client.py --name claude \
        --runner "claude -p {text}" --base-url http://127.0.0.1:18500

Flow: QR-login against the gateway (it mints a virtual identity) → long-poll
`getupdates` → for every message run the agent command → `sendmessage` the
answer back → the gateway forwards it to the real WeChat.

Useful flags
    --login-only     bind and print the identity, then exit
    --once           handle one message and exit (good for tests)
    --stdin          feed the message to the command on stdin instead of {text}
    --timeout N      per-message timeout for the agent command (default 900s)
    --creds PATH     where the virtual identity + cursor live (default data/virtual-client-<name>.json)
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_gateway import ilink  # noqa: E402

PLACEHOLDER = "{text}"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run_agent(argv: list[str], text: str, *, use_stdin: bool, timeout: float, cwd: str) -> str:
    """Run the agent command once and return its answer."""
    if use_stdin:
        command = argv
        stdin = text
    else:
        command = [text if part == PLACEHOLDER else part for part in argv]
        if command == argv and PLACEHOLDER not in argv:
            command = argv + [text]          # no placeholder: append the text
        stdin = None
    proc = subprocess.run(command, input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          cwd=cwd or None)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 and not out:
        raise RuntimeError(f"退出码 {proc.returncode}：{err[:300] or '（没有输出）'}")
    if err and not out:
        return err
    return out


def do_login(base_url: str, name: str, *, png: str = "", max_refreshes: int = 30,
             timeout: int = 480) -> dict:
    def on_event(kind: str, payload: dict) -> None:
        if kind == "qr":
            url = str(payload.get("url") or "")
            print("\n请批准这个 agent 接入（网关的批准页打开它给的链接即可）：", flush=True)
            if url:
                print(f"  {url}", flush=True)
            print(f"  qrcode = {payload.get('qrcode')}", flush=True)
            if png and url:
                try:
                    import segno  # type: ignore
                    segno.make(url, error="m").save(png, scale=6, border=2, kind="png")
                    print(f"  二维码图片：{png}", flush=True)
                except ImportError:
                    print("  （写 PNG 失败：pip install segno）", flush=True)
            print("  （如果这个 agent 已在网关的 auto_approve 里，会立刻通过）", flush=True)
        else:
            status = str(payload.get("status") or "")
            if status == "scaned":
                log("已扫码，等确认…")
            elif status == "expired":
                log("二维码过期，换一张…")

    creds = ilink.qr_login(base_url=base_url, timeout_seconds=timeout,
                           max_refreshes=max_refreshes, on_event=on_event)
    if not creds:
        raise SystemExit("登录失败：网关没给身份（超时？端口不对？）")
    log(f"拿到虚拟微信号：{creds['account_id']}（服务端 {creds['base_url']}）")
    return {"account_id": creds["account_id"], "token": creds["token"],
            "base_url": creds["base_url"], "cursor": "", "name": name}


def save_creds(path: Path, creds: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_creds(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an agent on WeChat via the virtual gateway")
    parser.add_argument("--name", default="agent", help="this agent's name (must match the gateway config)")
    parser.add_argument("--base-url", default="http://127.0.0.1:18500",
                        help="the gateway's virtual iLink server")
    parser.add_argument("--runner", default="", help=f"command template, e.g. 'claude -p {PLACEHOLDER}'")
    parser.add_argument("--stdin", action="store_true", help="pipe the message into the command instead")
    parser.add_argument("--cwd", default="", help="working directory for the agent command")
    parser.add_argument("--timeout", type=float, default=900.0, help="per-message timeout (seconds)")
    parser.add_argument("--max-chars", type=int, default=1200, help="split answers longer than this")
    parser.add_argument("--creds", default="", help="where to keep the virtual identity")
    parser.add_argument("--login-only", action="store_true")
    parser.add_argument("--once", action="store_true", help="handle a single message then exit")
    parser.add_argument("--max-refreshes", type=int, default=30)
    parser.add_argument("--login-timeout", type=int, default=480)
    parser.add_argument("--png", default="", help="write the QR to this PNG (needs segno)")
    parser.add_argument("--reuse-token", default="",
                        help="a token the agent already holds (see virtual.reuse_real_token_for)")
    args = parser.parse_args(argv)

    creds_path = Path(args.creds) if args.creds else Path("data") / f"virtual-client-{args.name}.json"
    creds = load_creds(creds_path)

    if args.reuse_token:
        # The agent is already bound to the real WeChat: keep that token, the
        # gateway accepts it as this agent's alias.
        creds = {"account_id": f"reuse-{args.name}", "token": args.reuse_token,
                 "base_url": args.base_url, "cursor": "", "name": args.name}
        save_creds(creds_path, creds)

    if not creds:
        creds = do_login(args.base_url, args.name, png=args.png,
                         max_refreshes=args.max_refreshes, timeout=args.login_timeout)
        save_creds(creds_path, creds)

    client = ilink.ILinkClient(creds["account_id"], creds["token"], base_url=creds["base_url"])
    log(f"身份 {creds['account_id']}，服务端 {creds['base_url']}，凭证 {creds_path}")

    try:                                     # a stale token must re-login, not spin
        client.get_config(creds["account_id"])
    except ilink.SessionExpired:
        log("旧身份已失效（网关重启会这样），重新扫码…")
        creds = do_login(args.base_url, args.name, png=args.png,
                         max_refreshes=args.max_refreshes, timeout=args.login_timeout)
        save_creds(creds_path, creds)
        client = ilink.ILinkClient(creds["account_id"], creds["token"], base_url=creds["base_url"])
    except ilink.ILinkError as exc:
        log(f"（探测身份时出问题，先继续：{exc}）")

    if args.login_only:
        print(json.dumps({k: creds[k] for k in ("account_id", "base_url", "name")},
                         ensure_ascii=False, indent=2))
        return 0

    if not args.runner:
        raise SystemExit("要么给 --runner，要么只做 --login-only。")
    # Windows paths in a --runner template would have their backslashes eaten by
    # posix-style splitting ("C:\x\y" -> "C:xy"), so normalise them to slashes.
    runner = shlex.split(args.runner.replace("\\", "/"))
    log(f"用命令处理消息：{' '.join(runner)}" + ("  (stdin)" if args.stdin else ""))

    cursor = str(creds.get("cursor") or "")
    handled = 0
    while True:
        try:
            response = client.get_updates(cursor)
        except ilink.SessionExpired:
            log("身份失效（ret=-14），退出；下次启动会重新扫码。")
            return 3
        except ilink.RateLimited:
            time.sleep(2)
            continue
        except ilink.ILinkError as exc:
            log(f"取消息失败：{exc}（2 秒后重试）")
            time.sleep(2)
            continue

        new_cursor = str(response.get("get_updates_buf") or "")
        if new_cursor and new_cursor != cursor:
            cursor = new_cursor
            creds["cursor"] = cursor
            save_creds(creds_path, creds)

        for message in response.get("msgs") or []:
            text = ilink.message_text(message)
            sender = ilink.sender_of(message)
            if not text.strip():
                log("收到一条没有文本的消息（图片/语音暂时不支持），跳过。")
                continue
            log(f"← {sender}: {text[:80]}")
            started = time.time()
            try:
                answer = run_agent(runner, text, use_stdin=args.stdin,
                                   timeout=args.timeout, cwd=args.cwd)
            except subprocess.TimeoutExpired:
                answer = f"（跑了 {args.timeout:.0f} 秒还没完，这次先放弃）"
            except Exception as exc:  # noqa: BLE001 - the agent must never kill the loop
                answer = f"（跑挂了：{exc}）"
            if not answer.strip():
                answer = "（这次没有输出）"
            log(f"→ {answer[:80]}（{time.time() - started:.1f}s）")
            for chunk in ilink.split_text(answer, args.max_chars):
                client.send_text(sender, chunk, context_token=message.get("context_token"))
            handled += 1
            if args.once:
                log("--once：处理完这条就退出。")
                return 0


if __name__ == "__main__":
    raise SystemExit(main())

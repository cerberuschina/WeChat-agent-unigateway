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
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_gateway import ilink  # noqa: E402

PLACEHOLDER = "{text}"
SESSION_PLACEHOLDER = "{session}"
SESSION_MARK = "##SESSION:"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def extract_session(answer: str) -> tuple[str, str]:
    """Pull a runner's ``##SESSION:<id>`` marker out of its output.

    A runner that supports multi-turn work prints the marker so the next message
    from the same peer can resume that conversation instead of starting cold.
    """
    session = ""
    kept: list[str] = []
    for line in answer.splitlines():
        if line.strip().startswith(SESSION_MARK):
            session = line.strip()[len(SESSION_MARK):].strip()
        else:
            kept.append(line)
    return "\n".join(kept).strip(), session


def load_sessions(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_sessions(path: Path, sessions: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")


def run_agent(argv: list[str], text: str, *, use_stdin: bool, timeout: float, cwd: str,
              session: str = "") -> str:
    """Run the agent command once and return its answer."""
    if use_stdin:
        command = argv
        stdin = text
    else:
        command = [text if part == PLACEHOLDER else session if part == SESSION_PLACEHOLDER else part
                   for part in argv]
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


def run_with_progress(runner: list[str], text: str, args, session: str, sender: str,
                      client, message: dict) -> str:
    """Run the agent while telling the peer it is still alive.

    WeChat cannot edit a sent message, so "streaming" here means: the gateway
    holds the typing indicator, and we post a few progress notes until the answer
    exists. Notes are deliberately few and back off (60s → 120s → 240s): WeChat
    allows a bot only ~10 messages before the user replies, and the answer must
    not be the thing that runs out of budget.
    """
    holder: dict = {}

    def work() -> None:
        try:
            holder["answer"] = run_agent(runner, text, use_stdin=args.stdin,
                                         timeout=args.timeout, cwd=args.cwd, session=session)
        except subprocess.TimeoutExpired:
            holder["answer"] = f"（跑了 {args.timeout:.0f} 秒还没完，这次先放弃）"
        except Exception as exc:  # noqa: BLE001 - the agent must never kill the loop
            holder["answer"] = f"（跑挂了：{exc}）"

    worker = threading.Thread(target=work, name="runner", daemon=True)
    started = time.time()
    worker.start()
    delay = max(float(args.progress_every), 0.0)
    notes = 0
    while worker.is_alive():
        worker.join(timeout=delay or 5.0)
        if not worker.is_alive():
            break
        if not delay or notes >= int(args.max_progress_notes):
            continue                       # keep waiting quietly, do not spend the budget
        elapsed = time.time() - started
        note = f"⏳ 还在跑（已 {int(elapsed // 60)} 分 {int(elapsed % 60):02d} 秒），完事我把结果发上来。"
        try:
            client.send_text(sender, note, context_token=message.get("context_token"))
            notes += 1
            delay = min(delay * 2, 600.0)   # back off: 60s → 120s → 240s …
        except Exception as exc:  # noqa: BLE001
            log(f"（进度发不出去：{exc}）")
    return holder.get("answer", "（没有结果）")


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
    parser.add_argument("--timeout", type=float, default=3600.0,
                        help="per-message timeout (seconds); a long agent run must not be cut short")
    parser.add_argument("--max-chars", type=int, default=0,
                        help="0 = send the answer whole and let the gateway render/split for WeChat")
    parser.add_argument("--progress-every", type=float, default=60.0,
                        help="first 「还在跑」 note after N seconds, then doubling (0 = off)")
    parser.add_argument("--max-progress-notes", type=int, default=3,
                        help="how many progress notes at most: WeChat allows a bot ~10 "
                             "messages before the user replies, and the answer needs the rest")
    parser.add_argument("--creds", default="", help="where to keep the virtual identity")
    parser.add_argument("--session-store", default="",
                        help="where to keep per-peer session ids (default: next to --creds); "
                             "use {session} in --runner to resume the conversation")
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
    sessions_path = (Path(args.session_store) if args.session_store
                     else creds_path.with_suffix(".sessions.json"))
    sessions = load_sessions(sessions_path)

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
            session = sessions.get(sender, "")
            answer = run_with_progress(runner, text, args, session, sender, client, message)
            answer, new_session = extract_session(answer)
            if new_session and new_session != session:
                sessions[sender] = new_session
                save_sessions(sessions_path, sessions)
                log(f"（会话已记下，下一条接着聊：{new_session[:8]}…）")
            if not answer.strip():
                answer = "（这次没有输出）"
            log(f"→ {answer[:80]}（{time.time() - started:.1f}s）")
            # 0 = hand the whole answer to the gateway, which renders Markdown for
            # WeChat and splits it into bubbles (it knows the channel's limits).
            chunks = [answer] if args.max_chars <= 0 else ilink.split_text(answer, args.max_chars)
            for chunk in chunks:
                client.send_text(sender, chunk, context_token=message.get("context_token"))
            handled += 1
            if args.once:
                log("--once：处理完这条就退出。")
                return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_gateway import ilink, ilink_media, media  # noqa: E402

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


def heartbeat(client, sender: str, message: dict, state: int) -> None:
    """Tell the gateway the agent is still alive.

    This goes to the *gateway*, never to WeChat: the gateway owns the real typing
    indicator, so a heartbeat costs zero messages out of the per-turn budget.
    """
    if not hasattr(client, "send_typing"):
        return
    try:
        client.send_typing(sender, state, typing_ticket="",
                           context_token=message.get("context_token"))
    except Exception as exc:  # noqa: BLE001 - a heartbeat must never break the run
        log(f"（心跳发不出去：{exc}）")


def run_with_progress(runner: list[str], text: str, args, session: str, sender: str,
                      client, message: dict) -> str:
    """Run the agent, keeping the peer's 「正在输入」 alive while it works.

    WeChat cannot edit a sent message, and it lets a bot send only ~10 messages
    before the user replies — so progress *messages* are a budget leak. Default is
    therefore a heartbeat: the agent says "still working", the gateway holds the
    typing indicator, and nothing is sent to the real WeChat. ``--progress-notes``
    opts back into visible notes (which do spend the budget).
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
    hb = max(float(args.progress_every), 0.0)
    notes = getattr(args, "progress_notes", False)
    delay = hb
    beating = False
    # 立刻打第一拍：微信的「正在输入」自己有寿命，等满一个周期再发的话，
    # 每轮开头那几十秒看起来跟死了没区别。心跳是零微信开销的，早点发不亏。
    if hb and not notes:
        heartbeat(client, sender, message, ilink.TYPING_START)
        beating = True
    while worker.is_alive():
        worker.join(timeout=delay or 5.0)
        if not worker.is_alive():
            break
        if not delay:
            continue
        elapsed = time.time() - started
        if notes:
            note = (f"⏳ 还在跑（已 {int(elapsed // 60)} 分 {int(elapsed % 60):02d} 秒），"
                    f"完事我把结果发上来。")
            try:
                client.send_text(sender, note, context_token=message.get("context_token"))
                delay = min(delay * 2, 600.0)   # 退避：进度是花微信额度的
            except Exception as exc:  # noqa: BLE001
                log(f"（进度发不出去：{exc}）")
        else:
            heartbeat(client, sender, message, ilink.TYPING_START)
            beating = True
    if beating:
        heartbeat(client, sender, message, ilink.TYPING_STOP)
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
    parser.add_argument("--progress-every", type=float, default=45.0,
                        help="heartbeat every N seconds while the agent works (0 = off); "
                             "heartbeats go to the gateway and cost no WeChat messages")
    parser.add_argument("--progress-notes", action="store_true",
                        help="send visible 「还在跑」 notes instead of heartbeats "
                             "(these DO spend WeChat's ~10 messages per turn)")
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
    parser.add_argument("--bind-key", default="",
                        help="pre-shared key a remote gateway requires before it hands out "
                             "an identity (also via the ILINK_BIND_KEY environment variable)")
    args = parser.parse_args(argv)

    creds_path = Path(args.creds) if args.creds else Path("data") / f"virtual-client-{args.name}.json"
    if args.bind_key:
        # The QR request is built deep inside ilink.fetch_qr; the env is how the
        # key travels without threading it through every call.
        os.environ["ILINK_BIND_KEY"] = args.bind_key
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
            if text.strip() and not is_local_gateway(getattr(client, "base_url", "")):
                # 网关在别的机器上：把「下载地址」换成本机真能读到的文件
                text = materialize_media(text, client, Path("data") / "inbox")
            if not text.strip():
                log("收到一条没有内容的消息，跳过。")
                continue
            log(f"← {sender}: {text[:80]}")
            started = time.time()
            session = sessions.get(sender, "")
            export_approval_env(client, sender)   # 工具钩子靠这几个变量找回网关
            answer = run_with_progress(runner, text, args, session, sender, client, message)
            answer, new_session = extract_session(answer)
            answer, media_paths = extract_media_marks(answer)
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
            for path in media_paths:
                send_local_media(client, sender, path, message)
            handled += 1
            if args.once:
                log("--once：处理完这条就退出。")
                return 0


def extract_media_marks(answer: str) -> tuple[str, list[str]]:
    """Pull ``##FILE:<path>`` / ``##IMAGE:<path>`` lines out of the runner's output.

    The runner is what knows it produced an artifact; it marks the path, and this
    client turns the mark into a real attachment.
    """
    paths: list[str] = []
    kept: list[str] = []
    for line in answer.splitlines():
        stripped = line.strip()
        if stripped.startswith(("##FILE:", "##IMAGE:")):
            path = stripped.split(":", 1)[1].strip()
            if path:
                paths.append(path)
                continue
        kept.append(line)
    return "\n".join(kept).strip(), paths


def is_local_gateway(base_url: str) -> bool:
    """True when the gateway runs on this very machine (paths are then shared)."""
    import urllib.parse as _urlparse

    host = (_urlparse.urlsplit(base_url or "").hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}


def _no_proxy_opener():
    """An opener that ignores HTTP(S)_PROXY.

    The gateway is on this machine or on the LAN; a corporate/system proxy in the
    middle would turn those requests into 502s.
    """
    import urllib.request as _urlrequest

    return _urlrequest.build_opener(_urlrequest.ProxyHandler({}))


def ask_approval(client, peer: str, title: str, detail: str = "", *, kind: str = "command",
                 ttl: float = 180, wait: float = 0, timeout: float = 30) -> dict:
    """Ask the human through the gateway and (by default) wait for the answer.

    This is the one call an agent needs to stop dying on "requires approval": the
    gateway puts the question on the phone, the user answers there, and the
    verdict comes back as ``{"decision": "allow"|"deny"|"expired", ...}``.
    """
    import urllib.request

    base = str(getattr(client, "base_url", "") or "").rstrip("/")
    token = str(getattr(client, "token", "") or "")
    request = urllib.request.Request(
        f"{base}/agent/approval",
        data=json.dumps({"peer": peer, "title": title, "detail": detail,
                         "kind": kind, "ttl": ttl}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with _no_proxy_opener().open(request, timeout=timeout) as response:
        started = json.loads(response.read().decode("utf-8") or "{}")
    approval_id = str(started.get("id") or "")
    if not approval_id:
        raise RuntimeError(f"网关没有受理这次放行请求：{started}")
    waiting = float(wait or 0) or (float(ttl) + 20)
    query = urllib.request.Request(
        f"{base}/agent/approval/{approval_id}/wait?timeout={int(waiting)}",
        headers={"Authorization": f"Bearer {token}"})
    with _no_proxy_opener().open(query, timeout=waiting + 30) as response:
        answered = json.loads(response.read().decode("utf-8") or "{}")
    return dict(answered.get("approval") or {})


def export_approval_env(client, peer: str, ttl: float = 180) -> None:
    """Publish the gateway coordinates for anything the runner spawns.

    A tool hook (see ``hooks/claude_approval_hook.py``) runs as a child process and
    cannot ask *us* for the token, so it inherits these instead.
    """
    os.environ["AGW_APPROVAL_URL"] = str(getattr(client, "base_url", "") or "")
    os.environ["AGW_TOKEN"] = str(getattr(client, "token", "") or "")
    os.environ["AGW_PEER"] = peer or ""
    os.environ["AGW_APPROVAL_TTL"] = str(int(ttl))


def upload_blob(client, name: str, data: bytes) -> str:
    """Push file bytes to the gateway; returns a ``blob:<id>`` reference.

    A remote agent cannot hand over a path — its filesystem is not the gateway's.
    """
    import urllib.parse as _urlparse
    import urllib.request

    url = f"{str(client.base_url).rstrip('/')}/upload"
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {client.token}",
                 "X-File-Name": _urlparse.quote(name),
                 "Content-Type": "application/octet-stream"})
    with _no_proxy_opener().open(request, timeout=300) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if int(payload.get("ret", -1)) != 0 or not payload.get("blob"):
        raise RuntimeError(f"网关拒绝了上传：{payload}")
    return str(payload["blob"])


def fetch_remote_media(client, url: str, out_dir: Path) -> Path:
    """Download one media file the gateway stored (remote agents only)."""
    import urllib.parse as _urlparse
    import urllib.request

    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {client.token}"})
    with _no_proxy_opener().open(request, timeout=300) as response:
        data = response.read()
        name = _urlparse.unquote(response.headers.get("X-File-Name") or "media.bin")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / Path(name).name
    target.write_bytes(data)
    return target


def materialize_media(text: str, client, out_dir: Path) -> str:
    """Turn the gateway's 「下载地址：…」 lines into real local paths.

    The gateway sits on another machine, so the path it printed means nothing
    here; we fetch the bytes with our own token and hand the agent a usable path.
    """
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("下载地址："):
            url = stripped[len("下载地址："):].strip()
            try:
                target = fetch_remote_media(client, url, out_dir)
                lines.append(f"本地路径：{target}")
                continue
            except Exception as exc:  # noqa: BLE001 - keep the URL so the agent can try
                log(f"（取不到远端文件 {url}：{exc}）")
        lines.append(line)
    return "\n".join(lines)


def send_local_media(client, sender: str, path: str, message: dict) -> None:
    """Hand a file to the gateway, which encrypts and uploads it.

    Same machine → the gateway reads it itself (``localpath:``). Different machine
    → we POST the bytes to the gateway's ``/upload`` first (``blob:``), because our
    path means nothing over there.
    """
    try:
        absolute = os.path.abspath(path)
        if not os.path.isfile(absolute):
            log(f"（{path} 不存在，跳过）")
            return
        reference = f"localpath:{absolute}"
        if not is_local_gateway(getattr(client, "base_url", "")):
            with open(absolute, "rb") as handle:
                reference = upload_blob(client, os.path.basename(absolute), handle.read())
        item = media.build_media_item(
            media.item_type_for(absolute),
            encrypt_query_param=reference,
            aes_key=bytes(16), filename=os.path.basename(absolute),
            plaintext_size=os.path.getsize(absolute), ciphertext_size=0)
        ilink_media.send_items(client, sender, [item],
                               context_token=message.get("context_token"))
        log(f"（已交给网关发送附件：{os.path.basename(absolute)}）")
    except Exception as exc:  # noqa: BLE001 - an attachment must not break the reply loop
        log(f"（附件发不出去 {path}：{exc}）")


if __name__ == "__main__":
    raise SystemExit(main())

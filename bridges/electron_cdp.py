#!/usr/bin/env python
"""Drive any Electron chat app over the Chrome DevTools Protocol (CDP).

Why this exists: some agents ship as **desktop apps only** — no CLI, no HTTP
API (WorkBuddy Desktop is one: its only loopback endpoint is a presence probe,
everything else is deliberately 404). But every Electron app can expose CDP if
it is started with ``--remote-debugging-port``, and CDP is enough to type into a
composer and read the reply back.

    # 1) restart the app with a debug port
    #    WorkBuddy: close it, then
    #    "D:\\Program Files\\WorkBuddy\\WorkBuddy.exe" --remote-debugging-port=9222
    #
    # 2) ask it something
    python bridges/electron_cdp.py --profile profiles/workbuddy.json --text "你好"

The gateway calls it as an ``exec`` backend:

    "workbuddy": {
      "type": "exec", "label": "WorkBuddy", "prefix": "w",
      "command": ["python", "bridges/electron_cdp.py",
                  "--profile", "profiles/workbuddy.json", "--text", "{text}"]
    }

Selector profiles live in ``profiles/*.json`` — see ``profiles/workbuddy.json``
and adjust the selectors once, on your machine.

Dependency: ``pip install websocket-client`` (only this optional bridge needs it;
the gateway core stays stdlib-only).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


class BridgeError(RuntimeError):
    """Anything the user should read as a sentence."""


def load_profile(path: str | Path) -> Dict[str, Any]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    for key in ("target_match", "composer_selector", "message_selector"):
        if not data.get(key):
            raise BridgeError(f"profile {path} 缺字段 {key}")
    data.setdefault("send_selector", "")
    data.setdefault("busy_selector", "")
    data.setdefault("settle_seconds", 1.5)
    data.setdefault("timeout", 600)
    data.setdefault("description", "")
    return data


def list_targets(port: int, timeout: float = 5.0) -> List[Dict[str, Any]]:
    url = f"http://127.0.0.1:{port}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise BridgeError(
            f"连不上 127.0.0.1:{port} 的调试端口（{exc}）。\n"
            f"这个应用必须以 --remote-debugging-port={port} 启动；已经在跑的话要先退出再带参数重启。"
        ) from exc
    return [t for t in data if isinstance(t, dict) and t.get("webSocketDebuggerUrl")]


def pick_target(targets: List[Dict[str, Any]], match: str) -> Dict[str, Any]:
    lowered = match.lower()
    for target in targets:
        if lowered in str(target.get("url", "")).lower() or lowered in str(target.get("title", "")).lower():
            return target
    listed = "\n".join(f"  - {t.get('title')} :: {t.get('url')}" for t in targets) or "  （没有可用 target）"
    raise BridgeError(f"没有匹配 {match!r} 的窗口。当前有：\n{listed}")


def js_composer_fill(selector: str, send_selector: str, text: str) -> str:
    """Set the composer's value the way React/ProseMirror notices, then send."""
    return f"""
(() => {{
  const box = document.querySelector({json.dumps(selector)});
  if (!box) return {{ ok: false, error: '找不到输入框: ' + {json.dumps(selector)} }};
  const text = {json.dumps(text)};
  const el = box.matches('textarea, input') ? box : (box.querySelector('textarea, [contenteditable="true"]') || box);
  el.focus();
  if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {{
    const setter = Object.getOwnPropertyDescriptor(el.tagName === 'TEXTAREA'
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value').set;
    setter.call(el, text);
    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
  }} else {{
    el.textContent = text;
    el.dispatchEvent(new InputEvent('input', {{ bubbles: true, data: text }}));
  }}
  const sendSel = {json.dumps(send_selector)};
  const button = sendSel ? document.querySelector(sendSel) : null;
  if (button) {{ button.click(); return {{ ok: true, how: 'button' }}; }}
  el.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }}));
  return {{ ok: true, how: 'enter' }};
}})()
"""


def js_count_messages(selector: str) -> str:
    return f"document.querySelectorAll({json.dumps(selector)}).length"


def js_last_message(selector: str) -> str:
    return (f"(() => {{ const all = document.querySelectorAll({json.dumps(selector)});"
            f" return all.length ? all[all.length - 1].innerText : ''; }})()")


def js_is_busy(selector: str) -> str:
    if not selector:
        return "false"
    return f"!!document.querySelector({json.dumps(selector)})"


class CDP:
    """Tiny CDP client (needs websocket-client)."""

    def __init__(self, ws_url: str, timeout: float = 60.0):
        try:
            import websocket  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise BridgeError("需要 websocket-client：pip install websocket-client") from exc
        self._ws = websocket.create_connection(ws_url, timeout=timeout)
        self._id = 0

    def eval(self, expression: str, timeout: float = 60.0) -> Any:
        self._id += 1
        message_id = self._id
        self._ws.send(json.dumps({
            "id": message_id, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }))
        self._ws.settimeout(timeout)
        while True:
            raw = self._ws.recv()
            if not raw:
                raise BridgeError("调试连接被关闭")
            payload = json.loads(raw)
            if payload.get("id") != message_id:
                continue
            if "error" in payload:
                raise BridgeError(f"CDP 报错：{payload['error']}")
            result = payload.get("result", {}).get("result", {})
            if result.get("subtype") == "error":
                raise BridgeError(f"页面里抛错：{result.get('description')}")
            return result.get("value")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass


def ask(port: int, profile: Dict[str, Any], text: str, *, quiet: bool = False) -> str:
    targets = list_targets(port)
    target = pick_target(targets, str(profile["target_match"]))
    cdp = CDP(target["webSocketDebuggerUrl"], timeout=profile["timeout"])
    try:
        before = cdp.eval(js_count_messages(profile["message_selector"])) or 0
        filled = cdp.eval(js_composer_fill(profile["composer_selector"],
                                           profile.get("send_selector", ""), text))
        if isinstance(filled, dict) and filled.get("ok") is False:
            raise BridgeError(str(filled.get("error")))
        deadline = time.time() + float(profile["timeout"])
        last = ""
        stable = 0
        while time.time() < deadline:
            time.sleep(float(profile["settle_seconds"]))
            busy = cdp.eval(js_is_busy(profile.get("busy_selector", "")))
            count = cdp.eval(js_count_messages(profile["message_selector"])) or 0
            current = cdp.eval(js_last_message(profile["message_selector"])) or ""
            if count > before and current and current == last:
                stable += 1
            else:
                stable = 0
            last = current or last
            if not busy and stable >= 1 and count > before:
                return current
        raise BridgeError(f"等回复超时（>{profile['timeout']}s）；最后一条还停在：{last[:120]!r}")
    finally:
        cdp.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ask an Electron chat app over CDP")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--text", default="", help="prompt (or pipe it on stdin)")
    parser.add_argument("--port", type=int, default=0, help="override the profile's debug port")
    parser.add_argument("--list", action="store_true", help="list targets and exit")
    args = parser.parse_args(argv)

    try:
        profile = load_profile(args.profile)
        port = args.port or int(profile.get("port") or 9222)
        if args.list:
            for target in list_targets(port):
                print(f"{target.get('title')} :: {target.get('url')}")
            return 0
        text = args.text or sys.stdin.read().strip()
        if not text:
            print("没有要发的内容。", file=sys.stderr)
            return 2
        print(ask(port, profile, text))
        return 0
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

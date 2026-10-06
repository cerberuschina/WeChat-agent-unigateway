#!/usr/bin/env python
"""装作 claude 的 stream-json 会话进程 —— 给 clients/stream_session.py 的测试用。

只实现协议里我们依赖的那几条：
  * stdin 上一行一个 JSON 的用户消息 → 回一个 assistant 事件 + 一个 result 事件
  * control_request（interrupt）→ 回 control_response，并把请求记进 --record 文件
  * --delay 让一轮变慢，好让测试在"跑着的时候"插话
"""
from __future__ import annotations

import argparse
import json
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--delay", type=float, default=0.0, help="每轮回答前先等这么久")
parser.add_argument("--record", default="", help="把收到的事件写进这个文件")
parser.add_argument("--session-id", default="fake-session-1")
parser.add_argument("--die-after", type=int, default=0, help="跑完第 N 轮就退出")
parser.add_argument("--silent", action="store_true", help="收话但不回答（测超时）")
args = parser.parse_args()


def record(entry: dict) -> None:
    if args.record:
        with open(args.record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def answer(text: str, turns: int, stop: str = "end_turn") -> None:
    emit({"type": "system", "subtype": "noise"})          # 真进程也会夹一堆 system
    if not args.silent:
        emit({"type": "assistant",
              "message": {"content": [{"type": "text", "text": text}]}})
        emit({"type": "result", "subtype": "success", "result": text,
              "session_id": args.session_id, "stop_reason": stop, "total_cost_usd": 0.001})


turns = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    record(event)
    if event.get("type") == "control_request":
        emit({"type": "control_response",
              "response": {"subtype": "success", "request_id": event.get("request_id")}})
        continue
    blocks = (event.get("message") or {}).get("content") or []
    text = "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))
    turns += 1
    if args.delay:
        time.sleep(args.delay)
    answer(f"回复：{text}（第 {turns} 轮）", turns)
    if args.die_after and turns >= args.die_after:
        break

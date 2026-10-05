#!/usr/bin/env python
"""End-to-end demo: a real iLink client talks to the *virtual* server.

Run it and watch the whole story in one process:

    python examples/virtual_loop_demo.py

No WeChat, no network, no credentials: the only thing redirected is the base
URL. The client code below is ordinary iLink client code (the same class the
gateway itself uses) — it believes it is talking to `ilinkai.weixin.qq.com`.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_gateway import ilink  # noqa: E402
from agent_gateway.virtual_ilink import VirtualILinkServer  # noqa: E402

PEER = "o9cq80…@im.wechat"          # 真微信那头（网关眼里的联系人）
AGENT = "claude"                     # 一个想接微信的 agent


def main() -> int:
    outbound = []

    server = VirtualILinkServer(
        port=0,                                     # 随便挑个空闲端口
        data_dir=Path("data/virtual-demo"),
        auto_approve=[AGENT],                        # 演示：预先授权，免人工批准
        on_outbound=lambda bind, text: outbound.append((bind.name, text)),
        on_log=lambda message: print(f"  [网关] {message}"),
    )
    host, port = server.start()
    print(f"\n① 虚拟 iLink 服务端起来了：http://{host}:{port}\n")

    # —— agent 侧：完全正常的 iLink 登录流程，唯一的区别是 base_url ——
    print("② agent 照常扫码登录（它以为对面是微信）")
    creds = ilink.qr_login(base_url=server.base_url(), max_refreshes=1, timeout_seconds=10,
                           on_event=lambda kind, payload: None)
    if not creds:
        print("   登录失败（不应该发生）", file=sys.stderr)
        return 1
    print(f"   拿到「微信号」：{creds['account_id']}")
    print(f"   服务端地址：{creds['base_url']}    ← 网关自己，不是 ilinkai.weixin.qq.com")

    client = ilink.ILinkClient(creds["account_id"], creds["token"], base_url=creds["base_url"])
    print(f"   token 前 8 位：{creds['token'][:8]}…（网关签发的虚拟凭证）\n")

    # —— 真微信来消息：网关把它放进这个 agent 的队列 ——
    text = "帮我看下这个空指针"
    print(f"③ 真微信来了一条消息：{text!r}")
    server.deliver(AGENT, text=text, peer=PEER, message_id="m-1", context_token="ctx-1")

    response = client.get_updates("")
    messages = response.get("msgs") or []
    if not messages:
        print("   agent 没拉到消息（不应该发生）", file=sys.stderr)
        return 1
    msg = messages[0]
    print(f"   agent 长轮询拉到：{msg['item_list'][0]['text_item']['text']!r}"
          f"（来自 {msg['from_user_id']}）")
    print(f"   游标 = {response['get_updates_buf']}\n")

    # —— agent 回话：sendmessage 到网关，网关再发进真微信 ——
    answer = "第 42 行的 user 可能为空，加个判空就行。"
    print(f"④ agent 回话：{answer!r}")
    client.send_text(PEER, answer, context_token=msg.get("context_token"))
    time.sleep(0.1)
    if outbound:
        name, sent = outbound[0]
        print(f"   网关已把它转发进真微信（发言人 = {name}）：{sent!r}\n")
    else:
        print("   没有转发出去（不应该发生）", file=sys.stderr)
        return 1

    print("⑤ 全程：agent 只把 base_url 指过来，没改一行代码，也没要第二个微信号。")
    server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

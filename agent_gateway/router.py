"""Routing rules: one WeChat chat, N agents.

Pure functions only — the gateway calls :func:`route` and acts on the result,
which makes every rule here unit-testable without WeChat or network.

Grammar (agents configure their own prefix, e.g. ``h`` / ``c`` / ``w``):

    /c fix the flaky test        -> one-shot: send to the agent whose prefix is c
    /use claude                  -> sticky: this chat talks to claude from now on
    /new                         -> the chat's agent starts a fresh session
    /sessions  /resume 3         -> the chat's agent lists / switches its sessions
    /approve k7  /reject k7      -> answer an agent's "may I?" (see approvals.py)
    /always                      -> allow every question left in this round
    /ilink     (or 保活 alone)   -> channel keepalive: the gateway answers, no agent
    /who                         -> which agent is this chat talking to
    /agents                      -> the roster
    /help                        -> this text
    anything else                -> the chat's sticky agent (or the default)

Command names win over agent prefixes, so an agent cannot hijack ``/help``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

COMMANDS = ("help", "agents", "who", "use", "new", "reset", "sessions", "resume", "ping",
            "approve", "reject", "always", "stop", "queue", "ilink")


@dataclass
class Decision:
    """What the gateway should do with one inbound message."""

    kind: str                     # "reply" | "dispatch" | "ignore" | "keepalive" | "approve" ...
    agent: str = ""               # dispatch target (agent name)
    text: str = ""                # payload for the agent, or the reply body
    note: str = ""                # free-form, for logs
    set_sticky: Optional[str] = None  # gateway persists this as the chat's agent
    metadata: dict = field(default_factory=dict)


COMMAND_PREFIXES = ("/", "／", "!")


def _norm(text: str) -> str:
    return (text or "").strip()


def _compact(text: str) -> str:
    """Lowercase, drop spaces/punctuation: "iLink 保活！" and "ilink保活" are one shape."""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


# Whole-message shapes only. A real request that merely contains the word ("保活完
# 顺手看下盘口") must still reach the agent, so nothing longer matches.
_KEEPALIVE_RE = re.compile(
    r"^(?:保活|保活信息|保活消息|保活一下|心跳|"
    r"ilink|ilink保活|ilink保活信息|ilink保活消息|ilink保活一下|保活ilink|"
    r"keepalive|ilinkkeepalive|keepaliveping|ping)$"
)


def is_keepalive(text: str) -> bool:
    """True when the message is nothing but a channel ping ("保活" / "ilink保活")."""
    return bool(_KEEPALIVE_RE.match(_compact(text)))


def roster_lines(cfg) -> List[str]:
    lines = []
    for agent in cfg.enabled_agents():
        marker = "（默认）" if agent.name == (cfg.fallback_agent() or agent).name else ""
        prefix = f"/{agent.prefix} " if agent.prefix else ""
        lines.append(f"  {prefix or '　　　'} {agent.display} [{agent.name}] {marker}".rstrip())
    return lines


def help_text(cfg, sticky: Optional[str]) -> str:
    current = cfg.agent(sticky) or cfg.fallback_agent()
    lines = ["这是本机 agent 网关：一个微信，接住本机所有 agent。", ""]
    lines.append(f"直接说话 → 当前 agent（现在：{current.display if current else '-'}）")
    for agent in cfg.enabled_agents():
        if agent.prefix:
            lines.append(f"/{agent.prefix} 内容 → 这一条只发给「{agent.display}」")
    lines += [
        "/use <名字> → 这个会话以后都发给它",
        "/new → 让当前 agent 开一个新会话（之前的上下文不再带上）",
        "/sessions → 看当前 agent 有哪些会话可以接着聊",
        "/resume <编号> → 切到其中一条，下一条消息接着那个会话",
        "/approve <编号> / /reject <编号> → 放行或拒绝 agent 等你点头的那件事",
        "/always → 这一轮剩下的都别再问，全部放行",
        "/stop → 让正在跑的 agent 停下来（它几秒内会收到）",
        "/queue <内容> → 这条排在正在跑的活后面，不打断它",
        "/ilink（或只发「保活」）→ 通道保活：网关自己回一句，不打扰 agent",
        "/agents 看名单　/who 看当前　/help 看这条",
        "",
        "名单：",
        *roster_lines(cfg),
    ]
    return "\n".join(lines)


def is_command(text: str) -> bool:
    """True when ``route`` would read the message as a ``/command``."""
    return _norm(text).startswith(COMMAND_PREFIXES)


def route(text: str, cfg, *, sticky: Optional[str] = None) -> Decision:
    """Decide what to do with one inbound message."""
    raw = _norm(text)
    if not raw:
        return Decision("ignore", note="empty")

    if raw.startswith(COMMAND_PREFIXES):
        head, _, rest = raw[1:].partition(" ")
        name = head.strip().lower()

        if name in ("help", "?"):
            return Decision("reply", text=help_text(cfg, sticky), note="help")
        if name == "agents":
            return Decision("reply", text="名单：\n" + "\n".join(roster_lines(cfg)), note="agents")
        if name == "ping":
            return Decision("reply", text="网关在。", note="ping")
        if name in ("ilink", "keepalive"):
            # /ilink 只为保活存在（网关自己回，agent 看不到）。带了别的内容就明说，
            # 免得一条真活儿被静默吞掉。
            if rest.strip() and not is_keepalive(rest):
                return Decision("reply",
                                text="/ilink 只是给通道保活用的。要干活就直接说，别加 /ilink。",
                                note="ilink-usage")
            return Decision("keepalive", note="keepalive")
        if name == "who":
            current = cfg.agent(sticky) or cfg.fallback_agent()
            return Decision("reply",
                            text=f"当前 agent：{current.display} [{current.name}]" if current
                            else "当前没有可用的 agent。",
                            note="who")
        if name == "use":
            wanted = rest.strip().lower()
            if not wanted:
                return Decision("reply", text="用法：/use <agent 名>，例如 /use claude。", note="use-usage")
            agent = cfg.agent(wanted) or cfg.agent_for_prefix(wanted)
            if not agent or not agent.enabled:
                known = "、".join(a.name for a in cfg.enabled_agents())
                return Decision("reply", text=f"没有这个 agent：{wanted}。有的是：{known}", note="use-unknown")
            return Decision("reply", text=f"好，这个会话以后发给「{agent.display}」。[{agent.name}]",
                            note="use", set_sticky=agent.name)

        if name in ("new", "reset", "sessions", "resume"):
            # 会话类命令——/new 换一条新的，/sessions 列出能接着聊的，/resume 切过去。
            # 会话本体在 agent 自己那边（Hermes 的 gateway、claude 客户端的模块里），
            # 网关碰不到它，所以只负责把命令原样送过去，由它自己答。
            # 后面带的话（/new 标题、/resume 编号）一起送，别在半路吃掉。
            # 全角的「／」在这里统一成半角，客户端只认 /new 这种写法。
            current = cfg.agent(sticky) or cfg.fallback_agent()
            if not current:
                return Decision("reply", text="没有可用的 agent。", note="no-agent")
            kind = "new" if name in ("new", "reset") else name
            return Decision(kind, agent=current.name, text="/" + raw[1:],
                            note=f"session-{name}")

        if name in ("approve", "yes", "ok", "reject", "no"):
            # Answering an agent's "may I?" — the gateway owns the pending list,
            # so the decision is made there; the id is optional (newest wins).
            return Decision("approve" if name in ("approve", "yes", "ok") else "reject",
                            text=rest.strip(), note=f"approval:{name}")

        if name in ("queue", "q"):
            payload = rest.strip()
            current = cfg.agent(sticky) or cfg.fallback_agent()
            if not payload:
                return Decision("reply",
                                text=f"这个会话的繁忙模式是「{cfg.delivery.busy_mode}」。"
                                     f"要把内容排到正在跑的活后面（不打断它），就写在后面："
                                     f"/queue 帮我看下 X",
                                note="queue-usage")
            if not current:
                return Decision("reply", text="没有可用的 agent。", note="no-agent")
            return Decision("dispatch", agent=current.name, text=payload, note="queue",
                            metadata={"busy": "queue"})

        if name == "stop":
            # "别做了" —— 网关停不了别人的进程，只能留个记号让正在等的 agent
            # 自己去收（见 virtual_ilink.request_stop / 客户端每两秒的问询）。
            return Decision("stop", text=rest.strip(), note="stop")

        if name == "always":
            # "这一轮别再问我了" —— 一次把本轮剩下的问题全放行。
            # 它是**明确说的**（不是沉默），但仍然有期限，见 approvals.allow_all。
            return Decision("always", note="approval:always")

        # not a command -> maybe an agent prefix ("/c fix the test")
        agent = cfg.agent_for_prefix(name)
        if agent and agent.enabled:
            payload = rest.strip()
            if not payload:
                return Decision("reply", text=f"给「{agent.display}」发点什么？例如：/{agent.prefix} 看一下 xxx",
                                note="prefix-empty")
            return Decision("dispatch", agent=agent.name, text=payload, note=f"prefix:{agent.prefix}")

        return Decision("reply", text=f"不认识这个命令：/{name}（/help 看用法）", note="unknown-command")

    if is_keepalive(raw):
        return Decision("keepalive", note="keepalive")

    agent = cfg.agent(sticky) or cfg.fallback_agent()
    if not agent:
        return Decision("reply", text="没有可用的 agent。", note="no-agent")
    return Decision("dispatch", agent=agent.name, text=raw, note="default")

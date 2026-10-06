"""Routing rules: one WeChat chat, N agents.

Pure functions only — the gateway calls :func:`route` and acts on the result,
which makes every rule here unit-testable without WeChat or network.

Grammar (agents configure their own prefix, e.g. ``h`` / ``c`` / ``w``):

    /c fix the flaky test        -> one-shot: send to the agent whose prefix is c
    /use claude                  -> sticky: this chat talks to claude from now on
    /approve k7  /reject k7      -> answer an agent's "may I?" (see approvals.py)
    /always                      -> allow every question left in this round
    /who                         -> which agent is this chat talking to
    /agents                      -> the roster
    /help                        -> this text
    anything else                -> the chat's sticky agent (or the default)

Command names win over agent prefixes, so an agent cannot hijack ``/help``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

COMMANDS = ("help", "agents", "who", "use", "ping", "approve", "reject", "always", "stop")


@dataclass
class Decision:
    """What the gateway should do with one inbound message."""

    kind: str                     # "reply" | "dispatch" | "ignore"
    agent: str = ""               # dispatch target (agent name)
    text: str = ""                # payload for the agent, or the reply body
    note: str = ""                # free-form, for logs
    set_sticky: Optional[str] = None  # gateway persists this as the chat's agent
    metadata: dict = field(default_factory=dict)


def _norm(text: str) -> str:
    return (text or "").strip()


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
        "/approve <编号> / /reject <编号> → 放行或拒绝 agent 等你点头的那件事",
        "/always → 这一轮剩下的都别再问，全部放行",
        "/stop → 让正在跑的 agent 停下来（它几秒内会收到）",
        "/agents 看名单　/who 看当前　/help 看这条",
        "",
        "名单：",
        *roster_lines(cfg),
    ]
    return "\n".join(lines)


def route(text: str, cfg, *, sticky: Optional[str] = None) -> Decision:
    """Decide what to do with one inbound message."""
    raw = _norm(text)
    if not raw:
        return Decision("ignore", note="empty")

    if raw.startswith(("/", "／", "!")):
        head, _, rest = raw[1:].partition(" ")
        name = head.strip().lower()

        if name in ("help", "?"):
            return Decision("reply", text=help_text(cfg, sticky), note="help")
        if name == "agents":
            return Decision("reply", text="名单：\n" + "\n".join(roster_lines(cfg)), note="agents")
        if name == "ping":
            return Decision("reply", text="网关在。", note="ping")
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

        if name in ("approve", "yes", "ok", "reject", "no"):
            # Answering an agent's "may I?" — the gateway owns the pending list,
            # so the decision is made there; the id is optional (newest wins).
            return Decision("approve" if name in ("approve", "yes", "ok") else "reject",
                            text=rest.strip(), note=f"approval:{name}")

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

    agent = cfg.agent(sticky) or cfg.fallback_agent()
    if not agent:
        return Decision("reply", text="没有可用的 agent。", note="no-agent")
    return Decision("dispatch", agent=agent.name, text=raw, note="default")

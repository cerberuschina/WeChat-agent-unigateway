"""Configuration for agent-gateway.

One JSON file describes everything: the WeChat (iLink) account the gateway owns,
the agents behind it, and the routing rules. No third-party dependencies.

See ``gateway.example.json`` for the full schema.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


class ConfigError(RuntimeError):
    """Raised when the config file is missing or unusable."""


def read_token_source(source: str) -> str:
    """Resolve a token *source* to the token itself.

    ``file:path`` reads a (trimmed) file, ``env:VAR`` reads the environment, and
    anything else is the literal token. Sources exist so a shared secret lives in
    a file with 0600 rather than inside ``gateway.json`` — ``join.py`` writes
    ``file:data/<agent>-token.txt`` when it hands an agent a token we minted.
    """
    src = (source or "").strip()
    if not src:
        return ""
    if src.startswith("file:"):
        try:
            return Path(src[len("file:"):].strip()).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    if src.startswith("env:"):
        return os.environ.get(src[len("env:"):].strip(), "").strip()
    return src


_INSTANCE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def split_identity(key: str) -> Tuple[str, str]:
    """``hermes@mac-air`` -> ``("hermes", "mac-air")``; bare ``hermes`` -> ``("hermes", "")``."""
    agent, _, instance = (key or "").strip().partition("@")
    return agent.strip().lower(), instance.strip()


def _resolve_token_source(source: str, base: Path) -> str:
    """把 ``file:`` 的相对路径拧成相对**配置文件**的绝对路径。

    ``data/foo-token.txt`` 这种写法在网关以别的 cwd 启动时会读到别的文件（或读不到），
    accept_tokens 里的路径按配置文件所在目录解析，和 ``data_dir`` 一个规矩。
    """
    src = (source or "").strip()
    if not src.startswith("file:"):
        return src
    raw = Path(src[len("file:"):].strip()).expanduser()
    if not raw.is_absolute():
        raw = base / raw
    return f"file:{raw}"


@dataclass
class AccountConfig:
    """The single iLink bot identity this gateway owns (one WeChat, one bot)."""

    account_id: str = ""
    token: str = ""
    base_url: str = DEFAULT_BASE_URL

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self.token)


@dataclass
class WakeConfig:
    """How to bring a local agent's client back when a message finds it offline.

    The gateway cannot reach into another machine, so this only applies to
    agents that run *here* (the sub-gateway case): ``command`` is started
    detached, then the gateway waits up to ``timeout`` seconds for the agent's
    long-poll to show up again. ``cooldown`` keeps a dead command from being
    re-launched on every message.
    """

    command: str = ""
    cwd: str = ""
    timeout: float = 60.0
    cooldown: float = 120.0

    @property
    def enabled(self) -> bool:
        return bool(self.command.strip())


@dataclass
class AgentConfig:
    """One backend agent.

    ``type`` picks the adapter:
      - ``a2a``  — A2A JSON-RPC endpoint (any agent speaking the A2A protocol)
      - ``http`` — plain HTTP JSON endpoint
      - ``exec`` — a local command; the reply is its stdout
      - ``gateway`` — another agent-gateway's ``/federation/send`` entry
    """

    name: str
    type: str = "a2a"
    label: str = ""
    prefix: str = ""
    url: str = ""
    token: str = ""
    remote_agent: str = ""
    method: str = "POST"
    headers: Dict[str, str] = field(default_factory=dict)
    body_template: Optional[Dict[str, Any]] = None
    reply_path: str = ""
    command: List[str] = field(default_factory=list)
    cwd: str = ""
    timeout: float = 900.0
    enabled: bool = True
    wake: WakeConfig = field(default_factory=WakeConfig)

    @property
    def display(self) -> str:
        return self.label or self.name


@dataclass
class DeliveryConfig:
    max_chars_per_message: int = 1800
    # WeChat lets a bot send ~10 messages before the user replies again; spend the
    # budget on the answer, never on chatter.
    max_messages_per_turn: int = 10
    reserve_for_answer: int = 2
    ack: bool = True
    ack_template: str = "已转给 {label}，算完就回。"
    # Sent instead of the ack when the gateway knows the device's agent is not
    # listening (a sub-gateway reports its local agent is down). The device is
    # being woken, so this is not an error — it is the truth about the wait.
    offline_template: str = "「{label}」当前不在线，正在尝试唤醒并发送…"
    # ...and when that device cannot wake the agent by itself: the message is
    # kept until the agent comes back, but no wake is being attempted.
    offline_plain_template: str = "「{label}」当前不在线，消息已排队，等它上线后送出。"
    error_template: str = "「{label}」这次没跑通：{error}"
    queue_template: str = "队列里还有 {n} 条，按顺序回。"
    # A "保活" ping is answered by the gateway itself — no agent, no model round
    # trip. ``{n}`` is what stays in this turn's budget *after* this reply.
    keepalive_template: str = "已重置ilink限制数，除掉本条消息，当前剩余{n}条信息余量"
    # How long an agent's "may I?" stays open on the phone. Silence expires to
    # *deny* — an unanswered question must never turn into permission. 180s proved
    # too short in practice: cards died while the phone was in a pocket, the agent
    # read that as "denied" and asked again, so every task cost two round trips.
    approval_ttl_seconds: int = 900
    # How long a `/always` ("本轮全放行") keeps auto-approving. It is a ceiling,
    # not the usual ending: the user's next message clears it sooner (gateway
    # calls ``ApprovalBroker.clear_auto``). Both bounds exist because approval is
    # the permissive direction — an unbounded "yes" eventually fires unattended.
    always_window_seconds: float = 1800.0
    # What to do with a message that arrives while this chat's agent is still
    # working (Hermes calls the same knob busy_input_mode):
    #   "queue"     — hand it over; the agent gets to it when it finishes
    #   "interrupt" — stop the running task first, then hand this one over
    #   "steer"     — hand it over *now*, into the running session: the agent picks
    #                 it up the moment its current step ends (needs the client to
    #                 run with --session-command, otherwise it behaves like queue)
    busy_mode: str = "queue"


@dataclass
class AccessConfig:
    # Empty list = accept every DM (single-owner machines are the common case).
    allowed_users: List[str] = field(default_factory=list)

    def allows(self, user_id: str) -> bool:
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users


@dataclass
class VirtualConfig:
    """The virtual iLink server: agents connect to *us* instead of Tencent.

    ``enabled`` turns the local fake WeChat API on. ``auto_approve`` lists agent
    names that may bind without an operator clicking approve (handy in tests,
    and for an unattended machine).
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 18500
    auto_approve: List[str] = field(default_factory=list)
    # An agent that is *already* bound to the real WeChat (Hermes' weixin
    # channel is one) can keep its token: name it here and the virtual server
    # accepts that existing token as an alias for a virtual identity. This is
    # needed because some clients hard-wire their QR-login URL to Tencent, so
    # they cannot re-bind through us — only reuse the binding they have.
    reuse_real_token_for: str = ""
    # ...and where that agent's existing token comes from. Leave both empty to
    # fall back to the gateway's own binding (only correct when the agent shares
    # the gateway's bot identity).
    reuse_token_env: str = ""
    reuse_token_file: str = ""
    # The general form of the two knobs above: one entry per agent, e.g.
    # ``{"workbuddy": "file:data/workbuddy-token.txt"}``. ``join.py`` writes these
    # when it hands an agent a token *we* minted — the case of a client that can
    # neither re-point its QR login at us (WorkBuddy's QR path is hard-wired to
    # Tencent) nor hand over a real token. Without this the legacy slot above
    # would be the only way in, i.e. exactly one such agent per gateway.
    # A key may name a *device instance* of an agent: ``hermes@mac-air``,
    # ``claude@win59``. One agent may appear several times, each with its own
    # token file, so several machines can run the same kind of agent at once.
    # A key without ``@`` stays the single unnamed instance (the old meaning).
    accept_tokens: Dict[str, str] = field(default_factory=dict)
    # --- remote (non-local) agents ------------------------------------------
    # A pre-shared key a remote agent must present to get a QR at all. Empty =
    # only the auto_approve list may bind (in practice: the local machine).
    bind_key: str = ""
    # Where remote agents can reach this gateway (e.g. http://192.168.1.5:18500).
    # Used for the media download links handed to agents. No TLS here on purpose:
    # for anything beyond a trusted LAN, put an SSH tunnel or a reverse proxy with
    # a real certificate in front (see docs/REMOTE-AGENTS.md).
    public_url: str = ""
    # Public exposure: who may talk to the agent API at all (empty = 不限制来源，
    # 仍要 bind_key + token + 人工批准), and who may approve/list binds.
    allow_cidrs: List[str] = field(default_factory=list)
    admin_cidrs: List[str] = field(default_factory=lambda: ["127.0.0.1/32", "::1/128"])

    def approves(self, name: str) -> bool:
        return (name or "").lower() in [n.lower() for n in self.auto_approve]

    def reuse_tokens(self, own_token: str = "") -> Dict[str, str]:
        """token -> agent (or ``agent@instance``): the tokens the virtual server accepts.

        Two ways in, merged into one map: ``accept_tokens`` (per-agent sources,
        what ``join.py`` writes) and the legacy ``reuse_real_token_for`` slot
        (an agent whose *own* real token must be honoured, with a fallback to the
        gateway's own binding when neither env nor file gives one).
        """
        tokens: Dict[str, str] = {}
        if self.reuse_real_token_for:
            token = os.environ.get(self.reuse_token_env, "").strip() if self.reuse_token_env else ""
            if not token and self.reuse_token_file:
                try:
                    token = Path(self.reuse_token_file).expanduser().read_text(encoding="utf-8").strip()
                except OSError:
                    token = ""
            if not token:
                token = (own_token or "").strip()
            if token:
                tokens[token] = self.reuse_real_token_for
        for name, source in (self.accept_tokens or {}).items():
            token = read_token_source(source)
            if token:
                tokens[token] = name
        return tokens


@dataclass
class DashboardConfig:
    """本机网页控制台：看状态、批准接入。

    Still stdlib-only and loopback by default: it is an operator tool, not another
    exposed service.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 18600


@dataclass
class MemoryConfig:
    """统一记忆（docs/MEMORY-DESIGN.md）。

    P1 只实现内置后端：SQLite + FTS5 + Markdown 镜像。``rag`` 与
    ``extractor`` 的配置在这里保留形状，P2 才真正使用。
    """

    enabled: bool = False
    backend: str = "builtin"
    data_dir: str = ""             # 默认 <data_dir>/memory
    salt: str = ""                 # 空 = data_dir/memory/salt.txt（首次生成 0600）
    l0_ttl_days: float = 30.0
    inject_enabled: bool = True
    inject_max_chars: int = 800
    inject_recent_turns: int = 4
    inject_facts: int = 6
    global_writers: List[str] = field(default_factory=list)   # 允许写全局层的设备（agent@device）
    redact_patterns: List[str] = field(default_factory=list)
    rag_enabled: bool = False
    extractor_url: str = ""


@dataclass
class UpstreamConfig:
    """子网关模式：本机网关作为虚拟 iLink 客户端连到主网关。

    本机的 agent（Hermes / Claude …）仍然接在**本机**网关上；这里再给每个
    agent 一个主网关签发的 token，由本机网关替它们长轮询主网关、转发消息、
    把 agent 的回答送回去。主网关那边看到的就是普通的设备实例，多设备粘住和
    故障转移照常生效。
    """

    enabled: bool = False
    device: str = ""
    base_url: str = ""
    agents: Dict[str, str] = field(default_factory=dict)   # 本地 agent 名 -> 主网关 token
    timeout: float = 900.0
    idle: float = 120.0   # agent 安静多久算这一轮结束（之后补发的消息会被丢弃）


@dataclass
class Config:
    account: AccountConfig
    agents: Dict[str, AgentConfig]
    default_agent: str = ""
    data_dir: Path = Path("data")
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    access: AccessConfig = field(default_factory=AccessConfig)
    virtual: VirtualConfig = field(default_factory=VirtualConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    source: Optional[Path] = None

    # -- lookups ---------------------------------------------------------
    def agent(self, name: str) -> Optional[AgentConfig]:
        return self.agents.get((name or "").strip().lower())

    def enabled_agents(self) -> List[AgentConfig]:
        return [a for a in self.agents.values() if a.enabled]

    def agent_for_prefix(self, prefix: str) -> Optional[AgentConfig]:
        wanted = (prefix or "").strip().lower().lstrip("/")
        for agent in self.agents.values():
            if agent.prefix and agent.prefix.lower() == wanted:
                return agent
        return None

    def fallback_agent(self) -> Optional[AgentConfig]:
        if self.default_agent:
            agent = self.agent(self.default_agent)
            if agent and agent.enabled:
                return agent
        for agent in self.agents.values():
            if agent.enabled:
                return agent
        return None


def _agent_from_dict(name: str, raw: Dict[str, Any]) -> AgentConfig:
    raw = dict(raw)
    wake_raw = raw.pop("wake", None)
    if wake_raw is not None and not isinstance(wake_raw, dict):
        raise ConfigError(f"agent '{name}': wake must be an object")
    known = {f for f in AgentConfig.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"agent '{name}': unknown key(s) {sorted(unknown)}")
    if wake_raw:
        unknown_wake = set(wake_raw) - {f for f in WakeConfig.__dataclass_fields__}
        if unknown_wake:
            raise ConfigError(f"agent '{name}': unknown wake key(s) {sorted(unknown_wake)}")
        raw["wake"] = WakeConfig(command=str(wake_raw.get("command") or ""),
                                 cwd=str(wake_raw.get("cwd") or ""),
                                 timeout=float(wake_raw.get("timeout") or 60),
                                 cooldown=float(wake_raw.get("cooldown")
                                                if wake_raw.get("cooldown") is not None else 120))
    agent = AgentConfig(name=name.lower(), **raw)
    if agent.enabled and not agent.label:
        agent.label = name
    if agent.enabled and agent.type not in {"a2a", "http", "exec", "virtual", "gateway"}:
        raise ConfigError(
            f"agent '{name}': type must be a2a|http|exec|virtual|gateway, got {agent.type!r}")
    if agent.enabled and agent.type in {"a2a", "http", "gateway"} and not agent.url:
        raise ConfigError(f"agent '{name}': type '{agent.type}' needs a url")
    if agent.enabled and agent.type == "gateway" and not agent.token:
        raise ConfigError(f"agent '{name}': type 'gateway' needs the sub-gateway's token")
    if agent.enabled and agent.type == "exec" and not agent.command:
        raise ConfigError(f"agent '{name}': type 'exec' needs a command list")
    return agent


def load_config(path: str | os.PathLike[str]) -> Config:
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(
            f"config not found: {path}\n"
            f"copy gateway.example.json to {path.name} and fill it in"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config is not valid JSON: {exc}") from exc

    root = path.parent

    def resolve(p: str) -> Path:
        q = Path(p).expanduser()
        return q if q.is_absolute() else (root / q)

    account_raw = dict(raw.get("account") or {})
    account = AccountConfig(
        account_id=str(account_raw.get("account_id") or ""),
        token=str(account_raw.get("token") or ""),
        base_url=str(account_raw.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
    )

    agents_raw = raw.get("agents") or {}
    if not isinstance(agents_raw, dict) or not agents_raw:
        raise ConfigError("config needs a non-empty 'agents' object")
    agents = {name.lower(): _agent_from_dict(name, dict(cfg or {}))
              for name, cfg in agents_raw.items()}

    delivery = DeliveryConfig(**(raw.get("delivery") or {}))
    if delivery.busy_mode not in ("queue", "interrupt", "steer"):
        raise ConfigError(
            f"delivery.busy_mode 只能是 'queue' / 'interrupt' / 'steer'，收到 {delivery.busy_mode!r}")
    access = AccessConfig(allowed_users=[str(u) for u in (raw.get("access") or {}).get("allowed_users") or []])

    virtual_raw = dict(raw.get("virtual") or {})
    virtual = VirtualConfig(
        enabled=bool(virtual_raw.get("enabled", False)),
        host=str(virtual_raw.get("host") or "127.0.0.1"),
        # ``or`` would treat 0 (ephemeral port, used by tests) as "unset".
        port=int(virtual_raw["port"]) if "port" in virtual_raw else 18500,
        auto_approve=[str(n) for n in (virtual_raw.get("auto_approve") or [])],
        reuse_real_token_for=str(virtual_raw.get("reuse_real_token_for") or "").lower(),
        reuse_token_env=str(virtual_raw.get("reuse_token_env") or ""),
        reuse_token_file=str(virtual_raw.get("reuse_token_file") or ""),
        accept_tokens={str(name).lower(): _resolve_token_source(str(source), root)
                       for name, source in (virtual_raw.get("accept_tokens") or {}).items()},
        bind_key=str(virtual_raw.get("bind_key") or ""),
        public_url=str(virtual_raw.get("public_url") or "").rstrip("/"),
        allow_cidrs=[str(c) for c in (virtual_raw.get("allow_cidrs") or [])],
        admin_cidrs=[str(c) for c in (virtual_raw.get("admin_cidrs") or [])],
    )
    if virtual.reuse_real_token_for and virtual.reuse_real_token_for not in agents:
        raise ConfigError(
            f"virtual.reuse_real_token_for='{virtual.reuse_real_token_for}' is not a configured agent")
    for token_key in virtual.accept_tokens:
        token_agent, _, device = token_key.partition("@")
        if token_agent not in agents:
            raise ConfigError(
                f"virtual.accept_tokens['{token_key}'] is not a configured agent"
                + (f" (agent '{token_agent}')" if device else ""))
        if token_key.endswith("@") or (device and not _INSTANCE_RE.match(device)):
            raise ConfigError(
                f"virtual.accept_tokens['{token_key}']: instance must be "
                "letters, digits, '.', '_' or '-' after '@'")

    dashboard_raw = dict(raw.get("dashboard") or {})
    dashboard = DashboardConfig(
        enabled=bool(dashboard_raw.get("enabled", True)),
        host=str(dashboard_raw.get("host") or "127.0.0.1"),
        port=int(dashboard_raw["port"]) if "port" in dashboard_raw else 18600,
    )

    memory_raw = dict(raw.get("memory") or {})
    inject_raw = dict(memory_raw.get("inject") or {})
    rag_raw = dict(memory_raw.get("rag") or {})
    extractor_raw = dict(memory_raw.get("extractor") or {})
    memory = MemoryConfig(
        enabled=bool(memory_raw.get("enabled", False)),
        backend=str(memory_raw.get("backend") or "builtin").lower(),
        data_dir=str(memory_raw.get("data_dir") or ""),
        salt=_resolve_token_source(str(memory_raw.get("salt") or ""), root)
        if str(memory_raw.get("salt") or "").startswith("file:") else str(memory_raw.get("salt") or ""),
        l0_ttl_days=float(memory_raw.get("l0_ttl_days", 30) or 0),
        inject_enabled=bool(inject_raw.get("enabled", True)),
        inject_max_chars=int(inject_raw.get("max_chars", 800)),
        inject_recent_turns=int(inject_raw.get("recent_turns", 4)),
        inject_facts=int(inject_raw.get("facts", 6)),
        global_writers=[str(x).lower() for x in (memory_raw.get("global_writers") or [])],
        redact_patterns=[str(x) for x in (memory_raw.get("redact_patterns") or [])],
        rag_enabled=bool(rag_raw.get("enabled", False)),
        extractor_url=str(extractor_raw.get("url") or ""),
    )
    if memory.backend not in ("builtin",):
        raise ConfigError(
            f"memory.backend='{memory.backend}' 还没实现；P1 只有 'builtin'"
            f"（TencentDB-Agent-Memory 后端在 docs/MEMORY-DESIGN.md 的 P3）")
    if memory.rag_enabled:
        raise ConfigError("memory.rag.enabled 是 P2 的能力；现在请保持 false")

    up_raw = dict(raw.get("upstream") or {})
    upstream = UpstreamConfig(
        enabled=bool(up_raw.get("enabled")),
        device=str(up_raw.get("device") or ""),
        base_url=str(up_raw.get("base_url") or "").rstrip("/"),
        agents={str(k).strip().lower(): str(v).strip()
                for k, v in (up_raw.get("agents") or {}).items()},
        timeout=float(up_raw.get("timeout") or 900),
        idle=float(up_raw.get("idle") or 120),
    )
    if upstream.enabled:
        if not upstream.base_url:
            raise ConfigError("upstream.enabled 需要 upstream.base_url（主网关的虚拟 iLink 地址）")
        if not upstream.agents:
            raise ConfigError("upstream.enabled 需要 upstream.agents（本地 agent → 主网关发的 token）")
        missing = [a for a in upstream.agents if a not in agents]
        if missing:
            raise ConfigError(f"upstream.agents 里的 {', '.join(missing)} 不在 agents 里")

    cfg = Config(
        account=account,
        agents=agents,
        default_agent=str(raw.get("default_agent") or "").lower(),
        data_dir=resolve(str(raw.get("data_dir") or "data")),
        delivery=delivery,
        access=access,
        virtual=virtual,
        dashboard=dashboard,
        memory=memory,
        upstream=upstream,
        source=path,
    )
    if cfg.default_agent and cfg.default_agent not in cfg.agents:
        raise ConfigError(f"default_agent '{cfg.default_agent}' is not in agents")
    if not cfg.fallback_agent():
        raise ConfigError("no enabled agent to route to")
    virtual_agents = [a.name for a in agents.values() if a.enabled and a.type == "virtual"]
    if virtual_agents and not virtual.enabled:
        raise ConfigError(
            "agents " + ", ".join(virtual_agents) + " are type 'virtual' but "
            "'virtual.enabled' is false — either enable it or change those agents' type")
    return cfg


def save_account(path: str | os.PathLike[str], *, account_id: str, token: str,
                 base_url: str, user_id: str = "") -> Path:
    """Persist iLink credentials next to the config (mode 600)."""
    import time

    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "account_id": account_id,
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def load_account(path: str | os.PathLike[str]) -> Optional[Dict[str, Any]]:
    path = Path(path).expanduser()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None

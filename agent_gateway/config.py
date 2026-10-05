"""Configuration for agent-gateway.

One JSON file describes everything: the WeChat (iLink) account the gateway owns,
the agents behind it, and the routing rules. No third-party dependencies.

See ``gateway.example.json`` for the full schema.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


class ConfigError(RuntimeError):
    """Raised when the config file is missing or unusable."""


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
class AgentConfig:
    """One backend agent.

    ``type`` picks the adapter:
      - ``a2a``  — A2A JSON-RPC endpoint (any agent speaking the A2A protocol)
      - ``http`` — plain HTTP JSON endpoint
      - ``exec`` — a local command; the reply is its stdout
    """

    name: str
    type: str = "a2a"
    label: str = ""
    prefix: str = ""
    url: str = ""
    token: str = ""
    method: str = "POST"
    headers: Dict[str, str] = field(default_factory=dict)
    body_template: Optional[Dict[str, Any]] = None
    reply_path: str = ""
    command: List[str] = field(default_factory=list)
    cwd: str = ""
    timeout: float = 900.0
    enabled: bool = True

    @property
    def display(self) -> str:
        return self.label or self.name


@dataclass
class DeliveryConfig:
    max_chars_per_message: int = 1200
    ack: bool = True
    ack_template: str = "已转给 {label}，算完就回。"
    error_template: str = "「{label}」这次没跑通：{error}"
    queue_template: str = "队列里还有 {n} 条，按顺序回。"


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

    def approves(self, name: str) -> bool:
        return (name or "").lower() in [n.lower() for n in self.auto_approve]


@dataclass
class Config:
    account: AccountConfig
    agents: Dict[str, AgentConfig]
    default_agent: str = ""
    data_dir: Path = Path("data")
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    access: AccessConfig = field(default_factory=AccessConfig)
    virtual: VirtualConfig = field(default_factory=VirtualConfig)
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
    known = {f for f in AgentConfig.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"agent '{name}': unknown key(s) {sorted(unknown)}")
    agent = AgentConfig(name=name.lower(), **raw)
    if agent.enabled and not agent.label:
        agent.label = name
    if agent.enabled and agent.type not in {"a2a", "http", "exec", "virtual"}:
        raise ConfigError(
            f"agent '{name}': type must be a2a|http|exec|virtual, got {agent.type!r}")
    if agent.enabled and agent.type in {"a2a", "http"} and not agent.url:
        raise ConfigError(f"agent '{name}': type '{agent.type}' needs a url")
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
    access = AccessConfig(allowed_users=[str(u) for u in (raw.get("access") or {}).get("allowed_users") or []])

    virtual_raw = dict(raw.get("virtual") or {})
    virtual = VirtualConfig(
        enabled=bool(virtual_raw.get("enabled", False)),
        host=str(virtual_raw.get("host") or "127.0.0.1"),
        port=int(virtual_raw.get("port") or 18500),
        auto_approve=[str(n) for n in (virtual_raw.get("auto_approve") or [])],
        reuse_real_token_for=str(virtual_raw.get("reuse_real_token_for") or "").lower(),
    )
    if virtual.reuse_real_token_for and virtual.reuse_real_token_for not in agents:
        raise ConfigError(
            f"virtual.reuse_real_token_for='{virtual.reuse_real_token_for}' is not a configured agent")

    cfg = Config(
        account=account,
        agents=agents,
        default_agent=str(raw.get("default_agent") or "").lower(),
        data_dir=resolve(str(raw.get("data_dir") or "data")),
        delivery=delivery,
        access=access,
        virtual=virtual,
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

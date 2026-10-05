"""agent-gateway — one WeChat entry (iLink / ClawBot), many local agents.

    from agent_gateway import load_config, Gateway

    cfg = load_config("gateway.json")
    Gateway(cfg).run()
"""
from .config import (  # noqa: F401
    AccessConfig,
    AccountConfig,
    AgentConfig,
    Config,
    ConfigError,
    DeliveryConfig,
    load_account,
    load_config,
    save_account,
)
from .gateway import Gateway  # noqa: F401
from .router import Decision, route  # noqa: F401

__version__ = "0.1.0"
__all__ = [
    "AccessConfig",
    "AccountConfig",
    "AgentConfig",
    "Config",
    "ConfigError",
    "Decision",
    "DeliveryConfig",
    "Gateway",
    "load_account",
    "load_config",
    "route",
    "save_account",
]

"""``python -m agent_gateway`` — same entry point as the ``agent-gateway`` CLI.

Kept as a one-liner on purpose: the README, run.cmd and run.sh all document
``python -m agent_gateway --config gateway.json``, so this file existing (and
being tested) is what makes that command true.
"""
from .gateway import main

if __name__ == "__main__":
    raise SystemExit(main())

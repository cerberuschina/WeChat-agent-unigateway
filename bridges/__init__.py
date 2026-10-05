"""Optional bridges that let the gateway drive apps that have no agent API.

Nothing in here is imported by the gateway core — the gateway just runs these
as ``exec`` backends, so a missing optional dependency never breaks the gateway.
"""

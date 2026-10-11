# 0005. VPN behind an interface

## Status
Accepted.

## Context
The mesh is Tailscale today, but the plan is to migrate to WireGuard on the same
EC2 host to offload Tailscale. Routes and templates should not care which.

## Decision
`app/services/vpn_service.py` exposes `get_status()` and dispatches on the
`VPN_BACKEND` setting to Tailscale or WireGuard implementations. Both return the
same shape, including a heartbeat notion (peer online, or recent handshake).

## Consequences
- Switching backends is a config change, not a code change.
- The dashboard badge and `/vpn/heartbeat` are backend-agnostic.

# VPN and Network

EdgeShell reaches nodes over a private mesh that lives on the host. The app only
reads mesh status for a heartbeat badge; it does not route user traffic through
the mesh.

## Backends

`VPN_BACKEND` selects the implementation. The UI and heartbeat are identical
either way.

| Backend | Status source | Heartbeat definition |
|---|---|---|
| tailscale (default) | `tailscale status --json` | peer `Online` flag |
| wireguard | `wg show <iface> dump` | latest handshake younger than 180s |

Switch by setting `VPN_BACKEND=wireguard` and `WIREGUARD_INTERFACE=wg0`. No code
change; `app/services/vpn_service.py` dispatches.

## Migrating Tailscale to WireGuard on the same host

1. Stand up WireGuard on the EC2 host (`wg0`), peer it with the nodes.
2. Confirm `wg show wg0 dump` lists peers with recent handshakes.
3. Set `VPN_BACKEND=wireguard` in `.env`, restart the service.
4. The dashboard badge and `/vpn/heartbeat` now read WireGuard. Tailscale can be
   retired once nodes are reachable over `wg0`.

## Containerized networking

When EdgeShell runs in Docker, it needs a path to the nodes.

### Option A (recommended): mesh on the host

Run Tailscale or WireGuard on the host, and let the container use the host
network so node IPs resolve directly:

```yaml
services:
  edgeshell:
    network_mode: host      # remove the ports: mapping when using this
```

Simple, and the mesh stays out of the container.

### Option B: WireGuard sidecar in the container

Give the container its own mesh membership:

```yaml
services:
  edgeshell:
    cap_add: [NET_ADMIN]
    devices: ["/dev/net/tun"]
    volumes:
      - ./wg0.conf:/etc/wireguard/wg0.conf:ro
    environment:
      VPN_BACKEND: wireguard
```

Bring up `wg-quick up wg0` in an entrypoint or a sidecar. Use this when the host
cannot own the mesh.

## Ports

| Service | Default | Where |
|---|---|---|
| EdgeShell | 5000 | control plane |
| Node-RED | 1880 | each node (configurable per node) |
| OpenRemoteGUI | 6080 | each node (configurable per node) |
| SSH | 22 | each node (configurable per node) |

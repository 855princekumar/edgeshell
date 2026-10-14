# VPN and Network

EdgeShell reaches nodes over a private mesh that lives on the host. The app
reads mesh status (the VPN page shows the server and every peer live: online,
OS, direct or relayed link, last seen) but does not route user traffic through
the mesh. A peer being online on the VPN page means the network path works; it
does not mean SSH logins are allowed (see
[Tailscale SSH on the nodes](#tailscale-ssh-on-the-nodes)).

## Backends

`VPN_BACKEND` selects the implementation. The UI and heartbeat are identical
either way.

| Backend | Status source | Heartbeat definition |
|---|---|---|
| tailscale (default) | `tailscale status --json` | peer `Online` flag |
| wireguard | `wg show <iface> dump` | latest handshake younger than 180s |

Switch by setting `VPN_BACKEND=wireguard` and `WIREGUARD_INTERFACE=wg0`. No code
change; `app/services/vpn_service.py` dispatches.

## Tailscale SSH on the nodes

If the nodes run **Tailscale SSH** (turned on with `tailscale set --ssh` or
`tailscale up --ssh`), Tailscale itself answers port 22 on each node's tailnet
address. It ignores the username's password and decides every login from the
`"ssh"` section of your tailnet policy. The rest of the mesh is not involved:
ping, Node-RED (1880), OpenRemoteGUI (6080) and the VPN page keep working
whatever that section says. So "the VPN is fine but every node shows login
failed" almost always points here.

### How to recognise it

| Where | What you see |
|---|---|
| Node card or Monitoring | "Tailscale SSH on this node asks for a browser check, so the password is not used"; Node Fleet lists the node under **Automatic logins paused** with a "Tailscale check" badge |
| From the EdgeShell host | `ssh <node user>@<node tailnet IP>` hangs, or prints `# Tailscale SSH requires an additional check` with a login.tailscale.com link |
| Banner of port 22 | `SSH-2.0-Tailscale` (a node's own SSH server says `SSH-2.0-OpenSSH_...`) |

EdgeShell pauses these nodes after the first refusal instead of retrying (see
the [login policy](ARCHITECTURE.md#node-login-policy)) and resumes them by
itself when the policy is fixed.

### Why it happens

Tailscale's default policy has one SSH rule:

```jsonc
{
	"action": "check",
	"src":    ["autogroup:member"],
	"dst":    ["autogroup:self"],
	"users":  ["autogroup:nonroot", "root"],
}
```

`check` holds every login until a person approves it in a browser, and the
approval lasts for the check period (12 hours by default). A server such as
EdgeShell can never approve it, so its logins wait and time out. Fleets often
seem to work for a while and then fail all at once: someone approved a check
from the server earlier, and it lapsed.

**A `check` rule overrides an `accept` rule that matches the same login,
whatever their order.** Adding an `accept` rule above the default one is not
enough; the rules must not overlap for EdgeShell's logins.

### Fix A: an accept rule for the account that owns the nodes

Use this when the EdgeShell server and the nodes are registered under the same
user (check with `tailscale status`: the third column is the owner). Replace
the `"ssh"` section with three rules that never overlap:

```jsonc
"ssh": [
	// EdgeShell: the fleet owner's devices (the EdgeShell server among them) log in
	// to that owner's devices as a normal user without the browser check.
	{
		"action": "accept",
		"src":    ["<fleet owner>@<your domain>"],
		"dst":    ["autogroup:self"],
		"users":  ["autogroup:nonroot"],
	},
	// Root on anyone's own devices always needs the browser check.
	{
		"action": "check",
		"src":    ["autogroup:member"],
		"dst":    ["autogroup:self"],
		"users":  ["root"],
	},
	// Every other member keeps check mode as a normal user on their own devices.
	// List them here: a check rule that also matched the fleet owner would
	// override the accept rule above.
	{
		"action": "check",
		"src":    ["<other member>@<your domain>"],
		"dst":    ["autogroup:self"],
		"users":  ["autogroup:nonroot"],
	},
],
```

Leave out the third rule if the fleet owner is the only member. Add new members
to it when they join, or they cannot use Tailscale SSH on their own devices.
The accept rule also covers the fleet owner's other devices (a laptop, for
example); use fix B to limit it to the server.

### Fix B: tags (only the server gets in)

Tag the EdgeShell server (for example `tag:edgeshell`) and the nodes (for
example `tag:fleet`) in the admin console (Machines, Edit ACL tags), declare
both under `"tagOwners"`, and use:

```jsonc
{
	"action": "accept",
	"src":    ["tag:edgeshell"],
	"dst":    ["tag:fleet"],
	"users":  ["autogroup:nonroot"],
},
```

A tagged device can only Tailscale-SSH into tagged devices, which is why the
nodes need a tag too. Tagging also moves the server off a personal account,
whose key would otherwise expire with that person's session.

### Fix C: turn Tailscale SSH off on the nodes

`sudo tailscale set --ssh=false` on each node sends port 22 back to the node's
own SSH server, so EdgeShell's stored passwords are used again. The mesh and
everything else keep working.

### Make the change safely

1. Edit in the admin console's **Access controls, JSON editor**, or through the
   API (`GET`/`POST /api/v2/tailnet/-/acl`, with `/acl/validate` first and an
   `If-Match` header so a concurrent edit is never overwritten). Close any open
   console editor before saving through the API: a console save writes back its
   older copy of the whole file.
2. Tailscale validates the file on save; a mistake is refused, not applied.
3. Check what a device actually receives (from any device of the fleet owner):
   `tailscale debug netmap` and look at `SSHPolicy.rules` (`accept` or
   `holdAndDelegate`, which is check).
4. Test from the EdgeShell host: `ssh <node user>@<node tailnet IP> whoami`
   must print the user, and `ssh root@<node tailnet IP>` should still be held
   for a check.
5. In EdgeShell, nodes paused by Tailscale SSH resume within
   `LOGIN_RECHECK_MINUTES` (one canary login tests them), or at once with
   **Retry paused logins** on Node Fleet.

### sudo still uses the node's password

The accept rule only skips the password at login. `sudo` on the node works as
usual, and EdgeShell feeds the node's stored password to `sudo -S` over the
connection (never on a command line). A node whose stored password is wrong now
logs in but fails at sudo: correct it with **Fix login** or **Edit login**.

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

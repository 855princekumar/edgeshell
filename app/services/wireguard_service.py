"""WireGuard status as a heartbeat source.

Heartbeat definition: a peer is 'alive' if its latest handshake is recent
(< 180s). This mirrors Tailscale's online flag so the dashboard badge is
backend-agnostic.
"""
import subprocess
import time


def _run(args, timeout=10):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, '', 'wireguard-tools not installed'
    except Exception as e:
        return 1, '', str(e)


def get_status(interface='wg0'):
    rc, out, err = _run(['wg', 'show', interface, 'dump'])
    if rc != 0:
        return {'connected': False, 'backend': 'wireguard',
                'backend_state': 'down', 'error': err or 'interface down',
                'nodes': [], 'node_count': 0}

    lines = out.strip().splitlines()
    peers, now = [], int(time.time())
    for line in lines[1:]:  # first line is the interface itself
        f = line.split('\t')
        if len(f) < 5:
            continue
        endpoint = f[2]
        last_handshake = int(f[4]) if f[4].isdigit() else 0
        age = now - last_handshake if last_handshake else None
        peers.append({
            'name': f[0][:16] + '...',        # public key (truncated)
            'ip': (f[3].split(',')[0] if len(f) > 3 else ''),
            'endpoint': endpoint,
            'online': age is not None and age < 180,
            'handshake_age_s': age,
        })
    up = any(p['online'] for p in peers)
    return {'connected': up, 'backend': 'wireguard',
            'backend_state': 'Running' if up else 'Idle',
            'self_name': interface, 'error': None,
            'nodes': peers, 'node_count': len(peers)}

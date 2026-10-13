"""Live Tailscale status of the host EdgeShell runs on.

Read-only. Two ways in, tried in order:
  1. tailscaled's LocalAPI over its unix socket. In Docker the host's
     /var/run/tailscale is mounted into the container. tailscaled lets any
     local user read status but only root (or the configured operator)
     change anything, and EdgeShell runs as uid 10001, so the panel can
     never alter the VPN.
  2. The `tailscale` CLI (native installs).
"""
import http.client
import json
import os
import socket
import subprocess
from datetime import datetime, timezone

SOCKET = os.environ.get('TAILSCALE_SOCKET', '/var/run/tailscale/tailscaled.sock')
TIMEOUT_S = 5


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('local-tailscaled.sock', timeout=timeout)
        self._socket_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _status_from_socket():
    conn = _UnixHTTPConnection(SOCKET, TIMEOUT_S)
    try:
        conn.request('GET', '/localapi/v0/status',
                     headers={'Host': 'local-tailscaled.sock', 'Sec-Tailscale': 'localapi'})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f'tailscaled answered HTTP {resp.status}: {body[:160]!r}')
        return json.loads(body)
    finally:
        conn.close()


def _status_from_cli():
    result = subprocess.run(['tailscale', 'status', '--json'],
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or 'tailscale status failed').strip())
    return json.loads(result.stdout)


def _first(ips):
    ips = ips or []
    v4 = [ip for ip in ips if ':' not in ip]
    return (v4 or ips or [''])[0]


def _when(value):
    """Tailscale timestamps -> ISO 8601 UTC, or None for 'never'."""
    if not value or value.startswith('0001-'):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        return None


def _peer(key, p):
    return {
        'name': p.get('HostName') or key[:12],
        'dns_name': (p.get('DNSName') or '').rstrip('.'),
        'ip': _first(p.get('TailscaleIPs')),
        'ips': p.get('TailscaleIPs') or [],
        'os': p.get('OS', ''),
        'online': bool(p.get('Online')),
        'active': bool(p.get('Active')),
        'last_seen': _when(p.get('LastSeen')),
        'connection': ('direct' if p.get('CurAddr') else
                       (f"relay {p.get('Relay')}" if p.get('Relay') else '')),
        'rx_bytes': p.get('RxBytes', 0),
        'tx_bytes': p.get('TxBytes', 0),
        'exit_node': bool(p.get('ExitNode')),
        'tags': p.get('Tags') or [],
    }


def _normalize(data, source):
    self_node = data.get('Self') or {}
    nodes = sorted((_peer(k, v) for k, v in (data.get('Peer') or {}).items()),
                   key=lambda n: (not n['online'], n['name'].lower()))
    tailnet = data.get('CurrentTailnet') or {}
    state = data.get('BackendState', 'unknown')
    return {
        'connected': state == 'Running',
        'backend_state': state,
        'self_ip': _first(self_node.get('TailscaleIPs')),
        'self_ips': self_node.get('TailscaleIPs') or [],
        'self_name': self_node.get('HostName', ''),
        'self_dns_name': (self_node.get('DNSName') or '').rstrip('.'),
        'self_online': bool(self_node.get('Online')),
        'tailnet': tailnet.get('Name', ''),
        'magic_dns_suffix': tailnet.get('MagicDNSSuffix') or data.get('MagicDNSSuffix', ''),
        'version': (data.get('Version') or '').split('-')[0],
        'health': data.get('Health') or [],
        'exit_node': next((n['name'] for n in nodes if n['exit_node']), ''),
        'nodes': nodes,
        'node_count': len(nodes),
        'online_count': sum(1 for n in nodes if n['online']),
        'source': source,
        'checked_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'error': None,
    }


def get_tailscale_status():
    """Current Tailscale status of this host, never raises."""
    errors = []
    if os.path.exists(SOCKET):
        try:
            return _normalize(_status_from_socket(), 'localapi')
        except Exception as e:
            errors.append(f'LocalAPI: {e}')
    try:
        return _normalize(_status_from_cli(), 'cli')
    except FileNotFoundError:
        errors.append('the tailscale CLI is not installed here'
                      + ('' if os.path.exists(SOCKET) else
                         f' and {SOCKET} is not mounted'))
    except Exception as e:
        errors.append(f'CLI: {e}')
    return {'connected': False, 'backend_state': 'unavailable', 'nodes': [], 'node_count': 0,
            'online_count': 0, 'health': [], 'error': '; '.join(errors),
            'checked_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}


def connect_tailscale(auth_key):
    """Connect to Tailscale with auth key (native installs run as root only)."""
    try:
        result = subprocess.run(
            ['tailscale', 'up', f'--authkey={auth_key}'],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0, result.stdout + result.stderr
    except FileNotFoundError:
        return False, 'Manage Tailscale on the host: sudo tailscale up'
    except Exception as e:
        return False, str(e)


def disconnect_tailscale():
    """Disconnect from Tailscale (native installs run as root only)."""
    try:
        result = subprocess.run(
            ['tailscale', 'down'],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0, result.stdout + result.stderr
    except FileNotFoundError:
        return False, 'Manage Tailscale on the host: sudo tailscale down'
    except Exception as e:
        return False, str(e)

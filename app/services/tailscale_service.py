import subprocess
import json


def get_tailscale_status():
    """Get current Tailscale status."""
    try:
        result = subprocess.run(
            ['tailscale', 'status', '--json'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            peers = data.get('Peer', {})
            nodes = []
            for k, v in peers.items():
                nodes.append({
                    'name': v.get('HostName', k),
                    'ip': v.get('TailscaleIPs', [''])[0],
                    'online': v.get('Online', False),
                    'os': v.get('OS', ''),
                })
            self_node = data.get('Self', {})
            return {
                'connected': data.get('BackendState') == 'Running',
                'backend_state': data.get('BackendState', 'unknown'),
                'self_ip': self_node.get('TailscaleIPs', [''])[0] if self_node else '',
                'self_name': self_node.get('HostName', '') if self_node else '',
                'nodes': nodes,
                'node_count': len(nodes),
                'error': None
            }
        return {'connected': False, 'error': result.stderr, 'nodes': [], 'node_count': 0}
    except FileNotFoundError:
        return {'connected': False, 'error': 'Tailscale not installed', 'nodes': [], 'node_count': 0}
    except Exception as e:
        return {'connected': False, 'error': str(e), 'nodes': [], 'node_count': 0}


def connect_tailscale(auth_key):
    """Connect to Tailscale with auth key."""
    try:
        result = subprocess.run(
            ['tailscale', 'up', f'--authkey={auth_key}'],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0, result.stdout + result.stderr
    except FileNotFoundError:
        return False, 'Tailscale not installed on this system'
    except Exception as e:
        return False, str(e)


def disconnect_tailscale():
    """Disconnect from Tailscale."""
    try:
        result = subprocess.run(
            ['tailscale', 'down'],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0, result.stdout + result.stderr
    except FileNotFoundError:
        return False, 'Tailscale not installed on this system'
    except Exception as e:
        return False, str(e)

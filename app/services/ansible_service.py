"""Ansible-backed fleet operations with real per-host results.

Prefers ansible-runner (structured events per host). Falls back to sequential
SSH when Ansible is not installed, so the platform degrades gracefully on a
bare host. Every run yields a per-node {success, output, error} map that the
callers turn into ActivityLog / Deployment rows.
"""
import os
import re
import shutil
import tempfile
import yaml

from app.services.ssh_service import execute_command, upload_file


def _alias(node):
    """Inventory-safe host name. Node names may contain spaces ('Node 1'),
    which Ansible rejects, so hosts are keyed by id and mapped back."""
    return f"n{node.id}_" + re.sub(r'[^A-Za-z0-9_.-]', '_', node.name or '')[:40]


def build_inventory(nodes):
    inv = {'all': {'hosts': {}, 'vars': {
        'ansible_ssh_common_args': '-o StrictHostKeyChecking=no -o ConnectTimeout=10'}}}
    for n in nodes:
        inv['all']['hosts'][_alias(n)] = {
            'ansible_host': n.ip_address,
            'ansible_port': n.ssh_port or 22,
            'ansible_user': n.username,
            'ansible_password': n.password,
            'ansible_become': True,
            'ansible_become_method': 'sudo',
            'ansible_become_pass': n.password,
        }
    return inv


def _runner_available():
    """ansible-runner drives the ansible-playbook binary from ansible-core;
    both must be present, otherwise fall back to plain SSH."""
    try:
        import ansible_runner  # noqa: F401
    except Exception:
        return False
    return shutil.which('ansible-playbook') is not None


def run_shell(nodes, command, become=True):
    """Run a shell command across nodes. Returns {node_name: result}."""
    if not _runner_available():
        return _ssh_fallback_shell(nodes, command)

    import ansible_runner
    names = {_alias(n): n.name for n in nodes}
    results = {n.name: {'success': False, 'output': '', 'error': 'no result'}
               for n in nodes}
    with tempfile.TemporaryDirectory() as pd:
        os.makedirs(os.path.join(pd, 'inventory'), exist_ok=True)
        os.makedirs(os.path.join(pd, 'project'), exist_ok=True)
        with open(os.path.join(pd, 'inventory', 'hosts.yml'), 'w') as f:
            yaml.dump(build_inventory(nodes), f)
        playbook = [{
            'name': 'EdgeShell shell', 'hosts': 'all', 'gather_facts': False,
            'become': become,
            'tasks': [{'name': 'run', 'shell': command, 'ignore_errors': True,
                       'register': 'r'}],
        }]
        with open(os.path.join(pd, 'project', 'play.yml'), 'w') as f:
            yaml.dump(playbook, f)

        r = ansible_runner.run(private_data_dir=pd, playbook='play.yml',
                               quiet=True)
        for e in r.events:
            ev = e.get('event', '')
            if ev in ('runner_on_ok', 'runner_on_failed', 'runner_on_unreachable'):
                d = e.get('event_data', {})
                host = names.get(d.get('host'))
                res = d.get('res', {})
                if host in results:
                    results[host] = {
                        'success': ev == 'runner_on_ok',
                        'output': res.get('stdout', ''),
                        'error': res.get('stderr', '') or res.get('msg', ''),
                    }
    return results


def run_playbook(nodes, playbook_path, extra_vars=None, become=True):
    """Run a playbook file against nodes. Returns {node_name: result}."""
    if not _runner_available():
        return {n.name: {'success': False, 'output': '',
                         'error': 'ansible-runner not installed'} for n in nodes}
    import ansible_runner
    names = {_alias(n): n.name for n in nodes}
    results = {n.name: {'success': False, 'output': '', 'error': 'no result'}
               for n in nodes}
    with tempfile.TemporaryDirectory() as pd:
        os.makedirs(os.path.join(pd, 'inventory'), exist_ok=True)
        with open(os.path.join(pd, 'inventory', 'hosts.yml'), 'w') as f:
            yaml.dump(build_inventory(nodes), f)
        r = ansible_runner.run(private_data_dir=pd, playbook=playbook_path,
                               extravars=extra_vars or {}, quiet=True)
        for e in r.events:
            ev = e.get('event', '')
            if ev in ('runner_on_ok', 'runner_on_failed', 'runner_on_unreachable'):
                d = e.get('event_data', {})
                host, res = names.get(d.get('host')), d.get('res', {})
                if host in results and ev != 'runner_on_ok':
                    results[host] = {'success': False, 'output': res.get('stdout', ''),
                                     'error': res.get('stderr', '') or res.get('msg', '')}
                elif host in results and ev == 'runner_on_ok':
                    if results[host]['error'] == 'no result':
                        results[host] = {'success': True,
                                         'output': res.get('stdout', ''), 'error': ''}
    return results


def deploy_file(nodes, local_path, remote_path):
    results = {}
    for n in nodes:
        ok, msg = upload_file(n, local_path, remote_path)
        results[n.name] = {'success': ok, 'message': msg}
    return results


def _ssh_fallback_shell(nodes, command):
    results = {}
    for n in nodes:
        r = execute_command(n, command)
        results[n.name] = {'success': r['success'], 'output': r['stdout'],
                           'error': r['stderr']}
    return results

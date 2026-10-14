import time, socket, json, os, tempfile, shlex
import paramiko
from datetime import datetime
from app.utils.timeutil import iso_utc


class TailscaleCheckRequired(paramiko.AuthenticationException):
    """Port 22 on the node is answered by Tailscale SSH, which ignores the
    password and refused or held the login (typically an SSH policy with
    action "check", which waits for a person to approve in a browser)."""


TAILSCALE_CHECK_MESSAGE = ('Tailscale SSH on this node asks for a browser check, so the password is '
                           'not used. In the tailnet SSH policy, allow this server with an "accept" '
                           'rule that no "check" rule also matches, or turn off Tailscale SSH on the '
                           'node (docs/VPN-NETWORK.md, Tailscale SSH on the nodes).')


class LoginPaused(Exception):
    """Logins to this node are paused by the login policy."""


PAUSED_MESSAGE = ('Logins to this node are paused after failed logins, so EdgeShell does not keep '
                  'sending a password the node rejects. Fix the login on its card, or use '
                  '"Retry paused logins".')


def connect(hostname, port, username, password, timeout=10):
    """Open an SSH client. On any failure the half-open client is closed
    (otherwise its socket and transport thread stay open until the process
    runs out of file handles) and the error is raised."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=hostname, port=port, username=username, password=password,
                       timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                       allow_agent=False, look_for_keys=False)
        return client
    except paramiko.AuthenticationException as e:
        t = client.get_transport()
        banner = (getattr(t, 'remote_version', '') or '') if t else ''
        client.close()
        if 'tailscale' in banner.lower():
            err = TailscaleCheckRequired(TAILSCALE_CHECK_MESSAGE)
            err.ssh_banner = banner
            raise err from e
        e.ssh_banner = banner
        raise
    except BaseException:
        client.close()
        raise


def get_ssh_client(node, timeout=10):
    if getattr(node, 'login_paused_at', None):
        raise LoginPaused(PAUSED_MESSAGE)
    return connect(node.ip_address, node.ssh_port or 22, node.username, node.password, timeout)


def test_connection(node, timeout=10):
    try:
        client = get_ssh_client(node, timeout=timeout)
        _, stdout, _ = client.exec_command('echo ok', timeout=5)
        out = stdout.read().decode().strip()
        client.close()
        return True, 'Connection successful' if out == 'ok' else f'Connected ({out})'
    except TailscaleCheckRequired:
        return False, TAILSCALE_CHECK_MESSAGE
    except LoginPaused:
        return False, PAUSED_MESSAGE
    except paramiko.AuthenticationException:
        return False, 'Authentication failed - check credentials'
    except paramiko.SSHException as e:
        return False, f'SSH error: {e}'
    except socket.timeout:
        return False, f'Timed out after {timeout}s'
    except socket.error as e:
        return False, f'Network error: {e}'
    except Exception as e:
        return False, f'Error: {e}'


def execute_command(node, command, timeout=60):
    start  = time.time()
    result = dict(node=node.name, command=command, stdout='', stderr='',
                  exit_code=-1, duration=0, success=False,
                  timestamp=iso_utc(datetime.utcnow()))
    try:
        client = get_ssh_client(node)
        _, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=True)
        result['stdout']    = stdout.read().decode('utf-8', errors='replace')
        result['stderr']    = stderr.read().decode('utf-8', errors='replace')
        result['exit_code'] = stdout.channel.recv_exit_status()
        result['success']   = result['exit_code'] == 0
        client.close()
    except Exception as e:
        result['stderr']  = str(e)
        result['success'] = False
    result['duration'] = round(time.time() - start, 3)
    return result


_CWD_MARKER = '__EDGESHELL_CWD__'


def execute_in_dir(node, command, cwd=None, timeout=60):
    """Run a command anchored at `cwd` and report the resulting directory.

    Each SSH exec is its own shell, so a plain `cd` never persists. Here the
    command is run as `cd <cwd>; <command>` and the final `pwd` is captured via
    a marker line, stripped from the output, and returned as `result['cwd']`.
    The terminal stores that per node so `cd` sticks across commands, like a
    real shell. Environment exports still do not persist (that needs a full
    interactive session); working directory does, which is the common need.
    """
    prefix = f'cd {shlex.quote(cwd)} 2>/dev/null; ' if cwd else ''
    wrapped = (f'{prefix}{command}\n'
               f'__ec=$?\n'
               f'printf "%s%s\\n" "{_CWD_MARKER}" "$(pwd)"\n'
               f'exit $__ec')
    result = execute_command(node, wrapped, timeout=timeout)
    new_cwd = cwd
    kept = []
    for line in result['stdout'].splitlines():
        stripped = line.rstrip('\r')
        if stripped.startswith(_CWD_MARKER):
            new_cwd = stripped[len(_CWD_MARKER):].strip() or new_cwd
        else:
            kept.append(line)
    result['stdout'] = '\n'.join(kept)
    result['cwd'] = new_cwd
    return result


def upload_file(node, local_path, remote_path, timeout=120):
    """
    Upload any file to a node.
    Strategy:
      1. SFTP-put to /tmp/edgeshell_stage/<filename>  (always writable)
      2. SSH exec: sudo mv staging_file → target_path  (handles /opt, /etc, etc.)
      3. sudo chown/chmod as needed
    This bypasses SFTP permission limits for system directories.
    """
    filename   = os.path.basename(local_path)
    stage_dir  = '/tmp/edgeshell_stage'
    stage_file = f'{stage_dir}/{filename}'
    final_path = f"{remote_path.rstrip('/')}/{filename}"

    try:
        client = get_ssh_client(node, timeout=10)

        # Step 1 - ensure staging dir exists
        _, stdout, _ = client.exec_command(f'mkdir -p {stage_dir}', timeout=5)
        stdout.channel.recv_exit_status()

        # Step 2 - SFTP to staging (always /tmp, always writable)
        sftp = client.open_sftp()
        sftp.put(local_path, stage_file)
        sftp.close()

        # Step 3 - ensure target dir exists with sudo
        _, stdout, stderr = client.exec_command(
            f'sudo mkdir -p {remote_path} 2>/dev/null || mkdir -p {remote_path}', timeout=10)
        stdout.channel.recv_exit_status()

        # Step 4 - move from staging to target with sudo
        _, stdout, stderr_ch = client.exec_command(
            f'sudo cp {stage_file} {final_path} && sudo chmod 644 {final_path} && echo OK',
            timeout=30)
        out = stdout.read().decode().strip()
        err = stderr_ch.read().decode().strip()
        rc  = stdout.channel.recv_exit_status()

        # Cleanup staging
        client.exec_command(f'rm -f {stage_file}', timeout=5)
        client.close()

        if rc == 0 or 'OK' in out:
            return True, f'Deployed to {final_path}'
        return False, err or f'Deploy failed (exit {rc})'

    except Exception as e:
        return False, str(e)


# Full heartbeat metrics script
_METRICS_SCRIPT = r"""python3 << 'PYEOF'
import json, os, time, subprocess, platform

def _read(path, default=''):
    try:
        with open(path) as f: return f.read()
    except: return default

def _run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except: return ''

la = _read('/proc/loadavg', '0 0 0').split()
load_1 = float(la[0]) if len(la)>0 else 0
load_5 = float(la[1]) if len(la)>1 else 0
load_15= float(la[2]) if len(la)>2 else 0

mem = {}
for line in _read('/proc/meminfo').splitlines():
    p = line.split()
    if len(p) >= 2: mem[p[0].rstrip(':')] = int(p[1])
mem_total = mem.get('MemTotal', 1)
mem_free  = mem.get('MemAvailable', mem.get('MemFree', 0))
mem_used  = mem_total - mem_free
mem_pct   = round(mem_used / mem_total * 100, 1)

def cpu_sample():
    line = _read('/proc/stat').splitlines()[0].split()
    vals = [int(x) for x in line[1:]]
    return sum(vals), vals[3] + (vals[4] if len(vals)>4 else 0)

t1, i1 = cpu_sample()
time.sleep(0.4)
t2, i2 = cpu_sample()
dt, di = t2-t1, i2-i1
cpu_pct = round((1 - di/dt)*100, 1) if dt else 0

try:
    st = os.statvfs('/')
    disk_total = st.f_blocks * st.f_frsize
    disk_free  = st.f_bavail * st.f_frsize
    disk_used  = disk_total - disk_free
    disk_pct   = round(disk_used/disk_total*100, 1) if disk_total else 0
    disk_total_gb = round(disk_total/1073741824, 1)
    disk_used_gb  = round(disk_used/1073741824, 1)
except: disk_pct = disk_total_gb = disk_used_gb = 0

uptime_s = float(_read('/proc/uptime','0').split()[0])
d,r = divmod(int(uptime_s), 86400); h,r = divmod(r,3600); m = r//60
uptime_str = (f'{d}d ' if d else '') + f'{h}h {m}m'

temp = None
vcg = _run('vcgencmd measure_temp 2>/dev/null')
if 'temp=' in vcg:
    try: temp = float(vcg.split('=')[1].replace("'C",''))
    except: pass
if temp is None:
    tz = _read('/sys/class/thermal/thermal_zone0/temp','').strip()
    if tz.lstrip('-').isdigit(): temp = round(int(tz)/1000, 1)
if temp is None:
    ft = _read('/tmp/fake_temp','').strip()
    if ft.lstrip('-').isdigit(): temp = round(int(ft)/1000, 1)

arch     = _run('uname -m') or platform.machine()
os_name  = _run('cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2 | tr -d \\"')
kernel   = _run('uname -r')

net_rx = net_tx = 0
try:
    for line in open('/proc/net/dev'):
        if ':' not in line: continue
        iface, data = line.split(':', 1)
        if iface.strip() == 'lo': continue
        vals = data.split()
        net_rx += int(vals[0]); net_tx += int(vals[8]); break
except: pass

docker_installed = bool(_run('command -v docker 2>/dev/null'))
# Prefer sudo (pi is often not in the docker group); sudo -n never hangs,
# and we fall back to a plain check if passwordless sudo is unavailable.
docker_up = bool(_run('sudo -n docker ps -q 2>/dev/null || docker ps -q 2>/dev/null')) \
    if docker_installed else False
nodered_installed = bool(_run('command -v node-red 2>/dev/null') or
    _run('systemctl list-unit-files 2>/dev/null | grep -c -E "nodered|node-red"'))
nodered_up = bool(_run('pgrep -x node-red 2>/dev/null') or \
    _run('systemctl is-active nodered 2>/dev/null | grep -c active'))

print(json.dumps({
    'cpu_percent':    cpu_pct,
    'mem_percent':    mem_pct,
    'mem_total_mb':   round(mem_total/1024),
    'mem_used_mb':    round(mem_used/1024),
    'mem_free_mb':    round(mem_free/1024),
    'disk_percent':   disk_pct,
    'disk_total_gb':  disk_total_gb,
    'disk_used_gb':   disk_used_gb,
    'load_1':         load_1,
    'load_5':         load_5,
    'load_15':        load_15,
    'load_trend':     'rising' if load_1>load_15 else ('falling' if load_1<load_15 else 'stable'),
    'uptime':         uptime_str,
    'uptime_seconds': int(uptime_s),
    'temperature':    temp,
    'arch':           arch,
    'os_name':        os_name,
    'kernel':         kernel,
    'net_rx_bytes':   net_rx,
    'net_tx_bytes':   net_tx,
    'docker_running': docker_up,
    'docker_installed': docker_installed,
    'nodered_running':nodered_up,
    'nodered_installed': nodered_installed,
    'collected_at':   time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
}))
PYEOF"""


def collect_metrics(node):
    try:
        client = get_ssh_client(node, timeout=10)
        _, stdout, _ = client.exec_command(_METRICS_SCRIPT, timeout=25)
        output = stdout.read().decode('utf-8', errors='replace').strip()
        client.close()
        for line in reversed(output.splitlines()):
            line = line.strip()
            if line.startswith('{'):
                data = json.loads(line)
                if 'error' not in data:
                    return data
    except Exception:
        pass
    return None

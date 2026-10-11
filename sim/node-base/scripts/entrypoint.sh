#!/bin/bash
set -e

echo "============================================"
echo "  EdgeShell Node: $NODE_NAME"
echo "  Arch (sim): ${SIM_ARCH:-x86_64}"
echo "  OS   (sim): ${SIM_OS:-Ubuntu 22.04}"
echo "  SSH  User : $NODE_USER / raspberry"
echo "============================================"

# Generate SSH host keys
[ ! -f /etc/ssh/ssh_host_rsa_key ] && ssh-keygen -A

# Set hostname
hostname "$NODE_NAME" 2>/dev/null || true

# Patch /usr/bin/uname to report simulated arch
ARCH=${SIM_ARCH:-x86_64}
cat > /usr/local/bin/uname << UEOF
#!/bin/bash
case "\$*" in
  "-m"|"--machine") echo "$ARCH" ;;
  "-r"|"--kernel-release") echo "6.1.21-v8+ #1642 SMP PREEMPT Mon Apr  3 17:24:16 BST 2023" ;;
  "-a"|"--all") echo "Linux $NODE_NAME 6.1.21-v8+ #1642 SMP PREEMPT Mon Apr  3 17:24:16 BST 2023 $ARCH GNU/Linux" ;;
  *) /bin/uname "\$@" ;;
esac
UEOF
chmod +x /usr/local/bin/uname

# Patch platform.machine() for Python (used by metrics script)
SITE=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || echo "/usr/lib/python3/dist-packages")
cat > "$SITE/platform_patch.pth" << PEOF
import platform as _p; _p._machine_cache = "$ARCH"; _p.machine = lambda: "$ARCH"
PEOF

# Fake /etc/os-release to look like Pi OS
cat > /etc/os-release << OSEOF
PRETTY_NAME="${SIM_OS:-Raspberry Pi OS Lite}"
NAME="Raspberry Pi OS"
VERSION_ID="12"
VERSION="12 (bookworm)"
ID=raspbian
ID_LIKE=debian
OSEOF

# Seed log files
mkdir -p /var/log/edgeapp /home/$NODE_USER/.node-red
cat > /var/log/edgeapp/app.log << 'LOGEOF'
2024-01-15 08:00:01 INFO  [EdgeApp] Starting edge application v2.1.0
2024-01-15 08:00:03 INFO  [MQTT] Connected to broker
2024-01-15 08:01:00 INFO  [Sensor] temp=23.4C hum=61%
2024-01-15 08:02:00 WARN  [Sensor] Temperature spike: 45.2C
2024-01-15 08:10:00 ERROR [MQTT] Connection lost, reconnecting...
2024-01-15 08:10:02 INFO  [MQTT] Reconnected
LOGEOF
cat > /var/log/syslog << 'SYSEOF'
Jan 15 08:00:01 edge-node systemd[1]: Started OpenBSD Secure Shell server.
Jan 15 08:00:02 edge-node systemd[1]: Started Node-RED.
Jan 15 08:10:00 edge-node sshd[456]: Accepted password for pi from 10.10.0.10 port 54321 ssh2
SYSEOF

# Install fake docker/systemctl
/usr/local/bin/fake-docker-stats

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf

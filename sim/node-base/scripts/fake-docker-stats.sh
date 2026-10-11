#!/bin/bash
# Simulates 'docker ps' and 'systemctl is-active' output so EdgeShell
# monitoring sees realistic service states

# Make 'docker' command work even without real Docker
cat > /usr/local/bin/docker << 'DEOF'
#!/bin/bash
case "$*" in
  "ps"*|"ps -q"*)
    # Return 2-3 fake running containers
    echo "abc123def456"
    echo "789ghi012jkl"
    ;;
  "ps --format"*)
    echo "node-red-main|node-red:latest|Up 2 hours|running"
    echo "mqtt-broker|eclipse-mosquitto:latest|Up 2 hours|running"
    ;;
  "info"*)
    echo '{"ServerVersion":"24.0.5","Containers":2,"ContainersRunning":2}'
    ;;
  *)
    echo "Docker (simulated) - EdgeShell test environment"
    ;;
esac
DEOF
chmod +x /usr/local/bin/docker

# Make systemctl respond for node-red
cat > /usr/local/bin/systemctl << 'SEOF'
#!/bin/bash
SERVICE=$2
case "$1" in
  "is-active")
    case "$SERVICE" in
      "nodered"|"node-red"|"node-red.service") echo "active"; exit 0 ;;
      "docker"|"docker.service") echo "active"; exit 0 ;;
      "ssh"|"sshd") echo "active"; exit 0 ;;
      *) echo "inactive"; exit 1 ;;
    esac
    ;;
  "status")
    echo "● $SERVICE - Simulated service"
    echo "   Active: active (running)"
    ;;
  *) echo "systemctl: simulated" ;;
esac
SEOF
chmod +x /usr/local/bin/systemctl

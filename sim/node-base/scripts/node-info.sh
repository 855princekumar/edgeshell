#!/bin/bash
# Run on any node to get a summary - useful for manual SSH testing
echo "=== EdgeShell Node Info ==="
echo "Hostname : $(hostname)"
echo "Node Name: $NODE_NAME"
echo "User     : $(whoami)"
echo "IP       : $(hostname -I | awk '{print $1}')"
echo "Uptime   : $(cat /tmp/fake_uptime 2>/dev/null | awk '{printf "%d min\n", $1/60}' || uptime -p)"
echo "Metrics  :"
cat /tmp/metrics/summary.txt 2>/dev/null | grep -E "CPU|MEM|DISK|TEMP|LOAD"
echo "Node-RED : $(curl -s -o /dev/null -w '%{http_code}' http://localhost:1880 2>/dev/null || echo 'checking...')"
echo "==========================="

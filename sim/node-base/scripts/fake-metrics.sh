#!/bin/bash
# Continuously writes realistic fluctuating metrics to /tmp/metrics
# EdgeShell's SSH service reads these via 'cat /tmp/metrics/*'

NODE_NAME=${NODE_NAME:-edge-node}
UPTIME_SECS=0

while true; do
    UPTIME_SECS=$((UPTIME_SECS + 30))

    # Simulate realistic CPU (5-75% with occasional spikes)
    BASE_CPU=$((RANDOM % 40 + 5))
    SPIKE=$((RANDOM % 10))
    [ $SPIKE -eq 0 ] && BASE_CPU=$((BASE_CPU + 35))
    CPU_PCT=$BASE_CPU

    # RAM: 512MB total (Pi-like), 60-85% used
    MEM_TOTAL=524288   # kB
    MEM_USED=$((MEM_TOTAL * (60 + RANDOM % 25) / 100))
    MEM_FREE=$((MEM_TOTAL - MEM_USED))

    # Disk: 16GB, 45-70% used
    DISK_TOTAL=16384   # MB
    DISK_USED=$((DISK_TOTAL * (45 + RANDOM % 25) / 100))
    DISK_FREE=$((DISK_TOTAL - DISK_USED))
    DISK_PCT=$((DISK_USED * 100 / DISK_TOTAL))

    # Temp: 42-65°C (Pi-realistic)
    TEMP=$((42 + RANDOM % 23))

    # Load average
    LOAD1=$(echo "scale=2; $CPU_PCT / 100 * 1.2" | bc)
    LOAD5=$(echo "scale=2; $CPU_PCT / 100 * 0.9" | bc)
    LOAD15=$(echo "scale=2; $CPU_PCT / 100 * 0.7" | bc)

    # Write /proc-compatible files so SSH service can read them naturally
    # /proc/stat CPU line
    echo "cpu  $((CPU_PCT * 100)) 0 $((CPU_PCT * 50)) $((10000 - CPU_PCT * 150)) 0 0 0 0 0 0" > /tmp/fake_stat
    
    # /proc/meminfo
    cat > /tmp/fake_meminfo << MEMEOF
MemTotal:       ${MEM_TOTAL} kB
MemFree:        ${MEM_FREE} kB
MemAvailable:   $((MEM_FREE + MEM_USED / 4)) kB
Buffers:         16384 kB
Cached:         $((MEM_USED / 4)) kB
SwapTotal:      102400 kB
SwapFree:        98304 kB
MEMEOF

    # /proc/loadavg
    echo "$LOAD1 $LOAD5 $LOAD15 2/$(( RANDOM % 200 + 50)) $$" > /tmp/fake_loadavg

    # Uptime in seconds
    echo "$UPTIME_SECS.00 $((UPTIME_SECS / 2)).00" > /tmp/fake_uptime

    # Temperature (Pi thermal zone style)
    echo "$((TEMP * 1000))" > /tmp/fake_temp

    # Write a human-readable summary for quick SSH inspection
    mkdir -p /tmp/metrics
    cat > /tmp/metrics/summary.txt << SUMEOF
NODE=$NODE_NAME
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CPU_PCT=$CPU_PCT
MEM_TOTAL_KB=$MEM_TOTAL
MEM_USED_KB=$MEM_USED
MEM_FREE_KB=$MEM_FREE
DISK_TOTAL_MB=$DISK_TOTAL
DISK_USED_MB=$DISK_USED
DISK_FREE_MB=$DISK_FREE
DISK_PCT=$DISK_PCT
TEMP_C=$TEMP
LOAD_1=$LOAD1
LOAD_5=$LOAD5
LOAD_15=$LOAD15
UPTIME_SECS=$UPTIME_SECS
SUMEOF

    sleep 30
done

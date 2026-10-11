import re

SEVERITY_PATTERNS = [
    (re.compile(r'\b(CRITICAL|FATAL|EMERG)\b', re.I), 'critical'),
    (re.compile(r'\b(ERROR|ERR|FAILED|FAILURE)\b', re.I), 'error'),
    (re.compile(r'\b(WARN|WARNING)\b', re.I), 'warning'),
    (re.compile(r'\b(INFO|NOTICE|DEBUG)\b', re.I), 'info'),
]

KNOWN_PATTERNS = [
    (re.compile(r'ConnectionRefusedError', re.I), 'Connection refused - the target service is not accepting connections'),
    (re.compile(r'TimeoutError|timed? ?out', re.I), 'Operation timed out - check network connectivity and service health'),
    (re.compile(r'PermissionError|Permission denied', re.I), 'Permission denied - insufficient access rights'),
    (re.compile(r'FileNotFoundError|No such file', re.I), 'File or directory not found'),
    (re.compile(r'MemoryError|out of memory|OOM', re.I), 'Out of memory - system is under memory pressure'),
    (re.compile(r'disk.*full|No space left', re.I), 'Disk is full - free up storage space'),
    (re.compile(r'segfault|segmentation fault', re.I), 'Process crashed (segmentation fault) - may indicate a software bug'),
    (re.compile(r'kernel panic', re.I), 'Kernel panic - system required restart'),
    (re.compile(r'PLC.*timeout|timeout.*PLC', re.I), 'PLC communication timeout - check PLC connection'),
    (re.compile(r'MQTT.*disconnect|disconnect.*MQTT', re.I), 'MQTT broker disconnected'),
    (re.compile(r'Modbus.*error|error.*Modbus', re.I), 'Modbus communication error - verify device connectivity'),
    (re.compile(r'node-red.*start|starting node-red', re.I), 'Node-RED is starting up'),
    (re.compile(r'node-red.*stop|stopping node-red', re.I), 'Node-RED has stopped'),
    (re.compile(r'docker.*start|container.*start', re.I), 'Docker container started'),
    (re.compile(r'docker.*stop|container.*stop', re.I), 'Docker container stopped'),
    (re.compile(r'service.*restart', re.I), 'Service restarted'),
    (re.compile(r'CPU usage.*(\d+)%|(\d+)% cpu', re.I), 'High CPU utilization detected'),
    (re.compile(r'authentication failure|auth failed', re.I), 'Authentication failure - invalid credentials attempted'),
    (re.compile(r'ssh.*connection', re.I), 'SSH connection event'),
]


def detect_severity(line):
    for pattern, severity in SEVERITY_PATTERNS:
        if pattern.search(line):
            return severity
    return 'info'


def interpret_log_line(line):
    """Return a human-readable interpretation of a log line."""
    for pattern, description in KNOWN_PATTERNS:
        if pattern.search(line):
            return description
    return None


def format_log(raw_log: str, log_path: str = ''):
    """
    Format raw log text into structured entries.
    Returns list of dicts: {line, severity, interpretation, line_no}
    """
    lines = raw_log.splitlines()
    formatted = []
    for i, line in enumerate(lines, 1):
        severity = detect_severity(line)
        interpretation = interpret_log_line(line)
        formatted.append({
            'line_no': i,
            'raw': line,
            'severity': severity,
            'interpretation': interpretation,
        })
    return formatted


def get_log_summary(raw_log: str):
    """Return summary stats for a log."""
    lines = raw_log.splitlines()
    counts = {'critical': 0, 'error': 0, 'warning': 0, 'info': 0}
    for line in lines:
        s = detect_severity(line)
        counts[s] = counts.get(s, 0) + 1
    return {
        'total_lines': len(lines),
        'severity_counts': counts,
    }

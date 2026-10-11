"""CSV / TXT / JSON bulk node import with dynamic schema inference.

The uploaded file has no fixed schema. We sniff the delimiter, read the header
(or synthesize one), and map columns to Node fields by name and by content
(an IP-shaped column becomes ip_address even if unlabeled). JSON is accepted as
a list of node objects or as a Pi Fleet Emulator export ({"nodes": [...]}).
The caller shows the mapping for human verification before anything is
committed.
"""
import csv
import io
import json
import re

IP_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
# DNS / container names (pi-emu-node-1, host.docker.internal, pi.local).
HOST_RE = re.compile(r'^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
                     r'(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$')


def valid_host(value):
    """IPv4 address or hostname. Node addresses may be either."""
    v = (value or '').strip()
    if IP_RE.match(v):
        return all(0 <= int(o) <= 255 for o in v.split('.'))
    return bool(HOST_RE.match(v)) and not v.replace('.', '').isdigit()

FIELD_ALIASES = {
    'ip_address': ['ip', 'ip_address', 'ipaddr', 'address', 'host', 'hostname'],
    'name': ['name', 'node', 'nodename', 'hostname', 'id', 'label'],
    'username': ['user', 'username', 'ssh_user', 'login'],
    'password': ['password', 'pass', 'pwd', 'secret'],
    'ssh_port': ['port', 'ssh_port', 'sshport'],
    'tags': ['tags', 'tag', 'labels'],
    'description': ['description', 'desc', 'notes'],
    'environment': ['env', 'environment'],
    'location': ['location', 'site', 'place'],
    'node_type': ['type', 'node_type', 'role'],
    'nodered_port': ['nodered_port', 'nodered', 'nr_port'],
    'orgui_port': ['orgui_port', 'orgui', 'remote_port'],
}


def _sniff(text):
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
        return dialect.delimiter
    except Exception:
        for d in (',', ';', '\t', '|'):
            if d in sample:
                return d
        return ','


def _guess_field(header):
    h = (header or '').strip().lower().replace(' ', '_')
    for field, aliases in FIELD_ALIASES.items():
        if h in aliases:
            return field
    return None


def _parse_json(text):
    """JSON list of objects, or {"nodes": [...]} (Pi Fleet Emulator export)."""
    data = json.loads(text)
    items = data.get('nodes') if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}, [], []
    items = [i for i in items if isinstance(i, dict)]
    headers = []
    for item in items:
        for k in item:
            if k not in headers:
                headers.append(k)
    rows = [{h: '' if item.get(h) is None else str(item.get(h)).strip() for h in headers}
            for item in items]
    return {h: _guess_field(h) for h in headers}, rows, headers


def parse(text):
    """Return (mapping, rows, headers). mapping is header->field guesses."""
    if text.lstrip()[:1] in ('{', '['):
        try:
            return _parse_json(text)
        except ValueError:
            pass   # not JSON after all; fall through to CSV
    delim = _sniff(text)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    all_rows = [r for r in reader if any(c.strip() for c in r)]
    if not all_rows:
        return {}, [], []

    first = all_rows[0]
    header_is_data = any(IP_RE.match(c.strip()) for c in first)
    if header_is_data:
        headers = [f'col{i+1}' for i in range(len(first))]
        data = all_rows
    else:
        headers = [h.strip() for h in first]
        data = all_rows[1:]

    mapping = {h: _guess_field(h) for h in headers}

    # Content-based fallback: unmapped IP-shaped column -> ip_address.
    if 'ip_address' not in mapping.values():
        for idx, h in enumerate(headers):
            col = [row[idx].strip() for row in data if idx < len(row)]
            if col and sum(bool(IP_RE.match(c)) for c in col) >= max(1, len(col) // 2):
                mapping[h] = 'ip_address'
                break

    rows = [dict(zip(headers, [c.strip() for c in row])) for row in data]
    return mapping, rows, headers


def to_nodes(rows, mapping, default_password='', host_override=''):
    """Apply a confirmed mapping. Returns (valid_nodes, errors).

    default_password fills rows without one (an emulator export has none).
    host_override replaces host.docker.internal, for an EdgeShell that runs
    directly on the host rather than in Docker."""
    out, errors = [], []
    seen = set()
    for i, row in enumerate(rows, 1):
        rec = {}
        for header, field in mapping.items():
            if field and header in row and row[header] != '':
                rec[field] = row[header]
        ip = rec.get('ip_address', '')
        if host_override and ip == 'host.docker.internal':
            ip = rec['ip_address'] = host_override
        if not valid_host(ip):
            errors.append({'row': i, 'error': f'invalid or missing address: "{ip}"'})
            continue
        if default_password and not rec.get('password'):
            rec['password'] = default_password
        rec.setdefault('name', ip.replace('.', '-'))
        if rec['name'] in seen:
            rec['name'] = f"{rec['name']}-{i}"
        seen.add(rec['name'])
        rec.setdefault('username', 'pi')
        for p in ('ssh_port', 'nodered_port', 'orgui_port'):
            if p in rec:
                try:
                    rec[p] = int(rec[p])
                except ValueError:
                    rec.pop(p)
        out.append(rec)
    return out, errors

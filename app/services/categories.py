"""Device categories for the dashboard and filters.

A category groups nodes by their node type (for example every
'gateway'), has an icon and an SLA target, and gets its own card and
scoped stats on the dashboard. Administrators edit the list on the
dashboard; it is stored as JSON in the settings table. Nodes whose type is in
no category appear under "Other".
"""
import json
import re

from app import db

SETTING_KEY = 'dashboard_categories'
ICONS = ('hdd-network', 'droplet-half', 'heart-pulse', 'cpu', 'router', 'thermometer-half',
         'lightning-charge', 'water', 'broadcast', 'camera-video', 'gear', 'box')
DEFAULT = [
    {'name': 'Pi Nodes', 'types': ['pi-node'], 'icon': 'hdd-network', 'sla_target': 99.0},
    {'name': 'Edge Gateways', 'types': ['gateway'], 'icon': 'router', 'sla_target': 99.0},
    {'name': 'Mini PCs', 'types': ['mini-pc'], 'icon': 'cpu', 'sla_target': 99.0},
]
MAX_CATEGORIES = 24
_TYPE_RE = re.compile(r'^[a-z0-9][a-z0-9._ -]{0,31}$')


def load():
    from app.models import Settings
    row = Settings.query.filter_by(key=SETTING_KEY).first()
    if row and row.value:
        try:
            cats = json.loads(row.value)
            if isinstance(cats, list):
                return cats
        except ValueError:
            pass
    return [dict(c) for c in DEFAULT]


def clean(raw):
    """Validate an edited list. Returns (categories, error)."""
    if not isinstance(raw, list) or len(raw) > MAX_CATEGORIES:
        return None, f'Send a list of at most {MAX_CATEGORIES} categories.'
    out, names, taken = [], set(), {}
    for i, c in enumerate(raw, 1):
        if not isinstance(c, dict):
            return None, f'Category {i} is not valid.'
        name = str(c.get('name') or '').strip()[:40]
        if not name:
            return None, f'Category {i} needs a name.'
        if name.lower() in names:
            return None, f'Two categories are called "{name}".'
        names.add(name.lower())
        types = c.get('types') or []
        if isinstance(types, str):
            types = types.split(',')
        types = sorted({str(t).strip().lower() for t in types if str(t).strip()})
        if not types:
            return None, f'"{name}" needs at least one node type.'
        for t in types:
            if not _TYPE_RE.match(t):
                return None, f'"{t}" is not a valid node type (letters, digits, dot, dash, underscore).'
            if t in taken:
                return None, f'Node type "{t}" is in both "{taken[t]}" and "{name}".'
            taken[t] = name
        icon = c.get('icon') if c.get('icon') in ICONS else 'box'
        try:
            sla = float(c.get('sla_target', 99))
        except (TypeError, ValueError):
            sla = 99.0
        out.append({'name': name, 'types': types, 'icon': icon,
                    'sla_target': max(0.0, min(100.0, round(sla, 2)))})
    return out, None


def save(categories):
    from app.models import Settings
    row = Settings.query.filter_by(key=SETTING_KEY).first()
    value = json.dumps(categories)
    if row:
        row.value = value
    else:
        db.session.add(Settings(key=SETTING_KEY, value=value))
    db.session.commit()


def known_types():
    """{node_type: count} of active nodes, for the editor and filters."""
    from app.models.node import Node
    rows = (db.session.query(Node.node_type, db.func.count(Node.id))
            .filter(Node.is_deleted.isnot(True)).group_by(Node.node_type).all())
    return {(t or '').strip().lower(): n for t, n in rows if (t or '').strip()}


def category_of(node_type, categories):
    t = (node_type or '').strip().lower()
    for c in categories:
        if t in c['types']:
            return c['name']
    return 'Other'

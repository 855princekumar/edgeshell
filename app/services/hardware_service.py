"""Hardware lifecycle (ADR 0015): inventory, replacements, faults and fleet numbers.

A node is a position (name, location, role). The parts serving it are
components with a service period. The collector reads each node's hardware
identity (hw_identity) and calls observe():

  - the first time, the parts are recorded (the inventory)
  - when a part differs from the record, a *pending change* is opened and
    named ("Board replaced", "SD card or disk replaced", ...). An operator
    confirms it as a replacement (with a reason, optionally a fault) or
    rejects it as unexpected, which marks the node (a clone, or a part
    swapped without telling anyone)
  - when the hardware goes back to the record, a pending change is closed
    as reverted and a rejection mark is cleared

Anyone signed in reports faults; operators and administrators move them on
and resolve them. A new node can be linked to the node it replaces: the old
one is retired, never deleted, and both histories stay together.
"""
import csv
import difflib
import io
import json
import re
from datetime import datetime, timedelta

from app import db
from app.models import Settings
from app.models.api import NodeEvent
from app.models.hardware import (CHANGE_REASONS, DETECTED_KINDS, FAULT_SEVERITIES, KIND_LABELS, RESOLUTIONS,
                                 HardwareChange, HardwareComponent, HardwareEvent, HardwareFault)
from app.models.node import Node
from app.services import hw_identity, webhook_service
from app.services.hw_errors import HardwareError, public_message  # noqa: F401  (public_message: for the routes)

CATEGORIES_KEY = 'hardware_categories'
DEFAULT_CATEGORIES = [('power', 'Power supply'), ('storage', 'SD card or storage'), ('board', 'Board'),
                      ('network', 'Network'), ('peripheral', 'Sensor or peripheral'),
                      ('enclosure', 'Enclosure or environment'), ('other', 'Other')]
KIND_TO_CATEGORY = {'board': 'board', 'storage': 'storage', 'network': 'network', 'os': 'storage'}
CATEGORY_RE = re.compile(r'^[a-z0-9_]{1,32}$')
MAX_CATEGORIES = 30
UNDERVOLTAGE_EVENT_EVERY = timedelta(hours=24)
UNDERVOLTAGE_HINT_WINDOW = timedelta(days=7)
PROMPT_WINDOW = timedelta(days=7)
MAX_LINEAGE = 20




# ------------------------------------------------------------------ categories

def categories(include_hidden=False):
    """[{key, label, hidden}]: the defaults (which can be renamed or hidden) and custom ones."""
    saved = {}
    row = Settings.query.filter_by(key=CATEGORIES_KEY).first()
    try:
        for item in json.loads(row.value) if row and row.value else []:
            if isinstance(item, dict) and CATEGORY_RE.match(str(item.get('key', ''))):
                saved[item['key']] = item
    except ValueError:
        pass
    out = []
    for key, label in DEFAULT_CATEGORIES:
        s = saved.pop(key, {})
        out.append({'key': key, 'label': str(s.get('label') or label)[:48], 'hidden': bool(s.get('hidden')),
                    'builtin': True})
    for key, s in saved.items():
        out.append({'key': key, 'label': str(s.get('label') or key)[:48], 'hidden': bool(s.get('hidden')),
                    'builtin': False})
    return out if include_hidden else [c for c in out if not c['hidden']]


def category_keys():
    return {c['key'] for c in categories(include_hidden=True)}


def category_label(key):
    for c in categories(include_hidden=True):
        if c['key'] == key:
            return c['label']
    return KIND_LABELS.get(key, key)


def save_categories(items):
    clean, seen = [], set()
    for item in items or []:
        key = str((item or {}).get('key', '')).strip().lower()
        label = re.sub(r'[<>]', '', str(item.get('label', '')).strip())[:48]
        if not CATEGORY_RE.match(key) or key in seen:
            raise HardwareError('category_key')
        if not label:
            raise HardwareError('category_name')
        seen.add(key)
        clean.append({'key': key, 'label': label, 'hidden': bool(item.get('hidden'))})
    missing = {k for k, _ in DEFAULT_CATEGORIES} - seen
    clean += [{'key': k, 'label': lbl, 'hidden': False} for k, lbl in DEFAULT_CATEGORIES if k in missing]
    if len(clean) > MAX_CATEGORIES:
        raise HardwareError('too_many_categories')
    row = Settings.query.filter_by(key=CATEGORIES_KEY).first() or Settings(key=CATEGORIES_KEY)
    row.value = json.dumps(clean)
    db.session.add(row)
    db.session.commit()
    return categories(include_hidden=True)


# ------------------------------------------------------------------ helpers

def _event(node_id, node_name, kind, message, user=None, fault_id=None, change_id=None, at=None):
    db.session.add(HardwareEvent(node_id=node_id, node_name=node_name, kind=kind, message=(message or '')[:300],
                                 user=user, fault_id=fault_id, change_id=change_id, at=at or datetime.utcnow()))


def _feed(node, kind, new_value=None, message=None):
    """The API event feed (GET /api/v1/events)."""
    db.session.add(NodeEvent(node_id=node.id if node else None, node_name=node.name if node else None, kind=kind,
                             new_value=(new_value or '')[:32] or None, message=(message or '')[:250] or None))


def _seen_elsewhere(kind, identifier, node_id):
    """The node name where this part served before (or still serves), if any."""
    c = HardwareComponent.query.filter(HardwareComponent.kind == kind, HardwareComponent.identifier == identifier,
                                       HardwareComponent.node_id != node_id) \
        .order_by(HardwareComponent.installed_at.desc()).first()
    return c.node_name if c else None


def _short(identifier):
    return identifier if len(identifier or '') <= 20 else identifier[:8] + '...' + identifier[-6:]


def classify(kinds, known):
    """A plain name for a set of changed parts. `known`: kinds on record."""
    k = set(kinds)
    board = 'board' in k or ('network' in k and 'board' not in known)   # without a board serial, the MAC stands in
    if board and 'storage' in k:
        return 'Board and SD card or disk replaced'
    if board:
        return 'Board replaced (the same SD card or disk in new hardware)'
    if 'storage' in k:
        return 'SD card or disk replaced'
    if k == {'os'}:
        return 'Operating system reinstalled on the same hardware'
    if 'network' in k:
        return 'Network adapter changed'
    return 'Hardware changed: ' + ', '.join(KIND_LABELS.get(x, x) for x in sorted(k))


# ------------------------------------------------------------------ detection

def observe(node, hw, now=None):
    """Called by the collector after a successful check of `node`. Cheap when
    nothing changed (one fingerprint comparison)."""
    now = now or datetime.utcnow()
    node.hw_scanned_at = now
    if hw_identity.undervoltage(hw):
        node.hw_undervoltage_at = now
        last = HardwareEvent.query.filter_by(node_id=node.id, kind='undervoltage') \
            .order_by(HardwareEvent.at.desc()).first()
        if not last or now - last.at >= UNDERVOLTAGE_EVENT_EVERY:
            _event(node.id, node.name, 'undervoltage', 'The board reported low power (under-voltage): '
                   'check the power supply and cable', at=now)
            db.session.flush()
    ident = hw_identity.identity(hw)
    fp = hw_identity.fingerprint(hw)
    if not ident or node.hw_fingerprint == fp:
        return
    with db.session.begin_nested():
        _apply_identity(node, hw, ident, fp, now)


def _apply_identity(node, hw, ident, fp, now):
    node.hw_fingerprint = fp
    node.hw_model = hw.get('model') or node.hw_model
    node.hw_os = hw.get('os_name') or node.hw_os
    current = {c.kind: c for c in HardwareComponent.query.filter(
        HardwareComponent.node_id == node.id, HardwareComponent.removed_at.is_(None),
        HardwareComponent.kind.in_(DETECTED_KINDS))}
    if not current:
        _record_inventory(node, hw, ident, now)
        return
    diffs = []
    for kind, value in ident.items():
        cur = current.get(kind)
        if cur is None:                                    # newly readable, not a change
            db.session.add(HardwareComponent(node_id=node.id, node_name=node.name, kind=kind, identifier=value,
                                             model=hw_identity.model_of(kind, hw), installed_at=now,
                                             installed_known=False, last_seen=now))
        elif cur.identifier != value:
            diffs.append({'kind': kind, 'label': KIND_LABELS[kind], 'old': cur.identifier, 'new': value,
                          'old_model': cur.model, 'new_model': hw_identity.model_of(kind, hw),
                          'seen_on': _seen_elsewhere(kind, value, node.id)})
        else:
            cur.model = hw_identity.model_of(kind, hw) or cur.model
            cur.last_seen = now
    pending = HardwareChange.query.filter_by(node_id=node.id, status='pending').first()
    if not diffs:
        if pending:
            pending.status, pending.decided_at = 'reverted', now
            _event(node.id, node.name, 'change_reverted', 'The hardware matches the record again',
                   change_id=pending.id)
        if node.hw_alert:
            node.hw_alert = False
            _event(node.id, node.name, 'alert_cleared', 'The hardware matches the record again')
        return
    summary = classify([d['kind'] for d in diffs], set(current))
    if pending:
        pending.changes, pending.summary = json.dumps(diffs), summary
        _event(node.id, node.name, 'change_updated', summary, change_id=pending.id)
        return
    change = HardwareChange(node_id=node.id, node_name=node.name, detected_at=now, summary=summary,
                            changes=json.dumps(diffs))
    db.session.add(change)
    db.session.flush()
    _event(node.id, node.name, 'change_detected', summary + ': waiting for an operator to confirm',
           change_id=change.id)
    _feed(node, 'hardware_changed', 'pending', summary)
    webhook_service.emit('hardware.change_detected', _change_payload(change, node))


def _record_inventory(node, hw, ident, now):
    new_node = node.created_at and now - node.created_at < timedelta(days=1)
    parts = []
    for kind, value in ident.items():
        db.session.add(HardwareComponent(node_id=node.id, node_name=node.name, kind=kind, identifier=value,
                                         model=hw_identity.model_of(kind, hw),
                                         installed_at=node.created_at if new_node else now,
                                         installed_known=bool(new_node), last_seen=now))
        other = _seen_elsewhere(kind, value, node.id)
        parts.append(f"{KIND_LABELS[kind]} {hw_identity.model_of(kind, hw) or _short(value)}"
                     + (f' (was on {other})' if other else ''))
    _event(node.id, node.name, 'inventory_recorded', 'Hardware recorded: ' + '; '.join(parts))
    if node.hw_replace_prompt is None and new_node and node.replaces_id is None:
        db.session.flush()
        if candidates(node):
            node.hw_replace_prompt = 'pending'


def _change_payload(change, node=None):
    d = change.to_dict()
    if node is not None:
        d['node'] = {'id': node.id, 'name': node.name, 'location': node.location}
    return d


def decide_change(change, decision, user, reason=None, fault_id=None, note=''):
    """confirm: the parts on record become the new ones; reject: keep the record, mark the node."""
    if change.status != 'pending':
        raise HardwareError('decided')
    node = db.session.get(Node, change.node_id)
    now = datetime.utcnow()
    note = (note or '').strip()[:500]
    change.decided_by, change.decided_at, change.note = user, now, note or None
    if decision == 'reject':
        change.status = 'rejected'
        if node:
            node.hw_alert = True
        _event(change.node_id, change.node_name, 'change_rejected',
               'Unexpected hardware change, not confirmed: ' + change.summary + (f' ({note})' if note else ''),
               user=user, change_id=change.id)
        _feed(node, 'hardware_changed', 'rejected', change.summary)
        db.session.commit()
        webhook_service.emit('hardware.change_rejected', _change_payload(change, node))
        return change
    if decision != 'confirm':
        raise HardwareError('decision')
    if reason not in CHANGE_REASONS:
        raise HardwareError('reason')
    change.status, change.reason = 'confirmed', reason
    for item in change.items():
        old = HardwareComponent.query.filter_by(node_id=change.node_id, kind=item['kind'], removed_at=None).first()
        if old:
            old.removed_at, old.removed_reason = change.detected_at, reason
        moved = HardwareComponent.query.filter(
            HardwareComponent.kind == item['kind'], HardwareComponent.identifier == item['new'],
            HardwareComponent.removed_at.is_(None), HardwareComponent.node_id != change.node_id).all()
        for m in moved:                                   # a part taken from another node
            m.removed_at, m.removed_reason = change.detected_at, 'moved'
            _event(m.node_id, m.node_name, 'component_moved',
                   f"{KIND_LABELS[item['kind']]} moved to {change.node_name}", user=user)
        db.session.add(HardwareComponent(node_id=change.node_id, node_name=change.node_name, kind=item['kind'],
                                         identifier=item['new'], model=item.get('new_model'),
                                         installed_at=change.detected_at, installed_known=True, last_seen=now))
    if node:
        node.hw_alert = False
    _event(change.node_id, change.node_name, 'change_confirmed',
           f'{change.summary}: confirmed ({reason})' + (f', {note}' if note else ''), user=user, change_id=change.id)
    fault = None
    if reason == 'fault':
        first = change.items()[0]['kind'] if change.items() else 'board'
        if fault_id:
            fault = HardwareFault.query.filter_by(id=fault_id, node_id=change.node_id).first()
            if not fault or fault.status == 'resolved':
                raise HardwareError('fault_pick')
            _resolve(fault, 'replaced', user, note or change.summary, change.items()[0]['new'], now)
        else:                                             # record the failure, so the numbers count it
            fault = HardwareFault(node_id=change.node_id, node_name=change.node_name,
                                  component=KIND_TO_CATEGORY.get(first, 'other'), symptom=change.summary[:160],
                                  severity='normal', status='resolved', reported_by=user, reported_via='auto',
                                  created_at=change.detected_at, resolved_at=change.detected_at, resolved_by=user,
                                  resolution='replaced', resolution_note=note or None,
                                  new_serial=change.items()[0]['new'] if change.items() else None)
            db.session.add(fault)
            db.session.flush()
            _event(change.node_id, change.node_name, 'fault_resolved',
                   f'{category_label(fault.component)}: {fault.symptom} (recorded with the replacement)',
                   user=user, fault_id=fault.id)
    _feed(node, 'hardware_changed', 'confirmed', change.summary)
    db.session.commit()
    webhook_service.emit('hardware.change_confirmed', _change_payload(change, node))
    if fault is not None:
        webhook_service.emit('hardware.fault_resolved', fault_payload(fault))
    return change


# ------------------------------------------------------------------ faults

def fault_payload(f):
    d = f.to_dict()
    d['component_label'] = category_label(f.component)
    return d


def _parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        v = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        raise HardwareError('started_format')
    return v.replace(tzinfo=None) - (v.utcoffset() or timedelta(0)) if v.tzinfo else v


def report_fault(nodes, component, symptom, user, description='', severity='normal', started_at=None, via='web'):
    """One fault per node. Anyone signed in may report."""
    if not nodes:
        raise HardwareError('no_nodes')
    if len(nodes) > 500:
        raise HardwareError('too_many_nodes')
    if component not in category_keys():
        raise HardwareError('component')
    symptom = re.sub(r'[<>]', '', (symptom or '').strip())[:160]
    if not symptom:
        raise HardwareError('symptom')
    description = (description or '').strip()[:2000]
    if severity not in FAULT_SEVERITIES:
        raise HardwareError('severity')
    now = datetime.utcnow()
    started = _parse_time(started_at)
    if started and (started > now + timedelta(minutes=5) or started < now - timedelta(days=366)):
        raise HardwareError('started_range')
    faults = []
    for n in nodes:
        hint = component == 'power' and bool(n.hw_undervoltage_at and n.hw_undervoltage_at >= now - UNDERVOLTAGE_HINT_WINDOW)
        f = HardwareFault(node_id=n.id, node_name=n.name, component=component, symptom=symptom,
                          description=description or None, severity=severity, status='open', reported_by=user,
                          reported_via=via, started_at=started, created_at=now, updated_at=now,
                          undervoltage_hint=hint)
        db.session.add(f)
        db.session.flush()
        _event(n.id, n.name, 'fault_reported', f'{category_label(component)}: {symptom} ({severity})',
               user=user, fault_id=f.id)
        _feed(n, 'hardware_fault', component, symptom)
        faults.append(f)
    db.session.commit()
    for f in faults:
        webhook_service.emit('hardware.fault_reported', fault_payload(f))
    return faults


def _resolve(f, resolution, user, note, new_serial, now):
    if resolution not in RESOLUTIONS:
        raise HardwareError('resolution')
    f.status, f.resolution, f.resolved_at, f.resolved_by = 'resolved', resolution, now, user
    f.resolution_note = (note or '').strip()[:500] or None
    f.new_serial = hw_identity.clean(new_serial) or None
    f.downtime_min = downtime_minutes(f.node_id, f.started_at or f.created_at, now)
    f.updated_at = now
    if resolution == 'replaced' and f.component not in KIND_TO_CATEGORY.values():
        # parts EdgeShell cannot read (power supply, peripherals): keep their service life by hand
        old = HardwareComponent.query.filter_by(node_id=f.node_id, kind=f.component, removed_at=None).first()
        if old:
            old.removed_at, old.removed_reason = now, 'fault'
        db.session.add(HardwareComponent(node_id=f.node_id, node_name=f.node_name, kind=f.component,
                                         identifier=f.new_serial, model=None, source='manual', installed_at=now,
                                         installed_known=True, last_seen=now))
    _event(f.node_id, f.node_name, 'fault_resolved',
           f'{category_label(f.component)}: {resolution.replace("_", " ")}' + (f', {f.resolution_note}' if f.resolution_note else ''),
           user=user, fault_id=f.id)


def update_fault(f, action, user, resolution=None, note='', new_serial=None):
    """Operators and administrators: progress, note, resolve, reopen."""
    now = datetime.utcnow()
    note = re.sub(r'[<>]', '', (note or '').strip())[:500]
    event = 'hardware.fault_updated'
    if action == 'progress':
        if f.status != 'open':
            raise HardwareError('not_open')
        f.status = 'in_progress'
        _event(f.node_id, f.node_name, 'fault_in_progress', 'Work started' + (f': {note}' if note else ''),
               user=user, fault_id=f.id)
    elif action == 'note':
        if not note:
            raise HardwareError('note')
        _event(f.node_id, f.node_name, 'fault_note', note, user=user, fault_id=f.id)
    elif action == 'resolve':
        if f.status == 'resolved':
            raise HardwareError('resolved')
        _resolve(f, resolution, user, note, new_serial, now)
        event = 'hardware.fault_resolved'
    elif action == 'reopen':
        if f.status != 'resolved':
            raise HardwareError('not_resolved')
        f.status, f.resolved_at, f.resolved_by, f.resolution, f.downtime_min = 'open', None, None, None, None
        _event(f.node_id, f.node_name, 'fault_reopened', 'Reopened' + (f': {note}' if note else ''),
               user=user, fault_id=f.id)
    else:
        raise HardwareError('action')
    f.updated_at = now
    db.session.commit()
    webhook_service.emit(event, fault_payload(f))
    return f


def downtime_minutes(node_id, start, end):
    """Minutes the node was fully down (no SSH and no heartbeat) between start and end."""
    from sqlalchemy import func
    from app.models import MetricsHourly, SystemMetrics
    if not node_id or not start or not end or end <= start:
        return 0
    minutes = SystemMetrics.query.filter(
        SystemMetrics.node_id == node_id, SystemMetrics.timestamp >= start, SystemMetrics.timestamp < end,
        SystemMetrics.status == 'offline', SystemMetrics.heartbeat.isnot(True)).count()
    earliest = db.session.query(func.min(SystemMetrics.timestamp)).filter(SystemMetrics.node_id == node_id).scalar()
    if earliest is None or start < earliest:                       # older than the per-minute history
        until = min(end, earliest) if earliest else end
        for h in MetricsHourly.query.filter(MetricsHourly.node_id == node_id, MetricsHourly.hour >= start,
                                            MetricsHourly.hour < until):
            minutes += max(0, (h.offline or 0) - (h.heartbeat_only or 0))
    return int(minutes)


# ------------------------------------------------------------------ replacements

def lineage(node):
    """[node, the node it replaced, the one before...]"""
    chain, seen, cur = [node], {node.id}, node
    while cur.replaces_id and len(chain) < MAX_LINEAGE:
        prev = db.session.get(Node, cur.replaces_id)
        if prev is None or prev.id in seen:
            break
        chain.append(prev)
        seen.add(prev.id)
        cur = prev
    return chain


def candidates(node, limit=5):
    """Nodes this new one may replace, best first, with the reasons."""
    mine = {(c.kind, c.identifier) for c in HardwareComponent.query.filter_by(node_id=node.id, removed_at=None)}
    shared = {}
    if mine:
        for c in HardwareComponent.query.filter(HardwareComponent.node_id != node.id,
                                                HardwareComponent.removed_at.is_(None),
                                                HardwareComponent.identifier.in_([i for _, i in mine])):
            if (c.kind, c.identifier) in mine:
                shared.setdefault(c.node_id, []).append(KIND_LABELS.get(c.kind, c.kind))
    out = []
    others = Node.query.filter(Node.id != node.id, Node.is_deleted.isnot(True), Node.retired_at.is_(None),
                               Node.replaced_by_id.is_(None)).all()
    for o in others:
        score, why = 0.0, []
        if o.id in shared:
            score += 5
            why.append('same ' + ' and '.join(shared[o.id]).lower())
        if node.location and o.location and node.location.strip().lower() == o.location.strip().lower():
            score += 3
            why.append('same location')
        ratio = difflib.SequenceMatcher(None, node.name.lower(), o.name.lower()).ratio()
        if ratio >= 0.6:
            score += 3 * ratio
            why.append('similar name')
        if node.node_type and o.node_type and node.node_type.lower() == o.node_type.lower():
            score += 1
            why.append('same type')
        if o.status != 'online':
            score += 2
            why.append('not online' if o.status != 'auth_failed' else 'login failing')
        # a replaced node is down; in a numbered fleet every name is "similar", so an online
        # node is only suggested when it shares an actual part with the new one
        if score >= 3 and (o.id in shared or (o.status != 'online' and ('same location' in why or 'similar name' in why))):
            out.append({'id': o.id, 'name': o.name, 'location': o.location, 'node_type': o.node_type,
                        'status': o.status, 'last_seen': o.last_seen.isoformat(timespec='seconds') + 'Z'
                        if o.last_seen else None, 'score': round(score, 1), 'reasons': why})
    out.sort(key=lambda c: -c['score'])
    return out[:limit]


def link_replacement(new, old, user, failed=(), note=''):
    """`new` takes over from `old`: old is retired (kept with its history)."""
    if new.id == old.id:
        raise HardwareError('self')
    if old.retired_at or old.replaced_by_id:
        raise HardwareError('already_replaced')
    if new.retired_at or new.replaced_by_id:
        raise HardwareError('retired')
    if new.replaces_id:
        raise HardwareError('replaces_other')
    if new.id in {n.id for n in lineage(old)}:
        raise HardwareError('loop')
    keys = category_keys()
    failed = [f for f in dict.fromkeys(failed or []) if f in keys]
    note = re.sub(r'[<>]', '', (note or '').strip())[:500]
    now = datetime.utcnow()
    new.replaces_id, new.hw_replace_prompt = old.id, 'done'
    old.replaced_by_id, old.retired_at, old.is_active, old.status = new.id, now, False, 'retired'
    in_new = {(c.kind, c.identifier) for c in HardwareComponent.query.filter_by(node_id=new.id, removed_at=None)}
    failed_kinds = {k for k, cat in KIND_TO_CATEGORY.items() if cat in failed}
    for c in HardwareComponent.query.filter_by(node_id=old.id, removed_at=None):
        c.removed_at = now
        c.removed_reason = 'moved' if (c.kind, c.identifier) in in_new else (
            'fault' if c.kind in failed_kinds or c.kind in failed else 'retired')
    faults = []
    for cat in failed:
        f = HardwareFault(node_id=old.id, node_name=old.name, component=cat, symptom=f'Node replaced by {new.name}',
                          description=note or None, status='resolved', reported_by=user, reported_via='auto',
                          created_at=now, resolved_at=now, resolved_by=user, resolution='replaced')
        db.session.add(f)
        faults.append(f)
    db.session.flush()
    for f in faults:
        _event(old.id, old.name, 'fault_resolved', f'{category_label(f.component)}: failed, node replaced',
               user=user, fault_id=f.id)
    _event(old.id, old.name, 'node_replaced', f'Replaced by {new.name}' + (f': {note}' if note else ''), user=user)
    _event(new.id, new.name, 'node_replaces', f'Replaces {old.name}' + (f': {note}' if note else ''), user=user)
    _feed(old, 'node_replaced', new.name[:32], f'{old.name} replaced by {new.name}')
    db.session.commit()
    from app.services.collector import POOL
    POOL.drop(old.id)
    webhook_service.emit('node.replaced', {'old': {'id': old.id, 'name': old.name, 'location': old.location},
                                           'new': {'id': new.id, 'name': new.name, 'location': new.location},
                                           'failed': failed, 'note': note or None, 'by': user})
    return new


def dismiss_prompt(node, user):
    node.hw_replace_prompt = 'dismissed'
    _event(node.id, node.name, 'not_a_replacement', 'Marked as a new node, not a replacement', user=user)
    db.session.commit()


# ------------------------------------------------------------------ reading

def card_info(node_ids):
    """What the node cards show: open faults, a pending change, replacement links."""
    from sqlalchemy import func
    if not node_ids:
        return {}
    info = {nid: {'open_faults': 0, 'pending': None} for nid in node_ids}
    for nid, count in db.session.query(HardwareFault.node_id, func.count(HardwareFault.id)).filter(
            HardwareFault.node_id.in_(node_ids), HardwareFault.status != 'resolved').group_by(HardwareFault.node_id):
        info[nid]['open_faults'] = count
    for ch in HardwareChange.query.filter(HardwareChange.node_id.in_(node_ids), HardwareChange.status == 'pending'):
        info[ch.node_id]['pending'] = {'id': ch.id, 'summary': ch.summary}
    return info


def node_history(node):
    chain = lineage(node)
    ids = [n.id for n in chain]
    successor = db.session.get(Node, node.replaced_by_id) if node.replaced_by_id else None
    comps = HardwareComponent.query.filter(HardwareComponent.node_id.in_(ids)) \
        .order_by(HardwareComponent.installed_at.desc()).all()
    return {
        'node': {'id': node.id, 'name': node.name, 'location': node.location, 'model': node.hw_model,
                 'os': node.hw_os, 'scanned_at': _iso(node.hw_scanned_at), 'alert': bool(node.hw_alert),
                 'undervoltage_at': _iso(node.hw_undervoltage_at), 'retired_at': _iso(node.retired_at),
                 'replace_prompt': node.hw_replace_prompt},
        'lineage': [{'id': n.id, 'name': n.name, 'retired_at': _iso(n.retired_at)} for n in chain],
        'replaced_by': {'id': successor.id, 'name': successor.name} if successor else None,
        'components': [c.to_dict() for c in comps],
        'changes': [c.to_dict() for c in HardwareChange.query.filter(HardwareChange.node_id.in_(ids))
                    .order_by(HardwareChange.detected_at.desc()).limit(100)],
        'faults': [fault_payload(f) for f in HardwareFault.query.filter(HardwareFault.node_id.in_(ids))
                   .order_by(HardwareFault.created_at.desc()).limit(200)],
        'events': [e.to_dict() for e in HardwareEvent.query.filter(HardwareEvent.node_id.in_(ids))
                   .order_by(HardwareEvent.at.desc()).limit(300)],
        'categories': categories(),
    }


def fault_detail(f):
    d = fault_payload(f)
    d['timeline'] = [e.to_dict() for e in HardwareEvent.query.filter_by(fault_id=f.id).order_by(HardwareEvent.at)]
    return d


def _iso(t):
    return t.isoformat(timespec='seconds') + 'Z' if t else None


def fleet_nodes():
    return Node.query.filter(Node.is_deleted.isnot(True), Node.retired_at.is_(None))


def stats(days=90, now=None):
    """The fleet numbers (definitions in docs/HARDWARE.md)."""
    now = now or datetime.utcnow()
    since = now - timedelta(days=days)
    nodes = max(fleet_nodes().count(), 1)
    faults = HardwareFault.query.filter(HardwareFault.created_at >= since).all()
    last30 = [f for f in faults if f.created_at >= now - timedelta(days=30)]
    labels = {c['key']: c['label'] for c in categories(include_hidden=True)}
    by_component = {}
    for f in faults:
        by_component[f.component] = by_component.get(f.component, 0) + 1
    months = []
    for i in range(11, -1, -1):
        y, m = now.year, now.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        months.append(f'{y:04d}-{m:02d}')
    monthly = {m: {} for m in months}
    for f in HardwareFault.query.filter(HardwareFault.created_at >= datetime(int(months[0][:4]), int(months[0][5:]), 1)):
        key = f.created_at.strftime('%Y-%m')
        if key in monthly:
            monthly[key][f.component] = monthly[key].get(f.component, 0) + 1
    fixed = [f for f in faults if f.resolved_at and f.reported_via != 'auto']
    hours = sorted((f.resolved_at - (f.started_at or f.created_at)).total_seconds() / 3600 for f in fixed)
    life = {}
    for c in HardwareComponent.query.filter(HardwareComponent.removed_at.isnot(None),
                                            HardwareComponent.removed_reason == 'fault'):
        d = life.setdefault(c.kind, {'failed': 0, 'days': [], 'lower_bound': False})
        d['failed'] += 1
        d['days'].append((c.removed_at - c.installed_at).total_seconds() / 86400)
        d['lower_bound'] |= not c.installed_known
    in_service = {}
    for c in HardwareComponent.query.filter(HardwareComponent.removed_at.is_(None)):
        in_service.setdefault(c.kind, []).append((now - c.installed_at).total_seconds() / 86400)
    service_life = []
    for kind in sorted(set(life) | set(in_service)):
        d = life.get(kind, {'failed': 0, 'days': [], 'lower_bound': False})
        ages = sorted(in_service.get(kind, []))
        service_life.append({'kind': kind, 'label': labels.get(kind) or KIND_LABELS.get(kind, kind),
                             'failed': d['failed'],
                             'mean_days_to_failure': round(sum(d['days']) / len(d['days']), 1) if d['days'] else None,
                             'lower_bound': d['lower_bound'], 'in_service': len(ages),
                             'median_age_days': round(ages[len(ages) // 2], 1) if ages else None})
    per_node, per_location = {}, {}
    loc = {n.id: (n.location or '').strip() for n in Node.query.with_entities(Node.id, Node.location)}
    for f in faults:
        r = per_node.setdefault(f.node_name, {'node_id': f.node_id, 'node_name': f.node_name, 'faults': 0, 'open': 0})
        r['faults'] += 1
        r['open'] += f.status != 'resolved'
        place = loc.get(f.node_id) or 'unknown'
        per_location[place] = per_location.get(place, 0) + 1
    power = [f for f in faults if f.component == 'power']
    replacements = HardwareChange.query.filter(HardwareChange.status == 'confirmed',
                                               HardwareChange.decided_at >= since).count() + \
        HardwareEvent.query.filter(HardwareEvent.kind == 'node_replaced', HardwareEvent.at >= since).count()
    return {
        'days': days, 'nodes': nodes, 'generated_at': _iso(now),
        'open': HardwareFault.query.filter(HardwareFault.status == 'open').count(),
        'in_progress': HardwareFault.query.filter(HardwareFault.status == 'in_progress').count(),
        'faults': len(faults),
        'faults_per_100_nodes_30d': round(100.0 * len(last30) / nodes, 1),
        'by_component': [{'component': k, 'label': labels.get(k, k), 'faults': v}
                         for k, v in sorted(by_component.items(), key=lambda kv: -kv[1])],
        'monthly': [{'month': m, 'by_component': monthly[m], 'total': sum(monthly[m].values())} for m in months],
        'mean_hours_to_fix': round(sum(hours) / len(hours), 1) if hours else None,
        'median_hours_to_fix': round(hours[len(hours) // 2], 1) if hours else None,
        'downtime_hours': round(sum(f.downtime_min or 0 for f in fixed) / 60, 1),
        'replacements': replacements,
        'replacements_per_100_nodes': round(100.0 * replacements / nodes, 1),
        'service_life': service_life,
        'top_nodes': sorted(per_node.values(), key=lambda r: (-r['faults'], r['node_name']))[:10],
        'top_locations': [{'location': k, 'faults': v} for k, v in
                          sorted(per_location.items(), key=lambda kv: (-kv[1], kv[0]))[:10]],
        'power_faults': len(power),
        'power_flagged_by_undervoltage': sum(1 for f in power if f.undervoltage_hint),
        'pending_changes': HardwareChange.query.filter_by(status='pending').count(),
        'nodes_flagged': fleet_nodes().filter(Node.hw_alert.is_(True)).count(),
    }


def faults_query(status=None, component=None, node_id=None, since=None, q=None):
    query = HardwareFault.query
    if status == 'unresolved':
        query = query.filter(HardwareFault.status != 'resolved')
    elif status:
        query = query.filter(HardwareFault.status == status)
    if component:
        query = query.filter(HardwareFault.component == component)
    if node_id:
        query = query.filter(HardwareFault.node_id == node_id)
    if since:
        query = query.filter(HardwareFault.created_at >= since)
    if q:
        like = '%' + q.replace('%', '').replace('_', '')[:64] + '%'
        query = query.filter(db.or_(HardwareFault.node_name.like(like), HardwareFault.symptom.like(like)))
    return query.order_by(HardwareFault.created_at.desc())


def csv_cell(v):
    """A CSV cell a spreadsheet will not run as a formula (numbers are kept)."""
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'yes' if v else 'no'
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    if s[:1] in ('=', '+', '-', '@', '\t', '\r') and not re.fullmatch(r'[+-]?\d+(\.\d+)?', s):
        return "'" + s
    return s


def export_csv(what, days=365):
    """CSV text of faults, components or changes (formula-safe cells)."""
    since = datetime.utcnow() - timedelta(days=days)
    buf = io.StringIO()
    w = csv.writer(buf)
    if what == 'faults':
        cols = ['id', 'node_name', 'component', 'symptom', 'severity', 'status', 'reported_by', 'reported_via',
                'started_at', 'created_at', 'resolved_at', 'resolved_by', 'resolution', 'new_serial',
                'hours_to_fix', 'downtime_min', 'undervoltage_hint']
        rows = [fault_payload(f) for f in faults_query(since=since)]
    elif what == 'components':
        cols = ['id', 'node_name', 'kind', 'identifier', 'model', 'source', 'installed_at', 'installed_known',
                'removed_at', 'removed_reason', 'service_days']
        rows = [c.to_dict() for c in HardwareComponent.query.order_by(HardwareComponent.node_name,
                                                                      HardwareComponent.installed_at)]
    elif what == 'changes':
        cols = ['id', 'node_name', 'detected_at', 'status', 'summary', 'reason', 'decided_by', 'decided_at', 'note']
        rows = [c.to_dict() for c in HardwareChange.query.filter(HardwareChange.detected_at >= since)
                .order_by(HardwareChange.detected_at.desc())]
    else:
        raise HardwareError('export')
    w.writerow(cols)
    for r in rows:
        w.writerow([csv_cell(r.get(c)) for c in cols])
    return buf.getvalue()


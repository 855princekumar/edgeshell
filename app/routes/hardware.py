"""Hardware lifecycle pages and their JSON (ADR 0015).

Roles: everyone signed in sees the hardware history and reports faults;
operators and administrators move faults on, resolve them, decide detected
changes and link replacements; administrators manage the categories and
webhooks. The public API (/api/v1/hardware...) offers the same with tokens.
"""
from datetime import datetime

from flask import Blueprint, Response, jsonify, render_template, request
from flask_login import current_user, login_required

from app import db, limiter
from app.models.hardware import (CHANGE_REASONS, FAULT_SEVERITIES, RESOLUTIONS, HardwareChange, HardwareComponent,
                                 HardwareFault, Webhook, WebhookDelivery)
from app.models.node import Node
from app.services import hardware_service as hw
from app.services import webhook_service
from app.services.activity_service import log_activity
from app.services.rbac import admin_required, operator_required

hardware_bp = Blueprint('hardware', __name__, url_prefix='/hardware')


def _fail(message, code=400):
    return jsonify({'success': False, 'message': message}), code


def _node(node_id):
    return Node.query.filter(Node.id == node_id, Node.is_deleted.isnot(True)).first()


def _body():
    return request.get_json(silent=True) or {}


@hardware_bp.route('/')
@login_required
def index():
    return render_template('hardware/index.html', categories=hw.categories(),
                           severities=FAULT_SEVERITIES, resolutions=RESOLUTIONS, reasons=CHANGE_REASONS,
                           events=webhook_service.EVENTS,
                           nodes=hw.fleet_nodes().order_by(Node.name).with_entities(Node.id, Node.name).all())


# ------------------------------------------------------------------ reading

@hardware_bp.route('/api/stats')
@login_required
def stats():
    days = request.args.get('days', '90')
    days = int(days) if days in ('30', '90', '365') else 90
    return jsonify(hw.stats(days))


@hardware_bp.route('/api/faults')
@login_required
def faults():
    status = request.args.get('status') or None
    if status not in (None, 'open', 'in_progress', 'resolved', 'unresolved'):
        return _fail('Unknown status.')
    component = request.args.get('component') or None
    node_id = request.args.get('node_id', type=int)
    q = (request.args.get('q') or '').strip()[:64] or None
    page = max(1, min(request.args.get('page', 1, type=int), 10000))
    query = hw.faults_query(status, component, node_id, q=q)
    total = query.order_by(None).count()
    rows = query.offset((page - 1) * 50).limit(50).all()
    return jsonify({'faults': [hw.fault_payload(f) for f in rows], 'total': total, 'page': page,
                    'pages': (total + 49) // 50})


@hardware_bp.route('/api/faults/<int:fault_id>')
@login_required
def fault(fault_id):
    f = db.session.get(HardwareFault, fault_id)
    return jsonify(hw.fault_detail(f)) if f else _fail('No such fault.', 404)


@hardware_bp.route('/api/changes')
@login_required
def changes():
    status = request.args.get('status') or 'pending'
    if status not in ('pending', 'confirmed', 'rejected', 'reverted', 'all'):
        return _fail('Unknown status.')
    q = HardwareChange.query
    if status != 'all':
        q = q.filter(HardwareChange.status == status)
    return jsonify({'changes': [c.to_dict() for c in q.order_by(HardwareChange.detected_at.desc()).limit(200)]})


@hardware_bp.route('/api/inventory')
@login_required
def inventory():
    kind = request.args.get('kind') or None
    q = HardwareComponent.query.filter(HardwareComponent.removed_at.is_(None))
    if kind:
        q = q.filter(HardwareComponent.kind == kind)
    rows = q.order_by(HardwareComponent.node_name, HardwareComponent.kind).limit(5000).all()
    return jsonify({'components': [c.to_dict() for c in rows]})


@hardware_bp.route('/api/node/<int:node_id>')
@login_required
def node_history(node_id):
    n = db.session.get(Node, node_id)              # retired and binned nodes keep their history
    if n is None:
        return _fail('No such node.', 404)
    data = hw.node_history(n)
    if current_user.can_execute() and n.hw_replace_prompt == 'pending':
        data['candidates'] = hw.candidates(n)
    return jsonify(data)


@hardware_bp.route('/export.csv')
@login_required
def export():
    what = request.args.get('what', 'faults')
    try:
        text = hw.export_csv(what)
    except hw.HardwareError as e:
        return _fail(str(e))
    stamp = datetime.utcnow().strftime('%Y%m%d-%H%M')
    return Response(text, mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=hardware-{what}-{stamp}.csv',
                             'Cache-Control': 'no-store'})


# ------------------------------------------------------------------ reporting (everyone)

@hardware_bp.route('/api/report', methods=['POST'])
@login_required
@limiter.limit('30 per minute')
def report():
    d = _body()
    ids = d.get('node_ids') or []
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        return _fail('node_ids must be a list of node ids.')
    nodes = hw.fleet_nodes().filter(Node.id.in_(ids[:500])).all() if ids else []
    if len(nodes) != len(set(ids)):
        return _fail('Some nodes do not exist (or are retired).')
    try:
        faults = hw.report_fault(nodes, d.get('component'), d.get('symptom'), current_user.username,
                                 description=d.get('description') or '', severity=d.get('severity') or 'normal',
                                 started_at=d.get('started_at') or None)
    except hw.HardwareError as e:
        return _fail(str(e))
    log_activity('Report hardware fault', node=', '.join(n.name for n in nodes)[:64],
                 response=f"{d.get('component')}: {faults[0].symptom}")
    return jsonify({'success': True, 'faults': [f.id for f in faults]})


# ------------------------------------------------------------------ operators

@hardware_bp.route('/api/faults/<int:fault_id>', methods=['POST'])
@login_required
@operator_required
def fault_update(fault_id):
    f = db.session.get(HardwareFault, fault_id)
    if f is None:
        return _fail('No such fault.', 404)
    d = _body()
    try:
        hw.update_fault(f, d.get('action'), current_user.username, resolution=d.get('resolution'),
                        note=d.get('note') or '', new_serial=d.get('new_serial'))
    except hw.HardwareError as e:
        return _fail(str(e))
    log_activity('Hardware fault ' + str(d.get('action')), node=f.node_name, response=f'fault {f.id}: {f.status}')
    return jsonify({'success': True, 'fault': hw.fault_detail(f)})


@hardware_bp.route('/api/changes/<int:change_id>', methods=['POST'])
@login_required
@operator_required
def change_decide(change_id):
    ch = db.session.get(HardwareChange, change_id)
    if ch is None:
        return _fail('No such change.', 404)
    d = _body()
    try:
        hw.decide_change(ch, d.get('decision'), current_user.username, reason=d.get('reason'),
                         fault_id=d.get('fault_id') or None, note=d.get('note') or '')
    except hw.HardwareError as e:
        return _fail(str(e))
    log_activity('Hardware change ' + ch.status, node=ch.node_name, response=ch.summary)
    return jsonify({'success': True, 'change': ch.to_dict()})


@hardware_bp.route('/api/node/<int:node_id>/candidates')
@login_required
@operator_required
def candidates(node_id):
    n = _node(node_id)
    return jsonify({'candidates': hw.candidates(n)}) if n else _fail('No such node.', 404)


@hardware_bp.route('/api/node/<int:node_id>/replaces', methods=['POST'])
@login_required
@operator_required
def replaces(node_id):
    n = _node(node_id)
    d = _body()
    old = _node(d.get('replaces_node_id')) if isinstance(d.get('replaces_node_id'), int) else None
    if n is None or old is None:
        return _fail('Choose the node this one replaces.', 404)
    failed = d.get('failed') or []
    if not isinstance(failed, list):
        return _fail('failed must be a list of categories.')
    try:
        hw.link_replacement(n, old, current_user.username, failed=failed, note=d.get('note') or '')
    except hw.HardwareError as e:
        return _fail(str(e))
    log_activity('Replace node', node=n.name, response=f'replaces {old.name}; failed: {", ".join(failed) or "-"}')
    return jsonify({'success': True})


@hardware_bp.route('/api/node/<int:node_id>/not-replacement', methods=['POST'])
@login_required
@operator_required
def not_replacement(node_id):
    n = _node(node_id)
    if n is None:
        return _fail('No such node.', 404)
    hw.dismiss_prompt(n, current_user.username)
    return jsonify({'success': True})


# ------------------------------------------------------------------ administrators

@hardware_bp.route('/api/categories', methods=['GET', 'POST'])
@login_required
def categories():
    if request.method == 'GET':
        return jsonify({'categories': hw.categories(include_hidden=current_user.is_admin())})
    if not current_user.is_admin():
        return _fail('Administrators only.', 403)
    try:
        cats = hw.save_categories(_body().get('categories'))
    except hw.HardwareError as e:
        return _fail(str(e))
    log_activity('Hardware categories saved', response=', '.join(c['key'] for c in cats)[:500])
    return jsonify({'success': True, 'categories': cats})


@hardware_bp.route('/api/webhooks')
@login_required
@admin_required
def webhooks():
    return jsonify({'webhooks': [h.to_dict() for h in Webhook.query.order_by(Webhook.id)],
                    'events': list(webhook_service.EVENTS)})


@hardware_bp.route('/api/webhooks', methods=['POST'])
@login_required
@admin_required
@limiter.limit('20 per hour')
def webhook_create():
    d = _body()
    try:
        h, secret = webhook_service.create(d.get('name'), d.get('url'), d.get('events'), current_user.username)
    except ValueError as e:
        return _fail(str(e))
    log_activity('Add webhook', response=f'{h.name}: {h.url} ({h.events})')
    return jsonify({'success': True, 'webhook': h.to_dict(), 'secret': secret})


@hardware_bp.route('/api/webhooks/<int:hook_id>', methods=['POST'])
@login_required
@admin_required
def webhook_change(hook_id):
    h = db.session.get(Webhook, hook_id)
    if h is None:
        return _fail('No such webhook.', 404)
    action = _body().get('action')
    if action == 'delete':
        WebhookDelivery.query.filter_by(webhook_id=h.id).delete()
        db.session.delete(h)
        db.session.commit()
        log_activity('Delete webhook', response=h.name)
        return jsonify({'success': True})
    if action in ('enable', 'disable'):
        h.active, h.failures = action == 'enable', 0
        db.session.commit()
        log_activity(action.capitalize() + ' webhook', response=h.name)
        return jsonify({'success': True, 'webhook': h.to_dict()})
    if action == 'test':
        d = webhook_service.test(h)
        return jsonify({'success': bool(d and d.status_code and 200 <= d.status_code < 300),
                        'delivery': d.to_dict() if d else None})
    if action == 'deliveries':
        rows = WebhookDelivery.query.filter_by(webhook_id=h.id).order_by(WebhookDelivery.id.desc()).limit(50)
        return jsonify({'deliveries': [r.to_dict() for r in rows]})
    return _fail('Unknown action.')


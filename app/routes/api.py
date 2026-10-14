"""EdgeShell public API, version 1 (/api/v1). Reference: docs/API.md and
GET /api/v1/openapi.json (generated from the route declarations below, so the
document always matches the code).

Every route is declared with `endpoint(...)`: its permission, query
parameters (types, limits, allowed values) and JSON body schema. Requests are
validated before the handler runs; unknown body fields are refused. Data is
read and written through SQLAlchemy with bound parameters only.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import jsonschema
from flask import Blueprint, current_app, g, jsonify, request, send_file
from flask_login import current_user

from app import db, limiter
from app.models.node import Node
from app.services import api_auth
from app.services.activity_service import log_activity

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')

ROUTES = []          # (method, path, meta) for the OpenAPI document
NAME_RE = r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
HOST_RE = r'^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$'


# ------------------------------------------------------------------ framework

class Param:
    def __init__(self, name, kind='string', desc='', required=False, enum=None, minimum=None,
                 maximum=None, default=None, max_length=200, pattern=None):
        self.name, self.kind, self.desc, self.required = name, kind, desc, required
        self.enum, self.minimum, self.maximum, self.default = enum, minimum, maximum, default
        self.max_length, self.pattern = max_length, pattern

    def parse(self, raw):
        if raw is None or raw == '':
            if self.required:
                raise ValueError('is required')
            return self.default
        if len(raw) > self.max_length:
            raise ValueError(f'is longer than {self.max_length} characters')
        if self.kind == 'integer':
            if not re.fullmatch(r'-?\d{1,12}', raw):
                raise ValueError('must be an integer')
            v = int(raw)
            if self.minimum is not None and v < self.minimum or self.maximum is not None and v > self.maximum:
                raise ValueError(f'must be between {self.minimum} and {self.maximum}')
            return v
        if self.kind == 'datetime':
            try:
                v = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            except ValueError:
                raise ValueError('must be an ISO 8601 date and time, e.g. 2026-10-14T08:00:00Z')
            return v.replace(tzinfo=None) - (v.utcoffset() or timedelta(0)) if v.tzinfo else v
        if self.kind == 'integer_list':
            parts = [p for p in raw.split(',') if p]
            if len(parts) > 500 or not all(re.fullmatch(r'\d{1,10}', p) for p in parts):
                raise ValueError('must be up to 500 comma-separated integers')
            return [int(p) for p in parts]
        if self.enum and raw not in self.enum:
            raise ValueError(f'must be one of: {", ".join(self.enum)}')
        if self.pattern and not re.fullmatch(self.pattern, raw):
            raise ValueError('has characters that are not allowed')
        return raw

    def openapi(self, where='query'):
        schema = {'integer': {'type': 'integer'}, 'datetime': {'type': 'string', 'format': 'date-time'},
                  'integer_list': {'type': 'string', 'description': 'comma-separated integers'}}.get(
                      self.kind, {'type': 'string', 'maxLength': self.max_length})
        if self.enum:
            schema['enum'] = list(self.enum)
        if self.minimum is not None:
            schema['minimum'] = self.minimum
        if self.maximum is not None:
            schema['maximum'] = self.maximum
        if self.default is not None:
            schema['default'] = self.default
        return {'name': self.name, 'in': where, 'required': self.required or where == 'path',
                'description': self.desc, 'schema': schema}


PAGE = [Param('page', 'integer', 'Page number, from 1', minimum=1, maximum=100000, default=1),
        Param('per_page', 'integer', 'Items per page', minimum=1, maximum=500, default=100)]


def endpoint(method, path, summary, scope, params=(), body=None, auth=True, tag='General',
             rate=None, description=''):
    """Declare an API route: permission, validation and its OpenAPI entry."""
    def deco(fn):
        flask_path = re.sub(r'\{(\w+)\}', r'<int:\1>', path)

        def view(**kw):
            args = {}
            errors = {}
            for p in params:
                try:
                    args[p.name] = p.parse(request.args.get(p.name))
                except ValueError as e:
                    errors[p.name] = str(e)
            unknown = set(request.args) - {p.name for p in params}
            if unknown:
                errors.update({k: 'is not a known parameter' for k in sorted(unknown)[:10]})
            data = None
            if body is not None:
                if not request.is_json:
                    return api_auth.api_error(415, 'unsupported_media_type', 'Send a JSON body with '
                                              'Content-Type: application/json.')
                data = request.get_json(silent=True)
                if data is None:
                    return api_auth.api_error(400, 'invalid_json', 'The body is not valid JSON.')
                try:
                    jsonschema.validate(data, body)
                except jsonschema.ValidationError as e:
                    where = '.'.join(str(x) for x in e.absolute_path) or 'body'
                    errors[where] = e.message[:200]
            if errors:
                return api_auth.api_error(400, 'validation_error', 'Some values are not valid.', details=errors)
            return fn(args, data, **kw)

        view.__name__ = fn.__name__
        guarded = api_auth.requires(scope)(view) if auth else view
        if rate:
            guarded = limiter.limit(rate, key_func=api_auth.rate_key)(guarded)
        api_bp.add_url_rule(flask_path, endpoint=fn.__name__, view_func=guarded, methods=[method])
        ROUTES.append((method, path, {'summary': summary, 'scope': scope if auth else None,
                                      'params': params, 'body': body, 'tag': tag,
                                      'description': description, 'rate': rate}))
        return fn
    return deco


def page_of(query, args):
    total = query.order_by(None).count()
    items = query.offset((args['page'] - 1) * args['per_page']).limit(args['per_page']).all()
    return items, {'page': args['page'], 'per_page': args['per_page'], 'total': total,
                   'pages': (total + args['per_page'] - 1) // args['per_page']}


def audit(action, node=None, response=None, status='success'):
    log_activity(action, node=node, response=((response or '') + f' [{api_auth.actor_label()}]')[:500],
                 status=status)


def active_nodes():
    return Node.query.filter(Node.is_deleted.isnot(True))


def node_or_404(node_id):
    n = active_nodes().filter(Node.id == node_id).first()
    if n is None:
        return None, api_auth.api_error(404, 'not_found', 'No such node.')
    return n, None


# ----------------------------------------------------------- authentication

CRED = {'type': 'object', 'additionalProperties': False, 'required': ['username', 'password'],
        'properties': {'username': {'type': 'string', 'minLength': 1, 'maxLength': 64},
                       'password': {'type': 'string', 'minLength': 1, 'maxLength': 256}}}
VERIFY = {'type': 'object', 'additionalProperties': False, 'required': ['challenge', 'code'],
          'properties': {'challenge': {'type': 'string', 'minLength': 20, 'maxLength': 200},
                         'code': {'type': 'string', 'minLength': 6, 'maxLength': 32}}}


@endpoint('POST', '/auth/login', 'Sign in, step 1: username and password', None, body=CRED, auth=False,
          tag='Authentication', rate='10 per minute;60 per hour',
          description='Returns a short-lived `challenge` when two-factor authentication is on (always in '
                      'production); send it with the code to /auth/verify. Wrong passwords count toward '
                      'the account lockout, and every failure gives the same answer.')
def auth_login(args, data):
    from app.models.user import User
    from app.routes.auth import _burn_password_check, _locked, _record_failure
    from app.services import mfa_service
    user = User.query.filter_by(username=data['username'].strip()).first()
    bad = api_auth.api_error(401, 'invalid_credentials', 'Invalid username or password.')
    if user is None:
        _burn_password_check(data['password'])
        return bad
    if _locked(user):
        return api_auth.api_error(423, 'locked', 'Too many failed attempts. Try again later.')
    if not user.check_password(data['password']):
        _record_failure(user)
        return bad
    if not user.is_active or user.is_pending():
        return bad
    if mfa_service.mfa_required() and not mfa_service.is_enrolled(user):
        return api_auth.api_error(403, 'mfa_setup_required', 'Set up two-factor authentication in the web '
                                  'app first.')
    if mfa_service.mfa_required():
        row, challenge = api_auth.issue(user, 'challenge', scopes=(), expires_at=datetime.utcnow() + api_auth.CHALLENGE_TTL)
        db.session.commit()
        return jsonify({'mfa_required': True, 'challenge': challenge,
                        'expires_in': int(api_auth.CHALLENGE_TTL.total_seconds())})
    return _start_session(user, via='password')


@endpoint('POST', '/auth/verify', 'Sign in, step 2: the 2FA code', None, body=VERIFY, auth=False,
          tag='Authentication', rate='10 per minute;60 per hour',
          description='Exchanges the challenge and a 6-digit authenticator code (or a backup code) for an '
                      'API session token. A challenge allows 3 attempts; wrong codes also count toward '
                      'the account lockout.')
def auth_verify(args, data):
    from app.routes.auth import _locked, _record_failure
    from app.services import mfa_service
    row = api_auth.lookup(data['challenge'], kind='challenge')
    bad = api_auth.api_error(401, 'invalid_code', 'The code or the challenge is not valid.')
    if row is None:
        return bad
    user = row.user
    if _locked(user):
        api_auth.revoke(row, 'locked')
        db.session.commit()
        return api_auth.api_error(423, 'locked', 'Too many failed attempts. Try again later.')
    try:
        ok = mfa_service.verify(user, data['code'])
    except mfa_service.MFAUnavailable:
        return api_auth.api_error(503, 'mfa_unavailable', 'The two-factor service is not responding.')
    if not ok:
        row.attempts = (row.attempts or 0) + 1
        if row.attempts >= api_auth.CHALLENGE_ATTEMPTS:
            api_auth.revoke(row, 'attempts')
        _record_failure(user, '2FA code (API)')
        return bad
    api_auth.revoke(row, 'used')
    return _start_session(user, via='password + 2FA')


def _start_session(user, via):
    from app.models.api import ApiToken
    hours = int(current_app.config.get('API_SESSION_MAX_HOURS', 12))
    live = (ApiToken.query.filter(ApiToken.user_id == user.id, ApiToken.kind == 'session',
                                  ApiToken.revoked_at.is_(None), ApiToken.expires_at > datetime.utcnow())
            .order_by(ApiToken.created_at).all())
    for old in live[:max(0, len(live) - api_auth.MAX_SESSIONS_PER_USER + 1)]:
        api_auth.revoke(old, 'replaced')
    row, token = api_auth.issue(user, 'session', scopes=api_auth.ROLE_SCOPES.get(user.role, {'read'}),
                                expires_at=datetime.utcnow() + timedelta(hours=hours))
    user.failed_logins = 0
    db.session.commit()
    log_activity(f'API sign-in: {user.username}', status='success', response=f'{via}, session {row.prefix}')
    return jsonify({'token': token, 'token_type': 'Bearer', 'expires_at': row.public()['expires_at'],
                    'idle_timeout_minutes': int(current_app.config.get('API_SESSION_IDLE_MINUTES', 60)),
                    'scopes': sorted(row.scope_set())})


@endpoint('POST', '/auth/logout', 'End this API session', 'read', tag='Authentication')
def auth_logout(args, data):
    row = getattr(g, 'api_token', None)
    if row is None or row.kind != 'session':
        return api_auth.api_error(400, 'not_a_session', 'Only an API session token can be logged out; '
                                  'revoke a service key with DELETE /api/v1/keys/{id}.')
    api_auth.revoke(row, 'logout')
    db.session.commit()
    return jsonify({'success': True})


@endpoint('GET', '/auth/whoami', 'Who this credential is and what it may do', 'read', tag='Authentication')
def auth_whoami(args, data):
    row = getattr(g, 'api_token', None)
    return jsonify({'username': current_user.username, 'role': current_user.role,
                    'credential': row.public() if row else {'kind': 'browser_session'},
                    'scopes': sorted(api_auth.effective_scopes(current_user, row))})


# ------------------------------------------------------------- service keys

KEY_CREATE = {'type': 'object', 'additionalProperties': False,
              'required': ['name', 'scopes', 'password', 'code'],
              'properties': {
                  'name': {'type': 'string', 'minLength': 1, 'maxLength': 80, 'pattern': r'^[^<>]+$'},
                  'scopes': {'type': 'array', 'minItems': 1, 'maxItems': 4, 'uniqueItems': True,
                             'items': {'enum': list(api_auth.SCOPES)}},
                  'expires_in_days': {'type': 'integer', 'minimum': 1, 'maximum': 365},
                  'allowed_ips': {'type': 'string', 'maxLength': 500},
                  'password': {'type': 'string', 'minLength': 1, 'maxLength': 256},
                  'code': {'type': 'string', 'minLength': 6, 'maxLength': 32}}}


@endpoint('GET', '/keys', 'Your service keys (administrators: all keys)', 'read', tag='Service keys',
          params=[Param('all', 'string', 'Administrators: "true" lists every user\'s keys', enum=('true', 'false'))])
def keys_list(args, data):
    from app.models.api import ApiToken
    q = ApiToken.query.filter(ApiToken.kind == 'key')
    if not (args['all'] == 'true' and 'admin' in api_auth.effective_scopes(current_user, getattr(g, 'api_token', None))):
        q = q.filter(ApiToken.user_id == current_user.id)
    return jsonify({'keys': [k.public() for k in q.order_by(ApiToken.created_at.desc()).limit(500)]})


@endpoint('POST', '/keys', 'Create a service key (needs your password and a 2FA code)', 'read',
          body=KEY_CREATE, tag='Service keys', rate='10 per hour',
          description='For unattended integrations. Needs a signed-in API session (not another key) plus '
                      'your password and a current 2FA code, so a stolen session alone cannot mint keys. '
                      'Scopes cannot exceed your role. The key is returned once; only its hash is kept.')
def keys_create(args, data):
    from app.models.api import ApiToken
    from app.routes.auth import _record_failure
    from app.services import mfa_service
    row = getattr(g, 'api_token', None)
    if row is None or row.kind != 'session':
        return api_auth.api_error(403, 'session_required', 'Create keys from an API session (or the web '
                                  'app), not with another key.')
    if not current_user.check_password(data['password']):
        _record_failure(current_user, 'password (API key creation)')
        return api_auth.api_error(401, 'invalid_credentials', 'Password or code is not valid.')
    try:
        if mfa_service.mfa_required() and not mfa_service.verify(current_user, data['code']):
            _record_failure(current_user, '2FA code (API key creation)')
            return api_auth.api_error(401, 'invalid_credentials', 'Password or code is not valid.')
    except mfa_service.MFAUnavailable:
        return api_auth.api_error(503, 'mfa_unavailable', 'The two-factor service is not responding.')
    allowed = api_auth.effective_scopes(current_user)
    if not set(data['scopes']) <= allowed:
        return api_auth.api_error(403, 'forbidden', 'A key cannot have more permission than your role: '
                                  + ', '.join(sorted(allowed)))
    ips, err = api_auth.parse_ip_list(data.get('allowed_ips'))
    if err:
        return api_auth.api_error(400, 'validation_error', 'Some values are not valid.', details={'allowed_ips': err})
    live = ApiToken.query.filter(ApiToken.user_id == current_user.id, ApiToken.kind == 'key',
                                 ApiToken.revoked_at.is_(None), ApiToken.expires_at > datetime.utcnow()).count()
    if live >= api_auth.MAX_KEYS_PER_USER:
        return api_auth.api_error(409, 'too_many_keys', f'At most {api_auth.MAX_KEYS_PER_USER} active keys '
                                  'per user. Revoke one first.')
    days = data.get('expires_in_days', 90)
    key, plaintext = api_auth.issue(current_user, 'key', scopes=data['scopes'], name=data['name'].strip(),
                                    expires_at=datetime.utcnow() + timedelta(days=days), allowed_ips=ips)
    db.session.commit()
    audit('API key created', response=f'{key.prefix} "{key.name}" scopes {key.scopes}, {days} days'
          + (f', from {ips}' if ips else ''))
    return jsonify({'key': plaintext, 'details': key.public(),
                    'notice': 'Store the key now; it is not shown again.'}), 201


@endpoint('DELETE', '/keys/{key_id}', 'Revoke a service key', 'read', tag='Service keys',
          params=(), description='Your own keys; administrators may revoke any key.')
def keys_revoke(args, data, key_id):
    from app.models.api import ApiToken
    key = ApiToken.query.filter(ApiToken.id == key_id, ApiToken.kind == 'key').first()
    is_admin = 'admin' in api_auth.effective_scopes(current_user, getattr(g, 'api_token', None))
    if key is None or (key.user_id != current_user.id and not is_admin):
        return api_auth.api_error(404, 'not_found', 'No such key.')
    api_auth.revoke(key, 'revoked')
    db.session.commit()
    audit('API key revoked', response=f'{key.prefix} "{key.name}"')
    return jsonify({'success': True})


# ------------------------------------------------------------------- health

@endpoint('GET', '/health', 'Liveness and readiness (no sign-in needed)', None, auth=False, tag='Health',
          rate='120 per minute',
          description='Answers `ok` when the database responds and the metrics collector runs, else '
                      '`degraded` (HTTP 503). Reveals nothing else.')
def health(args, data):
    from app.services.collector import COLLECTOR
    try:
        db.session.execute(db.text('select 1'))
        database = True
    except Exception:
        database = False
    st = COLLECTOR.status() if hasattr(COLLECTOR, 'status') else {}
    collector_ok = bool(st.get('running')) or bool(current_app.config.get('TESTING'))
    ok = database and collector_ok
    return jsonify({'status': 'ok' if ok else 'degraded'}), (200 if ok else 503)


@endpoint('GET', '/health/details', 'Detailed health for monitoring systems', 'read', tag='Health')
def health_details(args, data):
    from app.services.collector import COLLECTOR
    st = COLLECTOR.status()
    return jsonify({'version': current_app.config.get('VERSION', 'dev'), 'database': db.engine.dialect.name,
                    'collector': {k: st.get(k) for k in ('running', 'nodes', 'ok', 'auth', 'unreachable',
                                                          'error', 'seconds', 'pooled_connections')},
                    'time': datetime.utcnow().isoformat(timespec='seconds') + 'Z'})


# ------------------------------------------------------------- fleet reads

NODE_FILTERS = [Param('status', 'string', 'Filter by status',
                      enum=('online', 'offline', 'auth_failed', 'unknown')),
                Param('category', 'string', 'Device category name', max_length=40, pattern=r'[^<>]{1,40}'),
                Param('search', 'string', 'Part of the name, address or location', max_length=64,
                      pattern=r'[A-Za-z0-9 ._:-]{1,64}')]


def _filtered_nodes(args):
    from app.services import categories as cats
    q = active_nodes()
    if args.get('status'):
        q = q.filter(Node.status == args['status'])
    if args.get('search'):
        like = '%' + args['search'].replace('%', '').replace('_', r'\_') + '%'
        q = q.filter(db.or_(Node.name.like(like), Node.ip_address.like(like), Node.location.like(like)))
    if args.get('category'):
        categories = cats.load()
        match = [c for c in categories if c['name'] == args['category']]
        if args['category'] == 'Other':
            known = {t for c in categories for t in c['types']}
            q = q.filter(db.or_(Node.node_type.is_(None), db.func.lower(Node.node_type).notin_(known)))
        elif match:
            q = q.filter(db.func.lower(Node.node_type).in_(match[0]['types']))
        else:
            q = q.filter(db.false())
    return q.order_by(Node.name)


@endpoint('GET', '/nodes', 'List nodes', 'read', params=NODE_FILTERS + PAGE, tag='Nodes')
def nodes_list(args, data):
    from app.services import categories as cats
    categories = cats.load()
    items, page = page_of(_filtered_nodes(args), args)
    return jsonify({'nodes': [dict(n.to_dict(), category=cats.category_of(n.node_type, categories)) for n in items],
                    'pagination': page})


@endpoint('GET', '/nodes/{node_id}', 'One node', 'read', tag='Nodes')
def nodes_get(args, data, node_id):
    n, err = node_or_404(node_id)
    return err or jsonify(n.to_dict())


@endpoint('GET', '/categories', 'Device categories and their SLA targets', 'read', tag='Nodes')
def categories(args, data):
    from app.services import categories as cats
    return jsonify({'categories': cats.load()})


@endpoint('GET', '/fleet/summary', 'Fleet totals by status and category', 'read', tag='Data points')
def fleet_summary(args, data):
    from app.services import categories as cats
    categories = cats.load()
    out, totals = {}, {'total': 0, 'online': 0, 'offline': 0, 'auth_failed': 0, 'unknown': 0}
    for n in active_nodes().filter(Node.is_active.is_(True)):
        c = cats.category_of(n.node_type, categories)
        row = out.setdefault(c, {'total': 0, 'online': 0, 'offline': 0, 'auth_failed': 0, 'unknown': 0})
        st = n.status if n.status in totals else 'unknown'
        for d in (row, totals):
            d['total'] += 1
            d[st] += 1
    for d in list(out.values()) + [totals]:
        d['availability_percent'] = round(d['online'] / d['total'] * 100, 2) if d['total'] else None
    targets = {c['name']: c.get('sla_target') for c in categories}
    return jsonify({'fleet': totals, 'categories': [dict(v, name=k, sla_target=targets.get(k)) for k, v in sorted(out.items())],
                    'time': datetime.utcnow().isoformat(timespec='seconds') + 'Z'})


METRIC_KEYS = ('cpu_percent', 'mem_percent', 'disk_percent', 'temperature', 'load_1', 'load_5', 'load_15',
               'uptime_seconds', 'net_rx_bytes', 'net_tx_bytes', 'docker_running', 'nodered_running',
               'orgui_running')


@endpoint('GET', '/metrics/latest', 'Latest data point of each node', 'read', tag='Data points',
          params=NODE_FILTERS + [Param('node_ids', 'integer_list', 'Only these nodes')] + PAGE,
          description='One data point per node from the background collector (every 30 s): status and '
                      'every metric, with the time it was collected. Poll this to drive thresholds and '
                      'alerts in another system.')
def metrics_latest(args, data):
    from app.services.collector import COLLECTOR
    q = _filtered_nodes(args)
    if args.get('node_ids'):
        q = q.filter(Node.id.in_(args['node_ids']))
    items, page = page_of(q, args)
    points = []
    for n in items:
        r = COLLECTOR.latest.get(n.id) or {}
        m = r.get('metrics') if r.get('ok') else None
        points.append({'node_id': n.id, 'node_name': n.name, 'status': n.status,
                       'collected_at': (r['at'].isoformat(timespec='seconds') + 'Z') if r.get('at') else None,
                       'metrics': {k: m.get(k) for k in METRIC_KEYS} if m else
                       {'cpu_percent': n.cpu_percent, 'mem_percent': n.ram_percent, 'disk_percent': n.disk_percent,
                        'temperature': n.temperature},
                       'error': None if m else (n.last_error or None)})
    return jsonify({'points': points, 'pagination': page})


@endpoint('GET', '/metrics/history', 'Time series of one node', 'read', tag='Data points',
          params=[Param('node_id', 'integer', 'Node', required=True, minimum=1, maximum=10 ** 9),
                  Param('from', 'datetime', 'Start (UTC, ISO 8601); default 24 hours ago'),
                  Param('to', 'datetime', 'End (UTC, ISO 8601); default now'),
                  Param('interval', 'string', 'raw: every sample (within the raw retention); hour or day: '
                        'rollups', enum=('raw', 'hour', 'day'), default='hour')],
          description='At most 10000 points per request; narrow the range for more.')
def metrics_history(args, data):
    n, err = node_or_404(args['node_id'])
    if err:
        return err
    end = args['to'] or datetime.utcnow()
    start = args['from'] or end - timedelta(hours=24)
    if start >= end:
        return api_auth.api_error(400, 'validation_error', 'Some values are not valid.',
                                  details={'from': 'must be before "to"'})
    from app.services import lake
    with lake.reader() as r:
        return _history(r, n, start, end, args)


def _history(r, n, start, end, args):
    from app.models import MetricsHourly, SystemMetrics
    if args['interval'] == 'raw':
        rows = (r.session.query(SystemMetrics).filter(SystemMetrics.node_id == n.id, SystemMetrics.timestamp >= start,
                                           SystemMetrics.timestamp < end)
                .order_by(SystemMetrics.timestamp).limit(10000).all())
        pts = [{'t': r.timestamp.isoformat(timespec='seconds') + 'Z', 'status': r.status or 'online',
                'cpu_percent': r.cpu_percent, 'mem_percent': r.ram_percent, 'disk_percent': r.disk_percent,
                'temperature': r.temperature, 'load_1': r.load_avg_1, 'uptime_seconds': r.uptime_seconds}
               for r in rows]
    else:
        rows = (r.session.query(MetricsHourly).filter(MetricsHourly.node_id == n.id, MetricsHourly.hour >= start,
                                           MetricsHourly.hour < end).order_by(MetricsHourly.hour).limit(10000).all())
        if args['interval'] == 'day':
            from app.services.history import _merge
            days = {}
            for r in rows:
                days.setdefault(r.hour.replace(hour=0), []).append(r)
            pts = [dict(_merge(rs), t=d.isoformat(timespec='seconds') + 'Z') for d, rs in sorted(days.items())]
        else:
            pts = [{'t': r.hour.isoformat(timespec='seconds') + 'Z', 'samples': r.samples,
                    'uptime_percent': round(r.online / r.samples * 100, 2) if r.samples else None,
                    'cpu_avg': r.cpu_avg, 'cpu_max': r.cpu_max, 'mem_avg': r.ram_avg, 'mem_max': r.ram_max,
                    'disk_avg': r.disk_avg, 'disk_max': r.disk_max, 'temp_avg': r.temp_avg, 'temp_max': r.temp_max,
                    'load1_avg': r.load1_avg, 'load1_max': r.load1_max, 'reboots': r.reboots}
                   for r in rows]
    return jsonify({'node_id': n.id, 'node_name': n.name, 'interval': args['interval'], 'source': r.source,
                    'from': start.isoformat(timespec='seconds') + 'Z', 'to': end.isoformat(timespec='seconds') + 'Z',
                    'points': pts, 'truncated': len(pts) >= 10000})


@endpoint('GET', '/events', 'Event feed for alerts and triggers (poll with a cursor)', 'read', tag='Data points',
          params=[Param('since_id', 'integer', 'Return events after this id (the cursor)', minimum=0,
                        maximum=10 ** 12, default=0),
                  Param('kind', 'string', 'Only this kind',
                        enum=('status_changed', 'login_paused', 'login_resumed')),
                  Param('node_id', 'integer', 'Only this node', minimum=1, maximum=10 ** 9),
                  Param('limit', 'integer', 'At most this many', minimum=1, maximum=1000, default=200)],
          description='Status changes (online, offline, login failed) and paused or resumed logins, oldest '
                      'first. Keep the last `id` you saw and pass it as `since_id` next time; nothing is '
                      'missed or repeated.')
def events(args, data):
    from app.models.api import NodeEvent
    from app.services import lake
    with lake.reader() as r:
        q = r.session.query(NodeEvent).filter(NodeEvent.id > args['since_id'])
        if args.get('kind'):
            q = q.filter(NodeEvent.kind == args['kind'])
        if args.get('node_id'):
            q = q.filter(NodeEvent.node_id == args['node_id'])
        rows = q.order_by(NodeEvent.id).limit(args['limit']).all()
        return jsonify({'events': [e.to_dict() for e in rows], 'source': r.source,
                        'next_since_id': rows[-1].id if rows else args['since_id']})


@endpoint('GET', '/activity', 'Activity log (audit trail)', 'read', params=PAGE, tag='Data points')
def activity(args, data):
    from app.models import ActivityLog
    from app.services import lake
    with lake.reader() as r:
        items, page = page_of(r.session.query(ActivityLog).order_by(ActivityLog.id.desc()), args)
        return jsonify({'activity': [a.to_dict() for a in items], 'pagination': page, 'source': r.source})


@endpoint('GET', '/status', 'Platform status', 'read', tag='Health')
def status(args, data):
    nodes = active_nodes().filter(Node.is_active.is_(True))
    return jsonify({'platform': 'EdgeShell', 'version': current_app.config.get('VERSION', 'dev'),
                    'nodes': {'total': nodes.count(), 'online': nodes.filter(Node.status == 'online').count()}})


# ----------------------------------------------------------- fleet changes

NODE_FIELDS = {
    'name': {'type': 'string', 'pattern': NAME_RE},
    'ip_address': {'type': 'string', 'pattern': HOST_RE},
    'ssh_port': {'type': 'integer', 'minimum': 1, 'maximum': 65535},
    'username': {'type': 'string', 'minLength': 1, 'maxLength': 64, 'pattern': r'^[A-Za-z0-9._-]+$'},
    'password': {'type': 'string', 'minLength': 1, 'maxLength': 256},
    'node_type': {'type': 'string', 'maxLength': 32, 'pattern': r'^[A-Za-z0-9._ -]*$'},
    'environment': {'type': 'string', 'maxLength': 32, 'pattern': r'^[A-Za-z0-9._ -]*$'},
    'location': {'type': 'string', 'maxLength': 128, 'pattern': r'^[^<>]*$'},
    'tags': {'type': 'string', 'maxLength': 256, 'pattern': r'^[^<>]*$'},
    'description': {'type': 'string', 'maxLength': 500, 'pattern': r'^[^<>]*$'},
}
NODE_CREATE = {'type': 'object', 'additionalProperties': False, 'properties': NODE_FIELDS,
               'required': ['name', 'ip_address', 'username', 'password']}
NODE_PATCH = {'type': 'object', 'additionalProperties': False, 'properties': NODE_FIELDS, 'minProperties': 1}


def _apply(n, data):
    for k, v in data.items():
        if k == 'password':
            n.password = v
        else:
            setattr(n, k, v.strip() if isinstance(v, str) else v)


@endpoint('POST', '/nodes', 'Add a node', 'write', body=NODE_CREATE, tag='Nodes', rate='120 per minute',
          description='Its SSH login is checked in the background; read the node to see the result.')
def nodes_create(args, data):
    if Node.query.filter_by(name=data['name']).first():
        return api_auth.api_error(409, 'conflict', 'A node with this name exists (it may be in the Recycle Bin).')
    n = Node(status='unknown')
    _apply(n, dict(data, ssh_port=data.get('ssh_port', 22)))
    db.session.add(n)
    db.session.commit()
    from app.services.collector import COLLECTOR
    COLLECTOR.kick([n.id])
    audit('Add Node', node=n.name)
    return jsonify(n.to_dict()), 201


@endpoint('PATCH', '/nodes/{node_id}', 'Change a node', 'write', body=NODE_PATCH, tag='Nodes', rate='120 per minute',
          description='Only the fields sent change. New login details clear a paused login and are checked once.')
def nodes_patch(args, data, node_id):
    n, err = node_or_404(node_id)
    if err:
        return err
    if 'name' in data and data['name'] != n.name and Node.query.filter_by(name=data['name']).first():
        return api_auth.api_error(409, 'conflict', 'A node with this name exists.')
    login_changed = bool({'ip_address', 'ssh_port', 'username', 'password'} & set(data))
    _apply(n, data)
    if login_changed:
        from app.services import login_policy
        login_policy.resume(n)
    n.updated_at = datetime.utcnow()
    db.session.commit()
    if login_changed:
        from app.services.collector import COLLECTOR, POOL
        POOL.drop(n.id)
        COLLECTOR.kick([n.id])
    audit('Edit Node', node=n.name, response='fields: ' + ', '.join(sorted(k for k in data if k != 'password'))
          + (' and password' if 'password' in data else ''))
    return jsonify(n.to_dict())


@endpoint('DELETE', '/nodes/{node_id}', 'Move a node to the Recycle Bin', 'write', tag='Nodes', rate='120 per minute')
def nodes_delete(args, data, node_id):
    n, err = node_or_404(node_id)
    if err:
        return err
    from app.services.collector import POOL
    n.is_deleted, n.deleted_at, n.is_active = True, datetime.utcnow(), False
    POOL.drop(n.id)
    db.session.commit()
    audit('Delete Node (recycle bin)', node=n.name)
    return jsonify({'success': True})


BULK_LOGIN = {'type': 'object', 'additionalProperties': False, 'required': ['node_ids'], 'minProperties': 2,
              'properties': {'node_ids': {'type': 'array', 'minItems': 1, 'maxItems': 5000, 'uniqueItems': True,
                                          'items': {'type': 'integer', 'minimum': 1}},
                             'username': NODE_FIELDS['username'], 'password': NODE_FIELDS['password'],
                             'ssh_port': NODE_FIELDS['ssh_port']}}


@endpoint('POST', '/nodes/bulk/credentials', 'Set the login of many nodes', 'write', body=BULK_LOGIN,
          tag='Nodes', rate='20 per minute', description='Returns a job; each node is checked once.')
def nodes_bulk_credentials(args, data):
    from app.routes.nodes import _check_logins
    from app.services import jobs, login_policy
    from app.services.collector import POOL
    nodes = active_nodes().filter(Node.id.in_(data['node_ids'])).all()
    for n in nodes:
        _apply(n, {k: data[k] for k in ('username', 'password', 'ssh_port') if k in data})
        login_policy.resume(n)
        POOL.drop(n.id)
    db.session.commit()
    jid = jobs.run_batches('login-check', f'Checking the new login on {len(nodes)} node(s)', current_user.id,
                           [n.id for n in nodes], _check_logins)
    audit('Bulk login update', response=', '.join(sorted(k for k in data if k not in ('node_ids', 'password')))
          + (' and password' if 'password' in data else '') + f' on {len(nodes)} node(s)')
    return jsonify({'job_id': jid, 'count': len(nodes)}), 202


# ------------------------------------------------------- commands and jobs

COMMAND = {'type': 'object', 'additionalProperties': False, 'required': ['node_ids', 'command'],
           'properties': {'node_ids': {'type': 'array', 'minItems': 1, 'maxItems': 200, 'uniqueItems': True,
                                       'items': {'type': 'integer', 'minimum': 1}},
                          'command': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
                          'timeout': {'type': 'integer', 'minimum': 5, 'maximum': 300},
                          'sudo': {'type': 'boolean'}}}


@endpoint('POST', '/commands', 'Run a shell command on nodes', 'control', body=COMMAND, tag='Commands',
          rate='10 per minute',
          description='Off unless the server sets API_ALLOW_COMMANDS=true. Runs on online nodes only (others '
                      'are listed as skipped), in parallel, and returns every result. Blocked commands '
                      '(Settings, Security) are refused. Every call is in the activity log.')
def commands_run(args, data):
    from app.services import collector, command_policy
    if not current_app.config.get('API_ALLOW_COMMANDS'):
        return api_auth.api_error(403, 'commands_disabled', 'Running commands through the API is turned off on '
                                  'this server (API_ALLOW_COMMANDS).')
    blocked = command_policy.blocked(data['command'])
    if blocked:
        audit('API command refused', response=f'blocked: {blocked}', status='warning')
        return api_auth.api_error(403, 'command_blocked', f'Command blocked by policy: {blocked}')
    nodes = active_nodes().filter(Node.id.in_(data['node_ids']), Node.is_active.is_(True)).all()
    run_on = [n for n in nodes if n.status == 'online' and n.login_paused_at is None]
    skipped = [{'node_id': n.id, 'node_name': n.name, 'reason': n.status or 'unknown'} for n in nodes if n not in run_on]
    targets = [collector.target_for(n) for n in run_on]
    timeout = data.get('timeout', 60)

    def one(t):
        r = collector.run_command(t, data['command'], timeout=timeout,
                                  sudo_password=t['password'] if data.get('sudo') else None)
        return {'node_id': t['id'], 'node_name': t['name'], 'exit_code': r.get('exit_code'),
                'success': bool(r.get('success')), 'output': (r.get('stdout') or '')[-20000:],
                'error': r.get('error'), 'seconds': r.get('duration')}
    with ThreadPoolExecutor(max_workers=max(1, min(16, len(targets) or 1))) as pool:
        results = list(pool.map(one, targets))
    ok = sum(1 for r in results if r['success'])
    audit('API command', response=f'{data["command"][:200]!r} on {len(results)} node(s): {ok} ok, '
          f'{len(results) - ok} failed, {len(skipped)} skipped')
    return jsonify({'results': results, 'skipped': skipped})


EXPORT = {'type': 'object', 'additionalProperties': False, 'required': ['period'],
          'properties': {'period': {'enum': ['day', 'week', 'month', 'quarter', 'semester', 'year']},
                         'grain': {'enum': ['daily', 'hourly', 'summary']},
                         'category': {'type': 'string', 'maxLength': 40, 'pattern': r'^[^<>]*$'}}}


@endpoint('POST', '/exports', 'Start a CSV stats export', 'write', body=EXPORT, tag='Jobs', rate='20 per hour',
          description='Poll GET /api/v1/jobs/{id} (string id); when done, download from '
                      '/api/v1/exports/{id}/download.')
def exports_start(args, data):
    from app.services import history, jobs
    owner = current_user.id
    period, grain, category = data['period'], data.get('grain', 'daily'), (data.get('category') or '').strip()
    jid = jobs.run_task('stats-export', f'Stats export: {period}, {category or "all categories"}', owner, 100,
                        lambda t: history.export_task(t, period, grain, category, owner))
    audit('Stats export started', response=f'{period}, {grain}, {category or "all"}')
    return jsonify({'job_id': jid}), 202


# Job ids are short hex strings, so these two routes take a string id.
def _job(job_id):
    from app.services import jobs
    if not re.fullmatch(r'[0-9a-f]{6,32}', job_id or ''):
        return None
    return jobs.get(job_id, current_user.id)


@api_bp.route('/jobs/<job_id>', methods=['GET'])
@api_auth.requires('read')
def job_get_str(job_id):
    j = _job(job_id)
    return jsonify(j) if j else api_auth.api_error(404, 'not_found', 'No such job.')


@api_bp.route('/exports/<job_id>/download', methods=['GET'])
@api_auth.requires('read')
def export_download(job_id):
    from app.services import history
    j = _job(job_id)
    res = (j or {}).get('result') or {}
    path = history.export_path(res.get('download'), current_user.id)
    if not path:
        return api_auth.api_error(404, 'not_found', 'No such export (exports are kept for an hour).')
    return send_file(path, mimetype='text/csv', as_attachment=True, download_name=res['filename'])


ROUTES.append(('GET', '/jobs/{job_id}', {'summary': 'Progress of a background job', 'scope': 'read',
                                         'params': (), 'body': None, 'tag': 'Jobs', 'rate': None, 'string_id': True,
                                         'description': 'Jobs started by this user: imports, login checks, exports.'}))
ROUTES.append(('GET', '/exports/{job_id}/download', {'summary': 'Download a finished CSV export', 'scope': 'read',
                                                     'params': (), 'body': None, 'tag': 'Jobs', 'rate': None,
                                                     'string_id': True, 'description': 'text/csv'}))


# ------------------------------------------------------------ documentation

@api_bp.route('/openapi.json')
def openapi():
    from app.services.api_spec import build
    return jsonify(build(ROUTES, current_app.config.get('VERSION', 'dev')))

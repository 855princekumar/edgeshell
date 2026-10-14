"""Authentication and authorization for the public API (/api/v1).

Credentials (see app/models/api.py) look like  es<k>_<prefix>_<secret>:
  esc_  sign-in challenge (between password and 2FA code)
  est_  API session token
  esk_  service key
Only a SHA-256 hash of the secret is stored; lookups use the prefix and the
comparison is constant-time. A request authenticates with
`Authorization: Bearer <token>`.

Permissions are the user's role intersected with the credential's scopes:
  read     monitor and above: read nodes, metrics, events, jobs
  write    operator and above: add, edit, delete nodes, bulk changes, exports
  control  operator and above: run commands on nodes (also needs API_ALLOW_COMMANDS)
  admin    administrators: users and every key
A browser session cookie may only read: any change through the API needs a
bearer credential, so the API is not exposed to cross-site request forgery.
"""
import hashlib
import hmac
import ipaddress
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import current_app, g, jsonify, request
from flask_login import current_user

from app import db

PREFIX = {'challenge': 'esc', 'session': 'est', 'key': 'esk'}
SCOPES = ('read', 'write', 'control', 'admin')
ROLE_SCOPES = {'monitor': {'read'}, 'operator': {'read', 'write', 'control'},
               'administrator': {'read', 'write', 'control', 'admin'}}
MAX_KEYS_PER_USER = 10
MAX_SESSIONS_PER_USER = 5
CHALLENGE_TTL = timedelta(minutes=5)
CHALLENGE_ATTEMPTS = 3


POLICY_KEY = 'api_policy'
DEFAULT_POLICY = {'enabled': True, 'scopes': ['read', 'write', 'control'], 'keys': True}


def policy():
    """The administrators' API policy (Administration, API Access)."""
    import json
    from app.models import Settings
    row = Settings.query.filter_by(key=POLICY_KEY).first()
    p = dict(DEFAULT_POLICY)
    try:
        if row and row.value:
            saved = json.loads(row.value)
            p.update({k: saved[k] for k in DEFAULT_POLICY if k in saved})
    except ValueError:
        pass
    p['scopes'] = [s for s in SCOPES if s in (p.get('scopes') or [])]
    return p


def save_policy(enabled, scopes, keys):
    import json
    from app.models import Settings
    value = json.dumps({'enabled': bool(enabled), 'scopes': [s for s in SCOPES if s in scopes], 'keys': bool(keys)})
    row = Settings.query.filter_by(key=POLICY_KEY).first()
    if row:
        row.value = value
    else:
        db.session.add(Settings(key=POLICY_KEY, value=value))
    db.session.commit()


def api_admin():
    """True when this API request may act on other users' credentials: the
    credential carries the admin permission and the policy has it turned on."""
    return ('admin' in effective_scopes(current_user, getattr(g, 'api_token', None))
            and 'admin' in policy()['scopes'])


def refusal(user=None, scope=None):
    """An error response when the policy or the account blocks this, else None."""
    p = policy()
    if not p['enabled']:
        return api_error(403, 'api_disabled', 'The API is turned off on this server by an administrator.')
    if user is not None and getattr(user, 'api_access', True) is False:
        return api_error(403, 'api_blocked', 'API access is blocked for this account by an administrator.')
    if scope and scope not in p['scopes']:
        return api_error(403, 'scope_disabled', f'The "{scope}" part of the API is turned off by an administrator.')
    return None


def _hash(secret):
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


def client_ip():
    return request.remote_addr or ''


def issue(user, kind, scopes=('read',), name=None, expires_at=None, allowed_ips=None):
    """Create a credential and return (row, plaintext). The plaintext is shown once."""
    from app.models.api import ApiToken
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    row = ApiToken(user_id=user.id, kind=kind, name=name, prefix=prefix, secret_hash=_hash(secret),
                   scopes=','.join(s for s in SCOPES if s in set(scopes)),
                   allowed_ips=allowed_ips, created_ip=client_ip(), expires_at=expires_at)
    db.session.add(row)
    db.session.flush()
    return row, f'{PREFIX[kind]}_{prefix}_{secret}'


def lookup(plaintext, kind=None):
    """The live credential for a plaintext token, or None (constant-time compare)."""
    from app.models.api import ApiToken
    try:
        tag, prefix, secret = (plaintext or '').strip().split('_', 2)
    except ValueError:
        return None
    if tag not in PREFIX.values() or not (8 <= len(prefix) <= 16) or len(secret) < 20 or len(secret) > 100:
        return None
    row = ApiToken.query.filter_by(prefix=prefix).first()
    if row is None or PREFIX.get(row.kind) != tag or (kind and row.kind != kind):
        _hash(secret)                                  # same work either way
        return None
    if not hmac.compare_digest(row.secret_hash, _hash(secret)):
        return None
    if row.revoked_at is not None or row.expires_at <= datetime.utcnow():
        return None
    return row


def revoke(row, reason):
    if row.revoked_at is None:
        row.revoked_at = datetime.utcnow()
        row.revoked_reason = reason


def revoke_all_for(user, reason, kinds=('session', 'key', 'challenge')):
    """End every API credential of a user (password change, reset, disable)."""
    from app.models.api import ApiToken
    for row in ApiToken.query.filter(ApiToken.user_id == user.id, ApiToken.kind.in_(kinds),
                                     ApiToken.revoked_at.is_(None)):
        revoke(row, reason)


def _ip_allowed(row, ip):
    if not row.allowed_ips:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in row.allowed_ips.split(','):
        try:
            if addr in ipaddress.ip_network(net.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def parse_ip_list(text):
    """Validate a comma-separated list of IPs/CIDRs; returns (normalized, error)."""
    if not text:
        return None, None
    nets = []
    for part in str(text).split(','):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(str(ipaddress.ip_network(part, strict=False)))
        except ValueError:
            return None, f'"{part}" is not an IP address or network'
    if len(nets) > 20:
        return None, 'at most 20 networks'
    return ','.join(nets) or None, None


def effective_scopes(user, row=None):
    allowed = set(ROLE_SCOPES.get((user.role or '').lower(), set()))
    if row is not None:
        allowed &= row.scope_set()
    return allowed


def authenticate_bearer():
    """Called for /api requests: resolve `Authorization: Bearer`. Returns the
    user (and sets g.api_token) or None. A bad bearer is an error, never a
    silent fall back to the cookie."""
    header = request.headers.get('Authorization', '')
    if not header:
        return None
    g.api_bearer_present = True
    scheme, _, token = header.partition(' ')
    if scheme.lower() != 'bearer' or not token:
        return None
    row = lookup(token)
    if row is None or row.kind == 'challenge':
        return None
    user = row.user
    if user is None or not user.is_active or user.is_pending():
        return None
    now = datetime.utcnow()
    if row.kind == 'session':
        idle = timedelta(minutes=int(current_app.config.get('API_SESSION_IDLE_MINUTES', 60)))
        if row.last_used_at and now - row.last_used_at > idle:
            revoke(row, 'idle')
            db.session.commit()
            return None
    if not _ip_allowed(row, client_ip()):
        g.api_ip_refused = True
        return None
    if not row.last_used_at or now - row.last_used_at > timedelta(seconds=30) or row.last_used_ip != client_ip():
        row.last_used_at = now
        row.last_used_ip = client_ip()
        db.session.commit()
    g.api_token = row
    return user


def api_error(status, code, message, **extra):
    body = {'error': code, 'message': message}
    body.update(extra)
    resp = jsonify(body)
    resp.status_code = status
    return resp


def requires(scope):
    """Route guard: signed in (bearer or, for reads, the browser session) and
    the scope allowed by both the role and the credential."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if getattr(g, 'api_ip_refused', False):
                return api_error(403, 'ip_not_allowed', 'This key may not be used from this address.')
            if not current_user.is_authenticated:
                if getattr(g, 'api_bearer_present', False):
                    return api_error(401, 'invalid_token', 'The token is missing, wrong, expired or revoked.')
                return api_error(401, 'unauthorized', 'Sign in: POST /api/v1/auth/login, then send '
                                 'Authorization: Bearer <token>.')
            row = getattr(g, 'api_token', None)
            if row is None and request.method not in ('GET', 'HEAD'):
                return api_error(403, 'bearer_required', 'Changes through the API need a bearer token '
                                 '(a browser session can only read).')
            if scope not in effective_scopes(current_user, row):
                return api_error(403, 'forbidden', f'This needs the "{scope}" permission, which this '
                                 'account or credential does not have.')
            if current_user.must_change_password:
                return api_error(403, 'action_required', 'Set a new password in the web app first.')
            blocked = refusal(current_user, scope)
            if blocked:
                return blocked
            g.api_scope = scope
            return fn(*a, **kw)
        wrapper.api_scope = scope
        return wrapper
    return deco


def rate_limit():
    """The limit for this request: API_RATE_LIMIT if set, else by role."""
    cfg = current_app.config
    if cfg.get('API_RATE_LIMIT'):
        return cfg['API_RATE_LIMIT']
    role = (current_user.role or '').lower() if current_user.is_authenticated else ''
    return (cfg.get('API_RATE_LIMITS') or {}).get(role, '120 per minute')


def rate_key():
    """Limit by credential when there is one, else by client address."""
    row = getattr(g, 'api_token', None)
    return f'tok:{row.id}' if row is not None else f'ip:{client_ip()}'


def actor_label():
    row = getattr(g, 'api_token', None)
    if row is None:
        return 'API (browser session)'
    return f'API {row.kind} {row.prefix}' + (f' ({row.name})' if row.name else '')

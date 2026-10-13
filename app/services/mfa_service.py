"""Two-factor authentication: an authenticator app (TOTP) plus one-time backup
codes, mandatory for every account.

Who checks the codes
  privacyidea  The Docker stack runs privacyIDEA (https://privacyidea.org) in
               its own container on the internal network. EdgeShell enrolls a
               TOTP token and a TAN list (the backup codes) per user and sends
               every code there to be checked. Token secrets live only in
               privacyIDEA, encrypted with its own key; EdgeShell keeps the
               token serials.
  local        Installs without that container (run.py, single container,
               tests). The TOTP secret is encrypted with CREDENTIAL_KEY and
               backup codes are stored as SHA-256 hashes.

The provider is chosen by MFA_PROVIDER (default: privacyidea when
PRIVACYIDEA_URL is set, otherwise local). A user enrolled under another
provider simply enrolls again at next sign-in.
"""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from datetime import datetime
from urllib.parse import quote, urlencode

import requests
from flask import current_app

from app import db
from app.models.user import UserMFA

TOTP_STEP = 30
TOTP_DIGITS = 6
BACKUP_CODES = 10
_BACKUP_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'   # no 0/o, 1/l/i


class MFAUnavailable(Exception):
    """The verification service could not be reached. Sign-in fails closed."""


# ---------------------------------------------------------------- helpers

def provider_name():
    name = (current_app.config.get('MFA_PROVIDER') or '').strip().lower()
    if name in ('privacyidea', 'local'):
        return name
    return 'privacyidea' if current_app.config.get('PRIVACYIDEA_URL') else 'local'


def mfa_required():
    return bool(current_app.config.get('MFA_REQUIRED', True))


def totp_now(secret_b32, at=None, step_offset=0):
    key = base64.b32decode(secret_b32 + '=' * (-len(secret_b32) % 8), casefold=True)
    counter = int((at or time.time()) // TOTP_STEP) + step_offset
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    code = (struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** TOTP_DIGITS
    return str(code).zfill(TOTP_DIGITS)


def otpauth_uri(secret_b32, account):
    issuer = current_app.config.get('MFA_ISSUER', 'EdgeShell')
    label = quote(f'{issuer}:{account}')
    query = urlencode({'secret': secret_b32, 'issuer': issuer, 'algorithm': 'SHA1',
                       'digits': TOTP_DIGITS, 'period': TOTP_STEP})
    return f'otpauth://totp/{label}?{query}'


def qr_svg(data):
    """Inline SVG QR code for the enrollment page (segno, pure Python)."""
    import segno
    return segno.make(data, error='m').svg_inline(scale=5, border=2,
                                                 dark='#111827', light='#ffffff')


def new_backup_codes(n=BACKUP_CODES):
    """'xxxxx-xxxxx' codes, about 49 bits each."""
    return ['-'.join(''.join(secrets.choice(_BACKUP_ALPHABET) for _ in range(5))
                     for _ in range(2)) for _ in range(n)]


def normalize_code(code):
    return ''.join(ch for ch in (code or '').lower() if ch.isalnum())


def _is_totp(code):
    return len(code) == TOTP_DIGITS and code.isdigit()


def _hash_code(code):
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


# ---------------------------------------------------------------- privacyIDEA

class PrivacyIDEA:
    """Minimal REST client: /auth, /token/init, /validate/check, /token/."""

    _token = None
    _token_at = 0.0
    JWT_REFRESH_S = 1800

    def __init__(self, cfg):
        self.url = (cfg.get('PRIVACYIDEA_URL') or '').rstrip('/')
        self.admin = cfg.get('PRIVACYIDEA_ADMIN') or 'edgeshell'
        self.password = cfg.get('PRIVACYIDEA_PASSWORD') or ''
        self.timeout = float(cfg.get('PRIVACYIDEA_TIMEOUT', 8))
        self.verify_tls = bool(cfg.get('PRIVACYIDEA_VERIFY_TLS', True))

    def _call(self, method, path, data=None, auth=True, retry=True):
        headers = {}
        if auth:
            headers['Authorization'] = self._jwt()
        try:
            r = requests.request(method, self.url + path, timeout=self.timeout,
                                 verify=self.verify_tls, headers=headers,
                                 data=data if method != 'GET' else None,
                                 params=data if method == 'GET' else None)
        except requests.RequestException as e:
            raise MFAUnavailable(f'privacyIDEA unreachable: {e.__class__.__name__}') from e
        if r.status_code == 401 and auth and retry:
            PrivacyIDEA._token = None          # JWT expired or service restarted
            return self._call(method, path, data, auth, retry=False)
        try:
            body = r.json()
        except ValueError:
            raise MFAUnavailable(f'privacyIDEA returned HTTP {r.status_code}')
        result = body.get('result') or {}
        if r.status_code >= 500 or (not result.get('status') and path != '/validate/check'):
            err = (result.get('error') or {}).get('message') or f'HTTP {r.status_code}'
            raise MFAUnavailable(f'privacyIDEA error: {err}')
        return body

    def _jwt(self):
        cls = PrivacyIDEA
        if cls._token and time.time() - cls._token_at < self.JWT_REFRESH_S:
            return cls._token
        body = self._call('POST', '/auth', {'username': self.admin,
                                            'password': self.password}, auth=False)
        token = ((body.get('result') or {}).get('value') or {}).get('token')
        if not token:
            raise MFAUnavailable('privacyIDEA rejected the service account')
        cls._token, cls._token_at = token, time.time()
        return token

    def init_totp(self, serial, description):
        body = self._call('POST', '/token/init', {
            'type': 'totp', 'genkey': 1, 'serial': serial, 'description': description,
            'otplen': TOTP_DIGITS, 'timeStep': TOTP_STEP, 'hashlib': 'sha1'})
        uri = ((body.get('detail') or {}).get('googleurl') or {}).get('value', '')
        from urllib.parse import parse_qs, urlparse
        secret = (parse_qs(urlparse(uri).query).get('secret') or [''])[0]
        if not secret:
            raise MFAUnavailable('privacyIDEA did not return a TOTP secret')
        return secret

    def init_tan(self, serial, description, codes):
        self._call('POST', '/token/init', {
            'type': 'tan', 'serial': serial, 'description': description,
            'tans': ' '.join(normalize_code(c) for c in codes)})

    def check(self, serial, code):
        body = self._call('POST', '/validate/check', {'serial': serial, 'pass': code},
                          auth=False)
        result = body.get('result') or {}
        message = (body.get('detail') or {}).get('message', '')
        if result.get('status') is False:
            # No such token (deleted) counts as a wrong code, not an outage.
            return False, message or (result.get('error') or {}).get('message', '')
        return bool(result.get('value')), message

    def remaining_tans(self, serial):
        body = self._call('GET', '/token/', {'serial': serial})
        tokens = ((body.get('result') or {}).get('value') or {}).get('tokens') or []
        if not tokens:
            return 0
        try:
            return int((tokens[0].get('info') or {}).get('tan.count', 0))
        except (TypeError, ValueError):
            return 0

    def delete(self, serial):
        try:
            self._call('DELETE', f'/token/{quote(serial, safe="")}')
        except MFAUnavailable as e:
            if 'No token with serial' not in str(e) and 'not found' not in str(e).lower():
                raise


def _pi():
    return PrivacyIDEA(current_app.config)


# ---------------------------------------------------------------- public API

def enrollment(user):
    """The user's MFA row if it belongs to the active provider, else None."""
    row = db.session.get(UserMFA, user.id)
    if row and row.provider != provider_name():
        return None
    return row


def is_enrolled(user):
    row = enrollment(user)
    return bool(row and row.confirmed_at)


def begin_enrollment(user):
    """Create a fresh authenticator secret (dropping any unfinished or old
    enrollment). Returns (otpauth_uri, secret_b32). Commit is the caller's."""
    remove(user)
    provider = provider_name()
    if provider == 'privacyidea':
        serial = f'ES{user.id}T{secrets.token_hex(4).upper()}'
        secret = _pi().init_totp(serial, f'EdgeShell {user.username}')
        ref = serial
    else:
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
        from app.services.crypto import encrypt_value
        ref = encrypt_value(secret)
    db.session.add(UserMFA(user_id=user.id, provider=provider, totp_ref=ref))
    db.session.flush()
    return otpauth_uri(secret, user.username), secret


def confirm_enrollment(user, code):
    """First code from the app. On success issue backup codes (shown once)."""
    row = enrollment(user)
    if not row or row.confirmed_at:
        return None
    if not _check_totp(row, normalize_code(code)):
        return None
    codes = _issue_backup_codes(user, row)
    row.confirmed_at = datetime.utcnow()
    return codes


def regenerate_backup_codes(user):
    row = enrollment(user)
    if not row or not row.confirmed_at:
        return None
    return _issue_backup_codes(user, row)


def _issue_backup_codes(user, row):
    codes = new_backup_codes()
    if row.provider == 'privacyidea':
        pi = _pi()
        serial = f'ES{user.id}B{secrets.token_hex(4).upper()}'
        pi.init_tan(serial, f'EdgeShell {user.username} backup codes', codes)
        old, row.backup_ref = row.backup_ref, serial
        if old:
            pi.delete(old)
    else:
        row.backup_ref = json.dumps([_hash_code(c) for c in codes])
    row.backup_total = len(codes)
    return codes


def verify(user, code):
    """Check a sign-in code: 6 digits = authenticator, otherwise a backup code.
    Returns 'totp', 'backup' or None. Raises MFAUnavailable when the
    verification service is down (the caller must refuse the sign-in)."""
    row = enrollment(user)
    if not row or not row.confirmed_at:
        return None
    norm = normalize_code(code)
    if not norm:
        return None
    if _is_totp(norm):
        return 'totp' if _check_totp(row, norm) else None
    return 'backup' if _use_backup(row, norm) else None


def backup_codes_left(user):
    row = enrollment(user)
    if not row or not row.backup_ref:
        return 0
    if row.provider == 'privacyidea':
        try:
            return _pi().remaining_tans(row.backup_ref)
        except MFAUnavailable:
            return None
    try:
        return len(json.loads(row.backup_ref))
    except ValueError:
        return 0


def remove(user):
    """Delete the user's second factor everywhere (admin reset, re-enroll).
    The local row always goes; a privacyIDEA outage only leaves an orphan
    token that can never be used, because EdgeShell forgets its serial."""
    row = db.session.get(UserMFA, user.id)
    if not row:
        return
    if row.provider == 'privacyidea':
        for serial in (row.totp_ref, row.backup_ref):
            if serial:
                try:
                    _pi().delete(serial)
                except MFAUnavailable as e:
                    current_app.logger.warning('Could not delete MFA token %s: %s',
                                               serial, e)
    db.session.delete(row)
    db.session.flush()


def _check_totp(row, code):
    if not _is_totp(code):
        return False
    if row.provider == 'privacyidea':
        ok, _ = _pi().check(row.totp_ref, code)
        return ok
    from app.services.crypto import decrypt_value
    secret = decrypt_value(row.totp_ref)
    if not secret:
        return False
    now = time.time()
    current = int(now // TOTP_STEP)
    for offset in (-1, 0, 1):            # tolerate 30 s of clock drift
        step = current + offset
        if step <= (row.last_step or 0):
            continue                      # each code works once
        if hmac.compare_digest(totp_now(secret, now, offset), code):
            row.last_step = step
            return True
    return False


def _use_backup(row, code):
    if not row.backup_ref:
        return False
    if row.provider == 'privacyidea':
        ok, _ = _pi().check(row.backup_ref, code)
        return ok
    try:
        hashes = json.loads(row.backup_ref)
    except ValueError:
        return False
    h = _hash_code(code)
    for stored in hashes:
        if hmac.compare_digest(stored, h):
            hashes.remove(stored)
            row.backup_ref = json.dumps(hashes)
            return True
    return False

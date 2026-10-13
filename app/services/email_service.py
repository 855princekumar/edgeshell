"""Transactional email via useSend (open source, Resend-compatible).

API: POST {base_url}/api/v1/emails
     Authorization: Bearer <api_key>
     JSON: {from, to, subject, html, text}   (to may be a string or array)

Configuration precedence: admin panel settings (DB) first, then environment.
This lets an administrator configure useSend from the Issue Hub without editing
.env. The API key is stored encrypted at rest. Sending is best-effort: a
failure is logged and returned, never raised, so a ticket action never breaks
because email is down or unconfigured.
"""
import requests
from flask import current_app

_KEYS = ('email_enabled', 'usesend_base_url', 'usesend_from', 'usesend_api_key_enc')


def get_email_config():
    """Resolve effective email config from DB settings, falling back to env."""
    from app.models import Settings
    rows = {r.key: r.value for r in
            Settings.query.filter(Settings.key.in_(_KEYS)).all()}
    cfg = current_app.config

    enabled_raw = rows.get('email_enabled')
    enabled = (enabled_raw == 'true') if enabled_raw is not None \
        else bool(cfg.get('EMAIL_ENABLED', False))

    base = rows.get('usesend_base_url') or cfg.get('USESEND_BASE_URL',
                                                   'https://app.usesend.com')
    sender = rows.get('usesend_from') or cfg.get('USESEND_FROM', '')

    key_enc = rows.get('usesend_api_key_enc')
    if key_enc:
        from app.services.crypto import decrypt_value
        api_key = decrypt_value(key_enc)
    else:
        api_key = cfg.get('USESEND_API_KEY', '')

    return {'enabled': enabled, 'base_url': base, 'from': sender, 'api_key': api_key}


def save_email_config(enabled, base_url, sender, api_key=None):
    """Persist config to DB settings. api_key=None keeps the existing key."""
    from app import db
    from app.models import Settings

    def setk(k, v):
        row = Settings.query.filter_by(key=k).first()
        if row:
            row.value = v
        else:
            db.session.add(Settings(key=k, value=v))

    setk('email_enabled', 'true' if enabled else 'false')
    setk('usesend_base_url', base_url or 'https://app.usesend.com')
    setk('usesend_from', sender or '')
    if api_key:  # only overwrite when a new key is supplied
        from app.services.crypto import encrypt_value
        setk('usesend_api_key_enc', encrypt_value(api_key))
    db.session.commit()


def is_enabled():
    c = get_email_config()
    return bool(c['enabled'] and c['api_key'] and c['from'])


def send_email(to, subject, html, text=None):
    """Return (ok, detail). Never raises."""
    c = get_email_config()
    if not (c['enabled'] and c['api_key'] and c['from']):
        return False, 'email disabled or not configured'
    if not to:
        return False, 'no recipient'
    url = f"{c['base_url'].rstrip('/')}/api/v1/emails"
    payload = {
        'from': c['from'],
        'to': to if isinstance(to, str) else list(to),  # useSend: string or array
        'subject': subject,
        'html': html,
        'text': text or _strip_html(html),
    }
    headers = {'Authorization': f"Bearer {c['api_key']}",
               'Content-Type': 'application/json'}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code < 300:
            return True, 'sent'
        return False, f'{r.status_code}: {r.text[:200]}'
    except Exception as e:
        return False, str(e)


def _strip_html(html):
    import re
    return re.sub(r'<[^>]+>', '', html or '')


# ---------------------------------------------------------------- SMTP
# Account mail (administrator reset links) goes through SMTP_* from the
# environment, or useSend when only that is configured. Never raises.

def smtp_configured():
    cfg = current_app.config
    return bool(cfg.get('SMTP_HOST') and cfg.get('SMTP_FROM'))


def account_mail_available():
    return smtp_configured() or is_enabled()


def send_account_email(to, subject, text):
    """Return (ok, detail)."""
    if smtp_configured():
        return _send_smtp(to, subject, text)
    if is_enabled():
        return send_email(to, subject, '<pre style="font-family:inherit">'
                          + _escape(text) + '</pre>', text)
    return False, 'no mail transport configured'


def _send_smtp(to, subject, text):
    import smtplib
    import ssl
    from email.message import EmailMessage
    cfg = current_app.config
    msg = EmailMessage()
    msg['From'] = cfg['SMTP_FROM']
    msg['To'] = to
    msg['Subject'] = subject
    msg.set_content(text)
    security = cfg.get('SMTP_SECURITY', 'starttls')
    port = int(cfg.get('SMTP_PORT') or (465 if security == 'ssl' else 587))
    try:
        ctx = ssl.create_default_context()
        if security == 'ssl':
            server = smtplib.SMTP_SSL(cfg['SMTP_HOST'], port, timeout=15, context=ctx)
        else:
            server = smtplib.SMTP(cfg['SMTP_HOST'], port, timeout=15)
        with server:
            if security == 'starttls':
                server.starttls(context=ctx)
            if cfg.get('SMTP_USER'):
                server.login(cfg['SMTP_USER'], cfg.get('SMTP_PASSWORD', ''))
            server.send_message(msg)
        return True, 'sent'
    except Exception as e:
        return False, f'{e.__class__.__name__}: {str(e)[:200]}'


def _escape(s):
    import html
    return html.escape(s or '')

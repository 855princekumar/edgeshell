"""Outgoing webhooks (ADR 0015): tell other software when hardware events happen.

Every delivery is a JSON POST signed with the webhook's own secret:

  X-EdgeShell-Event       e.g. hardware.fault_reported
  X-EdgeShell-Delivery    a unique id (repeated on retries, so receivers can de-duplicate)
  X-EdgeShell-Timestamp   Unix seconds
  X-EdgeShell-Signature   sha256=HMAC-SHA256(secret, "<timestamp>.<body>") in hex

Receivers check the signature and refuse old timestamps (replays). Deliveries
run in a small background pool, time out after a few seconds, follow no
redirects and are retried twice. Addresses that point back into the server's
own networks are refused: loopback, link-local (this includes the cloud
metadata service), multicast and reserved ranges always; private and
carrier-grade NAT ranges (a LAN, the Tailscale mesh) only when
WEBHOOK_ALLOW_PRIVATE is set.
"""
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit

from flask import current_app

from app import db
from app.models.hardware import Webhook, WebhookDelivery
from app.services.hw_errors import HardwareError

EVENTS = ('hardware.fault_reported', 'hardware.fault_updated', 'hardware.fault_resolved',
          'hardware.change_detected', 'hardware.change_confirmed', 'hardware.change_rejected', 'node.replaced')
MAX_WEBHOOKS = 20
KEEP_DELIVERIES = 200
DISABLE_AFTER = 50                  # failures in a row
RETRY_DELAYS = (2, 10)
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='webhook')


def new_secret():
    return 'whsec_' + secrets.token_urlsafe(32)


def sign(secret, timestamp, body):
    mac = hmac.new(secret.encode(), f'{timestamp}.'.encode() + body, hashlib.sha256).hexdigest()
    return 'sha256=' + mac


def check_url(url, allow_private=None):
    """Raises HardwareError (a ValueError) with a code when EdgeShell must not call this address."""
    if allow_private is None:
        allow_private = bool(current_app.config.get('WEBHOOK_ALLOW_PRIVATE'))
    if not isinstance(url, str) or len(url) > 500:
        raise HardwareError('url_long')
    parts = urlsplit(url.strip())
    if parts.scheme not in ('https', 'http'):
        raise HardwareError('url_https')
    if parts.scheme == 'http' and not allow_private:
        raise HardwareError('url_http')
    if parts.username or parts.password:
        raise HardwareError('url_creds')
    host = parts.hostname
    if not host:
        raise HardwareError('url_host')
    try:
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, ValueError, UnicodeError):
        raise HardwareError('url_dns')
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split('%')[0])
        if getattr(ip, 'ipv4_mapped', None):
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise HardwareError('url_internal')
        private = ip.is_private or (ip.version == 4 and ip in ipaddress.ip_network('100.64.0.0/10'))
        if private and not allow_private:
            raise HardwareError('url_private')
    return url.strip()


def emit(event, data):
    """Queue `event` for every active webhook subscribed to it. Never raises."""
    try:
        hooks = [h for h in Webhook.query.filter_by(active=True).all()
                 if event in h.event_set() or '*' in h.event_set()]
    except Exception:
        current_app.logger.warning('webhook lookup failed', exc_info=True)
        return 0
    if not hooks:
        return 0
    body = json.dumps({'event': event, 'occurred_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
                       'data': data}, separators=(',', ':'), default=str).encode()
    app = current_app._get_current_object()
    for h in hooks:
        job = (app, h.id, h.url, h.secret, event, body, str(uuid.uuid4()))
        if app.config.get('WEBHOOK_SYNC'):            # tests: deliver inline
            _deliver(*job)
        else:
            _POOL.submit(_deliver, *job)
    return len(hooks)


def _deliver(app, hook_id, url, secret, event, body, delivery_id, retry=True):
    import requests
    with app.app_context():
        status, error, attempts, started = None, None, 0, time.time()
        for delay in (0,) + (RETRY_DELAYS if retry else ()):
            if delay and not app.config.get('WEBHOOK_SYNC'):
                time.sleep(delay)
            attempts += 1
            try:
                check_url(url)
                ts = str(int(time.time()))
                r = requests.post(url, data=body, timeout=(3, 5), allow_redirects=False, headers={
                    'Content-Type': 'application/json', 'User-Agent': 'EdgeShell-Webhook',
                    'X-EdgeShell-Event': event, 'X-EdgeShell-Delivery': delivery_id,
                    'X-EdgeShell-Timestamp': ts, 'X-EdgeShell-Signature': sign(secret, ts, body)})
                status, error = r.status_code, None if 200 <= r.status_code < 300 else f'HTTP {r.status_code}'
                if error is None or 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                    break                             # done, or a refusal a retry will not fix
            except ValueError as e:
                from app.services.hw_errors import public_message
                status, error = None, public_message(e)
                break
            except Exception as e:
                status, error = None, type(e).__name__
        _record(hook_id, delivery_id, event, status, error, attempts, started)


def _record(hook_id, delivery_id, event, status, error, attempts, started):
    try:
        h = db.session.get(Webhook, hook_id)
        db.session.add(WebhookDelivery(webhook_id=hook_id, delivery=delivery_id, event=event, status_code=status,
                                       attempts=attempts, error=error,
                                       duration_ms=int((time.time() - started) * 1000)))
        if h:
            h.last_status, h.last_at = status, datetime.utcnow()
            h.failures = 0 if error is None else (h.failures or 0) + 1
            if h.failures >= DISABLE_AFTER:
                h.active = False                      # stop hammering a dead receiver
        old = WebhookDelivery.query.filter_by(webhook_id=hook_id).order_by(WebhookDelivery.id.desc()) \
            .offset(KEEP_DELIVERIES).first()
        if old:
            WebhookDelivery.query.filter(WebhookDelivery.webhook_id == hook_id,
                                         WebhookDelivery.id <= old.id).delete(synchronize_session=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.warning('webhook delivery record failed', exc_info=True)


def create(name, url, events, user):
    name = (name or '').strip()[:80]
    if not name:
        raise HardwareError('hook_name')
    events = sorted({e for e in events or [] if e in EVENTS})
    if not events:
        raise HardwareError('hook_events')
    if Webhook.query.count() >= MAX_WEBHOOKS:
        raise HardwareError('too_many_hooks')
    url = check_url(url)
    secret = new_secret()
    h = Webhook(name=name, url=url, events=','.join(events), created_by=user, active=True)
    h.secret = secret
    db.session.add(h)
    db.session.commit()
    return h, secret


def test(hook):
    """Send one webhook.test delivery now and return its record."""
    body = json.dumps({'event': 'webhook.test', 'occurred_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
                       'data': {'message': 'Test delivery from EdgeShell'}}, separators=(',', ':')).encode()
    delivery_id = str(uuid.uuid4())
    _deliver(current_app._get_current_object(), hook.id, hook.url, hook.secret, 'webhook.test', body, delivery_id,
             retry=False)
    return WebhookDelivery.query.filter_by(delivery=delivery_id).first()

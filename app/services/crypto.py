"""Credential encryption for node SSH passwords.

The key is resolved from Flask config (CREDENTIAL_KEY), which in production is
required from the environment (see config.ProductionConfig). A dev fallback
exists only for local development.

Rotation: changing CREDENTIAL_KEY makes existing ciphertext undecryptable.
Rotate with:  python manage.py reencrypt --old <KEY>   (re-encrypts under the
new key), or re-enter node passwords. decrypt_value fails soft (returns '') so
a rotated key never crashes the app; it surfaces as an auth failure on connect.
"""
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_DEV_FALLBACK = 'edgeshell-dev-only-not-for-production'


def _resolve_key():
    try:
        from flask import current_app
        key = current_app.config.get('CREDENTIAL_KEY')
        if key:
            return key
    except Exception:
        pass
    import os
    return os.environ.get('CREDENTIAL_KEY', _DEV_FALLBACK)


def _fernet(key_str=None):
    key_str = key_str or _resolve_key()
    key_bytes = key_str.encode('utf-8').ljust(32)[:32]
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=b'edgeshell_salt_v1', iterations=100000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(key_bytes)))


def encrypt_value(value: str, key_str=None) -> str:
    if not value:
        return ''
    return _fernet(key_str).encrypt(value.encode('utf-8')).decode('utf-8')


def decrypt_value(encrypted: str, key_str=None) -> str:
    if not encrypted:
        return ''
    try:
        return _fernet(key_str).decrypt(encrypted.encode('utf-8')).decode('utf-8')
    except Exception:
        return ''

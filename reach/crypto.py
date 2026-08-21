"""Encryption and hashing for sensitive REACH fields.

Contact values (email addresses, submission-form destinations) are encrypted at
rest and separately hashed so deduplication and suppression can work without
decrypting anything.

The key comes from ``REACH_ENCRYPTION_KEY``. When it is absent the process
generates an ephemeral key so development still works — but that state is
reported honestly through :func:`key_state` and surfaced on the Provider Health
screen. Nothing is ever written in plaintext.
"""

import base64
import hashlib
import hmac
import os
import threading

from cryptography.fernet import Fernet, InvalidToken

from . import config

_lock = threading.Lock()
_key = None
_key_source = None

KEY_CONFIGURED = "CONFIGURED"
KEY_EPHEMERAL = "DEV_EPHEMERAL"


def _load_key():
    global _key, _key_source
    with _lock:
        if _key is not None:
            return _key, _key_source
        raw = config.env(config.ENCRYPTION_KEY_ENV)
        if raw:
            try:
                key = raw.encode("utf-8")
                Fernet(key)  # validates length/encoding
                _key, _key_source = key, KEY_CONFIGURED
                return _key, _key_source
            except (ValueError, TypeError):
                # A malformed key must not silently downgrade to plaintext.
                derived = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
                _key, _key_source = derived, KEY_CONFIGURED
                return _key, _key_source
        _key, _key_source = Fernet.generate_key(), KEY_EPHEMERAL
        return _key, _key_source


def reset_key_cache():
    """Test support: forget the cached key so env changes take effect."""
    global _key, _key_source
    with _lock:
        _key = None
        _key_source = None


def key_state():
    _, source = _load_key()
    return source


def encrypt(plaintext):
    if plaintext is None:
        return None
    key, _ = _load_key()
    return Fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext):
    """Returns None when the value cannot be decrypted with the current key.

    Callers must treat None as "unavailable", never as "empty" — an ephemeral
    key that rotated on restart makes old ciphertext unreadable by design.
    """
    if ciphertext is None:
        return None
    key, _ = _load_key()
    try:
        return Fernet(key).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None


def _pepper():
    key, _ = _load_key()
    return hashlib.sha256(b"reach-hash-pepper" + key).digest()


def normalize_email(value):
    """Lowercase, trim, and strip a plus-tag so dedup and suppression agree."""
    if not value:
        return ""
    value = value.strip().lower()
    if "@" not in value:
        return value
    local, _, domain = value.partition("@")
    local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def hash_value(value):
    """Keyed hash of a normalized contact value, for dedup and suppression."""
    normalized = normalize_email(value)
    return hmac.new(_pepper(), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def preview_email(value):
    """A redacted form safe to show in lists: ``su•••@example.com``."""
    normalized = normalize_email(value)
    if "@" not in normalized:
        return "•••"
    local, _, domain = normalized.partition("@")
    head = local[:2] if len(local) > 2 else local[:1]
    return f"{head}•••@{domain}"


def content_hash(text):
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def url_hash(url):
    return hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest()


def random_token(length=24):
    return base64.urlsafe_b64encode(os.urandom(length)).decode("ascii").rstrip("=")

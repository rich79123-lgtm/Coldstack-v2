"""Encryption for stored inbox passwords and signed unsubscribe tokens."""
import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet, InvalidToken

from config import DATA_DIR

_KEY_FILE = DATA_DIR / "secret.key"


def _key() -> bytes:
    env = os.environ.get("SECRET_KEY")
    if env:
        return base64.urlsafe_b64encode(hashlib.sha256(env.encode()).digest())
    if not _KEY_FILE.exists():
        _KEY_FILE.write_bytes(Fernet.generate_key())
        try:
            os.chmod(_KEY_FILE, 0o600)
        except OSError:
            pass
    return _KEY_FILE.read_bytes().strip()


def encrypt(plain: str) -> str:
    if not plain:
        return ""
    return Fernet(_key()).encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return Fernet(_key()).decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def sign(value: str) -> str:
    mac = hmac.new(_key(), value.encode(), hashlib.sha256).hexdigest()[:20]
    return f"{value}.{mac}"


def unsign(token: str) -> str | None:
    if "." not in token:
        return None
    value, mac = token.rsplit(".", 1)
    good = hmac.new(_key(), value.encode(), hashlib.sha256).hexdigest()[:20]
    return value if hmac.compare_digest(mac, good) else None

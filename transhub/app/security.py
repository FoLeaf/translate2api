# -*- coding: utf-8 -*-
"""安全组件：密码哈希(scrypt)、会话令牌(HMAC)、API Key 校验、限流桶、登录防爆破。"""
import hashlib
import hmac
import secrets
import time

from . import db

_FAILS: dict[str, list[float]] = {}


# ----------------------------------------------------------------- 密码
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$%s$%s" % (salt.hex(), h.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, h_hex = stored.split("$")
        if algo != "scrypt":
            return False
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                           n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


def get_admin_password_hash() -> str | None:
    return db.get_setting("admin_password_hash")


def set_admin_password(password: str) -> None:
    db.set_setting("admin_password_hash", hash_password(password))


# ----------------------------------------------------------------- 会话
def _secret() -> bytes:
    return db.get_or_create_secret().encode()


def issue_session(days: int = 7) -> str:
    exp = int(time.time()) + days * 86400
    payload = str(exp)
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig


def check_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    if not payload.isdigit():
        return False
    expect = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return False
    return int(payload) > time.time()


# ------------------------------------------------------------- API Key
def check_api_key_from_header(auth_header: str | None) -> str | None:
    """兼容 Bearer / DeepL-Auth-Key / 裸 key 三种写法（ReadFrog 两种服务商都覆盖）。"""
    if not auth_header:
        return None
    token = auth_header.strip()
    low = token.lower()
    for prefix in ("bearer ", "deepl-auth-key ", "deepL-Auth-Key ".lower()):
        if low.startswith(prefix):
            token = token[len(prefix):].strip()
            break
    return db.verify_api_key(token)


# ----------------------------------------------------------------- 限流
class TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last = time.monotonic()

    def acquire(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


_buckets: dict[str, TokenBucket] = {}


def rate_allow(provider: str, default: tuple[float, float]) -> bool:
    cfg = db.get_setting("rate_" + provider)
    rate, burst = tuple(cfg) if isinstance(cfg, (list, tuple)) and len(cfg) == 2 else default
    b = _buckets.get(provider)
    if b is None or b.rate != rate or b.capacity != burst:
        b = TokenBucket(rate, burst)
        _buckets[provider] = b
    return b.acquire()


# --------------------------------------------------------- 登录防爆破
LOGIN_WINDOW = 900.0
LOGIN_MAX_FAILS = 8


def login_attempts_exceeded(ip: str) -> bool:
    now = time.time()
    lst = [t for t in _FAILS.get(ip, []) if now - t < LOGIN_WINDOW]
    _FAILS[ip] = lst
    return len(lst) >= LOGIN_MAX_FAILS


def record_login_fail(ip: str) -> None:
    _FAILS.setdefault(ip, []).append(time.time())


def record_login_ok(ip: str) -> None:
    _FAILS.pop(ip, None)

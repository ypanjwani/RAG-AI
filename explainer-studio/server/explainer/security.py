"""Passwords (bcrypt) and tokens (JWT access tokens, opaque refresh tokens).

- Passwords are hashed with bcrypt (cost 12). bcrypt reads at most 72 bytes, so longer passwords are
  refused by validation rather than silently cut.
- Access tokens are short-lived JWTs (HS256 only: the algorithm is pinned on decode, so "none" or a
  swapped algorithm is rejected). They carry the user's token_version; bumping it logs out every device.
- Refresh tokens are random strings. Only their SHA-256 hash is stored, so a database leak can't be replayed.
"""

from __future__ import annotations

import hashlib
import secrets
import time

import bcrypt
import jwt

BCRYPT_ROUNDS = 12
ISSUER = "explainer-studio"
AUDIENCE = "explainer-studio-api"
ALGORITHM = "HS256"
LEEWAY_SECONDS = 30  # clock skew between gunicorn workers or hosts

# Compared against when an email isn't registered, so a login takes as long whether or not the account exists.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    """Constant work whether or not there is a hash to check."""
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), (hashed or _DUMMY_HASH).encode())
    except ValueError:  # over 72 bytes, or a malformed hash
        return False
    return ok and hashed is not None


def create_access_token(user_id: str, token_version: int, secret: str, minutes: int) -> tuple[str, int]:
    """(token, expiry in epoch seconds)."""
    now = int(time.time())
    exp = now + minutes * 60
    claims = {"sub": user_id, "ver": token_version, "iat": now, "nbf": now, "exp": exp,
              "iss": ISSUER, "aud": AUDIENCE, "typ": "access", "jti": secrets.token_hex(8)}
    return jwt.encode(claims, secret, algorithm=ALGORITHM), exp


class TokenError(Exception):
    """The access token is missing, expired, tampered with or not ours. The message is safe to show."""


def decode_access_token(token: str, secret: str) -> dict:
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER, leeway=LEEWAY_SECONDS,
                            options={"require": ["sub", "exp", "iat", "iss", "aud", "ver", "typ"]})
    except jwt.ExpiredSignatureError:
        raise TokenError("Your session has expired. Please sign in again.") from None
    except jwt.PyJWTError:
        raise TokenError("Invalid session. Please sign in again.") from None
    if claims.get("typ") != "access" or not isinstance(claims.get("sub"), str) or not isinstance(claims.get("ver"), int):
        raise TokenError("Invalid session. Please sign in again.")
    return claims


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

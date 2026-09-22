from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets


def verify_access_secret(expected: str, supplied: str | None) -> bool:
    """Verify a configured login secret without a timing-sensitive equality check."""
    if not expected or supplied is None:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionCookie:
    name: str
    value: str
    max_age_seconds: int
    path: str = "/"
    http_only: bool = True
    secure: bool = True
    same_site: str = "Strict"

    def set_cookie_value(self) -> str:
        return (
            f"{self.name}={self.value}; Path={self.path}; Max-Age={self.max_age_seconds}; "
            "HttpOnly; Secure; SameSite=Strict"
        )

"""Renew and persist the Amazon Jobs website's anonymous session."""

import base64
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from http.cookies import SimpleCookie, CookieError


class SessionError(Exception):
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


def token_expiry(headers: dict[str, str]) -> float | None:
    """Read declared expiry without attempting to validate the signature."""
    try:
        authorization = next(value for key, value in headers.items() if key.lower() == "authorization")
        encoded = authorization.rsplit("|", 1)[-1].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        expiry = claims.get("exp") if isinstance(claims, dict) else None
        if type(expiry) in (int, float) and math.isfinite(expiry):
            return expiry
    except (StopIteration, IndexError, ValueError, UnicodeError):
        pass
    return None


def save_headers(path: Path, headers: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(headers, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def merge_cookies(current: str, response_headers: list[tuple[str, str]]) -> str:
    cookies = SimpleCookie()
    cookies.load(current)
    for name, value in response_headers:
        if name.lower() != "set-cookie":
            continue
        incoming = SimpleCookie()
        try:
            incoming.load(value)
        except CookieError:
            continue
        for key, cookie in incoming.items():
            if cookie["max-age"] == "0":
                cookies.pop(key, None)
            else:
                cookies[key] = cookie.value
    return "; ".join(cookie.OutputString(attrs=[]) for cookie in cookies.values())


class SessionManager:
    def __init__(self, path: Path, headers: dict[str, str], fetch, clock=None, margin: float = 300):
        self.path = path
        self.headers = {key.lower(): value for key, value in headers.items()}
        self.fetch = fetch
        self.clock = clock or time.time
        self.margin = margin
        self.last_attempt = -math.inf
        self.last_success = None

    def ensure(self, timeout: float, force: bool = False) -> dict[str, str]:
        now = self.clock()
        expiry = token_expiry(self.headers)
        if not force:
            if expiry is not None and expiry > now + self.margin:
                return self.headers
            if expiry is None and self.last_success is not None and now - self.last_success < 3000:
                return self.headers
        cooldown = 60 - (now - self.last_attempt)
        if cooldown > 0:
            raise SessionError("Waiting before another session renewal attempt.", cooldown)
        self.last_attempt = now
        logging.info("Renewing the anonymous Amazon Jobs session...")
        body, response_headers = self.fetch(self.headers, timeout)
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.strip() or "\r" in token or "\n" in token:
            raise SessionError("The session endpoint did not return a valid token.")
        renewed = dict(self.headers)
        renewed["authorization"] = "Bearer Status|UNAUTHENTICATED|Session|" + token
        new_expiry = token_expiry(renewed)
        if new_expiry is not None and new_expiry <= self.clock():
            raise SessionError("The session endpoint returned an expired token. Check the laptop's clock.")
        cookie = merge_cookies(renewed.get("cookie", ""), response_headers)
        if cookie:
            renewed["cookie"] = cookie
        else:
            renewed.pop("cookie", None)
        save_headers(self.path, renewed)
        self.headers = renewed
        self.last_success = self.clock()
        logging.info("Anonymous session renewed and saved to %s.", self.path.resolve())
        return self.headers

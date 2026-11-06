"""Bearer-key auth for every service but vLLM, which checks the same key itself.

There is one key, CARD_API_KEY. Every route except the open ones needs:

    Authorization: Bearer <key>

vLLM enforces the same key on /v1/* with its own VLLM_API_KEY. The api sends
the key on its own calls to the model services, so the internal Docker network
is not a trust boundary.

Keys are compared with hmac.compare_digest, so a wrong key takes the same time
as a right one. A failure returns 401 with WWW-Authenticate: Bearer and never
says which part was wrong.

The open set is /livez on every service. A service can name more: the mock of
vLLM leaves /health open, as vLLM does.

An address that fails FAIL_LIMIT times in FAIL_WINDOW_S seconds gets 429
instead of 401 on its next wrong key, for BLOCK_S seconds. A right key is always
served and clears the block, so a mistyped key cannot lock anyone out of a node
they have the key for. It is a nuisance limit and nothing more: it lives in one
process's memory, and it counts whatever address Caddy reports.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import Header, HTTPException, status

log = logging.getLogger(__name__)

# Shorter than this and a key is not worth having. openssl rand -hex 32 gives 64.
MIN_KEY_LENGTH = 32

_UNAUTHORIZED = {"WWW-Authenticate": "Bearer"}


def _deny() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing or invalid bearer token",
        headers=_UNAUTHORIZED,
    )


def parse_bearer(header: str | None) -> str | None:
    """Pull the token out of an Authorization header. None if it is not a bearer header.

    The scheme is case-insensitive, per RFC 7235.
    """
    if not header:
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def check_bearer(header: str | None, expected: str) -> None:
    """Raise 401 unless header carries exactly the expected key.

    An empty expected key denies everything. A service that forgot to load its
    key must not become an open service.
    """
    token = parse_bearer(header)
    if not expected or token is None:
        raise _deny()
    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise _deny()


def require_api_key(authorization: Annotated[str | None, Header()] = None) -> None:
    """FastAPI dependency. Put it on every route except /livez."""
    from common.settings import get_settings

    check_bearer(authorization, get_settings().card_api_key)


def assert_key_is_usable(key: str, backend: str, service_name: str = "service") -> None:
    """Startup guard. In real mode a missing or short key stops the process.

    Mock mode gets a warning instead, so a laptop run is not blocked by a weak
    key. The key itself is never part of the message.
    """
    if len(key) >= MIN_KEY_LENGTH:
        return
    problem = "missing" if not key else f"only {len(key)} characters"
    message = (
        f"{service_name}: CARD_API_KEY is {problem}. "
        f"It needs at least {MIN_KEY_LENGTH}. Generate one with: openssl rand -hex 32"
    )
    if backend == "real":
        raise RuntimeError(message)
    log.warning("%s Running anyway because BACKEND=mock.", message)


def auth_headers(key: str) -> dict[str, str]:
    """Header dict for calls the api makes to the model services."""
    return {"Authorization": f"Bearer {key}"}


# The nuisance limit. More than FAIL_LIMIT 401s from one address inside
# FAIL_WINDOW_S seconds, and that address gets 429 for BLOCK_S seconds.
FAIL_LIMIT = 20
FAIL_WINDOW_S = 60.0
BLOCK_S = 60.0


def client_ip(request) -> str:
    """The address the request came from, as far as this process can tell.

    Behind Caddy every request arrives from the gateway container, so the peer
    address is the same for everyone. Caddy's reverse_proxy appends the peer it
    saw to X-Forwarded-For, so the last entry in that header is the address
    Caddy was talking to. A caller who sends their own X-Forwarded-For only adds
    entries in front of it, which is why this reads the last one and not the
    first. It is still a header, and a second proxy in front of Caddy would
    change what it means.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        last = forwarded.rsplit(",", 1)[-1].strip()
        if last:
            return last
    return request.client.host if request.client else "unknown"


class FailedAuthLimiter:
    """Counts 401s per address, in this process's memory.

    Not a defence. It holds no state across a restart, and anything that can
    change its source address walks around it. What it does is stop a loop from
    filling the log and keep an idle node from answering thousands of guesses a
    second. docs/SECURITY.md says the same.

    It refuses wrong keys, never right ones. A blocked address that sends the
    real key is served and forgotten, because the alternative is a demo where
    one bad paste locks a room out for a minute.
    """

    def __init__(
        self,
        limit: int = FAIL_LIMIT,
        window_s: float = FAIL_WINDOW_S,
        block_s: float = BLOCK_S,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self.block_s = block_s
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._blocked: dict[str, float] = {}

    def retry_after(self, address: str, now: float | None = None) -> int:
        """Seconds until this address may try again. 0 when it is not blocked."""
        now = time.monotonic() if now is None else now
        until = self._blocked.get(address)
        if until is None:
            return 0
        if until <= now:
            del self._blocked[address]
            self._failures.pop(address, None)
            return 0
        return max(1, int(round(until - now)))

    def record_failure(self, address: str, now: float | None = None) -> None:
        """Count one 401. The one that goes past the limit starts the block."""
        now = time.monotonic() if now is None else now
        recent = self._failures[address]
        recent.append(now)
        while recent and recent[0] < now - self.window_s:
            recent.popleft()
        if len(recent) > self.limit:
            self._blocked[address] = now + self.block_s
            log.warning(
                "%d failed authentications from %s in %ds. 429 for the next %ds.",
                len(recent),
                address,
                int(self.window_s),
                int(self.block_s),
            )

    def forget(self, address: str) -> None:
        """Drop an address's history. A request that authenticates does this."""
        self._failures.pop(address, None)
        self._blocked.pop(address, None)


def is_open(path: str, open_paths: frozenset[str], open_prefixes: tuple[str, ...]) -> bool:
    """True for a path that is served without the key.

    A path carrying `..` is never open. Nothing in the open set needs one, and
    the gate reads the raw path out of the ASGI scope, so this is the layer that
    has to refuse `/app/../cards` rather than hand it on as an open request.
    """
    if ".." in path:
        return False
    return path in open_paths or path.startswith(open_prefixes)


def add_auth_gate(
    app,
    open_paths: frozenset[str] = frozenset({"/livez"}),
    open_prefixes: tuple[str, ...] = (),
    limiter: FailedAuthLimiter | None = None,
) -> FailedAuthLimiter:
    """Check the key before routing, so an unknown path fails closed with 401.

    Without this, a request for a path that does not exist gets a 404 and tells
    an unauthenticated caller which routes are real. Routes keep their own
    Depends(require_api_key) as well.

    Returns the limiter so a test can reach it.
    """
    from fastapi.responses import JSONResponse

    from common.settings import get_settings

    limiter = limiter or FailedAuthLimiter()
    app.state.auth_limiter = limiter

    @app.middleware("http")
    async def _auth_gate(request, call_next):
        if not is_open(request.url.path, open_paths, open_prefixes):
            address = client_ip(request)
            wait = limiter.retry_after(address)
            try:
                check_bearer(request.headers.get("authorization"), get_settings().card_api_key)
            except HTTPException as denied:
                limiter.record_failure(address)
                if wait:
                    return JSONResponse(
                        {"detail": "too many failed authentications. try again shortly."},
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        headers={"Retry-After": str(wait), **_UNAUTHORIZED},
                    )
                return JSONResponse(
                    {"detail": denied.detail}, status_code=denied.status_code, headers=_UNAUTHORIZED
                )
            # The key was right. Whoever this is, they are not the loop that
            # earned the block, so the count and the block both go.
            limiter.forget(address)
        return await call_next(request)

    return limiter

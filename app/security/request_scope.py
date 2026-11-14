"""Who is making this request, and which rows may they touch?

Every read or write of user-owned data (datasets, conversations, history,
feedback) asks `owner_scope()` for the owner it must filter by. The rule is
fail-closed:

* AUTH_REQUIRED is off  -> returns None, meaning "no filtering" (legacy
  single-user mode used by local demos and the original test-suite).
* AUTH_REQUIRED is on and a user is signed in -> returns that user's id.
* AUTH_REQUIRED is on and nobody is signed in -> raises. A code path that forgot
  to authenticate therefore errors instead of silently returning everyone's data.

The signed-in user lives in a ContextVar set by the request guard in app.py and
cleared when the request ends, so it cannot leak from one request to the next.
Background work that has no request must open `system_scope()` explicitly.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from config import Config
from app.utils.exceptions import AuthRequiredError

_current_user: ContextVar[dict | None] = ContextVar("ainsight_current_user", default=None)
_system_scope: ContextVar[bool] = ContextVar("ainsight_system_scope", default=False)


def set_current_user(user: dict | None):
    return _current_user.set(user)


def reset_current_user(token) -> None:
    _current_user.reset(token)


def current_user() -> dict | None:
    return _current_user.get()


def owner_scope() -> str | None:
    """Owner id to filter by, or None when filtering is disabled."""
    if not Config.AUTH_REQUIRED or _system_scope.get():
        return None
    user = _current_user.get()
    if not user:
        raise AuthRequiredError()
    return user["id"]


@contextmanager
def system_scope() -> Iterator[None]:
    """Explicitly act without a user (startup tasks, maintenance). Never used by routes."""
    token = _system_scope.set(True)
    try:
        yield
    finally:
        _system_scope.reset(token)

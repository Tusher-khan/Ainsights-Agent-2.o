"""Accounts, sessions and one-time tokens.

Design notes
------------
* Passwords are hashed with scrypt (PBKDF2-SHA256 where scrypt is unavailable).
  A plaintext password is never stored or logged.
* Sessions are server-side. The browser holds a random 256-bit token; the database
  holds only its SHA-256. Logging out, changing a password or resetting one deletes
  rows, which really ends those sessions (a signed cookie could not do that).
* Reset / verification tokens are single-use, expire, and are stored hashed.
* Login errors are identical for "no such email" and "wrong password", and a dummy
  hash is checked for unknown emails so response time does not reveal which exists.
* Repeated failures lock the account for a while (stored in the database, so it holds
  across worker processes); a small in-process limiter additionally throttles by IP.
  The in-process limiter is per worker — behind several workers, put a shared limiter
  (Redis, or the platform's WAF) in front for strict global limits.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

from config import Config
from app.database.connection import app_store
from app.security import rate_store
from app.services import email_service
from app.utils.exceptions import (
    AccountError,
    AuthenticationError,
    AuthRequiredError,
    TooManyRequestsError,
)
from app.utils.helpers import new_id, utcnow
from app.utils.logger import get_logger

log = get_logger("services.auth")

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_COMMON_PASSWORDS = {
    "password", "password1", "password123", "123456789", "1234567890", "qwertyuiop",
    "iloveyou12", "admin12345", "welcome123", "letmein123", "abc1234567", "changeme123",
    "ainsight123", "ainsights123",
}


# --------------------------------------------------------------------------
# hashing and validation
# --------------------------------------------------------------------------


def _hash(password: str) -> str:
    try:
        return generate_password_hash(password, method="scrypt")
    except (ValueError, AttributeError):  # scrypt not compiled into this OpenSSL
        return generate_password_hash(password, method="pbkdf2:sha256:600000")


_DUMMY_HASH = _hash("not-a-real-password-used-only-for-timing")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> str:
    email = normalize_email(email)
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise AccountError("Enter a valid email address.")
    return email


def validate_password(password: str, email: str = "") -> None:
    password = password or ""
    if len(password) < Config.PASSWORD_MIN_LENGTH:
        raise AccountError(f"Use at least {Config.PASSWORD_MIN_LENGTH} characters for your password.")
    if len(password) > 128:
        raise AccountError("Passwords can be at most 128 characters.")
    if password.lower() in _COMMON_PASSWORDS or password.isdigit() or len(set(password)) < 5:
        raise AccountError("That password is too easy to guess. Choose something less common.")
    local = email.split("@")[0].lower() if email else ""
    if local and len(local) >= 4 and local in password.lower():
        raise AccountError("Your password should not contain your email address.")


# --------------------------------------------------------------------------
# throttling
# --------------------------------------------------------------------------

def rate_limit(bucket: str, key: str, limit: int, window_seconds: int) -> None:
    """Raise TooManyRequestsError if `key` exceeded `limit` hits in the window (store: app/security/rate_store.py)."""
    if not Config.RATE_LIMIT_ENABLED:
        return
    if not rate_store.get_store().hit(f"{bucket}:{key}", limit, window_seconds):
        raise TooManyRequestsError()


def reset_rate_limits() -> None:
    rate_store.get_store().reset()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat() + "Z"


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z"))
    except ValueError:
        return None


def public_user(row) -> dict:
    """The only shape of a user that ever leaves the server."""
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "email_verified": bool(row["email_verified_at"]),
        "created_at": row["created_at"],
    }


def _link(path: str, token: str) -> str:
    base = Config.APP_BASE_URL or ""
    return f"{base}{path}?token={token}"


# --------------------------------------------------------------------------
# registration and login
# --------------------------------------------------------------------------


def register(email: str, password: str, name: str = "") -> dict:
    if not Config.ALLOW_REGISTRATION:
        raise AccountError("Sign-ups are closed for this workspace. Ask the owner for an invitation.")
    email = validate_email(email)
    validate_password(password, email)
    name = re.sub(r"\s+", " ", (name or "").strip())[:80]

    user_id = new_id("usr")
    now = utcnow()
    with app_store() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise AccountError("An account with that email already exists. Try signing in instead.")
        conn.execute(
            """INSERT INTO users (id,email,name,password_hash,created_at,updated_at)
               VALUES (?,?,?,?,?,?)""",
            (user_id, email, name or email.split("@")[0], _hash(password), now, now),
        )
    if Config.LEGACY_DATA_OWNER_EMAIL and email == Config.LEGACY_DATA_OWNER_EMAIL:
        adopted = adopt_unowned_rows(user_id)
        log.info("Account %s adopted %s pre-account rows", user_id, adopted)
    send_verification(user_id)
    log.info("Registered user %s", user_id)
    with app_store() as conn:
        return public_user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def adopt_unowned_rows(user_id: str) -> int:
    total = 0
    with app_store() as conn:
        for table in ("datasets", "conversations", "query_history", "feedback"):
            total += conn.execute(f"UPDATE {table} SET owner_id = ? WHERE owner_id IS NULL", (user_id,)).rowcount
    return total


def authenticate(email: str, password: str, ip: str = "", user_agent: str = "") -> tuple[dict, str]:
    """Check credentials and open a session. Returns (user, session_token)."""
    email = normalize_email(email)
    rate_limit("login-ip", ip or "-", 30, 300)
    rate_limit("login-email", email or "-", 10, 300)

    with app_store() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            check_password_hash(_DUMMY_HASH, password or "")  # equalise timing
            raise AuthenticationError()

        locked_until = _parse(row["locked_until"])
        if locked_until and locked_until > _now():
            check_password_hash(_DUMMY_HASH, password or "")
            raise TooManyRequestsError(
                f"This account is temporarily locked after repeated failed sign-ins. Try again in about "
                f"{max(1, int((locked_until - _now()).total_seconds() // 60) + 1)} minutes, or reset your password."
            )

        if not check_password_hash(row["password_hash"], password or ""):
            failures = row["failed_logins"] + 1
            lock = None
            if failures >= Config.MAX_FAILED_LOGINS:
                lock = _iso(_now() + timedelta(minutes=Config.LOCKOUT_MINUTES))
                failures = 0
                log.warning("Locked account %s after repeated failed logins", row["id"])
            conn.execute(
                "UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                (failures, lock, row["id"]),
            )
            conn.commit()
            raise AuthenticationError()

        if not row["is_active"]:
            raise AuthenticationError()
        if Config.REQUIRE_EMAIL_VERIFICATION and not row["email_verified_at"]:
            raise AccountError("Verify your email address first. Check your inbox for the confirmation link.")

        conn.execute(
            "UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
            (utcnow(), row["id"]),
        )
        user = public_user(row)

    return user, create_session(user["id"], ip, user_agent)


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


def create_session(user_id: str, ip: str = "", user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with app_store() as conn:
        # opportunistic cleanup keeps the table small without a scheduler
        conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (_iso(now),))
        conn.execute(
            """INSERT INTO auth_sessions (id,user_id,created_at,expires_at,last_seen_at,user_agent,ip)
               VALUES (?,?,?,?,?,?,?)""",
            (
                _sha(token),
                user_id,
                _iso(now),
                _iso(now + timedelta(days=Config.SESSION_TTL_DAYS)),
                _iso(now),
                (user_agent or "")[:200],
                (ip or "")[:64],
            ),
        )
    return token


def user_for_session(token: str | None) -> dict | None:
    """The signed-in user for a session token, or None (missing, unknown or expired)."""
    if not token:
        return None
    with app_store() as conn:
        row = conn.execute(
            """SELECT u.*, s.expires_at AS session_expires
               FROM auth_sessions s JOIN users u ON u.id = s.user_id
               WHERE s.id = ? AND u.is_active = 1""",
            (_sha(token),),
        ).fetchone()
        if row is None:
            return None
        expires = _parse(row["session_expires"])
        if not expires or expires < _now():
            conn.execute("DELETE FROM auth_sessions WHERE id = ?", (_sha(token),))
            return None
        return public_user(row)


def end_session(token: str | None) -> None:
    if token:
        with app_store() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE id = ?", (_sha(token),))


def end_all_sessions(user_id: str, except_token: str | None = None) -> int:
    with app_store() as conn:
        if except_token:
            cur = conn.execute(
                "DELETE FROM auth_sessions WHERE user_id = ? AND id != ?", (user_id, _sha(except_token))
            )
        else:
            cur = conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
    return cur.rowcount


# --------------------------------------------------------------------------
# profile and password changes
# --------------------------------------------------------------------------


def update_profile(user_id: str, name: str) -> dict:
    name = re.sub(r"\s+", " ", (name or "").strip())[:80]
    if not name:
        raise AccountError("Your name cannot be empty.")
    with app_store() as conn:
        conn.execute("UPDATE users SET name = ?, updated_at = ? WHERE id = ?", (name, utcnow(), user_id))
        return public_user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def change_password(user_id: str, current: str, new: str, keep_token: str | None = None) -> None:
    rate_limit("change-password", user_id, 10, 600)
    with app_store() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AuthRequiredError()
        if not check_password_hash(row["password_hash"], current or ""):
            raise AccountError("Your current password is not correct.")
        validate_password(new, row["email"])
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (_hash(new), utcnow(), user_id)
        )
    end_all_sessions(user_id, except_token=keep_token)


def verify_password(user_id: str, password: str) -> bool:
    with app_store() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and check_password_hash(row["password_hash"], password or ""))


# --------------------------------------------------------------------------
# one-time tokens: password reset and email verification
# --------------------------------------------------------------------------


def _issue_token(user_id: str, purpose: str, ttl: timedelta) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with app_store() as conn:
        # a new request invalidates earlier unused ones of the same kind
        conn.execute(
            "DELETE FROM auth_tokens WHERE user_id = ? AND purpose = ? AND used_at IS NULL", (user_id, purpose)
        )
        conn.execute(
            "INSERT INTO auth_tokens (id,user_id,purpose,created_at,expires_at) VALUES (?,?,?,?,?)",
            (_sha(token), user_id, purpose, _iso(now), _iso(now + ttl)),
        )
    return token


def _consume_token(token: str, purpose: str) -> str:
    """Mark a token used and return its user id, or raise if it is not valid."""
    with app_store() as conn:
        row = conn.execute(
            "SELECT * FROM auth_tokens WHERE id = ? AND purpose = ?", (_sha(token or ""), purpose)
        ).fetchone()
        expires = _parse(row["expires_at"]) if row else None
        if row is None or row["used_at"] or not expires or expires < _now():
            raise AccountError("This link is invalid or has expired. Request a new one.")
        conn.execute("UPDATE auth_tokens SET used_at = ? WHERE id = ?", (utcnow(), row["id"]))
        return row["user_id"]


def request_password_reset(email: str, ip: str = "") -> None:
    """Always returns normally so the response never reveals whether an account exists."""
    email = normalize_email(email)
    rate_limit("forgot-ip", ip or "-", 10, 900)
    rate_limit("forgot-email", email or "-", 3, 900)
    with app_store() as conn:
        row = conn.execute("SELECT id, name FROM users WHERE email = ? AND is_active = 1", (email,)).fetchone()
    if row is None:
        return
    token = _issue_token(row["id"], "reset", timedelta(minutes=Config.RESET_TOKEN_MINUTES))
    email_service.send(
        email,
        "Reset your AInsight password",
        f"Hello {row['name']},\n\nUse this link to choose a new password "
        f"(valid for {Config.RESET_TOKEN_MINUTES} minutes):\n{_link('/reset-password', token)}\n\n"
        "If you did not ask for this, you can ignore this email — your password has not changed.",
    )


def reset_password(token: str, new_password: str) -> None:
    rate_limit("reset-token", _sha(token or "")[:16], 10, 900)
    with app_store() as conn:
        row = conn.execute(
            "SELECT t.*, u.email FROM auth_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.id = ? AND t.purpose = 'reset'",
            (_sha(token or ""),),
        ).fetchone()
    if row is None:
        raise AccountError("This link is invalid or has expired. Request a new one.")
    validate_password(new_password, row["email"])  # check before consuming so a typo does not burn the link
    user_id = _consume_token(token, "reset")
    with app_store() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, failed_logins = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
            (_hash(new_password), utcnow(), user_id),
        )
    end_all_sessions(user_id)  # a reset ends every existing session


def send_verification(user_id: str) -> None:
    with app_store() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["email_verified_at"]:
        return
    token = _issue_token(user_id, "verify", timedelta(hours=Config.VERIFY_TOKEN_HOURS))
    email_service.send(
        row["email"],
        "Confirm your AInsight email address",
        f"Hello {row['name']},\n\nConfirm your email address:\n{_link('/verify-email', token)}\n",
    )


def resend_verification(user_id: str) -> None:
    rate_limit("verify-resend", user_id, 3, 900)
    send_verification(user_id)


def verify_email(token: str) -> None:
    user_id = _consume_token(token, "verify")
    with app_store() as conn:
        conn.execute(
            "UPDATE users SET email_verified_at = COALESCE(email_verified_at, ?), updated_at = ? WHERE id = ?",
            (utcnow(), utcnow(), user_id),
        )


# --------------------------------------------------------------------------
# account deletion
# --------------------------------------------------------------------------


def delete_account(user_id: str) -> None:
    """Remove the user and everything they own, including files and search index entries."""
    from app.security.request_scope import system_scope
    from app.services import dataset_service

    with system_scope():
        from app.database import repository
        from app.services import connection_service

        connection_service.delete_all_for_owner(user_id)

        for dataset in repository.list_datasets(owner_id=user_id):
            dataset_service.delete_dataset(dataset["id"], owner_id=user_id, remove_file=True)
    with app_store() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))  # cascades sessions, tokens, rows
    log.info("Deleted account %s", user_id)

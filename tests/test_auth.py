"""Accounts, sessions and — most importantly — tenant isolation.

The rest of the suite runs in legacy single-user mode (AUTH_REQUIRED off). Each test
here switches accounts on for its own duration through `auth_on()` and restores the
previous state afterwards, so nothing leaks between tests (this also works under the
dependency-free runner in tests/run_tests.py, which does not tear down fixtures per test).
"""

from __future__ import annotations

import importlib.util
import io
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from config import Config

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct horse battery"
HDR = {"X-AInsight-Client": "test"}

_app = None


def _application():
    global _app
    if _app is None:
        spec = importlib.util.spec_from_file_location("ainsight_entry_auth", ROOT / "app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _app = module.create_app()
        _app.config["TESTING"] = True
    return _app


@contextmanager
def auth_on(**overrides):
    """Turn accounts on (plus any other Config overrides) for the duration of a test."""
    from app.services import auth_service, email_service

    saved = {k: getattr(Config, k) for k in ("AUTH_REQUIRED", *overrides)}
    Config.AUTH_REQUIRED = True
    for key, value in overrides.items():
        setattr(Config, key, value)
    auth_service.reset_rate_limits()
    email_service.OUTBOX.clear()
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(Config, key, value)
        auth_service.reset_rate_limits()


def browser():
    """A fresh client with its own cookie jar — one 'browser'."""
    return _application().test_client()


def email_of(tag: str = "u") -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}@example.com"


def post(client, path, body=None, **kw):
    return client.post(path, json=body or {}, headers={**HDR, **kw.pop("headers", {})}, **kw)


def signup(client=None, email=None, password=PASSWORD, name="Test User"):
    client = client or browser()
    email = email or email_of()
    response = post(client, "/api/auth/register", {"email": email, "password": password, "name": name})
    assert response.status_code == 201, response.get_json()
    return client, email


CSV = pd.DataFrame(
    {
        "Region": ["North", "South", "East", "West", "North", "South"],
        "Sales": [100.0, 250.0, 75.0, 300.0, 60.0, 180.0],
        "Profit": [10.0, 25.0, 7.0, 30.0, 6.0, 18.0],
    }
).to_csv(index=False).encode()


def upload(client, name="sales.csv"):
    response = client.post(
        "/api/upload",
        data={"file": (io.BytesIO(CSV), name)},
        content_type="multipart/form-data",
        headers=HDR,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["dataset"]["id"]


def db_row(sql, params=()):
    from app.database.connection import app_store

    with app_store() as conn:
        return conn.execute(sql, params).fetchone()


# --------------------------------------------------------------------------
# sign-up, sign-in, sign-out
# --------------------------------------------------------------------------


def test_register_login_logout_cycle():
    with auth_on():
        client, email = signup()
        me = client.get("/api/auth/me")
        assert me.status_code == 200 and me.get_json()["user"]["email"] == email
        assert "password" not in str(me.get_json()).lower() or "hash" not in str(me.get_json()).lower()

        assert post(client, "/api/auth/logout").status_code == 200
        assert client.get("/api/auth/me").status_code == 401

        again = post(client, "/api/auth/login", {"email": email.upper(), "password": PASSWORD})
        assert again.status_code == 200  # email is case-insensitive
        assert client.get("/api/auth/me").status_code == 200


def test_wrong_password_and_unknown_email_look_identical():
    with auth_on():
        _client, email = signup()
        wrong = post(browser(), "/api/auth/login", {"email": email, "password": "not the password"})
        unknown = post(browser(), "/api/auth/login", {"email": email_of("nobody"), "password": "not the password"})
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.get_json()["message"] == unknown.get_json()["message"]


def test_password_rules():
    with auth_on():
        for bad in ("short", "1234567890123", "password123", "aaaaaaaaaaaaaaa"):
            r = post(browser(), "/api/auth/register", {"email": email_of(), "password": bad})
            assert r.status_code == 400, bad
        e = "jordanlee@example.com"
        r = post(browser(), "/api/auth/register", {"email": e, "password": "my-jordanlee-pass"})
        assert r.status_code == 400  # contains the email name
        assert post(browser(), "/api/auth/register", {"email": "not-an-email", "password": PASSWORD}).status_code == 400


def test_duplicate_email_is_rejected_case_insensitively():
    with auth_on():
        _c, email = signup()
        r = post(browser(), "/api/auth/register", {"email": email.upper(), "password": PASSWORD})
        assert r.status_code == 400


def test_passwords_and_session_tokens_are_never_stored_in_clear():
    with auth_on():
        client, email = signup()
        row = db_row("SELECT password_hash FROM users WHERE email = ?", (email,))
        assert PASSWORD not in row["password_hash"]
        assert row["password_hash"].split(":")[0] in {"scrypt", "pbkdf2"}

        cookie = client.get_cookie(Config.SESSION_COOKIE_NAME)
        assert cookie is not None
        assert db_row("SELECT 1 FROM auth_sessions WHERE id = ?", (cookie.value,)) is None  # raw token absent
        assert db_row("SELECT 1 FROM auth_sessions WHERE user_id = (SELECT id FROM users WHERE email = ?)", (email,))


def test_session_cookie_flags():
    with auth_on():
        client = browser()
        email = email_of()
        response = post(client, "/api/auth/register", {"email": email, "password": PASSWORD})
        header = response.headers.get("Set-Cookie", "")
        assert "HttpOnly" in header and "SameSite=Lax" in header
        with auth_on(COOKIE_SECURE=True):
            r = post(browser(), "/api/auth/login", {"email": email, "password": PASSWORD})
            assert "Secure" in r.headers.get("Set-Cookie", "")
            assert "Strict-Transport-Security" in r.headers


def test_expired_session_is_rejected():
    with auth_on():
        client, email = signup()
        from app.database.connection import app_store

        with app_store() as conn:
            conn.execute("UPDATE auth_sessions SET expires_at = '2000-01-01T00:00:00Z'")
        assert client.get("/api/auth/me").status_code == 401


def test_account_locks_after_repeated_failures_then_recovers():
    with auth_on(MAX_FAILED_LOGINS=3):
        _c, email = signup()
        attacker = browser()
        for _ in range(3):
            assert post(attacker, "/api/auth/login", {"email": email, "password": "wrong password!"}).status_code == 401
        locked = post(attacker, "/api/auth/login", {"email": email, "password": PASSWORD})
        assert locked.status_code == 429  # even the right password is refused while locked

        from app.database.connection import app_store

        with app_store() as conn:
            conn.execute("UPDATE users SET locked_until = NULL WHERE email = ?", (email,))
        assert post(browser(), "/api/auth/login", {"email": email, "password": PASSWORD}).status_code == 200


def test_rate_limiter_blocks_bursts():
    from app.services import auth_service
    from app.utils.exceptions import TooManyRequestsError

    with auth_on():
        for _ in range(3):
            auth_service.rate_limit("unit", "k", 3, 60)
        try:
            auth_service.rate_limit("unit", "k", 3, 60)
            raise AssertionError("expected the 4th call to be limited")
        except TooManyRequestsError:
            pass
        auth_service.rate_limit("unit", "other-key", 3, 60)  # keys are independent


def test_registration_can_be_closed():
    with auth_on(ALLOW_REGISTRATION=False):
        assert post(browser(), "/api/auth/register", {"email": email_of(), "password": PASSWORD}).status_code == 400
        assert browser().get("/api/auth/config").get_json()["registration_open"] is False


# --------------------------------------------------------------------------
# gatekeeping
# --------------------------------------------------------------------------


def test_anonymous_requests_are_refused_and_pages_redirect():
    with auth_on():
        anon = browser()
        for path in ("/api/datasets", "/api/history", "/api/metrics", "/api/system", "/api/v1/datasets"):
            assert anon.get(path).status_code == 401, path
        assert post(anon, "/api/chat", {"question": "total sales"}).status_code == 401
        assert anon.get("/api/health").status_code == 200  # health check stays public

        redirect = anon.get("/dashboard")
        assert redirect.status_code == 302 and redirect.headers["Location"].startswith("/login?next=/dashboard")
        for page in ("/", "/login", "/register", "/forgot-password"):
            assert anon.get(page).status_code == 200, page


def test_signed_in_user_reaches_app_pages_and_v1_mirror():
    with auth_on():
        client, _ = signup()
        for page in ("/dashboard", "/chat", "/datasets", "/profile"):
            assert client.get(page).status_code == 200, page
        assert client.get("/api/v1/auth/me").status_code == 200
        assert client.get("/api/v1/datasets").status_code == 200


def test_security_headers_present():
    with auth_on():
        r = browser().get("/login")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert browser().get("/api/health").headers["Cache-Control"] == "no-store"


def test_fail_closed_when_no_user_is_in_scope():
    from app.database import repository
    from app.security.request_scope import system_scope
    from app.utils.exceptions import AuthRequiredError

    with auth_on():
        try:
            repository.list_datasets()
            raise AssertionError("a data read with no signed-in user must not succeed")
        except AuthRequiredError:
            pass
        with system_scope():
            assert isinstance(repository.list_datasets(), list)


# --------------------------------------------------------------------------
# CSRF and bearer clients
# --------------------------------------------------------------------------


def test_cross_site_state_changes_are_blocked():
    with auth_on():
        client, _ = signup()
        did = upload(client)
        evil = client.delete(f"/api/datasets/{did}", headers={"Origin": "https://evil.example"})
        assert evil.status_code == 403
        bare = client.delete(f"/api/datasets/{did}")  # no Origin and no app header
        assert bare.status_code == 403
        assert client.get(f"/api/datasets/{did}").status_code == 200  # still there
        assert client.delete(f"/api/datasets/{did}", headers=HDR).status_code == 200


def test_bearer_token_clients_for_desktop_and_mobile():
    with auth_on():
        _c, email = signup()
        r = post(browser(), "/api/auth/login", {"email": email, "password": PASSWORD, "return_token": True})
        token = r.get_json()["token"]
        native = browser()
        auth = {"Authorization": f"Bearer {token}"}
        assert native.get("/api/auth/me", headers=auth).status_code == 200
        assert native.post("/api/auth/logout", headers=auth).status_code == 200  # no CSRF header needed
        assert native.get("/api/auth/me", headers=auth).status_code == 401  # the session really ended


# --------------------------------------------------------------------------
# tenant isolation
# --------------------------------------------------------------------------


def test_users_only_see_their_own_datasets():
    with auth_on():
        a, _ = signup(name="Alice")
        b, _ = signup(name="Bob")
        a_id = upload(a, "alice.csv")

        assert [d["id"] for d in a.get("/api/datasets").get_json()["datasets"]] == [a_id]
        assert b.get("/api/datasets").get_json()["datasets"] == []
        assert b.get("/api/datasets").get_json()["tables"] == []  # shared analytical tables stay hidden

        assert b.get(f"/api/datasets/{a_id}").status_code == 404
        assert b.get(f"/api/schema/{a_id}").status_code == 404
        assert b.get("/api/database/schema").get_json()["tables"] == []
        assert post(b, f"/api/datasets/{a_id}/refresh").status_code == 404
        assert b.delete(f"/api/datasets/{a_id}", headers=HDR).status_code == 404
        assert post(b, "/api/chat", {"question": "total sales by region", "dataset_id": a_id}).status_code == 404
        plan = {"measures": ["Sales"], "dimensions": ["Region"], "aggregation": "sum"}
        assert post(b, "/api/query", {"dataset_id": a_id, "plan": plan}).status_code == 404
        assert post(b, "/api/evaluation/run", {"dataset_id": a_id}).status_code in (404, 422)

        # Alice still has her dataset, untouched by Bob's attempts.
        assert a.get(f"/api/datasets/{a_id}").status_code == 200


def test_question_without_a_dataset_never_routes_to_someone_elses_data():
    with auth_on():
        a, _ = signup()
        b, _ = signup()
        upload(a, "alice_sales.csv")
        # Bob has nothing; the metadata search must not fall back to Alice's dataset.
        r = post(b, "/api/chat", {"question": "show total sales by region"})
        assert r.status_code == 404, r.get_json()
        assert post(a, "/api/chat", {"question": "show total sales by region"}).status_code == 200


def test_history_conversations_feedback_and_downloads_are_private():
    with auth_on():
        a, _ = signup()
        b, _ = signup()
        a_id = upload(a)
        answer = post(a, "/api/chat", {"question": "total sales by region", "dataset_id": a_id})
        assert answer.status_code == 200, answer.get_json()
        body = answer.get_json()
        history_id, conversation_id = body["history_id"], body["conversation_id"]

        assert len(a.get("/api/history").get_json()["history"]) == 1
        assert b.get("/api/history").get_json()["history"] == []
        assert b.get(f"/api/history/{history_id}").status_code == 400
        assert b.get(f"/api/download/{history_id}.csv").status_code == 400
        assert post(b, "/api/feedback", {"history_id": history_id, "rating": "helpful"}).status_code == 400
        assert b.get(f"/api/conversations/{conversation_id}").get_json()["messages"] == []
        assert b.delete(f"/api/conversations/{conversation_id}", headers=HDR).status_code == 400
        assert b.delete(f"/api/history/{history_id}", headers=HDR).get_json()["status"] == "missing"
        assert b.delete("/api/history", headers=HDR).get_json()["removed"] == 0  # "clear all" is per user

        metrics = b.get("/api/metrics").get_json()
        assert metrics["queries"] == 0 and metrics["datasets"] == 0 and metrics["recent_queries"] == []

        # Alice's data survived all of that.
        assert len(a.get("/api/history").get_json()["history"]) == 1
        assert a.get(f"/api/download/{history_id}.csv").status_code == 200
        assert len(a.get(f"/api/conversations/{conversation_id}").get_json()["messages"]) >= 2


def test_reusing_another_users_conversation_id_starts_a_fresh_one():
    with auth_on():
        a, _ = signup()
        b, _ = signup()
        a_id, b_id = upload(a), upload(b)
        conv = post(a, "/api/chat", {"question": "total sales by region", "dataset_id": a_id}).get_json()["conversation_id"]
        before = len(a.get(f"/api/conversations/{conv}").get_json()["messages"])

        r = post(b, "/api/chat", {"question": "total profit by region", "dataset_id": b_id, "conversation_id": conv})
        assert r.status_code == 200
        assert r.get_json()["conversation_id"] != conv
        assert len(a.get(f"/api/conversations/{conv}").get_json()["messages"]) == before


# --------------------------------------------------------------------------
# password reset, change, verification
# --------------------------------------------------------------------------


def _link_token(message_body: str) -> str:
    return re.search(r"token=([\w\-]+)", message_body).group(1)


def test_password_reset_flow():
    from app.services import email_service

    with auth_on(EMAIL_BACKEND="memory"):
        client, email = signup()
        email_service.OUTBOX.clear()

        unknown = post(browser(), "/api/auth/forgot-password", {"email": email_of("ghost")})
        known = post(browser(), "/api/auth/forgot-password", {"email": email})
        assert unknown.status_code == known.status_code == 200
        assert unknown.get_json() == known.get_json()  # no account enumeration
        assert [m["to"] for m in email_service.OUTBOX] == [email]  # only the real address got mail
        token = _link_token(email_service.OUTBOX[0]["body"])

        # a weak new password is refused and does not burn the link
        assert post(browser(), "/api/auth/reset-password", {"token": token, "password": "short"}).status_code == 400
        ok = post(browser(), "/api/auth/reset-password", {"token": token, "password": "a brand new passphrase"})
        assert ok.status_code == 200

        assert client.get("/api/auth/me").status_code == 401  # the old session was ended
        assert post(browser(), "/api/auth/login", {"email": email, "password": PASSWORD}).status_code == 401
        assert post(browser(), "/api/auth/login", {"email": email, "password": "a brand new passphrase"}).status_code == 200
        assert post(browser(), "/api/auth/reset-password", {"token": token, "password": "another passphrase!"}).status_code == 400  # single use


def test_reset_token_expires_and_garbage_is_rejected():
    from app.database.connection import app_store
    from app.services import email_service

    with auth_on(EMAIL_BACKEND="memory"):
        _c, email = signup()
        email_service.OUTBOX.clear()
        post(browser(), "/api/auth/forgot-password", {"email": email})
        token = _link_token(email_service.OUTBOX[0]["body"])
        with app_store() as conn:
            conn.execute("UPDATE auth_tokens SET expires_at = '2000-01-01T00:00:00Z'")
        assert post(browser(), "/api/auth/reset-password", {"token": token, "password": "a brand new passphrase"}).status_code == 400
        assert post(browser(), "/api/auth/reset-password", {"token": "nonsense", "password": "a brand new passphrase"}).status_code == 400


def test_changing_password_signs_out_other_devices_only():
    with auth_on():
        phone, email = signup()
        laptop = browser()
        assert post(laptop, "/api/auth/login", {"email": email, "password": PASSWORD}).status_code == 200

        wrong = post(laptop, "/api/auth/change-password", {"current_password": "nope nope nope", "new_password": "a brand new passphrase"})
        assert wrong.status_code == 400
        ok = post(laptop, "/api/auth/change-password", {"current_password": PASSWORD, "new_password": "a brand new passphrase"})
        assert ok.status_code == 200
        assert laptop.get("/api/auth/me").status_code == 200
        assert phone.get("/api/auth/me").status_code == 401


def test_email_verification():
    from app.services import email_service

    with auth_on(EMAIL_BACKEND="memory"):
        client, email = signup()
        assert client.get("/api/auth/me").get_json()["user"]["email_verified"] is False
        token = _link_token(email_service.OUTBOX[-1]["body"])
        assert post(browser(), "/api/auth/verify-email", {"token": token}).status_code == 200
        assert client.get("/api/auth/me").get_json()["user"]["email_verified"] is True
        assert post(browser(), "/api/auth/verify-email", {"token": token}).status_code == 400  # single use


def test_verification_can_be_required_before_sign_in():
    from app.services import email_service

    with auth_on(EMAIL_BACKEND="memory", REQUIRE_EMAIL_VERIFICATION=True):
        email = email_of()
        r = post(browser(), "/api/auth/register", {"email": email, "password": PASSWORD})
        assert r.status_code == 201 and r.get_json()["verification_required"] is True
        assert post(browser(), "/api/auth/login", {"email": email, "password": PASSWORD}).status_code == 400
        token = _link_token(email_service.OUTBOX[-1]["body"])
        post(browser(), "/api/auth/verify-email", {"token": token})
        assert post(browser(), "/api/auth/login", {"email": email, "password": PASSWORD}).status_code == 200


# --------------------------------------------------------------------------
# account deletion and legacy data
# --------------------------------------------------------------------------


def test_deleting_an_account_removes_its_data_and_files_but_not_others():
    with auth_on():
        a, a_email = signup()
        b, _ = signup()
        a_id, b_id = upload(a), upload(b)
        a_file = Path(db_row("SELECT file_path FROM datasets WHERE id = ?", (a_id,))["file_path"])
        b_file = Path(db_row("SELECT file_path FROM datasets WHERE id = ?", (b_id,))["file_path"])
        assert a_file.exists() and b_file.exists()
        post(a, "/api/chat", {"question": "total sales by region", "dataset_id": a_id})

        r = a.delete("/api/auth/account", json={"password": "wrong password!"}, headers=HDR)
        assert r.status_code == 400 and a_file.exists()

        r = a.delete("/api/auth/account", json={"password": PASSWORD}, headers=HDR)
        assert r.status_code == 200
        assert not a_file.exists()
        assert db_row("SELECT 1 FROM users WHERE email = ?", (a_email,)) is None
        assert db_row("SELECT 1 FROM datasets WHERE id = ?", (a_id,)) is None
        assert db_row("SELECT 1 FROM query_history WHERE dataset_id = ?", (a_id,)) is None
        assert a.get("/api/auth/me").status_code == 401

        assert b_file.exists() and b.get(f"/api/datasets/{b_id}").status_code == 200


def test_uploaded_file_is_removed_when_a_dataset_is_deleted():
    with auth_on():
        a, _ = signup()
        a_id = upload(a)
        path = Path(db_row("SELECT file_path FROM datasets WHERE id = ?", (a_id,))["file_path"])
        assert path.exists()
        assert a.delete(f"/api/datasets/{a_id}", headers=HDR).status_code == 200
        assert not path.exists()


def test_pre_account_data_is_adopted_by_the_configured_owner_only(registered_dataset):
    from app.database.connection import app_store

    with app_store() as conn:  # make sure there is at least one ownerless row
        conn.execute("UPDATE datasets SET owner_id = NULL WHERE id = ?", (registered_dataset["id"],))

    with auth_on():
        stranger, _ = signup()
        assert all(d["id"] != registered_dataset["id"] for d in stranger.get("/api/datasets").get_json()["datasets"])

    owner_email = email_of("owner")
    with auth_on(LEGACY_DATA_OWNER_EMAIL=owner_email):
        owner, _ = signup(email=owner_email)
        assert registered_dataset["id"] in [d["id"] for d in owner.get("/api/datasets").get_json()["datasets"]]
        later, _ = signup()
        assert later.get("/api/datasets").get_json()["datasets"] == []


def test_migration_adds_owner_columns_to_a_pre_account_database(tmp_path):
    """An existing store from before accounts must upgrade in place without losing rows."""
    import sqlite3

    from app.database import connection

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE datasets (id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL,
            source_type TEXT NOT NULL, file_path TEXT, sheet_name TEXT, table_name TEXT,
            row_count INTEGER DEFAULT 0, column_count INTEGER DEFAULT 0, size_bytes INTEGER DEFAULT 0,
            status TEXT DEFAULT 'ready', profile_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, dataset_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE query_history (id TEXT PRIMARY KEY, conversation_id TEXT, question TEXT NOT NULL, dataset_id TEXT,
            dataset_name TEXT, intent TEXT, engine TEXT, sql TEXT, plan_json TEXT, status TEXT, error TEXT,
            row_count INTEGER DEFAULT 0, execution_ms REAL DEFAULT 0, total_ms REAL DEFAULT 0, answer TEXT,
            result_json TEXT, created_at TEXT NOT NULL);
        CREATE TABLE feedback (id TEXT PRIMARY KEY, history_id TEXT, rating TEXT NOT NULL, comment TEXT, created_at TEXT NOT NULL);
        INSERT INTO datasets (id,name,slug,source_type,created_at,updated_at) VALUES ('ds_old','Old','old','csv','t','t');
        """
    )
    old.commit()
    old.close()

    saved = Config.DATABASE_URL
    Config.DATABASE_URL = f"sqlite:///{path}"
    try:
        connection.init_store()
        connection.init_store()  # running it twice must be harmless
        check = sqlite3.connect(path)
        columns = {r[1] for r in check.execute("PRAGMA table_info(datasets)")}
        kept = check.execute("SELECT owner_id FROM datasets WHERE id = 'ds_old'").fetchone()
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        check.close()
        assert "owner_id" in columns and kept == (None,)
        assert {"users", "auth_sessions", "auth_tokens"} <= tables
    finally:
        Config.DATABASE_URL = saved

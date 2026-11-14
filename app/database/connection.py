"""Database connections.

Two distinct concerns live here:

1. `app_store()` — the application's own SQLite store (datasets registry, query
   history, conversations, feedback). Read/write, owned by AInsight.
2. `analytics_connection()` — a **read-only** connection used by the SQL agent
   to run generated analytical SQL. It layers three defences: a read-only URI,
   a SQLite authorizer that rejects any non-SELECT operation, and a statement
   limit.

The `DataSourceBackend` indirection keeps the code PostgreSQL-ready: swapping
in psycopg only requires a second backend implementation of the same methods.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config import Config
from app.utils.exceptions import SQLSecurityError
from app.utils.logger import get_logger

log = get_logger("database.connection")
_LOCK = threading.Lock()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id               TEXT PRIMARY KEY,
    email            TEXT NOT NULL UNIQUE,      -- stored lower-cased
    name             TEXT NOT NULL DEFAULT '',
    password_hash    TEXT NOT NULL,
    is_active        INTEGER NOT NULL DEFAULT 1,
    email_verified_at TEXT,
    failed_logins    INTEGER NOT NULL DEFAULT 0,
    locked_until     TEXT,
    last_login_at    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Server-side sessions. Only the SHA-256 of the session token is stored, so a copy
-- of this table cannot be replayed as cookies.
CREATE TABLE IF NOT EXISTS auth_sessions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    user_agent  TEXT,
    ip          TEXT
);

-- One-time tokens for password reset and email verification (hashed, single use).
CREATE TABLE IF NOT EXISTS auth_tokens (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose     TEXT NOT NULL,                  -- reset | verify
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE TABLE IF NOT EXISTS datasets (
    id              TEXT PRIMARY KEY,
    owner_id        TEXT REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL,
    source_type     TEXT NOT NULL,          -- csv | excel | sqlite
    file_path       TEXT,
    sheet_name      TEXT,
    table_name      TEXT,
    connection_id   TEXT,
    row_count       INTEGER DEFAULT 0,
    column_count    INTEGER DEFAULT 0,
    size_bytes      INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'ready',
    profile_json    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Connected business data sources (databases, APIs, SQLite uploads). Non-secret settings
-- live in config_json; credentials are stored only as an encrypted blob in secrets_enc.
CREATE TABLE IF NOT EXISTS data_sources (
    id              TEXT PRIMARY KEY,
    owner_id        TEXT REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    config_json     TEXT NOT NULL DEFAULT '{}',
    secrets_enc     TEXT,
    status          TEXT NOT NULL DEFAULT 'untested',   -- untested | connected | failed | disabled
    status_message  TEXT,
    warnings_json   TEXT,
    last_checked_at TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Business semantic layer: named metrics (e.g. "Profit Margin" = SUM(profit) / SUM(sales))
-- that the agent uses whenever a question mentions them.
CREATE TABLE IF NOT EXISTS semantic_metrics (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT REFERENCES users(id) ON DELETE CASCADE,
    dataset_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    expression  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Agent observability: one row per tool call. Holds ids, tool names, timings and a short
-- error class — never arguments, SQL text, credentials or result data.
CREATE TABLE IF NOT EXISTS agent_events (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT REFERENCES users(id) ON DELETE CASCADE,
    analysis_id TEXT,
    tool        TEXT NOT NULL,
    source_id   TEXT,
    ok          INTEGER NOT NULL,
    ms          REAL,
    error       TEXT,
    model       TEXT,
    retries     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- Model usage (tokens / estimated cost). Never stores prompts or responses.
CREATE TABLE IF NOT EXISTS llm_usage (
    id            TEXT PRIMARY KEY,
    owner_id      TEXT REFERENCES users(id) ON DELETE CASCADE,
    analysis_id   TEXT,
    provider      TEXT,
    model         TEXT,
    tier          TEXT,
    purpose       TEXT,
    step          INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    est_cost_usd  REAL,
    ms            REAL,
    ok            INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

-- Organizations (scaffolding): membership and roles are stored and managed here; per-dataset sharing is not wired yet.
CREATE TABLE IF NOT EXISTS organizations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS org_members (
    org_id      TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (org_id, user_id)
);

-- Monthly analysis counter for plan limits. Separate from history so deleting history cannot reset a limit.
CREATE TABLE IF NOT EXISTS usage_counters (
    owner_id  TEXT NOT NULL,
    month     TEXT NOT NULL,
    analyses  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner_id, month)
);

CREATE TABLE IF NOT EXISTS dashboards (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    dataset_id  TEXT,
    config_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id            TEXT PRIMARY KEY,
    owner_id      TEXT REFERENCES users(id) ON DELETE CASCADE,
    title         TEXT,
    dataset_id    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id               TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    role             TEXT NOT NULL,
    content          TEXT NOT NULL,
    payload_json     TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_history (
    id              TEXT PRIMARY KEY,
    owner_id        TEXT REFERENCES users(id) ON DELETE CASCADE,
    conversation_id TEXT,
    question        TEXT NOT NULL,
    dataset_id      TEXT,
    dataset_name    TEXT,
    intent          TEXT,
    engine          TEXT,
    sql             TEXT,
    plan_json       TEXT,
    status          TEXT,
    error           TEXT,
    row_count       INTEGER DEFAULT 0,
    execution_ms    REAL DEFAULT 0,
    total_ms        REAL DEFAULT 0,
    answer          TEXT,
    result_json     TEXT,
    title           TEXT,
    favorite        INTEGER NOT NULL DEFAULT 0,
    response_json   TEXT,
    analysis_id     TEXT,
    refreshed_at    TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT REFERENCES users(id) ON DELETE CASCADE,
    history_id  TEXT,
    rating      TEXT NOT NULL,
    comment     TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_created ON query_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
"""


def _sqlite_path() -> Path:
    path = Config.sqlite_path(Config.DATABASE_URL)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def app_store() -> Iterator[sqlite3.Connection]:
    """Read/write connection to the application store."""
    if not Config.is_sqlite(Config.DATABASE_URL):
        raise NotImplementedError(
            "Only SQLite is wired up in this build; the store layer is "
            "abstracted so a PostgreSQL backend can be added without touching callers."
        )
    conn = sqlite3.connect(_sqlite_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_OWNED_TABLES = ("datasets", "conversations", "query_history", "feedback", "data_sources", "semantic_metrics", "agent_events", "llm_usage", "dashboards")


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, non-destructive upgrades for stores created before accounts existed.

    Existing rows keep working: they get owner_id NULL, which means "created before
    accounts" and is only visible when AUTH_REQUIRED is off (or after the account
    named by LEGACY_DATA_OWNER_EMAIL registers and adopts them).
    """
    for table in _OWNED_TABLES:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "owner_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_id TEXT REFERENCES users(id) ON DELETE CASCADE")
            log.info("Migrated %s: added owner_id", table)
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_owner ON {table}(owner_id)")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(datasets)")}
    if "connection_id" not in columns:
        conn.execute("ALTER TABLE datasets ADD COLUMN connection_id TEXT")
        log.info("Migrated datasets: added connection_id")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_datasets_connection ON datasets(connection_id)")
    usage_cols = {row["name"] for row in conn.execute("PRAGMA table_info(llm_usage)")}
    if "step" not in usage_cols:
        conn.execute("ALTER TABLE llm_usage ADD COLUMN step INTEGER")
    events = {row["name"] for row in conn.execute("PRAGMA table_info(agent_events)")}
    for column, ddl in (("model", "TEXT"), ("retries", "INTEGER NOT NULL DEFAULT 0")):
        if column not in events:
            conn.execute(f"ALTER TABLE agent_events ADD COLUMN {column} {ddl}")
    user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "plan" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
    hist = {row["name"] for row in conn.execute("PRAGMA table_info(query_history)")}
    for column, ddl in (("title", "TEXT"), ("favorite", "INTEGER NOT NULL DEFAULT 0"), ("response_json", "TEXT"),
                        ("analysis_id", "TEXT"), ("refreshed_at", "TEXT")):
        if column not in hist:
            conn.execute(f"ALTER TABLE query_history ADD COLUMN {column} {ddl}")
            log.info("Migrated query_history: added %s", column)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON auth_sessions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON auth_sessions(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_user ON auth_tokens(user_id, purpose)")


def init_store() -> None:
    """Create the application schema if it does not exist yet."""
    with _LOCK, app_store() as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate(conn)
    log.info("Application store ready at %s", _sqlite_path())


# --------------------------------------------------------------------------
# Read-only analytical connection
# --------------------------------------------------------------------------

_WRITE_ACTIONS = {
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_PRAGMA,
}


# Read-only pragmas AInsight itself needs to describe a schema. They expose no
# data and cannot modify anything, and they are never reachable from generated
# SQL: the introspection connection is opened only by schema_inspector.
_INTROSPECTION_PRAGMAS = {"table_info", "table_xinfo", "foreign_key_list", "index_list", "index_info"}


def _make_authorizer(allow_introspection: bool):
    def authorize(action, arg1, arg2, db_name, trigger):
        if action == sqlite3.SQLITE_PRAGMA:
            if allow_introspection and (arg1 or "").lower() in _INTROSPECTION_PRAGMAS:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in _WRITE_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and arg2 in {"load_extension", "readfile", "writefile"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


@contextmanager
def analytics_connection(url: str | None = None, allow_introspection: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a hardened, read-only connection.

    `allow_introspection=True` additionally permits the read-only PRAGMAs used
    to describe a schema. It is used by the schema inspector only — never for
    executing generated SQL, which always runs with introspection denied.
    """
    url = url or Config.ANALYTICS_DATABASE_URL
    if not Config.is_sqlite(url):
        raise NotImplementedError("Only SQLite analytics connections are implemented.")
    path = Config.sqlite_path(url)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(path).close()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.set_authorizer(_make_authorizer(allow_introspection))
    except Exception as exc:  # pragma: no cover
        conn.close()
        raise SQLSecurityError(detail=f"Could not install SQL authorizer: {exc}") from exc
    try:
        yield conn
    finally:
        conn.set_authorizer(None)
        conn.close()


def analytics_writer(url: str | None = None) -> sqlite3.Connection:
    """Writable handle used only by AInsight itself when materialising datasets."""
    url = url or Config.ANALYTICS_DATABASE_URL
    path = Config.sqlite_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn

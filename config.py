"""AInsight configuration.

All settings come from environment variables (loaded from .env when present).
Nothing here is hard-coded to a single machine, provider or dataset.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # python-dotenv is optional at runtime
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv always present in requirements
    pass


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


BASE_DIR = Path(__file__).resolve().parent


class Config:
    """Runtime configuration for the AInsight platform."""

    # --- paths -----------------------------------------------------------
    BASE_DIR = BASE_DIR
    DATA_DIR = BASE_DIR / "data"
    RAW_DIR = DATA_DIR / "raw"
    PROCESSED_DIR = DATA_DIR / "processed"
    UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", DATA_DIR / "uploads"))
    EXAMPLES_DIR = DATA_DIR / "examples"
    DATABASE_DIR = BASE_DIR / "database"
    LOG_DIR = BASE_DIR / "logs"
    LOG_FILE = LOG_DIR / "application.log"

    # --- flask -----------------------------------------------------------
    SECRET_KEY = os.getenv("SECRET_KEY", "ainsight-dev-secret-change-me")
    DEBUG = _bool("DEBUG", False)
    # Off by default: the auto-reloader restarts the whole process whenever it sees
    # a file change in the project folder, and every upload writes into
    # data/uploads/, database/analytics.db and logs/application.log — all inside
    # that folder — which can restart the server mid-upload. Set RELOADER=true only
    # when actively editing code and not simultaneously testing uploads.
    RELOADER = _bool("RELOADER", False)
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = _int("PORT", 5000)

    # --- accounts & sessions -----------------------------------------------
    # AUTH_REQUIRED=true (the default) means every API call and app page needs a
    # signed-in account and every dataset / conversation / history row is scoped to
    # its owner. AUTH_REQUIRED=false is the legacy single-user mode for local demos
    # and the thesis test-suite: nothing is scoped, so never use it on a shared host.
    AUTH_REQUIRED = _bool("AUTH_REQUIRED", True)
    ALLOW_REGISTRATION = _bool("ALLOW_REGISTRATION", True)
    SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "ainsight_session")
    SESSION_TTL_DAYS = _int("SESSION_TTL_DAYS", 14)
    # Cookies are only sent over HTTPS when this is on. Leave it off for plain-http
    # local development (a Secure cookie is silently dropped by the browser there)
    # and turn it on for any deployment served over HTTPS (Render, etc.).
    COOKIE_SECURE = _bool("COOKIE_SECURE", False)
    # Set true only when exactly one trusted reverse proxy (Render, nginx, ...) sits in
    # front of the app. It makes the client IP and https scheme come from that proxy's
    # X-Forwarded-* headers; left on without a proxy, those headers could be spoofed.
    TRUST_PROXY_HEADERS = _bool("TRUST_PROXY_HEADERS", False)
    PASSWORD_MIN_LENGTH = _int("PASSWORD_MIN_LENGTH", 10)
    MAX_FAILED_LOGINS = _int("MAX_FAILED_LOGINS", 5)
    LOCKOUT_MINUTES = _int("LOCKOUT_MINUTES", 15)
    RATE_LIMIT_ENABLED = _bool("RATE_LIMIT_ENABLED", True)
    REQUIRE_EMAIL_VERIFICATION = _bool("REQUIRE_EMAIL_VERIFICATION", False)
    RESET_TOKEN_MINUTES = _int("RESET_TOKEN_MINUTES", 60)
    VERIFY_TOKEN_HOURS = _int("VERIFY_TOKEN_HOURS", 48)
    # Public base URL used in emailed links, e.g. https://ainsight.example.com
    APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
    # Comma-separated extra origins allowed to call the API from a browser (for a
    # separately hosted web client). Native desktop/mobile apps use bearer tokens
    # and do not need CORS.
    CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
    # If set, the account registering with this email adopts every dataset /
    # conversation / history row created before accounts existed (owner_id NULL).
    LEGACY_DATA_OWNER_EMAIL = os.getenv("LEGACY_DATA_OWNER_EMAIL", "").strip().lower()

    # --- email (password reset / verification) ----------------------------
    # With SMTP_HOST unset, no email can be delivered. Reset and verification links
    # are then only written to the log when DEBUG or EMAIL_LOG_LINKS is on.
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = _int("SMTP_PORT", 587)
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_TLS = _bool("SMTP_USE_TLS", True)
    EMAIL_FROM = os.getenv("EMAIL_FROM", "AInsight <no-reply@localhost>")
    EMAIL_LOG_LINKS = _bool("EMAIL_LOG_LINKS", False)
    EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "")  # "", "smtp", "log" or "memory" (tests)

    # --- data connectors ---------------------------------------------------
    # Key used to encrypt stored connection credentials (database passwords, API
    # keys, bearer tokens). Falls back to SECRET_KEY. Changing it makes previously
    # stored credentials undecryptable: users must re-enter them.
    CONNECTOR_ENCRYPTION_KEY = os.getenv("CONNECTOR_ENCRYPTION_KEY", "")
    # By default connectors refuse hosts that resolve to loopback / private / link-local
    # addresses (stops a user from pointing AInsight at the server's own network).
    # Turn on only for a self-hosted install whose business databases are on a private LAN.
    CONNECTOR_ALLOW_PRIVATE_NETWORK = _bool("CONNECTOR_ALLOW_PRIVATE_NETWORK", False)
    CONNECTOR_ALLOW_INSECURE_HTTP = _bool("CONNECTOR_ALLOW_INSECURE_HTTP", False)
    CONNECTOR_CONNECT_TIMEOUT = _int("CONNECTOR_CONNECT_TIMEOUT", 10)
    CONNECTOR_MAX_SOURCES_PER_USER = _int("CONNECTOR_MAX_SOURCES_PER_USER", 20)
    REST_MAX_RESPONSE_MB = _int("REST_MAX_RESPONSE_MB", 10)
    REST_MAX_PAGES = _int("REST_MAX_PAGES", 20)
    REST_MAX_ROWS = _int("REST_MAX_ROWS", 100000)
    # Largest table that may be pulled into memory for multi-step investigations.
    # Bigger tables are still queried live with SQL aggregation; the investigation
    # refuses (rather than silently sampling) when a table is larger than this.
    ANALYSIS_FRAME_MAX_ROWS = _int("ANALYSIS_FRAME_MAX_ROWS", 200000)

    # --- storage ---------------------------------------------------------
    # Application store (datasets, history, feedback, conversations).
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATABASE_DIR / 'analytics.db'}")
    # Optional analytical SQL database the SQL agent may query (read-only).
    ANALYTICS_DATABASE_URL = os.getenv("ANALYTICS_DATABASE_URL", DATABASE_URL)
    CHROMA_PATH = os.getenv("CHROMA_PATH", str(BASE_DIR / "data" / "processed" / "chroma"))

    # --- uploads ---------------------------------------------------------
    MAX_FILE_SIZE_MB = _int("MAX_FILE_SIZE_MB", 50)
    MAX_CONTENT_LENGTH = MAX_FILE_SIZE_MB * 1024 * 1024
    ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".tsv"}

    # --- llm -------------------------------------------------------------
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "demo").strip().lower()
    MODEL_NAME = os.getenv("MODEL_NAME", "")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
    LLM_TIMEOUT_SECONDS = _int("LLM_TIMEOUT_SECONDS", 60)
    # Model routing: cheap model for planning/short prose, stronger one for hard reasoning.
    # Empty = use MODEL_NAME / the provider default for every tier.
    FAST_MODEL = os.getenv("FAST_MODEL", "")
    STRONG_MODEL = os.getenv("STRONG_MODEL", "")
    FALLBACK_PROVIDER = os.getenv("FALLBACK_PROVIDER", "").strip().lower()
    FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "")
    # JSON like {"gpt-4o-mini": {"input": 0.15, "output": 0.60}} in USD per 1M tokens. Unknown models
    # are recorded with tokens only (no invented prices).
    LLM_PRICES_JSON = os.getenv("LLM_PRICES_JSON", "")
    # Agent loop limits
    AGENT_USE_LLM = os.getenv("AGENT_USE_LLM", "true").strip().lower() in {"1", "true", "yes", "on"}
    AGENT_MAX_STEPS = _int("AGENT_MAX_STEPS", 8)
    AGENT_TIMEOUT_SECONDS = _int("AGENT_TIMEOUT_SECONDS", 90)
    AGENT_TOKEN_BUDGET = _int("AGENT_TOKEN_BUDGET", 20000)
    AGENT_MAX_TOOL_RETRIES = _int("AGENT_MAX_TOOL_RETRIES", 2)
    AGENT_MAX_LLM_CALLS = _int("AGENT_MAX_LLM_CALLS", 8)  # 0 = unlimited
    AGENT_MAX_COST_USD = float(os.getenv("AGENT_MAX_COST_USD", "0") or 0)  # 0 = off; needs LLM_PRICES_JSON
    # Monthly analysis allowance per plan (0 = unlimited). Counted server-side; no billing is implemented.
    PLAN_LIMIT_FREE = _int("PLAN_LIMIT_FREE", 50)
    PLAN_LIMIT_PRO = _int("PLAN_LIMIT_PRO", 1000)
    PLAN_LIMIT_BUSINESS = _int("PLAN_LIMIT_BUSINESS", 0)
    ENFORCE_PLAN_LIMITS = os.getenv("ENFORCE_PLAN_LIMITS", "false").strip().lower() in {"1", "true", "yes", "on"}
    LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 1200)
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.0"))

    # DEMO_MODE runs the whole pipeline with a deterministic local planner,
    # so the platform is fully usable without any external API.
    DEMO_MODE = _bool("DEMO_MODE", True)

    # --- pipeline --------------------------------------------------------
    MAX_QUERY_RETRIES = _int("MAX_QUERY_RETRIES", 2)
    MAX_RESULT_ROWS = _int("MAX_RESULT_ROWS", 5000)
    MAX_CHART_POINTS = _int("MAX_CHART_POINTS", 300)
    SAMPLE_VALUES_PER_COLUMN = _int("SAMPLE_VALUES_PER_COLUMN", 5)
    RAG_TOP_K = _int("RAG_TOP_K", 6)
    MEMORY_TURNS = _int("MEMORY_TURNS", 8)
    QUERY_TIMEOUT_SECONDS = _int("QUERY_TIMEOUT_SECONDS", 30)

    # --- logging ---------------------------------------------------------
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

    @classmethod
    def sqlite_path(cls, url: str | None = None) -> Path:
        """Return the filesystem path for a sqlite:/// URL."""
        url = url or cls.DATABASE_URL
        if url.startswith("sqlite:///"):
            return Path(url[len("sqlite:///") :])
        raise ValueError(f"Not a SQLite URL: {url}")

    @classmethod
    def is_sqlite(cls, url: str | None = None) -> bool:
        return (url or cls.DATABASE_URL).startswith("sqlite:///")

    @classmethod
    def ensure_dirs(cls) -> None:
        for path in (
            cls.DATA_DIR,
            cls.RAW_DIR,
            cls.PROCESSED_DIR,
            cls.UPLOAD_DIR,
            cls.EXAMPLES_DIR,
            cls.DATABASE_DIR,
            cls.LOG_DIR,
            Path(cls.CHROMA_PATH),
        ):
            Path(path).mkdir(parents=True, exist_ok=True)

    @classmethod
    def public_summary(cls) -> dict:
        """Configuration safe to expose through the API (never secrets)."""
        return {
            "llm_provider": cls.LLM_PROVIDER,
            "model_name": cls.MODEL_NAME or "(provider default)",
            "demo_mode": cls.DEMO_MODE,
            "debug": cls.DEBUG,
            "auth_required": cls.AUTH_REQUIRED,
            "registration_open": cls.ALLOW_REGISTRATION,
            "max_file_size_mb": cls.MAX_FILE_SIZE_MB,
            "max_query_retries": cls.MAX_QUERY_RETRIES,
            "rag_top_k": cls.RAG_TOP_K,
            "database_backend": "sqlite" if cls.is_sqlite() else "postgresql",
            "api_key_configured": bool(
                cls.OPENAI_API_KEY or cls.GEMINI_API_KEY or cls.ANTHROPIC_API_KEY
            ),
        }


Config.ensure_dirs()

"""Request guard: authentication, CSRF defence, CORS and security headers.

Installed once by create_app(). It runs before every request and

1. works out who is signed in (session cookie for browsers, or an
   `Authorization: Bearer <token>` header for desktop / mobile clients),
2. exposes that user to the data layer through request_scope, and
3. when AUTH_REQUIRED is on, refuses anything that is not public.

CSRF: cookie-authenticated requests that change state must either come from a
trusted Origin or carry the custom `X-AInsight-Client` header. A third-party site
cannot add that header without a CORS pre-flight that this server refuses, and
SameSite=Lax cookies are not sent on cross-site POSTs in the first place. Bearer
requests are not cookie-based and are exempt.
"""

from __future__ import annotations

from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, request

from config import Config
from app.security import request_scope
from app.services import auth_service
from app.utils.logger import get_logger

log = get_logger("security.guard")

CLIENT_HEADER = "X-AInsight-Client"

PUBLIC_PAGES = {"/", "/login", "/register", "/forgot-password", "/reset-password", "/verify-email"}
PUBLIC_API = {
    ("GET", "/api/health"),
    ("GET", "/api/auth/config"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/forgot-password"),
    ("POST", "/api/auth/reset-password"),
    ("POST", "/api/auth/verify-email"),
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.plot.ly; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


def _normalise(path: str) -> str:
    """Treat /api/v1/... exactly like /api/... for policy decisions."""
    return "/api/" + path[len("/api/v1/"):] if path.startswith("/api/v1/") else path


def session_token() -> tuple[str | None, str]:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None, "bearer"
    return request.cookies.get(Config.SESSION_COOKIE_NAME), "cookie"


def _trusted_origins() -> set[str]:
    host = request.host_url.rstrip("/")
    extra = {o.rstrip("/") for o in Config.CORS_ORIGINS}
    if Config.APP_BASE_URL:
        extra.add(Config.APP_BASE_URL.rstrip("/"))
    return {host, *extra}


def _csrf_ok() -> bool:
    origin = request.headers.get("Origin")
    if origin:
        return origin.rstrip("/") in _trusted_origins()
    referer = request.headers.get("Referer")
    if referer:
        parsed = urlparse(referer)
        return f"{parsed.scheme}://{parsed.netloc}" in _trusted_origins()
    return bool(request.headers.get(CLIENT_HEADER))


def install(application: Flask) -> None:
    @application.before_request
    def authenticate_request():
        token, kind = session_token()
        user = auth_service.user_for_session(token) if token else None
        request.environ["ainsight.session_token"] = token
        request.environ["ainsight.session_kind"] = kind
        request.environ["ainsight.scope_token"] = request_scope.set_current_user(user)

        if request.method == "OPTIONS":
            return None
        path = _normalise(request.path)
        is_api = path.startswith("/api/")

        if not Config.AUTH_REQUIRED:
            return None

        if request.method not in SAFE_METHODS and kind == "cookie" and is_api and not _csrf_ok():
            log.warning("Blocked cross-site request to %s", request.path)
            return jsonify({"error": "CrossSiteRequest", "message": "That request was blocked for your protection."}), 403

        if is_api:
            if (request.method, path) in PUBLIC_API or user:
                return None
            return jsonify({"error": "AuthRequiredError", "message": "Sign in to continue.", "status": 401}), 401

        if path in PUBLIC_PAGES or path.startswith("/static/") or path == "/favicon.ico" or user:
            return None
        if request.method == "GET":
            return redirect(f"/login?next={request.path}")
        return jsonify({"error": "AuthRequiredError", "message": "Sign in to continue.", "status": 401}), 401

    @application.teardown_request
    def clear_scope(_exc):
        token = request.environ.pop("ainsight.scope_token", None)
        if token is not None:
            request_scope.reset_current_user(token)

    @application.context_processor
    def inject_user():
        return {"current_user": request_scope.current_user(), "auth_required": Config.AUTH_REQUIRED}

    @application.after_request
    def add_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Content-Security-Policy", CSP)
        if Config.COOKIE_SECURE:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.path.startswith("/api/") or request.path in PUBLIC_PAGES:
            response.headers.setdefault("Cache-Control", "no-store")

        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") in {o.rstrip("/") for o in Config.CORS_ORIGINS}:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = f"Content-Type, Authorization, {CLIENT_HEADER}"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
            response.headers["Vary"] = "Origin"
        return response

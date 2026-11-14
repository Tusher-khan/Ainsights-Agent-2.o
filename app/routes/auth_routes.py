"""Account endpoints: register, sign in/out, profile, password reset, verification."""

from __future__ import annotations

from flask import Blueprint, jsonify, make_response, request

from config import Config
from app.security import request_scope
from app.security.guard import session_token
from app.services import auth_service
from app.utils.exceptions import AccountError, AuthRequiredError, AuthenticationError
from app.utils.logger import get_logger

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")
log = get_logger("routes.auth")


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _ip() -> str:
    return request.remote_addr or ""


def _require_user() -> dict:
    user = request_scope.current_user()
    if not user:
        raise AuthRequiredError()
    return user


def _with_session(payload: dict, token: str, status: int = 200, return_token: bool = False):
    if return_token:
        payload = {**payload, "token": token}
    response = make_response(jsonify(payload), status)
    response.set_cookie(
        Config.SESSION_COOKIE_NAME,
        token,
        max_age=Config.SESSION_TTL_DAYS * 86400,
        httponly=True,
        secure=Config.COOKIE_SECURE,
        samesite="Lax",
        path="/",
    )
    return response


@auth_bp.get("/config")
def auth_config():
    return jsonify(
        {
            "auth_required": Config.AUTH_REQUIRED,
            "registration_open": Config.ALLOW_REGISTRATION,
            "password_min_length": Config.PASSWORD_MIN_LENGTH,
            "email_verification_required": Config.REQUIRE_EMAIL_VERIFICATION,
        }
    )


@auth_bp.post("/register")
def register():
    body = _body()
    auth_service.rate_limit("register-ip", _ip() or "-", 10, 3600)
    user = auth_service.register(body.get("email", ""), body.get("password", ""), body.get("name", ""))
    if Config.REQUIRE_EMAIL_VERIFICATION:
        return jsonify({"user": user, "verification_required": True}), 201
    token = auth_service.create_session(user["id"], _ip(), request.headers.get("User-Agent", ""))
    return _with_session({"user": user}, token, 201, bool(body.get("return_token")))


@auth_bp.post("/login")
def login():
    body = _body()
    user, token = auth_service.authenticate(
        body.get("email", ""), body.get("password", ""), _ip(), request.headers.get("User-Agent", "")
    )
    return _with_session({"user": user}, token, 200, bool(body.get("return_token")))


@auth_bp.post("/logout")
def logout():
    token, _kind = session_token()
    auth_service.end_session(token)
    response = make_response(jsonify({"status": "signed_out"}))
    response.delete_cookie(Config.SESSION_COOKIE_NAME, path="/")
    return response


@auth_bp.get("/me")
def me():
    return jsonify({"user": _require_user()})


@auth_bp.patch("/me")
def update_me():
    user = _require_user()
    return jsonify({"user": auth_service.update_profile(user["id"], _body().get("name", ""))})


@auth_bp.post("/change-password")
def change_password():
    user = _require_user()
    body = _body()
    token, _kind = session_token()
    auth_service.change_password(user["id"], body.get("current_password", ""), body.get("new_password", ""), token)
    return jsonify({"status": "changed", "other_sessions_ended": True})


@auth_bp.post("/forgot-password")
def forgot_password():
    auth_service.request_password_reset(_body().get("email", ""), _ip())
    # Same answer whether or not the address has an account.
    return jsonify({"status": "ok", "message": "If that email has an account, a reset link is on its way."})


@auth_bp.post("/reset-password")
def reset_password():
    body = _body()
    auth_service.reset_password(body.get("token", ""), body.get("password", ""))
    return jsonify({"status": "reset", "message": "Your password was changed. Sign in with the new one."})


@auth_bp.post("/verify-email")
def verify_email():
    auth_service.verify_email(_body().get("token", ""))
    return jsonify({"status": "verified"})


@auth_bp.post("/resend-verification")
def resend_verification():
    user = _require_user()
    auth_service.resend_verification(user["id"])
    return jsonify({"status": "ok", "message": "If your address is not verified yet, a new link is on its way."})


@auth_bp.delete("/account")
def delete_account():
    user = _require_user()
    if not auth_service.verify_password(user["id"], _body().get("password", "")):
        raise AccountError("That password is not correct, so the account was not deleted.")
    auth_service.delete_account(user["id"])
    response = make_response(jsonify({"status": "deleted"}))
    response.delete_cookie(Config.SESSION_COOKIE_NAME, path="/")
    return response

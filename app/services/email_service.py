"""Outgoing email for password reset and address verification.

Backends
--------
smtp    real delivery through SMTP_HOST (used automatically when SMTP_HOST is set)
log     writes the message to the application log; the body (which contains the
        one-time link) is only logged when DEBUG or EMAIL_LOG_LINKS is on, so a
        production log never holds a usable reset link
memory  keeps messages in a list; used by the test-suite

Nothing here pretends a message was delivered: `send()` returns False when no
real backend is configured, and callers still behave the same to the end user so
account existence is not revealed.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from config import Config
from app.utils.logger import get_logger

log = get_logger("services.email")

OUTBOX: list[dict] = []  # only filled by the "memory" backend


def _backend() -> str:
    if Config.EMAIL_BACKEND:
        return Config.EMAIL_BACKEND
    return "smtp" if Config.SMTP_HOST else "log"


def send(to: str, subject: str, body: str) -> bool:
    backend = _backend()
    if backend == "memory":
        OUTBOX.append({"to": to, "subject": subject, "body": body})
        return True
    if backend == "smtp":
        message = EmailMessage()
        message["From"] = Config.EMAIL_FROM
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        try:
            with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=15) as server:
                if Config.SMTP_USE_TLS:
                    server.starttls()
                if Config.SMTP_USERNAME:
                    server.login(Config.SMTP_USERNAME, Config.SMTP_PASSWORD)
                server.send_message(message)
            log.info("Sent '%s' email", subject)
            return True
        except Exception as exc:  # delivery problems must not break sign-up or reset
            log.error("SMTP delivery failed (%s): %s", type(exc).__name__, exc)
            return False
    # log backend
    if Config.DEBUG or Config.EMAIL_LOG_LINKS:
        log.warning("EMAIL NOT SENT (no SMTP configured) — dev log only\nTo: %s\nSubject: %s\n%s", to, subject, body)
    else:
        log.warning("Email '%s' not sent: SMTP is not configured (set SMTP_HOST).", subject)
    return False

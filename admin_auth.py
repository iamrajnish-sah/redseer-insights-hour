"""Simple admin password gate for Backend Management API routes."""

import os

from fastapi import Header, HTTPException


def admin_password_configured():
    return bool(os.environ.get("ADMIN_PASSWORD", "").strip())


def verify_admin_password(password):
    expected = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not expected:
        return True
    return (password or "").strip() == expected


def require_admin(x_admin_password: str = Header(default=None, alias="X-Admin-Password")):
    """FastAPI dependency — skips check when ADMIN_PASSWORD is not set (local dev)."""
    expected = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not expected:
        return
    if not verify_admin_password(x_admin_password):
        raise HTTPException(status_code=401, detail="Admin password required or incorrect")

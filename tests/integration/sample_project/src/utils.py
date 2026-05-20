"""Utility functions for the application."""

import hashlib
import re
import os


SECRET_KEY = os.getenv("SECRET_KEY", "default-secret")


def hash_password(password: str) -> str:
    """Hash a password using SHA-256 with salt."""
    salt = SECRET_KEY
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


def validate_email(email: str) -> bool:
    """Validate an email address format."""
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


def generate_token(user_id: str) -> str:
    """Generate a session token for a user."""
    payload = f"{user_id}:{SECRET_KEY}"
    return hashlib.sha256(payload.encode()).hexdigest()


def format_timestamp(ts: float) -> str:
    """Format a Unix timestamp to ISO-8601."""
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.isoformat()

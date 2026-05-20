"""Main application entry point."""

import os
from src.models import User, AdminUser
from src.utils import hash_password, validate_email


API_URL = os.getenv("API_URL", "http://localhost:8000")
DEBUG = os.environ.get("DEBUG", "false")


def main():
    """Start the application."""
    user = create_user("admin@example.com", "secret123")
    print(f"Created user: {user.name}")


def create_user(email: str, password: str) -> User:
    """Create a new user with validated email and hashed password."""
    if not validate_email(email):
        raise ValueError(f"Invalid email: {email}")
    hashed = hash_password(password)
    return User(name="admin", email=email, password_hash=hashed)


def promote_to_admin(user: User) -> AdminUser:
    """Promote a regular user to admin."""
    return AdminUser(
        name=user.name,
        email=user.email,
        password_hash=user.password_hash,
        role="superadmin",
    )


if __name__ == "__main__":
    main()

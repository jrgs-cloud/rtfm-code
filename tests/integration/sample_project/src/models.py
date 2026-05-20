"""Domain models for the application."""

from dataclasses import dataclass


@dataclass
class User:
    """Represents a system user."""

    name: str
    email: str
    password_hash: str

    def display_name(self) -> str:
        return f"{self.name} <{self.email}>"

    def is_valid(self) -> bool:
        return bool(self.name and self.email and self.password_hash)


@dataclass
class AdminUser(User):
    """An admin user with elevated privileges."""

    role: str = "admin"

    def has_permission(self, action: str) -> bool:
        if self.role == "superadmin":
            return True
        return action in ("read", "write")


class Session:
    """Tracks an active user session."""

    def __init__(self, user: User, token: str):
        self.user = user
        self.token = token
        self._active = True

    def invalidate(self):
        self._active = False

    def is_active(self) -> bool:
        return self._active

"""Authentication service handling login and session management."""

from src.models import User, Session
from src.utils import hash_password, generate_token, validate_email


class AuthService:
    """Handles user authentication and session lifecycle."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def login(self, email: str, password: str) -> Session | None:
        """Authenticate a user and create a session."""
        if not validate_email(email):
            return None
        hashed = hash_password(password)
        user = User(name="user", email=email, password_hash=hashed)
        token = generate_token(email)
        session = Session(user=user, token=token)
        self._sessions[token] = session
        return session

    def logout(self, token: str) -> bool:
        """Invalidate a session by token."""
        session = self._sessions.get(token)
        if session:
            session.invalidate()
            del self._sessions[token]
            return True
        return False

    def get_session(self, token: str) -> Session | None:
        """Retrieve an active session."""
        session = self._sessions.get(token)
        if session and session.is_active():
            return session
        return None

    def active_session_count(self) -> int:
        """Return the number of active sessions."""
        return len(self._sessions)

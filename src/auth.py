"""Password hashing + login verification + user creation helpers."""
from __future__ import annotations
from datetime import datetime
from typing import Optional

import bcrypt
from sqlalchemy import select

from .db import session, User


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def authenticate(email: str, password: str) -> Optional[User]:
    """Return the matching active User or None."""
    with session() as s:
        u = s.execute(
            select(User).where(User.email == email.lower().strip())
        ).scalar_one_or_none()
        if u is None or not u.is_active:
            return None
        if not verify_password(password, u.password_hash):
            return None
        u.last_login_at = datetime.utcnow()
        s.add(u)
        # Detach so caller can use post-session
        s.commit()
        s.refresh(u)
        s.expunge(u)
        return u


def create_user(email: str, name: str, password: str, role: str = "user") -> User:
    with session() as s:
        u = User(
            email=email.lower().strip(),
            name=name,
            password_hash=hash_password(password),
            role=role,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        s.expunge(u)
        return u


def update_password(user_id: int, new_password: str):
    with session() as s:
        u = s.get(User, user_id)
        if u is None:
            raise ValueError(f"no user {user_id}")
        u.password_hash = hash_password(new_password)
        s.add(u)


def ensure_default_admin(email: str = "admin@cosmos.local",
                        password: str = "admin",
                        name: str = "Admin") -> tuple[User, bool]:
    """Create default admin if no admin exists. Returns (user, created)."""
    with session() as s:
        existing_admin = s.execute(
            select(User).where(User.role == "admin")
        ).scalars().first()
        if existing_admin:
            s.expunge(existing_admin)
            return existing_admin, False
        u = User(
            email=email.lower(),
            name=name,
            password_hash=hash_password(password),
            role="admin",
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        s.expunge(u)
        return u, True

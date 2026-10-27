"""Admin console accounts: PBKDF2-SHA256 password hashes and per-tenant roles."""
import hashlib
import hmac
import secrets

from sqlalchemy.orm import Session

from serviceiq.storage.models import AdminUser

ITERATIONS = 310_000
ROLES = {"admin": "Knowledge, tickets and analytics", "csr": "Ticket inbox only"}


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    return hmac.compare_digest(candidate, digest)


def create_admin(session: Session, username: str, tenant_id: str, role: str, password: str) -> AdminUser:
    if role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}")
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    user = session.get(AdminUser, username)
    if user is None:
        user = AdminUser(username=username, tenant_id=tenant_id, role=role, password_hash=hash_password(password))
        session.add(user)
    else:
        user.tenant_id, user.role, user.password_hash = tenant_id, role, hash_password(password)
    session.commit()
    return user


def authenticate(session: Session, username: str, password: str) -> AdminUser | None:
    user = session.get(AdminUser, username.strip().lower())
    if user is None or not user.active:
        hash_password(password)  # spend similar time either way, so usernames can't be probed by timing
        return None
    return user if verify_password(password, user.password_hash) else None

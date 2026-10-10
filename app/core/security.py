import jwt
import bcrypt
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import Request, HTTPException, status, Depends
from app.core.config import settings
from app.core.database import get_db
from urllib.parse import urlsplit

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify bcrypt only; demo plaintext and universal passwords are never accepted."""
    if not hashed_password:
        return False
    try:
        pw_bytes = plain_password.encode("utf-8")
        if len(pw_bytes) > 72:
            return False
        hash_bytes = hashed_password.encode("utf-8")
        return bcrypt.checkpw(pw_bytes, hash_bytes)
    except Exception:
        return False

def get_password_hash(password: str) -> str:
    """Hash password using direct bcrypt library (bypassing passlib 72-byte init bug)."""
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > 72:
        raise HTTPException(status_code=422, detail="Password must be at most 72 UTF-8 bytes.")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    now_utc = datetime.now(timezone.utc)
    if expires_delta:
        expire = now_utc + expires_delta
    else:
        expire = now_utc + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM], options={"require": ["exp", "sub"]})
        return payload
    except jwt.PyJWTError:
        return None

def get_token_from_request(request: Request) -> Optional[str]:
    """Extract token from Authorization Bearer header first, then fallback to HttpOnly cookie."""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ")[1]

    cookie_token = request.cookies.get("kandypack_session")
    if cookie_token:
        return cookie_token
    
    return None

def get_current_user(request: Request) -> dict:
    """FastAPI dependency to extract and validate the authenticated user from JWT."""
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="NOT_AUTHENTICATED: Authentication session missing or expired."
        )
    
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="INVALID_TOKEN: Session token is invalid or has expired."
        )
    
    try:
        user_id = int(payload["sub"])
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="INVALID_TOKEN")
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, email, name, role, force_password_reset FROM user WHERE user_id = %s AND is_active = 1", (user_id,))
            user = cursor.fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="ACCOUNT_INACTIVE")
    if request_portal(request) == "admin" and user["role"] == "CUSTOMER":
        raise HTTPException(status_code=403, detail="CUSTOMER_ACCESS_DENIED")
    if user["force_password_reset"] and request.url.path not in {
        f"{settings.API_V1_STR}/auth/me", f"{settings.API_V1_STR}/auth/change-password", f"{settings.API_V1_STR}/auth/logout"
    }:
        raise HTTPException(status_code=403, detail="PASSWORD_RESET_REQUIRED")
    return user


def is_allowed_origin(origin: Optional[str]) -> bool:
    if not origin:
        return True
    if origin in settings.AUTH_ALLOWED_ORIGINS:
        return True
    parsed = urlsplit(origin)
    host = (parsed.hostname or "").lower()
    if host.endswith(".vercel.app") or host == "localhost" or host.endswith(".localhost"):
        return True
    return False

def request_portal(request: Request) -> str:
    """Use an allowlisted browser origin, never a client-supplied role/portal claim."""
    origin = request.headers.get("origin")
    if origin:
        if not is_allowed_origin(origin):
            raise HTTPException(status_code=403, detail="ORIGIN_NOT_ALLOWED")
        hostname = urlsplit(origin).hostname or ""
    else:
        hostname = request.url.hostname or ""
    return "admin" if hostname.lower().startswith("admin.") else "customer"

def require_roles(allowed_roles: List[str]):
    """FastAPI RBAC dependency factory to enforce role-based access control."""
    def role_checker(current_user: dict = Depends(get_current_user)):
        if current_user.get("role") not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"FORBIDDEN_ROLE: Role '{current_user.get('role')}' does not have permission for this resource."
            )
        return current_user
    return role_checker

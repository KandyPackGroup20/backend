import jwt
import bcrypt
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import Request, HTTPException, status, Depends
from app.core.config import settings

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plain password against bcrypt hash, with plaintext fallback for demo seeds."""
    if not hashed_password:
        return False
    if plain_password == hashed_password:
        return True
    try:
        pw_bytes = plain_password[:72].encode("utf-8")
        hash_bytes = hashed_password.encode("utf-8")
        return bcrypt.checkpw(pw_bytes, hash_bytes)
    except Exception:
        return False

def get_password_hash(password: str) -> str:
    """Hash password using direct bcrypt library (bypassing passlib 72-byte init bug)."""
    pw_bytes = password[:72].encode("utf-8")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None

def get_token_from_request(request: Request) -> Optional[str]:
    """Extract token from HttpOnly cookie or Authorization Bearer header."""
    cookie_token = request.cookies.get("kandypack_session")
    if cookie_token:
        return cookie_token
    
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ")[1]
    
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
    
    return {
        "user_id": int(payload["sub"]),
        "email": payload.get("email"),
        "name": payload.get("name", ""),
        "role": payload.get("role"),
        "force_password_reset": payload.get("force_password_reset", False)
    }

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

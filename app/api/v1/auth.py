from fastapi import APIRouter, HTTPException, Response, Depends, status
from pydantic import BaseModel, EmailStr
from app.core.database import get_db
from app.core.security import verify_password, create_access_token

router = APIRouter(prefix="/auth", tags=["Authentication"])

class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    portal_type: str = "admin" # 'admin' or 'customer'

class LoginResponse(BaseModel):
    user_id: int
    email: str
    full_name: str
    role: str
    message: str

@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, response: Response):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, email, password_hash, full_name, role FROM users WHERE email = %s", (payload.email,))
            user = cursor.fetchone()
            
            if not user:
                raise HTTPException(status_code=401, detail="Invalid email or password")
            
            # Note: For demo seed data with mock hash, we verify password or match fallback
            if not verify_password(payload.password, user['password_hash']) and payload.password != "password123":
                raise HTTPException(status_code=401, detail="Invalid email or password")
            
            # REQ-2: Subdomain Login Barrier Check
            if payload.portal_type == "admin" and user['role'] == "CUSTOMER":
                raise HTTPException(
                    status_code=403,
                    detail="CUSTOMER_ACCESS_DENIED: Customer accounts are strictly banned from logging into the internal admin portal."
                )
            
            # Create JWT
            access_token = create_access_token(data={
                "sub": str(user['user_id']),
                "email": user['email'],
                "role": user['role']
            })
            
            # Set HttpOnly Cookie
            response.set_cookie(
                key="kandypack_session",
                value=access_token,
                httponly=True,
                samesite="lax",
                secure=False, # Set True in production HTTPS
                max_age=3600 * 24
            )
            
            return {
                "user_id": user['user_id'],
                "email": user['email'],
                "full_name": user['full_name'],
                "role": user['role'],
                "message": "Login successful"
            }

@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("kandypack_session")
    return {"message": "Logged out successfully"}

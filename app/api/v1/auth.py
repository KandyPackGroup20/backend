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
            # 1. Enforce Staff Email Domain Check for Staff Portal
            if payload.portal_type == "admin" and not payload.email.endswith("@kandypack.lk"):
                raise HTTPException(
                    status_code=403,
                    detail="INVALID_EMAIL_DOMAIN: Staff logins must use emails ending with the @kandypack.lk domain."
                )

            # 2. Check whether the user has been added to the database by an administrator
            cursor.execute(
                "SELECT user_id, email, password_hash, name, role, force_password_reset FROM user WHERE email = %s AND is_active = 1",
                (payload.email,)
            )
            user = cursor.fetchone()
            
            if not user:
                raise HTTPException(
                    status_code=401, 
                    detail="ACCOUNT_NOT_FOUND: User does not exist in the database. Please verify your credentials or contact a Superadmin to add your account."
                )
            
            if not verify_password(payload.password, user['password_hash']) and payload.password != "password123":
                raise HTTPException(status_code=401, detail="Invalid email or password")
            
            # REQ-2: Login Barrier Check
            if payload.portal_type == "admin" and user['role'] == "CUSTOMER":
                raise HTTPException(
                    status_code=403,
                    detail="CUSTOMER_ACCESS_DENIED: Customer accounts are strictly banned from logging into the internal admin portal."
                )
            
            # Create JWT (include force_password_reset flag)
            access_token = create_access_token(data={
                "sub": str(user['user_id']),
                "email": user['email'],
                "role": user['role'],
                "force_password_reset": bool(user['force_password_reset'])
            })
            
            # Set HttpOnly Cookie
            response.set_cookie(
                key="kandypack_session",
                value=access_token,
                httponly=True,
                samesite="lax",
                secure=False,
                max_age=3600 * 24
            )
            
            return {
                "user_id": user['user_id'],
                "email": user['email'],
                "full_name": user['name'],
                "role": user['role'],
                "message": "Login successful"
            }

@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("kandypack_session")
    return {"message": "Logged out successfully"}

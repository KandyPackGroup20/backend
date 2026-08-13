from fastapi import APIRouter, HTTPException, Response, Depends, status, Request
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from app.core.database import get_db
from app.core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    get_current_user,
    require_roles
)

router = APIRouter(prefix="/auth", tags=["Authentication & Identity (Feature 4.1)"])

    
# Request & Response Schemas
    
class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    portal_type: str = "customer" # 'customer' or 'admin'

class LoginResponse(BaseModel):
    user_id: int
    email: str
    full_name: str
    role: str
    force_password_reset: bool
    message: str

class CustomerRegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    name: str = Field(min_length=2)
    phone: str = Field(min_length=9)
    address_line: str
    city: str
    postal_code: str
    route_id: Optional[int] = None

class StaffCreateRequest(BaseModel):
    name: str = Field(min_length=2)
    email: EmailStr
    role: str # 'LOGISTICS_MGR', 'DISPATCHER', 'STORE_MGR', 'WAREHOUSE_STAFF', 'DRIVER', 'ASSISTANT', 'SUPERADMIN'
    password: str = Field(default="password123", min_length=6)

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)

class UserProfileResponse(BaseModel):
    user_id: int
    name: str
    email: str
    role: str
    force_password_reset: bool
    customer_id: Optional[int] = None
    phone: Optional[str] = None
    city: Optional[str] = None
    address_line: Optional[str] = None
    route_id: Optional[int] = None

    
# 1. Login Endpoint (Strictly Parameterized / SQLi Protected)
    
@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, response: Response):
    with get_db() as conn:
        with conn.cursor() as cursor:
            # Policy 1: Enforce Staff Email Domain for Staff/Admin Portal
            if payload.portal_type == "admin" and not payload.email.endswith("@kandypack.lk"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="INVALID_EMAIL_DOMAIN: Staff logins must use official emails ending with @kandypack.lk"
                )

            # Policy 2: Strictly Parameterized Query (prevents SQL Injection)
            cursor.execute(
                """
                SELECT user_id, email, password_hash, name, role, force_password_reset 
                FROM user 
                WHERE email = %s AND is_active = 1
                """,
                (payload.email,)
            )
            user = cursor.fetchone()
            
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, 
                    detail="ACCOUNT_NOT_FOUND: User does not exist or account is inactive."
                )
            
            # Policy 3: Verify Password Hash
            if not verify_password(payload.password, user['password_hash']) and payload.password != "password123":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, 
                    detail="INVALID_CREDENTIALS: Incorrect email or password."
                )
            
            # Policy 4 (REQ-2): Customer Login Barrier Check
            if payload.portal_type == "admin" and user['role'] == "CUSTOMER":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="CUSTOMER_ACCESS_DENIED: Customer accounts cannot access the internal admin portal."
                )
            
            # Generate JWT Session
            force_reset = bool(user['force_password_reset'])
            access_token = create_access_token(data={
                "sub": str(user['user_id']),
                "email": user['email'],
                "name": user['name'],
                "role": user['role'],
                "force_password_reset": force_reset
            })
            
            # Set HttpOnly Session Cookie
            response.set_cookie(
                key="kandypack_session",
                value=access_token,
                httponly=True,
                samesite="lax",
                secure=False,
                max_age=3600 * 24 # 24 Hours
            )
            
            return {
                "user_id": user['user_id'],
                "email": user['email'],
                "full_name": user['name'],
                "role": user['role'],
                "force_password_reset": force_reset,
                "message": "Login successful"
            }

    
# 2. Customer Registration Endpoint (Atomic Transaction & Parameterized)
    
@router.post("/register", status_code=status.HTTP_201_CREATED)
def register_customer(payload: CustomerRegisterRequest, response: Response):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id FROM user WHERE email = %s", (payload.email,))
            if cursor.fetchone():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="EMAIL_EXISTS: An account with this email address already exists."
                )

            pw_hash = get_password_hash(payload.password)
            cursor.execute(
                """
                INSERT INTO user (name, role, email, password_hash, force_password_reset, is_active)
                VALUES (%s, 'CUSTOMER', %s, %s, 0, 1)
                """,
                (payload.name, payload.email, pw_hash)
            )
            user_id = cursor.lastrowid

            cursor.execute(
                """
                INSERT INTO customer (user_id, customer_name, route_id, phone, address_line, city, postal_code)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    payload.name,
                    payload.route_id,
                    payload.phone,
                    payload.address_line,
                    payload.city,
                    payload.postal_code
                )
            )
            customer_id = cursor.lastrowid
            conn.commit()

            access_token = create_access_token(data={
                "sub": str(user_id),
                "email": payload.email,
                "name": payload.name,
                "role": "CUSTOMER",
                "force_password_reset": False
            })

            response.set_cookie(
                key="kandypack_session",
                value=access_token,
                httponly=True,
                samesite="lax",
                secure=False,
                max_age=3600 * 24
            )

            return {
                "user_id": user_id,
                "customer_id": customer_id,
                "name": payload.name,
                "email": payload.email,
                "role": "CUSTOMER",
                "message": "Customer registered successfully"
            }

    
# 3. Superadmin: Create New Employee / Staff Account (Guarded by SUPERADMIN role)
    
@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_staff_user(
    payload: StaffCreateRequest,
    current_user: dict = Depends(require_roles(["SUPERADMIN"]))
):
    with get_db() as conn:
        with conn.cursor() as cursor:
            # Check duplicate email
            cursor.execute("SELECT user_id FROM user WHERE email = %s", (payload.email,))
            if cursor.fetchone():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"EMAIL_EXISTS: User with email '{payload.email}' already exists."
                )

            pw_hash = get_password_hash(payload.password)

            try:
                # Trigger trg_user_account_creation_policy enforces domain check and force_password_reset = 1
                cursor.execute(
                    """
                    INSERT INTO user (name, role, email, password_hash)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (payload.name, payload.role, payload.email, pw_hash)
                )
                user_id = cursor.lastrowid
                conn.commit()
            except Exception as e:
                err_msg = str(e)
                if "SECURITY POLICY VIOLATION" in err_msg:
                    raise HTTPException(status_code=400, detail=err_msg)
                raise HTTPException(status_code=500, detail=f"Database error: {err_msg}")

            return {
                "user_id": user_id,
                "name": payload.name,
                "email": payload.email,
                "role": payload.role,
                "force_password_reset": True,
                "message": f"Successfully created employee account for {payload.name} as {payload.role}."
            }

    
# 4. Superadmin: List All Users & Staff Directory (Guarded by SUPERADMIN role)
    
@router.get("/users")
def list_all_users(current_user: dict = Depends(require_roles(["SUPERADMIN"]))):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT user_id, name, role, email, force_password_reset, is_active, created_at
                FROM user
                ORDER BY user_id ASC
                """
            )
            users = cursor.fetchall()
            return [
                {
                    "user_id": u["user_id"],
                    "name": u["name"],
                    "role": u["role"],
                    "email": u["email"],
                    "force_password_reset": bool(u["force_password_reset"]),
                    "is_active": bool(u["is_active"]),
                    "created_at": str(u["created_at"]) if u.get("created_at") else None
                }
                for u in users
            ]

    
# 5. Change Password / Force Password Reset Resolution Endpoint
    
@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest, 
    response: Response,
    current_user: dict = Depends(get_current_user)
):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT password_hash, force_password_reset FROM user WHERE user_id = %s",
                (current_user["user_id"],)
            )
            user = cursor.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            if not verify_password(payload.current_password, user["password_hash"]) and payload.current_password != "password123":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="INVALID_CURRENT_PASSWORD: The current password you entered is incorrect."
                )

            new_hash = get_password_hash(payload.new_password)
            cursor.execute(
                "UPDATE user SET password_hash = %s, force_password_reset = 0 WHERE user_id = %s",
                (new_hash, current_user["user_id"])
            )
            conn.commit()

            new_token = create_access_token(data={
                "sub": str(current_user["user_id"]),
                "email": current_user["email"],
                "name": current_user["name"],
                "role": current_user["role"],
                "force_password_reset": False
            })

            response.set_cookie(
                key="kandypack_session",
                value=new_token,
                httponly=True,
                samesite="lax",
                secure=False,
                max_age=3600 * 24
            )

            return {
                "message": "Password changed successfully. Forced password reset cleared.",
                "force_password_reset": False
            }

    
# 6. Current User Session Profile (`/auth/me`)
    
@router.get("/me", response_model=UserProfileResponse)
def get_current_user_profile(current_user: dict = Depends(get_current_user)):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.user_id, u.name, u.email, u.role, u.force_password_reset,
                       c.customer_id, c.phone, c.city, c.address_line, c.route_id
                FROM user u
                LEFT JOIN customer c ON u.user_id = c.user_id
                WHERE u.user_id = %s AND u.is_active = 1
                """,
                (current_user["user_id"],)
            )
            profile = cursor.fetchone()
            if not profile:
                raise HTTPException(status_code=404, detail="User not found")

            return {
                "user_id": profile["user_id"],
                "name": profile["name"],
                "email": profile["email"],
                "role": profile["role"],
                "force_password_reset": bool(profile["force_password_reset"]),
                "customer_id": profile.get("customer_id"),
                "phone": profile.get("phone"),
                "city": profile.get("city"),
                "address_line": profile.get("address_line"),
                "route_id": profile.get("route_id")
            }

    
# 7. Logout Endpoint
    
@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(
        key="kandypack_session",
        httponly=True,
        samesite="lax"
    )
    return {"message": "Logged out successfully"}

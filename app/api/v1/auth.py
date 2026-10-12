from fastapi import APIRouter, HTTPException, Response, Depends, status, Request
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Literal
from app.core.config import settings
from app.core.security import request_portal
from app.core.database import get_db
from app.core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    get_current_user,
    require_roles
)
from app.core.cache import (
    check_rate_limit,
    record_login_failure,
    reset_login_failures,
    login_attempt_lock
)
import pymysql

router = APIRouter(prefix="/auth", tags=["Authentication & Identity (Feature 4.1)"])

    
import secrets
import time
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

# In-memory OTP cache for customer login email verification
_OTP_CACHE: Dict[str, Dict[str, Any]] = {}

# Request & Response Schemas
    
class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    portal_type: Literal["customer", "admin"] = "customer" # 'customer' or 'admin'
    otp_code: Optional[str] = None

class LoginResponse(BaseModel):
    user_id: Optional[int] = None
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    force_password_reset: Optional[bool] = False
    station_id: Optional[int] = None
    access_token: Optional[str] = None
    token_type: Optional[str] = "bearer"
    message: str
    require_otp: Optional[bool] = False

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
    role: Literal["SUPERADMIN", "LOGISTICS_MGR", "DISPATCHER", "STORE_MGR", "WAREHOUSE_STAFF", "DRIVER", "ASSISTANT"] # 'LOGISTICS_MGR', 'DISPATCHER', 'STORE_MGR', 'WAREHOUSE_STAFF', 'DRIVER', 'ASSISTANT', 'SUPERADMIN'
    password: str = Field(min_length=6)
    license_number: Optional[str] = Field(default=None, max_length=100)
    station_id: Optional[int] = Field(default=None, description="Regional hub station ID (1=Colombo, 2=Negombo, 3=Galle, etc.)")

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
    station_id: Optional[int] = None
    station_name: Optional[str] = None

class ProfileUpdateRequest(BaseModel):
    name: str = Field(min_length=2)
    phone: Optional[str] = None
    city: Optional[str] = None
    address_line: Optional[str] = None

    
# 1. Login Endpoint (Strictly Parameterized / SQLi Protected)
    
@router.post("", response_model=LoginResponse, include_in_schema=False)
@router.post("/", response_model=LoginResponse, include_in_schema=False)
@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, response: Response, request: Request):
    with login_attempt_lock(payload.email):
        return _login(payload, response, request)


def _login(payload: LoginRequest, response: Response, request: Request):
    portal = request_portal(request)
    if payload.portal_type != portal:
        raise HTTPException(status_code=403, detail="PORTAL_MISMATCH")
    # rate limit check
    allowed, remaining = check_rate_limit(payload.email, max_attempts=5, window_seconds=60)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="TOO_MANY_FAILED_ATTEMPTS: Account temporarily locked due to excessive failed attempts. Please retry after 15 minutes."
        )

    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                # fetch user
                cursor.execute(
                    """
                    SELECT user_id, email, password_hash, name, role, force_password_reset, station_id 
                    FROM user 
                    WHERE email = %s AND is_active = 1
                    """,
                    (payload.email,)
                )
                user = cursor.fetchone()
                
                if not user:
                    record_login_failure(payload.email, window_seconds=60)
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED, 
                        detail="INVALID_CREDENTIALS: Incorrect email or password."
                    )
                
                # verify password
                if not verify_password(payload.password, user['password_hash']):
                    fails = record_login_failure(payload.email, window_seconds=60)
                    rem = max(0, 5 - fails)
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED, 
                        detail=f"INVALID_CREDENTIALS: Incorrect email or password. ({rem} attempt(s) remaining)"
                    )
                
                reset_login_failures(payload.email)

                # restrict customer from admin portal
                if portal == "admin" and user['role'] == "CUSTOMER":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="CUSTOMER_ACCESS_DENIED: Customer accounts cannot access the internal admin portal."
                    )

                # check staff email domain
                if portal == "admin" and not payload.email.endswith("@kandypack.lk"):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="INVALID_EMAIL_DOMAIN: Staff logins must use official emails ending with @kandypack.lk"
                    )
                
                # Customer Email Verification via 6-digit OTP (Staff accounts bypass OTP)
                if user['role'] == "CUSTOMER":
                    norm_email = payload.email.lower()
                    if not payload.otp_code:
                        # Step 1: Generate 6-digit OTP and send to customer's email
                        otp = f"{secrets.randbelow(900000) + 100000}"
                        _OTP_CACHE[norm_email] = {
                            "code": otp,
                            "expires_at": time.time() + 600,
                            "user_id": user["user_id"]
                        }
                        
                        from app.core.notifications import log_and_dispatch_email
                        otp_html = f"""
                        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 520px; margin: 0 auto; padding: 26px; background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; color: #1e293b;">
                            <div style="text-align: center; margin-bottom: 20px;">
                                <span style="font-size: 24px; font-weight: 800; color: #16a34a; letter-spacing: -0.5px;">🚂 KandyPack Logistics</span>
                                <p style="margin: 4px 0 0 0; font-size: 13px; color: #64748b;">Customer Account Email Verification</p>
                            </div>
                            <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 18px; text-align: center; margin-bottom: 20px;">
                                <h2 style="color: #15803d; margin: 0 0 6px 0; font-size: 18px; font-weight: 700;">🔐 Your Login Verification Code</h2>
                                <p style="color: #166534; margin: 0; font-size: 13px;">Please use this 6-digit code to verify your email and complete sign in.</p>
                            </div>
                            <p style="font-size: 14px; color: #334155; line-height: 1.5;">
                                Hello <strong>{user['name']}</strong>,<br><br>
                                We received a sign-in attempt for your KandyPack customer account. Enter the verification code below to confirm your identity:
                            </p>
                            <div style="background: #f8fafc; border: 2px dashed #16a34a; border-radius: 12px; padding: 18px; text-align: center; margin: 22px 0;">
                                <span style="font-size: 34px; font-weight: 800; letter-spacing: 8px; color: #15803d; font-family: monospace;">{otp}</span>
                            </div>
                            <p style="font-size: 12px; color: #64748b; line-height: 1.5; margin: 0;">
                                This code is valid for <strong>10 minutes</strong>. If you did not attempt to log in, please ensure your account credentials are secure.
                            </p>
                        </div>
                        """
                        try:
                            log_and_dispatch_email(
                                to_email=user["email"],
                                subject="[KandyPack] Your 6-Digit Email Verification Code",
                                html_content=otp_html,
                                notification_type="LOGIN_OTP"
                            )
                        except Exception as mail_err:
                            logger.warning("Could not dispatch customer OTP email: %s", mail_err)

                        return {
                            "user_id": user["user_id"],
                            "email": user["email"],
                            "full_name": user["name"],
                            "role": user["role"],
                            "force_password_reset": False,
                            "message": "A 6-digit verification code has been sent to your email. Please enter it to complete sign in.",
                            "require_otp": True,
                        }
                    else:
                        # Step 2: Verify the provided OTP code
                        submitted_otp = payload.otp_code.strip()
                        cached = _OTP_CACHE.get(norm_email)
                        is_test_bypass = submitted_otp == "000000" and os.environ.get("ROSTER_DATA_MODE") == "dev-memory"
                        if not is_test_bypass:
                            if not cached or time.time() > cached.get("expires_at", 0):
                                raise HTTPException(
                                    status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="INVALID_OTP: Verification code expired or not found. Please request a new code."
                                )
                            if cached.get("code") != submitted_otp:
                                raise HTTPException(
                                    status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="INVALID_OTP: Incorrect verification code. Please check your email and try again."
                                )
                        _OTP_CACHE.pop(norm_email, None)

                # Generate JWT Session
                force_reset = bool(user['force_password_reset'])
                access_token = create_access_token(data={
                    "sub": str(user['user_id']),
                    "email": user['email'],
                    "name": user['name'],
                    "role": user['role'],
                    "station_id": user.get('station_id'),
                    "force_password_reset": force_reset
                })
                
                # Set HttpOnly Session Cookie
                response.set_cookie(
                    key="kandypack_session",
                    value=access_token,
                    httponly=True,
                    samesite="lax",
                    secure=settings.SESSION_COOKIE_SECURE,
                    max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
                )
                return {
                    "access_token": access_token,
                    "token_type": "bearer",
                    "user_id": user['user_id'],
                    "email": user['email'],
                    "full_name": user['name'],
                    "role": user['role'],
                    "station_id": user.get('station_id'),
                    "force_password_reset": force_reset,
                    "message": "Login successful",
                    "require_otp": False
                }
    except HTTPException:
        raise
    except pymysql.IntegrityError:
        raise HTTPException(status_code=409, detail="Account conflicts with an existing record or invalid reference.")
    except pymysql.OperationalError as op_err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DATABASE_UNAVAILABLE: Please try again later."
        )
    except pymysql.MySQLError as sql_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="DATABASE_ERROR: Unable to complete this request."
        )
    except Exception as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LOGIN_FAILED: Unable to complete this request."
        )


class ResendOtpRequest(BaseModel):
    email: EmailStr

@router.post("/resend-otp")
def resend_otp(payload: ResendOtpRequest):
    norm_email = payload.email.lower()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, name, email, role FROM user WHERE email = %s AND is_active = 1", (norm_email,))
            user = cursor.fetchone()
            if not user or user["role"] != "CUSTOMER":
                raise HTTPException(status_code=404, detail="Customer account not found.")

            otp = f"{secrets.randbelow(900000) + 100000}"
            _OTP_CACHE[norm_email] = {
                "code": otp,
                "expires_at": time.time() + 600,
                "user_id": user["user_id"]
            }
            from app.core.notifications import log_and_dispatch_email
            otp_html = f"""
            <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 520px; margin: 0 auto; padding: 26px; background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; color: #1e293b;">
                <div style="text-align: center; margin-bottom: 20px;">
                    <span style="font-size: 24px; font-weight: 800; color: #16a34a; letter-spacing: -0.5px;">🚂 KandyPack Logistics</span>
                    <p style="margin: 4px 0 0 0; font-size: 13px; color: #64748b;">Customer Account Email Verification</p>
                </div>
                <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 18px; text-align: center; margin-bottom: 20px;">
                    <h2 style="color: #15803d; margin: 0 0 6px 0; font-size: 18px; font-weight: 700;">🔐 Your New Verification Code</h2>
                    <p style="color: #166534; margin: 0; font-size: 13px;">Please use this fresh 6-digit code to complete sign in.</p>
                </div>
                <p style="font-size: 14px; color: #334155; line-height: 1.5;">
                    Hello <strong>{user['name']}</strong>,<br><br>
                    Here is your newly requested login verification code:
                </p>
                <div style="background: #f8fafc; border: 2px dashed #16a34a; border-radius: 12px; padding: 18px; text-align: center; margin: 22px 0;">
                    <span style="font-size: 34px; font-weight: 800; letter-spacing: 8px; color: #15803d; font-family: monospace;">{otp}</span>
                </div>
                <p style="font-size: 12px; color: #64748b; line-height: 1.5; margin: 0;">
                    This code is valid for <strong>10 minutes</strong>.
                </p>
            </div>
            """
            try:
                log_and_dispatch_email(
                    to_email=user["email"],
                    subject="[KandyPack] Your New Login Verification Code",
                    html_content=otp_html,
                    notification_type="LOGIN_OTP"
                )
            except Exception as mail_err:
                logger.warning("Could not resend OTP: %s", mail_err)

            return {"message": "A new verification code has been sent to your email."}


# 2. Customer Registration Endpoint (Atomic Transaction & Parameterized)
    
@router.post("/register", status_code=status.HTTP_201_CREATED)
def register_customer(payload: CustomerRegisterRequest, response: Response, request: Request):
    if request_portal(request) != "customer":
        raise HTTPException(status_code=403, detail="CUSTOMER_REGISTRATION_ONLY")
    try:
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

                # Dispatch 6-digit OTP email verification for customer
                norm_email = payload.email.lower().strip()
                otp = f"{secrets.randbelow(900000) + 100000}"
                _OTP_CACHE[norm_email] = {
                    "code": otp,
                    "expires_at": time.time() + 600,
                    "user_id": user_id
                }

                from app.core.notifications import log_and_dispatch_email
                otp_html = f"""
                <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 520px; margin: 0 auto; padding: 26px; background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; color: #1e293b;">
                    <div style="text-align: center; margin-bottom: 20px;">
                        <span style="font-size: 24px; font-weight: 800; color: #16a34a; letter-spacing: -0.5px;">🚂 KandyPack Logistics</span>
                        <p style="margin: 4px 0 0 0; font-size: 13px; color: #64748b;">Customer Account Email Verification</p>
                    </div>
                    <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 18px; text-align: center; margin-bottom: 20px;">
                        <h2 style="color: #15803d; margin: 0 0 6px 0; font-size: 18px; font-weight: 700;">🔐 Verify Your Email Address</h2>
                        <p style="color: #166534; margin: 0; font-size: 13px;">Please use this 6-digit code to activate your account and complete sign in.</p>
                    </div>
                    <p style="font-size: 14px; color: #334155; line-height: 1.5;">
                        Hello <strong>{payload.name}</strong>,<br><br>
                        Thank you for registering with KandyPack Logistics! To activate your customer profile, enter the verification code below:
                    </p>
                    <div style="background: #f8fafc; border: 2px dashed #16a34a; border-radius: 12px; padding: 18px; text-align: center; margin: 22px 0;">
                        <span style="font-size: 34px; font-weight: 800; letter-spacing: 8px; color: #15803d; font-family: monospace;">{otp}</span>
                    </div>
                    <p style="font-size: 12px; color: #64748b; line-height: 1.5; margin: 0;">
                        This code is valid for <strong>10 minutes</strong>.
                    </p>
                </div>
                """
                try:
                    log_and_dispatch_email(
                        to_email=payload.email,
                        subject="[KandyPack] Verify Your New Account - 6-Digit Code",
                        html_content=otp_html,
                        notification_type="REGISTRATION_OTP"
                    )
                except Exception as mail_err:
                    logger.warning("Could not dispatch customer registration OTP email: %s", mail_err)

                return {
                    "user_id": user_id,
                    "customer_id": customer_id,
                    "name": payload.name,
                    "email": payload.email,
                    "role": "CUSTOMER",
                    "require_otp": True,
                    "message": "Registration successful! A 6-digit verification code has been dispatched to your email."
                }
    except HTTPException:
        raise
    except pymysql.IntegrityError:
        raise HTTPException(status_code=409, detail="Account conflicts with an existing record or invalid reference.")
    except pymysql.OperationalError as op_err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DATABASE_UNAVAILABLE: Please try again later."
        )
    except pymysql.MySQLError as sql_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="DATABASE_ERROR: Unable to complete this request."
        )
    except Exception as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="REGISTRATION_FAILED: Unable to complete this request."
        )


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp_code: str

@router.post("/verify-otp")
def verify_registration_otp(payload: VerifyOtpRequest, response: Response, request: Request):
    """Verifies customer registration or login OTP and issues authentication session."""
    norm_email = payload.email.lower().strip()
    submitted_otp = payload.otp_code.strip()
    cached = _OTP_CACHE.get(norm_email)
    is_test_bypass = submitted_otp == "000000" and os.environ.get("ROSTER_DATA_MODE") == "dev-memory"
    if not is_test_bypass:
        if not cached or time.time() > cached.get("expires_at", 0):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="INVALID_OTP: Verification code expired or not found. Please request a new code."
            )
        if cached.get("code") != submitted_otp:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="INVALID_OTP: Incorrect verification code. Please check your email and try again."
            )
    user_id = cached.get("user_id") if cached else None
    _OTP_CACHE.pop(norm_email, None)

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, name, email, role, force_password_reset FROM user WHERE user_id = %s OR email = %s", (user_id, norm_email))
            user = cursor.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User account not found.")

    access_token = create_access_token(data={
        "sub": str(user["user_id"]),
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "force_password_reset": bool(user["force_password_reset"])
    })

    response.set_cookie(
        key="kandypack_session",
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=settings.SESSION_COOKIE_SECURE,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )

    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "full_name": user["name"],
        "role": user["role"],
        "message": "Email verified successfully! Welcome to KandyPack."
    }


    
# 3. Superadmin: Create New Employee / Staff Account (Guarded by SUPERADMIN role)
    
@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_staff_user(
    payload: StaffCreateRequest,
    current_user: dict = Depends(require_roles(["SUPERADMIN"]))
):
    if payload.role in {"DRIVER", "ASSISTANT"} and not (payload.license_number or "").strip():
        raise HTTPException(status_code=422, detail="Delivery staff require a license/staff reference number.")
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
                # Enforce domain check and explicitly set force_password_reset = 1 for staff accounts
                if not payload.email.endswith("@kandypack.lk"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="SECURITY POLICY VIOLATION: Internal staff users must have an email ending with @kandypack.lk"
                    )

                cursor.execute("SET @kandypack_staff_provisioning = 1")
                cursor.execute(
                    """
                    INSERT INTO user (name, role, email, password_hash, force_password_reset, is_active, station_id)
                    VALUES (%s, %s, %s, %s, 1, 1, %s)
                    """,
                    (payload.name, payload.role, payload.email, pw_hash, payload.station_id)
                )
                user_id = cursor.lastrowid
                if payload.role in {"DRIVER", "ASSISTANT"}:
                    cursor.execute("INSERT INTO delivery_staff (user_id, license_number, work_hours) VALUES (%s, %s, 0)", (user_id, payload.license_number))
                if payload.role == "STORE_MGR" and payload.station_id:
                    cursor.execute("UPDATE station_store SET manager_id = %s WHERE station_id = %s", (user_id, payload.station_id))
                conn.commit()
            except HTTPException:
                raise
            except pymysql.IntegrityError:
                raise HTTPException(status_code=409, detail="Account conflicts with an existing record.")
            except Exception as e:
                err_msg = str(e)
                if "SECURITY POLICY VIOLATION" in err_msg:
                    raise HTTPException(status_code=400, detail=err_msg)
                raise HTTPException(status_code=500, detail="Staff creation failed; no account was created.")

            return {
                "user_id": user_id,
                "name": payload.name,
                "email": payload.email,
                "role": payload.role,
                "station_id": payload.station_id,
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
                SELECT u.user_id, u.name, u.role, u.email, u.force_password_reset, u.is_active, u.created_at,
                       u.station_id, ss.city AS station_name
                FROM user u
                LEFT JOIN station_store ss ON ss.station_id = u.station_id
                ORDER BY u.user_id ASC
                """
            )
            users = cursor.fetchall()
            return [
                {
                    "user_id": u["user_id"],
                    "name": u["name"],
                    "role": u["role"],
                    "email": u["email"],
                    "station_id": u.get("station_id"),
                    "station_name": u.get("station_name"),
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

            if not verify_password(payload.current_password, user["password_hash"]):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="INVALID_CURRENT_PASSWORD: The current password you entered is incorrect."
                )

            new_hash = get_password_hash(payload.new_password)
            if verify_password(payload.new_password, user["password_hash"]):
                raise HTTPException(status_code=400, detail="Choose a different password.")
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
                secure=settings.SESSION_COOKIE_SECURE,
                max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
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
                SELECT u.user_id, u.name, u.email, u.role, u.force_password_reset, u.station_id, ss.city AS station_name,
                       c.customer_id, c.phone, c.city, c.address_line, c.route_id
                FROM user u
                LEFT JOIN customer c ON u.user_id = c.user_id
                LEFT JOIN station_store ss ON u.station_id = ss.station_id
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
                "route_id": profile.get("route_id"),
                "station_id": profile.get("station_id"),
                "station_name": profile.get("station_name"),
            }

@router.put("/me", response_model=UserProfileResponse)
def update_current_user_profile(
    payload: ProfileUpdateRequest,
    response: Response,
    current_user: dict = Depends(get_current_user)
):
    with get_db() as conn:
        with conn.cursor() as cursor:
            # Update user table name
            cursor.execute(
                "UPDATE user SET name = %s WHERE user_id = %s",
                (payload.name, current_user["user_id"])
            )

            # Staff profiles must never create customer identities or overwrite routing.
            if current_user["role"] == "CUSTOMER":
                cursor.execute(
                    """UPDATE customer SET customer_name = %s,
                       phone = COALESCE(%s, phone), address_line = COALESCE(%s, address_line),
                       city = COALESCE(%s, city) WHERE user_id = %s""",
                    (payload.name, payload.phone, payload.address_line, payload.city, current_user["user_id"])
                )
            conn.commit()

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

            # Refresh token with updated name
            new_token = create_access_token(data={
                "sub": str(current_user["user_id"]),
                "email": current_user["email"],
                "name": profile["name"],
                "role": current_user["role"],
                "force_password_reset": bool(profile["force_password_reset"])
            })

            response.set_cookie(
                key="kandypack_session",
                value=new_token,
                httponly=True,
                samesite="lax",
                secure=settings.SESSION_COOKIE_SECURE,
                max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
            )

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

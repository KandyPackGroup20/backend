from fastapi import APIRouter, BackgroundTasks, HTTPException, status, Query, Request, Depends
from pydantic import BaseModel, EmailStr
from typing import List, Optional
from app.core.security import get_token_from_request, get_current_user, require_roles
from app.core.notifications import (
    log_and_dispatch_email,
    get_recent_notifications,
    get_unread_notification_count,
    mark_notification_as_read,
    mark_all_notifications_as_read,
    create_database_notification
)

router = APIRouter(prefix="/notifications", tags=["Email & Push Notifications (Logistics Alerts)"])

class TriggerNotificationRequest(BaseModel):
    recipient: EmailStr
    subject: str
    message: str
    notification_type: Optional[str] = "LOGISTICS_MGR_ALERT"
    order_id: Optional[int] = None
    user_id: Optional[int] = None

@router.get("/recent")
@router.get("")
def list_recent_notifications(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    unread_only: bool = Query(False),
    search: Optional[str] = Query(None)
):
    """
    Retrieves real-time dispatch alerts strictly scoped to the authenticated user's role.
    Unauthenticated public visitors receive an empty list with authenticated=False.
    """
    token = get_token_from_request(request)
    session_user = get_current_user(request) if token else None

    if not session_user:
        return {
            "notifications": [],
            "unread_count": 0,
            "total_dispatched": 0,
            "authenticated": False
        }

    user_id = int(session_user.get("user_id", 0))
    user_role = session_user.get("role")
    user_email = session_user.get("email")

    notifs = get_recent_notifications(
        limit=limit,
        unread_only=unread_only,
        search=search,
        user_id=user_id,
        user_role=user_role,
        user_email=user_email
    )
    unread_cnt = get_unread_notification_count(
        user_id=user_id,
        user_role=user_role,
        user_email=user_email
    )
    return {
        "notifications": notifs,
        "unread_count": unread_cnt,
        "total_dispatched": len(notifs),
        "authenticated": True,
        "role": user_role
    }

@router.patch("/{notification_id}/read")
def set_notification_read(notification_id: int, request: Request):
    """Marks a single alert as read with ownership verification."""
    token = get_token_from_request(request)
    session_user = get_current_user(request) if token else None
    if not session_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="NOT_AUTHENTICATED: Please log in to acknowledge alerts."
        )

    user_id = int(session_user.get("user_id", 0))
    user_role = session_user.get("role")
    user_email = session_user.get("email")

    success = mark_notification_as_read(
        notification_id,
        user_id=user_id,
        user_role=user_role,
        user_email=user_email
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification #{notification_id} not found or you are not authorized to update it."
        )
    return {
        "status": "SUCCESS",
        "notification_id": notification_id,
        "is_read": 1,
        "unread_count": get_unread_notification_count(
            user_id=user_id,
            user_role=user_role,
            user_email=user_email
        )
    }

@router.post("/mark-all-read")
def mark_all_read(request: Request):
    """Marks all unread alerts for the authenticated user as read."""
    token = get_token_from_request(request)
    session_user = get_current_user(request) if token else None
    if not session_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="NOT_AUTHENTICATED: Please log in to acknowledge alerts."
        )

    user_id = int(session_user.get("user_id", 0))
    user_role = session_user.get("role")
    user_email = session_user.get("email")

    updated = mark_all_notifications_as_read(
        user_id=user_id,
        user_role=user_role,
        user_email=user_email
    )
    return {
        "status": "SUCCESS",
        "marked_read_count": updated,
        "unread_count": 0
    }


@router.post("/send")
def send_notification(payload: TriggerNotificationRequest, background_tasks: BackgroundTasks,
                      current_user: dict = Depends(require_roles(["SUPERADMIN"]))):
    """Dispatches asynchronous notification alert (email or push notification) and records to DB."""
    # Persist in real database table
    notif_id = create_database_notification(
        recipient_email=payload.recipient,
        title=payload.subject,
        message=payload.message,
        notification_type=payload.notification_type or "LOGISTICS_MGR_ALERT",
        order_id=payload.order_id,
        user_id=payload.user_id
    )

    background_tasks.add_task(
        log_and_dispatch_email,
        payload.recipient,
        payload.subject,
        payload.message,
        payload.notification_type or "LOGISTICS_MGR_ALERT"
    )
    return {
        "status": "QUEUED",
        "notification_id": notif_id,
        "message": f"Notification successfully recorded and queued for {payload.recipient}.",
        "type": payload.notification_type
    }


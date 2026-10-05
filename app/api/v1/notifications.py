from fastapi import APIRouter, BackgroundTasks, HTTPException, status, Query
from pydantic import BaseModel, EmailStr
from typing import List, Optional
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
    limit: int = Query(50, ge=1, le=100),
    unread_only: bool = Query(False),
    search: Optional[str] = Query(None)
):
    """
    Retrieves real-time dispatch alerts and history from MySQL notification table.
    Returns list of notifications, current unread count, and total count.
    """
    notifs = get_recent_notifications(limit=limit, unread_only=unread_only, search=search)
    unread_cnt = get_unread_notification_count()
    return {
        "notifications": notifs,
        "unread_count": unread_cnt,
        "total_dispatched": len(notifs)
    }

@router.patch("/{notification_id}/read")
def set_notification_read(notification_id: int):
    """Marks a single alert as read in the database."""
    success = mark_notification_as_read(notification_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification #{notification_id} not found or already updated."
        )
    return {
        "status": "SUCCESS",
        "notification_id": notification_id,
        "is_read": 1,
        "unread_count": get_unread_notification_count()
    }

@router.post("/mark-all-read")
def mark_all_read():
    """Marks all unread alerts as read in the database."""
    updated = mark_all_notifications_as_read()
    return {
        "status": "SUCCESS",
        "marked_read_count": updated,
        "unread_count": 0
    }

@router.post("/send")
def send_notification(payload: TriggerNotificationRequest, background_tasks: BackgroundTasks):
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


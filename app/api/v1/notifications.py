from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, EmailStr
from typing import List, Optional
from app.core.notifications import (
    log_and_dispatch_email,
    get_recent_notifications
)

router = APIRouter(prefix="/notifications", tags=["Email & Push Notifications (Bonus Feature)"])

class TriggerNotificationRequest(BaseModel):
    recipient: EmailStr
    subject: str
    message: str
    notification_type: Optional[str] = "EMAIL"

@router.get("/recent")
def list_recent_notifications(limit: int = 10):
    """Retrieves live dispatch history of email and push alerts for dashboard inspection."""
    return {
        "notifications": get_recent_notifications(limit),
        "total_dispatched": len(get_recent_notifications(100))
    }

@router.post("/send")
def send_notification(payload: TriggerNotificationRequest, background_tasks: BackgroundTasks):
    """Dispatches asynchronous notification alert (email or push notification)."""
    background_tasks.add_task(
        log_and_dispatch_email,
        payload.recipient,
        payload.subject,
        payload.message,
        payload.notification_type
    )
    return {
        "status": "QUEUED",
        "message": f"Notification successfully queued for {payload.recipient}.",
        "type": payload.notification_type
    }

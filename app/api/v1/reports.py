from fastapi import APIRouter
from app.core.database import get_db

router = APIRouter(prefix="/reports", tags=["Management Reports & Analytics"])

@router.get("/analytics")
def get_rail_analytics():
    """Queries `v_quarterly_rail_analytics` database view."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM v_quarterly_rail_analytics")
            data = cursor.fetchall()
            return {"analytics": data}

@router.get("/driver-caps")
def get_driver_cap_warnings():
    """Queries `v_drivers_near_cap` database view for SR-5.2.4 visual warnings."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM v_drivers_near_cap")
            data = cursor.fetchall()
            return {"driver_cap_warnings": data}

@router.get("/audit-logs")
def get_roster_audit_logs():
    """Fetches immutable roster audit log entries for viva demonstration."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT log_id, dispatcher_id, attempt_timestamp, status, violation_rule, details
                FROM roster_audit_logs
                ORDER BY log_id DESC
                LIMIT 50
            """)
            logs = cursor.fetchall()
            return {"audit_logs": logs}

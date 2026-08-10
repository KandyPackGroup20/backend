from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.core.database import get_db

router = APIRouter(prefix="/roster", tags=["Fleet Roster & Driver Assignment"])

class RosterAssignRequest(BaseModel):
    route_id: int
    truck_id: int
    driver_id: int
    assistant_id: int
    dispatcher_id: int
    start_time: str # ISO format e.g. "2026-08-12 08:00:00"
    end_time: str
    duration_hours: float

@router.post("/assign")
def assign_roster(payload: RosterAssignRequest):
    # Triggers sp_assign_truck_roster procedure enforcing Checks A, B, C, D atomically with row locking
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                CALL sp_assign_truck_roster(%s, %s, %s, %s, %s, %s, %s, %s, @result_code);
            ''', (
                payload.route_id,
                payload.truck_id,
                payload.driver_id,
                payload.assistant_id,
                payload.dispatcher_id,
                payload.start_time,
                payload.end_time,
                payload.duration_hours
            ))
            
            cursor.execute("SELECT @result_code AS result_code;")
            res = cursor.fetchone()
            result_code = res['result_code'] if res else "UNKNOWN"
            
            if result_code != "SUCCESS":
                conn.rollback()
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error_code": result_code,
                        "message": f"Roster assignment rejected by DB procedure: {result_code}"
                    }
                )
            
            conn.commit()
            return {
                "status": "SUCCESS",
                "message": "Roster assignment created successfully.",
                "result_code": result_code
            }

@router.get("/candidates")
def get_roster_candidates():
    # Fetches candidate drivers, assistants, and trucks using DB Views
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM v_available_drivers")
            drivers = cursor.fetchall()
            
            cursor.execute("SELECT * FROM v_available_assistants")
            assistants = cursor.fetchall()
            
            cursor.execute("SELECT truck_id, plate_number, capacity FROM truck WHERE is_active = 1")
            trucks = cursor.fetchall()
            
            return {
                "drivers": drivers,
                "assistants": assistants,
                "trucks": trucks
            }

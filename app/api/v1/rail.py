from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from app.core.database import get_db

router = APIRouter(prefix="/rail", tags=["Rail Allocation & Schedules"])

class RailAllocateRequest(BaseModel):
    order_id: int

@router.post("/allocate")
def allocate_rail_capacity(payload: RailAllocateRequest):
    """Triggers `sp_allocate_rail_capacity` stored procedure to split cargo across train trips."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            # Call procedure with OUT parameter
            cursor.execute("CALL sp_allocate_rail_capacity(%s, @status_result);", (payload.order_id,))
            cursor.execute("SELECT @status_result AS status_result;")
            res = cursor.fetchone()
            
            status_code = res['status_result'] if res else "UNKNOWN"
            
            if status_code.startswith("ERROR"):
                conn.rollback()
                raise HTTPException(status_code=500, detail=f"Rail allocation failed: {status_code}")
            elif status_code == "INSUFFICIENT_RAIL_CAPACITY":
                conn.rollback()
                raise HTTPException(status_code=400, detail="Insufficient train cargo capacity for order items across available trips.")
            
            conn.commit()
            
            # Fetch allocation breakdown for presentation
            cursor.execute("""
                SELECT ota.allocation_id, ota.order_id, tt.trip_code, tt.departure_time, ota.allocated_quantity, ota.allocated_space
                FROM order_trip_allocations ota
                JOIN train_trips tt ON ota.trip_id = tt.trip_id
                WHERE ota.order_id = %s
            """, (payload.order_id,))
            allocations = cursor.fetchall()
            
            return {
                "order_id": payload.order_id,
                "status_result": status_code,
                "allocations": allocations
            }

@router.get("/schedules")
def get_train_schedules():
    """Lists scheduled train trips from Kandy to regional hubs."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT trip_id, trip_code, origin_hub, destination_hub, departure_time, arrival_time,
                       total_capacity_cubic_m, remaining_capacity_cubic_m, status
                FROM train_trips
                ORDER BY departure_time ASC
            """)
            trips = cursor.fetchall()
            return {"trips": trips}

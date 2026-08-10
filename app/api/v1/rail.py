from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.core.database import get_db

router = APIRouter(prefix="/rail", tags=["Rail Allocation & Schedules"])

class RailAllocateRequest(BaseModel):
    order_id: int

@router.post("/allocate")
def allocate_rail_capacity(payload: RailAllocateRequest):
    # Triggers sp_allocate_rail_capacity stored procedure to split cargo across train trips
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
            
            # Fetch allocation breakdown for presentation joining with order_item
            cursor.execute('''
                SELECT ra.allocation_id, oi.order_id, tt.trip_id, tt.departure_datetime, ra.allocated_quantity, ra.allocated_space
                FROM rail_allocation ra
                JOIN order_item oi ON ra.order_item_id = oi.order_item_id
                JOIN train_trip tt ON ra.trip_id = tt.trip_id
                WHERE oi.order_id = %s
            ''', (payload.order_id,))
            allocations = cursor.fetchall()
            
            # Format date to string for JSON serialization
            for alloc in allocations:
                if alloc.get('departure_datetime'):
                    alloc['departure_datetime'] = alloc['departure_datetime'].strftime("%Y-%m-%d %H:%M:%S")
            
            return {
                "order_id": payload.order_id,
                "status_result": status_code,
                "allocations": allocations
            }

@router.get("/schedules")
def get_train_schedules():
    # Lists scheduled train trips with dynamically calculated remaining capacity
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT tt.trip_id, 
                       ss1.city AS origin_city, 
                       ss2.city AS destination_city, 
                       tt.departure_datetime, 
                       tt.arrival_datetime,
                       tt.total_capacity, 
                       (tt.total_capacity - COALESCE((SELECT SUM(allocated_space) FROM rail_allocation WHERE trip_id = tt.trip_id), 0)) AS remaining_capacity, 
                       tt.status
                FROM train_trip tt
                JOIN station_store ss1 ON tt.origin_station_id = ss1.station_id
                JOIN station_store ss2 ON tt.destination_station_id = ss2.station_id
                ORDER BY tt.departure_datetime ASC
            ''')
            trips = cursor.fetchall()
            for trip in trips:
                if trip.get('departure_datetime'):
                    trip['departure_datetime'] = trip['departure_datetime'].strftime("%Y-%m-%d %H:%M:%S")
                if trip.get('arrival_datetime'):
                    trip['arrival_datetime'] = trip['arrival_datetime'].strftime("%Y-%m-%d %H:%M:%S")
            return {"trips": trips}

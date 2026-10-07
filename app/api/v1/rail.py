from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from app.core.database import get_db
from app.core.security import require_roles
from app.core.cache import get_cache, set_cache, invalidate_cache

router = APIRouter(prefix="/rail", tags=["Rail Allocation & Schedules"])

# Roles allowed to schedule orders onto trains
RAIL_ROLES = ["SUPERADMIN", "LOGISTICS_MGR"]

# sp_schedule_train_order result code -> HTTP status
RESULT_HTTP_STATUS = {
    "ORDER_NOT_FOUND": 404,
    "INVALID_ORDER_STATUS": 409,
    "INSUFFICIENT_RAIL_CAPACITY": 400,
    "DESTINATION_HUB_NOT_RESOLVED": 422,
    "ORDER_HAS_NO_ITEMS": 422,
    "INVALID_PRODUCT_SPACE_RATE": 422,
    "DEADLOCK_RETRY": 503,
    "ERROR_TRANSACTION_FAILED": 500,
}

class RailAllocateRequest(BaseModel):
    order_id: int

@router.post("/allocate")
def allocate_rail_capacity(
    payload: RailAllocateRequest,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    # Calls sp_schedule_train_order (Feature 4.2): row-locked, multi-trip spillover
    with get_db() as conn:
        with conn.cursor() as cursor:
            status_code = "UNKNOWN"
            for _ in range(3):  # retry if MySQL reports a deadlock / lock timeout
                cursor.execute(
                    "CALL sp_schedule_train_order(%s, %s, @status_result);",
                    (payload.order_id, current_user["user_id"]),
                )
                cursor.execute("SELECT @status_result AS status_result;")
                res = cursor.fetchone()
                status_code = res["status_result"] if res else "UNKNOWN"
                if status_code != "DEADLOCK_RETRY":
                    break

            if not status_code.startswith("SUCCESS"):
                conn.rollback()
                raise HTTPException(
                    status_code=RESULT_HTTP_STATUS.get(status_code, 500),
                    detail=f"Rail allocation failed: {status_code}",
                )

            conn.commit()
            invalidate_cache("cache:rail:")

            # Allocation breakdown for presentation
            cursor.execute('''
                SELECT ra.allocation_id, oi.order_id, tt.trip_id, tt.departure_datetime, ra.allocated_quantity, ra.allocated_space
                FROM rail_allocation ra
                JOIN order_item oi ON ra.order_item_id = oi.order_item_id
                JOIN train_trip tt ON ra.trip_id = tt.trip_id
                WHERE oi.order_id = %s
                ORDER BY tt.departure_datetime, ra.allocation_id
            ''', (payload.order_id,))
            allocations = cursor.fetchall()

            for alloc in allocations:
                if alloc.get('departure_datetime'):
                    alloc['departure_datetime'] = alloc['departure_datetime'].strftime("%Y-%m-%d %H:%M:%S")

            return {
                "order_id": payload.order_id,
                "status_result": status_code,
                "allocations": allocations
            }

@router.get("/orders/{order_id}/allocations")
def get_order_allocations(
    order_id: int,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    # Shows which trips an order was split across
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT ra.allocation_id, ra.order_item_id, ra.trip_id,
                       tt.departure_datetime, ra.allocated_quantity, ra.allocated_space
                FROM rail_allocation ra
                JOIN order_item oi ON oi.order_item_id = ra.order_item_id
                JOIN train_trip tt ON tt.trip_id = ra.trip_id
                WHERE oi.order_id = %s
                ORDER BY tt.departure_datetime, ra.allocation_id
            ''', (order_id,))
            rows = cursor.fetchall()
            for r in rows:
                if r.get('departure_datetime'):
                    r['departure_datetime'] = r['departure_datetime'].strftime("%Y-%m-%d %H:%M:%S")
            return {"order_id": order_id, "allocations": rows}

@router.get("/trips/capacity")
def get_trip_capacity(
    destination_station_id: Optional[int] = None,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    # Uses the v_trip_capacity_usage view (Feature 4.2)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT trip_id, origin_station_id, destination_station_id,
                       departure_datetime, status, total_capacity,
                       used_space, remaining_space, utilisation_pct
                FROM v_trip_capacity_usage
                WHERE (%s IS NULL OR destination_station_id = %s)
                ORDER BY departure_datetime
            ''', (destination_station_id, destination_station_id))
            trips = cursor.fetchall()
            for t in trips:
                if t.get('departure_datetime'):
                    t['departure_datetime'] = t['departure_datetime'].strftime("%Y-%m-%d %H:%M:%S")
            return {"trips": trips}

@router.get("/schedules")
def get_train_schedules(response: Response):
    cache_key = "cache:rail:schedules"
    cached = get_cache(cache_key)
    if cached is not None:
        response.headers["X-Cache"] = "HIT"
        return cached

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT tt.trip_id,
                       ss1.city AS origin_city,
                       ss2.city AS destination_city,
                       tt.departure_datetime,
                       tt.arrival_datetime,
                       tt.total_capacity,
                       v.remaining_space AS remaining_capacity,
                       tt.status
                FROM train_trip tt
                JOIN station_store ss1 ON tt.origin_station_id = ss1.station_id
                JOIN station_store ss2 ON tt.destination_station_id = ss2.station_id
                JOIN v_trip_capacity_usage v ON v.trip_id = tt.trip_id
                ORDER BY tt.departure_datetime ASC
            ''')
            trips = cursor.fetchall()
            for trip in trips:
                if trip.get('departure_datetime'):
                    trip['departure_datetime'] = trip['departure_datetime'].strftime("%Y-%m-%d %H:%M:%S")
                if trip.get('arrival_datetime'):
                    trip['arrival_datetime'] = trip['arrival_datetime'].strftime("%Y-%m-%d %H:%M:%S")
            
            result = {"trips": trips}
            set_cache(cache_key, result, ttl_seconds=60)
            response.headers["X-Cache"] = "MISS"
            return result
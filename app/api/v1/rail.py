from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
import pymysql

from app.core.database import get_db
from app.core.security import require_roles
from app.core.cache import get_cache, set_cache, invalidate_cache

router = APIRouter(prefix="/rail", tags=["Rail Allocation & Schedules (Feature 4.2)"])

# Roles allowed to access rail management endpoints
RAIL_ROLES = ["SUPERADMIN", "LOGISTICS_MGR"]

# Stored procedure result code -> HTTP status
RESULT_HTTP_STATUS = {
    "ORDER_NOT_FOUND": 404,
    "INVALID_ORDER_STATUS": 409,
    "INSUFFICIENT_RAIL_CAPACITY": 400,
    "DESTINATION_HUB_NOT_RESOLVED": 422,
    "ORIGIN_HUB_NOT_FOUND": 422,
    "ORDER_HAS_NO_ITEMS": 422,
    "INVALID_PRODUCT_SPACE_RATE": 422,
    "CANNOT_REVERSE_DEPARTED_OR_INACTIVE_TRIP": 409,
    "DEADLOCK_RETRY": 503,
    "ERROR_TRANSACTION_FAILED": 500,
}


# ==========================================
# Pydantic Schemas
# ==========================================

class RailAllocateRequest(BaseModel):
    order_id: int
    trip_id: Optional[int] = None

class ReverseAllocateRequest(BaseModel):
    order_id: int

class CreateTripRequest(BaseModel):
    origin_station_id: int
    destination_station_id: int
    departure_datetime: datetime
    arrival_datetime: datetime
    total_capacity: float = Field(gt=0, description="Total train carriage capacity in space units")

class UpdateTripRequest(BaseModel):
    total_capacity: Optional[float] = Field(default=None, gt=0)
    departure_datetime: Optional[datetime] = None
    arrival_datetime: Optional[datetime] = None


# ==========================================
# 1. Train Trip Management (LM-01, LM-02)
# ==========================================

@router.get("/trips")
def list_train_trips(
    status_filter: Optional[str] = Query(None, alias="status"),
    destination_station_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """List all train trips with capacity analytics and status (LM-02)."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT tt.trip_id,
                       tt.origin_station_id, ss1.city AS origin_city,
                       tt.destination_station_id, ss2.city AS destination_city,
                       tt.departure_datetime, tt.arrival_datetime,
                       tt.total_capacity,
                       COALESCE(v.used_space, 0.00) AS used_space,
                       COALESCE(v.remaining_space, tt.total_capacity) AS remaining_space,
                       COALESCE(v.utilisation_pct, 0.00) AS utilisation_pct,
                       tt.status
                FROM train_trip tt
                JOIN station_store ss1 ON tt.origin_station_id = ss1.station_id
                JOIN station_store ss2 ON tt.destination_station_id = ss2.station_id
                LEFT JOIN v_trip_capacity_usage v ON v.trip_id = tt.trip_id
                WHERE (%s IS NULL OR tt.status = %s)
                  AND (%s IS NULL OR tt.destination_station_id = %s)
                ORDER BY tt.departure_datetime ASC, tt.trip_id ASC
                """,
                (status_filter, status_filter, destination_station_id, destination_station_id)
            )
            trips = cursor.fetchall()
            for t in trips:
                if t.get("departure_datetime"):
                    t["departure_datetime"] = t["departure_datetime"].strftime("%Y-%m-%d %H:%M:%S")
                if t.get("arrival_datetime"):
                    t["arrival_datetime"] = t["arrival_datetime"].strftime("%Y-%m-%d %H:%M:%S")
            return {"trips": trips}


@router.post("/trips", status_code=status.HTTP_201_CREATED)
def create_train_trip(
    payload: CreateTripRequest,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """Create a new train trip departing from Kandy with capacity validation (LM-01, LM-03)."""
    # 1. API-level Validations
    if payload.origin_station_id == payload.destination_station_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Origin and destination stations must be distinct."
        )

    if payload.arrival_datetime <= payload.departure_datetime:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Arrival datetime must be strictly after departure datetime."
        )

    if payload.total_capacity <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Total capacity must be strictly greater than zero."
        )

    with get_db() as conn:
        with conn.cursor() as cursor:
            # 2. Origin Hub Verification (LM-03: must be Kandy)
            cursor.execute("SELECT station_id, city FROM station_store WHERE city = 'Kandy' LIMIT 1")
            kandy_row = cursor.fetchone()
            if not kandy_row:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Kandy station hub is not configured in the database."
                )

            if payload.origin_station_id != kandy_row["station_id"]:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid origin station #{payload.origin_station_id}. Rail allocation policy strictly mandates trips depart from Kandy (station ID {kandy_row['station_id']})."
                )

            # 3. Verify destination station exists
            cursor.execute("SELECT station_id, city FROM station_store WHERE station_id = %s", (payload.destination_station_id,))
            dest_row = cursor.fetchone()
            if not dest_row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Destination station #{payload.destination_station_id} does not exist."
                )

            # 4. Insert trip into DB (DB CHECKs verify chk_trip_capacity_positive, chk_trip_time_order, chk_trip_distinct_stations)
            try:
                cursor.execute(
                    """
                    INSERT INTO train_trip (origin_station_id, destination_station_id, departure_datetime, arrival_datetime, total_capacity, status)
                    VALUES (%s, %s, %s, %s, %s, 'SCHEDULED')
                    """,
                    (
                        payload.origin_station_id,
                        payload.destination_station_id,
                        payload.departure_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                        payload.arrival_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                        payload.total_capacity,
                    )
                )
                trip_id = cursor.lastrowid

                # Audit log entry
                cursor.execute(
                    """
                    INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                    VALUES (%s, 'CREATE_TRAIN_TRIP', %s, 'SUCCESS', 'train_trip')
                    """,
                    (current_user["user_id"], trip_id)
                )
                conn.commit()
            except pymysql.MySQLError as e:
                conn.rollback()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Database constraint violation creating train trip: {str(e)}"
                )

            invalidate_cache("cache:rail:")

            return {
                "trip_id": trip_id,
                "origin_station_id": payload.origin_station_id,
                "origin_city": kandy_row["city"],
                "destination_station_id": payload.destination_station_id,
                "destination_city": dest_row["city"],
                "departure_datetime": payload.departure_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                "arrival_datetime": payload.arrival_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                "total_capacity": payload.total_capacity,
                "status": "SCHEDULED",
                "message": f"Train trip #{trip_id} successfully created."
            }


@router.get("/trips/capacity")
def get_trip_capacity(
    destination_station_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """Real-time trip capacity view using v_trip_capacity_usage view."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT trip_id, origin_station_id, destination_station_id,
                       departure_datetime, status, total_capacity,
                       used_space, remaining_space, utilisation_pct
                FROM v_trip_capacity_usage
                WHERE (%s IS NULL OR destination_station_id = %s)
                ORDER BY departure_datetime ASC
                """,
                (destination_station_id, destination_station_id)
            )
            trips = cursor.fetchall()
            for t in trips:
                if t.get("departure_datetime"):
                    t["departure_datetime"] = t["departure_datetime"].strftime("%Y-%m-%d %H:%M:%S")
            return {"trips": trips}


@router.get("/trips/{trip_id}")
def get_train_trip(
    trip_id: int,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """Retrieve details for a single train trip (LM-02)."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT tt.trip_id,
                       tt.origin_station_id, ss1.city AS origin_city,
                       tt.destination_station_id, ss2.city AS destination_city,
                       tt.departure_datetime, tt.arrival_datetime,
                       tt.total_capacity,
                       COALESCE(v.used_space, 0.00) AS used_space,
                       COALESCE(v.remaining_space, tt.total_capacity) AS remaining_space,
                       COALESCE(v.utilisation_pct, 0.00) AS utilisation_pct,
                       tt.status
                FROM train_trip tt
                JOIN station_store ss1 ON tt.origin_station_id = ss1.station_id
                JOIN station_store ss2 ON tt.destination_station_id = ss2.station_id
                LEFT JOIN v_trip_capacity_usage v ON v.trip_id = tt.trip_id
                WHERE tt.trip_id = %s
                """,
                (trip_id,)
            )
            trip = cursor.fetchone()
            if not trip:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Train trip #{trip_id} not found.")

            if trip.get("departure_datetime"):
                trip["departure_datetime"] = trip["departure_datetime"].strftime("%Y-%m-%d %H:%M:%S")
            if trip.get("arrival_datetime"):
                trip["arrival_datetime"] = trip["arrival_datetime"].strftime("%Y-%m-%d %H:%M:%S")
            return trip


@router.put("/trips/{trip_id}")
def update_train_trip(
    trip_id: int,
    payload: UpdateTripRequest,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """Update train trip capacity and/or schedules (LM-02, enforces trg_train_trip_capacity_bu)."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM train_trip WHERE trip_id = %s FOR UPDATE", (trip_id,))
            trip = cursor.fetchone()
            if not trip:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Train trip #{trip_id} not found.")

            new_cap = payload.total_capacity if payload.total_capacity is not None else float(trip["total_capacity"])
            new_dep = payload.departure_datetime if payload.departure_datetime is not None else trip["departure_datetime"]
            new_arr = payload.arrival_datetime if payload.arrival_datetime is not None else trip["arrival_datetime"]

            if new_arr <= new_dep:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Arrival datetime must be strictly after departure datetime."
                )

            try:
                cursor.execute(
                    """
                    UPDATE train_trip
                    SET total_capacity = %s,
                        departure_datetime = %s,
                        arrival_datetime = %s
                    WHERE trip_id = %s
                    """,
                    (
                        new_cap,
                        new_dep.strftime("%Y-%m-%d %H:%M:%S") if isinstance(new_dep, datetime) else new_dep,
                        new_arr.strftime("%Y-%m-%d %H:%M:%S") if isinstance(new_arr, datetime) else new_arr,
                        trip_id
                    )
                )
                cursor.execute(
                    """
                    INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                    VALUES (%s, 'UPDATE_TRAIN_TRIP', %s, 'SUCCESS', 'train_trip')
                    """,
                    (current_user["user_id"], trip_id)
                )
                conn.commit()
            except pymysql.MySQLError as err:
                conn.rollback()
                err_msg = str(err)
                if "capacity cannot go below already allocated space" in err_msg:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Capacity update rejected: total capacity cannot be reduced below already allocated cargo space."
                    )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Database constraint error updating trip: {err_msg}"
                )

            invalidate_cache("cache:rail:")
            return {"trip_id": trip_id, "message": f"Train trip #{trip_id} successfully updated."}


@router.patch("/trips/{trip_id}/cancel")
def cancel_train_trip(
    trip_id: int,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """Cancel a train trip (LM-02, LM-15). Rejects if active allocations are booked."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT status FROM train_trip WHERE trip_id = %s FOR UPDATE", (trip_id,))
            trip = cursor.fetchone()
            if not trip:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Train trip #{trip_id} not found.")

            if trip["status"] == "CANCELLED":
                return {"trip_id": trip_id, "status": "CANCELLED", "message": "Trip is already cancelled."}

            # Check if active order allocations exist on this trip
            cursor.execute(
                """
                SELECT COUNT(*) AS active_allocations
                FROM rail_allocation
                WHERE trip_id = %s
                """,
                (trip_id,)
            )
            alloc_check = cursor.fetchone()
            if alloc_check and alloc_check["active_allocations"] > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot cancel trip #{trip_id}: {alloc_check['active_allocations']} active order allocation(s) are currently booked on this train. Please reverse or reschedule affected consignments before cancelling the trip."
                )

            cursor.execute("UPDATE train_trip SET status = 'CANCELLED' WHERE trip_id = %s", (trip_id,))
            cursor.execute(
                """
                INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                VALUES (%s, 'CANCEL_TRAIN_TRIP', %s, 'SUCCESS', 'train_trip')
                """,
                (current_user["user_id"], trip_id)
            )
            conn.commit()

            invalidate_cache("cache:rail:")
            return {"trip_id": trip_id, "status": "CANCELLED", "message": f"Train trip #{trip_id} successfully cancelled."}


@router.patch("/trips/{trip_id}/activate")
def activate_train_trip(
    trip_id: int,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """Activate a cancelled or draft train trip (LM-02)."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT status, departure_datetime FROM train_trip WHERE trip_id = %s FOR UPDATE", (trip_id,))
            trip = cursor.fetchone()
            if not trip:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Train trip #{trip_id} not found.")

            cursor.execute("UPDATE train_trip SET status = 'SCHEDULED' WHERE trip_id = %s", (trip_id,))
            cursor.execute(
                """
                INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                VALUES (%s, 'ACTIVATE_TRAIN_TRIP', %s, 'SUCCESS', 'train_trip')
                """,
                (current_user["user_id"], trip_id)
            )
            conn.commit()

            invalidate_cache("cache:rail:")
            return {"trip_id": trip_id, "status": "SCHEDULED", "message": f"Train trip #{trip_id} successfully activated."}


# ==========================================
# 2. Orders & Suitable Trip Discovery (LM-03, LM-04, LM-06, LM-15)
# ==========================================

@router.get("/orders/pending")
def list_pending_rail_orders(
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """List customer orders awaiting train allocation (LM-06)."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT co.order_id,
                       co.customer_id,
                       c.customer_name,
                       c.phone,
                       c.city AS customer_city,
                       ss.station_id AS destination_station_id,
                       ss.city AS destination_city,
                       dr.route_name,
                       co.order_date,
                       co.delivery_date,
                       co.status
                FROM customer_order co
                JOIN customer c ON c.customer_id = co.customer_id
                JOIN delivery_route dr ON dr.route_id = c.route_id
                JOIN station_store ss ON ss.station_id = dr.station_id
                WHERE co.status = 'PENDING_RAIL_SCHEDULING'
                ORDER BY co.delivery_date ASC, co.order_id ASC
                """
            )
            orders = cursor.fetchall()

            for order in orders:
                if order.get("order_date"):
                    order["order_date"] = order["order_date"].strftime("%Y-%m-%d")
                if order.get("delivery_date"):
                    order["delivery_date"] = order["delivery_date"].strftime("%Y-%m-%d")

                # Load items for this order
                cursor.execute(
                    """
                    SELECT oi.order_item_id,
                           oi.product_id,
                           p.product_name,
                           oi.quantity,
                           oi.unit_price_at_order,
                           p.space_consumption_rate,
                           (oi.quantity * p.space_consumption_rate) AS required_space
                    FROM order_item oi
                    JOIN product p ON p.product_id = oi.product_id
                    WHERE oi.order_id = %s
                    """,
                    (order["order_id"],)
                )
                items = cursor.fetchall()
                order["items"] = items
                order["total_quantity"] = sum(i["quantity"] for i in items)
                order["total_required_space"] = round(sum(float(i["required_space"]) for i in items), 4)

            return {"pending_orders": orders}


@router.get("/orders/{order_id}/suitable-trips")
def get_suitable_trips_for_order(
    order_id: int,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """Find chronological eligible trips departing Kandy for an order (LM-03, LM-04, LM-15)."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            # 1. Resolve order details and destination station
            cursor.execute(
                """
                SELECT co.order_id, co.status, co.delivery_date, dr.station_id AS dest_station_id, ss.city AS dest_city
                FROM customer_order co
                JOIN customer c ON c.customer_id = co.customer_id
                JOIN delivery_route dr ON dr.route_id = c.route_id
                JOIN station_store ss ON ss.station_id = dr.station_id
                WHERE co.order_id = %s
                """,
                (order_id,)
            )
            order_info = cursor.fetchone()
            if not order_info:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Order #{order_id} not found.")

            dest_station_id = order_info["dest_station_id"]
            delivery_date = order_info["delivery_date"]

            # 2. Resolve Kandy Station ID
            cursor.execute("SELECT station_id FROM station_store WHERE city = 'Kandy' LIMIT 1")
            kandy_row = cursor.fetchone()
            kandy_station_id = kandy_row["station_id"] if kandy_row else 7

            # 3. Query suitable trips matching exact sp_schedule_train_order criteria
            cursor.execute(
                """
                SELECT tt.trip_id,
                       tt.origin_station_id, 'Kandy' AS origin_city,
                       tt.destination_station_id, %s AS destination_city,
                       tt.departure_datetime, tt.arrival_datetime,
                       tt.total_capacity,
                       COALESCE(v.used_space, 0.00) AS used_space,
                       COALESCE(v.remaining_space, tt.total_capacity) AS remaining_space,
                       COALESCE(v.utilisation_pct, 0.00) AS utilisation_pct,
                       tt.status
                FROM train_trip tt
                LEFT JOIN v_trip_capacity_usage v ON v.trip_id = tt.trip_id
                WHERE tt.origin_station_id = %s
                  AND tt.destination_station_id = %s
                  AND tt.status = 'SCHEDULED'
                  AND tt.departure_datetime > NOW()
                  AND tt.arrival_datetime < %s + INTERVAL 1 DAY
                ORDER BY tt.departure_datetime ASC, tt.trip_id ASC
                """,
                (order_info["dest_city"], kandy_station_id, dest_station_id, delivery_date)
            )
            suitable_trips = cursor.fetchall()
            for t in suitable_trips:
                if t.get("departure_datetime"):
                    t["departure_datetime"] = t["departure_datetime"].strftime("%Y-%m-%d %H:%M:%S")
                if t.get("arrival_datetime"):
                    t["arrival_datetime"] = t["arrival_datetime"].strftime("%Y-%m-%d %H:%M:%S")

            return {
                "order_id": order_id,
                "order_status": order_info["status"],
                "destination_station_id": dest_station_id,
                "destination_city": order_info["dest_city"],
                "suitable_trips": suitable_trips
            }


# ==========================================
# 3. Allocation & Rescheduling Engine (LM-07, LM-10..LM-14, LM-16..LM-18)
# ==========================================

@router.post("/allocate")
def allocate_rail_capacity(
    payload: RailAllocateRequest,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """Execute row-locked rail capacity allocation: specific chosen trip or multi-trip spillover."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            status_code = "UNKNOWN"
            if payload.trip_id:
                # Direct allocation to user's explicitly selected train
                cursor.execute(
                    """
                    SELECT tt.trip_id, tt.origin_station_id, tt.destination_station_id,
                           tt.departure_datetime, tt.arrival_datetime,
                           fn_trip_remaining_capacity(tt.trip_id) AS remaining_space
                    FROM train_trip tt
                    WHERE tt.trip_id = %s AND tt.status = 'SCHEDULED'
                    FOR UPDATE
                    """,
                    (payload.trip_id,)
                )
                trip = cursor.fetchone()
                if not trip:
                    raise HTTPException(status_code=404, detail="Selected train trip not found or not scheduled.")

                cursor.execute(
                    """
                    SELECT co.status, co.delivery_date, dr.station_id AS dest_station_id
                    FROM customer_order co
                    JOIN customer c ON c.customer_id = co.customer_id
                    JOIN delivery_route dr ON dr.route_id = c.route_id
                    WHERE co.order_id = %s
                    FOR UPDATE
                    """,
                    (payload.order_id,)
                )
                ord_info = cursor.fetchone()
                if not ord_info:
                    raise HTTPException(status_code=404, detail="Order not found in database.")
                if ord_info["status"] != "PENDING_RAIL_SCHEDULING":
                    raise HTTPException(status_code=409, detail="Order is not in PENDING_RAIL_SCHEDULING.")
                if ord_info["dest_station_id"] != trip["destination_station_id"]:
                    raise HTTPException(status_code=400, detail="Train destination does not match order delivery destination.")

                cursor.execute(
                    """
                    SELECT oi.order_item_id, oi.quantity, p.space_consumption_rate,
                           (oi.quantity * p.space_consumption_rate) AS item_space
                    FROM order_item oi
                    JOIN product p ON p.product_id = oi.product_id
                    WHERE oi.order_id = %s
                    """,
                    (payload.order_id,)
                )
                items = cursor.fetchall()
                total_needed = sum(float(i["item_space"]) for i in items)

                if float(trip["remaining_space"]) < total_needed:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Selected train only has {float(trip['remaining_space']):.1f} space units available, but order requires {total_needed:.1f} units. Use Auto Multi-Trip Spillover to split across multiple trains."
                    )

                for it in items:
                    cursor.execute(
                        """
                        INSERT INTO rail_allocation (order_item_id, trip_id, allocated_quantity, allocated_space, allocated_by)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (it["order_item_id"], payload.trip_id, it["quantity"], it["item_space"], current_user["user_id"])
                    )

                cursor.execute(
                    "UPDATE customer_order SET status = 'SCHEDULED_FOR_RAIL' WHERE order_id = %s",
                    (payload.order_id,)
                )

                cursor.execute(
                    """
                    INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                    VALUES (%s, 'SCHEDULE_RAIL_ORDER', %s, 'SUCCESS_SINGLE_TRIP', 'customer_order')
                    """,
                    (current_user["user_id"], payload.order_id)
                )
                status_code = "SUCCESS_SINGLE_TRIP"
            else:
                # Multi-trip spillover algorithm
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
            cursor.execute(
                """
                SELECT ra.allocation_id, oi.order_id, tt.trip_id, tt.departure_datetime,
                       ra.allocated_quantity, ra.allocated_space
                FROM rail_allocation ra
                JOIN order_item oi ON ra.order_item_id = oi.order_item_id
                JOIN train_trip tt ON ra.trip_id = tt.trip_id
                WHERE oi.order_id = %s
                ORDER BY tt.departure_datetime ASC, ra.allocation_id ASC
                """,
                (payload.order_id,)
            )
            allocations = cursor.fetchall()
            for alloc in allocations:
                if alloc.get("departure_datetime"):
                    alloc["departure_datetime"] = alloc["departure_datetime"].strftime("%Y-%m-%d %H:%M:%S")

            return {
                "order_id": payload.order_id,
                "status_result": status_code,
                "allocations": allocations
            }


@router.post("/orders/{order_id}/reverse")
def reverse_rail_allocation(
    order_id: int,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """Reverse rail allocations for an order, releasing trip capacity and resetting status."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "CALL sp_reverse_rail_allocation(%s, %s, @status_result);",
                (order_id, current_user["user_id"]),
            )
            cursor.execute("SELECT @status_result AS status_result;")
            res = cursor.fetchone()
            status_code = res["status_result"] if res else "UNKNOWN"

            if status_code != "SUCCESS_REVERSED":
                conn.rollback()
                raise HTTPException(
                    status_code=RESULT_HTTP_STATUS.get(status_code, 400),
                    detail=f"Rail allocation reversal rejected: {status_code}",
                )

            conn.commit()
            invalidate_cache("cache:rail:")

            return {
                "order_id": order_id,
                "status_result": status_code,
                "message": f"Rail allocations for Order #{order_id} successfully reversed. Order status returned to PENDING_RAIL_SCHEDULING."
            }


@router.post("/allocate/reverse")
def reverse_rail_allocation_alias(
    payload: ReverseAllocateRequest,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """Alias for reverse rail allocation accepting JSON body."""
    return reverse_rail_allocation(payload.order_id, current_user)


# ==========================================
# 4. Breakdowns, Trip Cargo & Audit Trail (LM-19, LM-20, LM-23)
# ==========================================

@router.get("/orders/{order_id}/allocations")
def get_order_allocations(
    order_id: int,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """View trip-by-trip allocation breakdown for an order (LM-19)."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT ra.allocation_id, ra.order_item_id, ra.trip_id,
                       tt.departure_datetime, ra.allocated_quantity, ra.allocated_space
                FROM rail_allocation ra
                JOIN order_item oi ON oi.order_item_id = ra.order_item_id
                JOIN train_trip tt ON tt.trip_id = ra.trip_id
                WHERE oi.order_id = %s
                ORDER BY tt.departure_datetime ASC, ra.allocation_id ASC
                """,
                (order_id,)
            )
            rows = cursor.fetchall()
            for r in rows:
                if r.get("departure_datetime"):
                    r["departure_datetime"] = r["departure_datetime"].strftime("%Y-%m-%d %H:%M:%S")
            return {"order_id": order_id, "allocations": rows}


@router.get("/trips/{trip_id}/allocations")
def get_trip_allocations(
    trip_id: int,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """View all order consignments booked onto a specific train trip (LM-20)."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT ra.allocation_id,
                       ra.trip_id,
                       oi.order_id,
                       c.customer_name,
                       p.product_name,
                       ra.allocated_quantity,
                       ra.allocated_space,
                       ra.allocated_at,
                       u.name AS allocated_by_name
                FROM rail_allocation ra
                JOIN order_item oi ON oi.order_item_id = ra.order_item_id
                JOIN customer_order co ON co.order_id = oi.order_id
                JOIN customer c ON c.customer_id = co.customer_id
                JOIN product p ON p.product_id = oi.product_id
                LEFT JOIN user u ON u.user_id = ra.allocated_by
                WHERE ra.trip_id = %s
                ORDER BY ra.allocation_id ASC
                """,
                (trip_id,)
            )
            rows = cursor.fetchall()
            for r in rows:
                if r.get("allocated_at"):
                    r["allocated_at"] = r["allocated_at"].strftime("%Y-%m-%d %H:%M:%S")
            return {"trip_id": trip_id, "allocations": rows}


@router.get("/trips/capacity")
def get_trip_capacity(
    destination_station_id: Optional[int] = None,
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    # Uses the v_trip_capacity_usage view (Feature 4.2) with direct SQL fallback
    query_view = '''
        SELECT trip_id, origin_station_id, destination_station_id,
               departure_datetime, status, total_capacity,
               used_space, remaining_space, utilisation_pct
        FROM v_trip_capacity_usage
        WHERE (%s IS NULL OR destination_station_id = %s)
        ORDER BY departure_datetime
    '''
    query_fallback = '''
        SELECT tt.trip_id, tt.origin_station_id, tt.destination_station_id,
               tt.departure_datetime, tt.status, tt.total_capacity,
               COALESCE(SUM(ra.allocated_space), 0) AS used_space,
               tt.total_capacity - COALESCE(SUM(ra.allocated_space), 0) AS remaining_space,
               ROUND((COALESCE(SUM(ra.allocated_space), 0) / tt.total_capacity) * 100, 2) AS utilisation_pct
        FROM train_trip tt
        LEFT JOIN rail_allocation ra ON tt.trip_id = ra.trip_id
        WHERE (%s IS NULL OR tt.destination_station_id = %s)
        GROUP BY tt.trip_id, tt.origin_station_id, tt.destination_station_id, tt.departure_datetime, tt.status, tt.total_capacity
        ORDER BY tt.departure_datetime
    '''
    with get_db() as conn:
        with conn.cursor() as cursor:
            try:
                cursor.execute(query_view, (destination_station_id, destination_station_id))
            except Exception as e:
                err_code = getattr(e, "args", [None])[0]
                if err_code in (1054, 1146):
                    try:
                        from app.core.migrations import run_migrations
                        run_migrations()
                        cursor.execute(query_view, (destination_station_id, destination_station_id))
                    except Exception:
                        cursor.execute(query_fallback, (destination_station_id, destination_station_id))
                else:
                    raise

            trips = cursor.fetchall()
            for t in trips:
                if t.get('departure_datetime'):
                    t['departure_datetime'] = t['departure_datetime'].strftime("%Y-%m-%d %H:%M:%S")
            return {"trips": trips}


@router.get("/audit")
def get_rail_audit_trail(
    current_user: dict = Depends(require_roles(RAIL_ROLES)),
):
    """Retrieve immutable audit log for rail scheduling, cancellations, and reversals (LM-23)."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.audit_id,
                       a.user_id,
                       u.name AS user_name,
                       u.role AS user_role,
                       a.action,
                       a.entity_id,
                       a.outcome,
                       a.occurred_at,
                       a.entity_name
                FROM audit_log a
                LEFT JOIN user u ON u.user_id = a.user_id
                WHERE a.action IN (
                    'SCHEDULE_RAIL_ORDER',
                    'REVERSE_RAIL_ORDER',
                    'REVERSE_RAIL_ALLOCATION_ITEM',
                    'CREATE_TRAIN_TRIP',
                    'UPDATE_TRAIN_TRIP',
                    'CANCEL_TRAIN_TRIP',
                    'ACTIVATE_TRAIN_TRIP'
                )
                ORDER BY a.occurred_at DESC, a.audit_id DESC
                LIMIT 100
                """
            )
            logs = cursor.fetchall()
            for log in logs:
                if log.get("occurred_at"):
                    log["occurred_at"] = log["occurred_at"].strftime("%Y-%m-%d %H:%M:%S")
            return {"audit_trail": logs}


# ==========================================
# 5. Public Schedules & Capacity Analytics
# ==========================================

@router.get("/schedules")
def get_train_schedules(
    response: Response,
):
    """Cached train schedule overview (TTL 60s, invalidated on allocation/trip updates)."""
    cache_key = "cache:rail:schedules"
    cached = get_cache(cache_key)
    if cached is not None:
        response.headers["X-Cache"] = "HIT"
        return cached

    query_view = '''
        SELECT tt.trip_id,
               ss1.city AS origin_city,
               ss2.city AS destination_city,
               tt.departure_datetime,
               tt.arrival_datetime,
               tt.total_capacity,
               COALESCE(v.remaining_space, tt.total_capacity) AS remaining_capacity,
               tt.status
        FROM train_trip tt
        JOIN station_store ss1 ON tt.origin_station_id = ss1.station_id
        JOIN station_store ss2 ON tt.destination_station_id = ss2.station_id
        JOIN v_trip_capacity_usage v ON v.trip_id = tt.trip_id
        ORDER BY tt.departure_datetime ASC
    '''
    query_fallback = '''
        SELECT tt.trip_id,
               ss1.city AS origin_city,
               ss2.city AS destination_city,
               tt.departure_datetime,
               tt.arrival_datetime,
               tt.total_capacity,
               (tt.total_capacity - COALESCE(SUM(ra.allocated_space), 0)) AS remaining_capacity,
               tt.status
        FROM train_trip tt
        JOIN station_store ss1 ON tt.origin_station_id = ss1.station_id
        JOIN station_store ss2 ON tt.destination_station_id = ss2.station_id
        LEFT JOIN rail_allocation ra ON tt.trip_id = ra.trip_id
        GROUP BY tt.trip_id, ss1.city, ss2.city, tt.departure_datetime, tt.arrival_datetime, tt.total_capacity, tt.status
        ORDER BY tt.departure_datetime ASC
    '''
    with get_db() as conn:
        with conn.cursor() as cursor:
            try:
                cursor.execute(query_view)
            except Exception as e:
                err_code = getattr(e, "args", [None])[0]
                if err_code in (1054, 1146):
                    try:
                        from app.core.migrations import run_migrations
                        run_migrations()
                        cursor.execute(query_view)
                    except Exception:
                        cursor.execute(query_fallback)
                else:
                    raise
            trips = cursor.fetchall()
            for trip in trips:
                if trip.get("departure_datetime"):
                    trip["departure_datetime"] = trip["departure_datetime"].strftime("%Y-%m-%d %H:%M:%S")
                if trip.get("arrival_datetime"):
                    trip["arrival_datetime"] = trip["arrival_datetime"].strftime("%Y-%m-%d %H:%M:%S")

            result = {"trips": trips}
            set_cache(cache_key, result, ttl_seconds=60)
            response.headers["X-Cache"] = "MISS"
            return result
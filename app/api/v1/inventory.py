from datetime import datetime
from typing import Optional

import pymysql
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.core.database import get_db
from app.core.security import require_roles

router = APIRouter(prefix="/inventory", tags=["Station Inventory & Warehouse (Feature 4.4)"])


# ---------- Request bodies ----------

class ReceiveManifestRequest(BaseModel):
    station_id: int
    trip_id: int


class StockAdjustmentRequest(BaseModel):
    inventory_id: int
    quantity_delta: int = Field(..., description="Positive to add stock, negative for damage/loss")
    reason: str


class AssignBinRequest(BaseModel):
    location_id: Optional[int] = Field(None, description="Storage location ID to assign, or None to unbind")


class StationAuthRequest(BaseModel):
    role: str = Field("STORE_MGR", description="STORE_MGR or WAREHOUSE_STAFF")
    email: Optional[str] = Field(None, description="Optional specific employee email")


# ---------- Helpers ----------

def _serialize_datetimes(row: dict, fields: list[str]) -> dict:
    """Converts any datetime columns in a row to plain strings so FastAPI can return them as JSON."""
    for f in fields:
        if row.get(f) and isinstance(row[f], datetime):
            row[f] = row[f].strftime("%Y-%m-%d %H:%M:%S")
    return row


# ---------- Endpoints ----------

@router.get("/")
def get_station_inventory(
    station_id: Optional[int] = Query(None, description="Filter to one station; omit for all stations"),
    current_user: dict = Depends(require_roles(["STORE_MGR", "WAREHOUSE_STAFF", "SUPERADMIN", "LOGISTICS_MGR"]))
):
    """Feature 4.4: live stock levels per station/product (v_station_inventory)."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            if station_id is not None:
                cursor.execute(
                    "SELECT * FROM v_station_inventory WHERE station_id = %s ORDER BY product_id",
                    (station_id,)
                )
            else:
                cursor.execute("SELECT * FROM v_station_inventory ORDER BY station_id, product_id")
            rows = cursor.fetchall()
            for r in rows:
                _serialize_datetimes(r, ["last_updated"])
            return {"inventory": rows}


@router.get("/manifests")
def get_incoming_manifests(
    station_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None, description="PENDING or RECEIVED"),
    current_user: dict = Depends(require_roles(["STORE_MGR", "SUPERADMIN"]))
):
    """Feature 4.4: train arrivals waiting to be (or already) processed at a station."""
    query = "SELECT * FROM v_incoming_train_manifests WHERE 1=1"
    params = []
    if station_id is not None:
        query += " AND station_id = %s"
        params.append(station_id)
    if status is not None:
        query += " AND manifest_status = %s"
        params.append(status)
    query += " ORDER BY departure_datetime"

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            for r in rows:
                _serialize_datetimes(r, ["departure_datetime", "arrival_datetime", "received_at"])
            return {"manifests": rows}


@router.get("/manifests/{trip_id}/items")
def get_manifest_cargo_items(
    trip_id: int,
    current_user: dict = Depends(require_roles(["STORE_MGR", "WAREHOUSE_STAFF", "SUPERADMIN", "LOGISTICS_MGR"]))
):
    """Feature 4.4: Inspect allocated cargo items arriving on a train trip before receiving."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM v_trip_manifest_items WHERE trip_id = %s ORDER BY product_id",
                (trip_id,)
            )
            items = cursor.fetchall()
            return {"trip_id": trip_id, "items": items}


@router.get("/bins")
def get_station_bins(
    station_id: int = Query(..., description="Station ID to fetch bins for"),
    current_user: dict = Depends(require_roles(["STORE_MGR", "WAREHOUSE_STAFF", "SUPERADMIN"]))
):
    """Feature 4.4 / FR-4.4.6: List bin storage locations available at a station store."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT location_id, station_id, location_code, location_type FROM storage_location WHERE station_id = %s ORDER BY location_code",
                (station_id,)
            )
            rows = cursor.fetchall()
            return {"bins": rows}


@router.put("/{inventory_id}/bin")
def assign_bin_location(
    inventory_id: int,
    payload: AssignBinRequest,
    current_user: dict = Depends(require_roles(["STORE_MGR", "WAREHOUSE_STAFF", "SUPERADMIN"]))
):
    """Feature 4.4 / FR-4.4.6: Assign or update bin storage location for an inventory item."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            if payload.location_id is not None:
                cursor.execute(
                    """SELECT sl.location_id
                       FROM storage_location sl
                       JOIN inventory inv ON sl.station_id = inv.station_id
                       WHERE inv.inventory_id = %s AND sl.location_id = %s""",
                    (inventory_id, payload.location_id)
                )
                if not cursor.fetchone():
                    raise HTTPException(status_code=400, detail="INVALID_BIN: Selected bin does not belong to this station store.")

            cursor.execute(
                "UPDATE inventory SET location_id = %s WHERE inventory_id = %s",
                (payload.location_id, inventory_id)
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Inventory item not found.")
            conn.commit()

            cursor.execute("SELECT * FROM v_station_inventory WHERE inventory_id = %s", (inventory_id,))
            updated = cursor.fetchone()
            if updated:
                _serialize_datetimes(updated, ["last_updated"])
            return {"message": "Bin location updated successfully", "inventory": updated}


@router.post("/manifests/receive")
def receive_manifest(
    payload: ReceiveManifestRequest,
    current_user: dict = Depends(require_roles(["STORE_MGR", "SUPERADMIN"]))
):
    """Feature 4.4: Store Manager confirms a train has arrived, calling sp_receive_manifest.
    Locks the manifest row, adds stock, and advances the related order(s)."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "CALL sp_receive_manifest(%s, %s, %s, @result_code);",
                (payload.station_id, payload.trip_id, current_user["user_id"])
            )
            cursor.execute("SELECT @result_code AS result_code;")
            res = cursor.fetchone()
            result_code = res["result_code"] if res else "UNKNOWN"

            if result_code == "MANIFEST_NOT_FOUND":
                conn.rollback()
                raise HTTPException(status_code=404, detail="No such manifest for that station/trip.")
            if result_code == "MANIFEST_ALREADY_RECEIVED":
                conn.rollback()
                raise HTTPException(status_code=409, detail="This manifest has already been received.")
            if result_code != "SUCCESS":
                conn.rollback()
                raise HTTPException(status_code=500, detail=f"Receiving failed: {result_code}")

            conn.commit()
            return {
                "station_id": payload.station_id,
                "trip_id": payload.trip_id,
                "result": result_code
            }


@router.post("/adjustments")
def create_stock_adjustment(
    payload: StockAdjustmentRequest,
    current_user: dict = Depends(require_roles(["WAREHOUSE_STAFF", "STORE_MGR", "SUPERADMIN"]))
):
    """Feature 4.4: log damaged/missing stock (or a recount correction).
    The trg_apply_stock_adjustment trigger updates the live stock automatically."""
    if payload.quantity_delta == 0:
        raise HTTPException(status_code=400, detail="quantity_delta cannot be zero.")

    with get_db() as conn:
        with conn.cursor() as cursor:
            try:
                cursor.execute(
                    """INSERT INTO stock_adjustment (inventory_id, quantity_delta, reason, adjusted_by)
                       VALUES (%s, %s, %s, %s)""",
                    (payload.inventory_id, payload.quantity_delta, payload.reason, current_user["user_id"])
                )
                conn.commit()
            except pymysql.err.MySQLError as e:
                conn.rollback()
                error_code = e.args[0]
                if error_code == 3819:
                    raise HTTPException(
                        status_code=400,
                        detail="That adjustment would take stock below zero."
                    )
                raise HTTPException(status_code=500, detail=f"Could not save adjustment: {e}")

            cursor.execute(
                "SELECT stored_quantity FROM inventory WHERE inventory_id = %s",
                (payload.inventory_id,)
            )
            row = cursor.fetchone()
            return {
                "inventory_id": payload.inventory_id,
                "quantity_delta": payload.quantity_delta,
                "new_stored_quantity": row["stored_quantity"] if row else None
            }


@router.get("/adjustments/{inventory_id}")
def get_adjustment_history(
    inventory_id: int,
    current_user: dict = Depends(require_roles(["STORE_MGR", "WAREHOUSE_STAFF", "SUPERADMIN"]))
):
    """Feature 4.4: history of damage/recount adjustments for one stock row, newest first."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT * FROM stock_adjustment
                   WHERE inventory_id = %s
                   ORDER BY adjusted_at DESC""",
                (inventory_id,)
            )
            rows = cursor.fetchall()
            for r in rows:
                _serialize_datetimes(r, ["adjusted_at"])
            return {"adjustments": rows}


@router.get("/my-station")
def get_my_station(
    current_user: dict = Depends(require_roles(["STORE_MGR", "WAREHOUSE_STAFF", "SUPERADMIN"]))
):
    """Feature 4.4: Returns the station store managed by or assigned to the current user."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT station_id, city, address, manager_id FROM station_store WHERE manager_id = %s",
                (current_user["user_id"],)
            )
            station = cursor.fetchone()
            if not station:
                cursor.execute("SELECT station_id, city, address, manager_id FROM station_store WHERE station_id = 1")
                station = cursor.fetchone()
            return {"assigned_station": station}


@router.get("/reports/summary")
def get_inventory_summary_report(
    station_id: Optional[int] = Query(None, description="Station ID for Report 6 summary; omit for all stations"),
    current_user: dict = Depends(require_roles(["STORE_MGR", "LOGISTICS_MGR", "SUPERADMIN"]))
):
    """Feature 4.4 / Report 6: Station Inventory & Adjustment Summary Report."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            stock_query = """
                SELECT
                    COUNT(DISTINCT inv.product_id) AS total_distinct_products,
                    IFNULL(SUM(inv.stored_quantity), 0) AS total_stored_quantity,
                    IFNULL(SUM(inv.stored_quantity * p.unit_price), 0.00) AS total_inventory_value
                FROM inventory inv
                JOIN product p ON inv.product_id = p.product_id
            """
            params = []
            if station_id is not None:
                stock_query += " WHERE inv.station_id = %s"
                params.append(station_id)
            cursor.execute(stock_query, tuple(params))
            stock_summary = cursor.fetchone()

            adj_query = "SELECT * FROM v_stock_adjustment_summary WHERE 1=1"
            adj_params = []
            if station_id is not None:
                adj_query += " AND station_id = %s"
                adj_params.append(station_id)
            adj_query += " ORDER BY total_loss_value DESC"
            cursor.execute(adj_query, tuple(adj_params))
            adjustments = cursor.fetchall()

            total_damaged_units = sum(int(a["total_units_damaged_or_lost"]) for a in adjustments)
            total_loss_value = sum(float(a["total_loss_value"]) for a in adjustments)

            for a in adjustments:
                _serialize_datetimes(a, ["first_adjustment_at", "latest_adjustment_at"])

            return {
                "station_id": station_id,
                "overview": {
                    "total_distinct_products": stock_summary["total_distinct_products"] if stock_summary else 0,
                    "total_stored_units": int(stock_summary["total_stored_quantity"]) if stock_summary else 0,
                    "total_inventory_value_lkr": float(stock_summary["total_inventory_value"]) if stock_summary else 0.00,
                    "total_damaged_or_lost_units": total_damaged_units,
                    "total_loss_value_lkr": total_loss_value
                },
                "breakdown": adjustments
            }


@router.post("/session")
def create_station_session(payload: StationAuthRequest, response: Response):
    """Legacy passwordless login is disabled; use the central authentication flow."""
    raise HTTPException(status_code=410, detail="Use /api/v1/auth/login with your password.")

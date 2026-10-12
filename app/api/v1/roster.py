"""Authenticated roster reads and durable MySQL assignment creation."""

import logging
import re
import os
import pymysql
from datetime import date, datetime, timedelta
from typing import Annotated, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.security import require_roles

from app.roster.dependencies import (
    get_roster_assignment_repository, get_roster_reporting_repository, get_roster_repository,
    require_roster_reader, require_roster_writer,
)
from app.roster.mysql_adapter import MySQLRosterAdapter
from app.roster.policy import AssignmentProposal
from app.roster.repository import (
    RosterBusinessRejection, RosterDataError, RosterReportingRepository, RosterRepository,
    RosterWindowError, RosterIdempotencyConflict,
)
from app.roster.schemas import (
    AssignmentCreatedResponse, AssignmentRequest, AssignmentsResponse, AuditResponse,
    CandidatesResponse, HoursResponse, parse_instant,
)
from app.roster.schemas import (CargoSelection, CargoAssignedResponse, StoresResponse,
                                DemandResponse, SchedulesResponse, LoadingListResponse)
from app.roster.cargo import CargoRepository, CargoError

logger = logging.getLogger(__name__)


class RosterRoute(APIRoute):
    """Keep both roster data and authentication/validation errors out of caches."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store_handler(request: Request):
            try:
                response = await handler(request)
            except StarletteHTTPException as exc:
                response = await http_exception_handler(request, exc)
            except RequestValidationError as exc:
                response = await request_validation_exception_handler(request, exc)
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store_handler


router = APIRouter(
    prefix="/roster", tags=["Fleet Roster & Driver Assignment"], route_class=RosterRoute
)


def get_cargo_repository() -> CargoRepository:
    if os.environ.get("ROSTER_DATA_MODE", "mysql") != "mysql":
        raise HTTPException(status_code=503, detail={"error_code": "TRUCK_SCHEDULING_UNAVAILABLE",
                            "message": "Planned cargo requires the roster MySQL database."})
    return CargoRepository()


def cargo_operation(operation):
    try:
        return operation()
    except CargoError as exc:
        raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message}) from exc
    except Exception as exc:
        retryable = isinstance(exc, pymysql.OperationalError) and exc.args[0] in (1205, 1213, 2006, 2013)
        logger.exception("Planned cargo operation failed")
        raise HTTPException(status_code=503, detail={
            "error_code": "TRANSACTION_RETRY_REQUIRED" if retryable else "TRUCK_SCHEDULING_UNAVAILABLE",
            "message": "Read the loading list and retry the same selection." if retryable else "Planned cargo is unavailable. Check the fresh schema and retry.",
        }) from exc


def assignment_window(
    from_time: Annotated[str, Query(alias="from")],
    to_time: Annotated[str, Query(alias="to")],
) -> tuple[datetime, datetime]:
    try:
        start, end = parse_instant(from_time), parse_instant(to_time)
        if end <= start:
            raise ValueError("to must be later than from.")
        return start, end
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "INVALID_ROSTER_WINDOW", "message": str(exc)},
        ) from exc


def selected_week(week_start: Annotated[str, Query()]) -> date:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", week_start) is None:
            raise ValueError("week_start must use YYYY-MM-DD format.")
        result = date.fromisoformat(week_start)
        if result.year < 1000:
            raise ValueError("week_start is outside the supported MySQL date range.")
        if result.weekday() != 0:
            raise ValueError("week_start must be a Monday in Asia/Colombo.")
        result + timedelta(days=7)
        return result
    except (OverflowError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "INVALID_ROSTER_WEEK", "message": str(exc)},
        ) from exc


@router.get("/candidates", response_model=CandidatesResponse)
def get_roster_candidates(
    current_user: dict = Depends(require_roster_reader),
    repository: RosterRepository = Depends(get_roster_repository),
):
    try:
        return repository.candidates()
    except RosterDataError as exc:
        logger.error("Roster catalog read failed: %s", exc.error_code)
        raise HTTPException(status_code=503, detail={"error_code": exc.error_code, "message": exc.message}) from exc
    except Exception as exc:
        logger.exception("Roster catalog read failed")
        raise HTTPException(
            status_code=503,
            detail={"error_code": "ROSTER_DATA_UNAVAILABLE", "message": "Roster data is unavailable. Retry later."},
        ) from exc


@router.get("/assignments", response_model=AssignmentsResponse)
def get_roster_assignments(
    current_user: dict = Depends(require_roster_reader),
    window: tuple[datetime, datetime] = Depends(assignment_window),
    repository: RosterRepository = Depends(get_roster_repository),
):
    try:
        return repository.assignments(*window)
    except RosterWindowError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "INVALID_ROSTER_WINDOW", "message": str(exc)}) from exc
    except RosterDataError as exc:
        logger.error("Roster assignment read failed: %s", exc.error_code)
        raise HTTPException(status_code=503, detail={"error_code": exc.error_code, "message": exc.message}) from exc
    except Exception as exc:
        logger.exception("Roster assignment read failed")
        raise HTTPException(
            status_code=503,
            detail={"error_code": "ROSTER_DATA_UNAVAILABLE", "message": "Roster data is unavailable. Retry later."},
        ) from exc


@router.get("/hours", response_model=HoursResponse)
def get_roster_hours(
    current_user: dict = Depends(require_roster_reader),
    week_start: date = Depends(selected_week),
    repository: RosterReportingRepository = Depends(get_roster_reporting_repository),
):
    try:
        return repository.hours(week_start)
    except RosterWindowError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "INVALID_ROSTER_WEEK", "message": str(exc)},
        ) from exc
    except RosterDataError as exc:
        logger.error("Roster hours read failed: %s", exc.error_code)
        raise HTTPException(
            status_code=503, detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc
    except Exception as exc:
        logger.exception("Roster hours read failed")
        raise HTTPException(
            status_code=503,
            detail={"error_code": "ROSTER_DATA_UNAVAILABLE", "message": "Roster data is unavailable. Retry later."},
        ) from exc


@router.get("/audit", response_model=AuditResponse)
def get_roster_audit(
    current_user: dict = Depends(require_roster_reader),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    repository: RosterReportingRepository = Depends(get_roster_reporting_repository),
):
    try:
        return repository.audit(limit)
    except RosterWindowError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "INVALID_ROSTER_AUDIT_LIMIT", "message": str(exc)},
        ) from exc
    except RosterDataError as exc:
        logger.error("Roster audit read failed: %s", exc.error_code)
        raise HTTPException(
            status_code=503, detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc
    except Exception as exc:
        logger.exception("Roster audit read failed")
        raise HTTPException(
            status_code=503,
            detail={"error_code": "ROSTER_DATA_UNAVAILABLE", "message": "Roster data is unavailable. Retry later."},
        ) from exc


@router.post("/assign", status_code=status.HTTP_201_CREATED, response_model=AssignmentCreatedResponse)
def assign_roster(
    request: AssignmentRequest,
    current_user: dict = Depends(require_roster_writer),
    repository: MySQLRosterAdapter = Depends(get_roster_assignment_repository),
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)
    ] = None,
):
    proposal = AssignmentProposal(**request.model_dump())
    try:
        result = repository.assign(
            proposal,
            actor_id=current_user["user_id"],
            request_key=idempotency_key or str(uuid4()),
        )
        return AssignmentCreatedResponse(
            result_code="ROSTER_ASSIGNMENT_REPLAYED" if result.replayed else "ROSTER_ASSIGNED",
            message="Roster assignment already exists." if result.replayed else "Roster assignment created.",
            assignment=result.assignment,
        )
    except RosterIdempotencyConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc
    except RosterBusinessRejection as exc:
        missing = {"ROUTE_NOT_FOUND", "TRUCK_NOT_FOUND", "DRIVER_NOT_FOUND", "ASSISTANT_NOT_FOUND"}
        invalid = {
            "DRIVER_STAFF_TYPE_INVALID", "ASSISTANT_STAFF_TYPE_INVALID",
            "DRIVER_ASSISTANT_SAME_PERSON", "INVALID_ASSIGNMENT_INTERVAL", "TRUCK_INACTIVE",
            "DRIVER_INACTIVE", "ASSISTANT_INACTIVE",
        }
        rejection_status = 404 if exc.error_code in missing else 422 if exc.error_code in invalid else 409
        raise HTTPException(
            status_code=rejection_status,
            detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc
    except RosterDataError as exc:
        logger.error("Roster assignment failed: %s", exc.error_code)
        raise HTTPException(
            status_code=503,
            detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc
    except Exception as exc:
        logger.exception("Roster assignment failed")
        raise HTTPException(
            status_code=503,
            detail={"error_code": "ROSTER_ASSIGNMENT_FAILED", "message": "Roster assignment could not be stored."},
        ) from exc


class DriverDeliveryCompleteRequest(BaseModel):
    proof_reference: Optional[str] = Field("Handed over to recipient, signed.", max_length=500)


@router.get("/stores", response_model=StoresResponse)
def cargo_stores(user: dict = Depends(require_roster_reader), repository=Depends(get_cargo_repository)):
    return cargo_operation(lambda: repository.stores(user))


@router.get("/demand", response_model=DemandResponse)
def cargo_demand(station_id: Annotated[int, Query(gt=0)], from_date: date, to_date: date,
                 user: dict = Depends(require_roster_reader), repository=Depends(get_cargo_repository)):
    if from_date.year < 1000 or to_date < from_date or (to_date-from_date).days > 366:
        raise HTTPException(status_code=422, detail={"error_code": "INVALID_DEMAND_WINDOW", "message": "Choose an ordered date range of at most 366 days."})
    return cargo_operation(lambda: repository.demand(station_id, from_date, to_date))


@router.get("/schedules", response_model=SchedulesResponse)
def cargo_schedules(station_id: Annotated[int, Query(gt=0)],
                    window: tuple[datetime, datetime] = Depends(assignment_window),
                    user: dict = Depends(require_roster_reader), repository=Depends(get_cargo_repository)):
    return cargo_operation(lambda: repository.schedules(station_id, *window))


@router.get("/schedules/{roster_id}/loading-list", response_model=LoadingListResponse)
def cargo_loading_list(roster_id: int, user: dict = Depends(require_roster_reader), repository=Depends(get_cargo_repository)):
    return cargo_operation(lambda: repository.loading_list(roster_id))


@router.put("/schedules/{roster_id}/orders", response_model=CargoAssignedResponse)
def assign_whole_orders(roster_id: int, request: CargoSelection,
                        user: dict = Depends(require_roster_writer), repository=Depends(get_cargo_repository)):
    return cargo_operation(lambda: repository.assign(roster_id, request.order_ids, user["user_id"]))


@router.get("/driver/my-deliveries")
def get_driver_deliveries(
    current_user: dict = Depends(require_roles(["DRIVER", "ASSISTANT", "SUPERADMIN", "DISPATCHER", "LOGISTICS_MGR"]))
):
    """Driver views their assigned delivery runs and order stops."""
    from app.core.database import get_db
    with get_db() as conn:
        with conn.cursor() as cursor:
            params = []
            where_sql = "d.delivery_status <> 'CANCELLED'"
            if current_user["role"] in ("DRIVER", "ASSISTANT"):
                cursor.execute("SELECT delivery_staff_id FROM delivery_staff WHERE user_id = %s", (current_user["user_id"],))
                ds = cursor.fetchone()
                if not ds:
                    return {"deliveries": []}
                if current_user["role"] == "DRIVER":
                    where_sql += " AND ra.driver_id = %s"
                else:
                    where_sql += " AND ra.assistant_id = %s"
                params.append(ds["delivery_staff_id"])

            sql = f"""
                SELECT 
                    d.delivery_id,
                    d.order_id,
                    d.delivery_status,
                    d.cargo_weight_kg,
                    d.proof_reference,
                    d.delivered_at,
                    ra.roster_id,
                    ra.start_time,
                    ra.end_time,
                    ra.status AS run_status,
                    t.plate_number AS truck_plate,
                    dr.route_name,
                    ss.city AS station_name,
                    co.recipient_name,
                    co.recipient_phone,
                    co.delivery_address,
                    co.status AS order_status
                FROM delivery d
                JOIN roster_assignment ra ON ra.roster_id = d.roster_id
                JOIN truck t ON t.truck_id = ra.truck_id
                JOIN delivery_route dr ON dr.route_id = ra.route_id
                JOIN station_store ss ON ss.station_id = dr.station_id
                JOIN customer_order co ON co.order_id = d.order_id
                WHERE {where_sql}
                ORDER BY d.delivery_id DESC
            """
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
            for r in rows:
                if r.get("delivered_at") and isinstance(r["delivered_at"], datetime):
                    r["delivered_at"] = r["delivered_at"].strftime("%Y-%m-%d %H:%M:%S")
                if r.get("start_time") and isinstance(r["start_time"], datetime):
                    r["start_time"] = r["start_time"].strftime("%Y-%m-%d %H:%M:%S")
                if r.get("end_time") and isinstance(r["end_time"], datetime):
                    r["end_time"] = r["end_time"].strftime("%Y-%m-%d %H:%M:%S")
                r["cargo_weight_kg"] = str(r["cargo_weight_kg"])
            return {"deliveries": rows}


@router.post("/deliveries/{delivery_id}/complete")
def complete_driver_delivery(
    delivery_id: int,
    payload: DriverDeliveryCompleteRequest = DriverDeliveryCompleteRequest(),
    current_user: dict = Depends(require_roles(["DRIVER", "ASSISTANT", "SUPERADMIN", "DISPATCHER", "LOGISTICS_MGR"]))
):
    """Complete a specific delivery run stop and update the related customer order."""
    from app.core.database import get_db
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT d.delivery_id, d.order_id, d.roster_id, d.delivery_status, co.customer_id
                FROM delivery d
                JOIN customer_order co ON co.order_id = d.order_id
                WHERE d.delivery_id = %s
            """, (delivery_id,))
            deliv = cursor.fetchone()
            if not deliv:
                raise HTTPException(status_code=404, detail="Delivery record not found.")

            now = datetime.now()
            cursor.execute("""
                UPDATE delivery 
                SET delivery_status = 'DELIVERED', delivered_at = %s, proof_reference = %s
                WHERE delivery_id = %s
            """, (now, payload.proof_reference or "Handed over to recipient, signed.", delivery_id))

            cursor.execute("UPDATE customer_order SET status = 'DELIVERED' WHERE order_id = %s", (deliv["order_id"],))
            cursor.execute("""
                INSERT INTO order_status_history (status, order_id, changed_by, changed_at)
                VALUES ('DELIVERED', %s, %s, %s)
            """, (deliv["order_id"], current_user["user_id"], now))

            # Send notification & thank-you email
            cursor.execute("""
                SELECT 
                    u.email, u.user_id, u.name AS user_name,
                    c.customer_name,
                    co.recipient_name, co.recipient_phone, co.delivery_address, co.total_amount
                FROM customer c 
                JOIN `user` u ON u.user_id = c.user_id 
                JOIN customer_order co ON co.customer_id = c.customer_id
                WHERE c.customer_id = %s AND co.order_id = %s
            """, (deliv["customer_id"], deliv["order_id"]))
            cust_u = cursor.fetchone()
            if cust_u:
                proof_text = payload.proof_reference or "Handed over to recipient, signed."
                cursor.execute("""
                    INSERT INTO notification (user_id, recipient_email, notification_type, title, message, order_id, is_read, created_at)
                    VALUES (%s, %s, 'DELIVERY_UPDATE', 'Consignment Delivered to Doorstep', %s, %s, 0, %s)
                """, (
                    cust_u["user_id"], cust_u["email"],
                    f"Your order #{deliv['order_id']} has been delivered to {cust_u.get('delivery_address', 'your doorstep')}. Proof: {proof_text}. Please inspect and confirm receipt.",
                    deliv["order_id"], now
                ))

                # Dispatch customer thank-you email via notification service
                from app.core.notifications import log_and_dispatch_email
                recipient_disp_name = cust_u.get("recipient_name") or cust_u.get("customer_name") or cust_u.get("user_name") or "Valued Customer"
                deliv_address = cust_u.get("delivery_address") or "Your registered delivery address"
                email_html = f"""
                <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 28px; background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; color: #1e293b;">
                    <div style="text-align: center; margin-bottom: 24px;">
                        <span style="font-size: 26px; font-weight: 800; color: #16a34a; letter-spacing: -0.5px;">🚂 KandyPack Logistics</span>
                        <p style="margin: 4px 0 0 0; font-size: 13px; color: #64748b; font-weight: 500;">Rail & Road Supply Chain Network</p>
                    </div>

                    <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 20px; margin-bottom: 24px; text-align: center;">
                        <span style="font-size: 32px; display: block; margin-bottom: 8px;">📦✨</span>
                        <h2 style="color: #15803d; margin: 0 0 6px 0; font-size: 20px; font-weight: 700;">Consignment Delivered!</h2>
                        <p style="color: #166534; margin: 0; font-size: 14px;">Order <strong>#{deliv['order_id']}</strong> was marked as successfully delivered by the driver.</p>
                    </div>

                    <p style="font-size: 15px; line-height: 1.6; color: #334155;">
                        Dear <strong>{recipient_disp_name}</strong>,<br><br>
                        Thank you for choosing <strong>KandyPack</strong>! We are pleased to confirm that your shipment has completed its journey from our central depot and has been delivered to your doorstep.
                    </p>

                    <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 18px; margin: 20px 0;">
                        <h4 style="margin: 0 0 12px 0; font-size: 12px; text-transform: uppercase; color: #64748b; letter-spacing: 0.5px;">Delivery Summary</h4>
                        <table style="width: 100%; font-size: 14px; border-collapse: collapse;">
                            <tr>
                                <td style="padding: 6px 0; color: #64748b; width: 38%;">Order Number:</td>
                                <td style="padding: 6px 0; font-weight: 600; color: #0f172a;">#{deliv['order_id']}</td>
                            </tr>
                            <tr>
                                <td style="padding: 6px 0; color: #64748b;">Delivered To:</td>
                                <td style="padding: 6px 0; font-weight: 500; color: #0f172a;">{deliv_address}</td>
                            </tr>
                            <tr>
                                <td style="padding: 6px 0; color: #64748b;">Delivery Time:</td>
                                <td style="padding: 6px 0; color: #0f172a;">{now.strftime('%d/%m/%Y at %I:%M %p')}</td>
                            </tr>
                            <tr>
                                <td style="padding: 6px 0; color: #64748b;">Driver Handover Note:</td>
                                <td style="padding: 6px 0; font-style: italic; color: #0f172a;">"{proof_text}"</td>
                            </tr>
                        </table>
                    </div>

                    <p style="font-size: 14px; line-height: 1.6; color: #475569;">
                        Please inspect your delivered items. Once you're satisfied, sign in to your KandyPack account to confirm receipt and close your shipment.
                    </p>

                    <div style="text-align: center; margin: 28px 0 20px 0;">
                        <a href="http://localhost:3000/orders/{deliv['order_id']}/track" style="background: #16a34a; color: #ffffff; text-decoration: none; padding: 12px 28px; border-radius: 10px; font-weight: 600; font-size: 14px; display: inline-block; box-shadow: 0 4px 12px rgba(22, 163, 74, 0.25);">View Shipment & Confirm Receipt</a>
                    </div>

                    <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 24px 0 16px 0;" />
                    <p style="color: #94a3b8; font-size: 12px; text-align: center; margin: 0;">
                        Thank you for your trust in KandyPack Logistics. If you have any questions, reply to this email or reach out to your local depot.
                    </p>
                </div>
                """
                try:
                    log_and_dispatch_email(
                        to_email=cust_u["email"],
                        subject=f"[KandyPack] Order #{deliv['order_id']} Delivered — Thank you for choosing us!",
                        html_content=email_html,
                        notification_type="DELIVERY_CONFIRMATION"
                    )
                except Exception as mail_err:
                    logger.warning("Could not dispatch customer delivery email: %s", mail_err)

            conn.commit()
            return {"status": "SUCCESS", "message": f"Delivery #{delivery_id} marked as DELIVERED.", "order_id": deliv["order_id"]}


@router.get("/train-trips")
def get_roster_incoming_train_trips(
    station_id: int = Query(..., description="Target destination station ID"),
    current_user: dict = Depends(require_roles(["DISPATCHER", "SUPERADMIN", "LOGISTICS_MGR", "STORE_MGR", "WAREHOUSE_STAFF"]))
):
    """List arriving freight train trips, manifests, and cargo count for the selected regional station store."""
    from app.core.database import get_db
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 
                    tt.trip_id,
                    tt.origin_station_id,
                    ss_orig.city AS origin_city,
                    tt.destination_station_id,
                    ss_dest.city AS destination_city,
                    tt.departure_datetime,
                    tt.arrival_datetime,
                    tt.total_capacity,
                    tt.status AS trip_status,
                    COALESCE(m.manifest_id, 0) AS manifest_id,
                    COALESCE(m.status, 'SCHEDULED') AS manifest_status,
                    m.received_at,
                    COUNT(DISTINCT ra.order_item_id) AS allocated_items_count,
                    COUNT(DISTINCT oi.order_id) AS allocated_orders_count,
                    COALESCE(SUM(ra.allocated_quantity * p.unit_weight_kg), 0) AS total_cargo_weight_kg
                FROM train_trip tt
                JOIN station_store ss_orig ON ss_orig.station_id = tt.origin_station_id
                JOIN station_store ss_dest ON ss_dest.station_id = tt.destination_station_id
                LEFT JOIN manifest m ON m.trip_id = tt.trip_id AND m.station_id = tt.destination_station_id
                LEFT JOIN rail_allocation ra ON ra.trip_id = tt.trip_id
                LEFT JOIN order_item oi ON oi.order_item_id = ra.order_item_id
                LEFT JOIN product p ON p.product_id = oi.product_id
                WHERE tt.destination_station_id = %s
                GROUP BY tt.trip_id, tt.origin_station_id, ss_orig.city, tt.destination_station_id, ss_dest.city,
                         tt.departure_datetime, tt.arrival_datetime, tt.total_capacity, tt.status,
                         m.manifest_id, m.status, m.received_at
                ORDER BY tt.departure_datetime DESC, tt.trip_id DESC
                LIMIT 20
            """, (station_id,))
            rows = cursor.fetchall()
            for r in rows:
                if r.get("departure_datetime"):
                    r["departure_datetime"] = str(r["departure_datetime"])
                if r.get("arrival_datetime"):
                    r["arrival_datetime"] = str(r["arrival_datetime"])
                if r.get("received_at"):
                    r["received_at"] = str(r["received_at"])
                r["total_cargo_weight_kg"] = float(r["total_cargo_weight_kg"])
                r["total_capacity"] = float(r["total_capacity"])
            return {"trips": rows, "station_id": station_id}

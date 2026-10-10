"""Read-only management reports over the canonical MySQL schema."""
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pymysql
from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.database import get_db
from app.core.security import require_roles
from app.roster.mysql_adapter import MySQLRosterAdapter
from app.roster.repository import RosterDataError, RosterWindowError
from app.api.v1.roster import RosterRoute

logger = logging.getLogger(__name__)
REPORT_ROLES = ["SUPERADMIN"]
router = APIRouter(prefix="/reports", tags=["Management Reports & Analytics"],
                   route_class=RosterRoute, dependencies=[Depends(require_roles(REPORT_ROLES))])


def rows(query, parameters=()):
    # Never perform DDL, seed data, or substitute today's prices during a read.
    try:
        with get_db() as conn, conn.cursor() as cursor:
            cursor.execute(query, parameters)
            return cursor.fetchall()
    except pymysql.MySQLError as exc:
        logger.exception("Management reporting query failed")
        raise HTTPException(503, "Reporting unavailable. Check the reporting migrations and database connection.") from exc


@router.get("/quarterly-sales")
def get_quarterly_sales():
    return {"quarterly_sales": rows("SELECT * FROM v_report_quarterly_sales ORDER BY sales_year,sales_quarter,route_id,product_id")}


@router.get("/top-products")
def get_top_products(top_n: int = Query(1, ge=1, le=100)):
    return {"top_products": rows("SELECT * FROM v_report_top_products WHERE product_rank<=%s ORDER BY sales_year,sales_quarter,product_rank,product_id", (top_n,))}


@router.get("/rail-capacity-utilisation")
def get_rail_capacity_utilisation():
    return {"rail_capacity_utilisation": rows("SELECT * FROM v_report_rail_capacity ORDER BY dep_year,dep_month,station_id,row_level")}


@router.get("/workforce-hours")
def get_workforce_hours(week_start: date | None = None):
    if week_start is None:
        today = datetime.now(ZoneInfo("Asia/Colombo")).date()
        week_start = today - timedelta(days=today.weekday())
    try:
        result = MySQLRosterAdapter().hours(week_start)
    except RosterWindowError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (RosterDataError, pymysql.MySQLError) as exc:
        logger.exception("Workforce report failed")
        raise HTTPException(503, "Dated workforce reporting is unavailable.") from exc
    data = []
    for item in result.hours:
        scheduled, cap = item.scheduled_seconds, item.limit_seconds
        data.append({"delivery_staff_id": item.staff_id, "staff_name": item.staff_name,
                     "staff_role": item.staff_type, "accumulated_hours": scheduled / 3600,
                     "weekly_cap": cap / 3600, "remaining_hours": item.remaining_seconds / 3600,
                     "utilization_pct": round(100 * scheduled / cap, 2),
                     "status_flag": "OVER_CAP" if scheduled > cap else "NEAR_CAP_WARNING" if scheduled * 10 >= cap * 9 else "SAFE"})
    return {"workforce_hours": data, "week_start": result.week_start, "week_end": result.week_end}


@router.get("/truck-utilisation")
def get_truck_utilisation():
    return {"truck_utilisation": rows("SELECT * FROM v_report_truck_utilisation ORDER BY usage_year,usage_month,truck_id")}


@router.get("/station-inventory")
def get_station_inventory_report():
    return {"station_inventory": rows("SELECT * FROM v_report_station_inventory ORDER BY station_id,product_id")}


# Existing auxiliary contracts remain available.
@router.get("/driver-caps")
def get_driver_cap_warnings(week_start: date | None = None):
    report = get_workforce_hours(week_start)
    return {"driver_cap_warnings": [
        {"driver_id": r["delivery_staff_id"], "full_name": r["staff_name"],
         "accumulated_weekly_hours": r["accumulated_hours"], "cap_hours": r["weekly_cap"],
         "utilization_pct": r["utilization_pct"], "status_flag": r["status_flag"]}
        for r in report["workforce_hours"] if r["staff_role"] == "DRIVER"],
        "week_start": report["week_start"], "week_end": report["week_end"]}


@router.get("/analytics")
def get_rail_analytics():
    return {"analytics": rows("""SELECT s.city AS destination_hub,YEAR(o.order_date) AS order_year,
        QUARTER(o.order_date) AS order_quarter,COUNT(DISTINCT o.order_id) AS total_orders,
        SUM(a.allocated_quantity) AS total_units_shipped,SUM(a.allocated_space) AS total_cubic_meters_shipped
        FROM customer_order o JOIN delivery_route r ON r.route_id=o.delivery_route_id
        JOIN station_store s ON s.station_id=r.station_id JOIN order_item i ON i.order_id=o.order_id
        JOIN rail_allocation a ON a.order_item_id=i.order_item_id
        WHERE o.status<>'CANCELLED' GROUP BY s.station_id,s.city,YEAR(o.order_date),QUARTER(o.order_date)""")}


@router.get("/city-route-sales")
def get_city_route_sales():
    return {"city_route_sales": rows("""SELECT s.city AS city_name,r.route_name,
        SUM(i.quantity) AS total_quantity,ROUND(SUM(i.quantity*i.unit_price_at_order),2) AS total_sales
        FROM customer_order o JOIN delivery_route r ON r.route_id=o.delivery_route_id
        JOIN station_store s ON s.station_id=r.station_id
        JOIN order_item i ON i.order_id=o.order_id
        WHERE o.status<>'CANCELLED' GROUP BY s.station_id,s.city,r.route_id,r.route_name ORDER BY s.city,r.route_name""")}


@router.get("/audit-logs")
def get_roster_audit_logs():
    logs = rows("""SELECT audit_id AS log_id,user_id AS dispatcher_id,occurred_at AS attempt_timestamp,
        outcome AS status,action AS violation_rule,entity_name AS details FROM audit_log ORDER BY audit_id DESC LIMIT 50""")
    for log in logs:
        if log.get("attempt_timestamp"):
            log["attempt_timestamp"] = log["attempt_timestamp"].strftime("%Y-%m-%d %H:%M:%S")
    return {"audit_logs": logs}

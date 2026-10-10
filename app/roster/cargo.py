"""Whole-order planned cargo reads and atomic assignments"""

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pymysql

from app.core.database import get_db

ZONE = ZoneInfo("Asia/Colombo")


class CargoError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def placeholders(values):
    return ",".join("%s" for _ in values)


def money(value):
    return format(Decimal(value).quantize(Decimal("0.01")), "f")


def serialized(row):
    return {key: (value.replace(tzinfo=ZONE).isoformat() if isinstance(value, datetime)
                  else money(value) if isinstance(value, Decimal) else value)
            for key, value in row.items()}


ORDER_SQL = """
    SELECT co.order_id, co.delivery_date, co.status AS order_status,
           co.delivery_route_id AS route_id, co.delivery_address,
           co.recipient_name, co.recipient_phone, c.customer_name,
           dr.station_id, dr.route_name, ss.city AS station_name,
           d.roster_id AS assigned_roster_id, d.delivery_id,
           d.cargo_weight_kg AS assigned_weight_kg
    FROM customer_order co
    JOIN customer c ON c.customer_id=co.customer_id
    JOIN delivery_route dr ON dr.route_id=co.delivery_route_id
    JOIN station_store ss ON ss.station_id=dr.station_id
    LEFT JOIN delivery d ON d.order_id=co.order_id AND d.delivery_status<>'CANCELLED'
"""

ITEM_SQL = """
    SELECT oi.order_id, oi.order_item_id, oi.product_id, p.product_name,
           oi.quantity AS ordered_quantity, p.unit_weight_kg,
           COALESCE(a.allocated_quantity,0) AS allocated_quantity,
           COALESCE(a.received_quantity,0) AS received_quantity,
           COALESCE(a.wrong_destination,0) AS wrong_destination
    FROM order_item oi
    JOIN product p ON p.product_id=oi.product_id
    JOIN customer_order co ON co.order_id=oi.order_id
    JOIN delivery_route dr ON dr.route_id=co.delivery_route_id
    LEFT JOIN (
        SELECT ra.order_item_id, SUM(ra.allocated_quantity) AS allocated_quantity,
               SUM(CASE WHEN tt.destination_station_id=dest.station_id
                        AND m.station_id=dest.station_id AND m.status='RECEIVED'
                        AND m.received_at IS NOT NULL THEN ra.allocated_quantity ELSE 0 END) AS received_quantity,
               SUM(CASE WHEN tt.destination_station_id<>dest.station_id THEN 1 ELSE 0 END) AS wrong_destination
        FROM rail_allocation ra
        JOIN train_trip tt ON tt.trip_id=ra.trip_id
        JOIN order_item allocated_item ON allocated_item.order_item_id=ra.order_item_id
        JOIN customer_order allocated_order ON allocated_order.order_id=allocated_item.order_id
        JOIN delivery_route dest ON dest.route_id=allocated_order.delivery_route_id
        LEFT JOIN manifest m ON m.trip_id=tt.trip_id AND m.station_id=tt.destination_station_id
        GROUP BY ra.order_item_id
    ) a ON a.order_item_id=oi.order_item_id
    WHERE oi.order_id IN ({ids}) ORDER BY oi.order_id, oi.order_item_id
"""

SCHEDULE_SQL = """
    SELECT r.roster_id, r.route_id, dr.station_id, ss.city AS station_name, dr.route_name,
           r.truck_id, t.plate_number, t.capacity, t.capacity_unit, t.is_active,
           r.driver_id, driver.name AS driver_name, r.assistant_id, assistant.name AS assistant_name,
           r.start_time, r.end_time, r.status,
           (SELECT COUNT(*) FROM delivery d WHERE d.roster_id=r.roster_id AND d.delivery_status<>'CANCELLED') AS order_count,
           COALESCE((SELECT SUM(d.cargo_weight_kg) FROM delivery d
                     WHERE d.roster_id=r.roster_id AND d.delivery_status<>'CANCELLED'),0) AS cargo_weight_kg,
           COALESCE((SELECT SUM(oi.quantity) FROM delivery d JOIN order_item oi ON oi.order_id=d.order_id
                     WHERE d.roster_id=r.roster_id AND d.delivery_status<>'CANCELLED'),0) AS unit_count
    FROM roster_assignment r
    JOIN delivery_route dr ON dr.route_id=r.route_id
    JOIN station_store ss ON ss.station_id=dr.station_id
    JOIN truck t ON t.truck_id=r.truck_id
    JOIN delivery_staff ds ON ds.delivery_staff_id=r.driver_id
    JOIN user driver ON driver.user_id=ds.user_id
    JOIN delivery_staff ast ON ast.delivery_staff_id=r.assistant_id
    JOIN user assistant ON assistant.user_id=ast.user_id
"""


def assess_order(order, items):
    reasons = []
    if not all(str(order.get(key) or "").strip() for key in
               ("delivery_address", "recipient_name", "recipient_phone")):
        reasons.append("ORDER_DESTINATION_UNVERIFIED")
    if order["order_status"] in ("DELIVERED", "COMPLETED", "CANCELLED"):
        reasons.append("ORDER_CLOSED")
    if not items or any(int(i["ordered_quantity"]) <= 0
                        or int(i["allocated_quantity"]) != int(i["ordered_quantity"])
                        or int(i["received_quantity"]) != int(i["ordered_quantity"])
                        for i in items):
        reasons.append("ORDER_NOT_FULLY_RECEIVED")
    if any(i["wrong_destination"] for i in items):
        reasons.append("ORDER_ROUTE_MISMATCH")
    weight = Decimal(0)
    for item in items:
        unit_weight = item["unit_weight_kg"]
        if unit_weight is None or Decimal(unit_weight) <= 0:
            if "INVALID_PRODUCT_WEIGHT" not in reasons:
                reasons.append("INVALID_PRODUCT_WEIGHT")
        else:
            weight += int(item["ordered_quantity"]) * Decimal(unit_weight)
    return weight, reasons


@contextmanager
def reading():
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            try:
                yield cursor
            finally:
                conn.rollback()


class CargoRepository:
    def start(self, roster_id, actor_id):
        """Start only already attached, fully received whole orders, atomically."""
        with get_db() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    conn.begin()
                    cursor.execute("SELECT route_id,truck_id FROM roster_assignment WHERE roster_id=%s", (roster_id,))
                    identity = cursor.fetchone()
                    if not identity:
                        raise CargoError("ROSTER_NOT_FOUND", "Truck schedule not found.", 404)
                    # Same resource order as whole-order attachment and roster creation.
                    cursor.execute("SELECT station_id FROM delivery_route WHERE route_id=%s FOR UPDATE", (identity['route_id'],))
                    station = cursor.fetchone()['station_id']
                    cursor.execute("SELECT is_active FROM truck WHERE truck_id=%s FOR UPDATE", (identity['truck_id'],))
                    truck = cursor.fetchone()
                    cursor.execute("SELECT * FROM roster_assignment WHERE roster_id=%s FOR UPDATE", (roster_id,))
                    run = cursor.fetchone()
                    if not run or run['route_id'] != identity['route_id'] or run['truck_id'] != identity['truck_id']:
                        raise CargoError("RUN_CHANGED", "The schedule changed. Refresh the loading list and retry.")
                    cursor.execute("SELECT order_id FROM delivery WHERE roster_id=%s AND delivery_status<>'CANCELLED' ORDER BY order_id", (roster_id,))
                    ids = [row['order_id'] for row in cursor.fetchall()]
                    if not ids:
                        raise CargoError("RUN_EMPTY", "Attach whole orders before starting delivery.")
                    for order_id in ids:
                        cursor.execute("SELECT order_id FROM customer_order WHERE order_id=%s FOR UPDATE", (order_id,))
                        cursor.fetchone()
                    cursor.execute(f"SELECT oi.order_item_id FROM order_item oi JOIN product p ON p.product_id=oi.product_id WHERE oi.order_id IN ({placeholders(ids)}) ORDER BY oi.order_item_id FOR UPDATE", tuple(ids))
                    cursor.fetchall()
                    cursor.execute(f"SELECT ra.allocation_id FROM rail_allocation ra JOIN order_item oi ON oi.order_item_id=ra.order_item_id JOIN train_trip tt ON tt.trip_id=ra.trip_id LEFT JOIN manifest m ON m.trip_id=tt.trip_id AND m.station_id=tt.destination_station_id WHERE oi.order_id IN ({placeholders(ids)}) ORDER BY ra.allocation_id FOR SHARE", tuple(ids))
                    cursor.fetchall()
                    cursor.execute("SELECT delivery_id,order_id,delivery_status FROM delivery WHERE roster_id=%s AND delivery_status<>'CANCELLED' ORDER BY delivery_id FOR UPDATE", (roster_id,))
                    deliveries = cursor.fetchall()
                    orders = self._orders(cursor, f"co.order_id IN ({placeholders(ids)})", tuple(ids))
                    if sorted(d['order_id'] for d in deliveries) != ids or len(orders) != len(ids) or any(o['assigned_roster_id'] != roster_id for o in orders):
                        raise CargoError("RUN_CHANGED", "Attached orders changed. Refresh the loading list and retry.")
                    if run['status'] == 'IN_TRANSIT' and all(d['delivery_status'] == 'IN_TRANSIT' for d in deliveries) and all(o['order_status'] == 'OUT_FOR_DELIVERY' for o in orders):
                        conn.rollback()
                        return dict(status='SUCCESS', result_code='DELIVERY_ALREADY_STARTED', roster_id=roster_id)
                    if run['status'] != 'SCHEDULED' or any(d['delivery_status'] != 'ASSIGNED' for d in deliveries):
                        raise CargoError("INVALID_START_TRANSITION", "Only scheduled runs with assigned deliveries can start.")
                    if not truck['is_active']:
                        raise CargoError("TRUCK_INACTIVE", "The scheduled truck is inactive.")
                    for order in orders:
                        if order['order_status'] != 'ARRIVED_AT_STATION_STORE':
                            raise CargoError("INVALID_ORDER_TRANSITION", "Every attached order must be received at the station before departure.")
                        if order['route_id'] != run['route_id'] or order['station_id'] != station:
                            raise CargoError("ORDER_ROUTE_MISMATCH", "An attached order does not match the scheduled route.")
                        reasons = [r for r in order['blocked_reasons'] if r != 'ORDER_ALREADY_ASSIGNED']
                        if reasons:
                            raise CargoError(reasons[0], "An attached order no longer satisfies station receipt and cargo checks.")
                    cursor.execute("UPDATE roster_assignment SET status='IN_TRANSIT' WHERE roster_id=%s", (roster_id,))
                    cursor.execute("UPDATE delivery SET delivery_status='IN_TRANSIT' WHERE roster_id=%s AND delivery_status='ASSIGNED'", (roster_id,))
                    for order_id in ids:
                        cursor.execute("UPDATE customer_order SET status='OUT_FOR_DELIVERY' WHERE order_id=%s", (order_id,))
                        cursor.execute("INSERT INTO order_status_history (order_id,status,changed_by) VALUES (%s,'OUT_FOR_DELIVERY',%s)", (order_id, actor_id))
                    cursor.execute("INSERT INTO audit_log (user_id,action,entity_id,outcome,entity_name,roster_id) VALUES (%s,'START_DELIVERY',%s,'ACCEPTED','roster_assignment',NULL)", (actor_id, roster_id))
                conn.commit()
                return dict(status='SUCCESS', result_code='DELIVERY_STARTED', roster_id=roster_id)
            except Exception:
                conn.rollback()
                raise

    def stores(self):
        with reading() as cursor:
            cursor.execute("SELECT station_id, city AS station_name, address FROM station_store WHERE is_active=1 ORDER BY city, station_id")
            return {"stores": cursor.fetchall(), "timezone": "Asia/Colombo"}

    def _orders(self, cursor, where, params):
        cursor.execute(ORDER_SQL + " WHERE " + where + " ORDER BY co.delivery_date, co.order_id", params)
        orders = cursor.fetchall()
        if not orders:
            return []
        ids = [o["order_id"] for o in orders]
        cursor.execute(ITEM_SQL.format(ids=placeholders(ids)), ids)
        by_order = {i: [] for i in ids}
        for item in cursor.fetchall():
            for key in ("allocated_quantity", "received_quantity", "wrong_destination"):
                item[key] = int(item[key])
            by_order[item["order_id"]].append(item)
        result = []
        for order in orders:
            items = by_order[order["order_id"]]
            weight, reasons = assess_order(order, items)
            if order["assigned_roster_id"] is not None:
                reasons.append("ORDER_ALREADY_ASSIGNED")
            result.append(serialized(order) | {
                "items": [serialized(i) for i in items], "weight_kg": money(weight),
                "eligible": not reasons, "blocked_reasons": reasons,
            })
        return result

    def demand(self, station_id, from_date, to_date):
        with reading() as cursor:
            orders = self._orders(cursor, "dr.station_id=%s AND co.delivery_date BETWEEN %s AND %s",
                                  (station_id, from_date, to_date))
            return {"orders": orders, "timezone": "Asia/Colombo"}

    def schedules(self, station_id, start, end):
        with reading() as cursor:
            cursor.execute(SCHEDULE_SQL + " WHERE dr.station_id=%s AND r.start_time<%s AND r.end_time>%s ORDER BY r.start_time,r.roster_id",
                           (station_id, end.astimezone(ZONE).replace(tzinfo=None), start.astimezone(ZONE).replace(tzinfo=None)))
            return {"schedules": [serialized(r | {"unit_count": int(r["unit_count"])}) for r in cursor.fetchall()], "timezone": "Asia/Colombo"}

    def loading_list(self, roster_id):
        with reading() as cursor:
            cursor.execute(SCHEDULE_SQL + " WHERE r.roster_id=%s", (roster_id,))
            schedule = cursor.fetchone()
            if schedule is None:
                raise CargoError("ROSTER_NOT_FOUND", "Truck schedule does not exist.", 404)
            orders = self._orders(cursor, "d.roster_id=%s", (roster_id,))
            return {"schedule": serialized(schedule | {"unit_count": int(schedule["unit_count"])}), "orders": orders, "timezone": "Asia/Colombo"}

    def assign(self, roster_id, order_ids, actor_id):
        ids = sorted(order_ids)
        with get_db() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    conn.begin()
                    cursor.execute("SELECT route_id,truck_id FROM roster_assignment WHERE roster_id=%s", (roster_id,))
                    identity = cursor.fetchone()
                    if identity is None:
                        raise CargoError("ROSTER_NOT_FOUND", "Truck schedule does not exist.", 404)
                    cursor.execute("SELECT station_id FROM delivery_route WHERE route_id=%s FOR UPDATE", (identity["route_id"],))
                    station_id = cursor.fetchone()["station_id"]
                    cursor.execute("SELECT capacity,capacity_unit,is_active FROM truck WHERE truck_id=%s FOR UPDATE", (identity["truck_id"],))
                    truck = cursor.fetchone()
                    cursor.execute("SELECT route_id,truck_id,status FROM roster_assignment WHERE roster_id=%s FOR UPDATE", (roster_id,))
                    run = cursor.fetchone()
                    if run is None or any(run[k] != identity[k] for k in ("route_id", "truck_id")):
                        raise CargoError("RUN_NOT_SCHEDULED", "Truck schedule changed. Refresh and retry.")
                    for order_id in ids:
                        cursor.execute("SELECT order_id FROM customer_order WHERE order_id=%s FOR UPDATE", (order_id,))
                        if cursor.fetchone() is None:
                            raise CargoError("ORDER_NOT_FOUND", "A selected order does not exist.", 404)
                    cursor.execute("SELECT order_item_id,product_id FROM order_item WHERE order_id IN (" + placeholders(ids) + ") ORDER BY order_item_id FOR UPDATE", ids)
                    products = sorted({r["product_id"] for r in cursor.fetchall()})
                    for product_id in products:
                        cursor.execute("SELECT product_id FROM product WHERE product_id=%s FOR UPDATE", (product_id,))
                    orders = self._orders(cursor, "co.order_id IN (" + placeholders(ids) + ")", ids)
                    if len(orders) != len(ids):
                        raise CargoError("ORDER_DESTINATION_UNVERIFIED", "A selected order has no valid saved destination.", 422)
                    new_orders = []
                    for order in orders:
                        assigned = order["assigned_roster_id"]
                        if assigned is not None:
                            if assigned != roster_id:
                                raise CargoError("ORDER_ALREADY_ASSIGNED", "A selected order is already assigned to another truck schedule.")
                            continue
                        if order["route_id"] != run["route_id"] or order["station_id"] != station_id:
                            raise CargoError("ORDER_ROUTE_MISMATCH", "All orders must match the schedule's saved route and station.", 422)
                        if order["blocked_reasons"]:
                            code = order["blocked_reasons"][0]
                            raise CargoError(code, "A selected order is not eligible: " + code.replace("_", " ").lower() + ".",
                                             422 if code in ("INVALID_PRODUCT_WEIGHT", "ORDER_DESTINATION_UNVERIFIED", "ORDER_ROUTE_MISMATCH") else 409)
                        new_orders.append(order)
                    cursor.execute("SELECT cargo_weight_kg FROM delivery WHERE roster_id=%s AND delivery_status<>'CANCELLED' ORDER BY delivery_id FOR UPDATE", (roster_id,))
                    total = sum((Decimal(r["cargo_weight_kg"]) for r in cursor.fetchall()), Decimal(0))
                    if new_orders:
                        if run["status"] != "SCHEDULED":
                            raise CargoError("RUN_NOT_SCHEDULED", "Only scheduled runs accept new orders.")
                        if not truck["is_active"]:
                            raise CargoError("TRUCK_INACTIVE", "The selected truck is inactive.", 422)
                        if truck["capacity_unit"] != "KG":
                            raise CargoError("TRUCK_CAPACITY_UNIT_UNVERIFIED", "Truck capacity must be explicitly verified in kilograms.", 422)
                        total += sum((Decimal(o["weight_kg"]) for o in new_orders), Decimal(0))
                        if total > Decimal(truck["capacity"]):
                            raise CargoError("TRUCK_CAPACITY_EXCEEDED", "The selected whole orders exceed truck capacity.")
                        now = datetime.now(ZONE).replace(tzinfo=None, microsecond=0)
                        for order in new_orders:
                            cursor.execute("INSERT INTO delivery (roster_id,order_id,delivery_status,assigned_at,assigned_by,cargo_weight_kg) VALUES (%s,%s,'ASSIGNED',%s,%s,%s)",
                                           (roster_id, order["order_id"], now, actor_id, order["weight_kg"]))
                            delivery_id = cursor.lastrowid
                            cursor.execute("INSERT INTO audit_log (user_id,action,entity_id,outcome,entity_name,roster_id,occurred_at) VALUES (%s,'ASSIGN_ORDER_TO_ROSTER',%s,'ACCEPTED','delivery',NULL,%s)",
                                           (actor_id, delivery_id, now))
                    result = {"status": "SUCCESS", "result_code": "ORDERS_ASSIGNED" if new_orders else "ORDERS_ALREADY_ASSIGNED",
                              "roster_id": roster_id, "order_ids": ids, "cargo_weight_kg": money(total), "timezone": "Asia/Colombo"}
                    conn.commit()
                    return result
            except pymysql.IntegrityError as exc:
                conn.rollback()
                if exc.args[0] == 1062:
                    raise CargoError("ORDER_ALREADY_ASSIGNED", "A selected order was assigned concurrently. Refresh the loading list.") from exc
                raise
            except Exception:
                conn.rollback()
                raise

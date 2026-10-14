from fastapi import APIRouter, Depends
from app.core.database import get_db
from app.core.security import require_roles

REPORT_ROLES = ["SUPERADMIN"]

router = APIRouter(
    prefix="/reports",
    tags=["Management Reports & Analytics"],
    dependencies=[Depends(require_roles(REPORT_ROLES))],
)

def _execute_report_query(cursor, primary_query: str, fallback_query: str = None):
    """Executes a report SQL query with auto-migration recovery and fallback schema support."""
    try:
        cursor.execute(primary_query)
    except Exception as e:
        err_code = getattr(e, "args", [None])[0]
        if err_code in (1054, 1146):
            try:
                from app.core.migrations import run_migrations
                run_migrations()
                cursor.execute(primary_query)
                return
            except Exception:
                if fallback_query:
                    cursor.execute(fallback_query)
                    return
        raise

@router.get("/analytics")
def get_rail_analytics():
    # Queries v_quarterly_rail_analytics database view with fallback
    query_primary = "SELECT * FROM v_quarterly_rail_analytics"
    query_fallback = """
        SELECT 
            ss.city AS destination_hub,
            YEAR(co.order_date) AS order_year,
            QUARTER(co.order_date) AS order_quarter,
            COUNT(DISTINCT co.order_id) AS total_orders,
            SUM(ra.allocated_quantity) AS total_units_shipped,
            SUM(ra.allocated_space) AS total_cubic_meters_shipped
        FROM customer_order co
        JOIN customer c ON co.customer_id = c.customer_id
        LEFT JOIN delivery_route dr ON co.delivery_route_id = dr.route_id
        LEFT JOIN station_store ss ON dr.station_id = ss.station_id
        JOIN order_item oi ON co.order_id = oi.order_id
        JOIN rail_allocation ra ON oi.order_item_id = ra.order_item_id
        GROUP BY ss.city, YEAR(co.order_date), QUARTER(co.order_date);
    """
    with get_db() as conn:
        with conn.cursor() as cursor:
            _execute_report_query(cursor, query_primary, query_fallback)
            data = cursor.fetchall()
            return {"analytics": data}

@router.get("/driver-caps")
def get_driver_cap_warnings():
    # Queries v_drivers_near_cap database view for SR-5.2.4 visual warnings
    query_primary = "SELECT * FROM v_drivers_near_cap"
    query_fallback = """
        SELECT 
            ds.delivery_staff_id AS driver_id,
            u.name AS full_name,
            ds.work_hours AS accumulated_weekly_hours,
            40.00 AS cap_hours,
            ROUND((ds.work_hours / 40.00) * 100, 1) AS utilization_pct,
            CASE 
                WHEN ds.work_hours >= 40.00 THEN 'CAP_REACHED'
                WHEN ds.work_hours >= 36.00 THEN 'WARNING_NEAR_CAP'
                ELSE 'SAFE'
            END AS status_flag
        FROM delivery_staff ds
        JOIN user u ON ds.user_id = u.user_id
        WHERE u.role = 'DRIVER';
    """
    with get_db() as conn:
        with conn.cursor() as cursor:
            _execute_report_query(cursor, query_primary, query_fallback)
            data = cursor.fetchall()
            return {"driver_cap_warnings": data}
@router.get("/audit-logs")
def get_roster_audit_logs():
    # Fetches immutable roster audit log entries for audit inspections
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT audit_id AS log_id, user_id AS dispatcher_id, occurred_at AS attempt_timestamp, 
                       outcome AS status, action AS violation_rule, entity_name AS details
                FROM audit_log
                ORDER BY audit_id DESC
                LIMIT 50
            ''')
            logs = cursor.fetchall()
            for log in logs:
                if log.get('attempt_timestamp'):
                    log['attempt_timestamp'] = log['attempt_timestamp'].strftime("%Y-%m-%d %H:%M:%S")
            return {"audit_logs": logs}


@router.get("/quarterly-sales")
def get_quarterly_sales():
    query_primary = """
        SELECT
            YEAR(co.order_date) AS sales_year,
            QUARTER(co.order_date) AS sales_quarter,
            dr.route_name,
            p.product_name,
            SUM(oi.quantity) AS total_quantity,
            ROUND(
                SUM(oi.quantity * oi.unit_price_at_order),
                2
            ) AS total_sales
        FROM customer_order co
        JOIN customer c
            ON co.customer_id = c.customer_id
        LEFT JOIN delivery_route dr
            ON co.delivery_route_id = dr.route_id
        JOIN order_item oi
            ON co.order_id = oi.order_id
        JOIN product p
            ON oi.product_id = p.product_id
        GROUP BY
            YEAR(co.order_date),
            QUARTER(co.order_date),
            dr.route_name,
            p.product_name
        ORDER BY
            sales_year,
            sales_quarter,
            dr.route_name,
            p.product_name
    """
    query_fallback = """
        SELECT
            YEAR(co.order_date) AS sales_year,
            QUARTER(co.order_date) AS sales_quarter,
            dr.route_name,
            p.product_name,
            SUM(oi.quantity) AS total_quantity,
            ROUND(
                SUM(oi.quantity * p.unit_price),
                2
            ) AS total_sales
        FROM customer_order co
        JOIN customer c
            ON co.customer_id = c.customer_id
        LEFT JOIN delivery_route dr
            ON co.delivery_route_id = dr.route_id
        JOIN order_item oi
            ON co.order_id = oi.order_id
        JOIN product p
            ON oi.product_id = p.product_id
        GROUP BY
            YEAR(co.order_date),
            QUARTER(co.order_date),
            dr.route_name,
            p.product_name
        ORDER BY
            sales_year,
            sales_quarter,
            dr.route_name,
            p.product_name
    """
    with get_db() as conn:
        with conn.cursor() as cursor:
            _execute_report_query(cursor, query_primary, query_fallback)
            data = cursor.fetchall()
            return {"quarterly_sales": data}

        
@router.get("/top-products")
def get_top_products():
    query_primary = """
        WITH quarterly_product_sales AS (
            SELECT
                YEAR(co.order_date) AS sales_year,
                QUARTER(co.order_date) AS sales_quarter,
                p.product_id,
                p.product_name,
                SUM(oi.quantity) AS total_quantity,
                ROUND(
                    SUM(oi.quantity * oi.unit_price_at_order),
                    2
                ) AS total_sales
            FROM customer_order co
            JOIN order_item oi
                ON co.order_id = oi.order_id
            JOIN product p
                ON oi.product_id = p.product_id
            GROUP BY
                YEAR(co.order_date),
                QUARTER(co.order_date),
                p.product_id,
                p.product_name
        ),
        ranked_products AS (
            SELECT
                *,
                DENSE_RANK() OVER (
                    PARTITION BY sales_year, sales_quarter
                    ORDER BY total_quantity DESC
                ) AS product_rank
            FROM quarterly_product_sales
        )
        SELECT
            sales_year,
            sales_quarter,
            product_id,
            product_name,
            total_quantity,
            total_sales,
            product_rank
        FROM ranked_products
        WHERE product_rank = 1
        ORDER BY
            sales_year,
            sales_quarter
    """
    query_fallback = """
        WITH quarterly_product_sales AS (
            SELECT
                YEAR(co.order_date) AS sales_year,
                QUARTER(co.order_date) AS sales_quarter,
                p.product_id,
                p.product_name,
                SUM(oi.quantity) AS total_quantity,
                ROUND(
                    SUM(oi.quantity * p.unit_price),
                    2
                ) AS total_sales
            FROM customer_order co
            JOIN order_item oi
                ON co.order_id = oi.order_id
            JOIN product p
                ON oi.product_id = p.product_id
            GROUP BY
                YEAR(co.order_date),
                QUARTER(co.order_date),
                p.product_id,
                p.product_name
        ),
        ranked_products AS (
            SELECT
                *,
                DENSE_RANK() OVER (
                    PARTITION BY sales_year, sales_quarter
                    ORDER BY total_quantity DESC
                ) AS product_rank
            FROM quarterly_product_sales
        )
        SELECT
            sales_year,
            sales_quarter,
            product_id,
            product_name,
            total_quantity,
            total_sales,
            product_rank
        FROM ranked_products
        WHERE product_rank = 1
        ORDER BY
            sales_year,
            sales_quarter
    """
    with get_db() as conn:
        with conn.cursor() as cursor:
            _execute_report_query(cursor, query_primary, query_fallback)
            data = cursor.fetchall()
            return {"top_products": data}


@router.get("/city-route-sales")
def get_city_route_sales():
    query_primary = """
        SELECT
            ss.city AS city_name,
            dr.route_name,
            SUM(oi.quantity) AS total_quantity,
            ROUND(
                SUM(oi.quantity * oi.unit_price_at_order),
                2
            ) AS total_sales
        FROM customer_order co
        JOIN customer c
            ON co.customer_id = c.customer_id
        JOIN delivery_route dr
            ON co.delivery_route_id = dr.route_id
        JOIN station_store ss
            ON dr.station_id = ss.station_id
        JOIN order_item oi
            ON co.order_id = oi.order_id
        GROUP BY
            ss.city,
            dr.route_name
        ORDER BY
            ss.city,
            dr.route_name
    """
    query_fallback = """
        SELECT
            ss.city AS city_name,
            dr.route_name,
            SUM(oi.quantity) AS total_quantity,
            ROUND(
                SUM(oi.quantity * p.unit_price),
                2
            ) AS total_sales
        FROM customer_order co
        JOIN customer c
            ON co.customer_id = c.customer_id
        JOIN delivery_route dr
            ON co.delivery_route_id = dr.route_id
        JOIN station_store ss
            ON dr.station_id = ss.station_id
        JOIN order_item oi
            ON co.order_id = oi.order_id
        JOIN product p
            ON oi.product_id = p.product_id
        GROUP BY
            ss.city,
            dr.route_name
        ORDER BY
            ss.city,
            dr.route_name
    """
    with get_db() as conn:
        with conn.cursor() as cursor:
            _execute_report_query(cursor, query_primary, query_fallback)
            data = cursor.fetchall()
            return {"city_route_sales": data}

@router.get("/rail-capacity-utilisation")
def get_rail_capacity_utilisation():
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                WITH trip_capacity AS (
                    SELECT
                        tt.trip_id,
                        tt.destination_station_id,
                        YEAR(tt.departure_datetime) AS dep_year,
                        MONTH(tt.departure_datetime) AS dep_month,
                        tt.total_capacity
                    FROM train_trip tt
                ),
                trip_allocations AS (
                    SELECT
                        trip_id,
                        SUM(allocated_space) AS allocated_space
                    FROM rail_allocation
                    GROUP BY trip_id
                )
                SELECT
                    ss.city AS destination_hub,
                    tc.dep_year,
                    tc.dep_month,

                    ROUND(SUM(tc.total_capacity), 2) AS total_capacity,

                    ROUND(
                        SUM(COALESCE(ta.allocated_space, 0)),
                        2
                    ) AS allocated_capacity,

                    ROUND(
                        SUM(tc.total_capacity)
                        - SUM(COALESCE(ta.allocated_space, 0)),
                        2
                    ) AS remaining_capacity,

                    ROUND(
                        (
                            SUM(COALESCE(ta.allocated_space, 0))
                            / NULLIF(SUM(tc.total_capacity), 0)
                        ) * 100,
                        2
                    ) AS utilization_percentage

                FROM trip_capacity tc

                JOIN station_store ss
                    ON tc.destination_station_id = ss.station_id

                LEFT JOIN trip_allocations ta
                    ON tc.trip_id = ta.trip_id

                GROUP BY
                    ss.city,
                    tc.dep_year,
                    tc.dep_month

                ORDER BY
                    tc.dep_year,
                    tc.dep_month,
                    ss.city
            """)

            data = cursor.fetchall()

            return {"rail_capacity_utilisation": data}


@router.get("/workforce-hours")
def get_workforce_hours():
    query = """
        WITH split_assignments AS (
            -- Segments in the initial work week up to the Monday week boundary
            SELECT 
                ra.roster_id, ra.driver_id, ra.assistant_id,
                YEARWEEK(ra.start_time, 1) AS work_week,
                TIMESTAMPDIFF(MINUTE, ra.start_time, 
                    LEAST(ra.end_time, TIMESTAMP(DATE_ADD(DATE(ra.start_time), INTERVAL (7 - WEEKDAY(ra.start_time)) DAY)))
                ) / 60.0 AS duration_hours
            FROM roster_assignment ra
            WHERE ra.status <> 'CANCELLED'
            
            UNION ALL
            
            -- Segments that cross the Monday week boundary into the subsequent week
            SELECT 
                ra.roster_id, ra.driver_id, ra.assistant_id,
                YEARWEEK(ra.end_time, 1) AS work_week,
                TIMESTAMPDIFF(MINUTE, 
                    TIMESTAMP(DATE_ADD(DATE(ra.start_time), INTERVAL (7 - WEEKDAY(ra.start_time)) DAY)),
                    ra.end_time
                ) / 60.0 AS duration_hours
            FROM roster_assignment ra
            WHERE ra.status <> 'CANCELLED'
              AND YEARWEEK(ra.start_time, 1) <> YEARWEEK(ra.end_time, 1)
        ),
        weekly_hours AS (
            SELECT
                ds.delivery_staff_id,
                u.name AS staff_name,
                u.role AS staff_role,
                sa.work_week,
                SUM(sa.duration_hours) AS accumulated_hours,
                CASE
                    WHEN u.role = 'DRIVER' THEN 40.00
                    WHEN u.role = 'ASSISTANT' THEN 60.00
                END AS weekly_cap
            FROM split_assignments sa
            JOIN delivery_staff ds
                ON (
                    sa.driver_id = ds.delivery_staff_id
                    OR sa.assistant_id = ds.delivery_staff_id
                )
            JOIN user u
                ON ds.user_id = u.user_id
            WHERE
                u.role IN ('DRIVER', 'ASSISTANT')
            GROUP BY
                ds.delivery_staff_id,
                u.name,
                u.role,
                sa.work_week
        )
        SELECT
            delivery_staff_id,
            staff_name,
            staff_role,
            work_week,
            ROUND(accumulated_hours, 2) AS accumulated_hours,
            weekly_cap,
            ROUND(
                accumulated_hours / NULLIF(weekly_cap, 0) * 100,
                1
            ) AS utilization_pct,
            CASE
                WHEN accumulated_hours >= weekly_cap
                    THEN 'CAP_REACHED'
                WHEN accumulated_hours >= weekly_cap * 0.9
                    THEN 'NEAR_CAP_WARNING'
                ELSE 'SAFE'
            END AS status_flag
        FROM weekly_hours
        ORDER BY work_week, staff_name
    """

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            return {"workforce_hours": cursor.fetchall()}

@router.get("/truck-utilisation")
def get_truck_utilisation():
    query = """
        SELECT
            t.truck_id,
            t.plate_number,
            YEAR(ra.start_time) AS usage_year,
            MONTH(ra.start_time) AS usage_month,
            COUNT(ra.roster_id) AS total_delivery_runs,
            COUNT(DISTINCT ra.route_id) AS total_routes,
            ROUND(
                SUM(
                    TIMESTAMPDIFF(
                        MINUTE,
                        ra.start_time,
                        ra.end_time
                    )
                ) / 60.0,
                2
            ) AS total_operating_hours
        FROM truck t
        JOIN roster_assignment ra
            ON t.truck_id = ra.truck_id
        WHERE ra.status <> 'CANCELLED'
        GROUP BY
            t.truck_id,
            t.plate_number,
            YEAR(ra.start_time),
            MONTH(ra.start_time)
        ORDER BY
            usage_year,
            usage_month,
            t.truck_id
    """

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            return {"truck_utilisation": cursor.fetchall()}

@router.get("/station-inventory")
def get_station_inventory_report():
    query = """
        WITH inventory_summary AS (
                    SELECT
                        m.station_id,
                        oi.product_id,
                        SUM(inv.stored_quantity) AS stored_quantity

                    FROM inventory inv

                    JOIN order_item oi
                        ON inv.order_item_id = oi.order_item_id

                    JOIN manifest m
                        ON inv.manifest_id = m.manifest_id

                    GROUP BY
                        m.station_id,
                        oi.product_id
                ),

                adjustment_summary AS (
                    SELECT
                        station_id,
                        product_id,

                        SUM(
                            CASE
                                WHEN quantity_adjusted > 0
                                THEN quantity_adjusted
                                ELSE 0
                            END
                        ) AS positive_adjustments,

                        SUM(
                            CASE
                                WHEN quantity_adjusted < 0
                                THEN ABS(quantity_adjusted)
                                ELSE 0
                            END
                        ) AS negative_adjustments,

                        SUM(quantity_adjusted) AS net_adjustment

                    FROM stock_adjustment

                    GROUP BY
                        station_id,
                        product_id
                )

                SELECT
                    ss.station_id,
                    ss.city AS station,
                    p.product_id,
                    p.product_name,

                    COALESCE(i.stored_quantity, 0)
                        AS stored_quantity,

                    COALESCE(a.positive_adjustments, 0)
                        AS positive_adjustments,

                    COALESCE(a.negative_adjustments, 0)
                        AS negative_adjustments,

                    COALESCE(a.net_adjustment, 0)
                        AS net_adjustment,

                    COALESCE(i.stored_quantity, 0)
                    +
                    COALESCE(a.net_adjustment, 0)
                        AS adjusted_stock

                FROM inventory_summary i

                JOIN station_store ss
                    ON i.station_id = ss.station_id

                JOIN product p
                    ON i.product_id = p.product_id

                LEFT JOIN adjustment_summary a
                    ON i.station_id = a.station_id
                    AND i.product_id = a.product_id

                ORDER BY
                    ss.city,
                    p.product_name
    """
    with get_db() as conn:
        with conn.cursor() as cursor:
            _execute_report_query(cursor, query)
            data = cursor.fetchall()
            return {"station_inventory": data}


@router.get("/customer-order-history")
def get_customer_order_history(limit: int = 100):
    """Report 6: Customer Order History with Delivery Details (Project 3 Management Report #6)."""
    query = """
        SELECT
            co.order_id,
            c.customer_name,
            co.order_date,
            co.delivery_date,
            co.status AS order_status,
            dr.route_name,
            tt.trip_id,
            tt.departure_datetime,
            tt.arrival_datetime,
            ra.allocated_quantity,
            ra.allocated_space,
            ra.allocated_at,
            m.status AS manifest_status,
            m.received_at,
            t.plate_number,
            d.delivery_status,
            d.delivered_at,
            d.proof_reference
        FROM customer_order co
        JOIN customer c
            ON co.customer_id = c.customer_id
        LEFT JOIN delivery_route dr
            ON co.delivery_route_id = dr.route_id
        LEFT JOIN order_item oi
            ON co.order_id = oi.order_id
        LEFT JOIN rail_allocation ra
            ON oi.order_item_id = ra.order_item_id
        LEFT JOIN train_trip tt
            ON ra.trip_id = tt.trip_id
        LEFT JOIN manifest m
            ON tt.trip_id = m.trip_id
        LEFT JOIN delivery d
            ON co.order_id = d.order_id
        LEFT JOIN roster_assignment r
            ON d.roster_id = r.roster_id
        LEFT JOIN truck t
            ON r.truck_id = t.truck_id
        ORDER BY
            co.order_id DESC,
            tt.departure_datetime
        LIMIT %s
    """
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, (limit,))
            rows = cursor.fetchall()
            for r in rows:
                for k in ["order_date", "delivery_date", "departure_datetime", "arrival_datetime", "allocated_at", "received_at", "delivered_at"]:
                    if r.get(k):
                        r[k] = str(r[k])
            return {"customer_order_history": rows}
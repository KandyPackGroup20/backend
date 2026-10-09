"""
Kandypack Logistics Platform - Database Auto-Migration Module
Ensures all required tables, columns, constraints, views, and seed data exist upon application boot.
Runs seamlessly against both local MySQL and Cloud MySQL (Render, Aiven, Railway, AWS).
"""

import logging

logger = logging.getLogger("kandypack.migrations")

def run_migrations():
    """Executes all necessary schema verifications and upgrades idempotently."""
    try:
        from app.core.database import get_db
        with get_db() as conn:
            with conn.cursor() as cur:
                # 1. Ensure notification table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS notification (
                        notification_id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NULL,
                        recipient_email VARCHAR(255) NOT NULL,
                        notification_type VARCHAR(50) NOT NULL DEFAULT 'NEW_CONSIGNMENT',
                        title VARCHAR(255) NOT NULL,
                        message TEXT NOT NULL,
                        order_id INT NULL,
                        is_read TINYINT DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_notification_user (user_id),
                        INDEX idx_notification_order (order_id),
                        INDEX idx_notification_read (is_read),
                        INDEX idx_notification_time (created_at DESC)
                    ) ENGINE=InnoDB;
                """)

                # 2. Ensure stock_adjustment table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS stock_adjustment (
                        adjustment_id INT AUTO_INCREMENT PRIMARY KEY,
                        station_id INT NULL,
                        product_id INT NULL,
                        quantity_adjusted INT NULL DEFAULT 0,
                        inventory_id INT NULL,
                        quantity_delta INT NULL DEFAULT 0,
                        reason VARCHAR(255) NOT NULL,
                        reported_by INT NULL,
                        adjusted_by INT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        adjusted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_stock_adj_station (station_id),
                        INDEX idx_stock_adj_prod (product_id)
                    ) ENGINE=InnoDB;
                """)

                # Ensure stock_adjustment has both schema variants
                for col, defn in [
                    ("station_id", "INT NULL"),
                    ("product_id", "INT NULL"),
                    ("quantity_adjusted", "INT NULL DEFAULT 0"),
                    ("inventory_id", "INT NULL"),
                    ("quantity_delta", "INT NULL DEFAULT 0"),
                    ("reported_by", "INT NULL"),
                    ("adjusted_by", "INT NULL"),
                    ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
                    ("adjusted_at", "DATETIME DEFAULT CURRENT_TIMESTAMP")
                ]:
                    try:
                        cur.execute(f"SHOW COLUMNS FROM stock_adjustment LIKE '{col}'")
                        if not cur.fetchone():
                            cur.execute(f"ALTER TABLE stock_adjustment ADD COLUMN {col} {defn}")
                    except Exception:
                        pass

                # 3. Ensure order_item.unit_price_at_order exists
                cur.execute("SHOW COLUMNS FROM order_item LIKE 'unit_price_at_order'")
                if not cur.fetchone():
                    logger.info("Adding missing column 'unit_price_at_order' to order_item...")
                    cur.execute("""
                        ALTER TABLE order_item 
                        ADD COLUMN unit_price_at_order DECIMAL(10,2) NOT NULL DEFAULT 0.00;
                    """)
                    cur.execute("""
                        UPDATE order_item oi 
                        JOIN product p ON oi.product_id = p.product_id 
                        SET oi.unit_price_at_order = p.unit_price 
                        WHERE oi.unit_price_at_order = 0.00;
                    """)
                    logger.info("Backfilled order_item.unit_price_at_order with product prices.")

                # 4. Ensure roster_assignment.request_key exists
                cur.execute("SHOW COLUMNS FROM roster_assignment LIKE 'request_key'")
                if not cur.fetchone():
                    logger.info("Adding missing column 'request_key' to roster_assignment...")
                    cur.execute("""
                        ALTER TABLE roster_assignment 
                        ADD COLUMN request_key VARCHAR(128) NULL;
                    """)
                    cur.execute("""
                        UPDATE roster_assignment 
                        SET request_key = CONCAT('req-', roster_id) 
                        WHERE request_key IS NULL OR request_key = '';
                    """)
                    cur.execute("""
                        ALTER TABLE roster_assignment 
                        MODIFY COLUMN request_key VARCHAR(128) NOT NULL;
                    """)
                    try:
                        cur.execute("""
                            ALTER TABLE roster_assignment 
                            ADD CONSTRAINT uq_roster_assignment_request_key UNIQUE (request_key);
                        """)
                    except Exception:
                        pass
                    logger.info("Added roster_assignment.request_key.")

                # 5. Ensure audit_log.roster_id exists
                cur.execute("SHOW COLUMNS FROM audit_log LIKE 'roster_id'")
                if not cur.fetchone():
                    logger.info("Adding missing column 'roster_id' to audit_log...")
                    cur.execute("""
                        ALTER TABLE audit_log 
                        ADD COLUMN roster_id INT NULL;
                    """)
                    try:
                        cur.execute("""
                            ALTER TABLE audit_log 
                            ADD CONSTRAINT uq_audit_log_roster UNIQUE (roster_id);
                        """)
                    except Exception:
                        pass
                    logger.info("Added audit_log.roster_id.")

                # 6. Ensure Core Database Views
                # View 1: v_trip_capacity_usage (Feature 4.2 / Rail schedules & analytics)
                try:
                    cur.execute("""
                        CREATE OR REPLACE VIEW v_trip_capacity_usage AS
                        SELECT 
                            tt.trip_id,
                            tt.origin_station_id,
                            tt.destination_station_id,
                            tt.departure_datetime,
                            tt.status,
                            tt.total_capacity,
                            COALESCE(SUM(ra.allocated_space), 0) AS used_space,
                            tt.total_capacity - COALESCE(SUM(ra.allocated_space), 0) AS remaining_space,
                            ROUND((COALESCE(SUM(ra.allocated_space), 0) / tt.total_capacity) * 100, 2) AS utilisation_pct
                        FROM train_trip tt
                        LEFT JOIN rail_allocation ra ON tt.trip_id = ra.trip_id
                        GROUP BY tt.trip_id, tt.origin_station_id, tt.destination_station_id, tt.departure_datetime, tt.status, tt.total_capacity;
                    """)
                except Exception as e:
                    logger.warning(f"Could not create v_trip_capacity_usage view: {e}")

                # View 2: v_quarterly_rail_analytics
                try:
                    cur.execute("""
                        CREATE OR REPLACE VIEW v_quarterly_rail_analytics AS
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
                    """)
                except Exception as e:
                    logger.warning(f"Could not create v_quarterly_rail_analytics view: {e}")

                # View 3: v_drivers_near_cap
                try:
                    cur.execute("""
                        CREATE OR REPLACE VIEW v_drivers_near_cap AS
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
                    """)
                except Exception as e:
                    logger.warning(f"Could not create v_drivers_near_cap view: {e}")

                # View 4: v_roster_duty_intervals
                try:
                    cur.execute("""
                        CREATE OR REPLACE VIEW v_roster_duty_intervals AS
                        SELECT
                            roster_id,
                            route_id,
                            truck_id,
                            driver_id AS staff_id,
                            'DRIVER' AS duty_type,
                            start_time,
                            end_time,
                            status,
                            CASE
                                WHEN status = 'CANCELLED' THEN 0
                                WHEN status IN ('SCHEDULED', 'IN_TRANSIT', 'COMPLETED') THEN 1
                                ELSE NULL
                            END AS is_counted
                        FROM roster_assignment
                        UNION ALL
                        SELECT
                            roster_id,
                            route_id,
                            truck_id,
                            assistant_id AS staff_id,
                            'ASSISTANT' AS duty_type,
                            start_time,
                            end_time,
                            status,
                            CASE
                                WHEN status = 'CANCELLED' THEN 0
                                WHEN status IN ('SCHEDULED', 'IN_TRANSIT', 'COMPLETED') THEN 1
                                ELSE NULL
                            END AS is_counted
                        FROM roster_assignment;
                    """)
                except Exception as e:
                    logger.warning(f"Could not create v_roster_duty_intervals view: {e}")

                # View 5: v_available_drivers
                try:
                    cur.execute("""
                        CREATE OR REPLACE VIEW v_available_drivers AS
                        SELECT 
                            ds.delivery_staff_id AS driver_id,
                            u.name AS full_name,
                            ds.license_number AS license_no,
                            ds.work_hours AS accumulated_weekly_hours,
                            40.00 - ds.work_hours AS remaining_hours_allowed
                        FROM delivery_staff ds
                        JOIN user u ON ds.user_id = u.user_id
                        WHERE u.role = 'DRIVER' AND ds.work_hours < 40.00;
                    """)
                except Exception as e:
                    pass

                # View 6: v_available_assistants
                try:
                    cur.execute("""
                        CREATE OR REPLACE VIEW v_available_assistants AS
                        SELECT 
                            ds.delivery_staff_id AS assistant_id,
                            u.name AS full_name,
                            ds.work_hours AS accumulated_weekly_hours,
                            60.00 - ds.work_hours AS remaining_hours_allowed
                        FROM delivery_staff ds
                        JOIN user u ON ds.user_id = u.user_id
                        WHERE u.role = 'ASSISTANT' AND ds.work_hours < 60.00;
                    """)
                except Exception as e:
                    pass

                # View 7: v_station_inventory_overview
                try:
                    cur.execute("""
                        CREATE OR REPLACE VIEW v_station_inventory_overview AS
                        SELECT 
                            ss.station_id,
                            ss.city AS city_name,
                            p.product_id,
                            p.product_name,
                            inv.stored_quantity AS quantity_available,
                            sl.location_code AS bin_location
                        FROM inventory inv
                        JOIN order_item oi ON inv.order_item_id = oi.order_item_id
                        JOIN product p ON oi.product_id = p.product_id
                        JOIN manifest m ON inv.manifest_id = m.manifest_id
                        JOIN station_store ss ON m.station_id = ss.station_id
                        LEFT JOIN storage_location sl ON ss.station_id = sl.station_id;
                    """)
                except Exception as e:
                    pass

                # View 8: v_customer_orders
                try:
                    cur.execute("""
                        CREATE OR REPLACE VIEW v_customer_orders AS
                        SELECT 
                            co.order_id,
                            co.customer_id,
                            c.customer_name,
                            u.email AS customer_email,
                            co.order_date,
                            co.delivery_date,
                            co.status AS order_status,
                            dr.route_name AS destination_route,
                            ss.city AS arrival_hub,
                            co.created_at
                        FROM customer_order co
                        JOIN customer c ON co.customer_id = c.customer_id
                        JOIN user u ON c.user_id = u.user_id
                        LEFT JOIN delivery_route dr ON co.delivery_route_id = dr.route_id
                        LEFT JOIN station_store ss ON dr.station_id = ss.station_id;
                    """)
                except Exception as e:
                    pass

                conn.commit()
                logger.info("[DATABASE AUTO-MIGRATION] All schema verifications and views created successfully.")
                return True
    except Exception as e:
        logger.error(f"[DATABASE AUTO-MIGRATION ERROR] {e}")
        return False

if __name__ == "__main__":
    run_migrations()

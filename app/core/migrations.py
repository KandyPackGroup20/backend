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

                # 2b. Ensure customer_order table has all consignment & routing columns
                cur.execute("SHOW TABLES LIKE 'customer_order'")
                if cur.fetchone():
                    for col, defn in [
                        ("delivery_route_id", "INT NULL"),
                        ("delivery_address", "VARCHAR(500) NULL"),
                        ("recipient_name", "VARCHAR(255) NULL"),
                        ("recipient_phone", "VARCHAR(30) NULL"),
                    ]:
                        try:
                            cur.execute(f"SHOW COLUMNS FROM customer_order LIKE '{col}'")
                            if not cur.fetchone():
                                logger.info(f"Adding missing column '{col}' to customer_order...")
                                cur.execute(f"ALTER TABLE customer_order ADD COLUMN {col} {defn}")
                                logger.info(f"Added column '{col}' to customer_order.")
                        except Exception as e:
                            logger.warning(f"Error checking/adding column {col} to customer_order: {e}")

                    # Backfill any existing NULL customer_order values from customer table or defaults
                    try:
                        cur.execute("""
                            UPDATE customer_order co
                            JOIN customer c ON co.customer_id = c.customer_id
                            SET 
                                co.delivery_route_id = COALESCE(co.delivery_route_id, c.route_id, 1),
                                co.delivery_address = COALESCE(co.delivery_address, c.address_line, 'No. 12, Galle Road, Colombo'),
                                co.recipient_name = COALESCE(co.recipient_name, c.customer_name, 'Valued Customer'),
                                co.recipient_phone = COALESCE(co.recipient_phone, c.phone, '0771234567')
                            WHERE co.delivery_route_id IS NULL OR co.delivery_address IS NULL OR co.recipient_name IS NULL OR co.recipient_phone IS NULL;
                        """)
                    except Exception as e:
                        logger.warning(f"Could not backfill customer_order columns: {e}")

                    # Add index on delivery_route_id if missing
                    try:
                        cur.execute("SHOW INDEX FROM customer_order WHERE Key_name = 'idx_customer_order_route'")
                        if not cur.fetchone():
                            cur.execute("ALTER TABLE customer_order ADD INDEX idx_customer_order_route (delivery_route_id)")
                    except Exception:
                        pass

                # 2c. Ensure product table has category, unit_weight_kg, description, image_url, is_active
                cur.execute("SHOW TABLES LIKE 'product'")
                if cur.fetchone():
                    for col, defn in [
                        ("category", "VARCHAR(100) NOT NULL DEFAULT 'Ceylon Tea & Spices'"),
                        ("unit_weight_kg", "DECIMAL(8,2) NOT NULL DEFAULT 25.00"),
                        ("description", "VARCHAR(500) NULL"),
                        ("image_url", "VARCHAR(500) NULL"),
                        ("is_active", "TINYINT DEFAULT 1"),
                    ]:
                        try:
                            cur.execute(f"SHOW COLUMNS FROM product LIKE '{col}'")
                            if not cur.fetchone():
                                logger.info(f"Adding missing column '{col}' to product...")
                                cur.execute(f"ALTER TABLE product ADD COLUMN {col} {defn}")
                                logger.info(f"Added column '{col}' to product.")
                        except Exception as e:
                            logger.warning(f"Error checking/adding column {col} to product: {e}")

                    # Seed catalogue items if empty
                    try:
                        cur.execute("SELECT COUNT(*) AS cnt FROM product")
                        cnt = cur.fetchone()["cnt"]
                        if cnt == 0:
                            products_to_seed = [
                                (1, 'Kandy Pure Ceylon BOPF Tea (25kg Crate)', 'Ceylon Tea & Spices', 4500.00, 25.00, 0.0500, 'High-grown export grade Ceylon Black BOPF tea packed in moisture-resistant foil-lined wooden crates.', '/products/tea_crate.jpg'),
                                (2, 'Ceylon Spices & Cinnamon Sack (20kg)', 'Ceylon Tea & Spices', 3800.00, 20.00, 0.0400, 'Sun-cured Ceylon alba cinnamon sticks, premium cardamom pods, and organic cloves in heavy-duty jute sacks.', '/products/spices_sack.jpg'),
                                (3, 'Nuwara Eliya Highland Vegetables Crate (30kg)', 'Fresh Produce & FMCG', 2600.00, 30.00, 0.0800, 'Ventilated farm-fresh crates of premium highland carrots, leeks, bell peppers, and cabbage for rapid rail transit.', '/products/produce_crates.jpg'),
                                (4, 'Ceylon Virgin Coconut Oil Canister (20L / 18kg)', 'Fresh Produce & FMCG', 4200.00, 18.00, 0.0450, 'Cold-pressed extra-virgin coconut oil in food-grade sealed HDPE transit containers.', '/products/coconut_oil.jpg'),
                                (5, 'Kandy Handloom Cotton Textile Bolts (25kg)', 'Garments & Textiles', 5200.00, 25.00, 0.0600, 'Protective shrink-wrapped bolts of traditional Sri Lankan batik and handloom cotton textiles for commercial retail.', '/products/textile_rolls.jpg'),
                                (6, 'Apparel & Garment Export Cartons (20kg)', 'Garments & Textiles', 4800.00, 20.00, 0.0550, 'Triple-wall corrugated export master cartons of finished garments with security straps and barcoded tags.', '/products/garments_box.jpg'),
                                (7, 'Traditional Brassware & Metal Crafts Crate (35kg)', 'Hardware & Industrial', 7500.00, 35.00, 0.0700, 'Handcrafted polished brass oil lamps, brassware, and cultural souvenirs cushioned in protective wooden crates.', '/products/brassware_crate.jpg'),
                                (8, 'Precision Industrial Machinery Spares (40kg)', 'Hardware & Industrial', 8900.00, 40.00, 0.0850, 'High-grade steel gears, shafts, and mechanical components packed in shock-absorbing foam-lined transport cases.', '/products/machinery_parts.jpg'),
                            ]
                            for pid, pname, cat, price, weight, space, desc, img in products_to_seed:
                                cur.execute("""
                                    INSERT INTO product (product_id, product_name, category, unit_price, unit_weight_kg, space_consumption_rate, description, image_url, is_active)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
                                """, (pid, pname, cat, price, weight, space, desc, img))
                        else:
                            cur.execute("""
                                UPDATE product SET
                                    category = CASE 
                                        WHEN product_id IN (1, 2) THEN 'Ceylon Tea & Spices'
                                        WHEN product_id IN (3, 4) THEN 'Fresh Produce & FMCG'
                                        WHEN product_id IN (5, 6) THEN 'Garments & Textiles'
                                        WHEN product_id IN (7, 8) THEN 'Hardware & Industrial'
                                        ELSE COALESCE(category, 'Ceylon Tea & Spices')
                                    END,
                                    unit_weight_kg = COALESCE(unit_weight_kg, 25.00)
                                WHERE category IS NULL OR category = '' OR unit_weight_kg IS NULL OR unit_weight_kg = 0;
                            """)
                    except Exception as e:
                        logger.warning(f"Could not backfill or seed product table: {e}")

                # 2d. Ensure station_store has manager_id
                cur.execute("SHOW TABLES LIKE 'station_store'")
                if cur.fetchone():
                    try:
                        cur.execute("SHOW COLUMNS FROM station_store LIKE 'manager_id'")
                        if not cur.fetchone():
                            cur.execute("ALTER TABLE station_store ADD COLUMN manager_id INT NULL")
                    except Exception:
                        pass

                # 2e. Ensure user has force_password_reset
                cur.execute("SHOW TABLES LIKE 'user'")
                if cur.fetchone():
                    try:
                        cur.execute("SHOW COLUMNS FROM user LIKE 'force_password_reset'")
                        if not cur.fetchone():
                            cur.execute("ALTER TABLE user ADD COLUMN force_password_reset TINYINT DEFAULT 0")
                    except Exception:
                        pass

                # 2f. Ensure customer table has required columns
                cur.execute("SHOW TABLES LIKE 'customer'")
                if cur.fetchone():
                    for col, defn in [
                        ("route_id", "INT NULL"),
                        ("phone", "VARCHAR(30) NULL DEFAULT '0771234567'"),
                        ("address_line", "VARCHAR(500) NULL DEFAULT 'Delivery Address'"),
                        ("city", "VARCHAR(100) NULL DEFAULT 'Colombo'"),
                        ("postal_code", "VARCHAR(20) NULL DEFAULT '00100'"),
                    ]:
                        try:
                            cur.execute(f"SHOW COLUMNS FROM customer LIKE '{col}'")
                            if not cur.fetchone():
                                cur.execute(f"ALTER TABLE customer ADD COLUMN {col} {defn}")
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

                # 5b. Ensure all active stations have delivery routes
                try:
                    routes_to_seed = [
                        (1, 'Colombo Central Commercial Route', '04:30:00'),
                        (1, 'Greater Colombo Industrial Hub Route', '06:00:00'),
                        (2, 'Negombo Coastal & Industrial Route', '04:00:00'),
                        (3, 'Galle Coastal Route', '05:00:00'),
                        (4, 'Matara Southern Express Route', '04:30:00'),
                        (5, 'Jaffna Northern Peninsula Route', '05:00:00'),
                        (6, 'Trincomalee Eastern Port Route', '04:30:00'),
                    ]
                    for st_id, r_name, max_t in routes_to_seed:
                        cur.execute("SELECT route_id FROM delivery_route WHERE station_id = %s AND route_name = %s", (st_id, r_name))
                        if not cur.fetchone():
                            cur.execute("INSERT INTO delivery_route (station_id, route_name, max_delivery_time) VALUES (%s, %s, %s)", (st_id, r_name, max_t))
                except Exception as e:
                    logger.warning(f"Could not seed delivery routes: {e}")

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

                # 7. Ensure All Station Store Managers exist
                try:
                    default_pw_hash = '$2b$12$vZdokKakUJDbss6pql2eouY6R71UscldmnVEiJTCPfhOrL3DpEa6e'
                    store_mgrs = [
                        (4, 'Sunil Colombo Store Mgr', 'store.colombo@kandypack.lk', 1),
                        (13, 'Roshan Negombo Store Mgr', 'store.negombo@kandypack.lk', 2),
                        (14, 'Chaminda Galle Store Mgr', 'store.galle@kandypack.lk', 3),
                        (15, 'Ishara Matara Store Mgr', 'store.matara@kandypack.lk', 4),
                        (16, 'Vithursan Jaffna Store Mgr', 'store.jaffna@kandypack.lk', 5),
                        (17, 'Nadeesha Trinco Store Mgr', 'store.trinco@kandypack.lk', 6),
                        (18, 'Ajith Kandy Store Mgr', 'store.kandy@kandypack.lk', 7),
                    ]
                    for uid, name, email, st_id in store_mgrs:
                        cur.execute("SELECT user_id FROM user WHERE email = %s", (email,))
                        existing_u = cur.fetchone()
                        if not existing_u:
                            cur.execute(
                                """INSERT INTO user (user_id, name, role, email, password_hash, is_active, force_password_reset)
                                   VALUES (%s, %s, 'STORE_MGR', %s, %s, 1, 0)
                                   ON DUPLICATE KEY UPDATE name=VALUES(name), role=VALUES(role), is_active=1""",
                                (uid, name, email, default_pw_hash)
                            )
                            target_uid = uid
                        else:
                            target_uid = existing_u["user_id"]

                        # Link to station_store
                        cur.execute("UPDATE station_store SET manager_id = %s WHERE station_id = %s", (target_uid, st_id))
                    logger.info("[DATABASE AUTO-MIGRATION] Station store managers verified and linked.")
                except Exception as e:
                    logger.warning(f"[DATABASE AUTO-MIGRATION] Warning seeding store managers: {e}")

                # 8. Ensure Stored Functions
                try:
                    cur.execute("DROP FUNCTION IF EXISTS fn_order_item_space")
                    cur.execute("""
                        CREATE FUNCTION fn_order_item_space(p_order_item_id INT, p_quantity INT)
                        RETURNS DECIMAL(10,2)
                        READS SQL DATA
                        BEGIN
                          DECLARE v_rate DECIMAL(6,4);
                          DECLARE v_qty  INT;

                          SELECT p.space_consumption_rate, COALESCE(p_quantity, oi.quantity)
                            INTO v_rate, v_qty
                            FROM order_item oi
                            JOIN product p ON p.product_id = oi.product_id
                           WHERE oi.order_item_id = p_order_item_id;

                          IF v_rate IS NULL THEN
                            RETURN NULL;
                          END IF;

                          RETURN CEILING(v_qty * v_rate * 100) / 100;
                        END
                    """)

                    cur.execute("DROP FUNCTION IF EXISTS fn_trip_remaining_capacity")
                    cur.execute("""
                        CREATE FUNCTION fn_trip_remaining_capacity(p_trip_id INT)
                        RETURNS DECIMAL(10,2)
                        READS SQL DATA
                        BEGIN
                          DECLARE v_cap  DECIMAL(10,2);
                          DECLARE v_used DECIMAL(10,2);

                          SELECT total_capacity INTO v_cap FROM train_trip WHERE trip_id = p_trip_id;
                          IF v_cap IS NULL THEN
                            RETURN NULL;
                          END IF;

                          SELECT COALESCE(SUM(allocated_space), 0) INTO v_used
                            FROM rail_allocation WHERE trip_id = p_trip_id;

                          RETURN v_cap - v_used;
                        END
                    """)
                    logger.info("[DATABASE AUTO-MIGRATION] Stored functions created.")
                except Exception as e:
                    logger.warning(f"[DATABASE AUTO-MIGRATION] Warning creating stored functions: {e}")

                # 9. Ensure Stored Procedures
                try:
                    # 9a. sp_schedule_train_order
                    cur.execute("DROP PROCEDURE IF EXISTS sp_schedule_train_order")
                    cur.execute("""
                        CREATE PROCEDURE sp_schedule_train_order(
                          IN  p_order_id INT,
                          IN  p_user_id  INT,
                          OUT p_result   VARCHAR(50)
                        )
                        proc_body: BEGIN
                          DECLARE v_status        VARCHAR(50);
                          DECLARE v_delivery_date DATE;
                          DECLARE v_dest_station  INT;
                          DECLARE v_kandy_station INT;
                          DECLARE v_item_id       INT DEFAULT 0;
                          DECLARE v_next_item     INT;
                          DECLARE v_item_qty      INT;
                          DECLARE v_rate          DECIMAL(6,4);
                          DECLARE v_remaining     INT;
                          DECLARE v_last_dep      DATETIME;
                          DECLARE v_last_trip     INT;
                          DECLARE v_trip_id       INT;
                          DECLARE v_trip_dep      DATETIME;
                          DECLARE v_trip_cap      DECIMAL(10,2);
                          DECLARE v_used          DECIMAL(10,2);
                          DECLARE v_fit           INT;
                          DECLARE v_alloc_qty     INT;
                          DECLARE v_trip_count    INT DEFAULT 0;
                          DECLARE v_any_item      INT DEFAULT 0;

                          DECLARE EXIT HANDLER FOR 1213, 1205
                          BEGIN
                            ROLLBACK;
                            SET p_result = 'DEADLOCK_RETRY';
                          END;

                          DECLARE EXIT HANDLER FOR SQLEXCEPTION
                          BEGIN
                            ROLLBACK;
                            SET p_result = 'ERROR_TRANSACTION_FAILED';
                            IF p_user_id IS NOT NULL THEN
                              INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                              VALUES (p_user_id, 'SCHEDULE_RAIL_ORDER', p_order_id, 'ERROR_TRANSACTION_FAILED', 'customer_order');
                              COMMIT;
                            END IF;
                          END;

                          START TRANSACTION;

                          SET v_status = NULL;
                          SELECT status, delivery_date INTO v_status, v_delivery_date
                            FROM customer_order WHERE order_id = p_order_id FOR UPDATE;

                          IF v_status IS NULL THEN
                            ROLLBACK; SET p_result = 'ORDER_NOT_FOUND';
                            IF p_user_id IS NOT NULL THEN
                              INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                              VALUES (p_user_id, 'SCHEDULE_RAIL_ORDER', p_order_id, p_result, 'customer_order');
                              COMMIT;
                            END IF;
                            LEAVE proc_body;
                          END IF;

                          IF v_status <> 'PENDING_RAIL_SCHEDULING' THEN
                            ROLLBACK; SET p_result = 'INVALID_ORDER_STATUS';
                            IF p_user_id IS NOT NULL THEN
                              INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                              VALUES (p_user_id, 'SCHEDULE_RAIL_ORDER', p_order_id, p_result, 'customer_order');
                              COMMIT;
                            END IF;
                            LEAVE proc_body;
                          END IF;

                          SET v_dest_station = NULL;
                          SELECT dr.station_id INTO v_dest_station
                            FROM customer_order co
                            JOIN customer c        ON c.customer_id = co.customer_id
                            JOIN delivery_route dr ON dr.route_id   = co.delivery_route_id
                            JOIN station_store dest ON dest.station_id = dr.station_id AND dest.is_active = 1
                              AND dest.city IN ('Colombo','Negombo','Galle','Matara','Jaffna','Trincomalee')
                           WHERE co.order_id = p_order_id;

                          IF v_dest_station IS NULL THEN
                            ROLLBACK; SET p_result = 'DESTINATION_HUB_NOT_RESOLVED';
                            IF p_user_id IS NOT NULL THEN
                              INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                              VALUES (p_user_id, 'SCHEDULE_RAIL_ORDER', p_order_id, p_result, 'customer_order');
                              COMMIT;
                            END IF;
                            LEAVE proc_body;
                          END IF;

                          SET v_kandy_station = NULL;
                          SELECT station_id INTO v_kandy_station FROM station_store WHERE city = 'Kandy' AND is_active = 1 ORDER BY station_id LIMIT 1;
                          IF v_kandy_station IS NULL THEN
                            ROLLBACK; SET p_result = 'ORIGIN_HUB_NOT_FOUND';
                            IF p_user_id IS NOT NULL THEN
                              INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                              VALUES (p_user_id, 'SCHEDULE_RAIL_ORDER', p_order_id, p_result, 'customer_order');
                              COMMIT;
                            END IF;
                            LEAVE proc_body;
                          END IF;

                          item_loop: LOOP
                            SET v_next_item = NULL;
                            SELECT MIN(order_item_id) INTO v_next_item
                              FROM order_item WHERE order_id = p_order_id AND order_item_id > v_item_id;

                            IF v_next_item IS NULL THEN
                              LEAVE item_loop;
                            END IF;

                            SET v_item_id  = v_next_item;
                            SET v_any_item = 1;

                            SELECT oi.quantity, p.space_consumption_rate INTO v_item_qty, v_rate
                              FROM order_item oi JOIN product p ON p.product_id = oi.product_id
                             WHERE oi.order_item_id = v_item_id;

                            IF v_item_qty <= 0 THEN
                              ROLLBACK; SET p_result = 'INVALID_ORDER_QUANTITY';
                              LEAVE proc_body;
                            END IF;

                            IF v_rate IS NULL OR v_rate <= 0 THEN
                              ROLLBACK; SET p_result = 'INVALID_PRODUCT_SPACE_RATE';
                              IF p_user_id IS NOT NULL THEN
                                INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                                VALUES (p_user_id, 'SCHEDULE_RAIL_ORDER', p_order_id, p_result, 'customer_order');
                                COMMIT;
                              END IF;
                              LEAVE proc_body;
                            END IF;

                            SET v_remaining = v_item_qty;
                            SET v_last_dep  = '1000-01-01 00:00:00';
                            SET v_last_trip = 0;

                            trip_loop: WHILE v_remaining > 0 DO
                              SET v_trip_id = NULL;

                              SELECT tt.trip_id, tt.departure_datetime, tt.total_capacity
                                INTO v_trip_id, v_trip_dep, v_trip_cap
                                FROM train_trip tt
                               WHERE tt.origin_station_id = v_kandy_station
                                 AND tt.destination_station_id = v_dest_station
                                 AND tt.status = 'SCHEDULED'
                                 AND NOT EXISTS (SELECT 1 FROM manifest m WHERE m.trip_id=tt.trip_id AND m.status='RECEIVED')
                                 AND tt.departure_datetime > CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', '+05:30')
                                 AND tt.arrival_datetime < v_delivery_date + INTERVAL 1 DAY
                                 AND (tt.departure_datetime > v_last_dep
                                      OR (tt.departure_datetime = v_last_dep AND tt.trip_id > v_last_trip))
                               ORDER BY tt.departure_datetime, tt.trip_id
                               LIMIT 1
                               FOR UPDATE;

                              IF v_trip_id IS NULL THEN
                                LEAVE trip_loop;
                              END IF;

                              SET v_last_dep  = v_trip_dep;
                              SET v_last_trip = v_trip_id;

                              SELECT COALESCE(SUM(allocated_space), 0) INTO v_used
                                FROM rail_allocation WHERE trip_id = v_trip_id FOR UPDATE;

                              SET v_fit = FLOOR((v_trip_cap - v_used) / v_rate);

                              IF v_fit > 0 THEN
                                SET v_alloc_qty = LEAST(v_fit, v_remaining);

                                INSERT INTO rail_allocation
                                       (order_item_id, trip_id, allocated_quantity, allocated_space, allocated_by)
                                VALUES (v_item_id, v_trip_id, v_alloc_qty,
                                        fn_order_item_space(v_item_id, v_alloc_qty), p_user_id);

                                SET v_remaining = v_remaining - v_alloc_qty;
                              END IF;
                            END WHILE trip_loop;

                            IF v_remaining > 0 THEN
                              ROLLBACK; SET p_result = 'INSUFFICIENT_RAIL_CAPACITY';
                              IF p_user_id IS NOT NULL THEN
                                INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                                VALUES (p_user_id, 'SCHEDULE_RAIL_ORDER', p_order_id, p_result, 'customer_order');
                                COMMIT;
                              END IF;
                              LEAVE proc_body;
                            END IF;
                          END LOOP item_loop;

                          IF v_any_item = 0 THEN
                            ROLLBACK; SET p_result = 'ORDER_HAS_NO_ITEMS';
                            IF p_user_id IS NOT NULL THEN
                              INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                              VALUES (p_user_id, 'SCHEDULE_RAIL_ORDER', p_order_id, p_result, 'customer_order');
                              COMMIT;
                            END IF;
                            LEAVE proc_body;
                          END IF;

                          SELECT COUNT(DISTINCT ra.trip_id) INTO v_trip_count
                            FROM rail_allocation ra
                            JOIN order_item oi ON oi.order_item_id = ra.order_item_id
                           WHERE oi.order_id = p_order_id;

                          IF v_trip_count = 1 THEN
                            UPDATE customer_order SET status = 'SCHEDULED_FOR_RAIL' WHERE order_id = p_order_id;
                            INSERT INTO order_status_history (status, order_id, changed_by)
                            VALUES ('SCHEDULED_FOR_RAIL', p_order_id, p_user_id);
                            SET p_result = 'SUCCESS_SINGLE_TRIP';
                          ELSE
                            UPDATE customer_order SET status = 'SCHEDULED_MULTI_TRIP' WHERE order_id = p_order_id;
                            INSERT INTO order_status_history (status, order_id, changed_by)
                            VALUES ('SCHEDULED_MULTI_TRIP', p_order_id, p_user_id);
                            SET p_result = 'SUCCESS_MULTI_TRIP_SPILLOVER';
                          END IF;

                          IF p_user_id IS NOT NULL THEN
                            INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                            VALUES (p_user_id, 'SCHEDULE_RAIL_ORDER', p_order_id, 'SUCCESS', 'customer_order');
                          END IF;

                          COMMIT;
                        END
                    """)

                    # 9b. sp_reverse_rail_allocation
                    cur.execute("DROP PROCEDURE IF EXISTS sp_reverse_rail_allocation")
                    cur.execute("""
                        CREATE PROCEDURE sp_reverse_rail_allocation(
                            IN  p_order_id INT,
                            IN  p_user_id  INT,
                            OUT p_result   VARCHAR(50)
                        )
                        proc_body: BEGIN
                            DECLARE v_status VARCHAR(50);
                            DECLARE v_alloc_count INT;
                            DECLARE v_invalid_trip_count INT;
                            DECLARE done INT DEFAULT FALSE;

                            DECLARE v_audit_alloc_id INT;
                            DECLARE v_audit_trip_id INT;
                            DECLARE v_audit_qty INT;
                            DECLARE v_audit_space DECIMAL(10,2);

                            DECLARE cur_allocs CURSOR FOR
                                SELECT ra.allocation_id, ra.trip_id, ra.allocated_quantity, ra.allocated_space
                                FROM rail_allocation ra
                                JOIN order_item oi ON oi.order_item_id = ra.order_item_id
                                WHERE oi.order_id = p_order_id;

                            DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = TRUE;

                            DECLARE EXIT HANDLER FOR 1213, 1205
                            BEGIN
                                ROLLBACK;
                                SET p_result = 'DEADLOCK_RETRY';
                            END;

                            DECLARE EXIT HANDLER FOR SQLEXCEPTION
                            BEGIN
                                ROLLBACK;
                                SET p_result = 'ERROR_TRANSACTION_FAILED';
                                IF p_user_id IS NOT NULL THEN
                                    INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                                    VALUES (p_user_id, 'REVERSE_RAIL_ORDER', p_order_id, 'ERROR_TRANSACTION_FAILED', 'customer_order');
                                    COMMIT;
                                END IF;
                            END;

                            START TRANSACTION;

                            SET v_status = NULL;
                            SELECT status INTO v_status
                              FROM customer_order
                             WHERE order_id = p_order_id
                               FOR UPDATE;

                            IF v_status IS NULL THEN
                                ROLLBACK;
                                SET p_result = 'ORDER_NOT_FOUND';
                                IF p_user_id IS NOT NULL THEN
                                    INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                                    VALUES (p_user_id, 'REVERSE_RAIL_ORDER', p_order_id, p_result, 'customer_order');
                                    COMMIT;
                                END IF;
                                LEAVE proc_body;
                            END IF;

                            IF v_status NOT IN ('SCHEDULED_FOR_RAIL', 'SCHEDULED_MULTI_TRIP') THEN
                                ROLLBACK;
                                SET p_result = 'INVALID_ORDER_STATUS';
                                IF p_user_id IS NOT NULL THEN
                                    INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                                    VALUES (p_user_id, 'REVERSE_RAIL_ORDER', p_order_id, p_result, 'customer_order');
                                    COMMIT;
                                END IF;
                                LEAVE proc_body;
                            END IF;

                            SELECT COUNT(*) INTO v_alloc_count
                            FROM rail_allocation ra
                            JOIN order_item oi ON oi.order_item_id = ra.order_item_id
                            WHERE oi.order_id = p_order_id;

                            IF v_alloc_count = 0 THEN
                                ROLLBACK;
                                SET p_result = 'NO_ALLOCATIONS_FOUND';
                                IF p_user_id IS NOT NULL THEN
                                    INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                                    VALUES (p_user_id, 'REVERSE_RAIL_ORDER', p_order_id, p_result, 'customer_order');
                                    COMMIT;
                                END IF;
                                LEAVE proc_body;
                            END IF;

                            SELECT COUNT(DISTINCT tt.trip_id) INTO v_invalid_trip_count
                            FROM rail_allocation ra
                            JOIN order_item oi ON oi.order_item_id = ra.order_item_id
                            JOIN train_trip tt ON tt.trip_id = ra.trip_id
                            WHERE oi.order_id = p_order_id
                              AND (
                                  tt.status != 'SCHEDULED'
                                  OR tt.departure_datetime <= CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', '+05:30')
                                  OR EXISTS (SELECT 1 FROM manifest m WHERE m.trip_id = tt.trip_id AND m.status = 'RECEIVED')
                              );

                            IF v_invalid_trip_count > 0 THEN
                                ROLLBACK;
                                SET p_result = 'CANNOT_REVERSE_DEPARTED_OR_INACTIVE_TRIP';
                                IF p_user_id IS NOT NULL THEN
                                    INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                                    VALUES (p_user_id, 'REVERSE_RAIL_ORDER', p_order_id, p_result, 'customer_order');
                                    COMMIT;
                                END IF;
                                LEAVE proc_body;
                            END IF;

                            OPEN cur_allocs;
                            alloc_loop: LOOP
                                FETCH cur_allocs INTO v_audit_alloc_id, v_audit_trip_id, v_audit_qty, v_audit_space;
                                IF done THEN
                                    LEAVE alloc_loop;
                                END IF;

                                IF p_user_id IS NOT NULL THEN
                                    INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                                    VALUES (p_user_id, 'REVERSE_TRIP_ALLOCATION', v_audit_trip_id,
                                            CONCAT('RELEASED_QTY_', v_audit_qty, '_SPACE_', v_audit_space), 'rail_allocation');
                                END IF;
                            END LOOP alloc_loop;
                            CLOSE cur_allocs;

                            DELETE ra FROM rail_allocation ra
                            JOIN order_item oi ON oi.order_item_id = ra.order_item_id
                            WHERE oi.order_id = p_order_id;

                            UPDATE customer_order
                            SET status = 'PENDING_RAIL_SCHEDULING'
                            WHERE order_id = p_order_id;

                            INSERT INTO order_status_history (status, order_id, changed_by)
                            VALUES ('PENDING_RAIL_SCHEDULING', p_order_id, p_user_id);

                            IF p_user_id IS NOT NULL THEN
                                INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                                VALUES (p_user_id, 'REVERSE_RAIL_ORDER', p_order_id, 'SUCCESS', 'customer_order');
                            END IF;

                            COMMIT;
                            SET p_result = 'SUCCESS_REVERSED';
                        END
                    """)

                    # 9c. sp_receive_manifest
                    cur.execute("DROP PROCEDURE IF EXISTS sp_receive_manifest")
                    cur.execute("""
                        CREATE PROCEDURE sp_receive_manifest(
                            IN p_station_id INT,
                            IN p_trip_id INT,
                            IN p_user_id INT,
                            OUT p_result_code VARCHAR(50)
                        )
                        PROC_BODY: BEGIN
                            DECLARE v_manifest_id INT;
                            DECLARE v_manifest_status VARCHAR(50);

                            DECLARE EXIT HANDLER FOR SQLEXCEPTION
                            BEGIN
                                ROLLBACK;
                                SET p_result_code = 'ERROR_TRANSACTION_FAILED';
                            END;

                            START TRANSACTION;

                            SELECT manifest_id, status INTO v_manifest_id, v_manifest_status
                            FROM manifest
                            WHERE station_id = p_station_id AND trip_id = p_trip_id
                            FOR UPDATE;

                            IF v_manifest_id IS NULL THEN
                                ROLLBACK;
                                SET p_result_code = 'MANIFEST_NOT_FOUND';
                                LEAVE PROC_BODY;
                            END IF;

                            IF v_manifest_status <> 'PENDING' THEN
                                ROLLBACK;
                                SET p_result_code = 'MANIFEST_ALREADY_RECEIVED';
                                LEAVE PROC_BODY;
                            END IF;

                            add_stock: BEGIN
                                DECLARE v_product_id INT;
                                DECLARE v_qty INT;
                                DECLARE done INT DEFAULT FALSE;

                                DECLARE cur_items CURSOR FOR
                                    SELECT oi.product_id, ra.allocated_quantity
                                    FROM rail_allocation ra
                                    JOIN order_item oi ON ra.order_item_id = oi.order_item_id
                                    WHERE ra.trip_id = p_trip_id;
                                DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = TRUE;

                                OPEN cur_items;
                                item_loop: LOOP
                                    FETCH cur_items INTO v_product_id, v_qty;
                                    IF done THEN LEAVE item_loop; END IF;

                                    INSERT INTO inventory (station_id, product_id, stored_quantity)
                                    VALUES (p_station_id, v_product_id, v_qty)
                                    ON DUPLICATE KEY UPDATE stored_quantity = stored_quantity + v_qty;

                                END LOOP;
                                CLOSE cur_items;
                            END add_stock;

                            UPDATE manifest
                            SET status = 'RECEIVED', received_at = NOW()
                            WHERE manifest_id = v_manifest_id;

                            advance_orders: BEGIN
                                DECLARE v_order_id INT;
                                DECLARE v_remaining_trips INT;
                                DECLARE done2 INT DEFAULT FALSE;

                                DECLARE cur_orders CURSOR FOR
                                    SELECT DISTINCT oi.order_id
                                    FROM rail_allocation ra
                                    JOIN order_item oi ON ra.order_item_id = oi.order_item_id
                                    WHERE ra.trip_id = p_trip_id;
                                DECLARE CONTINUE HANDLER FOR NOT FOUND SET done2 = TRUE;

                                OPEN cur_orders;
                                order_loop: LOOP
                                    FETCH cur_orders INTO v_order_id;
                                    IF done2 THEN LEAVE order_loop; END IF;

                                    SELECT COUNT(*) INTO v_remaining_trips
                                    FROM rail_allocation ra2
                                    JOIN order_item oi2 ON ra2.order_item_id = oi2.order_item_id
                                    JOIN train_trip tt2 ON ra2.trip_id = tt2.trip_id
                                    LEFT JOIN manifest m2 ON m2.trip_id = tt2.trip_id AND m2.station_id = tt2.destination_station_id
                                    WHERE oi2.order_id = v_order_id
                                      AND (m2.status IS NULL OR m2.status <> 'RECEIVED');

                                    IF v_remaining_trips = 0 THEN
                                        UPDATE customer_order
                                        SET status = 'ARRIVED_AT_STATION_STORE'
                                        WHERE order_id = v_order_id;

                                        INSERT INTO order_status_history (status, order_id, changed_by)
                                        VALUES ('ARRIVED_AT_STATION_STORE', v_order_id, p_user_id);
                                    END IF;

                                END LOOP;
                                CLOSE cur_orders;
                            END advance_orders;

                            COMMIT;
                            SET p_result_code = 'SUCCESS';
                        END
                    """)
                    logger.info("[DATABASE AUTO-MIGRATION] Stored procedures created.")
                except Exception as e:
                    logger.warning(f"[DATABASE AUTO-MIGRATION] Warning creating stored procedures: {e}")

                # 10. Ensure Manifest Trigger
                try:
                    cur.execute("DROP TRIGGER IF EXISTS trg_rail_alloc_manifest_ai")
                    cur.execute("""
                        CREATE TRIGGER trg_rail_alloc_manifest_ai AFTER INSERT ON rail_allocation
                        FOR EACH ROW
                        BEGIN
                          INSERT INTO manifest (station_id, trip_id, status)
                          SELECT destination_station_id, trip_id, 'PENDING' FROM train_trip WHERE trip_id=NEW.trip_id
                          ON DUPLICATE KEY UPDATE manifest_id=manifest_id;
                        END
                    """)
                    logger.info("[DATABASE AUTO-MIGRATION] trg_rail_alloc_manifest_ai trigger created.")
                except Exception as e:
                    logger.warning(f"[DATABASE AUTO-MIGRATION] Warning creating trigger: {e}")

                conn.commit()
                logger.info("[DATABASE AUTO-MIGRATION] All schema verifications and views created successfully.")
                return True
    except Exception as e:
        logger.error(f"[DATABASE AUTO-MIGRATION ERROR] {e}")
        return False

if __name__ == "__main__":
    run_migrations()

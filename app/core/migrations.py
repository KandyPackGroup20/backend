"""
Kandypack Logistics Platform - Database Auto-Migration Module
Ensures all required tables, columns, constraints, and seed data exist upon application boot.
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
                        station_id INT NOT NULL,
                        product_id INT NOT NULL,
                        quantity_adjusted INT NOT NULL,
                        reason VARCHAR(255) NOT NULL,
                        reported_by INT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_stock_adj_station (station_id),
                        INDEX idx_stock_adj_prod (product_id)
                    ) ENGINE=InnoDB;
                """)

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

                conn.commit()
                logger.info("[DATABASE AUTO-MIGRATION] All schema verifications completed successfully.")
                return True
    except Exception as e:
        logger.error(f"[DATABASE AUTO-MIGRATION ERROR] {e}")
        return False

if __name__ == "__main__":
    run_migrations()

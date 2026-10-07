import pymysql
from app.core.database import get_db

def run():
    with get_db() as conn:
        with conn.cursor() as c:
            # 1. Create notification table if it does not exist
            c.execute("""
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
                    INDEX idx_notif_user (user_id),
                    INDEX idx_notif_is_read (is_read),
                    INDEX idx_notif_created (created_at DESC),
                    CONSTRAINT fk_notif_user FOREIGN KEY (user_id) REFERENCES user(user_id) ON DELETE CASCADE,
                    CONSTRAINT fk_notif_order FOREIGN KEY (order_id) REFERENCES customer_order(order_id) ON DELETE CASCADE
                ) ENGINE=InnoDB;
            """)
            conn.commit()
            print("notification table verified/created.")

            # 2. Check if we need to seed historic alerts for existing orders
            c.execute("SELECT COUNT(*) AS cnt FROM notification")
            count = c.fetchone()["cnt"]

            if count == 0:
                print("Seeding initial alerts from customer_order records...")
                c.execute("""
                    SELECT co.order_id, co.order_date, co.created_at, co.status,
                           c.customer_name, c.city,
                           COALESCE((SELECT GROUP_CONCAT(CONCAT(p.product_name, ' (x', oi.quantity, ')') SEPARATOR ', ')
                                     FROM order_item oi JOIN product p ON oi.product_id = p.product_id
                                     WHERE oi.order_id = co.order_id), 'Ceylon Tea & Spices') AS items_desc,
                           COALESCE((SELECT ROUND(SUM(oi.quantity * p.unit_weight_kg), 1)
                                     FROM order_item oi JOIN product p ON oi.product_id = p.product_id
                                     WHERE oi.order_id = co.order_id), 60.0) AS total_weight
                    FROM customer_order co
                    JOIN customer c ON co.customer_id = c.customer_id
                    ORDER BY co.order_id ASC
                """)
                orders = c.fetchall()

                # Find Logistics Manager user ID
                c.execute("SELECT user_id, email FROM user WHERE role = 'LOGISTICS_MGR' LIMIT 1")
                log_mgr = c.fetchone()
                mgr_id = log_mgr["user_id"] if log_mgr else 2
                mgr_email = log_mgr["email"] if log_mgr else "logistics@kandypack.lk"

                for idx, o in enumerate(orders):
                    is_read_val = 1 if idx < len(orders) - 2 else 0
                    city = o["city"] or "Colombo"
                    title = f"New Consignment KP-{o['order_id']:05d} Awaiting Rail Scheduling"
                    msg = (
                        f"Customer {o['customer_name']} placed a consignment to {city} Goods Shed. "
                        f"Cargo: {o['items_desc']}. Total Weight: {o['total_weight']} kg. "
                        f"Status: {o['status']}."
                    )
                    c.execute("""
                        INSERT INTO notification (user_id, recipient_email, notification_type, title, message, order_id, is_read, created_at)
                        VALUES (%s, %s, 'NEW_CONSIGNMENT', %s, %s, %s, %s, %s)
                    """, (mgr_id, mgr_email, title, msg, o["order_id"], is_read_val, o["created_at"]))
                conn.commit()
                print(f"Seeded {len(orders)} alerts for logistics manager.")

            c.execute("SELECT COUNT(*) AS cnt, SUM(CASE WHEN is_read=0 THEN 1 ELSE 0 END) AS unread FROM notification")
            stats = c.fetchone()
            print("Notification stats in DB:", stats)

if __name__ == "__main__":
    run()

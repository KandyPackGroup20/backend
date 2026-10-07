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

SHOW TABLES LIKE 'notification';

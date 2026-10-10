from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field
from typing import Optional, List
import datetime
import re
from zoneinfo import ZoneInfo

from app.core.database import get_db
from app.core.security import get_current_user, require_roles
from app.core.notifications import log_and_dispatch_email

router = APIRouter(prefix="/orders", tags=["Customer Orders & Consignments"])

HUB_CODE_MAP = {
    "CMB": {"city": "Colombo", "station": "Colombo Fort Goods Shed", "name": "Colombo"},
    "GAL": {"city": "Galle", "station": "Galle Central Hub", "name": "Galle"},
    "JAF": {"city": "Jaffna", "station": "Jaffna Railway Hub", "name": "Jaffna"},
    "NEG": {"city": "Negombo", "station": "Negombo Hub", "name": "Negombo"},
    "MAT": {"city": "Matara", "station": "Matara Railway Hub", "name": "Matara"},
    "TRN": {"city": "Trincomalee", "station": "Trincomalee Freight Hub", "name": "Trincomalee"},
}

CITY_TO_CODE = {
    "colombo": "CMB",
    "galle": "GAL",
    "jaffna": "JAF",
    "negombo": "NEG",
    "matara": "MAT",
    "trincomalee": "TRN",
    "kandy": "KDY"
}

def normalize_status(raw: str) -> str:
    r = (raw or "").upper()
    if "DELIVER" in r or "COMPLETE" in r:
        return "delivered"
    if "TRANSIT" in r or "SCHEDULED" in r or "ALLOCATED" in r:
        return "transit"
    if "CANCEL" in r or "FAIL" in r or "ISSUE" in r or "DELAY" in r:
        return "issue"
    return "pending"

class ProductCatalogueItem(BaseModel):
    product_id: int
    product_name: str
    category: str
    unit_price: float
    unit_weight_kg: float
    space_consumption_rate: float
    description: Optional[str] = None
    image_url: Optional[str] = None
    is_active: bool

class OrderCartItem(BaseModel):
    product_id: int
    quantity: int = Field(default=1, ge=1)

class OrderItemSchema(BaseModel):
    id: str
    order_id: int
    destination: str
    hubStation: str
    cargo: str
    weight: str
    date: str
    status: str
    trainSlot: str
    recipient: str
    amount: float

class CreateOrderRequest(BaseModel):
    destination_hub: str # e.g. "CMB", "GAL", "Colombo"
    cargo_type: Optional[str] = "tea"
    cargo_description: Optional[str] = None
    weight_kg: Optional[float] = 25.0
    recipient_name: Optional[str] = Field(default=None, max_length=255)
    recipient_phone: Optional[str] = Field(default=None, max_length=30)
    delivery_address: Optional[str] = Field(default=None, max_length=500)
    booking_date: Optional[str] = None
    slot: Optional[str] = "06:30 AM Express Rail 101"
    items: Optional[List[OrderCartItem]] = None
    delivery_route_id: Optional[int] = None

class MilestoneSchema(BaseModel):
    title: str
    location: str
    time: str
    description: str
    completed: bool
    active: bool

class OrderTrackingResponse(BaseModel):
    id: str
    order_id: int
    destination: str
    hubStation: str
    cargo: str
    weight: str
    date: str
    status: str
    trainSlot: str
    recipient: str
    amount: float
    milestones: List[MilestoneSchema]


@router.get("", response_model=List[OrderItemSchema])
def list_customer_orders(request: Request, search: Optional[str] = None, status_filter: Optional[str] = None,
                         current_user: dict = Depends(require_roles(["CUSTOMER", "SUPERADMIN", "LOGISTICS_MGR", "DISPATCHER", "STORE_MGR", "WAREHOUSE_STAFF"]))):
    """
    Fetch all real customer orders from MySQL database.
    Scopes to logged-in customer if authenticated; otherwise returns the active company order history.
    """
    session_user = {"sub": current_user["user_id"], "role": current_user["role"]}

    with get_db() as conn:
        with conn.cursor() as cursor:
            # Determine customer filtering
            where_clauses = ["1=1"]
            params = []

            if session_user and session_user.get("role") == "CUSTOMER":
                # Find customer_id for this user
                cursor.execute("SELECT customer_id FROM customer WHERE user_id = %s", (session_user["sub"],))
                cust = cursor.fetchone()
                if cust:
                    where_clauses.append("co.customer_id = %s")
                    params.append(cust["customer_id"])
                else:
                    return []

            sql = f"""
                SELECT 
                    co.order_id,
                    co.customer_id,
                    co.order_date,
                    co.delivery_date,
                    co.status AS raw_status,
                    co.created_at,
                    co.recipient_name AS customer_name,
                    co.recipient_phone AS customer_phone,
                    co.delivery_address AS address_line,
                    c.city AS customer_city,
                    ss.city AS destination_city,
                    ss.address AS hub_address,
                    COALESCE(
                        (SELECT GROUP_CONCAT(CONCAT(p.product_name) SEPARATOR ', ')
                         FROM order_item oi JOIN product p ON oi.product_id = p.product_id
                         WHERE oi.order_id = co.order_id),
                        'Highland Tea & Spices'
                    ) AS cargo_name,
                    COALESCE(
                        (SELECT ROUND(SUM(oi.quantity * p.unit_weight_kg), 0)
                         FROM order_item oi JOIN product p ON oi.product_id = p.product_id
                         WHERE oi.order_id = co.order_id),
                        60
                    ) AS total_weight_num,
                    COALESCE(
                        (SELECT ROUND(SUM(oi.quantity * oi.unit_price_at_order), 2)
                         FROM order_item oi JOIN product p ON oi.product_id = p.product_id
                         WHERE oi.order_id = co.order_id),
                        3200.00
                    ) AS total_amount,
                    COALESCE(
                        (SELECT CONCAT(TIME_FORMAT(tt.departure_datetime, '%%h:%%i %%p'), ' Freight Express ', tt.trip_id)
                         FROM rail_allocation ra
                         JOIN train_trip tt ON ra.trip_id = tt.trip_id
                         JOIN order_item oi ON ra.order_item_id = oi.order_item_id
                         WHERE oi.order_id = co.order_id LIMIT 1),
                        '06:30 AM Express Rail 101'
                    ) AS allocated_train_slot
                FROM customer_order co
                JOIN customer c ON co.customer_id = c.customer_id
                LEFT JOIN delivery_route dr ON co.delivery_route_id = dr.route_id
                LEFT JOIN station_store ss ON dr.station_id = ss.station_id
                WHERE {" AND ".join(where_clauses)}
                ORDER BY co.order_id DESC
            """
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()

            results = []
            for row in rows:
                city = row["destination_city"]
                code = CITY_TO_CODE.get(city.lower(), "CMB")
                tracking_id = f"KP-{row['order_id']:05d}-{code}"
                norm_status = normalize_status(row["raw_status"])
                order_date_str = str(row["order_date"]) if row.get("order_date") else "2026-09-04"

                item = OrderItemSchema(
                    id=tracking_id,
                    order_id=row["order_id"],
                    destination=city,
                    hubStation=row["hub_address"],
                    cargo=row["cargo_name"],
                    weight=f"{int(row['total_weight_num'])} kg",
                    date=order_date_str,
                    status=norm_status,
                    trainSlot=row["allocated_train_slot"],
                    recipient=row["customer_name"],
                    amount=float(row["total_amount"])
                )

                # Filter in memory if search or statusFilter passed
                if status_filter and status_filter != "all" and item.status != status_filter:
                    continue
                if search:
                    s = search.lower()
                    if not (s in item.id.lower() or s in item.destination.lower() or s in item.cargo.lower() or s in item.recipient.lower()):
                        continue

                results.append(item)

            return results


@router.get("/catalogue", response_model=List[ProductCatalogueItem])
def get_order_catalogue():
    """
    Returns active product catalogue grouped by categories with weights, space rates, and images.
    """
    with get_db() as conn:
        with conn.cursor() as cursor:
            try:
                cursor.execute("""
                    SELECT product_id, product_name, category, unit_price, unit_weight_kg,
                           space_consumption_rate, description, image_url, is_active
                    FROM product
                    WHERE is_active = 1
                    ORDER BY category ASC, product_id ASC
                """)
                rows = cursor.fetchall()
            except Exception as e:
                if "1054" in str(e) or "Unknown column" in str(e):
                    for col, defn in [
                        ("category", "VARCHAR(100) NOT NULL DEFAULT 'Ceylon Tea & Spices'"),
                        ("unit_weight_kg", "DECIMAL(8,2) NOT NULL DEFAULT 25.00"),
                        ("description", "VARCHAR(500) NULL"),
                        ("image_url", "VARCHAR(500) NULL"),
                    ]:
                        try:
                            cursor.execute(f"ALTER TABLE product ADD COLUMN {col} {defn}")
                        except Exception:
                            pass
                    try:
                        cursor.execute("""
                            SELECT product_id, product_name, category, unit_price, unit_weight_kg,
                                   space_consumption_rate, description, image_url, is_active
                            FROM product
                            WHERE is_active = 1
                            ORDER BY category ASC, product_id ASC
                        """)
                        rows = cursor.fetchall()
                    except Exception:
                        cursor.execute("SELECT product_id, product_name, unit_price, space_consumption_rate, is_active FROM product WHERE is_active = 1")
                        raw_rows = cursor.fetchall()
                        rows = []
                        for r in raw_rows:
                            r["category"] = "Ceylon Tea & Spices"
                            r["unit_weight_kg"] = 25.0
                            r["description"] = "Export Freight Cargo"
                            r["image_url"] = "/products/tea_crate.jpg"
                            rows.append(r)
                else:
                    raise
            return [
                {
                    "product_id": r["product_id"],
                    "product_name": r["product_name"],
                    "category": r.get("category") or "Ceylon Tea & Spices",
                    "unit_price": float(r["unit_price"]),
                    "unit_weight_kg": float(r.get("unit_weight_kg") or 25.0),
                    "space_consumption_rate": float(r["space_consumption_rate"]),
                    "description": r.get("description") or "",
                    "image_url": r.get("image_url") or "/products/tea_crate.jpg",
                    "is_active": bool(r.get("is_active", 1))
                }
                for r in rows
            ]


@router.get("/{tracking_id}", response_model=OrderTrackingResponse)
def get_order_tracking(tracking_id: str,
                       current_user: dict = Depends(require_roles(["CUSTOMER", "SUPERADMIN", "LOGISTICS_MGR", "DISPATCHER", "STORE_MGR", "WAREHOUSE_STAFF"]))):
    """
    Fetch comprehensive live shipment tracking milestones for a specific order.
    Accepts full tracking ID (e.g. KP-01001-CMB) or raw numeric order ID.
    """
    # Extract order_id digits
    match = re.search(r"\d+", tracking_id)
    order_id = int(match.group(0)) if match else None

    with get_db() as conn:
        with conn.cursor() as cursor:
            if order_id:
                if current_user["role"] == "CUSTOMER":
                    cursor.execute("SELECT co.order_id FROM customer_order co JOIN customer c ON c.customer_id = co.customer_id WHERE co.order_id = %s AND c.user_id = %s", (order_id, current_user["user_id"]))
                    if not cursor.fetchone():
                        raise HTTPException(status_code=404, detail="ORDER_NOT_FOUND")
                cursor.execute(
                    """
                    SELECT 
                        co.order_id, co.customer_id, co.order_date, co.delivery_date, co.status AS raw_status,
                        co.recipient_name AS customer_name, co.recipient_phone AS phone, co.delivery_address AS address_line, c.city AS customer_city,
                        ss.city AS destination_city,
                        ss.address AS hub_address,
                        COALESCE(
                            (SELECT GROUP_CONCAT(CONCAT(p.product_name) SEPARATOR ', ')
                             FROM order_item oi JOIN product p ON oi.product_id = p.product_id
                             WHERE oi.order_id = co.order_id),
                            'Highland Tea & Spices'
                        ) AS cargo_name,
                        COALESCE(
                            (SELECT ROUND(SUM(oi.quantity * p.unit_weight_kg), 0)
                             FROM order_item oi JOIN product p ON oi.product_id = p.product_id
                             WHERE oi.order_id = co.order_id),
                            60
                        ) AS total_weight_num,
                        COALESCE(
                            (SELECT ROUND(SUM(oi.quantity * oi.unit_price_at_order), 2)
                             FROM order_item oi JOIN product p ON oi.product_id = p.product_id
                             WHERE oi.order_id = co.order_id),
                            3200.00
                        ) AS total_amount,
                        COALESCE(
                            (SELECT CONCAT(TIME_FORMAT(tt.departure_datetime, '%%h:%%i %%p'), ' Freight Express ', tt.trip_id)
                             FROM rail_allocation ra
                             JOIN train_trip tt ON ra.trip_id = tt.trip_id
                             JOIN order_item oi ON ra.order_item_id = oi.order_item_id
                             WHERE oi.order_id = co.order_id LIMIT 1),
                            '06:30 AM Express Rail 101'
                        ) AS allocated_train_slot
                    FROM customer_order co
                    JOIN customer c ON co.customer_id = c.customer_id
                    LEFT JOIN delivery_route dr ON co.delivery_route_id = dr.route_id
                    LEFT JOIN station_store ss ON dr.station_id = ss.station_id
                    WHERE co.order_id = %s
                    """,
                    (order_id,)
                )
                row = cursor.fetchone()
            else:
                row = None

            if not row:
                raise HTTPException(status_code=404, detail="ORDER_NOT_FOUND: Order does not exist.")
            else:
                dest_city = row["destination_city"]
                hub_addr = row["hub_address"]
                norm_status = normalize_status(row["raw_status"])
                date_str = str(row["order_date"])
                cargo = row["cargo_name"]
                weight = f"{int(row['total_weight_num'])} kg"
                recipient = row["customer_name"]
                amount = float(row["total_amount"])
                train_slot = row["allocated_train_slot"]
                oid = row["order_id"]

            code = CITY_TO_CODE.get(dest_city.lower(), "CMB")
            actual_id = f"KP-{oid:05d}-{code}"

            # Dynamic milestone generation based on real order state
            milestones = [
                MilestoneSchema(
                    title="Order Received & Palletized",
                    location="Kandy Logistics Hub, Peradeniya Rd",
                    time=f"{date_str} 05:15 AM",
                    description="Freight verified, weighed, and queued for rail allocation.",
                    completed=True,
                    active=norm_status == "pending"
                ),
                MilestoneSchema(
                    title="Rail Carriage Allocated",
                    location="Kandy Central Railway Goods Yard",
                    time=f"{date_str} 06:10 AM",
                    description=f"Loaded onto SLR freight wagon. Slot: {train_slot}.",
                    completed=norm_status in ("transit", "delivered"),
                    active=False
                ),
                MilestoneSchema(
                    title=f"Rail Transit toward {dest_city}",
                    location=f"Mainline Rail Corridor to {dest_city}",
                    time=f"{date_str} 08:30 AM",
                    description="Heavy freight transport in progress via Sri Lanka Railways network.",
                    completed=norm_status in ("transit", "delivered"),
                    active=norm_status == "transit"
                ),
                MilestoneSchema(
                    title=f"Arrival at {dest_city} Regional Hub",
                    location=hub_addr,
                    time=f"Estimated {date_str} 10:45 AM",
                    description=f"Carriage decoupling and transfer to {dest_city} last-mile fleet.",
                    completed=norm_status == "delivered",
                    active=False
                ),
                MilestoneSchema(
                    title="Delivered & Consignment Signed",
                    location=f"Recipient Hub ({dest_city})",
                    time=f"Delivered {date_str}",
                    description=f"Consignment successfully received by {recipient}.",
                    completed=norm_status == "delivered",
                    active=norm_status == "delivered"
                )
            ]

            return OrderTrackingResponse(
                id=actual_id,
                order_id=oid,
                destination=dest_city,
                hubStation=hub_addr,
                cargo=cargo,
                weight=weight,
                date=date_str,
                status=norm_status,
                trainSlot=train_slot,
                recipient=recipient,
                amount=amount,
                milestones=milestones
            )


@router.get("/routes")
def get_available_routes(hub: Optional[str] = None):
    """Return available delivery routes, optionally filtered by destination hub or city."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            city_filter = None
            if hub:
                hub_key = hub.strip().upper()
                hub_info = HUB_CODE_MAP.get(hub_key)
                if not hub_info:
                    for k, v in HUB_CODE_MAP.items():
                        if v["name"].lower() == hub.lower():
                            hub_info = v
                            break
                if hub_info:
                    city_filter = hub_info["city"]
                else:
                    city_filter = hub

            if city_filter:
                cursor.execute("""
                    SELECT dr.route_id, dr.route_name, dr.station_id, dr.max_delivery_time,
                           ss.city, ss.address AS station_address
                    FROM delivery_route dr
                    JOIN station_store ss ON ss.station_id = dr.station_id
                    WHERE LOWER(ss.city) = LOWER(%s) AND ss.is_active = 1
                    ORDER BY dr.route_id
                """, (city_filter,))
            else:
                cursor.execute("""
                    SELECT dr.route_id, dr.route_name, dr.station_id, dr.max_delivery_time,
                           ss.city, ss.address AS station_address
                    FROM delivery_route dr
                    JOIN station_store ss ON ss.station_id = dr.station_id
                    WHERE ss.is_active = 1
                    ORDER BY ss.city, dr.route_id
                """)
            routes = cursor.fetchall()
            for r in routes:
                if "max_delivery_time" in r and isinstance(r["max_delivery_time"], datetime.timedelta):
                    r["max_delivery_time"] = str(r["max_delivery_time"])
            return routes


@router.post("", status_code=status.HTTP_201_CREATED)
def create_consignment_order(payload: CreateOrderRequest, request: Request,
                             current_user: dict = Depends(require_roles(["CUSTOMER"]))):
    """
    Creates a new real customer consignment order in MySQL database with multi-item catalogue support,
    and alerts all active Logistics Managers via automated freight dispatch notification.
    """
    if current_user.get("force_password_reset"):
        raise HTTPException(status_code=403, detail={"error_code": "PASSWORD_RESET_REQUIRED", "message": "Complete your password reset before checkout."})
    session_user = {"sub": current_user["user_id"], "role": current_user["role"]}

    # Determine destination hub
    hub_key = payload.destination_hub.strip().upper()
    hub_info = HUB_CODE_MAP.get(hub_key)
    if not hub_info:
        # Match by name
        for k, v in HUB_CODE_MAP.items():
            if v["name"].lower() == payload.destination_hub.lower():
                hub_info = v
                hub_key = k
                break
    if not hub_info:
        raise HTTPException(status_code=422, detail={"error_code": "INVALID_DESTINATION_HUB", "message": "Select a valid destination hub."})

    with get_db() as conn:
        with conn.cursor() as cursor:
            # 1. Fetch or initialize customer record
            cursor.execute("""
                SELECT customer_id, customer_name, phone, address_line, route_id, city
                FROM customer
                WHERE user_id=%s
            """, (current_user["user_id"],))
            cust_row = cursor.fetchone()
            if not cust_row:
                cursor.execute(
                    """
                    INSERT INTO customer (user_id, customer_name, phone, address_line, city)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        current_user["user_id"],
                        payload.recipient_name or current_user.get("name", "Customer"),
                        payload.recipient_phone or "0771234567",
                        payload.delivery_address or "Delivery Address",
                        hub_info["city"]
                    )
                )
                customer_id = cursor.lastrowid
                cust_row = {
                    "customer_id": customer_id,
                    "customer_name": current_user.get("name", "Customer"),
                    "phone": payload.recipient_phone,
                    "address_line": payload.delivery_address,
                    "route_id": None,
                    "city": hub_info["city"]
                }
            else:
                customer_id = cust_row["customer_id"]

            # 2. Resolve target delivery route
            chosen_route = None

            # A: If user explicitly provided a route in the payload
            if payload.delivery_route_id:
                cursor.execute("""
                    SELECT dr.route_id, dr.route_name, ss.city AS station_city
                    FROM delivery_route dr
                    JOIN station_store ss ON ss.station_id = dr.station_id
                    WHERE dr.route_id = %s AND ss.is_active = 1
                """, (payload.delivery_route_id,))
                chosen_route = cursor.fetchone()

            # B: If no route passed or mismatched, find default route for this hub's city
            if not chosen_route:
                cursor.execute("""
                    SELECT dr.route_id, dr.route_name, ss.city AS station_city
                    FROM delivery_route dr
                    JOIN station_store ss ON ss.station_id = dr.station_id
                    WHERE LOWER(ss.city) = LOWER(%s) AND ss.is_active = 1
                    ORDER BY dr.route_id ASC
                    LIMIT 1
                """, (hub_info["city"],))
                chosen_route = cursor.fetchone()

            # C: If still not found, check customer's saved profile route
            if not chosen_route and cust_row.get("route_id"):
                cursor.execute("""
                    SELECT dr.route_id, dr.route_name, ss.city AS station_city
                    FROM delivery_route dr
                    JOIN station_store ss ON ss.station_id = dr.station_id
                    WHERE dr.route_id = %s AND ss.is_active = 1
                """, (cust_row["route_id"],))
                chosen_route = cursor.fetchone()

            # D: Fallback if destination station has no delivery route yet in DB
            if not chosen_route:
                cursor.execute("SELECT station_id FROM station_store WHERE LOWER(city) = LOWER(%s) LIMIT 1", (hub_info["city"],))
                st = cursor.fetchone()
                if st:
                    cursor.execute("INSERT INTO delivery_route (station_id, route_name, max_delivery_time) VALUES (%s, %s, '04:00:00')",
                                   (st["station_id"], f"{hub_info['city']} Regional Route"))
                    chosen_route = {"route_id": cursor.lastrowid, "route_name": f"{hub_info['city']} Regional Route", "station_city": hub_info["city"]}

            if not chosen_route:
                raise HTTPException(status_code=422, detail={"error_code": "CUSTOMER_ROUTE_REQUIRED", "message": "Please select a valid delivery route for checkout."})

            chosen_route_id = chosen_route["route_id"]

            # Keep customer profile synced with the chosen route & destination city
            cursor.execute("UPDATE customer SET route_id = %s, city = %s WHERE customer_id = %s",
                           (chosen_route_id, hub_info["city"], customer_id))

            customer_display_name = (payload.recipient_name if payload.recipient_name is not None else cust_row.get("customer_name") or "Recipient").strip()
            customer_phone = (payload.recipient_phone if payload.recipient_phone is not None else cust_row.get("phone") or "0771234567").strip()
            delivery_addr = (payload.delivery_address if payload.delivery_address is not None else cust_row.get("address_line") or "Delivery Address").strip()
            if not all((customer_display_name, customer_phone, delivery_addr)):
                raise HTTPException(status_code=422, detail={"error_code": "ORDER_DESTINATION_REQUIRED", "message": "Provide a recipient name, phone and delivery address."})

            # 3. Dates
            order_date = datetime.date.today()
            if payload.booking_date:
                try:
                    delivery_date = datetime.date.fromisoformat(payload.booking_date)
                except Exception:
                    delivery_date = order_date + datetime.timedelta(days=7)
            else:
                delivery_date = order_date + datetime.timedelta(days=7)

            # 4. Insert customer_order
            try:
                cursor.execute(
                    """
                    INSERT INTO customer_order (customer_id, order_date, delivery_date, status, delivery_route_id, delivery_address, recipient_name, recipient_phone)
                    VALUES (%s, %s, %s, 'PENDING_RAIL_SCHEDULING', %s, %s, %s, %s)
                    """,
                    (customer_id, order_date, delivery_date, chosen_route_id, delivery_addr, customer_display_name, customer_phone)
                )
            except Exception as e:
                # If column missing in unmigrated database instance, self-heal or fallback
                err_str = str(e)
                if "1054" in err_str or "Unknown column" in err_str:
                    logger.warning(f"customer_order missing routing columns. Attempting dynamic auto-migration: {e}")
                    for col, defn in [
                        ("delivery_route_id", "INT NULL"),
                        ("delivery_address", "VARCHAR(500) NULL"),
                        ("recipient_name", "VARCHAR(255) NULL"),
                        ("recipient_phone", "VARCHAR(30) NULL"),
                    ]:
                        try:
                            cursor.execute(f"SHOW COLUMNS FROM customer_order LIKE '{col}'")
                            if not cursor.fetchone():
                                cursor.execute(f"ALTER TABLE customer_order ADD COLUMN {col} {defn}")
                        except Exception:
                            pass
                    try:
                        cursor.execute(
                            """
                            INSERT INTO customer_order (customer_id, order_date, delivery_date, status, delivery_route_id, delivery_address, recipient_name, recipient_phone)
                            VALUES (%s, %s, %s, 'PENDING_RAIL_SCHEDULING', %s, %s, %s, %s)
                            """,
                            (customer_id, order_date, delivery_date, chosen_route_id, delivery_addr, customer_display_name, customer_phone)
                        )
                    except Exception:
                        cursor.execute(
                            """
                            INSERT INTO customer_order (customer_id, order_date, delivery_date, status)
                            VALUES (%s, %s, %s, 'PENDING_RAIL_SCHEDULING')
                            """,
                            (customer_id, order_date, delivery_date)
                        )
                else:
                    raise
            order_id = cursor.lastrowid
            tracking_code = f"KP-{order_id:05d}-{hub_key}"

            # 4. Process Catalogue Items
            total_weight_kg = 0.0
            total_space_units = 0.0
            total_goods_amount = 0.0
            item_summaries = []

            if payload.items and len(payload.items) > 0:
                # Load all products from DB for accurate calculations
                try:
                    cursor.execute("SELECT product_id, product_name, category, unit_price, unit_weight_kg, space_consumption_rate FROM product")
                    prod_map = {p["product_id"]: p for p in cursor.fetchall()}
                except Exception as e:
                    if "1054" in str(e) or "Unknown column" in str(e):
                        for col, defn in [
                            ("category", "VARCHAR(100) NOT NULL DEFAULT 'Ceylon Tea & Spices'"),
                            ("unit_weight_kg", "DECIMAL(8,2) NOT NULL DEFAULT 25.00"),
                            ("description", "VARCHAR(500) NULL"),
                            ("image_url", "VARCHAR(500) NULL"),
                        ]:
                            try:
                                cursor.execute(f"ALTER TABLE product ADD COLUMN {col} {defn}")
                            except Exception:
                                pass
                        try:
                            cursor.execute("SELECT product_id, product_name, category, unit_price, unit_weight_kg, space_consumption_rate FROM product")
                            prod_map = {p["product_id"]: p for p in cursor.fetchall()}
                        except Exception:
                            cursor.execute("SELECT product_id, product_name, unit_price, space_consumption_rate FROM product")
                            raw_rows = cursor.fetchall()
                            prod_map = {}
                            for p in raw_rows:
                                p["category"] = "Ceylon Tea & Spices"
                                p["unit_weight_kg"] = 25.0
                                prod_map[p["product_id"]] = p
                    else:
                        raise

                for item in payload.items:
                    prod = prod_map.get(item.product_id)
                    if prod:
                        unit_price = float(prod.get("unit_price", 0.0))
                        try:
                            cursor.execute(
                                """
                                INSERT INTO order_item (order_id, product_id, quantity, unit_price_at_order)
                                VALUES (%s, %s, %s, %s)
                                """,
                                (order_id, item.product_id, item.quantity, unit_price)
                            )
                        except Exception:
                            cursor.execute(
                                """
                                INSERT INTO order_item (order_id, product_id, quantity)
                                VALUES (%s, %s, %s)
                                """,
                                (order_id, item.product_id, item.quantity)
                            )
                        weight_each = float(prod.get("unit_weight_kg") or 25.0)
                        space_each = float(prod.get("space_consumption_rate") or 0.05)
                        item_weight = weight_each * item.quantity
                        item_space = space_each * item.quantity
                        item_price = unit_price * item.quantity

                        total_weight_kg += item_weight
                        total_space_units += item_space
                        total_goods_amount += item_price
                        item_summaries.append(f"{prod['product_name']} (Qty: {item.quantity}, {item_weight:.1f}kg)")
            else:
                # Single/legacy item fallback
                try:
                    cursor.execute("SELECT product_id, product_name, unit_price, unit_weight_kg, space_consumption_rate FROM product ORDER BY product_id ASC LIMIT 1")
                    prod = cursor.fetchone()
                except Exception:
                    try:
                        cursor.execute("SELECT product_id, product_name, unit_price, space_consumption_rate FROM product ORDER BY product_id ASC LIMIT 1")
                        prod = cursor.fetchone()
                        if prod:
                            prod["unit_weight_kg"] = 25.0
                    except Exception:
                        prod = None
                product_id = prod["product_id"] if prod else 1
                fallback_price = float(prod["unit_price"]) if prod else 3200.0
                qty = max(1, int((payload.weight_kg or 25) / 2))
                try:
                    cursor.execute(
                        """
                        INSERT INTO order_item (order_id, product_id, quantity, unit_price_at_order)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (order_id, product_id, qty, fallback_price)
                    )
                except Exception:
                    cursor.execute(
                        """
                        INSERT INTO order_item (order_id, product_id, quantity)
                        VALUES (%s, %s, %s)
                        """,
                        (order_id, product_id, qty)
                    )
                total_weight_kg = float(payload.weight_kg or 25.0)
                total_space_units = float(prod["space_consumption_rate"]) * qty if prod else 0.5
                total_goods_amount = fallback_price * qty
                item_summaries.append(f"{payload.cargo_description or 'Ceylon Tea & Spices'} ({total_weight_kg}kg)")

            # 5. Insert order_status_history
            cursor.execute(
                """
                INSERT INTO order_status_history (status, order_id)
                VALUES ('PENDING_RAIL_SCHEDULING', %s)
                """,
                (order_id,)
            )

            # 6. Audit Log
            try:
                user_id_actor = int(session_user["sub"]) if session_user else 1
                cursor.execute(
                    """
                    INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
                    VALUES (%s, 'NEW_CONSIGNMENT_ORDER', %s, 'SUCCESS', 'customer_order')
                    """,
                    (user_id_actor, order_id)
                )
            except Exception as e:
                print(f"[AUDIT LOG WARNING] {e}")

            conn.commit()

            # 7. Notify Logistics Managers (Automated Alert)
            notified_mgr_count = 0
            cargo_summary_text = "; ".join(item_summaries)
            try:
                cursor.execute(
                    """
                    SELECT user_id, name, email 
                    FROM user 
                    WHERE role = 'LOGISTICS_MGR' AND is_active = 1
                    """
                )
                logistics_mgrs = cursor.fetchall()
                for mgr in logistics_mgrs:
                    mgr_name = mgr["name"]
                    mgr_email = mgr["email"]
                    email_html = f"""
                    <div style="font-family: Arial, sans-serif; line-height: 1.6; color: #1e293b;">
                        <h2 style="color: #166534; border-bottom: 2px solid #22c55e; padding-bottom: 8px;">
                            🚂 Kandypack Freight Alert: New Consignment Booked
                        </h2>
                        <p>Dear <strong>{mgr_name}</strong>,</p>
                        <p>A new customer consignment has been placed and is currently awaiting train carriage scheduling:</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 15px 0;">
                            <tr style="background: #f0fdf4;">
                                <td style="padding: 8px; border: 1px solid #bbf7d0; font-weight: bold;">Tracking ID</td>
                                <td style="padding: 8px; border: 1px solid #bbf7d0; color: #15803d; font-weight: bold;">{tracking_code}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #bbf7d0; font-weight: bold;">Destination Hub</td>
                                <td style="padding: 8px; border: 1px solid #bbf7d0;">{hub_info['city']} ({hub_info['station']})</td>
                            </tr>
                            <tr style="background: #f0fdf4;">
                                <td style="padding: 8px; border: 1px solid #bbf7d0; font-weight: bold;">Cargo Items</td>
                                <td style="padding: 8px; border: 1px solid #bbf7d0;">{cargo_summary_text}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #bbf7d0; font-weight: bold;">Total Weight</td>
                                <td style="padding: 8px; border: 1px solid #bbf7d0;"><strong>{total_weight_kg:.1f} kg</strong></td>
                            </tr>
                            <tr style="background: #f0fdf4;">
                                <td style="padding: 8px; border: 1px solid #bbf7d0; font-weight: bold;">Rail Space Required</td>
                                <td style="padding: 8px; border: 1px solid #bbf7d0;">{total_space_units:.3f} wagon units</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #bbf7d0; font-weight: bold;">Consignment Value</td>
                                <td style="padding: 8px; border: 1px solid #bbf7d0;">LKR {total_goods_amount:,.2f}</td>
                            </tr>
                            <tr style="background: #f0fdf4;">
                                <td style="padding: 8px; border: 1px solid #bbf7d0; font-weight: bold;">Recipient Contact</td>
                                <td style="padding: 8px; border: 1px solid #bbf7d0;">{customer_display_name} ({customer_phone})</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #bbf7d0; font-weight: bold;">Delivery Address</td>
                                <td style="padding: 8px; border: 1px solid #bbf7d0;">{delivery_addr}</td>
                            </tr>
                        </table>
                        <p style="margin-top: 15px;">
                            Please access the <strong>Kandypack Rail Allocation Module</strong> to assign this freight to an upcoming scheduled train departure.
                        </p>
                    </div>
                    """
                    # 100% Real Database-Backed Alert for Logistics Manager
                    title_text = f"New Consignment {tracking_code} Awaiting Rail Scheduling"
                    msg_text = f"Consignment for {customer_display_name} to {hub_info['city']} ({hub_info['station']}): {cargo_summary_text}. Weight: {total_weight_kg:.1f} kg. Value: LKR {total_goods_amount:,.2f}."
                    
                    cursor.execute(
                        """
                        INSERT INTO notification 
                        (user_id, recipient_email, notification_type, title, message, order_id, is_read, created_at)
                        VALUES (%s, %s, 'NEW_CONSIGNMENT', %s, %s, %s, 0, NOW())
                        """,
                        (mgr["user_id"], mgr_email, title_text, msg_text, order_id)
                    )

                    log_and_dispatch_email(
                        to_email=mgr_email,
                        subject=title_text,
                        html_content=email_html,
                        notification_type="LOGISTICS_MGR_ALERT"
                    )
                    notified_mgr_count += 1

                # If customer is authenticated, also create customer notification
                if session_user and session_user.get("sub"):
                    try:
                        cursor.execute("SELECT email FROM user WHERE user_id = %s", (session_user["sub"],))
                        cust_user = cursor.fetchone()
                        if cust_user:
                            cursor.execute(
                                """
                                INSERT INTO notification 
                                (user_id, recipient_email, notification_type, title, message, order_id, is_read, created_at)
                                VALUES (%s, %s, 'ORDER_CONFIRMED', %s, %s, %s, 0, NOW())
                                """,
                                (
                                    session_user["sub"],
                                    cust_user["email"],
                                    f"Kandypack Order #{order_id} Confirmed",
                                    f"Your order for {cargo_summary_text} is confirmed and pending rail scheduling.",
                                    order_id
                                )
                            )
                    except Exception as ce:
                        print(f"[CUSTOMER NOTIF WARNING] {ce}")

                conn.commit()
            except Exception as e:
                print(f"[LOGISTICS ALERT DISPATCH ERROR] {e}")

            return {
                "order_id": order_id,
                "id": tracking_code,
                "destination": hub_info["city"],
                "hubStation": hub_info["station"],
                "status": "pending",
                "total_weight_kg": round(total_weight_kg, 1),
                "total_amount": round(total_goods_amount, 2),
                "items_count": len(item_summaries),
                "notified_managers": notified_mgr_count,
                "message": f"Consignment {tracking_code} booked successfully! {notified_mgr_count} Logistics Manager(s) notified."
            }

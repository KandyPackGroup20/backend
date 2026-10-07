import pymysql
from app.core.database import get_db

def run_migration():
    with get_db() as conn:
        with conn.cursor() as c:
            # Check columns in product table
            c.execute("DESCRIBE product")
            cols = [r["Field"] for r in c.fetchall()]
            
            if "category" not in cols:
                c.execute("ALTER TABLE product ADD COLUMN category VARCHAR(100) NOT NULL DEFAULT 'Ceylon Tea & Spices'")
                print("Added column category")
            if "unit_weight_kg" not in cols:
                c.execute("ALTER TABLE product ADD COLUMN unit_weight_kg DECIMAL(8,2) NOT NULL DEFAULT 25.00")
                print("Added column unit_weight_kg")
            if "description" not in cols:
                c.execute("ALTER TABLE product ADD COLUMN description VARCHAR(500) NULL")
                print("Added column description")
            if "image_url" not in cols:
                c.execute("ALTER TABLE product ADD COLUMN image_url VARCHAR(500) NULL")
                print("Added column image_url")
            conn.commit()

            # Seed / Upsert the 8 products across 4 categories
            products = [
                (
                    1,
                    "Kandy Pure Ceylon BOPF Tea (25kg Crate)",
                    "Ceylon Tea & Spices",
                    4500.00,
                    25.00,
                    0.0500,
                    "High-grown export grade Ceylon Black BOPF tea packed in moisture-resistant foil-lined wooden crates.",
                    "/products/tea_crate.jpg"
                ),
                (
                    2,
                    "Ceylon Spices & Cinnamon Sack (20kg)",
                    "Ceylon Tea & Spices",
                    3800.00,
                    20.00,
                    0.0400,
                    "Sun-cured Ceylon alba cinnamon sticks, premium cardamom pods, and organic cloves in heavy-duty jute sacks.",
                    "/products/spices_sack.jpg"
                ),
                (
                    3,
                    "Nuwara Eliya Highland Vegetables Crate (30kg)",
                    "Fresh Produce & FMCG",
                    2600.00,
                    30.00,
                    0.0800,
                    "Ventilated farm-fresh crates of premium highland carrots, leeks, bell peppers, and cabbage for rapid rail transit.",
                    "/products/produce_crates.jpg"
                ),
                (
                    4,
                    "Ceylon Virgin Coconut Oil Canister (20L / 18kg)",
                    "Fresh Produce & FMCG",
                    4200.00,
                    18.00,
                    0.0450,
                    "Cold-pressed extra-virgin coconut oil in food-grade sealed HDPE transit containers.",
                    "/products/coconut_oil.jpg"
                ),
                (
                    5,
                    "Kandy Handloom Cotton Textile Bolts (25kg)",
                    "Garments & Textiles",
                    5200.00,
                    25.00,
                    0.0600,
                    "Protective shrink-wrapped bolts of traditional Sri Lankan batik and handloom cotton textiles for commercial retail.",
                    "/products/textile_rolls.jpg"
                ),
                (
                    6,
                    "Apparel & Garment Export Cartons (20kg)",
                    "Garments & Textiles",
                    4800.00,
                    20.00,
                    0.0550,
                    "Triple-wall corrugated export master cartons of finished garments with security straps and barcoded tags.",
                    "/products/garments_box.jpg"
                ),
                (
                    7,
                    "Traditional Brassware & Metal Crafts Crate (35kg)",
                    "Hardware & Industrial",
                    7500.00,
                    35.00,
                    0.0700,
                    "Handcrafted polished brass oil lamps, brassware, and cultural souvenirs cushioned in protective wooden crates.",
                    "/products/brassware_crate.jpg"
                ),
                (
                    8,
                    "Precision Industrial Machinery Spares (40kg)",
                    "Hardware & Industrial",
                    8900.00,
                    40.00,
                    0.0850,
                    "High-grade steel gears, shafts, and mechanical components packed in shock-absorbing foam-lined transport cases.",
                    "/products/machinery_parts.jpg"
                ),
            ]

            for p in products:
                c.execute("""
                    INSERT INTO product (product_id, product_name, category, unit_price, unit_weight_kg, space_consumption_rate, description, image_url, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
                    ON DUPLICATE KEY UPDATE
                        product_name = VALUES(product_name),
                        category = VALUES(category),
                        unit_price = VALUES(unit_price),
                        unit_weight_kg = VALUES(unit_weight_kg),
                        space_consumption_rate = VALUES(space_consumption_rate),
                        description = VALUES(description),
                        image_url = VALUES(image_url),
                        is_active = 1
                """, p)
            conn.commit()

            c.execute("SELECT product_id, product_name, category, unit_weight_kg, unit_price, image_url FROM product")
            rows = c.fetchall()
            print(f"Migration completed successfully! Total products: {len(rows)}")
            for r in rows:
                print(r)

if __name__ == "__main__":
    run_migration()

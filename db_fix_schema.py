# db_fix_schema.py
# Fix schema cho bảng trapbot_alerts trong Railway Postgres

import os
from sqlalchemy import create_engine, text

print("🚀 Running db_fix_schema.py ...")

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ ERROR: DATABASE_URL not found. Script must run inside Railway environment.")
    exit(1)

engine = create_engine(DATABASE_URL)

print("🔌 Connecting to database...")

# Danh sách các cột cần có trong trapbot_alerts
EXPECTED_COLUMNS = {
    "ts": "timestamptz",
    "symbol": "text",
    "kind": "text",
    "message": "text",
    "tei": "integer",
    "price": "numeric",
    "funding_pct": "numeric",
    "oi": "bigint",
    "z_vals": "jsonb",
    "meta": "jsonb",
    "created_at": "timestamptz DEFAULT NOW()"
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trapbot_alerts (
    id BIGSERIAL PRIMARY KEY
);
"""

print("🛠 Creating table if not exists...")

ALTER_SQLS = [
    f"ALTER TABLE trapbot_alerts ADD COLUMN IF NOT EXISTS {col} {datatype};"
    for col, datatype in EXPECTED_COLUMNS.items()
]

try:
    with engine.begin() as conn:
        conn.execute(text(CREATE_TABLE_SQL))
        print("✅ Table OK")

        print("🛠 Applying ALTER TABLE statements...")
        for sql in ALTER_SQLS:
            conn.execute(text(sql))
            print(f"   → {sql}")

    print("\n🎉 DONE — Schema updated successfully!")
except Exception as e:
    print("❌ ERROR while updating schema:", e)

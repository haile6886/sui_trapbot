# db_fix_schema.py
"""
Script an toàn để đảm bảo schema của trapbot_alerts tồn tại / có các cột cần thiết.
Chạy script này trong môi trường Railway (railway run python db_fix_schema.py)
Đảm bảo DATABASE_URL được set trong env của Railway (Railway inject tự động).
"""
import os
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set in environment. Railway should provide it when using `railway run`.")
    raise SystemExit(1)

engine = create_engine(DATABASE_URL)

# Safe create table if missing with minimal columns (keeps backward compatibility)
sql_create_if_missing = f"""
CREATE TABLE IF NOT EXISTS trapbot_alerts (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    kind TEXT,
    message TEXT,
    tei INTEGER,
    price NUMERIC,
    funding_pct NUMERIC,
    oi BIGINT,
    z_vals JSONB,
    meta JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

# Ensure additional expected columns exist (IF NOT EXISTS)
sql_alter = f"""
ALTER TABLE trapbot_alerts
  ADD COLUMN IF NOT EXISTS symbol TEXT,
  ADD COLUMN IF NOT EXISTS kind TEXT,
  ADD COLUMN IF NOT EXISTS message TEXT,
  ADD COLUMN IF NOT EXISTS tei INTEGER,
  ADD COLUMN IF NOT EXISTS price NUMERIC,
  ADD COLUMN IF NOT EXISTS funding_pct NUMERIC,
  ADD COLUMN IF NOT EXISTS oi BIGINT,
  ADD COLUMN IF NOT EXISTS z_vals JSONB,
  ADD COLUMN IF NOT EXISTS meta JSONB,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
"""

try:
    with engine.begin() as conn:
        print("[DB FIX] Ensuring table trapbot_alerts exists...")
        conn.execute(text(sql_create_if_missing))
        print("[DB FIX] Ensuring expected columns exist (ALTER TABLE ... IF NOT EXISTS)...")
        conn.execute(text(sql_alter))
    print("[DB FIX] Completed successfully.")
except SQLAlchemyError as e:
    print("[DB FIX] SQLAlchemyError:", e)
except Exception as e:
    print("[DB FIX] Unexpected error:", e)

# check_schema.py
import os
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ DATABASE_URL chưa được thiết lập.")
    print("Vào Railway → Service Postgres → Copy Connection URL, rồi set nó trong Railway Env hoặc local test.")
    raise SystemExit(1)

engine = create_engine(DATABASE_URL)
with engine.connect() as conn:
    print("✅ Kết nối thành công tới Database!")
    q = text("""
      SELECT column_name, data_type, is_nullable
      FROM information_schema.columns
      WHERE table_name = 'trapbot_alerts'
      ORDER BY ordinal_position;
    """)
    rows = conn.execute(q).fetchall()
    if not rows:
        print("⚠️  Bảng trapbot_alerts chưa tồn tại hoặc chưa có cột nào.")
    else:
        print("📋 Danh sách cột hiện có trong trapbot_alerts:")
        for r in rows:
            print(f" - {r[0]} | {r[1]} | nullable={r[2]}")

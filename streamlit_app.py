import os
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

st.set_page_config(page_title="SUI TrapBot Dashboard", layout="wide")

# -------- DB CONNECT --------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    st.error("❌ DATABASE_URL chưa thiết lập trong Railway → Variables.")
    st.stop()

engine = create_engine(DATABASE_URL)

# -------- UI OPTIONS --------
st.sidebar.title("Tuỳ chọn")
table_data = st.sidebar.text_input("Tên bảng dữ liệu", "trapbot_data")
table_alerts = st.sidebar.text_input("Tên bảng cảnh báo", "trapbot_alerts")
limit_rows = st.sidebar.number_input("Số dòng tối đa lấy", 1000, 200000, 1000)
resample_min = st.sidebar.number_input("Resample hiển thị (phút)", 1, 60, 5)
refresh_sec = st.sidebar.number_input("Refresh (giây)", 5, 300, 30)

if st.sidebar.button("Refresh"):
    st.experimental_rerun()


# -------- HELPER: SAFE SQL LOAD --------
def load_table(name):
    try:
        with engine.connect() as conn:
            q = text(f"SELECT * FROM {name} ORDER BY id DESC LIMIT :limit")
            df = pd.read_sql(q, conn, params={"limit": limit_rows})
        if df.empty:
            return None
        return df
    except Exception as e:
        st.warning(f"Không đọc được bảng `{name}`: {e}")
        return None


# -------- PAGE TITLE --------
st.title("📊 SUI TrapBot — Dashboard (UTC+7)")

# -------- LOAD DATA TABLES --------
df = load_table(table_data)
alerts = load_table(table_alerts)

# ====== SUMMARY SECTION ======
st.header("Tổng quan nhanh")

if df is None:
    st.info("⛔ Chưa có dữ liệu.")
else:
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Dòng gần nhất", len(df))
    col2.metric("Giá mới nhất", f"{df['price'].iloc[0]:.6f}")
    col3.metric("Funding mới nhất", f"{df['funding_pct'].iloc[0]:.6f}")
    col4.metric("OI mới nhất", f"{df['oi'].iloc[0]:,}")


# ====== CHART SECTION ======
st.header("Biểu đồ: Giá / Funding / OI (gần đây)")

if df is None:
    st.info("Chưa có dữ liệu để vẽ biểu đồ.")
else:
    # convert timestamp
    df['timestamp'] = pd.to_datetime(df['timestamp'])

    # resample theo phút
    df_res = df.resample(f"{resample_min}T", on="timestamp").last()

    st.line_chart(df_res[["price", "funding_pct", "oi"]])


# ====== ALERTS SECTION ======
st.header("Cảnh báo (Alerts) — giống Telegram")

if alerts is None:
    st.info("Chưa có cảnh báo.")
else:
    for _, row in alerts.sort_values("id", ascending=False).head(20).iterrows():
        st.write(f"**[{row['ts']}]** — {row['kind']}: {row['message']}")


# FOOTNOTE
st.caption("""
⚙️ DB: PostgreSQL trên Railway  
Dashboard chỉ đọc — bot ghi dữ liệu.  
Nếu không thấy dữ liệu → kiểm tra bảng, tên bảng hoặc biến môi trường.
""")

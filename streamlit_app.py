# streamlit_app.py
"""
SUI TrapBot — Dashboard compact (UTC+7)
- Không tự động ghi gì vào DB (tránh ảnh hưởng bot)
- Hiển thị thời gian chuyển sang Asia/Bangkok (UTC+7)
- Detect table, Refresh, Show compact charts + metrics
- Nếu muốn auto-retrain: cung cấp SQL để chạy thủ công
"""
import os
import time
import pandas as pd
import plotly.express as px
from sqlalchemy import create_engine, text
import streamlit as st
from pathlib import Path

# Optional small autorefresh helper
try:
    from streamlit_autorefresh import st_autorefresh
    AUTORELOAD_AVAILABLE = True
except Exception:
    AUTORELOAD_AVAILABLE = False

# --- DEPLOY HELPERS / CONFIG ---
BASE_DIR = Path(__file__).resolve().parent

st.set_page_config(page_title="SUI TrapBot Dashboard", layout="wide")
st.markdown("""
<style>
h1 {font-size:26px;}
h2 {font-size:18px;}
.section {padding-top:6px;padding-bottom:6px;}
.sidebar .block-container {padding-top:0.75rem;}
footer {visibility:hidden;}
</style>
""", unsafe_allow_html=True)

# Auto-refresh every 5 seconds if lib available
if AUTORELOAD_AVAILABLE:
    st_autorefresh(interval=5_000, key="auto_refresh")

st.title("📊 SUI TrapBot — Dashboard (Compact, UTC+7)")
st.caption("Realtime: Price / Funding / OI | Read-only (safe)")

# --- DB connect ---
db_url = os.getenv("DATABASE_URL")
if not db_url:
    st.error("❌ DATABASE_URL chưa thiết lập. Vào Railway → Postgres → Credentials → copy DATABASE_URL và thêm vào Variables của service dashboard.")
    st.stop()

# SQLAlchemy sometimes needs 'postgresql://' instead of 'postgres://'
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# create engine (lightweight)
try:
    engine = create_engine(db_url, connect_args={"sslmode": "require"})
except Exception as e:
    st.error(f"Không thể tạo engine DB: {e}")
    st.stop()

# --- Helper: detect table existence & sample load ---
def list_tables():
    q = """
    SELECT tablename
    FROM pg_catalog.pg_tables
    WHERE schemaname NOT IN ('pg_catalog','information_schema');
    """
    try:
        with engine.connect() as conn:
            res = conn.execute(text(q))
            return [r[0] for r in res.fetchall()]
    except Exception:
        return []

# cache the loaded dataframe to reduce DB load
@st.cache_data(ttl=10)  # short TTL
def load_latest(table_name="trapbot_data", limit=200, cache_bust: int = 0):
    q = text(f"SELECT * FROM public.{table_name} ORDER BY timestamp DESC LIMIT {limit}")
    try:
        with engine.connect() as conn:
            df = pd.read_sql(q, conn)
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Bangkok")
            df = df.sort_values("timestamp")
        return df
    except Exception:
        return pd.DataFrame()

# --- Session state ---
if "detected_tables" not in st.session_state:
    st.session_state["detected_tables"] = None
if "cache_bust" not in st.session_state:
    st.session_state["cache_bust"] = 0

# --- Sidebar controls ---
with st.sidebar:
    st.header("Controls")

    if st.button("Detect table automatically"):
        tables = list_tables()
        st.session_state["detected_tables"] = tables
        st.rerun()

    if st.button("Refresh data"):
        st.session_state["cache_bust"] += 1
        st.rerun()

    st.write("---")
    st.info("Trigger retrain: an toàn — chỉ hiển thị SQL để bạn chạy thủ công (không ghi tự động).")

    if st.button("Trigger retrain (show SQL)"):
        sql = (
            "-- SQL mẫu: INSERT vào trapbot_commands để yêu cầu retrain\n"
            "INSERT INTO trapbot_commands(command, created_at, payload)\n"
            "VALUES ('retrain', now(), '{\"reason\": \"manual trigger from dashboard\"}');"
        )
        st.code(sql, language="sql")
        st.success("SQL hiển thị ở trên. Chạy thủ công trong psql hoặc công cụ DB nếu muốn thực hiện.")

    st.write("---")
    st.caption("Note: dashboard chỉ đọc dữ liệu, không thay đổi bot.")

# --- Main area ---
default_table = "trapbot_data"
tables = st.session_state.get("detected_tables", None)
col_table, col_limit = st.columns([3,1])
with col_table:
    table_name = st.text_input("Table name to read", value=default_table)
with col_limit:
    nrows = st.number_input("Rows", min_value=50, max_value=2000, value=300, step=50)

if tables:
    st.write("Detected tables:", ", ".join(tables))

# --- Load data ---
df = load_latest(table_name, limit=nrows, cache_bust=st.session_state["cache_bust"])

# --- Metrics ---
if not df.empty:
    last = df.iloc[-1]
    col1, col2, col3 = st.columns([3,1,1])

    # Price
    try:
        col1.metric("Price (last)", f"{float(last.get('price', 0)):.6f}")
    except Exception:
        col1.metric("Price (last)", str(last.get("price", "n/a")))

    # Funding
    funding_val = None
    for key in ("funding", "funding_pct", "fund_rate"):
        if key in df.columns:
            funding_val = last.get(key)
            break
    funding_text = f"{float(funding_val):.6f}%" if funding_val is not None else "n/a"
    col2.metric("Funding (%)", funding_text)

    # OI
    oi_val = last.get("oi") if "oi" in df.columns else last.get("open_interest", None)
    oi_text = f"{int(oi_val):,}" if (oi_val is not None and pd.notna(oi_val)) else "n/a"
    col3.metric("Open Interest", oi_text)
else:
    st.warning("Chưa có dữ liệu trong bảng hoặc không thể đọc bảng.")

# --- Charts ---
st.markdown("---")
st.subheader("Price / Funding / OI (recent)")

if not df.empty:
    if "timestamp" not in df.columns:
        st.error("Không tìm thấy cột 'timestamp' trong bảng.")
    else:
        if "price" in df.columns:
            fig_price = px.line(df, x="timestamp", y="price", title="Price", height=280)
            st.plotly_chart(fig_price, use_container_width=True)

        funding_col = next((c for c in ("funding_pct", "funding", "fund_rate") if c in df.columns), None)
        if funding_col:
            fig_f = px.line(df, x="timestamp", y=funding_col, title="Funding (%)", height=220)
            st.plotly_chart(fig_f, use_container_width=True)

        oi_col = next((c for c in ("oi", "open_interest") if c in df.columns), None)
        if oi_col:
            fig_oi = px.line(df, x="timestamp", y=oi_col, title="Open Interest", height=220)
            st.plotly_chart(fig_oi, use_container_width=True)
else:
    st.info("Đang chờ bot ghi dữ liệu vào bảng. Kiểm tra tên bảng hoặc chờ vài phút.")

# --- Info ---
st.markdown("---")
st.markdown("**Info / Troubleshoot**")
st.write(f"- DB host (detected): `{engine.url}`")
st.write("- If time shown is not VN, check timestamp type in DB (should be timestamptz or stored in UTC).")
st.write("- This dashboard reads only; it will not modify bot data.")

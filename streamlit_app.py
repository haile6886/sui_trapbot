# streamlit_app.py
"""
SUI TrapBot — Dashboard compact (UTC+7)
- Read-only dashboard (không ghi DB)
- Hiển thị thời gian Asia/Bangkok (UTC+7)
- Auto-refresh tùy chọn (thử dùng st_autorefresh nếu có)
- Cấu hình từ ENV: STREAMLIT_REFRESH_SEC, STREAMLIT_RESAMPLE_MIN, STREAMLIT_MAX_ROWS
"""
import os
import time
from pathlib import Path
import pandas as pd
import plotly.express as px
from sqlalchemy import create_engine, text
import streamlit as st

# Optional autorefresh helper
try:
    from streamlit_autorefresh import st_autorefresh
    AUTORELOAD_AVAILABLE = True
except Exception:
    AUTORELOAD_AVAILABLE = False

# --- Config from env with sensible defaults ---
STREAMLIT_REFRESH_SEC = int(os.getenv("STREAMLIT_REFRESH_SEC", "30"))
STREAMLIT_RESAMPLE_MIN = int(os.getenv("STREAMLIT_RESAMPLE_MIN", "5"))
STREAMLIT_MAX_ROWS = int(os.getenv("STREAMLIT_MAX_ROWS", "2000"))

# --- Page config / styling ---
st.set_page_config(page_title="SUI TrapBot Dashboard", layout="wide")
st.markdown(
    """
    <style>
    h1 {font-size:28px;}
    h2 {font-size:20px;}
    .section {padding-top:6px;padding-bottom:6px;}
    .sidebar .block-container {padding-top:0.75rem;}
    footer {visibility:hidden;}
    </style>
    """,
    unsafe_allow_html=True,
)

# Auto-refresh if library available
if AUTORELOAD_AVAILABLE:
    st_autorefresh(interval=STREAMLIT_REFRESH_SEC * 1000, key="auto_refresh")

st.title("📊 SUI TrapBot — Dashboard (UTC+7)")
st.caption("Bảng điều khiển chỉ đọc — Giá / Funding / OI / Alerts (Tiếng Việt)")

BASE_DIR = Path(__file__).resolve().parent

# --- DB connection ---
db_url = os.getenv("DATABASE_URL", "").strip()
if not db_url:
    st.error("❌ DATABASE_URL chưa thiết lập. Vào Railway → Postgres → Credentials → copy DATABASE_URL và thêm vào Variables của service dashboard.")
    st.stop()

# ensure driver prefix
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# create engine with minimal safe connect args
try:
    connect_args = {}
    # if the DB string looks like a hosted railway internal URL, try require ssl
    if "railway" in db_url or "rlwy" in db_url:
        connect_args = {"sslmode": "require"}
    engine = create_engine(db_url, connect_args=connect_args)
except Exception as e:
    st.error(f"Không thể tạo kết nối DB: {e}")
    st.stop()

# --- Helper: load data safely using engine.connect() (avoids pandas DBAPI warnings) ---
@st.cache_data(ttl=20)
def load_data(table: str = "trapbot_data", limit: int = 1000):
    try:
        sql = f"SELECT * FROM public.{table} ORDER BY timestamp DESC LIMIT {int(limit)}"
        with engine.connect() as conn:
            df = pd.read_sql_query(sql, conn)
        if not df.empty and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Bangkok")
            df = df.sort_values("timestamp")
        return df
    except Exception as e:
        # return empty df on error (but keep user informed)
        st.warning(f"Lỗi đọc bảng `{table}`: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=20)
def load_alerts(table: str = "trapbot_alerts", limit: int = 500):
    try:
        sql = f"SELECT * FROM public.{table} ORDER BY ts DESC LIMIT {int(limit)}"
        with engine.connect() as conn:
            df = pd.read_sql_query(sql, conn)
        if not df.empty and "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Bangkok")
            df = df.sort_values("ts")
        return df
    except Exception as e:
        # Do not spam user with repeated warnings if table doesn't exist
        # but show a light message once
        st.info(f"Không đọc được bảng cảnh báo `{table}` (có thể không tồn tại): {e}")
        return pd.DataFrame()

# --- Sidebar: controls ---
with st.sidebar:
    st.header("Tùy chọn")
    table_name = st.text_input("Tên bảng dữ liệu", value="trapbot_data")
    alerts_table = st.text_input("Tên bảng cảnh báo", value="trapbot_alerts")
    max_rows = st.number_input("Số dòng tối đa lấy (max)", min_value=100, max_value=STREAMLIT_MAX_ROWS, value=1000, step=100)
    resample_min = st.number_input("Resample hiển thị (phút)", min_value=1, max_value=60, value=STREAMLIT_RESAMPLE_MIN, step=1)
    refresh_sec = st.number_input("Refresh (giây)", min_value=5, max_value=600, value=STREAMLIT_REFRESH_SEC, step=5)

    st.write("---")
    if st.button("Refresh"):
        # clear cached queries and notify user to reload if needed
        try:
            st.cache_data.clear()
        except Exception:
            pass
        # experimental rerun may not exist; try and fallback
        try:
            st.experimental_rerun()
        except Exception:
            st.success("Đã xóa cache. Vui lòng làm mới (F5) trình duyệt nếu nội dung không thay đổi ngay.")
    st.write("---")
    st.caption("Dashboard chỉ đọc; không thay đổi dữ liệu của bot.")

# --- Load data ---
df = load_data(table_name, limit=int(max_rows))

# --- Resample/aggregate for plotting (gộp theo phút nếu cần) ---
def resample_df(df_in: pd.DataFrame, minutes: int = 5):
    if df_in.empty or "timestamp" not in df_in.columns or "price" not in df_in.columns:
        return df_in
    d = df_in.set_index("timestamp").copy()
    # resample: price -> last, funding_pct -> mean, oi -> last (or sum)
    agg = {}
    if "price" in d.columns:
        agg["price"] = "last"
    funding_col = next((c for c in ("funding_pct", "funding", "fund_rate") if c in d.columns), None)
    if funding_col:
        agg[funding_col] = "mean"
    oi_col = next((c for c in ("oi", "open_interest") if c in d.columns), None)
    if oi_col:
        agg[oi_col] = "last"
    try:
        r = d.resample(f"{minutes}T").agg(agg).dropna(how="all")
        r = r.reset_index()
        return r
    except Exception:
        return df_in

df_plot = resample_df(df, minutes=int(resample_min))

# --- Alerts load ---
alerts_df = load_alerts(alerts_table, limit=500)

# --- Top metrics ---
st.markdown("---")
st.subheader("Tổng quan nhanh")

if df.empty:
    st.warning("Chưa có dữ liệu trong bảng hoặc không thể đọc bảng. Kiểm tra tên bảng hoặc chờ vài phút.")
else:
    last = df.iloc[-1]
    col1, col2, col3 = st.columns([3,1,1])

    # Price
    try:
        col1.metric("Giá (mới nhất)", f"{float(last.get('price', 0)):.6f}")
    except Exception:
        col1.metric("Giá (mới nhất)", str(last.get("price", "n/a")))

    # Funding
    funding_val = None
    for key in ("funding_pct", "funding", "fund_rate"):
        if key in df.columns:
            funding_val = last.get(key)
            funding_col_name = key
            break
    funding_text = f"{float(funding_val):.6f}%" if funding_val is not None else "n/a"
    col2.metric("Funding (%)", funding_text)

    # OI
    oi_val = None
    for key in ("oi", "open_interest"):
        if key in df.columns:
            oi_val = last.get(key)
            break
    oi_text = f"{int(oi_val):,}" if (oi_val is not None and pd.notna(oi_val)) else "n/a"
    col3.metric("Open Interest", oi_text)

# --- Charts ---
st.markdown("---")
st.subheader(f"Biểu đồ: Giá / Funding / Open Interest (gần đây, gom {resample_min} phút)")

if not df_plot.empty:
    # Price
    if "price" in df_plot.columns:
        fig_price = px.line(df_plot, x="timestamp", y="price", title="Giá (Price)", height=320)
        st.plotly_chart(fig_price, use_container_width=True)
    else:
        st.info("Không tìm thấy cột `price` để vẽ biểu đồ giá.")

    # Funding
    funding_col = next((c for c in ("funding_pct", "funding", "fund_rate") if c in df_plot.columns), None)
    if funding_col:
        fig_f = px.line(df_plot, x="timestamp", y=funding_col, title="Funding (%)", height=220)
        st.plotly_chart(fig_f, use_container_width=True)

    # OI
    oi_col = next((c for c in ("oi", "open_interest") if c in df_plot.columns), None)
    if oi_col:
        fig_oi = px.line(df_plot, x="timestamp", y=oi_col, title="Open Interest", height=220)
        st.plotly_chart(fig_oi, use_container_width=True)
else:
    st.info("Đang chờ bot ghi dữ liệu vào bảng hoặc không có dữ liệu đủ để vẽ biểu đồ.")

# --- Alerts / giống Telegram ---
st.markdown("---")
st.subheader("Cảnh báo (Alerts) — giống Telegram")

if alerts_df.empty:
    st.info(f"Chưa có cảnh báo trong bảng `{alerts_table}` hoặc không đọc được bảng cảnh báo.")
else:
    # Show recent alerts compact
    # choose columns if exist
    show_cols = [c for c in ("ts", "kind", "tei", "price", "message") if c in alerts_df.columns]
    st.write("Hiển thị các cảnh báo mới nhất:")
    st.dataframe(alerts_df[show_cols].tail(50).reset_index(drop=True))

# --- Footer / info ---
st.markdown("---")
st.markdown("**Info / Khắc phục nhanh**")
st.write(f"- DB (detected): `{engine.url}`")
st.write(f"- Auto-refresh thư viện: {'sẵn sàng' if AUTORELOAD_AVAILABLE else 'không có (bấm Refresh thủ công)'}")
st.write("- Nếu thời gian hiển thị không đúng VN, kiểm tra kiểu cột `timestamp` (nên là timestamptz lưu UTC).")
st.write("- Dashboard này chỉ đọc dữ liệu; không thay đổi bot.")

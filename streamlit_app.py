# streamlit_app.py
"""
SUI TrapBot — Dashboard compact (UTC+7) - Tiếng Việt
Cập nhật 2025-11-11: loại bỏ experimental_set_query_params / experimental_rerun usage
- Chỉ đọc dữ liệu (read-only)
- Dùng pd.read_sql(sql, con=engine)
- Refresh bằng cache_bust + optional experimental_rerun nếu có
"""
import os
import time
from pathlib import Path
import warnings

import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import create_engine, text

# optional small autorefresh helper
try:
    from streamlit_autorefresh import st_autorefresh
    AUTORELOAD_AVAILABLE = True
except Exception:
    AUTORELOAD_AVAILABLE = False

# silence that repetitive pandas+SQLAlchemy warning message if present
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy connectable")

# --- Page layout ---
st.set_page_config(page_title="SUI TrapBot Dashboard", layout="wide")
st.markdown("""
<style>
h1 {font-size:28px;}
h2 {font-size:18px;}
.section {padding-top:6px;padding-bottom:6px;}
.sidebar .block-container {padding-top:0.75rem;}
footer {visibility:hidden;}
</style>
""", unsafe_allow_html=True)

st.title("📊 SUI TrapBot — Dashboard (UTC+7)")
st.caption("Bảng chỉ đọc — Giá / Funding / OI / Alerts (Tiếng Việt)")

# --- Config from env (optional) ---
STREAMLIT_REFRESH_SEC = int(os.getenv("STREAMLIT_REFRESH_SEC", "30"))   # default 30s
STREAMLIT_RESAMPLE_MIN = int(os.getenv("STREAMLIT_RESAMPLE_MIN", "5"))  # default 5 minutes
STREAMLIT_MAX_ROWS = int(os.getenv("STREAMLIT_MAX_ROWS", "2000"))

BASE_DIR = Path(__file__).resolve().parent

# --- DATABASE URL (Railway sets DATABASE_URL) ---
db_url = os.getenv("DATABASE_URL", "").strip()
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# --- Sidebar controls ---
with st.sidebar:
    st.header("Tùy chọn")
    st.caption("Dashboard chỉ đọc; không thay đổi dữ liệu bot.")
    table_name = st.text_input("Tên bảng dữ liệu", value="trapbot_data")
    alerts_table = st.text_input("Tên bảng cảnh báo", value="trapbot_alerts")
    max_rows = st.number_input("Số dòng tối đa lấy (max)", min_value=100, max_value=STREAMLIT_MAX_ROWS, value=1000, step=100)
    resample_min = st.number_input("Resample hiển thị (phút)", min_value=1, max_value=60, value=STREAMLIT_RESAMPLE_MIN, step=1)
    refresh_sec = st.number_input("Refresh (giây)", min_value=5, max_value=300, value=STREAMLIT_REFRESH_SEC, step=5)
    st.write("---")
    if st.button("Refresh"):
        # Force reload by bumping cache_bust and trying to rerun if supported
        st.session_state["cache_bust"] = st.session_state.get("cache_bust", 0) + 1
        # Try to rerun programmatically on Streamlit versions that support it; ignore errors
        try:
            if hasattr(st, "experimental_rerun"):
                st.experimental_rerun()
        except Exception:
            # fallback: just inform user to manually reload page if automatic rerun not supported
            st.info("Yêu cầu làm mới đã ghi nhận. Nếu trang không đổi, bấm reload trình duyệt.")
    st.write("---")
    st.markdown("**Gợi ý**: Nếu dashboard không load, kiểm tra `DATABASE_URL` trong Variables của service trên Railway.")

# Autorefresh if available (uses the refresh_sec variable)
if AUTORELOAD_AVAILABLE:
    st_autorefresh(interval=int(refresh_sec * 1000), key="auto_refresh")

# --- DB url check ---
if not db_url:
    st.error("❌ DATABASE_URL chưa được thiết lập. Vào Railway → Postgres → Credentials → copy DATABASE_URL và thêm vào Variables của service dashboard.")
    st.stop()

# --- Create SQLAlchemy engine (pass to pandas directly) ---
try:
    engine = create_engine(db_url, connect_args={"sslmode": "require"})
except Exception as e:
    st.error(f"Không thể tạo engine DB: {e}")
    st.stop()

# --- cache bust state ---
if "cache_bust" not in st.session_state:
    st.session_state["cache_bust"] = 0

# --- Helpers (cached) ---
@st.cache_data(ttl=15)
def list_tables_cached(cache_bust: int):
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

@st.cache_data(ttl=10)
def load_latest_table(table_name: str, limit: int, cache_bust: int):
    """Load latest rows using pandas.read_sql with engine"""
    try:
        limit = int(limit)
    except:
        limit = 500
    sql = f"SELECT * FROM public.{table_name} ORDER BY timestamp DESC LIMIT {limit}"
    try:
        df = pd.read_sql(sql, con=engine)
        if not df.empty and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Bangkok")
            df = df.sort_values("timestamp")
        return df
    except Exception as e:
        return {"__error__": str(e)}

@st.cache_data(ttl=10)
def load_alerts_table(table_name: str, limit: int, cache_bust: int):
    try:
        limit = int(limit)
    except:
        limit = 200
    sql = f"SELECT * FROM public.{table_name} ORDER BY ts DESC LIMIT {limit}"
    try:
        df = pd.read_sql(sql, con=engine)
        if not df.empty and "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Bangkok")
            df = df.sort_values("ts")
        return df
    except Exception as e:
        return {"__error__": str(e)}

# --- Show detected DB host & tables in sidebar ---
st.sidebar.markdown("---")
st.sidebar.markdown(f"DB (detected): `{engine.url}`")

detected = list_tables_cached(st.session_state["cache_bust"])
if detected:
    st.sidebar.markdown("**Tables detected:**")
    st.sidebar.write(", ".join(detected))

# --- Load data ---
loaded = load_latest_table(table_name, max_rows, st.session_state["cache_bust"])
if isinstance(loaded, dict) and "__error__" in loaded:
    df = pd.DataFrame()
    st.warning(f"Lỗi đọc bảng `{table_name}`: {loaded['__error__']}")
else:
    df = loaded

loaded_alerts = load_alerts_table(alerts_table, 200, st.session_state["cache_bust"])
if isinstance(loaded_alerts, dict) and "__error__" in loaded_alerts:
    alerts_df = pd.DataFrame()
    alerts_error = loaded_alerts["__error__"]
else:
    alerts_df = loaded_alerts
    alerts_error = None

st.markdown("---")

# --- Status messages ---
if df.empty:
    st.info(f"Chưa có dữ liệu hoặc không thể đọc bảng `{table_name}`. Kiểm tra tên bảng hoặc chờ vài phút.")
else:
    st.success(f"Đã tải dữ liệu: {len(df)} dòng — mới nhất: {df['timestamp'].iloc[-1]}")

if alerts_error:
    st.info(f"Không đọc được bảng cảnh báo `{alerts_table}` (có thể không tồn tại): {alerts_error}")
elif alerts_df.empty:
    st.info(f"Chưa có cảnh báo trong `{alerts_table}`.")
else:
    st.success(f"Đã tải {len(alerts_df)} cảnh báo từ `{alerts_table}`")

st.markdown("---")

# --- Quick metrics ---
st.header("Tổng quan nhanh")
if not df.empty:
    last = df.iloc[-1]
    c1, c2, c3 = st.columns([3,1,1])
    # Price
    try:
        price_val = float(last.get("price", 0))
        c1.metric("Giá (mới nhất)", f"{price_val:.6f}")
    except Exception:
        c1.metric("Giá (mới nhất)", str(last.get("price", "n/a")))
    # Funding
    funding_val = None
    for k in ("funding_pct", "funding", "fund_rate"):
        if k in df.columns:
            funding_val = last.get(k)
            break
    funding_text = f"{float(funding_val):.6f}%" if funding_val is not None else "n/a"
    c2.metric("Funding (%)", funding_text)
    # OI
    oi_val = last.get("oi") if "oi" in df.columns else last.get("open_interest", None)
    oi_text = f"{int(oi_val):,}" if (oi_val is not None and pd.notna(oi_val)) else "n/a"
    c3.metric("Open Interest", oi_text)
else:
    st.info("Không có dữ liệu để hiển thị metrics.")

st.markdown("---")

# --- Charts (resample per user's resample_min) ---
st.subheader(f"Biểu đồ: Giá / Funding / Open Interest (gần đây, gom {resample_min} phút)")
if not df.empty and "timestamp" in df.columns:
    dfi = df.set_index("timestamp").copy()
    if resample_min and resample_min > 1:
        try:
            dfr = dfi.resample(f"{resample_min}T").mean().dropna(how="all")
        except Exception:
            dfr = dfi
    else:
        dfr = dfi

    # Price
    if "price" in dfr.columns:
        fig_price = px.line(dfr.reset_index(), x="timestamp", y="price", title="Giá", height=320)
        st.plotly_chart(fig_price, use_container_width=True)
    else:
        st.info("Bảng không chứa cột 'price' để vẽ biểu đồ.")

    # Funding
    funding_col = next((c for c in ("funding_pct", "funding", "fund_rate") if c in dfr.columns), None)
    if funding_col:
        fig_f = px.line(dfr.reset_index(), x="timestamp", y=funding_col, title="Funding (%)", height=220)
        st.plotly_chart(fig_f, use_container_width=True)

    # OI
    oi_col = next((c for c in ("oi", "open_interest") if c in dfr.columns), None)
    if oi_col:
        fig_oi = px.line(dfr.reset_index(), x="timestamp", y=oi_col, title="Open Interest", height=220)
        st.plotly_chart(fig_oi, use_container_width=True)
else:
    st.info("Đang chờ bot ghi dữ liệu vào bảng hoặc bảng không có cột 'timestamp'.")

st.markdown("---")

# --- Alerts display ---
st.subheader("Cảnh báo (Alerts) — giống Telegram")
if alerts_df is None or (isinstance(alerts_df, pd.DataFrame) and alerts_df.empty):
    st.info(f"Chưa có cảnh báo ({alerts_table}) hoặc không thể đọc bảng alerts.")
else:
    max_alert_show = min(50, len(alerts_df))
    for idx, row in alerts_df.sort_values(by="ts", ascending=False).head(max_alert_show).iterrows():
        ts = row.get("ts")
        kind = row.get("kind", "UNKNOWN")
        tei = row.get("tei", "")
        price = row.get("price", "")
        message = row.get("message", "")
        with st.expander(f"{ts} — {kind} — TEI: {tei} — Giá: {price}", expanded=False):
            st.write(message)
            if "meta" in row and pd.notna(row["meta"]):
                st.json(row["meta"])

st.markdown("---")

st.markdown("**Info / Troubleshoot**")
st.markdown(f"- DB (detected): `{engine.url}`")
st.markdown("- Nếu thời gian không đúng VN, kiểm tra kiểu cột `timestamp` (nên là timestamptz lưu UTC).")
st.markdown("- Dashboard chỉ đọc; không thay đổi dữ liệu bot.")

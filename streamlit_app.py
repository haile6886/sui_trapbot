# streamlit_app.py
"""
SUI TrapBot — Dashboard compact (UTC+7) - Tiếng Việt
- Chỉ đọc dữ liệu (read-only)
- Hiển thị Price / Funding / OI + Alerts (nếu có)
- Cấu hình: STREAMLIT_REFRESH_SEC, STREAMLIT_RESAMPLE_MIN, STREAMLIT_MAX_ROWS (env hoặc UI)
"""
import os
import time
from pathlib import Path
import warnings

# UX / plotting
import streamlit as st
import pandas as pd
import plotly.express as px

# DB
from sqlalchemy import create_engine, text

# optional tiny autorefresh helper (not required)
try:
    from streamlit_autorefresh import st_autorefresh
    AUTORELOAD_AVAILABLE = True
except Exception:
    AUTORELOAD_AVAILABLE = False

# ---- Silence a noisy pandas+SQLAlchemy warning (optional) ----
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy connectable")
# you can remove the above line if you prefer to keep warnings

# --- Page config ---
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

BASE_DIR = Path(__file__).resolve().parent

# --- Read defaults from environment (optional) ---
STREAMLIT_REFRESH_SEC = int(os.getenv("STREAMLIT_REFRESH_SEC", "30"))  # seconds for autorefresh (UI shows)
STREAMLIT_RESAMPLE_MIN = int(os.getenv("STREAMLIT_RESAMPLE_MIN", "5"))  # minutes for resample display
STREAMLIT_MAX_ROWS = int(os.getenv("STREAMLIT_MAX_ROWS", "2000"))

# --- DB url (Railway sets DATABASE_URL normally) ---
db_url = os.getenv("DATABASE_URL", "").strip()
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# --- Sidebar (controls) ---
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
        # bump session cache key
        st.session_state["cache_bust"] = st.session_state.get("cache_bust", 0) + 1
        # show small info
        st.experimental_set_query_params(_refresh=int(time.time()))
        st.experimental_rerun = getattr(st, "experimental_rerun", None)  # safe-guard; may not exist
    st.write("---")
    st.markdown("**Gợi ý**: Nếu dashboard không load dữ liệu, kiểm tra `DATABASE_URL` trong Variables của service trên Railway.")

# --- DB connection check ---
if not db_url:
    st.error("❌ DATABASE_URL chưa được thiết lập. Vào Railway → Postgres → Credentials → copy DATABASE_URL và thêm vào Variables của service dashboard.")
    st.stop()

# create engine
try:
    engine = create_engine(db_url, connect_args={"sslmode": "require"})
except Exception as e:
    st.error(f"Không thể tạo engine DB: {e}")
    st.stop()

# --- caching helper: cache_bust param forces reload when incremented ---
if "cache_bust" not in st.session_state:
    st.session_state["cache_bust"] = 0

@st.cache_data(ttl=10)
def list_tables_cached(cache_bust):
    q = """
    SELECT tablename
    FROM pg_catalog.pg_tables
    WHERE schemaname NOT IN ('pg_catalog','information_schema');
    """
    try:
        with engine.connect() as conn:
            res = conn.execute(text(q))
            rows = [r[0] for r in res.fetchall()]
            return rows
    except Exception:
        return []

@st.cache_data(ttl=10)
def load_latest(table_name: str, limit: int, cache_bust: int):
    """Load latest rows (ORDER BY timestamp DESC) using engine directly (pandas with SQLAlchemy)"""
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
        # return empty DataFrame but preserve error message
        return {"__error__": str(e)}

@st.cache_data(ttl=10)
def load_alerts(table_name: str, limit: int, cache_bust: int):
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

# --- show detected DB host ---
st.sidebar.markdown("---")
st.sidebar.markdown(f"DB host (detected): `{engine.url}`")

# --- show detected tables (button) ---
detected = list_tables_cached(st.session_state["cache_bust"])
if detected:
    st.sidebar.markdown("**Tables detected:**")
    st.sidebar.write(", ".join(detected))

# --- Load data (main) ---
loaded = load_latest(table_name, max_rows, st.session_state["cache_bust"])
if isinstance(loaded, dict) and "__error__" in loaded:
    st.warning(f"Lỗi đọc bảng {table_name}: {loaded['__error__']}")
    df = pd.DataFrame()
else:
    df = loaded

loaded_alerts = load_alerts(alerts_table, 200, st.session_state["cache_bust"])
if isinstance(loaded_alerts, dict) and "__error__" in loaded_alerts:
    alerts_df = pd.DataFrame()
    alerts_error = loaded_alerts["__error__"]
else:
    alerts_df = loaded_alerts
    alerts_error = None

# --- Top messages / status ---
if df.empty:
    st.info(f"Chưa có dữ liệu trong bảng hoặc không thể đọc bảng. Kiểm tra tên bảng `{table_name}` hoặc chờ vài phút.")
else:
    st.success(f"Đã tải dữ liệu: {len(df)} dòng (bảng `{table_name}`) — thời gian mới nhất: {df['timestamp'].iloc[-1]}")

if alerts_error:
    st.info(f"Không đọc được bảng cảnh báo {alerts_table} (có thể không tồn tại): {alerts_error}")
elif alerts_df.empty:
    st.info(f"Chưa có cảnh báo trong `{alerts_table}` hoặc bảng rỗng.")
else:
    st.success(f"Đã tải {len(alerts_df)} cảnh báo từ `{alerts_table}`")

st.markdown("---")

# --- Quick metrics (last value) ---
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

    # Funding: possible column names
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
    st.info("Chưa có dữ liệu để hiển thị metrics.")

st.markdown("---")

# --- Charts: Price / Funding / OI (resample optional) ---
st.subheader(f"Biểu đồ: Giá / Funding / Open Interest (gần đây, gom {resample_min} phút)")

if not df.empty and "timestamp" in df.columns:
    # set index for resampling
    dfi = df.set_index("timestamp").copy()
    # if user requests resample > 1min, do group/resample
    if resample_min and resample_min > 1:
        try:
            dfr = dfi.resample(f"{resample_min}T").mean().dropna(how="all")
        except Exception:
            dfr = dfi
    else:
        dfr = dfi

    # Price chart
    if "price" in dfr.columns:
        fig_price = px.line(dfr.reset_index(), x="timestamp", y="price", title="Giá", height=320)
        st.plotly_chart(fig_price, use_container_width=True)
    else:
        st.info("Bảng dữ liệu không chứa cột 'price' để vẽ biểu đồ.")

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

# --- Alerts section (display similar to Telegram) ---
st.subheader("Cảnh báo (Alerts) — giống Telegram")
if alerts_df is None or (isinstance(alerts_df, pd.DataFrame) and alerts_df.empty):
    st.info(f"Chưa có cảnh báo ({alerts_table} trống) hoặc không thể đọc bảng alerts.")
else:
    # show last 20 alerts
    max_alert_show = min(50, len(alerts_df))
    for idx, row in alerts_df.sort_values(by="ts", ascending=False).head(max_alert_show).iterrows():
        ts = row.get("ts")
        kind = row.get("kind", "UNKNOWN")
        tei = row.get("tei", "")
        price = row.get("price", "")
        message = row.get("message", "")
        with st.expander(f"{ts} — {kind} — TEI: {tei} — Giá: {price}", expanded=False):
            st.write(message)
            # show metadata if exists
            if "meta" in row and pd.notna(row["meta"]):
                st.json(row["meta"])

st.markdown("---")

# --- Footer / Tips ---
st.markdown("**Info / Troubleshoot**")
st.markdown(f"- DB (detected): `{engine.url}`")
st.markdown("- Nếu thời gian hiển thị không đúng VN, kiểm tra kiểu cột `timestamp` (nên dùng timestamptz lưu UTC).")
st.markdown("- Dashboard đọc dữ liệu; không thay đổi dữ liệu của bot.")

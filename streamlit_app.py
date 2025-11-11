# streamlit_app.py — SUI TrapBot Dashboard (Hoàn chỉnh, tiếng Việt)
import os
import time
from pathlib import Path
from datetime import timezone

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import create_engine

# optional auto-refresh helper (không bắt buộc)
try:
    from streamlit_autorefresh import st_autorefresh
    AUTORELOAD_AVAILABLE = True
except Exception:
    AUTORELOAD_AVAILABLE = False

# --- PAGE / STYLE ---
st.set_page_config(page_title="SUI TrapBot — Dashboard", layout="wide")
st.markdown("""
<style>
.stApp .block-container{padding-top:0.6rem;}
.alert-card{border-radius:8px;padding:10px;margin-bottom:8px;box-shadow:0 1px 2px rgba(0,0,0,0.05);}
.alert-strong{border-left:6px solid #ff4d4f;}
.alert-warning{border-left:6px solid #ffa940;}
.alert-info{border-left:6px solid #2f54eb;}
footer{visibility:hidden;}
.monospace{font-family:monospace;}
</style>
""", unsafe_allow_html=True)

st.title("📊 SUI TrapBot — Dashboard (UTC+7)")
st.caption("Bảng chỉ đọc — Giá / Funding / OI / Alerts (Tiếng Việt)")

# --- CONFIG from ENV (có thể override ở Railway Variables) ---
STREAMLIT_REFRESH_SEC = int(os.getenv("STREAMLIT_REFRESH_SEC", "30"))
STREAMLIT_RESAMPLE_MIN = int(os.getenv("STREAMLIT_RESAMPLE_MIN", "5"))
STREAMLIT_MAX_ROWS = int(os.getenv("STREAMLIT_MAX_ROWS", "2000"))

# auto refresh nếu thư viện có sẵn
if AUTORELOAD_AVAILABLE:
    st_autorefresh(interval=STREAMLIT_REFRESH_SEC * 1000, key="auto_refresh")
else:
    st.info(f"Tự động refresh không khả dụng. Bấm 'Refresh' để cập nhật (mặc định {STREAMLIT_REFRESH_SEC}s).")

# --- DATABASE URL ---
db_url = os.getenv("DATABASE_URL", "").strip()
if not db_url:
    st.error("❌ DATABASE_URL chưa thiết lập. Vào Railway → service → Variables → thêm DATABASE_URL (postgres URL).")
    st.stop()

# modern SQLAlchemy expects postgresql://
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# --- session state for cache busting ---
if "cache_bust" not in st.session_state:
    st.session_state["cache_bust"] = 0

# --- engine tạo cached resource ---
@st.cache_resource
def get_engine(url):
    # Nếu chạy trên Railway (rlwy) thì dùng sslmode=require
    connect_args = {"sslmode": "require"} if "rlwy" in url or "railway" in url else {}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)

# thử kết nối nhanh
try:
    engine = get_engine(db_url)
    with engine.connect() as conn:
        conn.execute("SELECT 1")
except Exception as e:
    st.error(f"Không thể kết nối CSDL: {e}")
    st.stop()

# --- Data loaders with cache that respects cache_bust ---
@st.cache_data(ttl=15)
def load_data(table="trapbot_data", limit=1000, cache_bust=0):
    """Load latest rows from trapbot_data. Returns DataFrame with timestamp tz-converted to Asia/Bangkok."""
    try:
        limit = int(limit)
        sql = f"SELECT * FROM public.{table} ORDER BY timestamp DESC LIMIT {limit}"
        df = pd.read_sql_query(sql, engine)
        if not df.empty and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Bangkok")
            df = df.sort_values("timestamp")
        return df
    except Exception as e:
        # trả DataFrame rỗng nếu lỗi, và log thông báo
        st.warning(f"Lỗi khi đọc bảng `{table}`: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=15)
def load_alerts(limit=200, cache_bust=0):
    try:
        limit = int(limit)
        sql = f"SELECT * FROM public.trapbot_alerts ORDER BY ts DESC LIMIT {limit}"
        df = pd.read_sql_query(sql, engine)
        if not df.empty and "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Bangkok")
            df = df.sort_values("ts")
        return df
    except Exception:
        return pd.DataFrame()

# --- SIDEBAR: controls (tiếng Việt) ---
with st.sidebar:
    st.header("Tùy chọn hiển thị")
    table_name = st.text_input("Tên bảng dữ liệu", value="trapbot_data")
    alerts_table = st.text_input("Tên bảng cảnh báo", value="trapbot_alerts")
    max_rows = st.number_input("Số dòng tối đa", min_value=100, max_value=STREAMLIT_MAX_ROWS, value=1000, step=100)
    resample_min = st.number_input("Gom theo (phút)", min_value=1, max_value=60, value=STREAMLIT_RESAMPLE_MIN, step=1)
    if st.button("Refresh"):
        st.session_state["cache_bust"] += 1
        st.success("Đã cập nhật cache — dữ liệu sẽ load lại.")
    st.write("---")
    st.caption("Dashboard chỉ đọc — không thay đổi dữ liệu bot.")
    st.markdown("- Các biến có thể thiết lập bằng Railway Variables:\n  - `STREAMLIT_REFRESH_SEC` (giây)\n  - `STREAMLIT_RESAMPLE_MIN` (phút)\n  - `STREAMLIT_MAX_ROWS`")

# --- Load data ---
df = load_data(table=table_name, limit=max_rows, cache_bust=st.session_state["cache_bust"])
alerts_df = load_alerts(limit=500, cache_bust=st.session_state["cache_bust"])

# --- Top metrics ---
col1, col2, col3 = st.columns([3, 1, 1])
if not df.empty:
    last = df.iloc[-1]
    try:
        col1.metric("Giá hiện tại", f"{float(last['price']):.6f}")
    except Exception:
        col1.metric("Giá hiện tại", str(last.get("price", "n/a")))
    # funding
    funding_col = next((c for c in ("funding_pct", "funding", "fund_rate") if c in df.columns), None)
    if funding_col:
        try:
            col2.metric("Funding (%)", f"{float(last[funding_col]):.6f}%")
        except Exception:
            col2.metric("Funding (%)", str(last.get(funding_col, "n/a")))
    else:
        col2.metric("Funding (%)", "n/a")
    # OI
    oi_col = next((c for c in ("oi", "open_interest") if c in df.columns), None)
    if oi_col:
        try:
            col3.metric("Open Interest", f"{int(last[oi_col]):,}")
        except Exception:
            col3.metric("Open Interest", str(last.get(oi_col, "n/a")))
    else:
        col3.metric("Open Interest", "n/a")
else:
    st.warning("Chưa có dữ liệu trong bảng hoặc không đọc được. Kiểm tra tên bảng và đợi bot ghi dữ liệu.")

# --- Charts ---
st.markdown("---")
st.subheader("Biểu đồ: Giá / Funding / Open Interest (gần đây)")
if not df.empty and "timestamp" in df.columns:
    chart_df = df.copy()
    # resample theo phút nếu yêu cầu
    if resample_min and int(resample_min) > 1:
        try:
            chart_df = chart_df.set_index("timestamp").resample(f"{int(resample_min)}T").last().dropna().reset_index()
        except Exception:
            # nếu resample lỗi thì dùng bản gốc
            chart_df = df.copy()
    # price
    if "price" in chart_df.columns:
        fig_price = px.line(chart_df, x="timestamp", y="price", title="Giá", height=320)
        st.plotly_chart(fig_price, use_container_width=True)
    # funding
    funding_col = next((c for c in ("funding_pct", "funding", "fund_rate") if c in chart_df.columns), None)
    if funding_col:
        fig_f = px.line(chart_df, x="timestamp", y=funding_col, title="Funding (%)", height=220)
        st.plotly_chart(fig_f, use_container_width=True)
    # oi
    oi_col = next((c for c in ("oi", "open_interest") if c in chart_df.columns), None)
    if oi_col:
        fig_oi = px.line(chart_df, x="timestamp", y=oi_col, title="Open Interest", height=220)
        st.plotly_chart(fig_oi, use_container_width=True)
else:
    st.info("Đang chờ dữ liệu từ bot (bảng chưa có hoặc chưa đọc được).")

# --- Alerts area ---
st.markdown("---")
st.subheader("Cảnh báo (Alerts) — giống Telegram")
if alerts_df.empty:
    st.info("Chưa có cảnh báo (bảng `trapbot_alerts` rỗng hoặc không tồn tại).")
else:
    # show recent alerts (mới nhất ở trên)
    for _, row in alerts_df.tail(100).iterrows():
        kind = str(row.get("kind", ""))
        tsi = row.get("ts", "")
        tei = row.get("tei", "")
        price = row.get("price", "")
        funding = row.get("funding_pct", row.get("funding", ""))
        msg = row.get("message", "")
        cls = "alert-card alert-info"
        if "BREAKOUT" in kind:
            cls = "alert-card alert-warning"
        if "FAKE" in kind or "EXTREME" in kind:
            cls = "alert-card alert-strong"
        st.markdown(f"""
            <div class="{cls}">
                <strong>{kind}</strong> | TEI: {tei} | Giá: {price} | Funding: {funding}
                <br><span style="font-size:0.9em;color:#666;">{tsi}</span>
                <pre style="white-space:pre-wrap;margin-top:6px;">{msg}</pre>
            </div>
        """, unsafe_allow_html=True)

# --- Debug / Info footer ---
st.markdown("---")
st.subheader("Thông tin kiểm tra nhanh")
try:
    st.write(f"- DB kết nối: `{engine.url}`")
except Exception:
    st.write("- DB kết nối: (không xác định)")
st.write(f"- Dòng đọc được: Dữ liệu={len(df)} | Alerts={len(alerts_df)}")
st.caption("Dashboard chỉ đọc, không ghi dữ liệu. Nếu cần thay đổi tham số bot, chỉnh file monitor_adaptive.py và redeploy bot service.")


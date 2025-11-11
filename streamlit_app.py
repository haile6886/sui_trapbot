# streamlit_app.py
"""
SUI TrapBot — Dashboard compact (UTC+7) — Vietnamese
Fixed version:
 - Avoids st.experimental_rerun (compatibility)
 - Uses SQLAlchemy engine directly with pandas.read_sql to avoid warnings
 - Adds cache_bust mechanism via st.session_state to force reload
 - Displays alerts safely and cleanly
 - Auto-refresh optional via streamlit_autorefresh if available
"""
import os
import time
from datetime import timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# Optional autorefresh helper
try:
    from streamlit_autorefresh import st_autorefresh
    AUTORELOAD_AVAILABLE = True
except Exception:
    AUTORELOAD_AVAILABLE = False

# --- Config / Defaults from env ---
BASE_DIR = Path(__file__).resolve().parent
st.set_page_config(page_title="SUI TrapBot — Dashboard", layout="wide")

# Environment-configurable params
STREAMLIT_REFRESH_SEC = int(os.getenv("STREAMLIT_REFRESH_SEC", "30"))        # default 30s
STREAMLIT_RESAMPLE_MIN = int(os.getenv("STREAMLIT_RESAMPLE_MIN", "5"))      # resample window in minutes
STREAMLIT_MAX_ROWS = int(os.getenv("STREAMLIT_MAX_ROWS", "2000"))           # max rows to fetch

st.markdown(
    """
    <style>
    .stApp .block-container{padding-top:0.5rem;}
    .alert-card {border-radius:10px;padding:12px;margin-bottom:8px;box-shadow:0 1px 2px rgba(0,0,0,0.06);}
    .alert-strong {border-left:6px solid #ff4d4f;}
    .alert-warning {border-left:6px solid #ffa940;}
    .alert-info {border-left:6px solid #2f54eb;}
    .monospace {font-family: monospace;}
    footer {visibility:hidden;}
    pre {white-space: pre-wrap; word-wrap: break-word;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📊 SUI TrapBot — Dashboard (UTC+7)")
st.caption("Bảng điều khiển chỉ đọc — Giá / Funding / OI / Alerts (Tiếng Việt)")

# Auto refresh
if AUTORELOAD_AVAILABLE:
    st_autorefresh(interval=STREAMLIT_REFRESH_SEC * 1000, key="auto_refresh")
else:
    st.info(f"Tự động refresh không khả dụng. Bấm nút 'Refresh' để cập nhật. (Mặc định {STREAMLIT_REFRESH_SEC}s)")

# --- DB URL & engine creation (cached) ---
db_url = os.getenv("DATABASE_URL", "").strip()
if not db_url:
    st.error("❌ DATABASE_URL chưa thiết lập. Vào Railway → Service empowering_hope → Variables → thêm DATABASE_URL.")
    st.stop()

# ensure scheme correctness for SQLAlchemy
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

@st.cache_resource
def get_engine(url):
    try:
        connect_args = {}
        # If using proxy/public url we require ssl
        if "switchback.proxy.rlwy.net" in url or (":40354" in url and "rlwy" in url):
            connect_args = {"sslmode": "require"}
        engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        # quick test
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception as e:
        st.error(f"Không thể kết nối DB: {e}")
        return None

engine = get_engine(db_url)
if engine is None:
    st.stop()

# session state for manual refresh cache busting
if "cache_bust" not in st.session_state:
    st.session_state["cache_bust"] = int(time.time())

# --- Helpers to load data (cached short TTL) ---
@st.cache_data(ttl=10)
def load_trapbot_data(table_name: str = "trapbot_data", limit: int = 1000, cache_bust: int = 0):
    sql = f"SELECT * FROM public.{table_name} ORDER BY timestamp DESC LIMIT {int(limit)}"
    try:
        # pass engine (SQLAlchemy connectable) to pandas to avoid warnings
        df = pd.read_sql(sql, con=engine)
        if df.empty:
            return df
        # convert timezone to Asia/Bangkok
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Bangkok")
        return df.sort_values("timestamp")
    except Exception as e:
        # return empty df on error; not to crash UI
        st.error(f"Lỗi đọc bảng `{table_name}`: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=10)
def load_alerts(table_name: str = "trapbot_alerts", limit: int = 200, cache_bust: int = 0):
    sql = f"SELECT * FROM public.{table_name} ORDER BY ts DESC LIMIT {int(limit)}"
    try:
        df = pd.read_sql(sql, con=engine)
        if df.empty:
            return df
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Bangkok")
        return df.sort_values("ts")
    except Exception:
        return pd.DataFrame()

# --- Sidebar controls (Vietnamese) ---
with st.sidebar:
    st.header("Tùy chọn")
    table_name = st.text_input("Tên bảng dữ liệu", value="trapbot_data")
    alerts_table = st.text_input("Tên bảng cảnh báo", value="trapbot_alerts")
    max_rows = st.number_input("Số dòng tối đa lấy (max)", min_value=100, max_value=STREAMLIT_MAX_ROWS, value=1000, step=100)
    resample_min = st.number_input("Resample hiển thị (phút)", min_value=1, max_value=60, value=STREAMLIT_RESAMPLE_MIN, step=1)
    if st.button("Refresh"):
        # increment cache_bust to force cached functions to reload
        st.session_state["cache_bust"] = int(time.time())
        st.success("Đã làm mới dữ liệu (cache_bust updated).")
    st.write("---")
    st.caption("Dashboard chỉ đọc; không thay đổi dữ liệu của bot.")
    st.write("- STREAMLIT_REFRESH_SEC:", STREAMLIT_REFRESH_SEC)
    st.write("- STREAMLIT_RESAMPLE_MIN:", resample_min)

# --- Load data ---
df = load_trapbot_data(table_name=table_name, limit=max_rows, cache_bust=st.session_state["cache_bust"])
alerts_df = load_alerts(table_name=alerts_table, limit=200, cache_bust=st.session_state["cache_bust"])

# --- Top metrics area ---
col_left, col_mid, col_right = st.columns([3,1,1])
if not df.empty:
    last = df.iloc[-1]
    try:
        price_s = f"{float(last.get('price', 0)):.6f}"
    except Exception:
        price_s = str(last.get("price", "n/a"))
    col_left.metric("Giá (mới nhất)", price_s)
    # funding - detect column name
    funding_col = next((c for c in ("funding_pct", "funding", "fund_rate") if c in df.columns), None)
    if funding_col:
        try:
            funding_val = float(last.get(funding_col, 0.0))
            col_mid.metric("Funding (%)", f"{funding_val:.6f}%")
        except Exception:
            col_mid.metric("Funding (%)", str(last.get(funding_col, "n/a")))
    else:
        col_mid.metric("Funding (%)", "n/a")
    # OI
    oi_col = next((c for c in ("oi", "open_interest") if c in df.columns), None)
    if oi_col:
        try:
            oi_val = int(last.get(oi_col)) if pd.notna(last.get(oi_col)) else None
            col_right.metric("Open Interest", f"{oi_val:,}" if oi_val is not None else "n/a")
        except Exception:
            col_right.metric("Open Interest", str(last.get(oi_col, "n/a")))
else:
    col_left.info("Chưa có dữ liệu trong bảng hoặc không thể đọc.")

# --- Main charts ---
st.markdown("---")
st.subheader("Biểu đồ: Giá / Funding / Open Interest (gần đây)")
if not df.empty and "timestamp" in df.columns:
    plot_df = df.copy()
    # optional resample: downsample to per resample_min minute by taking last value within bucket
    try:
        if resample_min > 1:
            plot_df = plot_df.set_index("timestamp").resample(f"{resample_min}T").last().dropna().reset_index()
    except Exception:
        plot_df = plot_df.sort_values("timestamp")
    # Price
    if "price" in plot_df.columns:
        fig_price = px.line(plot_df, x="timestamp", y="price", title="Price", height=320)
        fig_price.update_xaxes(rangeslider_visible=True)
        st.plotly_chart(fig_price, use_container_width=True)
    # Funding
    funding_col = next((c for c in ("funding_pct", "funding", "fund_rate") if c in plot_df.columns), None)
    if funding_col:
        fig_f = px.line(plot_df, x="timestamp", y=funding_col, title="Funding (%)", height=240)
        st.plotly_chart(fig_f, use_container_width=True)
    # OI
    oi_col = next((c for c in ("oi", "open_interest") if c in plot_df.columns), None)
    if oi_col:
        fig_oi = px.line(plot_df, x="timestamp", y=oi_col, title="Open Interest", height=240)
        st.plotly_chart(fig_oi, use_container_width=True)
else:
    st.info("Đang chờ dữ liệu từ bot (trapbot_data). Kiểm tra tên bảng hoặc chờ vài phút.")

# --- Alerts panel (like Telegram) ---
st.markdown("---")
st.subheader("Cảnh báo (Alerts) — giống Telegram")
if alerts_df is None or alerts_df.empty:
    st.info("Chưa có cảnh báo (trapbot_alerts trống) hoặc không thể đọc bảng alerts.")
else:
    alerts_df = alerts_df.sort_values("ts", ascending=False).reset_index(drop=True)
    top_n = min(50, len(alerts_df))
    for i in range(top_n):
        row = alerts_df.iloc[i]
        ts = row.get("ts")
        kind = str(row.get("kind", "")).upper()
        tei = row.get("tei", "")
        price = row.get("price", "")
        funding_pct = row.get("funding_pct", "")
        oi_val = row.get("oi", "")
        msg = row.get("message", "") or ""
        # determine class
        cls = "alert-card alert-info"
        if "BREAKOUT" in kind or "FUNDING" in kind or "OI_SPIKE" in kind:
            cls = "alert-card alert-warning"
        if "FAKE" in kind or "EXTREME" in kind:
            cls = "alert-card alert-strong"
        # render
        oi_display = f"{int(oi_val):,}" if (oi_val is not None and pd.notna(oi_val)) else ""
        inner_html = f"""
        <div class="{cls}">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div><strong>{kind}</strong> &nbsp; <span class="monospace">TEI: {tei}</span></div>
            <div style="text-align:right;font-size:0.9rem;color:#666;">{ts}</div>
          </div>
          <div style="margin-top:6px;font-size:0.95rem;color:#111;">
            <div><strong>Giá:</strong> {price} &nbsp; <strong>Funding:</strong> {funding_pct} &nbsp; <strong>OI:</strong> {oi_display}</div>
            <div style="margin-top:6px;"><pre>{st._legacy_compat.resolve_component_value(msg) if hasattr(st, '_legacy_compat') else msg}</pre></div>
          </div>
        </div>
        """
        # fallback: if st._legacy_compat not present, render msg raw inside <pre>
        if hasattr(st, '_legacy_compat'):
            st.markdown(inner_html, unsafe_allow_html=True)
        else:
            # avoid using unknown internals; simply escape by wrapping in <pre>
            safe_html = inner_html.replace("{st._legacy_compat.resolve_component_value(msg) if hasattr(st, '_legacy_compat') else msg}", msg)
            st.markdown(safe_html, unsafe_allow_html=True)

# --- Raw Data / Troubleshoot section ---
st.markdown("---")
st.subheader("Dữ liệu thô / Kiểm tra")
with st.expander("Xem nhanh các bảng (dòng cuối)"):
    try:
        st.write("DB host:", engine.url)
    except Exception:
        st.write("DB host: (không xác định)")
    st.write(f"Đã load {len(df)} dòng từ `{table_name}`.")
    st.write(f"Đã load {len(alerts_df)} dòng từ `{alerts_table}`.")
    if not df.empty:
        st.dataframe(df.tail(50).reset_index(drop=True))
    if not alerts_df.empty:
        st.dataframe(alerts_df.head(100).reset_index(drop=True))

st.markdown("---")
st.caption("Ghi chú: Dashboard chỉ đọc. Nếu cần filter hoặc highlight theo loại cảnh báo, mình sẽ bổ sung.")

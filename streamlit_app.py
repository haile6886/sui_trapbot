# streamlit_app.py
"""
SUI TrapBot — Dashboard (Compact, UTC+7) with Alerts table + filters + export
- Read-only dashboard (không ghi DB)
- Tùy chọn refresh, resample, số dòng tối đa
- Hiển thị alerts giống Telegram, có filter và export CSV
"""
import os
from pathlib import Path
from datetime import timezone
import io

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import create_engine

# --- CONFIG ---
BASE_DIR = Path(__file__).resolve().parent
st.set_page_config(page_title="SUI TrapBot — Dashboard", layout="wide")
st.markdown("""
<style>
h1 {font-size:28px;}
.sidebar .block-container {padding-top:0.6rem;}
footer {visibility:hidden;}
.alert-card{border-radius:8px;padding:10px;margin-bottom:8px;box-shadow:0 1px 2px rgba(0,0,0,0.04);}
.alert-strong{border-left:6px solid #ff4d4f;}
.alert-warning{border-left:6px solid #ffa940;}
.alert-info{border-left:6px solid #2f54eb;}
.monospace{font-family:monospace;}
</style>
""", unsafe_allow_html=True)

st.title("📊 SUI TrapBot — Dashboard (UTC+7)")
st.caption("Bảng chỉ đọc — Giá / Funding / OI / Alerts (Tiếng Việt)")

# ENV configs (Railway Variables)
STREAMLIT_REFRESH_SEC = int(os.getenv("STREAMLIT_REFRESH_SEC", "30"))
STREAMLIT_RESAMPLE_MIN = int(os.getenv("STREAMLIT_RESAMPLE_MIN", "5"))
STREAMLIT_MAX_ROWS = int(os.getenv("STREAMLIT_MAX_ROWS", "2000"))

# Sidebar
with st.sidebar:
    st.header("Tùy chọn")
    db_url = st.text_input("DATABASE_URL (hoặc để trống để dùng env)", value=os.getenv("DATABASE_URL", ""))
    if db_url == "":
        db_url = os.getenv("DATABASE_URL", "")
    table_name = st.text_input("Tên bảng dữ liệu", value="trapbot_data")
    alerts_table = st.text_input("Tên bảng cảnh báo", value="trapbot_alerts")
    max_rows = st.number_input("Số dòng tối đa (max)", min_value=100, max_value=STREAMLIT_MAX_ROWS, value=1000, step=100)
    resample_min = st.number_input("Gom theo (phút)", min_value=1, max_value=60, value=STREAMLIT_RESAMPLE_MIN, step=1)
    if st.button("Refresh"):
        st.experimental_rerun()
    st.write("---")
    st.caption("Dashboard chỉ đọc — không ghi dữ liệu bot.")
    st.markdown("- Rail env vars có thể set: `STREAMLIT_REFRESH_SEC`, `STREAMLIT_RESAMPLE_MIN`, `STREAMLIT_MAX_ROWS`")

# Validate DB URL
if not db_url:
    st.error("DATABASE_URL chưa thiết lập. Vào Railway → service → Variables → thêm DATABASE_URL.")
    st.stop()

if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# Create engine
try:
    engine = create_engine(db_url, connect_args={"sslmode":"require"} if "railway" in db_url or "rlwy" in db_url else {})
except Exception as e:
    st.error(f"Không thể tạo engine DB: {e}")
    st.stop()

# --- Helpers ---
@st.cache_data(ttl=20)
def load_data(table, limit=1000):
    try:
        sql = f"SELECT * FROM public.{table} ORDER BY timestamp DESC LIMIT {int(limit)}"
        df = pd.read_sql_query(sql, engine)
        if not df.empty and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Bangkok")
            df = df.sort_values("timestamp")
        return df
    except Exception as e:
        st.warning(f"Lỗi đọc bảng {table}: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=20)
def load_alerts(table, limit=500):
    try:
        sql = f"SELECT * FROM public.{table} ORDER BY ts DESC LIMIT {int(limit)}"
        df = pd.read_sql_query(sql, engine)
        if not df.empty and "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Bangkok")
            df = df.sort_values("ts")
        return df
    except Exception as e:
        st.warning(f"Lỗi đọc bảng {table}: {e}")
        return pd.DataFrame()

# Load
df = load_data(table_name, limit=max_rows)
alerts = load_alerts(alerts_table, limit=500)

# Top metrics
col1,col2,col3 = st.columns([3,1,1])
if not df.empty:
    last = df.iloc[-1]
    try:
        col1.metric("Giá hiện tại", f"{float(last['price']):.6f}")
    except Exception:
        col1.metric("Giá hiện tại", str(last.get("price","n/a")))
    funding_col = next((c for c in ("funding_pct","funding","fund_rate") if c in df.columns), None)
    if funding_col:
        col2.metric("Funding (%)", f"{float(last[funding_col]):.6f}%")
    else:
        col2.metric("Funding (%)","n/a")
    oi_col = next((c for c in ("oi","open_interest") if c in df.columns), None)
    if oi_col:
        col3.metric("Open Interest", f"{int(last[oi_col]):,}")
    else:
        col3.metric("Open Interest","n/a")
else:
    st.warning("Chưa có dữ liệu trong bảng hoặc không thể đọc.")

# Charts
st.markdown("---")
st.subheader("Biểu đồ: Giá / Funding / Open Interest (gần đây)")
if not df.empty and "timestamp" in df.columns:
    chart_df = df.copy()
    if resample_min and int(resample_min)>1:
        try:
            chart_df = chart_df.set_index("timestamp").resample(f"{int(resample_min)}T").last().dropna().reset_index()
        except Exception:
            chart_df = df.copy()
    if "price" in chart_df.columns:
        fig_price = px.line(chart_df, x="timestamp", y="price", title="Giá (gần đây)", height=320)
        st.plotly_chart(fig_price, use_container_width=True)
    funding_col = next((c for c in ("funding_pct","funding","fund_rate") if c in chart_df.columns), None)
    if funding_col:
        fig_f = px.line(chart_df, x="timestamp", y=funding_col, title="Funding (%)", height=220)
        st.plotly_chart(fig_f, use_container_width=True)
    oi_col = next((c for c in ("oi","open_interest") if c in chart_df.columns), None)
    if oi_col:
        fig_oi = px.line(chart_df, x="timestamp", y=oi_col, title="Open Interest", height=220)
        st.plotly_chart(fig_oi, use_container_width=True)
else:
    st.info("Đang chờ dữ liệu từ bot (bảng chưa có hoặc chưa đọc được).")

# Alerts area with filters + export
st.markdown("---")
st.subheader("Cảnh báo (Alerts) — giống Telegram")

if alerts.empty:
    st.info("Chưa có cảnh báo (bảng rỗng hoặc không tồn tại).")
else:
    # Filters
    f_col1, f_col2 = st.columns([2,3])
    with f_col1:
        kind_filter = st.text_input("Lọc theo loại cảnh báo (contains)", value="")
        min_tei = st.number_input("TEI tối thiểu", min_value=0, max_value=100, value=0)
    with f_col2:
        date_from = st.date_input("Từ ngày", value=None)
        date_to = st.date_input("Đến ngày", value=None)
    filtered = alerts.copy()
    if kind_filter:
        filtered = filtered[filtered["kind"].str.contains(kind_filter, na=False, case=False)]
    if "tei" in filtered.columns:
        filtered = filtered[filtered["tei"].fillna(0) >= int(min_tei)]
    if date_from:
        filtered = filtered[filtered["ts"].dt.date >= date_from]
    if date_to:
        filtered = filtered[filtered["ts"].dt.date <= date_to]
    st.write(f"Hiển thị {len(filtered)} cảnh báo (mới nhất ở dưới).")
    # Show table and allow export
    st.dataframe(filtered.sort_values("ts").reset_index(drop=True), height=300)
    csv_buf = io.StringIO()
    filtered.to_csv(csv_buf, index=False)
    st.download_button("Export CSV", data=csv_buf.getvalue(), file_name="trapbot_alerts.csv", mime="text/csv")

    # Also render formatted messages (recent)
    st.markdown("**Danh sách cảnh báo (mới nhất ở trên)**")
    for _, row in filtered.tail(100).iterrows():
        kind = str(row.get("kind",""))
        tsi = row.get("ts","")
        tei = row.get("tei","")
        price = row.get("price","")
        funding = row.get("funding_pct", row.get("funding",""))
        msg = row.get("message","")
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

# Footer debug
st.markdown("---")
st.subheader("Thông tin kiểm tra nhanh")
try:
    st.write(f"- DB kết nối: `{engine.url}`")
except Exception:
    st.write("- DB kết nối: (không xác định)")
st.write(f"- Dòng đọc được: data={len(df)} | alerts={len(alerts)}")
st.caption("Dashboard chỉ đọc; nếu cần thay đổi tham số bot, chỉnh monitor_adaptive.py và redeploy bot service.")

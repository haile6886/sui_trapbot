# streamlit_app.py
"""
SUI TrapBot — Dashboard compact (UTC+7)
- Read-only: không ghi gì vào DB
- Hiển thị toàn bộ trên một màn hình (wide)
- Dữ liệu gom theo N phút (mặc định 5 phút)
- Alerts cục bộ (hiển thị giống Telegram, tiếng Việt)
- Auto-refresh configurable
"""
import os
import time
import math
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from urllib.parse import urlparse

# Optional fast autorefresh
try:
    from streamlit_autorefresh import st_autorefresh
    AUTORELOAD_AVAILABLE = True
except Exception:
    AUTORELOAD_AVAILABLE = False

# --- Config & UI setup ---
BASE_DIR = Path(__file__).resolve().parent
st.set_page_config(page_title="SUI TrapBot — Dashboard", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
h1 {font-size:26px;}
h2 {font-size:18px;}
footer {visibility:hidden;}
</style>
""", unsafe_allow_html=True)

# Env defaults (có thể override bằng Railway Variables)
REFRESH_SEC = int(os.getenv("STREAMLIT_REFRESH_SEC", "30"))      # thời gian refresh trang (giây)
RESAMPLE_MIN = int(os.getenv("STREAMLIT_RESAMPLE_MIN", "5"))    # gom theo phút (mặc định 5)
MAX_ROWS = int(os.getenv("STREAMLIT_MAX_ROWS", "2000"))         # số dòng lấy từ DB
ALERT_FUND_SIGMA = float(os.getenv("ALERT_FUND_SIGMA", "2.0"))  # ngưỡng z-score funding
ALERT_OI_SIGMA = float(os.getenv("ALERT_OI_SIGMA", "2.0"))      # ngưỡng z-score OI

# Auto refresh (try library first, fallback meta-refresh)
if AUTORELOAD_AVAILABLE:
    st_autorefresh(interval=REFRESH_SEC * 1000, key="auto_refresh")

# fallback meta refresh (works in plain browsers)
st.markdown(f"<meta http-equiv='refresh' content='{REFRESH_SEC}'>", unsafe_allow_html=True)

# Header
st.title("📊 SUI TrapBot — Dashboard (Compact, UTC+7)")
st.caption(f"Tự động làm mới mỗi {REFRESH_SEC}s — Biểu đồ gom theo {RESAMPLE_MIN} phút — {time.strftime('%Y-%m-%d %H:%M:%S')}")

# --- DB connect ---
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    st.error("❌ DATABASE_URL chưa được thiết lập. Vui lòng thêm biến môi trường DATABASE_URL = ${Postgres-mCs3.DATABASE_URL} trong Railway.")
    st.stop()

# normalize scheme if needed
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# show DB host (không lộ password)
try:
    u = urlparse(DATABASE_URL)
    st.sidebar.write("DB host:", u.hostname, "port:", u.port)
except Exception:
    pass

@st.experimental_singleton(show_spinner=False)
def get_engine(url):
    try:
        eng = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})
        # quick smoke test
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return eng
    except Exception as e:
        st.sidebar.error(f"Kết nối DB lỗi: {e}")
        return None

engine = get_engine(DATABASE_URL)
if engine is None:
    st.stop()

# --- Sidebar controls (người dùng có thể thay tham số tại runtime) ---
with st.sidebar:
    st.header("Tùy chỉnh")
    table_name = st.text_input("Tên bảng (table)", value="trapbot_data")
    refresh_sec = st.number_input("Refresh (giây)", min_value=5, max_value=600, value=REFRESH_SEC, step=5)
    resample_min = st.number_input("Gom theo (phút)", min_value=1, max_value=60, value=RESAMPLE_MIN, step=1)
    max_rows = st.number_input("Số dòng tối đa", min_value=100, max_value=10000, value=MAX_ROWS, step=100)
    alert_f_sigma = st.number_input("Ngưỡng Funding z (sigma)", min_value=0.5, max_value=6.0, value=ALERT_FUND_SIGMA, step=0.1)
    alert_oi_sigma = st.number_input("Ngưỡng OI z (sigma)", min_value=0.5, max_value=6.0, value=ALERT_OI_SIGMA, step=0.1)
    if st.button("Làm mới (force)"):
        st.experimental_rerun()
    st.write("---")
    st.caption("Dashboard chỉ đọc dữ liệu. Để trigger retrain, dùng SQL thủ công.")

# apply sidebar overrides
REFRESH_SEC = int(refresh_sec)
RESAMPLE_MIN = int(resample_min)
MAX_ROWS = int(max_rows)
ALERT_FUND_SIGMA = float(alert_f_sigma)
ALERT_OI_SIGMA = float(alert_oi_sigma)

# --- Data loading (cache ngắn để giảm load DB) ---
@st.cache_data(ttl=REFRESH_SEC)
def load_data(table: str, limit: int):
    q = text(f"SELECT * FROM public.{table} ORDER BY timestamp DESC LIMIT :lim")
    try:
        with engine.connect() as conn:
            df = pd.read_sql(q, conn, params={"lim": limit})
        if df.empty:
            return df
        # convert timestamp to tz Asia/Bangkok for display & resampling (assume timestamptz/UTC stored)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Bangkok")
        df = df.sort_values("timestamp")
        return df
    except Exception as e:
        st.sidebar.error(f"Query lỗi: {e}")
        return pd.DataFrame()

df = load_data(table_name, MAX_ROWS)

# --- Top metrics (hiển thị trên 1 hàng) ---
if df.empty:
    st.warning("Chưa có dữ liệu hoặc không đọc được bảng. Kiểm tra tên bảng / chờ bot ghi dữ liệu.")
else:
    last = df.iloc[-1]
    c1, c2, c3, c4, c5 = st.columns([1.4,1.2,1.2,1.2,1.2])

    # Giá (price)
    price_val = last.get("price") if "price" in df.columns else None
    if pd.notna(price_val):
        c1.metric("Giá (mark)", f"{float(price_val):.6f}")
    else:
        c1.metric("Giá (mark)", "n/a")

    # current_price (nếu có)
    if "current_price" in df.columns:
        cp = last.get("current_price")
        c2.metric("Live (current_price)", f"{float(cp):.6f}" if pd.notna(cp) else "n/a")
    else:
        c2.metric("Live (current_price)", "Không có cột current_price")

    # Funding
    funding_col = next((c for c in ("funding_pct","funding","fund_rate") if c in df.columns), None)
    if funding_col:
        fval = last.get(funding_col)
        c3.metric("Funding (%)", f"{float(fval):.6f}%")
    else:
        c3.metric("Funding (%)", "n/a")

    # OI
    oi_col = next((c for c in ("oi","open_interest") if c in df.columns), None)
    if oi_col:
        oval = last.get(oi_col)
        c4.metric("Open Interest", f"{int(oval):,}" if pd.notna(oval) else "n/a")
    else:
        c4.metric("Open Interest", "n/a")

    # Last timestamp
    ts_display = pd.to_datetime(last["timestamp"]).strftime("%Y-%m-%d %H:%M:%S %Z")
    c5.metric("Bản ghi cuối (Asia/Bangkok)", ts_display)

st.markdown("---")

# --- Prepare resampled data for charts (gom theo RESAMPLE_MIN phút) ---
if not df.empty:
    df_chart = df.set_index("timestamp").copy()
    # chọn cột an toàn
    cols_to_keep = []
    for c in ("price","current_price","funding_pct","funding","fund_rate","oi","open_interest"):
        if c in df_chart.columns:
            cols_to_keep.append(c)
    # normalize column names for charts
    if "funding" in df_chart.columns and "funding_pct" not in df_chart.columns:
        df_chart = df_chart.rename(columns={"funding":"funding_pct"})
    if "open_interest" in df_chart.columns and "oi" not in df_chart.columns:
        df_chart = df_chart.rename(columns={"open_interest":"oi"})

    try:
        df_res = df_chart.resample(f"{RESAMPLE_MIN}T").last().dropna(how="all")
    except Exception:
        df_res = df_chart.copy()

    left, right = st.columns([2.2,1])

    with left:
        st.subheader(f"Giá & Live (gom {RESAMPLE_MIN} phút)")
        if "price" in df_res.columns or "current_price" in df_res.columns:
            cp = df_res[["price","current_price"]].copy() if "current_price" in df_res.columns else df_res[["price"]].copy()
            st.line_chart(cp)
        else:
            st.info("Không đủ dữ liệu giá để vẽ biểu đồ.")

        st.subheader("Funding (%)")
        if "funding_pct" in df_res.columns:
            st.line_chart(df_res[["funding_pct"]])
        else:
            st.info("Không có dữ liệu Funding để vẽ.")

        st.subheader("Open Interest (OI)")
        if "oi" in df_res.columns:
            st.line_chart(df_res[["oi"]])
        else:
            st.info("Không có dữ liệu OI để vẽ.")

    with right:
        st.subheader("Alerts (phát hiện cục bộ — tiếng Việt)")
        # đơn giản: tính mean/std trên window recent
        window = min(len(df), max(50, int(60 / max(1, RESAMPLE_MIN))))  # chọn window tương đối
        recent = df.tail(window).set_index("timestamp")
        alerts = []
        try:
            if "funding_pct" in recent.columns:
                fm = recent["funding_pct"].mean()
                fs = recent["funding_pct"].std(ddof=0) if recent["funding_pct"].std(ddof=0) > 0 else 1e-9
            else:
                fm, fs = 0.0, 1e9
            if "oi" in recent.columns:
                om = recent["oi"].mean()
                osd = recent["oi"].std(ddof=0) if recent["oi"].std(ddof=0) > 0 else 1e-9
            else:
                om, osd = 0.0, 1e9

            # kiểm tra các mẫu cuối (5 mẫu cuối)
            check_rows = recent.tail(6)
            for idx, row in check_rows.iterrows():
                ts_s = idx.strftime("%Y-%m-%d %H:%M:%S")
                if "funding_pct" in row.index and pd.notna(row["funding_pct"]):
                    zf = (row["funding_pct"] - fm) / (fs + 1e-12)
                    if abs(zf) >= ALERT_FUND_SIGMA:
                        emoji = "🔴" if abs(zf) >= 2.5 else "🟠"
                        txt = f"⚠️ FUNDING BẤT THƯỜNG {emoji}\n- Thời gian: {ts_s}\n- Funding: {row['funding_pct']:.6f}% | z={zf:.2f}"
                        alerts.append(("FUNDING", zf, txt, idx))
                if "oi" in row.index and pd.notna(row["oi"]):
                    zo = (row["oi"] - om) / (osd + 1e-12)
                    if abs(zo) >= ALERT_OI_SIGMA:
                        emoji = "🔴" if abs(zo) >= 2.5 else "🟠"
                        txt = f"⚠️ OI BẤT THƯỜNG {emoji}\n- Thời gian: {ts_s}\n- OI: {int(row['oi']):,} | z={zo:.2f}"
                        alerts.append(("OI", zo, txt, idx))
        except Exception as e:
            st.warning(f"Lỗi khi tính alerts: {e}")

        if alerts:
            # sort by time desc
            alerts_sorted = sorted(alerts, key=lambda x: x[3], reverse=True)
            for a in alerts_sorted:
                _, z, msg, _ = a
                st.markdown(f"**{msg.splitlines()[1].split(':',1)[1].strip()}**")  # show time line
                st.info(msg)
        else:
            st.success("Không phát hiện alert bất thường (theo kiểm tra cục bộ).")

        st.markdown("---")
        st.subheader("Bản ghi cuối (gần nhất)")
        show_n = min(30, len(df))
        tail = df.tail(show_n).copy()
        tail["timestamp"] = tail["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
        display_cols = [c for c in ("id","timestamp","price","current_price","funding_pct","oi") if c in tail.columns]
        st.dataframe(tail[display_cols], use_container_width=True, height=520)

# --- Footer: info & troubleshooting ---
st.markdown("---")
st.markdown("**Thông tin / Khắc phục nhanh**")
st.write(f"- DB (kết nối): `{engine.url}`")
st.write("- Nếu thời gian hiển thị không đúng VN, kiểm tra kiểu cột `timestamp` (nên là timestamptz lưu UTC).")
st.write("- Dashboard chỉ đọc dữ liệu; không thay đổi bot.")

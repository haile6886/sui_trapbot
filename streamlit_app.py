# streamlit_app.py (extended)
import os
import json
import math
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from datetime import timedelta

# ---------- Config ----------
PAGE_TITLE = "SUI TrapBot — Dashboard (UTC+7)"
st.set_page_config(page_title=PAGE_TITLE, layout="wide")

# ---------- Helpers ----------
def safe_create_engine(db_url):
    try:
        eng = create_engine(db_url)
        return eng
    except Exception as e:
        st.error(f"Không thể tạo engine: {e}")
        return None

@st.cache_data(ttl=30)
def load_table_df(engine, table_name, limit=1000):
    """Load table into pandas DataFrame using SQLAlchemy engine"""
    if engine is None:
        return None, "No engine"
    try:
        with engine.connect() as conn:
            q = text(f"SELECT * FROM {table_name} ORDER BY id DESC LIMIT :lim")
            df = pd.read_sql(q, conn, params={"lim": limit})
        if df is None or df.empty:
            return pd.DataFrame(), None
        return df, None
    except Exception as e:
        return None, str(e)

def show_masked_dburl(url):
    if not url:
        return "Not set"
    try:
        if "@" in url:
            parts = url.split("@")
            cred = parts[0]
            hide = cred.split("//")[-1]
            return url.replace(hide, "****")
    except:
        pass
    return "masked"

def ensure_timestamp(df, col="timestamp"):
    if col in df.columns:
        df[col] = pd.to_datetime(df[col], errors="coerce")
    return df

def resample_df(df, col="timestamp", rule="5T"):
    if df is None or df.empty:
        return df
    df = df.set_index(col)
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    if not numeric_cols:
        return df
    return df[numeric_cols].resample(rule).last().ffill()

# ---------- Read ENV and create engine ----------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    st.sidebar.error("❌ DATABASE_URL chưa thiết lập. Vào Railway → Postgres → Credentials → copy DATABASE_URL và thêm vào Variables của service dashboard.")
engine = safe_create_engine(DATABASE_URL) if DATABASE_URL else None

# ---------- Sidebar controls ----------
st.sidebar.title("Tuỳ chọn")
table_data = st.sidebar.text_input("Tên bảng dữ liệu", value="trapbot_data")
table_alerts = st.sidebar.text_input("Tên bảng cảnh báo", value="trapbot_alerts")
limit_rows = st.sidebar.number_input("Số dòng tối đa lấy", min_value=10, max_value=10000, value=1000, step=10)
resample_min = st.sidebar.number_input("Resample (phút)", min_value=1, max_value=60, value=5)
auto_refresh = st.sidebar.checkbox("Auto refresh (client)", value=False)
refresh_sec = st.sidebar.number_input("Refresh interval (giây)", min_value=5, max_value=600, value=30)
st.sidebar.markdown("---")
st.sidebar.write("DB detected:")
st.sidebar.code(show_masked_dburl(DATABASE_URL))
st.sidebar.write("Tables detected: (tự động phát hiện nếu có)")
# attempt to show table names (best-effort)
try:
    if engine is not None:
        with engine.connect() as conn:
            r = conn.execute(text("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'")).fetchall()
            names = [x[0] for x in r]
            st.sidebar.write(", ".join(names))
except Exception:
    pass

# ---------- Page header ----------
st.title(PAGE_TITLE)
st.caption("Dashboard chỉ đọc — không thay đổi dữ liệu bot. Nếu không thấy dữ liệu, kiểm tra biến môi trường DATABASE_URL và tên bảng.")

# ---------- Tabs ----------
tabs = st.tabs(["Overview", "Charts", "Alerts", "Raw data", "Diagnostics"])

# ---------- Overview ----------
with tabs[0]:
    st.header("Tổng quan nhanh")
    df, err = load_table_df(engine, table_data, limit=limit_rows)
    if err:
        st.warning(f"Không thể load bảng dữ liệu `{table_data}`: {err}")
    if df is None or df.empty:
        st.info("Chưa có dữ liệu trong bảng.")
    else:
        df = ensure_timestamp(df, "timestamp")
        latest = df.sort_values("id", ascending=False).iloc[0]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Dòng hiện tại", f"{len(df):,}")
        try:
            col2.metric("Giá mới nhất", f"{float(latest.get('price',0)):.6f}")
        except:
            col2.metric("Giá mới nhất", str(latest.get("price", "")))
        col3.metric("Funding mới nhất", f"{latest.get('funding_pct', '')}")
        col4.metric("OI mới nhất", f"{latest.get('oi', ''):,}")
        st.markdown("---")
        # small table summary
        st.subheader("10 dòng gần nhất")
        st.dataframe(df.sort_values("id", ascending=False).head(10), use_container_width=True)

# ---------- Charts ----------
with tabs[1]:
    st.header("Biểu đồ: Giá / Funding / OI")
    df_ch, err_ch = load_table_df(engine, table_data, limit=max(5000, limit_rows))
    if df_ch is None or df_ch.empty:
        st.info("Không có dữ liệu để vẽ biểu đồ.")
    else:
        df_ch = ensure_timestamp(df_ch, "timestamp")
        # convert timezone if needed (assume stored in UTC)
        # df_ch['timestamp'] = df_ch['timestamp'].dt.tz_convert('Asia/Bangkok')  # optional
        rule = f"{int(resample_min)}T"
        df_res = resample_df(df_ch, "timestamp", rule)
        if df_res is None or df_res.empty:
            st.info("Không có dữ liệu số để vẽ.")
        else:
            st.line_chart(df_res[["price"]].rename(columns={"price":"Price"}), height=300, use_container_width=True)
            # two smaller charts side-by-side
            c1, c2 = st.columns(2)
            with c1:
                if "funding_pct" in df_res.columns:
                    st.area_chart(df_res[["funding_pct"]], height=200, use_container_width=True)
                else:
                    st.write("No funding data")
            with c2:
                if "oi" in df_res.columns:
                    st.bar_chart(df_res[["oi"]], height=200, use_container_width=True)
                else:
                    st.write("No OI data")

# ---------- Alerts ----------
with tabs[2]:
    st.header("Alerts / Cảnh báo")
    alerts_df, alerts_err = load_table_df(engine, table_alerts, limit=200)
    if alerts_err:
        st.warning(f"Lỗi khi đọc bảng alerts `{table_alerts}`: {alerts_err}")
    if alerts_df is None or alerts_df.empty:
        st.info("Không có cảnh báo (hoặc bảng alerts rỗng).")
    else:
        alerts_df = ensure_timestamp(alerts_df, "ts")
        # show last 50 alerts with parsed content
        st.subheader("Last alerts")
        show_df = alerts_df.sort_values("id", ascending=False).head(50)
        # human-readable columns
        def pretty_row(r):
            ts = r.get("ts", "")
            kind = r.get("kind","")
            msg = r.get("message","")
            meta = r.get("meta","")
            try:
                meta_j = json.dumps(meta, ensure_ascii=False) if meta else ""
            except:
                meta_j = str(meta)
            return {"ts": str(ts), "kind": kind, "message": msg, "meta": meta_j}
        pretty = pd.DataFrame([pretty_row(r) for _, r in show_df.iterrows()])
        st.dataframe(pretty, use_container_width=True)

# ---------- Raw data ----------
with tabs[3]:
    st.header("Raw data / Table view")
    df_raw, raw_err = load_table_df(engine, table_data, limit=limit_rows)
    if raw_err:
        st.error(f"Lỗi khi đọc `{table_data}`: {raw_err}")
    if df_raw is None or df_raw.empty:
        st.info("Không có dữ liệu.")
    else:
        df_raw = ensure_timestamp(df_raw, "timestamp")
        page_size = st.selectbox("Số dòng / trang", [10, 25, 50, 100, 500], index=2)
        # simple pagination
        total = len(df_raw)
        pages = math.ceil(total / page_size)
        page = st.number_input("Trang", min_value=1, max_value=max(1,pages), value=1)
        start = (page-1)*page_size
        end = start + page_size
        st.write(f"Hiển thị {start+1}–{min(end,total)} trên {total}")
        st.dataframe(df_raw.sort_values("id", ascending=False).iloc[start:end], use_container_width=True)
        # export csv
        csv = df_raw.to_csv(index=False).encode("utf-8")
        st.download_button("Tải CSV toàn bộ (cẩn trọng)", data=csv, file_name=f"{table_data}.csv", mime="text/csv")

# ---------- Diagnostics ----------
with tabs[4]:
    st.header("Diagnostics / Logs")
    st.write("DATABASE_URL (masked):")
    st.code(show_masked_dburl(DATABASE_URL))
    # show sample latest 10 rows (json lines)
    try:
        with engine.connect() as conn:
            q = text(f"SELECT id, timestamp, symbol, price, created_at FROM {table_data} ORDER BY id DESC LIMIT 10")
            rows = conn.execute(q).fetchall()
            if not rows:
                st.info("No rows in table.")
            else:
                st.subheader("10 dòng gần nhất (raw)")
                for r in rows:
                    st.write(dict(id=r[0], timestamp=str(r[1]), symbol=r[2], price=str(r[3]), created_at=str(r[4])))
    except Exception as e:
        st.warning(f"Cannot query sample rows: {e}")

# ---------- Auto refresh logic (client) ----------
if auto_refresh:
    st.experimental_rerun()

# ===== End of file =====

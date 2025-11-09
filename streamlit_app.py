# streamlit_app.py — improved safe dashboard for SUI TrapBot
import os
import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
import json
from datetime import datetime

st.set_page_config(page_title="SUI TrapBot Dashboard", layout="wide")
st.title("📊 SUI TrapBot — Dashboard")
st.caption("Realtime view: Price / Funding / OI | Trigger retrain | Read-only (safe)")

# Optional simple token protection:
# If you set env var DASHBOARD_TOKEN, users must open URL like: https://.../?token=YOUR_TOKEN
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
if DASHBOARD_TOKEN:
    params = st.experimental_get_query_params()
    if "token" not in params or params["token"][0] != DASHBOARD_TOKEN:
        st.warning("Dashboard is protected. Provide ?token=... in URL.")
        st.stop()

# DB connect
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    st.error("DATABASE_URL chưa được thiết lập trong Railway Variables.")
    st.info("Vào Railway → Postgres → Credentials → copy DATABASE_URL và thêm vào Variables cho service dashboard.")
    st.stop()

@st.cache_resource
def get_engine():
    try:
        # short connect timeout may be driver-specific; this is generic
        return create_engine(DATABASE_URL, future=True)
    except Exception as e:
        st.error(f"Không thể tạo DB engine: {e}")
        raise

engine = get_engine()

# Helper: try several likely table names
POSSIBLE_TABLES = ["trapbot_data", "market", "data_log", "market_data", "market_logs"]

def detect_table(engine):
    with engine.connect() as conn:
        try:
            res = conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            ))
            tables = [r[0] for r in res.fetchall()]
            for t in POSSIBLE_TABLES:
                if t in tables:
                    return t
            # fallback: choose a table that has numeric price & funding columns
            for t in tables:
                try:
                    r = conn.execute(text(f"SELECT * FROM {t} LIMIT 1")).fetchone()
                    if not r:
                        continue
                    cols = [c[0].lower() for c in r._mapping.items()] if hasattr(r, "_mapping") else []
                except Exception:
                    cols = []
                # heuristics
                if any(c in cols for c in ("price","funding","oi","timestamp","ts")):
                    return t
        except Exception:
            return None
    return None

def load_df(engine, table_name, limit=1000):
    # Try reading and normalize columns
    try:
        q = text(f"SELECT * FROM {table_name} ORDER BY id DESC LIMIT :lim")
        df = pd.read_sql(q, engine, params={"lim": limit})
    except Exception:
        # fallback to order by timestamp if id not present
        try:
            q = text(f"SELECT * FROM {table_name} ORDER BY timestamp DESC LIMIT :lim")
            df = pd.read_sql(q, engine, params={"lim": limit})
        except Exception as e:
            raise

    # normalize timestamp column
    if "ts" in df.columns:
        df["timestamp"] = pd.to_datetime(df["ts"], errors="coerce")
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    # common rename attempts
    if "funding_pct" in df.columns and "funding" not in df.columns:
        df["funding"] = df["funding_pct"]
    if "funding_pct" not in df.columns and "funding" in df.columns:
        df["funding_pct"] = df["funding"]
    return df

# UI: left panel controls, right panel content
col_left, col_right = st.columns([1, 3])

with col_left:
    st.header("Controls")
    table_name = st.button("Detect table automatically")
    if st.button("Refresh data"):
        st.experimental_rerun()
    retrain_now = st.button("Trigger retrain (bot will pick up)")
    st.markdown("---")
    st.write("Info:")
    st.write(f"- DB host: `{DATABASE_URL.split('@')[-1][:80]}`")
    st.write(f"- Detected token protection: {'ON' if DASHBOARD_TOKEN else 'OFF'}")

# detect table
detected_table = None
try:
    detected_table = detect_table(engine)
except Exception as e:
    st.error(f"Error detecting tables: {e}")

if not detected_table:
    st.warning("Chưa tìm thấy bảng hợp lệ trong DB. Kiểm tra tên bảng hoặc chờ bot ghi dữ liệu.")
    # show list of tables for debugging
    try:
        with engine.connect() as conn:
            res = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"))
            st.write("Tables in DB:", [r[0] for r in res.fetchall()])
    except Exception:
        pass
    st.stop()

# load data
try:
    df = load_df(engine, detected_table, limit=2000)
except Exception as e:
    st.error(f"Lỗi khi đọc dữ liệu từ `{detected_table}`: {e}")
    st.stop()

if df is None or df.empty:
    st.info("Bảng trống. Hãy để bot chạy vài vòng để có dữ liệu.")
    st.stop()

# Right column: charts + metrics
with col_right:
    st.header("Realtime Metrics")
    latest = df.iloc[0]
    price_val = latest.get("price") or latest.get("mark") or latest.get("lastPrice") or None
    funding_val = latest.get("funding") or latest.get("funding_pct") or latest.get("funding_rate") or 0.0
    oi_val = latest.get("oi") or latest.get("openInterest") or 0

    c1, c2, c3 = st.columns(3)
    try:
        c1.metric("Price (last)", f"{float(price_val):.6f}" if price_val is not None else "n/a")
    except:
        c1.metric("Price (last)", "n/a")
    try:
        c2.metric("Funding (%)", f"{float(funding_val):.6f}%")
    except:
        c2.metric("Funding (%)", str(funding_val))
    try:
        c3.metric("Open Interest", f"{int(oi_val):,}")
    except:
        c3.metric("Open Interest", str(oi_val))

    st.markdown("---")
    st.subheader("Price / Funding / OI (recent)")
    plot_df = df.copy()
    if "timestamp" in plot_df.columns:
        plot_df = plot_df.sort_values("timestamp")
        if "price" in plot_df.columns:
            fig_price = px.line(plot_df, x="timestamp", y="price", title="Price")
            st.plotly_chart(fig_price, use_container_width=True)
        if "funding" in plot_df.columns:
            fig_f = px.line(plot_df, x="timestamp", y="funding", title="Funding (%)")
            st.plotly_chart(fig_f, use_container_width=True)
        if "oi" in plot_df.columns:
            fig_o = px.line(plot_df, x="timestamp", y="oi", title="Open Interest")
            st.plotly_chart(fig_o, use_container_width=True)
    else:
        st.write("Không có cột timestamp để vẽ biểu đồ.")

    st.markdown("---")
    st.subheader("Raw recent data")
    st.dataframe(plot_df.head(200))

# Trigger retrain: write a request row into model_state (bot checks it)
if retrain_now:
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO model_state (key, value)
                VALUES ('retrain_request', :v)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
            """), {"v": json.dumps({"requested_at": datetime.utcnow().isoformat()})})
        st.success("Retrain requested; bot sẽ pick up và retrain (nếu bot support retrain_request).")
    except SQLAlchemyError as e:
        st.error(f"Lỗi khi tạo retrain request: {e}")

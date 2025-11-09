import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import create_engine
import os

# --- Kết nối Database ---
db_url = os.getenv("DATABASE_URL")
engine = create_engine(db_url)

st.set_page_config(page_title="SUI TrapBot Dashboard", layout="wide")

st.title("📊 SUI TrapBot – Dashboard Theo Dõi Thị Trường")
st.caption("Theo dõi Funding, OI, Price và TEI realtime (từ Railway PostgreSQL)")

# --- Đọc dữ liệu ---
@st.cache_data(ttl=60)
def load_data():
    try:
        df = pd.read_sql("SELECT * FROM market ORDER BY id DESC LIMIT 5000", engine)
        df["ts"] = pd.to_datetime(df["ts"])
        return df
    except Exception as e:
        st.error(f"Lỗi khi đọc dữ liệu: {e}")
        return pd.DataFrame()

df = load_data()

if df.empty:
    st.warning("⚠️ Chưa có dữ liệu nào trong database. Hãy để bot chạy thêm một thời gian.")
else:
    # --- Hiển thị số liệu tổng ---
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("🪙 Giá gần nhất", f"{df['price'].iloc[-1]:.4f}")
    col2.metric("📈 Funding (%)", f"{df['funding_pct'].iloc[-1]:.4f}")
    col3.metric("💼 Open Interest", f"{int(df['oi'].iloc[-1]):,}")
    col4.metric("⏱️ Dòng dữ liệu", f"{len(df)} bản ghi")

    # --- Biểu đồ ---
    st.subheader("Biểu đồ Funding và OI")
    fig1 = px.line(df, x="ts", y="funding_pct", title="Funding Rate (%)")
    fig2 = px.line(df, x="ts", y="oi", title="Open Interest")

    st.plotly_chart(fig1, use_container_width=True)
    st.plotly_chart(fig2, use_container_width=True)

    st.subheader("Biểu đồ Giá (Price)")
    fig3 = px.line(df, x="ts", y="price", title="Biến động Giá SUI/USDT")
    st.plotly_chart(fig3, use_container_width=True)

    st.subheader("📄 Dữ liệu gần nhất")
    st.dataframe(df.tail(50))

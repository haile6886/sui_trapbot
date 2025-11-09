# streamlit_app.py
import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import create_engine
import os

st.set_page_config(page_title="SUI TrapBot Dashboard", layout="wide")

st.title("📊 SUI TrapBot Realtime Dashboard")
st.write("Theo dõi Funding, OI và TEI từ hệ thống bot SUI TrapBot (v5.9 Smart Follow-Up & Pro Alert+)")

# Lấy DATABASE_URL từ biến môi trường
db_url = os.getenv("DATABASE_URL")

if not db_url:
    st.error("❌ DATABASE_URL chưa được thiết lập trong Railway Variables.")
else:
    try:
        engine = create_engine(db_url)
        df = pd.read_sql("SELECT * FROM trapbot_data ORDER BY timestamp DESC LIMIT 500", engine)

        st.success("✅ Kết nối database thành công!")
        st.write(f"Dữ liệu mới nhất: {len(df)} bản ghi")

        fig1 = px.line(df, x="timestamp", y="price", title="📈 Giá SUI/USDT theo thời gian")
        fig2 = px.line(df, x="timestamp", y="funding", title="💰 Funding Rate (%)")
        fig3 = px.line(df, x="timestamp", y="oi", title="📦 Open Interest (OI)")

        st.plotly_chart(fig1, use_container_width=True)
        st.plotly_chart(fig2, use_container_width=True)
        st.plotly_chart(fig3, use_container_width=True)

    except Exception as e:
        st.error(f"Lỗi khi tải dữ liệu: {e}")

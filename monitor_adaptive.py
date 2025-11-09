# monitor_adaptive.py
"""
SUI TrapBot v5.9 — Smart Follow-Up & Pro Alert+ (VN Full)
+ Thêm chức năng ghi dữ liệu trực tiếp vào PostgreSQL Railway
"""
import os
import time
import csv
import json
import math
import requests
import datetime as dt
import statistics
import warnings
import logging
import traceback
from collections import deque, defaultdict
from dotenv import load_dotenv
from sqlalchemy import create_engine, text   # ✅ thêm để kết nối PostgreSQL

# ------------------ CẤU HÌNH / HẰNG SỐ ------------------
warnings.filterwarnings("ignore", category=DeprecationWarning)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Env / defaults
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "7"))
SYMBOL = os.getenv("SYMBOL", "SUIUSDT")
INTERVAL = int(os.getenv("INTERVAL_SEC", "60"))        # sleep giữa các vòng lặp
ADAPT_WINDOW = int(os.getenv("ADAPT_WINDOW", "180"))   # mẫu thống kê
SUMMARY_15M = int(os.getenv("SUMMARY_15M", "15"))      # phút
SUMMARY_60M = int(os.getenv("SUMMARY_60M", "60"))      # phút
VERSION = os.getenv("BOT_VERSION", "v5.9 Smart Follow-Up & Pro Alert+ (VN Full)")
DATA_LOG_FILE = os.getenv("DATA_LOG_FILE", os.path.join(BASE_DIR, "data_log.csv"))
MODEL_STATE_FILE = os.getenv("MODEL_STATE_FILE", os.path.join(BASE_DIR, "model_state.json"))
TELEGRAM_DRY_RUN = os.getenv("TELEGRAM_DRY_RUN", "false").lower() in ("1", "true", "yes")

# PostgreSQL kết nối (Railway)
db_url = os.getenv("DATABASE_URL")
engine = create_engine(db_url) if db_url else None

# Real-time params
EWMA_ALPHA = float(os.getenv("EWMA_ALPHA", "0.15"))
CONFIRM_COUNT = int(os.getenv("CONFIRM_COUNT", "2"))
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "600"))
EARLY_SIGMA = float(os.getenv("EARLY_SIGMA", "1.5"))
STRONG_SIGMA = float(os.getenv("STRONG_SIGMA", "2.0"))
EXTREME_SIGMA = float(os.getenv("EXTREME_SIGMA", "2.8"))
EPS = 1e-9

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
fh = logging.FileHandler(os.path.join(BASE_DIR, "trapbot_send.log"), encoding="utf-8")
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logging.getLogger().addHandler(fh)

# ------------------ TIỆN ÍCH ------------------
def now_vn():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)).strftime("%d/%m/%Y %H:%M")

def ensure_dir_for_file(path):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

# ------------------ GHI DATABASE ------------------
def write_to_db(ts, price, funding, oi):
    """Ghi dữ liệu mới vào PostgreSQL nếu DATABASE_URL có sẵn"""
    if not engine:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO trapbot_data (timestamp, price, funding_pct, oi)
                VALUES (NOW(), :price, :funding, :oi)
            """), {"price": price, "funding": funding, "oi": oi})
        logging.info("[DB] ✅ Đã ghi dữ liệu vào trapbot_data")
    except Exception as e:
        logging.warning(f"[DB] Lỗi ghi dữ liệu: {e}")

# ------------------ TELEGRAM ------------------
def tg_send(msg, parse_mode="Markdown"):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        logging.warning("[tg] TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID chưa có; skip send")
        return False
    if TELEGRAM_DRY_RUN:
        logging.info("[tg] DRY_RUN=true -> preview: %s", msg.replace("\n", " | ")[:250])
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": msg, "parse_mode": parse_mode}
    try:
        r = requests.post(url, json=payload, timeout=10)
        logging.info("[tg] HTTP %s | preview: %s", r.status_code, msg.replace("\n"," | ")[:150])
        return r.status_code == 200
    except Exception as e:
        logging.error("[tg] Exception: %s", e)
        logging.error(traceback.format_exc())
        return False

# ------------------ I/O MUA DỮ LIỆU ------------------
def get_market(symbol=SYMBOL):
    try:
        j = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}", timeout=8).json()
        o = requests.get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}", timeout=8).json()
        mark = float(j.get("markPrice", 0))
        funding_raw = float(j.get("lastFundingRate", 0)) * 100  # %
        oi = float(o.get("openInterest", 0))
        return mark, funding_raw, oi
    except Exception as e:
        logging.warning("[get_market] %s", e)
        return None, None, None

def append_data_log(ts, price, funding, oi, path=DATA_LOG_FILE):
    first = not os.path.exists(path)
    ensure_dir_for_file(path)
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if first:
                w.writerow(["ts","price","funding_pct","oi"])
            w.writerow([ts, f"{price:.8f}", f"{funding:.6f}", int(oi)])
    except Exception as e:
        logging.error("[append_data_log] %s", e)

# ------------------ CHÍNH: VÒNG LẶP CHÍNH ------------------
def main_loop():
    state = {"thresholds": {"fund_sigma": 2.25, "oi_sigma": 2.25, "vol_mult": 1.8}}
    logging.info("[%s] Starting %s", now_vn(), VERSION)
    tg_send(f"🚀 Bot VI {VERSION} đã khởi động\n⏰ {now_vn()} (UTC+7)")

    while True:
        try:
            price, funding, oi = get_market(SYMBOL)
            if price is None:
                logging.warning("[WARN] Market fetch returned None; sleeping")
                time.sleep(max(1, INTERVAL))
                continue

            ts = now_vn()
            append_data_log(ts, price, funding, oi)
            write_to_db(ts, price, funding, oi)   # ✅ GHI DATABASE

            logging.info("[%s] price=%.4f funding=%.5f%% oi=%s",
                         ts, price, funding, f"{int(oi):,}")
            time.sleep(max(1, INTERVAL))

        except KeyboardInterrupt:
            logging.info("Interrupted by user. Exiting.")
            break
        except Exception as e:
            logging.error("[ERR] %s", e)
            logging.error(traceback.format_exc())
            time.sleep(5)

if __name__ == "__main__":
    import sys, traceback, time, logging
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format='[%(asctime)s] %(levelname)s: %(message)s')

    while True:
        try:
            logging.info("Starting SUI_TrapBot main loop...")
            main_loop()
        except Exception as e:
            logging.error(f"Uncaught exception: {e}")
            traceback.print_exc()
            logging.info("Retrying after 10 seconds...")
            time.sleep(10)

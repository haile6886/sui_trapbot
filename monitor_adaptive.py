# monitor_adaptive.py
"""
SUI TrapBot v5.9 — Full production-ready monitor_adaptive with alert DB writes
- Ghi dữ liệu thời gian thực vào trapbot_data (nếu WRITE_TO_DB=true)
- Ghi alerts vào trapbot_alerts để dashboard có thể đọc và hiển thị
- TEI/SL/TP cải tiến (ATR-based), FAKE/TRUE tính chặt hơn
- Cấu hình nhiều tham số qua .env
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

# env
from dotenv import load_dotenv

# optional DB (SQLAlchemy)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError, OperationalError

# ------------------ Config / Defaults ------------------
warnings.filterwarnings("ignore", category=DeprecationWarning)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Env / defaults
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "7"))   # VN = UTC+7
SYMBOL = os.getenv("SYMBOL", "SUIUSDT")
INTERVAL = int(os.getenv("INTERVAL_SEC", "60"))        # seconds between cycles
ADAPT_WINDOW = int(os.getenv("ADAPT_WINDOW", "180"))   # samples for statistics
SUMMARY_15M = int(os.getenv("SUMMARY_15M", "15"))      # minutes
SUMMARY_60M = int(os.getenv("SUMMARY_60M", "60"))      # minutes
VERSION = os.getenv("BOT_VERSION", "v5.9 Smart Follow-Up & Pro Alert+ (VN Full)")
DATA_LOG_FILE = os.getenv("DATA_LOG_FILE", os.path.join(BASE_DIR, "data_log.csv"))
MODEL_STATE_FILE = os.getenv("MODEL_STATE_FILE", os.path.join(BASE_DIR, "model_state.json"))

# Safety / Behavior flags
TELEGRAM_DRY_RUN = os.getenv("TELEGRAM_DRY_RUN", "true").lower() in ("1", "true", "yes")
WRITE_TO_DB = os.getenv("WRITE_TO_DB", "false").lower() in ("1", "true", "yes")
DB_TABLE_NAME = os.getenv("DB_TABLE_NAME", "trapbot_data")
ALERTS_TABLE = os.getenv("ALERTS_TABLE", "trapbot_alerts")  # table to store alerts

# Real-time params
EWMA_ALPHA = float(os.getenv("EWMA_ALPHA", "0.15"))
CONFIRM_COUNT = int(os.getenv("CONFIRM_COUNT", "2"))   # samples needed to confirm spike
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "600"))  # seconds between same alert
EARLY_SIGMA = float(os.getenv("EARLY_SIGMA", "1.5"))
STRONG_SIGMA = float(os.getenv("STRONG_SIGMA", "2.0"))
EXTREME_SIGMA = float(os.getenv("EXTREME_SIGMA", "2.8"))
EPS = 1e-9

# Risk sizing / Pro tip tuning (env-configurable)
MIN_SL_PCT = float(os.getenv("MIN_SL_PCT", "0.0006"))    # tối thiểu Stop-loss (abs)
MIN_TP_PCT = float(os.getenv("MIN_TP_PCT", "0.0008"))    # tối thiểu TP (abs)
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.5"))     # SL = ATR * x
ATR_TP1_MULT = float(os.getenv("ATR_TP1_MULT", "1.0"))   # TP1 = ATR * x
ATR_TP2_MULT = float(os.getenv("ATR_TP2_MULT", "2.0"))   # TP2 = ATR * x
MAX_SL_PCT = float(os.getenv("MAX_SL_PCT", "0.02"))      # max SL cap (2%)

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Database / SQLAlchemy engine
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_ENGINE = None
# Improved DB connect: retry with short connect_timeout
if WRITE_TO_DB and DATABASE_URL:
    for attempt in range(1, 7):
        try:
            DB_ENGINE = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={"connect_timeout": 10})
            # quick test connection
            with DB_ENGINE.connect() as conn:
                conn.execute(text("SELECT 1"))
            logging.basicConfig(level=logging.INFO)
            logging.info("[DB] Connected to database on attempt %d", attempt)
            break
        except OperationalError as oe:
            logging.basicConfig(level=logging.WARNING)
            logging.warning("[DB] connect attempt %d failed: %s", attempt, oe)
            DB_ENGINE = None
            time.sleep(min(5 * attempt, 30))
        except Exception as e:
            logging.basicConfig(level=logging.ERROR)
            logging.error("[DB] unexpected error on connect attempt %d: %s", attempt, e)
            DB_ENGINE = None
            time.sleep(min(5 * attempt, 30))

# Logging (file + stdout)
LOG_FILE = os.path.join(BASE_DIR, "trapbot_send.log")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logging.getLogger().addHandler(fh)

# ------------------ Utilities ------------------
def now_vn():
    """Return VN formatted string (UTC+TZ_OFFSET)"""
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)).strftime("%d/%m/%Y %H:%M:%S")

def iso_now_utc():
    """Return current UTC timestamp ISO (no tz suffix) - suitable for timestamptz insert"""
    return dt.datetime.utcnow().isoformat()

def ensure_dir_for_file(path):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

# ------------------ Telegram ------------------
def tg_send(msg, parse_mode="Markdown"):
    """Send msg via telegram. If TELEGRAM_DRY_RUN, only preview to log."""
    if TELEGRAM_DRY_RUN:
        logging.info("[tg][DRY] %s", msg.replace("\n", " | ")[:400])
        return True
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("[tg] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; skip send")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": parse_mode}
    try:
        r = requests.post(url, json=payload, timeout=10)
        logging.info("[tg] HTTP %s | preview: %s", r.status_code, msg.replace("\n"," | ")[:150])
        return r.status_code == 200
    except Exception as e:
        logging.error("[tg] Exception: %s", e)
        logging.error(traceback.format_exc())
        return False

# ------------------ Market data I/O ------------------
def get_market(symbol=SYMBOL):
    """Fetch from Binance futures premiumIndex & openInterest"""
    try:
        j = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}", timeout=8).json()
        o = requests.get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}", timeout=8).json()
        mark = float(j.get("markPrice", 0))
        funding_raw = float(j.get("lastFundingRate", 0)) * 100.0  # convert to %
        oi = float(o.get("openInterest", 0))
        return mark, funding_raw, oi
    except Exception as e:
        logging.warning("[get_market] %s", e)
        return None, None, None

def append_data_log(ts, price, funding, oi, path=DATA_LOG_FILE):
    """Append to CSV log for backup / offline analysis"""
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

def load_recent(limit, path=DATA_LOG_FILE):
    """Load recent rows from CSV for local stats if needed"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= 1:
            return []
        data_lines = lines[-(limit+1):] if limit < len(lines) else lines
        out = []
        for ln in data_lines[1:]:
            parts = ln.strip().split(",")
            try:
                out.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except:
                continue
        return out
    except Exception as e:
        logging.warning("[load_recent] %s", e)
        return []

# ------------------ DB write (optional) ------------------
def write_to_db(ts_iso, price, funding, oi, current_price=None):
    """Insert row into Postgres trapbot_data if DB_ENGINE configured."""
    if not DB_ENGINE:
        logging.debug("[DB] DB_ENGINE not available, skipping write")
        return False
    try:
        with DB_ENGINE.begin() as conn:
            q = text(f"""
                INSERT INTO {DB_TABLE_NAME} (timestamp, price, funding_pct, oi, other_json, current_price)
                VALUES (:ts, :price, :funding, :oi, :other_json, :current_price)
            """)
            # minimal other_json kept for compatibility; change if you want to store extras
            other_json = {}
            conn.execute(q, {
                "ts": ts_iso,
                "price": float(price),
                "funding": float(funding),
                "oi": int(oi),
                "other_json": json.dumps(other_json),
                "current_price": float(current_price) if current_price is not None else None
            })
        logging.info("[DB] ✅ Đã ghi dữ liệu vào %s", DB_TABLE_NAME)
        return True
    except SQLAlchemyError as e:
        logging.error("[DB] SQLAlchemyError: %s", e)
        return False
    except Exception as e:
        logging.error("[DB] Exception: %s", e)
        return False

# ------------------ Alert write ------------------
def write_alert_to_db(ts_iso, kind, message, tei=None, price=None, funding=None, oi=None, z_vals=None, meta=None):
    """
    Ghi một alert vào bảng ALERTS_TABLE (trapbot_alerts).
    Không gây lỗi nếu DB không khả dụng — chỉ log.
    """
    if not DB_ENGINE:
        logging.debug("[ALERT-DB] DB_ENGINE not available, skipping alert write")
        return False
    try:
        with DB_ENGINE.begin() as conn:
            q = text(f"""
                INSERT INTO {ALERTS_TABLE}
                  (ts, kind, message, tei, price, funding_pct, oi, z_vals, meta)
                VALUES
                  (:ts, :kind, :message, :tei, :price, :funding, :oi, :z_vals, :meta)
            """)
            conn.execute(q, {
                "ts": ts_iso,
                "kind": kind,
                "message": message,
                "tei": int(tei) if tei is not None else None,
                "price": float(price) if price is not None else None,
                "funding": float(funding) if funding is not None else None,
                "oi": int(oi) if oi is not None else None,
                "z_vals": json.dumps(z_vals or {}),
                "meta": json.dumps(meta or {})
            })
        logging.info("[ALERT-DB] wrote alert kind=%s at %s", kind, ts_iso)
        return True
    except Exception as e:
        logging.error("[ALERT-DB] failed to write alert: %s", e)
        return False

# ------------------ State persist ------------------
def load_state(path=MODEL_STATE_FILE):
    if not os.path.exists(path):
        return {
            "thresholds": {"fund_sigma": 2.25, "oi_sigma": 2.25, "vol_mult": 1.8},
            "last_updated": "",
            "temp_adjust_until": {},
            "tei_history": []
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning("[load_state] %s", e)
        return {"thresholds": {"fund_sigma": 2.25, "oi_sigma": 2.25, "vol_mult": 1.8}, "last_updated": ""}

def save_state(state, path=MODEL_STATE_FILE):
    try:
        ensure_dir_for_file(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning("[save_state] %s", e)

# ------------------ Realtime EWMA & Stats ------------------
_price_buf = deque(maxlen=ADAPT_WINDOW)
_fund_buf = deque(maxlen=ADAPT_WINDOW)
_oi_buf = deque(maxlen=ADAPT_WINDOW)

_ewma_p = None
_ewma_f = None
_ewma_o = None
_ewma_var_f = None
_ewma_var_o = None

_alert_counts = defaultdict(int)
_last_alert_time = defaultdict(lambda: 0.0)

def _update_ewma(new, cur, alpha):
    if cur is None:
        return new
    return alpha * new + (1 - alpha) * cur

def compute_stats_realtime():
    global _ewma_p, _ewma_f, _ewma_o, _ewma_var_f, _ewma_var_o
    if len(_fund_buf) < 3:
        return None
    f_list = list(_fund_buf)
    o_list = list(_oi_buf)
    p_list = list(_price_buf)
    if _ewma_f is None:
        _ewma_f = sum(f_list)/len(f_list)
        _ewma_o = sum(o_list)/len(o_list)
        _ewma_p = sum(p_list)/len(p_list)
        _ewma_var_f = statistics.pvariance(f_list) if len(f_list)>1 else 0.0
        _ewma_var_o = statistics.pvariance(o_list) if len(o_list)>1 else 0.0
    else:
        _ewma_f = _update_ewma(f_list[-1], _ewma_f, EWMA_ALPHA)
        _ewma_o = _update_ewma(o_list[-1], _ewma_o, EWMA_ALPHA)
        _ewma_p = _update_ewma(p_list[-1], _ewma_p, EWMA_ALPHA)
        # variance EWMA (approx)
        dev_f = (f_list[-1] - _ewma_f)**2
        dev_o = (o_list[-1] - _ewma_o)**2
        if _ewma_var_f is None:
            _ewma_var_f = dev_f
            _ewma_var_o = dev_o
        else:
            _ewma_var_f = EWMA_ALPHA * dev_f + (1 - EWMA_ALPHA) * _ewma_var_f
            _ewma_var_o = EWMA_ALPHA * dev_o + (1 - EWMA_ALPHA) * _ewma_var_o

    stats = {
        "fm": _ewma_f,
        "fs": math.sqrt(_ewma_var_f) if (_ewma_var_f is not None and _ewma_var_f>=0) else 0.0,
        "om": _ewma_o,
        "os": math.sqrt(_ewma_var_o) if (_ewma_var_o is not None and _ewma_var_o>=0) else 0.0,
        "pv": statistics.pstdev(p_list) if len(p_list)>1 else 0.0
    }
    return stats

def record_and_update_buffers(price, funding, oi):
    _price_buf.append(price)
    _fund_buf.append(funding)
    _oi_buf.append(oi)
    return compute_stats_realtime()

# ------------------ Detection ------------------
def detect_signals_realtime(curr, stats, thresholds):
    """Return list of (key, zvalue) signals"""
    if not stats:
        return []
    out = []
    fs = max(stats.get("fs", 0.0), EPS)
    osd = max(stats.get("os", 0.0), EPS)
    zf = (curr["funding"] - stats.get("fm", 0.0)) / fs
    zo = (curr["oi"] - stats.get("om", 0.0)) / osd
    now = time.time()

    # FUNDING
    fund_th = thresholds.get("fund_sigma", 2.25)
    if abs(zf) >= fund_th:
        if now - _last_alert_time["FUNDING_SPIKE"] > ALERT_COOLDOWN:
            _alert_counts["FUNDING_SPIKE"] += 1
        else:
            _alert_counts["FUNDING_SPIKE"] = 1
        if _alert_counts["FUNDING_SPIKE"] >= CONFIRM_COUNT:
            out.append(("FUNDING_SPIKE", zf))
            _alert_counts["FUNDING_SPIKE"] = 0
    else:
        _alert_counts["FUNDING_SPIKE"] = 0

    # OI
    oi_th = thresholds.get("oi_sigma", 2.25)
    if abs(zo) >= oi_th:
        if now - _last_alert_time["OI_SPIKE"] > ALERT_COOLDOWN:
            _alert_counts["OI_SPIKE"] += 1
        else:
            _alert_counts["OI_SPIKE"] = 1
        if _alert_counts["OI_SPIKE"] >= CONFIRM_COUNT:
            out.append(("OI_SPIKE", zo))
            _alert_counts["OI_SPIKE"] = 0
    else:
        _alert_counts["OI_SPIKE"] = 0

    return out

def mark_alert_sent(key):
    _last_alert_time[key] = time.time()

# ------------------ Breakout classification & TEI ------------------
def detect_breakout_type(curr, stats):
    """Return 'FAKE', 'TRUE' or None (tối ưu: require OI confirmation cho TRUE)"""
    try:
        if not stats:
            return None
        pv = max(stats.get("pv", 0.0), EPS)
        prev_price = curr.get("price_prev") or curr["price"]
        momentum = (curr["price"] - prev_price) / pv if pv>0 else 0.0
        funding_z = (curr["funding"] - stats.get("fm", 0.0)) / max(stats.get("fs", 0.0), EPS)
        oi_z = (curr["oi"] - stats.get("om", 0.0)) / max(stats.get("os", 0.0), EPS)

        # FAKE: funding strong but OI not following + price momentum inconsistent
        if abs(funding_z) > 2.0 and abs(oi_z) < 0.8 and abs(momentum) > 0.6:
            return "FAKE"

        # TRUE: both funding & OI strongly move same direction AND momentum supports it
        if abs(funding_z) > 2.2 and abs(oi_z) > 2.2 and abs(momentum) > 1.0 and (funding_z * oi_z) > 0:
            return "TRUE"
    except Exception:
        return None
    return None

def compute_tei(curr, stats):
    """Trade Event Index 0..100 - điều chỉnh trọng số để OI có tiếng nói mạnh hơn"""
    if not stats:
        return 0
    pv = max(stats.get("pv", 0.0), EPS)
    funding_z = (curr["funding"] - stats.get("fm", 0.0)) / max(stats.get("fs", 0.0), EPS)
    oi_z = (curr["oi"] - stats.get("om", 0.0)) / max(stats.get("os", 0.0), EPS)
    prev_price = curr.get("price_prev") or curr["price"]
    momentum = (curr["price"] - prev_price) / pv if pv>0 else 0.0

    # new weights: ưu tiên OI (dòng tiền), funding là tín hiệu hỗ trợ
    w_f = 0.4
    w_o = 0.8
    w_m = 0.6
    score = w_f * funding_z + w_o * oi_z + w_m * momentum

    # normalize to 0..100 but compress extremes a bit (tăng robustness)
    norm = 50 + score * 8.0
    norm = max(0, min(100, norm))
    return int(norm)

# ------------------ Messaging / Builder ------------------
def sigma_level(z):
    az = abs(z)
    if az >= EXTREME_SIGMA:
        return "CỰC MẠNH", "🔴"
    if az >= STRONG_SIGMA:
        return "MẠNH", "🟠"
    if az >= EARLY_SIGMA:
        return "SỚM", "🟡"
    return "NHẸ", "⚪"

def fmt_pct(x):
    try:
        return f"{x:.4f}%"
    except:
        return str(x)

def build_alert_message(kind, curr, stats, z_vals, tei, thresholds):
    """
    kind: 'FUNDING_SPIKE' | 'OI_SPIKE' | 'BREAKOUT' | 'FAKE' ...
    z_vals: dict e.g. {'funding': zf, 'oi': zo, 'price': zp}
    """
    fm = stats.get("fm", 0.0) if stats else 0.0
    om = stats.get("om", 0.0) if stats else 0.0
    pv = stats.get("pv", 0.0) if stats else 0.0
    parts = []
    header_map = {
        "FUNDING_SPIKE": "⚠️ CẢNH BÁO FUNDING BẤT THƯỜNG",
        "OI_SPIKE": "⚠️ CẢNH BÁO OI BẤT THƯỜNG",
        "BREAKOUT": "🚨 BREAKOUT BẤT THƯỜNG",
        "BREAKDOWN": "🚨 BREAKDOWN BẤT THƯỜNG",
        "FAKE": "🕳️ FAKE BREAKOUT (Bẫy)",
        "SUMMARY_15": "📊 BÁO CÁO 15 PHÚT",
        "SUMMARY_60": "📊 BÁO CÁO 60 PHÚT"
    }
    hdr = header_map.get(kind, "⚠️ CẢNH BÁO")
    price_s = f"{curr['price']:.6f}"
    funding_s = f"{curr['funding']:.6f}%"
    oi_s = f"{int(curr['oi']):,}"
    zf = z_vals.get("funding", 0.0)
    zo = z_vals.get("oi", 0.0)
    zp = z_vals.get("price", 0.0)
    s_f, emoji_f = sigma_level(zf)
    s_o, emoji_o = sigma_level(zo)
    s_p, emoji_p = sigma_level(zp)
    try:
        fund_pct = ((curr['funding'] - fm) / (abs(fm)+EPS)) * 100.0 if fm != 0 else (curr['funding']*100.0)
    except:
        fund_pct = 0.0
    try:
        oi_pct = ((curr['oi'] - om) / (abs(om)+EPS)) * 100.0
    except:
        oi_pct = 0.0

    parts.append(f"{hdr}")
    parts.append(f"📌 TEI: {tei} | Giá: {price_s} | Funding: {funding_s} | OI: {oi_s}")
    parts.append("")
    parts.append("🔎 Phân tích chi tiết:")
    parts.append(f"- Funding: {funding_s} | z = {zf:.2f} ({s_f}) {emoji_f} | Δ so với trung bình ≈ {fund_pct:.1f}%")
    parts.append(f"- OI: {oi_s} | z = {zo:.2f} ({s_o}) {emoji_o} | Δ so với trung bình ≈ {oi_pct:.2f}%")
    parts.append(f"- Giá: z = {zp:.2f} ({s_p}) {emoji_p}")
    parts.append("")

    interpret = []
    action = []
    if kind in ("BREAKOUT", "BREAKDOWN", "FUNDING_SPIKE", "OI_SPIKE", "FAKE"):
        sign_f = zf
        sign_o = zo
        sign_p = zp
        if sign_p > 0 and sign_f > 0 and sign_o > 0:
            interpret.append("-> Dòng tiền xác nhận xu hướng tăng (Funding tăng + OI tăng).")
            action.append("Ưu tiên Long theo xu hướng. Chờ retest/pullback để vào lệnh an toàn.")
        elif sign_p < 0 and sign_f < 0 and sign_o > 0:
            interpret.append("-> Dòng tiền xác nhận phe Short (giá giảm + OI tăng).")
            action.append("Ưu tiên Short theo xu hướng. Tránh Long bắt đáy.")
        elif sign_f > 0 and sign_o < 0:
            interpret.append("-> Funding tăng nhưng OI không tăng (dòng tiền không xác nhận). Có nguy cơ 'bẫy Long'.")
            action.append("Tránh mua đuổi. Chờ OI xác nhận trước khi vào lệnh lớn.")
        elif sign_f < 0 and sign_o < 0 and sign_p < 0:
            interpret.append("-> Funding âm sâu nhưng OI giảm (Short không xác nhận). Có thể là cú rũ Long.")
            action.append("Quan sát vùng hỗ trợ; nếu giá hồi chắc có thể cân nhắc Long thăm dò.")
        else:
            interpret.append("-> Tín hiệu cần quan sát thêm (hỗn hợp).")
            action.append("Hạn chế vào lệnh lớn; ưu tiên quan sát 1-2 chu kỳ.")
    else:
        interpret.append("-> Báo cáo tóm tắt, theo dõi xu hướng.")
        action.append("Không hành động gấp; dùng làm thông tin tham khảo.")

    parts.append("\n".join(interpret))
    parts.append("")
    parts.append("🔧 Gợi ý hành động:")
    for a in action:
        parts.append(f"- {a}")
    parts.append("")

    # Pro Tips: Entry/SL/TP added at the bottom via append_pro_tips() in caller
    parts.append(f"⏱️ Thời gian: {curr['ts']} (UTC+7)")
    msg = "\n".join(parts)
    return msg

# ------------------ Pro Tips (Entry / SL / TP) ------------------
def append_pro_tips(msg, curr, stats, kind):
    """Append Entry / SL / TP suggestions based on ATR-like estimate from _price_buf
       Cải tiến: dùng ATR, áp min/max SL/TP, kiểm tra xác nhận OI vs Funding."""
    try:
        prices = list(_price_buf)
        atr = max(statistics.pstdev(prices) if len(prices) > 1 else 0.0005, 0.0001)

        entry = curr['price']
        prev = curr.get("price_prev")
        direction = "SHORT" if (prev is not None and entry < prev) else "LONG"

        sl_distance = ATR_SL_MULT * atr
        tp1_distance = ATR_TP1_MULT * atr
        tp2_distance = ATR_TP2_MULT * atr

        sl_distance = max(sl_distance, MIN_SL_PCT)
        tp1_distance = max(tp1_distance, MIN_TP_PCT)
        tp2_distance = max(tp2_distance, MIN_TP_PCT * 2)

        sl_distance = min(sl_distance, MAX_SL_PCT)

        funding_z = 0.0
        oi_z = 0.0
        if stats:
            funding_z = (curr['funding'] - stats.get("fm", 0.0)) / max(stats.get("fs", EPS), EPS)
            oi_z = (curr['oi'] - stats.get("om", 0.0)) / max(stats.get("os", EPS), EPS)

        divergence = (funding_z * oi_z) < 0
        caution_factor = 1.0
        if kind == "FAKE" or divergence:
            caution_factor = 1.4
            sl_distance *= caution_factor
            tp1_distance /= caution_factor
            tp2_distance /= (caution_factor * 1.2)

        if direction == "SHORT":
            sl = entry + sl_distance
            tp1 = entry - tp1_distance
            tp2 = entry - tp2_distance
        else:
            sl = entry - sl_distance
            tp1 = entry + tp1_distance
            tp2 = entry + tp2_distance

        msg += ("\n\n📈 Chiến lược gợi ý (Pro Tip):\n"
                f"- Entry: {entry:.6f}\n"
                f"- Stop-loss: {sl:.6f}  (khoảng {sl_distance:.6f} từ entry)\n"
                f"- Take-profit 1: {tp1:.6f}  (khoảng {tp1_distance:.6f})\n"
                f"- Take-profit 2: {tp2:.6f}  (khoảng {tp2_distance:.6f})\n"
                f"- ATR(ước lượng): {atr:.6f}\n")
        if divergence:
            msg += ("\n⚠️ Lưu ý: Funding và OI trái chiều (có thể là bẫy). Giảm kích thước lệnh hoặc chờ xác nhận OI.\n")
        elif kind == "FAKE":
            msg += ("\n⚠️ Lưu ý: Phát hiện FAKE breakout — ưu tiên quan sát, chỉ probe nhẹ nếu có plan rõ.\n")
    except Exception as e:
        logging.warning("[append_pro_tips] %s", e)
    return msg

# ------------------ Temp thresholds adjustments ------------------
def apply_temp_adjustments(state, now_ts):
    to_remove = []
    for k, v in (state.get("temp_adjust_until") or {}).items():
        until = v.get("until") if isinstance(v, dict) else v
        if now_ts >= until:
            to_remove.append(k)
    for k in to_remove:
        try:
            del state["temp_adjust_until"][k]
        except:
            pass

def set_temp_adjust(state, key, until_ts, delta):
    if "temp_adjust_until" not in state or state["temp_adjust_until"] is None:
        state["temp_adjust_until"] = {}
    state["temp_adjust_until"][key] = {"until": int(until_ts), "delta": delta}

def merge_thresholds_with_temp(base_th, state, now_ts):
    th = base_th.copy()
    for k, v in (state.get("temp_adjust_until") or {}).items():
        until = v.get("until")
        delta = v.get("delta", {})
        if now_ts <= until:
            for kk, vv in (delta or {}).items():
                th[kk] = vv
    return th

# ------------------ Adaptive base thresholds ------------------
def adapt_thresholds(stats, prev_state):
    th = {"fund_sigma": 2.25, "oi_sigma": 2.25, "vol_mult": 1.8}
    if not stats:
        return th
    pv = stats.get("pv", 0.0)
    if pv < 0.005:
        th["fund_sigma"], th["oi_sigma"] = 2.5, 2.5
    elif pv > 0.03:
        th["fund_sigma"], th["oi_sigma"] = 1.5, 1.5
    else:
        fs = stats.get("fs", 0.0)
        if fs > 0.02:
            th["fund_sigma"] = max(1.5, th["fund_sigma"] - 0.25)
    prev = prev_state.get("thresholds", {}) if prev_state else {}
    for k in th:
        if k in prev and prev[k] is not None:
            th[k] = (th[k] + prev.get(k, th[k])) / 2
    return th

# ------------------ Summaries ------------------ (rest unchanged)
def make_summary_message(kind, samples, alerts):
    if not samples:
        return None
    prices = [x[0] for x in samples]
    fundings = [x[1] for x in samples]
    ois = [x[2] for x in samples]
    avg_p = statistics.mean(prices)
    avg_f = statistics.mean(fundings)
    avg_o = statistics.mean(ois)
    trend_p = (prices[-1] - prices[0]) / (prices[0] + EPS) * 100.0
    trend_o = (ois[-1] - ois[0]) / (ois[0] + EPS) * 100.0
    title = "BÁO CÁO 15 PHÚT" if kind == "15" else "BÁO CÁO 60 PHÚT"
    msg = [
        f"📊 {title} — {now_vn()}",
        f"Giá TB: {avg_p:.6f} | Funding TB: {avg_f:.6f}% | OI TB: {int(avg_o):,}",
        f"Biến động (start->now): Giá {trend_p:+.2f}% | OI {trend_o:+.2f}%",
        "",
        "🔎 Nhận xét nhanh:"
    ]
    if trend_p > 0.25 and trend_o > 0.3:
        msg.append("-> Xu hướng tăng, dòng tiền xác nhận. Ưu tiên Long.")
    elif trend_p < -0.25 and trend_o > 0.3:
        msg.append("-> Xu hướng giảm, OI tăng (Short có lực). Ưu tiên Short.")
    elif abs(trend_p) < 0.15:
        msg.append("-> Thị trường sideway/giảm biến động. Chờ xu hướng rõ.")
    else:
        msg.append("-> Tín hiệu hỗn hợp, quan sát thêm.")
    msg.append("")
    msg.append(f"⚡ Alerts (15m/60m): Funding={alerts.get('funding',0)} | OI={alerts.get('oi',0)}")
    return "\n".join(msg)

def make_summary_message_short(kind, samples, alerts):
    return make_summary_message(kind, samples, alerts)

# ------------------ Follow-up 15m & Pro tips queue ------------------
follow_queue = []  # list of follow tasks

def add_follow_task(kind, price, oi):
    now_ts = time.time()
    follow_queue.append({
        "type": kind,
        "price_entry": price,
        "time": now_ts,
        "follow_until": now_ts + 15*60,
        "ref_oi": oi
    })

def check_follow_up(price, funding, oi, stats):
    """Check follow_queue each loop; send follow-up if OI/price changes significantly"""
    now_ts = time.time()
    if not follow_queue:
        return
    remaining = []
    for task in follow_queue:
        if now_ts > task["follow_until"]:
            # expired
            continue
        delta_oi_pct = (oi - task["ref_oi"]) / max(task["ref_oi"], EPS) * 100.0
        # heuristics
        if delta_oi_pct > 2.0:
            msg = (f"✅ FOLLOW-UP: OI tăng {delta_oi_pct:+.2f}% sau {int((now_ts-task['time'])/60)} phút.\n"
                   f"Ref: {task['type']} tại {task['price_entry']:.6f}\n"
                   f"Đánh giá: Dòng tiền xác nhận tiếp diễn. Theo hướng ban đầu.\n"
                   f"⏱️ {now_vn()} (UTC+7)")
            ok = tg_send(msg)
            logging.info("[FOLLOW-UP] Confirmed follow-up sent: %s", msg.replace("\n"," | "))
            try:
                ts_iso = iso_now_utc()
                write_alert_to_db(ts_iso, "FOLLOW_UP_CONFIRMED", msg, price=task['price_entry'], oi=oi, funding=funding, z_vals={})
            except Exception as e:
                logging.warning("[FOLLOW-UP] failed to write follow-up to DB: %s", e)
        elif delta_oi_pct < -1.0:
            msg = (f"⚠️ FOLLOW-UP: OI giảm {delta_oi_pct:+.2f}% sau {int((now_ts-task['time'])/60)} phút.\n"
                   f"Ref: {task['type']} tại {task['price_entry']:.6f}\n"
                   f"Đánh giá: Dòng tiền giảm, khả năng hồi/false-break. Cân nhắc điều chỉnh lệnh.\n"
                   f"⏱️ {now_vn()} (UTC+7)")
            ok = tg_send(msg)
            logging.info("[FOLLOW-UP] Rebound follow-up sent")
            try:
                ts_iso = iso_now_utc()
                write_alert_to_db(ts_iso, "FOLLOW_UP_REBOUND", msg, price=task['price_entry'], oi=oi, funding=funding, z_vals={})
            except Exception as e:
                logging.warning("[FOLLOW-UP] failed to write follow-up to DB: %s", e)
        else:
            remaining.append(task)
    follow_queue[:] = remaining

# ------------------ Main loop ------------------
def main_loop():
    state = load_state()
    alerts = {"funding": 0, "oi": 0}
    samples_15m = []
    samples_60m = []
    counter = 0
    last_price = None
    last_15m = time.time()
    last_60m = time.time()
    logging.info("[%s] Starting %s - thresholds=%s", now_vn(), VERSION, state.get("thresholds"))
    # startup notification
    startup_msg = f"🚀 Bot VI {VERSION} đã khởi động | ⏰ {now_vn()} (UTC+7)"
    tg_send(startup_msg)
    try:
        ts_iso = iso_now_utc()
        write_alert_to_db(ts_iso, "BOT_START", startup_msg, meta={"note":"startup"})
    except Exception as e:
        logging.debug("[STARTUP] alert write failed: %s", e)

    while True:
        try:
            price, funding, oi = get_market(SYMBOL)
            if price is None:
                logging.warning("[WARN] Market fetch returned None; sleeping")
                time.sleep(max(1, INTERVAL))
                continue
            ts = now_vn()
            # local CSV log
            append_data_log(ts, price, funding, oi, path=DATA_LOG_FILE)

            # optionally write to DB (trapbot_data)
            if WRITE_TO_DB:
                if DB_ENGINE:
                    ts_iso = iso_now_utc()
                    ok = write_to_db(ts_iso, price, funding, oi, current_price=price)
                    if ok:
                        logging.debug("[DB] wrote row at %s", ts_iso)
                else:
                    logging.warning("[DB] WRITE_TO_DB=true but DB_ENGINE not configured (DATABASE_URL missing or invalid)")

            # realtime stats via buffers
            stats = record_and_update_buffers(price, funding, oi) or compute_stats_realtime()
            base_th = adapt_thresholds(stats, state)
            now_ts = time.time()
            apply_temp_adjustments(state, now_ts)
            thresholds = merge_thresholds_with_temp(base_th, state, now_ts)
            state["thresholds"] = thresholds
            state["last_updated"] = ts
            save_state(state, path=MODEL_STATE_FILE)

            curr = {"ts": ts, "price": price, "funding": funding, "oi": oi, "price_prev": last_price}
            # compute z-scores for building messages
            z_vals = {"funding": 0.0, "oi": 0.0, "price": 0.0}
            if stats:
                z_vals["funding"] = (funding - stats.get("fm",0.0)) / max(stats.get("fs", EPS), EPS)
                z_vals["oi"] = (oi - stats.get("om",0.0)) / max(stats.get("os", EPS), EPS)
                pv_val = stats.get("pv", EPS) if stats.get("pv", None) is not None else EPS
                z_vals["price"] = (price - (curr.get("price_prev") or price)) / max(pv_val, EPS)

            # detect and classify
            signals = detect_signals_realtime(curr, stats, thresholds) if stats else []
            br_type = detect_breakout_type(curr, stats)
            tei = compute_tei(curr, stats)

            # specialized breakout handling
            if br_type == "FAKE":
                until = int(time.time() + 30*60)
                set_temp_adjust(state, "reduce_sens_30m", until, {"fund_sigma": max(3.0, thresholds.get("fund_sigma",2.25)), "oi_sigma": max(3.0, thresholds.get("oi_sigma",2.25))})
                save_state(state, path=MODEL_STATE_FILE)
                msg = build_alert_message("FAKE", curr, stats or {}, z_vals, tei, thresholds)
                msg = append_pro_tips(msg, curr, stats or {}, "FAKE")
                ok = tg_send(msg)
                logging.info("[FAKE] %s", msg.replace("\n"," | "))
                # write alert to DB
                try:
                    ts_iso = iso_now_utc()
                    write_alert_to_db(ts_iso, "FAKE", msg, tei=tei, price=price, funding=funding, oi=oi, z_vals=z_vals, meta={"sent": ok})
                except Exception as e:
                    logging.warning("[FAKE] failed to write alert to DB: %s", e)
                add_follow_task("FAKE", price, oi)
            elif br_type == "TRUE":
                until = int(time.time() + 20*60)
                set_temp_adjust(state, "increase_watch_20m", until, {"fund_sigma": max(1.2, thresholds.get("fund_sigma",1.5)), "oi_sigma": max(1.2, thresholds.get("oi_sigma",1.5))})
                save_state(state, path=MODEL_STATE_FILE)
                msg = build_alert_message("BREAKOUT", curr, stats or {}, z_vals, tei, thresholds)
                msg = append_pro_tips(msg, curr, stats or {}, "BREAKOUT")
                ok = tg_send(msg)
                logging.info("[TRUE] %s", msg.replace("\n"," | "))
                try:
                    ts_iso = iso_now_utc()
                    write_alert_to_db(ts_iso, "BREAKOUT_TRUE", msg, tei=tei, price=price, funding=funding, oi=oi, z_vals=z_vals, meta={"sent": ok})
                except Exception as e:
                    logging.warning("[TRUE] failed to write alert to DB: %s", e)
                add_follow_task("BREAKOUT", price, oi)

            # regular signals handling
            if signals:
                for s in signals:
                    key = s[0]
                    zval = s[1]
                    if time.time() - _last_alert_time[key] < ALERT_COOLDOWN:
                        logging.debug("[ALERT] Skipped %s due cooldown", key)
                        continue
                    msg = build_alert_message(key, curr, stats or {}, z_vals, tei, thresholds)
                    msg = append_pro_tips(msg, curr, stats or {}, key)
                    ok = tg_send(f"🚀 Bot VI {VERSION} - cảnh báo\n" + msg)
                    logging.info("[ALERT] %s | sent=%s", msg.replace("\n"," | "), ok)
                    if ok:
                        mark_alert_sent(key)
                    if key.startswith("FUNDING"):
                        alerts["funding"] += 1
                    if key.startswith("OI"):
                        alerts["oi"] += 1
                    # write alert to DB
                    try:
                        ts_iso = iso_now_utc()
                        write_alert_to_db(ts_iso, key, msg, tei=tei, price=price, funding=funding, oi=oi, z_vals=z_vals, meta={"sent": ok})
                    except Exception as e:
                        logging.warning("[ALERT] failed to write alert to DB: %s", e)
                    # add follow-up for major signals
                    if key in ("FUNDING_SPIKE", "OI_SPIKE"):
                        add_follow_task(key, price, oi)
            else:
                logging.info("[%s] price=%0.6f funding=%0.6f%% oi=%s TEI=%s thresholds=%s",
                             ts, price, funding, f"{int(oi):,}", tei, thresholds)

            # add to summary windows
            samples_15m.append((price, funding, oi))
            samples_60m.append((price, funding, oi))
            # cap sizes
            max_15_samples = max(1, int((SUMMARY_15M*60) / max(1, INTERVAL)))
            max_60_samples = max(1, int((SUMMARY_60M*60) / max(1, INTERVAL)))
            if len(samples_15m) > max_15_samples:
                samples_15m = samples_15m[-max_15_samples:]
            if len(samples_60m) > max_60_samples:
                samples_60m = samples_60m[-max_60_samples:]

            # time-based reports (15m)
            if time.time() - last_15m >= SUMMARY_15M * 60:
                msg15 = make_summary_message("15", samples_15m, alerts)
                if msg15:
                    ok = tg_send(msg15)
                    logging.info("[SUMMARY_15] sent=%s", ok)
                    try:
                        ts_iso = iso_now_utc()
                        write_alert_to_db(ts_iso, "SUMMARY_15", msg15, meta={"sent": ok})
                    except Exception as e:
                        logging.warning("[SUMMARY_15] failed to write alert to DB: %s", e)
                last_15m = time.time()
                samples_15m = []

            # time-based reports (60m)
            if time.time() - last_60m >= SUMMARY_60M * 60:
                msg60 = make_summary_message("60", samples_60m, alerts)
                if msg60:
                    ok = tg_send(msg60)
                    logging.info("[SUMMARY_60] sent=%s", ok)
                    try:
                        ts_iso = iso_now_utc()
                        write_alert_to_db(ts_iso, "SUMMARY_60", msg60, meta={"sent": ok})
                    except Exception as e:
                        logging.warning("[SUMMARY_60] failed to write alert to DB: %s", e)
                last_60m = time.time()
                samples_60m = []

            # record TEI history and save state
            thist = state.get("tei_history", [])
            thist.append({"ts": ts, "price": price, "tei": tei})
            state["tei_history"] = thist[-500:]
            save_state(state, path=MODEL_STATE_FILE)

            # check follow-up queue
            check_follow_up(price, funding, oi, stats)

            last_price = price

            # increment counter and keepalive logs
            counter += 1
            if counter % 60 == 0:
                logging.info("[keepalive] Bot running normally – still alive ✅")

            time.sleep(max(1, INTERVAL))
        except KeyboardInterrupt:
            logging.info("Interrupted by user. Exiting.")
            break
        except Exception as e:
            logging.error("[ERR] %s", e)
            logging.error(traceback.format_exc())
            # small backoff
            time.sleep(5)

# ------------------ Entrypoint ------------------
if __name__ == "__main__":
    import sys
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

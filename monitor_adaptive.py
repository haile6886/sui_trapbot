# monitor_adaptive.py
"""
SUI TrapBot v5.9 — Full production-ready monitor_adaptive with alert DB writes + 3-layer architecture (sklearn AI)
- Lớp 1: Data ingestion & real-time stats (EWMA, buffers, ATR proxy, OHLC fetch)
- Lớp 2: Rule-based detection (Funding/OI/Breakout/FAKE, candlestick patterns proxy, RSI real)
- Lớp 3: AI (sklearn SGDClassifier + StandardScaler) online via partial_fit, features extended (RSI, MACD)
- Ghi dữ liệu thời gian thực vào trapbot_data (nếu WRITE_TO_DB=true)
- Ghi alerts vào trapbot_alerts để dashboard có thể đọc và hiển thị
- TEI/SL/TP cải tiến (ATR-based), FAKE/TRUE tính chặt hơn
- Cấu hình nhiều tham số qua .env
"""
from __future__ import annotations

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
from typing import Optional, Dict, Any, List, Tuple

# env
from dotenv import load_dotenv

# DB
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError, OperationalError

# sklearn (AI layer)
try:
    from joblib import dump, load
    from sklearn.linear_model import SGDClassifier
    from sklearn.preprocessing import StandardScaler
    import numpy as np
    SKLEARN_AVAILABLE = True
except Exception:
    SKLEARN_AVAILABLE = False

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

# AI (Layer 3) configuration - sklearn wrapper
AI_SKLEARN = os.getenv("AI_SKLEARN", "true").lower() in ("1", "true", "yes")
AI_ENABLED = AI_SKLEARN and SKLEARN_AVAILABLE
AI_LEARNING_RATE = float(os.getenv("AI_LEARNING_RATE", "0.01"))
AI_REG_L2 = float(os.getenv("AI_REG_L2", "1e-4"))
AI_HORIZON_SEC = int(os.getenv("AI_HORIZON_SEC", "180"))  # delay to observe outcome
AI_PROB_THRESHOLD = float(os.getenv("AI_PROB_THRESHOLD", "0.65"))
AI_MIN_TRAIN_EXAMPLES = int(os.getenv("AI_MIN_TRAIN_EXAMPLES", "20"))
SKLEARN_MODEL_PATH = os.getenv("SKLEARN_MODEL_PATH", os.path.join(BASE_DIR, "models", "ai_sgd_model.joblib"))
SKLEARN_SCALER_PATH = os.getenv("SKLEARN_SCALER_PATH", os.path.join(BASE_DIR, "models", "ai_scaler.joblib"))

# OHLC / Indicators config
OHLC_WINDOW = int(os.getenv("OHLC_WINDOW", "500"))  # keep up to 500 candles
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "200"))  # number of candles to request if needed
KLINES_INTERVAL = os.getenv("KLINES_INTERVAL", "1m")  # could be 1m, 3m, 5m etc. (string for Binance)

# ------------------ Logging (file + stdout) ------------------
LOG_FILE = os.path.join(BASE_DIR, "trapbot_send.log")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logging.getLogger().addHandler(fh)
logger = logging.getLogger("trapbot")

# ------------------ Utilities ------------------
def now_vn() -> str:
    """Return VN formatted string (UTC+TZ_OFFSET)"""
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)).strftime("%d/%m/%Y %H:%M:%S")

def iso_now_utc() -> str:
    """Return current UTC timestamp ISO (no tz suffix) - suitable for timestamptz insert"""
    return dt.datetime.utcnow().isoformat()

def ensure_dir_for_file(path: str):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

# ------------------ DB initialization & helper ------------------
def init_db_engine():
    global DB_ENGINE
    if not WRITE_TO_DB or not DATABASE_URL:
        logger.warning("[DB] WRITE_TO_DB disabled or DATABASE_URL not set. Skipping DB initialization.")
        return None
    # Try connect with retries
    for attempt in range(1, 7):
        try:
            DB_ENGINE = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={"connect_timeout": 10})
            # quick test connection
            with DB_ENGINE.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("[DB] Connected to database on attempt %d", attempt)
            # init tables
            init_db_tables()
            return DB_ENGINE
        except OperationalError as oe:
            logger.warning("[DB] connect attempt %d failed: %s", attempt, oe)
            DB_ENGINE = None
            time.sleep(min(5 * attempt, 30))
        except Exception as e:
            logger.error("[DB] unexpected error on connect attempt %d: %s", attempt, e)
            DB_ENGINE = None
            time.sleep(min(5 * attempt, 30))
    logger.error("[DB] Failed to connect after retries.")
    return None

def init_db_tables():
    """Create trapbot_data and trapbot_alerts if not exists (Postgres-ready)."""
    if not DB_ENGINE:
        logger.debug("[DB] DB_ENGINE not configured; skipping table init.")
        return
    try:
        with DB_ENGINE.begin() as conn:
            # trapbot_data
            conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_NAME} (
                id BIGSERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL,
                symbol TEXT DEFAULT :symbol,
                price NUMERIC,
                funding_pct NUMERIC,
                oi BIGINT,
                other_json JSONB,
                current_price NUMERIC,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """), {"symbol": SYMBOL})
            # trapbot_alerts
            conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {ALERTS_TABLE} (
                id BIGSERIAL PRIMARY KEY,
                ts TIMESTAMPTZ NOT NULL,
                symbol TEXT DEFAULT :symbol,
                kind TEXT,
                message TEXT,
                tei INTEGER,
                price NUMERIC,
                funding_pct NUMERIC,
                oi BIGINT,
                z_vals JSONB,
                meta JSONB,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """), {"symbol": SYMBOL})
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{DB_TABLE_NAME}_ts ON {DB_TABLE_NAME} (timestamp DESC);"))
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{ALERTS_TABLE}_ts ON {ALERTS_TABLE} (ts DESC);"))
        logger.info("[DB] Tables ensured: %s, %s", DB_TABLE_NAME, ALERTS_TABLE)
    except Exception as e:
        logger.exception("[DB] init_db_tables failed: %s", e)

# Initialize DB engine at import/start
if WRITE_TO_DB and DATABASE_URL:
    init_db_engine()

# ------------------ Telegram ------------------
def tg_send(msg: str, parse_mode: str="Markdown", max_retries: int = 2) -> bool:
    """Send msg via Telegram. If TELEGRAM_DRY_RUN, only preview to log."""
    try:
        if TELEGRAM_DRY_RUN:
            logger.info("[tg][DRY] %s", msg.replace("\n", " | ")[:800])
            return True
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.warning("[tg] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; skip send")
            return False
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": parse_mode}
        for attempt in range(1, max_retries+1):
            try:
                r = requests.post(url, json=payload, timeout=10)
                if r.status_code == 200:
                    logger.info("[tg] Sent (len=%d) ok", len(msg))
                    return True
                else:
                    logger.warning("[tg] HTTP %s attempt %d", r.status_code, attempt)
            except Exception as e:
                logger.warning("[tg] Exception on attempt %d: %s", attempt, e)
                time.sleep(1)
        logger.error("[tg] Failed to send after %d attempts", max_retries)
        return False
    except Exception as e:
        logger.exception("[tg] Fatal exception: %s", e)
        return False

# ------------------ Market data I/O ------------------
def get_market(symbol: str = SYMBOL) -> Tuple[Optional[float], Optional[float], Optional[int]]:
    """Fetch from Binance futures premiumIndex & openInterest, return (mark, funding_pct, oi)"""
    try:
        j = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}", timeout=8).json()
        o = requests.get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}", timeout=8).json()
        mark = float(j.get("markPrice", 0))
        funding_raw = float(j.get("lastFundingRate", 0)) * 100.0  # convert to %
        oi = int(float(o.get("openInterest", 0)))
        return mark, funding_raw, oi
    except Exception as e:
        logger.warning("[get_market] %s", e)
        return None, None, None

def append_data_log(ts: str, price: float, funding: float, oi: int, path: str = DATA_LOG_FILE):
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
        logger.error("[append_data_log] %s", e)

def load_recent(limit: int, path: str = DATA_LOG_FILE) -> List[Tuple[float, float, int]]:
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
                out.append((float(parts[1]), float(parts[2]), int(float(parts[3]))))
            except Exception:
                continue
        return out
    except Exception as e:
        logger.warning("[load_recent] %s", e)
        return []

# ------------------ DB write (optional) ------------------
def write_to_db(ts_iso: str, price: float, funding: float, oi: int, current_price: Optional[float] = None):
    """Insert row into Postgres trapbot_data if DB_ENGINE configured."""
    if not DB_ENGINE:
        logger.debug("[DB] DB_ENGINE not available, skipping write")
        return False
    try:
        with DB_ENGINE.begin() as conn:
            q = text(f"""
                INSERT INTO {DB_TABLE_NAME} (timestamp, symbol, price, funding_pct, oi, other_json, current_price)
                VALUES (:ts, :symbol, :price, :funding, :oi, :other_json, :current_price)
            """)
            other_json = json.dumps({})
            conn.execute(q, {
                "ts": ts_iso,
                "symbol": SYMBOL,
                "price": float(price),
                "funding": float(funding),
                "oi": int(oi),
                "other_json": other_json,
                "current_price": float(current_price) if current_price is not None else None
            })
        logger.info("[DB] ✅ Đã ghi dữ liệu vào %s", DB_TABLE_NAME)
        return True
    except SQLAlchemyError as e:
        logger.error("[DB] SQLAlchemyError: %s", e)
        return False
    except Exception as e:
        logger.error("[DB] Exception: %s", e)
        return False

# ------------------ Alert write ------------------
def write_alert_to_db(ts_iso: str, kind: str, message: str, tei: Optional[int] = None,
                      price: Optional[float] = None, funding: Optional[float] = None,
                      oi: Optional[int] = None, z_vals: Optional[Dict[str, Any]] = None,
                      meta: Optional[Dict[str, Any]] = None):
    """
    Ghi một alert vào bảng ALERTS_TABLE (trapbot_alerts).
    Không gây lỗi nếu DB không khả dụng — chỉ log.
    """
    if not DB_ENGINE:
        logger.debug("[ALERT-DB] DB_ENGINE not available, skipping alert write")
        return False
    try:
        with DB_ENGINE.begin() as conn:
            q = text(f"""
                INSERT INTO {ALERTS_TABLE}
                  (ts, symbol, kind, message, tei, price, funding_pct, oi, z_vals, meta)
                VALUES
                  (:ts, :symbol, :kind, :message, :tei, :price, :funding, :oi, :z_vals, :meta)
            """)
            conn.execute(q, {
                "ts": ts_iso,
                "symbol": SYMBOL,
                "kind": kind,
                "message": message,
                "tei": int(tei) if tei is not None else None,
                "price": float(price) if price is not None else None,
                "funding": float(funding) if funding is not None else None,
                "oi": int(oi) if oi is not None else None,
                "z_vals": json.dumps(z_vals or {}),
                "meta": json.dumps(meta or {})
            })
        logger.info("[ALERT-DB] wrote alert kind=%s at %s", kind, ts_iso)
        return True
    except Exception as e:
        logger.exception("[ALERT-DB] failed to write alert: %s", e)
        return False

# ------------------ State persist ------------------
def load_state(path: str = MODEL_STATE_FILE) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {
            "thresholds": {"fund_sigma": 2.25, "oi_sigma": 2.25, "vol_mult": 1.8},
            "last_updated": "",
            "temp_adjust_until": {},
            "tei_history": [],
            "ai": {"meta": {}}
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("[load_state] %s", e)
        return {"thresholds": {"fund_sigma": 2.25, "oi_sigma": 2.25, "vol_mult": 1.8}, "last_updated": ""}

def save_state(state: Dict[str, Any], path: str = MODEL_STATE_FILE):
    try:
        ensure_dir_for_file(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("[save_state] %s", e)

# ------------------ Realtime EWMA & Stats ------------------
_price_buf = deque(maxlen=ADAPT_WINDOW)
_fund_buf = deque(maxlen=ADAPT_WINDOW)
_oi_buf = deque(maxlen=ADAPT_WINDOW)

_ewma_p: Optional[float] = None
_ewma_f: Optional[float] = None
_ewma_o: Optional[float] = None
_ewma_var_f: Optional[float] = None
_ewma_var_o: Optional[float] = None

_alert_counts = defaultdict(int)
_last_alert_time = defaultdict(lambda: 0.0)

def _update_ewma(new: float, cur: Optional[float], alpha: float) -> float:
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
        "fm": _ewma_f or 0.0,
        "fs": math.sqrt(_ewma_var_f) if (_ewma_var_f is not None and _ewma_var_f>=0) else 0.0,
        "om": _ewma_o or 0.0,
        "os": math.sqrt(_ewma_var_o) if (_ewma_var_o is not None and _ewma_var_o>=0) else 0.0,
        "pv": statistics.pstdev(p_list) if len(p_list)>1 else 0.0
    }
    return stats

def record_and_update_buffers(price: float, funding: float, oi: int):
    _price_buf.append(price)
    _fund_buf.append(funding)
    _oi_buf.append(oi)
    return compute_stats_realtime()

# ------------------ OHLCV & Indicators (RSI, MACD) ------------------
_ohlc_buf: deque = deque(maxlen=OHLC_WINDOW)  # stores dicts {"ts":..., "o":..., "h":..., "l":..., "c":..., "v":...}

def fetch_ohlcv_and_update_buffers(symbol: str = SYMBOL, interval: str = KLINES_INTERVAL, limit: int = KLINES_LIMIT):
    """
    Fetch recent klines from Binance and update _ohlc_buf.
    Each kline -> dict(ts_open_iso, o, h, l, c, v)
    Falls back silently if request fails.
    """
    try:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            logger.warning("[OHLC] klines http %s", r.status_code)
            return False
        data = r.json()
        for k in data:
            ts_ms = int(k[0])
            o = float(k[1])
            h = float(k[2])
            l = float(k[3])
            c = float(k[4])
            v = float(k[5])
            iso_ts = dt.datetime.utcfromtimestamp(ts_ms/1000.0).isoformat()
            _ohlc_buf.append({"ts": iso_ts, "o": o, "h": h, "l": l, "c": c, "v": v})
        return True
    except Exception as e:
        logger.warning("[OHLC] fetch failed: %s", e)
        return False

def prices_from_ohlc(n: int = 200) -> List[float]:
    """Return list of close prices from ohlc buffer (oldest->newest), up to n items."""
    try:
        arr = list(_ohlc_buf)
        if not arr:
            return list(_price_buf)[-n:] if _price_buf else []
        closes = [x["c"] for x in arr][-n:]
        return closes
    except Exception:
        return list(_price_buf)[-n:] if _price_buf else []

def ema(series: List[float], span: int) -> List[float]:
    """Compute EMA of series. Return list same len. Simple EMA implementation."""
    if not series:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [series[0]]
    for x in series[1:]:
        out.append(alpha * x + (1 - alpha) * out[-1])
    return out

def macd_from_prices(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """
    Compute MACD line, signal line, histogram.
    prices: list oldest->newest
    returns (macd_last, signal_last, hist_last) or (0,0,0) if insufficient
    """
    try:
        if len(prices) < max(fast, slow) + signal:
            if len(prices) < slow:
                return 0.0, 0.0, 0.0
        ema_fast = ema(prices, fast)
        ema_slow = ema(prices, slow)
        if len(ema_fast) >= len(ema_slow):
            ef = ema_fast[-len(ema_slow):]
            es = ema_slow
        else:
            ef = ema_fast
            es = ema_slow[-len(ema_fast):]
        macd_series = [a - b for a, b in zip(ef, es)]
        if not macd_series:
            return 0.0, 0.0, 0.0
        signal_series = ema(macd_series, signal)
        macd_last = macd_series[-1]
        signal_last = signal_series[-1] if signal_series else 0.0
        hist_last = macd_last - signal_last
        return float(macd_last), float(signal_last), float(hist_last)
    except Exception as e:
        logger.warning("[MACD] compute failed: %s", e)
        return 0.0, 0.0, 0.0

def rsi_from_prices(prices: List[float], period: int = 14) -> float:
    """
    Compute RSI (classic Wilder) from close prices (oldest->newest).
    Return last RSI value (0..100). If insufficient data -> return 50.0 (neutral).
    """
    try:
        if len(prices) < period + 1:
            return 50.0
        deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
        gains = [d if d > 0 else 0.0 for d in deltas[-period:]]
        losses = [(-d) if d < 0 else 0.0 for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0.0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1 + rs))
        return float(max(0.0, min(100.0, rsi)))
    except Exception as e:
        logger.warning("[RSI] compute failed: %s", e)
        return 50.0

# ------------------ Detection (Rule-based / Lớp 2) ------------------
def detect_signals_realtime(curr: Dict[str, Any], stats: Optional[Dict[str, Any]], thresholds: Dict[str, float]):
    """Return list of (key, zvalue) signals. Now uses RSI real if available."""
    if not stats:
        return []
    out: List[Tuple[str, float]] = []
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

    # RSI real (preferred) or proxy
    try:
        closes = prices_from_ohlc(120)
        rsi_val = None
        if closes and len(closes) >= 15:
            rsi_val = rsi_from_prices(closes, period=14)
        else:
            prices = list(_price_buf)
            if len(prices) >= 10:
                up = sum(max(0.0, prices[i+1] - prices[i]) for i in range(-10, -1))
                down = sum(max(0.0, prices[i] - prices[i+1]) for i in range(-10, -1))
                rsi_val = 100.0 * up / (up + down + EPS)
            else:
                rsi_val = None
        if rsi_val is not None:
            if rsi_val > 75:
                out.append(("RSI_OVERBOUGHT", rsi_val))
            elif rsi_val < 25:
                out.append(("RSI_OVERSOLD", rsi_val))
    except Exception:
        pass

    # candle pattern proxy: quick checks for strong reversal patterns using last 3 price changes
    try:
        prices = list(_price_buf)
        if len(prices) >= 3:
            p0, p1, p2 = prices[-3], prices[-2], prices[-1]
            # bullish engulfing proxy: down then strong up
            if (p1 < p0) and (p2 > p1) and ((p2 - p1) > (p1 - p0) * 1.5):
                out.append(("CANDLE_BULL_ENGULF", p2 - p1))
            # bearish engulfing proxy: up then strong down
            if (p1 > p0) and (p2 < p1) and ((p1 - p2) > (p1 - p0) * 1.5):
                out.append(("CANDLE_BEAR_ENGULF", p1 - p2))
    except Exception:
        pass

    return out

def mark_alert_sent(key: str):
    _last_alert_time[key] = time.time()

# ------------------ Breakout classification & TEI ------------------
def detect_breakout_type(curr: Dict[str, Any], stats: Optional[Dict[str, Any]]):
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

def compute_tei(curr: Dict[str, Any], stats: Optional[Dict[str, Any]]) -> int:
    """Trade Event Index 0..100 - điều chỉnh trọng số để OI có tiếng nói mạnh hơn"""
    if not stats:
        return 0
    pv = max(stats.get("pv", 0.0), EPS)
    funding_z = (curr["funding"] - stats.get("fm", 0.0)) / max(stats.get("fs", EPS), EPS)
    oi_z = (curr["oi"] - stats.get("om", 0.0)) / max(stats.get("os", EPS), EPS)
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
def sigma_level(z: float) -> Tuple[str, str]:
    az = abs(z)
    if az >= EXTREME_SIGMA:
        return "CỰC MẠNH", "🔴"
    if az >= STRONG_SIGMA:
        return "MẠNH", "🟠"
    if az >= EARLY_SIGMA:
        return "SỚM", "🟡"
    return "NHẸ", "⚪"

def build_alert_message(kind: str, curr: Dict[str, Any], stats: Optional[Dict[str, Any]], z_vals: Dict[str, float], tei: int, thresholds: Dict[str, float]) -> str:
    """
    kind: 'FUNDING_SPIKE' | 'OI_SPIKE' | 'BREAKOUT' | 'FAKE' ...
    z_vals: dict e.g. {'funding': zf, 'oi': zo, 'price': zp}
    """
    fm = stats.get("fm", 0.0) if stats else 0.0
    om = stats.get("om", 0.0) if stats else 0.0
    pv = stats.get("pv", 0.0) if stats else 0.0
    parts: List[str] = []
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

    interpret: List[str] = []
    action: List[str] = []
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
    # Show AI prob if available in curr
    if "ai_prob" in curr and curr["ai_prob"] is not None:
        parts.append(f"🤖 AI (Layer3) xác suất bull: {curr['ai_prob']:.2f}")
    parts.append(f"⏱️ Thời gian: {curr['ts']} (UTC+7)")
    msg = "\n".join(parts)
    return msg

# ------------------ ATR (approx) & Pro Tips ------------------
def compute_atr_proxy(window: int = 14) -> float:
    """
    Compute an approximate ATR from price buffer when we don't have OHLC.
    Using mean absolute returns * price as proxy.
    """
    prices = list(_price_buf)
    if len(prices) < 2:
        return 0.0
    n = min(window, len(prices)-1)
    diffs = [abs(prices[-(i+1)] - prices[-(i+2)]) for i in range(n)]
    if not diffs:
        return 0.0
    atr = sum(diffs) / len(diffs)
    return max(atr, 0.0)

def append_pro_tips(msg: str, curr: Dict[str, Any], stats: Optional[Dict[str, Any]], kind: str) -> str:
    """Append Entry / SL / TP suggestions based on ATR-like estimate from _price_buf"""
    try:
        atr = compute_atr_proxy(window=14) or 0.0005
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
        logger.warning("[append_pro_tips] %s", e)
    return msg

# ------------------ Temp thresholds adjustments ------------------
def apply_temp_adjustments(state: Dict[str, Any], now_ts: float):
    to_remove = []
    for k, v in (state.get("temp_adjust_until") or {}).items():
        until = v.get("until") if isinstance(v, dict) else v
        if now_ts >= until:
            to_remove.append(k)
    for k in to_remove:
        try:
            del state["temp_adjust_until"][k]
        except Exception:
            pass

def set_temp_adjust(state: Dict[str, Any], key: str, until_ts: float, delta: Dict[str, Any]):
    if "temp_adjust_until" not in state or state["temp_adjust_until"] is None:
        state["temp_adjust_until"] = {}
    state["temp_adjust_until"][key] = {"until": int(until_ts), "delta": delta}

def merge_thresholds_with_temp(base_th: Dict[str, float], state: Dict[str, Any], now_ts: float) -> Dict[str, float]:
    th = base_th.copy()
    for k, v in (state.get("temp_adjust_until") or {}).items():
        until = v.get("until")
        delta = v.get("delta", {})
        if now_ts <= until:
            for kk, vv in (delta or {}).items():
                th[kk] = vv
    return th

# ------------------ Adaptive base thresholds ------------------
def adapt_thresholds(stats: Optional[Dict[str, Any]], prev_state: Optional[Dict[str, Any]]):
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

# ------------------ Summaries ------------------
def make_summary_message(kind: str, samples: List[Tuple[float, float, int]], alerts: Dict[str, int]) -> Optional[str]:
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

# ------------------ Follow-up 15m & Pro tips queue ------------------
follow_queue: List[Dict[str, Any]] = []  # list of follow tasks

def add_follow_task(kind: str, price: float, oi: int):
    now_ts_local = time.time()
    follow_queue.append({
        "type": kind,
        "price_entry": price,
        "time": now_ts_local,
        "follow_until": now_ts_local + 15*60,
        "ref_oi": oi
    })

def check_follow_up(price: float, funding: float, oi: int, stats: Optional[Dict[str, Any]]):
    """Check follow_queue each loop; send follow-up if OI/price changes significantly"""
    now_ts_local = time.time()
    if not follow_queue:
        return
    remaining: List[Dict[str, Any]] = []
    for task in follow_queue:
        if now_ts_local > task["follow_until"]:
            # expired
            continue
        ref_oi = task.get("ref_oi", 0)
        delta_oi_pct = (oi - ref_oi) / max(ref_oi, EPS) * 100.0 if ref_oi else 0.0
        # heuristics
        if delta_oi_pct > 2.0:
            msg = (f"✅ FOLLOW-UP: OI tăng {delta_oi_pct:+.2f}% sau {int((now_ts_local-task['time'])/60)} phút.\n"
                   f"Ref: {task['type']} tại {task['price_entry']:.6f}\n"
                   f"Đánh giá: Dòng tiền xác nhận tiếp diễn. Theo hướng ban đầu.\n"
                   f"⏱️ {now_vn()} (UTC+7)")
            ok = tg_send(msg)
            logger.info("[FOLLOW-UP] Confirmed follow-up sent: %s", msg.replace("\n"," | "))
            try:
                ts_iso = iso_now_utc()
                write_alert_to_db(ts_iso, "FOLLOW_UP_CONFIRMED", msg, price=task['price_entry'], oi=oi, funding=funding, z_vals={})
            except Exception as e:
                logger.warning("[FOLLOW-UP] failed to write follow-up to DB: %s", e)
        elif delta_oi_pct < -1.0:
            msg = (f"⚠️ FOLLOW-UP: OI giảm {delta_oi_pct:+.2f}% sau {int((now_ts_local-task['time'])/60)} phút.\n"
                   f"Ref: {task['type']} tại {task['price_entry']:.6f}\n"
                   f"Đánh giá: Dòng tiền giảm, khả năng hồi/false-break. Cân nhắc điều chỉnh lệnh.\n"
                   f"⏱️ {now_vn()} (UTC+7)")
            ok = tg_send(msg)
            logger.info("[FOLLOW-UP] Rebound follow-up sent")
            try:
                ts_iso = iso_now_utc()
                write_alert_to_db(ts_iso, "FOLLOW_UP_REBOUND", msg, price=task['price_entry'], oi=oi, funding=funding, z_vals={})
            except Exception as e:
                logger.warning("[FOLLOW-UP] failed to write follow-up to DB: %s", e)
        else:
            remaining.append(task)
    # mutate global queue
    follow_queue[:] = remaining

# ------------------ AI (sklearn wrapper) utilities ------------------
def ensure_model_dir():
    d = os.path.dirname(SKLEARN_MODEL_PATH)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def model_feature_compat_check(model, expected_dim: int) -> bool:
    """Return True if model trained weights compatible with expected_dim"""
    try:
        if model is None:
            return False
        if hasattr(model, "coef_"):
            coef = model.coef_
            if coef is None:
                return False
            # coef shape can be (1, n_features) or (n_classes, n_features)
            nfeat = coef.shape[-1]
            return int(nfeat) == int(expected_dim)
        return False
    except Exception:
        return False

def init_sklearn_ai(state: Dict[str, Any], feature_dim: int):
    """
    Return tuple (model, scaler). If model/scaler files exist -> load.
    Else create new SGDClassifier + StandardScaler.
    If model exists but feature dim mismatch -> remove files and recreate.
    """
    ensure_model_dir()
    model = None
    scaler = None
    if not SKLEARN_AVAILABLE:
        logger.warning("[AI-SK] sklearn/joblib not available in environment. AI disabled.")
        return None, None
    try:
        # if files exist, try load and check compatibility
        if os.path.exists(SKLEARN_MODEL_PATH) and os.path.exists(SKLEARN_SCALER_PATH):
            try:
                model = load(SKLEARN_MODEL_PATH)
                scaler = load(SKLEARN_SCALER_PATH)
                # if model exists but wrong feature dimension -> delete and recreate
                if not model_feature_compat_check(model, feature_dim):
                    logger.warning("[AI-SK] model feature dim mismatch. Recreating model.")
                    try:
                        os.remove(SKLEARN_MODEL_PATH)
                    except Exception:
                        pass
                    try:
                        os.remove(SKLEARN_SCALER_PATH)
                    except Exception:
                        pass
                    model = None
                    scaler = None
            except Exception:
                logger.exception("[AI-SK] failed to load model files. Will recreate.")
                model = None
                scaler = None
        if model is None:
            model = SGDClassifier(loss='log', penalty='l2', alpha=AI_REG_L2, learning_rate='optimal', max_iter=1, warm_start=True)
            scaler = StandardScaler()
            logger.info("[AI-SK] Created new sklearn SGDClassifier (not yet fitted).")
    except Exception as e:
        logger.exception("[AI-SK] init failed, creating fresh objects: %s", e)
        try:
            model = SGDClassifier(loss='log', penalty='l2', alpha=AI_REG_L2, learning_rate='optimal', max_iter=1, warm_start=True)
            scaler = StandardScaler()
        except Exception:
            model, scaler = None, None
    return model, scaler

def ai_sk_predict_proba(model, scaler, features):
    """
    features: list or np.array shape (n_features,)
    returns prob for class 1 (bull)
    """
    if model is None or scaler is None:
        return None
    try:
        X = np.array(features, dtype=float).reshape(1, -1)
        # scaler may be unfitted: catch
        try:
            Xs = scaler.transform(X)
        except Exception:
            # try fit or partial_fit depending on availability
            try:
                if hasattr(scaler, "partial_fit"):
                    scaler.partial_fit(X)
                else:
                    scaler.fit(X)
            except Exception:
                pass
            try:
                Xs = scaler.transform(X)
            except Exception:
                Xs = X
        # if model not fitted -> handle
        try:
            proba = model.predict_proba(Xs)[0][1]
            return float(proba)
        except Exception:
            # model not fitted -> return 0.5
            return 0.5
    except Exception:
        logger.exception("[AI-SK] predict failed")
        return None

def ai_sk_update(model, scaler, features, label):
    """
    Use partial_fit to update model online.
    label: 0 or 1
    """
    if model is None or scaler is None:
        return False
    try:
        X = np.array(features, dtype=float).reshape(1, -1)
        # fit/update scaler
        try:
            if hasattr(scaler, "partial_fit"):
                scaler.partial_fit(X)
            else:
                scaler.fit(X)
        except Exception:
            pass
        try:
            Xs = scaler.transform(X)
        except Exception:
            Xs = X
        # partial_fit requires classes on first call
        try:
            if not hasattr(model, "classes_") or getattr(model, "classes_", None) is None:
                model.partial_fit(Xs, np.array([label]), classes=np.array([0,1]))
            else:
                model.partial_fit(Xs, np.array([label]))
            return True
        except Exception:
            try:
                model.partial_fit(Xs, np.array([label]), classes=np.array([0,1]))
                return True
            except Exception:
                logger.exception("[AI-SK] partial_fit failed")
                return False
    except Exception:
        logger.exception("[AI-SK] update failed")
        return False

def ai_sk_save(model, scaler):
    try:
        ensure_model_dir()
        dump(model, SKLEARN_MODEL_PATH)
        dump(scaler, SKLEARN_SCALER_PATH)
        logger.info("[AI-SK] model and scaler saved to disk.")
    except Exception:
        logger.exception("[AI-SK] saving model failed")

# training buffer to be labeled after AI_HORIZON_SEC
training_buffer: List[Dict[str, Any]] = []  # each: {"ts": t, "features": [...], "price": p}

def extract_features_for_ai(curr: Dict[str, Any], stats: Optional[Dict[str, Any]], tei: int) -> List[float]:
    """
    Build feature vector:
      [funding_z, oi_z, price_z, tei_norm, momentum, atr_norm, rsi_norm, macd_hist_norm]
    rsi_norm: RSI 0..100 -> -1..1
    macd_hist_norm: hist normalized by recent volatility (pv)
    """
    funding_z = 0.0
    oi_z = 0.0
    price_z = 0.0
    momentum = 0.0
    atr = compute_atr_proxy(window=14) or 0.0005
    rsi_val = 50.0
    macd_hist = 0.0
    if stats:
        funding_z = (curr['funding'] - stats.get("fm", 0.0)) / max(stats.get("fs", EPS), EPS)
        oi_z = (curr['oi'] - stats.get("om", 0.0)) / max(stats.get("os", EPS), EPS)
        pv = stats.get("pv", EPS) if stats.get("pv", None) is not None else EPS
        price_z = (curr['price'] - (curr.get("price_prev") or curr['price'])) / max(pv, EPS)
        momentum = (curr['price'] - (curr.get("price_prev") or curr['price'])) / max(curr.get("price_prev", curr['price']), EPS)
    # RSI & MACD from ohlc if possible
    try:
        closes = prices_from_ohlc(240)
        if closes and len(closes) >= 15:
            rsi_val = rsi_from_prices(closes, period=14)
            _, _, macd_hist = macd_from_prices(closes, fast=12, slow=26, signal=9)
        else:
            # fallback to price buffer proxy
            prices = list(_price_buf)
            if len(prices) >= 15:
                rsi_val = rsi_from_prices(prices[-240:], period=14)
                _, _, macd_hist = macd_from_prices(prices[-240:], fast=12, slow=26, signal=9)
            else:
                rsi_val = 50.0
                macd_hist = 0.0
    except Exception:
        rsi_val = 50.0
        macd_hist = 0.0

    # normalize features
    tei_norm = (tei - 50.0) / 50.0  # -1..1
    atr_norm = atr / max(curr['price'], EPS)
    rsi_norm = (rsi_val - 50.0) / 50.0  # -1..1
    pv_local = stats.get("pv", EPS) if stats else EPS
    macd_hist_norm = macd_hist / max(pv_local, EPS)

    features = [funding_z, oi_z, price_z, tei_norm, momentum, atr_norm, rsi_norm, macd_hist_norm]
    return [float(x) for x in features]

def attempt_label_and_train(model, scaler, now_ts_local: float, state: Dict[str, Any]):
    """
    Check training_buffer for any entries older than AI_HORIZON_SEC and assign labels.
    Label rule (simple):
      - If after horizon the price moved up by > LABEL_PCT => label=1 (bull)
      - If moved down by < -LABEL_PCT => label=0
      - Else skip (ambiguous)
    """
    LABEL_PCT = float(os.getenv("AI_LABEL_PCT", "0.0025"))  # e.g., 0.25%
    trained = 0
    # iterate over a copy to allow removal
    for item in list(training_buffer):
        if now_ts_local - item["ts"] >= AI_HORIZON_SEC:
            future_price = item.get("future_price")
            if future_price is None:
                continue
            entry_price = item["price"]
            pct = (future_price - entry_price) / max(entry_price, EPS)
            if pct >= LABEL_PCT:
                label = 1
            elif pct <= -LABEL_PCT:
                label = 0
            else:
                # ambiguous -> skip
                try:
                    training_buffer.remove(item)
                except Exception:
                    pass
                continue
            success = ai_sk_update(model, scaler, item["features"], label)
            if success:
                trained += 1
            try:
                training_buffer.remove(item)
            except Exception:
                pass
    if trained > 0:
        ai_sk_save(model, scaler)
        state["ai"] = state.get("ai", {})
        state["ai"]["last_trained"] = now_ts_local
        save_state(state, path=MODEL_STATE_FILE)
        logger.info("[AI-SK] Trained on %d new examples.", trained)
    return trained

# ------------------ Main loop ------------------
def main_loop():
    state = load_state()
    # expected feature dimension for current extract_features_for_ai
    FEATURE_DIM = 8
    # initialize AI (sklearn)
    if AI_ENABLED:
        model, scaler = init_sklearn_ai(state, FEATURE_DIM)
        if model is None or scaler is None:
            logger.warning("[AI-SK] Failed to initialize sklearn AI; disabling AI.")
            ai_enabled_local = False
        else:
            # ensure model compatibility
            if not model_feature_compat_check(model, FEATURE_DIM):
                logger.info("[AI-SK] Model not yet fitted or incompatible; will fit on first partial_fit calls.")
            ai_enabled_local = True
    else:
        model, scaler = None, None
        ai_enabled_local = False

    alerts = {"funding": 0, "oi": 0}
    samples_15m: List[Tuple[float, float, int]] = []
    samples_60m: List[Tuple[float, float, int]] = []
    counter = 0
    last_price: Optional[float] = None
    last_15m = time.time()
    last_60m = time.time()
    logger.info("[%s] Starting %s - thresholds=%s AI_enabled=%s", now_vn(), VERSION, state.get("thresholds"), ai_enabled_local)
    # startup notification
    startup_msg = f"🚀 Bot VI {VERSION} đã khởi động | ⏰ {now_vn()} (UTC+7)"
    tg_send(startup_msg)
    try:
        ts_iso = iso_now_utc()
        write_alert_to_db(ts_iso, "BOT_START", startup_msg, meta={"note":"startup"})
    except Exception as e:
        logger.debug("[STARTUP] alert write failed: %s", e)

    # main loop
    while True:
        try:
            price, funding, oi = get_market(SYMBOL)
            if price is None:
                logger.warning("[WARN] Market fetch returned None; sleeping")
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
                        logger.debug("[DB] wrote row at %s", ts_iso)
                else:
                    logger.warning("[DB] WRITE_TO_DB=true but DB_ENGINE not configured (DATABASE_URL missing or invalid)")

            # update OHLC buffer (non-blocking)
            try:
                fetch_ohlcv_and_update_buffers(SYMBOL, interval=KLINES_INTERVAL, limit=min(KLINES_LIMIT, OHLC_WINDOW))
            except Exception:
                pass

            # realtime stats via buffers
            stats = record_and_update_buffers(price, funding, oi) or compute_stats_realtime()
            base_th = adapt_thresholds(stats, state)
            now_ts_local = time.time()
            apply_temp_adjustments(state, now_ts_local)
            thresholds = merge_thresholds_with_temp(base_th, state, now_ts_local)
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

            # detect and classify (Lớp 2)
            signals = detect_signals_realtime(curr, stats, thresholds) if stats else []
            br_type = detect_breakout_type(curr, stats)
            tei = compute_tei(curr, stats)

            # Lớp 3: AI inference and buffer training (sklearn)
            prob = None
            if ai_enabled_local and model is not None and scaler is not None:
                features = extract_features_for_ai(curr, stats, tei)
                prob = ai_sk_predict_proba(model, scaler, features)
                curr["ai_prob"] = prob
                # push to training buffer
                training_buffer.append({"ts": now_ts_local, "features": features, "price": price})
                # attach future_price to older items if current price becomes their future
                for item in training_buffer:
                    if "future_price" not in item and now_ts_local - item["ts"] >= AI_HORIZON_SEC:
                        item["future_price"] = price
                # attempt training
                trained = attempt_label_and_train(model, scaler, now_ts_local, state)
                # tweak thresholds if AI very confident
                if prob is not None:
                    if prob >= AI_PROB_THRESHOLD:
                        until = int(time.time() + 5*60)
                        set_temp_adjust(state, "ai_bias_bull", until, {"fund_sigma": max(1.2, thresholds.get("fund_sigma", 2.25) - 0.25)})
                        save_state(state, path=MODEL_STATE_FILE)
                    elif prob <= (1.0 - AI_PROB_THRESHOLD):
                        until = int(time.time() + 5*60)
                        set_temp_adjust(state, "ai_bias_bear", until, {"fund_sigma": max(1.2, thresholds.get("fund_sigma", 2.25) - 0.25)})
                        save_state(state, path=MODEL_STATE_FILE)

            # specialized breakout handling (Lớp 2 + Lớp 3 influence)
            if br_type == "FAKE":
                until = int(time.time() + 30*60)
                set_temp_adjust(state, "reduce_sens_30m", until, {"fund_sigma": max(3.0, thresholds.get("fund_sigma",2.25)), "oi_sigma": max(3.0, thresholds.get("oi_sigma",2.25))})
                save_state(state, path=MODEL_STATE_FILE)
                msg = build_alert_message("FAKE", curr, stats or {}, z_vals, tei, thresholds)
                msg = append_pro_tips(msg, curr, stats or {}, "FAKE")
                ok = tg_send(msg)
                logger.info("[FAKE] %s", msg.replace("\n"," | "))
                try:
                    ts_iso = iso_now_utc()
                    write_alert_to_db(ts_iso, "FAKE", msg, tei=tei, price=price, funding=funding, oi=oi, z_vals=z_vals, meta={"sent": ok, "ai_prob": prob})
                except Exception as e:
                    logger.warning("[FAKE] failed to write alert to DB: %s", e)
                add_follow_task("FAKE", price, oi)
            elif br_type == "TRUE":
                until = int(time.time() + 20*60)
                set_temp_adjust(state, "increase_watch_20m", until, {"fund_sigma": max(1.2, thresholds.get("fund_sigma",1.5)), "oi_sigma": max(1.2, thresholds.get("oi_sigma",1.5))})
                save_state(state, path=MODEL_STATE_FILE)
                msg = build_alert_message("BREAKOUT", curr, stats or {}, z_vals, tei, thresholds)
                msg = append_pro_tips(msg, curr, stats or {}, "BREAKOUT")
                ok = tg_send(msg)
                logger.info("[TRUE] %s", msg.replace("\n"," | "))
                try:
                    ts_iso = iso_now_utc()
                    write_alert_to_db(ts_iso, "BREAKOUT_TRUE", msg, tei=tei, price=price, funding=funding, oi=oi, z_vals=z_vals, meta={"sent": ok, "ai_prob": prob})
                except Exception as e:
                    logger.warning("[TRUE] failed to write alert to DB: %s", e)
                add_follow_task("BREAKOUT", price, oi)

            # regular signals handling (Lớp 2), augmented by AI prob
            if signals:
                for s in signals:
                    key = s[0]
                    zval = s[1]
                    if time.time() - _last_alert_time[key] < ALERT_COOLDOWN:
                        logger.debug("[ALERT] Skipped %s due cooldown", key)
                        continue
                    msg = build_alert_message(key, curr, stats or {}, z_vals, tei, thresholds)
                    msg = append_pro_tips(msg, curr, stats or {}, key)
                    ok = tg_send(f"🚀 Bot VI {VERSION} - cảnh báo\n" + msg)
                    logger.info("[ALERT] %s | sent=%s", msg.replace("\n"," | "), ok)
                    if ok:
                        mark_alert_sent(key)
                    if key.startswith("FUNDING"):
                        alerts["funding"] += 1
                    if key.startswith("OI"):
                        alerts["oi"] += 1
                    # write alert to DB
                    try:
                        ts_iso = iso_now_utc()
                        write_alert_to_db(ts_iso, key, msg, tei=tei, price=price, funding=funding, oi=oi, z_vals=z_vals, meta={"sent": ok, "ai_prob": prob})
                    except Exception as e:
                        logger.warning("[ALERT] failed to write alert to DB: %s", e)
                    # add follow-up for major signals
                    if key in ("FUNDING_SPIKE", "OI_SPIKE"):
                        add_follow_task(key, price, oi)
            else:
                logger.info("[%s] price=%0.6f funding=%0.6f%% oi=%s TEI=%s thresholds=%s AI_prob=%s",
                             ts, price, funding, f"{int(oi):,}", tei, thresholds, f"{prob:.2f}" if prob is not None else "N/A")

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
                    logger.info("[SUMMARY_15] sent=%s", ok)
                    try:
                        ts_iso = iso_now_utc()
                        write_alert_to_db(ts_iso, "SUMMARY_15", msg15, meta={"sent": ok})
                    except Exception as e:
                        logger.warning("[SUMMARY_15] failed to write alert to DB: %s", e)
                last_15m = time.time()
                samples_15m = []

            # time-based reports (60m)
            if time.time() - last_60m >= SUMMARY_60M * 60:
                msg60 = make_summary_message("60", samples_60m, alerts)
                if msg60:
                    ok = tg_send(msg60)
                    logger.info("[SUMMARY_60] sent=%s", ok)
                    try:
                        ts_iso = iso_now_utc()
                        write_alert_to_db(ts_iso, "SUMMARY_60", msg60, meta={"sent": ok})
                    except Exception as e:
                        logger.warning("[SUMMARY_60] failed to write alert to DB: %s", e)
                last_60m = time.time()
                samples_60m = []

            # record TEI history and save state (including AI metadata)
            thist = state.get("tei_history", [])
            thist.append({"ts": ts, "price": price, "tei": tei})
            state["tei_history"] = thist[-500:]
            if ai_enabled_local and model is not None and scaler is not None:
                state["ai"] = state.get("ai", {})
                state["ai"]["last_ai_save"] = time.time()
            save_state(state, path=MODEL_STATE_FILE)

            # check follow-up queue
            check_follow_up(price, funding, oi, stats)

            last_price = price

            # increment counter and keepalive logs
            counter += 1
            if counter % 60 == 0:
                logger.info("[keepalive] Bot running normally – still alive ✅")

            time.sleep(max(1, INTERVAL))
        except KeyboardInterrupt:
            logger.info("Interrupted by user. Exiting.")
            break
        except Exception as e:
            logger.error("[ERR] %s", e)
            logger.error(traceback.format_exc())
            # small backoff
            time.sleep(5)

# ------------------ Entrypoint ------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format='[%(asctime)s] %(levelname)s: %(message)s')
    # ensure DB engine (in case env loaded after import)
    if WRITE_TO_DB and DATABASE_URL and DB_ENGINE is None:
        init_db_engine()
    while True:
        try:
            logger.info("Starting SUI_TrapBot main loop...")
            main_loop()
        except Exception as e:
            logger.error(f"Uncaught exception: {e}")
            traceback.print_exc()
            logger.info("Retrying after 10 seconds...")
            time.sleep(10)

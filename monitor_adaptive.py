# monitor_adaptive.py
"""
SUI TrapBot v5.9 — Full monitor_adaptive (complete)
Features:
 - Realtime fetch markPrice, funding, openInterest from Binance futures
 - EWMA-based adaptative thresholds, z-scores, TEI
 - Persist to CSV (local) and optionally to Postgres (trapbot_data)
 - Save/load model state (model_state.json)
 - Telegram notifications with DRY-RUN option
 - Safe flags: WRITE_TO_DB, TELEGRAM_DRY_RUN to avoid unintended actions
 - Many comments and configurable via .env or Railway variables
"""
import os
import time
import csv
import json
import math
import logging
import traceback
import datetime as dt
from collections import deque, defaultdict

import requests
from dotenv import load_dotenv

# optional DB
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# -------------------- Load config / env --------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# General config
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "7"))   # VN = UTC+7
SYMBOL = os.getenv("SYMBOL", "SUIUSDT")
INTERVAL = int(os.getenv("INTERVAL_SEC", "60"))     # seconds between cycles
ADAPT_WINDOW = int(os.getenv("ADAPT_WINDOW", "180")) # number of samples in buffer
SUMMARY_15M = int(os.getenv("SUMMARY_15M", "15"))
SUMMARY_60M = int(os.getenv("SUMMARY_60M", "60"))
VERSION = os.getenv("BOT_VERSION", "v5.9 Smart Follow-Up & Pro Alert+ (VN Full)")
DATA_LOG_FILE = os.getenv("DATA_LOG_FILE", os.path.join(BASE_DIR, "data_log.csv"))
MODEL_STATE_FILE = os.getenv("MODEL_STATE_FILE", os.path.join(BASE_DIR, "model_state.json"))

# Safety flags
TELEGRAM_DRY_RUN = os.getenv("TELEGRAM_DRY_RUN", "true").lower() in ("1", "true", "yes")
WRITE_TO_DB = os.getenv("WRITE_TO_DB", "false").lower() in ("1", "true", "yes")
DB_TABLE_NAME = os.getenv("DB_TABLE_NAME", "trapbot_data")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Adaptive params
EWMA_ALPHA = float(os.getenv("EWMA_ALPHA", "0.15"))
CONFIRM_COUNT = int(os.getenv("CONFIRM_COUNT", "2"))
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "600"))  # seconds
EARLY_SIGMA = float(os.getenv("EARLY_SIGMA", "1.5"))
STRONG_SIGMA = float(os.getenv("STRONG_SIGMA", "2.0"))
EXTREME_SIGMA = float(os.getenv("EXTREME_SIGMA", "2.8"))
EPS = 1e-9

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_ENGINE = None
if WRITE_TO_DB and DATABASE_URL:
    try:
        DB_ENGINE = create_engine(DATABASE_URL, pool_pre_ping=True)
    except Exception as e:
        DB_ENGINE = None
        # We'll log later

# Logging setup
LOGFILE = os.path.join(BASE_DIR, "trapbot_send.log")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
fh = logging.FileHandler(LOGFILE, encoding="utf-8")
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logging.getLogger().addHandler(fh)

# -------------------- Utilities --------------------
def now_vn():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)).strftime("%d/%m/%Y %H:%M:%S")

def ensure_dir_for_file(path):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

# -------------------- Telegram --------------------
def tg_send(msg, parse_mode="Markdown"):
    if TELEGRAM_DRY_RUN:
        logging.info("[tg][DRY] %s", msg.replace("\n"," | ")[:240])
        return True
    token = TELEGRAM_BOT_TOKEN
    chat = TELEGRAM_CHAT_ID
    if not token or not chat:
        logging.warning("[tg] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": msg, "parse_mode": parse_mode}
    try:
        r = requests.post(url, json=payload, timeout=10)
        logging.info("[tg] HTTP %s | preview: %s", r.status_code, msg.replace("\n"," | ")[:150])
        return r.status_code == 200
    except Exception as e:
        logging.error("[tg] Exception: %s", e)
        return False

# -------------------- Data IO --------------------
def append_data_log(ts_str, price, funding, oi, path=DATA_LOG_FILE):
    ensure_dir_for_file(path)
    first = not os.path.exists(path)
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if first:
                w.writerow(["ts","price","funding_pct","oi"])
            w.writerow([ts_str, f"{price:.8f}", f"{funding:.6f}", int(oi)])
    except Exception as e:
        logging.error("[append_data_log] %s", e)

def write_to_db(ts_iso, price, funding, oi):
    """Insert into Postgres trapbot_data. Requires DB_ENGINE."""
    if not DB_ENGINE:
        logging.warning("[db] No DB engine available, skip write.")
        return False
    try:
        with DB_ENGINE.begin() as conn:
            # use parameterized insert
            q = text(f"""
                INSERT INTO {DB_TABLE_NAME} (timestamp, price, funding_pct, oi)
                VALUES (:ts, :price, :funding, :oi)
            """)
            conn.execute(q, {"ts": ts_iso, "price": float(price), "funding": float(funding), "oi": int(oi)})
        logging.info("[DB] ✅ Đã ghi dữ liệu vào %s", DB_TABLE_NAME)
        return True
    except SQLAlchemyError as e:
        logging.error("[DB] SQLAlchemyError: %s", e)
        return False
    except Exception as e:
        logging.error("[DB] Exception: %s", e)
        return False

# -------------------- State persistance --------------------
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

# -------------------- Stats & EWMA --------------------
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
    return new if cur is None else alpha * new + (1 - alpha) * cur

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

# -------------------- Signal detection --------------------
def detect_signals_realtime(curr, stats, thresholds):
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

# -------------------- Breakout classification & TEI --------------------
def detect_breakout_type(curr, stats):
    try:
        if not stats:
            return None
        pv = max(stats.get("pv", 0.0), EPS)
        prev_price = curr.get("price_prev") or curr["price"]
        momentum = (curr["price"] - prev_price) / pv if pv>0 else 0.0
        funding_z = (curr["funding"] - stats.get("fm", 0.0)) / max(stats.get("fs", 0.0), EPS)
        oi_z = (curr["oi"] - stats.get("om", 0.0)) / max(stats.get("os", 0.0), EPS)

        if abs(funding_z) > 2.0 and abs(oi_z) < 0.6 and abs(momentum) > 0.6:
            return "FAKE"
        if abs(funding_z) > 2.0 and abs(oi_z) > 2.0 and abs(momentum) > 1.0:
            return "TRUE"
    except Exception:
        return None
    return None

def compute_tei(curr, stats):
    if not stats:
        return 0
    pv = max(stats.get("pv", 0.0), EPS)
    funding_z = (curr["funding"] - stats.get("fm", 0.0)) / max(stats.get("fs", 0.0), EPS)
    oi_z = (curr["oi"] - stats.get("om", 0.0)) / max(stats.get("os", 0.0), EPS)
    prev_price = curr.get("price_prev") or curr["price"]
    momentum = (curr["price"] - prev_price) / pv if pv>0 else 0.0
    score = 0.5 * funding_z + 0.7 * oi_z + 0.6 * momentum
    norm = 50 + score * 10
    norm = max(0, min(100, norm))
    return int(norm)

# -------------------- Message builder --------------------
def sigma_level(z):
    az = abs(z)
    if az >= EXTREME_SIGMA:
        return "CỰC MẠNH", "🔴"
    if az >= STRONG_SIGMA:
        return "MẠNH", "🟠"
    if az >= EARLY_SIGMA:
        return "SỚM", "🟡"
    return "NHẸ", "⚪"

def build_alert_message(kind, curr, stats, z_vals, tei, thresholds):
    fm = stats.get("fm", 0.0) if stats else 0.0
    om = stats.get("om", 0.0) if stats else 0.0
    price_s = f"{curr['price']:.6f}"
    funding_s = f"{curr['funding']:.6f}%"
    oi_s = f"{int(curr['oi']):,}"
    zf = z_vals.get("funding", 0.0)
    zo = z_vals.get("oi", 0.0)
    zp = z_vals.get("price", 0.0)
    s_f, emoji_f = sigma_level(zf)
    s_o, emoji_o = sigma_level(zo)
    s_p, emoji_p = sigma_level(zp)

    parts = []
    header_map = {
        "FUNDING_SPIKE": "⚠️ CẢNH BÁO FUNDING BẤT THƯỜNG",
        "OI_SPIKE": "⚠️ CẢNH BÁO OI BẤT THƯỜNG",
        "BREAKOUT": "🚨 BREAKOUT BẤT THƯỜNG",
        "FAKE": "🕳️ FAKE BREAKOUT (Bẫy)",
    }
    hdr = header_map.get(kind, "⚠️ CẢNH BÁO")
    parts.append(hdr)
    parts.append(f"📌 TEI: {tei} | Giá: {price_s} | Funding: {funding_s} | OI: {oi_s}")
    parts.append("")
    parts.append("🔎 Phân tích:")
    parts.append(f"- Funding: {funding_s} | z = {zf:.2f} ({s_f}) {emoji_f}")
    parts.append(f"- OI: {oi_s} | z = {zo:.2f} ({s_o}) {emoji_o}")
    parts.append(f"- Giá: z = {zp:.2f} ({s_p}) {emoji_p}")
    parts.append("")
    # Interpretations (simplified)
    if kind in ("FUNDING_SPIKE", "OI_SPIKE", "BREAKOUT", "FAKE"):
        if zf > 0 and zo > 0:
            parts.append("-> Dòng tiền xác nhận xu hướng tăng. Ưu tiên Long.")
        elif zf < 0 and zo > 0:
            parts.append("-> Dòng tiền hỗ trợ Short.")
        elif zf > 0 and zo < 0:
            parts.append("-> Funding tăng nhưng OI giảm -> NGUY CƠ BẪY.")
        else:
            parts.append("-> Tín hiệu hỗn hợp, quan sát thêm.")
    parts.append("")
    parts.append(f"⏱️ {curr['ts']} (UTC+7)")
    return "\n".join(parts)

# -------------------- Temp adjustments / adapt thresholds --------------------
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

# -------------------- Follow-up & pro tips --------------------
follow_queue = []

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
    now_ts = time.time()
    if not follow_queue:
        return
    remaining = []
    for task in follow_queue:
        if now_ts > task["follow_until"]:
            continue
        delta_oi_pct = (oi - task["ref_oi"]) / max(task["ref_oi"], EPS) * 100.0
        if delta_oi_pct > 2.0:
            msg = f"✅ FOLLOW-UP: OI tăng {delta_oi_pct:+.2f}% -> Xác nhận. Ref {task['type']} @ {task['price_entry']:.6f}\n⏱️ {now_vn()}"
            tg_send(msg)
            logging.info("[FOLLOW-UP] %s", msg.replace("\n"," | "))
        elif delta_oi_pct < -1.0:
            msg = f"⚠️ FOLLOW-UP: OI giảm {delta_oi_pct:+.2f}% -> Có thể revert. Ref {task['type']} @ {task['price_entry']:.6f}\n⏱️ {now_vn()}"
            tg_send(msg)
            logging.info("[FOLLOW-UP] %s", msg.replace("\n"," | "))
        else:
            remaining.append(task)
    follow_queue[:] = remaining

# -------------------- Market fetch --------------------
def get_market(symbol=SYMBOL):
    try:
        j = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}", timeout=8).json()
        o = requests.get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}", timeout=8).json()
        mark = float(j.get("markPrice", 0))
        funding_raw = float(j.get("lastFundingRate", 0)) * 100.0  # %
        oi = float(o.get("openInterest", 0))
        return mark, funding_raw, oi
    except Exception as e:
        logging.warning("[get_market] %s", e)
        return None, None, None

# -------------------- Main loop --------------------
def main_loop():
    import statistics  # local import
    state = load_state()
    alerts = {"funding": 0, "oi": 0}
    samples_15m = []
    samples_60m = []
    last_price = None
    last_15m = time.time()
    last_60m = time.time()
    counter = 0

    logging.info("[%s] Starting %s - thresholds=%s", now_vn(), VERSION, state.get("thresholds"))
    tg_send(f"🚀 Bot VI {VERSION} started\n⏰ {now_vn()} (UTC+7)")

    while True:
        try:
            price, funding, oi = get_market(SYMBOL)
            if price is None:
                logging.warning("[WARN] Market fetch None -> sleep")
                time.sleep(max(1, INTERVAL))
                continue

            ts = now_vn()
            # persist CSV
            append_data_log(ts, price, funding, oi)

            # try write to DB if enabled
            if WRITE_TO_DB and DB_ENGINE:
                # use UTC timestamp isoformat for DB
                ts_iso = (dt.datetime.utcnow()).isoformat()  # no tz suffix to let DB interpret as timestamptz
                okdb = write_to_db(ts_iso, price, funding, oi)
                if okdb:
                    logging.info("[DB] [OK] wrote one row")
            else:
                if WRITE_TO_DB and not DB_ENGINE:
                    logging.warning("[DB] WRITE_TO_DB=true but no DB_ENGINE configured (DATABASE_URL missing or invalid)")

            # stats
            stats = record_and_update_buffers(price, funding, oi) or compute_stats_realtime()
            base_th = adapt_thresholds(stats, state)
            now_ts = time.time()
            apply_temp_adjustments(state, now_ts)
            thresholds = merge_thresholds_with_temp(base_th, state, now_ts)
            state["thresholds"] = thresholds
            state["last_updated"] = ts
            save_state(state)

            curr = {"ts": ts, "price": price, "funding": funding, "oi": oi, "price_prev": last_price}
            z_vals = {"funding": 0.0, "oi": 0.0, "price": 0.0}
            if stats:
                z_vals["funding"] = (funding - stats.get("fm", 0.0)) / max(stats.get("fs", EPS), EPS)
                z_vals["oi"] = (oi - stats.get("om", 0.0)) / max(stats.get("os", EPS), EPS)
                pv_val = stats.get("pv", EPS) if stats.get("pv", None) is not None else EPS
                z_vals["price"] = (price - (curr.get("price_prev") or price)) / max(pv_val, EPS)

            # detect
            signals = detect_signals_realtime(curr, stats, thresholds) if stats else []
            br_type = detect_breakout_type(curr, stats)
            tei = compute_tei(curr, stats)

            # breakout handling
            if br_type == "FAKE":
                until = int(time.time() + 30*60)
                set_temp_adjust(state, "reduce_sens_30m", until, {"fund_sigma": max(3.0, thresholds.get("fund_sigma",2.25)), "oi_sigma": max(3.0, thresholds.get("oi_sigma",2.25))})
                save_state(state)
                msg = build_alert_message("FAKE", curr, stats or {}, z_vals, tei, thresholds)
                tg_send(msg); logging.info("[FAKE] %s", msg.replace("\n"," | "))
                add_follow_task("FAKE", price, oi)
            elif br_type == "TRUE":
                until = int(time.time() + 20*60)
                set_temp_adjust(state, "increase_watch_20m", until, {"fund_sigma": max(1.2, thresholds.get("fund_sigma",1.5)), "oi_sigma": max(1.2, thresholds.get("oi_sigma",1.5))})
                save_state(state)
                msg = build_alert_message("BREAKOUT", curr, stats or {}, z_vals, tei, thresholds)
                tg_send(msg); logging.info("[TRUE] %s", msg.replace("\n"," | "))
                add_follow_task("BREAKOUT", price, oi)

            # signals
            if signals:
                for s in signals:
                    key, zval = s
                    if time.time() - _last_alert_time[key] < ALERT_COOLDOWN:
                        logging.debug("[ALERT] Skip %s due cooldown", key)
                        continue
                    msg = build_alert_message(key, curr, stats or {}, z_vals, tei, thresholds)
                    ok = tg_send(f"🚀 Bot VI {VERSION} - {key}\n" + msg)
                    logging.info("[ALERT] %s | sent=%s", msg.replace("\n"," | "), ok)
                    if ok:
                        mark_alert_sent(key)
                    if key.startswith("FUNDING"):
                        alerts["funding"] += 1
                    if key.startswith("OI"):
                        alerts["oi"] += 1
                    if key in ("FUNDING_SPIKE", "OI_SPIKE"):
                        add_follow_task(key, price, oi)
            else:
                logging.info("[%s] price=%0.6f funding=%0.6f%% oi=%s TEI=%s thresholds=%s",
                             ts, price, funding, f"{int(oi):,}", tei, thresholds)

            # summary windows
            samples_15m.append((price, funding, oi))
            samples_60m.append((price, funding, oi))
            max_15_samples = max(1, int((SUMMARY_15M*60) / max(1, INTERVAL)))
            max_60_samples = max(1, int((SUMMARY_60M*60) / max(1, INTERVAL)))
            if len(samples_15m) > max_15_samples:
                samples_15m = samples_15m[-max_15_samples:]
            if len(samples_60m) > max_60_samples:
                samples_60m = samples_60m[-max_60_samples:]

            # time-based reports
            if time.time() - last_15m >= SUMMARY_15M * 60:
                msg15 = make_summary_message_short("15", samples_15m, alerts)
                if msg15:
                    tg_send(msg15)
                    logging.info("[SUMMARY_15] sent")
                last_15m = time.time()
                samples_15m = []

            if time.time() - last_60m >= SUMMARY_60M * 60:
                msg60 = make_summary_message_short("60", samples_60m, alerts)
                if msg60:
                    tg_send(msg60)
                    logging.info("[SUMMARY_60] sent")
                last_60m = time.time()
                samples_60m = []

            # persist recent TEI to state
            thist = state.get("tei_history", [])
            thist.append({"ts": ts, "price": price, "tei": tei})
            state["tei_history"] = thist[-500:]
            save_state(state)

            # follow-up check
            check_follow_up(price, funding, oi, stats)

            last_price = price
            counter += 1
            if counter % 60 == 0:
                logging.info("[keepalive] Bot still alive")

            time.sleep(max(1, INTERVAL))

        except KeyboardInterrupt:
            logging.info("Interrupted by user. Exiting.")
            break
        except Exception as e:
            logging.error("[ERR] %s", e)
            logging.error(traceback.format_exc())
            time.sleep(5)

# -------------------- Short summary maker --------------------
def make_summary_message_short(kind, samples, alerts):
    if not samples:
        return None
    prices = [x[0] for x in samples]
    fundings = [x[1] for x in samples]
    ois = [x[2] for x in samples]
    avg_p = sum(prices)/len(prices)
    avg_f = sum(fundings)/len(fundings)
    avg_o = sum(ois)/len(ois)
    trend_p = (prices[-1] - prices[0]) / (prices[0] + EPS) * 100.0
    trend_o = (ois[-1] - ois[0]) / (ois[0] + EPS) * 100.0
    title = "BÁO CÁO 15 PHÚT" if kind == "15" else "BÁO CÁO 60 PHÚT"
    msg = [
        f"📊 {title} — {now_vn()}",
        f"Giá TB: {avg_p:.6f} | Funding TB: {avg_f:.6f}% | OI TB: {int(avg_o):,}",
        f"Biến động (start->now): Giá {trend_p:+.2f}% | OI {trend_o:+.2f}%",
        "",
        "🔎 Nhận xét:"
    ]
    if trend_p > 0.25 and trend_o > 0.3:
        msg.append("-> Xu hướng tăng, dòng tiền xác nhận. Ưu tiên Long.")
    elif trend_p < -0.25 and trend_o > 0.3:
        msg.append("-> Xu hướng giảm, OI tăng. Ưu tiên Short.")
    elif abs(trend_p) < 0.15:
        msg.append("-> Sideway/low vol.")
    else:
        msg.append("-> Tín hiệu hỗn hợp.")
    msg.append("")
    msg.append(f"⚡ Alerts (15m/60m): Funding={alerts.get('funding',0)} | OI={alerts.get('oi',0)}")
    return "\n".join(msg)

# -------------------- Entrypoint --------------------
if __name__ == "__main__":
    logging.info("Launching monitor_adaptive.py main...")
    try:
        main_loop()
    except Exception as e:
        logging.error("Fatal error at top level: %s", e)
        logging.error(traceback.format_exc())
        raise

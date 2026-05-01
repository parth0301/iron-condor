"""
Iron Condor Live Alert System for NIFTY 50
Sends Telegram alerts. Runs every 5 minutes.
"""

import os
import time
import json
import logging
import datetime
import requests
import yfinance as yf
import pytz
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8620794458:ААHyb0e5Wa7LjHqPjbFIhK4zqaannХpR5Pc")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "970123391")

NIFTY_TICKER  = "^NSEI"
VIX_TICKER    = "^INDIAVIX"
LOT_SIZE      = 50
SPREAD_WIDTH  = 300
WING_OFFSET   = 250
VIX_LIMIT     = 16.0
GAP_LIMIT     = 0.007
RANGE_LIMIT   = 0.004
ADJUST_ZONE   = 100

STATE_FILE    = Path("/tmp/trade_state.json")   # /tmp works on Render
IST           = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]   # stdout only — Render captures this
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            if state.get("date") != datetime.date.today().isoformat():
                log.info("New day — resetting state.")
                return default_state()
            return state
        except Exception:
            pass
    return default_state()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def default_state():
    return {
        "date": datetime.date.today().isoformat(),
        "status": "IDLE",
        "trade": None,
        "adjust_call_sent": False,
        "adjust_put_sent": False,
    }


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        r.raise_for_status()
        log.info("Telegram alert sent.")
    except Exception as e:
        log.error(f"Telegram failed: {e}")


# ─────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────
def get_nifty_data():
    try:
        ticker    = yf.Ticker(NIFTY_TICKER)
        spot      = float(ticker.fast_info.last_price)
        hist      = ticker.history(period="5d", interval="1d")
        prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else spot
        today_open = float(hist["Open"].iloc[-1]) if len(hist) >= 1 else spot

        intraday  = ticker.history(period="1d", interval="5m")
        now_ist   = datetime.datetime.now(IST)
        thirty_ago = now_ist - datetime.timedelta(minutes=30)
        intraday.index = intraday.index.tz_convert(IST)
        recent    = intraday[intraday.index >= thirty_ago]

        range_pct = 0.0
        if len(recent) > 0:
            range_pct = (float(recent["High"].max()) - float(recent["Low"].min())) / spot

        return {
            "spot": spot,
            "prev_close": prev_close,
            "today_open": today_open,
            "intraday_range_pct": range_pct,
        }
    except Exception as e:
        log.error(f"NIFTY fetch failed: {e}")
        return None


def get_vix():
    try:
        return float(yf.Ticker(VIX_TICKER).fast_info.last_price)
    except Exception as e:
        log.warning(f"VIX unavailable (skipping filter): {e}")
        return None


# ─────────────────────────────────────────────
# STRIKES & PREMIUMS
# ─────────────────────────────────────────────
def round50(v):
    return round(v / 50) * 50


def calculate_strikes(spot):
    ls = round50(spot - WING_OFFSET)
    us = round50(spot + WING_OFFSET)
    return ls, us, round50(ls - SPREAD_WIDTH), round50(us + SPREAD_WIDTH)


def trade_metrics(spot):
    short_prem  = spot * 0.004
    long_prem   = short_prem * 0.35
    net_credit  = round(2 * (short_prem - long_prem), 2)
    max_profit  = round(net_credit * LOT_SIZE, 2)
    max_loss    = round((SPREAD_WIDTH - net_credit) * LOT_SIZE, 2)
    target      = round(max_profit * 0.50, 2)
    stop_loss   = round(net_credit * 2 * LOT_SIZE, 2)
    return net_credit, max_profit, max_loss, target, stop_loss


# ─────────────────────────────────────────────
# CONDITIONS
# ─────────────────────────────────────────────
def check_conditions(data, vix):
    now  = datetime.datetime.now(IST)
    fail = []

    if now.weekday() not in (0, 1, 2):
        fail.append("Not Mon–Wed")

    entry_start = now.replace(hour=10, minute=30, second=0, microsecond=0)
    entry_end   = now.replace(hour=12, minute=30, second=0, microsecond=0)
    if not (entry_start <= now <= entry_end):
        fail.append(f"Outside entry window ({now.strftime('%H:%M')})")

    if vix is not None and vix >= VIX_LIMIT:
        fail.append(f"VIX {vix:.1f} >= {VIX_LIMIT}")

    gap = abs(data["today_open"] - data["prev_close"]) / data["prev_close"]
    if gap >= GAP_LIMIT:
        fail.append(f"Gap {gap*100:.2f}% >= 0.7%")

    if data["intraday_range_pct"] >= RANGE_LIMIT:
        fail.append(f"30-min range {data['intraday_range_pct']*100:.2f}% >= 0.4%")

    return fail


# ─────────────────────────────────────────────
# MESSAGES
# ─────────────────────────────────────────────
def msg_entry(spot, ls, us, ll, ul, nc, mp, ml, tgt, sl):
    return (
        f"🔵 <b>IRON CONDOR SETUP</b>\n\n"
        f"Spot: <b>{spot:.0f}</b>\n\n"
        f"PUT SIDE:\n  SELL {ls} PE\n  BUY  {ll} PE\n\n"
        f"CALL SIDE:\n  SELL {us} CE\n  BUY  {ul} CE\n\n"
        f"Range: <b>{ls} – {us}</b>\n\n"
        f"Net Credit : {nc:.1f} pts\n"
        f"Max Profit : ₹{mp:,.0f}\n"
        f"Max Loss   : ₹{ml:,.0f}\n"
        f"Target     : ₹{tgt:,.0f}\n"
        f"Stop Loss  : ₹{sl:,.0f}"
    )


def msg_exit(reason):
    return f"🔴 <b>EXIT TRADE NOW</b>\n\nReason: {reason}"


# ─────────────────────────────────────────────
# MAIN CYCLE
# ─────────────────────────────────────────────
def run_cycle():
    state   = load_state()
    now_ist = datetime.datetime.now(IST)
    log.info(f"Cycle | {now_ist.strftime('%H:%M')} IST | Status: {state['status']}")

    data = get_nifty_data()
    if data is None:
        return
    spot = data["spot"]
    vix  = get_vix()
    log.info(f"Spot: {spot:.2f} | VIX: {vix}")

    # ── CLOSED: nothing to do ──
    if state["status"] == "CLOSED":
        return

    # ── ACTIVE: monitor ──
    if state["status"] == "ACTIVE":
        trade  = state["trade"]
        ls, us = trade["lower_short"], trade["upper_short"]
        exit_t = now_ist.replace(hour=14, minute=45, second=0, microsecond=0)

        if now_ist >= exit_t:
            send_telegram(msg_exit("Time Exit (2:45 PM)"))
            state["status"] = "CLOSED"
            save_state(state)
            return

        if spot >= (us - ADJUST_ZONE) and not state["adjust_call_sent"]:
            send_telegram("🟡 <b>ADJUST CALL SIDE</b> – Price near upper range")
            state["adjust_call_sent"] = True

        if spot <= (ls + ADJUST_ZONE) and not state["adjust_put_sent"]:
            send_telegram("🟡 <b>ADJUST PUT SIDE</b> – Price near lower range")
            state["adjust_put_sent"] = True

        if spot > us + SPREAD_WIDTH or spot < ls - SPREAD_WIDTH:
            send_telegram(msg_exit("Strong Breakout"))
            state["status"] = "CLOSED"

        save_state(state)
        return

    # ── IDLE: check entry ──
    fail = check_conditions(data, vix)
    if fail:
        log.info(f"Conditions failed: {'; '.join(fail)}")
        return

    log.info("✅ All conditions met — sending entry alert.")
    ls, us, ll, ul        = calculate_strikes(spot)
    nc, mp, ml, tgt, sl   = trade_metrics(spot)
    send_telegram(msg_entry(spot, ls, us, ll, ul, nc, mp, ml, tgt, sl))

    state["status"] = "ACTIVE"
    state["trade"]  = {"lower_short": ls, "upper_short": us}
    save_state(state)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Iron Condor bot starting...")
    send_telegram("🟢 <b>Iron Condor Bot STARTED</b>\nMonitoring NIFTY every 5 minutes.")
    while True:
        try:
            run_cycle()
        except Exception as e:
            log.exception(f"Cycle error: {e}")
        time.sleep(300)

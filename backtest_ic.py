"""
Iron Condor Backtest — NIFTY & BANKNIFTY
Wide-range Iron Condor: Spot ± 250, Wings ± 300

Usage:
    python backtest_ic.py
    python backtest_ic.py --symbol NIFTY
    python backtest_ic.py --symbol BANKNIFTY
    python backtest_ic.py --symbol BOTH
"""

import argparse
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta, time as dtime
import time as _time

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
PERIOD          = "60d"
INTERVAL        = "15m"
LOT_SIZE        = 50        # NIFTY lot size
BNF_LOT_SIZE    = 15        # BANKNIFTY lot size
WING_OFFSET     = 250       # spot ± 250 = short strikes
SPREAD_WIDTH    = 300       # long wings beyond short strikes
VIX_LIMIT       = 16.0
GAP_LIMIT       = 0.007     # 0.7%
RANGE_LIMIT     = 0.004     # 0.4%
ENTRY_TIME      = dtime(11, 0)
EXIT_TIME       = dtime(14, 45)
EARLY_RANGE_END = dtime(10, 45)   # 30-min range window: 9:15–10:45

SYMBOLS = {
    "NIFTY":     ("^NSEI",    LOT_SIZE),
    "BANKNIFTY": ("^NSEBANK", BNF_LOT_SIZE),
}

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def round50(v):
    return round(v / 50) * 50


def estimate_net_credit(spot):
    short = spot * 0.004
    long  = short * 0.35
    return round(2 * (short - long), 2)


# ─────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────
def fetch_intraday(yf_symbol: str, period: str = PERIOD) -> pd.DataFrame:
    df = yf.Ticker(yf_symbol).history(period=period, interval=INTERVAL)
    if df.empty:
        return df
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    return df


def fetch_daily(yf_symbol: str, period: str = "90d") -> pd.DataFrame:
    df = yf.Ticker(yf_symbol).history(period=period, interval="1d")
    if df.empty:
        return df
    df = df[["Open", "Close"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def fetch_vix() -> pd.Series:
    try:
        df = yf.Ticker("^INDIAVIX").history(period="90d", interval="1d")
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df["Close"].rename("VIX")
    except Exception:
        print("  VIX unavailable — skipping VIX filter.")
        return None


# ─────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────
def run_backtest(label: str, yf_symbol: str, lot: int,
                 intraday: pd.DataFrame, daily: pd.DataFrame,
                 vix_series: pd.Series):

    trades = []
    filter_stats = {
        "total_days":      0,
        "skipped_vix":     0,
        "skipped_gap":     0,
        "skipped_range":   0,
        "traded":          0,
    }

    trade_dates = sorted(set(intraday.index.date))

    for i, date in enumerate(trade_dates):
        filter_stats["total_days"] += 1
        day_data = intraday[intraday.index.date == date]

        if day_data.empty:
            continue

        # ── VIX filter ──
        if vix_series is not None:
            vix_row = vix_series[vix_series.index.date == date]
            if not vix_row.empty and float(vix_row.iloc[0]) >= VIX_LIMIT:
                filter_stats["skipped_vix"] += 1
                continue

        # ── Gap filter (needs previous day close) ──
        daily_up_to = daily[daily.index.date < date]
        if len(daily_up_to) < 1:
            continue
        prev_close = float(daily_up_to["Close"].iloc[-1])
        today_open = float(day_data["Open"].iloc[0])
        gap_pct = abs(today_open - prev_close) / prev_close
        if gap_pct >= GAP_LIMIT:
            filter_stats["skipped_gap"] += 1
            continue

        # ── 30-min range filter (9:15–10:45) ──
        early = day_data[day_data.index.time <= EARLY_RANGE_END]
        if len(early) < 2:
            continue
        range_pct = (float(early["High"].max()) - float(early["Low"].min())) / today_open
        if range_pct >= RANGE_LIMIT:
            filter_stats["skipped_range"] += 1
            continue

        # ── Entry at 11:00 AM ──
        entry_bars = day_data[day_data.index.time >= ENTRY_TIME]
        if entry_bars.empty:
            continue
        spot       = float(entry_bars.iloc[0]["Open"])
        entry_time = entry_bars.index[0]

        ls = round50(spot - WING_OFFSET)
        us = round50(spot + WING_OFFSET)
        ll = round50(ls - SPREAD_WIDTH)
        ul = round50(us + SPREAD_WIDTH)

        net_credit = estimate_net_credit(spot)
        max_profit = round(net_credit * lot, 2)
        max_loss   = round((SPREAD_WIDTH - net_credit) * lot, 2)

        # ── Simulate candle-by-candle ──
        after_entry = day_data[day_data.index > entry_time]
        result    = "FULL_PROFIT"
        exit_time = None

        for idx, candle in after_entry.iterrows():
            if idx.time() >= EXIT_TIME:
                break

            hi = float(candle["High"])
            lo = float(candle["Low"])

            # Full breakout beyond long wings → full loss
            if hi > ul or lo < ll:
                result    = "FULL_LOSS"
                exit_time = idx
                break

            # Breach of short strike → partial loss
            if hi > us or lo < ls:
                result    = "PARTIAL_LOSS"
                exit_time = idx
                break

        if exit_time is None:
            exit_time = after_entry.index[-1] if not after_entry.empty else entry_time

        if result == "FULL_PROFIT":
            pnl = max_profit
        elif result == "PARTIAL_LOSS":
            pnl = -max_loss * 0.5
        else:
            pnl = -max_loss

        filter_stats["traded"] += 1
        trades.append({
            "date":        date,
            "spot":        round(spot, 2),
            "lower_short": ls,
            "upper_short": us,
            "net_credit":  net_credit,
            "max_profit":  max_profit,
            "max_loss":    max_loss,
            "result":      result,
            "pnl":         round(pnl, 2),
            "exit_time":   str(exit_time.time()),
        })

    return pd.DataFrame(trades), filter_stats


# ─────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────
def print_report(df: pd.DataFrame, label: str, filter_stats: dict):
    print("\n" + "=" * 60)
    print(f"FILTER BREAKDOWN — {label}")
    print("=" * 60)
    print(f"Total Days Checked  : {filter_stats['total_days']}")
    print(f"  Skipped (VIX)     : {filter_stats['skipped_vix']}")
    print(f"  Skipped (Gap)     : {filter_stats['skipped_gap']}")
    print(f"  Skipped (Range)   : {filter_stats['skipped_range']}")
    print(f"  ✅ TRADED         : {filter_stats['traded']}")

    if df.empty:
        print(f"\n{label}: No trades generated.")
        return

    total   = len(df)
    wins    = df[df["pnl"] > 0]
    losses  = df[df["pnl"] <= 0]
    win_rate   = len(wins) / total * 100
    total_pnl  = df["pnl"].sum()
    avg_pnl    = df["pnl"].mean()
    avg_win    = wins["pnl"].mean() if not wins.empty else 0
    avg_loss   = losses["pnl"].mean() if not losses.empty else 0

    cumulative = df["pnl"].cumsum()
    max_dd     = (cumulative - cumulative.cummax()).min()

    full_p  = len(df[df["result"] == "FULL_PROFIT"])
    partial = len(df[df["result"] == "PARTIAL_LOSS"])
    full_l  = len(df[df["result"] == "FULL_LOSS"])

    print("\n" + "=" * 60)
    print(f"BACKTEST REPORT — {label}  (Last 60 days, 15m bars)")
    print(f"Strategy : Iron Condor | Spot ± {WING_OFFSET} | Wings ± {SPREAD_WIDTH}")
    print(f"Entry    : 11:00 AM | Exit: 2:45 PM | Mon–Sun (all days)")
    print("=" * 60)
    print(f"Total Trades   : {total}")
    print(f"Wins           : {len(wins)}")
    print(f"Losses         : {len(losses)}")
    print(f"Win Rate       : {win_rate:.1f}%")
    print(f"Total PnL      : ₹{total_pnl:,.0f}")
    print(f"Avg PnL/Trade  : ₹{avg_pnl:,.0f}")
    print(f"Avg Win        : ₹{avg_win:,.0f}")
    print(f"Avg Loss       : ₹{avg_loss:,.0f}")
    print(f"Max Drawdown   : ₹{max_dd:,.0f}")
    print(f"\nOutcome Breakdown:")
    print(f"  Full Profit   : {full_p}")
    print(f"  Partial Loss  : {partial}")
    print(f"  Full Loss     : {full_l}")

    if win_rate >= 70:
        print(f"\n🎯 Strong Win Rate: {win_rate:.1f}%")
    elif win_rate >= 55:
        print(f"\n📊 Moderate Win Rate: {win_rate:.1f}%")
    else:
        print(f"\n⚠️  Low Win Rate: {win_rate:.1f}% — filters may need tightening")

    print("\nAll Trades:")
    print(df[["date","spot","lower_short","upper_short","result","pnl"]].to_string(index=False))

    out = f"backtest_ic_{label}.csv"
    df.to_csv(out, index=False)
    print(f"\nSaved → {out}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", choices=["NIFTY", "BANKNIFTY", "BOTH"], default="BOTH")
    args = parser.parse_args()

    print("Fetching India VIX...")
    vix = fetch_vix()

    targets = list(SYMBOLS.items()) if args.symbol == "BOTH" else [(args.symbol, SYMBOLS[args.symbol])]

    for label, (yf_sym, lot) in targets:
        print(f"\nFetching {label} intraday (15m, 60d)...")
        intraday = fetch_intraday(yf_sym)
        if intraday.empty:
            print(f"  No intraday data for {label}")
            continue
        print(f"  {len(intraday)} candles | {intraday.index[0].date()} → {intraday.index[-1].date()}")

        print(f"Fetching {label} daily (90d)...")
        daily = fetch_daily(yf_sym)

        df, stats = run_backtest(label, yf_sym, lot, intraday, daily, vix)
        print_report(df, label, stats)


if __name__ == "__main__":
    main()

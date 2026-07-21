"""
🌙 NIGHT SCALPER MAX YIELD FINAL — EUR/CHF
=====================================================
Parameters below came from a ForexLab Walk-Forward Optimization
pin (not yet independently re-validated with widened ranges).

⚠️ KNOWN ISSUES WITH THESE SPECIFIC PARAMETERS — READ BEFORE
   DEPLOYING LIVE:

1. NIGHT_START=21, NIGHT_END=23 mathematically disables the
   night-session filter. is_night_session() returns True if
   hour >= 21 OR hour < 23 -- that's true for every hour of the
   day (0-22 satisfies "hour < 23", 23 satisfies "hour >= 21").
   This bot will now scan and trade around the clock, not just at
   night. If that's not what you intended, this needs fixing
   before going live.

2. BB_STD=4.0 and ATR_SL_MULT=0.2 both sit exactly at the edge of
   the ranges tested during optimization. That's usually a sign
   the true optimum is outside the tested range, or that the
   optimizer is chasing an extreme rather than a real edge.
   Recommend widening the ranges in ForexLab and re-running
   Walk-Forward before trusting this.

3. ATR_SL_MULT=0.2 is an extremely tight stop relative to
   ATR_TP_MULT=1.8 (a ~9:1 reward-to-risk shape). This kind of
   stop is especially vulnerable to real-world slippage and
   spread widening beyond what any backtest can fully capture.
"""

import os
import time
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from match_trader_api import MatchTraderClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Walk-Forward pinned parameters (see caveats above)
STRATEGY_CONFIG = {
    "EUR_CHF": {
        "BB_PERIOD": 67,
        "BB_STD": 4.0,
        "ATR_SL_MULT": 0.2,
        "ATR_TP_MULT": 1.8
    }
}

INSTRUMENTS  = list(STRATEGY_CONFIG.keys())
NIGHT_START  = 21
NIGHT_END    = 23   # ⚠️ see caveat #1 above -- this disables the night filter
GRANULARITY  = "H1"
CANDLE_COUNT = 100   # bumped from 50 -- BB_PERIOD=67 needs at least 67+ candles
ATR_PERIOD   = 2
RISK_PCT     = 0.01
LOOP_SLEEP   = 300 # Scan every 5 minutes

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        logger.warning(f"Telegram error: {e}")

# ── Indicators ─────────────────────────────────────────────────────────────────
def compute_indicators(df, config):
    df = df.copy()
    bb_p = config["BB_PERIOD"]
    bb_s = config["BB_STD"]
    
    df["bb_mid"]   = df["close"].rolling(bb_p).mean()
    bb_std_val     = df["close"].rolling(bb_p).std()
    df["bb_upper"] = df["bb_mid"] + bb_s * bb_std_val
    df["bb_lower"] = df["bb_mid"] - bb_s * bb_std_val
    
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = df.apply(
        lambda r: max(r["high"] - r["low"],
                      abs(r["high"] - r["prev_close"]),
                      abs(r["low"]  - r["prev_close"])), axis=1)
    df["atr"] = df["tr"].rolling(ATR_PERIOD).mean()
    return df

def is_night_session():
    hour = datetime.now(timezone.utc).hour
    return hour >= NIGHT_START or hour < NIGHT_END

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    client = MatchTraderClient()
    if not client.login():
        logger.error("❌ Login Failed.")
        return

    logger.info("🌙 Night Scalper MAX YIELD Final Bot Started.")
    send_telegram("🌙 Night Scalper MAX YIELD Final Started | EUR/CHF | Risk: 0.5%")

    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.weekday() >= 5: # Skip weekends
                time.sleep(3600)
                continue

            night = is_night_session()
            balance = client.get_balance()
            if balance is None:
                time.sleep(60)
                continue

            for symbol in INSTRUMENTS:
                try:
                    config = STRATEGY_CONFIG[symbol]
                    positions = client.get_open_positions(symbol)
                    
                    # Session end: close all open trades for this symbol
                    if not night and positions:
                        for pos in positions:
                            pos_id   = pos.get("id") or pos.get("positionId")
                            pos_side = pos.get("side", "BUY")
                            pos_vol  = float(pos.get("volume", 0.01))
                            ok, err  = client.close_position(pos_id, symbol, pos_side, pos_vol)
                            if ok:
                                send_telegram(f"⏰ Session End: Closed {symbol} @ market.")
                        continue

                    # If already in a position, don't enter another
                    if positions:
                        continue

                    # If not night session, don't enter new trades
                    if not night:
                        continue

                    # Signal Detection
                    df = client.get_candles(symbol, CANDLE_COUNT, GRANULARITY)
                    if df is None or len(df) < config["BB_PERIOD"] + ATR_PERIOD + 2:
                        continue

                    df   = compute_indicators(df, config)
                    last = df.iloc[-1]

                    bb_upper, bb_lower, atr, close = last["bb_upper"], last["bb_lower"], last["atr"], last["close"]

                    if any(np.isnan(v) for v in [bb_upper, bb_lower, atr]):
                        continue

                    sl_dist = config["ATR_SL_MULT"] * atr
                    lots    = client.calculate_lots(balance, RISK_PCT, sl_dist, symbol)
                    if lots <= 0:
                        continue

                    # LONG Signal
                    if close < bb_lower:
                        sl = round(close - sl_dist, 5)
                        tp = round(close + config["ATR_TP_MULT"] * atr, 5)
                        logger.info(f"🌙 Night LONG {symbol} | Entry:{close} SL:{sl} TP:{tp}")
                        order_id, err = client.open_position(symbol, "BUY", lots, sl, tp)
                        if order_id:
                            send_telegram(f"🌙 Night LONG {symbol} Opened (Max Yield)\nEntry: {close} | SL: {sl} | TP: {tp}")

                    # SHORT Signal
                    elif close > bb_upper:
                        sl = round(close + sl_dist, 5)
                        tp = round(close - config["ATR_TP_MULT"] * atr, 5)
                        logger.info(f"🌙 Night SHORT {symbol} | Entry:{close} SL:{sl} TP:{tp}")
                        order_id, err = client.open_position(symbol, "SELL", lots, sl, tp)
                        if order_id:
                            send_telegram(f"🌙 Night SHORT {symbol} Opened (Max Yield)\nEntry: {close} | SL: {sl} | TP: {tp}")

                except Exception as e:
                    logger.error(f"❌ Error on {symbol}: {e}")

        except Exception as e:
            logger.error(f"🔥 Critical bot error: {e}")

        time.sleep(LOOP_SLEEP)

if __name__ == "__main__":
    main()

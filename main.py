"""
Night Scalper Bot — Optimized Version
=============================================
Strategy : Bollinger Band mean reversion during Asian/London session (21:00–05:00 UTC)
Instruments: AUD/NZD, EUR/CHF
Risk      : 1% per trade (FundingPips 2-Step Pro)
Optimized Period: July 2024 - June 2026
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

# Optimized Parameters per Symbol
STRATEGY_CONFIG = {
    "AUD_NZD": {
        "BB_PERIOD": 10,
        "BB_STD": 1.5,
        "ATR_SL_MULT": 1.5,
        "ATR_TP_MULT": 1.5
    },
    "EUR_CHF": {
        "BB_PERIOD": 30,
        "BB_STD": 1.5,
        "ATR_SL_MULT": 1.0,
        "ATR_TP_MULT": 1.0
    }
}

INSTRUMENTS  = list(STRATEGY_CONFIG.keys())
NIGHT_START  = 21
NIGHT_END    = 5
GRANULARITY  = "H1"
CANDLE_COUNT = 50
ATR_PERIOD   = 14
RISK_PCT     = 0.01
LOOP_SLEEP   = 300

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

# ── Session check ──────────────────────────────────────────────────────────────
def is_night_session():
    hour = datetime.now(timezone.utc).hour
    return hour >= NIGHT_START or hour < NIGHT_END

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    client = MatchTraderClient()
    if not client.login():
        msg = "❌ Night Scalper: Failed to login to FundingPips. Check credentials."
        logger.error(msg)
        send_telegram(msg)
        return

    logger.info("🌙 Night Scalper Bot (OPTIMIZED) started on FundingPips.")
    send_telegram("🌙 Night Scalper Bot (OPTIMIZED) started on FundingPips | Risk: 1%")

    active_trades = {}

    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.weekday() >= 5:
                logger.info("Weekend — sleeping 1h.")
                time.sleep(3600)
                continue

            night = is_night_session()
            balance = client.get_balance()
            if balance is None:
                time.sleep(LOOP_SLEEP)
                continue

            for symbol in INSTRUMENTS:
                try:
                    config = STRATEGY_CONFIG[symbol]
                    positions = client.get_open_positions(symbol)
                    if positions is None:
                        continue

                    # Session end: close all open trades
                    if not night and positions:
                        for pos in positions:
                            pos_id   = pos.get("id") or pos.get("positionId")
                            pos_side = pos.get("side", "BUY")
                            pos_vol  = float(pos.get("volume", 0.01))
                            ok, err  = client.close_position(pos_id, symbol, pos_side, pos_vol)
                            if ok:
                                send_telegram(f"⏰ Session End: Closed {symbol} @ market.")
                                if symbol in active_trades:
                                    del active_trades[symbol]
                        continue

                    if positions:
                        continue

                    if not night:
                        if symbol in active_trades:
                            del active_trades[symbol]
                        continue

                    # Signal Detection
                    df = client.get_candles(symbol, CANDLE_COUNT, GRANULARITY)
                    if df is None or len(df) < config["BB_PERIOD"] + ATR_PERIOD + 2:
                        continue

                    df   = compute_indicators(df, config)
                    last = df.iloc[-1]

                    bb_upper = last["bb_upper"]
                    bb_lower = last["bb_lower"]
                    atr      = last["atr"]
                    close    = last["close"]

                    if any(np.isnan(v) for v in [bb_upper, bb_lower, atr]) or atr <= 0:
                        continue

                    sl_dist = config["ATR_SL_MULT"] * atr
                    lots    = client.calculate_lots(balance, RISK_PCT, sl_dist, symbol)
                    if lots <= 0:
                        continue

                    if close < bb_lower:
                        sl = round(close - sl_dist, 5)
                        tp = round(close + config["ATR_TP_MULT"] * atr, 5)
                        logger.info(f"🌙 Night LONG {symbol} | Entry:{close} SL:{sl} TP:{tp} Lots:{lots}")
                        order_id, err = client.open_position(symbol, "BUY", lots, sl, tp)
                        if order_id:
                            active_trades[symbol] = {"id": order_id}
                            send_telegram(f"🌙 Night LONG {symbol} opened (Optimized)\nEntry: {close} | SL: {sl} | TP: {tp}")

                    elif close > bb_upper:
                        sl = round(close + sl_dist, 5)
                        tp = round(close - config["ATR_TP_MULT"] * atr, 5)
                        logger.info(f"🌙 Night SHORT {symbol} | Entry:{close} SL:{sl} TP:{tp} Lots:{lots}")
                        order_id, err = client.open_position(symbol, "SELL", lots, sl, tp)
                        if order_id:
                            active_trades[symbol] = {"id": order_id}
                            send_telegram(f"🌙 Night SHORT {symbol} opened (Optimized)\nEntry: {close} | SL: {sl} | TP: {tp}")

                except Exception as e:
                    logger.error(f"❌ Error on {symbol}: {e}")

        except Exception as e:
            logger.error(f"🔥 Critical bot error: {e}")

        time.sleep(LOOP_SLEEP)

if __name__ == "__main__":
    main()

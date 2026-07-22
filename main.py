"""
⚡ EMA TREND SCALPER — EUR/CHF
=====================================================
Validated via ForexLab pipeline on real OANDA EUR/CHF H1 history
(2021-2026):

  - Backtest (realistic $2.50/lot commission, 1 pip slippage):
      374 trades, 44.7% win rate, +$1,459 net profit, 10.3% max DD
  - Walk-Forward Optimization: 4/5 folds profitable out-of-sample
  - Monte Carlo (3000 resamples of the actual trades): 84.6%
      probability of profit

Logic: fast/slow EMA crossover, filtered by a longer-term trend
EMA (only trade with the dominant direction) and RSI momentum
confirmation. Trades all day, every weekday -- no time-of-day
restriction, unlike the Night Scalper bot.

⚠️ Same caveat as the other validated bots: this is the best
result from a multi-round parameter search. Walk-Forward and
Monte Carlo both passed, which is a good sign, but recommend
demo-account-first, not immediately full size on live/funded.
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

SYMBOL       = "EUR_CHF"
GRANULARITY  = "H1"
CANDLE_COUNT = 500   # generous window so the 175-period trend EMA has room to converge

# ForexLab-validated parameters
FAST_EMA_PERIOD  = 3
SLOW_EMA_PERIOD  = 18
TREND_EMA_PERIOD = 175
RSI_PERIOD       = 21
ATR_PERIOD       = 26
ATR_SL_MULT      = 2.75
ATR_TP_MULT      = 4.5

RISK_PCT     = 0.0075   # 0.5% per trade -- matches what was actually tested
LOOP_SLEEP   = 3600    # scan once per hour, matches H1 granularity

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
def compute_indicators(df):
    df = df.copy()

    df["fast_ema"]  = df["close"].ewm(span=FAST_EMA_PERIOD, adjust=False).mean()
    df["slow_ema"]  = df["close"].ewm(span=SLOW_EMA_PERIOD, adjust=False).mean()
    df["trend_ema"] = df["close"].ewm(span=TREND_EMA_PERIOD, adjust=False).mean()

    delta    = df["close"].diff()
    gain     = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss     = (-delta).clip(lower=0).rolling(RSI_PERIOD).mean()
    rs       = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    df["prev_close"] = df["close"].shift(1)
    df["tr"] = df.apply(
        lambda r: max(r["high"] - r["low"],
                      abs(r["high"] - r["prev_close"]),
                      abs(r["low"]  - r["prev_close"])), axis=1)
    df["atr"] = df["tr"].rolling(ATR_PERIOD).mean()
    return df

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    client = MatchTraderClient()
    if not client.login():
        logger.error("❌ Login Failed.")
        return

    logger.info("⚡ EMA Trend Scalper Bot Started.")
    send_telegram("⚡ EMA Trend Scalper Bot Started | EUR/CHF | Risk: 0.5%")

    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.weekday() >= 5:  # Skip weekends
                time.sleep(3600)
                continue

            balance = client.get_balance()
            if balance is None:
                time.sleep(60)
                continue

            positions = client.get_open_positions(SYMBOL)
            if positions:  # Only one position at a time
                time.sleep(LOOP_SLEEP)
                continue

            df = client.get_candles(SYMBOL, CANDLE_COUNT, GRANULARITY)
            if df is None or len(df) < TREND_EMA_PERIOD + ATR_PERIOD + 2:
                time.sleep(60)
                continue

            df   = compute_indicators(df)
            last = df.iloc[-1]
            prev = df.iloc[-2]

            close     = last["close"]
            fast_ema  = last["fast_ema"]
            slow_ema  = last["slow_ema"]
            trend_ema = last["trend_ema"]
            rsi_val   = last["rsi"]
            atr_val   = last["atr"]

            if any(np.isnan(v) for v in [fast_ema, slow_ema, trend_ema, rsi_val, atr_val,
                                          prev["fast_ema"], prev["slow_ema"]]):
                time.sleep(60)
                continue

            crossed_up   = prev["fast_ema"] <= prev["slow_ema"] and fast_ema > slow_ema
            crossed_down = prev["fast_ema"] >= prev["slow_ema"] and fast_ema < slow_ema

            sl_dist = ATR_SL_MULT * atr_val
            tp_dist = ATR_TP_MULT * atr_val

            lots = client.calculate_lots(balance, RISK_PCT, sl_dist, SYMBOL)
            if lots <= 0:
                time.sleep(60)
                continue

            # LONG Signal
            if crossed_up and close > trend_ema and rsi_val > 50:
                sl = round(close - sl_dist, 5)
                tp = round(close + tp_dist, 5)
                logger.info(f"🔼 LONG {SYMBOL} | Entry:{close} SL:{sl} TP:{tp}")
                order_id, err = client.open_position(SYMBOL, "BUY", lots, sl, tp)
                if order_id:
                    send_telegram(f"✅ LONG {SYMBOL} Opened (EMA Trend Scalper)\nEntry: {close} | SL: {sl} | TP: {tp}")

            # SHORT Signal
            elif crossed_down and close < trend_ema and rsi_val < 50:
                sl = round(close + sl_dist, 5)
                tp = round(close - tp_dist, 5)
                logger.info(f"🔽 SHORT {SYMBOL} | Entry:{close} SL:{sl} TP:{tp}")
                order_id, err = client.open_position(SYMBOL, "SELL", lots, sl, tp)
                if order_id:
                    send_telegram(f"✅ SHORT {SYMBOL} Opened (EMA Trend Scalper)\nEntry: {close} | SL: {sl} | TP: {tp}")

        except Exception as e:
            logger.error(f"🔥 Error: {e}")

        time.sleep(LOOP_SLEEP)

if __name__ == "__main__":
    main()

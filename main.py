"""
Night Scalper Bot — FundingPips Match-Trader
=============================================
Strategy : Bollinger Band mean reversion during Asian/London session (21:00–05:00 UTC)
Instruments: AUD/NZD, EUR/CHF
Risk      : 1% per trade  (FundingPips 2-Step Pro)

Environment Variables (Railway):
  MT_PLATFORM_URL   = https://mtr-platform.fundingpips.com
  MT_EMAIL          = casematteson6@gmail.com
  MT_PASSWORD       = 2866def46a
  MT_BROKER_ID      = FundingPips
  MT_ACCOUNT_ID     = 2009271
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
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

INSTRUMENTS  = ["AUD_NZD", "EUR_CHF"]
NIGHT_START  = 21     # UTC hour session opens
NIGHT_END    = 5      # UTC hour session closes
GRANULARITY  = "H1"
CANDLE_COUNT = 50

BB_PERIOD    = 20
BB_STD       = 2.0
ATR_PERIOD   = 14
ATR_SL_MULT  = 1.5
ATR_TP_MULT  = 1.5
RISK_PCT     = 0.01   # 1% per trade
LOOP_SLEEP   = 300    # 5 minutes

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
    df["bb_mid"]   = df["close"].rolling(BB_PERIOD).mean()
    bb_std         = df["close"].rolling(BB_PERIOD).std()
    df["bb_upper"] = df["bb_mid"] + BB_STD * bb_std
    df["bb_lower"] = df["bb_mid"] - BB_STD * bb_std
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

    logger.info("🌙 Night Scalper Bot started on FundingPips Match-Trader.")
    send_telegram("🌙 Night Scalper Bot started on FundingPips (AUD/NZD, EUR/CHF) | Risk: 1%")

    active_trades = {}  # symbol -> {position_id, side, sl, tp, lots}

    while True:
        try:

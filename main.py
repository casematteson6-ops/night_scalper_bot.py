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
            now = datetime.now(timezone.utc)

            # Weekend check
            if now.weekday() >= 5:
                logger.info("Weekend — sleeping 1h.")
                time.sleep(3600)
                continue

            night = is_night_session()

            balance = client.get_balance()
            if balance is None:
                logger.warning("⚠️ Could not fetch balance. Retrying.")
                time.sleep(LOOP_SLEEP)
                continue

            for symbol in INSTRUMENTS:
                try:
                    positions = client.get_open_positions(symbol)
                    if positions is None:
                        logger.warning(f"⚠️ Skipping {symbol} — could not retrieve positions.")
                        continue

                    # ── Session end: close all open trades ────────────────────
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
                            else:
                                send_telegram(f"❌ Failed to close {symbol} at session end: {err}")
                        continue

                    # ── Manage existing trade ──────────────────────────────────
                    if positions:
                        continue  # SL/TP managed by OANDA — just wait

                    # Clean up if position closed externally
                    if not positions and symbol in active_trades:
                        send_telegram(f"✅ {symbol} night trade closed (SL/TP hit).")
                        del active_trades[symbol]

                    if not night:
                        continue  # Outside session — don't open new trades

                    # ── Signal Detection ───────────────────────────────────────
                    df = client.get_candles(symbol, CANDLE_COUNT, GRANULARITY)
                    if df is None or len(df) < BB_PERIOD + ATR_PERIOD + 2:
                        logger.warning(f"⚠️ Not enough data for {symbol}.")
                        continue

                    df   = compute_indicators(df)
                    last = df.iloc[-1]

                    bb_upper = last["bb_upper"]
                    bb_lower = last["bb_lower"]
                    atr      = last["atr"]
                    close    = last["close"]

                    if any(np.isnan(v) for v in [bb_upper, bb_lower, atr]) or atr <= 0:
                        continue

                    sl_dist = ATR_SL_MULT * atr
                    lots    = client.calculate_lots(balance, RISK_PCT, sl_dist, symbol)
                    if lots <= 0:
                        continue

                    # LONG: price below lower BB (mean reversion up)
                    if close < bb_lower:
                        sl = round(close - sl_dist, 5)
                        tp = round(close + ATR_TP_MULT * atr, 5)
                        logger.info(f"🌙 Night LONG {symbol} | Entry:{close} SL:{sl} TP:{tp} Lots:{lots}")
                        order_id, err = client.open_position(symbol, "BUY", lots, sl, tp)
                        if order_id:
                            active_trades[symbol] = {"position_id": order_id, "side": "BUY",
                                                      "sl": sl, "tp": tp, "lots": lots}
                            send_telegram(
                                f"🌙 Night LONG {symbol} opened\n"
                                f"Entry: {close} | SL: {sl} | TP: {tp}\n"
                                f"Lots: {lots} | Risk: ${round(balance * RISK_PCT, 2)}"
                            )
                        else:
                            logger.warning(f"❌ Night LONG {symbol} failed: {err}")
                            send_telegram(f"❌ Night LONG {symbol} failed: {err}")

                    # SHORT: price above upper BB (mean reversion down)
                    elif close > bb_upper:
                        sl = round(close + sl_dist, 5)
                        tp = round(close - ATR_TP_MULT * atr, 5)
                        logger.info(f"🌙 Night SHORT {symbol} | Entry:{close} SL:{sl} TP:{tp} Lots:{lots}")
                        order_id, err = client.open_position(symbol, "SELL", lots, sl, tp)
                        if order_id:
                            active_trades[symbol] = {"position_id": order_id, "side": "SELL",
                                                      "sl": sl, "tp": tp, "lots": lots}
                            send_telegram(
                                f"🌙 Night SHORT {symbol} opened\n"
                                f"Entry: {close} | SL: {sl} | TP: {tp}\n"
                                f"Lots: {lots} | Risk: ${round(balance * RISK_PCT, 2)}"
                            )
                        else:
                            logger.warning(f"❌ Night SHORT {symbol} failed: {err}")
                            send_telegram(f"❌ Night SHORT {symbol} failed: {err}")

                except Exception as e:
                    logger.error(f"❌ Error on {symbol}: {e}")
                    send_telegram(f"❌ Night Scalper error on {symbol}: {e}")

        except Exception as e:
            logger.error(f"🔥 Critical bot error: {e}")
            send_telegram(f"🔥 Night Scalper critical error: {e}")

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    main()

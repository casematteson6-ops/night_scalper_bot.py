"""
🌙 NIGHT SCALPER MAX YIELD — v2 (Challenge Edition)
===================================================
EUR/CHF & AUD/NZD Multi-Pair
Risk: 0.5% per trade (Safer for Prop Firm Challenges)
"""
import os, time, logging, requests, pandas as pd, numpy as np
from datetime import datetime, timezone
from match_trader_api import MatchTraderClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STRATEGY_CONFIG = {
    "EUR_CHF": {"BB_PERIOD": 30, "BB_STD": 1.5, "ATR_SL_MULT": 1.0, "ATR_TP_MULT": 1.0},
    "AUD_NZD": {"BB_PERIOD": 20, "BB_STD": 1.5, "ATR_SL_MULT": 1.5, "ATR_TP_MULT": 2.0}
}
INSTRUMENTS, NIGHT_START, NIGHT_END = list(STRATEGY_CONFIG.keys()), 21, 5
GRANULARITY, CANDLE_COUNT, ATR_PERIOD = "H1", 50, 14
RISK_PCT = 0.005 # 0.5% Risk
LOOP_SLEEP = 300

def compute_indicators(df, config):
    df = df.copy()
    df["bb_mid"] = df["close"].rolling(config["BB_PERIOD"]).mean()
    std = df["close"].rolling(config["BB_PERIOD"]).std()
    df["bb_upper"], df["bb_lower"] = df["bb_mid"] + config["BB_STD"]*std, df["bb_mid"] - config["BB_STD"]*std
    df["tr"] = df.apply(lambda r: max(r["high"]-r["low"], abs(r["high"]-df.shift(1).loc[r.name, "close"]), abs(r["low"]-df.shift(1).loc[r.name, "close"])) if r.name in df.index else 0, axis=1)
    df["atr"] = df["tr"].rolling(ATR_PERIOD).mean()
    return df

def main():
    client = MatchTraderClient()
    if not client.login(): return
    while True:
        try:
            if datetime.now(timezone.utc).weekday() >= 5: time.sleep(3600); continue
            night, balance = (datetime.now(timezone.utc).hour >= NIGHT_START or datetime.now(timezone.utc).hour < NIGHT_END), client.get_balance()
            for symbol in INSTRUMENTS:
                config, positions = STRATEGY_CONFIG[symbol], client.get_open_positions(symbol)
                if not night and positions:
                    for pos in positions: client.close_position(pos.get("id") or pos.get("positionId"), symbol, pos.get("side", "BUY"), float(pos.get("volume", 0.01)))
                elif not positions and night:
                    df = compute_indicators(client.get_candles(symbol, CANDLE_COUNT, GRANULARITY), config)
                    last = df.iloc[-1]
                    sl_dist = config["ATR_SL_MULT"] * last["atr"]
                    lots = client.calculate_lots(balance, RISK_PCT, sl_dist, symbol)
                    if last["close"] < last["bb_lower"]: client.open_position(symbol, "BUY", lots, round(last["close"]-sl_dist, 5), round(last["close"]+config["ATR_TP_MULT"]*last["atr"], 5))
                    elif last["close"] > last["bb_upper"]: client.open_position(symbol, "SELL", lots, round(last["close"]+sl_dist, 5), round(last["close"]-config["ATR_TP_MULT"]*last["atr"], 5))
        except Exception as e: logger.error(e)
        time.sleep(LOOP_SLEEP)

if __name__ == "__main__": main()

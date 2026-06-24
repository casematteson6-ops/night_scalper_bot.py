import os
import time
import pandas as pd
import numpy as np
import requests
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades
import oandapyV20.endpoints.accounts as accounts
from datetime import datetime, timezone

# --- CONFIGURATION ---
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENVIRONMENT = os.getenv("OANDA_ENVIRONMENT", "practice").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# --- NIGHT SCALPER PARAMETERS ---
PAIRS = ["AUD_NZD", "EUR_CHF"]
NIGHT_START = 21  # UTC hour — session opens
NIGHT_END = 5     # UTC hour — session closes
BB_PERIOD = 20
BB_STD = 2.0
ATR_PERIOD = 14
ATR_SL_MULT = 1.5
ATR_TP_MULT = 1.5
RISK_PER_TRADE = 0.01   # 1% risk per trade (prop firm safe)
MAX_RETRIES = 5         # Max retries for any API call before giving up
RETRY_DELAY = 15        # Seconds to wait between retries

# --- Oanda API Client ---
# Increased timeout to 30s to prevent premature timeouts on open trades calls
api = oandapyV20.API(
    access_token=OANDA_API_KEY,
    environment=OANDA_ENVIRONMENT,
    request_params={"timeout": 30}
)

# --- HELPERS ---
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"Telegram not configured: {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram send failed: {e}")

def api_call_with_retry(endpoint, description="API call"):
    """
    Wraps any oandapyV20 API call with retry logic.
    Retries up to MAX_RETRIES times on timeout or connection errors.
    Returns the endpoint object on success, or None on failure.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            api.request(endpoint)
            return endpoint
        except Exception as e:
            error_str = str(e).lower()
            is_timeout = "timed out" in error_str or "timeout" in error_str or "connection" in error_str
            if is_timeout and attempt < MAX_RETRIES:
                print(f"⚠️ {description} timed out (attempt {attempt}/{MAX_RETRIES}). Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"❌ {description} failed after {attempt} attempts: {e}")
                return None
    return None

def get_open_trades_for_instrument(instrument):
    """Returns open trades for a given instrument sorted oldest-first (FIFO)."""
    r = trades.OpenTrades(accountID=OANDA_ACCOUNT_ID)
    result = api_call_with_retry(r, f"get open trades for {instrument}")
    if result is None:
        return None  # Signal a failure, not an empty list
    instrument_trades = [t for t in result.response.get("trades", []) if t["instrument"] == instrument]
    instrument_trades.sort(key=lambda x: x["openTime"])
    return instrument_trades

def get_account_balance():
    """Returns account balance with retry logic."""
    r = accounts.AccountSummary(accountID=OANDA_ACCOUNT_ID)
    result = api_call_with_retry(r, "get account balance")
    if result is None:
        return None
    return float(result.response["account"]["balance"])

def get_candles(symbol, count=50, granularity="H1"):
    """Fetches OHLCV candles with retry logic."""
    r = instruments.InstrumentsCandles(
        instrument=symbol,
        params={"count": count, "granularity": granularity}
    )
    result = api_call_with_retry(r, f"get candles for {symbol}")
    if result is None:
        return None
    data = []
    for candle in result.response["candles"]:
        if candle["complete"]:
            data.append({
                "High": float(candle["mid"]["h"]),
                "Low": float(candle["mid"]["l"]),
                "Close": float(candle["mid"]["c"])
            })
    if not data:
        return None
    df = pd.DataFrame(data)
    df["MA20"] = df["Close"].rolling(window=BB_PERIOD).mean()
    df["STD20"] = df["Close"].rolling(window=BB_PERIOD).std()
    df["UpperBB"] = df["MA20"] + (df["STD20"] * BB_STD)
    df["LowerBB"] = df["MA20"] - (df["STD20"] * BB_STD)
    high_low = df["High"] - df["Low"]
    high_close = np.abs(df["High"] - df["Close"].shift(1))
    low_close = np.abs(df["Low"] - df["Close"].shift(1))
    df["ATR"] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(window=ATR_PERIOD).mean()
    return df

def place_order(symbol, units, sl_price, tp_price):
    """Places a market order with SL/TP and returns (tradeID, fill_price, error)."""
    order_body = {
        "order": {
            "type": "MARKET",
            "instrument": symbol,
            "units": str(int(units)),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": f"{sl_price:.5f}"},
            "takeProfitOnFill": {"price": f"{tp_price:.5f}"}
        }
    }
    print(f"DEBUG: Attempting {symbol} order: Units={int(units)}, SL={sl_price:.5f}, TP={tp_price:.5f}")
    r = orders.OrderCreate(accountID=OANDA_ACCOUNT_ID, data=order_body)
    result = api_call_with_retry(r, f"place order for {symbol}")
    if result is None:
        return None, None, "Order request timed out after all retries"

    if "orderFillTransaction" in result.response:
        fill = result.response["orderFillTransaction"]
        return fill["tradeID"], float(fill["price"]), None

    cancel_reason = "Unknown rejection reason"
    if "orderCancelTransaction" in result.response:
        cancel_reason = result.response["orderCancelTransaction"].get("reason", cancel_reason)
    return None, None, f"Order cancelled. Reason: {cancel_reason}"

def close_trade_by_id(trade_id, symbol):
    """Closes a specific trade by ID (FIFO compliant)."""
    r = trades.TradeClose(accountID=OANDA_ACCOUNT_ID, tradeID=trade_id, data={"units": "ALL"})
    result = api_call_with_retry(r, f"close trade {trade_id} for {symbol}")
    if result is None:
        send_telegram(f"❌ *CLOSE FAILED:* {symbol} trade {trade_id} — timed out after retries.")
        return False
    return True

# --- MAIN LOOP ---
def run_night_scalper():
    print("🌙 Night Scalper Bot Starting...")

    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        msg = "❌ CRITICAL: OANDA_API_KEY or OANDA_ACCOUNT_ID not set!"
        print(msg)
        send_telegram(msg)
        return

    print(f"DEBUG: OANDA_API_KEY loaded: {OANDA_API_KEY[:4]}...{OANDA_API_KEY[-4:]}")
    print(f"DEBUG: OANDA_ACCOUNT_ID loaded: {OANDA_ACCOUNT_ID}")
    print(f"DEBUG: OANDA_ENVIRONMENT: {OANDA_ENVIRONMENT}")

    send_telegram(f"🌙 *Night Scalper Bot Live*\nPairs: {', '.join(PAIRS)}\nRisk: {RISK_PER_TRADE*100}% per trade")

    # Track open trades by tradeID for FIFO compliance
    active_virtual_trades = {}

    while True:
        try:
            now = datetime.now(timezone.utc)

            # Skip weekends entirely
            if now.weekday() >= 5:
                time.sleep(3600)
                continue

            is_night = (now.hour >= NIGHT_START or now.hour < NIGHT_END)

            for symbol in PAIRS:
                try:
                    open_trades = get_open_trades_for_instrument(symbol)

                    # If API call failed (None), skip this pair this cycle — do NOT crash
                    if open_trades is None:
                        print(f"⚠️ Skipping {symbol} this cycle — could not retrieve open trades.")
                        continue

                    # --- Session end: close all open trades ---
                    if not is_night and open_trades:
                        for t in open_trades:
                            trade_id = t["id"]
                            if close_trade_by_id(trade_id, symbol):
                                send_telegram(f"⏰ *Session End:* Closed {symbol} trade @ market.")
                                if trade_id in active_virtual_trades:
                                    del active_virtual_trades[trade_id]
                        continue

                    # --- Night session: manage existing trade ---
                    if open_trades:
                        current_trade = open_trades[0]
                        trade_id = current_trade["id"]

                        if trade_id in active_virtual_trades:
                            vt = active_virtual_trades[trade_id]
                            df = get_candles(symbol, count=1, granularity="M1")
                            if df is None or df.empty:
                                continue
                            price = df["Close"].iloc[-1]

                            # Check virtual SL
                            if (vt["dir"] == "long" and price <= vt["sl"]) or \
                               (vt["dir"] == "short" and price >= vt["sl"]):
                                if close_trade_by_id(trade_id, symbol):
                                    send_telegram(f"⏹ *CLOSED {symbol}* (SL Hit) @ {price:.5f}")
                                    del active_virtual_trades[trade_id]
                        continue

                    # --- Night session: look for new entry ---
                    if not is_night:
                        continue

                    df = get_candles(symbol, count=50, granularity="H1")
                    if df is None or len(df) < BB_PERIOD:
                        continue

                    last = df.iloc[-1]
                    if pd.isna(last["ATR"]) or pd.isna(last["LowerBB"]):
                        continue

                    balance = get_account_balance()
                    if balance is None:
                        print(f"⚠️ Skipping {symbol} — could not get balance.")
                        continue

                    risk_amount = balance * RISK_PER_TRADE
                    atr = last["ATR"]
                    sl_dist = ATR_SL_MULT * atr

                    if sl_dist <= 0:
                        continue

                    # LONG: price below lower BB (mean reversion back up)
                    if last["Close"] < last["LowerBB"]:
                        sl_price = last["Close"] - sl_dist
                        tp_price = last["Close"] + (ATR_TP_MULT * atr)
                        # Correct unit calculation: risk_amount / sl_distance_in_price
                        units = int(risk_amount / sl_dist)
                        if units > 0:
                            tid, fill_price, error = place_order(symbol, units, sl_price, tp_price)
                            if tid:
                                active_virtual_trades[tid] = {"sl": sl_price, "tp": tp_price, "dir": "long", "entry": fill_price, "atr": atr}
                                send_telegram(f"🌙 *Night LONG {symbol}*\nEntry: {fill_price:.5f}\nSL: {sl_price:.5f}\nTP: {tp_price:.5f}\nUnits: {units}\nRisk: ${risk_amount:.2f}")
                            else:
                                send_telegram(f"❌ *Order Failed {symbol}:* {error}")

                    # SHORT: price above upper BB (mean reversion back down)
                    elif last["Close"] > last["UpperBB"]:
                        sl_price = last["Close"] + sl_dist
                        tp_price = last["Close"] - (ATR_TP_MULT * atr)
                        units = int(risk_amount / sl_dist)
                        if units > 0:
                            tid, fill_price, error = place_order(symbol, -units, sl_price, tp_price)
                            if tid:
                                active_virtual_trades[tid] = {"sl": sl_price, "tp": tp_price, "dir": "short", "entry": fill_price, "atr": atr}
                                send_telegram(f"🌙 *Night SHORT {symbol}*\nEntry: {fill_price:.5f}\nSL: {sl_price:.5f}\nTP: {tp_price:.5f}\nUnits: {units}\nRisk: ${risk_amount:.2f}")
                            else:
                                send_telegram(f"❌ *Order Failed {symbol}:* {error}")

                except Exception as pair_err:
                    # Log the error but NEVER crash — move on to next pair
                    print(f"⚠️ Error processing {symbol}: {pair_err}")
                    time.sleep(10)
                    continue

            time.sleep(300)  # Check every 5 minutes

        except Exception as loop_err:
            # Outer safety net — log, notify, and keep running
            print(f"🔥 Main loop error: {loop_err}")
            send_telegram(f"🔥 *Night Scalper Error:* {loop_err}\nBot is still running.")
            time.sleep(60)

if __name__ == "__main__":
    run_night_scalper()

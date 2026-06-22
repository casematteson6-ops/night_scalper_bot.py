import os
import time
import pandas as pd
import numpy as np
import requests
from v20 import Context
from datetime import datetime, timezone

# --- CONFIGURATION ---
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_URL = os.getenv("OANDA_URL", "api-fxpractice.oanda.com")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- NIGHT SCALPER PARAMETERS ---
PAIRS = ["AUD_NZD", "EUR_CHF"]
NIGHT_START = 21 # UTC
NIGHT_END = 5    # UTC
BB_PERIOD = 20
BB_STD = 2.0
ATR_PERIOD = 14
ATR_SL_MULT = 1.5
ATR_TP_MULT = 1.5
RISK_PER_TRADE = 0.005 

# --- HELPERS ---
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"Failed to send Telegram: {e}")

def get_ohlcv(ctx, symbol):
    try:
        response = ctx.instrument.candles(symbol, granularity="H1", count=50)
        candles = response.get("candles", 200)
        data = [{"High": float(c.mid.h), "Low": float(c.mid.l), "Close": float(c.mid.c)} for c in candles if c.complete]
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
    except Exception as e:
        print(f"Error fetching OHLCV for {symbol}: {e}")
        return None

def place_order(ctx, symbol, units, sl, tp):
    try:
        sl_price = round(sl, 5)
        tp_price = round(tp, 5)
        
        # OANDA v20 library requires the instrument to be a separate argument in .market()
        # and NOT inside the 'order' dictionary for some versions.
        order_conf = {
            "type": "MARKET", 
            "instrument": symbol, 
            "units": str(units),
            "timeInForce": "FOK",
            "stopLossOnFill": {"price": f"{sl_price:.5f}"},
            "takeProfitOnFill": {"price": f"{tp_price:.5f}"}
        }
        
        print(f"DEBUG: Attempting {symbol} order: Units={units}, SL={sl_price}, TP={tp_price}")
        # Passing order_conf as keyword arguments
        response = ctx.order.market(OANDA_ACCOUNT_ID, **order_conf)
        
        if response.status != 201:
            body = response.body
            err_msg = body.get("errorMessage", "Order Failed")
            
            reject_reason = "N/A"
            if "orderRejectTransaction" in body:
                reject_obj = body["orderRejectTransaction"]
                reject_reason = getattr(reject_obj, "rejectReason", "Unknown Reason")
            
            msg = f"❌ Order Rejected for {symbol}!\nStatus: {response.status}\nReason: {err_msg}\nReject: {reject_reason}"
            print(msg)
            send_telegram(msg)
            return None
            
        print(f"✅ Order Successful: {symbol}")
        return response
    except Exception as e:
        print(f"Error in place_order for {symbol}: {e}")
        return None

# --- MAIN LOOP ---
def run_night_scalper():
    print("🌙 Night Scalper Bot Starting...")
    
    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        error_msg = "❌ CRITICAL: OANDA_API_KEY or OANDA_ACCOUNT_ID not set!"
        print(error_msg)
        send_telegram(error_msg)
        return

    print(f"DEBUG: OANDA_API_KEY loaded: {OANDA_API_KEY[:4]}...{OANDA_API_KEY[-4:] if OANDA_API_KEY else 'NONE'}")
    print(f"DEBUG: OANDA_ACCOUNT_ID loaded: {OANDA_ACCOUNT_ID}")

    ctx = Context(OANDA_URL, 443, token=OANDA_API_KEY)
    
    send_telegram("🌙 Night Scalper Bot Live (AUD_NZD, EUR_CHF)")
    
    while True:
        try:
            now = datetime.now(timezone.utc)
            is_night = (now.hour >= NIGHT_START or now.hour <= NIGHT_END)
            
            if now.weekday() >= 5:
                time.sleep(3600); continue
                
            for symbol in PAIRS:
                try:
                    trades_resp = ctx.trade.list_open(OANDA_ACCOUNT_ID)
                    if trades_resp.status != 200:
                        print(f"API Error {trades_resp.status}: {trades_resp.body}")
                        if trades_resp.status == 403:
                            send_telegram(f"❌ 403 Forbidden: Check OANDA permissions for account {OANDA_ACCOUNT_ID}")
                            time.sleep(3600)
                        continue
                        
                    open_trades = [t for t in trades_resp.get("trades", 200) if t.instrument == symbol]
                    
                    if not is_night and open_trades:
                        for t in open_trades:
                            ctx.trade.close(OANDA_ACCOUNT_ID, t.id)
                            send_telegram(f"⏰ Session End: Closed {symbol} trade.")
                        continue
                        
                    if is_night and not open_trades:
                        df = get_ohlcv(ctx, symbol)
                        if df is None or len(df) < 20: continue
                        last = df.iloc[-1]
                        
                        summary_resp = ctx.account.summary(OANDA_ACCOUNT_ID)
                        summary = summary_resp.get("account", 200)
                        balance = float(summary.balance)
                        
                        atr = last["ATR"]
                        sl_dist = ATR_SL_MULT * atr
                        
                        # --- LEVERAGE CAP ---
                        # To prevent "Insufficient Margin", we limit the max trade size.
                        # Max units = Balance * Max Leverage (e.g., 20)
                        MAX_LEVERAGE = 20
                        max_units_cap = int(balance * MAX_LEVERAGE)
                        
                        if last["Close"] < last["LowerBB"]:
                            sl = last["Close"] - sl_dist
                            tp = last["Close"] + (ATR_TP_MULT * atr)
                            
                            if sl_dist > 0:
                                units = int((balance * RISK_PER_TRADE) / sl_dist)
                                # Apply Leverage Cap
                                units = min(units, max_units_cap)
                                if units > 0:
                                    place_order(ctx, symbol, units, sl, tp)
                                    send_telegram(f"🌙 Night LONG {symbol}\nPrice: {last['Close']}\nUnits: {units}")
                        
                        elif last["Close"] > last["UpperBB"]:
                            sl = last["Close"] + sl_dist
                            tp = last["Close"] - (ATR_TP_MULT * atr)
                            
                            if sl_dist > 0:
                                units = int((balance * RISK_PER_TRADE) / sl_dist) * -1
                                # Apply Leverage Cap (using abs for negative units)
                                if abs(units) > max_units_cap:
                                    units = -max_units_cap
                                    
                                if units < 0:
                                    place_order(ctx, symbol, units, sl, tp)
                                    send_telegram(f"🌙 Night SHORT {symbol}\nPrice: {last['Close']}\nUnits: {units}")
                
                except Exception as api_err:
                    print(f"API interaction error: {api_err}")
                    time.sleep(60)
                    
            time.sleep(300)
            
        except Exception as loop_err:
            print(f"Main loop error: {loop_err}")
            time.sleep(60)

if __name__ == "__main__":
    run_night_scalper()

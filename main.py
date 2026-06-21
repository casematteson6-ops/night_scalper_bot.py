import os
import time
import pandas as pd
import numpy as np
import requests
from v20 import Context
from datetime import datetime, timezone # Added timezone here

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
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message} )

def get_ohlcv(ctx, symbol):
    try:
        response = ctx.instrument.candles(symbol, granularity="H1", count=50)
        candles = response.get("candles", 200)
        data = [{"High": float(c.mid.h), "Low": float(c.mid.l), "Close": float(c.mid.c)} for c in candles if c.complete]
        df = pd.DataFrame(data)
        df['MA20'] = df['Close'].rolling(window=BB_PERIOD).mean()
        df['STD20'] = df['Close'].rolling(window=BB_PERIOD).std()
        df['UpperBB'] = df['MA20'] + (df['STD20'] * BB_STD)
        df['LowerBB'] = df['MA20'] - (df['STD20'] * BB_STD)
        high_low = df['High'] - df['Low']
        high_close = np.abs(df['High'] - df['Close'].shift(1))
        low_close = np.abs(df['Low'] - df['Close'].shift(1))
        df['ATR'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(window=ATR_PERIOD).mean()
        return df
    except: return None

def place_order(ctx, symbol, units, sl, tp):
    order_conf = {
        "order": {
            "type": "MARKET", "instrument": symbol, "units": str(units),
            "timeInForce": "FOK",
            "stopLossOnFill": {"price": f"{sl:.5f}"},
            "takeProfitOnFill": {"price": f"{tp:.5f}"}
        }
    }
    return ctx.order.market(OANDA_ACCOUNT_ID, **order_conf)

# --- MAIN LOOP ---
def run_night_scalper():
    print("🌙 Night Scalper Bot Started...")
    send_telegram("🌙 Night Scalper Bot Live (AUD_NZD, EUR_CHF)")
    ctx = Context(OANDA_URL, 443, token=OANDA_API_KEY)
    
    while True:
        # FIXED: Use timezone-aware UTC time to remove the DeprecationWarning
        now = datetime.now(timezone.utc)
        is_night = (now.hour >= NIGHT_START or now.hour <= NIGHT_END)
        
        if now.weekday() >= 5:
            time.sleep(3600); continue
            
        for symbol in PAIRS:
            trades_resp = ctx.trade.list_open(OANDA_ACCOUNT_ID)
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
                
                if last['Close'] < last['LowerBB']:
                    balance = float(ctx.account.summary(OANDA_ACCOUNT_ID).get("account", 200).balance)
                    sl = last['Close'] - (ATR_SL_MULT * last['ATR'])
                    tp = last['Close'] + (ATR_TP_MULT * last['ATR'])
                    units = int((balance * RISK_PER_TRADE) / (last['Close'] - sl))
                    place_order(ctx, symbol, units, sl, tp)
                    send_telegram(f"🌙 Night LONG {symbol}\nPrice: {last['Close']}\nUnits: {units}")
                
                elif last['Close'] > last['UpperBB']:
                    balance = float(ctx.account.summary(OANDA_ACCOUNT_ID).get("account", 200).balance)
                    sl = last['Close'] + (ATR_SL_MULT * last['ATR'])
                    tp = last['Close'] - (ATR_TP_MULT * last['ATR'])
                    units = int((balance * RISK_PER_TRADE) / (sl - last['Close'])) * -1
                    place_order(ctx, symbol, units, sl, tp)
                    send_telegram(f"🌙 Night SHORT {symbol}\nPrice: {last['Close']}\nUnits: {units}")
                    
        time.sleep(300)

if __name__ == "__main__":
    run_night_scalper()

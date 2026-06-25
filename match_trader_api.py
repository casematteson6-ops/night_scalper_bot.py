"""
Match-Trader API Client for FundingPips
========================================
Handles authentication, token refresh, and all API calls
for the FundingPips Match-Trader platform.

Authentication flow (from official Match-Trader docs):
1. POST /manager/mtr-login  
   → returns tradingApiToken (for Auth-trading-api header)
   → returns tradingAccountToken.token (for Cookie: co-auth=<token>)
2. All trading endpoints use: /mtr-api/<systemUUID>/<endpoint>
3. Token expires in 15 minutes → refresh via POST /manager/refresh-token
"""

import os
import time
import requests
import pandas as pd
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ── Environment Variables ──────────────────────────────────────────────────────
MT_PLATFORM_URL = os.getenv("MT_PLATFORM_URL", "https://mtr-platform.fundingpips.com").rstrip("/")
MT_EMAIL        = os.getenv("MT_EMAIL", "")
MT_PASSWORD     = os.getenv("MT_PASSWORD", "")
MT_BROKER_ID    = os.getenv("MT_BROKER_ID", "FundingPips")
MT_ACCOUNT_ID   = os.getenv("MT_ACCOUNT_ID", "")  # e.g. "2009271"


class MatchTraderClient:
    """
    Authenticated client for the Match-Trader Platform API.
    One instance per bot. Handles login, token refresh, and all trading calls.
    """

    def __init__(self):
        self.platform_url        = MT_PLATFORM_URL
        self.email               = MT_EMAIL
        self.password            = MT_PASSWORD
        self.broker_id           = MT_BROKER_ID
        self.account_id          = MT_ACCOUNT_ID

        self.trading_api_token   = None   # Auth-trading-api header
        self.account_token       = None   # Cookie: co-auth=<token>
        self.system_uuid         = None   # /mtr-api/<systemUUID>/...
        self.token_expiry        = datetime.min

    # ── Authentication ─────────────────────────────────────────────────────────

    def login(self):
        """Login and extract tokens. Returns True on success."""
        url = f"{self.platform_url}/manager/mtr-login"
        payload = {
            "email":    self.email,
            "password": self.password,
            "brokerId": self.broker_id,
        }
        try:
            resp = requests.post(url, json=payload,
                                 headers={"Content-Type": "application/json"},
                                 timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # Find the correct account by account ID
            accounts = data.get("tradingAccounts", [])
            target = None
            for acct in accounts:
                if str(acct.get("tradingAccountId", "")) == str(self.account_id):
                    target = acct
                    break

            # Fall back to selectedTradingAccount or first account
            if target is None:
                target = data.get("selectedTradingAccount") or (accounts[0] if accounts else None)

            if target is None:
                logger.error("Login failed: no trading account found.")
                return False

            self.trading_api_token = target.get("tradingApiToken", "")
            self.account_token     = (target.get("tradingAccountToken") or {}).get("token", "")

            # Extract systemUUID for trading endpoints
            system = (target.get("offer") or {}).get("system") or {}
            self.system_uuid = system.get("uuid", "")

            # Token valid 15 min; refresh after 12
            self.token_expiry = datetime.now() + timedelta(minutes=12)

            logger.info(f"✅ Login OK | Account: {target.get('tradingAccountId')} | SystemUUID: {self.system_uuid}")
            return True

        except Exception as e:
            logger.error(f"❌ Login error: {e}")
            return False

    def refresh_token(self):
        """Refresh session token. Falls back to full re-login if refresh fails."""
        url = f"{self.platform_url}/manager/refresh-token"
        try:
            resp = requests.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "Cookie": f"co-auth={self.account_token}",
                },
                timeout=30,
            )
            if resp.status_code == 200:
                self.token_expiry = datetime.now() + timedelta(minutes=12)
                logger.info("🔄 Token refreshed.")
                return True
        except Exception as e:
            logger.warning(f"Token refresh error: {e}")

        logger.warning("Refresh failed — re-logging in...")
        return self.login()

    def ensure_auth(self):
        """Call before every API request."""
        if self.trading_api_token is None:
            return self.login()
        if datetime.now() >= self.token_expiry:
            return self.refresh_token()
        return True

    def _headers(self):
        return {
            "Auth-trading-api": self.trading_api_token,
            "Cookie":           f"co-auth={self.account_token}",
            "Content-Type":     "application/json",
            "Accept":           "application/json",
        }

    # ── Core Request ───────────────────────────────────────────────────────────

    def request(self, method, path, retries=5, delay=15, **kwargs):
        """
        Authenticated request with retry logic.
        path: relative e.g. '/mtr-api/<uuid>/balance'
        """
        for attempt in range(1, retries + 1):
            try:
                self.ensure_auth()
                url  = f"{self.platform_url}{path}"
                resp = requests.request(method, url, headers=self._headers(),
                                        timeout=30, **kwargs)

                if resp.status_code == 401:
                    logger.warning(f"401 on attempt {attempt} — re-logging in...")
                    self.login()
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.Timeout:
                logger.warning(f"⚠️ Timeout attempt {attempt}/{retries}. Retry in {delay}s...")
            except requests.exceptions.RequestException as e:
                logger.warning(f"⚠️ Request error attempt {attempt}/{retries}: {e}. Retry in {delay}s...")
            except Exception as e:
                logger.warning(f"⚠️ Unexpected error attempt {attempt}/{retries}: {e}. Retry in {delay}s...")

            if attempt < retries:
                time.sleep(delay)

        logger.error(f"❌ {method} {path} failed after {retries} attempts.")
        return None

    # ── Trading Helpers ────────────────────────────────────────────────────────

    def get_balance(self):
        """Returns account balance as float, or None."""
        data = self.request("GET", f"/mtr-api/{self.system_uuid}/balance")
        if data is None:
            return None
        try:
            return float(data.get("balance", 0))
        except Exception as e:
            logger.error(f"Balance parse error: {e} | {data}")
            return None

    def get_open_positions(self, symbol=None):
        """
        Returns list of open positions (optionally filtered by symbol).
        Returns None on API failure so callers can skip safely.
        Each position has: id, symbol, volume, side, openPrice, stopLoss, takeProfit, profit
        """
        data = self.request("GET", f"/mtr-api/{self.system_uuid}/open-positions")
        if data is None:
            return None
        positions = data.get("positions", [])
        if symbol:
            mt_sym = symbol.replace("_", "").upper()
            positions = [p for p in positions if p.get("symbol", "").upper() == mt_sym]
        return positions

    def open_position(self, symbol, side, lots, sl_price, tp_price):
        """
        Opens a market position.
        symbol: 'EUR_USD' or 'EURUSD'
        side: 'BUY' or 'SELL'
        lots: float e.g. 0.10
        sl_price / tp_price: float (pass 0 if not used)
        Returns (order_id, error_message)
        """
        mt_sym = symbol.replace("_", "")
        payload = {
            "instrument": mt_sym,
            "orderSide":  side.upper(),
            "volume":     round(lots, 2),
            "slPrice":    round(sl_price, 5) if sl_price else 0,
            "tpPrice":    round(tp_price, 5) if tp_price else 0,
            "isMobile":   False,
        }
        data = self.request("POST", f"/mtr-api/{self.system_uuid}/open-position", json=payload)
        if data is None:
            return None, "API call failed after retries"

        status   = data.get("status", "")
        error    = data.get("errorMessage", "")
        order_id = data.get("orderId", "")

        if status == "OK":
            return order_id, None
        else:
            reason = error or data.get("nativeCode", "Unknown rejection")
            return None, reason

    def close_position(self, position_id, symbol, open_side, volume):
        """
        Closes a specific position by ID.
        open_side: the side the position was opened with ('BUY' or 'SELL')
        """
        mt_sym     = symbol.replace("_", "")
        close_side = "SELL" if open_side.upper() == "BUY" else "BUY"
        payload = {
            "positionId": str(position_id),
            "instrument": mt_sym,
            "orderSide":  close_side,
            "volume":     str(round(volume, 2)),
        }
        data = self.request("POST", f"/mtr-api/{self.system_uuid}/close-positions", json=payload)
        if data is None:
            return False, "API call failed after retries"

        status = data.get("status", "")
        error  = data.get("errorMessage", "")

        if status == "OK":
            return True, None
        else:
            reason = error or data.get("nativeCode", "Unknown close error")
            return False, reason

    def get_candles(self, symbol, count, granularity="H1"):
        """
        Fetches OHLC candle data as a pandas DataFrame.
        granularity: 'M1','M5','M15','M30','H1','H4','D1'
        Returns DataFrame with columns [open, high, low, close] or None.
        """
        mt_sym  = symbol.replace("_", "")
        gran_map = {
            "M1":  "ONE_MINUTE",
            "M5":  "FIVE_MINUTES",
            "M15": "FIFTEEN_MINUTES",
            "M30": "THIRTY_MINUTES",
            "H1":  "ONE_HOUR",
            "H4":  "FOUR_HOURS",
            "D":   "ONE_DAY",
            "D1":  "ONE_DAY",
        }
        mt_gran = gran_map.get(granularity.upper(), "ONE_HOUR")
        path    = f"/mtr-api/{self.system_uuid}/candles?symbol={mt_sym}&timeFrame={mt_gran}&count={count}"
        data    = self.request("GET", path)

        if data is None:
            return None

        candles = data.get("candles", [])
        if not candles:
            logger.warning(f"No candles returned for {symbol}")
            return None

        try:
            df = pd.DataFrame(candles)
            for col in ["open", "high", "low", "close"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df.dropna(subset=["open", "high", "low", "close"], inplace=True)
            df.reset_index(drop=True, inplace=True)
            return df
        except Exception as e:
            logger.error(f"Candle parse error for {symbol}: {e}")
            return None

    # ── Position Sizing ────────────────────────────────────────────────────────

    @staticmethod
    def calculate_lots(balance, risk_pct, sl_distance_price, symbol=""):
        """
        Calculate lot size for Match-Trader.
        Standard FX: 1 lot = 100,000 units.
        risk_pct: e.g. 0.01 for 1%
        sl_distance_price: distance from entry to SL in price
        Returns lot size (float, minimum 0.01).
        """
        if sl_distance_price <= 0:
            return 0.01
        risk_amount = balance * risk_pct
        lots = risk_amount / (sl_distance_price * 100000)
        lots = max(0.01, round(lots, 2))
        return lots

"""
Match-Trader API Client for FundingPips
========================================
Endpoints confirmed by live browser network inspection on 2026-06-25.

Authentication flow:
1. POST /mtr-core-edge/v2/login
   Body: {"login": "2009271", "password": "2866def46a", "partnerId": 1}
   → Returns auth token in response + sets session cookies

2. All trading endpoints use: /mtr-api/{systemUUID}/...
3. Candle data uses: /market-data-api/{systemUUID}/candles

Your System UUID: beedbea9-c757-46ad-b93b-a52ba2c3d648
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
MT_ACCOUNT_ID   = os.getenv("MT_ACCOUNT_ID", "2009271")

# System UUID discovered from live browser session — fixed for this account
SYSTEM_UUID = "beedbea9-c757-46ad-b93b-a52ba2c3d648"


class MatchTraderClient:
    """
    Authenticated client for the Match-Trader Platform API (FundingPips).
    Endpoints confirmed via live browser network inspection.
    """

    def __init__(self):
        self.platform_url  = MT_PLATFORM_URL
        self.email         = MT_EMAIL
        self.password      = MT_PASSWORD
        self.account_id    = MT_ACCOUNT_ID
        self.system_uuid   = SYSTEM_UUID

        self.session       = requests.Session()
        self.auth_token    = None
        self.token_expiry  = datetime.min

    # ── Authentication ─────────────────────────────────────────────────────────

    def login(self):
        """
        Login using account number + password (not email).
        Confirmed endpoint: POST /mtr-core-edge/v2/login
        """
        url = f"{self.platform_url}/mtr-core-edge/v2/login"
        payload = {
            "login":     self.account_id,   # Account number e.g. "2009271"
            "password":  self.password,
            "partnerId": 1
        }
        headers = {
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "Origin":       self.platform_url,
            "Referer":      f"{self.platform_url}/login",
            "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }
        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # Extract auth token from response
            self.auth_token = (
                data.get("token") or
                data.get("authToken") or
                data.get("accessToken") or
                data.get("tradingApiToken") or
                ""
            )

            # Also check for system UUID in response (in case it changes)
            accounts = data.get("tradingAccounts", [])
            for acct in accounts:
                if str(acct.get("tradingAccountId", "")) == str(self.account_id):
                    offer = acct.get("offer") or {}
                    system = offer.get("system") or {}
                    uuid = system.get("uuid", "")
                    if uuid:
                        self.system_uuid = uuid
                        logger.info(f"System UUID updated: {uuid}")
                    break

            self.token_expiry = datetime.now() + timedelta(minutes=12)
            logger.info(f"✅ Login OK | Account: {self.account_id} | UUID: {self.system_uuid}")
            return True

        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ Login HTTP error: {e.response.status_code} — {e.response.text[:300]}")
            return False
        except Exception as e:
            logger.error(f"❌ Login error: {e}")
            return False

    def ensure_auth(self):
        if self.auth_token is None or datetime.now() >= self.token_expiry:
            return self.login()
        return True

    def _headers(self):
        h = {
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "Origin":       self.platform_url,
            "Referer":      f"{self.platform_url}/app/trade",
            "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }
        if self.auth_token:
            h["Authorization"]     = f"Bearer {self.auth_token}"
            h["Auth-trading-api"]  = self.auth_token
        return h

    # ── Core Request ───────────────────────────────────────────────────────────

    def request(self, method, path, retries=5, delay=15, **kwargs):
        for attempt in range(1, retries + 1):
            try:
                self.ensure_auth()
                url  = f"{self.platform_url}{path}"
                resp = self.session.request(method, url, headers=self._headers(),
                                            timeout=30, **kwargs)

                if resp.status_code == 401:
                    logger.warning(f"401 on attempt {attempt} — re-logging in...")
                    self.auth_token = None
                    self.login()
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.Timeout:
                logger.warning(f"⚠️ Timeout attempt {attempt}/{retries}. Retry in {delay}s...")
            except requests.exceptions.HTTPError as e:
                logger.warning(f"⚠️ HTTP {e.response.status_code} attempt {attempt}/{retries}: {e.response.text[:200]}")
            except Exception as e:
                logger.warning(f"⚠️ Error attempt {attempt}/{retries}: {e}")

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
        Returns None on API failure.
        """
        data = self.request("GET", f"/mtr-api/{self.system_uuid}/open-positions")
        if data is None:
            return None
        positions = data.get("positions", []) if isinstance(data, dict) else data
        if symbol:
            mt_sym = symbol.replace("_", "").upper()
            positions = [p for p in positions if p.get("symbol", "").upper() == mt_sym]
        return positions

    def open_position(self, symbol, side, lots, sl_price, tp_price):
        """
        Opens a market position.
        Returns (order_id, error_message)
        """
        mt_sym = symbol.replace("_", "").upper()
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
        order_id = data.get("orderId", "") or data.get("positionId", "")

        if status == "OK" or order_id:
            return order_id or "filled", None
        else:
            reason = error or data.get("nativeCode", str(data))
            return None, reason

    def close_position(self, position_id, symbol, open_side, volume):
        """Closes a specific position by ID."""
        mt_sym     = symbol.replace("_", "").upper()
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
            reason = error or data.get("nativeCode", str(data))
            return False, reason

    def get_candles(self, symbol, count, granularity="H1"):
        """
        Fetches OHLC candle data as a pandas DataFrame.
        Uses confirmed endpoint: /market-data-api/{uuid}/candles
        granularity: 'M1','M5','M15','M30','H1','H4','D1'
        """
        mt_sym  = symbol.replace("_", "").upper()
        # Confirmed interval format from browser: H1, M1, M5, M15, M30, H4, D1
        path = (f"/market-data-api/{self.system_uuid}/candles"
                f"?symbol={mt_sym}&interval={granularity}&candleSide=BID&amount={count}")
        data = self.request("GET", path)

        if data is None:
            return None

        candles = data.get("candles", []) if isinstance(data, dict) else data
        if not candles:
            logger.warning(f"No candles returned for {symbol}")
            return None

        try:
            rows = []
            for c in candles:
                rows.append({
                    "open":  float(c.get("open",  c.get("o", 0))),
                    "high":  float(c.get("high",  c.get("h", 0))),
                    "low":   float(c.get("low",   c.get("l", 0))),
                    "close": float(c.get("close", c.get("c", 0))),
                })
            df = pd.DataFrame(rows)
            df.dropna(inplace=True)
            df.reset_index(drop=True, inplace=True)
            # Drop the last (incomplete) candle
            if len(df) > 1:
                df = df.iloc[:-1]
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

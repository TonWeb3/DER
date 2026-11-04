"""Deriv NEW-platform client — Multipliers & multi-symbol support for BOOM1000 & CRASH1000.

Deriv migrated off the legacy ws.derivws.com/websockets/v3 + authorize(token) flow.
The new platform is REST -> OTP -> WebSocket:

  1. REST GET  /trading/v1/options/accounts                -> list accounts (demo/real)
  2. REST GET  /trading/v1/options/legacy/migration-status -> must be "complete"
  3. REST POST /trading/v1/options/accounts/{id}/otp        -> a ready ws url (OTP embedded)
  4. connect wss://.../options/ws/{demo,real}?otp=...       -> legacy JSON protocol

Auth headers on every REST call: `Deriv-App-ID: <app_id>` + `Authorization: Bearer <pat>`.
The WS is bound to ONE account (chosen by MODE: demo->account_type "demo", live->"real"),
so demo mode can never fire a real-money trade. OTP is one-time: a fresh one is fetched
on every (re)connect.

Multipliers (MULTUP / MULTDOWN):
  - contract_type: 'MULTUP' (Multiplier Up) | 'MULTDOWN' (Multiplier Down)
  - multiplier: 100, 200, 300, 400, 500
  - underlying_symbol: 'BOOM1000' | 'CRASH1000'
  - limit_order: optional { take_profit: X, stop_loss: Y }
  - Contracts can be closed anytime via `sell(contract_id, price=0)`.
"""
import asyncio
import math
import json
import time
from typing import Any, Dict, List, Optional, Union

import aiohttp

REST_BASE = "https://api.derivws.com/trading/v1"

FALLBACK_MIN_STAKE = 1.0


class DerivClient:
    def __init__(self, app_id: str, token: str, symbols: Union[List[str], str], mode: str = "demo",
                 rest_base: str = REST_BASE):
        self.app_id = str(app_id or "")
        self.token = token or ""
        if isinstance(symbols, str):
            self.symbols = [symbols]
        else:
            self.symbols = list(symbols) if symbols else ["BOOM1000", "CRASH1000"]
        self.symbol = self.symbols[0]
        self.mode = (mode or "demo").lower()
        self.rest_base = rest_base

        self.ws = None
        self._session = None
        self.closed = False
        self.connected = False
        self.authorized = False

        # Multi-symbol tick tracking: symbol -> latest price, timestamp, pip_size
        self.last_prices: Dict[str, float] = {}
        self.last_tss: Dict[str, float] = {}
        self.pip_sizes: Dict[str, int] = {s: 4 if "CRASH" in s or "75" in s else 2 for s in self.symbols}

        # Backwards compatible single-symbol properties
        self.last_price: Optional[float] = None
        self.last_ts: Optional[float] = None
        self.pip_size: int = 2

        self.min_stake: Optional[float] = None
        self.max_payout: Optional[float] = None
        self.balance_trace: List[tuple] = []
        self.balance: Optional[float] = None
        self.currency: str = "USD"

        self.accounts: List[Dict[str, Any]] = []
        self.account_id: Optional[str] = None
        self.account_type: Optional[str] = None     # "demo" | "real"
        self.migration_status: Optional[str] = None
        self.last_error: Optional[str] = None

        self._req_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.on_tick = None

    # ── account type helpers ──────────────────────────────────────────────────────
    @property
    def is_virtual(self) -> Optional[bool]:
        if self.account_type is None:
            return None
        return self.account_type == "demo"

    @property
    def login_id(self) -> Optional[str]:
        return self.account_id

    def _want_type(self) -> str:
        return "demo" if self.mode == "demo" else "real"

    def _select_account(self) -> Optional[Dict[str, Any]]:
        want = self._want_type()
        for a in self.accounts:
            if (a.get("account_type") or "").lower() == want:
                return a
        return None

    # ── REST ──────────────────────────────────────────────────────────────────────
    def _headers(self) -> Dict[str, str]:
        return {"Deriv-App-ID": self.app_id, "Authorization": f"Bearer {self.token}",
                "Accept": "application/json", "Content-Type": "application/json"}

    async def _rest(self, method: str, path: str) -> Dict[str, Any]:
        url = self.rest_base + path
        async with aiohttp.ClientSession() as s:
            async with s.request(method, url, headers=self._headers(),
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
                try:
                    data = json.loads(text)
                except Exception:
                    data = {"_raw": text}
                if r.status >= 400:
                    msg = (data.get("message") if isinstance(data, dict) else None) or text[:120]
                    raise DerivError(r.status, msg)
                return data

    async def fetch_accounts(self) -> List[Dict[str, Any]]:
        data = await self._rest("GET", "/options/accounts")
        self.accounts = data.get("data") or []
        return self.accounts

    async def fetch_migration_status(self) -> Optional[str]:
        try:
            data = await self._rest("GET", "/options/legacy/migration-status")
            self.migration_status = data.get("status")
        except DerivError as e:
            self.migration_status = f"error_{e.code}"
        return self.migration_status

    async def _get_otp_ws_url(self, account_id: str) -> str:
        data = await self._rest("POST", f"/options/accounts/{account_id}/otp")
        url = (data.get("data") or {}).get("url")
        if not url:
            raise DerivError(0, "no ws url in OTP response")
        return url

    # ── lifecycle ───────────────────────────────────────────────────────────────
    async def start(self):
        self._loop = asyncio.get_running_loop()
        while not self.closed:
            try:
                if not (self.app_id and self.token):
                    self.last_error = "no_token"
                    await asyncio.sleep(3)
                    continue

                await self.fetch_accounts()
                acct = self._select_account()
                if acct is None:
                    self.last_error = f"no_{self._want_type()}_account"
                    await asyncio.sleep(5)
                    continue
                self.account_id = acct.get("account_id")
                self.account_type = (acct.get("account_type") or "").lower()
                if acct.get("balance") is not None:
                    try:
                        self._set_balance(float(acct["balance"]), "reconnect.rest")
                    except Exception:
                        pass
                self.currency = acct.get("currency", self.currency)
                await self.fetch_migration_status()
                ws_url = await self._get_otp_ws_url(self.account_id)

                self._session = aiohttp.ClientSession()
                async with self._session.ws_connect(ws_url, heartbeat=20) as ws:
                    self.ws = ws
                    self.connected = True
                    self.authorized = True
                    self.last_error = None
                    print(f"Deriv Multipliers connected: {self.account_id} ({self.account_type}) symbols={self.symbols}")

                    # Subscribe to ticks for all active symbols
                    for sym in self.symbols:
                        await self._send({"ticks": sym, "subscribe": 1}, wait=False)

                    # Subscribe to balance stream
                    await self._send({"balance": 1, "subscribe": 1}, wait=False)

                    # Refresh active symbol info and balance
                    asyncio.create_task(self.fetch_symbol_info())
                    asyncio.create_task(self.fetch_balance())

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._dispatch(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except DerivError as e:
                self.last_error = f"{e.code}: {e.message}"
                print(f"Deriv REST/WS error: {self.last_error}")
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                print(f"Deriv WS error: {e}")
            finally:
                self.connected = False
                self.authorized = False
                self.ws = None
                if self._session:
                    await self._session.close()
                    self._session = None
                for fut in list(self._pending.values()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("Deriv WS dropped"))
                self._pending.clear()
            if not self.closed:
                await asyncio.sleep(2)

    def close(self):
        self.closed = True

    async def update_symbols(self, new_symbols: List[str]):
        """Dynamically add/remove active symbols without dropping the WebSocket connection."""
        clean = [s for s in new_symbols if s]
        if not clean:
            return
        if self.ws and self.connected:
            # Unsubscribe symbols no longer wanted
            for s in self.symbols:
                if s not in clean:
                    await self._send({"forget": s}, wait=False)
            # Subscribe to newly added symbols
            for s in clean:
                if s not in self.symbols:
                    await self._send({"ticks": s, "subscribe": 1}, wait=False)
        self.symbols = clean
        self.symbol = clean[0]

    # ── request / response plumbing ───────────────────────────────────────────────
    async def _send(self, payload: Dict[str, Any], wait: bool = True,
                    timeout: float = 15.0) -> Dict[str, Any]:
        if self.ws is None:
            raise ConnectionError("Deriv WS not connected")
        self._req_id += 1
        rid = self._req_id
        payload = dict(payload, req_id=rid)
        if wait:
            fut = self._loop.create_future()
            self._pending[rid] = fut
        await self.ws.send_str(json.dumps(payload))
        if not wait:
            return {}
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    def _dispatch(self, data: Dict[str, Any]):
        rid = data.get("req_id")
        mtype = data.get("msg_type")

        if mtype == "tick" and "tick" in data:
            t = data["tick"]
            q = t.get("quote")
            sym = t.get("symbol") or t.get("underlying_symbol")
            if q is not None:
                val = float(q)
                ts = float(t.get("epoch") or time.time())
                if sym:
                    self.last_prices[sym] = val
                    self.last_tss[sym] = ts
                    if t.get("pip_size") is not None:
                        try:
                            self.pip_sizes[sym] = int(t["pip_size"])
                        except Exception:
                            pass
                if not sym or sym == self.symbol:
                    self.last_price = val
                    self.last_ts = ts
                    if t.get("pip_size") is not None:
                        try:
                            self.pip_size = int(t["pip_size"])
                        except Exception:
                            pass
                if self.on_tick and sym:
                    try:
                        self.on_tick(sym, val, ts)
                    except Exception:
                        pass

        elif mtype == "balance" and "balance" in data:
            b = data["balance"]
            if b.get("balance") is not None:
                self._set_balance(_f(b["balance"]), "push" if rid is None or rid not in self._pending else "reply")
                self.currency = b.get("currency", self.currency)

        if "error" in data:
            err = data["error"] or {}
            self.last_error = f"{err.get('code')}: {err.get('message')}"

        fut = self._pending.get(rid) if rid is not None else None
        if fut is not None and not fut.done():
            if "error" in data:
                err = data["error"] or {}
                fut.set_exception(DerivError(err.get("code"), err.get("message"),
                                             err.get("subcode"), err.get("code_args")))
            else:
                fut.set_result(data)

    # ── accessors ────────────────────────────────────────────────────────────────
    def get_last(self, symbol: Optional[str] = None) -> Dict[str, Optional[float]]:
        sym = symbol or self.symbol
        return {
            "price": self.last_prices.get(sym, self.last_price),
            "ts": self.last_tss.get(sym, self.last_ts)
        }

    def get_pip_size(self, symbol: Optional[str] = None) -> int:
        sym = symbol or self.symbol
        return self.pip_sizes.get(sym, self.pip_size)

    async def fetch_symbol_info(self) -> Dict[str, Any]:
        """Query active symbols to populate pip sizes for all tracked symbols."""
        try:
            resp = await self._send({"active_symbols": "brief"})
            for x in (resp.get("active_symbols") or []):
                sym = x.get("underlying_symbol")
                if sym in self.symbols:
                    pv = x.get("pip_size")
                    if isinstance(pv, (int, float)) and 0 < pv < 1:
                        self.pip_sizes[sym] = max(0, round(-math.log10(pv)))
                    elif pv is not None:
                        self.pip_sizes[sym] = int(pv)
            if self.symbol in self.pip_sizes:
                self.pip_size = self.pip_sizes[self.symbol]
            return {"ok": True, "pip_sizes": dict(self.pip_sizes)}
        except Exception as e:
            self.last_error = f"active_symbols: {e}"
            return {"ok": False, "error": str(e)}

    def _set_balance(self, value: Optional[float], source: str):
        if value is None:
            return
        prev = self.balance
        self.balance = value
        if prev is None or abs(prev - value) > 1e-9:
            self.balance_trace.append((time.time(), source, prev, value))
            del self.balance_trace[:-40]

    async def fetch_balance(self) -> Optional[float]:
        try:
            resp = await self._send({"balance": 1})
            b = resp.get("balance") or {}
            v = _f(b.get("balance"))
            if v is not None:
                self._set_balance(v, "fetch")
                self.currency = b.get("currency", self.currency)
            return v
        except Exception as e:
            self.last_error = f"balance: {e}"
            return None

    def effective_min_stake(self) -> float:
        return self.min_stake if self.min_stake is not None else FALLBACK_MIN_STAKE

    def _absorb_validation_params(self, p: Dict[str, Any]):
        vp = p.get("validation_params") or {}
        mn = (vp.get("stake") or {}).get("min")
        mx = (vp.get("payout") or {}).get("max")
        if mn is not None:
            v = _f(mn)
            if v:
                self.min_stake = v
        if mx is not None:
            v = _f(mx)
            if v:
                self.max_payout = v

    async def fetch_seed_candles(self, symbol: str, count: int = 500, granularity: int = 60) -> List[Dict[str, Any]]:
        """Fetch historical OHLC candles for a specific symbol."""
        try:
            resp = await self._send({"ticks_history": symbol, "style": "candles",
                                     "granularity": granularity, "count": count, "end": "latest"})
            out = []
            span_ms = granularity * 1000
            for c in resp.get("candles", []):
                out.append({"openTime": int(c["epoch"]) * 1000, "open": float(c["open"]),
                            "high": float(c["high"]), "low": float(c["low"]),
                            "close": float(c["close"]), "closeTime": int(c["epoch"]) * 1000 + span_ms - 1})
            return out
        except Exception as e:
            self.last_error = f"seed_{symbol}: {e}"
            return []

    # ── Multiplier Trading APIs ──────────────────────────────────────────────────
    async def get_multiplier_proposal(self, symbol: str, contract_type: str, amount: float,
                                      multiplier: int, take_profit: Optional[float] = None,
                                      stop_loss: Optional[float] = None,
                                      currency: Optional[str] = None) -> Dict[str, Any]:
        """Request a Multiplier price proposal (MULTUP or MULTDOWN)."""
        req = {
            "proposal": 1,
            "amount": round(float(amount), 2),
            "basis": "stake",
            "contract_type": contract_type,
            "currency": currency or self.currency or "USD",
            "multiplier": int(multiplier),
            "underlying_symbol": symbol,
        }
        limit_order = {}
        if take_profit is not None and float(take_profit) > 0:
            limit_order["take_profit"] = round(float(take_profit), 2)
        if stop_loss is not None and float(stop_loss) > 0:
            limit_order["stop_loss"] = round(float(stop_loss), 2)
        if limit_order:
            req["limit_order"] = limit_order

        try:
            resp = await self._send(req)
            p = resp.get("proposal", {})
            self._absorb_validation_params(p)
            return {
                "ok": True,
                "id": p.get("id"),
                "ask_price": _f(p.get("ask_price")),
                "spot": _f(p.get("spot")),
                "multiplier": p.get("multiplier", multiplier),
                "symbol": symbol,
                "contract_type": contract_type,
                "limit_order": limit_order
            }
        except DerivError as e:
            return {"ok": False, "error": f"{e.code}: {e.message}", "subcode": e.subcode}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def buy(self, proposal_id: str, price: float) -> Dict[str, Any]:
        try:
            resp = await self._send({"buy": proposal_id, "price": round(float(price), 2)})
            b = resp.get("buy", {})
            if b.get("balance_after") is not None:
                self._set_balance(_f(b["balance_after"]), "buy.balance_after")
            return {
                "ok": True,
                "contract_id": b.get("contract_id"),
                "buy_price": _f(b.get("buy_price")),
                "payout": _f(b.get("payout")),
                "response": b
            }
        except DerivError as e:
            return {"ok": False, "error": f"{e.code}: {e.message}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def contract_status(self, contract_id: Union[int, str]) -> Dict[str, Any]:
        """Poll open multiplier contract status, live profit/loss, stop-out barrier, etc."""
        try:
            resp = await self._send({"proposal_open_contract": 1, "contract_id": contract_id})
            c = resp.get("proposal_open_contract", {})
            return {
                "ok": True,
                "is_sold": bool(c.get("is_sold")),
                "status": c.get("status"),
                "profit": _f(c.get("profit")),
                "profit_percentage": _f(c.get("profit_percentage")),
                "sell_price": _f(c.get("sell_price")),
                "bid_price": _f(c.get("bid_price")),
                "buy_price": _f(c.get("buy_price")),
                "entry_spot": _f(c.get("entry_spot")),
                "current_spot": _f(c.get("current_spot")),
                "barrier": _f(c.get("barrier")),   # for multipliers, this is the stop-out barrier!
                "multiplier": c.get("multiplier"),
                "symbol": c.get("underlying_symbol"),
                "contract_type": c.get("contract_type"),
                "is_valid_to_sell": bool(c.get("is_valid_to_sell"))
            }
        except DerivError as e:
            return {"ok": False, "error": f"{e.code}: {e.message}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def sell(self, contract_id: Union[int, str], price: float = 0) -> Dict[str, Any]:
        """Close an active multiplier contract immediately at the current market value."""
        try:
            resp = await self._send({"sell": contract_id, "price": round(float(price), 2)})
            s = resp.get("sell", {})
            if s.get("balance_after") is not None:
                self.balance = _f(s["balance_after"])
            return {"ok": True, "sold_for": _f(s.get("sold_for")), "response": s}
        except DerivError as e:
            return {"ok": False, "error": f"{e.code}: {e.message}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class DerivError(Exception):
    def __init__(self, code, message, subcode=None, code_args=None):
        self.code = code
        self.message = message
        self.subcode = subcode
        self.code_args = code_args or []
        super().__init__(f"{code}: {message}")

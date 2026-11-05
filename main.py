"""DERIV Multipliers Bot — Spikebot 3.00 Market Structure Strategy (BOOM1000 & CRASH1000).

Faithfully ported from the MT5 EA:
- StructureCore.mqh (5-bar causal confirmed pivots, BOS / CHoCH, role-reversal retest zones)
- SpikebotSignals.mqh (Active Structure & Aligned Structure higher-timeframe confluence)
- Multiplier leverage: 100x, 200x, 300x, 400x, 500x.
- Real-time Interactive Candlestick Chart with symbol switcher tab on the dashboard.
"""
import asyncio
import json
import math
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bot.config import (settings, SUPPORTED_SYMBOLS, SUPPORTED_MULTIPLIERS,
                        SUPPORTED_TIMEFRAMES)
import bot.spikebot as spikebot
import bot.utils as utils
from bot.deriv_ws import DerivClient

SEED_CANDLE_COUNT = 300


class SymbolState:
    """Maintains multi-timeframe candles and Spikebot 3.00 Market Structure state for one symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self.base_candles: List[Dict[str, Any]] = []
        self.p1_candles: List[Dict[str, Any]] = []
        self.p2_candles: List[Dict[str, Any]] = []
        self.analyzer = spikebot.SpikebotAnalyzer(
            symbol=self.symbol,
            timeframe_minutes=settings.TIMEFRAME_MINUTES,
            signal_mode=settings.SIGNAL_MODE,
            structure_window=spikebot.ms_window(settings.TIMEFRAME_MINUTES)
        )
        self.seeded = False
        self.latest_analysis: Dict[str, Any] = {}
        self.last_candidate: Optional[Dict[str, Any]] = None
        self.last_logged_break: Optional[str] = None
        self.last_logged_zone: bool = False
        self.wins = 0
        self.losses = 0

    @property
    def prev_bar(self) -> Optional[Dict[str, Any]]:
        return self.base_candles[-2] if len(self.base_candles) >= 2 else None

    @property
    def current_forming_bar(self) -> Optional[Dict[str, Any]]:
        return self.base_candles[-1] if self.base_candles else None


symbols_state: Dict[str, SymbolState] = {}


def get_symbol_state(symbol: str) -> SymbolState:
    sym = symbol.upper()
    if sym not in symbols_state:
        symbols_state[sym] = SymbolState(sym)
    return symbols_state[sym]


class ConnectionManager:
    """Manages active WebSocket connections for real-time live streaming."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        if not self.active_connections:
            return
        dead = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for d in dead:
            if d in self.active_connections:
                self.active_connections.remove(d)


manager = ConnectionManager()
tick_event = asyncio.Event()


def _on_tick_received(sym: str, price: float, epoch: float):
    """Callback fired on every raw tick from Deriv WebSocket."""
    tick_event.set()


deriv = DerivClient(
    settings.DERIV_APP_ID,
    settings.DERIV_TOKEN,
    settings.active_symbols(),
    mode=settings.MODE
)
deriv.on_tick = _on_tick_received

state: Dict[str, Any] = {
    "latest_data": {
        "timestamp": datetime.now().isoformat(),
        "timing": {},
        "strategy": "Spikebot 3.00 Market Structure",
        "trading_state": {
            "mode": settings.MODE,
            "balance": None,
            "running": False,
            "currency": "USD",
            "authorized": False,
            "connected": False,
            "is_virtual": True,
            "can_trade": False,
            "guard": "initializing",
            "multiplier": settings.MULTIPLIER,
            "signal_mode": "active_structure",
            "timeframe_minutes": settings.TIMEFRAME_MINUTES,
            "symbols": settings.active_symbols(),
            "active_trades": [],
            "history_count": 0,
            "tp_sl_mode": "structural",
            "risk": {"type": settings.RISK_TYPE, "value": settings.RISK_VALUE}
        },
        "symbols": {
            sym: {
                "symbol": sym,
                "spot": 0.0,
                "pip_size": 0.001,
                "analysis": {"direction_label": "WAIT", "zone_active": False, "signal_confirmed": False},
                "candidate": None,
                "prev_bar": None,
                "forming_bar": None,
                "wins": 0,
                "losses": 0,
                "reason": "initializing",
                "candles_count": 0
            }
            for sym in settings.active_symbols()
        }
    },
    "last_update_ts": 0,
    "balance": None,
    "active_trades": [],
    "trade_history": [],
    "logs": [],
    "running": False,
    "entered_windows": [],   # formatted as f"{symbol}:{window_id}"
}


def log_message(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    state["logs"].append(line)
    if len(state["logs"]) > 200:
        state["logs"].pop(0)

    # Persist directly to activity.log so logs are permanently preserved
    try:
        utils.ensure_dir("logs")
        with open("logs/activity.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"activity.log write error: {e}")

    try:
        loop = asyncio.get_running_loop()
        if loop and loop.is_running():
            asyncio.create_task(manager.broadcast({"type": "log", "line": line}))
    except RuntimeError:
        pass


def save_state():
    try:
        with open("state_data.json", "w") as f:
            json.dump({
                "running": state["running"],
                "active_trades": state["active_trades"],
                "trade_history": state["trade_history"],
                "entered_windows": state["entered_windows"]
            }, f, indent=2)
    except Exception as e:
        print(f"save_state error: {e}")


def load_state():
    try:
        # Load previous activity logs from disk if available
        if os.path.exists("logs/activity.log"):
            try:
                with open("logs/activity.log", "r", encoding="utf-8") as f:
                    lines = [ln.strip() for ln in f if ln.strip()]
                    state["logs"] = lines[-100:]
            except Exception as e:
                print(f"error loading logs/activity.log: {e}")

        if os.path.exists("state_data.json"):
            with open("state_data.json") as f:
                d = json.load(f)
            state["running"] = bool(d.get("running", False))
            state["active_trades"] = d.get("active_trades", [])
            state["trade_history"] = d.get("trade_history", [])
            state["entered_windows"] = d.get("entered_windows", d.get("entered_markets", []))
            log_message("Persistent state and history loaded")
        else:
            log_message("No previous state file found; fresh session initialized")
    except Exception as e:
        print(f"load_state error: {e}")


def can_trade():
    if not (settings.DERIV_APP_ID and settings.DERIV_TOKEN):
        return False, "no_token"
    if not deriv.authorized:
        return False, "not_authorized"
    if deriv.account_type is None:
        return False, "account_unknown"
    want = "demo" if settings.MODE == "demo" else "real"
    if deriv.account_type != want:
        return False, f"no_{want}_account"
    if deriv.migration_status and deriv.migration_status != "complete":
        return False, "migration_pending"
    return True, "ok"


def window_timing() -> Dict[str, float]:
    wms = settings.TIMEFRAME_MINUTES * 60_000
    now_ms = time.time() * 1000
    start = (now_ms // wms) * wms
    end = start + wms
    return {
        "startMs": start,
        "endMs": end,
        "elapsedMinutes": (now_ms - start) / 60_000,
        "remainingMinutes": (end - now_ms) / 60_000,
    }


def update_forming_candle(candles: List[Dict[str, Any]], spot: float, tf_minutes: int) -> bool:
    """Fold latest spot into the forming candle for a given timeframe. Returns True if candle just closed."""
    if spot is None:
        return False
    now_ms = time.time() * 1000
    wms = tf_minutes * 60_000
    bucket = int(now_ms // wms) * wms

    if candles and candles[-1]["openTime"] == bucket:
        c = candles[-1]
        c["close"] = spot
        c["high"] = max(c["high"], spot)
        c["low"] = min(c["low"], spot)
        return False

    rolled = bool(candles)
    candles.append({
        "openTime": bucket,
        "open": spot,
        "high": spot,
        "low": spot,
        "close": spot,
        "closeTime": bucket + wms - 1
    })
    if len(candles) > 1500:
        candles.pop(0)
    return rolled


def generate_preview_candles(symbol: str, count: int = 150, granularity: int = 300) -> List[Dict[str, Any]]:
    """Generate initial preview candles for BOOM1000 / CRASH1000 aligned to clean granularity boundaries."""
    step_sec = granularity
    step_ms = step_sec * 1000
    now_sec = (int(time.time()) // step_sec) * step_sec
    now_ms = now_sec * 1000
    start_ms = now_ms - (count * step_ms)
    candles = []
    base_price = 12450.0 if "BOOM" in symbol else 6320.0
    price = base_price

    import random
    rng = random.Random(abs(hash(symbol)) % 10000)

    for i in range(count):
        bucket = start_ms + (i * step_ms)
        open_p = price
        if "BOOM" in symbol:
            is_spike = (i % 14 == 0) and i > 0
            if is_spike:
                high_p = open_p + rng.uniform(20.0, 50.0)
                close_p = high_p - rng.uniform(0.5, 3.0)
                low_p = open_p - rng.uniform(0.2, 1.0)
            else:
                close_p = open_p - rng.uniform(0.5, 2.0)
                high_p = open_p + rng.uniform(0.1, 0.5)
                low_p = close_p - rng.uniform(0.1, 0.5)
        else:
            is_crash = (i % 14 == 0) and i > 0
            if is_crash:
                low_p = open_p - rng.uniform(20.0, 50.0)
                close_p = low_p + rng.uniform(0.5, 3.0)
                high_p = open_p + rng.uniform(0.2, 1.0)
            else:
                close_p = open_p + rng.uniform(0.5, 2.0)
                high_p = close_p + rng.uniform(0.1, 0.5)
                low_p = open_p - rng.uniform(0.1, 0.5)

        price = close_p
        candles.append({
            "openTime": bucket,
            "open": round(open_p, 3),
            "high": round(high_p, 3),
            "low": round(low_p, 3),
            "close": round(close_p, 3),
            "closeTime": bucket + step_ms - 1
        })
    return candles


async def seed_candles_all():
    """Seed base and higher-timeframe ladder candles for all active symbols."""
    active_syms = settings.active_symbols()
    for sym in active_syms:
        sym_st = get_symbol_state(sym)
        p1_min, p2_min = spikebot.parent_timeframes_for(settings.TIMEFRAME_MINUTES)
        sym_st.analyzer.timeframe_minutes = settings.TIMEFRAME_MINUTES
        sym_st.analyzer.p1_minutes = p1_min
        sym_st.analyzer.p2_minutes = p2_min
        sym_st.analyzer.signal_mode = settings.SIGNAL_MODE
        sym_st.analyzer.structure_window = spikebot.ms_window(settings.TIMEFRAME_MINUTES)

        # 1. Base timeframe candles
        base = await deriv.fetch_seed_candles(sym, SEED_CANDLE_COUNT, settings.TIMEFRAME_MINUTES * 60)
        # 2. Parent 1 timeframe candles (300 candles ensure full structure warmup)
        p1 = await deriv.fetch_seed_candles(sym, SEED_CANDLE_COUNT, p1_min * 60)
        # 3. Parent 2 timeframe candles (300 candles ensure full structure warmup)
        p2 = await deriv.fetch_seed_candles(sym, SEED_CANDLE_COUNT, p2_min * 60)

        if base:
            sym_st.base_candles = base
            sym_st.p1_candles = p1 or []
            sym_st.p2_candles = p2 or []
            sym_st.analyzer.update_data(sym_st.base_candles, sym_st.p1_candles, sym_st.p2_candles)
            sym_st.latest_analysis = sym_st.analyzer.analyze_latest()
            sym_st.seeded = True
            log_message(f"Seeded {len(base)}x {settings.TIMEFRAME_MINUTES}m bars + parents ({p1_min}m, {p2_min}m) for {sym}")
        else:
            log_message(f"Seeding pending for {sym}...")


def _stake_amount() -> Optional[float]:
    bal = state["balance"]
    if bal is None or bal <= 0:
        return None
    if settings.RISK_TYPE == "percent":
        amt = (settings.RISK_VALUE / 100.0) * bal
    else:
        amt = float(settings.RISK_VALUE)
    return max(deriv.effective_min_stake(), round(amt, 2))


async def _open_multiplier_trade(symbol: str, contract_type: str, stake: float, multiplier: int,
                                 structural_stop: Optional[float] = None,
                                 structural_target: Optional[float] = None):
    """Execute a Multiplier buy order with dynamic Structural Stop Loss and Target TP."""
    spot_val = deriv.get_last(symbol).get("price") or 1.0

    dollar_tp = None
    dollar_sl = None

    # Calculate exact dollar TP and SL derived from Structural Target and Structural Stop
    if spot_val > 0:
        if contract_type == "MULTUP":
            if structural_target and structural_target > spot_val:
                dollar_tp = round(stake * multiplier * (structural_target - spot_val) / spot_val, 2)
            if structural_stop and structural_stop < spot_val:
                dollar_sl = round(stake * multiplier * (spot_val - structural_stop) / spot_val, 2)
        else:  # MULTDOWN
            if structural_target and structural_target < spot_val:
                dollar_tp = round(stake * multiplier * (spot_val - structural_target) / spot_val, 2)
            if structural_stop and structural_stop > spot_val:
                dollar_sl = round(stake * multiplier * (structural_stop - spot_val) / spot_val, 2)

    # Deriv limit_order proposal stop_loss cannot exceed stake (capped to 95% of stake if larger)
    deriv_sl = None
    if dollar_sl and dollar_sl > 0:
        deriv_sl = round(min(dollar_sl, stake * 0.95), 2)

    prop = await deriv.get_multiplier_proposal(
        symbol=symbol,
        contract_type=contract_type,
        amount=stake,
        multiplier=multiplier,
        take_profit=dollar_tp,
        stop_loss=deriv_sl
    )
    if not prop.get("ok"):
        log_message(f"Proposal failed for {symbol} {contract_type}: {prop.get('error')}")
        return "proposal_failed"

    res = await deriv.buy(prop["id"], prop["ask_price"])
    if not res.get("ok"):
        log_message(f"BUY failed for {symbol} {contract_type}: {res.get('error')}")
        return "buy_failed"

    contract_id = res.get("contract_id")
    live_spot = deriv.get_last(symbol).get("price") or prop.get("spot")

    st = await deriv.contract_status(contract_id)
    entry_spot = st.get("entry_spot") or live_spot
    barrier = st.get("barrier")  # Stop-out level for multipliers

    trade = {
        "contract_id": contract_id,
        "symbol": symbol,
        "contract_type": contract_type,
        "multiplier": multiplier,
        "stake": stake,
        "entry_spot": entry_spot,
        "current_spot": entry_spot,
        "barrier": barrier,
        "structural_stop": structural_stop,
        "structural_target": structural_target,
        "dollar_tp": dollar_tp,
        "dollar_sl": dollar_sl,
        "live_pl": 0.0,
        "live_return_pct": 0.0,
        "entry_time": datetime.now().isoformat(),
        "status": "OPEN",
        "profit_loss": None,
        "mode": settings.MODE
    }
    state["active_trades"].append(trade)
    save_state()

    log_message(
        f"BUY {symbol} {contract_type} {multiplier}x @ {entry_spot} | Stake: ${stake:.2f} | "
        f"Structural SL: {structural_stop} | Structural TP: {structural_target} (Contract ID: {contract_id})"
    )
    return "entered"


async def close_position_manually(contract_id: Union[int, str]) -> Dict[str, Any]:
    """Sell/close an active multiplier contract."""
    res = await deriv.sell(contract_id, price=0)
    if res.get("ok"):
        log_message(f"Sold contract {contract_id} for ${res.get('sold_for', 0):.2f}")
        await settle_trades()
        return {"ok": True, "sold_for": res.get("sold_for")}
    return {"ok": False, "error": res.get("error")}


async def settle_trades():
    """Poll open contracts, track real-time P/L, and settle sold positions."""
    remaining = []
    changed = False

    for t in state["active_trades"]:
        cid = t.get("contract_id")
        if not cid:
            continue
        st = await deriv.contract_status(cid)
        if not st.get("ok"):
            remaining.append(t)
            continue

        profit = st.get("profit")
        profit_pct = st.get("profit_percentage")
        current_spot = st.get("current_spot")
        if profit is not None:
            t["live_pl"] = profit
        if profit_pct is not None:
            t["live_return_pct"] = profit_pct
        if current_spot is not None:
            t["current_spot"] = current_spot
        if st.get("barrier") is not None:
            t["barrier"] = st.get("barrier")

        if st.get("is_sold"):
            t["status"] = "WON" if (profit or 0) > 0 else "LOST"
            t["profit_loss"] = profit
            t["sell_price"] = st.get("sell_price")
            t["exit_time"] = datetime.now().isoformat()
            state["trade_history"].append(t)
            changed = True
            tag = "WIN" if t["status"] == "WON" else "LOSS"
            sym_st = symbols_state.get(t["symbol"])
            if sym_st:
                if t["status"] == "WON":
                    sym_st.wins += 1
                else:
                    sym_st.losses += 1
            log_message(f"{tag}: {t['symbol']} {t['contract_type']} {t['multiplier']}x settled with P/L ${profit:.2f}")
        else:
            # Market Structure Automatic Exit Checks (Structural Stop Loss & Target TP)
            s_stop = t.get("structural_stop")
            s_target = t.get("structural_target")
            is_multup = t.get("contract_type") == "MULTUP"
            cur_price = current_spot or deriv.get_last(t.get("symbol")).get("price")

            should_close = False
            close_reason = ""

            # 1. Structural Stop Loss: Invalidation price breached
            if cur_price and s_stop:
                if is_multup and cur_price <= s_stop:
                    should_close = True
                    close_reason = f"Structural Stop Loss hit: spot {cur_price:.2f} <= SL {s_stop:.2f} (P/L: ${profit or 0:.2f})"
                elif not is_multup and cur_price >= s_stop:
                    should_close = True
                    close_reason = f"Structural Stop Loss hit: spot {cur_price:.2f} >= SL {s_stop:.2f} (P/L: ${profit or 0:.2f})"

            # 2. Structural Target: Target price achieved
            if not should_close and cur_price and s_target:
                if is_multup and cur_price >= s_target:
                    should_close = True
                    close_reason = f"Structural Target reached: spot {cur_price:.2f} >= TP {s_target:.2f} (P/L: +${profit or 0:.2f})"
                elif not is_multup and cur_price <= s_target:
                    should_close = True
                    close_reason = f"Structural Target reached: spot {cur_price:.2f} <= TP {s_target:.2f} (P/L: +${profit or 0:.2f})"

            if should_close:
                log_message(f"Auto-closing {t['symbol']} {t['contract_type']}: {close_reason}")
                await deriv.sell(cid, price=0)

            remaining.append(t)

    state["active_trades"] = remaining
    if changed:
        await deriv.fetch_balance()
        state["balance"] = deriv.balance
        save_state()


async def update_loop():
    """Event-driven trading engine loop reacting to real-time ticks & broadcasting via WebSocket."""
    csv_header = ["timestamp", "symbol", "spot", "contract_type", "multiplier", "stake", "direction", "zone_active", "signal_confirmed", "reason"]
    utils.ensure_dir("logs")

    while True:
        try:
            # Event-driven wait: trigger immediately on incoming tick, or timeout after 1.0s for time-based tasks
            try:
                await asyncio.wait_for(tick_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            finally:
                tick_event.clear()

            active_syms = settings.active_symbols()

            if deriv.connected:
                need_seed = any(not get_symbol_state(s).seeded for s in active_syms)
                if need_seed:
                    await seed_candles_all()

            await settle_trades()

            if deriv.connected and time.time() - state.get("balance_fetched_at", 0) > 25:
                await deriv.fetch_balance()
                state["balance_fetched_at"] = time.time()
            state["balance"] = deriv.balance

            timing = window_timing()
            window_id = int(timing["startMs"])
            stake = _stake_amount()

            symbols_payload = {}
            for sym in active_syms:
                sym_st = get_symbol_state(sym)
                spot = deriv.get_last(sym).get("price")

                # Update base & parent forming candles
                base_closed = update_forming_candle(sym_st.base_candles, spot, settings.TIMEFRAME_MINUTES)
                update_forming_candle(sym_st.p1_candles, spot, sym_st.analyzer.p1_minutes)
                update_forming_candle(sym_st.p2_candles, spot, sym_st.analyzer.p2_minutes)

                # Re-run Spikebot 3.00 Market Structure analysis
                sym_st.analyzer.update_data(sym_st.base_candles, sym_st.p1_candles, sym_st.p2_candles)
                analysis = sym_st.analyzer.analyze_latest()
                sym_st.latest_analysis = analysis

                contract_type = analysis.get("contract_type", "MULTUP" if "BOOM" in sym else "MULTDOWN")
                display_stake = stake if stake is not None else 1.0

                cand_data = {
                    "symbol": sym,
                    "contract_type": contract_type,
                    "multiplier": settings.MULTIPLIER,
                    "stake": display_stake,
                    "priced": False,
                    "spot": spot
                }

                if deriv.connected and deriv.authorized and spot:
                    try:
                        prop = await deriv.get_multiplier_proposal(
                            sym, contract_type, display_stake, settings.MULTIPLIER
                        )
                        if prop.get("ok"):
                            cand_data["ask_price"] = prop.get("ask_price")
                            cand_data["spot"] = prop.get("spot")
                            cand_data["priced"] = True
                    except Exception:
                        pass
                sym_st.last_candidate = cand_data

                # ── Spikebot Execution Gate ──
                reason = "waiting"
                open_trades_this_sym = [t for t in state["active_trades"] if t.get("symbol") == sym]
                window_key = f"{sym}:{window_id}"

                if not state["running"]:
                    reason = "bot_stopped"
                elif not spot:
                    reason = "waiting_for_spot"
                elif not analysis.get("ready"):
                    reason = analysis.get("reason", "warming_up")
                elif len(open_trades_this_sym) >= settings.MAX_OPEN_TRADES_PER_SYMBOL:
                    reason = f"max_open_trades_reached ({len(open_trades_this_sym)})"
                elif window_key in state["entered_windows"]:
                    reason = "candle_window_already_entered"
                elif not analysis.get("signal_confirmed"):
                    reason = analysis.get("reason", "awaiting_confirmed_retest")
                elif stake is None:
                    reason = "insufficient_balance"
                else:
                    trade_ok, guard_why = can_trade()
                    if not trade_ok:
                        reason = guard_why
                    else:
                        # Confirmed Spikebot Setup: Enter Multiplier Trade!
                        exec_res = await _open_multiplier_trade(
                            symbol=sym,
                            contract_type=contract_type,
                            stake=stake,
                            multiplier=settings.MULTIPLIER,
                            structural_stop=analysis.get("stop"),
                            structural_target=analysis.get("target")
                        )
                        if exec_res == "entered":
                            state["entered_windows"].append(window_key)
                            if len(state["entered_windows"]) > 200:
                                state["entered_windows"].pop(0)
                            reason = f"entered_{contract_type}_retest_setup"
                        else:
                            reason = exec_res

                # Log newly detected breaks or zone status
                curr_break = analysis.get("break_label", "NONE")
                curr_zone = bool(analysis.get("zone_active", False))
                if curr_break != "NONE" and curr_break != sym_st.last_logged_break:
                    sym_st.last_logged_break = curr_break
                    log_message(f"[{sym}] ⚡ Break Confirmed: {curr_break} @ {spot:.2f} | Retest zone: [{analysis.get('zone_low', 0):.2f} - {analysis.get('zone_high', 0):.2f}]")

                if curr_zone and not sym_st.last_logged_zone:
                    sym_st.last_logged_zone = True
                    log_message(f"[{sym}] Retest Zone active [{analysis.get('zone_low', 0):.2f} - {analysis.get('zone_high', 0):.2f}] | Invalidation: {analysis.get('invalidation', 0):.2f}")
                elif not curr_zone and sym_st.last_logged_zone:
                    sym_st.last_logged_zone = False

                if base_closed and spot:
                    log_message(f"[{sym} {settings.TIMEFRAME_MINUTES}m Closed] Close: {spot:.2f} | Trend: {analysis.get('direction_label')} | Break: {curr_break} | Zone: {'Active (Age ' + str(analysis.get('retest_age')) + ')' if curr_zone else 'None'} | Gate: {reason}")

                # Record signal telemetry
                utils.append_csv_row("./logs/signals.csv", csv_header, [
                    datetime.now().isoformat(), sym, spot, contract_type,
                    settings.MULTIPLIER, display_stake, analysis.get("direction_label"),
                    analysis.get("zone_active"), analysis.get("signal_confirmed"), reason
                ])

                # Build symbol summary for API / WS
                symbols_payload[sym] = {
                    "symbol": sym,
                    "spot": spot,
                    "pip_size": deriv.get_pip_size(sym),
                    "analysis": analysis,
                    "candidate": cand_data,
                    "prev_bar": sym_st.prev_bar,
                    "forming_bar": sym_st.current_forming_bar,
                    "wins": sym_st.wins,
                    "losses": sym_st.losses,
                    "reason": reason,
                    "candles_count": len(sym_st.base_candles)
                }

            # Periodic heartbeat logging every 2 minutes when bot is running
            if state["running"] and time.time() - state.get("last_heartbeat_at", 0) > 120:
                state["last_heartbeat_at"] = time.time()
                bal_str = f"${deriv.balance:.2f}" if deriv.balance is not None else "—"
                log_message(f"[Heartbeat] Bot Active ({settings.TIMEFRAME_MINUTES}m) | Symbols: {active_syms} | Bal: {bal_str} | Open Trades: {len(state['active_trades'])}")

            # Update live state snapshot
            state["latest_data"] = {
                "timestamp": datetime.now().isoformat(),
                "timing": timing,
                "strategy": "Spikebot 3.00 Market Structure",
                "trading_state": {
                    "mode": settings.MODE,
                    "balance": state["balance"],
                    "running": state["running"],
                    "currency": deriv.currency,
                    "authorized": deriv.authorized,
                    "connected": deriv.connected,
                    "is_virtual": deriv.is_virtual,
                    "can_trade": can_trade()[0],
                    "guard": can_trade()[1],
                    "multiplier": settings.MULTIPLIER,
                    "signal_mode": settings.SIGNAL_MODE,
                    "timeframe_minutes": settings.TIMEFRAME_MINUTES,
                    "symbols": active_syms,
                    "active_trades": state["active_trades"],
                    "history_count": len(state["trade_history"]),
                    "tp_sl_mode": "structural",
                    "risk": {"type": settings.RISK_TYPE, "value": settings.RISK_VALUE}
                },
                "symbols": symbols_payload
            }
            state["last_update_ts"] = time.time()

            # Real-time WebSocket Broadcast
            await manager.broadcast({
                "type": "update",
                "data": state["latest_data"],
                "trade_history": state["trade_history"]
            })

        except Exception as e:
            print(f"update_loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()

    # Pre-populate preview candles for all active symbols so both BOOM & CRASH charts are immediately ready
    for s in settings.active_symbols():
        sym_st = get_symbol_state(s)
        if not sym_st.base_candles:
            preview = generate_preview_candles(s, count=150, granularity=settings.TIMEFRAME_MINUTES * 60)
            sym_st.base_candles = preview
            sym_st.analyzer.update_data(preview)
            sym_st.latest_analysis = sym_st.analyzer.analyze_latest()

    deriv_task = asyncio.create_task(deriv.start())
    for _ in range(50):
        if deriv.connected:
            break
        await asyncio.sleep(0.2)

    await seed_candles_all()
    tasks = [deriv_task, asyncio.create_task(update_loop())]
    yield
    deriv.close()
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await asyncio.wait_for(t, timeout=5)
        except BaseException:
            pass


app = FastAPI(title="Deriv Spikebot 3.00 Multiplier Bot", lifespan=lifespan)
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Web Routes ───────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


# ── WebSocket Real-Time Stream ──────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Deliver immediate snapshot upon connection
        await websocket.send_json({
            "type": "snapshot",
            "data": state["latest_data"],
            "trade_history": state["trade_history"],
            "logs": state["logs"]
        })
        while True:
            text = await websocket.receive_text()
            if text == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


# ── API Endpoints ────────────────────────────────────────────────────────────
@app.get("/api/latest")
async def latest():
    return state["latest_data"]


@app.get("/api/logs")
async def logs():
    return state["logs"]


@app.get("/api/logs/download")
async def download_logs():
    """Download full activity log file from startup."""
    log_path = os.path.abspath("logs/activity.log")
    if not os.path.exists(log_path):
        utils.ensure_dir("logs")
        with open(log_path, "w", encoding="utf-8") as f:
            for l in state["logs"]:
                f.write(l + "\n")
    return FileResponse(
        path=log_path,
        filename=f"spikebot_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        media_type="text/plain"
    )


@app.get("/history")
async def history():
    return state["trade_history"]


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "last_update": state["last_update_ts"],
        "mode": settings.MODE,
        "running": state["running"],
        "connected": deriv.connected,
        "authorized": deriv.authorized
    }


@app.get("/api/candles/{symbol}")
async def get_candles(symbol: str):
    """Serve OHLC candles for the interactive chart."""
    sym = symbol.upper()
    sym_st = get_symbol_state(sym)
    if sym_st.base_candles:
        return sym_st.base_candles

    if deriv.connected:
        candles = await deriv.fetch_seed_candles(sym, count=300, granularity=settings.TIMEFRAME_MINUTES * 60)
        if candles:
            sym_st.base_candles = candles
            sym_st.analyzer.update_data(candles)
            sym_st.latest_analysis = sym_st.analyzer.analyze_latest()
            return candles

    preview = generate_preview_candles(sym, count=150, granularity=settings.TIMEFRAME_MINUTES * 60)
    sym_st.base_candles = preview
    sym_st.analyzer.update_data(preview)
    sym_st.latest_analysis = sym_st.analyzer.analyze_latest()
    return preview


@app.get("/api/indicator/{symbol}")
async def get_indicator(symbol: str):
    """Serve market structure indicator data (markers, price levels, confluence) for the interactive chart."""
    sym = symbol.upper()
    sym_st = get_symbol_state(sym)

    if not sym_st.base_candles:
        if deriv.connected:
            candles = await deriv.fetch_seed_candles(sym, count=300, granularity=settings.TIMEFRAME_MINUTES * 60)
            if candles:
                sym_st.base_candles = candles
        if not sym_st.base_candles:
            sym_st.base_candles = generate_preview_candles(sym, count=150, granularity=settings.TIMEFRAME_MINUTES * 60)
        sym_st.analyzer.update_data(sym_st.base_candles)
        sym_st.latest_analysis = sym_st.analyzer.analyze_latest()

    analysis = dict(sym_st.latest_analysis or {})
    if not analysis and sym_st.base_candles:
        sym_st.analyzer.update_data(sym_st.base_candles)
        analysis = sym_st.analyzer.analyze_latest()
        sym_st.latest_analysis = analysis

    markers = list(analysis.get("markers", []))
    if not markers and sym_st.base_candles:
        bars = spikebot.convert_raw_candles(sym_st.base_candles, settings.TIMEFRAME_MINUTES)
        spike_dir = 1 if "BOOM" in sym else -1
        markers = spikebot.compute_structure_markers(bars, spike_dir)

    levels = {
        "support": float(analysis.get("support", 0.0) or 0.0),
        "resistance": float(analysis.get("resistance", 0.0) or 0.0),
        "zone_active": bool(analysis.get("zone_active", False)),
        "zone_mid": float(analysis.get("zone_mid", 0.0) or 0.0),
        "zone_low": float(analysis.get("zone_low", 0.0) or 0.0),
        "zone_high": float(analysis.get("zone_high", 0.0) or 0.0),
        "invalidation": float(analysis.get("invalidation", 0.0) or 0.0),
        "stop": float(analysis.get("stop", 0.0) or 0.0),
        "target": float(analysis.get("target", 0.0) or 0.0),
        "direction": str(analysis.get("direction_label", "NEUTRAL")),
        "break_label": str(analysis.get("break_label", "NONE")),
        "signal_confirmed": bool(analysis.get("signal_confirmed", False))
    }

    return {
        "symbol": sym,
        "levels": levels,
        "markers": markers
    }


@app.post("/api/close-trade/{contract_id}")
async def close_trade(contract_id: str):
    """Manual 1-click close of an active multiplier position."""
    res = await close_position_manually(contract_id)
    return res


@app.post("/api/start")
async def start_trading():
    state["running"] = True
    save_state()
    log_message(f"Trading STARTED by user (Spikebot 3.00 Engine Active | TF: {settings.TIMEFRAME_MINUTES}m | Multiplier: {settings.MULTIPLIER}x | Symbols: {settings.active_symbols()})")
    return {"ok": True, "running": True}


@app.post("/api/stop")
async def stop_trading():
    state["running"] = False
    save_state()
    log_message("Trading STOPPED by user")
    return {"ok": True, "running": False}


@app.get("/api/settings")
async def get_settings():
    tok = settings.DERIV_TOKEN
    masked = (tok[:6] + "..." + tok[-4:]) if tok and len(tok) > 12 else tok
    return {
        "mode": settings.MODE,
        "deriv": {"app_id": settings.DERIV_APP_ID, "token": masked},
        "trading": {
            "symbols": settings.active_symbols(),
            "currency": settings.CURRENCY,
            "multiplier": settings.MULTIPLIER,
            "timeframe_minutes": settings.TIMEFRAME_MINUTES,
            "risk_type": settings.RISK_TYPE,
            "risk_value": settings.RISK_VALUE,
            "max_open_trades_per_symbol": settings.MAX_OPEN_TRADES_PER_SYMBOL
        },
        "supported_symbols": SUPPORTED_SYMBOLS,
        "supported_multipliers": SUPPORTED_MULTIPLIERS,
        "supported_timeframes": SUPPORTED_TIMEFRAMES,
        "strategy": "active_structure"
    }


@app.post("/api/settings")
async def post_settings(new: Dict[str, Any]):
    old_symbols = list(settings.SYMBOLS)
    old_token = settings.DERIV_TOKEN
    old_app = settings.DERIV_APP_ID
    old_mode = settings.MODE
    old_tf = settings.TIMEFRAME_MINUTES

    if isinstance(new.get("deriv"), dict):
        t = new["deriv"].get("token")
        if t is not None and "..." in t:
            new["deriv"].pop("token", None)

    cfg = {}
    if os.path.exists("config.json"):
        try:
            with open("config.json") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

    def merge_dict(b, o):
        for k, v in o.items():
            if isinstance(v, dict) and isinstance(b.get(k), dict):
                merge_dict(b[k], v)
            else:
                b[k] = v
        return b

    merged_cfg = merge_dict(cfg, new)
    # Strip core strategy constants from saved settings file
    if "trading" in merged_cfg and isinstance(merged_cfg["trading"], dict):
        for core_key in ["signal_mode", "tp_sl_mode", "take_profit", "stop_loss", "structure_window"]:
            merged_cfg["trading"].pop(core_key, None)

    with open("config.json", "w") as f:
        json.dump(merged_cfg, f, indent=2)

    if "mode" in new:
        settings.MODE = new["mode"]

    if "deriv" in new:
        d = new["deriv"]
        settings.DERIV_APP_ID = str(d.get("app_id", settings.DERIV_APP_ID))
        if d.get("token") is not None:
            settings.DERIV_TOKEN = d["token"]

    if "trading" in new:
        t = new["trading"]
        if "symbols" in t and isinstance(t["symbols"], list):
            clean = [s for s in t["symbols"] if s in SUPPORTED_SYMBOLS]
            if clean:
                settings.SYMBOLS = clean
        if "multiplier" in t:
            m = int(t["multiplier"])
            if m in SUPPORTED_MULTIPLIERS:
                settings.MULTIPLIER = m
        settings.SIGNAL_MODE = "active_structure"
        if "timeframe_minutes" in t:
            tf = int(t["timeframe_minutes"])
            if tf in SUPPORTED_TIMEFRAMES:
                settings.TIMEFRAME_MINUTES = tf
                for s in settings.active_symbols():
                    st_obj = get_symbol_state(s)
                    st_obj.analyzer.timeframe_minutes = tf
                    p1_m, p2_m = spikebot.parent_timeframes_for(tf)
                    st_obj.analyzer.p1_minutes = p1_m
                    st_obj.analyzer.p2_minutes = p2_m
                    st_obj.analyzer.structure_window = spikebot.ms_window(tf)
        if "risk_type" in t:
            settings.RISK_TYPE = t["risk_type"]
        if "risk_value" in t:
            settings.RISK_VALUE = float(t["risk_value"])

    if settings.DERIV_TOKEN != old_token or settings.DERIV_APP_ID != old_app or settings.MODE != old_mode:
        _reconnect_deriv()
        log_message(f"Deriv reconnected (mode={settings.MODE}, symbols={settings.SYMBOLS})")
    elif settings.SYMBOLS != old_symbols:
        await deriv.update_symbols(settings.active_symbols())
        await seed_candles_all()
    elif settings.TIMEFRAME_MINUTES != old_tf:
        p1_m, p2_m = spikebot.parent_timeframes_for(settings.TIMEFRAME_MINUTES)
        log_message(f"Timeframe switched to {settings.TIMEFRAME_MINUTES}m (Parents: {p1_m}m, {p2_m}m) — reseeding candles")
        await seed_candles_all()

    return {"status": "ok"}


def _reconnect_deriv():
    global deriv
    deriv.close()
    deriv = DerivClient(
        settings.DERIV_APP_ID,
        settings.DERIV_TOKEN,
        settings.active_symbols(),
        mode=settings.MODE
    )
    deriv.on_tick = _on_tick_received
    asyncio.create_task(deriv.start())


@app.get("/api/auth-status")
async def auth_status():
    ok, why = can_trade()
    accts = [
        {
            "account_id": a.get("account_id"),
            "account_type": a.get("account_type"),
            "currency": a.get("currency"),
            "balance": a.get("balance")
        }
        for a in deriv.accounts
    ]
    return {
        "connected": deriv.connected,
        "authorized": deriv.authorized,
        "account_id": deriv.account_id,
        "account_type": deriv.account_type,
        "is_virtual": deriv.is_virtual,
        "balance": deriv.balance,
        "currency": deriv.currency,
        "mode": settings.MODE,
        "migration": deriv.migration_status,
        "can_trade": ok,
        "guard": why,
        "accounts": accts,
        "has_token": bool(settings.DERIV_APP_ID and settings.DERIV_TOKEN),
        "error": deriv.last_error
    }


@app.post("/api/test-connection")
async def test_connection():
    ok, why = can_trade()
    return {
        "ok": deriv.authorized,
        "connected": deriv.connected,
        "authorized": deriv.authorized,
        "account_id": deriv.account_id,
        "account_type": deriv.account_type,
        "is_virtual": deriv.is_virtual,
        "balance": deriv.balance,
        "currency": deriv.currency,
        "migration": deriv.migration_status,
        "can_trade": ok,
        "guard": why,
        "error": deriv.last_error
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8070)

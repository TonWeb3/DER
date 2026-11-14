"""DERIV Multipliers Bot — Spikebot 3.00 Market Structure Multi-Timeframe Strategy.
Scans ALL supported timeframes concurrently:
- 1m  -> 500x leverage (Parents: 5m, 15m)
- 2m  -> 400x leverage (Parents: 10m, 30m)
- 5m  -> 300x leverage (Parents: 15m, 60m)
- 15m -> 200x leverage (Parents: 60m, 240m)

Features:
- Independent trade lifecycle per position (each tracks its own timeframe, leverage, SL, and TP).
- Concurrent positions on the same symbol up to `max_open_trades_per_symbol`.
- Direction-locked execution: BOOM strictly trades MULTUP (BUY); CRASH strictly trades MULTDOWN (SELL).
- Interactive Chart supporting real-time switching across symbols and all timeframes.
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

from bot.config import (settings, SUPPORTED_SYMBOLS, SUPPORTED_TIMEFRAMES,
                        TIMEFRAME_MULTIPLIER_MAP, ALL_REQUIRED_GRANULARITIES)
import bot.spikebot as spikebot
import bot.utils as utils
from bot.deriv_ws import DerivClient

SEED_CANDLE_COUNT = 300
ALL_SCAN_TIMEFRAMES = [1, 2, 5, 15]


class SymbolState:
    """Maintains multi-timeframe candles and Spikebot 3.00 analyzers across all timeframes for one symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        # Granularity in minutes -> candle list
        self.candles: Dict[int, List[Dict[str, Any]]] = {g: [] for g in ALL_REQUIRED_GRANULARITIES}
        # Independent analyzers for each scanned timeframe
        self.analyzers: Dict[int, spikebot.SpikebotAnalyzer] = {
            tf: spikebot.SpikebotAnalyzer(
                symbol=self.symbol,
                timeframe_minutes=tf,
                signal_mode=settings.SIGNAL_MODE,
                structure_window=spikebot.ms_window(tf)
            )
            for tf in ALL_SCAN_TIMEFRAMES
        }
        self.seeded = False
        self.latest_analyses: Dict[int, Dict[str, Any]] = {}
        self.last_logged_break: Dict[int, Optional[str]] = {tf: None for tf in ALL_SCAN_TIMEFRAMES}
        self.last_logged_zone: Dict[int, bool] = {tf: False for tf in ALL_SCAN_TIMEFRAMES}
        self.wins = 0
        self.losses = 0

    def get_candles(self, tf: int) -> List[Dict[str, Any]]:
        return self.candles.get(tf, [])

    def prev_bar(self, tf: int) -> Optional[Dict[str, Any]]:
        c = self.candles.get(tf, [])
        return c[-2] if len(c) >= 2 else None

    def current_forming_bar(self, tf: int) -> Optional[Dict[str, Any]]:
        c = self.candles.get(tf, [])
        return c[-1] if c else None


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
        "strategy": "Spikebot 3.00 Multi-Timeframe Engine",
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
            "timeframe_multiplier_map": TIMEFRAME_MULTIPLIER_MAP,
            "signal_mode": "active_structure",
            "symbols": settings.active_symbols(),
            "active_trades": [],
            "history_count": 0,
            "max_open_trades_per_symbol": settings.MAX_OPEN_TRADES_PER_SYMBOL,
            "risk": {"type": settings.RISK_TYPE, "value": settings.RISK_VALUE}
        },
        "symbols": {}
    },
    "last_update_ts": 0,
    "balance": None,
    "active_trades": [],
    "trade_history": [],
    "logs": [],
    "running": False,
    "entered_windows": [],   # formatted as f"{symbol}:{tf}m:{window_id}"
}


def log_message(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    state["logs"].append(line)
    if len(state["logs"]) > 250:
        state["logs"].pop(0)

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
            state["entered_windows"] = d.get("entered_windows", [])
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


def window_timing(tf_minutes: int = 5) -> Dict[str, float]:
    wms = tf_minutes * 60_000
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
    """Fold latest spot into the forming candle for a given timeframe. Returns True if candle just rolled/closed."""
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
    rng = random.Random(abs(hash(symbol)) % 10000 + granularity)

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
    """Seed all granularities across all base and parent timeframes for all active symbols."""
    active_syms = settings.active_symbols()
    for sym in active_syms:
        sym_st = get_symbol_state(sym)
        loaded = 0
        for g in ALL_REQUIRED_GRANULARITIES:
            raw = await deriv.fetch_seed_candles(sym, SEED_CANDLE_COUNT, g * 60)
            if raw:
                sym_st.candles[g] = raw
                loaded += 1
            elif not sym_st.candles[g]:
                sym_st.candles[g] = generate_preview_candles(sym, count=150, granularity=g * 60)

        # Update all analyzers for base timeframes (1m, 2m, 5m, 15m)
        for tf in ALL_SCAN_TIMEFRAMES:
            p1_min, p2_min = spikebot.parent_timeframes_for(tf)
            sym_st.analyzers[tf].update_data(
                local_raw=sym_st.candles.get(tf, []),
                p1_raw=sym_st.candles.get(p1_min, []),
                p2_raw=sym_st.candles.get(p2_min, [])
            )
            sym_st.latest_analyses[tf] = sym_st.analyzers[tf].analyze_latest()

        sym_st.seeded = True
        log_message(f"Seeded candles for {sym} ({loaded}/{len(ALL_REQUIRED_GRANULARITIES)} live). Active scanning: {ALL_SCAN_TIMEFRAMES}")


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
                                 structural_target: Optional[float] = None,
                                 timeframe: int = 5):
    """Execute a Multiplier buy order with dynamic Structural Stop Loss and Target TP for a specific timeframe."""
    sym_st = get_symbol_state(symbol)
    spot_val = deriv.get_last(symbol).get("price") or 1.0

    # Retrieve current scale (bar range) from latest analysis
    tf_analysis = sym_st.latest_analyses.get(timeframe, {})
    scale = float(tf_analysis.get("scale", 0.0) or (spot_val * 0.001))
    if scale <= 0:
        scale = spot_val * 0.001

    # Guarantee strict structural validity relative to current spot price
    if contract_type == "MULTUP":
        # For BUY: SL must be strictly below spot, TP must be strictly above spot
        if structural_stop is None or structural_stop >= spot_val or structural_stop <= 0:
            structural_stop = round(spot_val - max(scale, 1.0), 3)
        risk_dist = max(spot_val - structural_stop, scale * 0.5)
        if structural_target is None or structural_target <= spot_val:
            structural_target = round(spot_val + (2.0 * risk_dist), 3)

        dollar_tp = round(stake * multiplier * (structural_target - spot_val) / spot_val, 2)
        dollar_sl = round(stake * multiplier * (spot_val - structural_stop) / spot_val, 2)
    else:  # MULTDOWN
        # For SELL: SL must be strictly above spot, TP must be strictly below spot
        if structural_stop is None or structural_stop <= spot_val or structural_stop <= 0:
            structural_stop = round(spot_val + max(scale, 1.0), 3)
        risk_dist = max(structural_stop - spot_val, scale * 0.5)
        if structural_target is None or structural_target >= spot_val:
            structural_target = round(spot_val - (2.0 * risk_dist), 3)

        dollar_tp = round(stake * multiplier * (spot_val - structural_target) / spot_val, 2)
        dollar_sl = round(stake * multiplier * (structural_stop - spot_val) / spot_val, 2)

    # Deriv limit_order proposal:
    # Only pass stop_loss to Deriv if dollar_sl < stake * 0.95.
    # If dollar_sl >= stake * 0.95, do NOT pass stop_loss to Deriv because:
    # 1. Deriv multipliers already enforce a hard 100% loss cap at stake amount (cannot lose more than stake).
    # 2. Capping SL to 95% of stake in the Deriv broker order forces an artificial premature stop-out
    #    before price reaches the actual structural invalidation level!
    # Our settle_trades() loop tracks and enforces the exact structural SL in real-time.
    deriv_sl = None
    if dollar_sl and 0 < dollar_sl < (stake * 0.95):
        deriv_sl = round(dollar_sl, 2)

    deriv_tp = dollar_tp if (dollar_tp and dollar_tp > 0) else None

    prop = await deriv.get_multiplier_proposal(
        symbol=symbol,
        contract_type=contract_type,
        amount=stake,
        multiplier=multiplier,
        take_profit=deriv_tp,
        stop_loss=deriv_sl
    )
    if not prop.get("ok"):
        log_message(f"[{symbol} {timeframe}m] Proposal failed for {contract_type} {multiplier}x: {prop.get('error')}")
        return "proposal_failed"

    res = await deriv.buy(prop["id"], prop["ask_price"])
    if not res.get("ok"):
        log_message(f"[{symbol} {timeframe}m] BUY failed for {contract_type} {multiplier}x: {res.get('error')}")
        return "buy_failed"

    contract_id = res.get("contract_id")
    live_spot = deriv.get_last(symbol).get("price") or prop.get("spot")

    st = await deriv.contract_status(contract_id)
    entry_spot = float(st.get("entry_spot") or live_spot or spot_val)
    barrier = st.get("barrier")

    # Final post-fill reconciliation: ensure structural SL & TP remain aligned with actual fill entry_spot
    if entry_spot > 0:
        if contract_type == "MULTUP":
            if structural_stop >= entry_spot:
                structural_stop = round(entry_spot - risk_dist, 3)
            if structural_target <= entry_spot:
                structural_target = round(entry_spot + (2.0 * risk_dist), 3)
        else:  # MULTDOWN
            if structural_stop <= entry_spot:
                structural_stop = round(entry_spot + risk_dist, 3)
            if structural_target >= entry_spot:
                structural_target = round(entry_spot - (2.0 * risk_dist), 3)

    trade = {
        "contract_id": contract_id,
        "symbol": symbol,
        "contract_type": contract_type,
        "timeframe": timeframe,
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
        f"⚡ [{symbol} {timeframe}m] ENTERED {contract_type} {multiplier}x @ {entry_spot:.2f} | "
        f"Stake: ${stake:.2f} | SL: {structural_stop:.2f} | TP: {structural_target:.2f} (ID: {contract_id})"
    )
    return "entered"


async def close_position_manually(contract_id: Union[int, str]) -> Dict[str, Any]:
    """Sell/close an active multiplier contract immediately."""
    res = await deriv.sell(contract_id, price=0)
    if res.get("ok"):
        log_message(f"Sold contract {contract_id} for ${res.get('sold_for', 0):.2f}")
        await settle_trades()
        return {"ok": True, "sold_for": res.get("sold_for")}
    return {"ok": False, "error": res.get("error")}


async def settle_trades():
    """Poll open contracts, track real-time P/L independently for each trade, and auto-settle upon SL/TP."""
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

        tf_lbl = f"{t.get('timeframe', '?')}m"

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
            log_message(f"{tag}: [{t['symbol']} {tf_lbl}] {t['contract_type']} {t['multiplier']}x settled with P/L ${profit:.2f}")
        else:
            # Independent Market Structure Automatic Exit Checks (SL & TP)
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
                    close_reason = f"Structural SL hit [{tf_lbl}]: spot {cur_price:.2f} <= SL {s_stop:.2f} (P/L: ${profit or 0:.2f})"
                elif not is_multup and cur_price >= s_stop:
                    should_close = True
                    close_reason = f"Structural SL hit [{tf_lbl}]: spot {cur_price:.2f} >= SL {s_stop:.2f} (P/L: ${profit or 0:.2f})"

            # 2. Structural Target: Target price achieved
            if not should_close and cur_price and s_target:
                if is_multup and cur_price >= s_target:
                    should_close = True
                    close_reason = f"Structural Target reached [{tf_lbl}]: spot {cur_price:.2f} >= TP {s_target:.2f} (P/L: +${profit or 0:.2f})"
                elif not is_multup and cur_price <= s_target:
                    should_close = True
                    close_reason = f"Structural Target reached [{tf_lbl}]: spot {cur_price:.2f} <= TP {s_target:.2f} (P/L: +${profit or 0:.2f})"

            if should_close:
                log_message(f"Auto-closing [{t['symbol']} {tf_lbl}] {t['contract_type']}: {close_reason}")
                sell_res = await deriv.sell(cid, price=0)
                if sell_res.get("ok"):
                    post_st = await deriv.contract_status(cid)
                    if post_st.get("ok") and post_st.get("is_sold"):
                        p_val = post_st.get("profit") if post_st.get("profit") is not None else profit
                        t["status"] = "WON" if (p_val or 0) > 0 else "LOST"
                        t["profit_loss"] = p_val
                        t["sell_price"] = post_st.get("sell_price") or cur_price
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
                        log_message(f"{tag}: [{t['symbol']} {tf_lbl}] {t['contract_type']} {t['multiplier']}x settled with P/L ${p_val or 0:.2f}")
                        continue

            remaining.append(t)

    state["active_trades"] = remaining
    if changed:
        await deriv.fetch_balance()
        state["balance"] = deriv.balance
        save_state()


async def update_loop():
    """Event-driven trading engine loop reacting to ticks, scanning all timeframes, and broadcasting live."""
    csv_header = ["timestamp", "symbol", "timeframe", "spot", "contract_type", "multiplier", "stake", "direction", "zone_active", "signal_confirmed", "reason"]
    utils.ensure_dir("logs")

    while True:
        try:
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

            stake = _stake_amount()
            symbols_payload = {}

            for sym in active_syms:
                sym_st = get_symbol_state(sym)
                spot = deriv.get_last(sym).get("price")

                # 1. Update forming candles across all granularities
                closed_info = {}
                for g in ALL_REQUIRED_GRANULARITIES:
                    rolled = update_forming_candle(sym_st.candles[g], spot, g)
                    closed_info[g] = rolled

                # 2. Multi-Timeframe Concurrent Scans across 1m, 2m, 5m, 15m
                tf_payloads = {}
                open_trades_this_sym = [t for t in state["active_trades"] if t.get("symbol") == sym]

                for tf in ALL_SCAN_TIMEFRAMES:
                    p1_min, p2_min = spikebot.parent_timeframes_for(tf)
                    sym_st.analyzers[tf].update_data(
                        local_raw=sym_st.candles.get(tf, []),
                        p1_raw=sym_st.candles.get(p1_min, []),
                        p2_raw=sym_st.candles.get(p2_min, [])
                    )
                    analysis = sym_st.analyzers[tf].analyze_latest()
                    sym_st.latest_analyses[tf] = analysis
                    tf_leverage = TIMEFRAME_MULTIPLIER_MAP[tf]

                    # Enforce strict direction lock: BOOM strictly MULTUP; CRASH strictly MULTDOWN
                    correct_contract = "MULTUP" if "BOOM" in sym else "MULTDOWN"
                    expected_spike_dir = 1 if "BOOM" in sym else -1

                    timing = window_timing(tf)
                    window_id = int(timing["startMs"])
                    window_key = f"{sym}:{tf}m:{window_id}"

                    # Execution Gate for this specific timeframe
                    reason = "waiting"

                    if not state["running"]:
                        reason = "bot_stopped"
                    elif not spot:
                        reason = "waiting_for_spot"
                    elif not analysis.get("ready"):
                        reason = analysis.get("reason", "warming_up")
                    elif len(open_trades_this_sym) >= settings.MAX_OPEN_TRADES_PER_SYMBOL:
                        reason = f"max_open_trades_reached ({len(open_trades_this_sym)}/{settings.MAX_OPEN_TRADES_PER_SYMBOL})"
                    elif window_key in state["entered_windows"]:
                        reason = f"{tf}m_window_already_entered"
                    elif not analysis.get("signal_confirmed"):
                        reason = analysis.get("reason", "awaiting_confirmed_retest")
                    elif analysis.get("signal") != expected_spike_dir:
                        # Safety guarantee: never buy crash or sell boom
                        reason = "direction_mismatch_rejected"
                    elif stake is None:
                        reason = "insufficient_balance"
                    else:
                        trade_ok, guard_why = can_trade()
                        if not trade_ok:
                            reason = guard_why
                        else:
                            # Confirmed Spikebot Setup on this timeframe: Enter Multiplier Trade!
                            exec_res = await _open_multiplier_trade(
                                symbol=sym,
                                contract_type=correct_contract,
                                stake=stake,
                                multiplier=tf_leverage,
                                structural_stop=analysis.get("stop"),
                                structural_target=analysis.get("target"),
                                timeframe=tf
                            )
                            if exec_res == "entered":
                                state["entered_windows"].append(window_key)
                                if len(state["entered_windows"]) > 300:
                                    state["entered_windows"].pop(0)
                                reason = f"entered_{correct_contract}_{tf}m_setup"
                                # Update local open trades count
                                open_trades_this_sym = [t for t in state["active_trades"] if t.get("symbol") == sym]
                            else:
                                reason = exec_res

                    # Log newly detected breaks or zone status on this timeframe
                    curr_break = analysis.get("break_label", "NONE")
                    curr_zone = bool(analysis.get("zone_active", False))
                    if curr_break != "NONE" and curr_break != sym_st.last_logged_break.get(tf):
                        sym_st.last_logged_break[tf] = curr_break
                        log_message(f"[{sym} {tf}m] ⚡ Break Confirmed: {curr_break} @ {spot:.2f} | Zone: [{analysis.get('zone_low', 0):.2f} - {analysis.get('zone_high', 0):.2f}]")

                    if curr_zone and not sym_st.last_logged_zone.get(tf):
                        sym_st.last_logged_zone[tf] = True
                        log_message(f"[{sym} {tf}m] Retest Zone active [{analysis.get('zone_low', 0):.2f} - {analysis.get('zone_high', 0):.2f}] | Invalidation: {analysis.get('invalidation', 0):.2f}")
                    elif not curr_zone and sym_st.last_logged_zone.get(tf):
                        sym_st.last_logged_zone[tf] = False

                    if closed_info.get(tf) and spot:
                        log_message(f"[{sym} {tf}m Closed] Close: {spot:.2f} | Trend: {analysis.get('direction_label')} | Break: {curr_break} | Zone: {'Active (Age ' + str(analysis.get('retest_age')) + '/12)' if curr_zone else 'None'} | Gate: {reason}")

                    # Record telemetry
                    utils.append_csv_row("./logs/signals.csv", csv_header, [
                        datetime.now().isoformat(), sym, tf, spot, correct_contract,
                        tf_leverage, stake if stake else 1.0, analysis.get("direction_label"),
                        analysis.get("zone_active"), analysis.get("signal_confirmed"), reason
                    ])

                    tf_payloads[str(tf)] = {
                        "timeframe": tf,
                        "multiplier": tf_leverage,
                        "analysis": analysis,
                        "reason": reason,
                        "prev_bar": sym_st.prev_bar(tf),
                        "forming_bar": sym_st.current_forming_bar(tf),
                        "candles_count": len(sym_st.candles.get(tf, []))
                    }

                primary_tf_data = tf_payloads.get("5", tf_payloads.get("1"))
                symbols_payload[sym] = {
                    "symbol": sym,
                    "spot": spot,
                    "pip_size": deriv.get_pip_size(sym),
                    "primary_tf": 5,
                    "analysis": primary_tf_data["analysis"] if primary_tf_data else {},
                    "reason": primary_tf_data["reason"] if primary_tf_data else "initializing",
                    "timeframes": tf_payloads,
                    "open_trades_count": len(open_trades_this_sym),
                    "max_trades": settings.MAX_OPEN_TRADES_PER_SYMBOL,
                    "wins": sym_st.wins,
                    "losses": sym_st.losses
                }

            # Periodic heartbeat logging every 2 minutes
            if state["running"] and time.time() - state.get("last_heartbeat_at", 0) > 120:
                state["last_heartbeat_at"] = time.time()
                bal_str = f"${deriv.balance:.2f}" if deriv.balance is not None else "—"
                log_message(f"[Heartbeat] Bot Active (Scanning {ALL_SCAN_TIMEFRAMES}) | Symbols: {active_syms} | Bal: {bal_str} | Open Trades: {len(state['active_trades'])}")

            # Update live state snapshot
            state["latest_data"] = {
                "timestamp": datetime.now().isoformat(),
                "strategy": "Spikebot 3.00 Multi-Timeframe Engine",
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
                    "timeframe_multiplier_map": TIMEFRAME_MULTIPLIER_MAP,
                    "signal_mode": settings.SIGNAL_MODE,
                    "symbols": active_syms,
                    "active_trades": state["active_trades"],
                    "history_count": len(state["trade_history"]),
                    "max_open_trades_per_symbol": settings.MAX_OPEN_TRADES_PER_SYMBOL,
                    "risk": {"type": settings.RISK_TYPE, "value": settings.RISK_VALUE}
                },
                "symbols": symbols_payload
            }
            state["last_update_ts"] = time.time()

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

    # Pre-populate preview candles across all granularities for all symbols
    for s in settings.active_symbols():
        sym_st = get_symbol_state(s)
        for g in ALL_REQUIRED_GRANULARITIES:
            if not sym_st.candles[g]:
                sym_st.candles[g] = generate_preview_candles(s, count=150, granularity=g * 60)
        for tf in ALL_SCAN_TIMEFRAMES:
            p1_min, p2_min = spikebot.parent_timeframes_for(tf)
            sym_st.analyzers[tf].update_data(
                local_raw=sym_st.candles.get(tf, []),
                p1_raw=sym_st.candles.get(p1_min, []),
                p2_raw=sym_st.candles.get(p2_min, [])
            )
            sym_st.latest_analyses[tf] = sym_st.analyzers[tf].analyze_latest()

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


app = FastAPI(title="Deriv Spikebot 3.00 Multi-Timeframe Bot", lifespan=lifespan)
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
    """Download full activity log file."""
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
async def get_candles(symbol: str, tf: int = 5):
    """Serve OHLC candles for any requested timeframe (1m, 2m, 5m, 15m) for the interactive chart."""
    sym = symbol.upper()
    sym_st = get_symbol_state(sym)
    c = sym_st.candles.get(tf, [])
    if c:
        return c

    if deriv.connected:
        try:
            raw = await asyncio.wait_for(deriv.fetch_seed_candles(sym, count=300, granularity=tf * 60), timeout=2.5)
            if raw:
                sym_st.candles[tf] = raw
                return raw
        except Exception:
            pass

    preview = generate_preview_candles(sym, count=150, granularity=tf * 60)
    sym_st.candles[tf] = preview
    return preview


@app.get("/api/indicator/{symbol}")
async def get_indicator(symbol: str, tf: int = 5):
    """Serve market structure indicator levels & markers for any requested timeframe (1m, 2m, 5m, 15m)."""
    sym = symbol.upper()
    sym_st = get_symbol_state(sym)
    p1, p2 = spikebot.parent_timeframes_for(tf)

    if tf not in sym_st.analyzers:
        sym_st.analyzers[tf] = spikebot.SpikebotAnalyzer(
            symbol=sym,
            timeframe_minutes=tf,
            signal_mode=settings.SIGNAL_MODE,
            structure_window=spikebot.ms_window(tf)
        )

    sym_st.analyzers[tf].update_data(
        local_raw=sym_st.candles.get(tf, []),
        p1_raw=sym_st.candles.get(p1, []),
        p2_raw=sym_st.candles.get(p2, [])
    )
    analysis = sym_st.analyzers[tf].analyze_latest()

    active_trade = None
    for t in state.get("active_trades", []):
        if t.get("symbol") == sym and int(t.get("timeframe", 0)) == int(tf):
            active_trade = t
            break

    levels = {
        "support": float(analysis.get("support", 0.0) or 0.0),
        "resistance": float(analysis.get("resistance", 0.0) or 0.0),
        "zone_active": bool(analysis.get("zone_active", False)),
        "zone_mid": float(analysis.get("zone_mid", 0.0) or 0.0),
        "zone_low": float(analysis.get("zone_low", 0.0) or 0.0),
        "zone_high": float(analysis.get("zone_high", 0.0) or 0.0),
        "invalidation": float(analysis.get("invalidation", 0.0) or 0.0),
        "stop": float(active_trade.get("structural_stop") if active_trade else (analysis.get("stop") or 0.0)),
        "target": float(active_trade.get("structural_target") if active_trade else (analysis.get("target") or 0.0)),
        "trade_entry": float(active_trade.get("entry_spot")) if active_trade and active_trade.get("entry_spot") else None,
        "direction": str(analysis.get("direction_label", "NEUTRAL")),
        "break_label": str(analysis.get("break_label", "NONE")),
        "signal_confirmed": bool(analysis.get("signal_confirmed", False))
    }

    return {
        "symbol": sym,
        "timeframe": tf,
        "levels": levels,
        "active_trade": active_trade,
        "markers": analysis.get("markers", [])
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
    log_message(f"Trading STARTED by user (Scanning timeframes: {ALL_SCAN_TIMEFRAMES} | Multipliers: {TIMEFRAME_MULTIPLIER_MAP} | Max Trades/Sym: {settings.MAX_OPEN_TRADES_PER_SYMBOL})")
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
            "risk_type": settings.RISK_TYPE,
            "risk_value": settings.RISK_VALUE,
            "max_open_trades_per_symbol": settings.MAX_OPEN_TRADES_PER_SYMBOL
        },
        "timeframe_multiplier_map": TIMEFRAME_MULTIPLIER_MAP,
        "supported_symbols": SUPPORTED_SYMBOLS,
        "supported_timeframes": SUPPORTED_TIMEFRAMES,
        "strategy": "active_structure"
    }


@app.post("/api/settings")
async def post_settings(new: Dict[str, Any]):
    old_symbols = list(settings.SYMBOLS)
    old_token = settings.DERIV_TOKEN
    old_app = settings.DERIV_APP_ID
    old_mode = settings.MODE

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
    # Strip manual leverage or single tf overrides
    if "trading" in merged_cfg and isinstance(merged_cfg["trading"], dict):
        for k in ["multiplier", "timeframe_minutes"]:
            merged_cfg["trading"].pop(k, None)

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
        if "risk_type" in t:
            settings.RISK_TYPE = t["risk_type"]
        if "risk_value" in t:
            settings.RISK_VALUE = float(t["risk_value"])
        if "max_open_trades_per_symbol" in t:
            settings.MAX_OPEN_TRADES_PER_SYMBOL = max(1, int(t["max_open_trades_per_symbol"]))

    if settings.DERIV_TOKEN != old_token or settings.DERIV_APP_ID != old_app or settings.MODE != old_mode:
        _reconnect_deriv()
        log_message(f"Deriv reconnected (mode={settings.MODE}, symbols={settings.SYMBOLS})")
    elif settings.SYMBOLS != old_symbols:
        await deriv.update_symbols(settings.active_symbols())
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

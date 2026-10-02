"""Deriv Rise and Fall Trading Bot (5-minute timeframe).

Strategy:
  1. Tracks 5-minute candle's Open price against current spot price.
  2. If current price > Open price: RISE (CALL).
  3. If current price < Open price: FALL (PUT).
  4. Expiry: Configurable in ticks (default 5 ticks).
  5. 5-minute candle countdown & remaining time displayed on dashboard.
  6. Execution window: Trades placed only within the first N minutes (default 3m) of the 5m candle.
  7. Martingale: Toggle ON/OFF, multiplier, max steps.
  8. Target profit: Toggle ON/OFF, 5m-window scope vs General session scope.
"""
import asyncio
import json
import math
import os
import sys
import time
from contextlib import asynccontextmanager

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from bot.config import (
    settings,
    SUPPORTED_SYMBOLS,
    SUPPORTED_TIMEFRAMES,
    load_settings,
)
from bot.deriv_ws import DerivClient
from bot.strategy import CandleTracker, MartingaleManager, ProfitAndRiskManager
import bot.utils as utils

candle_tracker = CandleTracker(timeframe_minutes=settings.TIMEFRAME_MINUTES)
martingale = MartingaleManager(
    enabled=settings.MARTINGALE_ENABLED,
    multiplier=settings.MARTINGALE_MULTIPLIER,
    max_steps=settings.MARTINGALE_MAX_STEPS,
)
risk_mgr = ProfitAndRiskManager(
    target_profit_enabled=settings.TARGET_PROFIT_ENABLED,
    target_profit_scope=settings.TARGET_PROFIT_SCOPE,
    target_profit_amount=settings.TARGET_PROFIT_AMOUNT,
    risk_type=settings.RISK_TYPE,
    stop_loss_enabled=settings.STOP_LOSS_ENABLED,
    stop_loss_amount=settings.STOP_LOSS_AMOUNT,
)

deriv = DerivClient(
    app_id=settings.DERIV_APP_ID,
    token=settings.DERIV_TOKEN,
    symbol=settings.SYMBOL,
    mode=settings.MODE,
)

state: Dict[str, Any] = {
    "latest_data": {},
    "last_update_ts": 0,
    "balance": None,
    "balance_fetched_at": 0,
    "active_trades": [],
    "trade_history": [],
    "logs": [],
    "running": False,
    "entered_candles": [],
    "last_trade_reason": "Bot stopped",
}


def log_message(msg: str):
    timestamp_str = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp_str}] {msg}"
    print(line)
    state["logs"].append(line)
    if len(state["logs"]) > 150:
        state["logs"].pop(0)


def save_state():
    try:
        with open("state_data.json", "w", encoding="utf-8") as f:
            json.dump({
                "active_trades": state["active_trades"],
                "trade_history": state["trade_history"],
                "entered_candles": state["entered_candles"],
                "martingale": martingale.get_state(),
                "risk_summary": risk_mgr.get_summary(),
            }, f, indent=2)
    except Exception as e:
        print(f"save_state error: {e}")


def load_state():
    try:
        if os.path.exists("state_data.json"):
            with open("state_data.json", "r", encoding="utf-8") as f:
                d = json.load(f)
            state["active_trades"] = d.get("active_trades", [])
            state["trade_history"] = d.get("trade_history", [])
            state["entered_candles"] = d.get("entered_candles", [])
            m_data = d.get("martingale", {})
            if m_data:
                martingale.loss_streak = m_data.get("loss_streak", 0)
                martingale.win_streak = m_data.get("win_streak", 0)
                martingale.current_step = m_data.get("current_step", 0)
                martingale.last_result = m_data.get("last_result")
                martingale.candle_paused_max_steps = m_data.get("candle_paused_max_steps", False)
                martingale.paused_candle_ts = m_data.get("paused_candle_ts")
            r_data = d.get("risk_summary", {})
            if r_data:
                risk_mgr.session_profit = r_data.get("session_profit", 0.0)
                risk_mgr.total_trades = r_data.get("total_trades", 0)
                risk_mgr.total_wins = r_data.get("total_wins", 0)
                risk_mgr.total_losses = r_data.get("total_losses", 0)
            log_message("Persistent state loaded")
    except Exception as e:
        print(f"load_state error: {e}")


def can_trade() -> Tuple[bool, str]:
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


async def seed_candles():
    granularity = settings.TIMEFRAME_MINUTES * 60
    seeds = await deriv.fetch_seed_candles(count=100, granularity=granularity)
    if seeds:
        candle_tracker.seed(seeds)
        log_message(f"Seeded {len(seeds)} historical {settings.TIMEFRAME_MINUTES}m candles for {settings.SYMBOL}")
        return True
    log_message("Seeding candles pending (will retry once connected)")
    return False


def _calculate_base_stake() -> Optional[float]:
    bal = state["balance"]
    if bal is None or bal <= 0:
        return None
    if settings.RISK_TYPE == "percent":
        amt = (settings.RISK_VALUE / 100.0) * bal
    else:
        amt = float(settings.RISK_VALUE)

    min_s = deriv.effective_min_stake()
    return max(min_s, round(amt, 2))


async def settle_trades():
    """Settle open contracts; when settled, update history, martingale, and risk manager."""
    remaining = []
    changed = False

    for trade in state["active_trades"]:
        cid = trade.get("contract_id")
        if not cid:
            remaining.append(trade)
            continue

        st = await deriv.contract_status(cid)
        if not st.get("ok"):
            remaining.append(trade)
            continue

        if st.get("is_sold"):
            profit_loss = st.get("profit") or 0.0
            won = profit_loss > 0
            trade["status"] = "WON" if won else "LOST"
            trade["profit_loss"] = profit_loss
            trade["sell_price"] = st.get("sell_price")
            trade["exit_tick"] = st.get("exit_tick")
            trade["exit_time"] = datetime.now().isoformat()
            state["trade_history"].insert(0, trade)
            if len(state["trade_history"]) > 200:
                state["trade_history"].pop()

            changed = True
            busted = martingale.on_trade_result(won, candle_start_ts=trade.get("candle_start_ts"))
            risk_mgr.on_trade_settled(profit_loss)

            tag = "WIN" if won else "LOSS"
            log_message(f"{tag}: {trade['contract_type']} {trade['symbol']} settled (P/L ${profit_loss:+.2f})")

            if busted:
                log_message(f"Martingale max_steps ({martingale.max_steps}) hit! Reset to Base Stake. Pausing trading until next {settings.TIMEFRAME_MINUTES}m candle window.")

            # Check if target profit or stop loss triggered auto-stop
            can_tr, reason = risk_mgr.can_trade_target_and_risk(balance=state["balance"])
            if not can_tr and "session" in reason:
                state["running"] = False
                log_message(f"Trading HALTED automatically: {reason}")
            elif not can_tr and "candle" in reason:
                log_message(f"5m Candle limit reached: {reason}. Pausing until next 5m candle.")
        else:
            trade["live_pl"] = st.get("profit")
            trade["current_spot"] = st.get("current_spot")
            remaining.append(trade)

    state["active_trades"] = remaining
    if changed:
        await deriv.fetch_balance()
        state["balance"] = deriv.balance
        save_state()


async def _open_trade(direction: str, duration_ticks: int, stake: float, candle_start_ts: int) -> str:
    """Execute proposal and buy for Rise/Fall tick contract."""
    live_spot = deriv.get_last().get("price")
    if not live_spot:
        return "no_spot"

    # Price the contract
    proposal = await deriv.get_proposal(
        contract_type=direction,
        duration=duration_ticks,
        duration_unit="t",
        amount=stake,
        currency=deriv.currency or settings.CURRENCY,
    )

    if not proposal.get("ok"):
        err = proposal.get("error", "proposal_failed")
        log_message(f"Proposal error: {err}")
        return f"proposal_err:{err}"

    proposal_id = proposal.get("id")
    ask_price = proposal.get("ask_price")
    quoted_payout = proposal.get("payout")

    # Buy contract
    buy_result = await deriv.buy(proposal_id, ask_price)
    if not buy_result.get("ok"):
        err = buy_result.get("error", "buy_failed")
        log_message(f"Buy failed: {err}")
        return f"buy_err:{err}"

    contract_id = buy_result.get("contract_id")
    trade_obj = {
        "contract_id": contract_id,
        "contract_type": direction,
        "symbol": settings.SYMBOL,
        "stake": stake,
        "payout": quoted_payout,
        "entry_spot": proposal.get("spot") or live_spot,
        "duration_ticks": duration_ticks,
        "duration_unit": "ticks",
        "candle_start_ts": candle_start_ts,
        "martingale_step": martingale.current_step,
        "entry_time": datetime.now().isoformat(),
        "status": "OPEN",
        "profit_loss": None,
        "mode": settings.MODE,
    }

    state["active_trades"].append(trade_obj)
    risk_mgr.on_trade_opened()
    save_state()
    log_message(f"ENTERED {direction} @ {trade_obj['entry_spot']} | Stake: ${stake:.2f} | Payout: ${quoted_payout:.2f} | Ticks: {duration_ticks} | Contract ID: {contract_id}")
    return "entered"


def build_snapshot(reason: Optional[str] = None) -> Dict[str, Any]:
    spot = deriv.get_last().get("price")
    timing = candle_tracker.get_timing(trade_window_minutes=settings.TRADE_WINDOW_MINUTES)
    signal_info = candle_tracker.get_signal(spot)
    base_stake = _calculate_base_stake()
    actual_stake = None
    if base_stake:
        actual_stake = martingale.calculate_stake(
            base_stake=base_stake,
            min_stake=deriv.effective_min_stake(),
            max_payout=deriv.max_payout,
        )

    return {
        "timestamp": datetime.now().isoformat(),
        "timing": timing,
        "prices": {
            "spot": spot,
            "pip_size": deriv.pip_size,
        },
        "signal": signal_info,
        "strategy_status": {
            "reason": reason or state.get("last_trade_reason", "stopped"),
            "in_trade_window": timing["in_trade_window"],
            "duration_ticks": settings.DURATION_TICKS,
            "trade_window_minutes": settings.TRADE_WINDOW_MINUTES,
            "timeframe_minutes": settings.TIMEFRAME_MINUTES,
        },
        "martingale": {
            **martingale.get_state(),
            "base_stake": base_stake,
            "next_stake": actual_stake,
        },
        "risk": risk_mgr.get_summary(balance=state["balance"]),
        "trading_state": {
            "mode": settings.MODE,
            "symbol": settings.SYMBOL,
            "balance": state["balance"],
            "currency": deriv.currency or settings.CURRENCY,
            "authorized": deriv.authorized,
            "connected": deriv.connected,
            "is_virtual": deriv.is_virtual,
            "can_trade": can_trade()[0],
            "running": state["running"],
            "active_trades": state["active_trades"],
            "history_count": len(state["trade_history"]),
            "min_stake": deriv.effective_min_stake(),
            "max_payout": deriv.max_payout,
            "last_error": deriv.last_error,
        },
        "logs": list(state["logs"][-50:]),
        "history": list(state["trade_history"][:50]),
    }


async def update_loop():
    csv_header = [
        "timestamp", "candle_start", "elapsed_sec", "remaining_sec",
        "open_price", "spot", "diff", "signal", "in_window",
        "martingale_step", "stake", "trade_result", "session_profit"
    ]
    seeded_symbol = None

    while True:
        try:
            if deriv.connected and seeded_symbol != settings.SYMBOL:
                ok = await seed_candles()
                if ok:
                    seeded_symbol = settings.SYMBOL

            spot = deriv.get_last().get("price")
            just_rolled = candle_tracker.update(spot)
            if just_rolled:
                risk_mgr.on_candle_rolled()
                martingale.on_candle_rolled()
                log_message(f"New {settings.TIMEFRAME_MINUTES}m candle started. Reset candle window metrics and Martingale pause.")

            # Settle open trades
            await settle_trades()

            # Refresh balance periodically
            if deriv.connected and (time.time() - state.get("balance_fetched_at", 0) > 25):
                await deriv.fetch_balance()
                state["balance_fetched_at"] = time.time()
            state["balance"] = deriv.balance

            timing = candle_tracker.get_timing(trade_window_minutes=settings.TRADE_WINDOW_MINUTES)
            signal_info = candle_tracker.get_signal(spot)
            candle_start_ts = timing["candle_start_ts"]
            martingale.check_candle_rollover(candle_start_ts)

            # Evaluate execution gating
            reason = "stopped"
            base_stake = _calculate_base_stake()
            actual_stake = None
            if base_stake:
                actual_stake = martingale.calculate_stake(
                    base_stake=base_stake,
                    min_stake=deriv.effective_min_stake(),
                    max_payout=deriv.max_payout,
                )

            if not state["running"]:
                reason = "stopped"
            elif not spot:
                reason = "waiting_for_spot"
            elif not candle_tracker.current_candle:
                reason = "forming_candle"
            elif len(state["active_trades"]) > 0:
                reason = "trade_in_progress"
            elif martingale.candle_paused_max_steps:
                reason = f"martingale_max_steps_reached (waiting for next {settings.TIMEFRAME_MINUTES}m candle)"
            elif not timing["in_trade_window"]:
                reason = f"outside_window ({timing['remaining_minutes']:.1f}m left in candle)"
            else:
                # Check risk and target profit
                risk_ok, risk_msg = risk_mgr.can_trade_target_and_risk(balance=state["balance"])
                if not risk_ok:
                    reason = risk_msg
                elif not signal_info["ready"]:
                    reason = f"signal_neutral (spot == open)"
                elif actual_stake is None:
                    reason = "no_balance"
                else:
                    auth_ok, auth_why = can_trade()
                    if not auth_ok:
                        reason = auth_why
                    else:
                        direction = signal_info["direction"]
                        exec_res = await _open_trade(
                            direction=direction,
                            duration_ticks=settings.DURATION_TICKS,
                            stake=actual_stake,
                            candle_start_ts=candle_start_ts,
                        )
                        reason = exec_res

            state["last_trade_reason"] = reason

            # Append CSV record
            utils.append_csv_row("./logs/signals.csv", csv_header, [
                datetime.now().isoformat(),
                candle_start_ts,
                round(timing["elapsed_seconds"], 1),
                round(timing["remaining_seconds"], 1),
                signal_info.get("open_price"),
                spot,
                round(signal_info.get("diff", 0.0), 4),
                signal_info.get("label"),
                timing["in_trade_window"],
                martingale.current_step,
                actual_stake,
                reason,
                risk_mgr.session_profit,
            ])

            # Prepare real-time JSON payload for frontend
            state["latest_data"] = build_snapshot(reason=reason)
            state["last_update_ts"] = time.time()

            # Broadcast snapshot over WebSocket in real-time to all connected clients
            await ws_manager.broadcast(state["latest_data"])

        except Exception as e:
            print(f"Error in update loop: {e}")

        # Event-driven WebSocket wait: wakes up immediately upon incoming Deriv tick (0ms delay)
        try:
            await asyncio.wait_for(deriv.tick_event.wait(), timeout=1.0)
            deriv.tick_event.clear()
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    deriv_task = asyncio.create_task(deriv.start())
    for _ in range(30):
        if deriv.connected:
            break
        await asyncio.sleep(0.2)
    await seed_candles()
    loop_task = asyncio.create_task(update_loop())
    yield
    deriv.close()
    deriv_task.cancel()
    loop_task.cancel()
    try:
        await asyncio.gather(deriv_task, loop_task, return_exceptions=True)
    except BaseException:
        pass


app = FastAPI(title="Deriv Rise & Fall 5m Bot", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, data: dict):
        if not self.active_connections:
            return
        msg = json.dumps(data)
        dead = []
        for conn in self.active_connections:
            try:
                await conn.send_text(msg)
            except Exception:
                dead.append(conn)
        for d in dead:
            self.disconnect(d)


ws_manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Full-duplex WebSocket endpoint for real-time frontend streaming."""
    await ws_manager.connect(websocket)
    if state.get("latest_data"):
        try:
            await websocket.send_text(json.dumps(state["latest_data"]))
        except Exception:
            pass
    try:
        while True:
            text = await websocket.receive_text()
            if text == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


# Routes
@app.get("/", response_class=HTMLResponse)
async def dashboard_view(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/api/latest")
async def get_latest_data():
    if not state["latest_data"]:
        state["latest_data"] = build_snapshot()
    return state["latest_data"]


@app.get("/api/logs")
async def get_logs():
    return state["logs"]


@app.get("/history")
async def get_trade_history():
    return state["trade_history"]


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "last_update": state["last_update_ts"],
        "mode": settings.MODE,
        "running": state["running"],
        "connected": deriv.connected,
        "authorized": deriv.authorized,
    }


@app.post("/api/start")
async def start_bot():
    state["running"] = True
    if state["latest_data"].get("trading_state"):
        state["latest_data"]["trading_state"]["running"] = True
    log_message("Trading STARTED by user")
    deriv.tick_event.set()
    return {"ok": True, "running": True}


@app.post("/api/stop")
async def stop_bot():
    state["running"] = False
    if state["latest_data"].get("trading_state"):
        state["latest_data"]["trading_state"]["running"] = False
    log_message("Trading STOPPED by user")
    deriv.tick_event.set()
    return {"ok": True, "running": False}


@app.post("/api/reset_stats")
async def reset_statistics():
    martingale.reset()
    risk_mgr.session_profit = 0.0
    risk_mgr.total_trades = 0
    risk_mgr.total_wins = 0
    risk_mgr.total_losses = 0
    risk_mgr.candle_profit = 0.0
    risk_mgr.candle_trades_count = 0
    save_state()
    log_message("Session statistics and Martingale RESET by user")
    deriv.tick_event.set()
    return {"ok": True}


@app.get("/api/settings")
async def fetch_settings():
    tok = settings.DERIV_TOKEN
    masked_tok = (tok[:6] + "..." + tok[-4:]) if tok and len(tok) > 12 else tok
    return {
        "mode": settings.MODE,
        "deriv": {
            "app_id": settings.DERIV_APP_ID,
            "token": masked_tok,
        },
        "trading": {
            "symbol": settings.SYMBOL,
            "currency": settings.CURRENCY,
            "risk_type": settings.RISK_TYPE,
            "risk_value": settings.RISK_VALUE,
            "timeframe_minutes": settings.TIMEFRAME_MINUTES,
            "duration_ticks": settings.DURATION_TICKS,
            "trade_window_minutes": settings.TRADE_WINDOW_MINUTES,
        },
        "martingale": {
            "enabled": settings.MARTINGALE_ENABLED,
            "multiplier": settings.MARTINGALE_MULTIPLIER,
            "max_steps": settings.MARTINGALE_MAX_STEPS,
        },
        "target_profit": {
            "enabled": settings.TARGET_PROFIT_ENABLED,
            "scope": settings.TARGET_PROFIT_SCOPE,
            "amount": settings.TARGET_PROFIT_AMOUNT,
        },
        "stop_loss": {
            "enabled": settings.STOP_LOSS_ENABLED,
            "amount": settings.STOP_LOSS_AMOUNT,
        },
        "supported_symbols": SUPPORTED_SYMBOLS,
        "supported_timeframes": SUPPORTED_TIMEFRAMES,
    }


@app.post("/api/settings")
async def update_settings(new_config: Dict[str, Any]):
    # Prevent replacing token with masked value
    if isinstance(new_config.get("deriv"), dict):
        tok_val = new_config["deriv"].get("token")
        if tok_val and "..." in tok_val:
            new_config["deriv"].pop("token", None)

    cfg: Dict[str, Any] = {}
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

    def deep_merge(base, override):
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    merged = deep_merge(cfg, new_config)
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)

    # Apply to running instances
    if "mode" in new_config:
        settings.MODE = str(new_config["mode"]).lower()
        deriv.mode = settings.MODE

    if "deriv" in new_config:
        d = new_config["deriv"]
        if "app_id" in d:
            settings.DERIV_APP_ID = str(d["app_id"])
            deriv.app_id = settings.DERIV_APP_ID
        if d.get("token"):
            settings.DERIV_TOKEN = str(d["token"])
            deriv.token = settings.DERIV_TOKEN

    if "trading" in new_config:
        t = new_config["trading"]
        if t.get("symbol") in SUPPORTED_SYMBOLS:
            settings.SYMBOL = t["symbol"]
            deriv.symbol = settings.SYMBOL
        if "currency" in t:
            settings.CURRENCY = t["currency"]
        if "risk_type" in t:
            settings.RISK_TYPE = t["risk_type"]
            risk_mgr.risk_type = settings.RISK_TYPE
        if "risk_value" in t:
            settings.RISK_VALUE = float(t["risk_value"])
        if "timeframe_minutes" in t:
            settings.TIMEFRAME_MINUTES = int(t["timeframe_minutes"])
            candle_tracker.timeframe_minutes = settings.TIMEFRAME_MINUTES
        if "duration_ticks" in t:
            settings.DURATION_TICKS = max(1, min(10, int(t["duration_ticks"])))
        if "trade_window_minutes" in t:
            settings.TRADE_WINDOW_MINUTES = float(t["trade_window_minutes"])

    if "martingale" in new_config:
        m = new_config["martingale"]
        if "enabled" in m:
            settings.MARTINGALE_ENABLED = bool(m["enabled"])
            martingale.enabled = settings.MARTINGALE_ENABLED
        if "multiplier" in m:
            settings.MARTINGALE_MULTIPLIER = float(m["multiplier"])
            martingale.multiplier = settings.MARTINGALE_MULTIPLIER
        if "max_steps" in m:
            settings.MARTINGALE_MAX_STEPS = int(m["max_steps"])
            martingale.max_steps = settings.MARTINGALE_MAX_STEPS

    if "target_profit" in new_config:
        tp = new_config["target_profit"]
        if "enabled" in tp:
            settings.TARGET_PROFIT_ENABLED = bool(tp["enabled"])
            risk_mgr.target_profit_enabled = settings.TARGET_PROFIT_ENABLED
        if "scope" in tp:
            settings.TARGET_PROFIT_SCOPE = tp["scope"]
            risk_mgr.target_profit_scope = settings.TARGET_PROFIT_SCOPE
        if "amount" in tp:
            settings.TARGET_PROFIT_AMOUNT = float(tp["amount"])
            risk_mgr.target_profit_amount = settings.TARGET_PROFIT_AMOUNT

    if "stop_loss" in new_config:
        sl = new_config["stop_loss"]
        if "enabled" in sl:
            settings.STOP_LOSS_ENABLED = bool(sl["enabled"])
            risk_mgr.stop_loss_enabled = settings.STOP_LOSS_ENABLED
        if "amount" in sl:
            settings.STOP_LOSS_AMOUNT = float(sl["amount"])
            risk_mgr.stop_loss_amount = settings.STOP_LOSS_AMOUNT

    state["latest_data"] = build_snapshot()
    log_message("Settings updated and applied dynamically")
    return {"ok": True, "settings": await fetch_settings()}


@app.post("/api/test_token")
async def test_token_endpoint(body: Dict[str, Any]):
    app_id = str(body.get("app_id") or settings.DERIV_APP_ID).strip()
    token = str(body.get("token") or settings.DERIV_TOKEN).strip()
    mode = str(body.get("mode") or settings.MODE).lower()

    if not (app_id and token):
        return {"ok": False, "error": "App ID and Personal Access Token are required"}

    try:
        test_client = DerivClient(app_id=app_id, token=token, symbol=settings.SYMBOL, mode=mode)
        accounts = await test_client.fetch_accounts()
        want_type = "demo" if mode == "demo" else "real"
        matched = [a for a in accounts if (a.get("account_type") or "").lower() == want_type]

        if not matched:
            return {
                "ok": False,
                "error": f"Token valid, but no '{want_type}' account found on this profile",
                "accounts": accounts,
            }

        status = await test_client.fetch_migration_status()
        acct = matched[0]
        return {
            "ok": True,
            "account_id": acct.get("account_id"),
            "account_type": acct.get("account_type"),
            "balance": acct.get("balance"),
            "currency": acct.get("currency"),
            "migration_status": status,
            "accounts": accounts,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8070))
    print("\n" + "=" * 55)
    print("  [+] Deriv Rise & Fall 5m Bot")
    print(f"  [*] Dashboard: http://localhost:{port}")
    print(f"  [*] Settings:  http://localhost:{port}/settings")
    print("=" * 55 + "\n")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        ws="websockets",
        reload=False,
    )


# Deriv Rise and Fall Trading Bot (5-Minute Candle Open-Breakout)

An automated **Rise / Fall (CALL / PUT)** trading bot for **Deriv Synthetic Indices** (e.g. Volatility 100, 75, 50, 25, 10, etc.) with a **FastAPI** backend and responsive **Alpine.js + TailwindCSS** dark-mode dashboard.

---

## 🎯 Strategy & Logic

1. **5-Minute Candle Tracking**:
   - The bot tracks live ticks and groups them into 5-minute candles (`00:00`, `05:00`, `10:00`, etc.).
   - At the start of each 5m candle, the first tick records the **Open Price**.
2. **Rise / Fall Direction**:
   - **Current Spot > Candle Open**: Trades **RISE (`CALL`)**.
   - **Current Spot < Candle Open**: Trades **FALL (`PUT`)**.
   - **Current Spot == Candle Open**: Holds / Neutral (no trade).
3. **Expiry Duration in Ticks**:
   - The contract expiry is set in **ticks** (default: **5 ticks**, configurable in Settings between 1 and 10 ticks).
4. **Candle Countdown & Continuous Trading Window**:
   - The dashboard displays a live countdown timer showing the remaining time in the current 5-minute candle.
   - **Continuous Trading**: The bot executes trades **continuously** (each trade begins as soon as the previous 5-tick contract finishes) within the **first 3 minutes** of the 5-minute candle (configurable in Settings).
   - **Gating Rules**:
     - It trades continuously until it hits the **Target Profit (TP)** or **Stop Loss (SL)** for that 5m window (when Target Scope is set to `5m_window`), or until the **Allowed Trade Window (Minutes)** has passed.
     - When TP or SL for the 5m window is hit, trading pauses for the remainder of that 5-minute candle, and automatically resets and resumes when the next 5-minute candle opens.
     - Once the trade window expires (e.g. past the first 3 minutes of the 5m candle), trading pauses until the next candle opens.
5. **Martingale Money Management**:
   - Toggle to turn Martingale **ON** or **OFF**.
   - Configurable **Multiplier** (e.g. `2.0x`) and **Max Steps** (e.g. `5` steps).
   - On consecutive losses: Stake increases according to `base_stake * (multiplier ^ step)`.
   - On a win: Resets stake back to the base stake.
6. **Target Profit & Stop Loss Risk Management**:
   - Both **Target Profit** and **Stop Loss** strictly inherit their calculation format from **Risk / Stake Type**:
     - When `risk_type` is **`percent`**: Both Target Profit and Stop Loss represent a `%` of your live account balance (e.g. 5% target profit on a $1,000 balance = $50.00; 15% stop loss = $150.00 max drawdown).
     - When `risk_type` is **`fixed`**: Both evaluate strictly as fixed dollar amounts ($).
   - **Scope Modes**:
     - **5m Window Target**: If profit target or stop loss is reached within the current 5-minute candle, trading pauses for the rest of that candle and resets on the next 5m candle.
     - **General / Session Target**: If cumulative session net profit or stop loss reaches the threshold, the bot halts session trading.

---

## 🚀 Getting Started

### 1. Install Requirements

```bash
pip install -r requirements.txt
```

### 2. Run the Bot

```bash
uvicorn main:app --host 0.0.0.0 --port 8070
```

Open your browser at [http://localhost:8070](http://localhost:8070).

---

## ⚙️ Configuration

Open the **Settings** page at [http://localhost:8070/settings](http://localhost:8070/settings):
- **Account Mode**: Demo or Live.
- **Deriv App ID & Token**: Enter your App ID (from `developers.deriv.com`) and Personal Access Token (with Read & Trade permissions). Click **Save & Test Connection**.
- **Symbol**: Select between Volatility indices (`R_100`, `R_75`, `R_50`, `R_25`, `R_10`, `1HZ100V`, etc.).
- **Expiry in Ticks**: Set duration (default: `5 ticks`).
- **Allowed Trade Window**: Set allowed minutes inside 5m candle (default: `3.0 minutes`).
- **Risk / Stake Type**: Choose Fixed ($) or Percent of Balance (%), which automatically sets the format for Base Stake, Target Profit, and Stop Loss.
- **Martingale**: Toggle ON/OFF, multiplier, max steps.
- **Target Profit**: Toggle ON/OFF, choose between `5m Window` or `Session Total`, and set target value.
- **Stop Loss**: Toggle ON/OFF, and set stop loss threshold (evaluated against 5m candle or session).

---

## ⚡ Full WebSocket Architecture (Zero Polling)

The entire application operates via **full real-time WebSockets**:
1. **Deriv WebSocket (`bot/deriv_ws.py`)**: Subscribes to live ticks (`{"ticks": symbol, "subscribe": 1}`) and balances. Each incoming tick instantly trips an event trigger (`deriv.tick_event.set()`), evaluating the strategy with **0ms latency**.
2. **Client Streaming WebSocket (`main.py` -> `/ws`)**: Broadcasts real-time snapshots (live spot, candle countdown, active trades, logs) directly to connected browsers via persistent WebSocket connections.
3. **No HTTP Polling**: `poll_interval_ms` and frontend `setInterval` polling have been completely removed.

---

## 📊 File Layout

- `main.py` — FastAPI application, WebSocket connection manager (`/ws`), background update loop, and trade execution.
- `bot/deriv_ws.py` — Deriv REST + OTP + WebSocket client handling live ticks, balances, proposals, and orders.
- `bot/strategy.py` — 5m candle tracking, Open price comparison, Martingale, and Target Profit logic.
- `bot/config.py` — Settings loading and runtime configuration management.
- `bot/utils.py` — CSV logging and utility helpers.
- `templates/index.html` — Real-time dark mode dashboard with WebSocket connection (`/ws`), 5m countdown progress bar, and active contract tables.
- `templates/settings.html` — Configuration UI for API tokens, timing, Martingale, and risk parameters.
- `config.json` — Persisted user settings.
- `state_data.json` — Persisted trade history and session metrics.

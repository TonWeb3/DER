# Spikebot 3.00 — Deriv Multipliers Multi-Timeframe Bot (BOOM 1000 & CRASH 1000)

A high-performance automated trading bot accurately converted from the **MetaTrader 5 (MT5) Spikebot 3.00 EA** (`StructureCore.mqh`, `SpikebotSignals.mqh`, `Spikebot.mq5`), engineered specifically for trading **Deriv Multiplier contracts** (`MULTUP` / `MULTDOWN`) on **BOOM 1000** and **CRASH 1000**.

---

## Key Features & Upgrades

1. **Exact 1-to-1 MT5 Algorithmic Replica**:
   - Causal 5-bar pivot swings with zero lookahead bias.
   - Dynamic 20-bar median scale (`MSScale`).
   - True both-direction Break of Structure (`BOS`) and Change of Character (`CHoCH`).
   - Role-reversal retest zones ($\pm 0.25 \times \text{scale}$) with structural invalidation ($zone\_mid - direction \times 0.5 \times \text{scale}$) and 12-bar max lifespan.
   - Retest entry triggers before spikes: touch, hold, no spike ($body \le 2 \times \text{scale}$), pinbar rejection or slowing pullback, and $\ge 1.5\text{x}$ opposing swing room.
   - Parent ladder confluence:
     - **1m** base $\rightarrow$ Parents **5m** & **15m**
     - **2m** base $\rightarrow$ Parents **10m** & **30m**
     - **5m** base $\rightarrow$ Parents **15m** & **60m (1h)**
     - **15m** base $\rightarrow$ Parents **60m (1h)** & **240m (4h)**

2. **Concurrent Multi-Timeframe Scanning**:
   - All 4 timeframes (`1m`, `2m`, `5m`, `15m`) are scanned simultaneously on every tick.
   - Whenever an eligible setup confirms on *any* timeframe, the bot enters the trade immediately.

3. **Multi-Timeframe Leverage Mapping**:
   Manual leverage selection is replaced with an automatic, optimal timeframe-to-multiplier matrix:
   - **1m** $\rightarrow$ **500x** (Fast Scalp)
   - **2m** $\rightarrow$ **400x** (Short Scalp)
   - **5m** $\rightarrow$ **300x** (Standard Swing)
   - **15m** $\rightarrow$ **200x** (Macro Swing)

4. **Concurrent Position Tracking per Symbol (`max_open_trades_per_symbol`)**:
   - Holds concurrent positions on the same symbol across different timeframes (e.g., an active 1m position does not block a 5m setup).
   - Configurable limit via `max_open_trades_per_symbol` (default: 2, up to 4).
   - Each position is tracked independently with its own timeframe, entry price, structural Stop Loss, and structural Take Profit.

5. **Strict Direction Lock**:
   - **BOOM 1000**: Exclusively trades **`MULTUP`** (BUY) on bullish structure retests to capture upward spikes. Never sells Boom.
   - **CRASH 1000**: Exclusively trades **`MULTDOWN`** (SELL) on bearish structure retests to capture downward crashes. Never buys Crash.

6. **Interactive Multi-Timeframe Chart**:
   - Real-time TradingView Lightweight Charts with instant switching between symbols (`BOOM 1000` / `CRASH 1000`) and all timeframes (`1m`, `2m`, `5m`, `15m`).
   - Live rendering of Support & Resistance levels, Retest Zones, Invalidation lines, BOS/CHoCH markers, and Buy/Sell execution arrows for the active timeframe.

---

## Quick Start

```bash
pip install -r requirements.txt
python main.py
```

Open **http://localhost:8070** in your browser. Configure your Deriv App ID and Personal Access Token in **Settings**, then click **▶ Start Bot**.

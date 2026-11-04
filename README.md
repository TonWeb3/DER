# Spikebot 3.00 — Deriv Multipliers Market Structure Bot (BOOM 1000 & CRASH 1000)

A high-performance automated trading bot accurately converted from the **MetaTrader 5 (MT5) Spikebot 3.00 EA** (`StructureCore.mqh`, `SpikebotSignals.mqh`, `Spikebot.mq5`), engineered specifically for trading **Deriv Multiplier contracts** (`MULTUP` / `MULTDOWN`) on **BOOM 1000** and **CRASH 1000**.

---

## The Strategy: Structure Atlas & Role-Reversal Retest

The bot implements the exact algorithmic rules of the MT5 Spikebot:

1. **Confirmed Swings (5-Bar Pivot Engine)**:
   - Identifies swing highs ($h1, h2$) and swing lows ($l1, l2$) with a 5-bar causal window ($[p-2 \dots p+2]$).
   - Pivots are usable strictly 2 bars after their center, eliminating lookahead bias.
   - Evaluates baseline structure scale using the median range of the prior 20 bars (`MSScale`).

2. **Market Structure Breaks (BOS & CHoCH)**:
   - Detects breaks when price closes beyond the confirmed swing level by $\ge 0.10 \times \text{scale}$.
   - **BOS (Break of Structure)**: Break in the direction of the existing trend.
   - **CHoCH (Change of Character)**: Break opposing the existing trend, confirming a structural trend reversal.

3. **Role-Reversal Retest Zones**:
   - Created at the broken swing price level ($h1$ or $l1$).
   - Upper boundary: $zone\_mid + 0.25 \times scale$
   - Lower boundary: $zone\_mid - 0.25 \times scale$
   - Structural invalidation: $zone\_mid - direction \times 0.5 \times scale$
   - Active for up to 12 bars unless invalidated by a close past the invalidation level.

4. **Retest Entry Mechanics (Enter Before the Spike)**:
   - **Touch & Hold**: Low touches zone high, and close holds on the correct side of $zone\_mid$.
   - **No Spike on Entry**: Candle body $\le 2 \times scale$ ensures entry is executed *before* the spike occurs.
   - **Momentum Filter**: Rejection pinbar (close in extreme 35% of bar range) OR Slowing momentum ($|body| \le 0.85 \times |previous|$).
   - **Risk/Reward**: Minimum 1.5x risk room to opposing swing extreme.

5. **Multi-Timeframe Confluence (Parent Ladder)**:
   - Base timeframe is confirmed by 2 closed higher parent timeframes:
     - **1m** base $\rightarrow$ Parents: **5m** & **15m**
     - **2m** base $\rightarrow$ Parents: **10m** & **30m**
     - **5m** base $\rightarrow$ Parents: **15m** & **60m (1h)**
     - **15m** base $\rightarrow$ Parents: **60m (1h)** & **240m (4h)**
   - **Active Structure (Default)**: Local setup + at least 1 closed parent agrees.
   - **Aligned Structure**: All closed parents must strictly agree with the spike direction.

---

## Contract Execution (Deriv Multipliers)

- **BOOM 1000**: When a confirmed bullish structure retest fires $\rightarrow$ enters **`MULTUP`** to capture upward spikes.
- **CRASH 1000**: When a confirmed bearish structure retest fires $\rightarrow$ enters **`MULTDOWN`** to capture downward crashes.
- **Multiplier Leverage**: Selectable from **`100x`**, **`200x`**, **`300x`**, **`400x`**, or **`500x`**.
- **Risk Protection**: Maximum loss is strictly capped to the initial stake; optional Take-Profit ($) and Stop-Loss ($) triggers automatically close positions.
- **Manual Control**: 1-click **`✕ Close Position`** button in the open trades table.

---

## Quick Start

```bash
pip install -r requirements.txt
python main.py
```

Open **http://localhost:8070** in your browser. Configure your Deriv App ID and Personal Access Token in **Settings**, then press **▶ Start Bot**.

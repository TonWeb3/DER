"""Spikebot 3.00 — Market Structure & Spike Hunter Engine.
Exact, faithful Python replica of the MetaTrader 5 (MT5) source code:
- StructureCore.mqh (5-bar causal confirmed pivots, BOS / CHoCH, role-reversal retest zones, evidence gating)
- SpikebotSignals.mqh (Active Structure & Aligned Structure higher-timeframe confluence)
- Spikebot.mq5 (Parent ladders, execution rules, and visualization logic)

Algorithmic Invariants (Strict MT5):
1. Confirmed Swings: 5-bar pivot window [p-2 ... p+2] centered at p = j - 2. First usable strictly at j (2 bars lag).
2. Market Structure Breaks: Close beyond swing level by >= 0.10 * scale (BOS = with trend, CHoCH = counter trend).
3. Role-Reversal Retest Zones:
   - zone_mid = broken swing level (h1 or l1)
   - zone_low = zone_mid - 0.25 * zone_scale
   - zone_high = zone_mid + 0.25 * zone_scale
   - invalidation = zone_mid - broke * 0.5 * zone_scale
   - zone_active lifespan strictly <= 12 bars, or until price closes past invalidation.
4. Retest Entry Trigger:
   - Touch: low <= zone_high and high >= zone_low
   - Hold: direction * (close - zone_mid) >= 0
   - No spike on entry bar: body <= 2.0 * scale
   - Momentum: Rejection pinbar (close in extreme 35% of bar) OR Slowing pullback (previous < 0 and |body| <= 0.85 * |previous|)
   - Room: Opposing swing space <= 0 or space >= 1.5 * risk
   - Stop Loss: entry - direction * risk, where risk = max(direction * (entry - invalidation), 0.5 * scale)
   - Target: opposing swing if space > 0 else entry + direction * 2 * risk
5. Multi-Timeframe Confluence:
   - Base timeframe confirmed by higher parent timeframes (Ladder).
   - BOOM1000: spike_direction = +1 (MULTUP)
   - CRASH1000: spike_direction = -1 (MULTDOWN)
"""
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class MSBar:
    time: int       # open epoch seconds
    end: int        # close epoch seconds
    slot: int       # time slot index
    open: float
    high: float
    low: float
    close: float
    scale: float = 0.0


@dataclass
class MSState:
    ready: int = 0
    direction: int = 0          # +1 Bullish, -1 Bearish, 0 Neutral
    entry: int = 0              # +1 Buy, -1 Sell, 0 None
    break_kind: int = 0         # +1 BOS Up, +2 CHoCH Up, -1 BOS Down, -2 CHoCH Down
    zone_direction: int = 0
    zone_active: int = 0
    retest_age: int = 0
    break_time: int = 0
    zone_time: int = 0
    support_time: int = 0
    resistance_time: int = 0
    support: float = 0.0
    resistance: float = 0.0
    zone_low: float = 0.0
    zone_high: float = 0.0
    zone_mid: float = 0.0
    zone_scale: float = 0.0
    invalidation: float = 0.0
    entry_price: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    retracement: float = -999.0


def ms_clear(s: MSState):
    """Exact port of MSClear from StructureCore.mqh."""
    s.ready = 0
    s.direction = 0
    s.entry = 0
    s.break_kind = 0
    s.zone_direction = 0
    s.zone_active = 0
    s.retest_age = 0
    s.break_time = 0
    s.zone_time = 0
    s.support_time = 0
    s.resistance_time = 0
    s.support = 0.0
    s.resistance = 0.0
    s.zone_low = 0.0
    s.zone_high = 0.0
    s.zone_mid = 0.0
    s.zone_scale = 0.0
    s.invalidation = 0.0
    s.entry_price = 0.0
    s.stop = 0.0
    s.target = 0.0
    s.retracement = -999.0


def ms_scale(b: List[MSBar], i: int) -> float:
    """Exact port of MSScale from StructureCore.mqh:
    Median bar range over 20 bars prior to bar i."""
    if i < 20:
        return 0.0
    a = [b[i - 1 - k].high - b[i - 1 - k].low for k in range(20)]
    a.sort()
    return (a[9] + a[10]) * 0.5


def ms_analyze(b: List[MSBar], at: int, s: MSState, window: int = 96):
    """Exact port of MSAnalyze from StructureCore.mqh."""
    ms_clear(s)
    if at < window + 23 or b[at].scale <= 0:
        return

    # In MT5: for(int k=at-window-23;k<at;k++) if(b[k].end!=b[k+1].time) return;
    for k in range(at - window - 23, at):
        if b[k].end != b[k + 1].time:
            return

    s.ready = 1
    h1 = h2 = l1 = l2 = 0.0
    ht = lt = broken_h = broken_l = 0
    born = -1
    used = False

    for j in range(at - window + 1, at + 1):
        p = j - 2
        ph = True
        pl = True

        for k in range(p - 2, p + 3):
            if k == p:
                continue
            if b[p].high <= b[k].high:
                ph = False
            if b[p].low >= b[k].low:
                pl = False

        # The pivot is first usable at j, two bars AFTER its center p.
        if ph:
            h2 = h1
            h1 = b[p].high
            ht = b[p].time
        if pl:
            l2 = l1
            l1 = b[p].low
            lt = b[p].time

        if s.direction == 0 and h2 > 0 and l2 > 0:
            if h1 > h2 and l1 > l2:
                s.direction = 1
            elif h1 < h2 and l1 < l2:
                s.direction = -1

        broke = 0
        if h1 > 0 and ht != broken_h and b[j].close > h1 + 0.10 * b[j].scale and b[j - 1].close <= h1 + 0.10 * b[j].scale:
            broke = 1
            broken_h = ht
        if l1 > 0 and lt != broken_l and b[j].close < l1 - 0.10 * b[j].scale and b[j - 1].close >= l1 - 0.10 * b[j].scale:
            broke = -1
            broken_l = lt

        if broke != 0 and b[j].scale > 0:
            s.break_kind = broke * (2 if (s.direction != 0 and s.direction != broke) else 1)
            s.direction = broke
            s.break_time = b[j].end
            s.zone_time = b[j].end
            s.zone_direction = broke
            s.zone_mid = h1 if broke == 1 else l1
            s.zone_scale = b[j].scale
            s.zone_low = s.zone_mid - 0.25 * s.zone_scale
            s.zone_high = s.zone_mid + 0.25 * s.zone_scale
            s.invalidation = s.zone_mid - broke * 0.5 * s.zone_scale
            s.zone_active = 1
            born = j
            used = False

        if s.zone_active == 1:
            s.retest_age = j - born
            invalid = s.zone_direction * (b[j].close - s.invalidation) < 0
            if (j - born > 12) or invalid:
                s.zone_active = 0

        if s.zone_active == 1 and j > born and not used:
            d = s.zone_direction
            touch = (b[j].low <= s.zone_high) and (b[j].high >= s.zone_low)
            hold = d * (b[j].close - s.zone_mid) >= 0
            bar_range = b[j].high - b[j].low
            location = (b[j].close - b[j].low) / bar_range if bar_range > 0 else 0.5
            body = d * (b[j].close - b[j].open)
            previous = d * (b[j - 1].close - b[j - 1].open)
            rejection = body > 0 and ((d == 1 and location >= 0.65) or (d == -1 and location <= 0.35))
            slowing = body <= 0 and previous < 0 and abs(body) <= 0.85 * abs(previous)
            no_spike = body <= 2.0 * b[j].scale

            if touch and hold and no_spike and (rejection or slowing):
                entry = b[j].close
                structural = s.invalidation
                risk = max(d * (entry - structural), 0.5 * b[j].scale)
                stop = entry - d * risk
                opposing = h1 if d == 1 else l1
                space = d * (opposing - entry)
                room = space <= 0 or space >= 1.5 * risk

                if room and risk > 0:
                    used = True
                    if j == at:
                        s.entry = d
                        s.entry_price = entry
                        s.stop = stop
                        s.target = opposing if space > 0 else entry + d * 2.0 * risk

        s.support = l1
        s.resistance = h1
        s.support_time = lt
        s.resistance_time = ht

    if s.zone_active == 1:
        extreme = h1 if s.zone_direction == 1 else l1
        impulse = s.zone_direction * (extreme - s.zone_mid)
        if impulse > 0:
            s.retracement = 100.0 * s.zone_direction * (extreme - b[at].close) / impulse


def ms_closed_index(b: List[MSBar], decision: int) -> int:
    """Exact port of MSClosedIndex from StructureCore.mqh."""
    count = len(b)
    if count == 0:
        return -1
    lo, hi, ans = 0, count - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if b[mid].end <= decision:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if ans < 0:
        return -1
    if decision == b[ans].end:
        return ans
    if ans + 1 >= count or b[ans + 1].time != b[ans].end or b[ans + 1].time > decision or b[ans + 1].end <= decision:
        return -1
    return ans


def sb_active_confluence(local: MSState, a: MSState, b: MSState, parent_count: int, spike_direction: int) -> bool:
    """Exact port of SBActiveConfluence from SpikebotSignals.mqh."""
    if local.ready == 0 or local.entry != spike_direction:
        return False
    if parent_count >= 1 and a.ready == 0:
        return False
    if parent_count >= 2 and b.ready == 0:
        return False
    if parent_count == 0:
        return True
    return a.direction == spike_direction or (parent_count >= 2 and b.direction == spike_direction)


def ms_confluence(local: MSState, a: MSState, b: MSState, parent_count: int, spike_direction: int) -> bool:
    """Exact port of MSConfluence from StructureCore.mqh (Aligned Structure mode)."""
    if local.ready == 0 or local.entry != spike_direction:
        return False
    if parent_count >= 1 and (a.ready == 0 or a.direction != spike_direction):
        return False
    if parent_count >= 2 and (b.ready == 0 or b.direction != spike_direction):
        return False
    return True


def ms_wilson(hits: int, n: int) -> Tuple[float, float]:
    """Exact port of MSWilson from StructureCore.mqh."""
    if n <= 0:
        return 0.0, 1.0
    z = 2.5758293035489004
    p = hits / float(n)
    den = 1.0 + (z * z) / float(n)
    center = (p + (z * z) / (2.0 * float(n))) / den
    half = (z * math.sqrt(p * (1.0 - p) / float(n) + (z * z) / (4.0 * float(n) * float(n)))) / den
    return center - half, center + half


def ms_evidence_pass(n: int, hits: int, bn: int, bh: int) -> Tuple[bool, float]:
    """Exact port of MSEvidencePass from StructureCore.mqh."""
    lower, upper = ms_wilson(hits, n)
    bl, bu = ms_wilson(bh, bn)
    passed = (n >= 200 and bn >= 200 and lower >= 0.65 and (lower - bu) >= 0.05)
    return passed, lower


def ms_apply_gate(bars: List[MSBar], count: int, raw: List[int], eligible: List[int],
                  direction: int, preview: bool, warmup: int = 119) -> Tuple[List[int], List[int], List[float]]:
    """Exact port of MSApplyGate from StructureCore.mqh."""
    pn = [0] * (count + 1)
    ph = [0] * (count + 1)
    bn = [0] * (count + 1)
    bh = [0] * (count + 1)
    signals = [0] * count
    prior = [0] * count
    lower = [0.0] * count

    for j in range(count):
        valid = 0
        hit = 0
        candidate = 0
        if j + 3 < count and eligible[j] == 1 and bars[j].slot % 3 == 2:
            valid = 1
            for k in range(j, j + 3):
                if bars[k].end != bars[k + 1].time:
                    valid = 0
            for k in range(j + 1, j + 4):
                if bars[k].scale > 0 and direction * (bars[k].close - bars[k].open) > 2.0 * bars[k].scale:
                    hit = 1
            candidate = 1 if raw[j] != 0 else 0

        pn[j + 1] = pn[j] + valid * candidate
        ph[j + 1] = ph[j] + valid * candidate * hit
        bn[j + 1] = bn[j] + valid
        bh[j + 1] = bh[j] + valid * hit

    for i in range(count):
        signals[i] = raw[i] if preview else 0
        prior[i] = 0
        lower[i] = 0.0
        lo = i - 6002
        hi = i - 3
        if lo < warmup:
            continue
        n = pn[hi + 1] - pn[lo]
        h = ph[hi + 1] - ph[lo]
        base_n = bn[hi + 1] - bn[lo]
        base_h = bh[hi + 1] - bh[lo]
        passed, lower_bound = ms_evidence_pass(n, h, base_n, base_h)
        prior[i] = n
        lower[i] = lower_bound
        if not preview and passed and bars[i].slot % 3 == 2:
            signals[i] = raw[i]

    return signals, prior, lower


def parent_timeframes_for(minutes: int) -> Tuple[int, int]:
    """Exact MT5 Ladder(_Period) from Spikebot.mq5:
    1m  -> parents (5m, 15m)
    2m  -> parents (10m, 30m)
    3m  -> parents (15m, 60m)
    5m  -> parents (15m, 60m)
    10m -> parents (30m, 120m)
    15m -> parents (60m, 240m)
    30m -> parents (120m, 240m)
    60m -> parents (240m, 1440m)
    """
    if minutes == 1:
        return 5, 15
    elif minutes == 2:
        return 10, 30
    elif minutes in (3, 4, 5):
        return 15, 60
    elif minutes in (6, 10):
        return 30, 120
    elif minutes in (12, 15):
        return 60, 240
    elif minutes in (20, 30):
        return 120, 240
    elif minutes == 60:
        return 240, 1440
    else:
        return 1440, 10080


def ms_window(timeframe_minutes: int = 5) -> int:
    """Exact MT5 Window(tf) from Spikebot.mq5:
    tf == PERIOD_MN1 ? 24 : (tf == PERIOD_W1 ? 52 : 96).
    """
    if timeframe_minutes >= 43200:
        return 24
    if timeframe_minutes >= 10080:
        return 52
    return 96


def convert_raw_candles(raw: List[Dict[str, Any]], timeframe_minutes: int) -> List[MSBar]:
    """Convert raw candle dicts into MSBar structures, sanitizing any missing gaps to ensure
    contiguous bars where bars[k].end == bars[k+1].time, exactly as MT5 expects.
    """
    step_sec = timeframe_minutes * 60
    if not raw:
        return []

    sorted_raw = sorted(raw, key=lambda c: int(c["openTime"]))
    deduped: List[Dict[str, Any]] = []
    for c in sorted_raw:
        ot = (int(c["openTime"]) // 1000 // step_sec) * step_sec
        if not deduped or deduped[-1]["openTime"] != ot * 1000:
            deduped.append({
                "openTime": ot * 1000,
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"])
            })

    bars: List[MSBar] = []
    for c in deduped:
        ot = int(c["openTime"]) // 1000
        # If there is a missing candle interval, fill with continuous bar at prior close
        if bars:
            last_end = bars[-1].end
            while last_end < ot:
                fill_p = bars[-1].close
                bars.append(MSBar(
                    time=last_end,
                    end=last_end + step_sec,
                    slot=last_end // step_sec,
                    open=fill_p,
                    high=fill_p,
                    low=fill_p,
                    close=fill_p,
                    scale=0.0
                ))
                last_end += step_sec

        bars.append(MSBar(
            time=ot,
            end=ot + step_sec,
            slot=ot // step_sec,
            open=float(c["open"]),
            high=float(c["high"]),
            low=float(c["low"]),
            close=float(c["close"]),
            scale=0.0
        ))

    for i in range(len(bars)):
        bars[i].scale = ms_scale(bars, i)
    return bars


def compute_structure_markers(bars: List[MSBar], spike_direction: int = 0,
                              signals: Optional[List[int]] = None,
                              states: Optional[List[MSState]] = None) -> List[Dict[str, Any]]:
    """Compute chart markers directly reflecting MT5 SpikebotVisuals & Spikebot.mq5:
    - Confirmed Pivot Highs (SH) and Pivot Lows (SL)
    - Break of Structure (BOS) & Change of Character (CHoCH)
    - BUY / SELL entry arrows
    """
    markers: List[Dict[str, Any]] = []
    n = len(bars)
    if n < 6:
        return markers

    h1 = h2 = l1 = l2 = 0.0
    ht = lt = broken_h = broken_l = 0
    dir_state = 0

    for i in range(2, n - 2):
        p = i
        ph = True
        pl = True
        for k in (p - 2, p - 1, p + 1, p + 2):
            if bars[p].high <= bars[k].high:
                ph = False
            if bars[p].low >= bars[k].low:
                pl = False

        if ph:
            h2 = h1
            h1 = bars[p].high
            ht = bars[p].time
            markers.append({
                "time": bars[p].time,
                "position": "aboveBar",
                "color": "#fb7185",
                "shape": "arrowDown",
                "text": "SH"
            })

        if pl:
            l2 = l1
            l1 = bars[p].low
            lt = bars[p].time
            markers.append({
                "time": bars[p].time,
                "position": "belowBar",
                "color": "#34d399",
                "shape": "arrowUp",
                "text": "SL"
            })

        # Break of Structure / CHoCH confirmation at bar i
        scale = bars[i].scale if bars[i].scale > 0 else 1.0
        if h1 > 0 and ht != broken_h and bars[i].close > h1 + 0.10 * scale and bars[i - 1].close <= h1 + 0.10 * scale:
            broken_h = ht
            lbl = "CHoCH ▲" if (dir_state != 0 and dir_state != 1) else "BOS ▲"
            dir_state = 1
            markers.append({
                "time": bars[i].time,
                "position": "aboveBar",
                "color": "#10b981",
                "shape": "arrowUp",
                "text": lbl
            })

        if l1 > 0 and lt != broken_l and bars[i].close < l1 - 0.10 * scale and bars[i - 1].close >= l1 - 0.10 * scale:
            broken_l = lt
            lbl = "CHoCH ▼" if (dir_state != 0 and dir_state != -1) else "BOS ▼"
            dir_state = -1
            markers.append({
                "time": bars[i].time,
                "position": "belowBar",
                "color": "#f43f5e",
                "shape": "arrowDown",
                "text": lbl
            })

        # Signal markers from MT5 execution signals array
        if signals and i < len(signals) and signals[i] != 0:
            if signals[i] > 0:
                markers.append({
                    "time": bars[i].time,
                    "position": "belowBar",
                    "color": "#1cdab4",
                    "shape": "arrowUp",
                    "text": "BUY ⚡",
                    "size": 2
                })
            else:
                markers.append({
                    "time": bars[i].time,
                    "position": "aboveBar",
                    "color": "#f5445f",
                    "shape": "arrowDown",
                    "text": "SELL ⚡",
                    "size": 2
                })

    markers.sort(key=lambda m: m["time"])
    return markers


class SpikebotAnalyzer:
    """Encapsulates full Spikebot 3.00 Market Structure analysis for a symbol across base + parent timeframes.
    Exact replica of Spikebot.mq5 OnCalculate execution flow.
    """

    def __init__(self, symbol: str, timeframe_minutes: int = 5, signal_mode: str = "active_structure",
                 structure_window: Optional[int] = None):
        self.symbol = symbol.upper()
        self.timeframe_minutes = timeframe_minutes
        self.signal_mode = signal_mode.lower()
        self.structure_window = structure_window if structure_window is not None else ms_window(timeframe_minutes)
        p1, p2 = parent_timeframes_for(timeframe_minutes)
        self.p1_minutes = p1
        self.p2_minutes = p2
        # Deriv tradeable spike direction: BOOM = BUY (+1), CRASH = SELL (-1)
        self.spike_direction = 1 if "BOOM" in self.symbol else -1

        self.local_bars: List[MSBar] = []
        self.p1_bars: List[MSBar] = []
        self.p2_bars: List[MSBar] = []

        self.local_state = MSState()
        self.p1_state = MSState()
        self.p2_state = MSState()

    def update_data(self, local_raw: List[Dict[str, Any]], p1_raw: Optional[List[Dict[str, Any]]] = None,
                    p2_raw: Optional[List[Dict[str, Any]]] = None):
        """Update bar series from raw candles."""
        self.local_bars = convert_raw_candles(local_raw, self.timeframe_minutes)
        if p1_raw:
            self.p1_bars = convert_raw_candles(p1_raw, self.p1_minutes)
        if p2_raw:
            self.p2_bars = convert_raw_candles(p2_raw, self.p2_minutes)

    def analyze_latest(self) -> Dict[str, Any]:
        """Compute the latest structure state and confluence entry signal exactly matching MT5 Spikebot.mq5."""
        total_bars = len(self.local_bars)
        warmup_req = self.structure_window + 24
        contract_type = "MULTUP" if self.spike_direction == 1 else "MULTDOWN"

        if total_bars < warmup_req:
            return {
                "symbol": self.symbol,
                "ready": False,
                "reason": f"Waiting for {warmup_req} closed chart bars ({total_bars}/{warmup_req})",
                "signal": 0,
                "contract_type": contract_type
            }

        # In MT5 OnCalculate:
        # forming = rates_total - 1
        # n = MathMin(rates_total - 1, InpHistoryBars)
        # local array contains the closed bars (up to rates_total - 2)
        n_closed = total_bars - 1
        closed_bars = self.local_bars[:n_closed]
        forming_bar = self.local_bars[-1]

        # 1. Analyze parent timeframes across all closed parent bars
        na = len(self.p1_bars)
        nb = len(self.p2_bars)
        p1_states = [MSState() for _ in range(na)]
        p2_states = [MSState() for _ in range(nb)]

        w_p1 = ms_window(self.p1_minutes)
        for i in range(na):
            ms_analyze(self.p1_bars, i, p1_states[i], w_p1)

        w_p2 = ms_window(self.p2_minutes)
        for i in range(nb):
            ms_analyze(self.p2_bars, i, p2_states[i], w_p2)

        parent_count = (1 if na > 0 else 0) + (1 if nb > 0 else 0)

        # 2. Analyze all closed chart bars (exact port of MT5 OnCalculate loop)
        ls = [MSState() for _ in range(n_closed)]
        raw = [0] * n_closed
        eligible = [0] * n_closed

        for i in range(n_closed):
            ms_analyze(closed_bars, i, ls[i], self.structure_window)

            s1 = MSState()
            s2 = MSState()
            ia = ms_closed_index(self.p1_bars, closed_bars[i].end) if parent_count >= 1 else -1
            ib = ms_closed_index(self.p2_bars, closed_bars[i].end) if parent_count >= 2 else -1

            if ia >= 0:
                s1 = p1_states[ia]
            if ib >= 0:
                s2 = p2_states[ib]

            # Signal evaluation matching MT5 InpSignalMode:
            if self.signal_mode == "aligned_structure":
                is_conf = ms_confluence(ls[i], s1, s2, parent_count, self.spike_direction)
            else:
                # "active_structure" (default MT5 mode)
                is_conf = sb_active_confluence(ls[i], s1, s2, parent_count, self.spike_direction)

            raw[i] = self.spike_direction if is_conf else 0
            eligible[i] = 1 if (ls[i].ready and (parent_count < 1 or s1.ready) and (parent_count < 2 or s2.ready)) else 0

        # Apply gate (preview=True for ACTIVE_STRUCTURE & ALIGNED_STRUCTURE; False for STRICT_EVIDENCE)
        preview = (self.signal_mode != "strict_evidence")
        signals, prior, lower = ms_apply_gate(
            closed_bars, n_closed, raw, eligible, self.spike_direction, preview,
            warmup=self.structure_window + 23
        )

        last_closed_idx = n_closed - 1
        latest_closed_state = ls[last_closed_idx]
        latest_signal = signals[last_closed_idx]
        signal_confirmed = (latest_signal == self.spike_direction)

        # 3. Analyze current forming bar for real-time HUD / level tracking
        forming_state = MSState()
        ms_analyze(self.local_bars, total_bars - 1, forming_state, self.structure_window)

        # Cache active states
        self.local_state = forming_state if forming_state.ready else latest_closed_state
        last_ia = ms_closed_index(self.p1_bars, forming_bar.end) if parent_count >= 1 else -1
        last_ib = ms_closed_index(self.p2_bars, forming_bar.end) if parent_count >= 2 else -1
        self.p1_state = p1_states[last_ia] if last_ia >= 0 else MSState()
        self.p2_state = p2_states[last_ib] if last_ib >= 0 else MSState()

        # Compute full chart markers matching MT5
        markers = compute_structure_markers(closed_bars, spike_direction=self.spike_direction,
                                            signals=signals, states=ls)

        # Determine human-readable HUD texts matching MT5 PanelRow
        dir_name = "UP" if self.local_state.direction > 0 else ("DOWN" if self.local_state.direction < 0 else "NEUTRAL")
        break_desc = "NONE"
        if abs(self.local_state.break_kind) == 2:
            break_desc = "CHoCH " + ("UP" if self.local_state.break_kind > 0 else "DOWN")
        elif abs(self.local_state.break_kind) == 1:
            break_desc = "BOS " + ("UP" if self.local_state.break_kind > 0 else "DOWN")

        p1_dir = "UP" if self.p1_state.direction > 0 else ("DOWN" if self.p1_state.direction < 0 else "NEUTRAL")
        p2_dir = "UP" if self.p2_state.direction > 0 else ("DOWN" if self.p2_state.direction < 0 else "NEUTRAL")

        # Determine valid tradeable zone matching MT5:
        # On BOOM (spike_direction +1): only bullish breaks create valid retest zones.
        # On CRASH (spike_direction -1): only bearish breaks create valid retest zones.
        zone_tradeable = bool(self.local_state.zone_active and self.local_state.zone_direction == self.spike_direction)

        forming_close = float(forming_bar.close) if self.local_bars else 0.0
        scale = float(forming_bar.scale if forming_bar.scale > 0 else (latest_closed_state.zone_scale if latest_closed_state.zone_scale > 0 else 1.0))

        # Set entry price, stop, and target strictly matching MT5 & Video rules
        if signal_confirmed and latest_closed_state.entry == self.spike_direction:
            entry_price = float(latest_closed_state.entry_price)
            s_stop = float(latest_closed_state.stop)
            s_target = float(latest_closed_state.target)

            # Mathematical validity enforcement based on trade direction:
            if self.spike_direction == 1:  # BUY (BOOM)
                if s_stop >= entry_price or s_stop <= 0:
                    risk = max(entry_price - float(latest_closed_state.invalidation), 0.5 * scale) if latest_closed_state.invalidation > 0 else 0.5 * scale
                    s_stop = entry_price - risk
                if s_target <= entry_price:
                    s_target = entry_price + 2.0 * max(0.5 * scale, entry_price - s_stop)
            else:  # SELL (CRASH)
                if s_stop <= entry_price or s_stop <= 0:
                    risk = max(float(latest_closed_state.invalidation) - entry_price, 0.5 * scale) if latest_closed_state.invalidation > 0 else 0.5 * scale
                    s_stop = entry_price + risk
                if s_target >= entry_price:
                    s_target = entry_price - 2.0 * max(0.5 * scale, s_stop - entry_price)

        elif zone_tradeable:
            # Valid pending retest zone waiting for entry trigger
            entry_price = forming_close
            inval = float(self.local_state.invalidation)

            if self.spike_direction == 1:  # BUY (BOOM)
                if inval > 0 and forming_close > inval:
                    risk = max(forming_close - inval, 0.5 * scale)
                    s_stop = inval
                    opposing = float(self.local_state.resistance)
                    space = opposing - forming_close
                    s_target = opposing if (space >= 1.5 * risk) else (forming_close + 2.0 * risk)
                else:
                    s_stop = 0.0
                    s_target = 0.0
                    zone_tradeable = False
            else:  # SELL (CRASH)
                if inval > 0 and forming_close < inval:
                    risk = max(inval - forming_close, 0.5 * scale)
                    s_stop = inval
                    opposing = float(self.local_state.support)
                    space = forming_close - opposing
                    s_target = opposing if (space >= 1.5 * risk) else (forming_close - 2.0 * risk)
                else:
                    s_stop = 0.0
                    s_target = 0.0
                    zone_tradeable = False
        else:
            # No confirmed signal and no valid pending zone for this symbol
            entry_price = forming_close
            s_stop = 0.0
            s_target = 0.0

        # Status reason matching MT5 PanelRow(6)
        if eligible[last_closed_idx] == 0:
            reason = "Entry blocked: structure/parent history incomplete"
        elif signal_confirmed:
            reason = "Setup confirmed at last chart close"
        elif raw[last_closed_idx] != 0:
            reason = "Structure setup present; evidence gate blocks entry"
        elif zone_tradeable:
            reason = f"Retest zone active | age {self.local_state.retest_age}/12 | waiting for retest entry"
        else:
            reason = "Waiting for aligned break + retest"

        inval_level = float(self.local_state.invalidation) if zone_tradeable else 0.0
        zone_mid_level = float(self.local_state.zone_mid) if zone_tradeable else 0.0
        zone_low_level = float(self.local_state.zone_low) if zone_tradeable else 0.0
        zone_high_level = float(self.local_state.zone_high) if zone_tradeable else 0.0
        retest_age = int(self.local_state.retest_age) if zone_tradeable else 0

        return {
            "symbol": self.symbol,
            "ready": bool(latest_closed_state.ready or self.local_state.ready),
            "signal": self.spike_direction if signal_confirmed else 0,
            "contract_type": contract_type,
            "direction": self.local_state.direction,
            "direction_label": dir_name,
            "break_kind": self.local_state.break_kind,
            "break_label": break_desc,
            "support": float(self.local_state.support),
            "resistance": float(self.local_state.resistance),
            "zone_active": bool(zone_tradeable),
            "zone_mid": zone_mid_level,
            "zone_low": zone_low_level,
            "zone_high": zone_high_level,
            "invalidation": inval_level,
            "retest_age": retest_age,
            "retracement": self.local_state.retracement if (zone_tradeable and self.local_state.retracement != -999.0) else None,
            "entry_price": entry_price,
            "stop": s_stop,
            "target": s_target,
            "scale": scale,
            "parent1": {"tf": f"{self.p1_minutes}m", "ready": bool(self.p1_state.ready), "dir": p1_dir},
            "parent2": {"tf": f"{self.p2_minutes}m", "ready": bool(self.p2_state.ready), "dir": p2_dir},
            "confluence_mode": self.signal_mode,
            "signal_confirmed": signal_confirmed,
            "closed_entry_bar_time": closed_bars[last_closed_idx].time if signal_confirmed else None,
            "reason": reason,
            "markers": markers
        }

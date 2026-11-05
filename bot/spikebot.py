"""Spikebot 3.00 — Market Structure & Spike Hunter Engine.
Faithfully ported from the MT5 EA (StructureCore.mqh, SpikebotSignals.mqh, Spikebot.mq5).

Core Strategy:
- Confirmed Swings: 5-bar pivot highs & lows (causal, 2 bars after center pivot).
- Market Structure Direction: higher highs/lows (Bullish=1) vs lower highs/lows (Bearish=-1).
- Break of Structure (BOS) & Change of Character (CHoCH):
    - Break threshold: close beyond swing level +/- 0.10 * scale.
    - If break aligns with trend: BOS (magnitude 1).
    - If break reverses trend: CHoCH (magnitude 2).
- Role-Reversal Retest Zones:
    - Zone mid = broken swing level (h1 or l1).
    - Zone band = zone_mid +/- 0.25 * scale.
    - Structural invalidation = zone_mid - broke * 0.5 * scale.
    - Retest age valid up to 12 bars, invalidated if price closes past invalidation.
- Entry Trigger:
    - Touch of zone: low <= zone_high and high >= zone_low
    - Hold of level: direction * (close - zone_mid) >= 0
    - No spike on entry bar: body <= 2 * scale (enter BEFORE spike)
    - Momentum: Rejection pinbar (closing in extreme 35%) OR Slowing pullback
    - Room: at least 1.5x risk to opposing swing extreme
- Multi-Timeframe Confluence (Ladder):
    - Base timeframe confirmed by 1 or 2 closed parent timeframes.
    - BOOM1000: spike_direction = +1 (MULTUP)
    - CRASH1000: spike_direction = -1 (MULTDOWN)
"""
import math
from dataclasses import dataclass, field
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


def ms_scale(bars: List[MSBar], i: int) -> float:
    """Median bar range over 20 bars prior to bar i (exact port of MSScale)."""
    if i < 20:
        return 0.0
    diffs = [bars[i - 1 - k].high - bars[i - 1 - k].low for k in range(20)]
    diffs.sort()
    return (diffs[9] + diffs[10]) * 0.5


def ms_analyze(bars: List[MSBar], at: int, s: MSState, window: int = 96):
    """Full Market Structure Analysis for bar `at` (exact port of MSAnalyze from StructureCore.mqh)."""
    ms_clear(s)
    if at < window + 23 or bars[at].scale <= 0:
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
        # 5-bar pivot window centered at p: p-2, p-1, p, p+1, p+2
        for k in range(p - 2, p + 3):
            if k == p:
                continue
            if bars[p].high <= bars[k].high:
                ph = False
            if bars[p].low >= bars[k].low:
                pl = False

        # Pivot is first usable at j, exactly two bars after center p
        if ph:
            h2 = h1
            h1 = bars[p].high
            ht = bars[p].time
        if pl:
            l2 = l1
            l1 = bars[p].low
            lt = bars[p].time

        # Initial trend determination from confirmed swings
        if s.direction == 0 and h2 > 0 and l2 > 0:
            if h1 > h2 and l1 > l2:
                s.direction = 1
            elif h1 < h2 and l1 < l2:
                s.direction = -1

        broke = 0
        if h1 > 0 and ht != broken_h and bars[j].close > h1 + 0.10 * bars[j].scale and bars[j - 1].close <= h1 + 0.10 * bars[j].scale:
            broke = 1
            broken_h = ht
        if l1 > 0 and lt != broken_l and bars[j].close < l1 - 0.10 * bars[j].scale and bars[j - 1].close >= l1 - 0.10 * bars[j].scale:
            broke = -1
            broken_l = lt

        if broke != 0 and bars[j].scale > 0:
            s.break_kind = broke * (2 if (s.direction != 0 and s.direction != broke) else 1)
            s.direction = broke
            s.break_time = bars[j].end
            s.zone_time = bars[j].end
            s.zone_direction = broke
            s.zone_mid = h1 if broke == 1 else l1
            s.zone_scale = bars[j].scale
            s.zone_low = s.zone_mid - 0.25 * s.zone_scale
            s.zone_high = s.zone_mid + 0.25 * s.zone_scale
            s.invalidation = s.zone_mid - broke * 0.5 * s.zone_scale
            s.zone_active = 1
            born = j
            used = False

        if s.zone_active == 1:
            s.retest_age = j - born
            invalid = (s.zone_direction * (bars[j].close - s.invalidation)) < 0
            if (j - born > 12) or invalid:
                s.zone_active = 0

        if s.zone_active == 1 and j > born and not used:
            d = s.zone_direction
            touch = (bars[j].low <= s.zone_high) and (bars[j].high >= s.zone_low)
            hold = d * (bars[j].close - s.zone_mid) >= 0
            rng = bars[j].high - bars[j].low
            location = (bars[j].close - bars[j].low) / rng if rng > 0 else 0.5
            body = d * (bars[j].close - bars[j].open)
            previous = d * (bars[j - 1].close - bars[j - 1].open)
            rejection = body > 0 and ((d == 1 and location >= 0.65) or (d == -1 and location <= 0.35))
            slowing = body <= 0 and previous < 0 and abs(body) <= 0.85 * abs(previous)
            no_spike = body <= 2.0 * bars[j].scale

            if touch and hold and no_spike and (rejection or slowing):
                entry = bars[j].close
                structural = s.invalidation
                risk = max(d * (entry - structural), 0.5 * bars[j].scale)
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
                        s.target = opposing if space > 0 else (entry + d * 2 * risk)

        s.support = l1
        s.resistance = h1
        s.support_time = lt
        s.resistance_time = ht

    if s.zone_active == 1:
        extreme = h1 if s.zone_direction == 1 else l1
        impulse = s.zone_direction * (extreme - s.zone_mid)
        if impulse > 0:
            s.retracement = 100.0 * s.zone_direction * (extreme - bars[at].close) / impulse


def ms_closed_index(bars: List[MSBar], decision: int) -> int:
    """Binary search for the latest closed bar at or before `decision` (MSClosedIndex)."""
    count = len(bars)
    if count == 0:
        return -1
    lo, hi, ans = 0, count - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if bars[mid].end <= decision:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if ans < 0:
        return -1
    if decision == bars[ans].end:
        return ans
    if ans + 1 >= count or bars[ans + 1].time != bars[ans].end or bars[ans + 1].time > decision or bars[ans + 1].end <= decision:
        return -1
    return ans


def sb_active_confluence(local: MSState, a: MSState, b: MSState, parent_count: int, spike_direction: int) -> bool:
    """Active Structure mode (SBActiveConfluence from SpikebotSignals.mqh):
    Local entry must match spike direction; at least one parent must agree if parents exist."""
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
    """Aligned Structure mode (MSConfluence from StructureCore.mqh):
    Local entry must match spike direction; all parents must strictly agree."""
    if local.ready == 0 or local.entry != spike_direction:
        return False
    if parent_count >= 1 and (a.ready == 0 or a.direction != spike_direction):
        return False
    if parent_count >= 2 and (b.ready == 0 or b.direction != spike_direction):
        return False
    return True


def ms_wilson(hits: int, n: int) -> Tuple[float, float]:
    """Wilson score confidence interval lower & upper bounds."""
    if n <= 0:
        return 0.0, 1.0
    z = 2.5758293035489004
    p = hits / float(n)
    den = 1.0 + (z * z) / float(n)
    center = (p + (z * z) / (2.0 * float(n))) / den
    half = (z * math.sqrt(p * (1.0 - p) / float(n) + (z * z) / (4.0 * float(n) * float(n)))) / den
    return center - half, center + half


def ms_evidence_pass(n: int, hits: int, bn: int, bh: int) -> Tuple[bool, float]:
    """Strict statistical evidence verification (MSEvidencePass)."""
    lower, upper = ms_wilson(hits, n)
    bl, bu = ms_wilson(bh, bn)
    passed = (n >= 200 and bn >= 200 and lower >= 0.65 and (lower - bu) >= 0.05)
    return passed, lower


def parent_timeframes_for(minutes: int) -> Tuple[int, int]:
    """Timeframe ladder equivalent to Ladder(_Period) in MT5:
    1m  -> parents (5m, 15m)
    2m  -> parents (10m, 30m)
    5m  -> parents (15m, 60m)
    15m -> parents (60m, 240m)
    """
    if minutes == 1:
        return 5, 15
    elif minutes == 2:
        return 10, 30
    elif minutes == 5:
        return 15, 60
    elif minutes <= 15:
        return 60, 240
    else:
        return 240, 1440


def ms_window(timeframe_minutes: int = 5) -> int:
    """Exact MT5 Window(tf) from Spikebot.mq5:
    tf == PERIOD_MN1 ? 24 : (tf == PERIOD_W1 ? 52 : 96).
    For all minute/hour timeframes on synthetic indices, returns 96.
    """
    if timeframe_minutes >= 43200:  # Monthly
        return 24
    if timeframe_minutes >= 10080:  # Weekly
        return 52
    return 96


def convert_raw_candles(raw: List[Dict[str, Any]], timeframe_minutes: int) -> List[MSBar]:
    """Convert Deriv raw candle dictionaries to MSBar structures with scale pre-computed."""
    step_sec = timeframe_minutes * 60
    bars: List[MSBar] = []
    for c in raw:
        open_time = int(c["openTime"]) // 1000
        close_time = open_time + step_sec
        slot = open_time // step_sec
        bars.append(MSBar(
            time=open_time,
            end=close_time,
            slot=slot,
            open=float(c["open"]),
            high=float(c["high"]),
            low=float(c["low"]),
            close=float(c["close"]),
            scale=0.0
        ))
    for i in range(len(bars)):
        bars[i].scale = ms_scale(bars, i)
    return bars


def generate_preview_candles(symbol: str, count: int = 150, granularity: int = 300) -> List[Dict[str, Any]]:
    """Generate realistic initial preview candles for BOOM1000 / CRASH1000 so the chart
    renders immediately while Deriv WebSocket is establishing connection."""
    import time, random
    now_ms = int(time.time() * 1000)
    step_ms = granularity * 1000
    start_ms = now_ms - (count * step_ms)
    candles = []
    base_price = 12450.0 if "BOOM" in symbol else 6320.0
    price = base_price

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


def compute_structure_markers(bars: List[MSBar], spike_direction: int = 1) -> List[Dict[str, Any]]:
    """Compute chart markers for confirmed 5-bar pivots (SH/SL), structure breaks (BOS/CHoCH),
    and trade setups directly matching MT5 SpikebotVisuals.mqh.
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
        if h1 > 0 and ht != broken_h and bars[i].close > h1 + 0.10 * bars[i].scale and bars[i - 1].close <= h1 + 0.10 * bars[i].scale:
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

        if l1 > 0 and lt != broken_l and bars[i].close < l1 - 0.10 * bars[i].scale and bars[i - 1].close >= l1 - 0.10 * bars[i].scale:
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

    markers.sort(key=lambda m: m["time"])
    return markers


class SpikebotAnalyzer:
    """Encapsulates full Spikebot 3.00 Market Structure analysis for a symbol across base + parent timeframes."""

    def __init__(self, symbol: str, timeframe_minutes: int = 5, signal_mode: str = "active_structure",
                 structure_window: Optional[int] = None):
        self.symbol = symbol.upper()
        self.timeframe_minutes = timeframe_minutes
        self.signal_mode = signal_mode
        self.structure_window = structure_window if structure_window is not None else ms_window(timeframe_minutes)
        p1, p2 = parent_timeframes_for(timeframe_minutes)
        self.p1_minutes = p1
        self.p2_minutes = p2
        self.spike_direction = 1 if "BOOM" in self.symbol else -1

        self.local_bars: List[MSBar] = []
        self.p1_bars: List[MSBar] = []
        self.p2_bars: List[MSBar] = []

        self.local_state = MSState()
        self.p1_state = MSState()
        self.p2_state = MSState()

    def update_data(self, local_raw: List[Dict[str, Any]], p1_raw: Optional[List[Dict[str, Any]]] = None,
                    p2_raw: Optional[List[Dict[str, Any]]] = None):
        """Update bar series and compute state."""
        self.local_bars = convert_raw_candles(local_raw, self.timeframe_minutes)
        if p1_raw:
            self.p1_bars = convert_raw_candles(p1_raw, self.p1_minutes)
        if p2_raw:
            self.p2_bars = convert_raw_candles(p2_raw, self.p2_minutes)

    def analyze_latest(self) -> Dict[str, Any]:
        """Compute the latest structure state and confluence entry signal matching MT5 Spikebot."""
        n = len(self.local_bars)
        if n < self.structure_window + 23:
            return {
                "symbol": self.symbol,
                "ready": False,
                "reason": f"warming_up ({n}/{self.structure_window + 23} bars)",
                "signal": 0,
                "contract_type": "MULTUP" if self.spike_direction == 1 else "MULTDOWN"
            }

        # In MT5 Spikebot.mq5, local contains closed bars (rates_total - 2).
        # When self.local_bars contains the unclosed forming bar at index -1,
        # the latest completed closed bar is at closed_at = n - 2.
        closed_at = n - 2 if n >= 2 else n - 1
        forming_at = n - 1

        # 1. Closed Bar Analysis (exact MT5 setup confirmation on bar close)
        closed_state = MSState()
        ms_analyze(self.local_bars, closed_at, closed_state, self.structure_window)

        closed_p1_state = MSState()
        closed_p2_state = MSState()
        if len(self.p1_bars) > 0:
            ia_c = ms_closed_index(self.p1_bars, self.local_bars[closed_at].end)
            if ia_c >= 0:
                ms_analyze(self.p1_bars, ia_c, closed_p1_state, self.structure_window)

        if len(self.p2_bars) > 0:
            ib_c = ms_closed_index(self.p2_bars, self.local_bars[closed_at].end)
            if ib_c >= 0:
                ms_analyze(self.p2_bars, ib_c, closed_p2_state, self.structure_window)

        parent_count = (1 if len(self.p1_bars) > 0 else 0) + (1 if len(self.p2_bars) > 0 else 0)

        # Evaluate confluence on the closed bar
        if self.signal_mode == "aligned_structure":
            signal_confirmed = ms_confluence(
                closed_state, closed_p1_state, closed_p2_state, parent_count, self.spike_direction
            )
        else:
            # "active_structure" (default MT5 mode)
            signal_confirmed = sb_active_confluence(
                closed_state, closed_p1_state, closed_p2_state, parent_count, self.spike_direction
            )

        contract_type = "MULTUP" if self.spike_direction == 1 else "MULTDOWN"
        entry_signal = self.spike_direction if signal_confirmed else 0

        # 2. Live Forming Bar Analysis (for real-time HUD, support, resistance, forming retest zone)
        ms_analyze(self.local_bars, forming_at, self.local_state, self.structure_window)
        if len(self.p1_bars) > 0:
            ia = ms_closed_index(self.p1_bars, self.local_bars[forming_at].end)
            if ia >= 0:
                ms_analyze(self.p1_bars, ia, self.p1_state, self.structure_window)

        if len(self.p2_bars) > 0:
            ib = ms_closed_index(self.p2_bars, self.local_bars[forming_at].end)
            if ib >= 0:
                ms_analyze(self.p2_bars, ib, self.p2_state, self.structure_window)

        # Compute visual chart markers (SH, SL, BOS, CHoCH, BUY, SELL)
        markers = compute_structure_markers(self.local_bars, self.spike_direction)
        if signal_confirmed and len(self.local_bars) >= 2:
            signal_time = self.local_bars[closed_at].time
            if self.spike_direction == 1:
                markers.append({
                    "time": signal_time,
                    "position": "belowBar",
                    "color": "#22c55e",
                    "shape": "arrowUp",
                    "text": "BUY ⚡",
                    "size": 2
                })
            else:
                markers.append({
                    "time": signal_time,
                    "position": "aboveBar",
                    "color": "#ef4444",
                    "shape": "arrowDown",
                    "text": "SELL ⚡",
                    "size": 2
                })
            markers.sort(key=lambda m: m["time"])

        # Human-readable status description
        dir_name = "UP" if self.local_state.direction > 0 else ("DOWN" if self.local_state.direction < 0 else "NEUTRAL")
        break_desc = "NONE"
        if abs(self.local_state.break_kind) == 2:
            break_desc = "CHoCH " + ("UP" if self.local_state.break_kind > 0 else "DOWN")
        elif abs(self.local_state.break_kind) == 1:
            break_desc = "BOS " + ("UP" if self.local_state.break_kind > 0 else "DOWN")

        p1_dir = "UP" if self.p1_state.direction > 0 else ("DOWN" if self.p1_state.direction < 0 else "NEUTRAL")
        p2_dir = "UP" if self.p2_state.direction > 0 else ("DOWN" if self.p2_state.direction < 0 else "NEUTRAL")

        # If entry is confirmed on closed bar, use exact closed setup entry price, stop, target
        if signal_confirmed and closed_state.entry != 0:
            entry_price = float(closed_state.entry_price)
            s_stop = float(closed_state.stop)
            s_target = float(closed_state.target)
        else:
            entry_price = float(self.local_bars[-1].close) if self.local_bars else 0.0
            s_stop = float(self.local_state.stop)
            s_target = float(self.local_state.target)
            unit_scale = float(self.local_bars[-1].scale) if self.local_bars else 1.0

            if s_stop <= 0 and entry_price > 0:
                if self.spike_direction == 1:
                    s_stop = self.local_state.invalidation if self.local_state.invalidation > 0 else (self.local_state.support if self.local_state.support > 0 else entry_price - 2.0 * unit_scale)
                else:
                    s_stop = self.local_state.invalidation if self.local_state.invalidation > 0 else (self.local_state.resistance if self.local_state.resistance > 0 else entry_price + 2.0 * unit_scale)

            if s_target <= 0 and entry_price > 0:
                if self.spike_direction == 1:
                    risk = max(entry_price - s_stop, unit_scale) if s_stop > 0 else unit_scale
                    s_target = self.local_state.resistance if (self.local_state.resistance > entry_price) else entry_price + 2.0 * risk
                else:
                    risk = max(s_stop - entry_price, unit_scale) if s_stop > 0 else unit_scale
                    s_target = self.local_state.support if (self.local_state.support > 0 and self.local_state.support < entry_price) else entry_price - 2.0 * risk

        return {
            "symbol": self.symbol,
            "ready": bool(self.local_state.ready),
            "signal": entry_signal,
            "contract_type": contract_type,
            "direction": self.local_state.direction,
            "direction_label": dir_name,
            "break_kind": self.local_state.break_kind,
            "break_label": break_desc,
            "support": self.local_state.support,
            "resistance": self.local_state.resistance,
            "zone_active": bool(self.local_state.zone_active),
            "zone_mid": self.local_state.zone_mid,
            "zone_low": self.local_state.zone_low,
            "zone_high": self.local_state.zone_high,
            "invalidation": self.local_state.invalidation,
            "retest_age": self.local_state.retest_age,
            "retracement": self.local_state.retracement if self.local_state.retracement != -999.0 else None,
            "entry_price": entry_price,
            "stop": s_stop,
            "target": s_target,
            "parent1": {"tf": f"{self.p1_minutes}m", "ready": bool(self.p1_state.ready), "dir": p1_dir},
            "parent2": {"tf": f"{self.p2_minutes}m", "ready": bool(self.p2_state.ready), "dir": p2_dir},
            "confluence_mode": self.signal_mode,
            "signal_confirmed": signal_confirmed,
            "closed_entry_bar_time": self.local_bars[closed_at].time if signal_confirmed else None,
            "reason": "setup_confirmed" if signal_confirmed else ("waiting_for_retest" if self.local_state.zone_active else "waiting_for_structure_break"),
            "markers": markers
        }

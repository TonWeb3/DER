"""Strategy logic for Deriv Rise and Fall on 5-minute candles.

Rules:
1. 5-minute candle tracking (open, high, low, close).
2. Signal:
   - If current spot > open price -> RISE (CALL)
   - If current spot < open price -> FALL (PUT)
   - If current spot == open price -> NEUTRAL (no trade)
3. Trade window:
   - Only enters trades within the first N minutes (default 3m) of the 5m candle.
   - Remaining time in 5m candle is tracked for the dashboard.
4. Martingale management:
   - Configurable toggle, multiplier, max steps.
   - Resets stake on WIN, scales stake on LOSS.
5. Target profit & Risk:
   - 5m-window scope (resets per candle) vs General session scope.
   - Stop loss protection.
"""
import time
from typing import Any, Dict, List, Optional, Tuple


class CandleTracker:
    def __init__(self, timeframe_minutes: int = 5):
        self.timeframe_minutes = timeframe_minutes
        self.candles: List[Dict[str, Any]] = []
        self.current_candle: Optional[Dict[str, Any]] = None

    @property
    def span_ms(self) -> int:
        return self.timeframe_minutes * 60 * 1000

    def seed(self, candles: List[Dict[str, Any]]):
        if not candles:
            return
        self.candles = list(candles)
        self.current_candle = dict(self.candles[-1]) if self.candles else None

    def update(self, spot: float, ts_ms: Optional[float] = None) -> bool:
        """Update forming candle with the latest spot price.
        Returns True if a candle just closed (new candle rolled).
        """
        if spot is None:
            return False

        now_ms = int(ts_ms or (time.time() * 1000))
        bucket = (now_ms // self.span_ms) * self.span_ms

        if self.current_candle is None:
            self.current_candle = {
                "openTime": bucket,
                "open": spot,
                "high": spot,
                "low": spot,
                "close": spot,
                "closeTime": bucket + self.span_ms - 1,
            }
            self.candles.append(dict(self.current_candle))
            return False

        if self.current_candle["openTime"] == bucket:
            self.current_candle["close"] = spot
            self.current_candle["high"] = max(self.current_candle["high"], spot)
            self.current_candle["low"] = min(self.current_candle["low"], spot)
            if self.candles and self.candles[-1]["openTime"] == bucket:
                self.candles[-1] = dict(self.current_candle)
            return False

        # Previous candle closed, roll to new candle
        self.current_candle = {
            "openTime": bucket,
            "open": spot,
            "high": spot,
            "low": spot,
            "close": spot,
            "closeTime": bucket + self.span_ms - 1,
        }
        self.candles.append(dict(self.current_candle))
        if len(self.candles) > 1000:
            self.candles.pop(0)
        return True

    def get_timing(self, trade_window_seconds: Optional[float] = None,
                   trade_window_minutes: Optional[float] = None) -> Dict[str, Any]:
        """Compute candle timing, elapsed and remaining time, and trade window status."""
        span_sec = float(self.timeframe_minutes * 60)
        now_sec = time.time()
        bucket_sec = (int(now_sec) // int(span_sec)) * int(span_sec) if span_sec > 0 else int(now_sec)
        candle_end_sec = bucket_sec + int(span_sec)

        elapsed_sec = max(0.0, now_sec - bucket_sec)
        remaining_sec = max(0.0, candle_end_sec - now_sec)

        # Resolve trade window in seconds
        if trade_window_seconds is not None:
            win_sec = float(trade_window_seconds)
        elif trade_window_minutes is not None:
            win_sec = float(trade_window_minutes) * 60.0
        else:
            win_sec = 180.0

        # Cap trade window to the candle duration (e.g. max 60s for a 1m candle)
        allowed_window_sec = min(win_sec, span_sec) if span_sec > 0 else win_sec
        in_trade_window = elapsed_sec <= allowed_window_sec
        window_remaining_sec = max(0.0, allowed_window_sec - elapsed_sec) if in_trade_window else 0.0

        trade_window_min = round(allowed_window_sec / 60.0, 2)

        return {
            "candle_start_ts": bucket_sec,
            "candle_end_ts": candle_end_sec,
            "elapsed_seconds": elapsed_sec,
            "remaining_seconds": remaining_sec,
            "elapsed_minutes": elapsed_sec / 60.0,
            "remaining_minutes": remaining_sec / 60.0,
            "timeframe_minutes": self.timeframe_minutes,
            "trade_window_seconds": allowed_window_sec,
            "trade_window_minutes": trade_window_min,
            "in_trade_window": in_trade_window,
            "window_remaining_seconds": window_remaining_sec,
            "progress_pct": min(100.0, (elapsed_sec / span_sec) * 100.0) if span_sec > 0 else 0.0,
            "window_limit_pct": min(100.0, (allowed_window_sec / span_sec) * 100.0) if span_sec > 0 else 0.0,
        }

    def calculate_breakout_stats(self, lookback_candles: int = 50) -> Dict[str, Any]:
        """Compute the average high expansion and low expansion relative to the open price
        across the past N completed candles.

        high_delta = high - open (upward move from open)
        low_delta = open - low   (downward move from open)
        """
        closed_candles: List[Dict[str, Any]] = []
        if self.candles:
            if self.current_candle and self.candles[-1].get("openTime") == self.current_candle.get("openTime"):
                closed_candles = self.candles[:-1]
            else:
                closed_candles = list(self.candles)

        recent = closed_candles[-lookback_candles:] if closed_candles else []
        if not recent:
            return {
                "avg_high_delta": 0.0,
                "avg_low_delta": 0.0,
                "avg_move": 0.0,
                "candles_count": 0,
            }

        high_deltas = [max(0.0, float(c.get("high", 0.0)) - float(c.get("open", 0.0))) for c in recent]
        low_deltas = [max(0.0, float(c.get("open", 0.0)) - float(c.get("low", 0.0))) for c in recent]

        avg_high = (sum(high_deltas) / len(high_deltas)) if high_deltas else 0.0
        avg_low = (sum(low_deltas) / len(low_deltas)) if low_deltas else 0.0
        avg_move = (avg_high + avg_low) / 2.0

        return {
            "avg_high_delta": avg_high,
            "avg_low_delta": avg_low,
            "avg_move": avg_move,
            "candles_count": len(recent),
        }

    def get_signal(self, current_spot: Optional[float],
                   lookback_candles: int = 50,
                   threshold_pct: float = 10.0) -> Dict[str, Any]:
        """Compute Rise/Fall signal based on current spot vs current candle open,
        refined by historical breakout average over past N candles.

        Rise requires: spot - open >= rise_threshold
        Fall requires: open - spot >= fall_threshold
        Where:
        rise_threshold = avg_high_delta * (threshold_pct / 100.0)
        fall_threshold = avg_low_delta * (threshold_pct / 100.0)
        """
        stats = self.calculate_breakout_stats(lookback_candles=lookback_candles)
        avg_high = stats["avg_high_delta"]
        avg_low = stats["avg_low_delta"]
        avg_move = stats["avg_move"]
        candles_analyzed = stats["candles_count"]

        pct_factor = max(0.0, threshold_pct) / 100.0
        rise_threshold = round((avg_high if avg_high > 0 else avg_move) * pct_factor, 5)
        fall_threshold = round((avg_low if avg_low > 0 else avg_move) * pct_factor, 5)

        base_res = {
            "ready": False,
            "direction": None,
            "label": "WAITING",
            "diff": 0.0,
            "diff_pct": 0.0,
            "open_price": None,
            "spot": current_spot,
            "candle": dict(self.current_candle) if self.current_candle else None,
            "breakout": {
                "enabled": threshold_pct > 0,
                "lookback_candles": lookback_candles,
                "threshold_pct": threshold_pct,
                "avg_high_delta": avg_high,
                "avg_low_delta": avg_low,
                "avg_move": avg_move,
                "rise_threshold": rise_threshold,
                "fall_threshold": fall_threshold,
                "candles_analyzed": candles_analyzed,
                "in_breakout": False,
                "in_noise_buffer": False,
            }
        }

        if current_spot is None or self.current_candle is None:
            return base_res

        open_p = self.current_candle.get("open")
        if open_p is None:
            return base_res

        diff = current_spot - open_p
        diff_pct = (diff / open_p * 100.0) if open_p else 0.0

        direction = None
        label = "NEUTRAL"
        in_breakout = False
        in_noise_buffer = False

        if diff >= rise_threshold and (rise_threshold > 0 or diff > 1e-9):
            direction = "CALL"
            label = "RISE"
            in_breakout = True
        elif diff <= -fall_threshold and (fall_threshold > 0 or diff < -1e-9):
            direction = "PUT"
            label = "FALL"
            in_breakout = True
        else:
            direction = None
            if rise_threshold > 0 or fall_threshold > 0:
                label = "NOISE"
                in_noise_buffer = True
            else:
                label = "NEUTRAL"

        return {
            "ready": direction is not None,
            "direction": direction,  # "CALL" or "PUT" or None
            "label": label,          # "RISE", "FALL", "NOISE", "NEUTRAL"
            "diff": diff,
            "diff_pct": diff_pct,
            "open_price": open_p,
            "spot": current_spot,
            "candle": dict(self.current_candle),
            "breakout": {
                "enabled": threshold_pct > 0,
                "lookback_candles": lookback_candles,
                "threshold_pct": threshold_pct,
                "avg_high_delta": avg_high,
                "avg_low_delta": avg_low,
                "avg_move": avg_move,
                "rise_threshold": rise_threshold,
                "fall_threshold": fall_threshold,
                "candles_analyzed": candles_analyzed,
                "in_breakout": in_breakout,
                "in_noise_buffer": in_noise_buffer,
            }
        }


class MartingaleManager:
    def __init__(self, enabled: bool = False, multiplier: float = 2.0, max_steps: int = 5):
        self.enabled = enabled
        self.multiplier = max(1.0, multiplier)
        self.max_steps = max(1, max_steps)
        self.loss_streak = 0
        self.win_streak = 0
        self.current_step = 0
        self.last_result: Optional[str] = None
        self.candle_paused_max_steps: bool = False
        self.paused_candle_ts: Optional[int] = None

    def on_trade_result(self, won: bool, candle_start_ts: Optional[int] = None) -> bool:
        """Process trade outcome.
        Returns True if max_steps was hit, resetting stake to Base Stake and pausing until next candle.
        """
        if won:
            self.last_result = "WIN"
            self.win_streak += 1
            self.loss_streak = 0
            self.current_step = 0
            return False
        else:
            self.last_result = "LOSS"
            self.loss_streak += 1
            self.win_streak = 0

            # If martingale is enabled and recovery attempts exceeded max_steps:
            if self.enabled and self.loss_streak > self.max_steps:
                self.current_step = 0
                self.loss_streak = 0
                self.candle_paused_max_steps = True
                self.paused_candle_ts = candle_start_ts
                return True
            else:
                self.current_step = min(self.loss_streak, self.max_steps)
                return False

    def on_candle_rolled(self):
        """Called when a new 5-minute candle starts; resets the max_steps pause."""
        self.candle_paused_max_steps = False
        self.paused_candle_ts = None

    def check_candle_rollover(self, current_candle_start_ts: Optional[int]):
        """Ensure pause is cleared if candle rolled over even across restarts."""
        if self.candle_paused_max_steps and self.paused_candle_ts is not None:
            if current_candle_start_ts is not None and current_candle_start_ts != self.paused_candle_ts:
                self.candle_paused_max_steps = False
                self.paused_candle_ts = None

    def calculate_stake(self, base_stake: float, min_stake: float = 0.35,
                        max_payout: Optional[float] = None) -> float:
        if not self.enabled or self.current_step <= 0:
            return max(min_stake, round(base_stake, 2))

        # stake = base * (mult ^ step)
        mult_factor = self.multiplier ** self.current_step
        calc = base_stake * mult_factor

        if max_payout and max_payout > 0:
            # Rise/Fall typical payout multiple is ~1.9x to 1.95x, keep stake safe
            payout_multiple = 1.92
            capped = max_payout / payout_multiple
            if calc > capped:
                calc = capped

        return max(min_stake, round(calc, 2))

    def reset(self):
        self.loss_streak = 0
        self.win_streak = 0
        self.current_step = 0
        self.last_result = None
        self.candle_paused_max_steps = False
        self.paused_candle_ts = None

    def get_state(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "multiplier": self.multiplier,
            "max_steps": self.max_steps,
            "current_step": self.current_step,
            "loss_streak": self.loss_streak,
            "win_streak": self.win_streak,
            "last_result": self.last_result,
            "candle_paused_max_steps": self.candle_paused_max_steps,
            "paused_candle_ts": self.paused_candle_ts,
        }


class ProfitAndRiskManager:
    def __init__(self, target_profit_enabled: bool = False,
                 target_profit_scope: str = "candle_window",
                 target_profit_amount: float = 5.0,
                 risk_type: str = "fixed",
                 stop_loss_enabled: bool = False,
                 stop_loss_amount: float = 20.0):
        self.target_profit_enabled = target_profit_enabled
        self.target_profit_scope = target_profit_scope  # "candle_window" (or legacy "5m_window") vs "session"
        self.target_profit_amount = target_profit_amount
        self.risk_type = risk_type                      # "fixed" or "percent"
        self.stop_loss_enabled = stop_loss_enabled
        self.stop_loss_amount = stop_loss_amount

        self.candle_profit = 0.0
        self.candle_trades_count = 0
        self.session_profit = 0.0
        self.total_trades = 0
        self.total_wins = 0
        self.total_losses = 0

    def is_candle_scope(self) -> bool:
        """Determines if target profit or stop loss resets per candle (candle_window or legacy 5m_window)."""
        return str(self.target_profit_scope).lower() in ("candle_window", "5m_window", "candle")

    def is_target_percent(self) -> bool:
        """Determines if target profit is percentage-based: strictly follows risk_type."""
        return self.risk_type == "percent"

    def calculate_target_dollar(self, balance: Optional[float] = None) -> float:
        """Calculate effective dollar target threshold."""
        if self.is_target_percent():
            bal = float(balance) if (balance is not None and balance > 0) else 0.0
            return round((self.target_profit_amount / 100.0) * bal, 2)
        return round(float(self.target_profit_amount), 2)

    def calculate_stop_loss_dollar(self, balance: Optional[float] = None) -> float:
        """Calculate effective dollar stop loss threshold based on risk_type."""
        if self.is_target_percent():
            bal = float(balance) if (balance is not None and balance > 0) else 0.0
            return round((self.stop_loss_amount / 100.0) * bal, 2)
        return round(float(self.stop_loss_amount), 2)

    def on_candle_rolled(self):
        """Called when a new candle starts; resets the candle profit and trade count."""
        self.candle_profit = 0.0
        self.candle_trades_count = 0

    def on_trade_settled(self, profit_loss: float):
        self.total_trades += 1
        if profit_loss > 0:
            self.total_wins += 1
        else:
            self.total_losses += 1
        self.candle_profit += profit_loss
        self.session_profit += profit_loss

    def on_trade_opened(self):
        self.candle_trades_count += 1

    def can_trade_target_and_risk(self, balance: Optional[float] = None) -> Tuple[bool, str]:
        """Check if trading is permitted by target profit and stop loss rules."""
        is_pct = self.is_target_percent()

        # Check Stop Loss (strictly follows risk_type and target_profit_scope)
        if self.stop_loss_enabled:
            stop_dollar = self.calculate_stop_loss_dollar(balance)
            stop_desc = f"{self.stop_loss_amount}% (${stop_dollar:.2f})" if is_pct else f"${stop_dollar:.2f}"
            if stop_dollar > 0:
                if self.is_candle_scope():
                    if self.candle_profit <= -stop_dollar:
                        return False, f"candle_stop_loss_reached (-${abs(self.candle_profit):.2f} <= -{stop_desc})"
                else:  # "session"
                    if self.session_profit <= -stop_dollar:
                        return False, f"session_stop_loss_reached (-${abs(self.session_profit):.2f} <= -{stop_desc})"

        # Check Target Profit (strictly follows risk_type and target_profit_scope)
        if self.target_profit_enabled:
            target_dollar = self.calculate_target_dollar(balance)
            target_desc = f"{self.target_profit_amount}% (${target_dollar:.2f})" if is_pct else f"${target_dollar:.2f}"
            if target_dollar > 0:
                if self.is_candle_scope():
                    if self.candle_profit >= target_dollar:
                        return False, f"candle_target_reached (+${self.candle_profit:.2f} >= {target_desc})"
                else:  # "session"
                    if self.session_profit >= target_dollar:
                        return False, f"session_target_reached (+${self.session_profit:.2f} >= {target_desc})"

        return True, "ok"

    def get_summary(self, balance: Optional[float] = None) -> Dict[str, Any]:
        win_rate = (self.total_wins / self.total_trades * 100.0) if self.total_trades > 0 else 0.0
        target_dollar = self.calculate_target_dollar(balance)
        stop_dollar = self.calculate_stop_loss_dollar(balance)
        is_pct = self.is_target_percent()
        display_str = f"{self.target_profit_amount}% (${target_dollar:.2f})" if is_pct else f"${target_dollar:.2f}"
        stop_display = f"{self.stop_loss_amount}% (${stop_dollar:.2f})" if is_pct else f"${stop_dollar:.2f}"

        return {
            "candle_profit": round(self.candle_profit, 2),
            "candle_trades_count": self.candle_trades_count,
            "session_profit": round(self.session_profit, 2),
            "total_trades": self.total_trades,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "win_rate": round(win_rate, 1),
            "risk_type": self.risk_type,
            "target_profit_enabled": self.target_profit_enabled,
            "target_profit_scope": self.target_profit_scope,
            "is_candle_scope": self.is_candle_scope(),
            "target_profit_is_percent": is_pct,
            "target_profit_amount": self.target_profit_amount,
            "target_profit_dollar": target_dollar,
            "target_profit_display": display_str,
            "stop_loss_enabled": self.stop_loss_enabled,
            "stop_loss_amount": self.stop_loss_amount,
            "stop_loss_dollar": stop_dollar,
            "stop_loss_display": stop_display,
        }

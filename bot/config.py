import os
import json
from typing import Dict, List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


# Deriv synthetic spike indices traded by this bot with Multipliers:
# - BOOM1000: ticks down, spikes up -> traded strictly with MULTUP on bullish structure retest
# - CRASH1000: ticks up, drops down -> traded strictly with MULTDOWN on bearish structure retest
SUPPORTED_SYMBOLS = ["BOOM1000", "CRASH1000"]

# Timeframe to leverage mapping as requested:
# 1m -> 500x
# 2m -> 400x
# 5m -> 300x
# 15m -> 200x
TIMEFRAME_MULTIPLIER_MAP: Dict[int, int] = {
    1: 500,
    2: 400,
    5: 300,
    15: 200
}

SUPPORTED_TIMEFRAMES: List[int] = [1, 2, 5, 15]

# All required granularities across all base timeframes and their parent ladders:
# 1m  -> parents (5m, 15m)
# 2m  -> parents (10m, 30m)
# 5m  -> parents (15m, 60m)
# 15m -> parents (60m, 240m)
ALL_REQUIRED_GRANULARITIES: List[int] = [1, 2, 5, 10, 15, 30, 60, 240]

# Spikebot 3.00 Standard Engine: Active Structure Confluence (from SpikebotSignals.mqh)
DEFAULT_SIGNAL_MODE = "active_structure"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    # "demo" = Deriv virtual account; "live" = real money account.
    MODE: str = "demo"

    # ── Deriv connection (REST -> OTP -> WS) ─────────────────────────
    DERIV_APP_ID: str = os.getenv("DERIV_APP_ID", "")
    DERIV_TOKEN: str = os.getenv("DERIV_TOKEN", "")   # Personal Access Token (pat_...)

    # Active symbols traded concurrently (any subset of SUPPORTED_SYMBOLS)
    SYMBOLS: List[str] = ["BOOM1000", "CRASH1000"]
    SYMBOL: str = "BOOM1000"

    CURRENCY: str = "USD"

    # All timeframes scanned concurrently
    TIMEFRAMES: List[int] = [1, 2, 5, 15]

    # Timeframe to Multiplier leverage map
    TIMEFRAME_MULTIPLIER_MAP: Dict[int, int] = {
        1: 500,
        2: 400,
        5: 300,
        15: 200
    }

    # Spikebot Standard Strategy Mode (Active Structure)
    SIGNAL_MODE: str = "active_structure"

    # Structure Atlas swing & break detection window (default 96 from MT5)
    STRUCTURE_WINDOW: int = 96

    # Risk sizing: "percent" = RISK_VALUE% of balance; "fixed" = RISK_VALUE in USD.
    RISK_TYPE: str = "percent"
    RISK_VALUE: float = 2.0

    # Take Profit & Stop Loss are dynamically driven by Market Structure
    TP_SL_MODE: str = "structural"

    # Maximum concurrent open positions per symbol across all timeframes
    MAX_OPEN_TRADES_PER_SYMBOL: int = 2

    def active_symbols(self) -> List[str]:
        syms = [s for s in self.SYMBOLS if s in SUPPORTED_SYMBOLS]
        if not syms:
            syms = [SUPPORTED_SYMBOLS[0]]
        return syms

    def multiplier_for_tf(self, tf: int) -> int:
        return self.TIMEFRAME_MULTIPLIER_MAP.get(int(tf), 300)


def load_settings() -> Settings:
    base_settings = Settings()
    config_path = "config.json"
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                config_data = json.load(f)

            if "mode" in config_data:
                base_settings.MODE = config_data["mode"]

            if "deriv" in config_data:
                d = config_data["deriv"]
                if "app_id" in d: base_settings.DERIV_APP_ID = str(d["app_id"])
                if "token" in d: base_settings.DERIV_TOKEN = d["token"]

            if "trading" in config_data:
                trading = config_data["trading"]
                if "symbols" in trading and isinstance(trading["symbols"], list):
                    clean_syms = [s for s in trading["symbols"] if s in SUPPORTED_SYMBOLS]
                    if clean_syms:
                        base_settings.SYMBOLS = clean_syms
                elif "symbol" in trading and trading["symbol"] in SUPPORTED_SYMBOLS:
                    base_settings.SYMBOLS = [trading["symbol"]]

                base_settings.SYMBOL = base_settings.SYMBOLS[0]

                if "currency" in trading:
                    base_settings.CURRENCY = trading["currency"]

                base_settings.SIGNAL_MODE = "active_structure"

                if "risk_type" in trading:
                    base_settings.RISK_TYPE = trading["risk_type"]
                if "risk_value" in trading:
                    base_settings.RISK_VALUE = float(trading["risk_value"])
                if "max_open_trades_per_symbol" in trading:
                    base_settings.MAX_OPEN_TRADES_PER_SYMBOL = max(1, int(trading["max_open_trades_per_symbol"]))

        except Exception as e:
            print(f"Warning: Failed to load config.json: {e}")

    return base_settings


settings = load_settings()

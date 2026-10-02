import os
import json
from typing import List, Dict, Any
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_SYMBOLS = [
    "R_100",
    "R_75",
    "R_50",
    "R_25",
    "R_10",
    "1HZ100V",
    "1HZ75V",
    "1HZ50V",
    "1HZ25V",
    "1HZ10V",
]

SUPPORTED_TIMEFRAMES = [1, 2, 3, 5, 10, 15]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    MODE: str = "demo"  # "demo" | "live"

    DERIV_APP_ID: str = os.getenv("DERIV_APP_ID", "")
    DERIV_TOKEN: str = os.getenv("DERIV_TOKEN", "")

    SYMBOL: str = "R_100"
    CURRENCY: str = "USD"

    # 5-minute candle strategy parameters
    TIMEFRAME_MINUTES: int = 5
    DURATION_TICKS: int = 5
    TRADE_WINDOW_MINUTES: float = 3.0  # Only trade within first 3 minutes of 5m candle

    # Base stake & risk
    RISK_TYPE: str = "fixed"  # "fixed" or "percent"
    RISK_VALUE: float = 1.0

    # Martingale
    MARTINGALE_ENABLED: bool = False
    MARTINGALE_MULTIPLIER: float = 2.0
    MARTINGALE_MAX_STEPS: int = 5

    # Target Profit (strictly follows RISK_TYPE: fixed $ or % of balance)
    TARGET_PROFIT_ENABLED: bool = False
    TARGET_PROFIT_SCOPE: str = "5m_window"  # "5m_window" or "session"
    TARGET_PROFIT_AMOUNT: float = 5.0

    # Stop Loss
    STOP_LOSS_ENABLED: bool = False
    STOP_LOSS_AMOUNT: float = 20.0


def load_settings() -> Settings:
    base = Settings()
    config_path = "config.json"
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if "mode" in data:
                base.MODE = str(data["mode"]).lower()

            if "deriv" in data:
                d = data["deriv"]
                if "app_id" in d:
                    base.DERIV_APP_ID = str(d["app_id"])
                if "token" in d:
                    base.DERIV_TOKEN = str(d["token"])

            if "trading" in data:
                t = data["trading"]
                if "symbol" in t and t["symbol"] in SUPPORTED_SYMBOLS:
                    base.SYMBOL = t["symbol"]
                if "currency" in t:
                    base.CURRENCY = t["currency"]
                if "risk_type" in t:
                    base.RISK_TYPE = t["risk_type"]
                if "risk_value" in t:
                    base.RISK_VALUE = float(t["risk_value"])
                if "timeframe_minutes" in t:
                    base.TIMEFRAME_MINUTES = int(t["timeframe_minutes"])
                if "duration_ticks" in t:
                    base.DURATION_TICKS = max(1, min(10, int(t["duration_ticks"])))
                if "trade_window_minutes" in t:
                    base.TRADE_WINDOW_MINUTES = float(t["trade_window_minutes"])

            if "martingale" in data:
                m = data["martingale"]
                if "enabled" in m:
                    base.MARTINGALE_ENABLED = bool(m["enabled"])
                if "multiplier" in m:
                    base.MARTINGALE_MULTIPLIER = float(m["multiplier"])
                if "max_steps" in m:
                    base.MARTINGALE_MAX_STEPS = int(m["max_steps"])

            if "target_profit" in data:
                tp = data["target_profit"]
                if "enabled" in tp:
                    base.TARGET_PROFIT_ENABLED = bool(tp["enabled"])
                if "scope" in tp:
                    base.TARGET_PROFIT_SCOPE = tp["scope"]
                if "amount" in tp:
                    base.TARGET_PROFIT_AMOUNT = float(tp["amount"])

            if "stop_loss" in data:
                sl = data["stop_loss"]
                if "enabled" in sl:
                    base.STOP_LOSS_ENABLED = bool(sl["enabled"])
                if "amount" in sl:
                    base.STOP_LOSS_AMOUNT = float(sl["amount"])

        except Exception as e:
            print(f"Warning: Failed to load config.json: {e}")

    return base


settings = load_settings()

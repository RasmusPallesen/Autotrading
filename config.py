"""
Central configuration for the trading agent.
All secrets are loaded from environment variables — never hardcode keys here.
"""

import json
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class AlpacaConfig:
    api_key: str = os.getenv("ALPACA_API_KEY", "")
    secret_key: str = os.getenv("ALPACA_SECRET_KEY", "")
    paper: bool = os.getenv("ALPACA_PAPER", "true").lower() == "true"

    @property
    def base_url(self) -> str:
        return (
            "https://paper-api.alpaca.markets"
            if self.paper
            else "https://api.alpaca.markets"
        )


@dataclass
class CoinbaseConfig:
    api_key: str = os.getenv("COINBASE_API_KEY", "")
    api_secret: str = os.getenv("COINBASE_API_SECRET", "")
    sandbox: bool = os.getenv("COINBASE_SANDBOX", "true").lower() == "true"


@dataclass
class AnthropicConfig:
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 256


@dataclass
class RiskConfig:
    max_position_pct: float = 0.10
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.15
    max_daily_drawdown_pct: float = 0.04
    max_open_positions: int = 5
    min_settled_cash_reserve: float = 30.0   # Always keep $30 settled for HIGH urgency signals


class WatchlistConfig:
    """
    Loads the trading universe from watchlist.json (same directory as config.py).
    Edit watchlist.json and restart the agent to change the universe — no code change needed.
    """

    _WATCHLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")

    crypto: List[str] = ["BTC-USD", "ETH-USD", "SOL-USD"]

    def _load(self) -> dict:
        with open(self._WATCHLIST_PATH, encoding="utf-8") as f:
            return json.load(f)

    @property
    def all_symbols(self) -> List[str]:
        data = self._load()
        combined: List[str] = []
        for symbols in data.values():
            combined.extend(symbols)
        return list(dict.fromkeys(combined))

    @property
    def stocks(self) -> List[str]:
        return self.all_symbols


@dataclass
class AgentConfig:
    # Default 300s (5 min) — configurable per account type via env var.
    # Live account: set LOOP_INTERVAL_SECONDS=300 in .env_trading
    # Paper account: can use 300-900s depending on preference
    loop_interval_seconds: int = int(__import__("os").getenv("LOOP_INTERVAL_SECONDS", "300"))
    indicator_lookback: int = 50
    min_confidence: float = 0.50  # CHANGED: Lowered from 0.65 for high-velocity trading
    log_level: str = "INFO"

    preferred_sectors: List[str] = field(default_factory=lambda: [
        "AI", "semiconductor", "chip manufacturing", "green energy",
        "solar", "renewable", "EV charging", "hydrogen",
        "biotech", "biopharmaceutical", "clinical stage", "gene therapy",
        "dermatology", "hair loss", "oncology",
        "drone", "autonomous", "defence", "defense", "military",
        "loitering munition", "counter-drone", "unmanned", "UAV", "UAS",
    ])

    sector_bias_boost: float = 0.05


# Singletons
alpaca   = AlpacaConfig()
coinbase = CoinbaseConfig()
anthropic = AnthropicConfig()
risk     = RiskConfig()
watchlist = WatchlistConfig()
agent    = AgentConfig()

"""
Central configuration for the trading agent.
All secrets are loaded from environment variables — never hardcode keys here.
"""

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class AlpacaConfig:
    api_key: str = os.getenv("ALPACA_API_KEY", "")
    secret_key: str = os.getenv("ALPACA_SECRET_KEY", "")
    # paper=True uses paper trading endpoint; flip to False for live
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
    model: str = "claude-haiku-4-5-20251001"   # ← much cheaper than Opus
    max_tokens: int = 512                        # ← reduce from 1024


@dataclass
class RiskConfig:
    # Maximum % of portfolio in a single position
    max_position_pct: float = 0.10
    # Stop-loss: close position if it drops this % from entry
    stop_loss_pct: float = 0.05
    # Take-profit: close position if it gains this %
    take_profit_pct: float = 0.15
    # Max total daily loss before agent shuts down for the day
    max_daily_drawdown_pct: float = 0.03
    # Hard cap on number of open positions simultaneously
    max_open_positions: int = 10


@dataclass
class WatchlistConfig:
    stocks: List[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "NVDA", "TSLA", "AMD",
        "META", "GOOGL", "AMZN", "COIN", "MSTR", "NVO"
    ])
    crypto: List[str] = field(default_factory=lambda: [
        "BTC-USD", "ETH-USD", "SOL-USD"
    ])


@dataclass
class AgentConfig:
    # How often the agent runs its decision loop (seconds)
    loop_interval_seconds: int = 120
    # Lookback window for technical indicators (candles)
    indicator_lookback: int = 50
    # Minimum AI confidence score (0-1) to act on a signal
    min_confidence: float = 0.65
    # Log level
    log_level: str = "INFO"


# Singletons — import these throughout the project
alpaca = AlpacaConfig()
coinbase = CoinbaseConfig()
anthropic = AnthropicConfig()
risk = RiskConfig()
watchlist = WatchlistConfig()
agent = AgentConfig()

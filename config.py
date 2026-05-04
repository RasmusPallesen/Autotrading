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
    max_tokens: int = 512


@dataclass
class RiskConfig:
    max_position_pct: float = 0.1
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.10
    max_daily_drawdown_pct: float = 0.04
    max_open_positions: int = 10


@dataclass
class WatchlistConfig:

    # ── AI & Chip Manufacturing ────────────────────────────────────────────────
    ai_chips: List[str] = field(default_factory=lambda: [
        "NVDA", "AMD", "INTC", "AVGO", "QCOM",
        "ARM", "ASML", "TSM", "MRVL", "AMAT",
    ])

    # ── AI Software & Platforms ────────────────────────────────────────────────
    ai_software: List[str] = field(default_factory=lambda: [
        "MSFT", "GOOGL", "META", "AMZN",
        "PLTR", "AI", "SOUN", "BBAI",
    ])

    # ── Green Energy Technology ────────────────────────────────────────────────
    green_energy: List[str] = field(default_factory=lambda: [
        "ENPH", "SEDG", "FSLR", "NEE", "PLUG",
        "BE", "CHPT", "BLNK", "RUN", "ARRY",
    ])

    # ── MedTech — Diabetes Treatment & Monitoring ──────────────────────────────
    medtech_diabetes: List[str] = field(default_factory=lambda: [
        "NVO", "LLY", "DXCM", "ABT", "ISRG",
        "PODD", "TNDM", "MDT", "INVA", "RYTM",
    ])

    # ── Biotech & Clinical Stage ───────────────────────────────────────────────
    biotech: List[str] = field(default_factory=lambda: [
        "MANE", "RXRX", "BEAM", "CRSP", "NTLA",
    ])

    # ── Drone & Defence Technology ─────────────────────────────────────────────
    drone_defence: List[str] = field(default_factory=lambda: [
        "KTOS",   # Kratos Defence — autonomous combat drones, AI targeting
        "AVAV",   # AeroVironment — Switchblade loitering munition
        "RCAT",   # Red Cat Holdings — Teal drones, US Army standard
        "NOC",    # Northrop Grumman — Global Hawk surveillance drones
        "LMT",    # Lockheed Martin — F-35, missile defence
        "RTX",    # RTX/Raytheon — Coyote counter-drone, loitering munitions
        "AXON",   # Axon Enterprise — drone-mounted systems
        "UMAC",   # Unusual Machines — US-made drones + counter-drone
    ])

    # ── General (broad market / crypto proxy) ──────────────────────────────────
    general: List[str] = field(default_factory=lambda: [
        "AAPL", "TSLA", "COIN", "MSTR",
    ])

    # ── Active trading list (evaluated every tick) ─────────────────────────────
    @property
    def stocks(self) -> List[str]:
        return [
            # Core AI/chips
            "NVDA", "AMD", "ASML", "TSM", "AVGO", "INTC",
            # AI software
            "MSFT", "GOOGL", "PLTR",
            # Green energy
            "ENPH", "FSLR", "NEE",
            # MedTech diabetes
            "NVO", "LLY", "DXCM", "PODD",
            # Biotech
            "MANE", "RXRX",
            # Drone & Defence
            "KTOS", "AVAV",
            # General
            "TSLA", "AAPL",
        ]

    # ── Full research universe (all symbols monitored) ─────────────────────────
    @property
    def all_symbols(self) -> List[str]:
        return list(dict.fromkeys(
            self.ai_chips + self.ai_software + self.green_energy +
            self.medtech_diabetes + self.biotech + self.drone_defence +
            self.general
        ))

    crypto: List[str] = field(default_factory=lambda: [
        "BTC-USD", "ETH-USD", "SOL-USD",
    ])


@dataclass
class AgentConfig:
    loop_interval_seconds: int = 300
    indicator_lookback: int = 50
    min_confidence: float = 0.65
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

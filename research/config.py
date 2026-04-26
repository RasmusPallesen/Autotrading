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
    max_position_pct: float = 0.10
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.15
    max_daily_drawdown_pct: float = 0.03
    max_open_positions: int = 10


@dataclass
class WatchlistConfig:
    # ── AI & Chip Manufacturing ────────────────────────────────────────────────
    ai_chips: List[str] = field(default_factory=lambda: [
        "NVDA",   # Nvidia — dominant AI GPU manufacturer
        "AMD",    # AMD — GPUs, CPUs, AI accelerators
        "INTC",   # Intel — chips, AI accelerators (Gaudi)
        "AVGO",   # Broadcom — custom AI chips (Google TPU)
        "QCOM",   # Qualcomm — edge AI chips
        "ARM",    # ARM Holdings — chip architecture (AI inference)
        "ASML",   # ASML — EUV lithography, enables all advanced chips
        "TSM",    # TSMC — world's largest chip foundry
        "MRVL",   # Marvell — data centre AI chips
        "AMAT",   # Applied Materials — chip manufacturing equipment
    ])

    # ── Pure AI / Software ─────────────────────────────────────────────────────
    ai_software: List[str] = field(default_factory=lambda: [
        "MSFT",   # Microsoft — OpenAI partner, Azure AI
        "GOOGL",  # Alphabet — Gemini, TPUs, DeepMind
        "META",   # Meta — Llama, AI infrastructure
        "AMZN",   # Amazon — AWS AI, Bedrock, Trainium chips
        "PLTR",   # Palantir — AI/data analytics platform
        "AI",     # C3.ai — enterprise AI software
        "SOUN",   # SoundHound — voice AI
        "BBAI",   # BigBear.ai — AI analytics
    ])

    # ── Green Energy Tech ──────────────────────────────────────────────────────
    green_energy: List[str] = field(default_factory=lambda: [
        "ENPH",   # Enphase Energy — solar microinverters
        "SEDG",   # SolarEdge — solar inverters
        "FSLR",   # First Solar — thin-film solar panels
        "NEE",    # NextEra Energy — largest renewable utility
        "PLUG",   # Plug Power — hydrogen fuel cells
        "BE",     # Bloom Energy — solid oxide fuel cells
        "CHPT",   # ChargePoint — EV charging network
        "BLNK",   # Blink Charging — EV charging
        "RUN",    # Sunrun — residential solar
        "ARRY",   # Array Technologies — solar tracking
    ])

    # ── MedTech — Diabetes Treatment & Monitoring ────────────────────────────────
    medtech_diabetes: List[str] = field(default_factory=lambda: [
        "NVO",    # Novo Nordisk — Ozempic, Wegovy (GLP-1 market leader)
        "LLY",    # Eli Lilly — Mounjaro, Zepbound (GLP-1 challenger)
        "DXCM",   # Dexcom — continuous glucose monitors (CGM)
        "ABT",    # Abbott Labs — FreeStyle Libre CGM
        "ISRG",   # Intuitive Surgical — robotic surgery (diabetic complications)
        "PODD",   # Insulet — OmniPod insulin pump
        "TNDM",   # Tandem Diabetes — t:slim insulin pump
        "MDT",    # Medtronic — insulin pumps, CGM
        "INVA",   # Innoviva — specialty pharma / respiratory
        "RYTM",   # Rhythm Pharmaceuticals — rare obesity/diabetes disorders
    ])

    # ── Legacy watchlist (broad market / crypto proxy) ─────────────────────────
    general: List[str] = field(default_factory=lambda: [
        "AAPL",   # Apple
        "TSLA",   # Tesla — EV + AI (FSD, Dojo)
        "COIN",   # Coinbase
        "MSTR",   # MicroStrategy — Bitcoin proxy
    ])

    # ── Active trading list (subset used each cycle) ───────────────────────────
    # Agent trades these — keep to 15 max for cost and focus
    @property
    def stocks(self) -> List[str]:
        return [
            # Core AI/chips (highest conviction sector)
            "NVDA", "AMD", "ASML", "TSM", "AVGO",
            # AI software
            "MSFT", "GOOGL", "PLTR",
            # Green energy
            "ENPH", "FSLR", "NEE",
            # MedTech diabetes
            "NVO", "LLY", "DXCM", "PODD",
            # General
            "TSLA", "AAPL",
        ]

    @property
    def all_symbols(self) -> List[str]:
        """Full universe for research agent monitoring."""
        return list(dict.fromkeys(
            self.ai_chips + self.ai_software + self.green_energy +
            self.medtech_diabetes + self.general
        ))

    crypto: List[str] = field(default_factory=lambda: [
        "BTC-USD", "ETH-USD", "SOL-USD"
    ])


@dataclass
class AgentConfig:
    loop_interval_seconds: int = 300   # 5 min — cost efficient
    indicator_lookback: int = 50
    min_confidence: float = 0.65
    log_level: str = "INFO"

    # Sector bias injected into the AI prompt
    # Stocks in these sectors get a confidence boost when signals are mixed
    preferred_sectors: List[str] = field(default_factory=lambda: [
        "AI", "semiconductor", "chip manufacturing", "green energy",
        "solar", "renewable", "EV charging", "hydrogen"
    ])

    # Confidence bonus applied to preferred sector stocks (0.0 - 0.10)
    sector_bias_boost: float = 0.05


# Singletons
alpaca = AlpacaConfig()
coinbase = CoinbaseConfig()
anthropic = AnthropicConfig()
risk = RiskConfig()
watchlist = WatchlistConfig()
agent = AgentConfig()

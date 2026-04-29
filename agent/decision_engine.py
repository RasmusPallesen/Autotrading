"""
AI Decision Layer.
Sends market snapshots to Claude and parses structured trade decisions.
"""

import json
import logging
from dataclasses import dataclass
from typing import List, Literal, Optional

import anthropic

from signals.technical import SignalSnapshot

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an aggressive, opportunistic trading agent with a strong preference
for three high-conviction sectors:

1. AI & Machine Learning infrastructure (Nvidia, AMD, ASML, TSMC, Broadcom, Marvell, ARM)
2. AI Software & Platforms (Microsoft, Alphabet, Meta, Palantir, C3.ai)
3. Green Energy Technology (solar, wind, EV charging, hydrogen fuel cells)
4. MedTech — Diabetes Treatment & Monitoring (Novo Nordisk, Eli Lilly GLP-1 drugs,
5. Market Scanner Discoveries — stocks flagged for unusual price/volume activity;
   treat with higher caution but act if technical signals confirm the move
   Dexcom/Abbott CGM devices, Insulet/Tandem insulin pumps, Medtronic)

Your trading mandate:
- Momentum trading: ride strong trends in your preferred sectors
- Mean reversion: buy oversold dips in fundamentally strong companies
- Volume-confirmed breakouts
- Sector preference: when signals are mixed or confidence is borderline, FAVOUR stocks
  in AI, semiconductors, chip manufacturing, solar, EV charging, hydrogen, and medtech
  (especially GLP-1 diabetes drugs and CGM devices) over general market stocks

You will receive a market snapshot per symbol and current portfolio context.
Respond ONLY with a valid JSON object — no explanation, no markdown, no preamble.

JSON format:
{
  "symbol": "NVDA",
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "rationale": "One concise sentence explaining the decision",
  "sector": "AI_CHIPS" | "AI_SOFTWARE" | "GREEN_ENERGY" | "MEDTECH" | "GENERAL",
  "suggested_position_pct": 0.0-0.10,
  "suggested_stop_loss_pct": 0.01-0.10,
  "suggested_take_profit_pct": 0.01-0.20,
  "urgency": "LOW" | "MEDIUM" | "HIGH"
}

Rules:
- confidence reflects genuine signal strength; never inflate above 0.9
- for AI/chip/green energy stocks, apply up to +0.05 confidence boost vs equivalent
  signals in general market stocks — these sectors have structural tailwinds
- suggested_position_pct is % of total portfolio (max 0.10 = 10%)
- only return BUY/SELL if confidence >= 0.60
- return HOLD if signals are mixed or unclear
"""

# Sector classification for the watchlist
SECTOR_MAP = {
    # AI Chips
    "NVDA": "AI_CHIPS", "AMD": "AI_CHIPS", "INTC": "AI_CHIPS",
    "AVGO": "AI_CHIPS", "QCOM": "AI_CHIPS", "ARM": "AI_CHIPS",
    "ASML": "AI_CHIPS", "TSM": "AI_CHIPS", "MRVL": "AI_CHIPS", "AMAT": "AI_CHIPS",
    # AI Software
    "MSFT": "AI_SOFTWARE", "GOOGL": "AI_SOFTWARE", "META": "AI_SOFTWARE",
    "AMZN": "AI_SOFTWARE", "PLTR": "AI_SOFTWARE", "AI": "AI_SOFTWARE",
    "SOUN": "AI_SOFTWARE", "BBAI": "AI_SOFTWARE",
    # Green Energy
    "ENPH": "GREEN_ENERGY", "SEDG": "GREEN_ENERGY", "FSLR": "GREEN_ENERGY",
    "NEE": "GREEN_ENERGY", "PLUG": "GREEN_ENERGY", "BE": "GREEN_ENERGY",
    "CHPT": "GREEN_ENERGY", "BLNK": "GREEN_ENERGY", "RUN": "GREEN_ENERGY",
    "ARRY": "GREEN_ENERGY",
    # MedTech Diabetes
    "NVO": "MEDTECH", "LLY": "MEDTECH", "DXCM": "MEDTECH",
    "ABT": "MEDTECH", "ISRG": "MEDTECH", "PODD": "MEDTECH",
    "TNDM": "MEDTECH", "MDT": "MEDTECH", "INVA": "MEDTECH", "RYTM": "MEDTECH",
    # Biotech / Clinical Stage
    "MANE": "BIOTECH", "RXRX": "BIOTECH", "BEAM": "BIOTECH",
    "CRSP": "BIOTECH", "NTLA": "BIOTECH",
    # Drone & Defence
    "KTOS": "DRONE", "AVAV": "DRONE", "RCAT": "DRONE",
    "NOC": "DRONE", "LMT": "DRONE", "RTX": "DRONE",
    "AXON": "DRONE", "UMAC": "DRONE",
    # General
    "AAPL": "GENERAL", "TSLA": "GENERAL", "COIN": "GENERAL", "MSTR": "GENERAL",
}

PREFERRED_SECTORS = {"AI_CHIPS", "AI_SOFTWARE", "GREEN_ENERGY", "MEDTECH", "BIOTECH", "DRONE"}


@dataclass
class TradeDecision:
    symbol: str
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float
    rationale: str
    sector: str
    suggested_position_pct: float
    suggested_stop_loss_pct: float
    suggested_take_profit_pct: float
    urgency: Literal["LOW", "MEDIUM", "HIGH"]

    @classmethod
    def hold(cls, symbol: str, reason: str = "Insufficient confidence") -> "TradeDecision":
        return cls(
            symbol=symbol, action="HOLD", confidence=0.0, rationale=reason,
            sector=SECTOR_MAP.get(symbol, "GENERAL"),
            suggested_position_pct=0.0, suggested_stop_loss_pct=0.05,
            suggested_take_profit_pct=0.10, urgency="LOW",
        )


class AIDecisionEngine:
    """Uses Claude to make trade decisions based on technical signal snapshots."""

    def __init__(self, config):
        self.client = anthropic.Anthropic(api_key=config.api_key)
        self.model = config.model
        self.max_tokens = config.max_tokens

    def decide(
        self,
        snapshot: SignalSnapshot,
        portfolio_context: dict,
        existing_position: Optional[dict] = None,
        sector_bias_boost: float = 0.05,
        research_signal: Optional[dict] = None,
        massive_indicator=None,
        earnings_event=None,
    ) -> TradeDecision:
        user_prompt = self._build_prompt(snapshot, portfolio_context, existing_position, research_signal, massive_indicator, earnings_event)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = response.content[0].text.strip()
            decision = self._parse_response(raw, snapshot.symbol)

            # Apply sector bias boost for preferred sectors
            if decision.sector in PREFERRED_SECTORS and decision.action != "HOLD":
                original = decision.confidence
                decision.confidence = min(decision.confidence + sector_bias_boost, 0.95)
                if decision.confidence != original:
                    logger.debug(
                        "[%s] Sector bias applied: %.2f -> %.2f",
                        snapshot.symbol, original, decision.confidence,
                    )

            return decision

        except anthropic.APIError as e:
            logger.error("Anthropic API error for %s: %s", snapshot.symbol, e)
            return TradeDecision.hold(snapshot.symbol, f"API error: {e}")
        except Exception as e:
            logger.error("Unexpected error for %s: %s", snapshot.symbol, e)
            return TradeDecision.hold(snapshot.symbol, f"Error: {e}")

    def decide_batch(
        self,
        snapshots: List[SignalSnapshot],
        portfolio_context: dict,
        positions: dict,
        sector_bias_boost: float = 0.05,
        research_signals: dict = None,
        massive_indicators: dict = None,
        earnings_events: dict = None,
    ) -> List[TradeDecision]:
        decisions = []
        for snapshot in snapshots:
            existing = positions.get(snapshot.symbol)
            research_signal = (research_signals or {}).get(snapshot.symbol)
            massive_ind = (massive_indicators or {}).get(snapshot.symbol)
            earnings_event = (earnings_events or {}).get(snapshot.symbol)
            decision = self.decide(snapshot, portfolio_context, existing, sector_bias_boost, research_signal, massive_ind, earnings_event)
            sector_tag = f" [{decision.sector}]" if decision.sector != "GENERAL" else ""
            logger.info(
                "[%s]%s %s (conf=%.2f, urgency=%s): %s",
                snapshot.symbol, sector_tag, decision.action,
                decision.confidence, decision.urgency, decision.rationale,
            )
            decisions.append(decision)
        return decisions

    def _build_prompt(self, snapshot, portfolio_context, existing_position, research_signal=None, massive_indicator=None, earnings_event=None):
        sector = SECTOR_MAP.get(snapshot.symbol, "GENERAL")  # Unknown scanner discoveries default to GENERAL
        sector_note = (
            f"\nSector: {sector} — this is a PREFERRED sector, apply confidence boost if signals support it."
            if sector in PREFERRED_SECTORS
            else f"\nSector: {sector} — general market stock, standard analysis applies."
        )

        lines = [
            "=== MARKET SNAPSHOT ===",
            snapshot.to_prompt_text(),
            sector_note,
            "",
            "=== PORTFOLIO CONTEXT ===",
            f"Equity: ${portfolio_context.get('equity', 0):,.2f}",
            f"Cash: ${portfolio_context.get('cash', 0):,.2f}",
            f"Buying power: ${portfolio_context.get('buying_power', 0):,.2f}",
        ]

        if existing_position:
            lines += [
                "",
                "=== EXISTING POSITION ===",
                f"Qty: {existing_position.get('qty', 0)}",
                f"Avg entry: {existing_position.get('avg_entry_price', 0):.4f}",
                f"Unrealised P&L: {existing_position.get('unrealized_plpc', 0) * 100:.2f}%",
            ]
        else:
            lines.append("\nNo existing position.")

        # Inject Massive end-of-day indicators if available
        if massive_indicator:
            lines += [
                "",
                "=== MASSIVE.COM INDICATORS (end-of-day, exchange-grade) ===",
                massive_indicator.to_summary(),
            ]
            # Flag if Massive disagrees significantly with local calcs
            if massive_indicator.conflicts_with_local(
                getattr(snapshot, "rsi_14", None),
                getattr(snapshot, "ema_9", None),
            ):
                lines.append(
                    "WARNING: Massive indicators diverge significantly from local calculations. "
                    "Weight end-of-day Massive data carefully against intraday local data."
                )

        # Inject earnings context
        if earnings_event:
            lines += [
                "",
                "=== EARNINGS CALENDAR ===",
                earnings_event.to_prompt_text(),
            ]

        lines.append("\nProvide your trade decision as JSON only.")
        return "\n".join(lines)

    def _parse_response(self, raw: str, symbol: str) -> TradeDecision:
        try:
            clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(clean)
            return TradeDecision(
                symbol=data.get("symbol", symbol),
                action=data.get("action", "HOLD"),
                confidence=float(data.get("confidence", 0.0)),
                rationale=data.get("rationale", "No rationale"),
                sector=data.get("sector", SECTOR_MAP.get(symbol, "GENERAL")),
                suggested_position_pct=float(data.get("suggested_position_pct", 0.05)),
                suggested_stop_loss_pct=float(data.get("suggested_stop_loss_pct", 0.05)),
                suggested_take_profit_pct=float(data.get("suggested_take_profit_pct", 0.10)),
                urgency=data.get("urgency", "LOW"),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error("Failed to parse AI response for %s: %s", symbol, e)
            return TradeDecision.hold(symbol, "Failed to parse AI response")

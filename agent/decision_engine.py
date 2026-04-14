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

SYSTEM_PROMPT = """You are an aggressive, opportunistic trading agent. Your job is to analyse technical signals and decide whether to BUY, SELL, or HOLD a given asset.

You have a mandate for:
- Momentum trading: ride strong trends
- Mean reversion: buy oversold dips, sell overbought peaks  
- Volume-confirmed breakouts
- Risk awareness: never recommend a trade that violates risk limits

You will receive a market snapshot per symbol and the current portfolio context.
You must respond ONLY with a valid JSON object — no explanation, no markdown, no preamble.

JSON format:
{
  "symbol": "AAPL",
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "rationale": "One concise sentence explaining the decision",
  "suggested_position_pct": 0.0-0.10,
  "suggested_stop_loss_pct": 0.01-0.10,
  "suggested_take_profit_pct": 0.01-0.20,
  "urgency": "LOW" | "MEDIUM" | "HIGH"
}

Rules:
- confidence must reflect genuine signal strength; never inflate above 0.9
- suggested_position_pct is % of total portfolio (max 0.10 = 10%)
- Only return BUY/SELL if confidence >= 0.60
- Return HOLD if signals are mixed or unclear
"""


@dataclass
class TradeDecision:
    symbol: str
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float
    rationale: str
    suggested_position_pct: float
    suggested_stop_loss_pct: float
    suggested_take_profit_pct: float
    urgency: Literal["LOW", "MEDIUM", "HIGH"]

    @classmethod
    def hold(cls, symbol: str, reason: str = "Insufficient confidence") -> "TradeDecision":
        return cls(
            symbol=symbol,
            action="HOLD",
            confidence=0.0,
            rationale=reason,
            suggested_position_pct=0.0,
            suggested_stop_loss_pct=0.05,
            suggested_take_profit_pct=0.10,
            urgency="LOW",
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
    ) -> TradeDecision:
        """
        Send a signal snapshot to Claude and get a trade decision back.
        
        portfolio_context: dict with keys equity, cash, buying_power
        existing_position: dict or None if no current position
        """
        user_prompt = self._build_prompt(snapshot, portfolio_context, existing_position)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = response.content[0].text.strip()
            return self._parse_response(raw, snapshot.symbol)

        except anthropic.APIError as e:
            logger.error("Anthropic API error for %s: %s", snapshot.symbol, e)
            return TradeDecision.hold(snapshot.symbol, f"API error: {e}")
        except Exception as e:
            logger.error("Unexpected error deciding for %s: %s", snapshot.symbol, e)
            return TradeDecision.hold(snapshot.symbol, f"Unexpected error: {e}")

    def decide_batch(
        self,
        snapshots: List[SignalSnapshot],
        portfolio_context: dict,
        positions: dict,
    ) -> List[TradeDecision]:
        """Runs decisions for all snapshots sequentially."""
        decisions = []
        for snapshot in snapshots:
            existing = positions.get(snapshot.symbol)
            decision = self.decide(snapshot, portfolio_context, existing)
            logger.info(
                "[%s] %s (conf=%.2f, urgency=%s): %s",
                snapshot.symbol,
                decision.action,
                decision.confidence,
                decision.urgency,
                decision.rationale,
            )
            decisions.append(decision)
        return decisions

    def _build_prompt(
        self,
        snapshot: SignalSnapshot,
        portfolio_context: dict,
        existing_position: Optional[dict],
    ) -> str:
        lines = [
            "=== MARKET SNAPSHOT ===",
            snapshot.to_prompt_text(),
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
            lines.append("\nNo existing position in this symbol.")

        lines.append("\nProvide your trade decision as JSON only.")
        return "\n".join(lines)

    def _parse_response(self, raw: str, symbol: str) -> TradeDecision:
        """Parse Claude's JSON response into a TradeDecision."""
        try:
            # Strip any accidental markdown fences
            clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(clean)

            return TradeDecision(
                symbol=data.get("symbol", symbol),
                action=data.get("action", "HOLD"),
                confidence=float(data.get("confidence", 0.0)),
                rationale=data.get("rationale", "No rationale provided"),
                suggested_position_pct=float(data.get("suggested_position_pct", 0.05)),
                suggested_stop_loss_pct=float(data.get("suggested_stop_loss_pct", 0.05)),
                suggested_take_profit_pct=float(data.get("suggested_take_profit_pct", 0.10)),
                urgency=data.get("urgency", "LOW"),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error("Failed to parse AI response for %s: %s\nRaw: %s", symbol, e, raw)
            return TradeDecision.hold(symbol, "Failed to parse AI response")

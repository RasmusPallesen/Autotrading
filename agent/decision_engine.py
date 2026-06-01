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


def _build_system_prompt(risk, min_confidence: float = 0.55) -> str:
    """Build the system prompt from live risk config values so the prompt stays
    in sync with config.py without any manual copying."""
    max_pos_pct = risk.max_position_pct
    stop_pct    = risk.stop_loss_pct
    tp_pct      = risk.take_profit_pct
    max_pos     = risk.max_open_positions
    cash_reserve = risk.min_settled_cash_reserve
    return f"""You are an aggressive, opportunistic short-term swing trading agent targeting 1-3 day holds with a +{tp_pct*100:.0f}% profit objective. You have a strong preference for six high-conviction sectors:

SECTOR CLASSIFICATIONS (use the exact sector label for every symbol below):
- AI_CHIPS    — AI & semiconductor: NVDA, AMD, INTC, AVGO, QCOM, ARM, ASML, TSM, MRVL, AMAT
- AI_SOFTWARE — AI platforms & cloud: MSFT, GOOGL, META, AMZN, PLTR, AI, SOUN, BBAI
- GREEN_ENERGY — Solar, EV charging, hydrogen: ENPH, SEDG, FSLR, NEE, PLUG, BE, CHPT, BLNK, RUN, ARRY
- MEDTECH     — GLP-1/CGM/diabetes: NVO, LLY, DXCM, ABT, ISRG, PODD, TNDM, MDT, INVA, RYTM
- BIOTECH     — Clinical-stage gene therapy: MANE, RXRX, BEAM, CRSP, NTLA
- DRONE       — Autonomous systems & defence: KTOS, AVAV, RCAT, NOC, LMT, RTX, AXON, UMAC
- GENERAL     — Broad market / crypto: AAPL, TSLA, COIN, MSTR
- GENERAL     — Scanner discoveries not in the above lists

Your trading mandate:
- Momentum trading: ride strong trends in preferred sectors; enter on breakout confirmation
- Mean reversion: buy oversold dips (RSI < 35) in fundamentally sound companies
- Volume-confirmed breakouts above EMA-9 and EMA-21 with volume ≥ 1.5x average
- Sector bias: when signals are borderline, FAVOUR AI_CHIPS, AI_SOFTWARE, GREEN_ENERGY,
  MEDTECH, BIOTECH, and DRONE over GENERAL — these sectors have structural tailwinds
- Short-term cycling: target 5-15% quick moves; do not anchor to long-term thesis

Portfolio risk rules — hard constraints, not suggestions:
- Max position size: {max_pos_pct*100:.0f}% of total portfolio (suggested_position_pct ≤ {max_pos_pct:.2f})
- Standard stop loss: {stop_pct*100:.0f}% below entry (suggested_stop_loss_pct ≈ {stop_pct:.2f})
- Standard take profit: {tp_pct*100:.0f}% above entry (suggested_take_profit_pct ≈ {tp_pct:.2f})
- BIOTECH exception: use stop_loss {stop_pct * 1.6:.2f} if FDA/PDUFA event within 7 days (binary risk)
- Maximum {max_pos} simultaneous open positions; do not suggest BUY if portfolio is at capacity
- Never suggest position_pct > {max_pos_pct:.2f} regardless of confidence
- Only BUY if buying power > ${cash_reserve:.0f} (minimum cash reserve enforced externally)
- To add to an existing position, set action=BUY; the risk manager caps the add-on at remaining headroom shown in EXISTING POSITION; do NOT suggest BUY if headroom is $0

Technical indicator interpretation (use these thresholds when reading the MARKET SNAPSHOT):
- RSI: < 30 = oversold/strong BUY candidate; 30-45 = weak/recovering; 50-70 = bullish momentum; > 70 = overbought/SELL candidate
- EMA-9 vs EMA-21: EMA-9 > EMA-21 = short-term uptrend; EMA-9 < EMA-21 = downtrend
- VWAP: price above VWAP = bullish intraday bias; price below VWAP = bearish intraday bias
- Volume ratio vs average: > 1.5x confirms signal; 1.0-1.5x neutral; < 1.0x weakens signal
- EMA-50: primary trend direction; price above EMA-50 = macro uptrend (buy bias)

BUY signal patterns:
- DIP: RSI < 35 + price at/near VWAP support → mean reversion BUY (urgency: MEDIUM/HIGH)
- WAVE: RSI 50-65, price above EMA-9/EMA-21, volume 1.5x+ → momentum continuation BUY
- BREAKOUT: price crossed above EMA-9 and EMA-21 together, volume 2x+ → strong BUY (HIGH urgency)
- VWAP_RECLAIM: price just crossed above VWAP after being below → intraday reversal BUY

SELL signal patterns:
- RSI > 70 + declining volume = momentum exhaustion → SELL (take profit)
- Price drops below EMA-21 with volume = trend reversal → protective SELL
- Position at +15% gain with no exceptional momentum → take-profit SELL
- Position at -5% from entry → stop-loss SELL

HOLD criteria:
- RSI 45-55, price near VWAP, volume near average AND no research support = no clear direction → HOLD
- Conflicting signals: favour the signal aligned with EMA-50 trend direction; HOLD only if BOTH trend AND momentum are ambiguous
- Research conviction < 0.50 AND weak technicals (RSI 45-55, below VWAP) → HOLD

Research signal context (if a research signal is provided):
- conviction ≥ 0.70 + BULLISH: strong fundamental support — weight technical BUY signals more heavily; lower your confidence threshold slightly
- conviction ≥ 0.70 + BEARISH: caution — reduce BUY confidence by 0.05-0.10, or prefer HOLD/SELL
- conviction 0.50-0.69: moderate signal — let technicals be the primary driver of the decision
- conviction < 0.50: weak signal — rely entirely on technicals; ignore research direction

BIOTECH sector special handling (MANE, RXRX, BEAM, CRSP, NTLA):
- Clinical-stage biotech carries binary FDA/PDUFA event risk — treat with extra caution
- If earnings context shows a readout within 7 days, set suggested_stop_loss_pct to 0.08
- Gene-editing stocks (BEAM, CRSP, NTLA) should use HOLD unless RSI confirmation is strong and volume confirms
- A successful Phase 3 or FDA approval in the news = immediate HIGH urgency BUY signal
- A Phase 3 failure or FDA rejection = immediate HIGH urgency SELL signal regardless of RSI

Scanner discovery handling (symbols not in the sector classification list above):
- These are intraday momentum discoveries from unusual price/volume activity
- Require volume > 2x average AND RSI > 45 to recommend BUY; do not buy on low volume
- Default sector: GENERAL; set to the correct sector if news context identifies it clearly
- Use lower position size: suggested_position_pct = 0.01 (half the standard allocation)
- Do not hold scanner discoveries overnight if no research conviction signal is available

Earnings and catalyst context:
- Upcoming earnings within 48h: reduce suggested_position_pct by 50%; note the risk in rationale
- Post-earnings gap-up with volume > 2x: may warrant BUY on momentum, but note the post-gap risk
- FDA/clinical readout within 7 days: treat as binary event; set stop_loss to 0.08 or recommend HOLD

You will receive a market snapshot per symbol and current portfolio context.
Respond ONLY with a valid JSON object — no explanation, no markdown, no preamble.

JSON format:
{{
  "symbol": "NVDA",
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "rationale": "One concise sentence explaining the decision",
  "sector": "AI_CHIPS" | "AI_SOFTWARE" | "GREEN_ENERGY" | "MEDTECH" | "BIOTECH" | "DRONE" | "GENERAL",
  "suggested_position_pct": 0.0-{max_pos_pct:.2f},
  "suggested_stop_loss_pct": 0.01-0.10,
  "suggested_take_profit_pct": 0.01-{tp_pct + 0.05:.2f},
  "urgency": "LOW" | "MEDIUM" | "HIGH"
}}

Decision rules:
- confidence reflects genuine signal strength; never inflate above 0.9
- for preferred sectors (AI_CHIPS through DRONE), apply up to +0.05 confidence boost
- only return BUY/SELL if confidence ≥ {min_confidence:.2f}; return HOLD if signals are mixed or unclear
- urgency HIGH: strong immediate signal (breakout with volume, sharp VWAP reclaim)
- urgency MEDIUM: clear directional signal, no immediate time pressure
- urgency LOW: speculative or weakly confirmed signal; consider HOLD instead
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

    def __init__(self, config, risk_config=None):
        self.client = anthropic.Anthropic(api_key=config.api_key)
        self.model = config.model
        self.max_tokens = config.max_tokens
        # Build system prompt from live risk config so values stay in sync with config.py
        if risk_config is None:
            import config as _cfg
            risk_config = _cfg.risk
        import config as _cfg
        min_conf = getattr(_cfg.agent, "min_confidence", 0.55)
        self._system_prompt = _build_system_prompt(risk_config, min_confidence=min_conf)
        self._max_position_pct = risk_config.max_position_pct

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
                system=[{"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}}],
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
            equity = portfolio_context.get("equity", 0)
            market_value = float(existing_position.get("market_value", 0))
            headroom = max(0.0, equity * self._max_position_pct - market_value)
            lines += [
                "",
                "=== EXISTING POSITION ===",
                f"Qty: {existing_position.get('qty', 0)}",
                f"Avg entry: ${float(existing_position.get('avg_entry_price', 0)):.2f}",
                f"Current value: ${market_value:,.2f}",
                f"Unrealised P&L: {float(existing_position.get('unrealized_plpc', 0)) * 100:.2f}%",
                f"Headroom to add: ${headroom:,.2f} (max_position_pct cap — set action=BUY to add; $0 means at cap)",
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

        # Inject research signal — the fundamental context Claude needs for entry decisions
        if research_signal:
            conv = research_signal.get("conviction", 0)
            sentiment = research_signal.get("sentiment", "NEUTRAL")
            action = research_signal.get("recommended_action", "HOLD")
            sig_type = research_signal.get("signal_type", "FUNDAMENTAL")
            summary = research_signal.get("summary", "")
            key_points = research_signal.get("key_points") or []
            risk_factors = research_signal.get("risk_factors") or []
            lines += [
                "",
                "=== RESEARCH SIGNAL ===",
                f"Conviction: {conv:.0%}  |  Sentiment: {sentiment}  |  Recommended: {action}  |  Type: {sig_type}",
            ]
            if summary:
                lines.append(f"Summary: {summary}")
            if key_points:
                lines.append("Key points:")
                for pt in key_points[:3]:
                    lines.append(f"  • {pt}")
            if risk_factors:
                lines.append(f"Risk: {risk_factors[0]}")

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

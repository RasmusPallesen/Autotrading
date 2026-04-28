"""
Research analyst: sends collected data to Claude for sentiment scoring
and conviction rating per symbol.
Includes disk-backed analysis cache to avoid re-analysing unchanged filings.
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import List, Optional

import anthropic

logger = logging.getLogger(__name__)

# ── Simple disk cache ──────────────────────────────────────────────────────────
# Uses current working directory (project root when launched from start_research.bat)

def _cache_path() -> str:
    cwd = os.getcwd()
    logs_dir = os.path.join(cwd, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    return os.path.join(logs_dir, "analysis_cache.json")


def _load_cache() -> dict:
    p = _cache_path()
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info("Analysis cache loaded: %d entries from %s", len(data), p)
            return data
    except Exception as e:
        logger.warning("Could not load analysis cache: %s", e)
    return {}


def _save_cache(cache: dict):
    p = _cache_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=None)
        logger.debug("Analysis cache saved: %d entries", len(cache))
    except Exception as e:
        logger.error("Could not save analysis cache to %s: %s", p, e)


def _make_key(symbol: str, items) -> str:
    titles = sorted(item.title for item in items if item.title)
    raw = symbol + "|" + "|".join(titles[:10])
    return hashlib.md5(raw.encode()).hexdigest()


# Global cache — loaded once when first analyse_all() is called
_CACHE: dict = {}
_CACHE_LOADED = False

# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a professional financial research analyst. You will receive a batch of
raw research items (news, SEC filings, Reddit posts) about a stock symbol and must produce
a structured investment research summary.

Respond ONLY with valid JSON — no markdown, no preamble.

JSON format:
{
  "symbol": "AAPL",
  "overall_sentiment": "BULLISH" | "BEARISH" | "NEUTRAL",
  "conviction": 0.0-1.0,
  "summary": "2-3 sentence summary of the key findings",
  "key_points": ["point 1", "point 2", "point 3"],
  "risk_factors": ["risk 1", "risk 2"],
  "recommended_action": "BUY" | "SELL" | "HOLD" | "WATCH",
  "sources_used": 0,
  "confidence_explanation": "One sentence explaining why conviction is this level"
}

Rules:
- conviction above 0.75 only for very strong, multi-source confirmation
- recommended_action must be consistent with overall_sentiment
- key_points must be specific and fact-based, not generic
- always note if information is speculative (Reddit) vs official (SEC)
"""


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ResearchReport:
    symbol: str
    overall_sentiment: str
    conviction: float
    summary: str
    key_points: List[str]
    risk_factors: List[str]
    recommended_action: str
    sources_used: int
    confidence_explanation: str

    def is_high_conviction(self, threshold: float = 0.70) -> bool:
        return self.conviction >= threshold

    def to_email_html(self) -> str:
        sentiment_color = {
            "BULLISH": "#22c55e",
            "BEARISH": "#ef4444",
            "NEUTRAL": "#6b7280",
        }.get(self.overall_sentiment, "#6b7280")

        action_color = {
            "BUY": "#22c55e",
            "SELL": "#ef4444",
            "HOLD": "#6b7280",
            "WATCH": "#f59e0b",
        }.get(self.recommended_action, "#6b7280")

        key_points_html = "".join(f"<li>{p}</li>" for p in self.key_points)
        risk_html = "".join(f"<li>{r}</li>" for r in self.risk_factors)

        return f"""
        <div style="border:1px solid #e5e7eb;border-radius:8px;padding:20px;margin:16px 0;font-family:sans-serif;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <h2 style="margin:0;font-size:20px;">{self.symbol}</h2>
                <div>
                    <span style="background:{sentiment_color};color:white;padding:4px 10px;border-radius:4px;font-size:13px;font-weight:600;margin-right:8px;">
                        {self.overall_sentiment}
                    </span>
                    <span style="background:{action_color};color:white;padding:4px 10px;border-radius:4px;font-size:13px;font-weight:600;">
                        {self.recommended_action}
                    </span>
                </div>
            </div>
            <div style="background:#f9fafb;border-radius:6px;padding:12px;margin-bottom:12px;">
                <p style="margin:0;font-size:14px;color:#374151;">{self.summary}</p>
            </div>
            <div style="margin-bottom:12px;">
                <p style="font-size:13px;font-weight:600;color:#111827;margin:0 0 6px;">Conviction: {self.conviction*100:.0f}% &mdash; {self.confidence_explanation}</p>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
                <div>
                    <p style="font-size:12px;font-weight:600;color:#6b7280;margin:0 0 4px;text-transform:uppercase;">Key findings</p>
                    <ul style="margin:0;padding-left:16px;font-size:13px;color:#374151;">{key_points_html}</ul>
                </div>
                <div>
                    <p style="font-size:12px;font-weight:600;color:#6b7280;margin:0 0 4px;text-transform:uppercase;">Risk factors</p>
                    <ul style="margin:0;padding-left:16px;font-size:13px;color:#ef4444;">{risk_html}</ul>
                </div>
            </div>
            <p style="font-size:11px;color:#9ca3af;margin:12px 0 0;">Based on {self.sources_used} sources</p>
        </div>
        """


# ── Analyst class ──────────────────────────────────────────────────────────────

class ResearchAnalyst:

    def __init__(self, config):
        self.client = anthropic.Anthropic(api_key=config.api_key)
        self.model = config.model

    def analyse(self, symbol: str, items) -> ResearchReport:
        global _CACHE, _CACHE_LOADED

        if not items:
            return self._empty(symbol)

        # Check cache
        key = _make_key(symbol, items)
        if key in _CACHE:
            cached = _CACHE[key]
            logger.info(
                "[%s] CACHE HIT -- skipping Claude (conviction=%.0f%%, %d cached)",
                symbol, cached.get("conviction", 0) * 100, len(_CACHE),
            )
            return ResearchReport(
                symbol=symbol,
                overall_sentiment=cached.get("overall_sentiment", "NEUTRAL"),
                conviction=float(cached.get("conviction", 0.0)),
                summary=cached.get("summary", ""),
                key_points=cached.get("key_points", []),
                risk_factors=cached.get("risk_factors", []),
                recommended_action=cached.get("recommended_action", "HOLD"),
                sources_used=cached.get("sources_used", 0),
                confidence_explanation=cached.get("confidence_explanation", ""),
            )

        # Cache miss — call Claude
        logger.info("[%s] Cache miss -- calling Claude", symbol)

        context_lines = [f"Research items for {symbol} ({len(items)} total):\n"]
        for i, item in enumerate(items[:25], 1):
            context_lines.append(
                f"{i}. [{item.source.upper()}] {item.title}\n"
                f"   {item.summary[:800]}\n"
                f"   URL: {item.url}\n"
            )
        user_prompt = "\n".join(context_lines) + "\nProvide your research report as JSON only."

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1200,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = response.content[0].text.strip()
            clean = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(clean)

            report = ResearchReport(
                symbol=data.get("symbol", symbol),
                overall_sentiment=data.get("overall_sentiment", "NEUTRAL"),
                conviction=float(data.get("conviction", 0.0)),
                summary=data.get("summary", ""),
                key_points=data.get("key_points", []),
                risk_factors=data.get("risk_factors", []),
                recommended_action=data.get("recommended_action", "HOLD"),
                sources_used=data.get("sources_used", len(items)),
                confidence_explanation=data.get("confidence_explanation", ""),
            )

            # Save to cache
            _CACHE[key] = {
                "overall_sentiment": report.overall_sentiment,
                "conviction": report.conviction,
                "summary": report.summary,
                "key_points": report.key_points,
                "risk_factors": report.risk_factors,
                "recommended_action": report.recommended_action,
                "sources_used": report.sources_used,
                "confidence_explanation": report.confidence_explanation,
            }
            if len(_CACHE) > 300:
                oldest = next(iter(_CACHE))
                del _CACHE[oldest]
            _save_cache(_CACHE)

            return report

        except Exception as e:
            logger.error("Research analysis error for %s: %s", symbol, e)
            return self._empty(symbol, str(e))

    def analyse_all(self, items, symbols: List[str]) -> List[ResearchReport]:
        global _CACHE, _CACHE_LOADED

        # Load cache on first call
        if not _CACHE_LOADED:
            _CACHE = _load_cache()
            _CACHE_LOADED = True

        by_symbol = {s: [] for s in symbols}
        for item in items:
            if item.symbol in by_symbol:
                by_symbol[item.symbol].append(item)

        reports = []
        for symbol, symbol_items in by_symbol.items():
            if symbol_items:
                logger.info("Analysing %d items for %s", len(symbol_items), symbol)
                reports.append(self.analyse(symbol, symbol_items))

        return reports

    def _empty(self, symbol: str, reason: str = "No data") -> ResearchReport:
        return ResearchReport(
            symbol=symbol, overall_sentiment="NEUTRAL", conviction=0.0,
            summary=reason, key_points=[], risk_factors=[],
            recommended_action="HOLD", sources_used=0,
            confidence_explanation="Insufficient data.",
        )

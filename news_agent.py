"""
News Sentiment Agent — real-time news analysis via Claude AI.

Subscribes to Alpaca's NewsDataStream WebSocket ('*' = all US market news, 24/7).
Each article is keyword-scored for free; only high-scoring articles trigger a
Claude call. Results are written to research_signals so the trading agent and
dashboard pick them up automatically.

Run alongside main.py and research_agent.py:
    python news_agent.py

Environment variables required:
    ALPACA_API_KEY, ALPACA_SECRET_KEY   — Alpaca trading credentials
    ANTHROPIC_API_KEY                   — Claude API key
    DATABASE_URL (optional)             — PostgreSQL URL; falls back to SQLite
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

import anthropic

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.news_scanner import _score_text
from storage.research_store import ResearchStore
from storage.trade_store import TradeStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.getcwd(), "logs", "news_agent.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("news_agent")

# ── Config ────────────────────────────────────────────────────────────────────

MODEL            = "claude-haiku-4-5-20251001"
MAX_TOKENS       = 256
MIN_KEYWORD_SCORE = 2    # articles below this threshold skip Claude entirely
MAX_TICKERS_PER_ARTICLE = 5  # cap fan-out for broad market articles

SYSTEM_PROMPT = """\
You are a financial news sentiment analyser for a short-term momentum trading system.
Given a single news article headline and summary, output JSON only (no markdown, no prose):

{
  "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL",
  "conviction": <float 0.0–1.0>,
  "signal_type": "EARNINGS_SURPRISE" | "FDA_EVENT" | "ANALYST_ACTION" | "PARTNERSHIP" | "LEGAL_RISK" | "GUIDANCE_CHANGE" | "GENERAL_NEWS",
  "summary": "<one sentence: what happened and why it matters for the stock price>",
  "key_points": ["<specific fact 1>", "<specific fact 2>"]
}

Conviction guide:
  0.85–1.00  Strong catalyst — 5%+ single-day move very likely
  0.70–0.84  Clear signal — 3–5% move probable
  0.50–0.69  Moderate signal — 2–4% move possible
  0.30–0.49  Weak / ambiguous signal
  0.00–0.29  Minimal market impact expected

Rules:
- sentiment must match conviction direction (BULLISH = positive for stock price)
- key_points must contain specific numbers, names, or dates — never vague
- Output JSON only. No explanation outside the JSON object.\
"""


# ── Market-hours TTL helper ───────────────────────────────────────────────────

_ET = ZoneInfo("America/New_York")

def _signal_ttl() -> int:
    """Return 2h during US market hours, 8h overnight so signals survive to open."""
    now_et = datetime.now(_ET)
    is_market = (
        now_et.weekday() < 5
        and dtime(9, 30) <= now_et.time() <= dtime(16, 0)
    )
    return 2 if is_market else 8


# ── Claude sentiment call ─────────────────────────────────────────────────────

def _analyse_article(
    client: anthropic.Anthropic,
    headline: str,
    summary: str,
    symbols: list,
    source: str,
    published_at: str,
    trade_store: TradeStore,
) -> dict | None:
    """Call Claude to analyse a single news article. Returns parsed dict or None."""
    symbol_str = ", ".join(symbols[:5]) if symbols else "unknown"
    user_msg = (
        f"Symbols: {symbol_str}\n"
        f"Headline: {headline}\n"
        f"Summary: {summary or headline}\n"
        f"Source: {source}\n"
        f"Published: {published_at}"
    )
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()
        clean = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(clean)

        if trade_store:
            try:
                trade_store.log_api_usage("news_agent", MODEL, response.usage)
            except Exception:
                pass

        return result
    except json.JSONDecodeError:
        logger.warning("[NEWS] Claude returned non-JSON: %s", raw[:120])
        return None
    except Exception as e:
        logger.warning("[NEWS] Claude call error: %s", e)
        return None


# ── News callback ─────────────────────────────────────────────────────────────

def _make_news_handler(
    client: anthropic.Anthropic,
    research_store: ResearchStore,
    trade_store: TradeStore,
    seen_ids: set,
):
    """Return the async callback that fires for each incoming news article."""

    async def _on_news(article) -> None:
        try:
            article_id = str(getattr(article, "id", "") or "")
            if article_id and article_id in seen_ids:
                return
            if article_id:
                seen_ids.add(article_id)
                if len(seen_ids) > 8000:
                    # Evict oldest quarter to bound memory
                    to_remove = list(seen_ids)[:2000]
                    for uid in to_remove:
                        seen_ids.discard(uid)

            headline    = str(getattr(article, "headline", "") or "")
            summary     = str(getattr(article, "summary",  "") or "")
            source      = str(getattr(article, "source",   "") or "")
            symbols     = list(getattr(article, "symbols",  []) or [])
            url         = str(getattr(article, "url",       "") or "")
            created_at  = getattr(article, "created_at", None)
            pub_str     = created_at.isoformat() if created_at else datetime.now(timezone.utc).isoformat()

            # Skip articles with no ticker symbols (pure macro / editorial)
            if not symbols:
                return

            # Keyword pre-filter — free, fast, cuts ~75% of volume
            text  = f"{headline} {summary}"
            score = _score_text(text)
            if abs(score) < MIN_KEYWORD_SCORE:
                logger.debug("[NEWS] Skipped (score=%+d): %s", score, headline[:80])
                return

            logger.info(
                "[NEWS] Keyword score=%+d  symbols=%s  headline=%s",
                score, symbols[:5], headline[:100],
            )

            # Claude sentiment analysis (one call per article, not per ticker)
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                _analyse_article,
                client, headline, summary, symbols, source, pub_str, trade_store,
            )
            if result is None:
                return

            sentiment   = result.get("sentiment", "NEUTRAL")
            conviction  = float(result.get("conviction", 0.0))
            signal_type = result.get("signal_type", "GENERAL_NEWS")
            ai_summary  = result.get("summary", headline)
            key_points  = result.get("key_points", [headline])
            if url:
                key_points = list(key_points) + [f"Article: {url}"]
            ttl         = _signal_ttl()

            if conviction >= 0.70:
                rec_action = "BUY" if sentiment == "BULLISH" else "SELL" if sentiment == "BEARISH" else "WATCH"
            else:
                rec_action = "WATCH"

            # Write one signal per ticker (capped at MAX_TICKERS_PER_ARTICLE)
            written = []
            for sym in symbols[:MAX_TICKERS_PER_ARTICLE]:
                try:
                    research_store.write_signal(
                        symbol=sym,
                        sentiment=sentiment,
                        conviction=conviction,
                        recommended_action=rec_action,
                        summary=f"[NEWS] {ai_summary}",
                        key_points=key_points,
                        risk_factors=["Real-time news signal — verify before acting"],
                        sources_used=1,
                        ttl_hours=ttl,
                        signal_type="NEWS_SENTIMENT",
                    )
                    written.append(sym)
                except Exception as we:
                    logger.debug("[NEWS] DB write error %s: %s", sym, we)

            logger.info(
                "[NEWS] %s  conv=%.0f%%  %s  signal_type=%s  ttl=%dh  tickers=%s",
                sentiment, conviction * 100, rec_action, signal_type, ttl, written,
            )

        except Exception as e:
            logger.warning("[NEWS] Callback error: %s", e)

    return _on_news


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    os.makedirs(os.path.join(os.getcwd(), "logs"), exist_ok=True)

    alpaca_key    = os.getenv("ALPACA_API_KEY", "")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not alpaca_key or not alpaca_secret:
        logger.error("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        sys.exit(1)
    if not anthropic_key:
        logger.error("ANTHROPIC_API_KEY is required")
        sys.exit(1)

    logger.info("News Sentiment Agent starting")
    logger.info("  Model:           %s", MODEL)
    logger.info("  Min keyword score: %d", MIN_KEYWORD_SCORE)
    logger.info("  Max tickers/article: %d", MAX_TICKERS_PER_ARTICLE)
    logger.info("  Subscription:    * (all US market news, 24/7)")

    client         = anthropic.Anthropic(api_key=anthropic_key)
    research_store = ResearchStore()
    trade_store    = TradeStore()
    seen_ids: set  = set()

    running = True

    def _shutdown(sig, frame):
        nonlocal running
        logger.warning("Shutdown signal received — stopping.")
        running = False

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    handler = _make_news_handler(client, research_store, trade_store, seen_ids)

    reconnect_delay = 10
    while running:
        try:
            from alpaca.data.live import NewsDataStream
            stream = NewsDataStream(
                api_key=alpaca_key,
                secret_key=alpaca_secret,
            )
            stream.subscribe_news(handler, "*")
            logger.info("News stream connected — subscribed to all symbols")
            reconnect_delay = 10  # reset backoff on successful connect
            stream.run()          # blocks until disconnect/error
        except KeyboardInterrupt:
            break
        except Exception as e:
            if not running:
                break
            logger.warning(
                "News stream error — reconnecting in %ds: %s",
                reconnect_delay, e,
            )
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 120)  # exponential backoff, max 2 min

    research_store.close()
    trade_store.close()
    logger.info("News Sentiment Agent shut down cleanly.")


if __name__ == "__main__":
    main()

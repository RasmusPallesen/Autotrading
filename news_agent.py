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
from html.parser import HTMLParser
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import anthropic
import requests

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

MODEL             = "claude-haiku-4-5-20251001"
MODEL_HIGH_STAKES = "claude-sonnet-4-6"   # escalated for score >= HIGH_STAKES_SCORE
MAX_TOKENS        = 400                    # extra headroom when body text is included
MIN_KEYWORD_SCORE = 2    # articles below this threshold skip Claude entirely
HIGH_STAKES_SCORE = 4    # articles at or above this score use the stronger model
MAX_TICKERS_PER_ARTICLE = 5  # cap fan-out for broad market articles

ARTICLE_FETCH_TIMEOUT = 8     # seconds for HTTP GET
ARTICLE_MAX_CHARS     = 2500  # truncate fetched body to this length

# Domains known to be paywalled — skip the fetch attempt entirely
_PAYWALLED_DOMAINS = frozenset([
    "wsj.com", "bloomberg.com", "ft.com", "barrons.com",
    "seekingalpha.com", "reuters.com", "thestreet.com",
    "marketwatch.com", "economist.com",
])

# HTML phrases that indicate a paywall / subscription gate
_PAYWALL_PHRASES = [
    "subscribe to read", "subscription required", "members only",
    "sign in to continue", "sign in to read", "premium content",
    "create a free account", "already a subscriber", "subscribe now",
    "unlock this article", "register to read",
]

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

SYSTEM_PROMPT = """\
You are a financial news sentiment analyser for a short-term momentum trading system.
Given a news article (headline, summary, and optionally the full body), output JSON only (no markdown, no prose):

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

When market context is provided (current price, 5-day return), use it to assess
whether the catalyst is already priced in — lower conviction if the stock has
already moved significantly in the news direction.

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


# ── Article body fetcher ──────────────────────────────────────────────────────

class _ParagraphExtractor(HTMLParser):
    """Extract visible text from <p> tags."""
    def __init__(self):
        super().__init__()
        self._in_p = False
        self.paragraphs: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self._in_p = True

    def handle_endtag(self, tag):
        if tag == "p":
            self._in_p = False
            txt = "".join(self._buf).strip()
            if txt:
                self.paragraphs.append(txt)
            self._buf = []

    def handle_data(self, data):
        if self._in_p:
            self._buf.append(data)


def _fetch_article_body(url: str) -> tuple[str | None, str]:
    """
    Attempt to fetch and extract the article body from a URL.

    Returns (body_text | None, status) where status is one of:
      "ok"               — body extracted successfully
      "no_url"           — no URL provided
      "paywalled_domain" — known paywall domain, skipped without fetching
      "paywall_detected" — paywall phrases found in response or redirect to login
      "fetch_error"      — network / timeout error
      "empty"            — fetched but too little text (teaser / gated)
    """
    if not url:
        return None, "no_url"

    domain = urlparse(url).netloc.lower().lstrip("www.")
    if any(pw in domain for pw in _PAYWALLED_DOMAINS):
        return None, "paywalled_domain"

    try:
        resp = requests.get(
            url, headers=_FETCH_HEADERS,
            timeout=ARTICLE_FETCH_TIMEOUT,
            allow_redirects=True,
        )
    except Exception as e:
        logger.debug("[NEWS] Article fetch error (%s): %s", url, e)
        return None, "fetch_error"

    # Redirect to login / subscribe page
    final_url = resp.url
    if any(kw in final_url for kw in ("/login", "/subscribe", "/signin", "/register")):
        return None, "paywall_detected"

    html_lower = resp.text.lower()
    if any(phrase in html_lower for phrase in _PAYWALL_PHRASES):
        return None, "paywall_detected"

    parser = _ParagraphExtractor()
    try:
        parser.feed(resp.text)
    except Exception:
        return None, "fetch_error"

    body = " ".join(parser.paragraphs)
    if len(body) < 150:
        return None, "empty"

    return body[:ARTICLE_MAX_CHARS], "ok"


# ── Market context helper ─────────────────────────────────────────────────────

def _get_market_context(data_fetcher, symbol: str) -> tuple[float | None, float | None]:
    """
    Return (current_price, pct_5d_return). Either value may be None on failure.
    pct_5d is the percentage change over the last 5 daily bars.
    """
    if data_fetcher is None:
        return None, None

    price = None
    try:
        price = data_fetcher.get_latest_price(symbol)
    except Exception:
        pass

    pct_5d = None
    try:
        bars = data_fetcher.get_bars([symbol], lookback_bars=6, timeframe="1Day")
        df = bars.get(symbol)
        if df is not None and len(df) >= 2:
            pct_5d = (df["close"].iloc[-1] - df["close"].iloc[0]) / df["close"].iloc[0] * 100
    except Exception:
        pass

    return price, pct_5d


# ── Claude sentiment call ─────────────────────────────────────────────────────

def _analyse_article(
    client: anthropic.Anthropic,
    headline: str,
    summary: str,
    symbols: list,
    source: str,
    published_at: str,
    trade_store: TradeStore,
    full_body: str | None = None,
    current_price: float | None = None,
    pct_5d: float | None = None,
    model: str = MODEL,
) -> dict | None:
    """Call Claude to analyse a single news article. Returns parsed dict or None."""
    symbol_str = ", ".join(symbols[:5]) if symbols else "unknown"

    parts = [
        f"Symbols: {symbol_str}",
        f"Headline: {headline}",
        f"Summary: {summary or headline}",
        f"Source: {source}",
        f"Published: {published_at}",
    ]
    if current_price is not None:
        ctx = f"Current price: ${current_price:.2f}"
        if pct_5d is not None:
            ctx += f" | 5-day return: {pct_5d:+.1f}%"
        parts.append(ctx)
    if full_body:
        parts.append(f"\nArticle body:\n{full_body}")

    user_msg = "\n".join(parts)

    try:
        response = client.messages.create(
            model=model,
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
                trade_store.log_api_usage("news_agent", model, response.usage)
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
    data_fetcher=None,
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
                    to_remove = list(seen_ids)[:2000]
                    for uid in to_remove:
                        seen_ids.discard(uid)

            headline    = str(getattr(article, "headline", "") or "")
            summary     = str(getattr(article, "summary",  "") or "")
            source      = str(getattr(article, "source",   "") or "")
            # Strip exchange prefixes (e.g. "TSX:TFPM" → "TFPM") — Alpaca only accepts plain tickers
            symbols     = [s.split(":")[-1] for s in (getattr(article, "symbols", []) or [])]
            url         = str(getattr(article, "url",       "") or "")
            created_at  = getattr(article, "created_at", None)
            pub_str     = created_at.isoformat() if created_at else datetime.now(timezone.utc).isoformat()

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

            # ── Full article fetch ────────────────────────────────────────────
            body, body_status = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_article_body, url,
            )
            if body_status in ("paywalled_domain", "paywall_detected"):
                logger.info(
                    "[NEWS] Paywalled (%s) — using headline+summary only: %s",
                    body_status, source,
                )
            elif body_status == "ok":
                logger.debug("[NEWS] Article body fetched (%d chars) from %s", len(body), source)
            elif body_status not in ("no_url",):
                logger.debug("[NEWS] Article body unavailable (%s) from %s", body_status, source)

            # ── Model escalation ──────────────────────────────────────────────
            model = MODEL_HIGH_STAKES if abs(score) >= HIGH_STAKES_SCORE else MODEL
            if model == MODEL_HIGH_STAKES:
                logger.info(
                    "[NEWS] High-stakes article (score=%+d) — using %s",
                    score, MODEL_HIGH_STAKES,
                )

            ttl = _signal_ttl()

            # ── Per-ticker: market context + Claude call ──────────────────────
            written = []
            for sym in symbols[:MAX_TICKERS_PER_ARTICLE]:
                # Market context (price + 5-day return) so Claude can assess
                # whether the catalyst is already priced in
                price, pct_5d = await asyncio.get_event_loop().run_in_executor(
                    None, _get_market_context, data_fetcher, sym,
                )

                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda s=sym, p=price, r=pct_5d: _analyse_article(
                        client, headline, summary, [s], source, pub_str, trade_store,
                        full_body=body,
                        current_price=p,
                        pct_5d=r,
                        model=model,
                    ),
                )
                if result is None:
                    continue

                sentiment   = result.get("sentiment", "NEUTRAL")
                conviction  = float(result.get("conviction", 0.0))
                signal_type = result.get("signal_type", "GENERAL_NEWS")
                ai_summary  = result.get("summary", headline)
                key_points  = list(result.get("key_points", [headline]))
                if url:
                    key_points.append(f"Article: {url}")

                if conviction >= 0.70:
                    rec_action = "BUY" if sentiment == "BULLISH" else "SELL" if sentiment == "BEARISH" else "WATCH"
                else:
                    rec_action = "WATCH"

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
                    "[NEWS] %s  conv=%.0f%%  %s  signal_type=%s  ttl=%dh  tickers=[%r]",
                    sentiment, conviction * 100, rec_action, signal_type, ttl, sym,
                )

            if written:
                logger.info("[NEWS] Signals written for: %s", written)

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
    logger.info("  Model (routine):      %s", MODEL)
    logger.info("  Model (high-stakes):  %s (score >= %d)", MODEL_HIGH_STAKES, HIGH_STAKES_SCORE)
    logger.info("  Min keyword score:    %d", MIN_KEYWORD_SCORE)
    logger.info("  Max tickers/article:  %d", MAX_TICKERS_PER_ARTICLE)
    logger.info("  Article fetch:        enabled (timeout=%ds)", ARTICLE_FETCH_TIMEOUT)
    logger.info("  Subscription:         * (all US market news, 24/7)")

    client         = anthropic.Anthropic(api_key=anthropic_key)
    research_store = ResearchStore()
    trade_store    = TradeStore()
    seen_ids: set  = set()

    # Market context fetcher — provides current price + 5-day return per ticker
    data_fetcher = None
    try:
        from data.alpaca_fetcher import AlpacaDataFetcher
        from config import config
        data_fetcher = AlpacaDataFetcher(config.alpaca)
        logger.info("  Market context:       enabled (price + 5d return per ticker)")
    except Exception as e:
        logger.warning("  Market context:       disabled (%s)", e)

    running = True

    def _shutdown(sig, frame):
        nonlocal running
        logger.warning("Shutdown signal received — stopping.")
        running = False

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    handler = _make_news_handler(
        client, research_store, trade_store, seen_ids,
        data_fetcher=data_fetcher,
    )

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
            reconnect_delay = 10
            stream.run()
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
            reconnect_delay = min(reconnect_delay * 2, 120)

    research_store.close()
    trade_store.close()
    logger.info("News Sentiment Agent shut down cleanly.")


if __name__ == "__main__":
    main()

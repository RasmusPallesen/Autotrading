"""
Pre-breakout screener.
Detects stocks showing early accumulation signatures BEFORE the price move,
so the trading agent is primed and waiting when the technical breakout forms.

Runs as part of the research agent cycle. Returns ResearchItem objects
tagged [PRE-BREAKOUT] for Claude to analyse and write to research_signals.

Four signal types detected:

1. ACCUMULATION  — Volume building without price move (institutional buying)
   Volume 1.5-2x average but price flat or down <1%. Classic quiet accumulation.

2. RSI_TURN      — RSI climbing from oversold (catching the turn, not the trend)
   RSI crossed upward through 35 in last 3 bars. Early mean-reversion entry.

3. BB_SQUEEZE    — Bollinger Band width at 20-bar low (breakout imminent)
   Band width compressed to tightest in lookback period. Direction unknown
   but volatility expansion is coming. Combined with volume for direction bias.

4. LOW_INSIDER   — Near 52-week low with recent insider buying
   Price within 15% of 52-week low. Insider buy detected in research signals.
   One of the most reliable early signals available from free data.

Each signal is scored 0-3 (one point per confirming signal type).
Only symbols scoring >= MIN_SCORE are returned as ResearchItems.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict

import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)

# Minimum number of signal types to trigger a ResearchItem
MIN_SCORE = int(os.getenv("BREAKOUT_MIN_SCORE", "1"))

# Thresholds — all configurable via env vars
ACCUM_VOLUME_RATIO   = float(os.getenv("BREAKOUT_ACCUM_VOLUME", "1.5"))   # Volume x average
ACCUM_MAX_PRICE_MOVE = float(os.getenv("BREAKOUT_ACCUM_PRICE", "1.0"))    # Max % price move
RSI_TURN_THRESHOLD   = float(os.getenv("BREAKOUT_RSI_TURN", "35.0"))      # RSI level to cross
BB_SQUEEZE_LOOKBACK  = int(os.getenv("BREAKOUT_BB_LOOKBACK", "20"))       # Bars for squeeze
LOW_PROXIMITY_PCT    = float(os.getenv("BREAKOUT_LOW_PROXIMITY", "15.0")) # % above 52w low


@dataclass
class BreakoutSignal:
    symbol: str
    score: int                          # 0-4: number of signal types firing
    signals: List[str]                  # Which signal types detected
    current_price: float
    volume_ratio: Optional[float]
    rsi: Optional[float]
    bb_width_pct: Optional[float]       # BB width as % of price (squeeze metric)
    price_change_1h: Optional[float]
    price_change_1d: Optional[float]
    near_52w_low: bool = False
    has_insider_signal: bool = False
    details: Dict[str, str] = field(default_factory=dict)

    def to_research_summary(self) -> str:
        """Format for injection into Claude research analysis."""
        signal_str = " + ".join(self.signals)
        lines = [
            f"PRE-BREAKOUT SETUP DETECTED: {self.symbol} ({signal_str})",
            f"Score: {self.score}/4 — {'High' if self.score >= 3 else 'Medium' if self.score >= 2 else 'Low'} conviction setup",
            f"Current price: ${self.current_price:.2f}",
        ]

        if self.volume_ratio:
            lines.append(
                f"Volume: {self.volume_ratio:.1f}x average "
                f"({'significant accumulation' if self.volume_ratio >= 2.0 else 'elevated'})"
            )

        if self.rsi:
            lines.append(
                f"RSI: {self.rsi:.1f} "
                f"({'turning from oversold — early mean reversion signal' if self.rsi < 40 else 'neutral'})"
            )

        if self.bb_width_pct:
            lines.append(
                f"Bollinger Band width: {self.bb_width_pct:.2f}% of price "
                f"({'squeeze — breakout imminent' if self.bb_width_pct < 3.0 else 'normal'})"
            )

        if self.price_change_1h is not None:
            lines.append(f"1h price change: {self.price_change_1h:+.2f}%")

        if self.price_change_1d is not None:
            lines.append(f"1d price change: {self.price_change_1d:+.2f}%")

        if self.near_52w_low:
            lines.append(
                "NEAR 52-WEEK LOW: Price within 15% of yearly low. "
                "If fundamentals are intact, deep value setup."
            )

        if self.has_insider_signal:
            lines.append(
                "INSIDER BUY DETECTED: Recent Form 4 filing shows insider accumulation. "
                "Combined with technical setup — high conviction early signal."
            )

        # Signal-specific guidance
        if "ACCUMULATION" in self.signals:
            lines.append(
                "ACCUMULATION pattern: Volume building without price move suggests "
                "institutional buyers absorbing sell pressure quietly. "
                "Watch for volume continuation — breakout typically follows within 1-5 days."
            )
        if "RSI_TURN" in self.signals:
            lines.append(
                "RSI TURN: RSI crossing upward through oversold zone. "
                "Catching the turn rather than the trend — higher reward/risk than "
                "momentum entry but requires confirmation from volume."
            )
        if "BB_SQUEEZE" in self.signals:
            lines.append(
                "BOLLINGER BAND SQUEEZE: Volatility compressed to multi-week low. "
                "Breakout expected soon — volume direction will indicate which way. "
                "Set alerts rather than entering before direction confirmed."
            )
        if "LOW_INSIDER" in self.signals:
            lines.append(
                "52-WEEK LOW + INSIDER BUY: One of the most reliable free-data signals. "
                "Insiders rarely buy near lows unless they see fundamental value. "
                "Risk: catching a falling knife — wait for RSI stabilisation before entry."
            )

        return "\n".join(lines)


class BreakoutScreener:
    """
    Screens all watchlist symbols for pre-breakout accumulation patterns.
    Designed to run inside the research agent cycle.
    Reuses bar data already fetched by the trading agent via Alpaca.
    """

    def __init__(self):
        # Cache daily bars to avoid re-fetching within same cycle
        self._daily_cache: Dict[str, pd.DataFrame] = {}
        self._daily_cache_ts: Optional[datetime] = None

    def scan(
        self,
        symbols: List[str],
        bars_1min: Dict[str, pd.DataFrame],  # Already fetched 1-min bars
        research_signals: Dict[str, dict] = None,  # Active research signals from DB
        alpaca_config=None,                  # For fetching daily bars
    ) -> List[BreakoutSignal]:
        """
        Scan symbols for pre-breakout setups.

        Args:
            symbols: Watchlist symbols to screen
            bars_1min: Dict of symbol -> 1-min OHLCV DataFrame (from alpaca_fetcher)
            research_signals: Active signals from Supabase (for insider detection)
            alpaca_config: Alpaca config for fetching daily bars (52w low)

        Returns:
            List of BreakoutSignal objects, sorted by score descending
        """
        # Fetch daily bars once per cycle for 52-week context
        daily_bars = self._get_daily_bars(symbols, alpaca_config)

        results = []
        for symbol in symbols:
            df = bars_1min.get(symbol)
            if df is None or len(df) < 25:
                continue

            try:
                signal = self._analyse(
                    symbol=symbol,
                    df_1min=df,
                    df_daily=daily_bars.get(symbol),
                    research_signals=research_signals or {},
                )
                if signal and signal.score >= MIN_SCORE:
                    results.append(signal)
            except Exception as e:
                logger.debug("[%s] Breakout screen error: %s", symbol, e)

        results.sort(key=lambda s: s.score, reverse=True)

        if results:
            logger.info(
                "Breakout screener: %d setups found across %d symbols",
                len(results), len(symbols),
            )
            for s in results:
                logger.info(
                    "[%s] Score=%d signals=%s vol=%.1fx rsi=%.1f",
                    s.symbol, s.score, s.signals,
                    s.volume_ratio or 0, s.rsi or 0,
                )
        else:
            logger.debug("Breakout screener: no setups found this cycle")

        return results

    def _analyse(
        self,
        symbol: str,
        df_1min: pd.DataFrame,
        df_daily: Optional[pd.DataFrame],
        research_signals: dict,
    ) -> Optional[BreakoutSignal]:
        """Compute all signal types for a single symbol."""
        close  = df_1min["close"]
        volume = df_1min["volume"]
        current_price = float(close.iloc[-1])

        # ── Base indicators ───────────────────────────────────────────────────
        rsi_series = ta.rsi(close, length=14)
        rsi = float(rsi_series.iloc[-1]) if rsi_series is not None and not rsi_series.empty else None

        vol_sma = ta.sma(volume, length=20)
        vol_sma_val = float(vol_sma.iloc[-1]) if vol_sma is not None and not vol_sma.empty else None
        vol_latest  = float(volume.iloc[-1])
        vol_ratio   = (vol_latest / vol_sma_val) if vol_sma_val and vol_sma_val > 0 else None

        bb = ta.bbands(close, length=20, std=2)
        bb_upper = bb_lower = bb_width_pct = None
        if bb is not None:
            u_cols = [c for c in bb.columns if c.startswith("BBU")]
            l_cols = [c for c in bb.columns if c.startswith("BBL")]
            if u_cols and l_cols:
                bb_upper = float(bb[u_cols[0]].iloc[-1])
                bb_lower = float(bb[l_cols[0]].iloc[-1])
                if current_price > 0:
                    bb_width_pct = ((bb_upper - bb_lower) / current_price) * 100

        # Price changes
        lookback_1h = min(60, len(df_1min) - 1)
        price_1h_ago = float(close.iloc[-(lookback_1h + 1)])
        price_change_1h = ((current_price - price_1h_ago) / price_1h_ago) * 100

        lookback_1d = min(390, len(df_1min) - 1)  # ~6.5h * 60min
        price_1d_ago = float(close.iloc[-(lookback_1d + 1)])
        price_change_1d = ((current_price - price_1d_ago) / price_1d_ago) * 100

        # ── Signal detection ──────────────────────────────────────────────────
        signals: List[str] = []
        details: Dict[str, str] = {}

        # 1. ACCUMULATION: Volume building, price flat
        if vol_ratio and vol_ratio >= ACCUM_VOLUME_RATIO:
            if abs(price_change_1h) <= ACCUM_MAX_PRICE_MOVE:
                signals.append("ACCUMULATION")
                details["ACCUMULATION"] = (
                    f"Volume {vol_ratio:.1f}x average with only "
                    f"{price_change_1h:+.2f}% price move in 1h"
                )

        # 2. RSI_TURN: RSI crossing upward through threshold
        if rsi_series is not None and len(rsi_series) >= 4:
            recent_rsi = rsi_series.dropna().iloc[-4:]
            if len(recent_rsi) >= 2:
                prev_rsi = float(recent_rsi.iloc[-2])
                curr_rsi = float(recent_rsi.iloc[-1])
                # RSI was below threshold and is now crossing above it
                if prev_rsi < RSI_TURN_THRESHOLD and curr_rsi >= RSI_TURN_THRESHOLD:
                    signals.append("RSI_TURN")
                    details["RSI_TURN"] = (
                        f"RSI crossed upward through {RSI_TURN_THRESHOLD:.0f}: "
                        f"{prev_rsi:.1f} → {curr_rsi:.1f}"
                    )

        # 3. BB_SQUEEZE: Band width at recent low
        if bb is not None and bb_width_pct is not None:
            u_cols = [c for c in bb.columns if c.startswith("BBU")]
            l_cols = [c for c in bb.columns if c.startswith("BBL")]
            if u_cols and l_cols:
                widths = (
                    (bb[u_cols[0]] - bb[l_cols[0]]) / close * 100
                ).dropna()
                if len(widths) >= BB_SQUEEZE_LOOKBACK:
                    recent_widths = widths.iloc[-BB_SQUEEZE_LOOKBACK:]
                    current_width = float(widths.iloc[-1])
                    min_width = float(recent_widths.min())
                    # Current width is at or near the 20-bar minimum
                    if current_width <= min_width * 1.05:
                        signals.append("BB_SQUEEZE")
                        details["BB_SQUEEZE"] = (
                            f"Band width {current_width:.2f}% — "
                            f"at {BB_SQUEEZE_LOOKBACK}-bar low ({min_width:.2f}%)"
                        )

        # 4. LOW_INSIDER: Near 52-week low + insider buying signal
        near_low = False
        has_insider = False

        if df_daily is not None and len(df_daily) >= 5:
            yearly_low = float(df_daily["low"].min())
            if yearly_low > 0:
                pct_above_low = ((current_price - yearly_low) / yearly_low) * 100
                near_low = pct_above_low <= LOW_PROXIMITY_PCT

        # Check research signals for insider activity on this symbol
        sig = research_signals.get(symbol, {})
        summary_lower = str(sig.get("summary", "")).lower()
        if "insider" in summary_lower and "buy" in summary_lower:
            has_insider = True

        if near_low and has_insider:
            signals.append("LOW_INSIDER")
            details["LOW_INSIDER"] = (
                f"Price within {LOW_PROXIMITY_PCT:.0f}% of 52-week low "
                f"with insider buying detected"
            )

        if not signals:
            return None

        return BreakoutSignal(
            symbol=symbol,
            score=len(signals),
            signals=signals,
            current_price=current_price,
            volume_ratio=vol_ratio,
            rsi=rsi,
            bb_width_pct=bb_width_pct,
            price_change_1h=price_change_1h,
            price_change_1d=price_change_1d,
            near_52w_low=near_low,
            has_insider_signal=has_insider,
            details=details,
        )

    def _get_daily_bars(
        self,
        symbols: List[str],
        alpaca_config,
    ) -> Dict[str, pd.DataFrame]:
        """
        Fetch daily bars for 52-week low calculation.
        Cached for the full research cycle — only re-fetches once per day.
        """
        now = datetime.now(timezone.utc)

        # Cache valid for 6 hours — daily bars don't change intraday
        if (
            self._daily_cache
            and self._daily_cache_ts
            and (now - self._daily_cache_ts).total_seconds() < 21600
        ):
            logger.debug("Daily bar cache HIT (%d symbols)", len(self._daily_cache))
            return self._daily_cache

        if alpaca_config is None:
            logger.debug("No Alpaca config — skipping 52-week low calculation")
            return {}

        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            client = StockHistoricalDataClient(
                alpaca_config.api_key,
                alpaca_config.secret_key,
            )

            end   = now
            start = end - timedelta(days=365)
            result = {}

            for symbol in symbols:
                try:
                    req = StockBarsRequest(
                        symbol_or_symbols=symbol,
                        timeframe=TimeFrame.Day,
                        start=start,
                        end=end,
                        feed="iex",
                    )
                    bars = client.get_stock_bars(req)
                    data = bars.data if hasattr(bars, "data") else bars
                    bar_list = data.get(symbol) if isinstance(data, dict) else data

                    if not bar_list:
                        continue

                    if hasattr(bar_list, "df"):
                        df = bar_list.df.copy()
                    else:
                        df = pd.DataFrame([{
                            "timestamp": b.timestamp,
                            "open": float(b.open),
                            "high": float(b.high),
                            "low": float(b.low),
                            "close": float(b.close),
                            "volume": float(b.volume),
                        } for b in bar_list])
                        df = df.set_index("timestamp")

                    result[symbol] = df.sort_index()

                except Exception as e:
                    logger.debug("[%s] Daily bars fetch error: %s", symbol, e)

            self._daily_cache    = result
            self._daily_cache_ts = now
            logger.info(
                "Daily bar cache refreshed: %d/%d symbols fetched",
                len(result), len(symbols),
            )
            return result

        except Exception as e:
            logger.warning("Could not fetch daily bars for 52-week low: %s", e)
            return {}

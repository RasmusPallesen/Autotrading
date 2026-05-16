"""
Intraday Monitor — 1-minute bar signal engine for momentum/dip detection.
Stage 2 of the universe discovery pipeline.

Watches a dynamic candidate list (from UniverseScanner + high-volatility
watchlist stocks) every 60 seconds during market hours.

Signal types:
  DIP       — RSI < 25 + lower BB touch + volume spike (capitulation)
  WAVE      — RSI crossing above 50 + volume confirm (momentum resuming)
  BREAKOUT  — Price above upper BB + 2x volume (confirmed breakout)
  VWAP_DIP  — Price > 1.5% below VWAP on elevated volume (mean reversion)
  VWAP_RECLAIM — Price crossing back above VWAP (momentum flip)

Writes directly to research_signals with:
  - Short TTL (1-2 hours) — intraday signals expire fast
  - Source tag "intraday_monitor" for dashboard filtering
  - Conviction scaled by signal strength and fundamental gate

Fundamental gate: conviction is boosted if an existing BULLISH research
signal exists for the symbol. A stock with no fundamental context gets
capped at 65% conviction — enough to trade but not high priority.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)

# ── Thresholds ─────────────────────────────────────────────────────────────────
RSI_DIP_THRESHOLD       = float(os.getenv("INTRADAY_RSI_DIP", "30"))        # Raised from 28 — catch dips earlier
RSI_WAVE_CROSS          = float(os.getenv("INTRADAY_RSI_WAVE", "50"))
VWAP_DIP_PCT            = float(os.getenv("INTRADAY_VWAP_DIP", "1.2"))      # Lowered from 1.5 — tighter mean reversion
VOLUME_CONFIRM_RATIO    = float(os.getenv("INTRADAY_VOLUME_RATIO", "1.5"))  # Lowered from 1.8 — less strict volume
MIN_CONVICTION          = float(os.getenv("INTRADAY_MIN_CONVICTION", "0.55")) # Lowered from 0.60 — more aggressive
FUNDAMENTAL_BOOST       = float(os.getenv("INTRADAY_FUNDAMENTAL_BOOST", "0.10"))
MAX_CONVICTION_NO_FUND  = float(os.getenv("INTRADAY_MAX_CONV_NO_FUND", "0.65"))

# High-volatility symbols always monitored every 2 minutes for short-term waves
# EXPANDED for high-velocity short-term cycling — includes all high-beta stocks
CORE_INTRADAY = [
    # Mega-cap tech (high volume, intraday moves)
    "NVDA", "AMD", "MSFT", "GOOGL", "META", "TSLA", "AAPL",
    # AI software (volatile)
    "PLTR", "AI", "SOUN", "BBAI",
    # Biotech (high beta, catalyst-driven)
    "MANE", "RXRX", "BEAM", "CRSP", "NTLA",
    # MedTech (GLP-1 momentum)
    "NVO", "LLY", "DXCM", "PODD", "TNDM",
    # Drone/defence (news-driven)
    "KTOS", "AVAV", "RCAT", "AXON",
    # Green energy (volatile)
    "ENPH", "SEDG", "FSLR", "PLUG", "CHPT",
    # Crypto proxies
    "COIN", "MSTR",
    # Semiconductors
    "ASML", "TSM", "AVGO", "QCOM", "ARM", "INTC",
]


@dataclass
class IntradaySignal:
    symbol: str
    signal_type: str        # DIP | WAVE | BREAKOUT | VWAP_DIP | VWAP_RECLAIM
    conviction: float       # 0.0 - 1.0
    price: float
    rsi: Optional[float]
    volume_ratio: Optional[float]
    vwap: Optional[float]
    bb_pct: Optional[float]
    price_change_1h: float
    has_fundamental: bool   # Whether existing research signal exists
    rationale: str
    discovered_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def recommended_action(self) -> str:
        if self.signal_type in ("DIP", "VWAP_DIP"):
            return "BUY"
        elif self.signal_type in ("WAVE", "BREAKOUT", "VWAP_RECLAIM"):
            return "BUY"
        return "WATCH"

    @property
    def sentiment(self) -> str:
        if self.conviction >= 0.70:
            return "BULLISH"
        elif self.conviction >= 0.60:
            return "BULLISH"
        return "NEUTRAL"

    @property
    def ttl_hours(self) -> int:
        """Intraday signals have short TTL — they're time-sensitive."""
        if self.signal_type in ("BREAKOUT", "WAVE"):
            return 2
        return 1  # DIP signals expire faster — mean reversion is time-critical

    def to_summary(self) -> str:
        fund_note = (
            "Fundamental research signal confirms setup — higher conviction."
            if self.has_fundamental else
            "No existing fundamental research — purely technical setup. "
            "Use smaller position size and tighter stop."
        )
        return (
            f"[INTRADAY|{self.signal_type}] {self.symbol}: {self.rationale} "
            f"Price: ${self.price:.2f} | "
            f"RSI: {self.rsi:.1f} | " if self.rsi else ""
            f"Volume: {self.volume_ratio:.1f}x avg | " if self.volume_ratio else ""
            f"1h change: {self.price_change_1h:+.2f}%. "
            f"{fund_note}"
        )

    def to_key_points(self) -> list:
        points = [f"Signal type: {self.signal_type}"]
        if self.rsi:
            points.append(f"RSI: {self.rsi:.1f}")
        if self.volume_ratio:
            points.append(f"Volume: {self.volume_ratio:.1f}x average")
        if self.vwap and self.price:
            vwap_dev = ((self.price - self.vwap) / self.vwap) * 100
            points.append(f"VWAP deviation: {vwap_dev:+.2f}%")
        if self.bb_pct is not None:
            points.append(f"Bollinger Band %B: {self.bb_pct:.2f}")
        points.append(f"1h price change: {self.price_change_1h:+.2f}%")
        return points

    def to_risk_factors(self) -> list:
        risks = []
        if not self.has_fundamental:
            risks.append("No fundamental research context — purely technical trade")
        if self.signal_type == "DIP":
            risks.append("Catching falling knife risk — confirm volume exhaustion before entry")
        if self.signal_type == "BREAKOUT":
            risks.append("Breakout failure risk — false breaks common on low broader market volume")
        risks.append("Short TTL signal — intraday only, reassess at end of day")
        return risks


class IntradayMonitor:
    """
    Monitors a dynamic symbol list on 1-minute bars.
    Detects momentum setups and writes directly to research_signals.
    """

    def __init__(self):
        # Track recently flagged symbols to avoid spamming the same signal
        # {symbol: last_signal_at}
        self._signal_cache: Dict[str, datetime] = {}
        self._signal_cooldown_minutes = int(
            os.getenv("INTRADAY_SIGNAL_COOLDOWN", "20")
        )
        # Previous RSI values for crossover detection
        self._prev_rsi: Dict[str, float] = {}

    def scan(
        self,
        symbols: List[str],
        bars_1min: Dict[str, pd.DataFrame],
        research_signals: Dict[str, dict],
    ) -> List[IntradaySignal]:
        """
        Scan symbol list for intraday setups.

        Args:
            symbols: Combined list of core intraday + universe discoveries
            bars_1min: 1-minute OHLCV bars keyed by symbol
            research_signals: Active research signals from Supabase

        Returns:
            List of IntradaySignal objects above conviction threshold
        """
        signals = []

        for symbol in symbols:
            df = bars_1min.get(symbol)
            if df is None or len(df) < 25:
                continue

            # Skip if recently flagged
            last_signal = self._signal_cache.get(symbol)
            if last_signal:
                age_min = (datetime.now(timezone.utc) - last_signal).total_seconds() / 60
                if age_min < self._signal_cooldown_minutes:
                    continue

            try:
                sig = self._analyse(symbol, df, research_signals)
                if sig and sig.conviction >= MIN_CONVICTION:
                    signals.append(sig)
                    self._signal_cache[symbol] = datetime.now(timezone.utc)
                    logger.info(
                        "[INTRADAY] %s %s conviction=%.0f%% rsi=%.1f vol=%.1fx",
                        symbol, sig.signal_type, sig.conviction * 100,
                        sig.rsi or 0, sig.volume_ratio or 0,
                    )
            except Exception as e:
                logger.debug("[%s] Intraday analysis error: %s", symbol, e)

        # Evict stale cache entries
        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=self._signal_cooldown_minutes * 3
        )
        self._signal_cache = {
            k: v for k, v in self._signal_cache.items() if v > cutoff
        }

        return signals

    def _analyse(
        self,
        symbol: str,
        df: pd.DataFrame,
        research_signals: Dict[str, dict],
    ) -> Optional[IntradaySignal]:
        """Run all signal detectors on a symbol's 1-minute bars."""
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]
        current_price = float(close.iloc[-1])

        # ── Indicators ────────────────────────────────────────────────────────
        rsi_series = ta.rsi(close, length=14)
        rsi = float(rsi_series.iloc[-1]) if rsi_series is not None and len(rsi_series) > 0 else None
        prev_rsi = self._prev_rsi.get(symbol)
        if rsi is not None:
            self._prev_rsi[symbol] = rsi

        vol_sma = ta.sma(volume, length=20)
        vol_sma_val = float(vol_sma.iloc[-1]) if vol_sma is not None and not vol_sma.empty else None
        vol_latest  = float(volume.iloc[-1])
        vol_ratio   = (vol_latest / vol_sma_val) if vol_sma_val and vol_sma_val > 0 else None

        bb = ta.bbands(close, length=20, std=2)
        bb_upper = bb_lower = bb_pct = None
        if bb is not None:
            u_cols = [c for c in bb.columns if c.startswith("BBU")]
            l_cols = [c for c in bb.columns if c.startswith("BBL")]
            p_cols = [c for c in bb.columns if c.startswith("BBP")]
            if u_cols:
                bb_upper = float(bb[u_cols[0]].iloc[-1])
            if l_cols:
                bb_lower = float(bb[l_cols[0]].iloc[-1])
            if p_cols:
                bb_pct = float(bb[p_cols[0]].iloc[-1])

        # VWAP — computed from available bars
        vwap = self._compute_vwap(df)

        # 1h price change
        lookback = min(60, len(df) - 1)
        price_1h = float(close.iloc[-(lookback + 1)])
        price_change_1h = ((current_price - price_1h) / price_1h) * 100

        # Fundamental gate
        research = research_signals.get(symbol, {})
        has_fundamental = (
            research.get("sentiment") == "BULLISH"
            and float(research.get("conviction", 0)) >= 0.55
        )

        # ── Signal detection (priority order) ─────────────────────────────────

        # 1. DIP — RSI extreme oversold + lower BB + volume spike
        if (rsi is not None and rsi < RSI_DIP_THRESHOLD
                and bb_lower is not None and current_price <= bb_lower * 1.005
                and vol_ratio is not None and vol_ratio >= VOLUME_CONFIRM_RATIO):
            conviction = self._scale_conviction(
                base=0.72,
                vol_ratio=vol_ratio,
                has_fundamental=has_fundamental,
                signal_type="DIP",
            )
            return IntradaySignal(
                symbol=symbol,
                signal_type="DIP",
                conviction=conviction,
                price=current_price,
                rsi=rsi,
                volume_ratio=vol_ratio,
                vwap=vwap,
                bb_pct=bb_pct,
                price_change_1h=price_change_1h,
                has_fundamental=has_fundamental,
                rationale=(
                    f"RSI {rsi:.1f} extreme oversold at lower Bollinger Band "
                    f"with {vol_ratio:.1f}x volume — capitulation signal. "
                    f"Mean reversion setup."
                ),
            )

        # 2. WAVE — RSI crossing upward through 50 (momentum resuming)
        if (rsi is not None and prev_rsi is not None
                and prev_rsi < RSI_WAVE_CROSS <= rsi
                and vol_ratio is not None and vol_ratio >= 1.5):
            conviction = self._scale_conviction(
                base=0.68,
                vol_ratio=vol_ratio,
                has_fundamental=has_fundamental,
                signal_type="WAVE",
            )
            return IntradaySignal(
                symbol=symbol,
                signal_type="WAVE",
                conviction=conviction,
                price=current_price,
                rsi=rsi,
                volume_ratio=vol_ratio,
                vwap=vwap,
                bb_pct=bb_pct,
                price_change_1h=price_change_1h,
                has_fundamental=has_fundamental,
                rationale=(
                    f"RSI crossed upward through {RSI_WAVE_CROSS:.0f} "
                    f"({prev_rsi:.1f} → {rsi:.1f}) with {vol_ratio:.1f}x volume — "
                    f"momentum resuming."
                ),
            )

        # 3. BREAKOUT — price above upper BB + strong volume
        if (bb_upper is not None and current_price > bb_upper
                and vol_ratio is not None and vol_ratio >= 2.0
                and price_change_1h > 1.0):
            conviction = self._scale_conviction(
                base=0.70,
                vol_ratio=vol_ratio,
                has_fundamental=has_fundamental,
                signal_type="BREAKOUT",
            )
            return IntradaySignal(
                symbol=symbol,
                signal_type="BREAKOUT",
                conviction=conviction,
                price=current_price,
                rsi=rsi,
                volume_ratio=vol_ratio,
                vwap=vwap,
                bb_pct=bb_pct,
                price_change_1h=price_change_1h,
                has_fundamental=has_fundamental,
                rationale=(
                    f"Price broke above upper Bollinger Band "
                    f"({current_price:.2f} > {bb_upper:.2f}) "
                    f"on {vol_ratio:.1f}x volume — confirmed breakout."
                ),
            )

        # 4. VWAP_DIP — price significantly below VWAP with elevated volume
        if (vwap is not None and vwap > 0):
            vwap_dev = ((current_price - vwap) / vwap) * 100
            if (vwap_dev < -VWAP_DIP_PCT
                    and vol_ratio is not None and vol_ratio >= VOLUME_CONFIRM_RATIO
                    and rsi is not None and rsi < 40):
                conviction = self._scale_conviction(
                    base=0.65,
                    vol_ratio=vol_ratio,
                    has_fundamental=has_fundamental,
                    signal_type="VWAP_DIP",
                )
                return IntradaySignal(
                    symbol=symbol,
                    signal_type="VWAP_DIP",
                    conviction=conviction,
                    price=current_price,
                    rsi=rsi,
                    volume_ratio=vol_ratio,
                    vwap=vwap,
                    bb_pct=bb_pct,
                    price_change_1h=price_change_1h,
                    has_fundamental=has_fundamental,
                    rationale=(
                        f"Price {abs(vwap_dev):.1f}% below VWAP (${vwap:.2f}) "
                        f"with RSI {rsi:.1f} and {vol_ratio:.1f}x volume — "
                        f"mean reversion to VWAP likely."
                    ),
                )

        # 5. VWAP_RECLAIM — price crossing back above VWAP
        if vwap is not None and vwap > 0 and len(close) >= 3:
            prev_close = float(close.iloc[-2])
            prev_vwap_dev = (prev_close - vwap) / vwap * 100
            curr_vwap_dev = (current_price - vwap) / vwap * 100
            if (prev_vwap_dev < -0.3 and curr_vwap_dev >= 0
                    and vol_ratio is not None and vol_ratio >= 1.5):
                conviction = self._scale_conviction(
                    base=0.63,
                    vol_ratio=vol_ratio,
                    has_fundamental=has_fundamental,
                    signal_type="VWAP_RECLAIM",
                )
                return IntradaySignal(
                    symbol=symbol,
                    signal_type="VWAP_RECLAIM",
                    conviction=conviction,
                    price=current_price,
                    rsi=rsi,
                    volume_ratio=vol_ratio,
                    vwap=vwap,
                    bb_pct=bb_pct,
                    price_change_1h=price_change_1h,
                    has_fundamental=has_fundamental,
                    rationale=(
                        f"Price reclaimed VWAP (${vwap:.2f}) after trading below — "
                        f"momentum flip signal with {vol_ratio:.1f}x volume."
                    ),
                )

        return None

    def _scale_conviction(
        self,
        base: float,
        vol_ratio: Optional[float],
        has_fundamental: bool,
        signal_type: str,
    ) -> float:
        """
        Scale conviction based on volume strength and fundamental context.
        Caps at MAX_CONVICTION_NO_FUND if no fundamental research exists.
        """
        conviction = base

        # Volume boost — stronger volume = more reliable signal
        if vol_ratio:
            if vol_ratio >= 3.0:
                conviction += 0.05
            elif vol_ratio >= 2.0:
                conviction += 0.03

        # Fundamental boost — existing BULLISH research = higher confidence
        if has_fundamental:
            conviction += FUNDAMENTAL_BOOST
        else:
            # Cap conviction for purely technical signals with no fundamental context
            conviction = min(conviction, MAX_CONVICTION_NO_FUND)

        return round(min(conviction, 0.92), 2)

    def _compute_vwap(self, df: pd.DataFrame) -> Optional[float]:
        """Compute VWAP from available intraday bars."""
        try:
            typical_price = (df["high"] + df["low"] + df["close"]) / 3
            vol = df["volume"]
            vwap = (typical_price * vol).sum() / vol.sum()
            return float(vwap) if not pd.isna(vwap) else None
        except Exception:
            return None

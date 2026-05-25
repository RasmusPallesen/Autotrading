"""
Multi-timeframe momentum monitor for 1-3 day swing setups.
Analyses 15-minute and 1-hour bars — fills the gap between the 1-min intraday
monitor (too short-term) and the 15-min deep research cycle (too slow).

Signal types:
  TREND_DAY    — 15-min RSI > 60, price above VWAP, 3+ consecutive green bars
  MEAN_REVERT  — 1-hr RSI < 35, price below lower BB, volume declining (exhaustion)
  VOLUME_SURGE — Today's volume > 2x 5-day avg before 2pm ET (institutional accumulation)

Writes signals directly to research_signals with signal_type="MOMENTUM".
No Claude call — pure price/volume logic.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)

# ── Thresholds ─────────────────────────────────────────────────────────────────
RSI_TREND_MIN      = float(os.getenv("MOMENTUM_RSI_TREND", "58"))
RSI_REVERT_MAX     = float(os.getenv("MOMENTUM_RSI_REVERT", "36"))
VOLUME_SURGE_RATIO = float(os.getenv("MOMENTUM_VOLUME_SURGE", "2.0"))
MIN_CONVICTION     = float(os.getenv("MOMENTUM_MIN_CONVICTION", "0.58"))
SIGNAL_COOLDOWN_H  = int(os.getenv("MOMENTUM_COOLDOWN_HOURS", "4"))


@dataclass
class MomentumSignal:
    symbol: str
    signal_type: str         # TREND_DAY | MEAN_REVERT | VOLUME_SURGE
    conviction: float
    summary: str
    key_points: List[str]
    risk_factors: List[str]
    ttl_hours: int = 4
    discovered_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def sentiment(self) -> str:
        if self.signal_type == "MEAN_REVERT":
            return "BULLISH"
        if self.conviction >= 0.65:
            return "BULLISH"
        return "BULLISH"  # All momentum signals are directionally bullish

    @property
    def recommended_action(self) -> str:
        return "BUY"


class MomentumMonitor:
    """
    Scans a symbol list for multi-timeframe momentum setups.
    Fetches 15-min and 1-hr bars from Alpaca and applies price/volume rules.
    """

    def __init__(self):
        self._signal_cache: Dict[str, datetime] = {}

    def scan(self, symbols: List[str], alpaca_config) -> List[MomentumSignal]:
        if alpaca_config is None:
            return []

        try:
            from data.alpaca_fetcher import AlpacaDataFetcher
            fetcher = AlpacaDataFetcher(alpaca_config)
        except Exception as e:
            logger.warning("MomentumMonitor: could not init AlpacaDataFetcher: %s", e)
            return []

        bars_15m = fetcher.get_bars(symbols, lookback_bars=48, timeframe="15Min")
        bars_1h  = fetcher.get_bars(symbols, lookback_bars=24, timeframe="1Hour")

        signals = []
        for symbol in symbols:
            if self._in_cooldown(symbol):
                continue
            try:
                sig = self._analyse(symbol, bars_15m.get(symbol), bars_1h.get(symbol))
                if sig and sig.conviction >= MIN_CONVICTION:
                    signals.append(sig)
                    self._signal_cache[symbol] = datetime.now(timezone.utc)
                    logger.info(
                        "[MOMENTUM] %s %s conviction=%.0f%%",
                        symbol, sig.signal_type, sig.conviction * 100,
                    )
            except Exception as e:
                logger.debug("[%s] Momentum analysis error: %s", symbol, e)

        return signals

    def _in_cooldown(self, symbol: str) -> bool:
        last = self._signal_cache.get(symbol)
        if not last:
            return False
        age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return age_h < SIGNAL_COOLDOWN_H

    def _analyse(
        self,
        symbol: str,
        df_15m: Optional[pd.DataFrame],
        df_1h: Optional[pd.DataFrame],
    ) -> Optional[MomentumSignal]:
        # Try VOLUME_SURGE on 15-min data (needs enough bars for today)
        if df_15m is not None and len(df_15m) >= 10:
            sig = self._check_volume_surge(symbol, df_15m)
            if sig:
                return sig

        # Try TREND_DAY on 15-min data
        if df_15m is not None and len(df_15m) >= 20:
            sig = self._check_trend_day(symbol, df_15m)
            if sig:
                return sig

        # Try MEAN_REVERT on 1-hr data
        if df_1h is not None and len(df_1h) >= 14:
            sig = self._check_mean_revert(symbol, df_1h)
            if sig:
                return sig

        return None

    def _check_trend_day(self, symbol: str, df: pd.DataFrame) -> Optional[MomentumSignal]:
        """15-min trend: RSI > threshold, price above VWAP, 3+ consecutive green bars."""
        rsi_series = ta.rsi(df["close"], length=14)
        if rsi_series is None or rsi_series.isna().all():
            return None

        rsi = float(rsi_series.iloc[-1])
        if rsi < RSI_TREND_MIN:
            return None

        # VWAP
        vwap = _calc_vwap(df)
        price = float(df["close"].iloc[-1])
        if vwap is None or price <= vwap:
            return None

        # 3 consecutive green bars
        last3 = df.tail(3)
        if not all(last3["close"].values > last3["open"].values):
            return None

        # Volume vs 10-bar average
        vol_ratio = float(df["volume"].iloc[-1]) / float(df["volume"].iloc[-10:].mean())

        conviction = 0.60
        if rsi > 65:
            conviction += 0.05
        if vol_ratio > 1.5:
            conviction += 0.05
        conviction = min(conviction, 0.78)

        vwap_dev = (price - vwap) / vwap * 100
        return MomentumSignal(
            symbol=symbol,
            signal_type="TREND_DAY",
            conviction=conviction,
            summary=(
                f"15-min trend day: RSI {rsi:.0f}, price {vwap_dev:+.1f}% above VWAP, "
                f"3 consecutive green bars. Volume {vol_ratio:.1f}x avg."
            ),
            key_points=[
                f"RSI: {rsi:.0f} (trending momentum)",
                f"Price vs VWAP: {vwap_dev:+.2f}%",
                f"Volume: {vol_ratio:.1f}x 10-bar average",
                "3 consecutive green 15-min candles",
            ],
            risk_factors=[
                "Trend-day setup can reverse sharply on news",
                "Check broader market direction before entry",
            ],
            ttl_hours=4,
        )

    def _check_mean_revert(self, symbol: str, df: pd.DataFrame) -> Optional[MomentumSignal]:
        """1-hr mean reversion: RSI < threshold + price below lower BB + volume declining."""
        rsi_series = ta.rsi(df["close"], length=14)
        if rsi_series is None or rsi_series.isna().all():
            return None

        rsi = float(rsi_series.iloc[-1])
        if rsi > RSI_REVERT_MAX:
            return None

        bb = ta.bbands(df["close"], length=20, std=2)
        if bb is None or bb.empty:
            return None

        lower_col = [c for c in bb.columns if "BBL" in c]
        if not lower_col:
            return None

        lower_bb = float(bb[lower_col[0]].iloc[-1])
        price = float(df["close"].iloc[-1])
        if price > lower_bb * 1.01:  # Must be at or below lower BB
            return None

        # Volume declining over last 3 bars (exhaustion signal)
        last3_vol = df["volume"].iloc[-3:].values
        vol_declining = last3_vol[-1] < last3_vol[0]

        conviction = 0.60
        if rsi < 30:
            conviction += 0.07
        if vol_declining:
            conviction += 0.04
        conviction = min(conviction, 0.75)

        pct_below_bb = (lower_bb - price) / lower_bb * 100
        return MomentumSignal(
            symbol=symbol,
            signal_type="MEAN_REVERT",
            conviction=conviction,
            summary=(
                f"1-hr oversold: RSI {rsi:.0f}, price {pct_below_bb:.1f}% below lower BB. "
                f"{'Volume declining (exhaustion).' if vol_declining else 'Watch for volume confirmation.'}"
            ),
            key_points=[
                f"RSI: {rsi:.0f} (oversold on 1-hr bars)",
                f"Price {pct_below_bb:.1f}% below lower Bollinger Band",
                "Volume declining — selling exhaustion signal" if vol_declining else "Volume pattern: no exhaustion yet",
            ],
            risk_factors=[
                "Mean reversion can fail in strong downtrends",
                "Confirm with 1-min volume spike before entry",
                "Short-term only — reassess daily",
            ],
            ttl_hours=3,
        )

    def _check_volume_surge(self, symbol: str, df: pd.DataFrame) -> Optional[MomentumSignal]:
        """Today's volume vs 5-day average — detects institutional accumulation."""
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")

        # Only fire before 18:00 UTC (2pm ET) — early volume surge is more meaningful
        if now_utc.hour >= 18:
            return None

        # Filter to today's bars
        if hasattr(df.index, "date"):
            today_df = df[df.index.date == now_utc.date()]
        else:
            today_df = df[df.index.astype(str).str.startswith(today_str)]

        if len(today_df) < 3:
            return None

        today_vol = float(today_df["volume"].sum())

        # 5-day average daily volume from prior bars
        prior_df = df[~df.index.isin(today_df.index)] if hasattr(df.index, "date") else df.iloc[:-len(today_df)]
        if len(prior_df) < 10:
            return None

        # Project today's volume to full day (based on how much of the day has elapsed)
        market_open_utc = now_utc.replace(hour=13, minute=30, second=0, microsecond=0)
        elapsed_min = max((now_utc - market_open_utc).total_seconds() / 60, 1)
        projected_vol = today_vol * (390 / elapsed_min)  # 390 min = full trading day

        # Build 5-day daily volume from prior bars (sum per day)
        if hasattr(prior_df.index, "date"):
            daily_vols = prior_df.groupby(prior_df.index.date)["volume"].sum()
        else:
            daily_vols = prior_df.resample("1D")["volume"].sum()

        daily_vols = daily_vols[daily_vols > 0].tail(5)
        if len(daily_vols) < 3:
            return None

        avg_daily_vol = float(daily_vols.mean())
        vol_ratio = projected_vol / avg_daily_vol if avg_daily_vol > 0 else 1.0

        if vol_ratio < VOLUME_SURGE_RATIO:
            return None

        price = float(df["close"].iloc[-1])
        price_change = (price - float(df["open"].iloc[0])) / float(df["open"].iloc[0]) * 100

        conviction = 0.60
        if vol_ratio > 3.0:
            conviction += 0.08
        elif vol_ratio > 2.5:
            conviction += 0.05
        if price_change > 1.0:
            conviction += 0.05
        conviction = min(conviction, 0.80)

        return MomentumSignal(
            symbol=symbol,
            signal_type="VOLUME_SURGE",
            conviction=conviction,
            summary=(
                f"Volume surge: {vol_ratio:.1f}x projected daily vs 5-day avg. "
                f"Price change today: {price_change:+.1f}%. Possible institutional accumulation."
            ),
            key_points=[
                f"Projected daily volume: {vol_ratio:.1f}x 5-day average",
                f"Today's price change: {price_change:+.2f}%",
                f"Bars captured so far: {len(today_df)} (elapsed ~{elapsed_min:.0f} min)",
            ],
            risk_factors=[
                "Volume surge without price follow-through can be distribution",
                "Verify no pending earnings or binary event driving the volume",
            ],
            ttl_hours=4,
        )


def _calc_vwap(df: pd.DataFrame) -> Optional[float]:
    """Compute VWAP from OHLCV bars."""
    try:
        typical = (df["high"] + df["low"] + df["close"]) / 3
        vwap = (typical * df["volume"]).sum() / df["volume"].sum()
        return float(vwap)
    except Exception:
        return None

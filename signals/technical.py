"""
Signal engine: computes technical indicators on OHLCV DataFrames.
Returns a structured SignalSnapshot per symbol for the AI to consume.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)


@dataclass
class SignalSnapshot:
    symbol: str
    current_price: float
    price_change_pct_1h: Optional[float]

    # Momentum
    rsi_14: Optional[float]
    macd: Optional[float]
    macd_signal: Optional[float]
    macd_histogram: Optional[float]

    # Trend
    ema_9: Optional[float]
    ema_21: Optional[float]
    ema_50: Optional[float]
    above_ema9: Optional[bool]
    above_ema21: Optional[bool]

    # Volatility
    bb_upper: Optional[float]
    bb_lower: Optional[float]
    bb_mid: Optional[float]
    bb_pct: Optional[float]
    atr_14: Optional[float]

    # Volume
    volume_latest: Optional[float]
    volume_sma_20: Optional[float]
    volume_ratio: Optional[float]

    # Derived signals
    signals: Dict[str, str] = field(default_factory=dict)

    def to_prompt_text(self) -> str:
        """Formats snapshot as concise text for the AI prompt."""
        lines = [
            f"Symbol: {self.symbol}",
            f"Price: {self.current_price:.4f}",
            f"1h change: {self.price_change_pct_1h:.2f}%" if self.price_change_pct_1h else "1h change: N/A",
            f"RSI(14): {self.rsi_14:.1f}" if self.rsi_14 else "RSI: N/A",
            f"MACD: {self.macd:.4f} | Signal: {self.macd_signal:.4f} | Hist: {self.macd_histogram:.4f}"
            if all(v is not None for v in [self.macd, self.macd_signal, self.macd_histogram])
            else "MACD: N/A",
            f"EMA9: {self.ema_9:.4f} | EMA21: {self.ema_21:.4f} | EMA50: {self.ema_50:.4f}"
            if all(v is not None for v in [self.ema_9, self.ema_21, self.ema_50])
            else "EMAs: N/A",
            f"BB%: {self.bb_pct:.2f} (upper={self.bb_upper:.4f}, lower={self.bb_lower:.4f})"
            if all(v is not None for v in [self.bb_pct, self.bb_upper, self.bb_lower])
            else "BB: N/A",
            f"ATR(14): {self.atr_14:.4f}" if self.atr_14 else "ATR: N/A",
            f"Volume ratio: {self.volume_ratio:.2f}x" if self.volume_ratio else "Volume: N/A",
        ]
        if self.signals:
            lines.append("Derived signals: " + ", ".join(f"{k}={v}" for k, v in self.signals.items()))
        return "\n".join(lines)


def _safe(series: pd.Series, idx: int = -1) -> Optional[float]:
    """Safely extract a value from a Series; return None on failure."""
    try:
        val = series.iloc[idx]
        if pd.isna(val):
            return None
        return float(val)
    except Exception:
        return None


def _bb_col(bb: pd.DataFrame, prefix: str) -> Optional[float]:
    """Find a Bollinger Band column by prefix (handles version differences)."""
    matches = [c for c in bb.columns if c.startswith(prefix)]
    if not matches:
        return None
    return _safe(bb[matches[0]])


def compute_signals(symbol: str, df: pd.DataFrame) -> Optional[SignalSnapshot]:
    """
    Compute all technical indicators for a symbol given its OHLCV DataFrame.
    Returns None if there is insufficient data.
    """
    if df is None or len(df) < 20:
        logger.warning("Insufficient data for %s (%d bars)", symbol, len(df) if df is not None else 0)
        return None

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    current_price = float(close.iloc[-1])

    # 1h price change
    lookback = min(60, len(df) - 1)
    price_1h_ago = float(close.iloc[-(lookback + 1)])
    price_change_1h = ((current_price - price_1h_ago) / price_1h_ago) * 100

    # RSI
    rsi = ta.rsi(close, length=14)
    rsi_val = _safe(rsi)

    # MACD
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    if macd_df is not None:
        macd_cols = macd_df.columns.tolist()
        macd_val = _safe(macd_df[[c for c in macd_cols if c.startswith("MACD_")][0]]) if any(c.startswith("MACD_") for c in macd_cols) else None
        macd_sig = _safe(macd_df[[c for c in macd_cols if c.startswith("MACDs")][0]]) if any(c.startswith("MACDs") for c in macd_cols) else None
        macd_hist = _safe(macd_df[[c for c in macd_cols if c.startswith("MACDh")][0]]) if any(c.startswith("MACDh") for c in macd_cols) else None
    else:
        macd_val = macd_sig = macd_hist = None

    # EMAs
    ema9 = ta.ema(close, length=9)
    ema21 = ta.ema(close, length=21)
    ema50 = ta.ema(close, length=50) if len(df) >= 50 else None
    ema9_val = _safe(ema9)
    ema21_val = _safe(ema21)

    # Bollinger Bands (column names vary by pandas-ta version, match by prefix)
    bb = ta.bbands(close, length=20, std=2)
    if bb is not None:
        bb_upper = _bb_col(bb, "BBU")
        bb_lower = _bb_col(bb, "BBL")
        bb_mid   = _bb_col(bb, "BBM")
        bb_pct   = _bb_col(bb, "BBP")
    else:
        bb_upper = bb_lower = bb_mid = bb_pct = None

    # ATR
    atr = ta.atr(high, low, close, length=14)

    # Volume
    vol_sma = ta.sma(volume, length=20)
    vol_latest = _safe(volume)
    vol_sma_val = _safe(vol_sma)
    vol_ratio = (vol_latest / vol_sma_val) if (vol_latest and vol_sma_val and vol_sma_val > 0) else None

    # Derived signals
    signals: Dict[str, str] = {}
    if rsi_val:
        if rsi_val < 30:
            signals["RSI"] = "OVERSOLD"
        elif rsi_val > 70:
            signals["RSI"] = "OVERBOUGHT"
        else:
            signals["RSI"] = "NEUTRAL"

    if ema9_val and ema21_val:
        signals["EMA_CROSS"] = "BULLISH" if ema9_val > ema21_val else "BEARISH"

    if macd_hist is not None:
        signals["MACD"] = "BULLISH" if macd_hist > 0 else "BEARISH"

    if bb_pct is not None:
        if bb_pct < 0.2:
            signals["BB"] = "NEAR_LOWER"
        elif bb_pct > 0.8:
            signals["BB"] = "NEAR_UPPER"
        else:
            signals["BB"] = "MID_BAND"

    if vol_ratio and vol_ratio > 1.5:
        signals["VOLUME"] = f"ELEVATED({vol_ratio:.1f}x)"

    return SignalSnapshot(
        symbol=symbol,
        current_price=current_price,
        price_change_pct_1h=price_change_1h,
        rsi_14=rsi_val,
        macd=macd_val,
        macd_signal=macd_sig,
        macd_histogram=macd_hist,
        ema_9=ema9_val,
        ema_21=ema21_val,
        ema_50=_safe(ema50) if ema50 is not None else None,
        above_ema9=current_price > ema9_val if ema9_val else None,
        above_ema21=current_price > ema21_val if ema21_val else None,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        bb_mid=bb_mid,
        bb_pct=bb_pct,
        atr_14=_safe(atr),
        volume_latest=vol_latest,
        volume_sma_20=vol_sma_val,
        volume_ratio=vol_ratio,
        signals=signals,
    )
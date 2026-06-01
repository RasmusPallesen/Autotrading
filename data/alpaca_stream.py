"""
Live bar streaming via Alpaca WebSocket.

Maintains a rolling 200-bar buffer per symbol and exposes a hot_queue
that fires whenever a bar close looks like a momentum event worth
evaluating immediately (outside the normal 5-minute polling loop).

Thread-safety: the buffer dict and its deques are protected by a single
RLock so the main thread can read buffers while the stream thread writes.
"""

import logging
import math
import queue
import threading
from collections import deque
from datetime import datetime, timezone

import pandas as pd

logger = logging.getLogger(__name__)

MAX_BARS = 200  # 200 bars ≈ 3h20m of 1-min data; enough for EMA-50 to converge reliably

# A bar close is "hot" (triggers fast evaluation) if:
#   volume >= HOT_VOLUME_MULTIPLE × 10-bar average  AND  |bar change| >= HOT_PRICE_PCT
# OR the close crosses above EMA-21 from below (bullish entry signal)
# OR the close crosses below EMA-21 from above on a held position (bearish exit signal)
# OR a held position drops more than HOT_DROP_PCT in a single bar (immediate sell eval)
HOT_VOLUME_MULTIPLE = 2.0
HOT_PRICE_PCT       = 0.010   # 1.0%
HOT_DROP_PCT        = 0.020   # 2.0% single-bar drop on a held position


class AlpacaBarStream:
    """
    Background WebSocket stream that buffers 1-min bars per symbol and
    enqueues hot symbols for immediate evaluation.
    """

    def __init__(self, api_key: str, secret_key: str, symbols: list[str],
                 held_symbols: set[str] | None = None):
        self._api_key     = api_key
        self._secret_key  = secret_key
        self._lock        = threading.RLock()
        self._buffers: dict[str, deque] = {}          # symbol → deque of bar dicts
        self._subscribed: set[str] = set()
        self._held        = held_symbols or set()
        self._stream      = None
        self._thread: threading.Thread | None = None
        self._stopping    = False

        self.hot_queue: queue.Queue = queue.Queue()   # (symbol, bar_dict) tuples

        self._init_stream()
        if symbols:
            self._subscribe(set(symbols))

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the WebSocket stream in a daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="alpaca-stream")
        self._thread.start()
        logger.info("[STREAM] Started — subscribed to %d symbols", len(self._subscribed))

    def stop(self) -> None:
        """Graceful shutdown."""
        self._stopping = True
        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                pass
        logger.info("[STREAM] Stopped")

    def get_bars_df(self, symbol: str) -> pd.DataFrame | None:
        """
        Return the rolling bar buffer for a symbol as a DataFrame.
        Returns None if the buffer is empty (not yet populated).
        """
        with self._lock:
            buf = self._buffers.get(symbol)
            if not buf:
                return None
            df = pd.DataFrame(list(buf))
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.set_index("timestamp").sort_index()
            return df

    def update_symbols(self, symbols: list[str], held_symbols: set[str] | None = None) -> None:
        """
        Sync WebSocket subscriptions with the current active symbol list.
        Called once per 5-minute tick by the main loop.
        """
        if held_symbols is not None:
            self._held = held_symbols

        want = set(symbols)
        with self._lock:
            to_add  = want - self._subscribed
            to_drop = self._subscribed - want

        if to_add:
            self._subscribe(to_add)
        if to_drop:
            self._unsubscribe(to_drop)

    def seed_buffers(self, bars_map: dict) -> None:
        """
        Pre-populate buffers with historical bars fetched via REST.
        Call once at startup to eliminate the ~50-minute cold-start period.

        bars_map: {symbol: pd.DataFrame} as returned by AlpacaDataFetcher.get_bars().
        Each DataFrame must have columns: open, high, low, close, volume.
        """
        seeded = 0
        with self._lock:
            for symbol, df in bars_map.items():
                if df is None or df.empty:
                    continue
                if symbol not in self._buffers:
                    self._buffers[symbol] = deque(maxlen=MAX_BARS)
                buf = self._buffers[symbol]
                for ts, row in df.iterrows():
                    buf.append({
                        "timestamp": ts,
                        "open":   float(row.get("open",  row.get("o", 0))),
                        "high":   float(row.get("high",  row.get("h", 0))),
                        "low":    float(row.get("low",   row.get("l", 0))),
                        "close":  float(row.get("close", row.get("c", 0))),
                        "volume": float(row.get("volume", row.get("v", 0))),
                    })
                seeded += 1
        logger.info("[STREAM] Seeded %d/%d symbol buffers from REST history", seeded, len(bars_map))

    def buffer_size(self) -> dict[str, int]:
        """Return {symbol: bar_count} for diagnostics."""
        with self._lock:
            return {s: len(d) for s, d in self._buffers.items()}

    # ── Internal ──────────────────────────────────────────────────────────────

    def _init_stream(self) -> None:
        try:
            from alpaca.data.live import StockDataStream
            self._StockDataStream = StockDataStream
        except ImportError:
            raise ImportError("alpaca-py >= 0.20 required: pip install 'alpaca-py>=0.20.0'")

        self._stream = self._StockDataStream(
            self._api_key,
            self._secret_key,
        )

    def _subscribe(self, symbols: set[str]) -> None:
        if not symbols or not self._stream:
            return
        sym_list = sorted(symbols)
        try:
            self._stream.subscribe_bars(self._on_bar, *sym_list)
            with self._lock:
                self._subscribed |= symbols
                for s in symbols:
                    if s not in self._buffers:
                        self._buffers[s] = deque(maxlen=MAX_BARS)
            logger.info("[STREAM] Subscribed to %d symbols: %s…", len(sym_list), sym_list[:5])
        except Exception as e:
            logger.warning("[STREAM] Subscribe error: %s", e)

    def _unsubscribe(self, symbols: set[str]) -> None:
        if not symbols or not self._stream:
            return
        try:
            self._stream.unsubscribe_bars(*sorted(symbols))
            with self._lock:
                self._subscribed -= symbols
            logger.debug("[STREAM] Unsubscribed %d symbols", len(symbols))
        except Exception as e:
            logger.warning("[STREAM] Unsubscribe error: %s", e)

    async def _on_bar(self, bar) -> None:
        """
        Async callback fired by alpaca-py for each completed 1-min bar.
        Appends to buffer and checks hot criteria.
        """
        symbol = bar.symbol
        bar_dict = {
            "timestamp": bar.timestamp,
            "open":   float(bar.open),
            "high":   float(bar.high),
            "low":    float(bar.low),
            "close":  float(bar.close),
            "volume": float(bar.volume),
        }

        with self._lock:
            if symbol not in self._buffers:
                self._buffers[symbol] = deque(maxlen=MAX_BARS)
            self._buffers[symbol].append(bar_dict)
            buf_snapshot = list(self._buffers[symbol])

        if self._is_hot(symbol, bar_dict, buf_snapshot):
            self.hot_queue.put((symbol, bar_dict))
            logger.info(
                "[STREAM] HOT %s — close=%.2f vol=%.0f",
                symbol, bar_dict["close"], bar_dict["volume"],
            )

    def _is_hot(self, symbol: str, bar: dict, buf: list) -> bool:
        """Return True if this bar close warrants immediate evaluation."""
        if len(buf) < 2:
            return False

        closes  = [b["close"] for b in buf]
        volumes = [b["volume"] for b in buf]

        bar_change = (bar["close"] - bar["open"]) / bar["open"] if bar["open"] else 0

        # Volume surge + price move
        if len(volumes) >= 10:
            avg_vol_10 = sum(volumes[-10:]) / 10
            if avg_vol_10 > 0 and bar["volume"] >= HOT_VOLUME_MULTIPLE * avg_vol_10:
                if abs(bar_change) >= HOT_PRICE_PCT:
                    return True

        # EMA-21 cross — upward cross is a bullish entry signal;
        # downward cross on a held position is a bearish exit signal
        if len(closes) >= 22:
            ema21 = _ema(closes, 21)
            prev_close = closes[-2]
            # Bullish: price crosses above EMA-21 (any symbol)
            if prev_close < ema21[-2] and bar["close"] > ema21[-1]:
                return True
            # Bearish: price crosses below EMA-21 on a held position → sell signal
            if symbol in self._held and prev_close > ema21[-2] and bar["close"] < ema21[-1]:
                logger.info(
                    "[STREAM] HOT %s — bearish EMA-21 cross (held position), close=%.2f ema21=%.2f",
                    symbol, bar["close"], ema21[-1],
                )
                return True

        # Held position dropping sharply — immediate SELL consideration
        if symbol in self._held and bar_change <= -HOT_DROP_PCT:
            return True

        # RSI oversold bounce — momentum reversal entry signal (any symbol)
        if len(closes) >= 15:
            rsi = _rsi(closes, 14)
            if not (math.isnan(rsi[-2]) or math.isnan(rsi[-1])):
                if rsi[-2] < 30 and rsi[-1] >= 30:
                    return True

        # New 20-bar high — breakout from consolidation (any symbol)
        if len(closes) >= 21:
            if bar["close"] > max(closes[-21:-1]):
                return True

        # VWAP cross from below — intraday trend flip (any symbol)
        if len(buf) >= 5:
            vwap = _vwap(buf)
            if buf[-2]["close"] < vwap and bar["close"] >= vwap:
                return True

        return False

    def _run(self) -> None:
        """Blocking call — runs in daemon thread."""
        while not self._stopping:
            try:
                self._stream.run()
            except Exception as e:
                if not self._stopping:
                    logger.warning("[STREAM] Stream error, reconnecting in 5s: %s", e)
                    import time
                    time.sleep(5)
                    self._init_stream()
                    with self._lock:
                        to_resubscribe = set(self._subscribed)
                        self._subscribed.clear()
                    self._subscribe(to_resubscribe)


def _ema(values: list[float], period: int) -> list[float]:
    """Simple EMA helper — returns list same length as input (NaN-padded at start)."""
    if len(values) < period:
        return [float("nan")] * len(values)
    k = 2 / (period + 1)
    result = [float("nan")] * (period - 1)
    result.append(sum(values[:period]) / period)
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _rsi(values: list[float], period: int) -> list[float]:
    """Wilder RSI — returns list same length as input (NaN-padded at start)."""
    if len(values) < period + 1:
        return [float("nan")] * len(values)
    result = [float("nan")] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
    result.append(100 - 100 / (1 + rs))
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
        result.append(100 - 100 / (1 + rs))
    return result


def _vwap(buf: list[dict]) -> float:
    """Dollar-volume weighted average price over the buffer."""
    total_vol = sum(b["volume"] for b in buf)
    if total_vol == 0:
        return buf[-1]["close"]
    return sum(b["close"] * b["volume"] for b in buf) / total_vol

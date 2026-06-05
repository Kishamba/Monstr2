import logging
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)

# Phase 5: sector groups for correlated exposure control
SECTOR_GROUPS: dict[str, set] = {
    "layer1":   {"SOL/USDT:USDT", "AVAX/USDT:USDT", "SUI/USDT:USDT"},
    "payments": {"DOGE/USDT:USDT", "XRP/USDT:USDT"},
    "bluechip": {"ETH/USDT:USDT"},
}
MAX_SECTOR_POSITIONS = 2


class MarketRegimeFilter:
    """
    Phase 4: Blocks entries during chaotic market conditions.
    Provides BTC trend bias and trade quality scoring.
    """

    BTC_IMPULSE_PCT  = 1.2   # % move in ~45 min that triggers countertrend block
    ATR_EXPANSION    = 1.8   # current ATR / rolling mean > this → spike block
    ADX_CHOP_LIMIT   = 18    # ADX below this = chop (combined with EMA check)
    EMA_COMPRESS_PCT = 0.5   # EMA8/EMA21 spread < this % of price = compressed

    def __init__(self):
        self._btc_bias            = "neutral"  # "bullish" | "bearish" | "neutral"
        self._btc_impulse_active  = False
        self._btc_impulse_dir     = None       # "up" | "down"
        self._atr_history: dict[str, deque] = {}

    # ── BTC regime update ─────────────────────────────────────────────────────

    def update_btc(self, btc_15m_df) -> dict:
        """Call once per main loop with BTC 15m OHLCV DataFrame."""
        try:
            close = btc_15m_df["close"].values.astype(float)

            # Impulse: last candle vs 3 candles back (~45 min on 15m)
            if len(close) >= 4:
                pct = (close[-1] - close[-4]) / close[-4] * 100
                if abs(pct) >= self.BTC_IMPULSE_PCT:
                    self._btc_impulse_active = True
                    self._btc_impulse_dir    = "up" if pct > 0 else "down"
                else:
                    self._btc_impulse_active = False
                    self._btc_impulse_dir    = None

            # EMA bias from 15m closes (21 candles ≈ 5H — enough for trend)
            if len(close) >= 30:
                ema8  = self._ema(close, 8)
                ema21 = self._ema(close, 21)
                if ema8 > ema21 * 1.002:
                    self._btc_bias = "bullish"
                elif ema8 < ema21 * 0.998:
                    self._btc_bias = "bearish"
                else:
                    self._btc_bias = "neutral"

            logger.info(
                f"BTC regime: bias={self._btc_bias} "
                f"impulse={self._btc_impulse_active}({self._btc_impulse_dir})"
            )
            return {
                "btc_bias":        self._btc_bias,
                "btc_impulse":     self._btc_impulse_active,
                "btc_impulse_dir": self._btc_impulse_dir,
            }
        except Exception as e:
            logger.warning(f"BTC regime update error: {e}")
            return {"btc_bias": "neutral", "btc_impulse": False, "btc_impulse_dir": None}

    # ── Per-entry regime check ────────────────────────────────────────────────

    def is_entry_blocked(self, action: str, symbol: str,
                          indicators: dict) -> tuple[bool, str]:
        """Returns (blocked, reason). Call before opening each position."""

        # 1. BTC impulse blocks countertrend entries
        if self._btc_impulse_active:
            if self._btc_impulse_dir == "up" and action == "short":
                return True, "btc_impulse_up→blocks_short"
            if self._btc_impulse_dir == "down" and action == "long":
                return True, "btc_impulse_down→blocks_long"

        # 2. ATR expansion spike (rolling average per symbol)
        atr_pct = indicators.get("atr_pct_1h", 0)
        hist = self._atr_history.setdefault(symbol, deque(maxlen=24))
        if len(hist) >= 10:
            rolling_mean = float(np.mean(list(hist)))
            if rolling_mean > 0 and atr_pct > rolling_mean * self.ATR_EXPANSION:
                hist.append(atr_pct)
                return True, f"atr_spike ({atr_pct:.2f}% vs avg {rolling_mean:.2f}%)"
        hist.append(atr_pct)

        # 3. Chop detector: ADX + EMA compression both must be true
        adx   = indicators.get("adx_1h", 25)
        ema8  = indicators.get("ema_8", 1)
        ema21 = indicators.get("ema_21", 1)
        close = indicators.get("close_1h", 1) or 1
        ema_spread_pct = abs(ema8 - ema21) / close * 100

        if adx < self.ADX_CHOP_LIMIT and ema_spread_pct < self.EMA_COMPRESS_PCT:
            return True, f"chop (ADX={adx:.1f} EMA_spread={ema_spread_pct:.2f}%)"

        return False, "ok"

    # ── Correlated sector exposure ────────────────────────────────────────────

    def is_sector_blocked(self, symbol: str,
                           current_positions: dict) -> tuple[bool, str]:
        """Block if too many correlated positions already open."""
        for group_name, symbols in SECTOR_GROUPS.items():
            if symbol in symbols:
                sector_count = sum(1 for s in current_positions if s in symbols)
                if sector_count >= MAX_SECTOR_POSITIONS:
                    return True, f"sector_limit_{group_name} ({sector_count}/{MAX_SECTOR_POSITIONS})"
        return False, "ok"

    # ── Trade quality score 0–100 ─────────────────────────────────────────────

    def score_entry(self, action: str, indicators: dict) -> int:
        """Returns quality score. Entry blocked if score < threshold."""
        score = 0

        # ADX strength (max 25)
        adx = indicators.get("adx_1h", 0)
        if adx >= 40:    score += 25
        elif adx >= 30:  score += 20
        elif adx >= 20:  score += 12
        elif adx >= 15:  score += 5

        # Chop index (max 15)
        chop = indicators.get("chop", 50)
        if chop < 38:    score += 15
        elif chop < 45:  score += 10
        elif chop < 55:  score += 5

        # EMA alignment (max 15)
        ema8  = indicators.get("ema_8", 0)
        ema21 = indicators.get("ema_21", 0)
        if action == "long"  and ema8 > ema21: score += 15
        elif action == "short" and ema8 < ema21: score += 15

        # BTC bias alignment (max 15)
        if (action == "long"  and self._btc_bias == "bullish") or \
           (action == "short" and self._btc_bias == "bearish"):
            score += 15
        elif self._btc_bias == "neutral":
            score += 8

        # MACD histogram direction (max 10)
        macd_hist = indicators.get("macd_hist", 0)
        if action == "long"  and macd_hist > 0: score += 10
        elif action == "short" and macd_hist < 0: score += 10

        # RSI not extreme (max 10)
        rsi = indicators.get("rsi_1h", 50)
        if action == "long"  and 40 <= rsi <= 65: score += 10
        elif action == "short" and 35 <= rsi <= 60: score += 10
        elif 30 <= rsi <= 70: score += 5

        # Aroon confirmation (max 10)
        aroonosc = indicators.get("aroonosc", 0)
        if action == "long"  and aroonosc > 30: score += 10
        elif action == "short" and aroonosc < -30: score += 10

        return min(score, 100)

    def get_btc_bias(self) -> str:
        return self._btc_bias

    def btc_impulse_active(self) -> bool:
        return self._btc_impulse_active

    def preferred_direction(self) -> str | None:
        if self._btc_bias == "bullish":  return "long"
        if self._btc_bias == "bearish":  return "short"
        return None

    @staticmethod
    def _ema(values: np.ndarray, period: int) -> float:
        k = 2 / (period + 1)
        ema = float(values[0])
        for v in values[1:]:
            ema = float(v) * k + ema * (1 - k)
        return ema

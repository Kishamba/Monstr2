import numpy as np
import pandas as pd
import talib
from config import StrategyConfig


class Indicators:

    def calculate_all(self, df_1h: pd.DataFrame, df_5m: pd.DataFrame, config: StrategyConfig) -> dict:
        result = {}

        # ── 1H indicators ──────────────────────────────────────────────────────
        close_1h = df_1h['close'].values.astype(float)
        high_1h = df_1h['high'].values.astype(float)
        low_1h = df_1h['low'].values.astype(float)

        adx_arr = talib.ADX(high_1h, low_1h, close_1h, timeperiod=14)
        plus_di = talib.PLUS_DI(high_1h, low_1h, close_1h, timeperiod=14)
        minus_di = talib.MINUS_DI(high_1h, low_1h, close_1h, timeperiod=14)
        rsi_1h = talib.RSI(close_1h, timeperiod=14)
        atr_1h = talib.ATR(high_1h, low_1h, close_1h, timeperiod=14)

        result['adx_1h'] = float(adx_arr[-1])
        result['plus_di_1h'] = float(plus_di[-1])
        result['minus_di_1h'] = float(minus_di[-1])
        result['rsi_1h'] = float(rsi_1h[-1])
        result['atr_1h'] = float(atr_1h[-1])
        result['atr_pct_1h'] = float(atr_1h[-1] / close_1h[-1] * 100)

        # Keltner Channel
        ema_kc = talib.EMA(close_1h, timeperiod=config.kc_period)
        result['keltner_mid'] = float(ema_kc[-1])
        result['keltner_upper'] = float(ema_kc[-1] + config.kc_atr_mult * atr_1h[-1])
        result['keltner_lower'] = float(ema_kc[-1] - config.kc_atr_mult * atr_1h[-1])

        # Choppiness Index (14)
        result['chop'] = self._choppiness(high_1h, low_1h, close_1h, period=14)

        # EMA
        result['ema_8'] = float(talib.EMA(close_1h, timeperiod=8)[-1])
        result['ema_21'] = float(talib.EMA(close_1h, timeperiod=21)[-1])

        # MACD
        macd, macd_signal, macd_hist = talib.MACD(close_1h, fastperiod=12, slowperiod=26, signalperiod=9)
        result['macd'] = float(macd[-1])
        result['macd_signal'] = float(macd_signal[-1])
        result['macd_hist'] = float(macd_hist[-1])

        result['close_1h'] = float(close_1h[-1])

        # ── 5M indicators ──────────────────────────────────────────────────────
        close_5m = df_5m['close'].values.astype(float)
        high_5m = df_5m['high'].values.astype(float)
        low_5m = df_5m['low'].values.astype(float)

        result['adx_5m'] = float(talib.ADX(high_5m, low_5m, close_5m, timeperiod=14)[-1])
        result['rsi_5m'] = float(talib.RSI(close_5m, timeperiod=14)[-1])
        result['atr_5m'] = float(talib.ATR(high_5m, low_5m, close_5m, timeperiod=14)[-1])

        # ── Signal logic (KeltnerAdxChop) ──────────────────────────────────────
        close = result['close_1h']
        adx = result['adx_1h']
        chop = result['chop']

        if (close > result['keltner_upper']
                and adx > config.adx_threshold
                and chop < config.chop_threshold):
            result['signal'] = 'long'
            result['signal_reason'] = (
                f"Пробой Keltner вверх ({close:.2f} > {result['keltner_upper']:.2f}), "
                f"ADX={adx:.1f}>{config.adx_threshold}, Chop={chop:.1f}<{config.chop_threshold}"
            )
        elif (close < result['keltner_lower']
              and adx > config.adx_threshold
              and chop < config.chop_threshold):
            result['signal'] = 'short'
            result['signal_reason'] = (
                f"Пробой Keltner вниз ({close:.2f} < {result['keltner_lower']:.2f}), "
                f"ADX={adx:.1f}>{config.adx_threshold}, Chop={chop:.1f}<{config.chop_threshold}"
            )
        else:
            result['signal'] = 'hold'
            result['signal_reason'] = (
                f"Нет пробоя Keltner или слабый тренд: "
                f"ADX={adx:.1f}, Chop={chop:.1f}"
            )

        return result

    @staticmethod
    def _choppiness(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
        """Choppiness Index = 100 * log10(sum(ATR(1), n) / (highest_high - lowest_low)) / log10(n)"""
        if len(close) < period + 1:
            return 50.0

        atr1 = talib.ATR(high, low, close, timeperiod=1)
        atr_sum = np.sum(atr1[-period:])

        hh = np.max(high[-period:])
        ll = np.min(low[-period:])
        range_hl = hh - ll

        if range_hl == 0:
            return 50.0

        chop = 100.0 * np.log10(atr_sum / range_hl) / np.log10(period)
        return float(np.clip(chop, 0.0, 100.0))

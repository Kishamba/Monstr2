import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    import talib.abstract as ta
except ImportError:
    ta = None


class AroonMacdStrategy:
    """
    AroonMacd — ловит начало нового тренда.

    LONG когда:
      - AroonUp > AroonDown (бычий)
      - AroonUp пересёк AroonDown вверх за последние N свечей
      - AroonOsc > 0 и растёт
      - MACD > Signal
      - MACD пересёк Signal вверх за последние M свечей

    SHORT — зеркально.
    """

    name      = "AroonMacd"
    timeframe = "1h"

    # Параметры из оригинальной оптимизации
    AROON_CROSS_WINDOW = 15   # окно для Aroon crossover
    MACD_CROSS_WINDOW  = 5    # окно для MACD crossover

    def __init__(self, config):
        self.config = config

    def calculate_indicators(self, ohlcv: list) -> dict:
        """
        Рассчитывает Aroon, MACD, ATR из OHLCV данных.
        ohlcv: список [timestamp, open, high, low, close, volume]
        """
        if len(ohlcv) < 50:
            return {}

        closes = np.array([c[4] for c in ohlcv], dtype=float)
        highs  = np.array([c[2] for c in ohlcv], dtype=float)
        lows   = np.array([c[3] for c in ohlcv], dtype=float)

        try:
            if ta:
                df = pd.DataFrame({
                    'open':   [c[1] for c in ohlcv],
                    'high':   highs,
                    'low':    lows,
                    'close':  closes,
                    'volume': [c[5] for c in ohlcv],
                })

                aroon     = ta.AROON(df)
                aroonosc  = ta.AROONOSC(df)
                macd_data = ta.MACD(df)
                atr_data  = ta.ATR(df)

                return {
                    'aroonup':       float(aroon['aroonup'].iloc[-1]),
                    'aroondown':     float(aroon['aroondown'].iloc[-1]),
                    'aroonosc':      float(aroonosc.iloc[-1]),
                    'aroonosc_prev': float(aroonosc.iloc[-2]),
                    'macd':          float(macd_data['macd'].iloc[-1]),
                    'macdsignal':    float(macd_data['macdsignal'].iloc[-1]),
                    'macdhist':      float(macd_data['macdhist'].iloc[-1]),
                    'atr':           float(atr_data.iloc[-1]),
                    # История для crossover
                    'aroonup_hist':   list(aroon['aroonup'].iloc[-self.AROON_CROSS_WINDOW:]),
                    'aroondown_hist': list(aroon['aroondown'].iloc[-self.AROON_CROSS_WINDOW:]),
                    'macd_hist_arr':  list(macd_data['macd'].iloc[-self.MACD_CROSS_WINDOW:]),
                    'macd_sig_arr':   list(macd_data['macdsignal'].iloc[-self.MACD_CROSS_WINDOW:]),
                }
        except Exception as e:
            logger.error(f"AroonMacd indicators error: {e}")

        return {}

    @staticmethod
    def _crossed_above(series_a: list, series_b: list) -> bool:
        """Было ли пересечение A над B в переданном окне."""
        for i in range(1, len(series_a)):
            if series_a[i-1] <= series_b[i-1] and series_a[i] > series_b[i]:
                return True
        return False

    @staticmethod
    def _crossed_below(series_a: list, series_b: list) -> bool:
        """Было ли пересечение A под B в переданном окне."""
        for i in range(1, len(series_a)):
            if series_a[i-1] >= series_b[i-1] and series_a[i] < series_b[i]:
                return True
        return False

    def get_signal(self, ind: dict) -> dict:
        """
        Принимает словарь индикаторов (из calculate_indicators
        или из общего Indicators.calculate_all).
        Возвращает {'action': 'long'|'short'|'hold', ...}
        """
        # Совместимость с ключами calculate_all (macd_signal vs macdsignal)
        aroonup       = ind.get('aroonup',    ind.get('aroon_up',    None))
        aroondown     = ind.get('aroondown',  ind.get('aroon_down',  None))
        aroonosc      = ind.get('aroonosc',   None)
        aroonosc_prev = ind.get('aroonosc_prev', None)
        macd          = ind.get('macd',       None)
        macdsignal    = ind.get('macdsignal', ind.get('macd_signal', None))

        aroonup_hist   = ind.get('aroonup_hist',   [])
        aroondown_hist = ind.get('aroondown_hist',  [])
        macd_hist_arr  = ind.get('macd_hist_arr',  [])
        macd_sig_arr   = ind.get('macd_sig_arr',   [])

        # Если нет данных — hold
        if any(v is None for v in [aroonup, aroondown,
                                    aroonosc, macd, macdsignal]):
            return {'action': 'hold', 'strength': 0.0,
                    'reason': 'no aroon data'}

        # Crossover за окно
        aroon_cross_up = self._crossed_above(
            aroonup_hist, aroondown_hist
        ) if len(aroonup_hist) >= 2 else False

        aroon_cross_down = self._crossed_below(
            aroonup_hist, aroondown_hist
        ) if len(aroonup_hist) >= 2 else False

        macd_cross_up = self._crossed_above(
            macd_hist_arr, macd_sig_arr
        ) if len(macd_hist_arr) >= 2 else False

        macd_cross_down = self._crossed_below(
            macd_hist_arr, macd_sig_arr
        ) if len(macd_hist_arr) >= 2 else False

        # Рост AroonOsc
        osc_growing   = (aroonosc_prev is not None
                         and aroonosc > aroonosc_prev)
        osc_declining = (aroonosc_prev is not None
                         and aroonosc < aroonosc_prev)

        # ── LONG ──────────────────────────────────────────────────────────────
        long_conditions = [
            aroonup   > aroondown,     # бычий Aroon
            aroon_cross_up,            # Aroon пересёк вверх
            aroonosc  > 0,             # осциллятор положительный
            osc_growing,               # и растёт
            macd      > macdsignal,    # MACD бычий
            macd_cross_up,             # MACD пересёк вверх
        ]
        long_score = sum(long_conditions)

        if long_score >= 5:
            strength = round(long_score / 6, 3)
            return {
                'action':   'long',
                'strength': strength,
                'reason':   (
                    f"AroonMacd LONG: "
                    f"aroon={aroonup:.0f}/{aroondown:.0f} "
                    f"osc={aroonosc:.0f} "
                    f"macd={macd:.4f} "
                    f"score={long_score}/6"
                ),
            }

        # ── SHORT ─────────────────────────────────────────────────────────────
        short_conditions = [
            aroondown > aroonup,       # медвежий Aroon
            aroon_cross_down,          # Aroon пересёк вниз
            aroonosc  < 0,             # осциллятор отрицательный
            osc_declining,             # и падает
            macd      < macdsignal,    # MACD медвежий
            macd_cross_down,           # MACD пересёк вниз
        ]
        short_score = sum(short_conditions)

        if short_score >= 5:
            strength = round(short_score / 6, 3)
            return {
                'action':   'short',
                'strength': strength,
                'reason':   (
                    f"AroonMacd SHORT: "
                    f"aroon={aroonup:.0f}/{aroondown:.0f} "
                    f"osc={aroonosc:.0f} "
                    f"macd={macd:.4f} "
                    f"score={short_score}/6"
                ),
            }

        return {
            'action':   'hold',
            'strength': 0.0,
            'reason':   (
                f"AroonMacd HOLD: "
                f"long={long_score}/6 short={short_score}/6"
            ),
        }

    def calculate_exit_levels(self, action: str,
                               entry_price: float,
                               ind: dict,
                               strategy_config) -> dict:
        """SL/TP через ATR как в оригинале."""
        atr      = ind.get('atr', ind.get('atr_1h', 0))
        atr_mult = getattr(strategy_config, 'atr_mult', 2.5)
        rr       = getattr(strategy_config, 'risk_ratio', 2.0)

        if atr <= 0:
            atr = entry_price * 0.005  # fallback 0.5%

        sl_dist = atr_mult * atr
        tp_dist = sl_dist * rr

        if action == 'long':
            sl = entry_price - sl_dist
            tp = entry_price + tp_dist
        else:
            sl = entry_price + sl_dist
            tp = entry_price - tp_dist

        actual_rr = tp_dist / sl_dist if sl_dist > 0 else 0

        return {
            'sl':        round(sl, 6),
            'tp':        round(tp, 6),
            'actual_rr': round(actual_rr, 2),
        }

from config import StrategyConfig


class EMAMACDStrategy:

    name = "EMA_MACD"
    timeframe = "1h"

    def __init__(self, config: StrategyConfig):
        self.config = config

    def get_signal(self, indicators: dict) -> dict:
        ema_8 = indicators.get('ema_8', 0)
        ema_21 = indicators.get('ema_21', 0)
        macd = indicators.get('macd', 0)
        macd_signal = indicators.get('macd_signal', 0)
        macd_hist = indicators.get('macd_hist', 0)
        rsi = indicators.get('rsi_1h', 50)
        adx = indicators.get('adx_1h', 0)

        if (ema_8 > ema_21
                and macd > macd_signal
                and macd_hist > 0
                and rsi > 45):
            ema_margin = (ema_8 - ema_21) / ema_21 if ema_21 else 0
            hist_margin = macd_hist / abs(macd) if macd else 0
            strength = float(min(1.0, (ema_margin * 100 + abs(hist_margin)) / 2))
            return {
                'action': 'long',
                'reason': (
                    f"EMA8({ema_8:.4f})>EMA21({ema_21:.4f}), "
                    f"MACD hist={macd_hist:.4f}>0, RSI={rsi:.0f}"
                ),
                'strength': round(strength, 3),
            }

        if (ema_8 < ema_21
                and macd < macd_signal
                and macd_hist < 0
                and rsi < 55):
            ema_margin = (ema_21 - ema_8) / ema_21 if ema_21 else 0
            hist_margin = abs(macd_hist) / abs(macd) if macd else 0
            strength = float(min(1.0, (ema_margin * 100 + abs(hist_margin)) / 2))
            return {
                'action': 'short',
                'reason': (
                    f"EMA8({ema_8:.4f})<EMA21({ema_21:.4f}), "
                    f"MACD hist={macd_hist:.4f}<0, RSI={rsi:.0f}"
                ),
                'strength': round(strength, 3),
            }

        return {
            'action': 'hold',
            'reason': (
                f"EMA8={'>' if ema_8 > ema_21 else '<'}EMA21, "
                f"MACD_hist={macd_hist:.4f}, RSI={rsi:.0f}"
            ),
            'strength': 0.0,
        }

class RSIMomentumStrategy:
    name = "RSI_Momentum"
    timeframe = "1h"

    def __init__(self, config):
        self.config = config

    def get_signal(self, indicators: dict) -> dict:
        rsi_1h = indicators.get('rsi_1h', 50)
        rsi_5m = indicators.get('rsi_5m', 50)
        adx    = indicators.get('adx_1h', 0)
        macd_h = indicators.get('macd_hist', 0)
        ema_8  = indicators.get('ema_8', 0)
        ema_21 = indicators.get('ema_21', 0)

        # LONG: RSI выходит из перепроданности + тренд
        if (rsi_1h > 35 and rsi_1h < 60
                and rsi_5m > rsi_1h
                and adx > 15
                and macd_h > 0
                and ema_8 > ema_21):
            strength = min(1.0, (rsi_5m - rsi_1h) / 10 + adx / 100)
            return {
                'action': 'long',
                'reason': (
                    f"RSI_momentum: 1h={rsi_1h:.0f} "
                    f"5m={rsi_5m:.0f} ADX={adx:.0f}"
                ),
                'strength': round(strength, 3),
            }

        # SHORT: RSI падает из перекупленности + тренд
        if (rsi_1h < 65 and rsi_1h > 40
                and rsi_5m < rsi_1h
                and adx > 15
                and macd_h < 0
                and ema_8 < ema_21):
            strength = min(1.0, (rsi_1h - rsi_5m) / 10 + adx / 100)
            return {
                'action': 'short',
                'reason': (
                    f"RSI_momentum: 1h={rsi_1h:.0f} "
                    f"5m={rsi_5m:.0f} ADX={adx:.0f}"
                ),
                'strength': round(strength, 3),
            }

        return {
            'action': 'hold',
            'reason': f"RSI={rsi_1h:.0f} ADX={adx:.0f}",
            'strength': 0.0,
        }

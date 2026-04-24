from config import StrategyConfig


class KeltnerAdxStrategy:

    name = "KeltnerAdxChop"
    timeframe = "1h"

    def __init__(self, config: StrategyConfig):
        self.config = config

    def get_signal(self, indicators: dict) -> dict:
        action = indicators.get('signal', 'hold')
        reason = indicators.get('signal_reason', '')

        # Signal strength based on how far ADX and Chop are from thresholds
        adx = indicators.get('adx_1h', 0)
        chop = indicators.get('chop', 50)

        adx_margin = (adx - self.config.adx_threshold) / self.config.adx_threshold
        chop_margin = (self.config.chop_threshold - chop) / self.config.chop_threshold
        strength = float(max(0.0, min(1.0, (adx_margin + chop_margin) / 2)))

        return {
            'action': action,
            'reason': reason,
            'strength': round(strength, 3),
        }

    def calculate_exit_levels(
        self,
        action: str,
        entry_price: float,
        indicators: dict,
        config: StrategyConfig,
        position_size: float = 1.0,
    ) -> dict:
        atr = indicators.get('atr_1h', 0)
        sl_distance = atr * config.atr_mult
        tp_distance = sl_distance * config.risk_ratio

        if action == 'long':
            sl = entry_price - sl_distance
            tp = entry_price + tp_distance
        elif action == 'short':
            sl = entry_price + sl_distance
            tp = entry_price - tp_distance
        else:
            return {
                'sl': entry_price,
                'tp': entry_price,
                'risk_dollar': 0.0,
                'reward_dollar': 0.0,
                'actual_rr': 0.0,
            }

        risk_dollar = abs(entry_price - sl) * position_size
        reward_dollar = abs(entry_price - tp) * position_size
        actual_rr = reward_dollar / risk_dollar if risk_dollar > 0 else 0.0

        return {
            'sl': round(sl, 6),
            'tp': round(tp, 6),
            'risk_dollar': round(risk_dollar, 4),
            'reward_dollar': round(reward_dollar, 4),
            'actual_rr': round(actual_rr, 3),
        }

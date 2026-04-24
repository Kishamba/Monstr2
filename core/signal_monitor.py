import logging

logger = logging.getLogger(__name__)


class SignalMonitor:
    """
    Следит за консенсусом сигналов по всем парам.
    Если рынок разворачивается — говорит боту закрыть
    противоположные позиции и открыть новые.
    """

    def __init__(self, flip_threshold: float = 0.65):
        self.flip_threshold = flip_threshold
        self.market_bias = 'neutral'  # 'long' | 'short' | 'neutral'
        self.bias_strength = 0.0
        self.history = []             # последние 3 состояния

    def update(self, all_signals: list) -> dict:
        """
        Принимает список сигналов по всем парам.
        Каждый элемент: {'symbol': str, 'action': str, 'strength': float}
        Возвращает: {'bias': str, 'strength': float, 'flip': bool}
        """
        if not all_signals:
            return {'bias': 'neutral', 'strength': 0.0, 'flip': False,
                    'long_count': 0, 'short_count': 0, 'total': 0}

        total       = len(all_signals)
        long_count  = sum(1 for s in all_signals if s['action'] == 'long')
        short_count = sum(1 for s in all_signals if s['action'] == 'short')
        hold_count  = total - long_count - short_count

        long_ratio  = long_count  / total
        short_ratio = short_count / total

        if long_ratio >= self.flip_threshold:
            new_bias     = 'long'
            new_strength = long_ratio
        elif short_ratio >= self.flip_threshold:
            new_bias     = 'short'
            new_strength = short_ratio
        else:
            new_bias     = 'neutral'
            new_strength = max(long_ratio, short_ratio)

        old_bias = self.market_bias
        flip = (
            old_bias != 'neutral'
            and new_bias != 'neutral'
            and old_bias != new_bias
        )

        if flip:
            logger.info(
                f"MARKET FLIP: {old_bias.upper()} -> "
                f"{new_bias.upper()} "
                f"({long_count}L/{short_count}S/{hold_count}H "
                f"из {total} пар)"
            )

        self.market_bias   = new_bias
        self.bias_strength = new_strength
        self.history.append(new_bias)
        if len(self.history) > 3:
            self.history.pop(0)

        logger.info(
            f"Market bias: {new_bias.upper()} "
            f"strength={new_strength:.0%} "
            f"({long_count}L/{short_count}S/{hold_count}H)"
        )

        return {
            'bias':        new_bias,
            'strength':    new_strength,
            'flip':        flip,
            'long_count':  long_count,
            'short_count': short_count,
            'total':       total,
        }

    def should_close_position(self, position_side: str) -> bool:
        """Закрыть ли позицию если рынок развернулся против неё?"""
        if self.market_bias == 'neutral':
            return False
        if self.bias_strength < self.flip_threshold:
            return False
        if position_side == 'short' and self.market_bias == 'long':
            return True
        if position_side == 'long' and self.market_bias == 'short':
            return True
        return False

    def check_5m_conflict(self, indicators: dict,
                           action: str) -> bool:
        """Возвращает True если 5m RSI противоречит направлению входа."""
        rsi_5m = indicators.get('rsi_5m', 50)
        rsi_1h = indicators.get('rsi_1h', 50)

        if action == 'long':
            if rsi_5m < 40 and rsi_5m < rsi_1h - 10:
                return True

        if action == 'short':
            if rsi_5m > 60 and rsi_5m > rsi_1h + 10:
                return True

        return False

    def is_entry_allowed(self, action: str) -> bool:
        """Разрешён ли вход в данном направлении?"""
        if self.market_bias == 'neutral':
            return True
        if self.bias_strength < self.flip_threshold:
            return True
        if action == 'short' and self.market_bias == 'long':
            logger.info(
                f"Entry blocked: trying SHORT but market=LONG "
                f"({self.bias_strength:.0%})"
            )
            return False
        if action == 'long' and self.market_bias == 'short':
            logger.info(
                f"Entry blocked: trying LONG but market=SHORT "
                f"({self.bias_strength:.0%})"
            )
            return False
        return True

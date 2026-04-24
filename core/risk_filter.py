import logging
from datetime import datetime, timezone
from config import TradingConfig

logger = logging.getLogger(__name__)


class RiskFilter:

    def __init__(self, config: TradingConfig):
        self.config = config
        self.consecutive_sl: int = 0
        self.sl_cooldown: dict = {}  # symbol → datetime

    def check(
        self,
        signal: dict,
        indicators: dict,
        current_positions: dict,
        daily_pnl: float,
        symbol: str = '',
    ) -> tuple[bool, str]:

        action = signal.get('action', 'hold')

        # 1. No signal
        if action == 'hold':
            logger.debug("RiskFilter blocked: no_signal")
            return False, "no_signal"

        # 2. Bad hour
        current_hour = datetime.now(timezone.utc).hour
        if current_hour in self.config.bad_hours_utc:
            logger.info(f"RiskFilter blocked: bad_hour (UTC {current_hour})")
            return False, "bad_hour"

        # 3. Daily loss limit
        capital = self.config.shadow_capital
        daily_loss_pct = (daily_pnl / capital * 100) if capital > 0 else 0
        if daily_loss_pct <= -self.config.max_daily_loss_pct:
            logger.info(f"RiskFilter blocked: daily_loss_limit ({daily_loss_pct:.2f}%)")
            return False, "daily_loss_limit"

        # 4. Max concurrent positions
        if len(current_positions) >= self.config.max_concurrent_positions:
            logger.info("RiskFilter blocked: max_positions")
            return False, "max_positions"

        # 5. Consecutive SL streak
        if self.consecutive_sl >= 5:
            logger.info(f"RiskFilter blocked: sl_streak_pause ({self.consecutive_sl} SL in a row)")
            return False, "sl_streak_pause"

        # 5б. Cooldown на пару после SL — 3 часа
        if symbol and symbol in self.sl_cooldown:
            elapsed = (
                datetime.now(timezone.utc) - self.sl_cooldown[symbol]
            ).total_seconds() / 3600
            if elapsed < 3.0:
                remaining = 3.0 - elapsed
                logger.info(
                    f"RiskFilter blocked: sl_cooldown {symbol} "
                    f"({remaining:.1f}h left)"
                )
                return False, f"sl_cooldown_{symbol}"

        # 6. ADX too low
        adx = indicators.get('adx_1h', 0)
        if adx < 15:
            logger.info(f"RiskFilter blocked: adx_too_low ({adx:.1f})")
            return False, "adx_too_low"

        return True, "ok"

    def record_sl(self):
        self.consecutive_sl += 1

    def record_sl_for_symbol(self, symbol: str):
        self.sl_cooldown[symbol] = datetime.now(timezone.utc)
        self.consecutive_sl += 1
        logger.info(
            f"SL recorded: {symbol} cooldown 3h | "
            f"streak={self.consecutive_sl}"
        )

    def record_win(self):
        self.consecutive_sl = 0

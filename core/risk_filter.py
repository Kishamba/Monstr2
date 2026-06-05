import logging
from datetime import datetime, timezone
from config import TradingConfig

logger = logging.getLogger(__name__)

# Phase5: correlated sector exposure control
SECTOR_GROUPS: dict[str, set] = {
    'layer1':   {'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'SUI/USDT:USDT'},
    'payments': {'DOGE/USDT:USDT', 'XRP/USDT:USDT'},
    'bluechip': {'ETH/USDT:USDT'},
}
MAX_SECTOR_POSITIONS = 2


class RiskFilter:

    def __init__(self, config: TradingConfig, adx_threshold: float = 15.0):
        self.config = config
        self.adx_threshold = adx_threshold
        self.consecutive_sl: int = 0
        self.sl_cooldown: dict = {}         # symbol → datetime
        self.stagnation_cooldown: dict = {} # symbol → datetime
        self.stagnation_streak: dict = {}   # symbol → count

    def check(
        self,
        signal: dict,
        indicators: dict,
        current_positions: dict,
        daily_pnl: float,
        symbol: str = '',
        btc_trend: str = 'sideways',
    ) -> tuple[bool, str]:

        action = signal.get('action', 'hold')

        # 1. No signal
        if action == 'hold':
            logger.debug("RiskFilter blocked: no_signal")
            return False, "no_signal"

        # 2. BTC macro filter — главный фильтр
        if btc_trend == 'up' and action == 'short':
            logger.info(f"RiskFilter blocked: btc_trend_up_no_short")
            return False, "btc_trend_up_no_short"
        if btc_trend == 'down' and action == 'long':
            logger.info(f"RiskFilter blocked: btc_trend_down_no_long")
            return False, "btc_trend_down_no_long"

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

        # 5в. Cooldown после повторных стагнаций — 1 час
        if symbol and symbol in self.stagnation_cooldown:
            elapsed = (
                datetime.now(timezone.utc) - self.stagnation_cooldown[symbol]
            ).total_seconds() / 3600
            if elapsed < 1.0:
                remaining = 1.0 - elapsed
                logger.info(
                    f"RiskFilter blocked: stagnation_cooldown {symbol} "
                    f"({remaining:.1f}h left)"
                )
                return False, f"stagnation_cooldown_{symbol}"
            else:
                del self.stagnation_cooldown[symbol]
                self.stagnation_streak[symbol] = 0

        # 5г. Phase5: sector exposure — max 2 correlated alts simultaneously
        if symbol:
            for group_name, symbols in SECTOR_GROUPS.items():
                if symbol in symbols:
                    sector_count = sum(1 for s in current_positions if s in symbols)
                    if sector_count >= MAX_SECTOR_POSITIONS:
                        logger.info(
                            f"RiskFilter blocked: sector_limit {group_name} "
                            f"({sector_count}/{MAX_SECTOR_POSITIONS})"
                        )
                        return False, f"sector_limit_{group_name}"

        # 6. ADX too low — берём лучший из доступных таймфреймов
        adx_1h  = indicators.get('adx_1h',  0)
        adx_15m = indicators.get('adx_15m', 0)
        adx     = max(adx_1h, adx_15m)
        if adx < self.adx_threshold:
            logger.info(
                f"RiskFilter blocked: adx_too_low "
                f"(1h={adx_1h:.1f} 15m={adx_15m:.1f})"
            )
            return False, f"adx_too_low (1h={adx_1h:.1f} 15m={adx_15m:.1f})"

        return True, "ok"

    def record_close(self, exit_reason: str, symbol: str = ''):
        if exit_reason == 'SL':
            self.consecutive_sl += 1
            if symbol:
                self.sl_cooldown[symbol] = datetime.now(timezone.utc)
            self.stagnation_streak[symbol] = 0
            logger.info(
                f"SL streak: {self.consecutive_sl} | cooldown: {symbol}"
            )
        elif exit_reason == 'Breakeven_Stagnation':
            self.consecutive_sl = 0
            streak = self.stagnation_streak.get(symbol, 0) + 1
            self.stagnation_streak[symbol] = streak
            if streak >= 2:
                self.stagnation_cooldown[symbol] = datetime.now(timezone.utc)
                logger.info(
                    f"STAGNATION cooldown: {symbol} "
                    f"({streak} stagnations in a row)"
                )
        else:
            self.consecutive_sl = 0
            if symbol:
                self.stagnation_streak[symbol] = 0

    # Обратная совместимость
    def record_sl(self):
        self.consecutive_sl += 1

    def record_sl_for_symbol(self, symbol: str):
        self.record_close('SL', symbol)

    def record_win(self):
        self.consecutive_sl = 0

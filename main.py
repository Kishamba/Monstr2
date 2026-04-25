import asyncio
import logging
import os
import sys
from datetime import date, timezone

from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join('data', 'logs', 'monster.log'), encoding='utf-8'),
    ],
)
logger = logging.getLogger('main')

from config import get_config
from core.data_feed import DataFeed
from core.indicators import Indicators
from core.risk_filter import RiskFilter
from core.notifier import Notifier
from core.position_manager import PositionManager
from core.telegram_commands import TelegramCommands
from core.signal_monitor import SignalMonitor
from core.direction_filter import is_direction_allowed
from strategies.keltner_adx import KeltnerAdxStrategy
from strategies.ema_macd import EMAMACDStrategy
from strategies.aroon_macd import AroonMacdStrategy


async def main():
    os.makedirs(os.path.join('data', 'logs'), exist_ok=True)

    config, strategy_config = get_config()
    notifier = Notifier()
    data_feed = DataFeed(config)
    indicators_calc = Indicators()
    risk_filter = RiskFilter(config)
    strategy_keltner = KeltnerAdxStrategy(strategy_config)
    strategy_ema = EMAMACDStrategy(strategy_config)
    strategy_aroon = AroonMacdStrategy(strategy_config)
    position_manager = PositionManager(config, data_feed, notifier)
    signal_monitor = SignalMonitor(flip_threshold=0.65)

    # Telegram command handler (runs in background)
    tg_commands = TelegramCommands(
        position_manager, notifier,
        data_feed=data_feed,
        indicators_calc=indicators_calc,
        strategy_config=strategy_config,
    )
    asyncio.create_task(tg_commands.run())

    mode_label = "SHADOW" if config.trading_mode == 'shadow' else "LIVE"
    await notifier.send_message(
        f"🚀 <b>Monster 2.1</b>\n"
        f"Стратегии: Keltner + EMA_MACD + AroonMacd\n"
        f"Режим: {mode_label} | Капитал: ${config.shadow_capital:,.2f}\n"
        f"Пары: {', '.join(s.replace('/USDT:USDT','') for s in config.symbols)}"
    )
    logger.info(f"Monster 2.1 started in {mode_label} mode (Keltner + EMA_MACD + AroonMacd)")

    heartbeat_counter = 0
    current_day = date.today()

    while True:
        # ── Stop flag (set by /stop command) ──────────────────────────────────
        if getattr(position_manager, 'stop_requested', False):
            logger.info("Stop requested via Telegram, shutting down...")
            await notifier.send_message("🛑 Monster 2.0 остановлен по команде /stop")
            break

        # ── Daily reset at UTC midnight ────────────────────────────────────────
        import datetime as _dt
        today_utc = _dt.datetime.now(timezone.utc).date()
        if today_utc != current_day:
            current_day = today_utc
            position_manager.daily_pnl = 0.0
            risk_filter.consecutive_sl = 0
            logger.info("Daily reset: P&L и consecutive_sl сброшены")
            await notifier.send_message("🌅 Новый торговый день начат")

        try:
            # ── Собираем сигналы по всем парам для монитора ──────────────
            all_pair_signals = []
            for sym in config.symbols:
                try:
                    mdata = await data_feed.get_all_data(sym)
                    ind = indicators_calc.calculate_all(
                        mdata['ohlcv_1h'], mdata['ohlcv_5m'], strategy_config
                    )
                    sk = strategy_keltner.get_signal(ind)
                    se = strategy_ema.get_signal(ind)
                    sa = strategy_aroon.get_signal(ind)
                    active = [(s, n) for s, n in
                              [(sk, 'K'), (se, 'E'), (sa, 'A')]
                              if s['action'] != 'hold']
                    if active:
                        best, _ = max(active, key=lambda x: x[0]['strength'])
                        all_pair_signals.append({
                            'symbol':   sym,
                            'action':   best['action'],
                            'strength': best['strength'],
                        })
                    else:
                        all_pair_signals.append({
                            'symbol': sym, 'action': 'hold', 'strength': 0.0,
                        })
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

            market_state = signal_monitor.update(all_pair_signals)

            # ── Досрочное закрытие при флипе рынка ──────────────────────
            if market_state['flip'] and position_manager.positions:
                for sym, pos in list(position_manager.positions.items()):
                    if signal_monitor.should_close_position(pos['side']):
                        ticker = await data_feed.get_ticker(sym)
                        price  = ticker['price']
                        logger.info(
                            f"FLIP CLOSE: {pos['side'].upper()} {sym} "
                            f"@ {price} — market flipped to "
                            f"{market_state['bias'].upper()}"
                        )
                        await position_manager._close_position(
                            sym, pos, price,
                            f"FlipClose_{market_state['bias'].upper()}"
                        )
                await notifier.send_message(
                    f"🔄 <b>Рынок развернулся!</b>\n"
                    f"Направление: "
                    f"{'📈 LONG' if market_state['bias'] == 'long' else '📉 SHORT'}\n"
                    f"Сила: {market_state['strength']:.0%}\n"
                    f"Закрыты позиции против тренда"
                )

            for symbol in config.symbols:

                # Stop check inside symbol loop
                if getattr(position_manager, 'stop_requested', False):
                    break

                # 1. Получить данные
                logger.debug(f"Fetching data for {symbol}")
                market_data = await data_feed.get_all_data(symbol)

                # 2. Посчитать индикаторы
                indicators = indicators_calc.calculate_all(
                    market_data['ohlcv_1h'],
                    market_data['ohlcv_5m'],
                    strategy_config,
                )

                # 3. Получить сигналы от трёх стратегий
                sig_keltner = strategy_keltner.get_signal(indicators)
                sig_ema     = strategy_ema.get_signal(indicators)
                sig_aroon   = strategy_aroon.get_signal(indicators)

                all_signals = [
                    (sig_keltner, 'KeltnerAdxChop'),
                    (sig_ema,     'EMA_MACD'),
                    (sig_aroon,   'AroonMacd'),
                ]

                long_votes  = sum(1 for s, _ in all_signals if s['action'] == 'long')
                short_votes = sum(1 for s, _ in all_signals if s['action'] == 'short')
                active = [(s, n) for s, n in all_signals if s['action'] != 'hold']

                if active:
                    best_sig, best_name = max(active, key=lambda x: x[0]['strength'])
                    signal = {**best_sig, 'source': best_name}
                    votes = long_votes if signal['action'] == 'long' else short_votes
                    if votes >= 2:
                        signal['strength'] = min(1.0, signal['strength'] * 1.5)
                        signal['source'] = f"CONSENSUS_{votes}/3"
                        signal['confirmed'] = True
                else:
                    signal = {
                        'action': 'hold',
                        'reason': 'no signal from any strategy',
                        'strength': 0.0,
                        'source': 'none',
                    }

                logger.info(
                    f"{symbol}: {signal['action'].upper()} "
                    f"src={signal['source']} "
                    f"str={signal.get('strength', 0):.2f} "
                    f"votes=L{long_votes}/S{short_votes} "
                    f"ADX={indicators.get('adx_1h', 0):.0f} "
                    f"RSI={indicators.get('rsi_1h', 0):.0f}"
                )

                # 4а. Фильтр по направлению на основе исторических данных
                if not is_direction_allowed(symbol, signal['action']):
                    logger.info(
                        f"{symbol}: direction blocked — "
                        f"{signal['action']} not allowed for this pair"
                    )
                    await asyncio.sleep(2)
                    continue

                # 4б. Проверка конфликта 5m vs 1h
                if signal_monitor.check_5m_conflict(
                        indicators, signal['action']):
                    logger.info(
                        f"{symbol}: 5m conflict — "
                        f"action={signal['action']} "
                        f"rsi_5m={indicators.get('rsi_5m',0):.0f} "
                        f"rsi_1h={indicators.get('rsi_1h',0):.0f}"
                    )
                    await asyncio.sleep(2)
                    continue

                # 4в. При нейтральном рынке требуем консенсус 2/3
                if (signal_monitor.market_bias == 'neutral'
                        and 'CONSENSUS' not in signal.get('source', '')
                        and signal.get('source') != 'KeltnerAdxChop'):
                    logger.info(
                        f"{symbol}: NEUTRAL market requires CONSENSUS — "
                        f"src={signal.get('source')} skip"
                    )
                    await asyncio.sleep(2)
                    continue

                # 4. Проверяем разрешение монитора рынка
                if not signal_monitor.is_entry_allowed(signal['action']):
                    logger.info(
                        f"{symbol}: blocked by signal_monitor — "
                        f"market={signal_monitor.market_bias}"
                    )
                    await asyncio.sleep(3)
                    continue

                # 5. Жёсткий фильтр Python
                allowed, reason = risk_filter.check(
                    signal,
                    indicators,
                    position_manager.positions,
                    position_manager.daily_pnl,
                    symbol=symbol,
                )

                if not allowed:
                    logger.info(f"{symbol}: blocked by risk_filter — {reason}")
                    await asyncio.sleep(3)
                    continue

                if signal.get('strength', 0) < 0.3:
                    logger.info(
                        f"{symbol}: signal too weak "
                        f"str={signal.get('strength', 0):.2f}, skip"
                    )
                    await asyncio.sleep(3)
                    continue

                if indicators.get('adx_1h', 0) < 20:
                    logger.info(
                        f"{symbol}: ADX too low "
                        f"adx={indicators.get('adx_1h', 0):.0f}, skip"
                    )
                    await asyncio.sleep(3)
                    continue

                # 6. Рассчитать уровни (используем стратегию-источник)
                entry_price = market_data['ticker']['price']
                if signal['source'] in ('KeltnerAdxChop', 'BOTH'):
                    levels = strategy_keltner.calculate_exit_levels(
                        signal['action'], entry_price, indicators, strategy_config)
                else:
                    levels = strategy_keltner.calculate_exit_levels(
                        signal['action'], entry_price, indicators, strategy_config)

                decision = {
                    'action': signal['action'],
                    'sl_price': levels['sl'],
                    'tp_price': levels['tp'],
                    'risk_pct': 1.0,
                    'confidence': 'medium',
                    'reason': (
                        f"{signal.get('source', '?')} | "
                        f"ADX={indicators.get('adx_1h', 0):.0f} "
                        f"RSI={indicators.get('rsi_1h', 0):.0f} "
                        f"str={signal.get('strength', 0):.2f}"
                    ),
                    'entry_price': market_data['ticker']['price'],
                    'indicators': indicators,
                }

                logger.info(
                    f"{symbol}: Direct signal={signal['action']} "
                    f"src={signal.get('source', '?')} "
                    f"ADX={indicators.get('adx_1h', 0):.0f}"
                )

                # 8. Исполнить прямой подтверждённый сигнал
                if decision['action'] != 'hold':
                    decision['_indicators'] = indicators
                    await position_manager.open_position(
                        symbol, decision, position_manager.capital
                    )

            # 8. Проверить открытые позиции
            if position_manager.positions:
                prices = {}
                for sym in list(position_manager.positions.keys()):
                    ticker = await data_feed.get_ticker(sym)
                    prices[sym] = ticker['price']
                closed = await position_manager.check_positions(prices)

                for trade in closed:
                    reason = trade.get('exit_reason', 'unknown')
                    if reason == 'SL':
                        risk_filter.record_sl_for_symbol(
                            trade.get('symbol', '')
                        )
                    else:
                        risk_filter.record_win()

            # 9. Heartbeat каждый час (~40 итераций по 90 сек)
            heartbeat_counter += 1
            if heartbeat_counter >= 40:
                heartbeat_counter = 0
                today_str = _dt.date.today().isoformat()
                trades_today = [
                    t for t in position_manager.recent_trades
                    if t.get('timestamp', '').startswith(today_str)
                ]
                win_today  = sum(1 for t in trades_today if t.get('pnl_value', 0) > 0)
                loss_today = sum(1 for t in trades_today if t.get('pnl_value', 0) <= 0)
                await notifier.send_heartbeat({
                    'capital':        position_manager.capital,
                    'daily_pnl':      position_manager.daily_pnl,
                    'open_positions': len(position_manager.positions),
                    'trades_today':   len(trades_today),
                    'win_today':      win_today,
                    'loss_today':     loss_today,
                    'market_bias':    signal_monitor.market_bias,
                    'bias_strength':  signal_monitor.bias_strength,
                })

            await asyncio.sleep(90)

        except asyncio.CancelledError:
            logger.info("Main loop cancelled, shutting down...")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}", exc_info=True)
            await notifier.send_error(str(e))
            await asyncio.sleep(30)

    await tg_commands.stop()
    await data_feed.close()
    logger.info("Monster 2.0 stopped")


if __name__ == "__main__":
    asyncio.run(main())

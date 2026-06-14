import logging
import os
from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


class Notifier:

    def __init__(self):
        token = os.getenv('TELEGRAM_TOKEN', '')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
        self.bot = Bot(token=token)
        self.enabled = bool(
            token and self.chat_id
            and self.chat_id != 'ВСТАВИТЬ_ID'
        )

    async def _send(self, text: str) -> None:
        if not self.enabled:
            logger.info(f"[TELEGRAM disabled] {text[:80]}")
            return
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode='HTML',
            )
        except TelegramError as e:
            logger.error(f"Telegram send error: {e}")

    async def send_message(self, text: str) -> None:
        await self._send(text)

    async def send_signal(self, symbol: str, decision: dict,
                          indicators: dict, capital: float = 0,
                          position_size: float = 0,
                          notional: float = 0,
                          leverage: int = 10) -> None:
        """Уведомление об открытии позиции."""
        action   = decision.get('action', '?').upper()
        entry    = decision.get('entry_price', 0)
        sl       = float(decision.get('sl_price', 0))
        tp       = float(decision.get('tp_price', 0))
        conf     = decision.get('confidence', '—')
        reason   = decision.get('reason', '—')
        source   = decision.get('source', '—')

        icon      = "📈" if action == "LONG" else "📉"
        conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(conf, "⚪")

        # Расчёт %
        sl_pct = abs(entry - sl) / entry * 100 if entry else 0
        tp_pct = abs(entry - tp) / entry * 100 if entry else 0
        rr     = tp_pct / sl_pct if sl_pct > 0 else 0

        # Реальные расчёты
        risk_dollar   = capital * 0.01       # 1% капитала = реальный риск
        margin        = notional / leverage  # маржа = реально заморожено
        remaining     = capital - margin     # остаток после заморозки маржи

        text = (
            f"{icon} <b>ВХОД: {symbol} {action}</b>\n"
            f"{'─' * 30}\n"
            f"💰 Цена входа:   <code>${entry:,.4f}</code>\n"
            f"🛑 No-red стоп:  <code>${sl:,.4f}</code>\n"
            f"🎯 Выход:        <b>динамический trailing</b>\n"
            f"🔒 Правило:      стоп только вверх, назад не двигается\n"
            f"{'─' * 30}\n"
            f"💵 Капитал:      <code>${capital:,.2f}</code>\n"
            f"📐 Ставка:       <code>${margin:,.2f}</code> "
            f"× {leverage} = <code>${notional:,.2f}</code>\n"
            f"⚠️ Риск:         выход при уходе ниже входа\n"
            f"🏦 Остаток:      <code>${remaining:,.2f}</code>\n"
            f"{'─' * 30}\n"
            f"🧠 Стратегия:    {source or 'Monster 2.2 No-Red Trailing'}\n"
            f"{conf_icon} Уверенность:  {conf}\n"
            f"📝 {reason[:120]}\n"
        )

        if indicators:
            adx  = indicators.get('adx_1h', 0)
            chop = indicators.get('chop', 0)
            rsi  = indicators.get('rsi_1h', 0)
            text += (
                f"{'─' * 30}\n"
                f"📉 ADX: {adx:.0f}  "
                f"Chop: {chop:.0f}  "
                f"RSI: {rsi:.0f}"
            )

        await self._send(text)

    async def send_close(self, trade: dict,
                         capital_after: float = 0) -> None:
        """Уведомление о закрытии позиции."""
        pnl      = trade.get('pnl_value', 0)
        pnl_pct  = trade.get('pnl_pct', 0)
        exit_p   = trade.get('exit_price') or trade.get('exit', 0)
        reason   = trade.get('exit_reason', '—')
        duration = trade.get('duration_minutes', 0)
        notional = trade.get('notional', 0)
        leverage = trade.get('leverage', 10)
        entry    = trade.get('entry', 0)

        icon   = "✅" if pnl >= 0 else "❌"
        sign   = "+" if pnl >= 0 else ""
        result = "ПРОФИТ" if pnl >= 0 else "УБЫТОК"

        reason_icon = {
            'TP':          '🎯',
            'SL':          '🛑',
            'No_Red_Trailing_SL': '🔒',
            'No_Red_Emergency_Exit': '🛑',
            'Timeout_24h': '⏰',
            'manual':      '👋',
        }.get(reason, '📌')

        # Форматируем время
        hours   = int(duration // 60)
        minutes = int(duration % 60)
        dur_str = f"{hours}ч {minutes}м" if hours > 0 else f"{minutes}м"

        text = (
            f"{icon} <b>ЗАКРЫТА: {trade.get('symbol')} "
            f"{trade.get('side','').upper()}</b>\n"
            f"{'─' * 30}\n"
            f"📥 Вход:   <code>${entry:,.4f}</code>\n"
            f"📤 Выход:  <code>${exit_p:,.4f}</code>\n"
            f"{'─' * 30}\n"
            f"💰 P&L:    <b>{sign}{pnl:.2f}$  "
            f"({sign}{pnl_pct:.2f}%)</b>\n"
            f"💼 Объём:  <code>${notional:,.2f}</code> "
            f"(×{leverage})\n"
        )

        if capital_after > 0:
            text += f"💵 Баланс: <code>${capital_after:,.2f}</code>\n"

        text += (
            f"{'─' * 30}\n"
            f"{reason_icon} Причина:  {result} по {reason}\n"
            f"⏱ Время:   {dur_str}\n"
        )

        await self._send(text)

    async def send_heartbeat(self, stats: dict) -> None:
        """Ежечасный отчёт."""
        capital      = stats.get('capital', 0)
        daily_pnl    = stats.get('daily_pnl', 0)
        open_pos     = stats.get('open_positions', 0)
        trades_today = stats.get('trades_today', 0)
        win_today    = stats.get('win_today', 0)
        loss_today   = stats.get('loss_today', 0)
        bias         = stats.get('market_bias', 'neutral')
        strength     = stats.get('bias_strength', 0)

        sign      = "+" if daily_pnl >= 0 else ""
        pnl_icon  = "📈" if daily_pnl >= 0 else "📉"
        daily_pct = daily_pnl / 1000 * 100  # от стартового $1000
        bias_icon = {'long': '📈', 'short': '📉', 'neutral': '⏸'}.get(bias, '⏸')

        text = (
            f"💓 <b>Monster 2.2 — Статус</b>\n"
            f"{'─' * 30}\n"
            f"💵 Капитал:      <b>${capital:,.2f}</b>\n"
            f"{pnl_icon} Дневной P&L:  "
            f"<b>{sign}{daily_pnl:.2f}$ "
            f"({sign}{daily_pct:.2f}%)</b>\n"
            f"{'─' * 30}\n"
            f"{bias_icon} Рынок:        <b>{bias.upper()}</b> ({strength:.0%})\n"
            f"📂 Открытых:     {open_pos}\n"
            f"📊 Сделок сегодня: {trades_today} "
            f"(✅{win_today} / ❌{loss_today})\n"
        )
        await self._send(text)

    async def send_error(self, error_msg: str) -> None:
        text = (
            f"🚨 <b>ОШИБКА Monster 2.2</b>\n\n"
            f"<code>{error_msg[:400]}</code>"
        )
        await self._send(text)

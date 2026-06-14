import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone

import httpx

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes,
    MessageHandler, filters,
)
from core.chart_generator import generate_report_chart

logger = logging.getLogger(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'trades.db')


def bottom_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура внизу экрана."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("📊 Статус"),
             KeyboardButton("💼 Позиции")],
            [KeyboardButton("📜 Сделки"),
             KeyboardButton("📈 Рынок")],
            [KeyboardButton("🔴 Закрыть позицию"),
             KeyboardButton("⚙️ Настройки")],
            [KeyboardButton("📊 Аналитика"),
             KeyboardButton("🎯 Режим")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие...",
    )


def report_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("7 дней",  callback_data="report_7"),
        InlineKeyboardButton("30 дней", callback_data="report_30"),
    ]])


class TelegramCommands:

    def __init__(self, position_manager, notifier,
                 data_feed=None, indicators_calc=None, strategy_config=None):
        self.pm = position_manager
        self.notifier = notifier
        self.data_feed = data_feed
        self.indicators_calc = indicators_calc
        self.strategy_config = strategy_config
        self._app = None

    # ── Text generators ───────────────────────────────────────────────────────

    def _status_text(self) -> str:
        pm = self.pm
        capital = pm.capital
        daily_pnl = pm.daily_pnl
        daily_pct = daily_pnl / capital * 100 if capital > 0 else 0
        sign = "+" if daily_pnl >= 0 else ""
        mode = "SHADOW 🕶" if pm.shadow_mode else "LIVE 🔴"

        last = "—"
        if pm.recent_trades:
            t = pm.recent_trades[-1]
            pnl = t.get('pnl_value', 0)
            s = "+" if pnl >= 0 else ""
            last = f"{t.get('symbol','?')} {t.get('side','?').upper()} {s}{pnl:.2f}$"

        # Статистика текущей сессии
        session_line = ""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                session = conn.execute(
                    "SELECT id, started_at FROM sessions ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if session:
                    sid, started = session
                    stats = conn.execute("""
                        SELECT COUNT(*), SUM(pnl_value),
                               SUM(CASE WHEN pnl_value > 0 THEN 1 ELSE 0 END)
                        FROM trades WHERE session_id = ?
                    """, (sid,)).fetchone()
                    s_cnt  = stats[0] or 0
                    s_pnl  = stats[1] or 0
                    s_wins = stats[2] or 0
                    s_wr   = s_wins / s_cnt * 100 if s_cnt > 0 else 0
                    s_sign = "+" if s_pnl >= 0 else ""
                    session_line = (
                        f"📋 Сессия #{sid}:  "
                        f"{s_cnt} сд  WR={s_wr:.0f}%  "
                        f"{s_sign}{s_pnl:.2f}$\n"
                    )
        except Exception:
            pass

        return (
            f"📊 <b>Monster 2.2 — Статус</b>\n\n"
            f"Режим:          {mode}\n"
            f"💰 Капитал:     <b>${capital:,.2f}</b>\n"
            f"{session_line}"
            f"📈 Дневной P&L: <b>{sign}{daily_pnl:.2f}$ ({sign}{daily_pct:.2f}%)</b>\n"
            f"📂 Позиций:     {len(pm.positions)} / {pm.config.max_concurrent_positions}\n"
            f"🕐 Последняя:   {last}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )

    async def _positions_text(self) -> str:
        pm = self.pm
        if not pm.positions:
            return "📂 <b>Нет открытых позиций</b>"

        lines = ["📂 <b>Открытые позиции:</b>\n"]
        for sym, pos in pm.positions.items():
            side  = pos.get('side', '?').upper()
            entry = pos.get('entry', 0)
            sl    = pos.get('sl', 0)
            tp    = pos.get('tp', 0)
            size  = pos.get('size', 0)
            notional = pos.get('notional', 0)
            icon      = "📈" if side == "LONG" else "📉"
            side_icon = "🟢⬆️ LONG" if side == "LONG" else "🔴⬇️ SHORT"
            sl_pct = abs(entry - sl) / entry * 100 if entry else 0
            tp_pct = abs(entry - tp) / entry * 100 if entry else 0
            rr     = tp_pct / sl_pct if sl_pct > 0 else 0
            opened = pos.get('opened_at', '')[:16].replace('T', ' ')

            # Текущая цена и нереализованный PNL
            pnl_line = ""
            try:
                ticker = await self.data_feed.get_ticker(sym)
                cur_price = ticker['price']
                if side == "LONG":
                    unreal_pnl = (cur_price - entry) * size
                else:
                    unreal_pnl = (entry - cur_price) * size
                unreal_pct = unreal_pnl / notional * 100 if notional > 0 else 0
                sign = "+" if unreal_pnl >= 0 else ""
                pnl_color = "🟢" if unreal_pnl >= 0 else "🔴"
                pnl_line = (
                    f"\n  {pnl_color} P&L:   "
                    f"<b>{sign}{unreal_pnl:.2f}$ ({sign}{unreal_pct:.2f}%)</b>"
                    f"  @ <code>${cur_price:,.4f}</code>"
                )
            except Exception:
                pass

            lines.append(
                f"{icon} <b>{sym} {side_icon}</b>\n"
                f"  Вход:  <code>${entry:,.4f}</code>\n"
                f"  SL:    <code>${sl:,.4f}</code> (-{sl_pct:.1f}%)\n"
                f"  TP:    <code>${tp:,.4f}</code> (+{tp_pct:.1f}%)\n"
                f"  R:R:   {rr:.1f}:1\n"
                f"  Время: {opened} UTC"
                f"{pnl_line}"
            )
        return "\n".join(lines)

    def _trades_text(self) -> str:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                session = conn.execute(
                    "SELECT id, started_at FROM sessions ORDER BY id DESC LIMIT 1"
                ).fetchone()

                if session:
                    sid, started = session
                    rows = conn.execute("""
                        SELECT symbol, side, entry, exit_price, pnl_value,
                               pnl_pct, exit_reason, timestamp
                        FROM trades WHERE session_id = ?
                        ORDER BY id DESC LIMIT 10
                    """, (sid,)).fetchall()
                    stats = conn.execute("""
                        SELECT COUNT(*), SUM(pnl_value),
                               SUM(CASE WHEN pnl_value > 0 THEN 1 ELSE 0 END)
                        FROM trades WHERE session_id = ?
                    """, (sid,)).fetchone()
                    cnt   = stats[0] or 0
                    total_s = stats[1] or 0
                    wins  = stats[2] or 0
                    wr    = wins / cnt * 100 if cnt > 0 else 0
                    sign_s = "+" if total_s >= 0 else ""
                    header = (
                        f"📊 <b>Сессия #{sid}</b>  "
                        f"(с {started[5:16].replace('T', ' ')} UTC)\n"
                        f"Сделок: {cnt}  WR: {wr:.0f}%  "
                        f"P&L: <b>{sign_s}{total_s:.2f}$</b>\n\n"
                    )
                else:
                    rows = conn.execute(
                        "SELECT symbol, side, entry, exit_price, pnl_value, "
                        "pnl_pct, exit_reason, timestamp "
                        "FROM trades ORDER BY id DESC LIMIT 10"
                    ).fetchall()
                    header = "📜 <b>Последние сделки:</b>\n\n"
        except Exception:
            rows = []
            header = "📜 <b>Нет закрытых сделок</b>\n"

        if not rows:
            return header + "Сделок в этой сессии нет"

        lines = [header]
        total = 0.0
        for sym, side, entry, exit_p, pnl, pnl_pct, reason, ts in rows:
            pnl = pnl or 0.0
            total += pnl
            icon = "✅" if pnl >= 0 else "❌"
            sign = "+" if pnl >= 0 else ""
            ts_short = (ts or '')[:16].replace('T', ' ')
            lines.append(
                f"{icon} <b>{sym} {(side or '').upper()}</b>\n"
                f"  {sign}{pnl:.2f}$ ({sign}{(pnl_pct or 0):.2f}%) "
                f"[{reason}]  {ts_short}"
            )
        sign_t = "+" if total >= 0 else ""
        lines.append(f"\n<b>Итого: {sign_t}{total:.2f}$</b>")
        return "\n".join(lines)

    async def _market_text(self) -> str:
        """Человекочитаемый анализ рынка через Claude."""
        if not self.data_feed or not self.indicators_calc:
            return "📈 <b>Данные рынка недоступны</b>"
        try:
            lines = []
            for symbol in self.pm.config.symbols:
                try:
                    data = await self.data_feed.get_all_data(symbol)
                    ind = self.indicators_calc.calculate_all(
                        data['ohlcv_1h'], data['ohlcv_5m'], self.strategy_config
                    )
                    name  = symbol.replace('/USDT:USDT', '')
                    price = data['ticker']['price']
                    adx   = ind.get('adx_1h', 0)
                    rsi   = ind.get('rsi_1h', 50)
                    chop  = ind.get('chop', 50)
                    ema8  = ind.get('ema_8', 0)
                    ema21 = ind.get('ema_21', 0)
                    macd  = ind.get('macd_hist', 0)
                    lines.append(
                        f"{name}: цена={price:.4f} "
                        f"ADX={adx:.0f} RSI={rsi:.0f} "
                        f"Chop={chop:.0f} "
                        f"EMA={'бычий' if ema8 > ema21 else 'медвежий'} "
                        f"MACD={'↑' if macd > 0 else '↓'}"
                    )
                    await asyncio.sleep(0.3)
                except Exception as e:
                    name = symbol.replace('/USDT:USDT', '')
                    lines.append(f"{name}: ошибка {e}")

            market_data = "\n".join(lines)
            prompt = (
                "Вот данные по крипто-рынку прямо сейчас:\n\n"
                f"{market_data}\n\n"
                "Напиши ОЧЕНЬ короткий анализ (5-7 строк) для трейдера.\n"
                "Используй простой язык без технического жаргона.\n"
                "Для каждой монеты одна строка — что происходит и куда движется.\n"
                "В конце одна строка — общее настроение рынка.\n"
                "Используй эмодзи. Без лишних слов."
            )

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": os.getenv("ANTHROPIC_API_KEY", ""),
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 400,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=15.0,
                )

            if response.status_code == 200:
                analysis = response.json()["content"][0]["text"]
                return f"📈 <b>Рынок сейчас</b>\n\n{analysis}"
            raise Exception(f"API error {response.status_code}")

        except Exception as e:
            logger.warning("Claude market analysis failed: %s", e)
            return await self._market_text_simple()

    async def _market_text_simple(self) -> str:
        """Простой анализ без Claude если API недоступен."""
        if not self.data_feed or not self.indicators_calc:
            return "📈 <b>Данные рынка недоступны</b>"
        try:
            result = ["📈 <b>Рынок сейчас</b>\n"]
            for symbol in self.pm.config.symbols:
                try:
                    data  = await self.data_feed.get_all_data(symbol)
                    ind   = self.indicators_calc.calculate_all(
                        data['ohlcv_1h'], data['ohlcv_5m'], self.strategy_config
                    )
                    name  = symbol.replace('/USDT:USDT', '')
                    price = data['ticker']['price']
                    adx   = ind.get('adx_1h', 0)
                    rsi   = ind.get('rsi_1h', 50)
                    ema8  = ind.get('ema_8', 0)
                    ema21 = ind.get('ema_21', 0)
                    macd  = ind.get('macd_hist', 0)
                    chop  = ind.get('chop', 50)

                    bull     = sum([ema8 > ema21, macd > 0, rsi > 55, adx > 20])
                    bear     = sum([ema8 < ema21, macd < 0, rsi < 45, adx > 20])
                    sideways = chop > 55

                    if sideways:
                        mood, desc = '⚪', 'боковик, нет тренда'
                    elif bull >= 3:
                        mood = '🟢🔥' if adx > 30 else '🟢'
                        desc = f'рост, RSI={rsi:.0f}'
                    elif bear >= 3:
                        mood = '🔴🔥' if adx > 30 else '🔴'
                        desc = f'падение, RSI={rsi:.0f}'
                    else:
                        mood, desc = '⚪', 'неопределённость'

                    result.append(f"{mood} <b>{name}</b> ${price:.4f} — {desc}")
                    await asyncio.sleep(0.3)
                except Exception:
                    name = symbol.replace('/USDT:USDT', '')
                    result.append(f"❓ <b>{name}</b> — нет данных")

            return "\n".join(result)
        except Exception as e:
            return f"📈 Рынок: ошибка получения данных ({e})"

    def _close_menu_text(self) -> str:
        pm = self.pm
        if not pm.positions:
            return "📂 Нет открытых позиций для закрытия"

        lines = ["🔴 <b>Выбери позицию для закрытия:</b>\n"]
        for sym, pos in pm.positions.items():
            side  = pos.get('side', '?').upper()
            entry = pos.get('entry', 0)
            trail = pos.get('trail_active', False)
            name  = sym.replace('/USDT:USDT', '')
            trail_icon = "🟢 trail ON" if trail else "⚪ trail off"
            lines.append(
                f"📌 <b>{name} {side}</b>\n"
                f"  Вход: ${entry:,.4f}\n"
                f"  {trail_icon}\n"
            )
        return "\n".join(lines)

    def _close_keyboard(self) -> InlineKeyboardMarkup:
        pm = self.pm
        if not pm.positions:
            return InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Нет позиций", callback_data="no_positions"
                )
            ]])
        buttons = []
        for sym, pos in pm.positions.items():
            name = sym.replace('/USDT:USDT', '')
            side = pos.get('side', '?').upper()
            buttons.append([
                InlineKeyboardButton(
                    f"❌ Закрыть {name} {side}",
                    callback_data=f"close_{sym}"
                )
            ])
        buttons.append([
            InlineKeyboardButton(
                "❌ Закрыть ВСЕ", callback_data="close_ALL"
            )
        ])
        buttons.append([
            InlineKeyboardButton(
                "↩️ Назад", callback_data="positions"
            )
        ])
        return InlineKeyboardMarkup(buttons)

    def _settings_text(self) -> str:
        cfg = self.pm.config
        sc = self.strategy_config
        mode = "SHADOW" if cfg.trading_mode == 'shadow' else "LIVE"
        bad = f"0-{max(cfg.bad_hours_utc)}" if cfg.bad_hours_utc else "—"
        adx_thr = sc.adx_threshold if sc else "—"
        chop_thr = sc.chop_threshold if sc else "—"
        return (
            f"⚙️ <b>Настройки Monster 2.2</b>\n\n"
            f"Режим:         {mode}\n"
            f"Капитал:       ${cfg.shadow_capital:,.2f}\n"
            f"Плечо:         {cfg.leverage}x\n"
            f"Риск/сделку:   {cfg.risk_per_trade_pct}%\n"
            f"Макс позиций:  {cfg.max_concurrent_positions}\n"
            f"ADX порог:     {adx_thr}\n"
            f"Chop порог:    {chop_thr}\n"
            f"Bad hours UTC: {bad}\n"
            f"Стратегии:     Keltner + EMA_MACD + AroonMacd"
        )

    # ── Phase8: Analytics ─────────────────────────────────────────────────────

    def _analytics_text(self) -> str:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                by_symbol = conn.execute("""
                    SELECT replace(symbol,'/USDT:USDT',''),
                           COUNT(*), ROUND(SUM(pnl_value),2),
                           ROUND(AVG(CASE WHEN pnl_value>0 THEN 1.0 ELSE 0 END)*100,0),
                           ROUND(AVG(pnl_value),2)
                    FROM trades GROUP BY symbol ORDER BY SUM(pnl_value) DESC
                """).fetchall()
                by_reason = conn.execute("""
                    SELECT exit_reason, COUNT(*), ROUND(SUM(pnl_value),2),
                           ROUND(AVG(CASE WHEN pnl_value>0 THEN 1.0 ELSE 0 END)*100,0)
                    FROM trades GROUP BY exit_reason ORDER BY SUM(pnl_value) DESC
                """).fetchall()
                stats = conn.execute("""
                    SELECT COUNT(*),
                           AVG(CASE WHEN pnl_value>0 THEN pnl_value END),
                           AVG(CASE WHEN pnl_value<=0 THEN pnl_value END),
                           AVG(CASE WHEN pnl_value>0 THEN 1.0 ELSE 0 END)
                    FROM trades
                """).fetchone()

            wr      = stats[3] or 0
            avg_win  = stats[1] or 0
            avg_loss = stats[2] or 0
            exp = wr * avg_win + (1 - wr) * avg_loss
            pf  = (wr * avg_win) / abs((1-wr) * avg_loss) if avg_loss else 0

            lines = ["📊 <b>Аналитика</b>\n", "<b>По парам:</b>"]
            for sym, cnt, pnl, wr_p, avg in by_symbol:
                icon = "🟢" if (pnl or 0) >= 0 else "🔴"
                lines.append(
                    f"  {icon} {str(sym):6} {cnt:3}сд  "
                    f"WR={wr_p or 0:3.0f}%  {pnl or 0:>+7.2f}$  avg={avg or 0:>+5.2f}$"
                )
            lines.append("\n<b>По причине выхода:</b>")
            for reason, cnt, pnl, wr_p in by_reason:
                icon = "🟢" if (pnl or 0) >= 0 else "🔴"
                lines.append(
                    f"  {icon} {str(reason):22} {cnt:3}сд  "
                    f"WR={wr_p or 0:3.0f}%  {pnl or 0:>+7.2f}$"
                )
            lines.append(
                f"\n<b>Expectancy:</b> {exp:>+.3f}$ / сделку"
                f"\n<b>Profit factor:</b> {pf:.2f}"
                f"\n<b>Avg win:</b> +{avg_win:.2f}$  <b>Avg loss:</b> {avg_loss:.2f}$"
            )
            return "\n".join(lines)
        except Exception as e:
            return f"❌ Ошибка аналитики: {e}"

    async def cmd_analytics(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            self._analytics_text(), parse_mode='HTML', reply_markup=bottom_keyboard()
        )

    async def cmd_regime(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        mr = getattr(self, 'market_regime', None)
        if mr:
            bias = mr.get_btc_bias()
            impulse = mr.btc_impulse_active()
            imp_dir = mr._btc_impulse_dir
        else:
            bias, impulse, imp_dir = 'unknown', False, None
        icon = {'bullish': '📈', 'bearish': '📉', 'neutral': '⚪'}.get(bias, '❓')
        imp_line = f"Импульс: {imp_dir} 🚨" if impulse else "Импульс: нет"
        await update.message.reply_text(
            f"🎯 <b>Режим рынка</b>\n\n"
            f"{icon} BTC тренд: <b>{bias.upper()}</b>\n{imp_line}",
            parse_mode='HTML', reply_markup=bottom_keyboard()
        )

    # ── Command handlers ──────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👋 Monster 2.2 на связи!\nВыбери действие:",
            reply_markup=bottom_keyboard(),
        )

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            self._status_text(),
            parse_mode='HTML',
            reply_markup=bottom_keyboard(),
        )

    async def cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            await self._positions_text(),
            parse_mode='HTML',
            reply_markup=bottom_keyboard(),
        )

    async def cmd_trades(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            self._trades_text(),
            parse_mode='HTML',
            reply_markup=bottom_keyboard(),
        )

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 <b>Monster 2.2 — Команды</b>\n\n"
            "/start — главное меню\n"
            "/status — текущий статус\n"
            "/positions — открытые позиции\n"
            "/trades — последние сделки\n"
            "/stop — остановить бота\n\n"
            "Или жми кнопки внизу 👇",
            parse_mode='HTML',
            reply_markup=bottom_keyboard(),
        )

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.pm.stop_requested = True
        await update.message.reply_text(
            "🛑 <b>Остановка Monster 2.2...</b>",
            parse_mode='HTML',
            reply_markup=bottom_keyboard(),
        )

    async def cmd_report(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📊 Выбери период:",
            reply_markup=report_keyboard(),
        )

    # ── Error handler ─────────────────────────────────────────────────────────

    async def error_handler(self, update: object,
                             ctx: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Telegram error: {ctx.error}", exc_info=ctx.error)

    # ── ReplyKeyboard button handler ──────────────────────────────────────────

    async def handle_button(self, update: Update,
                             ctx: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        logger.info(f"Button pressed: {repr(text)}")

        if text == "📊 Статус":
            await update.message.reply_text(
                self._status_text(),
                parse_mode='HTML',
                reply_markup=bottom_keyboard(),
            )

        elif text == "💼 Позиции":
            await update.message.reply_text(
                await self._positions_text(),
                parse_mode='HTML',
                reply_markup=bottom_keyboard(),
            )

        elif text == "📜 Сделки":
            await update.message.reply_text(
                self._trades_text(),
                parse_mode='HTML',
                reply_markup=bottom_keyboard(),
            )

        elif text == "📈 Рынок":
            await update.message.reply_text(
                "⏳ Загружаю данные рынка...",
                reply_markup=bottom_keyboard(),
            )
            reply = await self._market_text()
            await update.message.reply_text(
                reply,
                parse_mode='HTML',
                reply_markup=bottom_keyboard(),
            )

        elif text == "⚙️ Настройки":
            await update.message.reply_text(
                self._settings_text(),
                parse_mode='HTML',
                reply_markup=bottom_keyboard(),
            )

        elif text == "📊 Отчёт":
            await update.message.reply_text(
                "📊 Выбери период:",
                reply_markup=report_keyboard(),
            )

        elif text == "📊 Аналитика":
            await update.message.reply_text(
                self._analytics_text(), parse_mode='HTML', reply_markup=bottom_keyboard()
            )

        elif text == "🎯 Режим":
            await self.cmd_regime(update, ctx)

        elif text == "🔴 Закрыть позицию":
            reply = self._close_menu_text()
            await update.message.reply_text(
                reply,
                parse_mode='HTML',
                reply_markup=self._close_keyboard()
            )

    # ── Inline callback handler ───────────────────────────────────────────────

    async def button_callback(self, update: Update,
                               ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data

        if data == "report_7":
            await self._send_report(query, days=7)
        elif data == "report_30":
            await self._send_report(query, days=30)

        elif data.startswith("close_"):
            target = data[6:]  # символ или ALL

            if not self.pm.positions:
                await query.answer()
                await query.edit_message_text(
                    "📂 Нет открытых позиций",
                )
                return

            if target != "ALL" and target not in self.pm.positions:
                await query.answer("Позиция не найдена")
                return

            await query.answer("⏳ Закрываю...")

            if target == "ALL":
                symbols_to_close = list(self.pm.positions.keys())
            else:
                symbols_to_close = [target]

            closed_list = []
            for sym in symbols_to_close:
                if sym not in self.pm.positions:
                    continue
                pos = self.pm.positions[sym]
                try:
                    ticker = await self.data_feed.get_ticker(sym)
                    price = ticker['price']
                    trade = await self.pm._close_position(
                        sym, pos, price, 'Manual_Close'
                    )
                    sign = '+' if trade.get('pnl_value', 0) > 0 else ''
                    pnl = trade.get('pnl_value', 0)
                    name = sym.replace('/USDT:USDT', '')
                    closed_list.append(f"✅ {name}: {sign}{pnl:.2f}$")
                except Exception as e:
                    name = sym.replace('/USDT:USDT', '')
                    closed_list.append(f"❌ {name}: ошибка {e}")

            result = "\n".join(closed_list)
            capital = self.pm.capital
            await query.edit_message_text(
                f"🔴 <b>Закрыто вручную:</b>\n\n"
                f"{result}\n\n"
                f"💵 Баланс: ${capital:,.2f}",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "📊 Статус", callback_data="status"
                    )
                ]])
            )

        else:
            await query.answer("Используй кнопки внизу 👇")

    async def _send_report(self, query, days: int):
        """Генерирует и отправляет диаграмму отчёта."""
        await query.answer("Генерирую отчёт...")
        try:
            png_bytes, report_text = generate_report_chart(days)
            await query.message.reply_photo(
                photo=png_bytes,
                caption=report_text,
                parse_mode='HTML',
                reply_markup=bottom_keyboard(),
            )
        except Exception as e:
            await query.message.reply_text(
                f"❌ Ошибка генерации отчёта: {e}",
                parse_mode='HTML',
                reply_markup=bottom_keyboard(),
            )

    # ── Runner ────────────────────────────────────────────────────────────────

    async def run(self):
        token = os.getenv('TELEGRAM_TOKEN', '')
        if not token:
            logger.warning("TELEGRAM_TOKEN not set, commands disabled")
            return

        self._app = ApplicationBuilder().token(token).build()

        self._app.add_handler(CommandHandler("start",     self.cmd_start))
        self._app.add_handler(CommandHandler("status",    self.cmd_status))
        self._app.add_handler(CommandHandler("positions", self.cmd_positions))
        self._app.add_handler(CommandHandler("trades",    self.cmd_trades))
        self._app.add_handler(CommandHandler("help",      self.cmd_help))
        self._app.add_handler(CommandHandler("stop",      self.cmd_stop))
        self._app.add_handler(CommandHandler("report",    self.cmd_report))
        self._app.add_handler(CommandHandler("analytics", self.cmd_analytics))
        self._app.add_handler(CommandHandler("regime",    self.cmd_regime))
        self._app.add_handler(CallbackQueryHandler(self.button_callback))
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self.handle_button,
            )
        )
        self._app.add_error_handler(self.error_handler)

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram command handler started (inline buttons)")

    async def stop(self):
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

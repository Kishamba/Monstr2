import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'trades.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT,
    symbol           TEXT,
    side             TEXT,
    entry            REAL,
    exit_price       REAL,
    sl               REAL,
    tp               REAL,
    size             REAL,
    notional         REAL,
    pnl_value        REAL,
    pnl_pct          REAL,
    exit_reason      TEXT,
    duration_minutes REAL,
    indicators_json  TEXT,
    leverage         INTEGER,
    status           TEXT DEFAULT 'closed',
    session_id       INTEGER
);
CREATE TABLE IF NOT EXISTS open_positions (
    symbol           TEXT PRIMARY KEY,
    side             TEXT,
    entry            REAL,
    sl               REAL,
    tp               REAL,
    trail_sl         REAL,
    trail_active     INTEGER DEFAULT 0,
    peak_price       REAL,
    size             REAL,
    notional         REAL,
    opened_at        TEXT,
    meta_json        TEXT,
    partial_tp_done  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    config_json TEXT,
    note        TEXT
);
"""


class PositionManager:

    PARTIAL_TP_DOLLAR  = 6.0   # фиксируем $6 с первой половины
    TRAIL_ACTIVATE_PCT = 0.5   # trailing активируется на +0.5%
    TRAIL_DIST_PCT     = 0.4   # дистанция trailing
    MAX_HOLD_HOURS     = 4
    STAGNATION_MINUTES = 45
    STAGNATION_BAND    = 0.20

    def __init__(self, config, data_feed, notifier):
        self.config = config
        self.data_feed = data_feed
        self.notifier = notifier

        self.shadow_mode = config.trading_mode == 'shadow'
        self.capital = config.shadow_capital
        self.daily_pnl = 0.0
        self.recent_trades = []
        self.stop_requested = False

        self._init_db()
        self.session_id = self._start_session()
        self.positions = self._load_positions()
        if self.positions:
            logger.info(
                f"Restored {len(self.positions)} positions: "
                f"{list(self.positions.keys())}"
            )

    def _start_session(self) -> int:
        config_snapshot = {
            'trading_mode':    self.config.trading_mode,
            'shadow_capital':  self.config.shadow_capital,
            'leverage':        self.config.leverage,
            'risk_per_trade':  self.config.risk_per_trade_pct,
            'symbols':         self.config.symbols,
            'trail_activate':  self.TRAIL_ACTIVATE_PCT,
            'trail_dist':      self.TRAIL_DIST_PCT,
            'max_hold_hours':  self.MAX_HOLD_HOURS,
            'partial_tp_dollar': self.PARTIAL_TP_DOLLAR,
        }
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "INSERT INTO sessions (started_at, config_json) VALUES (?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(config_snapshot),
                )
            )
            conn.commit()
            sid = cur.lastrowid
        logger.info(f"Session #{sid} started")
        return sid

    def _init_db(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(SCHEMA)
            # Миграция trades
            existing = [r[1] for r in conn.execute('PRAGMA table_info(trades)').fetchall()]
            for col, sql in [
                ('exit_price', 'ALTER TABLE trades ADD COLUMN exit_price REAL'),
                ('size',       'ALTER TABLE trades ADD COLUMN size REAL'),
                ('notional',   'ALTER TABLE trades ADD COLUMN notional REAL'),
                ('leverage',   'ALTER TABLE trades ADD COLUMN leverage INTEGER'),
                ('status',     'ALTER TABLE trades ADD COLUMN status TEXT'),
                ('session_id', 'ALTER TABLE trades ADD COLUMN session_id INTEGER'),
            ]:
                if col not in existing:
                    conn.execute(sql)
            # Миграция open_positions
            op_existing = [r[1] for r in conn.execute('PRAGMA table_info(open_positions)').fetchall()]
            for col, sql in [
                ('trail_sl',        'ALTER TABLE open_positions ADD COLUMN trail_sl REAL'),
                ('trail_active',    'ALTER TABLE open_positions ADD COLUMN trail_active INTEGER DEFAULT 0'),
                ('peak_price',      'ALTER TABLE open_positions ADD COLUMN peak_price REAL'),
                ('partial_tp_done', 'ALTER TABLE open_positions ADD COLUMN partial_tp_done INTEGER DEFAULT 0'),
            ]:
                if col not in op_existing:
                    conn.execute(sql)
            conn.commit()

    def _load_positions(self) -> dict:
        positions = {}
        try:
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT symbol, side, entry, sl, tp, "
                    "trail_sl, trail_active, peak_price, "
                    "size, notional, opened_at, meta_json, partial_tp_done "
                    "FROM open_positions"
                ).fetchall()
            for r in rows:
                sym = r[0]
                positions[sym] = {
                    'symbol':          sym,
                    'side':            r[1],
                    'entry':           r[2],
                    'sl':              r[3],
                    'tp':              r[4],
                    'trail_sl':        r[5],
                    'trail_active':    bool(r[6]),
                    'peak_price':      r[7] or r[2],
                    'size':            r[8],
                    'notional':        r[9],
                    'opened_at':       r[10],
                    'meta':            json.loads(r[11] or '{}'),
                    'partial_tp_done': bool(r[12]),
                }
        except Exception as e:
            logger.error(f"Error loading positions: {e}")
        return positions

    def _save_position(self, symbol: str, pos: dict):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO open_positions
                   (symbol, side, entry, sl, tp,
                    trail_sl, trail_active, peak_price,
                    size, notional, opened_at, meta_json,
                    partial_tp_done)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    symbol,
                    pos['side'],        pos['entry'],
                    pos['sl'],          pos['tp'],
                    pos.get('trail_sl', pos['sl']),
                    int(pos.get('trail_active', False)),
                    pos.get('peak_price', pos['entry']),
                    pos['size'],        pos['notional'],
                    pos['opened_at'],
                    json.dumps(pos.get('meta', {})),
                    int(pos.get('partial_tp_done', False)),
                )
            )
            conn.commit()

    def _update_position_sl(self, symbol: str, pos: dict):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """UPDATE open_positions
                   SET trail_sl=?, trail_active=?, peak_price=?,
                       partial_tp_done=?, size=?, notional=?
                   WHERE symbol=?""",
                (
                    pos.get('trail_sl', pos['sl']),
                    int(pos.get('trail_active', False)),
                    pos.get('peak_price', pos['entry']),
                    int(pos.get('partial_tp_done', False)),
                    pos['size'],
                    pos['notional'],
                    symbol,
                )
            )
            conn.commit()

    def _delete_position(self, symbol: str):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "DELETE FROM open_positions WHERE symbol=?", (symbol,)
            )
            conn.commit()

    async def open_position(self, symbol: str,
                             decision: dict, capital: float) -> dict:
        if symbol in self.positions:
            logger.warning(f"Already open: {symbol}, skip")
            return {}

        ticker = await self.data_feed.get_ticker(symbol)
        entry_price = ticker['price']
        sl_price = float(decision['sl_price'])
        tp_price = float(decision['tp_price'])
        action   = decision['action']
        risk_pct = float(decision.get('risk_pct',
                         self.config.risk_per_trade_pct))

        sl_distance = abs(entry_price - sl_price)
        if sl_distance < 1e-10:
            logger.warning(f"SL distance ~0 for {symbol}, skip")
            return {}

        risk_amount   = capital * (risk_pct / 100)
        position_size = risk_amount / sl_distance
        notional      = position_size * entry_price

        pos = {
            'symbol':          symbol,
            'side':            action,
            'entry':           entry_price,
            'sl':              sl_price,
            'tp':              tp_price,
            'trail_sl':        sl_price,
            'trail_active':    False,
            'peak_price':      entry_price,
            'size':            position_size,
            'notional':        notional,
            'opened_at':       datetime.now(timezone.utc).isoformat(),
            'meta':            {'decision': decision},
            'partial_tp_done': False,
        }

        self.positions[symbol] = pos
        self._save_position(symbol, pos)

        logger.info(
            f"[SHADOW] Opened {action} {symbol} "
            f"@ {entry_price} | SL={sl_price:.6f} "
            f"TP={tp_price:.6f} | size={position_size:.4f} "
            f"notional={notional:.2f}"
        )

        await self.notifier.send_signal(
            symbol=symbol,
            decision={**decision, 'entry_price': entry_price},
            indicators=decision.get('indicators', {}),
            capital=self.capital,
            position_size=position_size,
            notional=notional,
            leverage=self.config.leverage,
        )
        return pos

    async def check_positions(self, current_prices: dict) -> list:
        closed = []

        for symbol, pos in list(self.positions.items()):
            price = current_prices.get(symbol)
            if price is None:
                logger.warning(f"No price for {symbol}, skip check")
                continue

            side         = pos['side']
            entry        = pos['entry']
            size         = pos['size']
            notional     = pos['notional']
            sl           = pos.get('trail_sl', pos['sl'])
            tp           = pos['tp']
            trail_active = pos.get('trail_active', False)
            partial_done = pos.get('partial_tp_done', False)

            if side == 'long':
                profit_pct  = (price - entry) / entry * 100
                current_pnl = (price - entry) * size
            else:
                profit_pct  = (entry - price) / entry * 100
                current_pnl = (entry - price) * size

            logger.info(
                f"CHECK {symbol} {side.upper()} | "
                f"entry={entry:.4f} now={price:.4f} | "
                f"P&L={profit_pct:+.2f}% ${current_pnl:+.2f} | "
                f"trail={'ON' if trail_active else 'off'} | "
                f"partial={'DONE' if partial_done else 'pending'}"
            )

            exit_reason = None

            # ── ЧАСТИЧНЫЙ TP: фиксируем $6 с 50% позиции ────────────────
            if not partial_done and current_pnl >= self.PARTIAL_TP_DOLLAR:
                half_pnl  = current_pnl * 0.5
                half_size = size * 0.5
                half_notional = notional * 0.5

                pos['partial_tp_done'] = True
                pos['size']    = half_size
                pos['notional'] = half_notional
                self._update_position_sl(symbol, pos)

                dur = (
                    datetime.now(timezone.utc) -
                    datetime.fromisoformat(pos['opened_at'])
                ).total_seconds() / 60

                partial_trade = {
                    'symbol':           symbol,
                    'side':             side,
                    'entry':            entry,
                    'exit_price':       price,
                    'sl':               pos['sl'],
                    'tp':               tp,
                    'size':             half_size,
                    'notional':         half_notional,
                    'pnl_value':        round(half_pnl, 4),
                    'pnl_pct':          round(profit_pct, 4),
                    'exit_reason':      'Partial_TP',
                    'duration_minutes': round(dur, 1),
                    'timestamp':        datetime.now(timezone.utc).isoformat(),
                    'leverage':         self.config.leverage,
                }
                self.daily_pnl += half_pnl
                self.capital   += half_pnl
                await self._save_trade(partial_trade)
                self.recent_trades.append(partial_trade)
                if len(self.recent_trades) > 50:
                    self.recent_trades.pop(0)

                logger.info(
                    f"PARTIAL_TP {symbol} | "
                    f"+${half_pnl:.2f} (50% позиции) | "
                    f"Остаток идёт на trailing"
                )
                await self.notifier.send_message(
                    f"🎯 <b>ЧАСТИЧНЫЙ TP {symbol}</b>\n"
                    f"Зафиксировано: <b>+${half_pnl:.2f}</b> "
                    f"(50% позиции)\n"
                    f"Остаток держим на trailing...\n"
                    f"💵 Капитал: ${self.capital:,.2f}"
                )
                partial_done = True

            # ── TRAILING для второй половины (после partial TP) ──────────
            if partial_done:
                peak = pos.get('peak_price', entry)

                if side == 'long':
                    if price > peak:
                        pos['peak_price'] = price
                        peak = price

                    if not trail_active and profit_pct >= self.TRAIL_ACTIVATE_PCT:
                        trail_active = True
                        pos['trail_active'] = True
                        new_sl = peak * (1 - self.TRAIL_DIST_PCT / 100)
                        if new_sl > sl:
                            pos['trail_sl'] = new_sl
                            sl = new_sl
                            logger.info(
                                f"TRAIL ACTIVATED {symbol} LONG | "
                                f"peak={peak:.4f} SL→{new_sl:.4f}"
                            )
                    elif trail_active:
                        new_sl = peak * (1 - self.TRAIL_DIST_PCT / 100)
                        if new_sl > sl:
                            pos['trail_sl'] = new_sl
                            sl = new_sl
                            logger.info(
                                f"TRAIL MOVED {symbol} | "
                                f"peak={peak:.4f} SL→{new_sl:.4f}"
                            )

                    if price <= sl:
                        exit_reason = 'Trailing_SL' if trail_active else 'SL'

                else:  # short
                    if price < peak:
                        pos['peak_price'] = price
                        peak = price

                    if not trail_active and profit_pct >= self.TRAIL_ACTIVATE_PCT:
                        trail_active = True
                        pos['trail_active'] = True
                        new_sl = peak * (1 + self.TRAIL_DIST_PCT / 100)
                        if new_sl < sl:
                            pos['trail_sl'] = new_sl
                            sl = new_sl
                            logger.info(
                                f"TRAIL ACTIVATED {symbol} SHORT | "
                                f"peak={peak:.4f} SL→{new_sl:.4f}"
                            )
                    elif trail_active:
                        new_sl = peak * (1 + self.TRAIL_DIST_PCT / 100)
                        if new_sl < sl:
                            pos['trail_sl'] = new_sl
                            sl = new_sl
                            logger.info(
                                f"TRAIL MOVED {symbol} | "
                                f"peak={peak:.4f} SL→{new_sl:.4f}"
                            )

                    if price >= sl:
                        exit_reason = 'Trailing_SL' if trail_active else 'SL'

                self._update_position_sl(symbol, pos)

            else:
                # Partial TP ещё не взят — обычная логика SL/TP
                if side == 'long':
                    if price <= sl:
                        exit_reason = 'SL'
                    elif price >= tp:
                        exit_reason = 'TP'
                else:
                    if price >= sl:
                        exit_reason = 'SL'
                    elif price <= tp:
                        exit_reason = 'TP'

            # ── STAGNATION (только до partial TP) ───────────────────────
            if not exit_reason and not partial_done:
                try:
                    opened = datetime.fromisoformat(pos['opened_at'])
                    age_min = (
                        datetime.now(timezone.utc) - opened
                    ).total_seconds() / 60
                    if (age_min >= self.STAGNATION_MINUTES
                            and abs(profit_pct) <= self.STAGNATION_BAND):
                        exit_reason = 'Breakeven_Stagnation'
                        logger.info(
                            f"STAGNATION {symbol} | "
                            f"age={age_min:.0f}min | "
                            f"P&L={profit_pct:+.2f}%"
                        )
                except Exception:
                    pass

            # ── TIMEOUT ──────────────────────────────────────────────────
            if not exit_reason:
                try:
                    opened = datetime.fromisoformat(pos['opened_at'])
                    age_h = (
                        datetime.now(timezone.utc) - opened
                    ).total_seconds() / 3600
                    if age_h >= self.MAX_HOLD_HOURS:
                        exit_reason = f'Timeout_{self.MAX_HOLD_HOURS}h'
                        logger.info(
                            f"TIMEOUT {symbol} after {age_h:.1f}h | "
                            f"P&L={profit_pct:+.2f}%"
                        )
                except Exception:
                    pass

            if exit_reason:
                trade = await self._close_position(
                    symbol, pos, price, exit_reason
                )
                closed.append(trade)

        return closed

    async def _close_position(self, symbol: str, pos: dict,
                               exit_price: float, reason: str) -> dict:
        opened_dt = datetime.fromisoformat(pos['opened_at'])
        now = datetime.now(timezone.utc)
        dur = (now - opened_dt).total_seconds() / 60

        size     = pos['size']
        notional = pos['notional']

        if pos['side'] == 'long':
            pnl = (exit_price - pos['entry']) * size
        else:
            pnl = (pos['entry'] - exit_price) * size

        pnl_pct = pnl / notional * 100 if notional > 0 else 0

        trade = {
            'symbol':           symbol,
            'side':             pos['side'],
            'entry':            pos['entry'],
            'exit_price':       exit_price,
            'sl':               pos['sl'],
            'tp':               pos['tp'],
            'size':             size,
            'notional':         notional,
            'pnl_value':        round(pnl, 4),
            'pnl_pct':          round(pnl_pct, 4),
            'exit_reason':      reason,
            'duration_minutes': round(dur, 1),
            'timestamp':        now.isoformat(),
            'leverage':         self.config.leverage,
        }

        self.daily_pnl += pnl
        self.capital   += pnl
        del self.positions[symbol]
        self._delete_position(symbol)

        await self._save_trade(trade)
        self.recent_trades.append(trade)
        if len(self.recent_trades) > 50:
            self.recent_trades.pop(0)

        icon = '✅' if pnl >= 0 else '❌'
        logger.info(
            f"{icon} CLOSED {pos['side'].upper()} {symbol} | "
            f"reason={reason} | "
            f"pnl={pnl:+.4f}$ ({pnl_pct:+.2f}%) | "
            f"duration={dur:.0f}min | "
            f"capital_after={self.capital:.2f}"
        )

        await self.notifier.send_close(
            trade=trade,
            capital_after=self.capital,
        )
        return trade

    async def _save_trade(self, trade: dict):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """INSERT INTO trades
                   (timestamp, symbol, side, entry, exit_price,
                    sl, tp, size, notional,
                    pnl_value, pnl_pct, exit_reason,
                    duration_minutes, indicators_json,
                    leverage, status, session_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade['timestamp'],    trade['symbol'],
                    trade['side'],         trade['entry'],
                    trade['exit_price'],   trade['sl'],
                    trade['tp'],           trade['size'],
                    trade['notional'],     trade['pnl_value'],
                    trade['pnl_pct'],      trade['exit_reason'],
                    trade['duration_minutes'], '{}',
                    trade.get('leverage', 10), 'closed',
                    self.session_id,
                )
            )
            conn.commit()
        logger.info(
            f"Trade saved to DB: {trade['symbol']} "
            f"{trade['exit_reason']} {trade['pnl_value']:+.4f}$"
        )

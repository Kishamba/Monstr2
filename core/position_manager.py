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

    SESSION_NAME = 'Monster 2.2 — No-Red Trailing'
    NO_RED_BUFFER_PCT = 0.0       # hard floor/ceiling at breakeven in shadow mode
    TRAIL_LOCK_START = 0.35       # lock 35% of max profit immediately
    TRAIL_LOCK_GOOD = 0.55        # lock 55% once profit is decent
    TRAIL_LOCK_STRONG = 0.75      # lock 75% once move is strong
    TRAIL_GOOD_PROFIT_PCT = 0.35
    TRAIL_STRONG_PROFIT_PCT = 0.80
    MAX_HOLD_HOURS = 4

    def __init__(self, config, data_feed, notifier):
        self.config = config
        self.data_feed = data_feed
        self.notifier = notifier

        self.shadow_mode = config.trading_mode == 'shadow'
        self.recent_trades = []
        self.stop_requested = False

        self._init_db()
        self.capital   = self._restore_capital()
        self.daily_pnl = self._restore_daily_pnl()
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
            'session_name':    self.SESSION_NAME,
            'no_red_buffer_pct': self.NO_RED_BUFFER_PCT,
            'trail_lock_start': self.TRAIL_LOCK_START,
            'trail_lock_good': self.TRAIL_LOCK_GOOD,
            'trail_lock_strong': self.TRAIL_LOCK_STRONG,
            'trail_good_profit_pct': self.TRAIL_GOOD_PROFIT_PCT,
            'trail_strong_profit_pct': self.TRAIL_STRONG_PROFIT_PCT,
            'max_hold_hours':  self.MAX_HOLD_HOURS,
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
        logger.info(f"Session #{sid} started — open_positions preserved")
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

    def _restore_capital(self) -> float:
        base = self.config.shadow_capital
        try:
            with sqlite3.connect(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT SUM(pnl_value) FROM trades"
                ).fetchone()
                total_pnl = row[0] or 0.0
            capital = base + total_pnl
            logger.info(
                f"Capital restored: ${base:.2f} base "
                f"+ ${total_pnl:.2f} P&L = ${capital:.2f}"
            )
            return capital
        except Exception as e:
            logger.warning(f"Capital restore failed: {e}, using base ${base:.2f}")
            return base

    def _restore_daily_pnl(self) -> float:
        try:
            today = __import__('datetime').date.today().isoformat()
            with sqlite3.connect(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT SUM(pnl_value) FROM trades "
                    "WHERE timestamp >= ?",
                    (today,)
                ).fetchone()
                return row[0] or 0.0
        except Exception:
            return 0.0

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


    def _initial_no_red_stop(self, entry: float, side: str) -> float:
        buffer = self.NO_RED_BUFFER_PCT / 100
        if side == 'long':
            return entry * (1 + buffer)
        return entry * (1 - buffer)

    def _profit_pct(self, side: str, entry: float, price: float) -> float:
        if side == 'long':
            return (price - entry) / entry * 100
        return (entry - price) / entry * 100

    def _pnl_value(self, side: str, entry: float, price: float, size: float) -> float:
        if side == 'long':
            return (price - entry) * size
        return (entry - price) * size

    def _lock_ratio(self, profit_pct: float) -> float:
        if profit_pct >= self.TRAIL_STRONG_PROFIT_PCT:
            return self.TRAIL_LOCK_STRONG
        if profit_pct >= self.TRAIL_GOOD_PROFIT_PCT:
            return self.TRAIL_LOCK_GOOD
        return self.TRAIL_LOCK_START

    def _move_no_red_stop(self, symbol: str, pos: dict, price: float) -> float:
        side = pos['side']
        entry = pos['entry']
        old_stop = pos.get('trail_sl') or pos.get('sl') or entry
        no_red_stop = pos.get('no_red_stop') or self._initial_no_red_stop(entry, side)
        peak = pos.get('peak_price') or entry

        if side == 'long':
            if price > peak:
                peak = price
                pos['peak_price'] = peak
            max_profit = max(peak - entry, 0.0)
            peak_profit_pct = max((peak - entry) / entry * 100, 0.0)
            candidate = entry + max_profit * self._lock_ratio(peak_profit_pct)
            new_stop = max(old_stop, no_red_stop, candidate)
        else:
            if price < peak:
                peak = price
                pos['peak_price'] = peak
            max_profit = max(entry - peak, 0.0)
            peak_profit_pct = max((entry - peak) / entry * 100, 0.0)
            candidate = entry - max_profit * self._lock_ratio(peak_profit_pct)
            new_stop = min(old_stop, no_red_stop, candidate)

        pos['trail_sl'] = new_stop
        pos['sl'] = new_stop
        pos['trail_active'] = True
        pos['no_red_stop'] = no_red_stop
        if abs(new_stop - old_stop) > 1e-12:
            logger.info(
                f"NO_RED_TRAIL MOVE {symbol} {side.upper()} | "
                f"peak={peak:.6f} stop={old_stop:.6f}->{new_stop:.6f}"
            )
        return new_stop

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
        strategy_sl = float(decision['sl_price'])
        strategy_tp = float(decision['tp_price'])
        action = decision['action']
        risk_pct = float(decision.get('risk_pct',
                         self.config.risk_per_trade_pct))

        # Strategy SL is used only to size the position. The live/shadow exit
        # is controlled by No-Red trailing from breakeven.
        sizing_distance = abs(entry_price - strategy_sl)
        if sizing_distance < 1e-10:
            logger.warning(f"Sizing SL distance ~0 for {symbol}, skip")
            return {}

        fee_rate = 0.00055
        risk_amount = capital * (risk_pct / 100)
        sl_pct = sizing_distance / entry_price
        est_notional = risk_amount / sl_pct
        est_fees = est_notional * fee_rate * 2
        net_risk = max(risk_amount - est_fees, 0.01)
        position_size = net_risk / sizing_distance
        notional = position_size * entry_price
        no_red_stop = self._initial_no_red_stop(entry_price, action)

        pos = {
            'symbol':          symbol,
            'side':            action,
            'entry':           entry_price,
            'sl':              no_red_stop,
            'tp':              strategy_tp,
            'trail_sl':        no_red_stop,
            'trail_active':    True,
            'peak_price':      entry_price,
            'no_red_stop':     no_red_stop,
            'size':            position_size,
            'notional':        notional,
            'opened_at':       datetime.now(timezone.utc).isoformat(),
            'meta':            {
                'decision': decision,
                'strategy_sl': strategy_sl,
                'strategy_tp': strategy_tp,
                'exit_model': self.SESSION_NAME,
            },
            'partial_tp_done': True,
        }

        self.positions[symbol] = pos
        self._save_position(symbol, pos)

        logger.info(
            f"[SHADOW] Opened {action} {symbol} "
            f"@ {entry_price} | NO_RED_SL={no_red_stop:.6f} "
            f"dynamic_trailing=True | size={position_size:.4f} "
            f"notional={notional:.2f}"
        )

        await self.notifier.send_signal(
            symbol=symbol,
            decision={
                **decision,
                'entry_price': entry_price,
                'sl_price': no_red_stop,
                'tp_price': strategy_tp,
                'source': self.SESSION_NAME,
                'exit_model': self.SESSION_NAME,
            },
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

            side = pos['side']
            entry = pos['entry']
            size = pos['size']
            current_pnl = self._pnl_value(side, entry, price, size)
            profit_pct = self._profit_pct(side, entry, price)
            old_stop = pos.get('trail_sl', pos.get('sl', entry))
            stop = self._move_no_red_stop(symbol, pos, price)

            logger.info(
                f"NO_RED CHECK {symbol} {side.upper()} | "
                f"entry={entry:.6f} now={price:.6f} | "
                f"P&L={profit_pct:+.3f}% ${current_pnl:+.4f} | "
                f"stop={stop:.6f} peak={pos.get('peak_price', entry):.6f}"
            )

            exit_reason = None

            if side == 'long':
                if price < entry:
                    exit_reason = 'No_Red_Emergency_Exit'
                elif price <= stop and stop > entry:
                    exit_reason = 'No_Red_Trailing_SL'
            else:
                if price > entry:
                    exit_reason = 'No_Red_Emergency_Exit'
                elif price >= stop and stop < entry:
                    exit_reason = 'No_Red_Trailing_SL'

            if not exit_reason:
                try:
                    opened = datetime.fromisoformat(pos['opened_at'])
                    age_h = (
                        datetime.now(timezone.utc) - opened
                    ).total_seconds() / 3600
                    if age_h >= self.MAX_HOLD_HOURS:
                        exit_reason = f'No_Red_Timeout_{self.MAX_HOLD_HOURS}h'
                        logger.info(
                            f"NO_RED TIMEOUT {symbol} after {age_h:.1f}h | "
                            f"P&L={profit_pct:+.3f}%"
                        )
                except Exception:
                    pass

            if not exit_reason:
                self._update_position_sl(symbol, pos)
            else:
                logger.info(
                    f"NO_RED EXIT {symbol} | reason={exit_reason} | "
                    f"price={price:.6f} stop={old_stop:.6f}->{stop:.6f} "
                    f"pnl=${current_pnl:+.4f}"
                )
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
            'sl':               pos.get('trail_sl', pos['sl']),
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

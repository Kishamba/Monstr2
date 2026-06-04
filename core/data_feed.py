import logging
import time
import asyncio
import ccxt.async_support as ccxt
from ccxt.base.errors import RateLimitExceeded
import pandas as pd
from config import TradingConfig

logger = logging.getLogger(__name__)


class DataFeed:
    CACHE_TTL = 90  # seconds

    def __init__(self, config: TradingConfig):
        self.config = config
        exchange_params: dict = {
            'enableRateLimit': True,
            'options': {'defaultType': 'linear'},
        }
        # Only attach API keys in live mode (shadow only needs public endpoints)
        if config.trading_mode == 'live' and config.bybit_api_key and config.bybit_api_secret:
            exchange_params['apiKey'] = config.bybit_api_key
            exchange_params['secret'] = config.bybit_api_secret
        self.exchange = ccxt.bybit(exchange_params)
        if config.bybit_testnet:
            self.exchange.set_sandbox_mode(True)

        self._cache: dict = {}

    # ── Cache helpers ──────────────────────────────────────────────────────────

    def _cache_get(self, key: str):
        entry = self._cache.get(key)
        if entry and time.time() - entry['ts'] < self.CACHE_TTL:
            return entry['data']
        return None

    def _cache_set(self, key: str, data):
        self._cache[key] = {'ts': time.time(), 'data': data}

    # ── Retry wrapper ──────────────────────────────────────────────────────────

    async def _fetch_with_retry(self, coro_func, *args, retries=3, **kwargs):
        for attempt in range(retries):
            try:
                return await coro_func(*args, **kwargs)
            except RateLimitExceeded:
                wait = (attempt + 1) * 5  # 5, 10, 15 сек
                logger.warning(
                    f"Rate limit hit, waiting {wait}s "
                    f"(attempt {attempt+1}/{retries})"
                )
                await asyncio.sleep(wait)
        raise RateLimitExceeded("Max retries exceeded")

    # ── Public methods ─────────────────────────────────────────────────────────

    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        key = f"ohlcv:{symbol}:{timeframe}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        raw = await self._fetch_with_retry(
            self.exchange.fetch_ohlcv, symbol, timeframe, limit=limit
        )
        df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        self._cache_set(key, df)
        return df

    async def get_ticker(self, symbol: str) -> dict:
        key = f"ticker:{symbol}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        raw = await self._fetch_with_retry(self.exchange.fetch_ticker, symbol)
        data = {
            'price': raw['last'],
            'bid': raw['bid'],
            'ask': raw['ask'],
            'volume_24h': raw['quoteVolume'],
            'change_24h': raw['percentage'],
        }
        self._cache_set(key, data)
        return data

    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict:
        key = f"orderbook:{symbol}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        raw = await self._fetch_with_retry(
            self.exchange.fetch_order_book, symbol, limit=depth
        )
        bids = raw['bids'][:depth]
        asks = raw['asks'][:depth]

        bid_volume = sum(b[1] for b in bids)
        ask_volume = sum(a[1] for a in asks)
        total_volume = bid_volume + ask_volume

        imbalance = (bid_volume - ask_volume) / total_volume if total_volume > 0 else 0.0

        data = {
            'bids': bids,
            'asks': asks,
            'imbalance': round(imbalance, 4),
            'bid_pressure': round(bid_volume / total_volume, 4) if total_volume > 0 else 0.5,
            'ask_pressure': round(ask_volume / total_volume, 4) if total_volume > 0 else 0.5,
        }
        self._cache_set(key, data)
        return data

    async def get_all_data(self, symbol: str) -> dict:
        ohlcv_1h = await self.get_ohlcv(symbol, self.config.timeframe, limit=200)
        await asyncio.sleep(0.5)
        ohlcv_5m = await self.get_ohlcv(symbol, self.config.timeframe_fast, limit=200)
        await asyncio.sleep(0.5)
        ticker = await self.get_ticker(symbol)
        await asyncio.sleep(0.3)
        orderbook = await self.get_orderbook(symbol)
        return {
            'ohlcv_1h': ohlcv_1h,
            'ohlcv_5m': ohlcv_5m,
            'ticker': ticker,
            'orderbook': orderbook,
        }

    async def get_btc_trend(self) -> str:
        """Returns 'up', 'down', or 'sideways' based on last 3 BTC 1h closes."""
        try:
            ohlcv = await self.get_ohlcv('BTC/USDT:USDT', '1h', limit=10)
            if isinstance(ohlcv, pd.DataFrame):
                closes = list(ohlcv['close'].values)
            else:
                closes = [c[4] for c in ohlcv]
            if len(closes) < 4:
                return 'sideways'
            if closes[-1] > closes[-2] > closes[-3]:
                return 'up'
            elif closes[-1] < closes[-2] < closes[-3]:
                return 'down'
            return 'sideways'
        except Exception:
            return 'sideways'

    async def close(self):
        await self.exchange.close()

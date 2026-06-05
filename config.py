import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class TradingConfig:
    symbols: list = field(default_factory=lambda: [
        'DOGE/USDT:USDT',
        'SUI/USDT:USDT',
        'XRP/USDT:USDT',
        'SOL/USDT:USDT',
        'ETH/USDT:USDT',
        'AVAX/USDT:USDT',
    ])
    timeframe: str = '1h'
    timeframe_fast: str = '15m'
    leverage: int = 10
    risk_per_trade_pct: float = 0.25       # Phase1: was 0.5
    min_rr_ratio: float = 1.5              # Phase6: min acceptable R:R
    max_daily_loss_pct: float = 5.0
    max_concurrent_positions: int = 2      # Phase1: was 4
    bad_hours_utc: list = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 20, 21, 22, 23])

    # API keys (loaded from env)
    bybit_api_key: str = field(default_factory=lambda: os.getenv('BYBIT_API_KEY', ''))
    bybit_api_secret: str = field(default_factory=lambda: os.getenv('BYBIT_API_SECRET', ''))
    bybit_testnet: bool = field(default_factory=lambda: os.getenv('BYBIT_TESTNET', 'false').lower() == 'true')

    trading_mode: str = field(default_factory=lambda: os.getenv('TRADING_MODE', 'shadow'))
    shadow_capital: float = field(default_factory=lambda: float(os.getenv('SHADOW_CAPITAL', '1000.0')))


@dataclass
class StrategyConfig:
    # KeltnerAdxChop параметры
    kc_period: int = 20
    kc_atr_mult: float = 2.0
    adx_threshold: float = 15.0
    chop_threshold: float = 55.0
    atr_mult: float = 1.3                  # Phase6: was 1.0 — wider SL
    risk_ratio: float = 2.0


def get_config() -> tuple[TradingConfig, StrategyConfig]:
    return TradingConfig(), StrategyConfig()

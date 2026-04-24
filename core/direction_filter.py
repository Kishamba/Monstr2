LONG_ONLY  = set()
SHORT_ONLY = {'DOGE/USDT:USDT', 'SUI/USDT:USDT',
              'AVAX/USDT:USDT'}
BOTH       = {'SOL/USDT:USDT', 'ETH/USDT:USDT',
              'XRP/USDT:USDT'}


def is_direction_allowed(symbol: str, action: str) -> bool:
    if symbol in LONG_ONLY and action == 'short':
        return False
    if symbol in SHORT_ONLY and action == 'long':
        return False
    return True

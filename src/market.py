"""
美股交易時段判斷，用於動態調整同步頻率：
  - 美股活躍時段（盤前～收盤）：每分鐘同步（目標進出快）
  - 其餘時間：維持每小時 CHECK_MINUTE
"""
import logging
from datetime import datetime, time as dt_time

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception as e:  # 缺 tzdata 時退化
    _ET = None
    logging.getLogger(__name__).warning(
        f"無法載入 America/New_York 時區（{e}），美股時段判斷停用，"
        f"將一律走收盤排程。請安裝 tzdata（pip install tzdata）。"
    )

# 活躍時段（ET）：從盤前開始算（美股盤前最早 04:00 ET），到常規收盤 16:00 ET。
PREMARKET_OPEN = dt_time(4, 0)
MARKET_CLOSE = dt_time(16, 0)


def is_us_active_hours(now: datetime = None) -> bool:
    """
    判斷此刻是否為美股活躍時段（週一~五 盤前 04:00 – 收盤 16:00 ET）。
    每分鐘同步的頻率從盤前就開始，而不只是常規開盤。
    注意：未處理美國假日；假日會誤判為活躍而走每分鐘，
    屆時 xyz 下單會被交易所以「market closed」擋下（不影響主 perp）。
    """
    if _ET is None:
        return False
    now_et = datetime.now(_ET) if now is None else now.astimezone(_ET)
    if now_et.weekday() >= 5:  # 5=六, 6=日
        return False
    return PREMARKET_OPEN <= now_et.time() < MARKET_CLOSE

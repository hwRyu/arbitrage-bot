"""
Exchange rate service using yfinance (Yahoo Finance)
"""
import yfinance as yf
from datetime import datetime, timedelta
from typing import Optional
from utils.logger import get_logger
from config.settings import BotConfig

logger = get_logger('exchange_rate')


class ExchangeRateService:
    """
    yfinance를 통한 USD/KRW 환율 조회 서비스

    기존 한국수출입은행 API 대신 Yahoo Finance를 사용:
    - API 키 불필요
    - 24/7 실시간 환율 제공
    - Rate limit 없음
    """
    TICKER_SYMBOL = "KRW=X"  # Yahoo Finance USD/KRW 티커

    def __init__(self):
        self._cache = {}  # Simple cache: {timestamp: rate}
        self._cache_ttl = timedelta(hours=1)  # 1시간 캐시
        self._last_rate = None  # 최후 수단 폴백용

    def get_usd_krw_rate(self, date: Optional[str] = None) -> Optional[float]:
        """
        USD/KRW 환율 조회 (yfinance 사용)

        Args:
            date: 사용하지 않음 (하위 호환성을 위해 유지)

        Returns:
            USD/KRW 환율 (float) 또는 None (실패 시)
        """
        # 캐시 확인
        now = datetime.now()
        if 'timestamp' in self._cache:
            cached_rate, cached_time = self._cache['timestamp']
            if now - cached_time < self._cache_ttl:
                logger.debug(f"캐시된 환율 사용: {cached_rate:.2f} KRW/USD")
                return cached_rate

        # yfinance를 통해 실시간 환율 조회
        try:
            if hasattr(BotConfig, 'FIXED_EXCHANGE') and BotConfig.FIXED_EXCHANGE is not None:
                logger.info(f"고정 환율 모드 활성화: {BotConfig.FIXED_EXCHANGE} KRW/USD")
                rate = float(BotConfig.FIXED_EXCHANGE)

                # 캐시 업데이트
                self._cache['timestamp'] = (rate, now)
                self._last_rate = rate
                return rate
            
            logger.debug(f"yfinance로 USD/KRW 환율 조회 중... (티커: {self.TICKER_SYMBOL})")

            ticker = yf.Ticker(self.TICKER_SYMBOL)
            data = ticker.history(period="1d")

            if data.empty:
                logger.warning("yfinance에서 환율 데이터를 받지 못함, 대체 티커 시도 중...")
                # 대체 티커 시도 (USDKRW=X)
                ticker = yf.Ticker("USDKRW=X")
                data = ticker.history(period="1d")

            if not data.empty:
                # 최신 종가 사용
                rate = float(data['Close'].iloc[-1])

                # 캐시 업데이트
                self._cache['timestamp'] = (rate, now)
                self._last_rate = rate

                logger.info(f"환율 조회 완료 (yfinance): {rate:.2f} KRW/USD")
                return rate
            else:
                logger.error("yfinance에서 환율 데이터를 가져올 수 없음")

        except Exception as e:
            logger.error(f"yfinance 환율 조회 실패: {e}")

        # 실패 시 캐시된 환율 사용
        cached_rate = self.get_cached_rate()
        if cached_rate:
            logger.warning(f"캐시된 환율 사용 (최후 수단): {cached_rate:.2f} KRW/USD")
            return cached_rate

        # 최후 수단: 마지막으로 성공한 환율
        if self._last_rate:
            logger.warning(f"마지막 성공 환율 사용: {self._last_rate:.2f} KRW/USD")
            return self._last_rate

        return None

    def get_cached_rate(self) -> Optional[float]:
        """
        캐시된 환율 반환 (API 호출 없이)

        Returns:
            최근 캐시된 환율 또는 None
        """
        if not self._cache:
            return None

        # 캐시에 환율이 있으면 반환 (TTL 무시)
        if 'timestamp' in self._cache:
            rate, _ = self._cache['timestamp']
            return rate

        return None


# Singleton instance
_exchange_rate_service = None


def get_exchange_rate_service() -> ExchangeRateService:
    """Get singleton exchange rate service"""
    global _exchange_rate_service
    if _exchange_rate_service is None:
        _exchange_rate_service = ExchangeRateService()
    return _exchange_rate_service

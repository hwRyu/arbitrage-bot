"""
Layer 1: Market Scanner
Continuously scans the market for arbitrage opportunities
"""
import time
from datetime import datetime
from typing import Dict, List, Optional
from sqlalchemy.orm import Session

from config.settings import BotConfig, ScannerConfig
from models.position import MarketPrice
from models.database import SessionLocal
from models.models import MarketScan
from services.upbit import get_upbit_service
from services.bybit import get_bybit_service
from services.exchange_rate import get_exchange_rate_service
from utils.calculator import calculate_premium
from utils.logger import get_logger, log_premium

logger = get_logger('scanner')


class MarketScanner:
    """
    시장 스캐너 (Layer 1)

    역할:
    - 여러 코인의 시세를 조회
    - 김프 계산
    - 환율/USDT 가격 체크
    - 진입 기회 판단
    """

    def __init__(self):
        """Initialize market scanner"""
        self.upbit = get_upbit_service()
        self.bybit = get_bybit_service()
        self.exchange_rate_service = get_exchange_rate_service()

        self.target_coins = BotConfig.TARGET_COINS
        self.scan_interval = ScannerConfig.SCAN_INTERVAL

        # Cache for latest prices
        self.latest_prices: Dict[str, MarketPrice] = {}

        logger.info(f"Market scanner initialized. Targeting: {', '.join(self.target_coins)}")

    def scan_market(self) -> Dict[str, MarketPrice]:
        """
        Scan market for all target coins

        Returns:
            Dict of coin -> MarketPrice
        """
        logger.debug(f"Scanning market for {len(self.target_coins)} coins...")

        # Get exchange rate
        exchange_rate = self.exchange_rate_service.get_usd_krw_rate()
        if not exchange_rate:
            logger.error("Failed to fetch exchange rate")
            return {}

        # Get USDT/KRW price from Upbit
        usdt_ticker = self.upbit.fetch_ticker('USDT')
        if not usdt_ticker:
            logger.error("Failed to fetch USDT price")
            return {}

        usdt_krw = usdt_ticker['last']

        # Scan each coin
        results = {}
        for coin in self.target_coins:
            try:
                market_price = self._scan_coin(coin, exchange_rate, usdt_krw)
                if market_price:
                    results[coin] = market_price
                    self.latest_prices[coin] = market_price

                    # Log premium
                    log_premium(
                        logger, coin, market_price.premium,
                        market_price.krw_price, market_price.usd_price, exchange_rate
                    )

            except Exception as e:
                logger.error(f"Error scanning {coin}: {e}")
                continue

        logger.info(f"Market scan completed: {len(results)}/{len(self.target_coins)} coins")
        return results

    def _scan_coin(self, coin: str, exchange_rate: float, usdt_krw: float) -> Optional[MarketPrice]:
        """
        Scan single coin

        Args:
            coin: Coin symbol
            exchange_rate: USD/KRW exchange rate
            usdt_krw: USDT/KRW price

        Returns:
            MarketPrice or None
        """
        # Fetch prices
        upbit_ticker = self.upbit.fetch_ticker(coin)
        bybit_ticker = self.bybit.fetch_ticker(coin)

        if not upbit_ticker or not bybit_ticker:
            logger.warning(f"Failed to fetch ticker for {coin}")
            return None

        krw_price = upbit_ticker['last']
        usd_price = bybit_ticker['last']

        # Create MarketPrice
        market_price = MarketPrice(
            coin=coin,
            krw_price=krw_price,
            usd_price=usd_price,
            usdt_krw=usdt_krw,
            exchange_rate=exchange_rate
        )

        return market_price

    def find_opportunities(self, market_prices: Dict[str, MarketPrice],
                          active_strategies: List[str]) -> List[str]:
        """
        Find coins with arbitrage opportunities (역전 전략)

        **전략**: 김프가 낮거나 마이너스일 때 진입

        Args:
            market_prices: Dict of coin -> MarketPrice
            active_strategies: List of coins with active strategies

        Returns:
            List of coin symbols with opportunities
        """
        opportunities = []

        # 최대 진입 김프 (이 값 이하에서만 진입)
        max_entry_premium = BotConfig.MIN_ENTRY_PREMIUM

        for coin, price in market_prices.items():
            # Skip if already has active strategy
            if coin in active_strategies:
                logger.debug(f"Skipping {coin}: already has active strategy")
                continue

            # Check if premium is below threshold (역전 전략)
            # 김프가 낮을수록 좋은 진입 기회
            if price.premium <= max_entry_premium:
                opportunities.append(coin)
                logger.info(
                    f"Opportunity found: {coin} with premium {price.premium:.2f}% "
                    f"(max entry threshold: {max_entry_premium:.2f}%)"
                )

        return opportunities

    def save_scan_to_db(self, market_prices: Dict[str, MarketPrice],
                       opportunities: List[str]):
        """
        Save market scan results to database

        Args:
            market_prices: Dict of coin -> MarketPrice
            opportunities: List of coins with opportunities
        """
        try:
            db: Session = SessionLocal()

            for coin, price in market_prices.items():
                is_opportunity = coin in opportunities
                opportunity_reason = None if is_opportunity else "Premium below threshold"

                scan_record = MarketScan(
                    coin=coin,
                    premium=price.premium,
                    krw_price=price.krw_price,
                    usd_price=price.usd_price,
                    usdt_krw=price.usdt_krw,
                    exchange_rate=price.exchange_rate,
                    is_opportunity=is_opportunity,
                    opportunity_reason=opportunity_reason,
                    scanned_at=datetime.now()
                )

                db.add(scan_record)

            db.commit()
            logger.debug(f"Saved {len(market_prices)} scan records to database")

        except Exception as e:
            logger.error(f"Failed to save scan to database: {e}")
            db.rollback()
        finally:
            db.close()

    def get_latest_price(self, coin: str) -> Optional[MarketPrice]:
        """
        코인의 최신 시세 조회 (캐시 우선, 없으면 실시간 조회)

        봇 시작 시 전략 복구를 위해 캐시가 비어있으면 실시간으로 데이터를 가져옵니다.

        Args:
            coin: 코인 심볼

        Returns:
            MarketPrice 또는 None
        """
        # 1단계: 캐시 확인
        cached_price = self.latest_prices.get(coin)
        if cached_price:
            logger.debug(f"[{coin}] 캐시된 시세 사용")
            return cached_price

        # 2단계: 캐시가 비어있으면 실시간 조회 (봇 시작 시 복구용)
        logger.info(f"[{coin}] 캐시 없음, 실시간 시세 조회 중...")

        try:
            # 환율 조회
            exchange_rate = self.exchange_rate_service.get_usd_krw_rate()
            if not exchange_rate:
                logger.error(f"[{coin}] 환율 조회 실패, 시세 조회 불가")
                return None

            # USDT/KRW 가격 조회
            usdt_ticker = self.upbit.fetch_ticker('USDT')
            if not usdt_ticker:
                logger.error(f"[{coin}] USDT 가격 조회 실패, 시세 조회 불가")
                return None

            usdt_krw = usdt_ticker['last']

            # 코인 시세 조회
            market_price = self._scan_coin(coin, exchange_rate, usdt_krw)

            if market_price:
                # 캐시에 저장
                self.latest_prices[coin] = market_price
                logger.info(
                    f"[{coin}] 실시간 시세 조회 완료 - "
                    f"김프: {market_price.premium:.2f}%, "
                    f"업비트: {market_price.krw_price:.0f} KRW, "
                    f"바이비트: {market_price.usd_price:.2f} USD"
                )
                return market_price
            else:
                logger.error(f"[{coin}] 실시간 시세 조회 실패")
                return None

        except Exception as e:
            logger.error(f"[{coin}] 시세 조회 중 오류: {e}")
            return None

    def run_continuous_scan(self, callback=None):
        """
        Run continuous market scanning loop

        Args:
            callback: Optional callback function(market_prices, opportunities)
        """
        logger.info(f"Starting continuous market scan (interval: {self.scan_interval}s)")

        try:
            while True:
                start_time = time.time()

                # Scan market
                market_prices = self.scan_market()

                if market_prices:
                    # Find opportunities
                    # Note: active_strategies should be passed from main process
                    # For now, we'll assume it's managed externally
                    opportunities = self.find_opportunities(market_prices, [])

                    # Save to DB
                    self.save_scan_to_db(market_prices, opportunities)

                    # Call callback if provided
                    if callback:
                        callback(market_prices, opportunities)

                # Wait for next scan
                elapsed = time.time() - start_time
                sleep_time = max(0, self.scan_interval - elapsed)

                if sleep_time > 0:
                    logger.debug(f"Sleeping for {sleep_time:.1f}s until next scan")
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("Market scanner stopped by user")
        except Exception as e:
            logger.error(f"Market scanner error: {e}", exc_info=True)
            raise


def create_scanner() -> MarketScanner:
    """Create and return a market scanner instance"""
    return MarketScanner()

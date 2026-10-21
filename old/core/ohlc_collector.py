"""
OHLC 데이터 수집기
프리미엄 데이터를 일별 OHLC 캔들로 집계하여 분석 및 백테스팅에 활용
"""
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from config.settings import OHLCConfig, BotConfig
from models.database import SessionLocal
from models.models import PremiumOHLC, MarketScan
from utils.logger import get_logger

logger = get_logger('ohlc_collector')


class OHLCCollector:
    """
    OHLC 데이터 수집기

    역할:
    - market_scans 테이블에서 가격 데이터 읽기
    - 일별 OHLC 캔들 생성/업데이트
    - 날짜 경계에서 캔들 확정 (finalize)
    - 오래된 데이터 정리 (retention policy)
    """

    def __init__(self):
        """OHLC 수집기 초기화"""
        self.target_coins = OHLCConfig.get_target_coins()
        self.timeframes = OHLCConfig.TIMEFRAMES
        self.update_interval = OHLCConfig.UPDATE_INTERVAL
        self.retention_days = OHLCConfig.RETENTION_DAYS

        # Track last processed timestamp per coin
        self.last_processed: Dict[str, datetime] = {}

        logger.info(f"OHLC Collector initialized: coins={self.target_coins}, timeframes={self.timeframes}")

    def get_kst_date(self, dt: datetime = None) -> str:
        """
        KST 날짜 문자열 반환 (YYYY-MM-DD)

        Args:
            dt: datetime 객체 (UTC), 기본값은 현재 시각

        Returns:
            KST 타임존의 날짜 문자열
        """
        if dt is None:
            dt = datetime.now(timezone.utc)

        # Convert to KST (UTC+9)
        kst_dt = dt + timedelta(hours=OHLCConfig.KST_OFFSET_HOURS)
        return kst_dt.strftime('%Y-%m-%d')

    def get_current_candle(self, db: Session, coin: str, timeframe: str, date_kst: str) -> Optional[PremiumOHLC]:
        """
        기존 캔들 조회 또는 None 반환

        Args:
            db: 데이터베이스 세션
            coin: 코인 심볼
            timeframe: 타임프레임 (예: '1d')
            date_kst: KST 날짜 문자열

        Returns:
            PremiumOHLC 객체 또는 None
        """
        return db.query(PremiumOHLC).filter(
            and_(
                PremiumOHLC.coin == coin,
                PremiumOHLC.timeframe == timeframe,
                PremiumOHLC.date_kst == date_kst
            )
        ).first()

    def aggregate_candle(self, db: Session, coin: str, date_kst: str) -> Optional[Dict]:
        """
        특정 날짜의 market_scans 데이터에서 OHLC 데이터 집계

        Args:
            db: 데이터베이스 세션
            coin: 코인 심볼
            date_kst: KST 날짜 문자열 (YYYY-MM-DD)

        Returns:
            OHLC 데이터 딕셔너리 또는 데이터 없을 시 None
        """
        # KST 날짜에 대한 UTC 경계 계산
        # KST 00:00 = UTC (전날) 15:00
        # KST 23:59 = UTC (당일) 14:59
        kst_start = datetime.strptime(date_kst, '%Y-%m-%d')
        utc_start = kst_start - timedelta(hours=OHLCConfig.KST_OFFSET_HOURS)
        utc_end = utc_start + timedelta(days=1)

        # 이 날짜 범위에 대한 market_scans 쿼리
        scans = db.query(MarketScan).filter(
            and_(
                MarketScan.coin == coin,
                MarketScan.scanned_at >= utc_start,
                MarketScan.scanned_at < utc_end
            )
        ).order_by(MarketScan.scanned_at.asc()).all()

        if not scans:
            return None

        # 값 추출
        premiums = [s.premium for s in scans]
        krw_prices = [s.krw_price for s in scans]
        usd_prices = [s.usd_price for s in scans]
        exchange_rates = [s.exchange_rate for s in scans]

        # OHLC 구성
        ohlc_data = {
            # 프리미엄 OHLC
            'premium_open': premiums[0],
            'premium_high': max(premiums),
            'premium_low': min(premiums),
            'premium_close': premiums[-1],

            # KRW 가격 OHLC
            'krw_open': krw_prices[0],
            'krw_high': max(krw_prices),
            'krw_low': min(krw_prices),
            'krw_close': krw_prices[-1],

            # USD 가격 OHLC
            'usd_open': usd_prices[0],
            'usd_high': max(usd_prices),
            'usd_low': min(usd_prices),
            'usd_close': usd_prices[-1],

            # 환율 OHLC
            'exchange_rate_open': exchange_rates[0],
            'exchange_rate_high': max(exchange_rates),
            'exchange_rate_low': min(exchange_rates),
            'exchange_rate_close': exchange_rates[-1],

            # 메타데이터
            'scan_count': len(scans)
        }

        return ohlc_data

    def update_candle(self, db: Session, coin: str, timeframe: str, date_kst: str):
        """
        특정 날짜의 OHLC 캔들 업데이트 또는 생성

        Args:
            db: 데이터베이스 세션
            coin: 코인 심볼
            timeframe: 타임프레임 (예: '1d')
            date_kst: KST 날짜 문자열
        """
        # 데이터 집계
        ohlc_data = self.aggregate_candle(db, coin, date_kst)

        if not ohlc_data:
            logger.debug(f"[{coin}] No data for {date_kst}, skipping")
            return

        # 캔들 존재 여부 확인
        candle = self.get_current_candle(db, coin, timeframe, date_kst)

        if candle:
            # 기존 캔들 업데이트
            for key, value in ohlc_data.items():
                setattr(candle, key, value)
            candle.updated_at = datetime.now(timezone.utc)
            logger.debug(f"[{coin}] Updated {date_kst} candle: premium={ohlc_data['premium_close']:.2f}%")
        else:
            # 새 캔들 생성
            candle = PremiumOHLC(
                coin=coin,
                timeframe=timeframe,
                date_kst=date_kst,
                **ohlc_data
            )
            db.add(candle)
            logger.info(f"[{coin}] Created {date_kst} candle: premium={ohlc_data['premium_close']:.2f}%")

        db.commit()

    def finalize_candle(self, db: Session, coin: str, timeframe: str, date_kst: str):
        """
        캔들을 확정 상태로 표시 (날짜가 완료됨)

        Args:
            db: 데이터베이스 세션
            coin: 코인 심볼
            timeframe: 타임프레임
            date_kst: KST 날짜 문자열
        """
        candle = self.get_current_candle(db, coin, timeframe, date_kst)

        if candle and not candle.is_finalized:
            candle.is_finalized = True
            db.commit()
            logger.info(f"[{coin}] Finalized {date_kst} candle")

    def cleanup_old_data(self, db: Session):
        """
        보관 기간보다 오래된 OHLC 데이터 삭제

        Args:
            db: 데이터베이스 세션
        """
        if self.retention_days == 0:
            return  # 보관 기간이 무제한이면 정리하지 않음

        cutoff_date = (datetime.now() - timedelta(days=self.retention_days)).strftime('%Y-%m-%d')

        deleted = db.query(PremiumOHLC).filter(
            PremiumOHLC.date_kst < cutoff_date
        ).delete()

        if deleted > 0:
            db.commit()
            logger.info(f"Cleaned up {deleted} OHLC records older than {cutoff_date}")

    def collect_cycle(self):
        """
        단일 수집 사이클

        - 모든 코인의 오늘 캔들 업데이트
        - 어제 캔들 확정 (필요 시)
        - 오래된 데이터 정리
        """
        try:
            db: Session = SessionLocal()

            now_utc = datetime.now(timezone.utc)
            today_kst = self.get_kst_date(now_utc)
            yesterday_kst = self.get_kst_date(now_utc - timedelta(days=1))

            for coin in self.target_coins:
                for timeframe in self.timeframes:
                    # 오늘 캔들 업데이트
                    self.update_candle(db, coin, timeframe, today_kst)

                    # 어제 캔들 확정 (아직 확정되지 않았으면)
                    self.finalize_candle(db, coin, timeframe, yesterday_kst)

            # 오래된 데이터 정리 (사이클당 1회)
            self.cleanup_old_data(db)

        except Exception as e:
            logger.error(f"OHLC collection cycle error: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

    def run_continuous(self):
        """
        연속 OHLC 수집 루프 실행
        """
        logger.info(f"Starting OHLC collection loop (interval: {self.update_interval}s)")

        try:
            while True:
                start_time = time.time()

                # 수집 사이클 실행
                self.collect_cycle()

                # 다음 사이클까지 대기
                elapsed = time.time() - start_time
                sleep_time = max(0, self.update_interval - elapsed)

                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("OHLC collector stopped by user")
        except Exception as e:
            logger.error(f"OHLC collector error: {e}", exc_info=True)
            raise


def get_ohlc_collector() -> OHLCCollector:
    """싱글톤 OHLC 수집기 인스턴스 반환"""
    if not hasattr(get_ohlc_collector, '_instance'):
        get_ohlc_collector._instance = OHLCCollector()
    return get_ohlc_collector._instance

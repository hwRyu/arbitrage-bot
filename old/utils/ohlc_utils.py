"""
OHLC 유틸리티 함수
OHLC 데이터 조회 및 분석을 위한 헬퍼 함수
"""
from datetime import datetime, timedelta
from typing import List, Optional, Dict
from sqlalchemy.orm import Session
from sqlalchemy import and_, desc

from models.database import SessionLocal
from models.models import PremiumOHLC


def get_ohlc_data(coin: str, timeframe: str = '1d',
                  start_date: str = None, end_date: str = None,
                  limit: int = None) -> List[PremiumOHLC]:
    """
    코인의 OHLC 데이터 조회

    Args:
        coin: 코인 심볼
        timeframe: 타임프레임 ('1d', '4h', '1h')
        start_date: 시작 날짜 (YYYY-MM-DD) KST 기준, 포함
        end_date: 종료 날짜 (YYYY-MM-DD) KST 기준, 포함
        limit: 최대 레코드 수 (최신순 정렬)

    Returns:
        PremiumOHLC 객체 리스트
    """
    db: Session = SessionLocal()

    try:
        query = db.query(PremiumOHLC).filter(
            and_(
                PremiumOHLC.coin == coin,
                PremiumOHLC.timeframe == timeframe
            )
        )

        if start_date:
            query = query.filter(PremiumOHLC.date_kst >= start_date)

        if end_date:
            query = query.filter(PremiumOHLC.date_kst <= end_date)

        query = query.order_by(desc(PremiumOHLC.date_kst))

        if limit:
            query = query.limit(limit)

        return query.all()

    finally:
        db.close()


def get_latest_ohlc(coin: str, timeframe: str = '1d') -> Optional[PremiumOHLC]:
    """
    코인의 최신 OHLC 캔들 조회

    Args:
        coin: 코인 심볼
        timeframe: 타임프레임

    Returns:
        PremiumOHLC 객체 또는 None
    """
    result = get_ohlc_data(coin, timeframe, limit=1)
    return result[0] if result else None


def find_missing_dates(coin: str, timeframe: str = '1d',
                       start_date: str = None, end_date: str = None) -> List[str]:
    """
    OHLC 데이터에서 누락된 날짜 찾기

    Args:
        coin: 코인 심볼
        timeframe: 타임프레임
        start_date: 시작 날짜 (YYYY-MM-DD)
        end_date: 종료 날짜 (YYYY-MM-DD)

    Returns:
        누락된 날짜 문자열 리스트 (YYYY-MM-DD)
    """
    db: Session = SessionLocal()

    try:
        # Get existing dates
        query = db.query(PremiumOHLC.date_kst).filter(
            and_(
                PremiumOHLC.coin == coin,
                PremiumOHLC.timeframe == timeframe
            )
        )

        if start_date:
            query = query.filter(PremiumOHLC.date_kst >= start_date)

        if end_date:
            query = query.filter(PremiumOHLC.date_kst <= end_date)

        existing_dates = set([row[0] for row in query.all()])

        # Generate expected date range
        if not start_date:
            start_date = min(existing_dates) if existing_dates else datetime.now().strftime('%Y-%m-%d')

        if not end_date:
            end_date = datetime.now().strftime('%Y-%m-%d')

        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')

        expected_dates = set()
        current = start
        while current <= end:
            expected_dates.add(current.strftime('%Y-%m-%d'))
            current += timedelta(days=1)

        # Find missing
        missing = sorted(expected_dates - existing_dates)

        return missing

    finally:
        db.close()


def get_ohlc_summary(coin: str, timeframe: str = '1d', days: int = 30) -> Dict:
    """
    OHLC 요약 통계 조회

    Args:
        coin: 코인 심볼
        timeframe: 타임프레임
        days: 분석할 일 수

    Returns:
        요약 통계 딕셔너리
    """
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    data = get_ohlc_data(coin, timeframe, start_date=start_date)

    if not data:
        return {'error': 'No data available'}

    premiums = [d.premium_close for d in data if d.premium_close is not None]

    if not premiums:
        return {'error': 'No premium data available'}

    return {
        'coin': coin,
        'timeframe': timeframe,
        'days_analyzed': len(data),
        'avg_premium': sum(premiums) / len(premiums),
        'max_premium': max(premiums),
        'min_premium': min(premiums),
        'latest_premium': data[0].premium_close if data else None,
        'data_quality': sum([d.scan_count for d in data]) / len(data)  # avg scans per candle
    }

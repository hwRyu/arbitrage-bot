"""
과거 데이터 받아오기 + 저장해두기 (시뮬레이션용)

시뮬레이션은 "그 기간에 이 전략을 돌렸다면?" 을 계산한다. 그러려면 그 기간의 값이 필요하다.

    업비트 코인 1분봉 (원)       → 업비트 쪽 가격
    바이비트 코인 1분봉 (USDT)    → 바이비트 쪽 가격
    업비트 USDT 1분봉 (원)       → 김프 계산 기준 + 업비트 USDT 매매 가격
    달러 환율 1시간봉            → 표시용 (사이트식 김프, 테더 프리미엄)
    바이비트 펀딩비 이력          → 정산 시각별로 실제 적용된 비율

한 번 받은 데이터는 data/history.db 에 저장해두고, 다음엔 모자란 구간만 받는다.
(한 달 치 1분봉은 코인당 4만 개가 넘어서 매번 받으면 느리다)

실시간 봇 DB(data/kimp.db)와 섞이지 않게 파일을 따로 쓴다.
"""
from __future__ import annotations

import bisect
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import Float, Integer, String, Text, create_engine, delete, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

logger = logging.getLogger(__name__)

MINUTE_MS = 60_000
HOUR_MS = 3_600_000

# 캔들이 하나도 없는 구간(거래소 점검 등)을 만나면 이만큼(분) 건너뛰고 계속 받는다
GAP_SKIP_MINUTES = 200

# 달러 환율은 시작보다 이만큼 앞부터 받는다.
# 주말에는 외환시장이 닫혀서 환율이 안 나온다. 월요일 0시에 시작하면 직전 값은 금요일 밤이라
# 하루 전부터만 받으면 비어 있게 된다. (실제로 겪은 문제) → 넉넉하게 7일
FX_LOOKBACK_MS = 7 * 24 * HOUR_MS

# 펀딩비 기록은 200시간씩 끊어서 받는다.
# 바이비트에 '언제부터 200개' 만 주면, 1시간마다 정산하는 코인은 앞부분이 아니라 '끝 쪽 200개' 가 와서
# 중간이 비었다 (ERA 9/22 부터 요청 → 9/30 부터만 받음). 200시간 칸이면 1시간 코인도 200개 안에 다 들어간다.
FUNDING_WINDOW_MS = 200 * HOUR_MS

# 캔들 출처 이름
UPBIT = "upbit"  # 업비트 코인/KRW
BYBIT = "bybit"  # 바이비트 코인/USDT 무기한
UPBIT_USDT = "upbit_usdt"  # 업비트 USDT/KRW (코인 칸은 'USDT')


class HistoryBase(DeclarativeBase):
    """과거 데이터 DB 의 테이블 공통 부모 (실시간 봇 DB 와 따로)"""


class Candle(HistoryBase):
    """1분봉 한 개 (분이 시작하는 시각 기준)"""

    __tablename__ = "candles"

    source: Mapped[str] = mapped_column(String(20), primary_key=True)  # upbit / bybit / upbit_usdt
    coin: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts_ms: Mapped[int] = mapped_column(Integer, primary_key=True)  # 분 시작 시각 (UTC, ms)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)


class FxHourly(HistoryBase):
    """달러 환율 1시간봉 종가"""

    __tablename__ = "fx_hourly"

    ts_ms: Mapped[int] = mapped_column(Integer, primary_key=True)
    rate: Mapped[float] = mapped_column(Float)


class FundingHistory(HistoryBase):
    """바이비트 펀딩비 정산 한 번 (그 시각에 실제로 적용된 비율)"""

    __tablename__ = "funding_history"

    coin: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts_ms: Mapped[int] = mapped_column(Integer, primary_key=True)  # 정산 시각
    rate: Mapped[float] = mapped_column(Float)


class FetchedRange(HistoryBase):
    """
    "이 구간은 이미 받아봤다" 기록

    업비트는 거래가 없던 분에는 캔들이 없다. 그래서 캔들이 비어 있다고 다시 받으면 끝없이 다시 받게 된다.
    받아본 구간을 따로 적어두고, 그 구간은 다시 받지 않는다.
    """

    __tablename__ = "fetched_ranges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30))  # candle:upbit / candle:bybit / fx / funding
    coin: Mapped[str] = mapped_column(String(20))
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)


class SimRun(HistoryBase):
    """시뮬레이션 한 번 실행 기록"""

    __tablename__ = "sim_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String(40))  # 실행한 시각 (UTC ISO)
    from_ms: Mapped[int] = mapped_column(Integer)
    to_ms: Mapped[int] = mapped_column(Integer)
    coins: Mapped[str] = mapped_column(String(200))
    settings_json: Mapped[str] = mapped_column(Text)  # 그때 쓴 설정 (진입 기준, 목표, 비용, 호가 간격 등)
    summary_json: Mapped[str] = mapped_column(Text)  # 결과 요약


class SimTrade(HistoryBase):
    """시뮬레이션 안에서 일어난 거래 한 건 (진입~청산)"""

    __tablename__ = "sim_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    coin: Mapped[str] = mapped_column(String(20))
    opened_at: Mapped[str] = mapped_column(String(40))
    closed_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # 끝까지 안 닫혔으면 None
    entry_premium: Mapped[float] = mapped_column(Float)
    exit_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str] = mapped_column(String(30))  # TAKE_PROFIT / STOP_LOSS / LIQUIDATION / OPEN(끝까지 보유)
    net_pnl_usdt: Mapped[float] = mapped_column(Float)
    net_pnl_percent: Mapped[float] = mapped_column(Float)
    funding_usdt: Mapped[float] = mapped_column(Float)
    cost_usdt: Mapped[float] = mapped_column(Float)


def make_history_session_factory(db_path: str) -> sessionmaker[Session]:
    """과거 데이터 DB 를 열고(없으면 만들고) 세션 만드는 함수를 돌려준다"""
    if db_path != ":memory:":
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    HistoryBase.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# ----------------------------------------------------------------------
# 받아본 구간 계산
# ----------------------------------------------------------------------
def missing_ranges(want: Tuple[int, int], have: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """
    원하는 구간에서 아직 안 받은 부분만 골라낸다

    예) 원하는 구간 [0, 100), 받은 구간 [20, 50), [60, 70)
        → 안 받은 부분 [0, 20), [50, 60), [70, 100)
    """
    start, end = want
    result: List[Tuple[int, int]] = []
    cursor = start
    for a, b in sorted(have):
        if b <= cursor or a >= end:
            continue
        if a > cursor:
            result.append((cursor, min(a, end)))
        cursor = max(cursor, b)
        if cursor >= end:
            break
    if cursor < end:
        result.append((cursor, end))
    return result


class HistoryStore:
    """
    과거 데이터 저장소: 모자란 구간만 받아서 저장하고, 시뮬레이션이 읽을 수 있게 꺼내준다

    받아오는 함수들은 밖에서 넣어준다. (테스트에서는 가짜 함수, 실제로는 ccxt / yfinance)
    """

    def __init__(self, session_factory: sessionmaker[Session]):
        self.sf = session_factory

    # ---------------- 받아본 구간 ----------------
    def _have(self, session: Session, kind: str, coin: str) -> List[Tuple[int, int]]:
        rows = session.scalars(select(FetchedRange).where(FetchedRange.kind == kind, FetchedRange.coin == coin))
        return [(r.start_ms, r.end_ms) for r in rows]

    def _mark(self, session: Session, kind: str, coin: str, start: int, end: int) -> None:
        session.add(FetchedRange(kind=kind, coin=coin, start_ms=start, end_ms=end))

    # ---------------- 캔들 ----------------
    def ensure_candles(self, source: str, coin: str, start_ms: int, end_ms: int,
                       fetch: Callable[[int, int], List[list]], step_ms: int = MINUTE_MS) -> int:
        """
        [start_ms, end_ms) 구간의 캔들이 저장돼 있게 만든다 (없는 부분만 받음)

        Args:
            source: 캔들 출처 이름. 1분봉은 'upbit' 처럼, 다른 간격은 'upbit@15m' 처럼 간격을 붙인다
            fetch: (since_ms, limit) → [[ts, open, high, low, close, volume], ...] 를 돌려주는 함수
            step_ms: 캔들 간격 (1분봉 60,000 / 15분봉 900,000)
        Returns:
            새로 저장한 캔들 개수
        """
        kind = f"candle:{source}"
        saved = 0
        with self.sf() as session:
            todo = missing_ranges((start_ms, end_ms), self._have(session, kind, coin))
        for a, b in todo:
            since = a
            while since < b:
                rows = fetch(since, 1000)
                rows = [r for r in rows if since <= r[0] < b]
                if not rows:
                    # 이 구간엔 캔들이 없다 (거래소 점검, 상장 전 등). 멈추지 말고 건너뛰고 계속 받는다.
                    # (업비트는 "기준 시각 이전 200개" 를 주는 방식이라, 빈 구간을 물으면 그 앞쪽 캔들만 온다)
                    since += GAP_SKIP_MINUTES * step_ms
                    continue
                with self.sf() as session:
                    for ts, o, h, l, c, v in (r[:6] for r in rows):
                        session.merge(Candle(source=source, coin=coin, ts_ms=int(ts), open=float(o), high=float(h),
                                             low=float(l), close=float(c), volume=float(v or 0)))
                    session.commit()
                saved += len(rows)
                since = int(rows[-1][0]) + step_ms
            with self.sf() as session:
                self._mark(session, kind, coin, a, b)
                session.commit()
        return saved

    def load_closes(self, source: str, coin: str, start_ms: int, end_ms: int) -> Dict[int, float]:
        """저장된 1분봉 종가 {분 시작 시각: 종가}"""
        with self.sf() as session:
            rows = session.execute(
                select(Candle.ts_ms, Candle.close).where(
                    Candle.source == source, Candle.coin == coin, Candle.ts_ms >= start_ms, Candle.ts_ms < end_ms)
            )
            return {ts: close for ts, close in rows}

    def load_highs(self, source: str, coin: str, start_ms: int, end_ms: int) -> Dict[int, float]:
        """저장된 1분봉 고가 {분 시작 시각: 고가} (강제청산 확인용: 1분 안에 잠깐 치솟은 가격)"""
        with self.sf() as session:
            rows = session.execute(
                select(Candle.ts_ms, Candle.high).where(
                    Candle.source == source, Candle.coin == coin, Candle.ts_ms >= start_ms, Candle.ts_ms < end_ms)
            )
            return {ts: high for ts, high in rows}

    # ---------------- 달러 환율 ----------------
    def ensure_fx(self, start_ms: int, end_ms: int, fetch: Callable[[int, int], List[Tuple[int, float]]]) -> int:
        """
        [start_ms, end_ms) 구간의 달러 환율 1시간봉이 저장돼 있게 만든다

        시작 직전 값도 필요해서(첫 분에 쓸 환율) 7일 앞부터 받는다. (주말엔 환율이 없어서)
        Args:
            fetch: (start_ms, end_ms) → [(ts, rate), ...]
        """
        start_ms -= FX_LOOKBACK_MS
        saved = 0
        with self.sf() as session:
            todo = missing_ranges((start_ms, end_ms), self._have(session, "fx", "USDKRW"))
        for a, b in todo:
            rows = fetch(a, b)
            with self.sf() as session:
                for ts, rate in rows:
                    session.merge(FxHourly(ts_ms=int(ts), rate=float(rate)))
                self._mark(session, "fx", "USDKRW", a, b)
                session.commit()
            saved += len(rows)
        return saved

    def load_fx(self, start_ms: int, end_ms: int) -> List[Tuple[int, float]]:
        """저장된 환율 [(시각, 환율), ...] 시간 순 (시작 7일 전부터)"""
        with self.sf() as session:
            rows = session.execute(
                select(FxHourly.ts_ms, FxHourly.rate)
                .where(FxHourly.ts_ms >= start_ms - FX_LOOKBACK_MS, FxHourly.ts_ms < end_ms)
                .order_by(FxHourly.ts_ms)
            )
            return [(ts, rate) for ts, rate in rows]

    # ---------------- 펀딩비 ----------------
    def ensure_funding(self, coin: str, start_ms: int, end_ms: int,
                       fetch: Callable[[int], List[Tuple[int, float]]]) -> int:
        """
        [start_ms, end_ms) 구간의 펀딩비 정산 이력이 저장돼 있게 만든다

        Args:
            fetch: since_ms → [(정산 시각, 비율), ...]  since 부터 200시간(FUNDING_WINDOW_MS) 안의 정산
        """
        saved = 0
        with self.sf() as session:
            todo = missing_ranges((start_ms, end_ms), self._have(session, "funding", coin))
        for a, b in todo:
            since = a
            while since < b:
                window_end = since + FUNDING_WINDOW_MS
                rows = [r for r in fetch(since) if since <= r[0] < min(b, window_end)]
                if rows:
                    with self.sf() as session:
                        for ts, rate in rows:
                            session.merge(FundingHistory(coin=coin, ts_ms=int(ts), rate=float(rate)))
                        session.commit()
                    saved += len(rows)
                # 다음 칸으로 (이 칸에 정산이 없어도: 상장 전 등)
                since = window_end
            with self.sf() as session:
                self._mark(session, "funding", coin, a, b)
                session.commit()
        return saved

    def load_funding(self, coin: str, start_ms: int, end_ms: int) -> List[Tuple[int, float]]:
        """저장된 펀딩비 정산 [(시각, 비율), ...] 시간 순 (끝 뒤 하루까지: 마지막에 들고 있는 포지션용)"""
        with self.sf() as session:
            rows = session.execute(
                select(FundingHistory.ts_ms, FundingHistory.rate)
                .where(FundingHistory.coin == coin, FundingHistory.ts_ms >= start_ms,
                       FundingHistory.ts_ms < end_ms + 24 * HOUR_MS)
                .order_by(FundingHistory.ts_ms)
            )
            return [(ts, rate) for ts, rate in rows]


def value_at(series: Sequence[Tuple[int, float]], ts_ms: int) -> Optional[float]:
    """
    시간 순 [(시각, 값)] 에서 ts_ms 시점에 유효한 값 (그 시각 이전의 가장 최근 값)

    예) 환율이 10:00 에 1325, 11:00 에 1326 이면 10:37 의 환율은 1325
        (주말처럼 환율이 안 나오는 동안에는 마지막 값을 계속 쓴다)
    """
    i = bisect.bisect_right(series, (ts_ms, float("inf"))) - 1
    return series[i][1] if i >= 0 else None


# ----------------------------------------------------------------------
# 실제로 받아오는 함수들 (인터넷 필요)
# ----------------------------------------------------------------------
# 거래소가 "요청이 너무 많다"(429) 고 끊을 때 기다렸다 다시 받는 횟수와 대기 시간
RETRY_TIMES = 5
RETRY_WAIT_SEC = 2.0


def _retry(call, what: str):
    """
    거래소 요청을 하다가 막히면 잠깐 기다렸다 다시 해본다.

    과거 1분봉은 한 달 치가 코인당 4만 개라 요청을 수백 번 보낸다.
    업비트는 초당 요청 수가 넘으면 429 를 주는데, 그때 그냥 멈추면 받던 구간이 날아간다.
    그래서 2초 → 4초 → 6초 → 8초 → 10초 로 기다리며 최대 5번 다시 해본다.
    5번 다 실패하면 그때는 멈춘다 (키가 잘못됐거나 인터넷이 끊긴 경우).
    """
    import time

    for attempt in range(1, RETRY_TIMES + 1):
        try:
            return call()
        except Exception as exc:  # 429(요청 과다), 일시적인 네트워크 오류 등
            if attempt == RETRY_TIMES:
                raise
            wait = RETRY_WAIT_SEC * attempt
            logger.warning("%s 받기 실패(%d/%d), %.0f초 뒤 다시: %s",
                           what, attempt, RETRY_TIMES, wait, type(exc).__name__)
            time.sleep(wait)


def ccxt_candle_fetcher(exchange, symbol: str, timeframe: str = "1m") -> Callable[[int, int], List[list]]:
    """ccxt 거래소 객체로 캔들을 받아오는 함수 만들기 (업비트는 한 번에 200개, 바이비트는 1000개까지)"""
    limit = 200 if exchange.id == "upbit" else 1000

    def fetch(since_ms: int, _limit: int) -> List[list]:
        return _retry(lambda: exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit),
                      f"{exchange.id} {symbol} 캔들")

    return fetch


def ccxt_funding_fetcher(exchange, symbol: str) -> Callable[[int], List[Tuple[int, float]]]:
    """ccxt 바이비트로 펀딩비 이력을 받아오는 함수 만들기"""

    def fetch(since_ms: int) -> List[Tuple[int, float]]:
        until = since_ms + FUNDING_WINDOW_MS - 1  # 이 칸 안의 정산만 (설명: FUNDING_WINDOW_MS)
        rows = _retry(lambda: exchange.fetch_funding_rate_history(
            symbol, since=since_ms, limit=200, params={"until": until}), f"{symbol} 펀딩비")
        return [(int(r["timestamp"]), float(r["fundingRate"])) for r in rows if r.get("fundingRate") is not None]

    return fetch


def yfinance_fx_fetcher(start_ms: int, end_ms: int) -> List[Tuple[int, float]]:
    """
    yfinance 에서 달러 환율 1시간봉 받아오기

    yfinance 1시간봉은 최근 약 2년(730일)까지만 준다. 그보다 옛날은 받을 수 없다.
    """
    import yfinance as yf

    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    history = yf.Ticker("KRW=X").history(start=start, end=end, interval="1h")
    return [(int(ts.timestamp() * 1000), float(close)) for ts, close in history["Close"].items()]


# ======================================================================
# 정리 (캔들 캐시 줄이기)
# ======================================================================
# 캔들은 받아둔 캐시라서 지워도 다시 받을 수 있다. 하지만 같은 파일에 들어 있는
# sim_runs / sim_trades (시뮬레이션 결과)는 **되살릴 수 없다** → 절대 건드리지 않는다.
# 자세한 설명: docs/data.md
def candle_stats(sf: sessionmaker[Session]) -> List[dict]:
    """간격(source)별 캔들 개수·코인 수·가장 오래된/최근 시각"""
    with sf() as session:
        rows = session.execute(
            select(Candle.source, func.count(), func.count(func.distinct(Candle.coin)),
                   func.min(Candle.ts_ms), func.max(Candle.ts_ms))
            .group_by(Candle.source).order_by(func.count().desc())
        ).all()
    return [{"source": s, "rows": n, "coins": c, "from_ms": lo, "to_ms": hi} for s, n, c, lo, hi in rows]


def prune_candles(sf: sessionmaker[Session], before_ms: Optional[int] = None,
                  sources: Optional[Sequence[str]] = None, coins: Optional[Sequence[str]] = None) -> dict:
    """
    캔들 지우기 (받아본 구간 기록도 같이 손봐서 다음에 다시 받을 수 있게 한다)

    Args:
        before_ms: 이 시각보다 오래된 캔들만 지운다 (None 이면 기간 제한 없음)
        sources: 지울 간격. 'upbit@5m' 처럼 정확히, 또는 '@5m' 처럼 끝부분만 적어도 된다
        coins: 지울 코인 (None 이면 전부)

    왜 fetched_ranges 도 손보나: 업비트는 거래가 없던 분에 캔들을 안 줘서, '받아본 구간' 을 따로 적어 둔다.
    캔들만 지우면 그 구간은 '이미 받아봤다' 로 남아서 다시 받지 않는다.
      예) 9/1~10/1 을 받아봤고 9/15 이전을 지웠다면 → 받아본 구간을 9/15~10/1 로 줄인다

    Returns:
        {"candles": 지운 캔들 수, "ranges": 손본 구간 수, "sources": 지운 간격 목록}
    """
    def match(source: str) -> bool:
        if not sources:
            return True
        return any(source == s or source.endswith(s) for s in sources)

    with sf() as session:
        targets = [s for (s,) in session.execute(select(Candle.source).distinct()).all() if match(s)]
        if not targets:
            return {"candles": 0, "ranges": 0, "sources": []}
        where = [Candle.source.in_(targets)]
        if before_ms is not None:
            where.append(Candle.ts_ms < before_ms)
        if coins:
            where.append(Candle.coin.in_(list(coins)))
        deleted = session.execute(delete(Candle).where(*where)).rowcount or 0

        # 받아본 구간 기록 손보기 (kind 는 'candle:<source>' 꼴)
        kinds = [f"candle:{s}" for s in targets]
        rq = [FetchedRange.kind.in_(kinds)]
        if coins:
            rq.append(FetchedRange.coin.in_(list(coins)))
        touched = 0
        for row in session.scalars(select(FetchedRange).where(*rq)).all():
            if before_ms is None or row.end_ms <= before_ms:
                session.delete(row)  # 구간 전체가 지워진 범위에 들어감
                touched += 1
            elif row.start_ms < before_ms:
                row.start_ms = before_ms  # 걸쳐 있으면 남은 쪽만 '받아봤다' 로 둔다
                touched += 1
        session.commit()
    return {"candles": deleted, "ranges": touched, "sources": targets}


def vacuum(db_path: str) -> None:
    """지운 만큼 파일 크기를 실제로 줄인다 (SQLite 는 지워도 파일이 안 줄어든다)"""
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute("VACUUM")


def _fetch_cmd(sf, settings, args) -> int:
    """
    캔들 미리 받아두기

    왜 필요한가: 시뮬레이션·차트는 필요한 구간을 그때그때 받는다. 코인 하나 30일이면 업비트 216번 요청이라
    20초쯤 걸린다. 미리 받아두면 그다음부터는 바로 그려진다.
    같은 구간은 다시 받지 않는다 (fetched_ranges).
    """
    from kimp.market_data import USDT_KRW_SYMBOL, MarketData, bybit_symbol, upbit_symbol

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    tf = args.timeframe
    bar_ms = {"1m": MINUTE_MS, "5m": 5 * MINUTE_MS, "15m": 15 * MINUTE_MS}[tf]
    src = (lambda base: base if tf == "1m" else f"{base}@{tf}")  # 1분봉은 시뮬레이션과 같은 이름으로 공유
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) // bar_ms * bar_ms
    start_ms = now_ms - int(args.days * 86_400_000)
    print(f"{tf} 캔들 {args.days:g}일치 받기: {', '.join(coins)}")

    md = MarketData()
    store = HistoryStore(sf)
    got = store.ensure_candles(src(UPBIT_USDT), "USDT", start_ms, now_ms,
                               ccxt_candle_fetcher(md.upbit, USDT_KRW_SYMBOL, tf), step_ms=bar_ms)
    print(f"  업비트 USDT: 새로 {got:,}개")
    for i, coin in enumerate(coins, 1):
        if coin not in md.filter_supported([coin]):
            print(f"  [{i}/{len(coins)}] {coin}: 양쪽 거래소에 다 있지 않아 건너뜀")
            continue
        up = store.ensure_candles(src(UPBIT), coin, start_ms, now_ms,
                                  ccxt_candle_fetcher(md.upbit, upbit_symbol(coin), tf), step_ms=bar_ms)
        by = store.ensure_candles(src(BYBIT), coin, start_ms, now_ms,
                                  ccxt_candle_fetcher(md.bybit, bybit_symbol(coin), tf), step_ms=bar_ms)
        fund = 0
        if not args.no_funding:
            fund = store.ensure_funding(coin, start_ms, now_ms, ccxt_funding_fetcher(md.bybit, bybit_symbol(coin)))
        print(f"  [{i}/{len(coins)}] {coin}: 업비트 {up:,} / 바이비트 {by:,} / 펀딩비 {fund:,} (새로 받은 개수)")
    print(f"끝. {settings.history_db_path} 크기 {os.path.getsize(settings.history_db_path) / 1_048_576:,.1f}MB")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """python -m kimp.history info / prune"""
    import argparse

    from kimp.config import load_settings

    parser = argparse.ArgumentParser(description="과거 데이터(캔들) 상태 보기 · 받아오기 · 정리")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="간격별 캔들 개수와 크기 보기")
    f = sub.add_parser("fetch", help="캔들 미리 받아두기 (시뮬레이션·차트가 빨라진다)")
    f.add_argument("--coins", required=True, help="받을 코인. 예: CAP,SEI")
    f.add_argument("--days", type=float, default=30, help="며칠치 (기본 30)")
    f.add_argument("--timeframe", default="1m", choices=("1m", "5m", "15m"), help="간격 (기본 1m)")
    f.add_argument("--no-funding", action="store_true", help="펀딩비 이력은 받지 않음")
    p = sub.add_parser("prune", help="캔들 지우기 (시뮬레이션 결과는 건드리지 않음)")
    p.add_argument("--days", type=float, default=None, help="이 일수보다 오래된 캔들만 지움 (안 적으면 전부)")
    p.add_argument("--source", default=None,
                   help="지울 간격. 예: '@5m' (5분봉 전부) / 'upbit@15m' / 'bybit'. 안 적으면 전부")
    p.add_argument("--coins", default=None, help="지울 코인. 예: DOOD,ALT (안 적으면 전부)")
    p.add_argument("--yes", action="store_true", help="확인 없이 바로 지움 (안 적으면 무엇을 지울지만 보여줌)")
    p.add_argument("--no-vacuum", action="store_true", help="지운 뒤 파일 크기 줄이기(VACUUM)를 건너뜀")
    args = parser.parse_args(argv)

    settings = load_settings()
    path = settings.history_db_path
    if not os.path.exists(path):
        print(f"{path} 가 없습니다.")
        return 1
    sf = make_history_session_factory(path)
    size_mb = os.path.getsize(path) / 1_048_576
    stats = candle_stats(sf)
    total = sum(s["rows"] for s in stats) or 1
    print(f"{path}  {size_mb:,.1f}MB  캔들 {total:,}개  (1개당 약 {size_mb * 1_048_576 / total:.0f}바이트)")
    print()
    print(f"{'간격':14s} {'캔들':>12s} {'코인':>5s} {'크기(MB)':>9s}  기간 (한국 시간)")
    for s in stats:
        lo = datetime.fromtimestamp(s["from_ms"] / 1000, timezone.utc).astimezone()
        hi = datetime.fromtimestamp(s["to_ms"] / 1000, timezone.utc).astimezone()
        print(f"{s['source']:14s} {s['rows']:>12,} {s['coins']:>5} {s['rows'] / total * size_mb:>9.1f}  "
              f"{lo:%Y-%m-%d} ~ {hi:%Y-%m-%d}")

    if args.cmd == "fetch":
        return _fetch_cmd(sf, settings, args)

    if args.cmd == "info":
        print()
        print("정리하려면: python -m kimp.history prune --days 60        (60일보다 오래된 캔들)")
        print("            python -m kimp.history prune --source @5m    (5분봉 전부)")
        return 0

    before_ms = None
    if args.days:
        before_ms = int((datetime.now(timezone.utc).timestamp() - args.days * 86_400) * 1000)
    sources = [s.strip() for s in args.source.split(",")] if args.source else None
    coins = [c.strip().upper() for c in args.coins.split(",")] if args.coins else None

    print()
    what = []
    if args.days:
        what.append(f"{args.days:g}일보다 오래된 것")
    if sources:
        what.append(f"간격 {', '.join(sources)}")
    if coins:
        what.append(f"코인 {', '.join(coins)}")
    print(f"지울 대상: {' · '.join(what) if what else '캔들 전부'}")
    if not args.yes:
        print("실제로 지우려면 --yes 를 붙이세요. (시뮬레이션 결과 sim_runs/sim_trades 는 지우지 않습니다)")
        return 0

    result = prune_candles(sf, before_ms, sources, coins)
    print(f"지운 캔들 {result['candles']:,}개 · 받아본 구간 기록 {result['ranges']:,}개 정리 "
          f"(간격 {', '.join(result['sources']) or '-'})")
    if not args.no_vacuum:
        print("파일 크기 줄이는 중 (VACUUM)...")
        vacuum(path)
    print(f"끝. 지금 크기 {os.path.getsize(path) / 1_048_576:,.1f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

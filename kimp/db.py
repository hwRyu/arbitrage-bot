"""
DB 테이블 정의 (SQLite 파일 하나에 저장)

테이블 한눈에 보기
------------------------------------------------------------------
- positions         : 포지션 1개 = 진입부터 청산까지 한 사이클. 봇을 껐다 켜도 열린 포지션을 이어서 관리
- fills             : 체결 기록 하나하나 (업비트 매수/매도, 바이비트 숏 열기/닫기)
- funding_events    : 펀딩비를 받거나 낸 기록
- premium_snapshots : 김프 기록 (나중에 분석/백테스트용. 코인마다 1분에 1건 정도)
- paper_balances    : 페이퍼 트레이딩 잔고 (껐다 켜도 유지)

시간은 모두 UTC(세계 표준시)로 저장한다. (한국 시간 = UTC + 9시간)
old 봇은 저장할 때와 읽을 때 시간대가 달라서 날짜가 9시간 밀렸었다.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    """지금 시각 (UTC)"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """모든 테이블의 공통 부모"""


class PositionStatus:
    """포지션 상태"""

    OPEN = "OPEN"  # 양쪽 다 들어가 있음, 지켜보는 중
    CLOSED = "CLOSED"  # 양쪽 다 정리함, 손익 확정


class ExitReason:
    """청산한 이유"""

    TAKE_PROFIT = "TAKE_PROFIT"  # 목표 수익 도달 (익절)
    STOP_LOSS = "STOP_LOSS"  # 손실 한도 도달 (손절)


class Position(Base):
    """
    헷지 포지션 하나 (업비트 현물 매수 + 바이비트 숏, 진입 때 금액을 같게 맞춤)

    금액 헷징이라 양쪽 개수가 다르다. (역프면 업비트 개수가 조금 더 많다)
    페이퍼 모드는 양쪽이 동시에 체결되므로 상태는 OPEN / CLOSED 두 가지뿐이다.
    (실주문을 만들 때 '한쪽만 체결된 상태' 를 추가할 예정)
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    is_paper: Mapped[bool] = mapped_column(default=True)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---------------- 진입할 때 ----------------
    upbit_qty: Mapped[float] = mapped_column(Float)  # 업비트에서 산 개수
    bybit_qty: Mapped[float] = mapped_column(Float)  # 바이비트에서 숏 친 개수
    entry_upbit_price_krw: Mapped[float] = mapped_column(Float)  # 업비트 평균 매수가 (원)
    entry_bybit_price_usdt: Mapped[float] = mapped_column(Float)  # 바이비트 평균 숏 가격 (USDT)
    entry_fx_krw: Mapped[float] = mapped_column(Float)  # 진입 때 달러 환율
    entry_usdt_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 참고: 진입 때 업비트 USDT 가격
    entry_upbit_fee_krw: Mapped[float] = mapped_column(Float)  # 업비트 매수 수수료 (원)
    entry_bybit_fee_usdt: Mapped[float] = mapped_column(Float)  # 바이비트 진입 수수료 (USDT)
    entry_premium: Mapped[float] = mapped_column(Float)  # 실제 체결가로 계산한 진입 김프 (%)
    entry_tether_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 그중 테더 몫 (%)
    entry_coin_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 그중 코인 몫 (%)
    bybit_margin_usdt: Mapped[float] = mapped_column(Float)  # 바이비트에 묶인 증거금 (1배라 숏 금액과 같음)

    # ---------------- 진입할 때 정한 기준 (나중에 .env 를 바꿔도 이 포지션은 이 값으로 청산) ----------------
    entry_threshold_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 진입 기준 김프
    target_net_profit_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 목표 순이익률
    stop_loss_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 손절 순손익률 (0 = 없음)

    # ---------------- 들고 있는 동안 계속 바뀌는 값 ----------------
    funding_usdt: Mapped[float] = mapped_column(Float, default=0.0)  # 지금까지 펀딩비 합계 (+ 받음)
    next_funding_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 다음 펀딩비 정산 시각
    pending_funding_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 다음 정산 때 적용될 비율
    last_net_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 마지막으로 계산한 순손익률
    last_exit_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 마지막으로 계산한 청산 김프

    # ---------------- 청산할 때 ----------------
    exit_upbit_price_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_bybit_price_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_fx_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_usdt_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 참고용
    exit_upbit_fee_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_bybit_fee_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_tether_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 청산 때 테더 몫 (%)
    exit_coin_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 청산 때 코인 몫 (%)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    # ---------------- 확정 손익 (원화) ----------------
    upbit_leg_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 업비트 손익
    bybit_leg_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 바이비트 손익 (USDT 그대로)
    bybit_leg_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 바이비트 손익 (원화 환산)
    funding_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 펀딩비 합계 (원화 환산, + 받음)
    gross_pnl_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 비용 빼기 전
    total_cost_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 수수료 + 전송비
    net_pnl_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 최종 순손익
    net_pnl_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 최종 순손익률


class Fill(Base):
    """체결 한 건"""

    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), index=True)
    exchange: Mapped[str] = mapped_column(String(10))  # upbit / bybit
    side: Mapped[str] = mapped_column(String(20))  # buy / sell / open_short / close_short
    qty: Mapped[float] = mapped_column(Float)  # 개수
    avg_price: Mapped[float] = mapped_column(Float)  # 평균가 (업비트 원, 바이비트 USDT)
    fee: Mapped[float] = mapped_column(Float)
    fee_currency: Mapped[str] = mapped_column(String(10))  # KRW / USDT
    slippage_percent: Mapped[float] = mapped_column(Float)  # 맨 앞 호가보다 얼마나 밀렸나
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FundingEvent(Base):
    """펀딩비 한 번 (숏 기준 + 받음 / - 냄)"""

    __tablename__ = "funding_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), index=True)
    rate: Mapped[float] = mapped_column(Float)  # 펀딩비율 (0.0001 = 0.01%)
    mark_price: Mapped[float] = mapped_column(Float)
    amount_usdt: Mapped[float] = mapped_column(Float)
    funding_time_ms: Mapped[int] = mapped_column(Integer)  # 정산 시각
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PremiumSnapshot(Base):
    """
    김프 기록 한 건

    김프 = 달러 환율 기준(테더 포함). 테더 몫과 코인 몫도 따로 남겨서
    나중에 "김프가 테더 때문인지, 코인 때문인지" 나눠 볼 수 있게 한다.
    바이비트 펀딩비율도 같이 남긴다. (들고 있을 때 비용/수입이 얼마나 되는지 분석용)
    """

    __tablename__ = "premium_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(20), index=True)
    mid_premium: Mapped[float] = mapped_column(Float)  # 가운데 가격 기준 김프 (사이트 숫자와 비슷)
    entry_premium_top: Mapped[float] = mapped_column(Float)  # 맨 앞 호가 진입 김프
    exit_premium_top: Mapped[float] = mapped_column(Float)  # 맨 앞 호가 청산 김프
    fx_krw: Mapped[float] = mapped_column(Float)  # 달러 환율
    usdt_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 업비트 USDT 가격
    tether_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 테더 몫 (%)
    coin_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 코인 몫 (%, 가운데 가격 기준)
    funding_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 바이비트 다음 펀딩비율 (0.0001 = 0.01%)
    next_funding_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 다음 정산 시각
    funding_interval_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 정산 주기 (시간)
    upbit_mid_krw: Mapped[float] = mapped_column(Float)
    bybit_mid_usdt: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PaperBalance(Base):
    """
    페이퍼 트레이딩 잔고 한 줄

    asset 이름 규칙: '<거래소>:<자산>'
        upbit:KRW (업비트 원화), upbit:XRP (업비트 XRP 개수), bybit:USDT (바이비트 USDT)
    """

    __tablename__ = "paper_balances"

    asset: Mapped[str] = mapped_column(String(40), primary_key=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


def make_session_factory(db_path: str) -> sessionmaker[Session]:
    """
    DB 파일을 열고(없으면 만들고) 테이블을 만든 뒤, 세션 만드는 함수를 돌려준다

    Args:
        db_path: SQLite 파일 경로. ':memory:' 면 메모리에만 만든다 (테스트용)

    참고: 테이블 구조가 바뀌면 예전 DB 파일과 맞지 않는다.
          아직 운영 데이터가 없으므로, 구조가 바뀌면 DB 파일을 지우고 새로 시작하면 된다.
    """
    if db_path != ":memory:":
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    # expire_on_commit=False: 저장한 뒤에도 값을 바로 읽을 수 있게 한다
    return sessionmaker(bind=engine, expire_on_commit=False)

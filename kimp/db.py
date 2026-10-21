"""
DB 테이블 정의 (SQLite 파일 하나에 저장)

테이블 한눈에 보기
------------------------------------------------------------------
- positions         : 포지션 1개 = 진입부터 청산까지 한 사이클 (손익은 USDT). 껐다 켜도 이어서 관리
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
    LIQUIDATION = "LIQUIDATION"  # 바이비트 숏 강제청산 → 업비트도 즉시 정리


class Position(Base):
    """
    헷지 포지션 하나 (= 진입 한 번)

    업비트: USDT 를 팔아 코인 매수 → 청산 때 코인을 팔아 USDT 를 되삼
    바이비트: 같은 금액 숏
    손익은 모두 USDT. 금액 헷징이라 양쪽 코인 개수는 다를 수 있다.
    페이퍼 모드는 양쪽이 동시에 체결되므로 상태는 OPEN / CLOSED 두 가지뿐이다.
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    is_paper: Mapped[bool] = mapped_column(default=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---------------- 진입할 때 ----------------
    usdt_sold: Mapped[float] = mapped_column(Float)  # 업비트에서 판 USDT 개수 (= 숏 금액)
    entry_usdt_price_krw: Mapped[float] = mapped_column(Float)  # USDT 판 평균가 (원)
    entry_usdt_fee_krw: Mapped[float] = mapped_column(Float)
    upbit_qty: Mapped[float] = mapped_column(Float)  # 업비트에서 산 코인 개수
    entry_upbit_price_krw: Mapped[float] = mapped_column(Float)  # 코인 평균 매수가 (원)
    entry_upbit_fee_krw: Mapped[float] = mapped_column(Float)
    bybit_qty: Mapped[float] = mapped_column(Float)  # 바이비트 숏 개수
    entry_bybit_price_usdt: Mapped[float] = mapped_column(Float)  # 평균 숏 가격
    entry_bybit_fee_usdt: Mapped[float] = mapped_column(Float)
    entry_premium: Mapped[float] = mapped_column(Float)  # 실제 체결가로 계산한 진입 김프 (코인 몫, %)
    entry_tether_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 참고: 테더 프리미엄
    entry_site_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 참고: 사이트식 김프
    entry_fx_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 참고: 달러 환율

    # ---------------- 바이비트 레버리지 / 증거금 ----------------
    leverage: Mapped[float] = mapped_column(Float, default=1.0)
    bybit_margin_usdt: Mapped[float] = mapped_column(Float)  # 이 포지션에 묶인 증거금 (= 숏 금액 ÷ 레버리지)
    liquidation_price: Mapped[float] = mapped_column(Float)  # 숏 강제청산 가격

    # ---------------- 진입할 때 정한 기준 (나중에 .env 를 바꿔도 이 값으로 청산) ----------------
    entry_threshold_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_net_profit_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ---------------- 들고 있는 동안 바뀌는 값 ----------------
    funding_usdt: Mapped[float] = mapped_column(Float, default=0.0)  # 펀딩비 합계 (+ 받음)
    next_funding_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pending_funding_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_net_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 마지막 평가 순손익률
    last_exit_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 마지막 평가 청산 김프
    last_liq_distance_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 강제청산까지 남은 상승폭

    # ---------------- 청산할 때 ----------------
    exit_upbit_price_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_upbit_fee_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_usdt_price_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # USDT 되산 평균가
    exit_usdt_fee_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    usdt_bought: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 되산 USDT 개수
    exit_bybit_price_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_bybit_fee_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 청산 김프 (코인 몫)
    exit_tether_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_site_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_fx_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    # ---------------- 확정 손익 (USDT) ----------------
    upbit_leg_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 업비트 손익 (수수료 전)
    bybit_leg_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 바이비트 손익 (수수료 전)
    gross_pnl_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 비용 빼기 전
    total_cost_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 수수료 + 전송비
    net_pnl_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 최종 순손익
    net_pnl_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class Fill(Base):
    """체결 한 건"""

    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), index=True)
    exchange: Mapped[str] = mapped_column(String(10))  # upbit / bybit
    side: Mapped[str] = mapped_column(String(20))  # sell_usdt / buy / sell / buy_usdt / open_short / close_short / liquidated
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
    김프 기록 한 건 (코인마다 1분에 1건 정도)

    봇 김프(코인 몫)와 함께 사이트식 김프, 테더 프리미엄, 펀딩비도 남겨서 나중에 분석한다.
    """

    __tablename__ = "premium_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(20), index=True)
    mid_premium: Mapped[float] = mapped_column(Float)  # 봇 김프 (코인 몫, 가운데 가격)
    entry_premium_top: Mapped[float] = mapped_column(Float)  # 맨 앞 호가 진입 김프
    exit_premium_top: Mapped[float] = mapped_column(Float)  # 맨 앞 호가 청산 김프
    usdt_krw: Mapped[float] = mapped_column(Float)  # 업비트 USDT 가운데 가격
    usdt_spread_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # USDT/KRW 호가 간격
    fx_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 달러 환율 (표시용)
    tether_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 테더 프리미엄 (%)
    site_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 사이트식 김프 (%)
    funding_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 다음 펀딩비율
    next_funding_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    funding_interval_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    upbit_mid_krw: Mapped[float] = mapped_column(Float)
    bybit_mid_usdt: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PaperBalance(Base):
    """
    페이퍼 트레이딩 잔고 한 줄

    asset 이름 규칙: '<거래소>:<자산>'
        upbit:USDT (업비트 USDT), upbit:XRP (업비트 XRP 개수), bybit:USDT (바이비트 남은 USDT, 증거금 제외)
        upbit:KRW 는 매매 중 생기는 자투리 원화 (거의 0)
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

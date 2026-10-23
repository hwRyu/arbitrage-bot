"""
DB 테이블 정의 (SQLite 파일 하나에 저장)

테이블 한눈에 보기
------------------------------------------------------------------
- positions         : 포지션 1개 = 진입부터 청산까지 한 사이클 (손익은 USDT). 껐다 켜도 이어서 관리
- fills             : 체결 기록 하나하나 (업비트 매수/매도, 바이비트 숏 열기/닫기)
- funding_events    : 펀딩비를 받거나 낸 기록
- premium_snapshots : 김프 기록 (나중에 분석/백테스트용. 코인마다 1분에 1건 정도)
- paper_balances    : 페이퍼 트레이딩 잔고 (껐다 켜도 유지)
- carry_positions   : 펀딩비 수익 전략 포지션 (바이비트 현물 + 바이비트 숏, kimp/carry.py)
- carry_funding     : 펀딩비 전략 포지션이 받거나 낸 펀딩비 기록
- transfers         : 거래소 간 USDT 송금 (리밸런싱). 요청 → 도착 확인까지
- runs              : 봇을 켠 기록 (모드·코인·설정·git 커밋·종료 사유) — 나중에 "그때 설정이 뭐였지" 용
- decisions         : 진입 판단 기록 (기준 근처에 왔을 때 어느 단계에서 걸렸나) — kimp/telemetry.py
- position_snapshots: 보유 중 포지션 평가 기록 (1분마다 순손익·청산 김프·펀딩·강제청산 거리)
- engine_snapshots  : 양쪽 자본과 스캔 소요 시간 (1분마다)
- events            : WARNING 이상 로그 전부 (예외는 traceback 까지) — "언제 무슨 문제가 있었나"

시간은 모두 UTC(세계 표준시)로 저장한다. (한국 시간 = UTC + 9시간)
old 봇은 저장할 때와 읽을 때 시간대가 달라서 날짜가 9시간 밀렸었다.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    """지금 시각 (UTC)"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """모든 테이블의 공통 부모"""


class PositionStatus:
    """
    포지션 상태 (페이퍼·실거래 똑같이 이 순서로 움직인다)

    OPENING → OPEN → CLOSING → CLOSED
       └→ ABORTED (진입 중 한 단계가 실패해서 끝난 단계를 되돌림)
       └→ ERROR   (되돌리기도 실패. 사람이 거래소에서 직접 정리해야 함, 신규 진입 중단)
    """

    OPENING = "OPENING"  # 진입 주문 중 (세 단계 중 일부만 끝남)
    OPEN = "OPEN"  # 양쪽 다 들어가 있음, 지켜보는 중
    CLOSING = "CLOSING"  # 청산 주문 중 (일부 단계 실패 시 다음 스캔에 이어서)
    CLOSED = "CLOSED"  # 양쪽 다 정리함, 손익 확정
    ABORTED = "ABORTED"  # 진입 실패 → 되돌림 완료 (손익은 수수료 정도)
    ERROR = "ERROR"  # 되돌리기 실패 → 사람이 확인 필요

    UNFINISHED = (OPENING, CLOSING, ERROR)  # 이 코인은 새로 진입하면 안 됨


class ExitReason:
    """청산한 이유"""

    TAKE_PROFIT = "TAKE_PROFIT"  # 목표 수익 도달 (익절)
    STOP_LOSS = "STOP_LOSS"  # 손실 한도 도달 (손절)
    LIQUIDATION = "LIQUIDATION"  # 바이비트 숏 강제청산 → 업비트도 즉시 정리
    FUNDING_DROP = "FUNDING_DROP"  # (펀딩비 전략) 펀딩비가 기준 아래로 떨어져서 정리
    LIQ_GUARD = "LIQ_GUARD"  # (펀딩비 전략) 강제청산이 가까워서 미리 양쪽 정리
    TIMEOUT = "TIMEOUT"  # 한 번만 돌리기(ONE_SHOT)에서 ONE_SHOT_MAX_HOURS 를 넘겨 시장가로 정리
    MANUAL = "MANUAL"  # 사람이 지시해서 정리 (텔레그램 /close)
    FUNDING_COST = "FUNDING_COST"  # 펀딩비로 낸 돈이 MAX_FUNDING_COST_PERCENT 를 넘어서 정리


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

    # ---------------- 분석용 (매매 판단에는 안 쓴다, kimp/telemetry.py) ----------------
    planned_entry_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 판단 때 김프 (실제와의 차이 = 주문 사이 밀림)
    max_net_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 보유 중 순손익 최고 (목표에 얼마나 가까웠나)
    max_net_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    min_net_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 보유 중 순손익 최저
    min_net_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 어느 실행(runs.id)에서 잡았나

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


class FillState:
    """
    체결 기록 상태

    주문을 내기 '전에' SENT 로 먼저 저장한다 (추적번호 client_id 와 함께).
    → 주문 응답이 끊기거나 프로그램이 꺼져도, 다시 켤 때 추적번호로 거래소에 조회해서 결과를 채운다.
    """

    SENT = "SENT"  # 주문을 보냄, 결과 모름
    DONE = "DONE"  # 체결 확인 (일부 체결 포함)
    VOID = "VOID"  # 주문이 안 들어감 (거절 / 조회해도 없음)


class Fill(Base):
    """체결 한 건 (같은 단계가 여러 건일 수 있다: 일부 체결 뒤 남은 만큼 다시 주문)"""

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
    order_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)  # 거래소 주문 번호 (페이퍼는 paper-…)
    cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 체결 금액 (업비트 원, 바이비트 USDT)
    client_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)  # 우리가 붙인 추적번호
    state: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # FillState (비어 있으면 DONE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)  # 주문을 보낸 시각
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)  # 체결을 확인한 시각 (created_at 과의 차 = 주문 소요)
    expected_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 판단 때 호가 소진 계산으로 예상한 평균가 (avg_price 와 비교)


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


class Run(Base):
    """봇을 한 번 켠 기록. 포지션의 run_id 가 여기를 가리킨다 → '그 거래 때 설정·코드가 뭐였나' 를 되짚는다"""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10))  # paper / dry_run / live
    coins: Mapped[str] = mapped_column(String(200))  # 쉼표로 이은 코인
    git_commit: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    settings_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 비밀값 뺀 설정 전부
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_reason: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # one_shot / stopped / …


class Decision(Base):
    """
    진입 판단 한 줄 (kimp/telemetry.py decision)

    맨 앞 호가 김프가 기준 근처에 왔을 때만 남긴다. stage 가 어디서 걸렸는지:
    near / funding_block / size_reject / depth_reject / depth_fail / no_slot / balance_block / rejected / entered
    """

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(20), index=True)
    stage: Mapped[str] = mapped_column(String(20), index=True)
    premium_top: Mapped[float] = mapped_column(Float)  # 맨 앞 호가 진입 김프
    threshold_percent: Mapped[float] = mapped_column(Float)  # 그때 기준
    premium_depth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 수량 소진 평균가 김프 (계산했을 때만)
    size_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bybit_qty: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    funding_8h_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    position_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # entered 일 때
    note: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PositionSnapshot(Base):
    """보유 중 포지션의 '지금 청산하면' 평가 (SNAPSHOT_INTERVAL_SEC 마다 1줄)"""

    __tablename__ = "position_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), index=True)
    net_percent: Mapped[float] = mapped_column(Float)  # 지금 청산하면 순손익률
    net_usdt: Mapped[float] = mapped_column(Float)
    exit_premium: Mapped[float] = mapped_column(Float)  # 지금 청산 김프 (수량 소진 평균가)
    mid_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    upbit_mid_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bybit_mid_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    usdt_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    funding_usdt: Mapped[float] = mapped_column(Float, default=0.0)  # 그때까지 쌓인 펀딩비
    liq_distance_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class EngineSnapshot(Base):
    """엔진 현황 (SNAPSHOT_INTERVAL_SEC 마다 1줄): 양쪽 자본(USDT 환산)과 스캔 소요 시간"""

    __tablename__ = "engine_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upbit_usdt: Mapped[float] = mapped_column(Float)  # 업비트 자본 (USDT + 원화 + 코인 평가)
    bybit_usdt: Mapped[float] = mapped_column(Float)  # 바이비트 자본 (USDT + 증거금 + 숏 평가)
    open_positions: Mapped[int] = mapped_column(Integer)
    scan_ms: Mapped[float] = mapped_column(Float)  # 이번 스캔에 걸린 시간 (현황 로그 시점까지)
    quote_ms: Mapped[float] = mapped_column(Float)  # 그중 시세 조회
    usdt_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fx_krw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Event(Base):
    """WARNING 이상 로그 한 줄 (kimp/telemetry.py DbLogHandler). 예외는 traceback 까지"""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(10), index=True)  # WARNING / ERROR / CRITICAL
    logger: Mapped[str] = mapped_column(String(40))  # 어느 모듈 (kimp.broker, kimp.engine …)
    message: Mapped[str] = mapped_column(String(1000))
    exc_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 예외였으면 traceback
    run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TransferStatus:
    """송금 상태: REQUESTED(출금 요청) → CONFIRMED(받는 쪽 입금 확인) / FAILED"""

    REQUESTED = "REQUESTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


class Transfer(Base):
    """
    거래소 간 USDT 송금 한 건 (리밸런싱)

    direction: 'upbit->bybit' 또는 'bybit->upbit'
    amount_usdt: 보내는 쪽에서 빠지는 양 / fee_usdt: 출금 수수료 (받는 양 ≈ amount − fee)
    """

    __tablename__ = "transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    direction: Mapped[str] = mapped_column(String(20))
    amount_usdt: Mapped[float] = mapped_column(Float)
    fee_usdt: Mapped[float] = mapped_column(Float)
    network: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(12), index=True)
    withdraw_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # 청산 후 / 진입 막힘 / 시작
    note: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    alerted: Mapped[int] = mapped_column(Integer, default=0)  # 늦어서 긴급 알림을 보냈나

    @property
    def to_bybit(self) -> bool:
        """업비트 → 바이비트 방향인가 (수수료·입금 확인 쪽이 방향마다 달라서 자주 묻는다)"""
        return self.direction == "upbit->bybit"


class CarryPosition(Base):
    """
    펀딩비 수익 전략 포지션 하나

    바이비트 현물 qty 개 매수 + 바이비트 무기한 qty 개 숏 (같은 거래소, 같은 개수)
    → 가격이 올라도 내려도 두 쪽 손익이 서로 상쇄되고, 펀딩비(+)를 숏이 받는다.
    손익 = 현물 손익 + 숏 손익 + 받은 펀딩비 − 수수료 (모두 USDT)
    """

    __tablename__ = "carry_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(10), default=PositionStatus.OPEN, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    qty: Mapped[float] = mapped_column(Float)  # 현물 개수 = 숏 개수
    entry_spot_price: Mapped[float] = mapped_column(Float)  # 현물 평균 매수가
    entry_perp_price: Mapped[float] = mapped_column(Float)  # 숏 평균 진입가
    entry_spot_fee: Mapped[float] = mapped_column(Float)  # USDT
    entry_perp_fee: Mapped[float] = mapped_column(Float)  # USDT
    entry_basis_percent: Mapped[float] = mapped_column(Float)  # 선물가 ÷ 현물가 − 1 (%)
    entry_daily_funding_percent: Mapped[float] = mapped_column(Float)  # 진입 때 최근 평균 펀딩 (하루 %)
    leverage: Mapped[float] = mapped_column(Float)
    margin_usdt: Mapped[float] = mapped_column(Float)  # 숏 증거금
    liquidation_price: Mapped[float] = mapped_column(Float)

    funding_usdt: Mapped[float] = mapped_column(Float, default=0.0)  # 받은(+)/낸(−) 펀딩비 합
    funding_count: Mapped[int] = mapped_column(Integer, default=0)
    last_funding_ms: Mapped[int] = mapped_column(Integer, default=0)  # 마지막으로 반영한 정산 시각

    exit_spot_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_perp_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_spot_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_perp_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    spot_leg_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 현물 (팔 때 − 살 때)
    perp_leg_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 숏 (진입가 − 청산가) × 개수
    total_fee_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_pnl_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_pnl_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 포지션 금액 대비

    @property
    def notional_usdt(self) -> float:
        """포지션 금액 = 숏 개수 × 진입가"""
        return self.qty * self.entry_perp_price

    @property
    def capital_used_usdt(self) -> float:
        """이 포지션에 묶인 돈 = 현물 산 돈 + 증거금. 예) 200 USDT, 2배 → 200 + 100 = 300"""
        return self.qty * self.entry_spot_price + self.margin_usdt


class CarryFunding(Base):
    """펀딩비 전략: 정산 한 번 (+ 받음 / − 냄)"""

    __tablename__ = "carry_funding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("carry_positions.id"), index=True)
    funding_time_ms: Mapped[int] = mapped_column(Integer)
    rate: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    amount_usdt: Mapped[float] = mapped_column(Float)


class PaperBalance(Base):
    """
    페이퍼 트레이딩 잔고 한 줄

    asset 이름 규칙: '<거래소>:<자산>'
        upbit:USDT (업비트 USDT), upbit:XRP (업비트 XRP 개수), bybit:USDT (바이비트 남은 USDT, 증거금 제외)
        upbit:KRW 는 매매 중 생기는 자투리 원화 (거의 0)
        carry:USDT, carry:<코인> 은 펀딩비 전략 잔고 (바이비트, 김프 전략과 따로)
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
    add_missing_columns(engine, Base)
    # expire_on_commit=False: 저장한 뒤에도 값을 바로 읽을 수 있게 한다
    return sessionmaker(bind=engine, expire_on_commit=False)


def add_missing_columns(engine, base) -> List[str]:
    """
    예전 DB 파일에 없는 '새 칸' 을 추가한다 (빈 값 허용 칸만)

    create_all 은 없는 '테이블' 만 만들고, 이미 있는 테이블에 새로 생긴 칸은 안 만든다.
    예) fills 테이블에 order_id 칸을 새로 만들었으면 → ALTER TABLE fills ADD COLUMN order_id
    Returns:
        추가한 칸 이름들 ('테이블.칸')
    """
    added = []
    insp = inspect(engine)
    for table in base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have or not col.nullable:
                continue
            ddl = col.type.compile(dialect=engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))
            added.append(f"{table.name}.{col.name}")
    return added

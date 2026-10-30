"""
시뮬레이션: "이 기간에 지금 전략(.env 설정)을 돌렸다면 얼마 벌었을까?"

실행:
    python3 -m kimp.simulate --from 2026-10-01                 # 10월 1일 0시(한국 시간)부터 지금까지
    python3 -m kimp.simulate --from 2026-10-01 --to 2026-10-15
    python3 -m kimp.simulate --from "2026-10-20 09:00" --coins XRP,SOL
    python3 -m kimp.simulate --from 2026-10-01 --verbose       # 진입/청산 하나하나 보기

어떻게 계산하나?
------------------------------------------------------------------
실시간 봇과 **똑같은 엔진과 페이퍼 브로커**를 쓴다. 다른 점은 두 가지뿐이다.
  1) 시세: 거래소 실시간 호가 대신 과거 1분봉 종가로 호가를 만들어 넣는다
     (업비트 코인, 업비트 USDT/KRW, 바이비트 세 가지)
  2) 시계: 진짜 시계 대신 과거 시각을 1분씩 넘기는 가짜 시계
그래서 진입 기준, 목표, 손절, 강제청산, 재진입 대기, 펀딩비가 실제 봇과 같은 규칙으로 돈다.
시작 자본은 PAPER_INITIAL_TOTAL_USDT 를 목표 비율대로 나눠서 시작한다.

꼭 알아둘 한계
------------------------------------------------------------------
- 과거 호가창이 없다 → 호가 간격을 가정한다.
  코인(업비트 코인 + 바이비트): 실시간 김프 기록(data/kimp.db) 측정값 30건 이상 → <코인>_SIM_SPREAD_PERCENT
                               → SIM_DEFAULT_SPREAD_PERCENT 순서. 단 호가 한 칸보다 좁게 잡지 않는다.
    예) DOGE 업비트 109원, 호가 단위 1원 → 최소 0.92%. 안 그러면 종가가 109↔110원을 오가는 것만으로
        김프가 0.9% 씩 튀어 보여서 가짜 수익이 잔뜩 나온다. (실제로 겪은 문제)
  업비트 USDT/KRW: 호가 한 칸 (1원 ÷ 약 1,333원 ≈ 0.075%)
- 호가 두께(슬리피지)는 가정하지 않는다. 포지션이 크면 실제보다 좋게 나온다.
- 1분 사이의 움직임은 안 보인다 (진입/청산은 1분봉 종가). 강제청산만 1분봉 고가로 확인한다.
→ 결과는 "대략 이 정도" 로 보고, 실제 페이퍼 결과와 비교하며 가정값을 맞춰 간다.
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select

from kimp.config import Settings, load_settings
from kimp.db import Position, PositionStatus, make_session_factory
from kimp.spread import (LIVE_SAMPLES, MIN_SNAPSHOTS_FOR_SPREAD, MODE_AUTO, MODE_COMMON, MODE_LIVE,
                         MODE_RECORDS, MODES, accumulate_hint, forced_coins, is_rough,
                         median_spread_depth, resolve_spreads, snapshot_spreads_multi)
from kimp.engine import Engine
from kimp.history import (BYBIT, MINUTE_MS, UPBIT, UPBIT_USDT, HistoryStore, SimRun, SimTrade,
                          make_history_session_factory, value_at)
from kimp.market_data import FundingInfo
from kimp.broker import PaperBroker, broker_kwargs
from kimp.safety import Safety
from kimp.premium import OrderBook, Quote

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 업비트는 거래가 없던 분엔 캔들이 없다. 그럴 땐 최대 이 분(分)까지 직전 가격을 이어서 쓴다.
FILL_FORWARD_MINUTES = 5

# 과거엔 호가 두께를 모르니 '충분히 두껍다' 고 가정한다 (슬리피지 없음)
ASSUMED_DEPTH = 1e15

# 실시간 기록에서 호가 간격을 측정하려면 최소 이만큼은 있어야 믿는다


# ----------------------------------------------------------------------
# 과거 시세를 실시간 시세처럼 넣어주는 가짜 시장
# ----------------------------------------------------------------------
class SimClock:
    """시뮬레이션 시계. 지금 몇 시인지(초)를 들고 있다"""

    def __init__(self, start_sec: float):
        self.now_sec = start_sec

    def __call__(self) -> float:
        return self.now_sec

    @property
    def now_ms(self) -> int:
        return int(self.now_sec * 1000)


class HistoricalMarket:
    """
    과거 1분봉으로 MarketData 흉내를 내는 시장

    엔진은 이게 진짜 거래소인지 과거 데이터인지 모른다. 같은 함수(fetch_quotes 등)를 부르기 때문.
    시각 규칙: 시계가 10:01:00 이면 '10:00~10:01 분봉이 막 끝난 순간' 으로 보고 그 종가를 쓴다.
    """

    def __init__(
        self,
        clock: SimClock,
        precision,
        upbit: Dict[str, Dict[int, float]],
        bybit: Dict[str, Dict[int, float]],
        usdt_krw: Dict[int, float],
        funding: Dict[str, List[Tuple[int, float]]],
        spread_percent: Dict[str, float],
        usdt_spread_percent: float = 0.075,
        bybit_high: Optional[Dict[str, Dict[int, float]]] = None,
        exit_slippage_percent: Optional[Dict[str, float]] = None,
    ):
        """
        Args:
            clock: 시뮬레이션 시계
            precision: 바이비트 주문 단위를 아는 객체 (round_qty, min_qty). 보통 진짜 MarketData
            upbit / bybit: {코인: {분 시작 시각: 종가}}
            usdt_krw: {분 시작 시각: 업비트 USDT 종가}
            funding: {코인: [(정산 시각, 비율), ...]} 시간 순
            spread_percent: {코인: 가정한 코인 호가 간격(%)} = 업비트 코인 + 바이비트 몫
            usdt_spread_percent: 업비트 USDT/KRW 호가 간격(%)
            bybit_high: {코인: {분 시작 시각: 고가}} 강제청산 확인용 (없으면 종가로 확인)
        """
        self.clock = clock
        self.precision = precision
        self.upbit = upbit
        self.bybit = bybit
        self.usdt_krw = usdt_krw
        self.funding = funding
        self._funding_times = {c: [ts for ts, _ in rows] for c, rows in funding.items()}
        self.spread_percent = spread_percent
        # 체결 밀림 가정 (kimp/slippage.py): 청산 다리 — 업비트 코인 매수호가 — 를 이만큼 낮춰서 체결·평가한다.
        # 진입은 실측상 밀림이 거의 0 이라 그대로. 과거 데이터엔 호가판이 없어 실거래의 호가 두께(지정가 FOK 의
        # 안쪽 물량·안전 확인)를 재현할 수 없으니, 그 효과를 '청산이 이만큼 더 불리하다' 는 가정으로 대신한다
        self.exit_slippage_percent = exit_slippage_percent or {}
        self.usdt_spread_percent = usdt_spread_percent
        self.bybit_high = bybit_high or {}

    def _last_closed_minute(self) -> int:
        """시계 기준으로 막 끝난 분봉의 시작 시각"""
        return (self.clock.now_ms // MINUTE_MS) * MINUTE_MS - MINUTE_MS

    @staticmethod
    def _close_at(series: Dict[int, float], minute_ms: int) -> Optional[float]:
        """그 분의 값. 없으면 최대 FILL_FORWARD_MINUTES 분 전까지 거슬러 올라가서 찾는다"""
        for back in range(FILL_FORWARD_MINUTES + 1):
            value = series.get(minute_ms - back * MINUTE_MS)
            if value is not None:
                return value
        return None

    @staticmethod
    def _book(price: float, spread_percent: float) -> OrderBook:
        """
        종가 하나로 호가창 만들기 (가운데 = 종가, 위아래로 반 칸씩)

        예) 종가 1,000원, 호가 간격 0.05% → 매수 호가 999.75원 / 매도 호가 1,000.25원
        """
        half = spread_percent / 100 / 2
        return OrderBook(bids=[(price * (1 - half), ASSUMED_DEPTH)], asks=[(price * (1 + half), ASSUMED_DEPTH)])

    def fetch_quotes(self, coins: List[str], fx_krw: Optional[float] = None) -> Dict[str, Quote]:
        """그 분의 종가로 코인별 시세 묶음 만들기 (데이터가 없는 코인은 빠짐)"""
        minute = self._last_closed_minute()
        usdt = self._close_at(self.usdt_krw, minute)
        if usdt is None:
            return {}
        usdt_book = self._book(usdt, self.usdt_spread_percent)
        quotes: Dict[str, Quote] = {}
        for coin in coins:
            up = self._close_at(self.upbit.get(coin, {}), minute)
            by = self._close_at(self.bybit.get(coin, {}), minute)
            if up is None or by is None:
                continue
            # 코인 호가 간격을 업비트 코인과 바이비트에 반씩 나눠 준다
            leg = self.spread_percent.get(coin, 0.0) / 2
            high = self.bybit_high.get(coin, {}).get(minute)
            upbit_book = self._book(up, leg)
            slip = self.exit_slippage_percent.get(coin, 0.0)
            if slip:
                # 청산 다리(코인 매도 = 매수호가)만 밀림만큼 낮춘다. 매도호가(진입 때 사는 쪽)는 그대로
                upbit_book = OrderBook(bids=[(p * (1 - slip / 100), q) for p, q in upbit_book.bids],
                                       asks=upbit_book.asks)
            quotes[coin] = Quote(coin=coin, upbit=upbit_book, bybit=self._book(by, leg), usdt=usdt_book,
                                 fx_krw=fx_krw, bybit_high=high)
        return quotes

    def fetch_funding(self, coin: str) -> Optional[FundingInfo]:
        """'지금' 이후 첫 펀딩비 정산 (비율은 그 정산 때 실제로 적용된 값. 과거라 이미 알고 있음)"""
        times = self._funding_times.get(coin)
        if not times:
            return None
        i = bisect.bisect_right(times, self.clock.now_ms)
        if i >= len(times):
            return None
        mark = self._close_at(self.bybit.get(coin, {}), self._last_closed_minute())
        if mark is None:
            return None
        ts, rate = self.funding[coin][i]
        return FundingInfo(rate=rate, next_funding_ms=ts, mark_price=mark)

    def round_qty(self, coin: str, qty: float) -> float:
        """바이비트 주문 단위로 내림 (진짜 거래소 정보 사용)"""
        return self.precision.round_qty(coin, qty)

    def min_qty(self, coin: str) -> float:
        """바이비트 최소 주문 개수"""
        return self.precision.min_qty(coin)

    def min_notional_usdt(self, coin: str) -> float:
        """바이비트 최소 주문 금액 (실시간 봇과 같은 값)"""
        return getattr(self.precision, "min_notional_usdt", lambda _c: 0.0)(coin)


class SimFx:
    """과거 달러 환율을 '지금 환율' 처럼 알려주는 공급기 (표시용. 없으면 None)"""

    def __init__(self, clock: SimClock, series: Sequence[Tuple[int, float]]):
        self.clock = clock
        self.series = list(series)

    def get(self) -> Optional[float]:
        return value_at(self.series, self.clock.now_ms) or None


# ----------------------------------------------------------------------
# 결과
# ----------------------------------------------------------------------
@dataclass
class TradeResult:
    """거래 한 건 결과 (USDT)"""

    coin: str
    opened_at: datetime
    closed_at: Optional[datetime]  # None 이면 기간 끝까지 들고 있었음
    entry_premium: float
    exit_premium: Optional[float]
    exit_reason: str  # TAKE_PROFIT / STOP_LOSS / LIQUIDATION / OPEN
    net_pnl_usdt: float
    net_pnl_percent: float
    funding_usdt: float
    cost_usdt: float

    @property
    def hours(self) -> float:
        return ((self.closed_at - self.opened_at).total_seconds() / 3600) if self.closed_at else 0.0


@dataclass
class SimResult:
    """시뮬레이션 전체 결과"""

    start_ms: int
    end_ms: int
    coins: List[str]
    spread_percent: Dict[str, float]
    spread_source: Dict[str, str]
    usdt_spread_percent: float
    capital_start_usdt: float
    fx_end: Optional[float] = None
    trades: List[TradeResult] = field(default_factory=list)
    minutes_simulated: int = 0
    minutes_with_data: Dict[str, int] = field(default_factory=dict)
    transfer_count: int = 0  # 리밸런싱 송금 횟수 (필요할 때만)
    transfer_fee_usdt: float = 0.0  # 송금 수수료 합

    @property
    def closed(self) -> List[TradeResult]:
        """닫은 거래 (끝까지 들고 있는 것 제외)"""
        return [t for t in self.trades if t.exit_reason != "OPEN"]

    @property
    def open(self) -> List[TradeResult]:
        """기간 끝까지 들고 있는 포지션 (마지막 순간에 청산했다고 친 평가)"""
        return [t for t in self.trades if t.exit_reason == "OPEN"]

    @property
    def realized_usdt(self) -> float:
        """확정 손익 = 닫은 거래 합계"""
        return sum(t.net_pnl_usdt for t in self.closed)

    @property
    def unrealized_usdt(self) -> float:
        """평가 손익 = 들고 있는 포지션을 마지막 순간에 청산했다고 친 값"""
        return sum(t.net_pnl_usdt for t in self.open)

    @property
    def total_net_usdt(self) -> float:
        """총 손익 = 확정 + 평가 − 리밸런싱 송금 수수료"""
        return self.realized_usdt + self.unrealized_usdt - self.transfer_fee_usdt

    @property
    def return_percent(self) -> float:
        """시작 자본 대비 수익률 (확정 + 평가)"""
        return self.total_net_usdt / self.capital_start_usdt * 100 if self.capital_start_usdt else 0.0


def _to_dt(ms: int) -> datetime:
    """UTC ms → 시간대가 붙은 datetime"""
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite 에서 읽은 시각은 시간대 정보가 없다. UTC 로 저장했으니 UTC 라고 붙여준다"""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def run_simulation(
    settings: Settings,
    coins: List[str],
    market: HistoricalMarket,
    fx: SimFx,
    clock: SimClock,
    start_ms: int,
    end_ms: int,
    spread_source: Optional[Dict[str, str]] = None,
    progress: Optional[Callable[[float], None]] = None,
) -> SimResult:
    """
    시뮬레이션 실행 (데이터는 이미 준비돼 있어야 함)

    포지션/잔고는 메모리 DB 에서만 움직이고 실시간 봇 DB 는 건드리지 않는다.

    **실시간 봇과 똑같은 엔진·브로커를 쓴다.** 시뮬레이션용 판단 코드를 따로 두면 두 쪽이
    갈라져서, 백테스트에서 좋던 전략이 실거래에서 다르게 움직인다. 그래서 바꿔 끼우는 것은
    세 개뿐이다 — 시세(HistoricalMarket), 시계(SimClock), 환율(SimFx).

    1분씩 시계를 밀면서 engine.step() 을 한 번씩 부른다. 즉 실거래에서 20초마다 하는 판단을
    여기서는 1분마다 하는 것이고, 그래서 1분봉보다 짧은 기회는 못 본다.
    """
    # 시뮬레이션용 설정: 메모리 DB, 김프 기록/현황 로그는 끔 (필요 없고 느려짐)
    # 신호 신뢰도 게이트는 끈다 — 1분 간격 판단에서는 '몇 초 유지' 를 볼 수 없어 한 칸(1분) 늦추는 효과만 난다
    sim_settings = replace(settings, db_path=":memory:", snapshot_interval_sec=float("inf"), signal_filter_enabled=False)
    session_factory = make_session_factory(":memory:")
    # 실시간 봇과 같은 브로커·안전장치 설정 (비상정지 파일만 안 봄)
    safety = Safety(settings.max_consecutive_failures, settings.daily_loss_limit_usdt, None, clock)
    broker = PaperBroker(settings.fees, settings.max_slippage_percent, leverage=settings.bybit_leverage,
                         mmr=settings.bybit_mmr, now_fn=lambda: datetime.fromtimestamp(clock(), timezone.utc),
                         upbit_min_order_krw=settings.upbit_min_order_krw,
                         limit_on_tick=False,   # 가짜 호가(1분봉 + 가정 간격)는 호가 단위 밖 가격 → 판단가 그대로 지정가
                         **broker_kwargs(settings, safety))
    engine = Engine(sim_settings, market, fx, broker, session_factory, list(coins), clock=clock)

    # 첫 봉이 '막 끝난' 시각에서 시작한다. 봉 시작 시각으로 두면 아직 안 끝난 봉의 종가를
    # 보게 되어 미래를 엿보는 셈이 된다 (look-ahead).
    clock.now_sec = (start_ms + MINUTE_MS) / 1000
    engine.startup()
    result = SimResult(
        start_ms=start_ms, end_ms=end_ms, coins=list(coins),
        spread_percent=dict(market.spread_percent), spread_source=dict(spread_source or {}),
        usdt_spread_percent=market.usdt_spread_percent, capital_start_usdt=settings.paper_initial_total_usdt,
    )

    # 1분씩 시간을 넘기면서 엔진을 한 번씩 돌린다
    total = (end_ms - start_ms) // MINUTE_MS
    last_quotes: Dict[str, Quote] = {}
    for i, minute in enumerate(range(start_ms, end_ms, MINUTE_MS)):
        # 이 분봉이 막 끝난 순간으로 시계를 맞춘 뒤 판단한다 (그 봉의 종가까지만 알 수 있다)
        clock.now_sec = (minute + MINUTE_MS) / 1000  # 이 분봉이 막 끝난 순간
        engine.step()
        quotes = market.fetch_quotes(list(coins))
        for c in quotes:
            result.minutes_with_data[c] = result.minutes_with_data.get(c, 0) + 1
        last_quotes.update(quotes)
        result.minutes_simulated += 1
        if progress and i % 1440 == 0:
            progress(i / max(total, 1))
    result.fx_end = fx.get()
    transfers = engine.transfers
    result.transfer_count = len(transfers)
    result.transfer_fee_usdt = sum(t.fee_usdt for t in transfers)

    # 결과 모으기
    with session_factory() as session:
        for pos in session.scalars(select(Position).order_by(Position.id)):
            if pos.status == PositionStatus.CLOSED:
                result.trades.append(TradeResult(
                    coin=pos.coin, opened_at=_utc(pos.opened_at), closed_at=_utc(pos.closed_at),
                    entry_premium=pos.entry_premium, exit_premium=pos.exit_premium, exit_reason=pos.exit_reason,
                    net_pnl_usdt=pos.net_pnl_usdt, net_pnl_percent=pos.net_pnl_percent,
                    funding_usdt=pos.funding_usdt or 0.0, cost_usdt=pos.total_cost_usdt or 0.0,
                ))
            else:
                # 끝까지 들고 있는 포지션: 마지막 순간에 청산했다면? 으로 평가
                quote = last_quotes.get(pos.coin)
                val = broker.value_exit(pos, quote) if quote else None
                result.trades.append(TradeResult(
                    coin=pos.coin, opened_at=_utc(pos.opened_at), closed_at=None,
                    entry_premium=pos.entry_premium, exit_premium=val.exit_premium if val else None,
                    exit_reason="OPEN",
                    net_pnl_usdt=val.pnl.net_usdt if val else 0.0, net_pnl_percent=val.pnl.net_percent if val else 0.0,
                    funding_usdt=val.pnl.funding_usdt if val else 0.0, cost_usdt=val.pnl.total_cost_usdt if val else 0.0,
                ))
    return result


# ----------------------------------------------------------------------
# 호가 간격 정하기
# ----------------------------------------------------------------------
# 호가 단위를 찾을 때, 전체 가격 변화 중 이 비율보다 드물게 나온 차이는 무시한다 (이상한 값 걸러내기)
TICK_MIN_SHARE = 0.01


def tick_size(closes: Sequence[float]) -> Optional[float]:
    """
    가격 기록에서 호가 단위 알아내기 = 이어진 종가 사이의 '자주 나오는' 가장 작은 변화폭

    예) 종가가 109, 110, 109, 111 → 1원
    가끔 섞인 이상한 값은 무시한다. 실제로 업비트 USDT 1분봉 44,210개 중 딱 1개가 1,334.5원이라
    그냥 가장 작은 차이를 보면 호가 단위가 0.5원으로 잘못 잡혔다. (실제는 1원)
    """
    diffs: Dict[float, int] = {}
    for a, b in zip(closes, closes[1:]):
        d = round(abs(b - a), 10)
        if d > 1e-12:
            diffs[d] = diffs.get(d, 0) + 1
    if not diffs:
        return None
    total = sum(diffs.values())
    common = [d for d, n in diffs.items() if n >= total * TICK_MIN_SHARE]
    return min(common) if common else min(diffs)


def _median(values: Sequence[float]) -> float:
    return sorted(values)[len(values) // 2]


def tick_floor_percent(upbit_closes: Sequence[float], bybit_closes: Sequence[float]) -> Optional[float]:
    """
    코인 호가 간격의 최소값(%) = 업비트 호가 한 칸(%) + 바이비트 호가 한 칸(%)

    예) 업비트 DOGE 1원 / 109원 = 0.917%, 바이비트 0.00001 / 0.0817 = 0.012% → 약 0.93%
    """
    up_tick, by_tick = tick_size(upbit_closes), tick_size(bybit_closes)
    if not up_tick or not by_tick:
        return None
    return (up_tick / _median(upbit_closes) + by_tick / _median(bybit_closes)) * 100


def usdt_tick_percent(usdt_closes: Sequence[float]) -> float:
    """업비트 USDT/KRW 호가 한 칸 (%). 예) 1원 / 1,333원 ≈ 0.075%. 모르면 0.075"""
    tick = tick_size(usdt_closes)
    return tick / _median(usdt_closes) * 100 if tick and usdt_closes else 0.075


def apply_tick_floor(spreads: Dict[str, float], sources: Dict[str, str],
                     floors: Dict[str, Optional[float]]) -> None:
    """가정한 호가 간격이 호가 한 칸보다 작으면 호가 한 칸으로 올린다 (spreads/sources 를 직접 고침)"""
    for coin, floor in floors.items():
        if floor is not None and spreads.get(coin, 0.0) < floor:
            spreads[coin] = floor
            sources[coin] = f"{sources.get(coin, '-')} → 호가 단위 하한으로 올림"


def decide_spreads(settings: Settings, coins: List[str], md=None, usdt_krw: float = 1.0,
                   allow_live: bool = True, mode: str = MODE_AUTO) -> Tuple[Dict[str, float], Dict[str, str]]:
    """
    코인별 코인 호가 간격(%) 정하기 — 스크리너와 같은 규칙 (kimp/spread.py)

    코인별 설정 <코인>_SIM_SPREAD_PERCENT 가 있으면 모드와 무관하게 그 값.
    없으면 mode 에 따라: auto(기록 → 지금 호가 → 공통) / records(기록만) /
    common(공통값 통일) / live(지금 호가)

    과거 호가창은 거래소가 주지 않는다. 그래서 기록이 없으면 '지금' 호가를 대신 쓴다.
    공통값 0.1% 를 쓰면 실제(예: CAP 0.27%)보다 싸게 계산돼서 가짜 수익이 나온다.

    Args:
        md: MarketData. 주면 호가창을 조회해서 중간값을 쓴다
        usdt_krw: 업비트 USDT 가격 (물량을 USDT 로 바꿀 때만 쓴다)
        allow_live: False 면 지금 호가창을 조회하지 않는다 (어차피 막힐 실행에서 10초를 버리지 않도록)
        mode: 호가 간격을 어디서 가져올지 (kimp.spread.MODES)
    """
    live = None
    # common 모드는 조회가 필요 없고, records 모드는 기록만 쓰므로 조회하지 않는다
    if md is not None and allow_live and mode not in (MODE_COMMON, MODE_RECORDS):
        forced = forced_coins(coins)
        need = [c for c in coins if c not in forced
                and (mode == MODE_LIVE
                     or snapshot_spreads_multi(settings.snapshot_db_paths(), [c]).get(c, (0, 0))[1]
                     < MIN_SNAPSHOTS_FOR_SPREAD)]
        if need:
            print(f"호가 간격 확인 중 ({len(need)}개 코인, {LIVE_SAMPLES}번 재서 중간값)...")
            live = {c: v[0] for c, v in median_spread_depth(md, need, usdt_krw).items()}
    return resolve_spreads(coins, settings.snapshot_db_paths(), settings.sim_default_spread_percent,
                           live, mode=mode)


# ----------------------------------------------------------------------
# 출력 / 저장
# ----------------------------------------------------------------------
def _kst(dt_or_ms) -> str:
    dt = _to_dt(dt_or_ms) if isinstance(dt_or_ms, int) else _utc(dt_or_ms)
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def format_result(r: SimResult, settings: Settings, verbose: bool = False) -> str:
    """결과를 읽기 쉬운 글로"""
    share = settings.upbit_target_share()
    size = (f"{settings.position_size_usdt:,.0f} USDT 고정" if settings.position_size_usdt
            else f"자동 (업비트 자본 × {settings.capital_usage_percent:.0f}% ÷ {settings.max_open_positions})")
    krw = f" ≈ {r.total_net_usdt * r.fx_end:+,.0f}원" if r.fx_end else ""
    lines = [
        "=" * 76,
        f"시뮬레이션 결과  {_kst(r.start_ms)} ~ {_kst(r.end_ms)} (한국 시간, {r.minutes_simulated / 1440:.1f}일)",
        "=" * 76,
        f"시작 자본: {r.capital_start_usdt:,.0f} USDT → 업비트 {share * 100:.1f}% / 바이비트 {(1 - share) * 100:.1f}% "
        f"(레버리지 {settings.bybit_leverage:.0f}배, 여유분 {settings.bybit_margin_buffer_percent:.0f}%)",
        f"전략: 진입 ≤ {settings.entry_premium_threshold_percent:+.2f}% (코인 몫 김프) / 목표 "
        f"{settings.target_net_profit_percent:.2f}% / 손절 {settings.stop_loss_percent:.2f}% / 포지션 {size} × 최대 "
        f"{settings.max_open_positions}개",
        "호가 간격 가정: " + ", ".join(f"{c} {r.spread_percent[c]:.3f}% ({r.spread_source.get(c, '-')})" for c in r.coins)
        + f", USDT/KRW {r.usdt_spread_percent:.3f}%",
        "",
        f"■ 총 손익: {r.total_net_usdt:+,.2f} USDT{krw}  (시작 자본 대비 {r.return_percent:+.3f}%)",
    ]
    closed, open_ = r.closed, r.open
    wins = [t for t in closed if t.net_pnl_usdt > 0]
    liq = [t for t in closed if t.exit_reason == "LIQUIDATION"]
    lines.append(f"  확정 (닫은 거래 {len(closed)}건): 이익 {len(wins)} / 손해 {len(closed) - len(wins)}"
                 f"{f' (강제청산 {len(liq)})' if liq else ''}  합계 {r.realized_usdt:+,.2f} USDT")
    if open_:
        lines.append(f"  평가 (끝까지 들고 있는 {len(open_)}개, 지금 판다면): {r.unrealized_usdt:+,.2f} USDT  ← 아직 확정 아님")
    if r.trades:
        avg_h = sum(t.hours for t in closed) / len(closed) if closed else 0
        lines.append(f"  평균 보유 {avg_h:.1f}시간 (닫은 거래) / 펀딩비 합계 {sum(t.funding_usdt for t in r.trades):+,.2f} / "
                     f"비용 합계 {sum(t.cost_usdt for t in r.trades):,.2f} USDT")
    lines.append(f"  리밸런싱 송금 {r.transfer_count}번 (필요할 때만), 수수료 합계 {r.transfer_fee_usdt:,.2f} USDT")

    lines.append("")
    lines.append("■ 코인별")
    for coin in r.coins:
        ts = [t for t in r.trades if t.coin == coin]
        days = r.minutes_with_data.get(coin, 0) / 1440
        if not ts:
            lines.append(f"  {coin:<6} 거래 없음 (데이터 {days:.1f}일)")
            continue
        lines.append(f"  {coin:<6} 거래 {len(ts)}건, 손익 {sum(t.net_pnl_usdt for t in ts):+,.2f} USDT "
                     f"(평균 {sum(t.net_pnl_percent for t in ts) / len(ts):+.2f}%/건, 데이터 {days:.1f}일)")

    if verbose and r.trades:
        lines.append("")
        lines.append("■ 거래 내역")
        for t in r.trades:
            end = _kst(t.closed_at) if t.closed_at else "보유 중"
            exit_p = f"{t.exit_premium:+.2f}%" if t.exit_premium is not None else "-"
            lines.append(f"  {t.coin:<6} {_kst(t.opened_at)} → {end:<16} 김프 {t.entry_premium:+.2f}% → {exit_p:<7} "
                         f"{t.exit_reason:<11} {t.net_pnl_usdt:+,.2f} USDT ({t.net_pnl_percent:+.2f}%)")
    lines.append("")
    lines.append("※ 과거 호가창이 없어서 호가 간격은 가정값, 슬리피지는 없다고 보고 계산했다. 대략적인 값으로 볼 것.")
    return "\n".join(lines)


def save_result(history_sf, r: SimResult, settings: Settings) -> int:
    """결과를 history DB 에 저장하고 실행 번호를 돌려준다"""
    snapshot = {
        "entry_premium_threshold_percent": settings.entry_premium_threshold_percent,
        "target_net_profit_percent": settings.target_net_profit_percent,
        "stop_loss_percent": settings.stop_loss_percent,
        "position_size_usdt": settings.position_size_usdt,
        "capital_usage_percent": settings.capital_usage_percent,
        "max_open_positions": settings.max_open_positions,
        "bybit_leverage": settings.bybit_leverage,
        "bybit_margin_buffer_percent": settings.bybit_margin_buffer_percent,
        "reentry_cooldown_sec": settings.reentry_cooldown_sec,
        "fees": asdict(settings.fees),
        "coin_params": {c: asdict(p) for c, p in settings.coin_params.items()},
        "spread_percent": r.spread_percent,
        "spread_source": r.spread_source,
        "usdt_spread_percent": r.usdt_spread_percent,
        "paper_initial_total_usdt": settings.paper_initial_total_usdt,
    }
    summary = {
        "total_net_usdt": r.total_net_usdt,
        "realized_usdt": r.realized_usdt,
        "unrealized_usdt": r.unrealized_usdt,
        "return_percent": r.return_percent,
        "capital_start_usdt": r.capital_start_usdt,
        "closed_trades": len(r.closed),
        "open_positions": len(r.open),
        "minutes_simulated": r.minutes_simulated,
    }
    with history_sf() as session:
        run = SimRun(created_at=datetime.now(timezone.utc).isoformat(), from_ms=r.start_ms, to_ms=r.end_ms,
                     coins=",".join(r.coins), settings_json=json.dumps(snapshot, ensure_ascii=False),
                     summary_json=json.dumps(summary, ensure_ascii=False))
        session.add(run)
        session.flush()
        for t in r.trades:
            session.add(SimTrade(
                run_id=run.id, coin=t.coin, opened_at=t.opened_at.isoformat(),
                closed_at=t.closed_at.isoformat() if t.closed_at else None,
                entry_premium=t.entry_premium, exit_premium=t.exit_premium, exit_reason=t.exit_reason,
                net_pnl_usdt=t.net_pnl_usdt, net_pnl_percent=t.net_pnl_percent,
                funding_usdt=t.funding_usdt, cost_usdt=t.cost_usdt,
            ))
        session.commit()
        return run.id


# ----------------------------------------------------------------------
# 명령줄 실행
# ----------------------------------------------------------------------
def parse_kst(text: str) -> int:
    """'2026-10-01' 또는 '2026-10-01 09:30' (한국 시간) → UTC ms (분 단위로 내림)"""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text.strip(), fmt).replace(tzinfo=KST)
            return int(dt.timestamp() * 1000) // MINUTE_MS * MINUTE_MS
        except ValueError:
            continue
    raise ValueError(f"날짜 형식이 이상합니다: {text} (예: 2026-10-01 또는 '2026-10-01 09:30')")


def prepare_data(store: HistoryStore, coins: List[str], start_ms: int, end_ms: int, md) -> None:
    """시뮬레이션에 필요한 과거 데이터를 받아서 저장 (이미 있는 구간은 건너뜀)"""
    from kimp.history import ccxt_candle_fetcher, ccxt_funding_fetcher, yfinance_fx_fetcher
    from kimp.market_data import bybit_symbol, upbit_symbol

    print("과거 데이터 확인/받는 중... (처음 받는 구간은 시간이 좀 걸립니다)")
    try:
        n = store.ensure_fx(start_ms, end_ms, yfinance_fx_fetcher)
        print(f"  달러 환율 1시간봉 (표시용): 새로 {n:,}개")
    except Exception as exc:  # 표시용이라 실패해도 계속
        print(f"  달러 환율 받기 실패 (표시용이라 계속 진행): {exc}")
    n = store.ensure_candles(UPBIT_USDT, "USDT", start_ms, end_ms, ccxt_candle_fetcher(md.upbit, "USDT/KRW"))
    print(f"  업비트 USDT 1분봉: 새로 {n:,}개")
    for coin in coins:
        n1 = store.ensure_candles(UPBIT, coin, start_ms, end_ms, ccxt_candle_fetcher(md.upbit, upbit_symbol(coin)))
        n2 = store.ensure_candles(BYBIT, coin, start_ms, end_ms, ccxt_candle_fetcher(md.bybit, bybit_symbol(coin)))
        n3 = store.ensure_funding(coin, start_ms, end_ms, ccxt_funding_fetcher(md.bybit, bybit_symbol(coin)))
        print(f"  {coin}: 업비트 1분봉 {n1:,} / 바이비트 1분봉 {n2:,} / 펀딩비 {n3:,} (새로 받은 개수)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """python -m kimp.simulate --from 날짜 [--to 날짜]"""
    from kimp.market_data import MarketData

    parser = argparse.ArgumentParser(description="과거 기간에 지금 전략을 돌려보는 시뮬레이션")
    parser.add_argument("--from", dest="start", required=True, help="시작 (한국 시간) 예: 2026-10-01")
    parser.add_argument("--to", dest="end", default=None, help="끝 (한국 시간). 안 적으면 지금까지")
    parser.add_argument("--coins", default=None, help="코인 목록 (안 적으면 .env 의 COINS) 예: XRP,SOL")
    parser.add_argument("--verbose", action="store_true", help="거래 내역 하나하나 보기")
    parser.add_argument("--no-save", action="store_true", help="결과를 history DB 에 저장하지 않음")
    parser.add_argument("--spread-source", default=MODE_AUTO, choices=list(MODES),
                        help="호가 간격을 어디서 가져올지. auto=기록→지금 호가→공통값 (기본) / "
                             "records=실측 기록만 / common=공통값으로 통일 / live=지금 호가. "
                             "<코인>_SIM_SPREAD_PERCENT 가 있으면 그 코인은 모드와 무관하게 그 값")
    parser.add_argument("--exec-slippage", default=None,
                        help="체결 밀림 가정 %% (청산 때 업비트 매수호가를 이만큼 낮춰 체결·평가). 숫자 또는 "
                             "auto(실거래 체결 기록의 코인별 중앙값). 안 적으면 .env SLIPPAGE_BUFFER_PERCENT")
    parser.add_argument("--rough-spread", action="store_true",
                        help="호가 간격 기록이 없어도 '지금 호가' 로 대충 돌린다 (숫자가 실행마다 바뀜)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s - %(message)s")
    settings = load_settings()

    # 기간: 끝을 안 적으면 '지금' 까지. 아직 안 끝난 분봉은 못 쓰니 직전 분까지로 자른다.
    now_ms = int(time.time() * 1000) // MINUTE_MS * MINUTE_MS - MINUTE_MS
    start_ms = parse_kst(args.start)
    end_ms = min(parse_kst(args.end), now_ms) if args.end else now_ms
    if start_ms >= end_ms:
        print("시작이 끝보다 늦거나 같습니다.")
        return 2

    md = MarketData()  # 바이비트 주문 단위 정보 + 과거 데이터 받기용
    wanted = [c.strip().upper() for c in args.coins.split(",")] if args.coins else settings.coins
    coins = md.filter_supported(wanted)
    if not coins:
        print("시뮬레이션할 코인이 없습니다.")
        return 2

    history_sf = make_history_session_factory(settings.history_db_path)
    store = HistoryStore(history_sf)
    prepare_data(store, coins, start_ms, end_ms, md)

    pad = 10 * MINUTE_MS
    upbit = {c: store.load_closes(UPBIT, c, start_ms - pad, end_ms) for c in coins}
    bybit = {c: store.load_closes(BYBIT, c, start_ms - pad, end_ms) for c in coins}
    usdt = store.load_closes(UPBIT_USDT, "USDT", start_ms - pad, end_ms)
    spreads, sources = decide_spreads(settings, coins, md, usdt[max(usdt)] if usdt else 1.0,
                                      mode=getattr(args, 'spread_source', MODE_AUTO),
                                      allow_live=args.rough_spread)
    rough = [c for c in coins if is_rough(sources.get(c, ""))]
    if rough and not args.rough_spread:
        # 믿을 수 없는 숫자를 내놓는 대신 멈춘다 (실행마다 결과가 바뀌므로)
        print()
        print(accumulate_hint(rough))
        return 3
    if rough:
        print(f"[주의] 호가 간격이 '지금 호가' 기준인 코인: {', '.join(rough)} "
              f"— 숫자가 실행마다 바뀝니다 (--rough-spread)")
    apply_tick_floor(spreads, sources, {c: tick_floor_percent(list(upbit[c].values()), list(bybit[c].values()))
                                        for c in coins})
    from kimp.slippage import resolve as resolve_slippage
    slip = resolve_slippage(args.exec_slippage, settings.fees.slippage_buffer_percent, settings.live_db_path)
    print(slip.describe())
    from kimp.fees import with_bybit_fee_rates
    settings = with_bybit_fee_rates(settings, coins)   # 페이퍼 체결 수수료가 코인별 실제 요율을 쓰게
    clock = SimClock(start_ms / 1000)
    market = HistoricalMarket(
        clock=clock, precision=md, upbit=upbit, bybit=bybit, usdt_krw=usdt,
        funding={c: store.load_funding(c, start_ms, end_ms) for c in coins},
        spread_percent=spreads, usdt_spread_percent=usdt_tick_percent(list(usdt.values())),
        bybit_high={c: store.load_highs(BYBIT, c, start_ms - pad, end_ms) for c in coins},
        exit_slippage_percent={c: slip.for_coin(c) for c in coins},
    )
    fx = SimFx(clock, store.load_fx(start_ms, end_ms))

    if args.verbose:  # 진입/청산 로그도 보기
        logging.getLogger("kimp.engine").setLevel(logging.INFO)
    print(f"시뮬레이션 중... ({(end_ms - start_ms) / MINUTE_MS:,.0f}분)")
    result = run_simulation(settings, coins, market, fx, clock, start_ms, end_ms, sources,
                            progress=lambda p: print(f"  {p * 100:5.1f}%", end="\r"))
    print(format_result(result, settings, verbose=args.verbose))
    if not args.no_save:
        run_id = save_result(history_sf, result, settings)
        print(f"결과 저장: {settings.history_db_path} (실행 번호 {run_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

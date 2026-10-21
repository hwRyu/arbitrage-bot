"""
펀딩비 수익 전략 (캐리) — 김프 전략과 따로 돈다

구조
------------------------------------------------------------------
    바이비트 현물 qty 개 매수  +  바이비트 무기한 qty 개 숏   (같은 거래소, 같은 개수)

- 가격이 10% 오르면: 현물 +10%, 숏 −10% → 합 0. 내려도 마찬가지 → 가격 변동은 상쇄
- 무기한 선물은 정해진 시각(1/4/8시간)마다 롱과 숏이 펀딩비를 주고받는다
  펀딩비율이 + 면 롱이 숏에게 준다 → 우리(숏)가 받는다. 이게 수익이다
- 김프·환율·송금과 상관없다 (한 거래소 안에서 끝남)

예) 150 USDT 포지션, 하루 평균 펀딩 0.03%
    하루 0.045 USDT → 30일 1.35 USDT (+0.9%)
    비용: 현물 0.1% × 2 + 선물 0.055% × 2 = 0.31% (0.47 USDT) → 본전까지 약 10일
    묶이는 돈: 현물 150 + 증거금 150 (1배) = 300 USDT → 자본 대비로는 30일 +0.3%

진입 / 청산 (설정: CARRY_*)
------------------------------------------------------------------
- 진입: 최근 72시간 평균, 최근 24시간 평균이 둘 다 하루 0.03% 이상 + 지금 펀딩비율이 +
        + 선물 매수1호가 ÷ 현물 매도1호가 − 1 ≥ −0.1% (들어가자마자 큰 손해 방지)
- 청산: 최근 24시간 평균이 하루 0% 아래 (= 펀딩비를 내기 시작)
- 강제청산 미리 피하기: 숏이 격리 마진이라 가격이 크게 오르면 (2배면 약 +49.5%) 숏만 강제청산된다.
  그래서 강제청산까지 남은 상승폭이 15% (CARRY_LIQ_GUARD_PERCENT) 안쪽이면 양쪽을 같이 정리한다.
  예) ZEREBRO 2026-10 한 달 시뮬레이션: 안전장치 없이 −33 USDT (증거금 100 잃고 현물은 더 낮은 값에 팔림)
- 그래도 강제청산 가격에 닿으면 (한 시간 안에 급등 등): 증거금을 잃고 현물을 바로 판다

이 파일
------------------------------------------------------------------
- funding_daily_percent / CarryStats: 펀딩비 기록 → '하루 평균 %'
- CarryBroker: 페이퍼 체결과 잔고 (carry:USDT, carry:<코인>)
- CarryEngine: 매 스캔 펀딩비 반영 → 강제청산·청산 확인 → 새 진입. 실시간과 시뮬레이션이 같이 쓴다
- LiveCarryMarket: 바이비트 실시간 시세·펀딩비 (시뮬레이션은 kimp/carry_backtest.py 의 HistoricalCarryMarket)
"""
from __future__ import annotations

import bisect
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kimp.config import Settings
from kimp.history import FUNDING_WINDOW_MS
from kimp.db import CarryFunding, CarryPosition, ExitReason, PaperBalance, PositionStatus
from kimp.pnl import liquidation_price
from kimp.premium import OrderBook, estimate_fill

logger = logging.getLogger(__name__)

HOUR_MS = 3_600_000
CARRY_USDT = "carry:USDT"

# 스테이블코인은 대상 아님
STABLECOINS = {"USDT", "USDC", "DAI", "USDE", "FDUSD", "TUSD", "USD1", "PYUSD", "USDS", "BUSD"}


def carry_coin_key(coin: str) -> str:
    """펀딩비 전략 현물 잔고 이름. 예) XRP → 'carry:XRP'"""
    return f"carry:{coin}"


# ======================================================================
# 펀딩비 통계 (순수 계산)
# ======================================================================
def funding_daily_percent(history: Sequence[Tuple[int, float]], now_ms: int, hours: float) -> Optional[float]:
    """
    최근 hours 시간 동안 정산된 펀딩비 합을 '하루 평균 %' 로

    예) 최근 72시간(3일) 동안 8시간마다 +0.01% × 9번 = +0.09% → 하루 +0.03%
        1시간마다 정산되는 코인이면 72번 합 ÷ 3일
    Args:
        history: 시간 순 [(정산 시각 ms, 비율)]. 비율 0.0001 = 0.01%
    Returns:
        기간 안에 정산이 한 번도 없으면 None
    """
    start = now_ms - hours * HOUR_MS
    lo = bisect.bisect_right(history, (start, float("inf")))
    hi = bisect.bisect_right(history, (now_ms, float("inf")))
    window = history[lo:hi]
    if not window:
        return None
    return sum(r for _, r in window) * 100 / (hours / 24)


@dataclass(frozen=True)
class CarryStats:
    """
    코인 하나의 펀딩비 상태

    Attributes:
        long_daily: 최근 72시간 하루 평균 (%)
        short_daily: 최근 24시간 하루 평균 (%)
        latest_rate: 지금(다음 정산) 펀딩비율 (소수). 실시간은 거래소 예고값, 시뮬레이션은 직전 정산값
    """

    long_daily: Optional[float]
    short_daily: Optional[float]
    latest_rate: Optional[float]


def make_stats(history: Sequence[Tuple[int, float]], now_ms: int, settings: Settings,
               latest_rate: Optional[float] = None) -> CarryStats:
    """기록으로 CarryStats 만들기 (latest_rate 가 없으면 마지막 정산값)"""
    if latest_rate is None:
        i = bisect.bisect_right(history, (now_ms, float("inf"))) - 1
        latest_rate = history[i][1] if i >= 0 else None
    return CarryStats(
        long_daily=funding_daily_percent(history, now_ms, settings.carry_entry_lookback_hours),
        short_daily=funding_daily_percent(history, now_ms, settings.carry_exit_lookback_hours),
        latest_rate=latest_rate,
    )


def entry_ok(st: Optional[CarryStats], settings: Settings) -> bool:
    """
    진입 조건: 72시간 평균 ≥ 기준, 24시간 평균 ≥ 기준, 지금 비율 > 0

    24시간 평균도 보는 이유: 3일 평균은 높아도 어제부터 식었다면 들어가자마자 나오게 된다.
    """
    x = settings.carry_entry_min_daily_percent
    return (st is not None and st.long_daily is not None and st.short_daily is not None
            and st.latest_rate is not None
            and st.long_daily >= x and st.short_daily >= x and st.latest_rate > 0)


def exit_due(st: Optional[CarryStats], settings: Settings) -> bool:
    """청산 조건: 최근 24시간 평균 < 기준 (기본 0% = 펀딩비를 내기 시작). 기록이 없으면 판단 안 함"""
    return st is not None and st.short_daily is not None and st.short_daily < settings.carry_exit_below_daily_percent


# ======================================================================
# 시세
# ======================================================================
@dataclass(frozen=True)
class CarryQuote:
    """
    코인 하나의 바이비트 현물 / 무기한 호가창

    Attributes:
        perp_high: (시뮬레이션) 이번 봉 동안 선물 최고가. 강제청산 확인용. 실시간은 None (매도1호가로 확인)
    """

    coin: str
    spot: OrderBook
    perp: OrderBook
    perp_high: Optional[float] = None

    @property
    def perp_mid(self) -> Optional[float]:
        return self.perp.mid

    @property
    def basis_percent(self) -> Optional[float]:
        """진입 때 기준: 선물 매수1호가 ÷ 현물 매도1호가 − 1 (%). 예) 2.001 / 2.000 − 1 = +0.05%"""
        if not self.perp.best_bid or not self.spot.best_ask:
            return None
        return (self.perp.best_bid / self.spot.best_ask - 1) * 100


# ======================================================================
# 페이퍼 체결 / 잔고
# ======================================================================
class CarryRejected(Exception):
    """진입 못 함 (호가 부족, 잔고 부족 등). 아무것도 바뀌지 않았다"""


@dataclass(frozen=True)
class CarryExit:
    """지금 청산한다면? (가격, 수수료, 손익)"""

    spot_price: float
    perp_price: float
    spot_fee: float
    perp_fee: float
    spot_leg: float
    perp_leg: float
    net_usdt: float
    net_percent: float


class CarryBroker:
    """
    펀딩비 전략 가짜 체결기

    잔고: carry:USDT (남은 USDT, 증거금 제외), carry:<코인> (현물 개수)
    진입: USDT −= 현물 값 + 현물 수수료 + 증거금 + 선물 수수료, 코인 += qty
    청산: USDT += 현물 판 값 − 수수료 + 증거금 + 숏 손익 − 수수료, 코인 −= qty
    펀딩비: 정산 때마다 USDT 에 바로 더하기/빼기
    """

    def __init__(self, settings: Settings, now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.s = settings
        self.spot_fee = settings.carry_spot_fee_rate
        self.perp_fee = settings.fees.bybit_taker_fee_rate
        self.leverage = settings.carry_leverage
        self._now = now_fn

    @staticmethod
    def balance(session: Session, asset: str) -> float:
        row = session.get(PaperBalance, asset)
        return row.amount if row else 0.0

    @staticmethod
    def _add(session: Session, asset: str, delta: float) -> None:
        row = session.get(PaperBalance, asset)
        if row is None:
            row = PaperBalance(asset=asset, amount=0.0)
            session.add(row)
        row.amount += delta

    def ensure_initial_balance(self, session: Session, capital_usdt: float) -> bool:
        """처음 실행이면 펀딩비 전략 자본을 넣는다. 이미 있으면 그대로 (True = 새로 넣음)"""
        if session.get(PaperBalance, CARRY_USDT) is not None:
            return False
        session.add(PaperBalance(asset=CARRY_USDT, amount=capital_usdt))
        session.commit()
        logger.info("[펀딩비 전략] 페이퍼 시작 자본 %s USDT (바이비트)", f"{capital_usdt:,.2f}")
        return True

    @staticmethod
    def open_positions(session: Session) -> List[CarryPosition]:
        return list(session.scalars(select(CarryPosition).where(CarryPosition.status == PositionStatus.OPEN)))

    def open(self, session: Session, quote: CarryQuote, qty: float, daily_funding: float) -> CarryPosition:
        """
        진입: 현물 qty 개 시장가 매수 + 선물 qty 개 시장가 숏

        Raises:
            CarryRejected: 호가가 모자라거나, 너무 밀리거나, 돈이 모자람
        """
        spot = estimate_fill(quote.spot.asks, qty)
        perp = estimate_fill(quote.perp.bids, qty)
        if not spot or not spot.is_complete or not perp or not perp.is_complete:
            raise CarryRejected("호가가 모자람")
        for name, fill in (("현물", spot), ("선물", perp)):
            if fill.slippage_percent > self.s.max_slippage_percent:
                raise CarryRejected(f"{name}에서 너무 밀림 {fill.slippage_percent:.3f}%")
        spot_fee = spot.cost * self.spot_fee
        perp_fee = perp.cost * self.perp_fee
        margin = perp.cost / self.leverage
        need = spot.cost + spot_fee + margin + perp_fee
        have = self.balance(session, CARRY_USDT)
        if have < need:
            raise CarryRejected(f"USDT 부족 {have:,.2f} < {need:,.2f}")

        pos = CarryPosition(
            coin=quote.coin, status=PositionStatus.OPEN, opened_at=self._now(), qty=qty,
            entry_spot_price=spot.avg_price, entry_perp_price=perp.avg_price,
            entry_spot_fee=spot_fee, entry_perp_fee=perp_fee,
            entry_basis_percent=(perp.avg_price / spot.avg_price - 1) * 100,
            entry_daily_funding_percent=daily_funding, leverage=self.leverage, margin_usdt=margin,
            liquidation_price=liquidation_price(perp.avg_price, self.leverage, self.s.bybit_mmr),
            funding_usdt=0.0, funding_count=0, last_funding_ms=int(self._now().timestamp() * 1000),
        )
        session.add(pos)
        self._add(session, CARRY_USDT, -need)
        self._add(session, carry_coin_key(quote.coin), qty)
        session.commit()
        return pos

    def _result(self, pos: CarryPosition, spot_price: float, perp_price: float, perp_lost_margin: bool) -> CarryExit:
        """청산 가격으로 손익 계산"""
        spot_fee = pos.qty * spot_price * self.spot_fee
        perp_fee = 0.0 if perp_lost_margin else pos.qty * perp_price * self.perp_fee
        spot_leg = pos.qty * (spot_price - pos.entry_spot_price)
        # 강제청산이면 숏 손익 = 증거금 전부 잃음
        perp_leg = -pos.margin_usdt if perp_lost_margin else pos.qty * (pos.entry_perp_price - perp_price)
        fees = pos.entry_spot_fee + pos.entry_perp_fee + spot_fee + perp_fee
        net = spot_leg + perp_leg + pos.funding_usdt - fees
        return CarryExit(spot_price, perp_price, spot_fee, perp_fee, spot_leg, perp_leg, net,
                         net / pos.notional_usdt * 100)

    def value(self, pos: CarryPosition, quote: CarryQuote) -> Optional[CarryExit]:
        """지금 시장가로 청산한다면? (현물은 매수 호가에 팔고, 숏은 매도 호가에 되산다)"""
        spot = estimate_fill(quote.spot.bids, pos.qty)
        perp = estimate_fill(quote.perp.asks, pos.qty)
        if not spot or not perp:
            return None
        return self._result(pos, spot.avg_price, perp.avg_price, perp_lost_margin=False)

    def value_liquidation(self, pos: CarryPosition, quote: CarryQuote) -> Optional[CarryExit]:
        """강제청산: 숏 증거금을 잃고 현물은 지금 매수 호가에 판다"""
        spot = estimate_fill(quote.spot.bids, pos.qty)
        if not spot:
            return None
        return self._result(pos, spot.avg_price, pos.liquidation_price, perp_lost_margin=True)

    def close(self, session: Session, pos: CarryPosition, val: CarryExit, reason: str) -> None:
        """청산 확정: 잔고 반영 + 포지션 기록"""
        liquidated = reason == ExitReason.LIQUIDATION
        self._add(session, carry_coin_key(pos.coin), -pos.qty)
        back = pos.qty * val.spot_price - val.spot_fee  # 현물 판 돈
        if not liquidated:
            back += pos.margin_usdt + val.perp_leg - val.perp_fee  # 증거금 + 숏 손익
        self._add(session, CARRY_USDT, back)
        pos.status, pos.closed_at, pos.exit_reason = PositionStatus.CLOSED, self._now(), reason
        pos.exit_spot_price, pos.exit_perp_price = val.spot_price, val.perp_price
        pos.exit_spot_fee, pos.exit_perp_fee = val.spot_fee, val.perp_fee
        pos.spot_leg_usdt, pos.perp_leg_usdt = val.spot_leg, val.perp_leg
        pos.total_fee_usdt = pos.entry_spot_fee + pos.entry_perp_fee + val.spot_fee + val.perp_fee
        pos.net_pnl_usdt, pos.net_pnl_percent = val.net_usdt, val.net_percent
        session.commit()

    def apply_funding(self, session: Session, pos: CarryPosition, ts_ms: int, rate: float, price: float) -> float:
        """
        정산 한 번 반영. 숏이라 비율 + 면 받고 − 면 낸다

        예) 100개 숏, 가격 2.0, 비율 +0.01% → 100 × 2.0 × 0.0001 = +0.02 USDT
        """
        amount = pos.qty * price * rate
        pos.funding_usdt += amount
        pos.funding_count += 1
        pos.last_funding_ms = ts_ms
        session.add(CarryFunding(position_id=pos.id, funding_time_ms=ts_ms, rate=rate, price=price,
                                 amount_usdt=amount))
        self._add(session, CARRY_USDT, amount)
        return amount

    def equity(self, session: Session, quotes: Dict[str, CarryQuote]) -> float:
        """지금 전부 정리한다면 USDT 몇 개 (남은 USDT + 포지션마다 현물 판 돈 + 증거금 + 숏 손익 − 수수료)"""
        total = self.balance(session, CARRY_USDT)
        for pos in self.open_positions(session):
            q = quotes.get(pos.coin)
            val = self.value(pos, q) if q else None
            if val:
                total += pos.qty * val.spot_price - val.spot_fee + pos.margin_usdt + val.perp_leg - val.perp_fee
            else:
                total += pos.qty * pos.entry_spot_price + pos.margin_usdt
        return total


# ======================================================================
# 엔진
# ======================================================================
class CarryEngine:
    """
    펀딩비 전략 엔진 (스캔 한 번 = step())

    1) 후보 코인 펀딩비 상태 받기 (market.stats)
    2) 가진 포지션: 지나간 정산 반영 → 강제청산 확인 → 청산 조건 확인
    3) 자리가 남으면 진입 조건 맞는 코인 중 72시간 평균이 높은 순으로 진입

    market 이 갖춰야 할 것 (실시간: LiveCarryMarket, 시뮬레이션: HistoricalCarryMarket)
        stats(now_ms, include) → {코인: CarryStats}
        quote(coin) → CarryQuote 또는 None
        funding_since(coin, after_ms, now_ms) → [(정산 시각, 비율)]  (after 초과 ~ now 이하)
        round_qty(coin, qty), min_qty(coin)
    """

    def __init__(self, settings: Settings, market, broker: CarryBroker, session_factory: sessionmaker[Session],
                 clock: Callable[[], float] = time.time):
        self.s = settings
        self.market = market
        self.broker = broker
        self.sf = session_factory
        self._clock = clock
        self._last_log: Dict[str, float] = {}
        # 코인별 '다시 들어가도 되는 시각' (초). 청산하면 CARRY_REENTRY_COOLDOWN_HOURS 뒤
        self._reentry_at: Dict[str, float] = {}

    def _log_throttled(self, key: str, level: int, msg: str, every_sec: float = 600.0) -> None:
        now = self._clock()
        if now - self._last_log.get(key, -1e18) >= every_sec:
            self._last_log[key] = now
            logger.log(level, msg)

    def startup(self) -> None:
        with self.sf() as session:
            self.broker.ensure_initial_balance(session, self.s.carry_capital_usdt)
            for pos in self.broker.open_positions(session):
                logger.info("[펀딩비 전략] 이전 포지션 이어서 관리: #%d %s %g개 (펀딩 누적 %+.4f USDT)",
                            pos.id, pos.coin, pos.qty, pos.funding_usdt)

    def step(self) -> None:
        now_ms = int(self._clock() * 1000)
        with self.sf() as session:
            positions = self.broker.open_positions(session)
            stats = self.market.stats(now_ms, [p.coin for p in positions])
            held = set()
            for pos in positions:
                quote = self.market.quote(pos.coin)
                if quote is None:
                    held.add(pos.coin)
                    continue
                self._apply_funding(session, pos, quote, now_ms)
                if self._check_liquidation(session, pos, quote):
                    continue
                if exit_due(stats.get(pos.coin), self.s):
                    val = self.broker.value(pos, quote)
                    if val:
                        self._close(session, pos, val, ExitReason.FUNDING_DROP)
                        st = stats[pos.coin]
                        logger.info("[펀딩비 청산] #%d %s | 24시간 평균 %+.4f%%/일 < %+.3f%% | 보유 %.1f일, 펀딩 %+.4f, "
                                    "현물 %+.4f, 숏 %+.4f, 수수료 %.4f → 순손익 %+.4f USDT (%+.3f%%)",
                                    pos.id, pos.coin, st.short_daily, self.s.carry_exit_below_daily_percent,
                                    self._held_days(pos), pos.funding_usdt, val.spot_leg, val.perp_leg,
                                    pos.total_fee_usdt, val.net_usdt, val.net_percent)
                        continue
                held.add(pos.coin)
            self._try_entries(session, stats, held, now_ms)

    def _close(self, session: Session, pos: CarryPosition, val: CarryExit, reason: str) -> None:
        self.broker.close(session, pos, val, reason)
        self._reentry_at[pos.coin] = self._clock() + self.s.carry_reentry_cooldown_hours * 3600

    def _held_days(self, pos: CarryPosition) -> float:
        opened = pos.opened_at if pos.opened_at.tzinfo else pos.opened_at.replace(tzinfo=timezone.utc)
        return (self._clock() - opened.timestamp()) / 86400

    def _apply_funding(self, session: Session, pos: CarryPosition, quote: CarryQuote, now_ms: int) -> None:
        """진입 뒤 지나간 정산을 반영 (가격은 지금 선물 가운데 가격)"""
        price = quote.perp_mid or pos.entry_perp_price
        for ts, rate in self.market.funding_since(pos.coin, pos.last_funding_ms, now_ms):
            amount = self.broker.apply_funding(session, pos, ts, rate, price)
            logger.debug("[펀딩비] #%d %s 비율 %+.4f%% → %+.4f USDT", pos.id, pos.coin, rate * 100, amount)
        session.commit()

    def _check_liquidation(self, session: Session, pos: CarryPosition, quote: CarryQuote) -> bool:
        """강제청산이면 정리하고 True. 가까우면 양쪽을 미리 정리하고 True (안전장치를 끄면 경고만)"""
        price = quote.perp_high or quote.perp.best_ask
        if not price:
            return False
        if price >= pos.liquidation_price:
            val = self.broker.value_liquidation(pos, quote)
            if val:
                self._close(session, pos, val, ExitReason.LIQUIDATION)
                logger.error("[펀딩비 강제청산] #%d %s 선물 %.6g ≥ %.6g → 증거금 %.2f USDT 잃음, 현물 정리 → 순손익 %+.2f USDT",
                             pos.id, pos.coin, price, pos.liquidation_price, pos.margin_usdt, val.net_usdt)
                return True
        left = (pos.liquidation_price / price - 1) * 100
        guard = self.s.carry_liq_guard_percent
        if guard > 0 and left < guard:
            val = self.broker.value(pos, quote)
            if val:
                self._close(session, pos, val, ExitReason.LIQ_GUARD)
                logger.warning("[펀딩비 전략 · 강제청산 피하기] #%d %s 강제청산까지 +%.1f%% < %.0f%% → 양쪽 정리, "
                               "순손익 %+.2f USDT (펀딩 %+.2f)", pos.id, pos.coin, left, guard, val.net_usdt,
                               pos.funding_usdt)
                return True
        if left < self.s.liquidation_warn_percent:
            self._log_throttled(f"carry-liq-{pos.id}", logging.WARNING,
                                f"[펀딩비 전략 · 강제청산 주의] #{pos.id} {pos.coin} 강제청산까지 +{left:.1f}% "
                                f"→ 현물 이익 일부를 선물 증거금으로 옮기세요")
        return False

    def _try_entries(self, session: Session, stats: Dict[str, CarryStats], held: set, now_ms: int) -> None:
        slots = self.s.carry_max_positions - len(held)
        if slots <= 0:
            return
        picks = sorted((st.long_daily, coin) for coin, st in stats.items()
                       if coin not in held and entry_ok(st, self.s)
                       and self._clock() >= self._reentry_at.get(coin, 0.0))
        for daily, coin in reversed(picks):
            if slots <= 0:
                break
            quote = self.market.quote(coin)
            if quote is None or quote.basis_percent is None:
                continue
            if quote.basis_percent < self.s.carry_min_entry_basis_percent:
                self._log_throttled(f"carry-basis-{coin}", logging.INFO,
                                    f"[펀딩비 전략] {coin} 진입 보류: 선물이 현물보다 {quote.basis_percent:+.3f}% "
                                    f"(기준 {self.s.carry_min_entry_basis_percent:+.2f}%)")
                continue
            size = self.s.carry_size_usdt()
            qty = self.market.round_qty(coin, size / quote.spot.best_ask)
            if qty <= 0 or qty < self.market.min_qty(coin):
                continue
            try:
                pos = self.broker.open(session, quote, qty, daily)
            except CarryRejected as exc:
                self._log_throttled(f"carry-reject-{coin}", logging.INFO, f"[펀딩비 전략] {coin} 진입 보류: {exc}")
                continue
            slots -= 1
            st = stats[coin]
            logger.info("[펀딩비 진입] #%d %s | 현물 %g개 매수 %.6g + 숏 %.6g (%.0f배, 강제청산 %.6g) | "
                        "펀딩 72시간 %+.4f%%/일, 24시간 %+.4f%%/일, 지금 %+.4f%% | 차이 %+.3f%%",
                        pos.id, coin, pos.qty, pos.entry_spot_price, pos.entry_perp_price, pos.leverage,
                        pos.liquidation_price, st.long_daily, st.short_daily, (st.latest_rate or 0) * 100,
                        pos.entry_basis_percent)

    def run(self, stop_event: threading.Event) -> None:
        """메인 루프 (main.py 가 따로 스레드로 돌린다)"""
        self.startup()
        logger.info("[펀딩비 전략] 시작: %.0f초마다 스캔, 자본 %s USDT, 포지션 %s USDT × 최대 %d개, %.0f배",
                    self.s.carry_scan_interval_sec, f"{self.s.carry_capital_usdt:,.0f}",
                    f"{self.s.carry_size_usdt():,.0f}", self.s.carry_max_positions, self.s.carry_leverage)
        while not stop_event.is_set():
            try:
                self.step()
            except Exception:
                logger.exception("[펀딩비 전략] 스캔 중 오류 (다음 스캔에서 다시)")
            stop_event.wait(self.s.carry_scan_interval_sec)


# ======================================================================
# 실시간 시세 (바이비트)
# ======================================================================
def spot_symbol(coin: str) -> str:
    return f"{coin}/USDT"


def perp_symbol(coin: str) -> str:
    return f"{coin}/USDT:USDT"


def eligible_coins(markets: dict) -> List[str]:
    """바이비트 현물 COIN/USDT 와 무기한 COIN/USDT:USDT 가 둘 다 있는 코인 (스테이블코인 제외)"""
    out = []
    for m in markets.values():
        if not (m.get("spot") and m.get("quote") == "USDT" and m.get("active", True)):
            continue
        coin = m["base"]
        perp = markets.get(perp_symbol(coin))
        if coin not in STABLECOINS and perp and perp.get("active", True):
            out.append(coin)
    return sorted(set(out))


class LiveCarryMarket:
    """
    바이비트 실시간 시세 / 펀딩비 (펀딩비 전략 전용 ccxt 객체: 김프 엔진과 다른 스레드라 따로 만든다)

    후보 고르기 (CARRY_REFRESH_SEC 마다)
      CARRY_COINS 가 있으면 그 코인들, 없으면 자동:
        무기한 24시간 거래대금 ≥ CARRY_MIN_VOLUME_USDT 이고 지금 펀딩비율이 + 인 코인 중 높은 순 CARRY_UNIVERSE_MAX 개
      + 들고 있는 코인은 항상 포함
      → 그 코인들의 펀딩비 기록을 (모자란 만큼만) 받아서 72시간/24시간 평균 계산
    """

    def __init__(self, settings: Settings, exchange=None):
        import ccxt

        self.s = settings
        self.ex = exchange or ccxt.bybit({"enableRateLimit": True})
        self.ex.load_markets()
        self.coins = eligible_coins(self.ex.markets)
        self._hist: Dict[str, List[Tuple[int, float]]] = {}
        self._stats: Dict[str, CarryStats] = {}
        self._last_refresh = -1e18

    def _update_history(self, coin: str, now_ms: int) -> List[Tuple[int, float]]:
        """펀딩비 기록에서 모자란 뒷부분만 받기"""
        hist = self._hist.setdefault(coin, [])
        keep_from = now_ms - (self.s.carry_entry_lookback_hours + 24) * HOUR_MS
        since = hist[-1][0] + 1 if hist else int(keep_from)
        # 200시간 칸씩 받는다 (그냥 '언제부터 200개' 면 1시간 정산 코인은 끝 쪽만 와서 중간이 빈다)
        while since < now_ms:
            until = since + FUNDING_WINDOW_MS - 1
            rows = self.ex.fetch_funding_rate_history(perp_symbol(coin), since=since, limit=200,
                                                      params={"until": until})
            new = sorted((int(r["timestamp"]), float(r["fundingRate"])) for r in rows
                         if r.get("fundingRate") is not None and since <= int(r["timestamp"]) <= until)
            hist.extend(new)
            since = until + 1
        while hist and hist[0][0] < keep_from:
            hist.pop(0)
        return hist

    def stats(self, now_ms: int, include: Iterable[str] = ()) -> Dict[str, CarryStats]:
        include = [c for c in include]
        if now_ms / 1000 - self._last_refresh < self.s.carry_refresh_sec and all(c in self._stats for c in include):
            return self._stats
        self._last_refresh = now_ms / 1000
        tickers = self.ex.fetch_tickers([perp_symbol(c) for c in self.coins])
        rate = {}
        for c in self.coins:
            t = tickers.get(perp_symbol(c)) or {}
            info = t.get("info") or {}
            fr = info.get("fundingRate")
            rate[c] = (float(fr) if fr not in (None, "") else None, float(t.get("quoteVolume") or 0))
        if self.s.carry_coins:
            cands = [c for c in self.s.carry_coins if c in rate]
        else:
            ok = [c for c, (r, vol) in rate.items() if r is not None and r > 0 and vol >= self.s.carry_min_volume_usdt]
            cands = sorted(ok, key=lambda c: rate[c][0], reverse=True)[:self.s.carry_universe_max]
        stats = {}
        for c in dict.fromkeys(cands + include):
            try:
                hist = self._update_history(c, now_ms)
            except Exception as exc:
                logger.warning("[펀딩비 전략] %s 펀딩비 기록 받기 실패: %s", c, exc)
                continue
            stats[c] = make_stats(hist, now_ms, self.s, latest_rate=rate.get(c, (None, 0))[0])
        self._stats = stats
        good = sorted(((st.long_daily or 0, c) for c, st in stats.items() if entry_ok(st, self.s)), reverse=True)
        logger.info("[펀딩비 전략] 후보 %d개 중 진입 조건 맞음 %d개%s", len(stats), len(good),
                    (": " + ", ".join(f"{c} {d:+.3f}%/일" for d, c in good[:8])) if good else "")
        return stats

    def funding_since(self, coin: str, after_ms: int, now_ms: int) -> List[Tuple[int, float]]:
        return [(ts, r) for ts, r in self._hist.get(coin, []) if after_ms < ts <= now_ms]

    def quote(self, coin: str) -> Optional[CarryQuote]:
        try:
            s = self.ex.fetch_order_book(spot_symbol(coin), limit=50)
            p = self.ex.fetch_order_book(perp_symbol(coin), limit=50)
        except Exception as exc:
            logger.warning("[펀딩비 전략] %s 호가창 받기 실패: %s", coin, exc)
            return None

        def book(raw) -> OrderBook:
            return OrderBook(bids=[(float(a), float(b)) for a, b, *_ in raw["bids"]],
                             asks=[(float(a), float(b)) for a, b, *_ in raw["asks"]])

        return CarryQuote(coin=coin, spot=book(s), perp=book(p))

    def round_qty(self, coin: str, qty: float) -> float:
        """현물·선물 둘 다 주문 단위에 맞게 내림 (더 거친 쪽에 맞춤)"""
        q = float(self.ex.amount_to_precision(perp_symbol(coin), qty))
        return float(self.ex.amount_to_precision(spot_symbol(coin), q)) if q > 0 else 0.0

    def min_qty(self, coin: str) -> float:
        mins = [(self.ex.market(sym).get("limits", {}).get("amount", {}).get("min") or 0.0)
                for sym in (spot_symbol(coin), perp_symbol(coin))]
        return float(max(mins))

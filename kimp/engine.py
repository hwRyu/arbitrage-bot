"""
전략 엔진: 시세 보기 → 가진 포지션 관리 → 새로 진입

전략 한 줄 요약 (자세한 건 docs/concept.md)
------------------------------------------------------------------
코인 몫 김프(업비트 USDT 가격 기준)가 낮을 때
    업비트에서 USDT 를 팔아 코인을 사고 + 바이비트에서 같은 금액만큼 숏을 치고,
김프가 올라서 모든 비용을 빼고도 목표 수익이 나면 양쪽을 같이 정리한다 (업비트는 다시 USDT 로).
코인 가격, 달러 환율, 테더 프리미엄은 봇 손익에 영향이 없다. (서로 상쇄됨)

스캔 한 번에 하는 일 (순서가 중요)
------------------------------------------------------------------
1) 시세: 모든 코인 + 업비트 USDT/KRW 를 '같은 순간' 으로 가져온다 (달러 환율은 표시용)
2) 가진 포지션마다
   - 펀딩비 반영
   - 강제청산 확인: 바이비트 가격이 강제청산 가격에 닿았으면 업비트도 즉시 정리.
     가까워졌으면 '증거금 추가' 긴급 경고
   - "지금 청산하면 얼마?" → 목표/손절이면 청산
   (청산하면 돈이 풀리니까 새 진입보다 먼저 한다)
3) 청산이 있었으면 양쪽 자본 비율 확인 → 많이 벗어났으면 리밸런싱 알림
4) 새 진입: 김프가 기준보다 낮은 코인에 진입 (포지션 크기는 설정값 또는 자동)
5) 김프 기록 저장 + 현황 로그
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kimp.config import Settings
from kimp.db import ExitReason, FundingEvent, Position, PremiumSnapshot
from kimp.market_data import MarketData
from kimp.paper_broker import BYBIT_USDT, UPBIT_USDT, EntryRejected, ExitValuation, PaperBroker
from kimp.pnl import estimated_exit_premium, margin_to_add_usdt
from kimp.premium import Quote

logger = logging.getLogger(__name__)

# 업비트 원화 마켓 최소 주문 금액 (원). 이보다 작으면 거래소가 주문을 거절한다.
UPBIT_MIN_ORDER_KRW = 5_000

# 같은 경고를 너무 자주 찍지 않으려고 두는 간격 (초)
LOG_COOLDOWN_SEC = 60.0


class Engine:
    """김프 헷지 전략 엔진 (페이퍼 트레이딩)"""

    def __init__(
        self,
        settings: Settings,
        market: MarketData,
        fx,
        broker: PaperBroker,
        session_factory: sessionmaker[Session],
        coins: List[str],
        clock: Callable[[], float] = time.time,
    ):
        """
        Args:
            settings: 봇 설정
            market: 거래소 시세 조회기
            fx: 달러 환율 공급기 (get() 이 FxQuote 또는 None). 표시용이라 None 이어도 매매는 된다
            broker: 페이퍼 트레이딩 체결기
            session_factory: DB 연결 만드는 함수
            coins: 운용할 코인 (양쪽 거래소에 다 있는 것만)
            clock: 지금 시각(초). 평소엔 진짜 시계, 시뮬레이션에서는 과거 시각을 알려주는 가짜 시계
        """
        self.s = settings
        self.market = market
        self.fx = fx
        self.broker = broker
        self.session_factory = session_factory
        self.coins = coins
        self._clock = clock

        # 아래 시각들은 메모리에만 둔다 (clock 기준, 초)
        self._last_snapshot: Dict[str, float] = {}
        self._last_funding_check: Dict[str, float] = {}
        self._last_log: Dict[str, float] = {}
        self._reentry_allowed_at: Dict[str, float] = {}
        self._last_status_log = 0.0

    # ------------------------------------------------------------------
    # 실행
    # ------------------------------------------------------------------
    def startup(self) -> None:
        """시작할 때: 페이퍼 시작 자본 나눠 넣기 + 이전 포지션 안내 + 자본 비율 확인"""
        with self.session_factory() as session:
            self.broker.ensure_initial_balances(session, self.s.paper_initial_total_usdt, self.s.upbit_target_share())
            logger.info("잔고: 업비트 USDT %s / 바이비트 USDT %s (증거금 제외)",
                        f"{self.broker.balance(session, UPBIT_USDT):,.2f}",
                        f"{self.broker.balance(session, BYBIT_USDT):,.2f}")
            positions = self.broker.open_positions(session)
            for pos in positions:
                logger.info("이전 포지션 이어서 관리: #%d %s (업비트 %.6g개 / 바이비트 %g개) 진입 김프 %+.3f%%",
                            pos.id, pos.coin, pos.upbit_qty, pos.bybit_qty, pos.entry_premium)
                if pos.coin not in self.coins:
                    logger.warning("%s 는 COINS 에 없지만 열린 포지션이 있어서 청산할 때까지 관리합니다", pos.coin)
                    self.coins.append(pos.coin)
            self._check_rebalance(session, positions, {}, when="시작")

    def run(self, stop_event: threading.Event) -> None:
        """메인 루프. 종료 신호가 올 때까지 SCAN_INTERVAL_SEC 마다 step(). 에러가 나도 다음 스캔에서 다시"""
        self.startup()
        logger.info("엔진 시작: 코인 %s, %.1f초마다 스캔", ", ".join(self.coins), self.s.scan_interval_sec)
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                self.step()
            except Exception:
                logger.exception("스캔 중 에러 (다음 스캔에서 다시 시도)")
            stop_event.wait(max(0.0, self.s.scan_interval_sec - (time.monotonic() - started)))
        logger.info("엔진 종료 (열린 포지션은 DB 에 남아서 다음 실행 때 이어서 관리)")

    def step(self) -> None:
        """스캔 한 번 (순서는 파일 맨 위 설명 참고)"""
        fxq = self.fx.get() if self.fx else None
        quotes = self.market.fetch_quotes(self.coins, fxq.rate if fxq else None)
        if not quotes:
            return

        with self.session_factory() as session:
            open_positions = {p.coin: p for p in self.broker.open_positions(session)}

            closed_any = False
            for coin, pos in list(open_positions.items()):
                quote = quotes.get(coin)
                if quote is None:
                    continue  # 이번에 시세를 못 받음 → 다음 스캔에서 다시
                self._update_funding(session, pos, quote)
                if self._check_liquidation(session, pos, quote) or self._manage_position(session, pos, quote):
                    del open_positions[coin]
                    closed_any = True

            if closed_any:
                self._check_rebalance(session, list(open_positions.values()), quotes, when="청산 후")

            self._try_entries(session, quotes, open_positions)
            self._save_snapshots(session, quotes)
            self._log_status(session, quotes, open_positions)

    # ------------------------------------------------------------------
    # 가진 포지션
    # ------------------------------------------------------------------
    def _manage_position(self, session: Session, pos: Position, quote: Quote) -> bool:
        """
        "지금 청산하면 얼마?" 를 계산하고, 목표나 손절이면 청산한다

        Returns:
            청산했으면 True
        """
        valuation = self.broker.value_exit(pos, quote)
        if valuation is None:
            self._log_throttled(f"thin-{pos.coin}", logging.WARNING,
                                f"{pos.coin} 청산할 호가가 모자라서 이번엔 평가를 건너뜁니다")
            return False

        pnl = valuation.pnl
        pos.last_net_percent = pnl.net_percent
        pos.last_exit_premium = valuation.exit_premium
        session.commit()

        # 목표 / 손절은 진입할 때 포지션에 저장한 값 (들고 있는 중에 .env 를 바꿔도 처음 값으로)
        target, stop = self._exit_rules(pos)
        reason: Optional[str] = None
        if pnl.net_percent >= target:
            reason = ExitReason.TAKE_PROFIT
        elif stop < 0 and pnl.net_percent <= stop:
            reason = ExitReason.STOP_LOSS
        if reason is None:
            logger.debug("%s 보유 중: 청산 김프 %+.3f%%, 순손익 %+.3f%% (%+.2f USDT)",
                         pos.coin, valuation.exit_premium, pnl.net_percent, pnl.net_usdt)
            return False

        self._close(session, pos, valuation, reason)
        return True

    def _close(self, session: Session, pos: Position, valuation: ExitValuation, reason: str,
               liquidated: bool = False) -> None:
        """청산 실행 + 로그 + 재진입 대기 시작"""
        final = self.broker.close_hedge(session, pos, valuation, reason, liquidated=liquidated)
        self._reentry_allowed_at[pos.coin] = self._clock() + self.s.reentry_cooldown_sec
        level = logging.CRITICAL if liquidated else logging.INFO
        logger.log(
            level,
            "[청산 %s] #%d %s | 김프 %+.3f%% → %+.3f%% | 업비트 USDT %.2f개 → %.2f개 (%+.2f) | 바이비트 %+.2f "
            "| 펀딩 %+.2f - 비용 %.2f = 순손익 %+.2f USDT (%+.3f%%)",
            reason, pos.id, pos.coin, pos.entry_premium, valuation.exit_premium,
            pos.usdt_sold, valuation.plan.usdt_bought, valuation.plan.usdt_bought - pos.usdt_sold,
            final.bybit_leg_usdt, final.funding_usdt, final.total_cost_usdt, final.net_usdt, final.net_percent,
        )

    def _check_liquidation(self, session: Session, pos: Position, quote: Quote) -> bool:
        """
        바이비트 숏 강제청산 확인

        - 바이비트 가격이 강제청산 가격 이상이면: 숏은 강제청산된 것으로 보고(증거금 잃음),
          업비트 코인도 즉시 팔아 USDT 로 되돌린다 → True
        - 강제청산까지 남은 상승폭이 LIQUIDATION_WARN_PERCENT 보다 작으면: 증거금 추가 긴급 경고

        예) 강제청산 가격 1.793, 지금 1.60 → 남은 상승폭 12% → 경고 (기준 15%)
        """
        price = quote.liquidation_check_price
        if not price:
            return False
        if price >= pos.liquidation_price:
            valuation = self.broker.value_liquidation(pos, quote)
            if valuation is None:
                self._log_throttled(f"liq-thin-{pos.coin}", logging.CRITICAL,
                                    f"{pos.coin} 강제청산 가격 도달! 업비트 호가가 모자라 정리하지 못했습니다")
                return False
            logger.critical("[강제청산] #%d %s 바이비트 가격 %.6f ≥ 강제청산 가격 %.6f → 증거금 %.2f USDT 손실, "
                            "업비트 코인 즉시 정리", pos.id, pos.coin, price, pos.liquidation_price,
                            pos.bybit_margin_usdt)
            self._close(session, pos, valuation, ExitReason.LIQUIDATION, liquidated=True)
            return True

        distance = (pos.liquidation_price / price - 1) * 100
        pos.last_liq_distance_percent = distance
        if distance < self.s.liquidation_warn_percent:
            add = margin_to_add_usdt(pos.entry_bybit_price_usdt, price, pos.bybit_qty, pos.bybit_margin_usdt,
                                     self.s.liquidation_warn_percent * 2, self.s.bybit_mmr)
            self._log_throttled(
                f"liq-warn-{pos.coin}", logging.WARNING,
                f"[긴급] #{pos.id} {pos.coin} 강제청산까지 {distance:.1f}% 남음 (지금 {price:.6f}, 강제청산 "
                f"{pos.liquidation_price:.6f}) → 바이비트 포지션에 증거금 약 {add:,.2f} USDT 추가하세요",
            )
        return False

    def _exit_rules(self, pos: Position) -> tuple[float, float]:
        """이 포지션의 (목표, 손절). 진입 때 저장한 값을 쓰고, 없으면(옛날 포지션) 지금 설정"""
        params = self.s.params_for(pos.coin)
        target = pos.target_net_profit_percent
        stop = pos.stop_loss_percent
        return (target if target is not None else params.target_net_profit_percent,
                stop if stop is not None else params.stop_loss_percent)

    def _update_funding(self, session: Session, pos: Position, quote: Quote) -> None:
        """
        펀딩비: 지난 정산이 있으면 반영하고, 다음 정산 정보를 받아둔다

          1) 지난 정산 반영 — 매 스캔 확인 (인터넷 필요 없음). 기준 가격은 지금 바이비트 가운데 가격
             → 정산 직후 조회가 실패하거나 1분 안에 청산해도 펀딩비가 빠지지 않는다
          2) 다음 정산 정보 받기 — FUNDING_REFRESH_SEC 마다 (방금 반영했으면 바로)
        같은 정산을 두 번 반영하지 않으려고 이미 반영한 마지막 정산 시각과 비교한다.
        """
        now = self._clock()
        now_ms = int(now * 1000)
        last_applied = session.scalar(
            select(func.max(FundingEvent.funding_time_ms)).where(FundingEvent.position_id == pos.id)
        ) or 0

        just_applied = False
        if (pos.next_funding_ms and pos.pending_funding_rate is not None
                and now_ms >= pos.next_funding_ms and pos.next_funding_ms > last_applied and quote.bybit.mid):
            rate = pos.pending_funding_rate
            amount = self.broker.apply_funding(session, pos, rate, quote.bybit.mid, pos.next_funding_ms)
            logger.info("[펀딩비] #%d %s 비율 %+.4f%% → %+.4f USDT (지금까지 %+.4f)",
                        pos.id, pos.coin, rate * 100, amount, pos.funding_usdt)
            last_applied = pos.next_funding_ms
            just_applied = True

        if not just_applied and now - self._last_funding_check.get(pos.coin, 0.0) < self.s.funding_refresh_sec:
            return
        self._last_funding_check[pos.coin] = now
        info = self.market.fetch_funding(pos.coin)
        if info is None:
            return
        if info.next_funding_ms > last_applied:
            pos.next_funding_ms = info.next_funding_ms
            pos.pending_funding_rate = info.rate
            session.commit()

    # ------------------------------------------------------------------
    # 자본 비율 / 리밸런싱
    # ------------------------------------------------------------------
    def _check_rebalance(self, session: Session, positions: List[Position], quotes: Dict[str, Quote],
                         when: str) -> Optional[float]:
        """
        양쪽 자본 비율을 목표와 비교해서, 많이 벗어났으면 옮길 USDT 를 알려준다 (송금은 사용자)

        예) 목표 43.5 : 56.5, 지금 업비트 5,800 / 바이비트 4,200 (58 : 42) → 14.5%p 벗어남
            → "업비트 → 바이비트로 USDT 약 1,450 개 옮기세요"

        Returns:
            옮겨야 할 양 (USDT, + 면 업비트 → 바이비트). 기준 안이면 None
        """
        upbit, bybit = self.broker.capital_split(session, positions, quotes)
        total = upbit + bybit
        if total <= 0:
            return None
        target = self.s.upbit_target_share()
        share = upbit / total
        gap = (share - target) * 100  # %p
        if abs(gap) <= self.s.rebalance_threshold_percent:
            return None
        move = (share - target) * total
        direction = "업비트 → 바이비트" if move > 0 else "바이비트 → 업비트"
        self._log_throttled(
            f"rebalance-{when}", logging.WARNING,
            f"[리밸런싱 필요 · {when}] 업비트 {upbit:,.2f} / 바이비트 {bybit:,.2f} USDT "
            f"({share * 100:.1f} : {(1 - share) * 100:.1f}, 목표 {target * 100:.1f} : {(1 - target) * 100:.1f}) "
            f"→ {direction}로 USDT 약 {abs(move):,.2f} 개 옮기세요",
        )
        return move

    # ------------------------------------------------------------------
    # 새 진입
    # ------------------------------------------------------------------
    def _position_size_usdt(self, session: Session, coin: str, positions: List[Position],
                            quotes: Dict[str, Quote]) -> float:
        """
        이번에 진입할 포지션 크기 (바이비트 숏 금액, USDT)

        - 코인별 / 공통 POSITION_SIZE_USDT 가 있으면 그 값
        - 없으면 자동: 전체 자본 × 업비트 목표 비율 × 사용률 ÷ 최대 포지션 수
          예) 전체 10,000 USDT × 43.5% × 90% ÷ 3 ≈ 1,305 USDT
        """
        fixed = self.s.params_for(coin).position_size_usdt
        if fixed is not None:
            return fixed
        upbit, bybit = self.broker.capital_split(session, positions, quotes)
        return ((upbit + bybit) * self.s.upbit_target_share() * self.s.capital_usage_percent / 100
                / self.s.max_open_positions)

    def _try_entries(self, session: Session, quotes: Dict[str, Quote], open_positions: Dict[str, Position]) -> None:
        """
        진입 조건을 만족하는 코인에 진입한다

        - 이미 포지션이 있는 코인 / 막 청산한 코인(재진입 대기 중)은 건너뜀
        - 전체 포지션 수가 최대치면 진입 안 함
        - 진입 기준은 코인별 설정 (없으면 공통값)
        - 여러 코인이 동시에 조건을 만족하면 '자기 기준보다 더 많이 낮은' 코인부터
        """
        slots = self.s.max_open_positions - len(open_positions)
        if slots <= 0:
            return
        fee_rate = self.s.fees.upbit_trade_fee_rate

        candidates = []
        for coin, quote in quotes.items():
            if coin in open_positions or self._clock() < self._reentry_allowed_at.get(coin, 0.0):
                continue
            threshold = self.s.params_for(coin).entry_premium_threshold_percent
            top = quote.entry_premium_top
            if top is None or top > threshold:
                continue  # 맨 앞 호가로도 조건이 안 되면 더 볼 필요 없음
            size = self._position_size_usdt(session, coin, list(open_positions.values()), quotes)
            bybit_qty = self._plan_bybit_qty(coin, quote, size)
            if bybit_qty is None:
                continue
            premium = quote.entry_premium_for(bybit_qty, fee_rate)
            if premium is None:
                self._log_throttled(f"reject-{coin}", logging.INFO,
                                    f"{coin} 진입 보류: 호가가 모자라서 정한 크기를 다 못 채움")
                continue
            if premium <= threshold:
                candidates.append((premium - threshold, coin, bybit_qty, quote))

        for _margin, coin, bybit_qty, quote in sorted(candidates)[:slots]:
            params = self.s.params_for(coin)
            try:
                pos = self.broker.open_hedge(session, quote, bybit_qty)
            except EntryRejected as exc:
                self._log_throttled(f"reject-{coin}", logging.INFO, f"{coin} 진입 보류: {exc}")
                continue
            # 진입 때 기준을 포지션에 적어둔다 (나중에 설정을 바꿔도 이 포지션은 이 값으로 청산)
            pos.entry_threshold_percent = params.entry_premium_threshold_percent
            pos.target_net_profit_percent = params.target_net_profit_percent
            pos.stop_loss_percent = params.stop_loss_percent
            session.commit()
            notional = pos.bybit_qty * pos.entry_bybit_price_usdt
            target = estimated_exit_premium(pos.entry_premium, params.target_net_profit_percent, notional, 0.0,
                                            self.s.fees, quote.usdt_spread_percent or 0.0)
            logger.info(
                "[진입] #%d %s | 업비트 USDT %.2f개 매도(%.1f원) → %.6g개 매수(%s원) / 바이비트 %g개 숏(%.6f, %.0f배, "
                "강제청산 %.6f) | 진입 김프 %+.3f%% (기준 %+.2f%%) → 목표 %.2f%% 되려면 김프 약 %+.2f%%",
                pos.id, coin, pos.usdt_sold, pos.entry_usdt_price_krw, pos.upbit_qty,
                f"{pos.entry_upbit_price_krw:,.2f}", pos.bybit_qty, pos.entry_bybit_price_usdt, pos.leverage,
                pos.liquidation_price, pos.entry_premium, params.entry_premium_threshold_percent,
                params.target_net_profit_percent, target,
            )
            open_positions[coin] = pos

    def _plan_bybit_qty(self, coin: str, quote: Quote, size_usdt: float) -> Optional[float]:
        """
        바이비트 숏 개수 = 포지션 크기(USDT) ÷ 바이비트 맨 앞 매수 호가 → 주문 단위로 내림
        예) 1,000 USDT ÷ 1.35 = 740.74개 → XRP 는 0.1개 단위라 740.7개

        Returns:
            숏 개수. 최소 주문 개수/금액보다 작으면 None
        """
        best_bid = quote.bybit.best_bid
        if not best_bid or size_usdt <= 0:
            return None
        qty = self.market.round_qty(coin, size_usdt / best_bid)
        min_qty = self.market.min_qty(coin)
        if qty <= 0 or qty < min_qty:
            self._log_throttled(f"reject-{coin}", logging.INFO,
                                f"{coin} 진입 보류: 개수 {qty} 가 바이비트 최소 {min_qty} 보다 작음")
            return None
        if quote.usdt_krw and qty * best_bid * quote.usdt_krw < UPBIT_MIN_ORDER_KRW:
            self._log_throttled(f"reject-{coin}", logging.INFO,
                                f"{coin} 진입 보류: 업비트 주문 금액이 최소 {UPBIT_MIN_ORDER_KRW:,}원보다 작음")
            return None
        return qty

    # ------------------------------------------------------------------
    # 기록 / 로그
    # ------------------------------------------------------------------
    def _log_throttled(self, key: str, level: int, message: str) -> None:
        """같은 종류의 로그는 LOG_COOLDOWN_SEC 에 한 번만 (로그 도배 방지)"""
        now = self._clock()
        if now - self._last_log.get(key, 0.0) >= LOG_COOLDOWN_SEC:
            self._last_log[key] = now
            logger.log(level, message)

    def _save_snapshots(self, session: Session, quotes: Dict[str, Quote]) -> None:
        """
        코인마다 SNAPSHOT_INTERVAL_SEC 에 한 번 김프를 기록한다

        봇 김프(코인 몫), 맨 앞 호가 진입/청산 김프, USDT/KRW 와 호가 간격, 달러 환율,
        테더 프리미엄, 사이트식 김프, 바이비트 펀딩비율 / 다음 정산 시각 / 정산 주기
        """
        now = self._clock()
        added = False
        for coin, q in quotes.items():
            if now - self._last_snapshot.get(coin, 0.0) < self.s.snapshot_interval_sec:
                continue
            mid, entry, exit_ = q.mid_premium, q.entry_premium_top, q.exit_premium_top
            if mid is None or entry is None or exit_ is None:
                continue
            funding = self.market.fetch_funding(coin)
            session.add(PremiumSnapshot(
                coin=coin, mid_premium=mid, entry_premium_top=entry, exit_premium_top=exit_,
                usdt_krw=q.usdt_krw, usdt_spread_percent=q.usdt_spread_percent, fx_krw=q.fx_krw,
                tether_premium=q.tether_premium, site_premium=q.site_premium,
                funding_rate=funding.rate if funding else None,
                next_funding_ms=funding.next_funding_ms if funding else None,
                funding_interval_hours=funding.interval_hours if funding else None,
                upbit_mid_krw=q.upbit.mid, bybit_mid_usdt=q.bybit.mid,
            ))
            self._last_snapshot[coin] = now
            added = True
        if added:
            session.commit()

    def _log_status(self, session: Session, quotes: Dict[str, Quote], open_positions: Dict[str, Position]) -> None:
        """
        SNAPSHOT_INTERVAL_SEC 마다 현황 한 줄

        예) [현황] USDT 1,333원 (달러 1,325, 테더 +0.6%) | XRP -0.05%(사이트 +0.55%)/기준 -0.50% |
            포지션 0/3 | 자본 업비트 4,348 / 바이비트 5,652 USDT (43.5:56.5, 목표 43.5:56.5)
        """
        now = self._clock()
        if now - self._last_status_log < self.s.snapshot_interval_sec:
            return
        self._last_status_log = now

        q0 = next(iter(quotes.values()))
        head = f"USDT {q0.usdt_krw:,.1f}원"
        if q0.fx_krw and q0.tether_premium is not None:
            head += f" (달러 {q0.fx_krw:,.1f}, 테더 {q0.tether_premium:+.2f}%)"

        parts = []
        for coin, q in quotes.items():
            if coin in open_positions:
                p = open_positions[coin]
                target, _stop = self._exit_rules(p)
                text = f"{coin} 보유 {p.last_net_percent or 0:+.2f}%/목표 {target:.2f}%"
                if p.last_liq_distance_percent is not None and p.leverage > 1:
                    text += f" (강제청산까지 {p.last_liq_distance_percent:.0f}%)"
            else:
                entry = q.entry_premium_top
                text = f"{coin} {entry:+.2f}%" if entry is not None else f"{coin} -"
                if q.site_premium is not None:
                    text += f"(사이트 {q.site_premium:+.2f}%)"
                text += f"/기준 {self.s.params_for(coin).entry_premium_threshold_percent:+.2f}%"
            parts.append(text)

        upbit, bybit = self.broker.capital_split(session, list(open_positions.values()), quotes)
        total = upbit + bybit
        target = self.s.upbit_target_share()
        logger.info(
            "[현황] %s | %s | 포지션 %d/%d | 자본 업비트 %s / 바이비트 %s USDT (%.1f:%.1f, 목표 %.1f:%.1f)",
            head, ", ".join(parts), len(open_positions), self.s.max_open_positions,
            f"{upbit:,.2f}", f"{bybit:,.2f}",
            upbit / total * 100 if total else 0, bybit / total * 100 if total else 0,
            target * 100, (1 - target) * 100,
        )

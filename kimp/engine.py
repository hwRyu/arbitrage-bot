"""
전략 엔진: 시세 보기 → 가진 포지션 관리 → 새로 진입

전략 한 줄 요약 (자세한 건 docs/concept.md)
------------------------------------------------------------------
김프(달러 환율 기준, 테더 포함)가 낮을 때
    업비트에서 원화로 코인을 사고 + 바이비트에서 같은 금액만큼 숏을 치고,
김프가 올라서 모든 비용을 빼고도 목표 수익이 나면 양쪽을 같이 정리한다.
코인 가격이 오르든 내리든 상관없다. (양쪽이 서로 상쇄됨)

스캔 한 번에 하는 일 (순서가 중요)
------------------------------------------------------------------
1) 달러 환율 확인
   - 아직 한 번도 못 받았으면 → 이번 스캔은 쉰다
   - 너무 오래된 값이면 → 가진 포지션 관리만 하고 새 진입은 안 한다
2) 모든 코인 시세를 '같은 순간' 으로 가져온다
3) 가진 포지션: 펀딩비 반영 → "지금 청산하면 얼마?" 계산 → 목표/손절이면 청산
   (청산하면 돈이 풀리니까 새 진입보다 먼저 한다)
4) 새 진입: 김프가 기준보다 낮은 코인에 진입
5) 김프 기록 저장 + 현황 로그

old 봇과 다른 점
------------------------------------------------------------------
old 봇은 코인마다 스레드를 따로 돌려서 서로 꼬이는 문제가 있었고,
에러가 나면 포지션을 그대로 버려두는 버그가 있었다.
새 봇은 루프 하나에서 모든 코인을 차례로 처리하고, 열린 포지션은 DB 에 남아
봇을 껐다 켜도 이어서 관리한다.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kimp.config import Settings
from kimp.db import ExitReason, FundingEvent, Position, PremiumSnapshot
from kimp.fx import FxProvider
from kimp.market_data import MarketData
from kimp.paper_broker import KRW, USDT, EntryRejected, PaperBroker
from kimp.pnl import estimated_exit_premium
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
        fx: FxProvider,
        broker: PaperBroker,
        session_factory: sessionmaker[Session],
        coins: List[str],
    ):
        """
        Args:
            settings: 봇 설정
            market: 거래소 시세 조회기
            fx: 달러 환율 공급기
            broker: 페이퍼 트레이딩 체결기
            session_factory: DB 연결 만드는 함수
            coins: 실제로 운용할 코인 (양쪽 거래소에 다 있는 것만)
        """
        self.s = settings
        self.market = market
        self.fx = fx
        self.broker = broker
        self.session_factory = session_factory
        self.coins = coins

        # 아래 시각들은 메모리에만 둔다 (time.monotonic 기준)
        self._last_snapshot: Dict[str, float] = {}  # 코인별 마지막 김프 기록 시각
        self._last_funding_check: Dict[str, float] = {}  # 코인별 마지막 펀딩비 확인 시각
        self._last_log: Dict[str, float] = {}  # 같은 경고를 너무 자주 안 찍으려고
        self._reentry_allowed_at: Dict[str, float] = {}  # 코인별 '다시 진입해도 되는' 시각
        self._last_status_log = 0.0

    # ------------------------------------------------------------------
    # 실행
    # ------------------------------------------------------------------
    def startup(self) -> None:
        """시작할 때: 페이퍼 시작 잔고 넣기 + 이전에 열어둔 포지션 안내"""
        with self.session_factory() as session:
            self.broker.ensure_initial_balances(session, self.s.paper_initial_krw, self.s.paper_initial_usdt)
            logger.info(
                "잔고: 원화 %s원 / USDT %s",
                f"{self.broker.balance(session, KRW):,.0f}", f"{self.broker.balance(session, USDT):,.2f}",
            )
            for pos in self.broker.open_positions(session):
                logger.info(
                    "이전 포지션 이어서 관리: #%d %s (업비트 %.6g개 / 바이비트 %g개) 진입 김프 %+.3f%%",
                    pos.id, pos.coin, pos.upbit_qty, pos.bybit_qty, pos.entry_premium,
                )
                # COINS 에서 뺀 코인이라도 포지션이 남아 있으면 청산할 때까지 관리한다
                if pos.coin not in self.coins:
                    logger.warning("%s 는 COINS 에 없지만 열린 포지션이 있어서 청산할 때까지 관리합니다", pos.coin)
                    self.coins.append(pos.coin)

    def run(self, stop_event: threading.Event) -> None:
        """
        메인 루프. 종료 신호가 올 때까지 SCAN_INTERVAL_SEC 마다 step() 을 반복한다

        한 번의 스캔에서 에러가 나도 멈추지 않고 다음 스캔에서 다시 한다.
        """
        self.startup()
        logger.info("엔진 시작: 코인 %s, %.1f초마다 스캔", ", ".join(self.coins), self.s.scan_interval_sec)
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                self.step()
            except Exception:
                logger.exception("스캔 중 에러 (다음 스캔에서 다시 시도)")
            # 처리에 걸린 시간을 빼고 남은 만큼만 쉰다. 종료 신호가 오면 바로 깬다
            elapsed = time.monotonic() - started
            stop_event.wait(max(0.0, self.s.scan_interval_sec - elapsed))
        logger.info("엔진 종료 (열린 포지션은 DB 에 남아서 다음 실행 때 이어서 관리)")

    def step(self) -> None:
        """스캔 한 번 (순서는 파일 맨 위 설명 참고)"""
        # 1) 달러 환율
        fxq = self.fx.get()
        if fxq is None:
            self._log_throttled("fx-none", logging.ERROR, "달러 환율을 아직 못 받아서 이번 스캔은 건너뜁니다")
            return
        allow_entry = not fxq.is_stale
        if fxq.is_stale:
            self._log_throttled(
                "fx-stale", logging.WARNING,
                f"달러 환율이 {fxq.age_sec / 60:.0f}분 동안 갱신 안 됨 → 새 진입 중지 (보유 포지션 관리는 계속)",
            )

        # 2) 시세
        quotes = self.market.fetch_quotes(self.coins, fxq.rate)
        if not quotes:
            return

        with self.session_factory() as session:
            open_positions = {p.coin: p for p in self.broker.open_positions(session)}

            # 3) 가진 포지션 관리
            for coin, pos in list(open_positions.items()):
                quote = quotes.get(coin)
                if quote is None:
                    continue  # 이번에 시세를 못 받음 → 다음 스캔에서 다시 본다
                self._update_funding(session, pos)
                if self._manage_position(session, pos, quote):
                    del open_positions[coin]  # 청산 완료

            # 4) 새 진입
            if allow_entry:
                self._try_entries(session, quotes, open_positions)

            # 5) 기록
            self._save_snapshots(session, quotes)
            self._log_status(session, quotes, open_positions, fxq.rate)

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
        # 지금 상태를 DB 에 적어둔다 (밖에서 진행 상황을 볼 수 있게)
        pos.last_net_percent = pnl.net_percent
        pos.last_exit_premium = valuation.exit_premium
        session.commit()

        # 목표 / 손절은 '진입할 때 정해서 포지션에 저장한 값' 을 쓴다.
        # 그래서 들고 있는 중에 .env 를 바꿔도 이 포지션은 처음 정한 대로 청산된다. (새 설정은 다음 진입부터)
        target, stop = self._exit_rules(pos)
        reason: Optional[str] = None
        if pnl.net_percent >= target:
            reason = ExitReason.TAKE_PROFIT
        elif stop < 0 and pnl.net_percent <= stop:
            reason = ExitReason.STOP_LOSS

        if reason is None:
            logger.debug("%s 보유 중: 청산 김프 %+.3f%%, 순손익 %+.3f%% (%s원)",
                         pos.coin, valuation.exit_premium, pnl.net_percent, f"{pnl.net_krw:+,.0f}")
            return False

        final = self.broker.close_hedge(session, pos, valuation, reason)
        self._reentry_allowed_at[pos.coin] = time.monotonic() + self.s.reentry_cooldown_sec
        logger.info(
            "[청산 %s] #%d %s | 김프 %+.3f%% → %+.3f%% (%s) | 업비트 %s원 + 바이비트 %s원 (%+.2f USDT) "
            "+ 펀딩 %s원 - 비용 %s원 = 순손익 %s원 (%+.3f%%)",
            reason, pos.id, pos.coin, pos.entry_premium, valuation.exit_premium, self._split_text(pos),
            f"{final.upbit_leg_krw:+,.0f}", f"{final.bybit_leg_krw:+,.0f}", final.bybit_leg_usdt,
            f"{final.funding_krw:+,.0f}", f"{final.total_cost_krw:,.0f}",
            f"{final.net_krw:+,.0f}", final.net_percent,
        )
        return True

    def _exit_rules(self, pos: Position) -> tuple[float, float]:
        """
        이 포지션의 (목표 순이익률, 손절 순손익률)

        진입할 때 저장한 값을 쓴다. 혹시 저장된 값이 없으면(옛날 포지션 등) 지금 설정을 쓴다.
        """
        params = self.s.params_for(pos.coin)
        target = pos.target_net_profit_percent
        stop = pos.stop_loss_percent
        return (
            target if target is not None else params.target_net_profit_percent,
            stop if stop is not None else params.stop_loss_percent,
        )

    @staticmethod
    def _split_text(pos: Position) -> str:
        """
        김프 변화를 테더 몫 / 코인 몫으로 나눠서 글자로 (청산 로그용)

        예) "변화 +2.01%p = 테더 +0.00%p, 코인 +2.00%p"
        업비트 USDT 가격이 없어서 나눌 수 없으면 전체 변화만 보여준다.
        """
        total = (pos.exit_premium or 0) - pos.entry_premium
        if None in (pos.entry_tether_premium, pos.exit_tether_premium, pos.entry_coin_premium, pos.exit_coin_premium):
            return f"변화 {total:+.2f}%p"
        tether = pos.exit_tether_premium - pos.entry_tether_premium
        coin = pos.exit_coin_premium - pos.entry_coin_premium
        return f"변화 {total:+.2f}%p = 테더 {tether:+.2f}%p, 코인 {coin:+.2f}%p"

    def _update_funding(self, session: Session, pos: Position) -> None:
        """
        펀딩비 정산 시각이 지났으면 한 번 반영하고, 다음 정산 정보를 저장해둔다

        흐름:
          - pos.next_funding_ms : 다음 정산 시각, pos.pending_funding_rate : 그때 적용될 비율
          - 지금이 정산 시각을 지났고 아직 반영 안 했으면 → 1번 반영
          - 그다음 거래소에서 새 '다음 정산' 정보를 받아 저장
        같은 정산을 두 번 반영하지 않으려고, 이미 반영한 마지막 정산 시각과 비교한다.
        """
        now = time.monotonic()
        if now - self._last_funding_check.get(pos.coin, 0.0) < self.s.funding_refresh_sec:
            return
        self._last_funding_check[pos.coin] = now

        info = self.market.fetch_funding(pos.coin)
        if info is None:
            return

        now_ms = int(time.time() * 1000)
        last_applied = session.scalar(
            select(func.max(FundingEvent.funding_time_ms)).where(FundingEvent.position_id == pos.id)
        ) or 0

        if pos.next_funding_ms and now_ms >= pos.next_funding_ms and pos.next_funding_ms > last_applied:
            # 정산 전에 받아둔 비율을 쓴다 (정산이 끝난 뒤 거래소 값은 이미 다음 회차 비율일 수 있음)
            rate = pos.pending_funding_rate if pos.pending_funding_rate is not None else info.rate
            amount = self.broker.apply_funding(session, pos, rate, info.mark_price, pos.next_funding_ms)
            logger.info("[펀딩비] #%d %s 비율 %+.4f%% → %+.4f USDT (지금까지 %+.4f)",
                        pos.id, pos.coin, rate * 100, amount, pos.funding_usdt)
            last_applied = pos.next_funding_ms

        # 이미 반영한 정산보다 뒤의 정보일 때만 저장
        if info.next_funding_ms > last_applied:
            pos.next_funding_ms = info.next_funding_ms
            pos.pending_funding_rate = info.rate
            session.commit()

    # ------------------------------------------------------------------
    # 새 진입
    # ------------------------------------------------------------------
    def _try_entries(self, session: Session, quotes: Dict[str, Quote], open_positions: Dict[str, Position]) -> None:
        """
        진입 조건을 만족하는 코인에 진입한다

        - 이미 포지션이 있는 코인은 건너뜀 (코인당 1개)
        - 막 청산한 코인은 재진입 대기 시간 동안 건너뜀
        - 전체 포지션 수가 최대치면 진입 안 함
        - 진입 기준과 포지션 규모는 코인별 설정 (없으면 공통값)
        - 여러 코인이 동시에 조건을 만족하면 '자기 기준보다 더 많이 낮은' 코인부터
          예) XRP 기준 -0.5% 인데 지금 -1.0% (0.5%p 더 낮음), SOL 기준 -1.0% 인데 지금 -1.2% (0.2%p)
              → XRP 먼저
        """
        slots = self.s.max_open_positions - len(open_positions)
        if slots <= 0:
            return

        candidates = []
        for coin, quote in quotes.items():
            if coin in open_positions:
                continue
            if time.monotonic() < self._reentry_allowed_at.get(coin, 0.0):
                continue
            params = self.s.params_for(coin)
            threshold = params.entry_premium_threshold_percent
            bybit_qty = self._plan_bybit_qty(coin, quote, params.position_size_usdt)
            if bybit_qty is None:
                continue
            premium = quote.entry_premium_for(bybit_qty)
            if premium is None:
                # 맨 앞 호가로는 조건이 되는데 개수를 다 못 채우는 경우만 알려준다
                top = quote.entry_premium_top
                if top is not None and top <= threshold:
                    self._log_throttled(f"reject-{coin}", logging.INFO,
                                        f"{coin} 진입 보류: 호가가 모자라서 정한 규모를 다 못 채움")
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
            # 진입할 때의 기준을 포지션에 적어둔다 (나중에 설정을 바꿔도 이 포지션은 이 값으로 청산)
            pos.entry_threshold_percent = params.entry_premium_threshold_percent
            pos.target_net_profit_percent = params.target_net_profit_percent
            pos.stop_loss_percent = params.stop_loss_percent
            session.commit()
            notional_usdt = pos.bybit_qty * pos.entry_bybit_price_usdt
            target = estimated_exit_premium(
                pos.entry_premium, params.target_net_profit_percent, notional_usdt, 0.0, self.s.fees,
            )
            logger.info(
                "[진입] #%d %s | 업비트 %.6g개 × %s원 = %s원 / 바이비트 %g개 × %.6f = %.2f USDT 숏 | "
                "달러 %.2f원 | 진입 김프 %+.3f%% (기준 %+.3f%%) → 목표 %.2f%% 되려면 김프 약 %+.3f%%",
                pos.id, coin,
                pos.upbit_qty, f"{pos.entry_upbit_price_krw:,.2f}", f"{pos.upbit_qty * pos.entry_upbit_price_krw:,.0f}",
                pos.bybit_qty, pos.entry_bybit_price_usdt, notional_usdt,
                pos.entry_fx_krw, pos.entry_premium, params.entry_premium_threshold_percent,
                params.target_net_profit_percent, target,
            )
            open_positions[coin] = pos

    def _plan_bybit_qty(self, coin: str, quote: Quote, position_size_usdt: float) -> Optional[float]:
        """
        바이비트에서 숏 칠 개수 정하기

        개수 = 포지션 규모(USDT) ÷ 바이비트 맨 앞 매수 호가 → 주문 단위로 내림
        예) 1,000 USDT ÷ 1.35 = 740.74개 → XRP 는 0.1개 단위라 740.7개

        업비트 개수는 여기서 정하지 않는다. 진입할 때 바이비트 숏 금액과 같은 금액(달러 환율로 환산)
        만큼 업비트에서 사도록 PaperBroker.open_hedge 가 정한다. (금액 헷징)

        Returns:
            숏 개수. 최소 주문 개수/금액보다 작으면 None
        """
        best_bid = quote.bybit.best_bid
        if not best_bid:
            return None
        qty = self.market.round_qty(coin, position_size_usdt / best_bid)
        min_qty = self.market.min_qty(coin)
        if qty <= 0 or qty < min_qty:
            self._log_throttled(f"reject-{coin}", logging.INFO,
                                f"{coin} 진입 보류: 개수 {qty} 가 바이비트 최소 {min_qty} 보다 작음")
            return None
        # 업비트에서 살 금액 ≈ 숏 금액 × 달러 환율
        if qty * best_bid * quote.fx_krw < UPBIT_MIN_ORDER_KRW:
            self._log_throttled(f"reject-{coin}", logging.INFO,
                                f"{coin} 진입 보류: 업비트 주문 금액이 최소 {UPBIT_MIN_ORDER_KRW:,}원보다 작음")
            return None
        return qty

    # ------------------------------------------------------------------
    # 기록 / 로그
    # ------------------------------------------------------------------
    def _log_throttled(self, key: str, level: int, message: str) -> None:
        """같은 종류의 로그는 LOG_COOLDOWN_SEC 에 한 번만 찍는다 (로그 도배 방지)"""
        now = time.monotonic()
        if now - self._last_log.get(key, 0.0) >= LOG_COOLDOWN_SEC:
            self._last_log[key] = now
            logger.log(level, message)

    def _save_snapshots(self, session: Session, quotes: Dict[str, Quote]) -> None:
        """
        코인마다 SNAPSHOT_INTERVAL_SEC 에 한 번 김프를 기록한다

        남기는 것: 김프(진입/청산/가운데), 테더 몫과 코인 몫, 달러 환율, 업비트 USDT 가격,
                   바이비트 펀딩비율 / 다음 정산 시각 / 정산 주기
        펀딩비율은 이때 코인마다 한 번 더 조회한다. (1분에 코인당 1번 정도라 부담이 작다)
        """
        now = time.monotonic()
        added = False
        for coin, q in quotes.items():
            if now - self._last_snapshot.get(coin, 0.0) < self.s.snapshot_interval_sec:
                continue
            mid, entry, exit_ = q.mid_premium, q.entry_premium_top, q.exit_premium_top
            if mid is None or entry is None or exit_ is None:
                continue
            tether, coin_part = q.split_premium(mid)
            funding = self.market.fetch_funding(coin)
            session.add(PremiumSnapshot(
                coin=coin, mid_premium=mid, entry_premium_top=entry, exit_premium_top=exit_,
                fx_krw=q.fx_krw, usdt_krw=q.usdt_krw, tether_premium=tether, coin_premium=coin_part,
                funding_rate=funding.rate if funding else None,
                next_funding_ms=funding.next_funding_ms if funding else None,
                funding_interval_hours=funding.interval_hours if funding else None,
                upbit_mid_krw=q.upbit.mid, bybit_mid_usdt=q.bybit.mid,
            ))
            self._last_snapshot[coin] = now
            added = True
        if added:
            session.commit()

    def _log_status(self, session: Session, quotes: Dict[str, Quote], open_positions: Dict[str, Position],
                    fx_krw: float) -> None:
        """
        SNAPSHOT_INTERVAL_SEC 마다 현황을 한 줄로 보여준다

        예) [현황] 달러 1325.00원 | 업비트 USDT 1332원 (테더 +0.53%) | XRP +0.50%/기준 -0.50%, SOL 보유 +0.20%/목표 1.00% | ...
        """
        now = time.monotonic()
        if now - self._last_status_log < self.s.snapshot_interval_sec:
            return
        self._last_status_log = now

        any_quote = next(iter(quotes.values()))
        if any_quote.usdt_krw and any_quote.tether_premium is not None:
            tether_text = f"업비트 USDT {any_quote.usdt_krw:,.1f}원 (테더 {any_quote.tether_premium:+.2f}%)"
        else:
            tether_text = "업비트 USDT -"

        parts = []
        for coin, q in quotes.items():
            params = self.s.params_for(coin)
            if coin in open_positions:
                # 보유 중: 지금 순손익률 / 이 포지션의 목표 (진입 때 정한 값)
                p = open_positions[coin]
                target, _stop = self._exit_rules(p)
                parts.append(f"{coin} 보유 {p.last_net_percent or 0:+.2f}%/목표 {target:.2f}%")
            else:
                # 미보유: 지금 진입 김프 / 진입 기준
                entry = q.entry_premium_top
                now_text = f"{entry:+.2f}%" if entry is not None else "-"
                parts.append(f"{coin} {now_text}/기준 {params.entry_premium_threshold_percent:+.2f}%")
        logger.info(
            "[현황] 달러 %.2f원 | %s | %s | 포지션 %d/%d | 원화 %s원 USDT %s",
            fx_krw, tether_text, ", ".join(parts),
            len(open_positions), self.s.max_open_positions,
            f"{self.broker.balance(session, KRW):,.0f}", f"{self.broker.balance(session, USDT):,.2f}",
        )

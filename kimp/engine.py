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
from typing import Callable, Dict, List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kimp.config import Settings
from kimp.broker import EntryRejected, ExitValuation, HedgeBroker, InsufficientBalance
from kimp.db import ExitReason, FundingEvent, Position, PositionStatus, PremiumSnapshot
from kimp.executor import BYBIT_USDT, UPBIT_USDT
from kimp.market_data import MarketData
from kimp.pnl import estimated_exit_premium, margin_to_add_usdt
from kimp.premium import Quote

LF = chr(10)  # 줄바꿈 (편집 사고를 막으려고 상수로)

logger = logging.getLogger(__name__)


# 같은 경고를 너무 자주 찍지 않으려고 두는 간격 (초)
LOG_COOLDOWN_SEC = 60.0


def _as_utc(dt):
    """
    DB 에서 읽은 시각에 UTC 를 붙인다

    SQLite 는 시간대를 저장하지 않아서 읽으면 'naive'(시간대 없음) 로 온다.
    그대로 지금 시각(UTC)과 빼면 TypeError 가 나고, 한국 시간으로 착각하면 9시간이 밀린다.
    """
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Engine:
    """
    김프 헷지 전략 엔진 (페이퍼 · 드라이런 · 실거래 공통)

    엔진은 모드를 모른다. 주문·잔고는 전부 broker(HedgeBroker) 를 통하고, 모드 차이는 broker 의 실행기에만 있다.
    """

    def __init__(
        self,
        settings: Settings,
        market: MarketData,
        fx,
        broker: HedgeBroker,
        session_factory: sessionmaker[Session],
        coins: List[str],
        clock: Callable[[], float] = time.time,
    ):
        """
        Args:
            settings: 봇 설정
            market: 거래소 시세 조회기
            fx: 달러 환율 공급기 (get() 이 환율(원) 또는 None).
                표시용이라 공급기가 None 이든 환율을 못 받든 진입·청산은 그대로 한다
            broker: 체결기 (실행기만 페이퍼/드라이런/실거래로 다름)
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
        self._funding_8h: Dict[str, float] = {}       # 코인별 8시간 기준 펀딩비율 (%) — 진입 판단용
        self._funding_8h_at: Dict[str, float] = {}    # 그 값을 받은 시각 (너무 자주 조회하지 않게)
        self._last_log: Dict[str, float] = {}
        self._reentry_allowed_at: Dict[str, float] = {}
        self._last_status_log = 0.0

        # 사람이 지시한 강제 청산 요청 (텔레그램 /close). 다른 스레드에서 직접 청산하면
        # 엔진과 같은 포지션을 동시에 건드릴 수 있어서, 요청만 적어두고 다음 스캔에서 엔진이 처리한다.
        self._close_requests: set = set()   # 코인 이름들. "*" 이면 전부

        # 한 번만 돌리기(ONE_SHOT): 포지션 하나를 끝까지 보고 종료한다
        self._one_shot_opened = False  # 이번 실행에서 포지션을 잡았나 (또는 이미 있던 걸 이어받았나)
        self._one_shot_done = False    # 그 포지션이 끝났나 → run 루프가 멈춘다

    def request_close(self, coins: Optional[Sequence[str]] = None) -> str:
        """
        강제 청산 요청 (텔레그램 /close). 실제 청산은 다음 스캔에서 엔진이 한다.

        **신규 진입도 함께 막는다** (/stop 과 같은 효과). 사람이 정리하라고 했는데 같은 스캔에서
        다시 들어가면 뜻과 다르기 때문이다. 다시 돌리려면 /resume.

        Args:
            coins: 청산할 코인들. 없으면 전부
        Returns:
            사람에게 보여줄 안내 문장
        """
        self.broker.safety.manual_stop = True   # 정리하는 동안 새로 들어가지 않게
        with self.session_factory() as session:
            holding = self.broker.open_positions(session)
        if not holding:
            return "들고 있는 포지션이 없습니다. (신규 진입은 멈춰 뒀습니다. 재개: /resume)"
        if coins:
            want = {c.strip().upper() for c in coins if c.strip()}
            found = [p for p in holding if p.coin in want]
            missing = want - {p.coin for p in holding}
            if not found:
                return f"그 코인의 포지션이 없습니다 (지금: {', '.join(p.coin for p in holding)})"
            self._close_requests |= {p.coin for p in found}
            note = f" (없는 코인: {', '.join(sorted(missing))})" if missing else ""
            return (f"청산 요청: {', '.join(sorted(p.coin for p in found))}{note}"
                    f"{LF}다음 스캔({self.s.scan_interval_sec:.0f}초 안)에 지금 시세로 정리합니다."
                    f"{LF}신규 진입도 멈췄습니다 (재개: /resume)")
        self._close_requests.add("*")
        return (f"청산 요청: 전체 {len(holding)}개 ({', '.join(p.coin for p in holding)})"
                f"{LF}다음 스캔({self.s.scan_interval_sec:.0f}초 안)에 지금 시세로 정리합니다."
                f"{LF}신규 진입도 멈췄습니다 (재개: /resume)")

    def _handle_close_requests(self, session: Session, quotes: Dict[str, Quote],
                               open_positions: Dict[str, Position]) -> bool:
        """요청된 포지션을 지금 시세로 청산한다 (청산 사유 MANUAL). 하나라도 청산했으면 True"""
        if not self._close_requests:
            return False
        closed = False
        for coin, pos in list(open_positions.items()):
            if "*" not in self._close_requests and coin not in self._close_requests:
                continue
            quote = quotes.get(coin)
            valuation = self.broker.value_exit(pos, quote) if quote else None
            if valuation is None:
                logger.warning("[강제 청산] %s 호가가 모자라서 이번 스캔에는 못 했습니다 (다시 시도합니다)", coin)
                continue
            logger.warning("[강제 청산] #%d %s 사람이 지시 → 지금 시세로 정리 (순손익 %+.3f%%)",
                           pos.id, coin, valuation.pnl.net_percent)
            self._close(session, pos, valuation, ExitReason.MANUAL)
            self._close_requests.discard(coin)
            del open_positions[coin]
            closed = True
        if "*" in self._close_requests and not open_positions:
            self._close_requests.clear()
        return closed

    @property
    def transfers(self) -> list:
        """지금까지 한 송금 (실패 제외, DB 기록)"""
        with self.session_factory() as session:
            return self.broker.transfers(session)

    @property
    def auto_transfer(self) -> bool:
        """송금이 필요할 때 자동으로 보낼지 (브로커가 정해 주면 그 값, 아니면 PAPER_AUTO_REBALANCE)"""
        return self.s.paper_auto_rebalance if self.broker.auto_transfer is None else self.broker.auto_transfer

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
            if self.s.one_shot:
                if positions:
                    self._one_shot_opened = True
                    logger.warning("[한 번만 돌리기] 이미 열린 포지션 %d개를 끝까지 관리하고 종료합니다 "
                                   "(새로 진입하지 않습니다)", len(positions))
                else:
                    logger.warning("[한 번만 돌리기] 포지션 하나를 잡고, 청산되면 종료합니다"
                                   "%s", f" (최대 보유 {self.s.one_shot_max_hours:g}시간)"
                                   if self.s.one_shot_max_hours else " (목표에 닿을 때까지 기다립니다)")
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
            if self._one_shot_done:
                logger.warning("[한 번만 돌리기] 포지션이 끝났습니다 → 봇을 종료합니다")
                stop_event.set()
                break
            stop_event.wait(max(0.0, self.s.scan_interval_sec - (time.monotonic() - started)))
        logger.info("엔진 종료 (열린 포지션은 DB 에 남아서 다음 실행 때 이어서 관리)")

    def step(self) -> None:
        """스캔 한 번 (순서는 파일 맨 위 설명 참고)"""
        # 환율은 테더 프리미엄·사이트식 김프를 보여주는 데만 쓴다. 없으면 그 칸만 빈다
        fx_krw = self.fx.get() if self.fx else None
        quotes = self.market.fetch_quotes(self.coins, fx_krw)
        if not quotes:
            return

        with self.session_factory() as session:
            # 송금 도착 확인, 덜 끝난 진입·청산 이어서, 거래소와 대조 (페이퍼도 같은 흐름)
            self.broker.sync(session, quotes, self.coins)
            open_positions = {p.coin: p for p in self.broker.open_positions(session)}

            closed_any = self._handle_close_requests(session, quotes, open_positions)
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

            allowed, why = self.broker.entries_allowed(session)
            if allowed:
                self._try_entries(session, quotes, open_positions)
            else:
                self._log_throttled("entries-blocked", logging.WARNING, f"신규 진입 중단 중: {why}")
            self._save_snapshots(session, quotes)
            self._log_status(session, quotes, open_positions)
            if self.s.one_shot:
                self._check_one_shot(session, quotes)

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
        elif self.s.max_funding_cost_percent:
            # 펀딩비로 낸 돈이 한도를 넘으면 더 기다리지 않는다.
            # 김프 회복을 기다리는 전략이라, 내는 쪽에 오래 머물면 김프가 그대로여도 손실이 쌓인다.
            cost = self._funding_cost_percent(pos)
            if cost >= self.s.max_funding_cost_percent:
                logger.warning("[펀딩비 한도] #%d %s 펀딩비로 %.3f%% 썼습니다 (한도 %.2f%%) → 정리합니다 "
                               "(지금 순손익 %+.3f%%)", pos.id, pos.coin, cost,
                               self.s.max_funding_cost_percent, pnl.net_percent)
                reason = ExitReason.FUNDING_COST
        if reason is None:
            logger.debug("%s 보유 중: 청산 김프 %+.3f%%, 순손익 %+.3f%% (%+.2f USDT)",
                         pos.coin, valuation.exit_premium, pnl.net_percent, pnl.net_usdt)
            return False

        self._close(session, pos, valuation, reason)
        return True

    def _check_one_shot(self, session: Session, quotes: Dict[str, Quote]) -> None:
        """
        한 번만 돌리기: 포지션 하나가 끝났는지 보고, 끝났으면 종료 신호를 켠다

        끝났다고 보는 때
            정상 청산(CLOSED) / 진입 중 실패해서 되돌림(ABORTED) / 손으로 정리해야 하는 ERROR
        아직 안 끝난 것: OPENING · OPEN · CLOSING (다음 스캔에서 이어서 처리)

        ONE_SHOT_MAX_HOURS 를 넘기면 목표에 못 닿았어도 시장가로 정리한다 (청산 사유 TIMEOUT).
        """
        # 관리 중(OPEN) + 덜 끝난 것(OPENING/CLOSING/ERROR)을 모두 봐야 한다.
        # ERROR 는 봇이 청산하지 못하므로 '끝난 것' 으로 보고 경고와 함께 종료한다.
        open_now = self.broker.open_positions(session)
        unfinished = [p for p in self.broker.unfinished_positions(session)
                      if p.status != PositionStatus.ERROR]
        errors = [p for p in self.broker.unfinished_positions(session) if p.status == PositionStatus.ERROR]
        if open_now or unfinished:
            self._one_shot_opened = True
            if self.s.one_shot_max_hours:
                limit = self.s.one_shot_max_hours * 3600
                for pos in open_now:
                    held = self._now_utc().timestamp() - _as_utc(pos.opened_at).timestamp()
                    if held < limit:
                        continue
                    quote = quotes.get(pos.coin)
                    valuation = self.broker.value_exit(pos, quote) if quote else None
                    if valuation is None:
                        continue
                    logger.warning("[한 번만 돌리기] #%d %s 보유 %.1f시간 > %g시간 → 지금 시세로 정리합니다 "
                                   "(순손익 %+.3f%%)", pos.id, pos.coin, held / 3600,
                                   self.s.one_shot_max_hours, valuation.pnl.net_percent)
                    self._close(session, pos, valuation, ExitReason.TIMEOUT)
            # 방금 정리해서 남은 게 없으면 이번 스캔에서 바로 끝낸다 (한 번 더 기다리지 않게)
            if self.broker.open_positions(session) or [
                    p for p in self.broker.unfinished_positions(session) if p.status != PositionStatus.ERROR]:
                return
        if errors:
            logger.critical("[한 번만 돌리기] 확인이 필요한 포지션(ERROR) %d개가 있어 종료합니다 — "
                            "거래소에서 직접 정리하세요 (docs/live.md 6번)", len(errors))
            self._one_shot_done = True
            return
        if self._one_shot_opened:
            self._one_shot_done = True

    def _now_utc(self):
        """지금 시각 (UTC)"""
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self._clock(), timezone.utc)

    def _close(self, session: Session, pos: Position, valuation: ExitValuation, reason: str,
               liquidated: bool = False) -> None:
        """청산 실행 + 로그 + 재진입 대기 시작"""
        final = self.broker.close_hedge(session, pos, valuation, reason, liquidated=liquidated)
        self._reentry_allowed_at[pos.coin] = self._clock() + self.s.reentry_cooldown_sec
        if final is None:
            return  # 일부 단계 실패 → CLOSING 으로 남음, 다음 스캔에서 이어서 (broker.sync)
        level = logging.CRITICAL if liquidated else logging.INFO
        logger.log(
            level,
            "[청산 %s] #%d %s | 김프 %+.3f%% → %+.3f%% | 업비트 USDT %.2f개 → %.2f개 (%+.2f) | 바이비트 %+.2f "
            "| 펀딩 %+.2f - 비용 %.2f = 순손익 %+.2f USDT (%+.3f%%)",
            reason, pos.id, pos.coin, pos.entry_premium, pos.exit_premium,
            pos.usdt_sold, pos.usdt_bought, pos.usdt_bought - pos.usdt_sold,
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
                f"{pos.liquidation_price:.6f}) → 바이비트 포지션에 증거금 약 {add:,.2f} USDT 추가하세요 "
                f"(격리 마진이라 계좌 잔고가 자동으로 들어가지 않습니다 — 직접 넣어야 합니다)",
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
    def _funding_8h_percent(self, coin: str) -> Optional[float]:
        """
        코인의 펀딩비율을 8시간 기준 %로 (진입 판단용, FUNDING_REFRESH_SEC 마다 갱신)

        코인마다 정산 주기가 1·4·8시간으로 달라서 그대로 비교하면 안 된다.
          예) 1시간마다 0.0025% → 8시간 기준 0.02%
        우리는 숏이라 + 면 받고 − 면 낸다. 못 받으면 None (데이터가 없다고 기회를 버리지는 않는다)
        """
        now = self._clock()
        if now - self._funding_8h_at.get(coin, 0.0) < self.s.funding_refresh_sec:
            return self._funding_8h.get(coin)
        info = self.market.fetch_funding(coin)
        self._funding_8h_at[coin] = now
        if info is None:
            self._funding_8h.pop(coin, None)
            return None
        hours = info.interval_hours or 8.0
        value = info.rate * 100 * (8.0 / hours if hours else 1.0)
        self._funding_8h[coin] = value
        return value

    def _funding_cost_percent(self, pos: Position) -> float:
        """이 포지션에서 펀딩비로 **낸** 금액이 포지션 금액의 몇 % 인지 (받았으면 0)"""
        notional = pos.bybit_qty * pos.entry_bybit_price_usdt
        if notional <= 0 or pos.funding_usdt >= 0:
            return 0.0
        return -pos.funding_usdt / notional * 100

    def _check_rebalance(self, session: Session, positions: List[Position], quotes: Dict[str, Quote],
                         when: str, need: Optional[InsufficientBalance] = None) -> Optional[float]:
        """
        양쪽 자본 비율을 목표와 비교해서, 많이 벗어났으면 옮길 USDT 를 알려준다

        송금은 '필요할 때만' 한다 (거래마다 하는 게 아님):
          - 청산 후 비율이 목표에서 10%p 넘게 벗어났을 때
          - 진입하려는데 한쪽이 모자랄 때 (need: 벗어난 정도와 상관없이 맞춘다)
            단, 목표 비율로 맞춰도 여전히 모자라면 보내지 않는다 (수수료만 버림)
            예) 250 USDT, 업비트 몫 108, 포지션 50 × 3개 → 3번째는 맞춰도 못 들어감 → 송금 안 함
        실거래: 지금은 알림만 하고 송금은 사용자가 한다 (실주문 단계에서 자동 송금 예정, concept.md 11번).
        페이퍼/시뮬레이션은 PAPER_AUTO_REBALANCE 이면 사용자가 보냈다고 치고 옮긴다 (수수료 1번).

        예) 목표 43.5 : 56.5, 지금 업비트 5,800 / 바이비트 4,200 (58 : 42) → 14.5%p 벗어남
            → "업비트 → 바이비트로 USDT 약 1,450 개 옮기세요"

        Returns:
            옮겨야 할 양 (USDT, + 면 업비트 → 바이비트). 맞출 필요 없으면 None
        """
        upbit, bybit = self.broker.capital_split(session, positions, quotes)
        total = upbit + bybit
        if total <= 0:
            return None
        target = self.s.upbit_target_share()
        share = upbit / total
        gap = (share - target) * 100  # %p
        if need is None and abs(gap) <= self.s.rebalance_threshold_percent:
            return None
        move = (share - target) * total
        direction = "업비트 → 바이비트" if move > 0 else "바이비트 → 업비트"
        if need is not None:
            # 모자란 쪽으로 보내는 방향이고, 보내고 나면 진입할 만큼 되는지 확인
            to_short_side = (move > 0) == (need.side == "bybit")
            have = self.broker.balance(session, BYBIT_USDT if need.side == "bybit" else UPBIT_USDT)
            if not to_short_side or have + abs(move) - self.s.transfer_fee_usdt(move > 0) < need.need_usdt:
                self._log_throttled(
                    f"rebalance-useless", logging.INFO,
                    "진입 보류: 양쪽 비율을 맞춰도 자리가 없음 (포지션 크기 × 최대 개수가 자본에 비해 큼) → 송금 안 함",
                )
                return None
        if self.auto_transfer:
            if self.broker.pending_transfers(session):
                return None  # 송금 중 → 도착할 때까지 기다림
            fee = self.s.transfer_fee_usdt(move > 0)  # 보내는 거래소가 떼므로 방향별로 다르다
            sent = self.broker.transfer(session, move, fee, reason=when)
            if sent:
                logger.warning(
                    "[송금 · %s] %s로 USDT %s 개 (수수료 %s) | 전: 업비트 %s / 바이비트 %s (%.1f : %.1f, 목표 %.1f : %.1f)",
                    when, direction, f"{abs(sent):,.2f}", f"{fee:g}", f"{upbit:,.2f}", f"{bybit:,.2f}",
                    share * 100, (1 - share) * 100, target * 100, (1 - target) * 100,
                )
            return sent or None
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
        if fixed is None:
            upbit, bybit = self.broker.capital_split(session, positions, quotes)
            fixed = ((upbit + bybit) * self.s.upbit_target_share() * self.s.capital_usage_percent / 100
                     / self.s.max_open_positions)
        cap = self.s.max_position_usdt
        return min(fixed, cap) if cap else fixed

    def _try_entries(self, session: Session, quotes: Dict[str, Quote], open_positions: Dict[str, Position]) -> None:
        """
        진입 조건을 만족하는 코인에 진입한다

        - 이미 포지션이 있는 코인 / 막 청산한 코인(재진입 대기 중)은 건너뜀
        - 전체 포지션 수가 최대치면 진입 안 함
        - 진입 기준은 코인별 설정 (없으면 공통값)
        - 여러 코인이 동시에 조건을 만족하면 '자기 기준보다 더 많이 낮은' 코인부터
        """
        if self.s.one_shot and self._one_shot_opened:
            return  # 한 번만 돌리기: 이미 하나 잡았으면 더 잡지 않는다
        busy = {p.coin for p in self.broker.unfinished_positions(session)}  # 진입 중·청산 중·확인 필요
        # 남은 자리 = 최대치 − 보유 중 − (덜 끝난 것 중 보유로 안 세어진 것).
        # 덜 끝난 포지션도 자리를 차지한 것으로 봐야 한다 — 청산이 끝나지 않았는데 새로 잡으면
        # 자본이 모자라 양쪽 중 한쪽만 들어가는 사고가 난다.
        slots = self.s.max_open_positions - len(open_positions) - len(busy - set(open_positions))
        if slots <= 0:
            return
        fee_rate = self.s.fees.upbit_trade_fee_rate

        candidates = []
        for coin, quote in quotes.items():
            if coin in open_positions or coin in busy or self._clock() < self._reentry_allowed_at.get(coin, 0.0):
                continue
            threshold = self.s.params_for(coin).entry_premium_threshold_percent
            # 1차 걸러내기: 맨 앞 호가 한 칸만 보고 판단한다 (수량만큼 소진하는 계산은 비싸다).
            # 맨 앞 호가 김프는 실제 체결 김프보다 **항상 유리하므로**, 여기서 떨어지면
            # 수량을 넣고 계산해도 절대 통과할 수 없다 → 안전하게 건너뛸 수 있다.
            top = quote.entry_premium_top
            if top is None or top > threshold:
                continue  # 맨 앞 호가로도 조건이 안 되면 더 볼 필요 없음
            # 펀딩비 안전장치: 숏이 돈을 내는 쪽이 심하면 들어가지 않는다 (김프 조건을 통과한 코인만 확인)
            min_funding = self.s.entry_min_funding_8h_percent
            if min_funding is not None:
                funding = self._funding_8h_percent(coin)
                if funding is None:
                    self._log_throttled(f"funding-unknown-{coin}", logging.WARNING,
                                        f"{coin} 펀딩비율을 못 받아서 펀딩비 확인 없이 진입을 판단합니다")
                elif funding < min_funding:
                    self._log_throttled(
                        f"funding-block-{coin}", logging.INFO,
                        f"{coin} 진입 보류: 펀딩비 {funding:+.4f}%/8h < 기준 {min_funding:+.4f}%/8h "
                        f"(숏이 내는 쪽이라 오래 들고 있으면 김프가 올라도 손해)")
                    continue
            size = self._position_size_usdt(session, coin, list(open_positions.values()), quotes)
            bybit_qty = self._plan_bybit_qty(coin, quote, size)
            if bybit_qty is None:
                continue
            # 2차: 실제 주문 수량만큼 호가를 소진한 평균 체결가로 다시 계산한다.
            # 수량이 크면 호가가 밀려서 1차는 통과했어도 여기서 떨어질 수 있다 — 그게 맞는 동작이다.
            premium = quote.entry_premium_for(bybit_qty, fee_rate)
            if premium is None:
                self._log_throttled(f"reject-{coin}", logging.INFO,
                                    f"{coin} 진입 보류: 호가가 모자라서 정한 크기를 다 못 채움")
                continue
            if premium <= threshold:
                # 정렬 기준으로 '기준보다 얼마나 더 낮은가'(음수, 작을수록 좋음)를 맨 앞에 둔다.
                # 김프 절댓값으로 줄 세우면 기준이 느슨한 코인이 늘 이긴다 — 코인마다 기준이
                # 다르므로 '자기 기준 대비 얼마나 유리한가' 로 비교해야 공평하다.
                candidates.append((premium - threshold, coin, bybit_qty, quote))

        for _margin, coin, bybit_qty, quote in sorted(candidates)[:slots]:
            params = self.s.params_for(coin)
            try:
                try:
                    pos = self.broker.open_hedge(session, quote, bybit_qty)
                except InsufficientBalance as exc:
                    # 한쪽이 모자람 → 리밸런싱 (페이퍼는 자동 송금 후 한 번 더 시도, 실거래는 알림만)
                    self._log_throttled(f"reject-{coin}", logging.INFO, f"{coin} 진입 보류: {exc}")
                    if not self._check_rebalance(session, list(open_positions.values()), quotes,
                                                 when="진입 막힘", need=exc) or not self.auto_transfer:
                        continue
                    if self.broker.pending_transfers(session):
                        continue  # 실거래: 송금이 도착하면 다음 스캔에 다시 시도
                    pos = self.broker.open_hedge(session, quote, bybit_qty)
            except EntryRejected as exc:
                self._log_throttled(f"reject-{coin}", logging.INFO, f"{coin} 진입 보류: {exc}")
                continue
            # 진입 때 기준을 포지션에 적어둔다 (나중에 설정을 바꿔도 이 포지션은 이 값으로 청산)
            # 이게 없으면 들고 있는 중에 .env 목표를 올리면 이미 목표에 닿은 포지션이 안 팔린다.
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
            if self.s.one_shot:
                self._one_shot_opened = True
                logger.warning("[한 번만 돌리기] 포지션을 잡았습니다 (#%d %s) → 더 진입하지 않고 청산까지 봅니다",
                               pos.id, coin)
                return  # 여러 코인이 동시에 조건을 만족해도 하나만 잡는다

    def _plan_bybit_qty(self, coin: str, quote: Quote, size_usdt: float) -> Optional[float]:
        """
        바이비트 숏 개수 = 포지션 크기(USDT) ÷ 바이비트 맨 앞 매수 호가 → 주문 단위로 내림
        예) 1,000 USDT ÷ 1.35 = 740.74개 → XRP 는 0.1개 단위라 740.7개

        최소 주문 확인 (모든 모드 같음, 값만 거래소에서):
          바이비트 최소 개수·최소 금액 (마켓 정보, 예: XRP 0.1개 / 5 USDT),
          업비트 최소 주문 금액 (실거래는 거래소 조회, 아니면 UPBIT_MIN_ORDER_KRW 기본 5,000원)
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
        min_usdt = getattr(self.market, "min_notional_usdt", lambda _c: 0.0)(coin)
        if qty * best_bid < min_usdt:
            self._log_throttled(f"reject-{coin}", logging.INFO,
                                f"{coin} 진입 보류: 바이비트 주문 금액 {qty * best_bid:.2f} USDT 가 최소 {min_usdt:g} USDT 보다 작음")
            return None
        min_krw = self.broker.ex.min_order_krw(coin)
        if quote.usdt_krw and qty * best_bid * quote.usdt_krw < min_krw:
            self._log_throttled(f"reject-{coin}", logging.INFO,
                                f"{coin} 진입 보류: 업비트 주문 금액이 최소 {min_krw:,.0f}원보다 작음")
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

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
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kimp.config import Settings
from kimp.broker import EntryMissed, EntryRejected, ExitDeferred, ExitValuation, HedgeBroker, InsufficientBalance
from kimp.db import ExitReason, FundingEvent, Position, PositionStatus, PremiumSnapshot
from kimp.executor import BYBIT_USDT, UPBIT_USDT
from kimp.fok_sizing import EntryFit, fit_entry
from kimp.fok_trace import classify_miss
from kimp.market_data import MarketData
from kimp.pnl import estimated_exit_premium
from kimp.premium import Quote, near_tick_boundary
from kimp import telemetry

LF = chr(10)  # 줄바꿈 (편집 사고를 막으려고 상수로)

logger = logging.getLogger(__name__)


# 같은 경고를 너무 자주 찍지 않으려고 두는 간격 (초)
LOG_COOLDOWN_SEC = 60.0

# 소켓 시세일 때 연속 판단 사이 최소 간격 (초). 호가는 초당 10회 넘게 바뀌는데 판단마다 평가·기록이 따라오므로
# 이 간격으로 묶는다. 계좌 조회(REST)는 이와 무관하게 SCAN_INTERVAL_SEC 마다
EVENT_STEP_MIN_GAP_SEC = 0.25
# 포지션 평가값(순손익·청산 김프·여유)을 DB 에 적는 최소 간격 (초). 최고·최저가 바뀌면 간격과 무관하게 바로 적는다
POSITION_WRITE_MIN_GAP_SEC = 1.0
# 지정가 FOK 가 취소(놓침)된 뒤 같은 코인·포지션을 다시 시도하기까지 (초). 소켓 시세는 0.25초마다 판단하므로
# 이게 없으면 호가가 비껴 있는 동안 초당 4번 주문·취소가 나가 업비트 요청 제한에 걸린다
FOK_RETRY_SEC = 2.0

# 강제청산 가드가 한 번에 넣는 증거금의 최소 (USDT). 이보다 작으면 넣어도 의미가 없다 → 정리로
MIN_TOPUP_USDT = 0.5

# 청산 사유 → 알림에 쓰는 한글 (DB 에는 영문 코드가 그대로 남는다)
EXIT_LABEL = {
    ExitReason.TAKE_PROFIT: "익절", ExitReason.STOP_LOSS: "손절", ExitReason.LIQUIDATION: "강제청산",
    ExitReason.TIMEOUT: "시간 초과", ExitReason.MANUAL: "수동", ExitReason.FUNDING_COST: "펀딩비 한도",
    ExitReason.LIQ_GUARD: "강제청산 회피",
}


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
        self._tp_retry_at: Dict[int, float] = {}   # 익절 지정가가 놓친 포지션 → 이 시각까지 익절 판단 쉼
        self._last_status_log = 0.0

        # 사람이 지시한 강제 청산 요청 (텔레그램 /close). 다른 스레드에서 직접 청산하면
        # 엔진과 같은 포지션을 동시에 건드릴 수 있어서, 요청만 적어두고 다음 스캔에서 엔진이 처리한다.
        self._close_requests: set = set()   # 코인 이름들. "*" 이면 전부

        # 한 번만 돌리기(ONE_SHOT): 포지션 하나를 끝까지 보고 종료한다
        self._one_shot_opened = False  # 이번 실행에서 포지션을 잡았나 (또는 이미 있던 걸 이어받았나)
        self._one_shot_done = False    # 그 포지션이 끝났나 → run 루프가 멈춘다

        # 분석용 기록 (kimp/telemetry.py). 매매 판단에는 안 쓴다
        self.run_id: Optional[int] = None            # main 이 넣어 준다 (runs.id). 새 포지션에 적어 둔다
        self._last_pos_snapshot: Dict[int, float] = {}  # 포지션별 마지막 평가 기록 시각
        self._last_pos_write: Dict[int, float] = {}     # 포지션별 마지막 평가값 커밋 시각
        telemetry.reset_repeat_cooldown()               # 새 엔진 = 새 기록 흐름 (반복 단계 억제 초기화)
        self._last_account_refresh = 0.0                # 마지막으로 잔고·포지션을 거래소에서 새로 받은 시각
        self._account_dirty = True                      # 주문·송금·증거금을 낸 뒤 → 다음 스캔에 계좌를 꼭 새로 받는다
        self._step_started = 0.0                     # 이번 스캔 시작 (monotonic)
        self._quote_ms = 0.0                         # 이번 스캔의 시세 조회 소요 (ms)
        self._steps_since_status = 0                 # 마지막 [현황] 뒤 판단 횟수 (소켓이면 분당 수십~수백 회)

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
                self._log_throttled(f"manual-thin-{coin}", logging.WARNING,
                                    f"[강제 청산] {coin} 호가가 모자라서 이번 스캔에는 못 했습니다 (다시 시도합니다)")
                continue
            logger.warning("[수동 청산] #%d %s | 순손익 지금 %+.2f%% | 텔레그램 지시로 지금 시세에 정리합니다",
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
        """
        메인 루프. 종료 신호가 올 때까지 step() 을 반복한다. 에러가 나도 다음 스캔에서 다시

        언제 다음 step 을 도나:
            REST 시세  → SCAN_INTERVAL_SEC 마다 (예전 그대로)
            소켓 시세  → 호가가 바뀌는 즉시 (직전 step 에서 EVENT_STEP_MIN_GAP_SEC 뒤), 안 바뀌어도 SCAN_INTERVAL_SEC 마다
        """
        self.startup()
        feed = getattr(self.market, "feed", None)
        logger.info("엔진 시작: 코인 %s, %s", ", ".join(self.coins),
                    f"호가가 바뀌면 즉시 판단 (최소 {EVENT_STEP_MIN_GAP_SEC:g}초 간격), 계좌 조회 {self.s.scan_interval_sec:g}초마다"
                    if feed is not None else f"{self.s.scan_interval_sec:.1f}초마다 스캔")
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
            self._wait_next(stop_event, feed, started)
        logger.info("엔진 종료 (열린 포지션은 DB 에 남아서 다음 실행 때 이어서 관리)")

    def _wait_next(self, stop_event: threading.Event, feed, started: float) -> None:
        """다음 step 까지 대기 (run 의 설명 참고). 종료 신호가 오면 바로 돌아온다"""
        remain = self.s.scan_interval_sec - (time.monotonic() - started)
        if feed is None:
            stop_event.wait(max(0.0, remain))
            return
        gap = EVENT_STEP_MIN_GAP_SEC - (time.monotonic() - started)
        if gap > 0 and stop_event.wait(gap):
            return
        feed.wait_change(max(0.0, self.s.scan_interval_sec - (time.monotonic() - started)))

    def step(self) -> None:
        """스캔 한 번 (순서는 파일 맨 위 설명 참고)"""
        self._step_started = time.monotonic()
        self._steps_since_status += 1
        # 환율은 테더 프리미엄·사이트식 김프를 보여주는 데만 쓴다. 없으면 그 칸만 빈다
        fx_krw = self.fx.get() if self.fx else None
        t_quote = time.monotonic()
        quotes = self.market.fetch_quotes(self.coins, fx_krw)
        self._quote_ms = (time.monotonic() - t_quote) * 1000   # 시세 조회만 (환율 조회는 빼고)
        if not quotes:
            return

        with self.session_factory() as session:
            # 송금 도착 확인, 덜 끝난 진입·청산 이어서, 거래소와 대조 (페이퍼도 같은 흐름)
            # 잔고·포지션 REST 재조회는 SCAN_INTERVAL_SEC 마다, 그리고 주문·송금·증거금을 낸 다음 스캔엔 반드시.
            # 소켓 시세로 0.25초마다 판단할 때 이걸 매번 하면 거래소 요청 제한에 걸린다 (이 조회가 요청의 본체)
            now_mono = time.monotonic()
            refresh = self._account_dirty or now_mono - self._last_account_refresh >= self.s.scan_interval_sec
            self.broker.sync(session, quotes, self.coins, refresh_account=refresh)
            if refresh:
                self._last_account_refresh, self._account_dirty = now_mono, False
            self._close_vanished_shorts(session, quotes)
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
                # 사람이 멈춘 상태는 몇 시간씩 가므로 1분마다 알리면 텔레그램이 도배된다 → 30분에 한 번
                self._log_throttled("entries-blocked", logging.WARNING, f"[신규 진입 중단] {why}", cooldown_sec=1800.0)
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

        # 목표 / 손절은 진입할 때 포지션에 저장한 값 (들고 있는 중에 .env 를 바꿔도 처음 값으로)
        target, stop = self._exit_rules(pos)
        # 익절 기준은 목표 그대로다. 업비트 코인 매도가 판단가 지정가 FOK 라 판단보다 나쁘게 팔리지 않으므로
        # 밀림 여유를 목표에 얹지 않는다 (2026-10-29 유동 여유 제거, docs/issues.md #3)
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
                logger.warning("[펀딩비 한도] #%d %s | 펀딩비 지출 %.3f%% (한도 %.2f%%) | 순손익 지금 %+.2f%% | 정리합니다",
                               pos.id, pos.coin, cost, self.s.max_funding_cost_percent, pnl.net_percent)
                reason = ExitReason.FUNDING_COST
        if reason == ExitReason.TAKE_PROFIT and self._clock() < self._tp_retry_at.get(pos.id, 0.0):
            reason = None   # 방금 익절 지정가가 놓침 → FOK_RETRY_SEC 지나고 다시
        if reason == ExitReason.TAKE_PROFIT:
            # 익절만 확인 시세로 한 번 더 본다. 손절·펀딩비 한도는 늦출수록 위험하니 그대로 간다.
            fresh = self._fresh_quote(pos.coin)
            confirmed = self.broker.value_exit(pos, fresh) if fresh else None
            if confirmed is None or confirmed.pnl.net_percent < target:
                again = confirmed.pnl.net_percent if confirmed else None
                self._log_throttled(
                    f"tp-confirm-{pos.coin}", logging.INFO,
                    f"[익절 보류] #{pos.id} {pos.coin} | 스캔 시세 {pnl.net_percent:+.3f}% → 확인 시세 "
                    f"{'없음' if again is None else f'{again:+.3f}%'} | 목표 {target:.2f}% | 한순간 값, 다음 스캔에 다시")
                telemetry.decision(session, pos.coin, "tp_confirm_fail", pnl.net_percent, target,
                                   premium_depth=again, position_id=pos.id, now=self._now_utc())
                reason = None
                if confirmed is not None:
                    valuation, pnl = confirmed, confirmed.pnl   # 한순간 튄 값이 아니라 확인된 값으로 기록한다
            else:
                valuation, pnl = confirmed, confirmed.pnl       # 더 새 시세로 주문한다
                # 안전 확인 (docs/issues.md #3 A): 익절 지정가는 우리 수량이 닿는 가장 싼 매수 호가 줄이다.
                # 앞쪽 비싼 줄이 먼저 팔려 나가면 전부 그 가격에 팔릴 수 있으니, 그때도 목표를 넘을 때만 판다
                worst = self._worst_exit_percent(pos, confirmed)
                if worst is not None and worst < target:
                    self._log_throttled(
                        f"tp-worst-{pos.coin}", logging.INFO,
                        f"[익절 보류] #{pos.id} {pos.coin} | 평균 순손익 {pnl.net_percent:+.3f}% ≥ 목표 {target:.2f}% 이지만 "
                        f"전부 마지막 매수 호가 줄({confirmed.plan.upbit.worst_price:g})에 팔면 {worst:+.3f}% | 다음 스캔에 다시")
                    telemetry.decision(session, pos.coin, "tp_worst_fail", pnl.net_percent, target,
                                       premium_depth=worst, position_id=pos.id, now=self._now_utc())
                    reason = None
                else:
                    worst_text = "?" if worst is None else f"{worst:+.3f}%"
                    logger.info("[익절 판단] #%d %s | 순손익 %+.3f%% ≥ 목표 %.2f%% (전부 마지막 줄 가격이면 %s)",
                                pos.id, pos.coin, pnl.net_percent, target, worst_text)
                    telemetry.decision(session, pos.coin, "tp_judge", pnl.net_percent, target,
                                       premium_depth=worst, position_id=pos.id, now=self._now_utc())

        # 기록은 확인 시세 판정이 끝난 값으로 (가짜 꼭대기가 최고점·평가 기록에 남지 않게)
        pos.last_net_percent = pnl.net_percent
        pos.last_exit_premium = valuation.exit_premium
        now = self._clock()
        changed = telemetry.note_extremes(pos, pnl.net_percent, self._now_utc())
        snapshot = now - self._last_pos_snapshot.get(pos.id, 0.0) >= self.s.snapshot_interval_sec
        if snapshot:
            self._last_pos_snapshot[pos.id] = now
            telemetry.position_snapshot(session, pos, valuation, quote, self._now_utc())

        if reason is None:
            # 소켓 시세면 0.25초마다 오는데 매번 커밋할 필요는 없다 — 최고·최저가 바뀌었거나, 스냅샷을 적었거나,
            # 1초 지났을 때만. 안 적은 평가값은 다음 판단에서 다시 계산된다 (세션이 닫히며 버려진다)
            if changed or snapshot or now - self._last_pos_write.get(pos.id, 0.0) >= POSITION_WRITE_MIN_GAP_SEC:
                self._last_pos_write[pos.id] = now
                session.commit()
            logger.debug("%s 보유 중: 청산 김프 %+.3f%%, 순손익 %+.3f%% (%+.2f USDT)",
                         pos.coin, valuation.exit_premium, pnl.net_percent, pnl.net_usdt)
            return False

        # 청산: 위 평가값은 close_hedge 의 CLOSING 커밋에 같이 실린다 (주문 앞 커밋을 하나 줄인다)
        self.broker.mark_decided()
        self._account_dirty = True
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
                    logger.warning("[시간 초과 청산] #%d %s | 보유 %.1f시간 > %g시간 | 순손익 지금 %+.2f%% | 지금 시세에 정리합니다",
                                   pos.id, pos.coin, held / 3600, self.s.one_shot_max_hours, valuation.pnl.net_percent)
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

    def _fresh_quote(self, coin: str) -> Optional[Quote]:
        """
        주문 직전에 그 코인 시세를 한 번 더 받는다 (확인 시세)

        스캔 시작 때 받은 시세는 업비트와 바이비트를 따로 받은 것이라 0.3~1초 어긋나 있고, 그 사이
        가격이 움직이면 김프가 잠깐 튀어 보인다 (IRYS #3: 판단 +0.26% → 1초 뒤 체결 −0.82%).
        한 번 더 받아서 여전히 조건을 만족할 때만 주문한다. 못 받으면 None → 이번 스캔은 건너뛴다.
        """
        try:
            fx_krw = self.fx.get() if self.fx else None
            return self.market.fetch_quotes([coin], fx_krw).get(coin)
        except Exception as exc:
            logger.warning("%s 확인 시세 조회 실패 (이번 스캔은 주문 안 함): %s", coin, exc)
            return None

    def _worst_exit_percent(self, pos: Position, valuation: ExitValuation) -> Optional[float]:
        """
        코인을 전부 마지막으로 닿는 매수 호가 줄 가격에 팔았을 때의 순손익 (%) — 익절 안전 확인용

        그 줄 하나로 호가를 바꾼 시세로 손익을 다시 계산한다 (바이비트·USDT 는 그대로). 계산이 안 되면 None
        """
        worst_price = valuation.plan.upbit.worst_price
        quote = getattr(valuation, "quote", None)
        if not worst_price or quote is None:
            return None
        flat = self.broker.value_exit(pos, quote.with_flat_upbit("bids", worst_price))
        return flat.pnl.net_percent if flat else None

    def _miss_reason(self, coin: str, judged) -> Tuple[str, str]:
        """
        지정가 FOK 를 놓친 이유 (kimp/fok_trace.py) — 주문 때 본 호가와 지금 호가를 비교한다

        지금 호가는 확인 시세를 한 번 더 받아서 본다 (웹소켓이면 바로, 아니면 REST 한 번). 놓친 뒤에만 부르니
        주문 속도에는 영향이 없다. 매수를 놓쳤으면 매도호가, 매도를 놓쳤으면 매수호가를 본다.
        """
        if judged is None:
            return classify_miss(None, None)
        fresh = self._fresh_quote(coin)
        now_levels = None
        if fresh is not None:
            now_levels = fresh.upbit.asks if judged.side == "buy" else fresh.upbit.bids
        return classify_miss(judged, now_levels)

    def _now_utc(self):
        """지금 시각 (UTC)"""
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self._clock(), timezone.utc)

    def _close(self, session: Session, pos: Position, valuation: ExitValuation, reason: str,
               liquidated: bool = False) -> None:
        """청산 실행 + 로그 + 재진입 대기 시작. 익절 지정가가 놓치면(ExitDeferred) 포지션은 그대로, 잠시 뒤 다시"""
        try:
            final = self.broker.close_hedge(session, pos, valuation, reason, liquidated=liquidated)
        except ExitDeferred as exc:
            self._tp_retry_at[pos.id] = self._clock() + FOK_RETRY_SEC
            why, detail = self._miss_reason(pos.coin, exc.fok)
            logger.info("[익절 보류] #%d %s | %s | 이유: %s — %s | %g초 뒤 다시 판단",
                        pos.id, pos.coin, exc, why, detail, FOK_RETRY_SEC)
            telemetry.decision(session, pos.coin, "tp_miss", valuation.pnl.net_percent, 0.0, position_id=pos.id,
                               note=f"{why} | {exc} | {detail}", now=self._now_utc())
            return
        self._reentry_allowed_at[pos.coin] = self._clock() + self.s.reentry_cooldown_sec
        if final is None:
            return  # 일부 단계 실패 → CLOSING 으로 남음, 다음 스캔에서 이어서 (broker.sync)
        level = logging.CRITICAL if liquidated else logging.INFO
        held_h = (self._now_utc() - _as_utc(pos.opened_at)).total_seconds() / 3600
        logger.log(
            level,
            "[청산 · %s] #%d %s | 순손익 %+.2f USDT (%+.2f%%) | 김프 진입 %+.3f%% → 청산 %+.3f%% | "
            "업비트 USDT %.2f → %.2f (%+.2f) | 바이비트 %+.2f USDT | 펀딩 %+.2f · 비용 %.2f | 보유 %.1f시간",
            EXIT_LABEL.get(reason, reason), pos.id, pos.coin, final.net_usdt, final.net_percent,
            pos.entry_premium, pos.exit_premium if pos.exit_premium is not None else float("nan"),
            pos.usdt_sold, pos.usdt_bought, pos.usdt_bought - pos.usdt_sold,
            final.bybit_leg_usdt, final.funding_usdt, final.total_cost_usdt, held_h,
        )

    def _close_vanished_shorts(self, session: Session, quotes: Dict[str, Quote]) -> None:
        """
        거래소에서 숏이 사라진(강제청산된) OPEN 포지션을 그 스캔에 바로 정리한다

        봇 계산 강제청산가보다 거래소가 먼저 자르는 경우가 있다 (IRYS #4: 봇 0.0572, 거래소 0.0559).
        _close 를 거치므로 재진입 대기·"[청산 LIQUIDATION]" 요약 로그·기록이 정상 청산과 똑같이 남는다.
        """
        for pos in self.broker.vanished_shorts(session):
            quote = quotes.get(pos.coin)
            valuation = self.broker.value_liquidation(pos, quote) if quote else None
            if valuation is None:
                self._log_throttled(f"vanished-{pos.coin}", logging.CRITICAL,
                                    f"[숏 사라짐] #{pos.id} {pos.coin} | 바이비트에 숏이 없는데 시세·호가가 없어 코인을 "
                                    f"못 팔았습니다 | 다음 스캔에 다시 (그동안 코인만 들고 있어 가격에 노출)")
                continue
            logger.critical("[숏 사라짐] #%d %s | 바이비트에 숏이 없음 (장부 %g개) | 강제청산으로 보고 업비트 코인 즉시 정리 | "
                            "증거금 %.2f USDT 손실 | 순손익 예상 %+.2f%%",
                            pos.id, pos.coin, pos.bybit_qty, pos.bybit_margin_usdt, valuation.pnl.net_percent)
            self._close(session, pos, valuation, ExitReason.LIQUIDATION, liquidated=True)

    def _check_liquidation(self, session: Session, pos: Position, quote: Quote) -> bool:
        """
        바이비트 숏 강제청산 확인 + 가드

        1) 강제청산가는 거래소 값을 우선 쓴다 (봇 계산값보다 거래소가 먼저 자른다)
        2) 바이비트 가격이 강제청산가 이상이면: 숏은 강제청산된 것으로 보고(증거금 잃음), 업비트 코인도 즉시 정리 → True
        3) 남은 상승폭이 LIQ_GUARD_PERCENT 안이면 봇이 판단한다 (_liq_guard): 증거금 추가 또는 정리
        4) 그 밖에 LIQ_WARN_PERCENT 안이면 사람에게 경고만

        예) 강제청산가 1.793, 지금 1.60 → 남은 상승폭 12% → 경고(15%). 1.64 → 9.3% → 가드(10%)
        """
        price = quote.liquidation_check_price
        if not price:
            return False
        try:
            ex_liq = self.broker.ex.position_liq_price(session, pos.coin)
        except Exception as exc:
            self._log_throttled(f"liq-read-{pos.coin}", logging.WARNING,
                                f"[강제청산가 조회 실패] #{pos.id} {pos.coin} | {exc} | 봇 계산값으로 봅니다")
            ex_liq = None
        if ex_liq and ex_liq > 0:
            pos.liquidation_price = ex_liq   # 거래소 값이 진실
        liq = pos.liquidation_price
        if price >= liq:
            valuation = self.broker.value_liquidation(pos, quote)
            if valuation is None:
                self._log_throttled(f"liq-thin-{pos.coin}", logging.CRITICAL,
                                    f"[강제청산] #{pos.id} {pos.coin} | 강제청산가 도달 | 업비트 호가가 모자라 정리 못 함 | 다음 스캔에 다시")
                return False
            logger.critical("[강제청산] #%d %s | 바이비트 %.6f ≥ 강제청산가 %.6f | 증거금 %.2f USDT 손실 | 업비트 코인 즉시 정리",
                            pos.id, pos.coin, price, liq, pos.bybit_margin_usdt)
            self._close(session, pos, valuation, ExitReason.LIQUIDATION, liquidated=True)
            return True

        distance = (liq / price - 1) * 100
        pos.last_liq_distance_percent = distance
        if distance < self.s.liq_guard_percent:
            return self._liq_guard(session, pos, quote, price, distance)
        if distance < self.s.liq_warn_percent:
            # 예고: 가드(liq_guard_percent)가 곧 할 일을 그대로 적는다 — 사람이 먼저 /close 하거나 손으로 넣을 시간
            added = pos.margin_added_usdt or 0.0
            unit = (pos.bybit_margin_usdt - added) * self.s.liq_guard_topup_unit_ratio
            rounds_left = max(0, self.s.liq_guard_topup_max_rounds - (int(round(added / unit)) if unit > 0 else 0))
            target, _stop = self._exit_rules(pos)
            valuation = self.broker.value_exit(pos, quote)
            net_text = f"순손익 {valuation.pnl.net_percent:+.2f}% / 목표 {target:.2f}%" if valuation else "순손익 계산 불가"
            if rounds_left:
                plan = (f"목표까지 {self.s.liq_guard_near_target_percent:.1f}%p 이내거나 순손익 "
                        f"{self.s.liq_guard_hold_below_percent:+.0f}% 이하면 증거금 {unit:,.2f} USDT 추가 (남은 {rounds_left}회), "
                        f"아니면 정리")
            else:
                plan = f"증거금 추가 {self.s.liq_guard_topup_max_rounds}회 다 써서 정리"
            self._log_throttled(
                f"liq-warn-{pos.coin}", logging.WARNING,
                f"[긴급 · 강제청산 임박] #{pos.id} {pos.coin} | 강제청산까지 {distance:.1f}% (지금 {price:.6f} / 강제청산가 {liq:.6f}) | "
                f"{net_text} | {self.s.liq_guard_percent:.0f}% 안으로 오면 봇이: {plan}",
            )
        return False

    def _margin_buffer_usdt(self, session: Session, quote: Quote, open_positions: List[Position]) -> float:
        """
        여유분 = 자본을 나눌 때 바이비트에 증거금 외로 더 두기로 한 돈 (BYBIT_MARGIN_BUFFER_PERCENT). 증거금 추가는 이 안에서만

        자본 비율(upbit_target_share)은 업비트 : 바이비트 증거금 : 여유분 = 레버리지 : 1 : 여유분% 이다.
        그러니 여유분 = 전체 자본 × 여유분% ÷ (레버리지 + 1 + 여유분%).
        예) 1,400 USDT, 1배, 30% → 1,400 × 0.3 ÷ 2.3 ≈ 183 USDT
        포지션 크기와는 무관하다 — 검증용으로 포지션을 20 USDT 로 줄여도 바이비트에 더 둔 돈은 그대로다.
        """
        upbit, bybit = self.broker.capital_split(session, open_positions, {quote.coin: quote})
        b = self.s.bybit_margin_buffer_percent / 100
        return (upbit + bybit) * b / (self.s.bybit_leverage + 1 + b)

    def _liq_guard(self, session: Session, pos: Position, quote: Quote, price: float, distance: float) -> bool:
        """
        강제청산가의 90% 지점: 증거금을 더 넣고 들고 갈지, 지금 정리할지

            목표까지 ≤ LIQ_GUARD_NEAR_TARGET_PERCENT → 증거금 추가 (곧 익절)
            순손익 ≤ LIQ_GUARD_HOLD_BELOW_PERCENT    → 증거금 추가 (역프 깊음 — 지금 정리하면 손실 확정, 돌아올 때까지)
            그 외                                     → 정리 (LIQ_GUARD). 청산당하는 것과 돈 차이는 거의 없지만 시점을 우리가 고른다

        넣는 돈의 출처와 양 (_margin_buffer_usdt):
            한 번에 그 포지션의 원래 증거금 × LIQ_GUARD_TOPUP_UNIT_RATIO (1배·배수 1 이면 청산가 2배 → 3배),
            포지션당 최대 LIQ_GUARD_TOPUP_MAX_ROUNDS 회.
            전체 포지션이 넣은 합이 '여유분' (자본 나눌 때 바이비트에 더 둔 30%) 을 넘지 못한다
            → 자리 몫(다음 진입 돈)은 절대 건드리지 않는다. 여유분을 다 썼거나 회차를 다 썼으면 정리.

        Returns:
            정리했으면 True
        """
        valuation = self.broker.value_exit(pos, quote)
        net = valuation.pnl.net_percent if valuation else None
        target, _stop = self._exit_rules(pos)
        added = pos.margin_added_usdt or 0.0
        initial = pos.bybit_margin_usdt - added
        unit = initial * self.s.liq_guard_topup_unit_ratio          # 한 번에 넣는 양
        rounds_used = int(round(added / unit)) if unit > 0 else 0

        why_hold = None
        if net is not None and target - net <= self.s.liq_guard_near_target_percent:
            why_hold = f"목표까지 {target - net:.2f}%p 뿐"
        elif net is not None and net <= self.s.liq_guard_hold_below_percent:
            why_hold = f"역프 깊음 (순손익 {net:+.2f}%, 지금 정리하면 손실 확정)"

        blocked = None   # 넣고 싶은데 못 넣는 이유
        if why_hold:
            if rounds_used >= self.s.liq_guard_topup_max_rounds:
                blocked = f"이미 {rounds_used}회 넣음 (최대 {self.s.liq_guard_topup_max_rounds}회)"
            else:
                open_positions = self.broker.open_positions(session)
                buffer = self._margin_buffer_usdt(session, quote, open_positions)
                used = sum(p.margin_added_usdt or 0.0 for p in open_positions)
                free = self.broker.balance(session, BYBIT_USDT)
                add = min(unit, max(0.0, buffer - used), max(0.0, free))
                if add < MIN_TOPUP_USDT:
                    blocked = (f"여유분 {buffer:.2f} USDT 중 {used:.2f} 사용, 남은 {max(0.0, buffer - used):.2f} "
                               f"(바이비트 잔고 {free:.2f})")
                else:
                    try:
                        self._account_dirty = True
                        self.broker.add_margin(session, pos, add)
                    except Exception as exc:
                        blocked = f"거래소 거절: {exc}"
                        logger.critical("[증거금 추가 실패] #%d %s | %s", pos.id, pos.coin, exc)
                    else:
                        logger.warning("[증거금 추가] #%d %s | 강제청산까지 %.1f%% 였음 | +%.2f USDT (%d/%d회, 여유분 %.2f 중 %.2f 사용) | "
                                       "새 강제청산가 %.6f | 이유: %s",
                                       pos.id, pos.coin, distance, add, rounds_used + 1, self.s.liq_guard_topup_max_rounds,
                                       buffer, used + add, pos.liquidation_price, why_hold)
                        return False
            logger.warning("[증거금 추가 불가] #%d %s | %s | 정리로 넘어갑니다", pos.id, pos.coin, blocked)

        if valuation is None:
            self._log_throttled(f"guard-thin-{pos.coin}", logging.CRITICAL,
                                f"[강제청산 회피] #{pos.id} {pos.coin} | 정리하려는데 호가가 모자람 | 다음 스캔에 다시")
            return False
        reason_text = blocked if why_hold else f"목표까지 {target - (net or 0):.2f}%p 남고 손실도 {net or 0:+.2f}% 라 작음"
        logger.warning("[강제청산 회피] #%d %s | 강제청산까지 %.1f%% | %s | 지금 시세에 정리합니다 (순손익 %+.2f%%)",
                       pos.id, pos.coin, distance, reason_text, net or 0.0)
        self._close(session, pos, valuation, ExitReason.LIQ_GUARD)
        return True

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
                    "[송금 · %s] %s | 금액 %s USDT (수수료 %s) | 전: 업비트 %s / 바이비트 %s (%.1f : %.1f) | 목표 %.1f : %.1f",
                    when, direction, f"{abs(sent):,.2f}", f"{fee:g}", f"{upbit:,.2f}", f"{bybit:,.2f}",
                    share * 100, (1 - share) * 100, target * 100, (1 - target) * 100,
                )
            return sent or None
        self._log_throttled(
            f"rebalance-{when}", logging.WARNING,
            f"[리밸런싱 필요 · {when}] 업비트 {upbit:,.2f} / 바이비트 {bybit:,.2f} USDT "
            f"({share * 100:.1f} : {(1 - share) * 100:.1f}) | 목표 {target * 100:.1f} : {(1 - target) * 100:.1f} | "
            f"{direction}로 약 {abs(move):,.2f} USDT 옮기세요",
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
        candidates = []
        for coin, quote in quotes.items():
            fee_rate = self.s.fees.upbit_trade_rate(coin)   # 코인별 업비트 요율 (진입 김프 계산에)
            if coin in open_positions or coin in busy or self._clock() < self._reentry_allowed_at.get(coin, 0.0):
                continue
            if coin not in self.s.coins:
                continue  # 이어받은 포지션 때문에 관리만 하던 코인 — 청산됐으면 새로 들어가지 않는다
            threshold = self.s.params_for(coin).entry_premium_threshold_percent
            # 1차 걸러내기: 맨 앞 호가 한 칸만 보고 판단한다 (수량만큼 소진하는 계산은 비싸다).
            # 맨 앞 호가 김프는 실제 체결 김프보다 **항상 유리하므로**, 여기서 떨어지면
            # 수량을 넣고 계산해도 절대 통과할 수 없다 → 안전하게 건너뛸 수 있다.
            top = quote.entry_premium_top
            if top is None or top > threshold + telemetry.NEAR_MARGIN_PERCENT:
                continue  # 기준 근처에도 못 왔으면 기록도 안 남긴다
            # 분석용 기록: 기준 근처부터는 어느 단계에서 걸렸는지 한 줄씩 남긴다 (kimp/telemetry.py)
            now_utc = self._now_utc()
            if top > threshold:
                telemetry.decision(session, coin, "near", top, threshold, now=now_utc)
                continue  # 맨 앞 호가로도 조건이 안 되면 더 볼 필요 없음
            # 호가 간격 안전장치: 지금 왕복 호가가 한도를 넘으면 (호가 단위가 넓어진 코인) 들어가지 않는다.
            # 왕복 호가 > 목표면 수학적으로 이익이 날 수 없다 — 가격이 호가 단위 경계를 넘은 코인을 걸러낸다
            exit_top = quote.exit_premium_top
            spread = (top - exit_top) if exit_top is not None else None
            limit = self.s.entry_max_spread_percent
            if limit and spread is not None and spread > limit:
                self._log_throttled(
                    f"spread-{coin}", logging.INFO,
                    f"[진입 보류] {coin} | 왕복 호가 {spread:.2f}% > 한도 {limit:.2f}% | 호가 단위가 넓어 목표 "
                    f"{self.s.params_for(coin).target_net_profit_percent:.2f}% 가 안 나옵니다", cooldown_sec=600.0)
                telemetry.decision(session, coin, "spread_block", top, threshold, premium_depth=spread, now=now_utc)
                continue
            # 호가 단위 경계 근처: 매도 호가가 '한 칸이 커지는 경계' 바로 아래·딱 경계면 들어가지 않는다 (docs/issues.md #4).
            # 스프레드 가드는 지금 호가 간격만 보므로, 99.9 / 100원처럼 아래쪽이 아직 작은 칸이면 통과시킨다
            near = near_tick_boundary(quote.upbit.best_ask or 0.0, self.s.entry_tick_boundary_margin_percent)
            if near is not None:
                edge, below, above = near
                self._log_throttled(
                    f"tick-edge-{coin}", logging.INFO,
                    f"[진입 보류] {coin} | 업비트 매도 호가 {quote.upbit.best_ask:g}원이 호가 단위 경계 {edge:g}원 근처 "
                    f"(한 칸 {below:g} → {above:g}원, 경계 아래 {self.s.entry_tick_boundary_margin_percent:g}% 안) | "
                    f"주문 사이 다음 줄로 뛰면 놓치고, 넘으면 익절이 어려워짐", cooldown_sec=600.0)
                telemetry.decision(session, coin, "tick_edge_block", top, threshold,
                                   premium_depth=quote.upbit.best_ask, now=now_utc)
                continue
            # 펀딩비 안전장치: 숏이 돈을 내는 쪽이 심하면 들어가지 않는다 (김프 조건을 통과한 코인만 확인)
            min_funding = self.s.entry_min_funding_8h_percent
            funding = None
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
                    telemetry.decision(session, coin, "funding_block", top, threshold, funding_8h=funding, now=now_utc)
                    continue
            size = self._position_size_usdt(session, coin, list(open_positions.values()), quotes)
            bybit_qty = self._plan_bybit_qty(coin, quote, size)
            if bybit_qty is None:
                telemetry.decision(session, coin, "size_reject", top, threshold, size_usdt=size, now=now_utc)
                continue
            # 2차: 실제 주문 수량만큼 호가를 소진한 평균 체결가로 다시 계산한다.
            # 수량이 크면 호가가 밀려서 1차는 통과했어도 여기서 떨어질 수 있다 — 그게 맞는 동작이다.
            # 업비트 지정가 FOK 크기·안전 확인도 여기서 (kimp/fok_sizing.py, docs/issues.md #3 A·B):
            # 지정가 안쪽 물량의 일정 비율로 숏 개수를 줄이고, 전부 마지막 줄 가격에 사도 기준을 넘는지 본다
            fit = self._fit_entry(coin, quote, bybit_qty, threshold, fee_rate)
            premium = fit.premium
            if fit.status == "depth_reject":
                self._log_throttled(f"reject-{coin}", logging.INFO,
                                    f"{coin} 진입 보류: 호가가 모자라서 정한 크기를 다 못 채움")
                telemetry.decision(session, coin, "depth_reject", top, threshold, size_usdt=size,
                                   bybit_qty=bybit_qty, funding_8h=funding, now=now_utc)
                continue
            if fit.status == "ok" and fit.shrunk and not self._min_order_ok(coin, quote, fit.bybit_qty):
                telemetry.decision(session, coin, "size_reject", top, threshold, premium_depth=premium,
                                   size_usdt=size, bybit_qty=fit.bybit_qty, funding_8h=funding,
                                   note="지정가 안쪽 물량에 맞춰 줄였더니 최소 주문보다 작음", now=now_utc)
                continue
            if fit.status == "worst_fail":
                telemetry.decision(session, coin, "worst_fail", top, threshold, premium_depth=fit.worst_premium,
                                   size_usdt=size, bybit_qty=fit.bybit_qty, funding_8h=funding,
                                   note="평균 김프는 기준 안, 전부 마지막 줄 가격이면 기준 밖", now=now_utc)
                continue
            if fit.status == "ok":
                bybit_qty = fit.bybit_qty
            if fit.status == "ok" and premium <= threshold:
                # 정렬 기준으로 '기준보다 얼마나 더 낮은가'(음수, 작을수록 좋음)를 맨 앞에 둔다.
                # 김프 절댓값으로 줄 세우면 기준이 느슨한 코인이 늘 이긴다 — 코인마다 기준이
                # 다르므로 '자기 기준 대비 얼마나 유리한가' 로 비교해야 공평하다.
                candidates.append((premium - threshold, coin, bybit_qty, quote, top, size, funding))
            else:
                telemetry.decision(session, coin, "depth_fail", top, threshold, premium_depth=premium,
                                   size_usdt=size, bybit_qty=bybit_qty, funding_8h=funding, now=now_utc)

        ranked = sorted(candidates)
        for _margin, coin, bybit_qty, quote, top, size, funding in ranked[slots:]:
            # 조건은 됐지만 자리가 없어 밀린 코인 (분석용). 기준은 코인마다 다르니 다시 꺼낸다
            thr = self.s.params_for(coin).entry_premium_threshold_percent
            telemetry.decision(session, coin, "no_slot", top, thr, premium_depth=_margin + thr,
                               size_usdt=size, bybit_qty=bybit_qty, funding_8h=funding, now=self._now_utc())
        for _margin, coin, bybit_qty, quote, top, size, funding in ranked[:slots]:
            params = self.s.params_for(coin)
            threshold = params.entry_premium_threshold_percent
            depth = _margin + threshold
            # 주문 직전 확인 시세: 스캔 시작 때 값이 한순간 튄 것이면 여기서 걸러진다
            fresh = self._fresh_quote(coin)
            fit = self._fit_entry(coin, fresh, bybit_qty, threshold, fee_rate) if fresh else None
            again = fit.premium if fit else None
            if fit is not None and fit.status == "ok" and fit.shrunk and not self._min_order_ok(coin, fresh, fit.bybit_qty):
                fit = None
            if fit is None or fit.status != "ok" or again is None or again > threshold:
                self._log_throttled(
                    f"entry-confirm-{coin}", logging.INFO,
                    f"[진입 보류] {coin} | 스캔 시세 {depth:+.3f}% → 확인 시세 "
                    f"{'없음' if again is None else f'{again:+.3f}%'} | 기준 {threshold:+.2f}% | 한순간 값, 다음 스캔에 다시")
                telemetry.decision(session, coin, "confirm_fail", top, threshold, premium_depth=again,
                                   size_usdt=size, bybit_qty=bybit_qty, funding_8h=funding, now=self._now_utc())
                continue
            quote, depth = fresh, again   # 더 새 시세로 주문한다
            planned = self.market.round_qty(coin, size / quote.bybit.best_bid) if quote.bybit.best_bid else bybit_qty
            if fit.bybit_qty < planned * (1 - 1e-9):   # 스캔 단계에서 줄였든 확인 시세에서 더 줄였든 한 번만 남긴다
                logger.info("[진입 크기 줄임] %s | 숏 %g → %g개 (지정가 안쪽 물량의 %g%%) | %s", coin, planned,
                            fit.bybit_qty, self.s.entry_max_depth_share_percent, fit.describe())
            bybit_qty = fit.bybit_qty
            self.broker.mark_decided()    # 진입 판단 → 주문 전송 ms 측정
            self._account_dirty = True    # 주문이 나가니 다음 스캔엔 잔고를 새로 받는다 (성공하든 거절되든)
            try:
                try:
                    pos = self.broker.open_hedge(session, quote, bybit_qty)
                except InsufficientBalance as exc:
                    # 한쪽이 모자람 → 리밸런싱 (페이퍼는 자동 송금 후 한 번 더 시도, 실거래는 알림만)
                    self._log_throttled(f"reject-{coin}", logging.INFO, f"{coin} 진입 보류: {exc}")
                    telemetry.decision(session, coin, "balance_block", top, threshold, premium_depth=depth,
                                       size_usdt=size, bybit_qty=bybit_qty, funding_8h=funding, note=str(exc),
                                       now=self._now_utc())
                    if not self._check_rebalance(session, list(open_positions.values()), quotes,
                                                 when="진입 막힘", need=exc) or not self.auto_transfer:
                        continue
                    if self.broker.pending_transfers(session):
                        continue  # 실거래: 송금이 도착하면 다음 스캔에 다시 시도
                    pos = self.broker.open_hedge(session, quote, bybit_qty)
            except EntryMissed as exc:
                # 지정가가 비껴가 못 삼. 판 USDT 는 되샀고 숏은 없었다 → 잠시 뒤 다시 (호가가 돌아오면 그때)
                self._reentry_allowed_at[coin] = self._clock() + FOK_RETRY_SEC
                why, detail = self._miss_reason(coin, exc.fok)
                logger.info("[진입 놓침] %s | %s | 이유: %s — %s | %g초 뒤 다시", coin, exc, why, detail, FOK_RETRY_SEC)
                telemetry.decision(session, coin, "entry_miss", top, threshold, premium_depth=depth, size_usdt=size,
                                   bybit_qty=bybit_qty, funding_8h=funding, note=f"{why} | {exc} | {detail}",
                                   now=self._now_utc())
                continue
            except EntryRejected as exc:
                self._log_throttled(f"reject-{coin}", logging.INFO, f"{coin} 진입 보류: {exc}")
                telemetry.decision(session, coin, "rejected", top, threshold, premium_depth=depth, size_usdt=size,
                                   bybit_qty=bybit_qty, funding_8h=funding, note=str(exc), now=self._now_utc())
                continue
            # 진입 때 기준을 포지션에 적어둔다 (나중에 설정을 바꿔도 이 포지션은 이 값으로 청산)
            # 이게 없으면 들고 있는 중에 .env 목표를 올리면 이미 목표에 닿은 포지션이 안 팔린다.
            pos.entry_threshold_percent = params.entry_premium_threshold_percent
            pos.target_net_profit_percent = params.target_net_profit_percent
            pos.stop_loss_percent = params.stop_loss_percent
            pos.run_id = self.run_id
            session.commit()
            telemetry.decision(session, coin, "entered", top, threshold, premium_depth=depth, size_usdt=size,
                               bybit_qty=bybit_qty, funding_8h=funding, position_id=pos.id, now=self._now_utc())
            notional = pos.bybit_qty * pos.entry_bybit_price_usdt
            target = estimated_exit_premium(pos.entry_premium, params.target_net_profit_percent, notional, 0.0,
                                            self.s.fees, quote.usdt_spread_percent or 0.0, coin=coin)
            logger.info(
                "[진입] #%d %s | 김프 %+.3f%% (기준 %+.2f%%, 판단 %+.3f%%) | 목표 %.2f%% → 김프 약 %+.2f%% 필요 | "
                "업비트 USDT %.2f개 매도 (%.1f원) → %.6g개 매수 (%s원) | 바이비트 %g개 숏 (%.6f USDT, %.0f배) | "
                "강제청산가 %.6f",
                pos.id, coin, pos.entry_premium, params.entry_premium_threshold_percent, depth,
                params.target_net_profit_percent, target,
                pos.usdt_sold, pos.entry_usdt_price_krw, pos.upbit_qty, f"{pos.entry_upbit_price_krw:,.2f}",
                pos.bybit_qty, pos.entry_bybit_price_usdt, pos.leverage, pos.liquidation_price,
            )
            open_positions[coin] = pos
            if self.s.one_shot:
                self._one_shot_opened = True
                logger.warning("[한 번만 돌리기] #%d %s | 포지션을 잡았습니다 | 더 진입하지 않고 청산까지 봅니다",
                               pos.id, coin)
                return  # 여러 코인이 동시에 조건을 만족해도 하나만 잡는다

    def _fit_entry(self, coin: str, quote: Quote, bybit_qty: float, threshold: float, fee_rate: float) -> EntryFit:
        """업비트 지정가 FOK 진입 크기·안전 확인 (kimp/fok_sizing.py). 숏 개수는 바이비트 주문 단위로 내린다"""
        return fit_entry(quote, bybit_qty, fee_rate, threshold, self.s.entry_max_depth_share_percent,
                         lambda q: self.market.round_qty(coin, q))

    def _min_order_ok(self, coin: str, quote: Quote, qty: float) -> bool:
        """숏 개수가 거래소 최소 주문(바이비트 개수·금액, 업비트 금액)을 넘나. 아니면 사유를 로그로 남기고 False"""
        best_bid = quote.bybit.best_bid
        min_qty = self.market.min_qty(coin)
        if not best_bid or qty <= 0 or qty < min_qty:
            self._log_throttled(f"reject-{coin}", logging.INFO,
                                f"{coin} 진입 보류: 개수 {qty} 가 바이비트 최소 {min_qty} 보다 작음")
            return False
        min_usdt = getattr(self.market, "min_notional_usdt", lambda _c: 0.0)(coin)
        if qty * best_bid < min_usdt:
            self._log_throttled(f"reject-{coin}", logging.INFO,
                                f"{coin} 진입 보류: 바이비트 주문 금액 {qty * best_bid:.2f} USDT 가 최소 {min_usdt:g} USDT 보다 작음")
            return False
        min_krw = self.broker.ex.min_order_krw(coin)
        if quote.usdt_krw and qty * best_bid * quote.usdt_krw < min_krw:
            self._log_throttled(f"reject-{coin}", logging.INFO,
                                f"{coin} 진입 보류: 업비트 주문 금액이 최소 {min_krw:,.0f}원보다 작음")
            return False
        return True

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
        return qty if self._min_order_ok(coin, quote, qty) else None

    # ------------------------------------------------------------------
    # 기록 / 로그
    # ------------------------------------------------------------------
    def _log_throttled(self, key: str, level: int, message: str, cooldown_sec: float = LOG_COOLDOWN_SEC) -> None:
        """같은 종류의 로그는 cooldown_sec(기본 LOG_COOLDOWN_SEC)에 한 번만 (로그 도배 방지)"""
        now = self._clock()
        if now - self._last_log.get(key, 0.0) >= cooldown_sec:
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
        elapsed_min = max(1e-9, (now - self._last_status_log) / 60) if self._last_status_log else None
        steps_per_min = self._steps_since_status / elapsed_min if elapsed_min else None
        self._last_status_log, self._steps_since_status = now, 0

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
        scan_ms = (time.monotonic() - self._step_started) * 1000 if self._step_started else 0.0
        skews = [q.skew_ms for q in quotes.values() if q.skew_ms is not None]
        skew_ms = max(skews) if skews else None
        logger.info(
            "[현황] %s | %s | 포지션 %d/%d | 자본 업비트 %s / 바이비트 %s USDT (%.1f:%.1f, 목표 %.1f:%.1f) | "
            "스캔 %.1f초 (시세 %.1f초, 두 거래소 어긋남 %s) | 판단 %s",
            head, ", ".join(parts), len(open_positions), self.s.max_open_positions,
            f"{upbit:,.2f}", f"{bybit:,.2f}",
            upbit / total * 100 if total else 0, bybit / total * 100 if total else 0,
            target * 100, (1 - target) * 100, scan_ms / 1000, self._quote_ms / 1000,
            "-" if skew_ms is None else f"{skew_ms}ms",
            "-" if steps_per_min is None else f"{steps_per_min:.0f}회/분",
        )
        # 분석용: 자본 곡선과 스캔 소요 시간 (느려진 구간·통신 장애를 나중에 찾으려고)
        telemetry.engine_snapshot(session, upbit, bybit, len(open_positions), scan_ms, self._quote_ms,
                                  q0.usdt_krw, q0.fx_krw, self._now_utc(), quote_skew_ms=skew_ms)
        session.commit()

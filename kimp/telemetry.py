"""
나중에 분석하려고 남기는 기록 (매매 판단에는 아무 영향이 없다)

왜 따로 두나
------------------------------------------------------------------
실거래를 돌리고 난 뒤 "왜 안 들어갔나", "목표에 얼마나 가까이 갔다가 놓쳤나", "예상 체결가와 실제가
얼마나 달랐나", "스캔이 느려진 적은 없나" 를 로그 파일을 뒤지지 않고 DB 로 바로 셀 수 있어야 한다.
엔진·브로커는 여기 함수를 한 줄씩만 부르고, 무엇을 어떻게 남길지는 전부 이 파일에 있다.

무엇을 남기나 (표 이름 → 언제)
------------------------------------------------------------------
  runs               봇을 켤 때 1줄 (모드·코인·설정·git 커밋). 끌 때 종료 시각·사유를 채운다
  decisions          진입 판단: 맨 앞 호가 김프가 기준 근처(NEAR_MARGIN_PERCENT 안)에 올 때마다
                     어느 단계에서 걸렸는지 (near / funding_block / depth_fail / … / entered)
  position_snapshots 들고 있는 포지션의 평가를 SNAPSHOT_INTERVAL_SEC 마다 (순손익·청산 김프·펀딩·강제청산 거리)
  engine_snapshots   양쪽 자본과 스캔 소요 시간을 SNAPSHOT_INTERVAL_SEC 마다
  positions.max/min_net_percent  보유 중 순손익의 최고·최저와 그 시각 (목표에 얼마나 가까웠나)
  positions.planned_entry_premium 판단 때 김프 (실제 entry_premium 과의 차이 = 주문 사이 밀림)
  fills.expected_price / done_at 예상 체결가(호가 소진 계산)와 체결 확인 시각 (실제 평균가·보낸 시각과 비교)
  events             WARNING 이상 로그 전부를 DB 에도 남긴다 (예외는 traceback 까지) — 로그 파일이 넘어가도
                     "언제 무슨 문제가 있었나" 를 SQL 로 셀 수 있게 (DbLogHandler)

전부 '빈 값 허용' 칸과 새 표라서 예전 DB 파일에도 자동으로 덧붙는다 (kimp/db.py add_missing_columns).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import traceback
from dataclasses import asdict, is_dataclass
import time
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

from sqlalchemy.orm import Session

from kimp.db import Decision, EngineSnapshot, Event, Position, PositionSnapshot, Run, utcnow

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 문제 기록 (events) — 로그 핸들러
# ----------------------------------------------------------------------
class DbLogHandler(logging.Handler):
    """
    WARNING 이상 로그를 DB(events)에도 적는다

    로그 파일은 날짜별로 넘어가고 사람이 grep 해야 하지만, DB 에 있으면 "지난 2주 동안 CRITICAL 이
    몇 번, 어떤 종류였나" 를 바로 센다. 예외(logger.exception)는 traceback 전체를 남긴다.
    실패해도 절대 봇을 멈추지 않는다 (기록이 매매보다 중요할 수는 없다).
    """

    def __init__(self, session_factory, run_id: Optional[int] = None):
        super().__init__(level=logging.WARNING)
        self.session_factory = session_factory
        self.run_id = run_id

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(("sqlalchemy", "kimp.telemetry")):
            return  # DB 자체 로그를 DB 에 쓰면 돌고 돈다
        try:
            message = record.getMessage()
            exc_text = None
            if record.exc_info and record.exc_info[0] is not None:
                exc_text = "".join(traceback.format_exception(*record.exc_info))[-4000:]
            with self.session_factory() as session:
                session.add(Event(level=record.levelname, logger=record.name[:40], message=message[:1000],
                                  exc_text=exc_text, run_id=self.run_id, created_at=utcnow()))
                session.commit()
        except Exception:
            pass  # 기록 실패는 조용히 (여기서 로그를 찍으면 또 여기로 온다)


def install_db_log_handler(session_factory, run_id: Optional[int]) -> DbLogHandler:
    """루트 로거에 DbLogHandler 를 단다 (main 이 DB 를 연 다음 한 번 부른다)"""
    handler = DbLogHandler(session_factory, run_id)
    logging.getLogger().addHandler(handler)
    return handler

# 맨 앞 호가 김프가 '기준 + 이만큼' 안에 들어오면 판단 기록을 남긴다 (%p).
# 기준을 넘긴 것만 적으면 "아깝게 못 들어간" 순간이 안 남고, 모든 스캔을 적으면 표가 쓸데없이 커진다.
NEAR_MARGIN_PERCENT = 0.3

# 설정을 runs 에 남길 때 빼는 것 (비밀값)
SECRET_FIELDS = ("upbit_access_key", "upbit_secret_key", "bybit_api_key", "bybit_api_secret",
                 "telegram_bot_token", "telegram_chat_id", "bybit_deposit_address", "upbit_deposit_address",
                 "bybit_deposit_tag", "upbit_deposit_tag")


# ----------------------------------------------------------------------
# 실행 한 번 (runs)
# ----------------------------------------------------------------------
def git_commit() -> Optional[str]:
    """지금 코드의 git 커밋 (짧은 해시). git 이 없거나 저장소가 아니면 None"""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3,
                             cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return out.stdout.strip() or None
    except Exception:
        return None


def settings_summary(settings) -> Dict[str, Any]:
    """설정을 JSON 으로 남길 수 있는 모양으로 (비밀값 제외, 중첩 dataclass 는 펼친다)"""
    def plain(v):
        if is_dataclass(v):
            return {k: plain(x) for k, x in asdict(v).items()}
        if isinstance(v, (list, tuple)):
            return [plain(x) for x in v]
        if isinstance(v, dict):
            return {str(k): plain(x) for k, x in v.items()}
        if isinstance(v, (int, float, str, bool)) or v is None:
            return v
        return str(v)

    data = asdict(settings) if is_dataclass(settings) else dict(vars(settings))
    return {k: plain(v) for k, v in data.items() if k not in SECRET_FIELDS}


def start_run(session_factory, settings, coins: Iterable[str]) -> int:
    """봇 시작 기록 → run id. 실패해도 봇은 돌아야 하므로 예외를 삼키고 0 을 돌려준다"""
    try:
        with session_factory() as session:
            run = Run(mode=settings.mode, coins=",".join(coins), git_commit=git_commit(),
                      settings_json=json.dumps(settings_summary(settings), ensure_ascii=False),
                      started_at=utcnow())
            session.add(run)
            session.commit()
            logger.info("실행 기록 #%d (커밋 %s)", run.id, run.git_commit or "-")
            return run.id
    except Exception:
        logger.exception("실행 기록을 남기지 못했습니다 (봇은 계속 돕니다)")
        return 0


def end_run(session_factory, run_id: int, reason: str) -> None:
    """봇 종료 기록 (종료 시각·사유)"""
    if not run_id:
        return
    try:
        with session_factory() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.ended_at, run.stop_reason = utcnow(), reason[:40]
                session.commit()
    except Exception:
        logger.exception("실행 종료 기록을 남기지 못했습니다")


# ----------------------------------------------------------------------
# 진입 판단 (decisions)
# ----------------------------------------------------------------------
def decision(session: Session, coin: str, stage: str, premium_top: float, threshold: float, *,
             premium_depth: Optional[float] = None, size_usdt: Optional[float] = None,
             bybit_qty: Optional[float] = None, funding_8h: Optional[float] = None,
             position_id: Optional[int] = None, note: str = "", now: Optional[datetime] = None) -> None:
    """
    진입 판단 한 줄. stage 는 어디까지 갔다가 어떻게 됐는지:

        near           맨 앞 호가 김프가 기준 근처까지 왔지만 못 넘음 (아깝게 놓친 순간)
        spread_block   김프는 됐는데 지금 왕복 호가가 ENTRY_MAX_SPREAD_PERCENT 를 넘어 보류 (premium_depth = 왕복 호가)
        funding_block  김프는 됐는데 펀딩비가 너무 불리해서 보류
        size_reject    주문 크기가 거래소 최소 미만
        depth_reject   수량만큼 호가를 소진하니 다 못 채움 (호가 부족)
        depth_fail     맨 앞 호가는 통과했는데 수량 소진 평균가로는 기준 못 넘음 (호가 밀림). 크기를 지정가 안쪽 물량에 맞춘 뒤 본다
        worst_fail     평균 김프는 기준 안, 전부 마지막 매도 호가 줄 가격이면 기준 밖 (지정가 FOK 안전 확인, premium_depth = 그 김프)
        tick_edge_block 업비트 매도 호가가 호가 단위 경계(100원 등) 바로 아래·딱 경계라 보류 (premium_depth = 그 매도 호가)
        no_slot        조건은 됐지만 자리(최대 포지션 수·한 번만 돌리기)가 없어 다른 코인에 밀림
        balance_block  한쪽 USDT 가 모자람
        rejected       주문 단계에서 거절·되돌림 (note 에 이유)
        entered        진입함 (position_id)
        confirm_fail   조건은 됐는데 주문 직전 확인 시세에서는 기준을 못 넘음 (한순간 튄 값. premium_depth = 확인 시세 김프)
        tp_confirm_fail 익절 조건이 됐는데 확인 시세에서는 목표 미달 (premium_top = 처음 순손익, premium_depth = 확인 순손익,
                       threshold = 목표)
        tp_worst_fail  평균 순손익은 목표 이상, 전부 마지막 매수 호가 줄에 팔면 목표 미만 → 안 팖 (premium_depth = 그 순손익)
        tp_judge       익절 확정 (premium_top = 확인 순손익, threshold = 목표, premium_depth = 전부 마지막 매수 호가 줄에 팔 때
                       순손익). 2026-10-29 전 기록의 premium_depth 는 유동 여유(%p)
        entry_miss     코인 매수 지정가 FOK 가 전량 체결 안 돼 취소 → 판 USDT 되삼 (note 에 놓친 이유·지정가·호가)
        tp_miss        익절 매도 지정가 FOK 가 전량 체결 안 돼 취소 → 포지션 그대로, FOK_RETRY_SEC 뒤 다시
                       (premium_top = 순손익, note 에 놓친 이유·호가)
        filter_hold    김프는 기준 안인데 신호 신뢰도 점수가 컷 미만이라 기다림 (2026-10-31, docs/filter.md.
                       premium_depth = 수량 소진 김프, note = 점수)
        tp_filter_hold 순손익은 목표 이상인데 신뢰도 점수가 컷 미만이라 기다림 (premium_top = 순손익, threshold = 목표,
                       premium_depth = 점수)
    """
    # 조건이 유지되는 동안 매 판단마다 같은 줄이 쌓이는 단계(near·spread_block …)는 코인·단계별로
    # REPEAT_COOLDOWN_SEC 에 한 번만 적는다. 소켓 시세로 0.25초마다 판단하면 그냥 두면 시간당 만 줄이 넘는다.
    # 사건성 단계(entered·rejected·confirm_fail·tp_*)는 매번 적는다.
    if stage in REPEAT_STAGES:
        key = (coin, stage)
        last = _repeat_last.get(key)
        mono = time.monotonic()
        if last is not None and mono - last < REPEAT_COOLDOWN_SEC:
            return
        _repeat_last[key] = mono
    session.add(Decision(coin=coin, stage=stage, premium_top=premium_top, threshold_percent=threshold,
                         premium_depth=premium_depth, size_usdt=size_usdt, bybit_qty=bybit_qty,
                         funding_8h_percent=funding_8h, position_id=position_id, note=(note or "")[:200],
                         created_at=now or utcnow()))
    session.commit()


# 반복 단계: 조건이 이어지는 동안 매 판단마다 나오는 것들 (코인·단계별로 REPEAT_COOLDOWN_SEC 에 한 번만 기록)
REPEAT_STAGES = frozenset({"near", "spread_block", "funding_block", "size_reject", "depth_reject", "depth_fail",
                           "no_slot", "balance_block", "filter_hold", "tp_filter_hold"})
REPEAT_COOLDOWN_SEC = 10.0
_repeat_last: Dict[Tuple[str, str], float] = {}


def reset_repeat_cooldown() -> None:
    """테스트용: 반복 단계 기록 억제를 초기화 (같은 프로세스에서 여러 테스트가 돌 때)"""
    _repeat_last.clear()


# ----------------------------------------------------------------------
# 보유 중 평가 (position_snapshots + 최고/최저)
# ----------------------------------------------------------------------
def note_extremes(pos: Position, net_percent: float, now: datetime) -> bool:
    """
    보유 중 순손익의 최고·최저를 포지션에 적어 둔다 (목표에 얼마나 가까웠다가 놓쳤는지 보려고). commit 은 부르는 쪽

    Returns: 최고나 최저가 바뀌었으면 True (부르는 쪽이 이때는 꼭 커밋한다)
    """
    changed = False
    if pos.max_net_percent is None or net_percent > pos.max_net_percent:
        pos.max_net_percent, pos.max_net_at = net_percent, now
        changed = True
    if pos.min_net_percent is None or net_percent < pos.min_net_percent:
        pos.min_net_percent, pos.min_net_at = net_percent, now
        changed = True
    return changed


def position_snapshot(session: Session, pos: Position, valuation, quote, now: datetime) -> None:
    """들고 있는 포지션의 '지금 청산하면' 평가 한 줄 (SNAPSHOT_INTERVAL_SEC 마다). commit 은 부르는 쪽"""
    pnl = valuation.pnl
    session.add(PositionSnapshot(
        position_id=pos.id, net_percent=pnl.net_percent, net_usdt=pnl.net_usdt,
        exit_premium=valuation.exit_premium, mid_premium=quote.mid_premium,
        upbit_mid_krw=quote.upbit.mid, bybit_mid_usdt=quote.bybit.mid, usdt_krw=quote.usdt_krw,
        funding_usdt=pos.funding_usdt, liq_distance_percent=pos.last_liq_distance_percent,
        created_at=now,
    ))


# ----------------------------------------------------------------------
# 엔진 현황 (engine_snapshots)
# ----------------------------------------------------------------------
def engine_snapshot(session: Session, upbit_usdt: float, bybit_usdt: float, open_positions: int,
                    scan_ms: float, quote_ms: float, usdt_krw: Optional[float], fx_krw: Optional[float],
                    now: datetime, quote_skew_ms: Optional[int] = None) -> None:
    """양쪽 자본(USDT 환산)과 스캔 소요 시간 한 줄. commit 은 부르는 쪽"""
    session.add(EngineSnapshot(upbit_usdt=upbit_usdt, bybit_usdt=bybit_usdt, open_positions=open_positions,
                               scan_ms=scan_ms, quote_ms=quote_ms, usdt_krw=usdt_krw, fx_krw=fx_krw,
                               quote_skew_ms=quote_skew_ms, created_at=now))

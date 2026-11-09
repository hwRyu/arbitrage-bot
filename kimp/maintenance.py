"""
거래소 점검 모드 (2026-11-08, 사용자 결정)

왜 있나
    봇은 "점검" 을 몰랐다. 거래소가 점검에 들어가면 2초마다 잔고·호가·펀딩 호출이 전부 실패하고, 60초마다 [계좌 조회 지연],
    5분마다 [호가 조회 실패] 가 찍혀 1시간 점검에 텔레그램이 100통 왔다. 그리고 점검 직전에 들어간 포지션은 점검 내내
    익절을 못 내고, 끝난 뒤 가격 갭에 노출됐다. 바이비트가 오류 대신 빈 포지션 목록을 정상 응답으로 주면 봇은 강제청산으로
    오판해 업비트 코인을 팔아 헷지를 깨는 길도 있었다.

어떻게
    사람이 텔레그램으로 점검 일정을 알려 준다 — `/maint bybit 11-12 02:00 11-12 04:00` (거래소, 시작 날짜·시각, 종료 날짜·시각,
    한국 시간). 형식이 틀리거나 종료가 시작보다 앞이면 등록하지 않고 이유를 답한다. `/maint off bybit` 로 푼다.
    공지 없이 한 거래소가 MAINT_AUTO_DETECT_SEC 동안 계속 응답이 없으면 스스로 점검 모드로 들어간다 (실거래, 계좌 갱신 스레드 기준).

    단계 (거래소마다 따로)
      예고   시작 MAINT_PRE_SEC 전부터: 신규 진입·송금 보류. 보유 포지션 관리는 그대로
      점검   그 거래소를 **아예 부르지 않는다** — 웹소켓 끊고 재접속 안 함, REST 호가·잔고·포지션·펀딩 안 받음, 주문 안 냄.
             다른 거래소는 평소대로 받는다. 엔진은 '한쪽 모드' 로 돈다 (kimp/engine.py _step_maintenance):
               공통: 진입·익절·손절·숏 사라짐 판정·송금 없음 (두 다리가 필요한 일은 전부 멈춤)
               바이비트 점검: 매칭이 멈춰 강제청산도 안 난다. 업비트 가격으로 바이비트 가격을 추정해 복귀 때 강제청산가까지
                              얼마나 남을지 보고 위험하면 경고만
               업비트 점검:   바이비트 숏은 살아 있어 강제청산이 난다. 바이비트 가격만으로 거리를 재고 증거금을 추가한다
                              (코인 매도는 못 하니 정리는 안 한다)
             자동 감지로 들어온 점검은 MAINT_PROBE_SEC 마다 한 번만 살아났는지 찔러 본다
      복구   종료 시각이 되면 호출을 재개하되, 응답이 돌아오고 **잔고·숏이 DB 와 맞는지 대조**가 될 때까지는 한쪽 모드를 유지한다
             (점검 직후 거래소가 빈 포지션 목록을 주는 경우에 코인을 팔지 않게). 30분 넘게 안 돌아오면 [점검 연장] 한 번
      안정화 복구 확인 뒤 MAINT_RECOVER_HOLD_SEC 동안 신규 진입만 보류 (가격 갭·얇은 호가). 신호 필터는 처음부터 다시 쌓는다

    알림은 시작 1통, 복구 1통 (자동 감지는 감지 1통 추가). 그 사이 Monitor 류 중간 메모는 없다.

여기서 하지 않는 것
    거래소 공지 자동 수집 (공지 API 가 들쭉날쭉하다). 일정은 사람이 적는다.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from kimp.db import MaintenanceWindow, utcnow

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
EXCHANGES: Tuple[str, str] = ("upbit", "bybit")
LABEL = {"upbit": "업비트", "bybit": "바이비트"}
_ALIASES = {"upbit": "upbit", "업비트": "upbit", "up": "upbit",
            "bybit": "bybit", "바이비트": "bybit", "by": "bybit"}
MAX_WINDOW_HOURS = 48.0          # 이보다 길면 오타로 본다
MAX_AHEAD_DAYS = 30              # 이보다 먼 일정은 받지 않는다
EXTENSION_ALERT_SEC = 1800.0     # 종료 시각 뒤 이만큼 지나도 안 돌아오면 [점검 연장]
DB_REFRESH_SEC = 10.0            # 일정 표를 DB 에서 다시 읽는 간격 (명령이 오면 즉시)

USAGE = ("형식: /maint <upbit|bybit> <시작 날짜> <시작 시각> <종료 날짜> <종료 시각>  (한국 시간)"
         "\n예) /maint bybit 11-12 02:00 11-12 04:00   또는   /maint upbit 2026-11-12 23:00 2026-11-13 01:00"
         "\n조회: /maint   해제: /maint off <upbit|bybit>  (거래소를 빼면 전부)")

_DATE_RE = re.compile(r"^(?:(\d{4})[-/.])?(\d{1,2})[-/.](\d{1,2})$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _as_utc(dt: datetime) -> datetime:
    """SQLite 가 시간대를 떼고 돌려준 시각에 UTC 를 붙인다"""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_kst(date_text: str, time_text: str, now: datetime) -> datetime:
    """
    '11-12' '02:00' (또는 '2026-11-12') → 한국 시간 datetime (tz 포함). 틀리면 ValueError 에 이유

    연도가 없으면 올해. 그 결과가 지금보다 하루 넘게 과거면(12월 말에 1월 일정) 내년으로 본다
    """
    m = _DATE_RE.match(date_text)
    if not m:
        raise ValueError(f"날짜 형식이 틀립니다: '{date_text}' (예: 11-12 또는 2026-11-12)")
    t = _TIME_RE.match(time_text)
    if not t:
        raise ValueError(f"시각 형식이 틀립니다: '{time_text}' (예: 02:00)")
    year_text, month, day = m.groups()
    hour, minute = int(t.group(1)), int(t.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"시각이 말이 안 됩니다: '{time_text}'")
    now_kst = now.astimezone(KST)
    year = int(year_text) if year_text else now_kst.year
    try:
        dt = datetime(year, int(month), int(day), hour, minute, tzinfo=KST)
    except ValueError as exc:
        raise ValueError(f"날짜가 말이 안 됩니다: '{date_text} {time_text}' ({exc})") from exc
    if not year_text and dt < now_kst - timedelta(days=1):
        # 12월 말에 적은 1월 일정만 내년으로 본다. 그냥 지난 날짜(어제·지난달)는 그대로 두어 '이미 지났다' 로 거절되게
        rolled = dt.replace(year=year + 1)
        if rolled <= now_kst + timedelta(days=MAX_AHEAD_DAYS):
            dt = rolled
    return dt


def fmt_kst(dt: datetime) -> str:
    """'11-12 02:00' (한국 시간)"""
    return _as_utc(dt).astimezone(KST).strftime("%m-%d %H:%M")


def fmt_window(w: MaintenanceWindow) -> str:
    """'바이비트 11-12 02:00~04:00 KST' / 날짜가 다르면 둘 다 / 자동 감지(종료 미정)"""
    start = fmt_kst(w.start_at)
    if w.end_at is None:
        return f"{LABEL.get(w.exchange, w.exchange)} {start}~(종료 미정, 자동 감지) KST"
    end_kst = _as_utc(w.end_at).astimezone(KST)
    end = end_kst.strftime("%H:%M") if end_kst.date() == _as_utc(w.start_at).astimezone(KST).date() else fmt_kst(w.end_at)
    return f"{LABEL.get(w.exchange, w.exchange)} {start}~{end} KST"


@dataclass(frozen=True)
class ParsedCommand:
    """/maint 인자를 해석한 것. action: list / cancel / add"""
    action: str
    exchange: Optional[str] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


def parse_command(args: str, now: datetime) -> ParsedCommand:
    """
    /maint 인자 해석. 말이 안 되면 ValueError (답장 문구)

        ''                                   → 조회
        'off' / 'off bybit'                  → 해제 (전부 / 그 거래소)
        '<ex> <d1> <t1> <d2> <t2>'           → 등록
    """
    tokens = args.split()
    if not tokens:
        return ParsedCommand("list")
    head = tokens[0].lower()
    if head in ("off", "cancel", "clear", "해제", "취소"):
        if len(tokens) == 1:
            return ParsedCommand("cancel", None)
        ex = _ALIASES.get(tokens[1].lower())
        if ex is None:
            raise ValueError(f"거래소는 upbit 또는 bybit 입니다: '{tokens[1]}'\n{USAGE}")
        return ParsedCommand("cancel", ex)
    ex = _ALIASES.get(head)
    if ex is None:
        raise ValueError(f"거래소는 upbit 또는 bybit 입니다: '{tokens[0]}'\n{USAGE}")
    if len(tokens) != 5:
        raise ValueError(f"시작과 종료는 각각 '날짜 시각' 으로 적습니다 (받은 항목 {len(tokens) - 1}개, 필요 4개)\n{USAGE}")
    start = parse_kst(tokens[1], tokens[2], now)
    end = parse_kst(tokens[3], tokens[4], now)
    if end <= start:
        raise ValueError(f"종료({fmt_kst(end)})가 시작({fmt_kst(start)})보다 뒤여야 합니다")
    if end <= now:
        raise ValueError(f"종료 시각({fmt_kst(end)})이 이미 지났습니다 (지금 {fmt_kst(now)})")
    hours = (end - start).total_seconds() / 3600
    if hours > MAX_WINDOW_HOURS:
        raise ValueError(f"점검이 {hours:.0f}시간입니다 — {MAX_WINDOW_HOURS:.0f}시간을 넘는 일정은 오타로 보고 받지 않습니다")
    if start > now + timedelta(days=MAX_AHEAD_DAYS):
        raise ValueError(f"시작이 {MAX_AHEAD_DAYS}일 넘게 남았습니다 ({fmt_kst(start)}) — 너무 먼 일정은 받지 않습니다")
    return ParsedCommand("add", ex, start.astimezone(timezone.utc), end.astimezone(timezone.utc))


# ----------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------
def live_windows(session: Session, now: datetime) -> List[MaintenanceWindow]:
    """취소되지 않았고 아직 끝나지 않은(또는 종료 미정) 일정, 시작 순"""
    rows = session.scalars(select(MaintenanceWindow).where(MaintenanceWindow.cancelled_at.is_(None))
                           .order_by(MaintenanceWindow.start_at)).all()
    return [w for w in rows if w.end_at is None or _as_utc(w.end_at) > now]


def add_window(session: Session, exchange: str, start_at: datetime, end_at: Optional[datetime],
               source: str = "manual", note: str = "", now: Optional[datetime] = None) -> MaintenanceWindow:
    """일정 하나 저장. 같은 거래소에 겹치는 일정이 있으면 ValueError. now 는 호출자 시계 (테스트는 가짜 시계)"""
    now = now or utcnow()
    for w in live_windows(session, now):
        if w.exchange != exchange:
            continue
        w_end = _as_utc(w.end_at) if w.end_at is not None else None
        overlap = end_at is None or w_end is None or (start_at < w_end and _as_utc(w.start_at) < end_at)
        if overlap:
            raise ValueError(f"이미 등록된 일정과 겹칩니다: {fmt_window(w)} — 먼저 /maint off {exchange}")
    w = MaintenanceWindow(exchange=exchange, start_at=start_at, end_at=end_at, source=source, note=note or None)
    session.add(w)
    session.commit()
    return w


def cancel_windows(session: Session, exchange: Optional[str], now: datetime) -> List[MaintenanceWindow]:
    """살아 있는 일정을 취소 표시 (exchange 가 None 이면 전부). 취소한 것들을 돌려준다"""
    done = []
    for w in live_windows(session, now):
        if exchange is not None and w.exchange != exchange:
            continue
        w.cancelled_at = now
        done.append(w)
    session.commit()
    return done


def end_window(session: Session, window_id: int, now: datetime) -> None:
    """자동 감지 일정(종료 미정)에 종료 시각을 적는다 — 살아난 것을 확인했을 때"""
    w = session.get(MaintenanceWindow, window_id)
    if w is not None and w.end_at is None:
        w.end_at = now
        session.commit()


# ----------------------------------------------------------------------
# 엔진이 매 스캔 묻는 상태 기계
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class MaintStatus:
    """이번 스캔에서 '거래가 안 되는' 거래소들 (점검 중 + 복구 확인 중). 비어 있으면 평소대로"""
    down: FrozenSet[str] = frozenset()
    text: str = ""          # [현황] 에 붙일 한 줄 (비어 있으면 없음)

    @property
    def normal(self) -> bool:
        return not self.down


@dataclass
class _Recovery:
    since: float
    window: MaintenanceWindow
    last_probe: float = 0.0
    extension_told: float = 0.0


class MaintenanceManager:
    """
    점검 일정 → 거래소별 단계(예고/점검/복구/안정화) → 일시정지·재개·진입 차단

    pausers: pause_exchange(ex)/resume_exchange(ex) 를 가진 것들 (MarketData — 안에서 웹소켓·펀딩 캐시까지 끈다)
    refresher: 계좌 갱신 스레드 (실거래). pause(ex, probe_every)/resume(ex)/fresh_within(ex, sec)/failing_for(ex)
    probe: 비실거래에서 '살아났나' 확인 (ex → bool). 없으면 refresher 로만 판단
    confirm: 복구 확인 (ex → (ok, 설명)). 엔진이 잔고·숏 대조로 준다. 없으면 응답만 돌아오면 복구로 본다
    on_recovered: 복구 확인 뒤 한 번 부른다 (엔진이 신호 필터를 비우는 데 쓴다)
    """

    def __init__(self, settings, session_factory, clock: Callable[[], float] = time.time, safety=None,
                 pausers: Sequence = (), refresher=None, probe: Optional[Callable[[str], bool]] = None,
                 confirm: Optional[Callable[[str], Tuple[bool, str]]] = None,
                 on_recovered: Optional[Callable[[str], None]] = None):
        self.s = settings
        self.session_factory = session_factory
        self._clock = clock
        self.safety = safety
        self.pausers = [p for p in pausers if p is not None and hasattr(p, "pause_exchange")]
        self.refresher = refresher
        self._probe = probe
        self._confirm = confirm
        self._on_recovered = on_recovered
        self._windows: List[MaintenanceWindow] = []
        self._loaded_at = -1e18
        self._dirty = threading.Event()
        self._dirty.set()
        self._announced_pre: Set[int] = set()
        self._active: Dict[str, MaintenanceWindow] = {}
        self._recovering: Dict[str, _Recovery] = {}
        self._hold_until: Dict[str, float] = {}
        self._risk_notes: Dict[str, str] = {}      # 점검 중 엔진이 적어 둔 위험 메모 (복구 알림에 붙인다)
        self._last_log: Dict[str, float] = {}

    # ------------------------------------------------------------------ 설정값
    @property
    def pre_sec(self) -> float:
        return float(getattr(self.s, "maint_pre_sec", 1800.0))

    @property
    def hold_sec(self) -> float:
        return float(getattr(self.s, "maint_recover_hold_sec", 600.0))

    @property
    def detect_sec(self) -> float:
        return float(getattr(self.s, "maint_auto_detect_sec", 180.0))

    @property
    def probe_sec(self) -> float:
        return float(getattr(self.s, "maint_probe_sec", 300.0))

    def _now_dt(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), timezone.utc)

    # ------------------------------------------------------------------ 텔레그램 (다른 스레드)
    def command(self, args: str) -> str:
        """/maint 처리. 돌려주는 글자가 답장"""
        now = self._now_dt()
        try:
            cmd = parse_command(args or "", now)
        except ValueError as exc:
            return f"등록 안 됨: {exc}"
        with self.session_factory() as session:
            if cmd.action == "list":
                lines = self.status_lines(session)
                return "\n".join(lines + [USAGE])
            if cmd.action == "cancel":
                done = cancel_windows(session, cmd.exchange, now)
                self._dirty.set()
                if not done:
                    return "해제할 점검 일정이 없습니다" + (f" ({LABEL[cmd.exchange]})" if cmd.exchange else "")
                return "해제: " + ", ".join(fmt_window(w) for w in done) + "\n점검 중이었으면 다음 스캔부터 복구 확인으로 넘어갑니다"
            try:
                w = add_window(session, cmd.exchange, cmd.start_at, cmd.end_at, source="manual", now=now)
            except ValueError as exc:
                return f"등록 안 됨: {exc}"
            self._dirty.set()
            hours = (cmd.end_at - cmd.start_at).total_seconds() / 3600
            return (f"등록: {fmt_window(w)} ({hours:g}시간)"
                    f"\n• 시작 {self.pre_sec / 60:.0f}분 전부터 신규 진입·송금 보류 (보유 포지션 관리는 계속)"
                    f"\n• 점검 중 {LABEL[cmd.exchange]} 호출 전부 중단, 다른 거래소만 감시"
                    f"\n• 종료 뒤 잔고·숏 대조로 복구 확인 → {self.hold_sec / 60:.0f}분 안정화 뒤 진입 재개"
                    f"\n해제: /maint off {cmd.exchange}")

    def status_lines(self, session: Optional[Session] = None) -> List[str]:
        """/status·/maint 에 붙이는 줄들"""
        now = self._now_dt()
        if session is None:
            with self.session_factory() as s:
                wins = live_windows(s, now)
        else:
            wins = live_windows(session, now)
        if not wins and not self._recovering and not self._hold_until:
            return ["[점검] 등록된 점검 없음"]
        lines = []
        for w in wins:
            if w.exchange in self._active:
                state = "점검 중"
            elif _as_utc(w.start_at) - timedelta(seconds=self.pre_sec) <= now:
                state = "예고 (진입·송금 보류)"
            else:
                state = "예정"
            lines.append(f"[점검] {fmt_window(w)} · {state}" + (" · 자동 감지" if w.source == "auto" else ""))
        for ex in self._recovering:
            lines.append(f"[점검] {LABEL[ex]} 종료 → 복구 확인 중 (대조 일치까지 거래 안 함)")
        for ex, until in self._hold_until.items():
            left = max(0.0, until - self._clock()) / 60
            lines.append(f"[점검] {LABEL[ex]} 복귀 안정화 {left:.0f}분 남음 (신규 진입 보류)")
        return lines

    # ------------------------------------------------------------------ 엔진 (매 스캔)
    def update(self) -> MaintStatus:
        """한 스캔 앞에서 부른다. 단계를 옮기고, 거래가 안 되는 거래소 집합을 돌려준다"""
        now = self._clock()
        now_dt = datetime.fromtimestamp(now, timezone.utc)
        wins = self._load(now)
        down: Set[str] = set()
        reasons: List[str] = []
        texts: List[str] = []
        for ex in EXCHANGES:
            cur = self._current_window(wins, ex, now_dt)
            if ex in self._active:
                aw = self._active[ex]
                still = cur is not None and cur.id == aw.id
                if still and aw.source == "auto" and self._alive(ex):
                    with self.session_factory() as session:
                        end_window(session, aw.id, now_dt)
                    self._dirty.set()
                    still = False
                if still:
                    down.add(ex)
                    reasons.append(f"{LABEL[ex]} 점검 중 ({fmt_window(aw)})")
                    texts.append(self._active_text(aw, now_dt))
                else:
                    self._end_outage(ex, aw, now)
            elif ex not in self._recovering:
                if cur is not None and now_dt >= _as_utc(cur.start_at):
                    self._begin_outage(ex, cur, now)
                    down.add(ex)
                    reasons.append(f"{LABEL[ex]} 점검 중 ({fmt_window(cur)})")
                    texts.append(self._active_text(cur, now_dt))
                elif cur is not None:
                    if cur.id not in self._announced_pre:
                        self._announced_pre.add(cur.id)
                        logger.info("[점검 예정] %s | 시작 %.0f분 전부터 신규 진입·송금 보류, 보유 포지션 관리는 계속",
                                    fmt_window(cur), self.pre_sec / 60)
                    reasons.append(f"{LABEL[ex]} 점검 예정 ({fmt_window(cur)})")
                else:
                    auto = self._auto_detect(ex, now, now_dt)
                    if auto is not None:
                        self._begin_outage(ex, auto, now)
                        down.add(ex)
                        reasons.append(f"{LABEL[ex]} 점검 중 (자동 감지)")
                        texts.append(self._active_text(auto, now_dt))
            if ex in self._recovering:
                self._try_recover(ex, now)
            if ex in self._recovering:   # 확인이 안 끝났으면 이번 스캔도 거래 없음
                down.add(ex)
                reasons.append(f"{LABEL[ex]} 점검 종료 뒤 복구 확인 중")
                texts.append(f"{LABEL[ex]} 복구 확인 중")
            until = self._hold_until.get(ex)
            if until is not None:
                if now < until:
                    reasons.append(f"{LABEL[ex]} 복귀 안정화 {max(0.0, until - now) / 60:.0f}분 남음")
                else:
                    del self._hold_until[ex]
                    logger.info("[점검 복귀 완료] %s | 안정화 %.0f분 끝 → 신규 진입 재개", LABEL[ex], self.hold_sec / 60)
        if self.safety is not None:
            self.safety.maintenance_reason = ("거래소 점검: " + " / ".join(reasons)) if reasons else ""
        return MaintStatus(down=frozenset(down), text=" · ".join(texts))

    def transfers_blocked(self) -> bool:
        """예고·점검·복구·안정화 중에는 송금(입출금)을 보내지 않는다"""
        return bool(self.safety is not None and self.safety.maintenance_reason) or bool(self._active) or bool(self._recovering)

    def note_risk(self, exchange: str, text: str) -> None:
        """점검 중 엔진이 본 위험을 적어 둔다 (복구 알림에 한 줄로 붙인다)"""
        self._risk_notes[exchange] = text

    # ------------------------------------------------------------------ 내부
    def _load(self, now: float) -> List[MaintenanceWindow]:
        if self._dirty.is_set() or now - self._loaded_at >= DB_REFRESH_SEC:
            self._dirty.clear()
            self._loaded_at = now
            with self.session_factory() as session:
                rows = live_windows(session, datetime.fromtimestamp(now, timezone.utc))
                # 세션을 닫아도 값을 읽게 미리 만져 둔다 (expire_on_commit=False 지만 안전하게)
                self._windows = [w for w in rows]
        return self._windows

    def _current_window(self, wins: Iterable[MaintenanceWindow], exchange: str, now_dt: datetime) -> Optional[MaintenanceWindow]:
        """예고 구간에 들어왔거나 진행 중인 일정 (시작 순으로 첫 것)"""
        for w in wins:
            if w.exchange != exchange:
                continue
            start = _as_utc(w.start_at)
            if start - timedelta(seconds=self.pre_sec) <= now_dt and (w.end_at is None or now_dt < _as_utc(w.end_at)):
                return w
        return None

    def _active_text(self, w: MaintenanceWindow, now_dt: datetime) -> str:
        if w.end_at is None:
            return f"{LABEL[w.exchange]} 점검 중 (자동 감지, {self.probe_sec / 60:.0f}분마다 확인)"
        left = (_as_utc(w.end_at) - now_dt).total_seconds() / 60
        return f"{LABEL[w.exchange]} 점검 중 ({fmt_window(w)}, 남은 {max(0.0, left):.0f}분)"

    def _alive(self, exchange: str) -> bool:
        """그 거래소가 응답하나 (실거래: 갱신 스레드의 최근 성공, 아니면 probe 함수)"""
        if self.refresher is not None and hasattr(self.refresher, "fresh_within"):
            interval = float(getattr(self.refresher, "interval_sec", 2.0))
            return bool(self.refresher.fresh_within(exchange, max(interval * 3, 6.0)))
        if self._probe is not None:
            try:
                return bool(self._probe(exchange))
            except Exception:   # noqa: BLE001 — 확인 실패는 '아직' 이다
                return False
        return True

    def _auto_detect(self, exchange: str, now: float, now_dt: datetime) -> Optional[MaintenanceWindow]:
        """공지 없는 장애: 갱신 스레드가 detect_sec 넘게 그 거래소에서 실패만 하면 자동 점검 일정을 만든다"""
        if self.detect_sec <= 0 or self.refresher is None or not hasattr(self.refresher, "failing_for"):
            return None
        failing = self.refresher.failing_for(exchange)
        if failing < self.detect_sec:
            return None
        with self.session_factory() as session:
            try:
                w = add_window(session, exchange, now_dt - timedelta(seconds=failing), None, source="auto",
                               note=f"{failing:.0f}초 응답 없음", now=now_dt)
            except ValueError:
                return None
        self._dirty.set()
        logger.critical("[점검 감지] %s %.0f초째 응답 없음 → 점검 모드 | 호출 중단, %.0f분마다 한 번만 확인 | "
                        "다른 거래소만 감시, 진입·청산·송금 없음", LABEL[exchange], failing, self.probe_sec / 60)
        return w

    def _begin_outage(self, exchange: str, w: MaintenanceWindow, now: float) -> None:
        self._active[exchange] = w
        self._risk_notes.pop(exchange, None)
        for p in self.pausers:
            try:
                p.pause_exchange(exchange)
            except Exception:   # noqa: BLE001
                logger.exception("[점검] %s 일시정지 실패: %s", exchange, type(p).__name__)
        if self.refresher is not None and hasattr(self.refresher, "pause"):
            self.refresher.pause(exchange, probe_every_sec=self.probe_sec if w.source == "auto" else None)
        if w.source == "manual":
            other = LABEL["upbit" if exchange == "bybit" else "bybit"]
            logger.warning("[점검 시작] %s | %s 호출 전부 중단 (시세·잔고·포지션·펀딩·주문) | %s 시세만 감시, 진입·청산·송금 없음 | "
                           "종료 뒤 대조로 복구 확인 → %.0f분 안정화 뒤 진입 재개",
                           fmt_window(w), LABEL[exchange], other, self.hold_sec / 60)

    def _end_outage(self, exchange: str, w: MaintenanceWindow, now: float) -> None:
        del self._active[exchange]
        for p in self.pausers:
            try:
                p.resume_exchange(exchange)
            except Exception:   # noqa: BLE001
                logger.exception("[점검] %s 재개 실패: %s", exchange, type(p).__name__)
        if self.refresher is not None and hasattr(self.refresher, "resume"):
            self.refresher.resume(exchange)
        self._recovering[exchange] = _Recovery(since=now, window=w)
        logger.info("[점검 종료 시각] %s | 호출 재개, 응답과 잔고·숏 대조가 될 때까지 거래 없음", fmt_window(w))

    def _try_recover(self, exchange: str, now: float) -> None:
        r = self._recovering[exchange]
        # 첫 확인은 재개 뒤 갱신 스레드가 한 바퀴 돌 시간을 준다 (즉시 묻으면 늘 '아직')
        first_wait = 5.0 if self.refresher is not None else 0.0
        if r.last_probe and now - r.last_probe < self.probe_sec:
            return
        if not r.last_probe and now - r.since < first_wait:
            return
        r.last_probe = now
        if not self._alive(exchange):
            self._extension_alert(exchange, r, now)
            return
        ok, detail = (True, "")
        if self._confirm is not None:
            try:
                ok, detail = self._confirm(exchange)
            except Exception as exc:   # noqa: BLE001
                ok, detail = False, f"확인 중 오류: {exc}"
        if not ok:
            self._log_throttled(f"confirm-{exchange}", logging.CRITICAL,
                                f"[점검 복구 대조 불일치] {LABEL[exchange]} 응답은 돌아왔지만 {detail} | 거래 계속 보류, "
                                f"{self.probe_sec / 60:.0f}분 뒤 다시 확인", 1800.0)
            return
        del self._recovering[exchange]
        self._hold_until[exchange] = now + self.hold_sec
        risk = self._risk_notes.pop(exchange, None)
        logger.warning("[점검 종료] %s 복구 확인 (%s) | %.0f분 안정화 뒤 신규 진입 재개 | 점검 중 위험 메모: %s",
                       LABEL[exchange], detail or "응답 정상", self.hold_sec / 60, risk or "없음")
        if self._on_recovered is not None:
            try:
                self._on_recovered(exchange)
            except Exception:   # noqa: BLE001
                logger.exception("[점검] 복구 뒤 처리 실패")

    def _extension_alert(self, exchange: str, r: _Recovery, now: float) -> None:
        over = now - r.since
        if over >= EXTENSION_ALERT_SEC and now - r.extension_told >= EXTENSION_ALERT_SEC:
            r.extension_told = now
            logger.critical("[점검 연장] %s 종료 시각에서 %.0f분 지났는데 아직 응답 없음 | %.0f분마다 확인, 거래 보류 계속",
                            LABEL[exchange], over / 60, self.probe_sec / 60)

    def _log_throttled(self, key: str, level: int, message: str, cooldown_sec: float) -> None:
        now = self._clock()
        if now - self._last_log.get(key, -1e18) >= cooldown_sec:
            self._last_log[key] = now
            logger.log(level, message)

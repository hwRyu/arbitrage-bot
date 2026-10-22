"""
성과 대시보드 집계 — "정한 기간에 얼마 벌었나" 를 한 덩어리로 계산한다

webui 의 `/api/dashboard` 가 이걸 쓴다. HTTP 와 떼어 놔서 테스트가 쉽고, 터미널에서도 쓸 수 있다.

읽는 곳 (source)
------------------------------------------------------------------
    paper / dry_run / live   봇이 실제로 돌려서 남긴 `positions` (모드마다 DB 가 다르다)
    sim:<번호>               시뮬레이션 결과 `sim_trades` (같은 모양으로 맞춰서 본다)

실거래 기록이 없을 때도 시뮬레이션 결과를 같은 화면으로 볼 수 있게 두 곳을 한 모양(`Trade`)으로 맞춘다.

계산하는 것
------------------------------------------------------------------
    요약      거래 수 · 승률 · 순손익 · 김프 몫 / 펀딩비 / 비용 쪼개기 · 평균 보유 시간 · 최대 낙폭 · 월 환산
    누적 곡선  청산 시각 순서로 순손익을 더해 나간 선 (차트용)
    일별      한국 시간 날짜별 손익 (막대 차트용)
    코인별    코인마다 거래 수 · 손익 · 승률 · 평균 보유 시간
    사유별    TAKE_PROFIT / STOP_LOSS / ... 별 거래 수와 손익
    이력      거래 하나하나 + 아직 열려 있는 포지션 + 송금 기록

기간은 **청산 시각(closed_at)** 기준으로 거른다. 손익이 확정된 날에 잡는 게 맞기 때문이다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select

from kimp.config import Settings
from kimp.db import Position, PositionStatus, Transfer, make_session_factory
from kimp.history import SimRun, SimTrade, make_history_session_factory

KST = timezone(timedelta(hours=9))
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
DEFAULT_DAYS = 30.0
TRADE_LIMIT = 500  # 이력 표에 내려줄 최대 거래 수 (화면이 느려지지 않게)

OPEN_STATES = (PositionStatus.OPENING, PositionStatus.OPEN, PositionStatus.CLOSING, PositionStatus.ERROR)


# ======================================================================
# 한 모양으로 맞추기
# ======================================================================
@dataclass(frozen=True)
class Trade:
    """끝난 거래 한 건 (봇 포지션이든 시뮬레이션이든 이 모양으로 본다. 금액은 모두 USDT)"""

    id: int
    coin: str
    opened_ms: int
    closed_ms: Optional[int]
    entry_premium: Optional[float]
    exit_premium: Optional[float]
    exit_reason: Optional[str]
    net_usdt: float  # 순손익 = 김프 몫 + 펀딩비 − 비용
    net_percent: Optional[float]
    funding_usdt: float
    cost_usdt: float
    notional_usdt: Optional[float]  # 포지션 금액 (= 업비트에서 판 USDT)

    @property
    def hold_hours(self) -> Optional[float]:
        """보유 시간 (시간). 아직 안 닫혔으면 None"""
        return None if self.closed_ms is None else (self.closed_ms - self.opened_ms) / HOUR_MS

    @property
    def gross_usdt(self) -> float:
        """김프 몫 (비용·펀딩비 빼기 전) = 순손익 − 펀딩비 + 비용"""
        return self.net_usdt - self.funding_usdt + self.cost_usdt

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "coin": self.coin, "opened_ms": self.opened_ms, "closed_ms": self.closed_ms,
            "entry_premium": self.entry_premium, "exit_premium": self.exit_premium,
            "exit_reason": self.exit_reason, "net_usdt": self.net_usdt, "net_percent": self.net_percent,
            "funding_usdt": self.funding_usdt, "cost_usdt": self.cost_usdt, "gross_usdt": self.gross_usdt,
            "notional_usdt": self.notional_usdt, "hold_hours": self.hold_hours,
        }


def _ms(value: Any) -> Optional[int]:
    """DB 에서 읽은 시각 → epoch 밀리초. SQLite 는 시간대를 안 적으므로 없으면 UTC 로 본다"""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _kst_date(ms: int) -> str:
    """epoch 밀리초 → 한국 시간 날짜 (YYYY-MM-DD)"""
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%Y-%m-%d")


def _from_position(p: Position) -> Trade:
    return Trade(
        id=p.id, coin=p.coin, opened_ms=_ms(p.opened_at) or 0, closed_ms=_ms(p.closed_at),
        entry_premium=p.entry_premium, exit_premium=p.exit_premium, exit_reason=p.exit_reason,
        net_usdt=p.net_pnl_usdt or 0.0, net_percent=p.net_pnl_percent,
        funding_usdt=p.funding_usdt or 0.0, cost_usdt=p.total_cost_usdt or 0.0,
        notional_usdt=p.usdt_sold,
    )


def _from_sim_trade(t: SimTrade) -> Trade:
    return Trade(
        id=t.id, coin=t.coin, opened_ms=_ms(t.opened_at) or 0, closed_ms=_ms(t.closed_at),
        entry_premium=t.entry_premium, exit_premium=t.exit_premium, exit_reason=t.exit_reason,
        net_usdt=t.net_pnl_usdt or 0.0, net_percent=t.net_pnl_percent,
        funding_usdt=t.funding_usdt or 0.0, cost_usdt=t.cost_usdt or 0.0,
        # 시뮬레이션은 포지션 금액을 따로 안 적는다 → 손익과 손익률로 거꾸로 구한다
        notional_usdt=(t.net_pnl_usdt / t.net_pnl_percent * 100) if t.net_pnl_percent else None,
    )


# ======================================================================
# 어디서 읽을지 (source)
# ======================================================================
def _mode_db(settings: Settings, source: str) -> Optional[str]:
    """source 가 모드 이름이면 그 모드의 DB 경로"""
    return {"paper": settings.db_path, "dry_run": settings.dry_run_db_path,
            "live": settings.live_db_path}.get(source)


def default_source(settings: Settings) -> str:
    """
    처음 열었을 때 보여줄 기록

    지금 모드(실거래/드라이런/페이퍼)에 닫은 거래가 있으면 그것, 없으면 가장 최근 시뮬레이션.
    실거래를 아직 한 번도 안 했을 때 빈 화면이 뜨지 않게 하려는 것이다.
    """
    if _closed_count(_mode_db(settings, settings.mode)):
        return settings.mode
    runs = _sim_runs(settings, limit=1)
    return f"sim:{runs[0]['id']}" if runs else settings.mode


def list_sources(settings: Settings) -> List[Dict[str, Any]]:
    """
    고를 수 있는 기록 목록 (화면 드롭다운용)

    모드 DB 는 닫은 거래 수를, 시뮬레이션은 기간·코인·총손익을 같이 준다.
    """
    out: List[Dict[str, Any]] = []
    for key, label in (("live", "실거래"), ("dry_run", "드라이런"), ("paper", "페이퍼")):
        path = _mode_db(settings, key)
        out.append({"value": key, "label": f"{label} ({os.path.basename(path)})", "kind": "bot",
                    "db": path, "exists": os.path.exists(path), "trades": _closed_count(path),
                    "active": settings.mode == key})
    for run in _sim_runs(settings):
        summary = run["summary"]
        out.append({
            "value": f"sim:{run['id']}", "kind": "sim", "db": settings.history_db_path, "exists": True,
            "label": f"시뮬 #{run['id']} {run['coins']} "
                     f"{_kst_date(run['from_ms'])}~{_kst_date(run['to_ms'])} "
                     f"({summary.get('total_net_usdt', 0):+.1f} USDT)",
            "trades": summary.get("closed_trades"),
            "from_ms": run["from_ms"], "to_ms": run["to_ms"], "active": False,
        })
    return out


def _closed_count(path: Optional[str]) -> int:
    """그 DB 에 닫은 거래가 몇 건인지 (없는 파일이면 0)"""
    if not path or not os.path.exists(path):
        return 0
    with make_session_factory(path)() as session:
        return session.scalar(select(func.count()).select_from(Position)
                              .where(Position.status == PositionStatus.CLOSED)) or 0


def _sim_runs(settings: Settings, limit: int = 30) -> List[Dict[str, Any]]:
    """시뮬레이션 실행 목록 (최근 것부터)"""
    if not os.path.exists(settings.history_db_path):
        return []
    with make_history_session_factory(settings.history_db_path)() as session:
        rows = session.scalars(select(SimRun).order_by(SimRun.id.desc()).limit(limit)).all()
        return [{"id": r.id, "coins": r.coins, "from_ms": r.from_ms, "to_ms": r.to_ms,
                 "summary": json.loads(r.summary_json or "{}")} for r in rows]


def load_trades(settings: Settings, source: str) -> Tuple[List[Trade], Dict[str, Any]]:
    """
    기록 읽기 (기간으로 거르기 전)

    Returns:
        (끝난 거래 전부, 그 기록에 대한 설명 {db, kind, exists, ...})
    """
    if source.startswith("sim:"):
        run_id = int(source.split(":", 1)[1])
        meta = {"kind": "sim", "db": settings.history_db_path, "run_id": run_id,
                "exists": os.path.exists(settings.history_db_path)}
        if not meta["exists"]:
            return [], meta
        with make_history_session_factory(settings.history_db_path)() as session:
            run = session.get(SimRun, run_id)
            if run is not None:
                meta.update({"coins": run.coins.split(","), "from_ms": run.from_ms, "to_ms": run.to_ms,
                             "summary": json.loads(run.summary_json or "{}"),
                             "settings": json.loads(run.settings_json or "{}")})
            rows = session.scalars(select(SimTrade).where(SimTrade.run_id == run_id)
                                   .order_by(SimTrade.id)).all()
        # 기간 끝까지 들고 있던 것(closed_at 없음)은 끝난 거래가 아니다
        return [_from_sim_trade(t) for t in rows if t.closed_at], meta

    path = _mode_db(settings, source)
    if path is None:
        raise ValueError(f"모르는 기록: {source}")
    meta = {"kind": "bot", "db": path, "mode": source, "exists": os.path.exists(path)}
    if not meta["exists"]:
        return [], meta
    with make_session_factory(path)() as session:
        rows = session.scalars(select(Position).where(Position.status == PositionStatus.CLOSED)
                               .order_by(Position.id)).all()
    return [_from_position(p) for p in rows], meta


def load_open(settings: Settings, source: str) -> List[Dict[str, Any]]:
    """아직 열려 있는 포지션 (시뮬레이션에는 없다)"""
    path = None if source.startswith("sim:") else _mode_db(settings, source)
    if not path or not os.path.exists(path):
        return []
    with make_session_factory(path)() as session:
        return [
            {"id": p.id, "coin": p.coin, "status": p.status, "opened_ms": _ms(p.opened_at),
             "entry_premium": p.entry_premium, "notional_usdt": p.usdt_sold,
             "funding_usdt": p.funding_usdt, "last_net_percent": p.last_net_percent,
             "last_exit_premium": p.last_exit_premium, "target": p.target_net_profit_percent,
             "stop": p.stop_loss_percent, "liquidation_price": p.liquidation_price}
            for p in session.scalars(select(Position).where(Position.status.in_(OPEN_STATES))
                                     .order_by(Position.id)).all()
        ]


def load_transfers(settings: Settings, source: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
    """기간 안의 거래소 간 송금 기록 (리밸런싱 이력)"""
    path = None if source.startswith("sim:") else _mode_db(settings, source)
    if not path or not os.path.exists(path):
        return []
    with make_session_factory(path)() as session:
        rows = session.scalars(select(Transfer).order_by(Transfer.id.desc())).all()
    out = []
    for t in rows:
        at = _ms(t.requested_at)
        if at is None or at < start_ms or at > end_ms:
            continue
        out.append({"id": t.id, "direction": t.direction, "amount_usdt": t.amount_usdt,
                    "fee_usdt": t.fee_usdt, "network": t.network, "status": t.status,
                    "reason": t.reason, "note": t.note, "requested_ms": at,
                    "confirmed_ms": _ms(t.confirmed_at)})
    return out


# ======================================================================
# 집계
# ======================================================================
def in_range(trades: Sequence[Trade], start_ms: int, end_ms: int) -> List[Trade]:
    """청산 시각이 기간 안에 있는 거래만"""
    return [t for t in trades if t.closed_ms is not None and start_ms <= t.closed_ms <= end_ms]


def summarize(trades: Sequence[Trade], start_ms: int, end_ms: int) -> Dict[str, Any]:
    """
    기간 요약

    수익률은 두 가지로 본다 (어느 쪽도 혼자서는 오해를 준다)
        거래 규모 대비 = 순손익 ÷ 거래한 금액 합계  — 한 건당 얼마 남았나
        평균 손익률    = 거래별 손익률의 평균       — 포지션 크기가 달라도 비교 가능
    """
    days = max((end_ms - start_ms) / DAY_MS, 1e-9)
    if not trades:
        return {"trades": 0, "days": round(days, 1), "net_usdt": 0.0, "gross_usdt": 0.0,
                "funding_usdt": 0.0, "cost_usdt": 0.0, "wins": 0, "losses": 0, "win_rate": None,
                "avg_net_usdt": None, "avg_net_percent": None, "turnover_usdt": 0.0,
                "return_on_turnover": None, "best": None, "worst": None, "avg_hold_hours": None,
                "max_drawdown_usdt": 0.0, "net_per_day": 0.0, "net_per_month": 0.0,
                "trades_per_day": 0.0}

    net = sum(t.net_usdt for t in trades)
    wins = [t for t in trades if t.net_usdt > 0]
    losses = [t for t in trades if t.net_usdt < 0]
    percents = [t.net_percent for t in trades if t.net_percent is not None]
    holds = [t.hold_hours for t in trades if t.hold_hours is not None]
    turnover = sum(t.notional_usdt or 0.0 for t in trades)
    peak = drawdown = 0.0
    for point in equity_curve(trades):
        peak = max(peak, point["cum_usdt"])
        drawdown = min(drawdown, point["cum_usdt"] - peak)
    best = max(trades, key=lambda t: t.net_usdt)
    worst = min(trades, key=lambda t: t.net_usdt)
    return {
        "trades": len(trades),
        "days": round(days, 1),
        "net_usdt": net,
        "gross_usdt": sum(t.gross_usdt for t in trades),
        "funding_usdt": sum(t.funding_usdt for t in trades),
        "cost_usdt": sum(t.cost_usdt for t in trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_net_usdt": net / len(trades),
        "avg_net_percent": (sum(percents) / len(percents)) if percents else None,
        "turnover_usdt": turnover,
        "return_on_turnover": (net / turnover * 100) if turnover else None,
        "best": best.as_dict(),
        "worst": worst.as_dict(),
        "avg_hold_hours": (sum(holds) / len(holds)) if holds else None,
        "max_drawdown_usdt": drawdown,
        "net_per_day": net / days,
        "net_per_month": net / days * 30,
        "trades_per_day": len(trades) / days,
    }


def equity_curve(trades: Sequence[Trade]) -> List[Dict[str, Any]]:
    """청산 시각 순으로 순손익을 더해 나간 선 (차트용)"""
    out: List[Dict[str, Any]] = []
    total = 0.0
    for t in sorted(trades, key=lambda x: x.closed_ms or 0):
        total += t.net_usdt
        out.append({"ts": t.closed_ms, "cum_usdt": total, "net_usdt": t.net_usdt, "coin": t.coin})
    return out


def daily(trades: Sequence[Trade]) -> List[Dict[str, Any]]:
    """한국 시간 날짜별 손익 (막대 차트용). 거래가 없던 날은 넣지 않는다"""
    buckets: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        if t.closed_ms is None:
            continue
        day = _kst_date(t.closed_ms)
        b = buckets.setdefault(day, {"date": day, "net_usdt": 0.0, "trades": 0, "wins": 0})
        b["net_usdt"] += t.net_usdt
        b["trades"] += 1
        b["wins"] += 1 if t.net_usdt > 0 else 0
    return [buckets[d] for d in sorted(buckets)]


def by_coin(trades: Sequence[Trade]) -> List[Dict[str, Any]]:
    """코인별 집계 (손익 큰 순)"""
    groups: Dict[str, List[Trade]] = {}
    for t in trades:
        groups.setdefault(t.coin, []).append(t)
    out = []
    for coin, ts in groups.items():
        percents = [x.net_percent for x in ts if x.net_percent is not None]
        holds = [x.hold_hours for x in ts if x.hold_hours is not None]
        wins = sum(1 for x in ts if x.net_usdt > 0)
        out.append({
            "coin": coin, "trades": len(ts),
            "net_usdt": sum(x.net_usdt for x in ts),
            "funding_usdt": sum(x.funding_usdt for x in ts),
            "cost_usdt": sum(x.cost_usdt for x in ts),
            "wins": wins,
            "win_rate": wins / len(ts) * 100,
            "avg_net_percent": (sum(percents) / len(percents)) if percents else None,
            "avg_hold_hours": (sum(holds) / len(holds)) if holds else None,
            "avg_entry_premium": _avg([x.entry_premium for x in ts]),
            "avg_exit_premium": _avg([x.exit_premium for x in ts]),
            "best_usdt": max(x.net_usdt for x in ts),
            "worst_usdt": min(x.net_usdt for x in ts),
        })
    out.sort(key=lambda r: -r["net_usdt"])
    return out


def by_reason(trades: Sequence[Trade]) -> List[Dict[str, Any]]:
    """청산 사유별 집계 (거래 많은 순)"""
    groups: Dict[str, List[Trade]] = {}
    for t in trades:
        groups.setdefault(t.exit_reason or "-", []).append(t)
    out = [{"reason": r, "trades": len(ts), "net_usdt": sum(x.net_usdt for x in ts),
            "avg_net_percent": _avg([x.net_percent for x in ts]),
            "avg_hold_hours": _avg([x.hold_hours for x in ts])} for r, ts in groups.items()]
    out.sort(key=lambda r: -r["trades"])
    return out


def _avg(values: Sequence[Optional[float]]) -> Optional[float]:
    """None 을 뺀 평균 (값이 없으면 None)"""
    ok = [v for v in values if v is not None]
    return (sum(ok) / len(ok)) if ok else None


def default_range(now_ms: Optional[int] = None, days: float = DEFAULT_DAYS) -> Tuple[int, int]:
    """기본 기간: 지금까지 days 일"""
    import time as _time

    end = now_ms if now_ms is not None else int(_time.time() * 1000)
    return end - int(days * DAY_MS), end


def parse_day(text: str, end_of_day: bool = False) -> Optional[int]:
    """
    화면에서 온 'YYYY-MM-DD' → epoch 밀리초 (한국 시간 기준)

    끝 날짜는 그 날 23:59:59 까지 포함해야 한다 (end_of_day=True).
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=KST)
    except ValueError:
        return None
    if end_of_day:
        day += timedelta(days=1, microseconds=-1)
    return int(day.timestamp() * 1000)


def dashboard(settings: Settings, source: str = "", start: str = "", end: str = "",
              now_ms: Optional[int] = None) -> Dict[str, Any]:
    """
    대시보드 한 덩어리 (화면이 쓰는 모든 값)

    Args:
        source: 'live' / 'dry_run' / 'paper' / 'sim:3'
                (비우면 지금 모드. 지금 모드에 거래가 없으면 가장 최근 시뮬레이션)
        start, end: 'YYYY-MM-DD' (한국 시간). 비우면 기록 전체가 들어가는 기간으로 맞춘다
    """
    source = source or default_source(settings)
    trades, meta = load_trades(settings, source)
    closed = [t for t in trades if t.closed_ms is not None]

    # 거꾸로 적었으면 날짜 글자를 바꿔서 본다 (끝 날짜는 '그 날 끝까지' 로 늘려야 하므로 숫자가 아니라 글자를 바꾼다)
    if parse_day(start) is not None and parse_day(end) is not None and parse_day(end) < parse_day(start):
        start, end = end, start
    # 기간: 적어 준 날짜 → 없으면 기록 전체 → 기록도 없으면 최근 30일
    start_ms = parse_day(start)
    end_ms = parse_day(end, end_of_day=True)
    if start_ms is None or end_ms is None:
        if closed:
            auto_start = min(t.closed_ms for t in closed)
            auto_end = max(t.closed_ms for t in closed)
        else:
            auto_start, auto_end = default_range(now_ms)
        start_ms = start_ms if start_ms is not None else auto_start
        end_ms = end_ms if end_ms is not None else auto_end
    if end_ms < start_ms:  # 한쪽만 적어서 기록 범위와 엇갈린 경우
        start_ms, end_ms = end_ms, start_ms

    picked = in_range(closed, start_ms, end_ms)
    return {
        "source": source,
        "sources": list_sources(settings),
        "meta": meta,
        "range": {"start_ms": start_ms, "end_ms": end_ms,
                  "start": _kst_date(start_ms), "end": _kst_date(end_ms)},
        "all_trades": len(closed),
        "summary": summarize(picked, start_ms, end_ms),
        "equity": equity_curve(picked),
        "daily": daily(picked),
        "by_coin": by_coin(picked),
        "by_reason": by_reason(picked),
        "trades": [t.as_dict() for t in sorted(picked, key=lambda x: -(x.closed_ms or 0))][:TRADE_LIMIT],
        "open": load_open(settings, source),
        "transfers": load_transfers(settings, source, start_ms, end_ms),
    }

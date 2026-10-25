"""
목표 순이익 → 진입·청산 관리선(LCL / UCL) 역산

"순이익 1% 를 남기려면 김프가 몇 %p 올라야 하고, 그 폭을 어디에 놓아야 기회가 가장 많나"
김프 차트의 **목표로 맞추기** 가 이걸 쓴다.

왜 필요한가
------------------------------------------------------------------
김프가 올라야 하는 폭은 목표 수익만이 아니다. 나가는 돈을 다 메우고 **그 위로** 목표만큼 올라야 한다.
그런데 나가는 돈은 코인마다 다르다 (호가 간격이 ALT 0.95% / CAP 0.10%). 그래서 같은 '목표 1%' 가
코인마다 전혀 다른 난이도가 된다. 이 모듈은 코인의 실제 수치로 그 폭을 계산하고,
**그 폭을 기간 안에서 어디에 놓으면 거래가 가장 많이 일어나는지**까지 찾아 준다.

계산 순서
------------------------------------------------------------------
1) 비용을 모은다 (모두 포지션 금액 대비 %)
       왕복 거래 수수료   업비트 0.05%×4 + 바이비트 0.055%×2 = 0.31%
       코인 호가 간격     그 코인 실측 (없으면 설정값 → 기본 0.1%).  kimp/spread.py 와 같은 출처
                          + 업비트·바이비트 **호가 한 칸** 보다 좁으면 한 칸으로 올린다 (구조적 하한)
       USDT 호가 간격     기록의 중간값 (없으면 0.075%)
       전송비             TRANSFER_FEE_USDT_PER_CYCLE ÷ 포지션 금액
       펀딩비             8시간 비율 × 예상 보유 시간 ÷ 8  (받는 쪽이면 마이너스 = 필요 폭이 줄어든다)
       여유               SLIPPAGE_BUFFER_PERCENT (기본 0.05%)
2) 필요한 폭 = 목표 + 비용 합계
3) 그 폭을 유지한 채 LCL 을 아래에서 위로 훑어서, **완결된 왕복이 가장 많은** 자리를 고른다
       LCL 이하로 내려오면 진입 / 그 뒤 UCL 이상으로 올라오면 청산 (한 코인은 한 번에 하나만)
4) 펀딩비는 보유 시간에 걸리고 보유 시간은 관리선에 걸린다 (서로 물려 있다)
   → 3번을 한 번 더 돈다 (1차에서 나온 평균 보유 시간으로 비용을 다시 계산)

가운데 김프(`mid`)를 기준으로 계산한다. 호가 간격을 **비용 항목으로 따로** 넣기 때문에
진입/청산 김프를 쓰면 호가를 두 번 세게 된다. 1분봉만 있는 코인도 쓸 수 있다는 장점도 있다.

한계: 과거에 몇 번 지나갔는지는 '그 기간에 그랬다' 는 것뿐이다. 다음 기간을 보장하지 않는다.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kimp.config import Settings
from kimp.spread import resolve_spreads

PREMIUM_KEY = "mid"  # 어느 지표로 계산하나 (가운데 김프)
DEFAULT_USDT_SPREAD_PERCENT = 0.075  # 업비트 USDT/KRW 호가 간격 (기록이 없을 때 쓰는 값)
DEFAULT_NOTIONAL_USDT = 100.0  # 포지션 금액을 못 정할 때 (전송비 비율 계산용)
SCAN_STEPS = 240  # LCL 자리를 몇 칸으로 나눠 훑어볼지
MIN_POINTS = 20  # 이보다 점이 적으면 계산하지 않는다
HOUR_MS = 3_600_000


# ======================================================================
# 비용
# ======================================================================
@dataclass(frozen=True)
class CostItem:
    """비용 한 줄 (포지션 금액 대비 %)"""

    key: str
    name: str
    percent: float
    detail: str

    def as_dict(self) -> Dict[str, Any]:
        """화면에 보낼 모양 (JSON). detail 은 사람이 읽는 근거 글이다"""
        return {"key": self.key, "name": self.name, "percent": self.percent, "detail": self.detail}


def _median(values: Sequence[Optional[float]]) -> Optional[float]:
    """None 을 뺀 중간값 (값이 하나도 없으면 None)

    평균이 아니라 중간값을 쓰는 이유: 호가·펀딩비는 가끔 튀는 값이 섞여서 평균이 끌려간다.
    """
    ok = [v for v in values if v is not None]
    return statistics.median(ok) if ok else None


def notional_for(settings: Settings, coin: str) -> float:
    """
    이 코인 포지션 금액 (USDT) — 전송비가 몇 %인지 재려면 금액이 있어야 한다

    코인별/공통 POSITION_SIZE_USDT → MAX_POSITION_USDT → 기본값.
    자동 크기(자본 ÷ 포지션 수)는 잔고를 봐야 알 수 있어서 여기서는 상한을 대신 쓴다.
    """
    fixed = settings.params_for(coin).position_size_usdt
    if fixed:
        return float(fixed)
    if settings.max_position_usdt:
        return float(settings.max_position_usdt)
    return DEFAULT_NOTIONAL_USDT


def coin_spread_percent(settings: Settings, coin: str) -> Tuple[float, str]:
    """코인 호가 간격과 그 출처 (스크리너·시뮬레이션과 같은 규칙)"""
    paths = [p for p in settings.snapshot_db_paths() if os.path.exists(p)]
    spreads, sources = resolve_spreads([coin], paths, settings.sim_default_spread_percent)
    return float(spreads.get(coin) or settings.sim_default_spread_percent), sources.get(coin, "기본 설정")


def tick_floor_from_series(series: Sequence[Dict[str, Any]]) -> Optional[float]:
    """
    차트 가격으로 구한 호가 간격 하한 (%) = 업비트 호가 한 칸 + 바이비트 호가 한 칸

    업비트 원화 가격은 정해진 칸 단위로만 올라간다. 그래서 호가 간격은 **한 칸보다 좁아질 수 없다.**
    예) DOGE 106원, 칸 1원 → 아무리 거래가 활발해도 최소 0.94%
    기록이 모자라 추측값(0.1%)을 쓰는 코인이 실제보다 싸게 계산되는 걸 막는다.
    (시뮬레이션·스크리너가 쓰는 kimp/simulate.py 의 tick_floor_percent 와 같은 함수)
    """
    from kimp.simulate import tick_floor_percent

    up = [float(r["upbit_krw"]) for r in series if r.get("upbit_krw")]
    by = [float(r["bybit_usdt"]) for r in series if r.get("bybit_usdt")]
    if len(up) < 10 or len(by) < 10:
        return None
    try:
        return tick_floor_percent(up, by)
    except Exception:
        return None


def cost_items(settings: Settings, coin: str, series: Sequence[Dict[str, Any]],
               notional_usdt: float, hold_hours: float, exec_slippage=None) -> List[CostItem]:
    """
    나가는 돈 전부 (포지션 금액 대비 %)

    Args:
        hold_hours: 예상 보유 시간. 펀딩비가 여기에 비례한다
        exec_slippage: 체결 밀림 가정 (kimp/slippage.SlippageAssumption). None 이면 SLIPPAGE_BUFFER_PERCENT
    """
    f = settings.fees
    spread, spread_source = coin_spread_percent(settings, coin)
    floor = tick_floor_from_series(series)
    if floor is not None and spread < floor:  # 호가 한 칸보다 좁게 가정하는 일은 없어야 한다
        spread, spread_source = floor, f"{spread_source} {spread:.3f}% → 호가 단위 하한으로 올림"
    usdt_spread = _median([r.get("usdt_spread") for r in series])
    funding_8h = _median([r.get("funding") for r in series])

    items = [
        CostItem("fee", "왕복 거래 수수료", f.round_trip_trade_fee_percent(coin),
                 f"업비트 USDT {f.upbit_trade_fee_rate * 100:.3f}%×2 + 코인 {f.upbit_trade_rate(coin) * 100:.3f}%×2 "
                 f"+ 바이비트 {f.bybit_taker_rate(coin) * 100:.3f}%×2"
                 + (" (이 코인은 바이비트 2배 요율)" if f.bybit_taker_rate(coin) > f.bybit_taker_fee_rate * 1.5 else "")),
        CostItem("coin_spread", "코인 호가 간격", spread,
                 f"{spread_source} — 사자마자 팔면 날아가는 폭"),
        CostItem("usdt_spread", "USDT 호가 간격",
                 usdt_spread if usdt_spread is not None else DEFAULT_USDT_SPREAD_PERCENT,
                 "기록 중간값" if usdt_spread is not None else f"기록 없음 → 기본 {DEFAULT_USDT_SPREAD_PERCENT}%"),
    ]
    transfer = f.transfer_fee_usdt_per_cycle / notional_usdt * 100 if notional_usdt > 0 else 0.0
    items.append(CostItem("transfer", "전송비", transfer,
                          f"{f.transfer_fee_usdt_per_cycle:g} USDT ÷ 포지션 {notional_usdt:,.0f} USDT"
                          if f.transfer_fee_usdt_per_cycle else "사이클당 전송비 설정 0"))
    # 펀딩비: 우리는 숏이라 비율이 + 면 받는다 → 비용이 마이너스(필요 폭이 줄어든다)
    if funding_8h is None:
        items.append(CostItem("funding", "펀딩비", 0.0, "기록 없음 → 0 으로 봄"))
    else:
        items.append(CostItem("funding", "펀딩비", -funding_8h * hold_hours / 8.0,
                              f"8시간 {funding_8h:+.4f}% × 예상 보유 {hold_hours:.1f}시간"
                              f" ({'내는 쪽' if funding_8h < 0 else '받는 쪽'})"))
    if exec_slippage is not None:
        items.append(CostItem("buffer", "체결 밀림 가정", exec_slippage.for_coin(coin), exec_slippage.source))
    else:
        items.append(CostItem("buffer", "체결 밀림 가정", f.slippage_buffer_percent,
                              "SLIPPAGE_BUFFER_PERCENT — 청산 때 판단 호가보다 불리하게 체결되는 몫"))
    return items


# ======================================================================
# 관리선 자리 찾기
# ======================================================================
@dataclass(frozen=True)
class Levels:
    """관리선 한 쌍과 그 기간에 몇 번 왕복했나"""

    lcl: float
    ucl: float
    round_trips: int  # 진입 → 청산까지 끝난 횟수
    entries: int  # 진입 조건을 만족한 횟수 (청산 못 한 것 포함)
    avg_hold_hours: Optional[float]
    still_holding: bool  # 기간 끝에 아직 들고 있었나

    def as_dict(self) -> Dict[str, Any]:
        """화면에 보낼 모양 (JSON)"""
        return {"lcl": self.lcl, "ucl": self.ucl, "round_trips": self.round_trips,
                "entries": self.entries, "avg_hold_hours": self.avg_hold_hours,
                "still_holding": self.still_holding}


def count_round_trips(points: Sequence[Tuple[int, float]], lcl: float, ucl: float) -> Levels:
    """
    이 관리선으로 그 기간을 지나가 보면 몇 번 왕복하나

    LCL 이하로 내려오면 진입, 그 뒤 UCL 이상으로 올라오면 청산.
    한 코인은 한 번에 포지션 하나만 (봇도 같은 코인을 겹쳐 잡지 않는다).
    """
    trips = entries = 0
    holding_since: Optional[int] = None
    holds: List[float] = []
    for ts, value in points:
        if holding_since is None:
            if value <= lcl:
                holding_since = ts
                entries += 1
        elif value >= ucl:
            trips += 1
            holds.append((ts - holding_since) / HOUR_MS)
            holding_since = None
    return Levels(lcl=lcl, ucl=ucl, round_trips=trips, entries=entries,
                  avg_hold_hours=(sum(holds) / len(holds)) if holds else None,
                  still_holding=holding_since is not None)


def best_levels(points: Sequence[Tuple[int, float]], gap: float,
                steps: int = SCAN_STEPS) -> Tuple[Optional[Levels], List[Levels]]:
    """
    폭(gap)을 유지한 채 LCL 을 아래에서 위로 훑어 가장 좋은 자리를 찾는다

    고르는 순서: 왕복 횟수 많은 것 → 평균 보유 시간 짧은 것 → LCL 이 낮은 것(보수적)
    평균 보유 시간을 두 번째로 보는 이유: 자리를 빨리 비우는 쪽이 다른 코인의 진입을 안 막는다.

    Returns:
        (가장 좋은 자리, 후보들 — 왕복 횟수가 1회 이상인 것만 좋은 순서로)
    """
    values = [v for _, v in points]
    if len(values) < MIN_POINTS:
        return None, []
    lo, hi = min(values), max(values)
    top = hi - gap  # LCL 이 이보다 높으면 UCL 이 기간 최고점보다 위에 있어 청산이 안 된다
    found: List[Levels] = []
    if top >= lo:
        step = (top - lo) / steps if steps > 0 and top > lo else 0.0
        seen = set()
        for i in range(steps + 1):
            lcl = round(lo + step * i, 4)
            if lcl in seen:
                continue
            seen.add(lcl)
            found.append(count_round_trips(points, lcl, lcl + gap))
    if not found:  # 폭이 기간 변동폭보다 커서 놓을 자리가 없다 → 그래도 한 쌍은 돌려준다
        return count_round_trips(points, lo, lo + gap), []

    def rank(x: Levels):
        """고르는 순서: 왕복 많은 것 → 평균 보유 짧은 것 → LCL 낮은 것

        보유 시간을 두 번째로 보는 이유: 자리를 빨리 비우는 쪽이 다른 코인의 진입을 안 막는다.
        보유 시간이 없으면(왕복 0회) 1e9 로 밀어서 맨 뒤로 보낸다.
        """
        return (-x.round_trips, x.avg_hold_hours if x.avg_hold_hours is not None else 1e9, x.lcl)

    found.sort(key=rank)
    return found[0], [x for x in found if x.round_trips > 0][:8]


# ======================================================================
# 한 덩어리로
# ======================================================================
def plan(settings: Settings, coin: str, series: Sequence[Dict[str, Any]], target_percent: float = 1.0,
         notional_usdt: Optional[float] = None, hours: Optional[float] = None, exec_slippage=None) -> Dict[str, Any]:
    """
    목표 순이익에 맞는 관리선 한 쌍과 그 근거

    Args:
        series: 김프 차트 데이터 (`webui.premium_history` 의 series). `mid` 를 쓴다
        target_percent: 포지션 금액 대비 남기고 싶은 순이익 (%)
        notional_usdt: 포지션 금액 (없으면 설정에서 정한다)
        hours: 기간 (월 환산 거래 수를 내는 데 쓴다. 없으면 데이터에서 구한다)
    """
    points = [(int(r["ts"]), float(r[PREMIUM_KEY])) for r in series
              if r.get(PREMIUM_KEY) is not None and r.get("ts") is not None]
    notional = float(notional_usdt) if notional_usdt else notional_for(settings, coin)
    span_hours = hours if hours else ((points[-1][0] - points[0][0]) / HOUR_MS if len(points) > 1 else 0.0)

    out: Dict[str, Any] = {
        "coin": coin, "target_percent": target_percent, "notional_usdt": notional,
        "premium_key": PREMIUM_KEY, "points": len(points), "hours": round(span_hours, 2),
    }
    if len(points) < MIN_POINTS:
        out["error"] = (f"점이 {len(points)}개뿐입니다 (최소 {MIN_POINTS}개). "
                        f"기간을 늘리거나 캔들을 먼저 받아 주세요")
        return out

    values = [v for _, v in points]
    out["window"] = {"min": min(values), "max": max(values), "swing": max(values) - min(values),
                     "mean": statistics.fmean(values), "last": values[-1]}

    # 1차: 펀딩비를 모르는 상태(보유 0시간)로 폭을 잡고 한 번 돌려 본다
    first_items = cost_items(settings, coin, series, notional, hold_hours=0.0, exec_slippage=exec_slippage)
    first_gap = target_percent + sum(i.percent for i in first_items)
    first, _ = best_levels(points, first_gap)

    # 2차: 1차에서 나온 평균 보유 시간으로 펀딩비를 넣어 다시 잡는다 (서로 물려 있어서 한 번 더 돈다)
    hold = (first.avg_hold_hours if first and first.avg_hold_hours else 0.0)
    items = cost_items(settings, coin, series, notional, hold_hours=hold, exec_slippage=exec_slippage)
    gap = target_percent + sum(i.percent for i in items)
    best, candidates = best_levels(points, gap)

    out["assumed_hold_hours"] = hold
    out["costs"] = [i.as_dict() for i in items]
    out["cost_percent"] = sum(i.percent for i in items)
    out["required_gap_percent"] = gap
    out["levels"] = best.as_dict() if best else None
    out["candidates"] = [c.as_dict() for c in candidates]
    if best and span_hours > 0:
        out["trips_per_month"] = best.round_trips / span_hours * 24 * 30
        out["profit_per_month_usdt"] = out["trips_per_month"] * notional * target_percent / 100
    if best and best.round_trips == 0:
        out["warning"] = (
            f"이 기간에는 목표 {target_percent:g}% 를 낼 수 있는 왕복이 한 번도 없었습니다. "
            f"필요한 폭 {gap:.3f}%p 인데 기간 중 김프 변동폭은 {out['window']['swing']:.3f}%p 입니다."
        )
    return out

"""
체결 밀림 가정 (백테스트·차트용) — "청산 때 판단 호가보다 얼마나 불리하게 체결되나" 를 비용으로 넣는다

왜 따로 있나
    실거래 익절 판단은 유동 여유(kimp/exec_buffer.py)가 **그 순간 호가판** 으로 계산한다. 하지만 추천(screen)·
    시뮬레이션(simulate)·김프 차트의 관리선(levels)은 과거 1분봉 위에서 도는데, 과거에는 호가판이 없다. 그래서
    거기서는 밀림을 "평균 얼마" 라는 가정값으로 비용에 넣을 수밖에 없다. 이 모듈은 그 가정값을 정한다:

        (없음)   → .env SLIPPAGE_BUFFER_PERCENT (기본 0.05)
        숫자     → 그 값 (%). 예) 0.15
        auto     → 실거래 체결 기록(fills.expected_price 대 avg_price)에서 코인별 청산 밀림 중앙값.
                   그 코인 기록이 MIN_CLOSES 건 미만이면 전체 중앙값, 그것도 없으면 설정값

    "실측" 은 다음 한 건을 맞히는 예측이 아니라 백테스트 비용의 평균 가정이다 — 그 자리에선 평균이 맞다.

밀림 계산
    청산은 다리가 셋: 업비트 코인 매도(sell) · 바이비트 숏 닫기(close_short) · 업비트 USDT 되사기(buy_usdt).
    다리마다 '우리에게 불리한 방향' 을 양수로 잰다 (팔 때는 싸게 팔린 만큼, 살 때는 비싸게 산 만큼).
    포지션 한 건의 밀림 = 세 다리 합 (각각 그 다리 가격 대비 %). 2026-10-24 실거래: 진입 다리는 거의 0,
    청산 다리가 −0.12 ~ −1.58% 로 거의 매번 불리했다 — 청산은 김프가 튀는 순간이라 호가가 얇고 빠르다.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import select

from kimp.db import Fill, Position, make_session_factory

EXIT_SIDES = ("sell", "close_short", "buy_usdt")   # 청산 다리
MIN_CLOSES = 3                                       # 코인별 중앙값을 믿는 최소 청산 건수


def leg_adverse_percent(side: str, expected: float, actual: float) -> float:
    """
    다리 하나의 밀림 (%). 우리에게 불리하면 양수

        파는 다리 (sell, sell_usdt, open_short): 체결가가 낮을수록 손해 → (기대 − 체결) ÷ 기대
        사는 다리 (buy, buy_usdt, close_short):  체결가가 높을수록 손해 → (체결 − 기대) ÷ 기대
    """
    if not expected:
        return 0.0
    selling = side in ("sell", "sell_usdt", "open_short")
    return ((expected - actual) if selling else (actual - expected)) / expected * 100


@dataclass
class Measured:
    """실거래 기록에서 잰 청산 밀림"""
    per_coin: Dict[str, float] = field(default_factory=dict)    # 코인 → 중앙값 (%). MIN_CLOSES 건 이상인 코인만
    counts: Dict[str, int] = field(default_factory=dict)        # 코인 → 청산 건수 (전부)
    overall: Optional[float] = None                             # 전체 중앙값 (%). 기록이 하나도 없으면 None
    total: int = 0


def measured_exit_slippage(db_path: str) -> Measured:
    """
    실거래 DB 의 체결 기록으로 코인별 청산 밀림 중앙값을 구한다

    세 청산 다리에 모두 판단 때 호가(expected_price)가 적힌 포지션만 센다 (그 기록은 2026-10-24 부터 쌓인다).
    DB 가 없거나 비어 있으면 빈 Measured.
    """
    out = Measured()
    try:
        sf = make_session_factory(db_path)
        with sf() as s:
            rows = s.execute(
                select(Fill.position_id, Position.coin, Fill.side, Fill.expected_price, Fill.avg_price)
                .join(Position, Fill.position_id == Position.id)
                .where(Fill.side.in_(EXIT_SIDES), Fill.expected_price.isnot(None), Fill.avg_price > 0)
            ).all()
    except Exception:
        return out
    by_pos: Dict[int, Dict[str, float]] = {}
    coin_of: Dict[int, str] = {}
    for pid, coin, side, expected, actual in rows:
        by_pos.setdefault(pid, {})[side] = leg_adverse_percent(side, expected, actual)
        coin_of[pid] = coin
    per_coin_values: Dict[str, List[float]] = {}
    for pid, legs in by_pos.items():
        if set(legs) != set(EXIT_SIDES):
            continue   # 세 다리가 다 있어야 '그 청산의 밀림' 이다
        per_coin_values.setdefault(coin_of[pid], []).append(sum(legs.values()))
    everything: List[float] = []
    for coin, vals in per_coin_values.items():
        out.counts[coin] = len(vals)
        everything.extend(vals)
        if len(vals) >= MIN_CLOSES:
            out.per_coin[coin] = statistics.median(vals)
    out.total = len(everything)
    out.overall = statistics.median(everything) if everything else None
    return out


@dataclass
class SlippageAssumption:
    """추천·시뮬·차트가 코인마다 비용에 넣을 체결 밀림 (%)"""
    default: float                              # 코인별 값이 없을 때
    per_coin: Dict[str, float] = field(default_factory=dict)
    source: str = ""                            # 어디서 온 값인지 (결과 머리말에 적는다)

    def for_coin(self, coin: str) -> float:
        return self.per_coin.get(coin, self.default)

    def describe(self) -> str:
        return f"체결 밀림 가정 {self.source}"


def resolve(option: Optional[str], setting_percent: float, live_db_path: str) -> SlippageAssumption:
    """
    옵션 문자열을 가정값으로. None → 설정값 / 숫자 → 그 값 / 'auto' → 실측

    Raises:
        ValueError: 숫자도 auto 도 아닐 때
    """
    text = (option or "").strip().lower()
    if not text:
        return SlippageAssumption(default=setting_percent, source=f"{setting_percent:g}% (SLIPPAGE_BUFFER_PERCENT)")
    if text != "auto":
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"--exec-slippage 는 숫자(%) 또는 auto 여야 합니다: {option!r}") from None
        if value < 0:
            raise ValueError("--exec-slippage 는 0 이상이어야 합니다")
        return SlippageAssumption(default=value, source=f"{value:g}% (입력값)")
    m = measured_exit_slippage(live_db_path)
    if m.overall is None:
        return SlippageAssumption(default=setting_percent,
                                  source=f"{setting_percent:g}% (실측 기록 없음 → SLIPPAGE_BUFFER_PERCENT)")
    coins = ", ".join(f"{c} {v:.2f}({m.counts[c]}건)" for c, v in sorted(m.per_coin.items()))
    source = (f"실측 중앙값 — {coins + ' · ' if coins else ''}그 외 코인 {m.overall:.2f}% "
              f"(전체 {m.total}건, 코인별 값은 {MIN_CLOSES}건 이상일 때)")
    return SlippageAssumption(default=m.overall, per_coin=dict(m.per_coin), source=source)

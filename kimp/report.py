"""
김프 기록 분석 리포트

봇이 돌면서 쌓은 김프 기록(premium_snapshots)을 읽어서 코인별로 정리해 보여준다.
진입 기준을 정할 때, 펀딩비를 전략에 어떻게 쓸지 정할 때 참고하는 용도다.

실행:
    python -m kimp.report                 # 최근 7일 (DB 는 .env 의 DB_PATH)
    python -m kimp.report --hours 24      # 최근 24시간
    python -m kimp.report --db data/kimp.db

보여주는 것 (코인마다)
------------------------------------------------------------------
1) 김프 수준: 평균 / 최저 / 최고, 테더 몫과 코인 몫 평균
2) 진입 김프 분포: "하위 5% / 25% / 50% 일 때 진입 김프가 몇 % 였나"
   → 예) 하위 5% 가 -0.60% 면, 기록된 시간 중 5% 정도는 진입 김프가 -0.60% 이하였다는 뜻
3) 호가 간격 비용: 진입 김프 - 청산 김프 평균 (바로 들어갔다 바로 나오면 이만큼 손해)
4) 기준값별 시간 비율: "진입 김프가 X% 이하였던 시간이 몇 %" + 그때 평균 펀딩비율
5) 김프 구간별 평균 펀딩비율: 역프가 깊을 때 펀딩비를 받는 편인지 내는 편인지
   → 우리는 숏이라 펀딩비율이 + 면 받고, - 면 낸다
"""
from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select

from kimp.db import PremiumSnapshot, make_session_factory

# 기준값별 시간 비율을 볼 진입 김프 기준들 (%)
DEFAULT_THRESHOLDS: Tuple[float, ...] = (0.5, 0.0, -0.5, -1.0, -2.0)

# 김프 구간 (진입 김프 기준, %). (아래 끝, 위 끝, 이름)
PREMIUM_BUCKETS: Tuple[Tuple[float, float, str], ...] = (
    (float("-inf"), -1.0, "-1% 미만"),
    (-1.0, -0.5, "-1 ~ -0.5%"),
    (-0.5, 0.0, "-0.5 ~ 0%"),
    (0.0, 0.5, "0 ~ +0.5%"),
    (0.5, 1.0, "+0.5 ~ +1%"),
    (1.0, float("inf"), "+1% 이상"),
)


@dataclass(frozen=True)
class Row:
    """분석에 쓰는 김프 기록 한 줄 (DB 와 상관없이 계산하려고 필요한 값만 뽑음)"""

    coin: str
    mid_premium: float
    entry_premium: float
    exit_premium: float
    tether_premium: Optional[float]
    coin_premium: Optional[float]
    funding_rate: Optional[float]


@dataclass
class BucketStat:
    """김프 구간 하나의 통계"""

    label: str
    count: int = 0
    avg_funding_rate: Optional[float] = None  # 소수 (0.0001 = 0.01%)


@dataclass
class ThresholdStat:
    """진입 기준 하나의 통계"""

    threshold: float
    time_ratio: float  # 진입 김프가 기준 이하였던 비율 (0~1)
    avg_funding_rate: Optional[float]


@dataclass
class CoinSummary:
    """코인 하나의 분석 결과"""

    coin: str
    count: int
    mid_avg: float
    mid_min: float
    mid_max: float
    tether_avg: Optional[float]
    coin_avg: Optional[float]
    entry_p5: float
    entry_p25: float
    entry_p50: float
    spread_cost_avg: float
    funding_avg: Optional[float]
    funding_negative_ratio: Optional[float]
    thresholds: List[ThresholdStat] = field(default_factory=list)
    buckets: List[BucketStat] = field(default_factory=list)


def _avg(values: Sequence[float]) -> Optional[float]:
    """평균 (값이 없으면 None)"""
    return statistics.fmean(values) if values else None


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    """
    하위 p% 지점 값 (sorted_values 는 오름차순 정렬돼 있어야 함)

    예) [1, 2, 3, 4, 5] 의 하위 50% → 3
    """
    if not sorted_values:
        raise ValueError("값이 없습니다")
    k = (len(sorted_values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def summarize_coin(coin: str, rows: Sequence[Row],
                   thresholds: Sequence[float] = DEFAULT_THRESHOLDS) -> CoinSummary:
    """
    코인 하나의 기록을 정리한다

    Args:
        coin: 코인 이름
        rows: 그 코인의 기록들 (1개 이상)
        thresholds: 시간 비율을 볼 진입 기준들 (%)
    """
    mids = [r.mid_premium for r in rows]
    entries = sorted(r.entry_premium for r in rows)
    fundings = [r.funding_rate for r in rows if r.funding_rate is not None]

    threshold_stats = []
    for t in thresholds:
        hit = [r for r in rows if r.entry_premium <= t]
        threshold_stats.append(ThresholdStat(
            threshold=t,
            time_ratio=len(hit) / len(rows),
            avg_funding_rate=_avg([r.funding_rate for r in hit if r.funding_rate is not None]),
        ))

    bucket_stats = []
    for lo, hi, label in PREMIUM_BUCKETS:
        inside = [r for r in rows if lo <= r.entry_premium < hi]
        bucket_stats.append(BucketStat(
            label=label,
            count=len(inside),
            avg_funding_rate=_avg([r.funding_rate for r in inside if r.funding_rate is not None]),
        ))

    return CoinSummary(
        coin=coin,
        count=len(rows),
        mid_avg=statistics.fmean(mids),
        mid_min=min(mids),
        mid_max=max(mids),
        tether_avg=_avg([r.tether_premium for r in rows if r.tether_premium is not None]),
        coin_avg=_avg([r.coin_premium for r in rows if r.coin_premium is not None]),
        entry_p5=_percentile(entries, 5),
        entry_p25=_percentile(entries, 25),
        entry_p50=_percentile(entries, 50),
        spread_cost_avg=statistics.fmean(r.entry_premium - r.exit_premium for r in rows),
        funding_avg=_avg(fundings),
        funding_negative_ratio=(sum(1 for f in fundings if f < 0) / len(fundings)) if fundings else None,
        thresholds=threshold_stats,
        buckets=bucket_stats,
    )


def load_rows(db_path: str, hours: float) -> Tuple[Dict[str, List[Row]], Optional[datetime], Optional[datetime]]:
    """
    DB 에서 최근 hours 시간의 기록을 코인별로 읽는다

    Returns:
        ({코인: [Row...]}, 가장 이른 기록 시각, 가장 늦은 기록 시각)
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    session_factory = make_session_factory(db_path)
    by_coin: Dict[str, List[Row]] = {}
    first: Optional[datetime] = None
    last: Optional[datetime] = None
    with session_factory() as session:
        # SQLite 는 시간대 정보 없이 저장하므로 비교도 시간대 없는 UTC 로 한다
        stmt = select(PremiumSnapshot).where(PremiumSnapshot.created_at >= since.replace(tzinfo=None))
        for s in session.scalars(stmt.order_by(PremiumSnapshot.created_at)):
            by_coin.setdefault(s.coin, []).append(Row(
                coin=s.coin, mid_premium=s.mid_premium, entry_premium=s.entry_premium_top,
                exit_premium=s.exit_premium_top, tether_premium=s.tether_premium,
                coin_premium=s.coin_premium, funding_rate=s.funding_rate,
            ))
            first = first or s.created_at
            last = s.created_at
    return by_coin, first, last


def _pct(value: Optional[float], digits: int = 2) -> str:
    """퍼센트 값을 글자로 (+0.12%)"""
    return "-" if value is None else f"{value:+.{digits}f}%"


def _rate(value: Optional[float]) -> str:
    """펀딩비율(소수)을 % 글자로 (0.0001 → +0.0100%)"""
    return "-" if value is None else f"{value * 100:+.4f}%"


def format_summary(s: CoinSummary) -> str:
    """코인 하나의 결과를 읽기 쉬운 글로 만든다"""
    lines = [
        f"■ {s.coin}  (기록 {s.count:,}건)",
        f"  김프(가운데 가격): 평균 {_pct(s.mid_avg)}  최저 {_pct(s.mid_min)}  최고 {_pct(s.mid_max)}",
        f"    └ 평균 테더 몫 {_pct(s.tether_avg)} / 코인 몫 {_pct(s.coin_avg)}",
        f"  진입 김프 분포: 하위 5% {_pct(s.entry_p5)} / 하위 25% {_pct(s.entry_p25)} / 중간값 {_pct(s.entry_p50)}",
        f"  호가 간격 비용(진입 김프 - 청산 김프): 평균 {s.spread_cost_avg:.3f}%p",
        f"  펀딩비율: 평균 {_rate(s.funding_avg)}  (마이너스 = 숏이 냄, 비율 "
        f"{'-' if s.funding_negative_ratio is None else f'{s.funding_negative_ratio * 100:.0f}%'})",
        "  진입 기준별: 진입 김프가 기준 이하였던 시간 / 그때 평균 펀딩비율",
    ]
    for t in s.thresholds:
        lines.append(f"    기준 {t.threshold:+.1f}% 이하: {t.time_ratio * 100:5.1f}% 의 시간 / 펀딩 {_rate(t.avg_funding_rate)}")
    lines.append("  김프 구간별 평균 펀딩비율 (역프가 깊을 때 펀딩비를 받나 내나):")
    for b in s.buckets:
        if b.count:
            lines.append(f"    {b.label:>11}: {b.count:6,}건 / 펀딩 {_rate(b.avg_funding_rate)}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """명령줄 실행 (python -m kimp.report)"""
    from kimp.config import load_settings

    parser = argparse.ArgumentParser(description="김프 기록 분석 리포트")
    parser.add_argument("--hours", type=float, default=24 * 7, help="최근 몇 시간 (기본 168 = 7일)")
    parser.add_argument("--db", default=None, help="DB 파일 (기본: .env 의 DB_PATH)")
    args = parser.parse_args(argv)

    db_path = args.db or load_settings().db_path
    by_coin, first, last = load_rows(db_path, args.hours)
    if not by_coin:
        print(f"최근 {args.hours:g}시간 동안 기록이 없습니다. (DB: {db_path})")
        print("봇을 돌리면 코인마다 1분에 한 번씩 기록이 쌓입니다.")
        return 0

    # 시간은 UTC 로 저장돼 있으니 한국 시간(+9)으로 보여준다
    kst = timedelta(hours=9)
    print(f"김프 기록 분석 (DB: {db_path})")
    print(f"기간: {(first + kst):%Y-%m-%d %H:%M} ~ {(last + kst):%Y-%m-%d %H:%M} (한국 시간)")
    print("김프 = 달러 환율 기준 (테더 포함). 우리는 숏이라 펀딩비율 + 면 받고, - 면 낸다.")
    print("주의: 기록 기간이 짧으면 결과가 치우칠 수 있다. 최소 며칠은 쌓인 뒤에 판단할 것.\n")
    for coin in sorted(by_coin):
        print(format_summary(summarize_coin(coin, by_coin[coin])))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

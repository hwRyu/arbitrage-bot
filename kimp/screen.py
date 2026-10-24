"""
추천 코인 순위 (코인 스크리너)

업비트 원화 마켓과 바이비트 USDT 무기한에 둘 다 있는 코인을 전부 훑어서,
"우리 전략(역프일 때 사고 김프가 오르면 판다)이 잘 통했을 코인" 순서로 보여준다.

실행:
    python3 -m kimp.screen                      # 최근 30일, 15분봉, 상위 20개
    python3 -m kimp.screen --days 14 --top 30
    python3 -m kimp.screen --coins XRP,SOL,DOGE # 정해둔 코인만 비교
    python3 -m kimp.screen --target 0.5         # 목표 순이익을 바꿔서 보기
    python3 -m kimp.screen --end 2026-09-22     # 9/22 까지 30일로 고르기 (그다음 기간에 시뮬레이션해서 검증)

어떻게 고르나 (코인마다)
------------------------------------------------------------------
1) 최근 N일 15분봉으로 '봇 김프'(코인 몫, 업비트 USDT 기준) 기록을 만든다
     김프 = 업비트 코인 원화 가격 ÷ (바이비트 가격 × 업비트 USDT 원화 가격) − 1
2) 김프가 어떻게 움직였나
   - 중간값, 흔들림(표준편차), 평소 범위(하위 5% ~ 상위 5%)
   - 횡보 비율: 김프가 중간값 ±0.3% 안에 머문 시간 비율 (높을수록 얌전함)
   - 추세: 앞 1/4 기간 중간값 → 뒤 1/4 기간 중간값이 얼마나 옮겨갔나 (클수록 불안정)
   - 되돌아오는 속도(반감기): 김프가 평소보다 벌어졌을 때 그 차이가 절반으로 줄어드는 데 걸린 시간
     (호가 튐을 빼려고 1시간 평균 김프로 계산)
3) 지금 호가창 / 24시간 거래량 / 평균 펀딩비율
   - 호가 간격 = 업비트 코인 + 바이비트 (실시간 값과 '호가 한 칸' 중 큰 값)
   - 한 바퀴 비용 = 거래 수수료 + 전송비 + (코인 호가 간격 + USDT 호가 간격) × 2
4) 그 코인 하나로 진입 기준을 여러 개 넣어 '스윙 백테스트'를 돌려서 가장 잘 된 기준을 찾는다
   - 진입: 진입 김프(체결가 기준) ≤ 기준
   - 청산: 순이익 ≥ 목표 / 손절: 순이익 ≤ 손절
   - 펀딩비 포함 (우리는 숏: 펀딩비율 + 면 받고, − 면 낸다)
5) 점수 = 월 환산 수익률 (포지션 금액 대비 %)
   너무 '지난 기간에만 맞춘' 기준을 막으려고,
     · 전체 기간에 가장 좋은 기준의 월 환산 수익
     · 앞 2/3 기간에서 고른 기준을 뒤 1/3 기간에 그대로 써본 월 환산 수익 (검증)
   둘 중 '작은 값'을 기본 점수로 쓴다. 예) 전체 +3.0%/월, 검증 +1.2%/월 → 기본 점수 1.2
6) 우리 전략에 맞는 코인 = '일정한 띠 안에서 김프가 자주 오르내리는 코인'. 그래서 기본 점수에 두 가중치를 곱한다
   - 반복 주기 가중치: 주기 = 기간 ÷ 왕복(진입→청산) 횟수
       주기 ≤ 24시간 → 1.0 / 48시간 → 0.5 / 1주(168시간) → 0.14  (= 24 ÷ 주기)
       예) 30일에 3번 왕복 → 주기 240시간 → 0.1 (기회가 너무 드묾)
   - 띠 가중치: 기간을 4등분한 구간별 김프 중간값이 얼마나 옮겨 다녔나 (띠 이동폭 = 최고 − 최저)
       0.3%p 이하 → 1.0 / 0.9%p → 0.5 / 1.5%p 이상 → 0  (띠가 흔들리면 진입 기준이 금방 안 맞게 됨)
   점수 = 기본 점수 ÷ 자리 점유율 × 띠 가중치  예) 기본 4.0 ÷ 점유율 0.5 × 띠 0.8 → 6.4
   - 김프가 1시간 평균으로 ±5% 를 넘은 적이 있으면 '이상김프' (입출금 막힘 등) → 순위 맨 아래로
     예) LSK 2026-09-22~10-02 김프 −5% ~ −31% → 들어가자마자 손절만 반복 (한 달 −64 USDT)

주의
------------------------------------------------------------------
- 15분봉 종가로 보기 때문에 15분 안에 잠깐 열렸다 닫힌 기회는 못 본다 (실제보다 기회가 적게 보임)
- 종가는 매수/매도 호가 사이를 왔다 갔다 해서 가짜 흔들림이 섞인다. 백테스트는 '김프가 낮게 찍힌 봉' 에
  사고 '높게 찍힌 봉' 에 팔기 때문에, 하필 종가가 매수 호가에 찍혀 낮아 보인 봉에 들어가기 쉽다.
  그래서 넉넉하게 진입 때 호가 간격 한 번, 청산 때 한 번 (한 바퀴에 두 번) 손해 보는 것으로 계산한다.
  예) 호가 간격 0.9% 코인 → 한 바퀴에 1.8% 를 비용으로 잡음 (호가 넓은 코인이 위로 올라오는 착시 방지)
- 호가 간격은 '지금' 호가창 값이다. 과거에는 더 넓었을 수도 있다.
- 지나간 기간 결과다. 앞으로도 그렇다는 보장은 없다. 고른 코인은 시뮬레이션(python3 -m kimp.simulate)으로
  1분봉 기준으로 한 번 더 확인하고 쓰는 것을 권한다.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from kimp.config import Settings, load_settings
from kimp.history import (
    BYBIT,
    UPBIT,
    UPBIT_USDT,
    HistoryStore,
    ccxt_candle_fetcher,
    ccxt_funding_fetcher,
    make_history_session_factory,
)
from kimp.market_data import USDT_KRW_SYMBOL, bybit_symbol, upbit_symbol
from kimp.simulate import tick_floor_percent, usdt_tick_percent
from kimp.spread import (LIVE_SAMPLES, MODE_AUTO, MODE_COMMON, MODE_LIVE, MODE_RECORDS, MODES,
                         is_rough, median_spread_depth, resolve_spreads)

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000

# 캔들 간격 이름 → 밀리초
TIMEFRAME_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}

# 2단계(정밀 재평가) 기본값: 1단계에서 고른 상위 몇 개를, 어떤 봉으로 다시 볼지
# 1분봉이 시뮬레이션과 같은 기준이다. 15분봉으로만 보면 짧게 내려갔다 올라온 기회를 못 봐서
# 스크리너 순위와 시뮬레이션 결과가 어긋난다 (CAP: 15분봉 9건 vs 1분봉 80건)
REFINE_TOP = 20
REFINE_TIMEFRAME = "1m"

# 스테이블코인은 김프 헷지 대상이 아님 (가격이 안 움직이니 기회도 없음)
STABLECOINS = {"USDT", "USDC", "DAI", "USDE", "FDUSD", "TUSD", "USD1", "PYUSD", "USDS", "BUSD"}

# 횡보 판정 폭: 김프가 중간값 ± 이 값(%p) 안이면 '횡보' 로 본다
SIDEWAYS_BAND_PERCENT = 0.3

# 진입 기준 후보: 진입 김프 기록의 하위 몇 % 지점들
#   예) 0.05 → "기록된 시간 중 5% 정도만 이보다 낮았던 값" 을 기준으로 써본다
THRESHOLD_QUANTILES = (0.01, 0.02, 0.03, 0.05, 0.07, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50)

# 경고 표시 기준
WIDE_SPREAD_PERCENT = 0.3  # 호가 간격(코인)이 이보다 넓으면 '호가 넓음'
TREND_WARN_PERCENT = 1.0  # 김프 중간값이 기간 동안 이만큼(%p) 넘게 옮겨가면 '추세'
SLOW_REVERT_HOURS = 48.0  # 반감기가 이보다 길면 '회귀 느림'
MIN_COVERAGE = 0.8  # 캔들이 기간의 80% 미만이면 '데이터 부족' (신규 상장, 점검 등)
ABNORMAL_PREMIUM_PERCENT = 5.0  # 1시간 평균 김프가 ±5% 를 넘은 적이 있으면 '이상김프'

# 반복 주기 가중치: 주기가 이 시간 이하면 1.0, 길어지면 (이 시간 ÷ 주기)
CYCLE_GOOD_HOURS = 24.0

# 자리 점유율 하한. 거래가 1~2건뿐인 코인이 '자리를 거의 안 쓴다' 는 이유로
# 점수가 부풀지 않게 막는다. 0.25 = 자리를 4분의 1 은 쓴다고 보고 계산
MIN_OCCUPANCY = 0.25
# 검증 기간에 완결된 거래가 아예 없을 때 전체 점수에 곱할 값 (0 으로 뭉개지 않고 깎기만 한다)
RECENT_FAIL_DISCOUNT = 0.25

# 추천 COINS 목록을 만들 때 쓸 자리 예산 비율.
# 자리 3개 × 0.8 = 2.4 까지만 채운다 (꽉 채우면 빠른 코인이 들어갈 틈이 없다)
SLOT_BUDGET_RATIO = 0.8
# 띠 가중치: 구간별 중간값 이동폭이 OK 이하면 1.0, ZERO 이상이면 0, 사이는 직선
BAND_SHIFT_OK_PERCENT = 0.3
BAND_SHIFT_ZERO_PERCENT = 1.5


# ======================================================================
# 김프 기록 만들기와 통계 (순수 계산)
# ======================================================================
def premium_series(upbit: Dict[int, float], bybit: Dict[int, float],
                   usdt: Dict[int, float]) -> List[Tuple[int, float]]:
    """
    같은 시각의 세 가격으로 봇 김프(%) 기록 만들기. 셋 중 하나라도 빠진 시각은 건너뛴다.

    예) 업비트 XRP 3,300원, 바이비트 2.50 USDT, 업비트 USDT 1,320원
        → 3,300 ÷ (2.50 × 1,320) − 1 = 0% / 업비트가 3,267원이면 −1.0%
    """
    out = []
    for ts in sorted(upbit.keys() & bybit.keys() & usdt.keys()):
        b, u = bybit[ts], usdt[ts]
        if b > 0 and u > 0:
            out.append((ts, (upbit[ts] / (b * u) - 1) * 100))
    return out


def quantile(values: Sequence[float], q: float) -> float:
    """정렬해서 q 지점 값. 예) [1,2,3,4,5] 의 0.5 → 3, 0.0 → 1"""
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(q * (len(s) - 1) + 0.5)))]


def half_life_hours(values: Sequence[float], bar_ms: int) -> Optional[float]:
    """
    김프가 평소에서 벌어졌을 때, 그 차이가 절반으로 줄어드는 데 걸리는 시간 (시간 단위)

    방법: 평균과의 차이 x 가 다음 봉에 φ 배로 남는다고 보고 (x[t] ≈ φ × x[t−1]) φ 를 구한다.
      반감기(봉 개수) = ln(0.5) ÷ ln(φ)
    예) 15분봉에서 φ = 0.9 → 0.693 ÷ 0.105 ≈ 6.6봉 ≈ 1.6시간 (빨리 돌아옴 = 좋음)
        φ = 0.999 → 693봉 ≈ 173시간 (거의 안 돌아옴)
        φ ≥ 1 → 안 돌아옴 (None)
    """
    if len(values) < 10:
        return None
    m = statistics.fmean(values)
    x = [v - m for v in values]
    num = sum(a * b for a, b in zip(x[1:], x[:-1]))
    den = sum(a * a for a in x[:-1])
    if den <= 0:
        return None
    phi = num / den
    if phi <= 0:
        return bar_ms / 3_600_000  # 한 봉 안에 바로 돌아옴
    if phi >= 1:
        return None
    return math.log(0.5) / math.log(phi) * bar_ms / 3_600_000


@dataclass
class PremiumStats:
    """
    김프 기록 통계 (단위: %)

    Attributes:
        median: 중간값. 예) -0.10 → 평소 0.1% 역프
        std: 흔들림 정도 (표준편차)
        p5, p95: 하위 5% / 상위 5% 값 → 평소 움직이는 범위
        sideways_share: 중간값 ±0.3% 안에 있던 시간 비율 (0~1)
        drift: 뒤 1/4 기간 중간값 − 앞 1/4 기간 중간값 (%p). 예) +0.8 → 김프가 점점 올라감
        half_life_h: 되돌아오는 속도 (시간). None = 안 돌아옴
        band_shift: 기간을 4등분한 구간별 중간값의 최고 − 최저 (%p). 작을수록 같은 띠에서 횡보
                    예) 구간 중간값 -0.1, 0.0, +0.1, -0.05 → 0.2%p (얌전) / -0.2, +0.9, ... → 1.1%p
        max_abs: 1시간 평균 김프의 절댓값 최대 (%). 5 넘으면 이상김프
    """

    median: float
    std: float
    p5: float
    p95: float
    sideways_share: float
    drift: float
    half_life_h: Optional[float]
    band_shift: float = 0.0
    max_abs: float = 0.0

    @property
    def range_width(self) -> float:
        """평소 움직이는 폭 = 상위 5% − 하위 5%. 예) -0.8 ~ +0.6 → 1.4%p (넓을수록 기회가 큼)"""
        return self.p95 - self.p5


def premium_stats(values: Sequence[float], bar_ms: int) -> PremiumStats:
    med = statistics.median(values)
    quarter = max(1, len(values) // 4)
    parts = [statistics.median(values[i * quarter:(i + 1) * quarter]) for i in range(4)
             if values[i * quarter:(i + 1) * quarter]]
    hourly = hourly_means(values, bar_ms) or list(values)
    return PremiumStats(
        median=med,
        std=statistics.pstdev(values),
        p5=quantile(values, 0.05),
        p95=quantile(values, 0.95),
        sideways_share=sum(1 for v in values if abs(v - med) <= SIDEWAYS_BAND_PERCENT) / len(values),
        drift=statistics.median(values[-quarter:]) - statistics.median(values[:quarter]),
        half_life_h=half_life_hours(hourly, 3_600_000),
        band_shift=max(parts) - min(parts),
        max_abs=max(abs(v) for v in hourly),
    )


def hourly_means(values: Sequence[float], bar_ms: int) -> List[float]:
    """
    1시간씩 묶어 평균 (15분봉이면 4개씩). 종가가 호가 사이를 튀는 잡음을 줄이려고 쓴다.

    예) 15분봉 김프 -0.5, +0.4, -0.5, +0.4 (호가 튐) → 1시간 평균 -0.05
    """
    n = max(1, 3_600_000 // bar_ms)
    return [statistics.fmean(values[i:i + n]) for i in range(0, len(values) - n + 1, n)]


# ======================================================================
# 스윙 백테스트 (순수 계산)
# ======================================================================
@dataclass
class SwingCosts:
    """
    백테스트에 쓰는 비용 (모두 포지션 금액 대비 %)

    Attributes:
        leg_spread: 진입 한 번 / 청산 한 번마다 손해 보는 호가 간격 (코인 + USDT).
                    종가가 매수·매도 호가 중 어디에 찍혔는지 몰라서 넉넉하게 '호가 간격 한 번' 으로 잡는다.
                    예) 코인 0.10% + USDT 0.075% → 0.175 (한 바퀴에 0.35)
        fixed: 거래 수수료 + 전송비. 예) 수수료 0.31% + 전송비 1 USDT / 1,000 USDT = 0.41
        target: 목표 순이익 (청산 조건). 예) 1.0
        stop: 손절 (음수, 0 = 사용 안 함). 예) -5.0
    """

    leg_spread: float
    fixed: float
    target: float
    stop: float


@dataclass
class SwingResult:
    """
    한 코인 · 한 진입 기준으로 돌린 결과 (모두 포지션 금액 대비 %)

    Attributes:
        threshold: 진입 기준 (진입 김프가 이 값 이하이면 진입)
        trades: 닫은 거래 수 / wins: 그중 이익 / net_closed: 닫은 거래 순손익 합
        open_net: 끝까지 들고 있는 포지션을 마지막에 판다면 순손익 (없으면 0)
        holding_hours: 닫은 거래 보유 시간들 / worst: 들고 있는 동안 가장 나빴던 순손익
        funding: 받은(+)/낸(−) 펀딩비 합
    """

    threshold: float
    trades: int = 0
    wins: int = 0
    net_closed: float = 0.0
    open_net: float = 0.0
    is_open_at_end: bool = False
    holding_hours: List[float] = field(default_factory=list)
    worst: float = 0.0
    funding: float = 0.0

    @property
    def total(self) -> float:
        return self.net_closed + self.open_net


def run_swing(series: Sequence[Tuple[int, float]], threshold: float, costs: SwingCosts,
              funding: Sequence[Tuple[int, float]] = ()) -> SwingResult:
    """
    김프 기록을 따라가며 '한 번에 포지션 하나' 로 진입/청산을 반복해 본다

    순손익(%) ≈ (청산 김프 − 진입 김프) − 수수료·전송비 + 펀딩비
      진입 김프 = 종가 김프 + 호가 간격 / 청산 김프 = 종가 김프 − 호가 간격
    예) 기준 -1.0, 호가 간격 0.1, 수수료+전송비 0.41, 목표 1.0
        종가 김프 -1.2 → 진입 김프 -1.1 ≤ -1.0 → 진입
        필요한 청산 김프 = -1.1 + 0.41 + 1.0 = +0.31 → 종가 김프 +0.41 이 되면 청산
    """
    res = SwingResult(threshold=threshold)
    fi = 0  # 다음에 볼 펀딩비 정산 위치
    entry: Optional[Tuple[int, float]] = None  # (진입 시각, 진입 김프)
    fund = 0.0
    net = 0.0
    for ts, p in series:
        # 이번 봉까지 지나간 펀딩비 정산 반영 (들고 있을 때만)
        # fi 를 앞으로만 움직여서 펀딩비를 두 번 세지 않는다 (봉마다 처음부터 훑으면 O(n²)).
        # '들고 있을 때 + 진입 시각 이후' 정산만 더한다 — 진입 전 정산은 우리 돈이 아니다.
        while fi < len(funding) and funding[fi][0] <= ts:
            if entry is not None and funding[fi][0] > entry[0]:
                fund += funding[fi][1] * 100  # 비율 0.0001 → 0.01%
            fi += 1
        if entry is None:
            # 진입 판단은 **종가에 호가 간격을 더한 값**으로 한다 (살 때는 불리한 쪽이므로).
            # 종가 그대로 판단하면 실제로는 못 들어갈 자리에 들어간 것으로 계산돼 가짜 수익이 난다.
            if p + costs.leg_spread <= threshold:
                entry, fund = (ts, p + costs.leg_spread), 0.0
                net = -2 * costs.leg_spread - costs.fixed  # 바로 판다면 (호가 간격 + 수수료만큼 손해)
            continue
        # 평가 순손익: 청산은 종가에서 호가 간격을 **빼고**(팔 때도 불리한 쪽), 진입 김프를 빼고,
        # 수수료·전송비를 빼고, 그동안 받은(낸) 펀딩비를 더한다.
        # 호가 간격이 진입·청산에 한 번씩 들어가므로 왕복으로 2배가 비용이 된다.
        net = (p - costs.leg_spread) - entry[1] - costs.fixed + fund
        res.worst = min(res.worst, net)
        hit_target = net >= costs.target
        hit_stop = costs.stop < 0 and net <= costs.stop
        if hit_target or hit_stop:
            res.trades += 1
            res.wins += 1 if net > 0 else 0
            res.net_closed += net
            res.funding += fund
            res.holding_hours.append((ts - entry[0]) / 3_600_000)
            entry = None
    # 기간이 끝났는데 아직 들고 있으면 '닫은 거래' 로 세지 않고 따로 표시한다.
    # 닫지 못한 것을 수익으로 세면, 끝까지 물려 있던 코인이 좋아 보이게 된다 (물림 경고).
    if entry is not None:
        res.is_open_at_end = True
        res.open_net = net
        res.funding += fund
    return res


def threshold_candidates(series: Sequence[Tuple[int, float]], leg_spread: float) -> List[float]:
    """
    진입 기준 후보 = 진입 김프 기록의 하위 1%, 2%, ..., 50% 지점 (0.05% 단위로 반올림, 중복 제거)

    예) 진입 김프가 하위 5% 일 때 -0.83% → 후보 -0.85
    """
    # 기준 후보를 '그 코인이 실제로 지나간 김프 분포' 에서 뽑는다. 고정 격자(-0.1, -0.2 …)로
    # 훑으면 김프가 늘 +0.2 근처인 코인은 후보가 전부 통과하거나 전부 탈락해 비교가 안 된다.
    # 0.05% 단위로 둥글리는 이유: 소수점까지 맞춘 기준은 그 기간에만 맞는 과최적화다.
    entries = [p + leg_spread for _, p in series]
    return sorted({round(round(quantile(entries, q) / 0.05) * 0.05, 2) for q in THRESHOLD_QUANTILES})


def best_swing(series: Sequence[Tuple[int, float]], costs: SwingCosts,
               funding: Sequence[Tuple[int, float]] = (),
               candidates: Optional[Sequence[float]] = None) -> Optional[SwingResult]:
    """
    후보 기준을 다 돌려보고 가장 좋은 결과 (총 순손익이 가장 큰 것, 같으면 거래가 많은 것)

    닫은 거래가 하나도 없으면 고르지 않는다. (들고만 있다가 끝난 건 '통했다' 고 보기 어렵다)
    """
    if candidates is None:
        candidates = threshold_candidates(series, costs.leg_spread)
    best: Optional[SwingResult] = None
    for t in candidates:
        r = run_swing(series, t, costs, funding)
        if r.trades == 0:
            continue  # 한 번도 못 닫은 기준은 '통했다' 고 볼 수 없다
        # 총손익이 같으면 거래가 많은 쪽을 고른다 — 한 번 크게 번 것보다 여러 번 반복된 쪽이
        # 다음 기간에도 재현될 가능성이 높다.
        if best is None or (r.total, r.trades) > (best.total, best.trades):
            best = r
    return best


def per_month(percent: float, days: float) -> float:
    """기간 수익률을 30일 기준으로 바꾸기. 예) 14일 동안 +0.7% → +1.5%/월"""
    return percent * 30 / days if days > 0 else 0.0


# ======================================================================
# 코인 하나 평가
# ======================================================================
@dataclass
class CoinLive:
    """
    지금 시점 시장 정보

    Attributes:
        upbit_krw_24h: 업비트 24시간 거래대금 (원)
        bybit_usdt_24h: 바이비트 24시간 거래대금 (USDT)
        spread_percent: 지금 호가 간격 (업비트 코인 + 바이비트, %). 모르면 None
        depth_usdt: 맨 앞 호가에서 0.3% 안쪽에 쌓인 물량 (업비트 매도벽 / 바이비트 매수벽 중 작은 쪽, USDT)
    """

    upbit_krw_24h: float = 0.0
    bybit_usdt_24h: float = 0.0
    spread_percent: Optional[float] = None
    depth_usdt: Optional[float] = None


def cycle_weight(cycle_hours: Optional[float], good_hours: float = CYCLE_GOOD_HOURS) -> float:
    """
    반복 주기 가중치 = min(1, 24시간 ÷ 주기). 왕복이 없으면 0

    예) 주기 12시간 → 1.0 / 48시간 → 0.5 / 240시간(30일에 3번) → 0.1

    지금은 점수에 쓰지 않는다 (자리 점유율로 대체). 표에 주기를 보여줄 때만 참고.
    """
    if not cycle_hours:
        return 0.0
    return min(1.0, good_hours / cycle_hours)


def slot_efficiency(monthly_percent: float, occupancy: float) -> float:
    """
    자리당 효율 = 월 수익 ÷ 자리 점유율 (포지션 자리 하나를 한 달 내내 썼다면 얼마 벌었나)

    왜 필요한가: 동시에 들 수 있는 포지션은 MAX_OPEN_POSITIONS(기본 3)개뿐이다.
    느린 코인은 자리를 며칠씩 차지해서, 몇 시간마다 기회가 오는 코인의 진입을 막는다.
    코인을 하나씩 따로 보면 이 손실이 안 보인다.
      예) 자리를 80% 쓰면서 월 +6% 인 코인 → 6 ÷ 0.8 = 7.5
          자리를 20% 쓰면서 월 +4% 인 코인 → 4 ÷ 0.25(하한) = 16.0  ← 남는 자리에 다른 코인을 넣을 수 있어 더 좋다

    점유율이 아주 작은 코인(거래 1~2건)이 과대평가되지 않도록 하한 MIN_OCCUPANCY 를 둔다.
    """
    return monthly_percent / max(occupancy, MIN_OCCUPANCY)


def band_weight(band_shift: float) -> float:
    """
    띠 가중치: 구간별 중간값 이동폭 0.3%p 이하 → 1.0, 1.5%p 이상 → 0, 사이는 직선

    예) 0.3 → 1.0 / 0.9 → 0.5 / 1.2 → 0.25
    """
    span = BAND_SHIFT_ZERO_PERCENT - BAND_SHIFT_OK_PERCENT
    return max(0.0, min(1.0, 1 - (band_shift - BAND_SHIFT_OK_PERCENT) / span))


@dataclass
class CoinScore:
    """코인 하나의 평가 결과 (표 한 줄)"""

    coin: str
    stats: PremiumStats
    live: CoinLive
    spread_percent: float  # 계산에 쓴 코인 호가 간격 (실시간과 호가 한 칸 중 큰 값)
    cost_percent: float  # 한 바퀴 비용 = 수수료 + 전송비 + (코인 호가 + USDT 호가) × 2
    funding_8h: float  # 평균 펀딩비율 (8시간 기준 %로 환산). + 면 숏이 받음
    coverage: float  # 기간 중 캔들이 있는 비율
    full: Optional[SwingResult]  # 전체 기간 가장 좋은 기준 결과
    test: Optional[SwingResult]  # 앞 2/3 에서 고른 기준을 뒤 1/3 에 쓴 결과
    days: float
    test_days: float
    flags: List[str] = field(default_factory=list)
    cycle_good_hours: float = CYCLE_GOOD_HOURS
    timeframe: str = ""  # 이 점수를 어떤 봉으로 계산했는지 (2단계에서 1분봉으로 다시 본 코인 구분)

    @property
    def abnormal(self) -> bool:
        """이상김프 (입출금 막힘 등으로 김프가 ±5% 넘게 벌어진 적 있음)"""
        return self.stats.max_abs > ABNORMAL_PREMIUM_PERCENT

    @property
    def cycle_hours(self) -> Optional[float]:
        """반복 주기 = 기간 ÷ 왕복(닫은 거래) 횟수 (시간). 예) 30일에 15번 → 48시간"""
        if not self.full or self.full.trades == 0:
            return None
        return self.days * 24 / self.full.trades

    @property
    def occupancy(self) -> float:
        """
        자리 점유율 = 포지션을 들고 있던 시간 ÷ 전체 기간 (0~1)

        예) 30일(720시간) 중 거래 14건 × 평균 36.7시간 = 514시간 들고 있었으면 0.71
        한 번에 포지션 1개로 돌린 결과라 1 을 넘지 않는다.
        """
        if not self.full or self.days <= 0:
            return 0.0
        held = sum(self.full.holding_hours)
        return min(1.0, held / (self.days * 24))

    @property
    def weight(self) -> float:
        """띠 가중치 (0~1). 같은 띠 안에서 움직이는 코인만 인정한다"""
        return band_weight(self.stats.band_shift)

    @property
    def monthly(self) -> float:
        """전체 기간 월 환산 수익 (%)"""
        return per_month(self.full.total, self.days) if self.full else 0.0

    @property
    def test_monthly(self) -> float:
        """검증 기간 월 환산 수익 (%)"""
        return per_month(self.test.total, self.test_days) if self.test else 0.0

    @property
    def recent_empty(self) -> bool:
        """검증 기간에 완결된 거래가 하나도 없었나 (= 최근에는 목표를 낼 기회가 없었다)"""
        return self.test is None or self.test.trades == 0

    @property
    def base_score(self) -> float:
        """
        기본 점수 = 전체 월 수익과 검증 월 수익 중 작은 값 (지난 기간에만 맞춘 기준을 걸러내려고)

        단, 검증 기간에 **거래가 아예 없으면** 0 으로 뭉개지 말고 전체 점수를
        RECENT_FAIL_DISCOUNT 배로 깎는다. 0 으로 두면 '최근에 기회가 없던' 코인이 전부 0 점이 되어
        서로 구분이 안 된다 — 월 23% 를 자리 7% 로 버는 코인과 월 4% 를 자리 58% 로 버는 코인이 같은 0 점.
        벌점(4분의 1)은 그대로 세면서 순위는 살려 둔다.
        검증 기간에 거래가 있었는데 손실이면 min 이 음수가 되어 그대로 깔린다 (기회가 없던 것보다 나쁘다).
        """
        if not self.full:
            return -999.0
        if self.recent_empty and self.monthly > 0:
            return self.monthly * RECENT_FAIL_DISCOUNT
        return min(self.monthly, self.test_monthly)

    @property
    def score(self) -> float:
        """
        점수 = 자리당 효율 × 띠 가중치 (기본 점수가 + 일 때만. − 는 그대로 둬서 손해가 작아 보이지 않게)

        자리당 효율 = 기본 점수(전체·검증 중 작은 월수익) ÷ 자리 점유율

        예) 기본 13.94, 점유율 0.71, 띠 이동 0.23%p(가중치 1.0) → 13.94 ÷ 0.71 × 1.0 = 19.6
            기본  5.25, 점유율 0.79, 띠 가중치 1.0              →  5.25 ÷ 0.79 × 1.0 =  6.6
        → 자리를 비슷하게 쓰면서 덜 버는 코인이 확실히 밀린다.
        """
        base = self.base_score
        if base <= 0:
            return base
        return slot_efficiency(base, self.occupancy) * self.weight


def funding_per_8h_percent(funding: Sequence[Tuple[int, float]], days: float) -> float:
    """
    기간 동안 받은 펀딩비 합을 '8시간 한 번' 기준 평균 % 로 바꾸기

    코인마다 정산 주기(1/4/8시간)가 달라서, 합계 ÷ (기간 ÷ 8시간) 으로 맞춘다.
    예) 14일 동안 합 +0.42% → 8시간 칸이 42개 → +0.01%
    """
    if days <= 0 or not funding:
        return 0.0
    return sum(r for _, r in funding) * 100 / (days * 3)


def evaluate_coin(coin: str, series: Sequence[Tuple[int, float]], bar_ms: int, days: float,
                  spread_percent: float, usdt_spread_percent: float, fixed_cost_percent: float,
                  target: float, stop: float, funding: Sequence[Tuple[int, float]],
                  live: CoinLive, size_usdt: float,
                  cycle_good_hours: float = CYCLE_GOOD_HOURS,
                  recent_days: float = 0.0, entry_threshold: Optional[float] = None) -> Optional[CoinScore]:
    """
    코인 하나 평가: 통계 + 백테스트(전체 / 검증) + 경고 표시

    Args:
        series: [(시각, 김프 %)]
        entry_threshold: 진입 김프 기준을 이 값(%)으로 고정 (.env 의 ENTRY_PREMIUM_THRESHOLD_PERCENT 와 같은 뜻).
            None 이면 코인마다 후보를 다 돌려 가장 좋았던 기준을 고른다 (전체·검증 모두)
        spread_percent: 코인 호가 간격 (업비트 + 바이비트, %)
        usdt_spread_percent: 업비트 USDT/KRW 호가 간격 (%)
        fixed_cost_percent: 수수료 + 전송비 (%)
        recent_days: 검증에 쓸 최근 기간 (일). 0 이면 기간의 뒤 1/3
    """
    if len(series) < 50:
        return None
    values = [p for _, p in series]
    stats = premium_stats(values, bar_ms)
    leg = spread_percent + usdt_spread_percent
    costs = SwingCosts(leg_spread=leg, fixed=fixed_cost_percent, target=target, stop=stop)

    fixed_candidates = [entry_threshold] if entry_threshold is not None else None
    full = best_swing(series, costs, funding, fixed_candidates)

    # 검증: 앞 구간에서 기준 고르기 → 뒤 구간(최근)에 그대로 써보기
    # 기본은 뒤 1/3. --recent-days 를 주면 그 일수로 자른다 (--days 를 바꿔도 검증 길이가 안 흔들리게)
    cut = len(series) * 2 // 3
    if recent_days > 0 and series:
        edge = series[-1][0] - recent_days * DAY_MS
        at = next((i for i, (ts, _) in enumerate(series) if ts >= edge), None)
        if at is not None and 0 < at < len(series):
            cut = at
    train, test_part = series[:cut], series[cut:]
    picked = best_swing(train, costs, funding, fixed_candidates)
    test = run_swing(test_part, picked.threshold, costs, funding) if picked else None
    test_days = (test_part[-1][0] - test_part[0][0]) / DAY_MS if test_part else 0.0

    score = CoinScore(
        coin=coin, stats=stats, live=live, spread_percent=spread_percent,
        cost_percent=2 * leg + fixed_cost_percent,
        funding_8h=funding_per_8h_percent(funding, days),
        coverage=len(series) / max(1.0, days * DAY_MS / bar_ms),
        full=full, test=test, days=days, test_days=test_days, cycle_good_hours=cycle_good_hours,
    )
    score.flags = make_flags(score, size_usdt)
    return score


def make_flags(s: CoinScore, size_usdt: float) -> List[str]:
    """조심할 점 표시"""
    flags = []
    if s.abnormal:
        flags.append("이상김프")
    if s.coverage < MIN_COVERAGE:
        flags.append("데이터부족")
    if s.spread_percent > WIDE_SPREAD_PERCENT:
        flags.append("호가넓음")
    if s.live.depth_usdt is not None and s.live.depth_usdt < size_usdt:
        flags.append("물량얇음")
    if abs(s.stats.drift) > TREND_WARN_PERCENT:
        flags.append("추세↑" if s.stats.drift > 0 else "추세↓")
    if s.stats.half_life_h is None or s.stats.half_life_h > SLOW_REVERT_HOURS:
        flags.append("회귀느림")
    if s.cycle_hours and s.cycle_hours > 7 * 24:
        flags.append("기회드묾")
    if s.full and s.full.trades and s.recent_empty:
        flags.append("최근기회없음")   # 전체 기간엔 벌었지만 검증(최근) 구간엔 완결된 거래가 없다
    if s.funding_8h < 0:
        flags.append("펀딩냄")
    if s.full and s.full.is_open_at_end and s.full.open_net < 0:
        flags.append("물림")
    return flags


def rank(scores: Sequence[CoinScore]) -> List[CoinScore]:
    """점수 높은 순 (같으면 전체 월 수익 높은 순). 이상김프 코인은 맨 아래"""
    return sorted(scores, key=lambda s: (not s.abnormal, s.score, s.monthly), reverse=True)


# ======================================================================
# 출력
# ======================================================================
def _hl(h: Optional[float]) -> str:
    if h is None:
        return "  ∞"
    return f"{h:4.1f}" if h < 100 else f"{h:4.0f}"


def format_table(ranked: Sequence[CoinScore], top: int, settings_note: str) -> str:
    lines = [
        settings_note,
        "",
        "점수 = min(전체 월수익, 검증 월수익) ÷ 자리 점유율 × 띠 가중치 · 수익은 포지션 금액 대비 % "
        "(한 번에 포지션 1개, 펀딩비 포함)",
        "",
        f"{'순위':>3} {'코인':<7}{'봉':>4}{'점수':>7} {'전체/월':>7} {'검증/월':>7} {'거래':>4} {'승':>3} {'검증거래':>7} {'승':>3} "
        f"{'진입기준':>7} {'보유h':>5} {'주기h':>5} {'점유':>5} {'가중':>4} │ {'중간값':>6} {'범위(5~95%)':>13} {'횡보':>4} "
        f"{'띠이동':>5} {'반감h':>5} "
        f"│ {'호가':>5} {'비용':>5} {'업비트(억)':>9} {'바이비트(M$)':>11} {'펀딩/8h':>7}  경고",
    ]
    for i, s in enumerate(ranked[:top], 1):
        f = s.full
        hold = statistics.fmean(f.holding_hours) if f and f.holding_hours else 0.0
        lines.append(
            f"{i:>3}. {s.coin:<7}{s.timeframe:>4}{(f'{s.score:+.2f}' if f else '-'):>7} {s.monthly:>+7.2f} {s.test_monthly:>+7.2f} "
            f"{(f.trades if f else 0):>4} {(f.wins if f else 0):>3} "
            f"{(s.test.trades if s.test else 0):>7} {(s.test.wins if s.test else 0):>3} "
            f"{(f'{f.threshold:+.2f}' if f else '-'):>7} {hold:>5.1f} {_hl(s.cycle_hours):>5} "
            f"{s.occupancy * 100:>4.0f}% {s.weight:>4.2f} │ "
            f"{s.stats.median:>+6.2f} {s.stats.p5:>+6.2f}~{s.stats.p95:>+6.2f} "
            f"{s.stats.sideways_share * 100:>3.0f}% {s.stats.band_shift:>5.2f} {_hl(s.stats.half_life_h):>5} │ "
            f"{s.spread_percent:>5.2f} {s.cost_percent:>5.2f} {s.live.upbit_krw_24h / 1e8:>9,.0f} "
            f"{s.live.bybit_usdt_24h / 1e6:>11,.1f} {s.funding_8h:>+7.4f}  {','.join(s.flags)}"
        )
    return "\n".join(lines)


LEGEND = """
열 설명
  봉       이 점수를 계산한 캔들 간격. 1m = 2단계에서 시뮬레이션과 같은 기준으로 다시 본 코인
  전체/월  전체 기간에 가장 좋았던 진입 기준으로 돌렸을 때 월 환산 수익 (%)
  검증/월  앞 기간에서 고른 기준을 뒤(최근) 기간에 그대로 썼을 때 월 환산 수익 (%)
           기본은 뒤 1/3 (--recent-days 로 '최근 N일' 고정 가능). 0.00 이면 그 구간에 완결된 거래가 없었다는 뜻
           → 점수는 전체/월 의 1/4 로 깎인다 (최근기회없음 경고). 0 으로 뭉개면 코인끼리 구분이 안 되므로
  거래/승  전체 기간 닫은 거래 수 / 그중 이익 / 진입기준: 그때 쓴 진입 김프 기준 (%)
  보유h    거래 하나 평균 보유 시간
  주기h    반복 주기 = 기간 ÷ 왕복 횟수 (시간). 24시간 이하면 가중치 1, 48시간 0.5, 1주 0.14
  점유     자리 점유율 = 포지션을 들고 있던 시간 ÷ 전체 기간. 자리가 MAX_OPEN_POSITIONS 개뿐이라,
           오래 들고 있는 코인은 다른 코인의 진입을 막는다 → 점수를 이 값으로 나눈다 (하한 25%)
  가중     띠 가중치 (0~1). 같은 띠 안에서 움직이는 코인만 인정
  중간값·범위  봇 김프(코인 몫) 중간값과 하위 5% ~ 상위 5% (넓을수록 기회가 큼)
  횡보     중간값 ±0.3% 안에 있던 시간 비율
  띠이동   기간 4등분 구간별 중간값의 최고 − 최저 (%p). 0.3 이하 가중치 1, 1.5 이상 0
  반감h    김프가 벌어졌다가 절반 돌아오는 데 걸린 시간 (짧을수록 좋음, ∞ = 안 돌아옴)
  호가     코인 호가 간격 (업비트+바이비트, 실시간과 호가 한 칸 중 큰 값, %)
  비용     한 바퀴 비용 = 수수료 + 전송비 + (코인 호가 + USDT 호가) × 2 (%)
  펀딩/8h  평균 펀딩비율 (8시간 기준 %). + 면 숏인 우리가 받음
경고
  이상김프(1시간 평균 ±5% 초과, 입출금 막힘 의심 → 맨 아래) 기회드묾(주기 1주 초과) 데이터부족(기간 80% 미만) 호가넓음(>0.3%) 물량얇음(호가 0.3% 안 물량 < 포지션 크기)
  추세↑↓(중간값이 1%p 넘게 이동) 회귀느림(반감기 48시간 초과) 펀딩냄 물림(끝에 손실로 들고 있음)
  최근기회없음(전체 기간엔 벌었지만 검증 구간엔 완결된 거래가 없음 → 점수 1/4 로 감점)
"""


def env_snippet(ranked: Sequence[CoinScore], n: int, max_open_positions: int = 3) -> str:
    """
    상위 코인을 .env 에 바로 붙일 수 있게

    자리가 MAX_OPEN_POSITIONS 개뿐이라 점유율 합이 그 안에 들어가게 고른다.
    꽉 채우면 몇 시간마다 기회가 오는 코인이 들어갈 틈이 없어서, 예산은 자리 수 × 0.8 만 쓴다.
      예) 자리 3개 → 예산 2.4. CAP(0.71) + WAVES(0.79) + GRVT(0.78) = 2.28 까지 담고 다음은 버림
    """
    budget = max_open_positions * SLOT_BUDGET_RATIO
    picks: List[CoinScore] = []
    used = 0.0
    skipped: List[str] = []
    for s in ranked[:n]:
        if not (s.full and s.score > 0 and not s.abnormal):
            continue
        need = max(s.occupancy, MIN_OCCUPANCY)
        if used + need > budget:
            skipped.append(s.coin)
            continue
        picks.append(s)
        used += need
    if not picks:
        return "점수가 + 인 코인이 없습니다. (목표 순이익을 낮추거나 기간을 바꿔서 다시 보세요)"
    lines = [f"# .env 에 붙여넣기 예시 (자리 {max_open_positions}개 예산 {budget:.1f} 중 {used:.2f} 사용)",
             "COINS=" + ",".join(s.coin for s in picks)]
    lines += [f"{s.coin}_ENTRY_PREMIUM_THRESHOLD_PERCENT={s.full.threshold:.2f}" for s in picks]
    if skipped:
        lines.append(f"# 자리가 모자라 빠진 코인: {', '.join(skipped)} "
                     f"(넣으려면 MAX_OPEN_POSITIONS 를 늘리거나 위 코인을 빼세요)")
    return "\n".join(lines)


# ======================================================================
# 데이터 받기 (거래소)
# ======================================================================
def with_retry(fn, *args, tries: int = 4, wait_sec: float = 2.0, **kwargs):
    """
    거래소가 '요청이 너무 많다' 고 거절하면 잠깐 쉬고 다시 시도 (2초 → 4초 → 8초)

    코인 80개를 연달아 받다 보면 바이비트가 가끔 거절한다 (retCode 10006).
    """
    import ccxt

    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection, ccxt.NetworkError):
            if i == tries - 1:
                raise
            time.sleep(wait_sec * 2 ** i)


def list_universe(md) -> List[str]:
    """업비트 원화 마켓과 바이비트 USDT 무기한에 둘 다 있는 코인 (스테이블코인 제외)"""
    up = {m["base"] for m in md.upbit.markets.values() if m.get("quote") == "KRW" and m.get("active", True)}
    coins = [c for c in up if c not in STABLECOINS and bybit_symbol(c) in md.bybit.markets]
    return sorted(coins)


def fetch_live(md, coins: List[str]) -> Dict[str, CoinLive]:
    """24시간 거래대금(양쪽) + 지금 호가 간격·물량"""
    live = {c: CoinLive() for c in coins}
    up_t = md.upbit.fetch_tickers([upbit_symbol(c) for c in coins])
    by_t = md.bybit.fetch_tickers([bybit_symbol(c) for c in coins])
    for c in coins:
        live[c].upbit_krw_24h = float((up_t.get(upbit_symbol(c)) or {}).get("quoteVolume") or 0)
        live[c].bybit_usdt_24h = float((by_t.get(bybit_symbol(c)) or {}).get("quoteVolume") or 0)
    return live


def load_series(store: HistoryStore, md, coins: Sequence[str], timeframe: str, start_ms: int, now_ms: int,
                src, retrying, with_funding: bool = True, progress: bool = True):
    """
    코인별 (업비트 종가, 바이비트 종가, 펀딩비 기록) 받아오기

    Returns:
        ({코인: (업비트, 바이비트, 펀딩)}, 업비트 USDT 종가, USDT 호가 간격 %, 지금 USDT 가격)
        USDT 캔들을 못 받으면 (None, ...) 가 아니라 빈 dict 와 None 들을 돌려준다
    """
    bar_ms = TIMEFRAME_MS[timeframe]
    store.ensure_candles(src(UPBIT_USDT, timeframe), "USDT", start_ms, now_ms,
                         ccxt_candle_fetcher(md.upbit, USDT_KRW_SYMBOL, timeframe), step_ms=bar_ms)
    usdt = store.load_closes(src(UPBIT_USDT, timeframe), "USDT", start_ms, now_ms)
    if not usdt:
        return {}, {}, None, None
    data = {}
    for i, c in enumerate(coins, 1):
        if progress:
            print(f"  [{i}/{len(coins)}] {c}        ", end='\r')
        try:
            store.ensure_candles(src(UPBIT, timeframe), c, start_ms, now_ms,
                                 retrying(ccxt_candle_fetcher(md.upbit, upbit_symbol(c), timeframe)), step_ms=bar_ms)
            store.ensure_candles(src(BYBIT, timeframe), c, start_ms, now_ms,
                                 retrying(ccxt_candle_fetcher(md.bybit, bybit_symbol(c), timeframe)), step_ms=bar_ms)
            if with_funding:
                store.ensure_funding(c, start_ms, now_ms, retrying(ccxt_funding_fetcher(md.bybit, bybit_symbol(c))))
        except Exception as exc:
            print('\n' + f"  {c} 데이터 받기 실패 (건너뜀): {exc}")
            continue
        data[c] = (store.load_closes(src(UPBIT, timeframe), c, start_ms, now_ms),
                   store.load_closes(src(BYBIT, timeframe), c, start_ms, now_ms),
                   [r for r in store.load_funding(c, start_ms, now_ms) if r[0] < now_ms])
    if progress:
        print("")  # 한 줄 띄우기
    return data, usdt, usdt_tick_percent(list(usdt.values())), usdt[max(usdt)]


def evaluate_all(data, usdt, timeframe: str, days: float, resolved: Dict[str, float], usdt_spread: float,
                 fixed: float, target: float, stop: float, live: Dict[str, CoinLive],
                 size: float, cycle_hours: float, recent_days: float = 0.0,
                 entry_threshold: Optional[float] = None, exec_slippage=None) -> List[CoinScore]:
    """받아둔 종가로 코인마다 점수 계산 (호가 간격은 미리 정해둔 resolved 값 사용)"""
    bar_ms = TIMEFRAME_MS[timeframe]
    scores = []
    for c, (up, by, funding) in data.items():
        floor = tick_floor_percent(list(up.values()), list(by.values())) or 0.0
        spread = max(resolved.get(c, 0.0), floor)
        # 체결 밀림 가정(kimp/slippage.py)은 거래당 고정 비용으로 — 청산 때 판단 호가보다 불리하게 체결되는 몫
        slip = exec_slippage.for_coin(c) if exec_slippage is not None else 0.0
        s = evaluate_coin(c, premium_series(up, by, usdt), bar_ms, days, spread, usdt_spread,
                          fixed + slip, target, stop, funding, live[c], size, cycle_hours, recent_days, entry_threshold)
        if s:
            s.timeframe = timeframe
            scores.append(s)
    return scores


def save_json(ranked: Sequence[CoinScore], top: int, note: str, snippet: str, path: str) -> None:
    """
    결과를 JSON 으로 저장 (로컬 화면 `python -m kimp.webui` 가 읽는다)

    화면에서 표를 다시 그리기 위한 것이라, 표에 보이는 값만 담는다.
    """
    import statistics as _stat

    rows = []
    for i, s in enumerate(ranked[:top], 1):
        f = s.full
        rows.append({
            "rank": i, "coin": s.coin, "timeframe": s.timeframe, "score": s.score,
            "monthly": s.monthly, "test_monthly": s.test_monthly,
            "trades": f.trades if f else 0, "wins": f.wins if f else 0,
            "test_trades": s.test.trades if s.test else 0, "test_wins": s.test.wins if s.test else 0,
            "threshold": f.threshold if f else None,
            "holding_hours": _stat.fmean(f.holding_hours) if f and f.holding_hours else 0.0,
            "cycle_hours": s.cycle_hours, "occupancy": s.occupancy,
            "spread_percent": s.spread_percent, "cost_percent": s.cost_percent,
            "funding_8h": s.funding_8h, "median": s.stats.median, "band_shift": s.stats.band_shift,
            "upbit_krw_24h": s.live.upbit_krw_24h, "bybit_usdt_24h": s.live.bybit_usdt_24h,
            "flags": list(s.flags),
        })
    data = {"created_at": datetime.now(timezone.utc).isoformat(), "note": note,
            "env_snippet": snippet, "rows": rows}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=1)
    print()
    print(f"결과 저장: {path} (로컬 화면에서 보려면 python -m kimp.webui)")


def default_size_usdt(settings: Settings) -> float:
    """포지션 크기: 설정값, 없으면 페이퍼 시작 자본으로 자동 계산한 값 (봇과 같은 식)"""
    if settings.position_size_usdt:
        return settings.position_size_usdt
    return (settings.paper_initial_total_usdt * settings.upbit_target_share()
            * settings.capital_usage_percent / 100 / settings.max_open_positions)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """python3 -m kimp.screen [--days 30] [--top 20]"""
    from kimp.market_data import MarketData

    parser = argparse.ArgumentParser(description="추천 코인 순위 (최근 김프 움직임으로 전략이 잘 통했을 코인 찾기)")
    parser.add_argument("--days", type=float, default=30, help="최근 며칠을 볼지 (기본 30)")
    parser.add_argument("--top", type=int, default=20, help="몇 위까지 보여줄지 (기본 20)")
    parser.add_argument("--spread-source", default=MODE_AUTO, choices=list(MODES),
                        help="호가 간격을 어디서 가져올지. auto=기록→지금 호가→공통값 (기본) / "
                             "records=실측 기록만 / common=공통값으로 통일 / live=지금 호가. "
                             "<코인>_SIM_SPREAD_PERCENT 가 있으면 그 코인은 모드와 무관하게 그 값")
    parser.add_argument("--recent-days", type=float, default=0.0,
                        help="검증에 쓸 최근 기간 (일). 0 = 기간의 뒤 1/3 (기본). "
                             "예: --days 30 --recent-days 7 → 앞 23일로 기준 고르고 최근 7일로 검증")
    parser.add_argument("--timeframe", default="15m", choices=sorted(TIMEFRAME_MS), help="캔들 간격 (기본 15m)")
    parser.add_argument("--coins", default=None, help="이 코인들만 보기 예: XRP,SOL (안 적으면 전체)")
    parser.add_argument("--json-path", default="data/screen_latest.json",
                        help="결과를 저장할 JSON 경로 (로컬 화면이 읽는다)")
    parser.add_argument("--refine-top", type=int, default=REFINE_TOP,
                        help=f"1단계 상위 몇 개를 짧은 봉으로 다시 평가할지 (기본 {REFINE_TOP}, 0 이면 2단계 생략)")
    parser.add_argument("--refine-timeframe", default=REFINE_TIMEFRAME, choices=sorted(TIMEFRAME_MS),
                        help=f"2단계에서 쓸 캔들 간격 (기본 {REFINE_TIMEFRAME} = 시뮬레이션과 같은 기준)")
    parser.add_argument("--end", default=None,
                        help="이 날짜(한국 시간)까지의 데이터로 순위 매기기 예: 2026-09-22 (안 적으면 지금). "
                             "과거 한 달로 고르고 다음 한 달에 시뮬레이션해 보는 검증용")
    parser.add_argument("--max-coins", type=int, default=80,
                        help="거래량 걸러낸 뒤 업비트 거래대금 상위 몇 개까지 볼지 (기본 80)")
    parser.add_argument("--min-upbit-krw", type=float, default=1e9, help="업비트 24시간 거래대금 최소 (원, 기본 10억)")
    parser.add_argument("--min-bybit-usdt", type=float, default=1e6, help="바이비트 24시간 거래대금 최소 (USDT, 기본 100만)")
    parser.add_argument("--target", type=float, default=None, help="목표 순이익 %% (안 적으면 .env)")
    parser.add_argument("--entry-threshold", type=float, default=None,
                        help="진입 김프 기준 %% 를 이 값으로 고정 (ENTRY_PREMIUM_THRESHOLD_PERCENT 와 같은 뜻). "
                             "안 적으면 코인마다 후보를 돌려 가장 좋았던 기준을 고른다")
    parser.add_argument("--exec-slippage", default=None,
                        help="체결 밀림 가정 %% (청산 때 판단 호가보다 불리하게 체결되는 몫, 거래당 비용에 더함). "
                             "숫자 또는 auto(실거래 체결 기록의 코인별 중앙값). 안 적으면 .env SLIPPAGE_BUFFER_PERCENT")
    parser.add_argument("--cycle-hours", type=float, default=CYCLE_GOOD_HOURS,
                        help="반복 주기가 이 시간 이하면 가중치 1 (기본 24)")
    parser.add_argument("--size-usdt", type=float, default=None, help="포지션 크기 USDT (전송비 %%·물량 확인용)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s - %(message)s")
    settings = load_settings()
    target = args.target if args.target is not None else settings.target_net_profit_percent
    size = args.size_usdt or default_size_usdt(settings)
    from kimp.slippage import resolve as resolve_slippage
    slip = resolve_slippage(args.exec_slippage, settings.fees.slippage_buffer_percent, settings.live_db_path)
    fixed = settings.fees.round_trip_trade_fee_percent() + settings.fees.transfer_fee_usdt_per_cycle / size * 100
    bar_ms = TIMEFRAME_MS[args.timeframe]

    md = MarketData()
    if args.coins:
        coins = md.filter_supported([c.strip().upper() for c in args.coins.split(",")])
    else:
        coins = list_universe(md)
    print(f"양쪽 거래소에 다 있는 코인 {len(coins)}개 → 거래량 확인 중...")
    live = fetch_live(md, coins)
    if not args.coins:  # 전체를 볼 때만 거래량으로 거르기
        coins = [c for c in coins if live[c].upbit_krw_24h >= args.min_upbit_krw
                 and live[c].bybit_usdt_24h >= args.min_bybit_usdt]
        coins = sorted(coins, key=lambda c: live[c].upbit_krw_24h, reverse=True)[:args.max_coins]
        print(f"거래량 기준 통과 {len(coins)}개 (업비트 ≥ {args.min_upbit_krw / 1e8:,.0f}억원, "
              f"바이비트 ≥ {args.min_bybit_usdt / 1e6:,.1f}M USDT)")
    if not coins:
        print("볼 코인이 없습니다.")
        return 2

    now_ms = int(time.time() * 1000) // bar_ms * bar_ms - bar_ms  # 끝난 봉까지만
    if args.end:
        from kimp.simulate import parse_kst

        now_ms = min(now_ms, parse_kst(args.end) // bar_ms * bar_ms)
        print(f"{args.end} 까지의 데이터로 봅니다 (거래량·호가 간격·물량은 '지금' 값이라 참고만)")
    start_ms = now_ms - int(args.days * DAY_MS)
    store = HistoryStore(make_history_session_factory(settings.history_db_path))
    def src(base: str, timeframe: str) -> str:
        """
        캔들 저장 이름. 1분봉은 시뮬레이션과 **같은 이름**으로 저장해서 서로 재사용한다.
        (1분봉 외에는 'upbit@15m' 처럼 간격을 붙여 따로 저장)
        """
        return base if timeframe == "1m" else f"{base}@{timeframe}"

    retrying = lambda fetch: (lambda *a: with_retry(fetch, *a))

    print(f"1단계: {args.timeframe} 캔들 받는 중 (처음엔 시간이 걸리고, 다음부터는 모자란 구간만 받음)...")
    data, usdt, usdt_spread, usdt_now = load_series(store, md, coins, args.timeframe, start_ms, now_ms, src, retrying)
    if not data:
        print("업비트 USDT 캔들을 못 받았습니다.")
        return 2

    print(f"지금 호가창 확인 중... ({LIVE_SAMPLES}번 재서 중간값. 한 번만 재면 그 순간 호가에 순위가 휘둘린다)")
    books = median_spread_depth(md, list(data), usdt_now, retry=with_retry,
                                progress=lambda n, total: print(f"  {n}/{total}번째", flush=True))
    # 봇이 쌓은 기록이 있으면 그게 우선 (시뮬레이션과 같은 규칙: kimp/spread.py)
    resolved, spread_sources = resolve_spreads(list(data), settings.snapshot_db_paths(),
                                               settings.sim_default_spread_percent,
                                               {c: v[0] for c, v in books.items()},
                                               mode=args.spread_source)
    for c in data:
        live[c].spread_percent, live[c].depth_usdt = books.get(c, (None, None))

    scores = evaluate_all(data, usdt, args.timeframe, args.days, resolved, usdt_spread, fixed, target,
                          settings.stop_loss_percent, live, size, args.cycle_hours, args.recent_days,
                          args.entry_threshold, exec_slippage=slip)

    # 2단계: 1단계 상위 코인만 더 짧은 봉으로 다시 평가 (시뮬레이션과 같은 기준으로 맞추기)
    refined_timeframe = None
    if args.refine_top and args.refine_timeframe != args.timeframe:
        picks = [x.coin for x in rank(scores) if not x.abnormal][:args.refine_top]
        if picks:
            refined_timeframe = args.refine_timeframe
            print(f"2단계: 상위 {len(picks)}개를 {refined_timeframe} 봉으로 다시 평가합니다 "
                  f"({args.timeframe} 봉은 짧게 벌어졌다 메워진 기회를 놓칩니다. "
                  f"받은 1분봉은 시뮬레이션에서도 그대로 쓰입니다)")
            r_bar = TIMEFRAME_MS[refined_timeframe]
            r_now = now_ms // r_bar * r_bar
            r_start = r_now - int(args.days * DAY_MS)
            r_data, r_usdt, r_usdt_spread, _ = load_series(store, md, picks, refined_timeframe, r_start, r_now,
                                                           src, retrying, with_funding=False)
            if r_data:
                refined = evaluate_all(r_data, r_usdt, refined_timeframe, args.days, resolved, r_usdt_spread,
                                       fixed, target, settings.stop_loss_percent, live, size,
                                       args.cycle_hours, args.recent_days, args.entry_threshold, exec_slippage=slip)
                done = {x.coin for x in refined}
                scores = refined + [x for x in scores if x.coin not in done]
                usdt_spread = r_usdt_spread
            else:
                print("  2단계 캔들을 못 받아서 1단계 결과로 보여줍니다.")
                refined_timeframe = None

    ranked = rank(scores)
    used = Counter(spread_sources[s.coin] for s in scores)
    bars = (f"{args.timeframe}봉 → 상위 {args.refine_top}개는 {refined_timeframe}봉 재평가"
            if refined_timeframe else f"{args.timeframe}봉")
    entry_note = (f"진입 기준 고정 {args.entry_threshold:+g}% · " if args.entry_threshold is not None
                  else "진입 기준 코인별 최적 · ")
    note = (f"{'~' + args.end + ' ' if args.end else '최근 '}{args.days:g}일 · {bars} · {len(scores)}개 코인 · 목표 순이익 {target:g}% · "
            f"{entry_note}손절 {settings.stop_loss_percent:g}% · 포지션 {size:,.0f} USDT · "
            f"수수료+전송비 {fixed:.2f}% · USDT 호가 {usdt_spread:.3f}% · {slip.describe()}\n"
            f"코인 호가 간격 출처: {', '.join(f'{k} {v}개' for k, v in used.most_common())} "
            f"(호가 단위보다 좁으면 호가 단위로 올림)")
    print()
    rough = [c for c in data if is_rough(spread_sources.get(c, ""))]
    if rough:
        print(f"[주의] 호가 간격을 '지금 호가' 로 잰 코인 {len(rough)}개 — 순위가 실행마다 흔들립니다.")
        print("       봇을 페이퍼로 돌려 기록을 쌓으면 고정됩니다 (docs/commands.md '호가 기록 쌓기').")
        print()
    print(format_table(ranked, args.top, note))
    print(LEGEND)
    snippet = env_snippet(ranked, args.top, settings.max_open_positions)
    print(snippet)
    save_json(ranked, args.top, note, snippet, args.json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

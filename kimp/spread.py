"""
코인 호가 간격(스프레드) 정하기 — 스크리너와 시뮬레이션이 같은 값을 쓰도록 모아둔 곳

호가 간격은 이 전략의 비용에서 가장 큰 몫이다. 목표가 1% 인데 호가 간격이 0.35% 면
왕복 0.7% 가 비용으로 빠져서, 이 값이 틀리면 코인 순위와 시뮬레이션 손익이 전부 틀린다.

**왜 따로 모았나 (2026-10-22)**
전에는 스크리너가 호가창을 '한 번' 조회해서 그 값으로 30일을 평가했다. 그런데 호가 간격은
몇 칸 벌어져 있는지가 수시로 바뀌어서, 14분 간격으로 두 번 돌리면 결과가 이렇게 달라졌다.

    CAP  호가 0.28% → 0.37%   검증 월수익 +13.94% → +4.90%   거래 14건 → 9건
    SEI  호가 0.27% → 0.14%   전체 월수익  +3.44% → +21.81%  거래  3건 → 20건

코인 데이터는 하나도 안 바뀌었는데 '그 순간 호가창' 때문에 순위가 뒤집힌 것이다.
그래서 지금은 아래 순서로 정하고, 조회할 때도 여러 번 재서 중간값을 쓴다.

    1) 봇이 쌓은 기록 (premium_snapshots) 평균 — 가장 믿을 만하다
    2) 코인별 설정 <코인>_SIM_SPREAD_PERCENT
    3) 지금 호가창 여러 번 조회한 중간값
    4) 공통 설정 SIM_DEFAULT_SPREAD_PERCENT (기본 0.1%)

어느 것을 썼는지 'source' 로 함께 돌려줘서 결과 표에 그대로 보여준다.
"""
from __future__ import annotations

import logging
import os
import statistics
import time
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select

from kimp.db import PremiumSnapshot, make_session_factory

logger = logging.getLogger(__name__)

# 기록 평균을 '실측' 으로 인정하는 최소 건수. 1분에 1건씩 쌓이므로 30건 = 30분
MIN_SNAPSHOTS_FOR_SPREAD = 30

# 지금 호가창을 몇 번 재서 중간값을 쓸지, 그리고 그 사이에 몇 초 쉴지
# 한 번만 재면 위 CAP·SEI 처럼 순간값에 휘둘린다. 3번이면 약 (3-1) × 4초 = 8초 더 걸린다.
LIVE_SAMPLES = 3
LIVE_SAMPLE_PAUSE_SEC = 4.0

# 물량을 볼 때 맨 앞 호가에서 이만큼(소수) 안쪽까지 센다. 0.003 = 0.3%
DEPTH_WITHIN = 0.003


def snapshot_spreads_multi(db_paths: Sequence[str], coins: Sequence[str]) -> Dict[str, Tuple[float, int]]:
    """
    여러 DB 에서 호가 간격 기록을 모아 합친다 (앞쪽 DB 가 우선)

    수집기 DB(data/market.db) 를 먼저 보고, 없는 코인은 봇 DB 의 기록을 쓴다.
    같은 코인이 양쪽에 있으면 건수가 많은 쪽을 쓴다 (더 많이 재 본 쪽이 믿을 만하다).
    """
    merged: Dict[str, Tuple[float, int]] = {}
    for path in db_paths:
        for coin, (avg, n) in snapshot_spreads(path, coins).items():
            if coin not in merged or n > merged[coin][1]:
                merged[coin] = (avg, n)
    return merged


def snapshot_spreads(db_path: str, coins: Sequence[str]) -> Dict[str, Tuple[float, int]]:
    """
    봇이 쌓은 김프 기록에서 코인 호가 간격 평균 꺼내기

    코인 호가 간격 = 진입 김프 − 청산 김프 − USDT 호가 간격
      예) 진입 +0.165%, 청산 −0.259%, USDT 0.075% → 0.349%

    Returns:
        {코인: (평균 %, 건수)} — 기록이 없으면 빈 dict
    """
    if not coins or not os.path.exists(db_path):  # 없는 DB 를 새로 만들지 않도록
        return {}
    spread = (PremiumSnapshot.entry_premium_top - PremiumSnapshot.exit_premium_top
              - func.coalesce(PremiumSnapshot.usdt_spread_percent, 0.0))
    with make_session_factory(db_path)() as session:
        rows = session.execute(
            select(PremiumSnapshot.coin, func.avg(spread), func.count())
            .where(PremiumSnapshot.coin.in_(list(coins))).group_by(PremiumSnapshot.coin)
        )
        return {coin: (avg, n) for coin, avg, n in rows if avg is not None}


def _depth(levels, best: float, is_ask: bool, within: float = DEPTH_WITHIN) -> float:
    """맨 앞 호가에서 within(소수) 안쪽에 쌓인 금액 (가격 × 개수 합)"""
    limit = best * (1 + within) if is_ask else best * (1 - within)
    return sum(p * q for p, q, *_ in levels if (p <= limit if is_ask else p >= limit))


def fetch_spread_depth(md, coins: Sequence[str], usdt_krw: float,
                       retry=None) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    """
    코인마다 (지금 호가 간격 %, 물량 USDT) 한 번 조회

    호가 간격 = 업비트 (매도1 − 매수1) ÷ 중간 + 바이비트 (매도1 − 매수1) ÷ 중간
    물량 = 업비트 매도벽(우리가 살 쪽)과 바이비트 매수벽(우리가 숏 칠 쪽) 중 작은 쪽

    Args:
        retry: 거래소 호출을 감쌀 함수 (없으면 그냥 호출). 스크리너는 429 재시도를 넣어서 쓴다
    """
    from kimp.market_data import bybit_symbol, upbit_symbol

    call = retry or (lambda fn, *a, **kw: fn(*a, **kw))
    out: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    up_books: Dict[str, dict] = {}
    for i in range(0, len(coins), 50):  # 업비트는 여러 코인 호가창을 한 번에 받을 수 있다
        chunk = [upbit_symbol(c) for c in coins[i:i + 50]]
        try:
            up_books.update(call(md.upbit.fetch_order_books, chunk, limit=15))
        except Exception as exc:
            logger.warning("업비트 호가창 받기 실패: %s", exc)
    for c in coins:
        try:
            ub = up_books.get(upbit_symbol(c))
            bb = call(md.bybit.fetch_order_book, bybit_symbol(c), limit=50)
            if not ub or not ub["bids"] or not ub["asks"] or not bb["bids"] or not bb["asks"]:
                out[c] = (None, None)
                continue
            ub_bid, ub_ask = ub["bids"][0][0], ub["asks"][0][0]
            bb_bid, bb_ask = bb["bids"][0][0], bb["asks"][0][0]
            spread = ((ub_ask - ub_bid) / ((ub_ask + ub_bid) / 2)
                      + (bb_ask - bb_bid) / ((bb_ask + bb_bid) / 2)) * 100
            depth = min(_depth(ub["asks"], ub_ask, True) / usdt_krw, _depth(bb["bids"], bb_bid, False))
            out[c] = (spread, depth)
        except Exception as exc:
            logger.warning("%s 호가창 받기 실패: %s", c, exc)
            out[c] = (None, None)
    return out


def median_spread_depth(md, coins: Sequence[str], usdt_krw: float, samples: int = LIVE_SAMPLES,
                        pause_sec: float = LIVE_SAMPLE_PAUSE_SEC, retry=None,
                        progress=None) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    """
    호가창을 samples 번 조회해서 **호가 간격은 중간값**, 물량은 마지막 값으로 돌려준다

    중간값을 쓰는 이유: 한 번만 재면 그 순간 호가가 1칸이었는지 3칸이었는지에 결과가 좌우된다.
      예) 3번 재서 0.11% / 0.35% / 0.33% → 중간값 0.33% (한 번만 쟀다면 0.11% 가 나올 수도 있었다)
    """
    runs: List[Dict[str, Tuple[Optional[float], Optional[float]]]] = []
    for i in range(max(1, samples)):
        if i and pause_sec:
            time.sleep(pause_sec)
        if progress:
            progress(i + 1, max(1, samples))
        runs.append(fetch_spread_depth(md, coins, usdt_krw, retry=retry))
    out: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    for c in coins:
        spreads = [r[c][0] for r in runs if r.get(c) and r[c][0] is not None]
        depths = [r[c][1] for r in runs if r.get(c) and r[c][1] is not None]
        out[c] = (statistics.median(spreads) if spreads else None, depths[-1] if depths else None)
    return out


# 출처 라벨. '기록' 과 '코인별 설정' 만 믿을 만한 값으로 본다 (아래 is_rough)
SOURCE_RECORDS = "기록"
SOURCE_ENV = "코인별 설정"
SOURCE_LIVE = f"지금 호가 {LIVE_SAMPLES}번 중간값"
SOURCE_DEFAULT = "기본 설정"


def is_rough(source: str) -> bool:
    """
    이 출처는 '대충 값' 인가 (= 실행마다 결과가 바뀔 수 있나)

    '지금 호가' 와 '기본 설정' 은 과거 기간과 상관없는 값이라 시뮬레이션 숫자를 못 믿는다.
    실제로 같은 기간을 돌렸는데 실행마다 CAP 호가가 0.125% / 0.266% 로 잡혀서
    거래 수가 115건 / 76건, 손익이 +63 / +42 USDT 로 갈렸다. (2026-10-22)
    """
    return source.startswith(SOURCE_LIVE[:6]) or source == SOURCE_DEFAULT


def accumulate_hint(coins: Sequence[str], rough_flag: str = "--rough-spread") -> str:
    """호가 기록을 쌓는 방법 안내 (시뮬레이션이 막혔을 때 그대로 보여준다)"""
    names = ",".join(coins)
    BS = chr(92)
    lines = [
        f"호가 간격 기록이 없어서 숫자를 믿을 수 없습니다: {names}",
        "",
        "과거 호가창은 거래소가 주지 않습니다. 시세 수집기로 직접 쌓아야 합니다",
        f"(코인마다 1분에 1건. {MIN_SNAPSHOTS_FOR_SPREAD}건만 넘으면 자동으로 그 값을 씁니다).",
        "",
        "  윈도우",
        f"    .{BS}.venv{BS}Scripts{BS}python.exe -m kimp.collect --coins {names}",
        "",
        "  맥 / Git Bash",
        f"    python -m kimp.collect --coins {names}",
        "",
        "  (주문·포지션 없이 김프·호가만 적습니다. 코인당 1분에 1줄, 30줄만 넘으면 바로 쓸 수 있습니다)",
        "",
        "자세한 설명: docs/commands.md '시세 수집'",
        f"그래도 지금 대충이라도 돌려보려면 {rough_flag} 를 붙이세요 (숫자는 실행마다 바뀝니다).",
    ]
    return chr(10).join(lines)


def resolve_spreads(coins: Sequence[str], db_path, default_percent: float,
                    live: Optional[Dict[str, Optional[float]]] = None,
                    min_snapshots: int = MIN_SNAPSHOTS_FOR_SPREAD) -> Tuple[Dict[str, float], Dict[str, str]]:
    """
    코인별 호가 간격을 정한다: 기록 평균 → 코인별 설정 → 지금 호가창 중간값 → 공통 기본값

    Args:
        db_path: 기록 DB 경로 하나, 또는 여러 개 (앞쪽 우선). 없는 파일은 건너뛴다
        default_percent: 아무것도 없을 때 쓸 값 (SIM_DEFAULT_SPREAD_PERCENT)
        live: {코인: 지금 호가 간격 %} (median_spread_depth 결과). 없으면 건너뛴다
    Returns:
        ({코인: %}, {코인: 어디서 온 값인지 설명})
    """
    paths = [db_path] if isinstance(db_path, str) else list(db_path)
    measured = snapshot_spreads_multi(paths, coins)
    spreads: Dict[str, float] = {}
    sources: Dict[str, str] = {}
    for coin in coins:
        avg_n = measured.get(coin)
        env_value = os.getenv(f"{coin}_SIM_SPREAD_PERCENT", "").strip()
        live_value = (live or {}).get(coin)
        if avg_n and avg_n[1] >= min_snapshots:
            spreads[coin], sources[coin] = avg_n[0], f"{SOURCE_RECORDS} {avg_n[1]:,}건"
        elif env_value:
            spreads[coin], sources[coin] = float(env_value), SOURCE_ENV
        elif live_value is not None:
            spreads[coin], sources[coin] = live_value, SOURCE_LIVE
        else:
            spreads[coin], sources[coin] = default_percent, SOURCE_DEFAULT
    return spreads, sources

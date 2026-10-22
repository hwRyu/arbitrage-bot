"""
시세 수집기 — 김프·호가 기록만 쌓는다 (주문도 포지션도 없다)

    python -m kimp.collect                   # .env 의 COINS
    python -m kimp.collect --coins CAP,SEI   # 코인 지정
    python -m kimp.collect --top 40          # 거래대금 상위 40개
    python -m kimp.collect --all             # 양쪽 거래소에 다 있는 코인 전부 (약 240개)
    python -m kimp.collect --once            # 한 번만 기록하고 종료 (확인용)
    python -m kimp.collect --hours 24        # 24시간 돌고 종료

**봇(main.py)과 역할이 다르다**
    main.py          전략을 돌린다 (진입·청산·손익). 페이퍼/드라이런/실거래
    kimp.collect     시장 데이터만 적는다. 주문·포지션·잔고·텔레그램 없음

**왜 따로 있나**
과거 호가창은 거래소가 주지 않는다. 그래서 '그 순간 호가로 진입/청산하면 김프가 얼마였나' 는
우리가 직접 적어 둬야만 알 수 있다. 이 기록이 있으면 스크리너·시뮬레이션이 추측값(기본 0.1%) 대신
실측 호가 간격을 쓴다. 전략을 돌릴 필요가 없으니 수집은 따로 돌린다.
    - 코인을 수십 개 넣어도 안전하다 (진입을 안 하므로)
    - 자본·레버리지·안전장치와 무관하다
    - 봇과 같이 돌려도 된다 (DB 가 따로다: data/market.db)

쌓는 곳: `SNAPSHOT_DB_PATH` (기본 `data/market.db`) 의 `premium_snapshots` 테이블.
코인마다 `--interval` 초에 한 줄. 자세한 컬럼 설명은 docs/data.md.
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from typing import Dict, List, Optional, Sequence

from kimp.config import Settings, load_settings
from kimp.db import PremiumSnapshot, make_session_factory
from kimp.market_data import MarketData, TopQuote, fetch_tops

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 60.0
STATUS_EVERY = 10  # 몇 번 기록할 때마다 현황 한 줄을 찍을지


def snapshot_rows(tops: Dict[str, TopQuote]) -> List[PremiumSnapshot]:
    """맨 앞 호가 묶음 → DB 에 넣을 기록 줄 (봇이 남기는 것과 같은 모양)"""
    rows = []
    for coin, t in tops.items():
        rows.append(PremiumSnapshot(
            coin=coin,
            mid_premium=t.mid_premium,
            entry_premium_top=t.entry_premium_top,
            exit_premium_top=t.exit_premium_top,
            usdt_krw=t.usdt_mid,
            usdt_spread_percent=t.usdt_spread_percent,
            fx_krw=t.fx_krw,
            tether_premium=t.tether_premium,
            site_premium=t.site_premium,
            funding_rate=t.funding_rate,
            next_funding_ms=t.next_funding_ms,
            funding_interval_hours=t.funding_interval_hours,
            upbit_mid_krw=t.upbit_mid,
            bybit_mid_usdt=t.bybit_mid,
        ))
    return rows


def pick_coins(settings: Settings, md: MarketData, coins: Optional[str], top: int, use_all: bool) -> List[str]:
    """
    수집할 코인 정하기

        --coins 를 적으면 그것 (양쪽 거래소에 다 있는 것만)
        --all / --top N 이면 거래대금 순
        아무것도 없으면 .env 의 COINS
    """
    if coins:
        picked = [c.strip().upper() for c in coins.split(",") if c.strip()]
        return md.filter_supported(picked)
    if use_all or top:
        from kimp.screen import list_universe
        from kimp.market_data import upbit_symbol

        universe = list_universe(md)
        tickers = md.upbit.fetch_tickers([upbit_symbol(c) for c in universe])
        volume = {c: float((tickers.get(upbit_symbol(c)) or {}).get("quoteVolume") or 0) for c in universe}
        universe.sort(key=lambda c: volume[c], reverse=True)
        return universe if use_all else universe[:top]
    return md.filter_supported(list(settings.coins))


def run(settings: Settings, coins: Sequence[str], interval_sec: float, stop: threading.Event,
        once: bool = False, max_hours: float = 0.0, md: Optional[MarketData] = None) -> int:
    """
    기록 루프. interval_sec 마다 코인 전체를 한 번씩 기록한다.

    Returns:
        적은 줄 수
    """
    md = md or MarketData(depth=5)  # 맨 앞 호가만 쓰므로 깊이는 얕게
    fx = None
    if settings.fixed_usd_krw:
        fx = settings.fixed_usd_krw
    sf = make_session_factory(settings.snapshot_db_path)
    started = time.monotonic()
    written = rounds = 0
    logger.info("수집 시작: 코인 %d개, %.0f초마다 기록 → %s", len(coins), interval_sec, settings.snapshot_db_path)
    logger.info("코인: %s", ", ".join(coins))
    while not stop.is_set():
        begin = time.monotonic()
        if fx is None or not settings.fixed_usd_krw:
            fx = _fx_or_none(fx)
        try:
            tops = fetch_tops(md, list(coins), fx_krw=fx)
            rows = snapshot_rows(tops)
            if rows:
                with sf() as session:
                    session.add_all(rows)
                    session.commit()
                written += len(rows)
            rounds += 1
            missing = len(coins) - len(tops)
            if rounds == 1 or rounds % STATUS_EVERY == 0:
                sample = next(iter(tops.values()), None)
                logger.info("기록 %d회 · 총 %d줄 (이번 %d코인%s)%s", rounds, written, len(tops),
                            f", 실패 {missing}" if missing else "",
                            f" | 예: {sample.coin} 김프 {sample.mid_premium:+.3f}%"
                            f" (진입 {sample.entry_premium_top:+.3f} / 청산 {sample.exit_premium_top:+.3f})"
                            if sample else "")
        except Exception:
            logger.exception("기록 실패 (다음 차례에 다시 시도)")
        if once:
            break
        if max_hours and (time.monotonic() - started) >= max_hours * 3600:
            logger.info("정해진 %g시간이 지나 종료합니다", max_hours)
            break
        stop.wait(max(0.0, interval_sec - (time.monotonic() - begin)))
    logger.info("수집 종료: %d회 · %d줄 (%s)", rounds, written, settings.snapshot_db_path)
    return written


def _fx_or_none(previous: Optional[float]) -> Optional[float]:
    """달러 환율 (표시용). 실패하면 이전 값을 그대로 쓴다 — 김프 계산에는 쓰지 않는다"""
    try:
        from kimp.fx import fetch_usd_krw_from_yfinance
        return fetch_usd_krw_from_yfinance()
    except Exception as exc:
        logger.debug("환율 조회 실패 (표시용): %s", exc)
        return previous


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="시세 수집기 — 김프·호가 기록만 쌓는다 (주문·포지션 없음). 봇(main.py)과 따로 돈다")
    parser.add_argument("--coins", default=None, help="코인 지정. 예: CAP,SEI (안 적으면 .env 의 COINS)")
    parser.add_argument("--all", action="store_true", help="양쪽 거래소에 다 있는 코인 전부 (약 240개)")
    parser.add_argument("--top", type=int, default=0, help="업비트 거래대금 상위 N개")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC,
                        help=f"기록 주기 (초, 기본 {DEFAULT_INTERVAL_SEC:g})")
    parser.add_argument("--once", action="store_true", help="한 번만 기록하고 종료 (확인용)")
    parser.add_argument("--hours", type=float, default=0.0, help="이 시간만 돌고 종료 (기본 무제한)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    settings = load_settings()
    md = MarketData(depth=5)
    coins = pick_coins(settings, md, args.coins, args.top, args.all)
    if not coins:
        print("수집할 코인이 없습니다 (양쪽 거래소에 다 있는 코인이어야 합니다)")
        return 2

    stop = threading.Event()

    def on_signal(*_):
        print()
        logger.info("종료 신호를 받았습니다 (지금까지 적은 기록은 남습니다)")
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    try:
        signal.signal(signal.SIGTERM, on_signal)
    except (AttributeError, ValueError):  # 윈도우에서 없을 수 있다
        pass
    run(settings, coins, args.interval, stop, once=args.once, max_hours=args.hours, md=md)
    print(f"{LF}쌓인 기록 보기: python -m kimp.webui (데이터 탭) 또는 python -m kimp.report")
    return 0


LF = chr(10)

if __name__ == "__main__":
    raise SystemExit(main())

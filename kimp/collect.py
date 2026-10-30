"""
시세 수집기 — 김프·호가 기록만 쌓는다 (주문도 포지션도 없다)

    python -m kimp.collect                   # .env 의 COINS
    python -m kimp.collect --coins CAP,SEI   # 코인 지정
    python -m kimp.collect --top 40          # 거래대금 상위 40개
    python -m kimp.collect --all             # 양쪽 거래소에 다 있는 코인 전부 (약 240개)
    python -m kimp.collect --once            # 한 번만 기록하고 종료 (확인용)
    python -m kimp.collect --hours 24        # 24시간 돌고 종료
    python -m kimp.collect --ws --top 40     # 웹소켓으로 0.5초마다 맨 앞 호가 김프 (tick_premiums, docs/filter.md)

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

**--ws 모드 (2026-10-30, docs/filter.md 1단계)**: REST 대신 웹소켓 호가(kimp/ws_feed.WsFeed)로 코인 수십 개를 보고,
**호가가 바뀔 때마다** 맨 앞 호가 김프를 계산해 0.01%p 넘게 달라진 코인만 `tick_premiums` 에 적는다 (60초 넘게 안 바뀌면
하트비트 한 줄). 처음엔 0.5초마다 훑었는데 0.5초보다 짧게 사는 틈(BLEND #42 2026-10-30 18:31 익절 신호, 수명 <0.5초)이
기록에 안 잡혀 이벤트 방식으로 바꿨다. 신호 신뢰도 필터의 상수를 '우리 코인' 이 아니라 넓은 표본으로 정하려는 자료라
기본은 거래대금 상위 40개를 권한다.
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from typing import Dict, List, Optional, Sequence

from kimp.config import Settings, load_settings
from kimp.db import PremiumSnapshot, TickPremium, make_session_factory
from kimp.market_data import MarketData, TopQuote, fetch_tops
from kimp.premium import premium_percent

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 60.0
STATUS_EVERY = 10  # 몇 번 기록할 때마다 현황 한 줄을 찍을지
TICK_INTERVAL_SEC = 0.5        # --ws 모드: 이 간격으로 전체를 한 번씩 훑는다 (이벤트를 놓쳐도 이 안에 잡힘) + 쓰기 묶음 간격
TICK_HEARTBEAT_SEC = 60.0      # 값이 안 바뀌어도 이 시간마다 한 줄 (조용함 ≠ 끊김)
TICK_STATUS_SEC = 60.0         # --ws 모드 현황 로그 간격
TICK_QUANT_PCT = 0.01          # 이 %p 단위로 같으면 '안 바뀜' 으로 본다 (바이비트 호가 1틱 흔들림까지 다 적지 않게)
TICK_FLUSH_SEC = 0.2           # 이벤트로 모인 줄을 DB 에 쓰는 묶음 간격


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
    # 달러 환율은 테더 프리미엄·사이트식 김프를 '표시' 하는 데만 쓴다 (봇 김프 계산에는 안 들어간다).
    # 고정값이 설정돼 있으면 조회하지 않는다.
    fx = None
    if settings.fixed_usd_krw:
        fx = settings.fixed_usd_krw
    # 봇 DB 가 아니라 수집 전용 DB(기본 data/market.db) 에 적는다 → 봇과 같이 돌려도 안 섞인다
    sf = make_session_factory(settings.snapshot_db_path)
    started = time.monotonic()
    written = rounds = 0
    logger.info("수집 시작: 코인 %d개, %.0f초마다 기록 → %s", len(coins), interval_sec, settings.snapshot_db_path)
    logger.info("코인: %s", ", ".join(coins))
    while not stop.is_set():
        begin = time.monotonic()  # 한 바퀴에 걸린 시간을 빼서 주기를 맞추려고 (아래 stop.wait)
        if fx is None or not settings.fixed_usd_krw:
            fx = _fx_or_none(fx)  # 실패하면 이전 값을 그대로 쓴다 (표시용이라 멈출 이유가 없다)
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
            # 한 바퀴가 실패해도 **절대 멈추지 않는다.** 며칠 돌려야 의미가 있는 작업이라,
            # 거래소 한 번 끊김으로 종료되면 그동안 쌓을 기록을 다 놓친다.
            logger.exception("기록 실패 (다음 차례에 다시 시도)")
        if once:
            break
        if max_hours and (time.monotonic() - started) >= max_hours * 3600:
            logger.info("정해진 %g시간이 지나 종료합니다", max_hours)
            break
        # 조회에 걸린 시간을 빼고 기다린다 → 코인이 많아도 주기가 밀리지 않는다.
        # stop.wait 을 쓰는 이유: sleep 과 달리 Ctrl+C 신호에 바로 깨어난다.
        stop.wait(max(0.0, interval_sec - (time.monotonic() - begin)))
    logger.info("수집 종료: %d회 · %d줄 (%s)", rounds, written, settings.snapshot_db_path)
    return written


def tick_row(coin: str, fb, ts_ms: int) -> TickPremium:
    """웹소켓 호가 세 장 → 초 단위 김프 기록 한 줄 (맨 앞 호가만 쓴다)"""
    up, by, us = fb.upbit, fb.bybit, fb.usdt
    return TickPremium(
        coin=coin, ts_ms=ts_ms,
        entry_premium_top=premium_percent(up.best_ask, by.best_bid, us.best_bid),
        exit_premium_top=premium_percent(up.best_bid, by.best_ask, us.best_ask),
        mid_premium=premium_percent((up.best_bid + up.best_ask) / 2, (by.best_bid + by.best_ask) / 2,
                                    (us.best_bid + us.best_ask) / 2),
        upbit_bid=up.best_bid, upbit_ask=up.best_ask, bybit_bid=by.best_bid, bybit_ask=by.best_ask,
        usdt_bid=us.best_bid, usdt_ask=us.best_ask, skew_ms=getattr(fb, "skew_ms", None),
    )


class TickDedup:
    """코인마다 마지막으로 적은 (진입·청산 김프, 시각) 을 기억해 바뀐 것만 적게 한다 (하트비트 포함)"""

    def __init__(self, heartbeat_sec: float = TICK_HEARTBEAT_SEC):
        self.heartbeat_ms = heartbeat_sec * 1000
        self._last: Dict[str, tuple] = {}

    def should_write(self, row: TickPremium) -> bool:
        # 0.005%p 단위로 양자화: 필터가 보는 변화(평소 변화폭 MAD 의 몇 배, 0.02%p 이상)보다 훨씬 작은 흔들림은
        # 기록하지 않는다. 40코인 3일에 수 GB 가 되는 것을 막는다 (docs/filter.md 1단계)
        key = (round(row.entry_premium_top / TICK_QUANT_PCT), round(row.exit_premium_top / TICK_QUANT_PCT))
        prev = self._last.get(row.coin)
        if prev is not None and prev[0] == key and row.ts_ms - prev[1] < self.heartbeat_ms:
            return False
        self._last[row.coin] = (key, row.ts_ms)
        return True


def run_ws(settings: Settings, coins: Sequence[str], interval_sec: float, stop: threading.Event,
           max_hours: float = 0.0, feed=None, now_ms=None, max_rounds: int = 0) -> int:
    """
    --ws 모드 기록 루프: 호가가 바뀔 때마다(feed.on_update) 그 코인의 김프를 다시 계산해 달라졌으면 적는다.
    interval_sec 마다 전체를 한 번씩 훑어 이벤트를 놓쳐도 잡고, 60초 하트비트로 '조용함' 을 남긴다.

    소켓 스레드는 콜백에서 코인 이름만 집합에 넣고(빨리), 이 루프가 꺼내서 계산·기록한다. DB 쓰기는 TICK_FLUSH_SEC 묶음.

    Args:
        feed: 테스트용 가짜 WsFeed (books(coin) 만 있으면 된다; on_update 속성이 있으면 이벤트도 받는다). None 이면 진짜 WsFeed
        now_ms: 테스트용 시각 함수. None 이면 time.time
        max_rounds: 0 이면 무제한 (테스트용 — 전체 훑기 횟수)
    Returns:
        적은 줄 수
    """
    from kimp.ws_feed import WsFeed
    own_feed = feed is None
    if own_feed:
        feed = WsFeed(coins, max_age_ms=settings.quote_ws_max_age_ms)
    pending: set = set()
    lock = threading.Lock()
    wake = threading.Event()

    def on_update(coin: str) -> None:
        with lock:
            pending.add(coin)
        wake.set()

    if hasattr(feed, "on_update"):
        feed.on_update = on_update
    if own_feed:
        feed.start()
        if not feed.wait_ready(timeout=15.0):
            logger.warning("웹소켓 호가가 15초 안에 다 들어오지 않았습니다 (들어온 코인부터 적습니다): %s", feed.status_text())
    clock = now_ms or (lambda: int(time.time() * 1000))
    sf = make_session_factory(settings.snapshot_db_path)
    dedup = TickDedup()
    started = time.monotonic()
    written = rounds = events = 0
    last_status = last_flush = time.monotonic()
    last_sweep = -1e18          # 시작하자마자 전체를 한 번 훑는다
    buffer: List[TickPremium] = []
    logger.info("수집 시작 (웹소켓): 코인 %d개, 호가가 바뀔 때마다 + %.2g초마다 전체 확인, 바뀐 것만 기록 → %s (tick_premiums)",
                len(coins), interval_sec, settings.snapshot_db_path)
    logger.info("코인: %s", ", ".join(coins))

    def collect(targets, ts) -> int:
        n = 0
        for coin in targets:
            fb = feed.books(coin)
            if fb is None:
                continue
            row = tick_row(coin, fb, ts)
            if dedup.should_write(row):
                buffer.append(row)
                n += 1
        return n

    try:
        while not stop.is_set():
            try:
                wake.wait(TICK_FLUSH_SEC)
                wake.clear()
                with lock:
                    todo, pending = pending, set()
                ts = clock()
                if todo:
                    events += len(todo)
                    collect(coins if "*" in todo else [c for c in coins if c in todo], ts)
                now = time.monotonic()
                if now - last_sweep >= interval_sec:
                    last_sweep = now
                    collect(coins, ts)
                    rounds += 1
                if buffer and (now - last_flush >= TICK_FLUSH_SEC or len(buffer) >= 500):
                    with sf() as session:
                        session.add_all(buffer)
                        session.commit()
                    written += len(buffer)
                    buffer = []
                    last_flush = now
                if now - last_status >= TICK_STATUS_SEC:
                    last_status = now
                    fresh = len(feed.fresh_coins()) if hasattr(feed, "fresh_coins") else len(coins)
                    logger.info("기록 총 %d줄 (호가 변화 %d건 처리) | 신선한 코인 %d/%d%s", written, events, fresh, len(coins),
                                f" | {feed.status_text()}" if hasattr(feed, "status_text") else "")
            except Exception:
                logger.exception("기록 실패 (다음 차례에 다시 시도)")
                buffer = []
            if max_rounds and rounds >= max_rounds:
                break
            if max_hours and (time.monotonic() - started) >= max_hours * 3600:
                logger.info("정해진 %g시간이 지나 종료합니다", max_hours)
                break
    finally:
        if buffer:
            try:
                with sf() as session:
                    session.add_all(buffer)
                    session.commit()
                written += len(buffer)
            except Exception:
                logger.exception("마지막 묶음 기록 실패")
        if own_feed:
            feed.stop()
    logger.info("수집 종료 (웹소켓): 전체 확인 %d회 · %d줄 (%s)", rounds, written, settings.snapshot_db_path)
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
    """
    명령줄 실행 (python -m kimp.collect)

    Ctrl+C 로 멈춰도 **그때까지 적은 기록은 남는다** (한 바퀴마다 커밋하기 때문).
    봇과 같이 돌려도 된다 — DB 가 다르고(data/market.db) 주문을 내지 않는다.
    """
    parser = argparse.ArgumentParser(
        description="시세 수집기 — 김프·호가 기록만 쌓는다 (주문·포지션 없음). 봇(main.py)과 따로 돈다")
    parser.add_argument("--coins", default=None, help="코인 지정. 예: CAP,SEI (안 적으면 .env 의 COINS)")
    parser.add_argument("--all", action="store_true", help="양쪽 거래소에 다 있는 코인 전부 (약 240개)")
    parser.add_argument("--top", type=int, default=0, help="업비트 거래대금 상위 N개")
    parser.add_argument("--interval", type=float, default=None,
                        help=f"기록 주기 (초, 기본 {DEFAULT_INTERVAL_SEC:g}, --ws 면 {TICK_INTERVAL_SEC:g})")
    parser.add_argument("--ws", action="store_true",
                        help="웹소켓 호가로 초 단위 김프 시계열(tick_premiums) 기록 (docs/filter.md). 바뀐 값만 적는다")
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
        """Ctrl+C / 종료 신호 → 루프에게 '그만' 을 알린다 (중간에 끊지 않고 한 바퀴를 마친다)"""
        print()
        logger.info("종료 신호를 받았습니다 (지금까지 적은 기록은 남습니다)")
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    try:
        signal.signal(signal.SIGTERM, on_signal)
    except (AttributeError, ValueError):  # 윈도우에서 없을 수 있다
        pass
    if args.ws:
        run_ws(settings, coins, args.interval or TICK_INTERVAL_SEC, stop, max_hours=args.hours)
    else:
        run(settings, coins, args.interval or DEFAULT_INTERVAL_SEC, stop, once=args.once, max_hours=args.hours, md=md)
    print(f"{LF}쌓인 기록 보기: python -m kimp.webui (데이터 탭) 또는 python -m kimp.report")
    return 0


LF = chr(10)

if __name__ == "__main__":
    raise SystemExit(main())

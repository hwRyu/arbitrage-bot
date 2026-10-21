"""
펀딩비 수익 전략: 코인 순위 / 과거 시뮬레이션

실행:
    python3 -m kimp.carry_backtest screen                   # 최근 30일, 펀딩비 전략이 잘 통했을 코인 순위
    python3 -m kimp.carry_backtest screen --days 60 --top 30
    python3 -m kimp.carry_backtest run --from 2026-09-22    # .env 설정 그대로 과거 기간 돌려보기 (끝 = 지금)
    python3 -m kimp.carry_backtest run --from 2026-09-22 --coins AKT,TAO,MOODENG

어떻게 돌리나
------------------------------------------------------------------
- 실시간과 같은 CarryEngine / CarryBroker 를 1시간봉 따라 1시간씩 돌린다
- 데이터: 바이비트 현물·무기한 1시간봉 + 펀딩비 정산 기록 (data/history.db 에 저장, 모자란 구간만 받음)
- 과거 호가창은 없어서 '지금' 호가 간격을 쓴다 (현물·선물 각각, 최소 0.02%)
- 진입 판단의 '지금 펀딩비율' 은 직전 정산값으로 대신한다 (과거 예고값은 못 받음)
- 강제청산은 1시간봉 고가로 확인

screen 점수
------------------------------------------------------------------
- 코인마다 따로 '한 번에 포지션 1개, 1,000 USDT' 로 돌린 결과
- 자본 대비 월 수익 = 순손익 ÷ (포지션 금액 × (1 + 1/레버리지)) × 30일 ÷ 기간
    예) 1,000 USDT, 2배 → 묶이는 돈 1,500. 30일 순손익 +9 → 자본 대비 +0.6%/월
- 점수 = min(전체 기간 월 수익, 뒤 절반 기간 월 수익)  (최근에도 통했나)
- 주의: 코인은 '지금' 거래대금으로 고른다. 그 사이 상장폐지된 코인은 빠져 있어서 실제보다 좋게 나올 수 있다
"""
from __future__ import annotations

import argparse
import bisect
import logging
import statistics
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from kimp.carry import (
    CarryBroker,
    CarryEngine,
    CarryQuote,
    CarryStats,
    eligible_coins,
    make_stats,
    perp_symbol,
    spot_symbol,
)
from kimp.config import Settings, load_settings
from kimp.db import CarryPosition, PositionStatus, make_session_factory
from kimp.history import BYBIT, HistoryStore, ccxt_candle_fetcher, ccxt_funding_fetcher, make_history_session_factory
from kimp.premium import OrderBook
from kimp.simulate import SimClock, parse_kst

logger = logging.getLogger(__name__)

BAR_MS = 3_600_000  # 1시간봉
DAY_MS = 86_400_000
SPOT_1H = "bybit_spot@1h"
PERP_1H = f"{BYBIT}@1h"
MIN_SPREAD_PERCENT = 0.02  # 호가 간격 최소 (현물·선물 각각)
ASSUMED_DEPTH = 1e15
FILL_FORWARD_BARS = 3  # 캔들이 빠진 시간은 3시간까지 직전 값으로


class HistoricalCarryMarket:
    """
    과거 1시간봉 + 펀딩비 기록으로 CarryEngine 이 쓸 시세를 만들어 준다

    시각 t 에는 '이미 끝난 봉' 의 종가만 보인다. 예) 10:20 → 09:00~10:00 봉 종가
    """

    def __init__(self, settings: Settings, clock: SimClock, coins: List[str],
                 spot: Dict[str, Dict[int, float]], perp: Dict[str, Dict[int, float]],
                 perp_high: Dict[str, Dict[int, float]], funding: Dict[str, List[Tuple[int, float]]],
                 spread: Dict[str, Tuple[float, float]]):
        self.s = settings
        self.clock = clock
        self.coins = coins
        self.spot, self.perp, self.perp_high = spot, perp, perp_high
        self.funding = funding
        self.spread = spread

    def _bar(self) -> int:
        """지금 보이는 마지막 봉의 시작 시각"""
        return int(self.clock() * 1000) // BAR_MS * BAR_MS - BAR_MS

    @staticmethod
    def _at(series: Dict[int, float], bar: int) -> Optional[float]:
        for k in range(FILL_FORWARD_BARS + 1):
            v = series.get(bar - k * BAR_MS)
            if v is not None:
                return v
        return None

    @staticmethod
    def _book(price: float, spread_percent: float) -> OrderBook:
        half = spread_percent / 100 / 2
        return OrderBook(bids=[(price * (1 - half), ASSUMED_DEPTH)], asks=[(price * (1 + half), ASSUMED_DEPTH)])

    def stats(self, now_ms: int, include=()) -> Dict[str, CarryStats]:
        return {c: make_stats(self.funding.get(c, []), now_ms, self.s) for c in dict.fromkeys(list(self.coins) + list(include))}

    def funding_since(self, coin: str, after_ms: int, now_ms: int) -> List[Tuple[int, float]]:
        hist = self.funding.get(coin, [])
        lo = bisect.bisect_right(hist, (after_ms, float("inf")))
        hi = bisect.bisect_right(hist, (now_ms, float("inf")))
        return hist[lo:hi]

    def quote(self, coin: str) -> Optional[CarryQuote]:
        bar = self._bar()
        s, p = self._at(self.spot.get(coin, {}), bar), self._at(self.perp.get(coin, {}), bar)
        if s is None or p is None:
            return None
        ss, ps = self.spread.get(coin, (0.05, 0.05))
        return CarryQuote(coin=coin, spot=self._book(s, ss), perp=self._book(p, ps),
                          perp_high=self.perp_high.get(coin, {}).get(bar))

    def round_qty(self, coin: str, qty: float) -> float:
        return qty

    def min_qty(self, coin: str) -> float:
        return 0.0


@dataclass
class CarryTrade:
    """거래 하나 결과 (끝까지 들고 있으면 reason = OPEN, 마지막 순간 청산 가정)"""

    coin: str
    opened_ms: int
    closed_ms: Optional[int]
    reason: str
    notional: float
    capital: float
    funding: float
    fees: float
    net: float
    funding_count: int

    @property
    def days(self) -> float:
        return ((self.closed_ms or 0) - self.opened_ms) / DAY_MS


@dataclass
class CarryRunResult:
    start_ms: int
    end_ms: int
    capital: float
    equity_end: float
    trades: List[CarryTrade] = field(default_factory=list)
    bars: int = 0
    bars_in_market: int = 0  # 포지션을 하나라도 들고 있던 봉 수

    @property
    def days(self) -> float:
        return (self.end_ms - self.start_ms) / DAY_MS

    @property
    def net(self) -> float:
        return self.equity_end - self.capital

    @property
    def return_percent(self) -> float:
        return self.net / self.capital * 100


def _ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def run_carry(settings: Settings, market: HistoricalCarryMarket, clock: SimClock,
              start_ms: int, end_ms: int) -> CarryRunResult:
    """CarryEngine 을 1시간씩 돌린다 (메모리 DB, 실시간 DB 는 안 건드림)"""
    sf = make_session_factory(":memory:")
    broker = CarryBroker(settings, now_fn=lambda: datetime.fromtimestamp(clock(), timezone.utc))
    engine = CarryEngine(settings, market, broker, sf, clock=clock)
    clock.now_sec = start_ms / 1000
    engine.startup()
    res = CarryRunResult(start_ms=start_ms, end_ms=end_ms, capital=settings.carry_capital_usdt, equity_end=0.0)
    for bar in range(start_ms, end_ms, BAR_MS):
        clock.now_sec = (bar + BAR_MS) / 1000
        engine.step()
        res.bars += 1
        with sf() as session:
            if broker.open_positions(session):
                res.bars_in_market += 1
    with sf() as session:
        quotes = {c: q for c in market.coins if (q := market.quote(c))}
        res.equity_end = broker.equity(session, quotes)
        for pos in session.query(CarryPosition).order_by(CarryPosition.id):
            if pos.status == PositionStatus.CLOSED:
                net, fees, closed, reason = pos.net_pnl_usdt, pos.total_fee_usdt, _ms(pos.closed_at), pos.exit_reason
            else:
                q = quotes.get(pos.coin)
                val = broker.value(pos, q) if q else None
                net = val.net_usdt if val else 0.0
                fees = pos.entry_spot_fee + pos.entry_perp_fee + (val.spot_fee + val.perp_fee if val else 0.0)
                closed, reason = end_ms, "OPEN"
            res.trades.append(CarryTrade(coin=pos.coin, opened_ms=_ms(pos.opened_at), closed_ms=closed, reason=reason,
                                         notional=pos.notional_usdt, capital=pos.capital_used_usdt,
                                         funding=pos.funding_usdt, fees=fees, net=net,
                                         funding_count=pos.funding_count))
    return res


# ======================================================================
# 데이터
# ======================================================================
def _exchange():
    import ccxt

    ex = ccxt.bybit({"enableRateLimit": True})
    ex.load_markets()
    return ex


def pick_universe(ex, min_perp_usdt: float, min_spot_usdt: float, max_coins: int) -> Tuple[List[str], Dict[str, float]]:
    """현물·무기한 둘 다 있고 24시간 거래대금이 충분한 코인 (무기한 거래대금 높은 순)"""
    coins = eligible_coins(ex.markets)
    perp_t = ex.fetch_tickers([perp_symbol(c) for c in coins])
    spot_t = ex.fetch_tickers([spot_symbol(c) for c in coins])
    vol = {c: float((perp_t.get(perp_symbol(c)) or {}).get("quoteVolume") or 0) for c in coins}
    svol = {c: float((spot_t.get(spot_symbol(c)) or {}).get("quoteVolume") or 0) for c in coins}
    ok = [c for c in coins if vol[c] >= min_perp_usdt and svol[c] >= min_spot_usdt]
    return sorted(ok, key=lambda c: vol[c], reverse=True)[:max_coins], vol


def live_spreads(ex, coins: List[str]) -> Dict[str, Tuple[float, float]]:
    """지금 호가 간격 (현물 %, 선물 %), 각각 최소 0.02%"""
    out = {}
    for c in coins:
        sp = []
        for sym in (spot_symbol(c), perp_symbol(c)):
            try:
                ob = ex.fetch_order_book(sym, limit=5)
                b, a = ob["bids"][0][0], ob["asks"][0][0]
                sp.append(max(MIN_SPREAD_PERCENT, (a - b) / ((a + b) / 2) * 100))
            except Exception:
                sp.append(0.1)
        out[c] = (sp[0], sp[1])
    return out


def load_data(store: HistoryStore, ex, coins: List[str], start_ms: int, end_ms: int, lookback_ms: int):
    """1시간봉(현물·선물)과 펀딩비 기록을 받고 읽어온다 (진입 판단용으로 시작 전 lookback 만큼 더)"""
    a = start_ms - lookback_ms
    spot, perp, high, funding = {}, {}, {}, {}
    for i, c in enumerate(coins, 1):
        print(f"  [{i}/{len(coins)}] {c}        ", end="\r")
        try:
            store.ensure_candles(SPOT_1H, c, a, end_ms, ccxt_candle_fetcher(ex, spot_symbol(c), "1h"), step_ms=BAR_MS)
            store.ensure_candles(PERP_1H, c, a, end_ms, ccxt_candle_fetcher(ex, perp_symbol(c), "1h"), step_ms=BAR_MS)
            store.ensure_funding(c, a, end_ms, ccxt_funding_fetcher(ex, perp_symbol(c)))
        except Exception as exc:
            print(f"\n  {c} 데이터 받기 실패 (건너뜀): {exc}")
            continue
        spot[c] = store.load_closes(SPOT_1H, c, a, end_ms)
        perp[c] = store.load_closes(PERP_1H, c, a, end_ms)
        high[c] = store.load_highs(PERP_1H, c, a, end_ms)
        funding[c] = [r for r in store.load_funding(c, a, end_ms) if r[0] <= end_ms]
    print()
    return spot, perp, high, funding


# ======================================================================
# screen: 코인별 순위
# ======================================================================
@dataclass
class CarryScore:
    coin: str
    full: CarryRunResult
    recent: CarryRunResult
    avg_daily: float  # 기간 전체 하루 평균 펀딩 (%)
    positive_share: float  # 정산 중 + 였던 비율
    worst_day: float  # 하루 펀딩 합이 가장 나빴던 날 (%)
    perp_volume: float
    spread: Tuple[float, float]
    leverage: float

    def monthly_on_capital(self, r: CarryRunResult) -> float:
        cap = 1000 * (1 + 1 / self.leverage)
        return r.net / cap * 100 * 30 / r.days if r.days > 0 else 0.0

    @property
    def monthly(self) -> float:
        return self.monthly_on_capital(self.full)

    @property
    def recent_monthly(self) -> float:
        return self.monthly_on_capital(self.recent)

    @property
    def score(self) -> float:
        return min(self.monthly, self.recent_monthly)


def funding_profile(hist: Sequence[Tuple[int, float]], start_ms: int, end_ms: int) -> Tuple[float, float, float]:
    """(하루 평균 %, + 비율, 가장 나쁜 하루 %)"""
    win = [(ts, r) for ts, r in hist if start_ms < ts <= end_ms]
    if not win:
        return 0.0, 0.0, 0.0
    days = (end_ms - start_ms) / DAY_MS
    by_day: Dict[int, float] = {}
    for ts, r in win:
        by_day[ts // DAY_MS] = by_day.get(ts // DAY_MS, 0.0) + r * 100
    return (sum(r for _, r in win) * 100 / days, sum(1 for _, r in win if r > 0) / len(win), min(by_day.values()))


def screen_main(args, settings: Settings) -> int:
    ex = _exchange()
    print("바이비트 현물·무기한 둘 다 있는 코인 확인 중...")
    coins, vol = pick_universe(ex, args.min_perp_usdt, args.min_spot_usdt, args.max_coins)
    print(f"거래대금 기준 통과 {len(coins)}개 (무기한 ≥ {args.min_perp_usdt / 1e6:g}M, 현물 ≥ {args.min_spot_usdt / 1e6:g}M USDT)")
    end_ms = int(time.time() * 1000) // BAR_MS * BAR_MS
    start_ms = end_ms - int(args.days * DAY_MS)
    store = HistoryStore(make_history_session_factory(settings.history_db_path))
    print("1시간봉·펀딩비 기록 받는 중...")
    lookback = int(settings.carry_entry_lookback_hours * 3_600_000)
    spot, perp, high, funding = load_data(store, ex, coins, start_ms, end_ms, lookback)
    print("지금 호가 간격 확인 중...")
    spreads = live_spreads(ex, list(spot))

    one = replace(settings, carry_max_positions=1, carry_position_size_usdt=1000.0, carry_capital_usdt=1e9)
    mid_ms = start_ms + (end_ms - start_ms) // 2 // BAR_MS * BAR_MS
    scores = []
    for c in spot:
        def run(a: int) -> CarryRunResult:
            clock = SimClock(a / 1000)
            m = HistoricalCarryMarket(one, clock, [c], spot, perp, high, funding, spreads)
            return run_carry(one, m, clock, a, end_ms)

        full, recent = run(start_ms), run(mid_ms)
        avg, pos_share, worst = funding_profile(funding[c], start_ms, end_ms)
        scores.append(CarryScore(c, full, recent, avg, pos_share, worst, vol.get(c, 0), spreads[c],
                                 settings.carry_leverage))
    scores.sort(key=lambda s: (s.score, s.monthly), reverse=True)

    print()
    print(f"최근 {args.days:g}일 · 1시간봉 · {len(scores)}개 코인 · 진입 72h·24h 평균 ≥ {settings.carry_entry_min_daily_percent:g}%/일 · "
          f"청산 24h 평균 < {settings.carry_exit_below_daily_percent:g}%/일 · {settings.carry_leverage:g}배 · "
          f"현물 수수료 {settings.carry_spot_fee_rate * 100:g}% · 선물 {settings.fees.bybit_taker_fee_rate * 100:g}%")
    print("점수 = min(전체 월수익, 뒤 절반 월수익) · 월수익 = 묶인 자본(현물 + 증거금) 대비 % · 코인마다 1,000 USDT 하나씩")
    print()
    print(f"{'순위':>3} {'코인':<9}{'점수':>6} {'전체/월':>7} {'최근/월':>7} {'거래':>4} {'보유%':>5} "
          f"{'펀딩합':>7} {'비용':>6} │ {'펀딩/일':>7} {'+비율':>5} {'최악일':>7} │ {'현물호가':>7} {'선물호가':>7} {'거래대금(M$)':>11}")
    for i, s in enumerate(scores[:args.top], 1):
        f = s.full
        in_mkt = f.bars_in_market / f.bars * 100 if f.bars else 0
        print(f"{i:>3}. {s.coin:<9}{s.score:>+6.2f} {s.monthly:>+7.2f} {s.recent_monthly:>+7.2f} {len(f.trades):>4} "
              f"{in_mkt:>4.0f}% {sum(t.funding for t in f.trades):>+7.2f} {sum(t.fees for t in f.trades):>6.2f} │ "
              f"{s.avg_daily:>+7.4f} {s.positive_share * 100:>4.0f}% {s.worst_day:>+7.3f} │ "
              f"{s.spread[0]:>7.3f} {s.spread[1]:>7.3f} {s.perp_volume / 1e6:>11,.1f}")
    print("""
열 설명
  전체/월 · 최근/월  기간 전체 / 뒤 절반에 전략을 돌린 순손익을 '묶인 자본 대비 한 달' 로 (%)
  거래 · 보유%       닫거나 들고 있는 거래 수 / 기간 중 포지션을 들고 있던 시간 비율
  펀딩합 · 비용      받은 펀딩비 합 / 수수료 합 (1,000 USDT 포지션 기준 USDT)
  펀딩/일 · +비율    기간 전체 하루 평균 펀딩비율 (%), 정산 중 + 였던 비율 / 최악일: 하루 펀딩 합이 가장 나빴던 날
""")
    picks = [s.coin for s in scores[:args.top] if s.score > 0][:settings.carry_max_positions * 3]
    if picks:
        print("# .env 에 붙여넣기 예시 (점수 + 인 상위 코인. 비워두면 자동 선택)")
        print("CARRY_COINS=" + ",".join(picks))
    return 0


# ======================================================================
# run: 포트폴리오 시뮬레이션
# ======================================================================
def run_main(args, settings: Settings) -> int:
    ex = _exchange()
    end_now = int(time.time() * 1000) // BAR_MS * BAR_MS
    start_ms = parse_kst(args.start) // BAR_MS * BAR_MS
    end_ms = min(parse_kst(args.end), end_now) // BAR_MS * BAR_MS if args.end else end_now
    if start_ms >= end_ms:
        print("시작이 끝보다 늦거나 같습니다.")
        return 2
    if args.coins:
        coins = [c.strip().upper() for c in args.coins.split(",")]
    elif settings.carry_coins:
        coins = settings.carry_coins
    else:
        coins, _ = pick_universe(ex, settings.carry_min_volume_usdt, args.min_spot_usdt, args.max_coins)
        print(f"CARRY_COINS 가 비어 있어서 지금 거래대금 기준 {len(coins)}개 코인으로 돌립니다 (자동 선택 흉내)")
    store = HistoryStore(make_history_session_factory(settings.history_db_path))
    print("1시간봉·펀딩비 기록 받는 중...")
    lookback = int(settings.carry_entry_lookback_hours * 3_600_000)
    spot, perp, high, funding = load_data(store, ex, coins, start_ms, end_ms, lookback)
    spreads = live_spreads(ex, list(spot))
    clock = SimClock(start_ms / 1000)
    market = HistoricalCarryMarket(settings, clock, list(spot), spot, perp, high, funding, spreads)
    if args.verbose:
        logging.getLogger("kimp.carry").setLevel(logging.INFO)
    r = run_carry(settings, market, clock, start_ms, end_ms)
    print(format_run(r, settings))
    return 0


def _kst(ms: int) -> str:
    from kimp.simulate import KST

    return datetime.fromtimestamp(ms / 1000, KST).strftime("%Y-%m-%d %H:%M")


def format_run(r: CarryRunResult, s: Settings) -> str:
    closed = [t for t in r.trades if t.reason != "OPEN"]
    open_ = [t for t in r.trades if t.reason == "OPEN"]
    lines = [
        "=" * 76,
        f"펀딩비 전략 시뮬레이션  {_kst(r.start_ms)} ~ {_kst(r.end_ms)} (한국 시간, {r.days:.1f}일)",
        "=" * 76,
        f"자본 {r.capital:,.0f} USDT · 포지션 {s.carry_size_usdt():,.0f} USDT × 최대 {s.carry_max_positions}개 · "
        f"{s.carry_leverage:g}배 · 진입 ≥ {s.carry_entry_min_daily_percent:g}%/일 · 청산 < {s.carry_exit_below_daily_percent:g}%/일",
        "",
        f"■ 총 손익: {r.net:+,.2f} USDT  (자본 대비 {r.return_percent:+.3f}%, 한 달로 치면 {r.return_percent * 30 / r.days:+.3f}%)",
        f"  닫은 거래 {len(closed)}건 합계 {sum(t.net for t in closed):+,.2f} / 들고 있는 {len(open_)}개 평가 {sum(t.net for t in open_):+,.2f}",
        f"  펀딩비 받은 합계 {sum(t.funding for t in r.trades):+,.2f} / 수수료 {sum(t.fees for t in r.trades):,.2f} USDT · "
        f"포지션 들고 있던 시간 {r.bars_in_market / max(1, r.bars) * 100:.0f}%",
        "",
        "■ 거래",
    ]
    for t in r.trades:
        lines.append(f"  {t.coin:<9}{_kst(t.opened_ms)} → {(_kst(t.closed_ms) if t.reason != 'OPEN' else '보유 중'):<16} "
                     f"{t.days:5.1f}일 펀딩 {t.funding_count:3}번 {t.funding:+7.2f} 비용 {t.fees:5.2f} "
                     f"→ {t.net:+7.2f} USDT ({t.net / t.notional * 100:+.2f}%) {t.reason}")
    lines.append("")
    lines.append("※ 과거 호가창이 없어 지금 호가 간격을 쓰고, 진입 판단의 '지금 펀딩비율' 은 직전 정산값으로 대신했다.")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="펀딩비 수익 전략: 코인 순위 / 과거 시뮬레이션")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("screen", help="코인 순위")
    p.add_argument("--days", type=float, default=30)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--max-coins", type=int, default=120, help="거래대금 상위 몇 개까지 볼지 (기본 120)")
    p.add_argument("--min-perp-usdt", type=float, default=5e6, help="무기한 24시간 거래대금 최소 (기본 5M)")
    p.add_argument("--min-spot-usdt", type=float, default=5e5, help="현물 24시간 거래대금 최소 (기본 0.5M)")
    r = sub.add_parser("run", help="과거 기간 시뮬레이션")
    r.add_argument("--from", dest="start", required=True, help="시작 (한국 시간) 예: 2026-09-22")
    r.add_argument("--to", dest="end", default=None, help="끝 (안 적으면 지금)")
    r.add_argument("--coins", default=None, help="코인 (안 적으면 CARRY_COINS, 그것도 없으면 거래대금 상위)")
    r.add_argument("--max-coins", type=int, default=60)
    r.add_argument("--min-spot-usdt", type=float, default=5e5)
    r.add_argument("--verbose", action="store_true", help="진입/청산 로그 보기")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s - %(message)s")
    settings = load_settings()
    return screen_main(args, settings) if args.cmd == "screen" else run_main(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())

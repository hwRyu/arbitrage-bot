"""
거래소 시세 가져오기 (업비트 원화 마켓, 바이비트 USDT 무기한)

API 키가 필요 없는 '공개 시세' 만 쓴다. 주문은 하지 않는다.

old 봇과 달라진 점
------------------------------------------------------------------
- old: 페이퍼 모드에서 시세를 볼 때마다 거래소 연결을 새로 만들었다 → 느리고 요청 제한(429)에 걸림
  new: 연결은 한 번만 만들고, 마켓 정보도 시작할 때 한 번만 받는다
- old: 업비트 코인 하나당 요청 1번
  new: 업비트는 여러 코인 호가를 요청 1번에 받는다
- old: 최근 체결가(last)로 비교
  new: 호가창(bids/asks)을 받아 실제 체결될 가격으로 비교
"""
from __future__ import annotations

import logging
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional

import ccxt

from kimp.premium import OrderBook, Quote

logger = logging.getLogger(__name__)

# 같은 조회 실패를 매 스캔(10초)마다 찍으면 거래소 점검 한 번에 텔레그램이 수백 통 온다.
# 처음 한 번만 그 레벨로 알리고, 그 뒤 이 시간 동안은 DEBUG 로 낮춘다. 성공하면 '복구' 를 한 번 알린다.
REPEAT_COOLDOWN_SEC = 300.0
_fail_since: Dict[str, float] = {}   # 키 → 처음 실패한 시각 (monotonic)
_last_told: Dict[str, float] = {}    # 키 → 마지막으로 알린 시각


def _log_repeat(key: str, level: int, message: str) -> None:
    """되풀이되는 조회 실패를 REPEAT_COOLDOWN_SEC 에 한 번만 알린다"""
    now = time.monotonic()
    _fail_since.setdefault(key, now)
    if now - _last_told.get(key, -1e18) >= REPEAT_COOLDOWN_SEC:
        _last_told[key] = now
        logger.log(level, message)
    else:
        logger.debug("(반복, 알림 생략) %s", message)


def _log_recovered(key: str, what: str) -> None:
    """막혔던 조회가 다시 되면 얼마나 막혔었는지 한 번 알린다 (알렸던 적이 있을 때만)"""
    since = _fail_since.pop(key, None)
    _last_told.pop(key, None)
    if since is not None:
        logger.warning("[%s 복구] %.0f초 동안 안 됐음", what, time.monotonic() - since)


# 업비트 USDT/KRW: 김프 계산 기준이자, 진입/청산 때 실제로 USDT 를 사고파는 마켓
USDT_KRW_SYMBOL = "USDT/KRW"

# 바이비트 호가를 코인별로 동시에 받을 때 쓰는 공개 연결 수 (ccxt 객체 하나를 두 스레드가 같이 쓰면 안 된다)
BYBIT_PARALLEL = 4


def upbit_symbol(coin: str) -> str:
    """업비트 원화 마켓 이름. 예) XRP → 'XRP/KRW'"""
    return f"{coin}/KRW"


def bybit_symbol(coin: str) -> str:
    """바이비트 USDT 무기한 이름. 예) XRP → 'XRP/USDT:USDT'"""
    return f"{coin}/USDT:USDT"


@dataclass(frozen=True)
class FundingInfo:
    """
    바이비트 펀딩비 정보

    펀딩비란? 무기한 선물 가격을 현물 가격에 붙여두려고, 롱과 숏이 정해진 시각마다 서로 주고받는 돈.

    Attributes:
        rate: 다음 정산 때 비율 (0.0001 = 0.01%). +면 롱이 숏에게 줌 → 우리(숏)는 받음
        next_funding_ms: 다음 정산 시각
        mark_price: 기준 가격 (펀딩비 = 숏 개수 × 이 가격 × 비율)
        interval_hours: 정산 주기 (시간). 코인마다 1 / 4 / 8 시간 등으로 다르다. 모르면 None
    """

    rate: float
    next_funding_ms: int
    mark_price: float
    interval_hours: Optional[float] = None


class MarketData:
    """
    양쪽 거래소 시세 조회기

    사용법:
        md = MarketData(depth=30)
        coins = md.filter_supported(["XRP", "SOL"])   # 양쪽에 다 있는 코인만 남김
        quotes = md.fetch_quotes(coins, fx_krw=1325)  # 매 스캔마다 호출 (환율은 표시용, 없어도 됨)

    시세를 어디서 받나 (QUOTE_SOURCE):
        feed 가 없으면 REST (스캔마다 요청). feed(kimp/ws_feed.WsFeed) 가 붙어 있으면 소켓으로 밀려 온 호가를
        먼저 쓰고, 없거나 묵은 코인만 REST 로 받는다. Quote 를 만드는 곳은 여기 한 군데라 엔진은 출처를 모른다.
    """

    def __init__(self, depth: int = 30, feed=None):
        """
        Args:
            depth: 호가창을 몇 줄까지 받을지 (REST)
            feed: 웹소켓 호가 (WsFeed). books(coin) 만 쓴다. None 이면 REST 만
        """
        self.depth = depth
        self.feed = feed
        # enableRateLimit: 거래소 요청 간격을 ccxt 가 알아서 지켜준다
        # 업비트 시세 조회는 초당 10회까지인데 ccxt 기본값은 50ms(초당 20회)라 너무 빠르다.
        # 그대로 두면 과거 캔들을 많이 받을 때 429(요청 과다)로 끊긴다 → 110ms(초당 약 9회)로 늘린다.
        self.upbit = ccxt.upbit({"enableRateLimit": True, "rateLimit": 110})
        # defaultType=swap: 바이비트 무기한 선물을 기본으로
        self.bybit = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})

        # 마켓 정보(어떤 코인이 있는지, 주문 단위 등)는 시작할 때 한 번만 받는다
        logger.info("거래소 마켓 정보 받는 중...")
        self.upbit.load_markets()
        self.bybit.load_markets()
        logger.info("거래소 마켓 정보 받기 완료")
        # 호가 동시 수신용 바이비트 공개 연결 묶음 (마켓 정보는 위에서 받은 것을 그대로 꽂아 다시 안 받는다)
        self._bybit_pool: "queue.Queue" = queue.Queue()
        for _ in range(BYBIT_PARALLEL):
            ex = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})
            ex.set_markets(self.bybit.markets)
            self._bybit_pool.put(ex)

    # ------------------------------------------------------------------
    # 마켓 정보
    # ------------------------------------------------------------------
    def filter_supported(self, coins: List[str]) -> List[str]:
        """
        업비트 원화 마켓과 바이비트 USDT 무기한에 둘 다 있는 코인만 남긴다

        한쪽에만 있으면 헷지를 못 하므로 경고를 남기고 뺀다.
        """
        supported: List[str] = []
        for coin in coins:
            on_upbit = upbit_symbol(coin) in self.upbit.markets
            on_bybit = bybit_symbol(coin) in self.bybit.markets
            if on_upbit and on_bybit:
                supported.append(coin)
            else:
                logger.warning(
                    "%s 제외: 업비트 원화 마켓 %s, 바이비트 USDT 무기한 %s",
                    coin, "있음" if on_upbit else "없음", "있음" if on_bybit else "없음",
                )
        return supported

    def round_qty(self, coin: str, qty: float) -> float:
        """
        바이비트 주문 개수를 주문 단위에 맞게 내림

        바이비트는 코인마다 주문 단위가 있다. (예: XRP 는 0.1개 단위 → 740.74개는 740.7개로)
        단위를 안 맞추면 주문이 거절된다. (old 봇이 거절당하던 원인 중 하나)
        """
        return float(self.bybit.amount_to_precision(bybit_symbol(coin), qty))

    def min_qty(self, coin: str) -> float:
        """바이비트 최소 주문 개수 (정보가 없으면 0)"""
        market = self.bybit.market(bybit_symbol(coin))
        return float(market.get("limits", {}).get("amount", {}).get("min") or 0.0)

    def min_notional_usdt(self, coin: str) -> float:
        """바이비트 최소 주문 금액 (USDT). 예) XRP 5 USDT. 정보가 없으면 0"""
        market = self.bybit.market(bybit_symbol(coin))
        info = (market.get("info") or {}).get("lotSizeFilter") or {}
        return float(info.get("minNotionalValue") or market.get("limits", {}).get("cost", {}).get("min") or 0.0)

    # ------------------------------------------------------------------
    # 시세
    # ------------------------------------------------------------------
    @staticmethod
    def _to_orderbook(raw: dict) -> OrderBook:
        """ccxt 호가창 → 우리 OrderBook 모양으로 바꾸기"""
        return OrderBook(
            bids=[(float(p), float(q)) for p, q, *_ in raw.get("bids", [])],
            asks=[(float(p), float(q)) for p, q, *_ in raw.get("asks", [])],
            timestamp_ms=raw.get("timestamp") or int(time.time() * 1000),
        )

    def fetch_quotes(self, coins: List[str], fx_krw: Optional[float] = None) -> Dict[str, Quote]:
        """
        코인별 시세 묶음(Quote) 가져오기 — 소켓 호가가 있으면 그걸, 없거나 묵은 코인만 REST 로

        Returns:
            {코인: Quote}. 어느 쪽으로도 못 받은 코인은 빠진다.
        """
        quotes: Dict[str, Quote] = {}
        missing = list(coins)
        if self.feed is not None:
            missing = []
            for coin in coins:
                fb = self.feed.books(coin)
                if fb is None:
                    missing.append(coin)
                    continue
                quotes[coin] = Quote(coin=coin, upbit=fb.upbit, bybit=fb.bybit, usdt=fb.usdt, fx_krw=fx_krw,
                                     skew_ms=fb.skew_ms)
            if missing:
                _log_repeat("ws-missing", logging.WARNING,
                            f"[웹소켓 호가 없음·묵음] {', '.join(missing)} → 이 코인은 REST 로 받습니다")
            else:
                _log_recovered("ws-missing", "웹소켓 호가")
        if missing:
            quotes.update(self._fetch_quotes_rest(missing, fx_krw))
        return quotes

    def _fetch_quotes_rest(self, coins: List[str], fx_krw: Optional[float] = None) -> Dict[str, Quote]:
        """
        REST 로 코인별 시세 묶음 받기

        1) 업비트: 모든 코인 + USDT/KRW 호가를 요청 1번에
        2) 바이비트: 코인마다 호가 요청 (한 번에 받는 기능이 없음)

        Args:
            coins: 조회할 코인 목록
            fx_krw: 달러 환율 (표시용: 사이트식 김프, 테더 프리미엄). 없어도 매매에는 상관없음

        Returns:
            {코인: Quote}. 조회에 실패한 코인은 빠진다.
            업비트 USDT/KRW 호가가 없으면 김프를 계산할 수 없어서 이번 스캔은 비어 있는 결과를 돌려준다.
        """
        symbols = [upbit_symbol(c) for c in coins] + [USDT_KRW_SYMBOL]

        # 업비트 묶음 조회와 바이비트 코인별 조회를 **동시에** 보낸다. 순차로 받으면 두 스냅샷이
        # 0.3~0.5초 어긋나고, 급변 때 그 사이 가격 차이가 '김프' 로 보인다 (2026-10-23 IRYS #3).
        # ccxt 객체는 스레드마다 따로 (업비트 1개, 바이비트는 pool 에서 하나씩 꺼내 쓰고 되돌린다).
        def get_upbit():
            books = self.upbit.fetch_order_books(symbols, self.depth)
            return books, time.monotonic()

        def get_bybit(coin):
            ex = self._bybit_pool.get()
            try:
                return self._to_orderbook(ex.fetch_order_book(bybit_symbol(coin), self.depth)), time.monotonic()
            finally:
                self._bybit_pool.put(ex)

        with ThreadPoolExecutor(max_workers=1 + min(len(coins), BYBIT_PARALLEL) or 1) as pool:
            fut_up = pool.submit(get_upbit)
            fut_by = {coin: pool.submit(get_bybit, coin) for coin in coins}
            try:
                upbit_books, t_up = fut_up.result()
            except Exception as exc:  # 네트워크 오류, 요청 제한, 거래소 점검 등
                # 이게 안 되면 이번 스캔은 통째로 쉰다 (보유 포지션 관리도 못 한다) — 길어지면 5분마다 알림
                _log_repeat("upbit-books", logging.ERROR,
                            f"[업비트 호가 조회 실패] {exc} | 이 동안은 진입·청산·강제청산 확인을 못 합니다")
                for f in fut_by.values():
                    f.cancel()
                return {}
            bybit_books: Dict[str, tuple] = {}
            for coin, f in fut_by.items():
                try:
                    bybit_books[coin] = f.result()
                except Exception as exc:
                    # 이 코인만 이번 스캔에서 빠진다 (들고 있으면 그동안 평가·청산을 못 한다)
                    _log_repeat(f"bybit-{coin}", logging.WARNING, f"[바이비트 호가 조회 실패] {coin} | {exc}")
        _log_recovered("upbit-books", "업비트 호가 조회")

        usdt_raw = upbit_books.get(USDT_KRW_SYMBOL)
        usdt_book = self._to_orderbook(usdt_raw) if usdt_raw else None
        if usdt_book is None or usdt_book.mid is None:
            _log_repeat("usdt-book", logging.ERROR, "[업비트 USDT/KRW 호가 없음] 이번 스캔은 건너뜁니다")
            return {}
        _log_recovered("usdt-book", "업비트 USDT/KRW 호가")

        quotes: Dict[str, Quote] = {}
        for coin in coins:
            up_raw = upbit_books.get(upbit_symbol(coin))
            if not up_raw:
                _log_repeat(f"upbit-{coin}", logging.WARNING, f"{coin} 업비트 호가 없음")
                continue
            if coin not in bybit_books:
                continue
            _log_recovered(f"bybit-{coin}", f"{coin} 바이비트 호가 조회")

            upbit_book = self._to_orderbook(up_raw)
            bybit_book, t_by = bybit_books[coin]
            if upbit_book.mid is None or bybit_book.mid is None:
                _log_repeat(f"empty-{coin}", logging.WARNING, f"{coin} 호가가 비어 있어서 건너뜀")
                continue
            _log_recovered(f"upbit-{coin}", f"{coin} 업비트 호가")
            _log_recovered(f"empty-{coin}", f"{coin} 호가")
            quotes[coin] = Quote(coin=coin, upbit=upbit_book, bybit=bybit_book, usdt=usdt_book, fx_krw=fx_krw,
                                 skew_ms=int(abs(t_by - t_up) * 1000))
        return quotes

    def fetch_funding(self, coin: str) -> Optional[FundingInfo]:
        """
        바이비트 펀딩비 정보 가져오기

        Returns:
            FundingInfo. 실패하면 None
        """
        try:
            raw = self.bybit.fetch_funding_rate(bybit_symbol(coin))
        except Exception as exc:
            _log_repeat(f"funding-{coin}", logging.WARNING, f"[펀딩비 조회 실패] {coin} | {exc}")
            return None
        _log_recovered(f"funding-{coin}", f"{coin} 펀딩비 조회")

        rate = raw.get("fundingRate")
        # ccxt 바이비트는 '다음 정산 시각' 을 fundingTimestamp 에 넣어준다
        next_ms = raw.get("fundingTimestamp") or raw.get("nextFundingTimestamp")
        mark = raw.get("markPrice")
        if rate is None or not next_ms or not mark:
            logger.warning("%s 펀딩비 응답에 필요한 값이 없음: %s", coin, raw)
            return None
        return FundingInfo(rate=float(rate), next_funding_ms=int(next_ms), mark_price=float(mark),
                           interval_hours=_parse_interval_hours(raw.get("interval")))


def _parse_interval_hours(text) -> Optional[float]:
    """
    ccxt 가 주는 정산 주기 글자를 시간 숫자로 바꾼다

    예) '8h' → 8.0, '4h' → 4.0, '60m' → 1.0, 모르는 모양이면 None
    """
    if not text or not isinstance(text, str):
        return None
    try:
        if text.endswith("h"):
            return float(text[:-1])
        if text.endswith("m"):
            return float(text[:-1]) / 60
    except ValueError:
        return None
    return None


# ======================================================================
# 여러 코인을 한 번에 (맨 앞 호가만)
# ======================================================================
@dataclass(frozen=True)
class TopQuote:
    """
    코인 하나의 '맨 앞 호가' 묶음 (여러 코인을 한 번에 받을 때 쓴다)

    Quote(호가창 전체)와 달리 1호가만 들고 있다. 그래서 '수량만큼 긁은 평균가' 는 계산할 수 없고,
    김프 기록·현황 표시용이다. 대신 코인 200개도 몇 초면 받는다.
    """

    coin: str
    upbit_bid: float
    upbit_ask: float
    bybit_bid: float
    bybit_ask: float
    usdt_bid: float
    usdt_ask: float
    funding_rate: Optional[float] = None
    funding_interval_hours: Optional[float] = None
    next_funding_ms: Optional[int] = None
    fx_krw: Optional[float] = None

    @property
    def upbit_mid(self) -> float:
        """업비트 가운데 가격 = (매수호가 + 매도호가) ÷ 2. 실제로 체결되는 가격은 아니다"""
        return (self.upbit_bid + self.upbit_ask) / 2

    @property
    def bybit_mid(self) -> float:
        """바이비트 가운데 가격"""
        return (self.bybit_bid + self.bybit_ask) / 2

    @property
    def usdt_mid(self) -> float:
        """업비트 USDT/KRW 가운데 가격 (김프 계산의 분모)"""
        return (self.usdt_bid + self.usdt_ask) / 2

    @property
    def mid_premium(self) -> float:
        """가운데 가격끼리 계산한 봇 김프 (%, 코인 몫)"""
        from kimp.premium import premium_percent
        return premium_percent(self.upbit_mid, self.bybit_mid, self.usdt_mid)

    @property
    def entry_premium_top(self) -> float:
        """지금 진입하면 나오는 김프: 코인 살 값 / 숏 칠 값 / USDT 팔 값"""
        from kimp.premium import premium_percent
        return premium_percent(self.upbit_ask, self.bybit_bid, self.usdt_bid)

    @property
    def exit_premium_top(self) -> float:
        """지금 청산하면 나오는 김프: 코인 팔 값 / 숏 닫을 값 / USDT 살 값"""
        from kimp.premium import premium_percent
        return premium_percent(self.upbit_bid, self.bybit_ask, self.usdt_ask)

    @property
    def usdt_spread_percent(self) -> float:
        """업비트 USDT/KRW 호가 간격 (%)"""
        return (self.usdt_ask / self.usdt_bid - 1) * 100

    @property
    def coin_spread_percent(self) -> float:
        """코인 호가 간격 (%) = 업비트 + 바이비트"""
        return (self.upbit_ask / self.upbit_bid - 1) * 100 + (self.bybit_ask / self.bybit_bid - 1) * 100

    @property
    def tether_premium(self) -> Optional[float]:
        """테더 프리미엄 (%) = 업비트 USDT 가격 ÷ 달러 환율 − 1. 환율이 없으면 None"""
        return (self.usdt_mid / self.fx_krw - 1) * 100 if self.fx_krw else None

    @property
    def site_premium(self) -> Optional[float]:
        """사이트식 김프 (%) = 달러 환율 기준. 환율이 없으면 None"""
        from kimp.premium import premium_percent
        return premium_percent(self.upbit_mid, self.bybit_mid, self.fx_krw) if self.fx_krw else None

    def funding_8h_percent(self) -> Optional[float]:
        """펀딩비율을 8시간 기준 %로 (코인마다 정산 주기가 1·4·8시간이라 맞춰야 비교된다)"""
        if self.funding_rate is None:
            return None
        hours = self.funding_interval_hours or 8.0
        return self.funding_rate * 100 * (8.0 / hours if hours else 1.0)


def fetch_tops(md: "MarketData", coins: List[str], fx_krw: Optional[float] = None) -> Dict[str, TopQuote]:
    """
    여러 코인의 맨 앞 호가를 한 번에 받는다 (요청을 적게 쓰려고)

        업비트  호가창을 50개씩 묶어서 (코인 150개면 3번)
        바이비트 티커 1번 — 맨 앞 호가·펀딩비율이 다 들어 있다 (900개가 1초)

    코인마다 호가창을 따로 받으면 200번이 되어 느리다. 그래서 김프 기록·현황 표시는 이 함수를 쓴다.
    (단, 진입·청산 판단은 '수량만큼 긁은 평균가' 가 필요해서 fetch_quotes 를 쓴다)
    """
    out: Dict[str, TopQuote] = {}
    if not coins:
        return out
    books: Dict[str, dict] = {}
    symbols = [upbit_symbol(c) for c in coins] + [USDT_KRW_SYMBOL]
    for i in range(0, len(symbols), 50):
        try:
            books.update(md.upbit.fetch_order_books(symbols[i:i + 50], 5))
        except Exception as exc:
            logger.warning("업비트 호가 묶음 조회 실패: %s", exc)
    usdt = books.get(USDT_KRW_SYMBOL) or {}
    usdt_bid = (usdt.get("bids") or [[None]])[0][0]
    usdt_ask = (usdt.get("asks") or [[None]])[0][0]
    if not usdt_bid or not usdt_ask:
        logger.error("업비트 USDT/KRW 호가가 없어서 김프를 계산할 수 없습니다")
        return out
    try:
        tickers = md.bybit.fetch_tickers()
    except Exception as exc:
        logger.error("바이비트 티커 조회 실패: %s", exc)
        return out

    for coin in coins:
        up = books.get(upbit_symbol(coin)) or {}
        by = tickers.get(bybit_symbol(coin)) or {}
        up_bid = (up.get("bids") or [[None]])[0][0]
        up_ask = (up.get("asks") or [[None]])[0][0]
        by_bid, by_ask = by.get("bid"), by.get("ask")
        if not (up_bid and up_ask and by_bid and by_ask):
            continue
        info = by.get("info") or {}
        rate = info.get("fundingRate")
        next_ms = info.get("nextFundingTime")
        try:  # 정산 주기는 마켓 정보에 분 단위로 있다 (480분 = 8시간)
            minutes = float(((md.bybit.market(bybit_symbol(coin)) or {}).get("info") or {})
                            .get("fundingInterval") or 480)
        except Exception:
            minutes = 480.0
        out[coin] = TopQuote(
            coin=coin, upbit_bid=up_bid, upbit_ask=up_ask, bybit_bid=by_bid, bybit_ask=by_ask,
            usdt_bid=usdt_bid, usdt_ask=usdt_ask,
            funding_rate=float(rate) if rate not in (None, "") else None,
            funding_interval_hours=minutes / 60 if minutes else 8.0,
            next_funding_ms=int(next_ms) if next_ms not in (None, "") else None,
            fx_krw=fx_krw,
        )
    return out

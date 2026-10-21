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
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import ccxt

from kimp.premium import OrderBook, Quote

logger = logging.getLogger(__name__)

# 업비트 USDT/KRW: 김프 계산 기준이자, 진입/청산 때 실제로 USDT 를 사고파는 마켓
USDT_KRW_SYMBOL = "USDT/KRW"


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
    """

    def __init__(self, depth: int = 30):
        """
        Args:
            depth: 호가창을 몇 줄까지 받을지
        """
        self.depth = depth
        # enableRateLimit: 거래소 요청 간격을 ccxt 가 알아서 지켜준다
        self.upbit = ccxt.upbit({"enableRateLimit": True})
        # defaultType=swap: 바이비트 무기한 선물을 기본으로
        self.bybit = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})

        # 마켓 정보(어떤 코인이 있는지, 주문 단위 등)는 시작할 때 한 번만 받는다
        logger.info("거래소 마켓 정보 받는 중...")
        self.upbit.load_markets()
        self.bybit.load_markets()
        logger.info("거래소 마켓 정보 받기 완료")

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
        코인별 시세 묶음(Quote) 가져오기

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
        try:
            upbit_books = self.upbit.fetch_order_books(symbols, self.depth)
        except Exception as exc:  # 네트워크 오류, 요청 제한 등
            logger.error("업비트 호가 조회 실패: %s", exc)
            return {}

        usdt_raw = upbit_books.get(USDT_KRW_SYMBOL)
        usdt_book = self._to_orderbook(usdt_raw) if usdt_raw else None
        if usdt_book is None or usdt_book.mid is None:
            logger.error("업비트 USDT/KRW 호가가 없어서 이번 스캔은 건너뜁니다")
            return {}

        quotes: Dict[str, Quote] = {}
        for coin in coins:
            up_raw = upbit_books.get(upbit_symbol(coin))
            if not up_raw:
                logger.warning("%s 업비트 호가 없음", coin)
                continue
            try:
                by_raw = self.bybit.fetch_order_book(bybit_symbol(coin), self.depth)
            except Exception as exc:
                logger.warning("%s 바이비트 호가 조회 실패: %s", coin, exc)
                continue

            upbit_book = self._to_orderbook(up_raw)
            bybit_book = self._to_orderbook(by_raw)
            if upbit_book.mid is None or bybit_book.mid is None:
                logger.warning("%s 호가가 비어 있어서 건너뜀", coin)
                continue
            quotes[coin] = Quote(coin=coin, upbit=upbit_book, bybit=bybit_book, usdt=usdt_book, fx_krw=fx_krw)
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
            logger.warning("%s 펀딩비 조회 실패: %s", coin, exc)
            return None

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

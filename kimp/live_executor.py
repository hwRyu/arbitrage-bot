"""
실거래 실행기 / 드라이런 실행기

둘 다 kimp/executor.py 의 PaperExecutor 와 '똑같은 동작 목록' 을 가진다. 순서·되돌리기·손익·송금 흐름은
kimp/broker.py 가 모드와 상관없이 똑같이 돌리고, 여기서는 한 동작씩만 한다.

LiveExecutor (실제 주문)
------------------------------------------------------------------
- 업비트: USDT/KRW 시장가 매도(개수), 코인 시장가 매수(원화 금액), 코인 시장가 매도(개수), USDT 시장가 매수(원화 금액)
- 바이비트: USDT 무기한 시장가 숏(개수, 단방향 모드), reduceOnly 시장가 매수로 숏 닫기
- 주문을 낸 뒤 '끝났다'(closed/canceled) 가 될 때까지 조회해서 실제 체결 개수·평균가·수수료를 돌려준다
  예) 업비트 시장가 매수는 남은 원화가 생기면 상태가 'cancel' 로 끝나지만 체결된 만큼은 유효하다
- 주문마다 추적번호(client_id)를 붙인다 (바이비트 orderLinkId, 업비트 identifier)
  · 주문 요청이 오류로 끝나도 바로 실패로 치지 않는다 → 추적번호로 조회해서 실제로 들어갔는지 확인
    있으면 그 체결값으로 진행 / 없으면 실패 / 조회도 안 되면 '모름'(OrderUnknown) → 브로커가 다음 스캔에 다시 조회
  · 20초 안에 안 끝나면: 주문 취소 → 다시 조회 → 체결된 만큼만 결과로 (남은 양은 브로커가 다시 주문)
- 잔고·포지션은 스캔마다 한 번 받아 두고(refresh), 주문하면 다시 받는다
- 송금: 업비트 → 바이비트는 업비트 출금 API (net_type), 바이비트 → 업비트는 통합계좌 → 펀딩 계좌 이동 후 출금
  받는 쪽 입금 내역에서 금액이 맞는 입금이 '완료' 면 도착

DryRunExecutor (주문 안 냄)
------------------------------------------------------------------
- 처음 한 번 '실제 잔고' 를 페이퍼 장부에 복사하고, 그다음부터는 페이퍼와 똑같이 그 순간 호가로 체결을 계산한다
- 동작마다 "[드라이런] 실제였다면 …" 로그를 남긴다 → 실거래 전에 하루 이틀 돌려서 확인하는 단계
"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from kimp.config import Settings
from kimp.db import PaperBalance
from kimp.executor import (
    BYBIT_USDT,
    UPBIT_KRW,
    UPBIT_USDT,
    LegFill,
    OrderFailed,
    OrderUnknown,
    PaperExecutor,
    new_client_id,
)
from kimp.market_data import USDT_KRW_SYMBOL, bybit_symbol, upbit_symbol
from kimp.premium import Quote

logger = logging.getLogger(__name__)

DONE_STATUSES = ("closed", "canceled", "cancelled", "expired", "rejected")


# 체결 단계 이름 → 어느 거래소 어느 마켓인지 (추적번호 조회용)
BYBIT_SIDES = {"open_short", "close_short", "trim_short", "unwind_close_short"}
USDT_SIDES = {"sell_usdt", "buy_usdt", "trim_buy_usdt", "unwind_buy_usdt"}


class LiveExecutor:
    """실제 주문 실행기 (ccxt 업비트 / 바이비트, 키가 들어간 객체를 받는다)"""

    is_paper = False
    name = "live"

    def __init__(self, upbit, bybit, settings: Settings, sleep: Callable[[float], None] = time.sleep,
                 order_timeout_sec: float = 20.0):
        self.up = upbit
        self.by = bybit
        self.s = settings
        self._sleep = sleep
        self.order_timeout_sec = order_timeout_sec
        self._up_bal: Optional[dict] = None
        self._by_bal: Optional[dict] = None
        self._shorts: Dict[str, float] = {}
        self._liq: Dict[str, Optional[float]] = {}   # 코인 → 거래소 강제청산가 (스캔당 한 번 받아 둔다)
        self._min_krw: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # 준비 / 잔고
    # ------------------------------------------------------------------
    def setup(self, coins: Iterable[str], leverage: float) -> None:
        """
        시작할 때 바이비트 설정 맞추기: 단방향 모드, 코인마다 격리 마진 + 레버리지
        이미 그렇게 돼 있으면 거래소가 '바뀐 게 없음' 오류를 주는데 무시한다.
        """
        for what, call in [("단방향 모드", lambda: self.by.set_position_mode(False))]:
            self._ignore_not_modified(what, call)
        for coin in coins:
            sym = bybit_symbol(coin)
            self._ignore_not_modified(f"{coin} 격리 마진",
                                      lambda: self.by.set_margin_mode("isolated", sym, {"leverage": leverage}))
            self._ignore_not_modified(f"{coin} 레버리지 {leverage:g}배", lambda: self.by.set_leverage(leverage, sym))

    def min_order_krw(self, coin: str) -> float:
        """
        업비트 최소 주문 금액 (원): 업비트 '주문 가능 정보' 에서 그 마켓의 매수·매도 최소 금액 중 큰 값

        예) KRW-XRP → bid.min_total 5000, ask.min_total 5000 → 5,000원
        한 번 받은 값은 기억. 못 받으면 설정 UPBIT_MIN_ORDER_KRW (기본 5,000원)
        """
        if coin not in self._min_krw:
            value = self.s.upbit_min_order_krw
            try:
                info = self.up.private_get_orders_chance({"market": f"KRW-{coin}"})
                mk = info.get("market") or {}
                got = max(float((mk.get(side) or {}).get("min_total") or 0) for side in ("bid", "ask"))
                if got > 0:
                    value = got
            except Exception as exc:
                logger.warning("업비트 %s 최소 주문 금액 조회 실패 → 설정값 %s원: %s", coin, f"{value:,.0f}", exc)
            self._min_krw[coin] = value
        return self._min_krw[coin]

    @staticmethod
    def _ignore_not_modified(what: str, call) -> None:
        """
        바이비트 설정 바꾸기를 시도하고, '이미 그 값이다' 오류는 무시한다

        set_position_mode / set_margin_mode / set_leverage 는 **이미 원하는 값이면 오류**를 준다.
        봇을 다시 켤 때마다 이 오류가 나므로 그냥 넘겨야 한다.
            110043  레버리지가 안 바뀜 (이미 그 값)
            110026  마진 모드가 안 바뀜
            110025  포지션 모드가 안 바뀜
        그 밖의 실패는 경고만 남기고 계속한다 — 설정이 안 맞으면 주문 단계에서 다시 걸러진다.
        """
        try:
            call()
        except Exception as exc:  # 바뀐 게 없다는 오류는 정상
            text = str(exc).lower()
            if "not modified" in text or "110043" in text or "110026" in text or "110025" in text:
                return
            logger.warning("바이비트 설정 '%s' 실패: %s", what, exc)

    def ensure_initial(self, session: Session, total_usdt: float, upbit_share: float) -> bool:
        """실거래는 넣을 시작 자본이 없다 (거래소 실제 잔고를 쓴다)"""
        return False

    def refresh(self) -> None:
        """스캔마다 잔고·포지션을 새로 받게 비운다"""
        self._up_bal = None
        self._by_bal = None
        self._shorts = {}
        self._liq = {}

    def prefetch(self, coins: Iterable[str]) -> None:
        """
        캐시를 비운 직후(판단 전) 잔고와 숏을 미리 받아 둔다 — 주문 경로가 REST 를 기다리지 않게

        청산 경로는 '지금 코인 잔고' 와 '숏이 살아 있나' 를 확인하는데, 캐시가 비어 있으면 그 자리에서 REST 를
        두 번 기다린다 (CAP #8 2026-10-24: 판단 → 전송 411 ms). 스캔 맨 앞에서 받아 두면 주문 경로는 캐시만 읽는다.
        업비트 잔고 ∥ (바이비트 잔고 → 숏 한 번에) 로 동시에 받는다 (ccxt 객체는 스레드마다 다른 것).
        """
        coins = [c for c in coins if c not in self._shorts]

        def bybit_side():
            self._balances_one("bybit")
            if coins:
                self._load_shorts(coins)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fu = pool.submit(self._balances_one, "upbit")
            fb = pool.submit(bybit_side)
            for f in (fu, fb):
                f.result()

    def _balances_one(self, exchange: str) -> None:
        """한쪽 잔고만 받아 캐시에 넣는다 (prefetch 가 두 거래소를 동시에 받을 때)"""
        if exchange == "upbit" and self._up_bal is None:
            self._up_bal = self.up.fetch_balance()
        elif exchange == "bybit" and self._by_bal is None:
            self._by_bal = self.by.fetch_balance()

    def _balances(self):
        """
        업비트·바이비트 잔고 (스캔당 한 번만 받고 기억한다)

        한 스캔에서 free() 를 여러 번 부르는데 매번 조회하면 요청 제한에 걸린다.
        주문·출금 뒤에는 잔고가 바뀌므로 refresh() 로 비워서 다시 받게 한다.
        """
        # 지역 변수로 한 번만 읽는다. 텔레그램 스레드(/status)가 refresh() 로 캐시를 비우는 순간과
        # 겹치면, 필드를 두 번 읽는 코드는 (None, …) 을 돌려줘 잔고 계산이 터진다.
        up, by = self._up_bal, self._by_bal
        if up is None:
            up = self._up_bal = self.up.fetch_balance()
        if by is None:
            by = self._by_bal = self.by.fetch_balance()
        return up, by

    @staticmethod
    def _amount(bal: dict, cur: str, kind: str) -> float:
        """
        ccxt 잔고에서 숫자 하나 꺼내기 (없는 통화·없는 칸은 0)

        ccxt 잔고는 {'USDT': {'free': 1.2, 'used': 0, 'total': 1.2}, ...} 모양이다.
        kind: 'free' 주문에 쓸 수 있는 양 / 'total' 주문에 묶인 것까지 포함
        """
        return float((bal.get(cur) or {}).get(kind) or 0.0)

    def free(self, session: Session, asset: str) -> float:
        """
        쓸 수 있는 잔고 (asset 이름 규칙은 kimp/executor.py 와 같다)

            upbit:USDT   업비트 USDT / upbit:KRW 업비트 원화 / bybit:USDT 바이비트 USDT
            upbit:<코인>  업비트 코인 개수

        session 은 쓰지 않는다 — 페이퍼는 DB 장부를 보지만 실거래는 거래소가 진실이다.
        모르는 이름은 KeyError 로 터뜨린다. 조용히 0 을 돌려주면 '잔고가 없다' 로 오해해서
        주문을 안 내고 조용히 멈춰 버린다.
        """
        up, by = self._balances()
        if asset == UPBIT_USDT:
            return self._amount(up, "USDT", "free")
        if asset == UPBIT_KRW:
            return self._amount(up, "KRW", "free")
        if asset == BYBIT_USDT:
            return self._amount(by, "USDT", "free")
        if asset.startswith("upbit:"):
            return self._amount(up, asset.split(":", 1)[1], "free")
        raise KeyError(asset)

    def coin_total(self, session: Session, coin: str) -> float:
        """
        업비트에 있는 코인 전체 개수 (주문에 묶인 것까지 포함)

        청산할 때 '가진 만큼 다 팔기' 에 쓴다. free 가 아니라 total 을 쓰는 이유:
        주문이 걸려 있어도 그 코인은 내 것이라 결국 팔아야 하기 때문이다.
        """
        up, _ = self._balances()
        return self._amount(up, coin, "total")

    def short_qty(self, session: Session, coin: str) -> float:
        """바이비트 실제 숏 개수 (없으면 0)"""
        if coin not in self._shorts:
            self._load_short(coin)
        return self._shorts[coin]

    def _load_short(self, coin: str) -> None:
        """숏 개수와 거래소 강제청산가를 받아 둔다 (스캔당 한 번)"""
        self._load_shorts([coin])

    def _load_shorts(self, coins: List[str]) -> None:
        """여러 코인의 숏 개수·강제청산가를 요청 한 번으로 받아 둔다 (없는 코인은 0 / None)"""
        found: Dict[str, tuple] = {c: (0.0, None) for c in coins}
        by_symbol = {bybit_symbol(c): c for c in coins}
        for p in self.by.fetch_positions(list(by_symbol)):
            # ccxt 는 symbol 을 늘 채워 준다. 심볼 하나만 물었을 땐 없어도 그 코인으로 본다 (예전 응답 모양 대비)
            coin = by_symbol.get(p.get("symbol")) or (coins[0] if len(coins) == 1 else None)
            if coin is None or p.get("side") != "short":
                continue
            qty, liq = found[coin]
            qty += abs(float(p.get("contracts") or 0.0))
            try:
                liq = float(p.get("liquidationPrice") or 0) or None
            except (TypeError, ValueError):
                liq = None
            found[coin] = (qty, liq)
        for coin, (qty, liq) in found.items():
            self._shorts[coin] = qty
            self._liq[coin] = liq

    def position_liq_price(self, session: Session, coin: str) -> Optional[float]:
        """
        거래소가 알려주는 강제청산가 (없으면 None)

        봇 계산값(진입가 × (1 + 1/레버리지 − mmr))보다 **거래소가 먼저** 자른다 — 수수료·유지증거금 구간 때문.
        IRYS 2026-10-24: 봇 0.0572 (12.8% 남음 경고) vs 거래소 0.0559 (실제 6.3%). 그래서 이 값을 우선 쓴다.
        """
        if coin not in self._liq:
            self._load_short(coin)
        return self._liq.get(coin)

    def add_margin(self, session: Session, coin: str, usdt: float) -> float:
        """
        격리 숏 포지션에 증거금 추가 (계좌 여유 USDT → 그 포지션 주머니). 주문이 아니라 계좌 안 이동이라 수수료 없음

        Raises:
            OrderFailed: 거래소가 거절 (여유 부족 등)
        """
        try:
            self.by.add_margin(bybit_symbol(coin), float(f"{usdt:.4f}"), {"positionIdx": 0})
        except Exception as exc:
            raise OrderFailed(f"증거금 추가 실패: {exc}") from exc
        self.refresh()
        return usdt

    def short_liquidated(self, session: Session, coin: str) -> bool:
        """
        바이비트 숏이 사라졌나 (= 강제청산됐나)

        기억해 둔 값을 **일부러 비우고 다시 조회**한다. 강제청산은 우리가 주문하지 않아도
        일어나므로, 캐시된 값을 보면 '아직 있다' 로 잘못 판단한다.
        """
        self._shorts.pop(coin, None)
        return self.short_qty(session, coin) <= 0

    def short_gone(self, session: Session, coin: str) -> bool:
        """
        거래소에 숏이 남아 있지 않나 — 청산 주문을 내기 직전에 본다

        스캔 맨 앞에서 선조회(prefetch)한 값(길어야 SCAN_INTERVAL_SEC 전)을 쓴다. 예전엔 여기서 캐시를 비우고
        REST 를 다시 불렀는데, 그 150~200 ms 가 '판단 → 주문 전송' 에 그대로 얹혔다 (CAP #8 2026-10-24: 411 ms).
        그 2초 사이에 강제청산이 났다면 닫기 주문이 거절되고(OrderFailed → VOID) 다음 스캔에서 숏 없음으로
        마무리되므로 복구된다. 강제청산 여부를 꼭 새로 확인해야 하는 자리는 short_liquidated 를 쓴다.
        """
        return self.short_qty(session, coin) <= 0

    def mark_liquidated(self, session: Session, coin: str, qty: float) -> None:
        """거래소가 이미 정리했으니 할 일 없음"""

    def credit_funding(self, session: Session, amount_usdt: float) -> None:
        """펀딩비는 거래소가 잔고에 반영한다"""

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------
    def _wait(self, ex, order: dict, symbol: str) -> dict:
        """주문이 끝날 때까지 조회 (시장가라 보통 1초 안에 끝남)"""
        deadline = time.monotonic() + self.order_timeout_sec
        oid = order.get("id")
        while True:
            try:
                if ex is self.by:
                    got = self.by.fetch_order(oid, symbol, {"acknowledged": True})
                else:
                    got = ex.fetch_order(oid, symbol)
            except Exception as exc:
                got = None
                logger.warning("주문 %s 조회 실패 (다시): %s", oid, exc)
            if got and (got.get("status") in DONE_STATUSES or
                        (got.get("filled") and not got.get("remaining"))):
                return got
            if time.monotonic() > deadline:
                return self._cancel_and_get(ex, oid, symbol)
            self._sleep(0.25)   # 시장가는 보통 첫 조회에 끝난다. 대기는 짧게

    def _cancel_and_get(self, ex, oid, symbol: str) -> dict:
        """
        시간 안에 안 끝난 주문: 취소 → 다시 조회 → 체결된 만큼 (전혀 안 됐으면 체결 0 → 실패)

        취소가 실패해도(이미 다 체결됨 등) 다시 조회한 최종 상태를 쓴다. 조회도 안 되면 '모름'.
        """
        try:
            ex.cancel_order(oid, symbol)
        except Exception as exc:
            logger.warning("주문 %s 취소 실패 (이미 끝났을 수 있음, 다시 조회): %s", oid, exc)
        for _ in range(5):
            try:
                if ex is self.by:
                    return self.by.fetch_order(oid, symbol, {"acknowledged": True})
                return ex.fetch_order(oid, symbol)
            except Exception as exc:
                logger.warning("주문 %s 취소 후 조회 실패 (다시): %s", oid, exc)
                self._sleep(1.0)
        raise OrderUnknown(f"주문 {oid} 이 안 끝나서 취소했는데 결과 조회도 실패")

    def _find(self, ex, symbol: str, client_id: str) -> Optional[dict]:
        """
        추적번호로 주문 찾기. 없으면 None, 조회 자체가 안 되면 예외

        바이비트: 진행 중/최근 주문 → 지난 주문 내역 순서로 orderLinkId 조회
        업비트: identifier 로 주문 조회 (없으면 order_not_found)
        """
        if ex is self.by:
            market = self.by.market(symbol)
            req = {"category": "linear", "symbol": market["id"], "orderLinkId": client_id}
            for call in (self.by.private_get_v5_order_realtime, self.by.private_get_v5_order_history):
                rows = ((call(req) or {}).get("result") or {}).get("list") or []
                if rows:
                    return self.by.parse_order(rows[0], market)
            return None
        try:
            raw = self.up.private_get_order({"identifier": client_id})
        except Exception as exc:
            if "not_found" in str(exc).lower() or "notfound" in type(exc).__name__.lower():
                return None
            raise
        return self.up.parse_order(raw)

    @staticmethod
    def _fill(order: dict, fee_ccy: str, what: str) -> LegFill:
        """
        ccxt 주문 응답 → 체결값 (LegFill)

        거래소마다 비는 칸이 달라서 서로 메꿔 준다.
            average 가 없으면  cost / filled 로 평균가를 낸다
            cost 가 없으면     average x filled 로 금액을 낸다
        수수료는 **우리가 세는 통화만** 더한다 (fee_ccy). 업비트는 원화, 바이비트는 USDT 로 떼는데
        한 주문에 여러 통화 수수료가 섞여 오는 경우가 있어서 그대로 더하면 금액이 틀린다.

        Raises:
            OrderFailed: 체결이 0 개 (주문이 거절되거나 전량 취소된 것)
        """
        qty = float(order.get("filled") or 0.0)
        if qty <= 0:
            raise OrderFailed(f"{what}: 체결 0 (상태 {order.get('status')})")
        cost = float(order.get("cost") or 0.0)
        avg = float(order.get("average") or 0.0)
        if not avg and cost:
            avg = cost / qty
        if not cost:
            cost = avg * qty
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        fee = sum(float(f.get("cost") or 0.0) for f in fees if (f.get("currency") or fee_ccy) == fee_ccy)
        return LegFill(qty=qty, avg_price=avg, cost=cost, fee=fee, order_id=str(order.get("id") or ""))

    def _order(self, ex, symbol: str, what: str, place, fee_ccy: str, client_id: str) -> LegFill:
        """
        주문 → 끝날 때까지 조회 → 체결값

        주문 요청이 오류로 끝나면 추적번호로 조회해서 실제로 들어갔는지 확인한다 (응답만 끊겼을 수 있음).
          있음 → 그 주문 결과로 진행 / 없음 → 실패 (OrderFailed) / 조회도 실패 → 모름 (OrderUnknown)
        """
        try:
            order = place()
        except Exception as exc:
            found, err = None, None
            for _ in range(3):
                try:
                    found = self._find(ex, symbol, client_id)
                    err = None
                    break
                except Exception as e2:
                    err = e2
                    self._sleep(1.0)
            if err is not None:
                raise OrderUnknown(f"{what}: 주문 요청 오류({exc}) + 추적번호 {client_id} 조회도 실패({err})") from exc
            if found is None:
                raise OrderFailed(f"{what}: 주문 거절 {exc}") from exc
            logger.warning("%s: 주문 요청은 오류였지만 추적번호 %s 로 조회하니 들어가 있음 → 그대로 진행", what, client_id)
            order = found
        done = self._wait(ex, order, symbol)
        self.refresh()
        fill = self._fill(done, fee_ccy, what)
        logger.info("[주문 체결] %s %g개 평균 %.8g (금액 %.8g, 수수료 %.8g %s, 주문 %s)",
                    what, fill.qty, fill.avg_price, fill.cost, fill.fee, fee_ccy, fill.order_id)
        return fill

    @staticmethod
    def _nothing() -> LegFill:
        """주문 단위로 내리니 0개 → 주문 안 냄 (브로커는 '더 할 게 없음' 으로 본다)"""
        return LegFill(qty=0.0, avg_price=0.0, cost=0.0, fee=0.0, order_id="")

    def open_short(self, session: Session, quote: Quote, coin: str, qty: float, client_id: str = "") -> LegFill:
        """
        바이비트 USDT 무기한 시장가 숏 열기 (개수 지정)

        positionIdx 0 = 단방향(one-way) 모드. setup() 에서 맞춰 둔다 — 양방향 모드면
        롱·숏이 따로 잡혀서 reduceOnly 로 닫는 흐름이 깨진다.
        amount_to_precision 으로 거래소 주문 단위에 맞추고, 그래서 0 이 되면 주문을 안 낸다.
        """
        sym = bybit_symbol(coin)
        cid = client_id or new_client_id()
        amount = float(self.by.amount_to_precision(sym, qty))
        if amount <= 0:
            return self._nothing()
        return self._order(self.by, sym, f"바이비트 {coin} 숏 열기", lambda: self.by.create_order(
            sym, "market", "sell", amount, None, {"positionIdx": 0, "orderLinkId": cid}), "USDT", cid)

    def close_short(self, session: Session, quote: Quote, coin: str, qty: float,
                    entry_price: float, margin_usdt: float, client_id: str = "") -> LegFill:
        """
        바이비트 숏 닫기 (reduceOnly 시장가 매수)

        reduceOnly 를 붙이는 이유: 숏이 이미 강제청산됐거나 수량이 어긋나 있을 때 그냥 매수하면
        **반대 방향 롱이 새로 생긴다.** reduceOnly 면 줄이기만 하고 새로 생기지 않는다.
        entry_price·margin_usdt 는 페이퍼 쪽 계산에만 쓰는 인자라 여기서는 안 쓴다
        (실거래는 거래소가 손익을 계산한다). 모드끼리 동작 목록을 맞추려고 받아만 둔다.
        """
        sym = bybit_symbol(coin)
        cid = client_id or new_client_id()
        try:
            amount = float(self.by.amount_to_precision(sym, qty))
        except Exception:  # 주문 단위보다 작아서 자리수 변환이 터진다 → 할 게 없음
            return self._nothing()
        if amount <= 0:
            return self._nothing()
        return self._order(self.by, sym, f"바이비트 {coin} 숏 닫기", lambda: self.by.create_order(
            sym, "market", "buy", amount, None, {"positionIdx": 0, "reduceOnly": True, "orderLinkId": cid}),
            "USDT", cid)

    def _upbit_sell(self, sym: str, qty: float, what: str, client_id: str) -> LegFill:
        """
        업비트 시장가 매도 (개수 지정) — 코인 팔기와 USDT 팔기가 같은 모양이라 모아 둠

        amount_to_precision 이 터지거나 0 이 되면 주문 단위보다 작다는 뜻 → 주문 안 냄.
        identifier 가 추적번호다 (응답이 끊겨도 이걸로 주문을 되찾는다).
        """
        cid = client_id or new_client_id()
        try:
            amount = float(self.up.amount_to_precision(sym, qty))
        except Exception:
            return self._nothing()
        if amount <= 0:
            return self._nothing()
        return self._order(self.up, sym, what, lambda: self.up.create_order(
            sym, "market", "sell", amount, None, {"identifier": cid}), "KRW", cid)

    def _upbit_buy_cost(self, sym: str, krw: float, what: str, client_id: str) -> LegFill:
        """
        업비트 시장가 매수 (**원화 금액** 지정) — 코인 사기와 USDT 사기가 같은 모양

        업비트 시장가 매수는 '몇 개' 가 아니라 '몇 원어치' 로만 주문된다. 그래서 금액 헷징과
        궁합이 맞는다 (바이비트 숏 금액만큼 사면 된다).
        금액은 1원 단위로 **내림** 한다 — 올리면 잔고가 모자라 주문이 거절된다.
        """
        cid = client_id or new_client_id()
        krw = math.floor(krw)  # 업비트 시장가 매수는 원화 금액 (1원 단위로 내림)
        if krw <= 0:
            return self._nothing()
        return self._order(self.up, sym, what, lambda: self.up.create_market_buy_order_with_cost(
            sym, krw, {"identifier": cid}), "KRW", cid)

    # 아래 네 개가 업비트에서 하는 동작 전부다 (진입 2단계 + 청산 2단계).
    # quote(그 순간 호가)는 페이퍼가 체결가를 계산할 때만 쓰고, 실거래는 거래소가 정하므로 안 쓴다.
    def sell_usdt(self, session: Session, quote: Quote, amount_usdt: float, client_id: str = "") -> LegFill:
        """진입 1단계: USDT 를 팔아 원화 만들기"""
        return self._upbit_sell(USDT_KRW_SYMBOL, amount_usdt, "업비트 USDT 매도", client_id)

    def buy_coin_with_krw(self, session: Session, quote: Quote, coin: str, krw: float,
                          client_id: str = "") -> LegFill:
        """진입 2단계: 그 원화로 코인 사기 (바이비트 숏 금액과 같은 금액)"""
        return self._upbit_buy_cost(upbit_symbol(coin), krw, f"업비트 {coin} 매수", client_id)

    def sell_coin(self, session: Session, quote: Quote, coin: str, qty: float, client_id: str = "") -> LegFill:
        """청산 1단계: 코인을 팔아 원화 만들기"""
        return self._upbit_sell(upbit_symbol(coin), qty, f"업비트 {coin} 매도", client_id)

    def buy_usdt_with_krw(self, session: Session, quote: Quote, krw: float, client_id: str = "") -> LegFill:
        """청산 2단계: 그 원화로 USDT 되사기 (여기까지 오면 자본이 다시 USDT 가 된다)"""
        return self._upbit_buy_cost(USDT_KRW_SYMBOL, krw, "업비트 USDT 매수", client_id)

    def lookup(self, session: Session, side: str, coin: str, client_id: str) -> Optional[LegFill]:
        """
        추적번호로 주문 결과 찾기 (결과를 모르던 주문 / 재시작 복구)

        Returns:
            체결값. 주문이 없거나 체결 0 이면 None. 조회가 안 되면 예외 (다음에 다시)
        """
        if side in BYBIT_SIDES:
            ex, sym, ccy = self.by, bybit_symbol(coin), "USDT"
        elif side in USDT_SIDES:
            ex, sym, ccy = self.up, USDT_KRW_SYMBOL, "KRW"
        else:
            ex, sym, ccy = self.up, upbit_symbol(coin), "KRW"
        order = self._find(ex, sym, client_id)
        if order is None:
            return None
        done = self._wait(ex, order, sym)
        self.refresh()
        if not float(done.get("filled") or 0.0):
            return None
        return self._fill(done, ccy, f"{side} 조회")

    # ------------------------------------------------------------------
    # 송금
    # ------------------------------------------------------------------
    def _deposit_address(self, to_bybit: bool):
        """
        받는 주소 (설정값이 있으면 그것, 없으면 받는 거래소에서 내 입금 주소 조회)

        네트워크 이름은 받는 거래소의 이름으로 (바이비트 BYBIT_TRANSFER_NETWORK, 업비트 UPBIT_TRANSFER_NETWORK)
        """
        if to_bybit and self.s.bybit_deposit_address:
            return self.s.bybit_deposit_address, self.s.bybit_deposit_tag or None
        if not to_bybit and self.s.upbit_deposit_address:
            return self.s.upbit_deposit_address, self.s.upbit_deposit_tag or None
        if to_bybit:
            got = self.by.fetch_deposit_address("USDT", {"network": self.s.bybit_transfer_network})
        else:
            got = self.up.fetch_deposit_address("USDT", {"network": self.s.upbit_transfer_network})
        return got["address"], got.get("tag") or None

    def withdraw(self, session: Session, to_bybit: bool, amount_usdt: float, network: str) -> str:
        """출금 (네트워크 이름은 보내는 거래소의 이름: 업비트 net_type 'TRX' / 바이비트 chain 'TRC20')"""
        try:
            address, tag = self._deposit_address(to_bybit)
            # ccxt 는 두 거래소 모두 withdraw(코드, 수량, 주소, params) 다. 태그는 params['tag'] 로 넣는다.
            # 4번째 인자로 태그를 넘기면 실거래에서 이렇게 터진다 (2026-10-22 실제로 겪음):
            #   upbit.withdraw() takes from 4 to 5 positional arguments but 6 were given
            if to_bybit:
                params = {"net_type": self.s.upbit_transfer_network}
                if tag:
                    params["tag"] = tag
                r = self.up.withdraw("USDT", amount_usdt, address, params)
            else:
                # 바이비트는 거래(통합) 계좌 → 펀딩 계좌로 옮긴 뒤 출금.
                # 출금이 거절되면 펀딩 계좌에 남은 돈을 도로 옮긴다 — 안 그러면 봇이 보는 free 잔고에서
                # 그 돈이 사라진 채 굳는다 (2026-10-24: 73.68 USDT 가 펀딩 계좌에 남았던 사고)
                self.by.transfer("USDT", amount_usdt, "unified", "funding")
                params = {"network": self.s.bybit_transfer_network}
                if tag:
                    params["tag"] = tag
                try:
                    r = self.by.withdraw("USDT", amount_usdt, address, params)
                except Exception as exc:
                    try:
                        self.by.transfer("USDT", amount_usdt, "funding", "unified")
                        logger.warning("바이비트 출금 실패 → 펀딩 계좌로 옮겼던 %.4f USDT 를 통합 계좌로 되돌림", amount_usdt)
                    except Exception as back:
                        logger.critical("바이비트 출금 실패 뒤 펀딩 → 통합 되돌리기도 실패: %s (펀딩 계좌에 %.4f USDT 남음)",
                                        back, amount_usdt)
                    raise
        except Exception as exc:
            raise OrderFailed(f"출금 실패: {exc}") from exc
        self.refresh()
        return str(r.get("id") or "")

    def transfer_arrived(self, session: Session, transfer) -> Optional[bool]:
        """
        받는 쪽 입금 내역에 '완료' 된 입금이 있나 (금액: 보낸 양 − 수수료 근처)

        보내는 쪽 출금이 실패·취소로 끝났으면 False. 아직이면 None.
        """
        src = self.up if transfer.to_bybit else self.by
        dst = self.by if transfer.to_bybit else self.up
        since = int(transfer.requested_at.timestamp() * 1000) - 120_000
        try:
            for w in src.fetch_withdrawals("USDT", since):
                if str(w.get("id")) == str(transfer.withdraw_id) and w.get("status") in ("failed", "canceled"):
                    return False
        except Exception as exc:
            logger.debug("출금 내역 조회 실패: %s", exc)
        expect = transfer.amount_usdt - transfer.fee_usdt
        for d in dst.fetch_deposits("USDT", since):
            amount = float(d.get("amount") or 0.0)
            if d.get("status") == "ok" and expect - 2.0 <= amount <= transfer.amount_usdt + 0.5:
                self.refresh()
                return True
        return None

    def read_balances(self) -> Dict[str, float]:
        """드라이런 시작용: 지금 실제 잔고 {UPBIT_USDT, UPBIT_KRW, BYBIT_USDT}"""
        self.refresh()
        return {UPBIT_USDT: self.free(None, UPBIT_USDT), UPBIT_KRW: self.free(None, UPBIT_KRW),
                BYBIT_USDT: self.free(None, BYBIT_USDT)}


class DryRunExecutor(PaperExecutor):
    """
    드라이런: 실제 잔고로 시작 + 체결은 페이퍼와 똑같이 계산 + "실제였다면 이 주문" 로그

    주문은 절대 내지 않는다 (live 는 잔고를 한 번 읽는 데만 쓴다).
    """

    name = "dry_run"

    def __init__(self, fees, leverage: float, live: LiveExecutor):
        super().__init__(fees, leverage)
        self.live = live

    def min_order_krw(self, coin: str) -> float:
        """최소 주문 금액은 실제 거래소 값 (실거래와 같은 판단)"""
        return self.live.min_order_krw(coin)

    def ensure_initial(self, session: Session, total_usdt: float, upbit_share: float) -> bool:
        """
        처음 한 번만: 실제 거래소 잔고를 페이퍼 장부로 복사한다

        이미 장부가 있으면(두 번째 실행부터) 아무것도 안 한다 — 복사하면 그동안 쌓인
        드라이런 손익이 지워진다. total_usdt·upbit_share 는 페이퍼용 인자라 안 쓴다
        (실제 잔고를 그대로 쓰므로 비율을 정할 필요가 없다).

        Returns:
            복사했으면 True (브로커가 '시작 자본을 넣었다' 로 로그를 남긴다)
        """
        if session.get(PaperBalance, UPBIT_USDT) is not None or session.get(PaperBalance, BYBIT_USDT) is not None:
            return False
        real = self.live.read_balances()
        for asset, amount in real.items():
            session.add(PaperBalance(asset=asset, amount=amount))
        session.commit()
        logger.info("[드라이런] 실제 잔고로 시작: 업비트 USDT %s (+ 원화 %s) / 바이비트 USDT %s",
                    f"{real[UPBIT_USDT]:,.2f}", f"{real[UPBIT_KRW]:,.0f}", f"{real[BYBIT_USDT]:,.2f}")
        return True

    def _say(self, text: str, f: LegFill) -> LegFill:
        """'실제였다면 이런 주문' 로그를 남기고 체결값은 그대로 돌려준다 (아래 래퍼들의 공통부)"""
        logger.info("[드라이런] 실제였다면 %s → 계산 체결 %g개 평균 %.8g (주문은 안 냄)", text, f.qty, f.avg_price)
        return f

    # 아래는 전부 PaperExecutor 의 계산을 그대로 쓰고 로그만 덧붙이는 래퍼다.
    # 주문은 한 줄도 내지 않는다 — 실거래 전 '같은 판단이 나오나' 를 눈으로 보는 단계.
    def open_short(self, session, quote, coin, qty, client_id=""):
        return self._say(f"바이비트 {coin} {qty:g}개 시장가 숏",
                         super().open_short(session, quote, coin, qty, client_id))

    def close_short(self, session, quote, coin, qty, entry_price, margin_usdt, client_id=""):
        return self._say(f"바이비트 {coin} {qty:g}개 숏 닫기",
                         super().close_short(session, quote, coin, qty, entry_price, margin_usdt, client_id))

    def sell_usdt(self, session, quote, amount_usdt, client_id=""):
        return self._say(f"업비트 USDT {amount_usdt:,.4f}개 시장가 매도",
                         super().sell_usdt(session, quote, amount_usdt, client_id))

    def buy_coin_with_krw(self, session, quote, coin, krw, client_id=""):
        return self._say(f"업비트 {coin} {krw:,.0f}원어치 시장가 매수",
                         super().buy_coin_with_krw(session, quote, coin, krw, client_id))

    def sell_coin(self, session, quote, coin, qty, client_id=""):
        return self._say(f"업비트 {coin} {qty:g}개 시장가 매도", super().sell_coin(session, quote, coin, qty, client_id))

    def buy_usdt_with_krw(self, session, quote, krw, client_id=""):
        return self._say(f"업비트 USDT {krw:,.0f}원어치 시장가 매수",
                         super().buy_usdt_with_krw(session, quote, krw, client_id))

    def withdraw(self, session, to_bybit, amount_usdt, network):
        logger.warning("[드라이런] 실제였다면 %s 로 USDT %s 개 출금 (%s) (출금은 안 함)",
                       "업비트 → 바이비트" if to_bybit else "바이비트 → 업비트", f"{amount_usdt:,.2f}", network)
        return super().withdraw(session, to_bybit, amount_usdt, network)

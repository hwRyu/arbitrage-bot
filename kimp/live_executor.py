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
from typing import Callable, Dict, Iterable, Optional

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

    def _balances(self):
        if self._up_bal is None:
            self._up_bal = self.up.fetch_balance()
        if self._by_bal is None:
            self._by_bal = self.by.fetch_balance()
        return self._up_bal, self._by_bal

    @staticmethod
    def _amount(bal: dict, cur: str, kind: str) -> float:
        return float((bal.get(cur) or {}).get(kind) or 0.0)

    def free(self, session: Session, asset: str) -> float:
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
        up, _ = self._balances()
        return self._amount(up, coin, "total")

    def short_qty(self, session: Session, coin: str) -> float:
        """바이비트 실제 숏 개수 (없으면 0)"""
        if coin not in self._shorts:
            qty = 0.0
            for p in self.by.fetch_positions([bybit_symbol(coin)]):
                if p.get("side") == "short":
                    qty += abs(float(p.get("contracts") or 0.0))
            self._shorts[coin] = qty
        return self._shorts[coin]

    def short_liquidated(self, session: Session, coin: str) -> bool:
        self._shorts.pop(coin, None)
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
            self._sleep(0.5)

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
        sym = bybit_symbol(coin)
        cid = client_id or new_client_id()
        amount = float(self.by.amount_to_precision(sym, qty))
        if amount <= 0:
            return self._nothing()
        return self._order(self.by, sym, f"바이비트 {coin} 숏 열기", lambda: self.by.create_order(
            sym, "market", "sell", amount, None, {"positionIdx": 0, "orderLinkId": cid}), "USDT", cid)

    def close_short(self, session: Session, quote: Quote, coin: str, qty: float,
                    entry_price: float, margin_usdt: float, client_id: str = "") -> LegFill:
        sym = bybit_symbol(coin)
        cid = client_id or new_client_id()
        try:
            amount = float(self.by.amount_to_precision(sym, qty))
        except Exception:  # 주문 단위보다 작음
            return self._nothing()
        if amount <= 0:
            return self._nothing()
        return self._order(self.by, sym, f"바이비트 {coin} 숏 닫기", lambda: self.by.create_order(
            sym, "market", "buy", amount, None, {"positionIdx": 0, "reduceOnly": True, "orderLinkId": cid}),
            "USDT", cid)

    def _upbit_sell(self, sym: str, qty: float, what: str, client_id: str) -> LegFill:
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
        cid = client_id or new_client_id()
        krw = math.floor(krw)  # 업비트 시장가 매수는 원화 금액 (1원 단위로 내림)
        if krw <= 0:
            return self._nothing()
        return self._order(self.up, sym, what, lambda: self.up.create_market_buy_order_with_cost(
            sym, krw, {"identifier": cid}), "KRW", cid)

    def sell_usdt(self, session: Session, quote: Quote, amount_usdt: float, client_id: str = "") -> LegFill:
        return self._upbit_sell(USDT_KRW_SYMBOL, amount_usdt, "업비트 USDT 매도", client_id)

    def buy_coin_with_krw(self, session: Session, quote: Quote, coin: str, krw: float,
                          client_id: str = "") -> LegFill:
        return self._upbit_buy_cost(upbit_symbol(coin), krw, f"업비트 {coin} 매수", client_id)

    def sell_coin(self, session: Session, quote: Quote, coin: str, qty: float, client_id: str = "") -> LegFill:
        return self._upbit_sell(upbit_symbol(coin), qty, f"업비트 {coin} 매도", client_id)

    def buy_usdt_with_krw(self, session: Session, quote: Quote, krw: float, client_id: str = "") -> LegFill:
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
                # 바이비트는 거래(통합) 계좌 → 펀딩 계좌로 옮긴 뒤 출금
                self.by.transfer("USDT", amount_usdt, "unified", "funding")
                params = {"network": self.s.bybit_transfer_network}
                if tag:
                    params["tag"] = tag
                r = self.by.withdraw("USDT", amount_usdt, address, params)
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
        logger.info("[드라이런] 실제였다면 %s → 계산 체결 %g개 평균 %.8g (주문은 안 냄)", text, f.qty, f.avg_price)
        return f

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

"""
실거래 실행기 + 공통 브로커 테스트 (인터넷 필요 없음)

ccxt 를 흉내 내는 가짜 업비트·바이비트를 만들어 LiveExecutor 의 실제 코드(주문 → 조회 → 체결값)를 돌린다.
브로커는 페이퍼와 같은 HedgeBroker 다.
"""
import logging
import os
import tempfile
import unittest
from datetime import timedelta

from kimp.broker import EntryRejected, HedgeBroker, PaperBroker
from kimp.config import FeeSettings, Settings
from kimp.db import ExitReason, Fill, Position, PositionStatus, Transfer, TransferStatus, make_session_factory, utcnow
from kimp.executor import BYBIT_USDT, UPBIT_USDT, OrderFailed
from kimp.live_executor import DryRunExecutor, LiveExecutor
from kimp.premium import OrderBook, Quote
from kimp.safety import Safety

FEES = FeeSettings(upbit_trade_fee_rate=0.0005, bybit_taker_fee_rate=0.00055, transfer_fee_usdt_per_cycle=0.0,
                   slippage_buffer_percent=0.05)
DEEP = 1e12


class Prices:
    """가짜 시장 가격 (가짜 거래소와 Quote 가 같이 본다)"""

    def __init__(self):
        self.usdt_bid, self.usdt_ask = 1399.0, 1400.0  # 업비트 USDT/KRW
        self.coin_bid, self.coin_ask = 2800.0, 2801.0  # 업비트 XRP/KRW
        self.perp_bid, self.perp_ask = 2.0, 2.0001  # 바이비트 XRP 무기한

    def quote(self) -> Quote:
        def ob(b, a):
            return OrderBook(bids=[(b, DEEP)], asks=[(a, DEEP)])

        return Quote(coin="XRP", upbit=ob(self.coin_bid, self.coin_ask), bybit=ob(self.perp_bid, self.perp_ask),
                     usdt=ob(self.usdt_bid, self.usdt_ask))


class FakeExchange:
    """주문을 받아 바로 체결하고, fetch_order 로 결과를 돌려주는 가짜 ccxt"""

    def __init__(self, prices: Prices):
        self.p = prices
        self.orders = {}
        self.by_client = {}  # 추적번호 → 주문 번호
        self.calls = []
        self.fail = set()
        self.fail_once = {}  # {동작: 에러 문구} — 그 문구로 한 번 거절  # 실패시킬 동작 이름 (한 번, 주문 안 들어감)
        self.lost = set()  # 주문은 들어가는데 응답만 끊기는 동작 (한 번)
        self.partial = {}  # 동작 이름 → 체결 비율 (한 번)
        self.slow = set()  # 시간 안에 안 끝나는 동작 (한 번): 'open' 상태로 일부만 체결, 취소하면 끝
        self.lookup_down = False  # 추적번호 조회도 안 됨
        self.deposits = []
        self.withdrawals = []
        self._n = 0

    def _id(self):
        self._n += 1
        return f"{type(self).__name__}-{self._n}"

    def _maybe_fail(self, what):
        self.calls.append(what)
        if what in getattr(self, "fail_once", {}):   # 거래소가 준 '그 문구' 로 거절시키기 (규정 거절 구분 테스트)
            raise RuntimeError(self.fail_once.pop(what))
        if what in self.fail:
            self.fail.discard(what)
            raise RuntimeError(f"가짜 거래소 거절: {what}")
        return self.partial.pop(what, 1.0)

    def _save(self, what, order, client_id):
        """주문 저장 (+ 응답 끊김 / 느림 흉내)"""
        self.orders[order["id"]] = order
        if client_id:
            self.by_client[client_id] = order["id"]
        if what in self.slow:
            self.slow.discard(what)
            order["status"] = "open"
            order["remaining"] = 1.0
        if what in self.lost:
            self.lost.discard(what)
            raise ConnectionError(f"응답 끊김: {what}")
        return {"id": order["id"]}

    def cancel_order(self, oid, symbol=None):
        self.calls.append(("cancel", oid))
        o = self.orders[oid]
        o["status"], o["remaining"] = "canceled", 0.0

    def market(self, symbol):
        return {"id": symbol.replace("/", "").split(":")[0], "symbol": symbol}

    @staticmethod
    def parse_order(raw, market=None):
        return raw

    @staticmethod
    def amount_to_precision(symbol, amount):
        return f"{amount:.8f}"

    def fetch_order(self, oid, symbol, params=None):
        return self.orders[oid]

    def fetch_deposits(self, code, since=None):
        return list(self.deposits)

    def fetch_withdrawals(self, code, since=None):
        return list(self.withdrawals)


class FakeUpbit(FakeExchange):
    fee = 0.0005

    def __init__(self, prices):
        super().__init__(prices)
        self.bal = {"USDT": 500.0, "KRW": 0.0, "XRP": 0.0}

    def _book(self, symbol):
        return (self.p.usdt_bid, self.p.usdt_ask) if symbol == "USDT/KRW" else (self.p.coin_bid, self.p.coin_ask)

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        what = f"sell {symbol}"
        amount *= self._maybe_fail(what)
        base = symbol.split("/")[0]
        bid, _ask = self._book(symbol)
        cost = amount * bid
        fee = cost * self.fee
        self.bal[base] -= amount
        self.bal["KRW"] += cost - fee
        oid = self._id()
        order = {"id": oid, "status": "closed", "filled": amount, "average": bid, "cost": cost,
                 "fee": {"cost": fee, "currency": "KRW"}}
        return self._save(what, order, (params or {}).get("identifier"))

    def create_market_buy_order_with_cost(self, symbol, cost, params=None):
        what = f"buy {symbol}"
        cost *= self._maybe_fail(what)
        base = symbol.split("/")[0]
        _bid, ask = self._book(symbol)
        qty = cost / ask
        fee = cost * self.fee
        self.bal["KRW"] -= cost + fee
        self.bal[base] = self.bal.get(base, 0.0) + qty
        oid = self._id()
        # 업비트 시장가 매수는 'cancel' 로 끝나도 체결된 만큼은 유효 (ccxt: canceled)
        order = {"id": oid, "status": "canceled", "filled": qty, "average": ask, "cost": cost,
                 "fee": {"cost": fee, "currency": "KRW"}}
        return self._save(what, order, (params or {}).get("identifier"))

    def private_get_order(self, params):
        if self.lookup_down:
            raise ConnectionError("조회 안 됨")
        oid = self.by_client.get(params.get("identifier"))
        if oid is None:
            raise RuntimeError("upbit order_not_found")
        return self.orders[oid]

    def fetch_balance(self):
        return {k: {"free": v, "total": v} for k, v in self.bal.items()}

    def fetch_deposit_address(self, code, params=None):
        return {"address": "UPBIT-ADDR", "tag": None}

    def withdraw(self, code, amount, address, params=None):  # ccxt 와 같은 인자 (태그는 params['tag'])
        self._maybe_fail("withdraw")
        self.bal["USDT"] -= amount
        wid = self._id()
        self.withdrawals.append({"id": wid, "status": "pending", "amount": amount})
        self.sent = (amount, address, params)
        return {"id": wid}


class FakeBybit(FakeExchange):
    fee = 0.00055

    def __init__(self, prices, leverage=1.0):
        super().__init__(prices)
        self.free_usdt = 600.0
        self.short = 0.0
        self.entry = 0.0
        self.margin = 0.0
        self.leverage = leverage
        self.funding_usdt = 0.0
        self.settings_calls = []

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        params = params or {}
        what = "open_short" if side == "sell" else "close_short"
        if side == "sell":
            amount *= self._maybe_fail(what)
            px = self.p.perp_bid
            cost = amount * px
            fee = cost * self.fee
            self.entry = (self.entry * self.short + px * amount) / (self.short + amount)
            self.short += amount
            m = cost / self.leverage
            self.margin += m
            self.free_usdt -= m + fee
        else:
            amount *= self._maybe_fail(what)
            assert params.get("reduceOnly"), "숏 닫기는 reduceOnly 여야 함"
            px = self.p.perp_ask
            cost = amount * px
            fee = cost * self.fee
            part = amount / self.short
            m = self.margin * part
            self.free_usdt += m + (self.entry - px) * amount - fee
            self.margin -= m
            self.short -= amount
        oid = self._id()
        order = {"id": oid, "status": "closed", "filled": amount, "average": px, "cost": cost,
                 "fee": {"cost": fee, "currency": "USDT"}}
        return self._save(what, order, params.get("orderLinkId"))

    def private_get_v5_order_realtime(self, req):
        if self.lookup_down:
            raise ConnectionError("조회 안 됨")
        oid = self.by_client.get(req.get("orderLinkId"))
        return {"result": {"list": [self.orders[oid]] if oid else []}}

    def private_get_v5_order_history(self, req):
        return {"result": {"list": []}}

    def fetch_positions(self, symbols=None):
        return [{"side": "short", "contracts": self.short}] if self.short > 0 else []

    def fetch_balance(self):
        return {"USDT": {"free": self.free_usdt, "total": self.free_usdt + self.margin}}

    def set_position_mode(self, hedged):
        self.settings_calls.append(("position_mode", hedged))

    def set_margin_mode(self, mode, symbol, params=None):
        self.settings_calls.append(("margin", mode, symbol))
        raise RuntimeError("bybit {\"retCode\":110026,\"retMsg\":\"not modified\"}")  # 이미 설정됨 → 무시돼야 함

    def set_leverage(self, lev, symbol):
        self.settings_calls.append(("leverage", lev, symbol))

    def fetch_deposit_address(self, code, params=None):
        return {"address": "BYBIT-ADDR", "tag": None}

    def transfer(self, code, amount, from_, to_):
        self.calls.append(("transfer", from_, to_))

    def withdraw(self, code, amount, address, params=None):  # ccxt 와 같은 인자 (태그는 params['tag'])
        self._maybe_fail("withdraw")
        self.free_usdt -= amount
        wid = self._id()
        self.withdrawals.append({"id": wid, "status": "pending", "amount": amount})
        return {"id": wid}


def settings(**kw) -> Settings:
    base = dict(coins=["XRP"], fees=FEES, paper_trading=False, live_dry_run=False, live_confirm="YES",
                upbit_access_key="k", upbit_secret_key="s", bybit_api_key="k", bybit_api_secret="s",
                upbit_transfer_network="TRX", bybit_transfer_network="TRC20")
    base.update(kw)
    return Settings(**base)


def make_live(fail_safety=3):
    prices = Prices()
    up, by = FakeUpbit(prices), FakeBybit(prices)
    s = settings()
    ex = LiveExecutor(up, by, s, sleep=lambda _x: None, order_timeout_sec=0.01)
    safety = Safety(max_consecutive_failures=fail_safety)
    broker = HedgeBroker(ex, FEES, 0.3, leverage=1.0, safety=safety, auto_transfer=True, network="TRX",
                         transfer_min_usdt=10.0)
    sf = make_session_factory(":memory:")
    return prices, up, by, broker, sf


def equity(up, by, prices):
    """양쪽 총 자본 (USDT): 업비트 USDT + 원화/USDT + 코인 평가 + 바이비트 free + 증거금 + 숏 평가"""
    usdt_mid = (prices.usdt_bid + prices.usdt_ask) / 2
    return (up.bal["USDT"] + up.bal["KRW"] / usdt_mid + up.bal["XRP"] * prices.coin_bid / usdt_mid
            + by.free_usdt + by.margin + (by.entry - prices.perp_ask) * by.short)


class LiveRoundTripTest(unittest.TestCase):
    def test_entry_and_exit_with_real_fills(self):
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)  # 50개 숏 ≈ 100 USDT
            self.assertEqual(pos.status, PositionStatus.OPEN)
            self.assertEqual(by.short, 50.0)
            self.assertAlmostEqual(pos.usdt_sold, 100.0)  # 숏 금액만큼 USDT 매도
            self.assertAlmostEqual(up.bal["XRP"], pos.upbit_qty)
            self.assertAlmostEqual(up.bal["KRW"], 0.0, delta=1.0)  # 원화는 1원 미만 내림 자투리만
            start = 500 + 600
            # 김프가 올라서 청산
            prices.coin_bid, prices.coin_ask = 2850.0, 2851.0
            val = broker.value_exit(pos, prices.quote())
            pnl = broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertEqual(by.short, 0.0)
            self.assertAlmostEqual(up.bal["XRP"], 0.0, places=6)  # 주문 단위 반올림 자투리
            # DB 손익 = 실제 잔고 변화 (원화 자투리 정도 차이)
            self.assertAlmostEqual(equity(up, by, prices) - start, pnl.net_usdt, delta=0.01)
            self.assertGreater(pnl.net_usdt, 0)

    def test_bybit_settings_on_setup(self):
        prices, up, by, broker, sf = make_live()
        broker.ex.setup(["XRP"], 1.0)  # '바뀐 게 없음' 오류는 무시
        self.assertIn(("position_mode", False), by.settings_calls)
        self.assertIn(("leverage", 1.0, "XRP/USDT:USDT"), by.settings_calls)


class UnwindTest(unittest.TestCase):
    def test_coin_buy_fails_then_unwind(self):
        """코인 매수 실패 → 받은 원화로 USDT 되사기 + 숏 닫기 → ABORTED, 잔고 거의 원상태"""
        prices, up, by, broker, sf = make_live()
        up.fail.add("buy XRP/KRW")
        with sf() as s:
            with self.assertRaises(EntryRejected):
                broker.open_hedge(s, prices.quote(), 50.0)
            pos = s.query(Position).one()
            self.assertEqual(pos.status, PositionStatus.ABORTED)
            self.assertEqual(by.short, 0.0)
            self.assertAlmostEqual(up.bal["USDT"], 500.0, delta=0.5)  # 수수료·호가 간격만큼 줄어듦
            self.assertLess(pos.net_pnl_usdt, 0)
            self.assertTrue(broker.entries_allowed(s)[0])  # 한 번 실패는 계속 진행

    def test_unwind_fails_marks_error_and_blocks(self):
        prices, up, by, broker, sf = make_live()
        up.fail.add("buy XRP/KRW")
        by.fail.add("close_short")
        with sf() as s:
            with self.assertRaises(EntryRejected):
                broker.open_hedge(s, prices.quote(), 50.0)
            self.assertEqual(s.query(Position).one().status, PositionStatus.ERROR)
            ok, why = broker.entries_allowed(s)
            self.assertFalse(ok)
            self.assertIn("ERROR", why)

    def test_bybit_reject_unwinds_usdt_sold_in_parallel(self):
        """숏 거절 → 동시에 나간 USDT 매도만 되돌린다 (수수료 왕복만 손해). 코인 매수는 안 간다
        (전에는 숏 먼저·순차라 '아무 일도 없음' 이었는데, 2026-10-23 다리 겹치기로 바뀜)"""
        prices, up, by, broker, sf = make_live()
        by.fail.add("open_short")
        with sf() as s:
            with self.assertRaises(EntryRejected):
                broker.open_hedge(s, prices.quote(), 50.0)
            self.assertEqual(s.query(Position).one().status, PositionStatus.ABORTED)
            self.assertAlmostEqual(up.bal["USDT"], 500.0, delta=0.3)   # 수수료 2번만큼만
            self.assertEqual(up.calls.count("sell USDT/KRW"), 1)
            self.assertEqual(up.calls.count("buy USDT/KRW"), 1)
            self.assertNotIn("buy XRP/KRW", up.calls)

    def test_consecutive_failures_block(self):
        prices, up, by, broker, sf = make_live(fail_safety=2)
        with sf() as s:
            for _ in range(2):
                by.fail.add("open_short")
                with self.assertRaises(EntryRejected):
                    broker.open_hedge(s, prices.quote(), 50.0)
            self.assertFalse(broker.entries_allowed(s)[0])
            broker.safety.resume()
            self.assertTrue(broker.entries_allowed(s)[0])


class ResumeTest(unittest.TestCase):
    def test_close_fails_then_continues_next_scan(self):
        """숏 닫기 실패 → CLOSING 으로 남음 → 다음 스캔에 숏 닫기만 다시 (업비트는 다시 안 팜)"""
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            by.fail.add("close_short")
            val = broker.value_exit(pos, prices.quote())
            self.assertIsNone(broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT))
            self.assertEqual(pos.status, PositionStatus.CLOSING)
            sells_before = up.calls.count("sell XRP/KRW")
            broker.sync(s, {"XRP": prices.quote()}, ["XRP"])
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertEqual(up.calls.count("sell XRP/KRW"), sells_before)
            self.assertEqual(by.short, 0.0)

    def test_crash_during_entry_is_unwound_on_restart(self):
        """숏만 들어간 상태로 꺼짐(OPENING) → 다시 켜면 숏을 닫고 ABORTED"""
        prices, up, by, broker, sf = make_live()

        class Boom(Exception):
            pass

        orig = broker.ex.sell_usdt
        broker.ex.sell_usdt = lambda *a, **k: (_ for _ in ()).throw(Boom())
        with sf() as s:
            with self.assertRaises(Boom):
                broker.open_hedge(s, prices.quote(), 50.0)
            self.assertEqual(s.query(Position).one().status, PositionStatus.OPENING)
        broker.ex.sell_usdt = orig
        with sf() as s:
            broker.sync(s, {"XRP": prices.quote()}, ["XRP"])
            self.assertEqual(s.query(Position).one().status, PositionStatus.ABORTED)
            self.assertEqual(by.short, 0.0)

    def test_reconcile_mismatch_blocks_entries(self):
        """숏이 '일부' 만 어긋나면 (누가 손으로 30개 줄임) 자동으로 고치지 않고 신규 진입만 막는다.
        숏이 '아예 없는' 경우는 VanishedShortTest — 그건 즉시 코인을 판다"""
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            by.short = 30.0  # 누가 거래소에서 손으로 일부 닫음
            broker._last_reconcile = -1e18
            broker.sync(s, {"XRP": prices.quote()}, ["XRP"])
            ok, why = broker.entries_allowed(s)
            self.assertFalse(ok)
            self.assertIn("대조 불일치", why)
            self.assertEqual(pos.status, PositionStatus.OPEN)   # 건드리지 않는다

    def test_liquidation_price_but_short_still_open_is_closed_normally(self):
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            val = broker.value_liquidation(pos, prices.quote())
            pnl = broker.close_hedge(s, pos, val, ExitReason.LIQUIDATION, liquidated=True)
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertEqual(pos.exit_reason, ExitReason.STOP_LOSS)  # 실제로는 숏을 시장가로 닫음
            self.assertEqual(by.short, 0.0)
            self.assertGreater(pnl.bybit_leg_usdt, -1.0)  # 증거금을 다 잃은 게 아님


class LegRiskTest(unittest.TestCase):
    """레그 리스크: 부분 체결, 응답 끊김, 느린 주문"""

    def test_partial_coin_buy_trims_short(self):
        """코인이 80%만 사짐 → 남는 숏을 닫아 양쪽 금액을 1% 안으로 맞춤, 비용은 진입 비용에"""
        prices, up, by, broker, sf = make_live()
        up.partial["buy XRP/KRW"] = 0.8
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            coin_usdt = pos.upbit_qty * prices.coin_bid / ((prices.usdt_bid + prices.usdt_ask) / 2)
            short_usdt = by.short * prices.perp_bid
            self.assertLess(abs(short_usdt - coin_usdt) / short_usdt, 0.011)
            self.assertAlmostEqual(pos.bybit_qty, by.short)  # DB = 실제
            self.assertLess(by.short, 50.0)
            self.assertGreater(pos.entry_bybit_fee_usdt, 50 * 2.0 * 0.00055)  # 맞추는 비용이 더해짐
            # 청산까지 하면 잔고 변화 = DB 손익
            start = 500 + 600
            val = broker.value_exit(pos, prices.quote())
            pnl = broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
            self.assertAlmostEqual(equity(up, by, prices) - start, pnl.net_usdt, delta=0.01)

    def test_small_difference_is_left_alone(self):
        prices, up, by, broker, sf = make_live()
        up.partial["buy XRP/KRW"] = 0.995  # 0.5% 차이 → 그대로
        with sf() as s:
            broker.open_hedge(s, prices.quote(), 50.0)
            self.assertEqual(by.short, 50.0)

    def test_partial_close_short_retries_remaining(self):
        """숏 닫기가 60%만 → 남은 40%만 다시 주문 → 숏 0, 손익은 두 체결 합"""
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            by.partial["close_short"] = 0.6
            start = 500 + 600
            val = broker.value_exit(pos, prices.quote())
            pnl = broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertAlmostEqual(by.short, 0.0)
            self.assertEqual(by.calls.count("close_short"), 2)
            self.assertAlmostEqual(equity(up, by, prices) - start, pnl.net_usdt, delta=0.01)

    def test_partial_coin_sell_retries_remaining(self):
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            up.partial["sell XRP/KRW"] = 0.5
            val = broker.value_exit(pos, prices.quote())
            broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertAlmostEqual(up.bal["XRP"], 0.0, places=6)
            self.assertEqual(up.calls.count("sell XRP/KRW"), 2)

    def test_lost_response_but_order_went_in(self):
        """주문은 들어갔는데 응답만 끊김 → 추적번호로 찾아서 그대로 진행 (중복 주문 없음)"""
        prices, up, by, broker, sf = make_live()
        by.lost.add("open_short")
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            self.assertEqual(pos.status, PositionStatus.OPEN)
            self.assertEqual(by.calls.count("open_short"), 1)
            self.assertEqual(by.short, 50.0)

    def test_rejected_and_not_found_is_failure(self):
        prices, up, by, broker, sf = make_live()
        up.fail.add("sell USDT/KRW")  # 거절 + 조회해도 없음 → 실패 → 숏 되돌림
        with sf() as s:
            with self.assertRaises(EntryRejected):
                broker.open_hedge(s, prices.quote(), 50.0)
            self.assertEqual(s.query(Position).one().status, PositionStatus.ABORTED)
            self.assertEqual(by.short, 0.0)

    def test_unknown_result_waits_then_resolves(self):
        """응답 끊김 + 조회도 안 됨 → 추측 안 하고 멈춤 → 조회되면 이어서 진입 완료"""
        prices, up, by, broker, sf = make_live()
        up.lost.add("sell USDT/KRW")
        up.lookup_down = True
        with sf() as s:
            with self.assertRaises(EntryRejected):
                broker.open_hedge(s, prices.quote(), 50.0)
            pos = s.query(Position).one()
            self.assertEqual(pos.status, PositionStatus.OPENING)
            self.assertFalse(broker.entries_allowed(s)[0])
            broker.sync(s, {"XRP": prices.quote()}, ["XRP"])  # 아직 조회 안 됨 → 손대지 않음
            self.assertEqual(pos.status, PositionStatus.OPENING)
            up.lookup_down = False
            broker.sync(s, {"XRP": prices.quote()}, ["XRP"])
            # USDT 매도는 들어가 있었음 → 코인 매수 단계가 없으니 되돌림 (USDT 되사기 + 숏 닫기)
            self.assertEqual(pos.status, PositionStatus.ABORTED)
            self.assertEqual(by.short, 0.0)
            self.assertAlmostEqual(up.bal["USDT"], 500.0, delta=0.5)
            self.assertTrue(broker.entries_allowed(s)[0])

    def test_slow_order_is_cancelled_and_partial_used(self):
        """20초 안에 안 끝남 → 취소 → 체결된 만큼으로 진행"""
        prices, up, by, broker, sf = make_live()
        by.slow.add("open_short")
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            self.assertIn("cancel", [c[0] for c in by.calls if isinstance(c, tuple)])
            self.assertEqual(pos.status, PositionStatus.OPEN)
            self.assertAlmostEqual(pos.bybit_qty, by.short)

    def test_paper_partial_fill_same_logic(self):
        """페이퍼도 같은 크기 맞추기 로직"""
        prices = Prices()
        broker = PaperBroker(FEES, 0.3)
        sf = make_session_factory(":memory:")
        with sf() as s:
            broker.ensure_initial_balances(s, 1000, 0.5)
            broker.ex.partial_next["buy_coin_with_krw"] = 0.8
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            self.assertLess(pos.bybit_qty, 50.0)
            self.assertAlmostEqual(broker.ex.short_qty(s, "XRP"), pos.bybit_qty)
            # 청산 뒤 페이퍼 장부 변화 = 손익 기록 (남은 원화도 USDT 로 돌아옴)
            val = broker.value_exit(pos, prices.quote())
            pnl = broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
            from kimp.executor import UPBIT_KRW
            usdt_mid = (prices.usdt_bid + prices.usdt_ask) / 2
            total = (broker.balance(s, UPBIT_USDT) + broker.balance(s, UPBIT_KRW) / usdt_mid
                     + broker.balance(s, BYBIT_USDT))
            self.assertAlmostEqual(total - 1000, pnl.net_usdt, delta=0.01)


class JumpyBybit(FakeBybit):
    """숏이 체결되는 순간 업비트 코인 가격이 뛴다 — REST 로 주문 세 개를 순서대로 내는 사이의 시간을 흉내"""

    jump = 1.01

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        r = super().create_order(symbol, type_, side, amount, price, params)
        if side == "sell":
            self.p.coin_bid *= self.jump
            self.p.coin_ask *= self.jump
        return r


class HedgeGapTest(unittest.TestCase):
    """
    실거래 전 검증에서 찾은 구멍들 (2026-10-23)

    - 크기 맞추기(trim) 주문의 결과를 모르면 OPEN 으로 끝내 버려 DB 숏 ≠ 실제 숏 이 굳던 문제
    - 되돌리기 중 결과를 모르면 바로 ERROR(사람 호출) 로 끝내던 문제
    - 주문 사이에 가격이 움직여 진입 김프가 나빠져도 아무 말이 없던 문제
    - 청산이 막히면 10초마다 CRITICAL 을 찍어 텔레그램을 도배하던 문제
    """

    def test_trim_unknown_keeps_opening_until_resolved(self):
        """크기 맞추기(trim_short) 결과를 모름 → OPEN 으로 끝내지 않는다 → 조회되면 실제 숏 개수로 OPEN"""
        prices, up, by, broker, sf = make_live()
        up.partial["buy XRP/KRW"] = 0.8          # 코인 80% 만 사짐 → 숏 20% 를 줄여야 함
        by.lost.add("close_short")               # 그 trim 주문: 들어갔는데 응답 끊김
        by.lookup_down = True                    # 추적번호 조회도 안 됨
        with sf() as s:
            with self.assertRaises(EntryRejected):
                broker.open_hedge(s, prices.quote(), 50.0)
            pos = s.query(Position).one()
            self.assertEqual(pos.status, PositionStatus.OPENING)     # OPEN 이 아니라 멈춤
            self.assertFalse(broker.entries_allowed(s)[0])            # 그동안 신규 진입 막힘
            by.lookup_down = False
            broker.sync(s, {"XRP": prices.quote()}, ["XRP"])
            s.refresh(pos)
            self.assertEqual(pos.status, PositionStatus.OPEN)
            self.assertAlmostEqual(pos.bybit_qty, by.short, places=6)  # DB = 실제 (전에는 50 vs 39.93)
            self.assertEqual(by.calls.count("close_short"), 1)        # 중복 trim 없음
            self.assertTrue(broker.entries_allowed(s)[0])
            self.assertEqual(broker.reconcile(s, ["XRP"]), [])

    def test_unwind_unknown_waits_instead_of_error(self):
        """되돌리기 주문의 결과를 모름 → ERROR 가 아니라 OPENING 유지 → 조회되면 ABORTED 로 마무리"""
        prices, up, by, broker, sf = make_live()
        up.fail.add("buy XRP/KRW")               # 3단계 실패 → 되돌리기 시작
        by.lost.add("close_short")               # 되돌리는 숏 닫기: 들어갔는데 결과 모름
        by.lookup_down = True
        with sf() as s:
            with self.assertRaises(EntryRejected):
                broker.open_hedge(s, prices.quote(), 50.0)
            pos = s.query(Position).one()
            self.assertEqual(pos.status, PositionStatus.OPENING)     # ERROR(사람 호출) 가 아니다
            by.lookup_down = False
            broker.sync(s, {"XRP": prices.quote()}, ["XRP"])
            s.refresh(pos)
            self.assertEqual(pos.status, PositionStatus.ABORTED)
            self.assertEqual(by.short, 0.0)
            self.assertEqual(by.calls.count("close_short"), 1)        # 중복 주문 없음
            self.assertAlmostEqual(up.bal["USDT"], 500.0, delta=0.5)  # USDT 로 되돌아옴

    def test_entry_premium_drift_is_warned(self):
        """주문 사이에 가격이 움직여 실제 진입 김프가 판단보다 MAX_SLIPPAGE 넘게 나빠지면 경고 (헷지는 유지)"""
        prices = Prices()
        up, by = FakeUpbit(prices), JumpyBybit(prices)
        ex = LiveExecutor(up, by, settings(), sleep=lambda _x: None, order_timeout_sec=0.01)
        broker = HedgeBroker(ex, FEES, 0.3, leverage=1.0, safety=Safety(3), auto_transfer=True, network="TRX")
        sf = make_session_factory(":memory:")
        with sf() as s:
            q = prices.quote()
            judged = q.entry_plan(50.0, FEES.upbit_trade_fee_rate).premium
            with self.assertLogs("kimp.broker", level=logging.WARNING) as logs:
                pos = broker.open_hedge(s, q, 50.0)
            self.assertEqual(pos.status, PositionStatus.OPEN)
            self.assertGreater(pos.entry_premium - judged, 0.9)        # 약 1%p 불리하게 기록됨
            self.assertTrue(any("진입 김프 밀림" in m for m in logs.output), logs.output)

    def test_small_drift_is_quiet(self):
        """0.3% 안쪽으로만 움직이면 경고하지 않는다"""
        prices = Prices()
        up, by = FakeUpbit(prices), JumpyBybit(prices)
        by.jump = 1.001
        ex = LiveExecutor(up, by, settings(), sleep=lambda _x: None, order_timeout_sec=0.01)
        broker = HedgeBroker(ex, FEES, 0.3, leverage=1.0, safety=Safety(3), auto_transfer=True, network="TRX")
        sf = make_session_factory(":memory:")
        with sf() as s:
            with self.assertNoLogs("kimp.broker", level=logging.WARNING):
                broker.open_hedge(s, prices.quote(), 50.0)

    def test_close_failure_alert_is_not_repeated_every_scan(self):
        """청산이 계속 막혀도 CRITICAL 은 5분에 한 번 (텔레그램 도배 방지). 풀리면 정상 완료"""
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            val = broker.value_exit(pos, prices.quote())
            up.fail.add("sell XRP/KRW")
            with self.assertLogs("kimp.broker", level=logging.CRITICAL) as first:
                broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
            self.assertEqual(sum("[청산 실패]" in m for m in first.output), 1)
            up.fail.add("sell XRP/KRW")              # 다음 스캔에도 막힘
            with self.assertNoLogs("kimp.broker", level=logging.CRITICAL):
                broker.sync(s, {"XRP": prices.quote()}, ["XRP"])
            self.assertEqual(pos.status, PositionStatus.CLOSING)
            broker.sync(s, {"XRP": prices.quote()}, ["XRP"])   # 이제 됨
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertEqual(by.short, 0.0)


class VanishedShortTest(unittest.TestCase):
    """
    거래소가 우리 계산보다 먼저 숏을 강제청산한 경우 (2026-10-23 빈틈 메우기)

    전에는 포지션이 OPEN 인 채 코인만 들고 있었고(가격 노출), 청산하려 하면 숏 닫기가 거절되어 CLOSING 에 갇혔다.
    """

    def _liquidate_on_exchange(self, by):
        """바이비트가 숏을 강제청산함: 포지션 사라지고 증거금은 돌아오지 않는다"""
        by.short, by.margin, by.entry = 0.0, 0.0, 0.0

    @staticmethod
    def _engine_like_scan(broker, s, quote):
        """엔진 step 의 해당 부분: sync → 숏 사라진 포지션을 강제청산 경로로 정리"""
        broker.sync(s, {"XRP": quote}, ["XRP"])
        for pos in broker.vanished_shorts(s):
            broker.close_hedge(s, pos, broker.value_liquidation(pos, quote), ExitReason.LIQUIDATION, liquidated=True)

    def test_open_position_with_no_short_is_closed_next_scan(self):
        """OPEN 인데 거래소 숏이 0 → 다음 sync 에서 바로 코인을 팔아 LIQUIDATION 으로 마무리"""
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            self._liquidate_on_exchange(by)
            prices.coin_bid, prices.coin_ask = 5600.0, 5601.0   # 코인은 2배 (강제청산 상황)
            broker.ex.refresh()
            self.assertEqual([p.id for p in broker.vanished_shorts(s)], [pos.id])
            self._engine_like_scan(broker, s, prices.quote())
            s.refresh(pos)
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertEqual(pos.exit_reason, ExitReason.LIQUIDATION)
            self.assertAlmostEqual(up.bal["XRP"], 0.0, places=6)            # 코인 즉시 정리
            self.assertAlmostEqual(pos.bybit_leg_usdt, -pos.bybit_margin_usdt)  # 증거금 손실로 기록
            self.assertGreater(pos.upbit_leg_usdt, 90)                       # 코인 2배 → 업비트 몫이 메움
            self.assertEqual(by.calls.count("close_short"), 0)               # 없는 숏에 주문 안 냄
            self.assertTrue(broker.entries_allowed(s)[0])                    # 정리됐으니 막히지 않는다

    def test_close_when_short_already_gone_finishes_instead_of_sticking(self):
        """목표 도달로 청산하는데 숏이 사라져 있음 → 거절·CLOSING 갇힘 대신 남은 양을 강제청산으로 마무리"""
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            val = broker.value_exit(pos, prices.quote())
            self._liquidate_on_exchange(by)
            broker.ex.refresh()
            pnl = broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
            self.assertIsNotNone(pnl)
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertEqual(pos.exit_reason, ExitReason.LIQUIDATION)
            self.assertEqual(by.calls.count("close_short"), 0)
            self.assertAlmostEqual(up.bal["XRP"], 0.0, places=6)
            self.assertAlmostEqual(pos.bybit_leg_usdt, -pos.bybit_margin_usdt)

    def test_partial_close_then_liquidated_records_both(self):
        """숏 60% 닫은 뒤 나머지가 강제청산됨 → 닫은 몫 손익 + 남은 40% 증거금 손실"""
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            margin = pos.bybit_margin_usdt
            by.partial["close_short"] = 0.6
            val = broker.value_exit(pos, prices.quote())

            orig = by.create_order

            def close_then_vanish(symbol, type_, side, amount, price=None, params=None):
                r = orig(symbol, type_, side, amount, price, params)
                if side == "buy":            # 60% 체결 직후 나머지는 거래소가 청산
                    self._liquidate_on_exchange(by)
                return r

            by.create_order = close_then_vanish
            pnl = broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
            self.assertIsNotNone(pnl)
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertEqual(by.calls.count("close_short"), 1)
            # 바이비트 몫 = 닫은 30개 손익(가격 거의 같음 → ≈ −수수료) + 남은 20개 증거금 손실(40%)
            self.assertAlmostEqual(pos.bybit_leg_usdt, -margin * 0.4, delta=0.1)

    def test_live_position_still_there_is_left_alone(self):
        """숏이 멀쩡히 있으면 아무것도 안 한다"""
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            self.assertEqual(broker.vanished_shorts(s), [])
            self._engine_like_scan(broker, s, prices.quote())
            self.assertEqual(pos.status, PositionStatus.OPEN)
            self.assertEqual(by.short, 50.0)


class ParallelLegsTest(unittest.TestCase):
    """청산(코인 매도 ∥ 숏 닫기)·진입(숏 열기 ∥ USDT 매도) 을 동시에 낼 때 한쪽만 실패·결과 모름"""

    def test_close_legs_run_at_the_same_time(self):
        """코인 매도와 숏 닫기가 겹친다 (각 0.3초면 순차 0.6초, 동시 0.3초 남짓)"""
        import time
        prices, up, by, broker, sf = make_live()
        slow_up, slow_by = up.create_order, by.create_order
        up.create_order = lambda *a, **k: (time.sleep(0.3), slow_up(*a, **k))[1]
        by.create_order = lambda *a, **k: (time.sleep(0.3), slow_by(*a, **k))[1]
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            val = broker.value_exit(pos, prices.quote())
            t0 = time.monotonic()
            pnl = broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
            took = time.monotonic() - t0
            self.assertIsNotNone(pnl)
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertLess(took, 0.95, f"{took:.2f}초 — 코인 매도·숏 닫기·USDT 매수가 전부 순차였다")

    def test_close_coin_sell_fails_short_close_succeeds(self):
        """동시에 냈는데 코인 매도만 거절 → 숏은 닫힌 채 CLOSING → 다음 스캔에 코인만 다시 (숏 주문 중복 없음)"""
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            up.fail.add("sell XRP/KRW")
            val = broker.value_exit(pos, prices.quote())
            self.assertIsNone(broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT))
            self.assertEqual(pos.status, PositionStatus.CLOSING)
            self.assertEqual(by.short, 0.0)                       # 숏은 이미 닫힘
            self.assertGreater(up.bal["XRP"], 0)                  # 코인은 아직
            broker.sync(s, {"XRP": prices.quote()}, ["XRP"])
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertAlmostEqual(up.bal["XRP"], 0.0, places=6)
            self.assertEqual(by.calls.count("close_short"), 1)    # 숏 닫기는 한 번만
            self.assertEqual(up.calls.count("sell XRP/KRW"), 2)   # 코인 매도만 다시

    def test_close_short_unknown_coin_sold(self):
        """동시에 냈는데 숏 닫기 결과를 모름 → 코인 매도는 기록되고 SENT 는 남음 → 조회되면 CLOSED, 중복 없음"""
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            by.lost.add("close_short")
            by.lookup_down = True
            val = broker.value_exit(pos, prices.quote())
            self.assertIsNone(broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT))
            self.assertEqual(pos.status, PositionStatus.CLOSING)
            self.assertFalse(broker.entries_allowed(s)[0])
            sides = {(f.side, f.state) for f in s.query(Fill).filter(Fill.position_id == pos.id)}
            self.assertIn(("sell", "DONE"), sides)
            self.assertIn(("close_short", "SENT"), sides)
            by.lookup_down = False
            broker.sync(s, {"XRP": prices.quote()}, ["XRP"])
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertEqual(by.calls.count("close_short"), 1)
            self.assertEqual(by.short, 0.0)

    def test_entry_legs_run_at_the_same_time_and_short_reject_unwinds_usdt(self):
        """숏 열기 ∥ USDT 매도. 숏이 거절되면 이미 판 USDT 를 되사서 ABORTED (중복 없음)"""
        prices, up, by, broker, sf = make_live()
        by.fail.add("open_short")
        with sf() as s:
            with self.assertRaises(EntryRejected):
                broker.open_hedge(s, prices.quote(), 50.0)
            pos = s.query(Position).one()
            self.assertEqual(pos.status, PositionStatus.ABORTED)
            self.assertEqual(by.short, 0.0)
            self.assertEqual(up.calls.count("sell USDT/KRW"), 1)
            self.assertEqual(up.calls.count("buy USDT/KRW"), 1)      # 되사기
            self.assertAlmostEqual(up.bal["USDT"], 500.0, delta=0.5)  # 수수료만큼만 빠짐

    def test_entry_partial_short_is_balanced_by_trim(self):
        """숏이 80% 만 체결 (느린 주문 취소) → USDT 는 계획대로 팔렸으니 코인이 많다 → 크기 맞추기가 코인을 판다"""
        prices, up, by, broker, sf = make_live()
        by.partial["open_short"] = 0.8
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            self.assertEqual(pos.status, PositionStatus.OPEN)
            self.assertAlmostEqual(pos.bybit_qty, 40.0)
            usdt_mid = (prices.usdt_bid + prices.usdt_ask) / 2
            coin_usdt = pos.upbit_qty * prices.coin_bid / usdt_mid
            short_usdt = by.short * prices.perp_bid
            self.assertLess(abs(short_usdt - coin_usdt) / short_usdt, 0.011)
            self.assertAlmostEqual(up.bal["XRP"], pos.upbit_qty, places=6)


class MinOrderTest(unittest.TestCase):
    """최소 주문 금액: 하드코딩 아님 (실거래는 거래소 값, 없으면 설정값)"""

    def test_upbit_min_from_exchange_then_fallback(self):
        prices = Prices()
        up, by = FakeUpbit(prices), FakeBybit(prices)
        up.private_get_orders_chance = lambda p: {"market": {"bid": {"min_total": "5000"},
                                                              "ask": {"min_total": "7000"}}}
        ex = LiveExecutor(up, by, settings(upbit_min_order_krw=1234), sleep=lambda _x: None)
        self.assertEqual(ex.min_order_krw("XRP"), 7000.0)  # 매수·매도 중 큰 값

        def down(_p):
            raise ConnectionError("조회 실패")
        up2 = FakeUpbit(prices)
        up2.private_get_orders_chance = down
        ex2 = LiveExecutor(up2, by, settings(upbit_min_order_krw=1234), sleep=lambda _x: None)
        self.assertEqual(ex2.min_order_krw("XRP"), 1234.0)  # 못 받으면 설정값

    def test_paper_uses_setting(self):
        broker = PaperBroker(FEES, 0.3, upbit_min_order_krw=8000)
        self.assertEqual(broker.ex.min_order_krw("XRP"), 8000)

    def test_engine_rejects_below_bybit_min_notional(self):
        from kimp.engine import Engine

        class M:
            def round_qty(self, c, q):
                return round(q, 1)

            def min_qty(self, c):
                return 0.1

            def min_notional_usdt(self, c):
                return 5.0

        prices = Prices()
        broker = PaperBroker(FEES, 0.3)
        e = Engine(Settings(coins=["XRP"], fees=FEES), M(), None, broker, make_session_factory(":memory:"), ["XRP"])
        self.assertIsNone(e._plan_bybit_qty("XRP", prices.quote(), 4.0))  # 4 USDT < 최소 5 USDT
        self.assertIsNotNone(e._plan_bybit_qty("XRP", prices.quote(), 10.0))


class LiveTransferTest(unittest.TestCase):
    def test_transfer_pending_then_arrives(self):
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            sent = broker.transfer(s, 100.0, 1.0, reason="테스트")
            self.assertEqual(sent, 100.0)
            self.assertEqual(up.sent[1], "BYBIT-ADDR")
            self.assertEqual(up.sent[2], {"net_type": "TRX"})
            t = s.query(Transfer).one()
            self.assertEqual(t.status, TransferStatus.REQUESTED)
            # 송금 중: 또 안 보냄, 비율 계산엔 받는 쪽으로 침
            self.assertEqual(broker.transfer(s, 50.0, 1.0), 0.0)
            _up, by_cap = broker.capital_split(s, [], {"XRP": prices.quote()})
            self.assertAlmostEqual(by_cap, 600 + 99)
            # 바이비트에 입금 도착
            by.deposits.append({"status": "ok", "amount": 99.0})
            by.free_usdt += 99.0
            broker.sync(s, {"XRP": prices.quote()}, ["XRP"])
            self.assertEqual(t.status, TransferStatus.CONFIRMED)

    def test_bybit_withdraw_failure_moves_funds_back(self):
        """바이비트 출금이 거절되면 펀딩 계좌로 옮겼던 돈을 통합 계좌로 되돌린다"""
        prices, up, by, broker, sf = make_live()
        by.fail.add("withdraw")
        with sf() as s:
            broker.ex.refresh()
            sent = broker.transfer(s, -50.0, 1.3, reason="테스트")
            self.assertEqual(sent, 0.0)
            moves = [c for c in by.calls if isinstance(c, tuple) and c[0] == "transfer"]
            self.assertEqual(moves, [("transfer", "unified", "funding"), ("transfer", "funding", "unified")])
            self.assertEqual(s.query(Transfer).one().status, TransferStatus.FAILED)

    def test_bybit_to_upbit_moves_to_funding_first(self):
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            self.assertEqual(broker.transfer(s, -100.0, 1.0), -100.0)
            self.assertIn(("transfer", "unified", "funding"), by.calls)

    def test_daily_cap_and_min(self):
        prices, up, by, broker, sf = make_live()
        broker.transfer_max_usdt_per_day = 120.0
        with sf() as s:
            self.assertEqual(broker.transfer(s, 5.0, 1.0), 0.0)  # 최소 10 USDT 미만
            self.assertEqual(broker.transfer(s, 100.0, 1.0), 100.0)
            s.query(Transfer).one().status = TransferStatus.CONFIRMED
            s.commit()
            self.assertEqual(broker.transfer(s, 100.0, 1.0), 20.0)  # 오늘 남은 한도 20

    def test_late_transfer_alerts_once(self):
        prices, up, by, broker, sf = make_live()
        with sf() as s:
            broker.transfer(s, 100.0, 1.0)
            t = s.query(Transfer).one()
            t.requested_at = utcnow() - timedelta(minutes=90)
            s.commit()
            with self.assertLogs("kimp.broker", level=logging.CRITICAL):
                broker.sync(s, {"XRP": prices.quote()}, ["XRP"])
            self.assertEqual(t.alerted, 1)


class DryRunTest(unittest.TestCase):
    def test_dry_run_never_orders_and_starts_from_real_balances(self):
        prices = Prices()
        up, by = FakeUpbit(prices), FakeBybit(prices)
        live = LiveExecutor(up, by, settings(), sleep=lambda _x: None)
        broker = HedgeBroker(DryRunExecutor(FEES, 1.0, live), FEES, 0.3)
        sf = make_session_factory(":memory:")
        with sf() as s:
            broker.ensure_initial_balances(s, 10_000, 0.5)  # 시작 자본 설정은 무시, 실제 잔고 복사
            self.assertEqual(broker.balance(s, UPBIT_USDT), 500.0)
            self.assertEqual(broker.balance(s, BYBIT_USDT), 600.0)
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            val = broker.value_exit(pos, prices.quote())
            broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
            broker.transfer(s, 50.0, 1.0)
        self.assertEqual(up.orders, {})
        self.assertEqual(by.orders, {})
        self.assertNotIn("withdraw", up.calls)


class SamePathTest(unittest.TestCase):
    def test_paper_uses_same_unwind_logic(self):
        """페이퍼도 같은 되돌리기 흐름 (실행기만 다름)"""
        prices = Prices()
        broker = PaperBroker(FEES, 0.3)
        sf = make_session_factory(":memory:")
        with sf() as s:
            broker.ensure_initial_balances(s, 1000, 0.5)
            broker.ex.fail_next.add("buy_coin_with_krw")
            with self.assertRaises(EntryRejected):
                broker.open_hedge(s, prices.quote(), 50.0)
            self.assertEqual(s.query(Position).one().status, PositionStatus.ABORTED)
            self.assertAlmostEqual(broker.ex.short_qty(s, "XRP"), 0.0)


class SafetyAndConfigTest(unittest.TestCase):
    def test_stop_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "STOP")
            safety = Safety(stop_file=path)
            sf = make_session_factory(":memory:")
            with sf() as s:
                self.assertTrue(safety.entries_allowed(s)[0])
                open(path, "w").close()
                self.assertFalse(safety.entries_allowed(s)[0])

    def test_daily_loss_limit(self):
        safety = Safety(daily_loss_limit_usdt=10)
        sf = make_session_factory(":memory:")
        with sf() as s:
            for loss in (-4.0, -7.0):
                s.add(Position(coin="XRP", status=PositionStatus.CLOSED, usdt_sold=0, entry_usdt_price_krw=0,
                               entry_usdt_fee_krw=0, upbit_qty=0, entry_upbit_price_krw=0, entry_upbit_fee_krw=0,
                               bybit_qty=0, entry_bybit_price_usdt=0, entry_bybit_fee_usdt=0, entry_premium=0,
                               bybit_margin_usdt=0, liquidation_price=0, closed_at=utcnow(), net_pnl_usdt=loss))
            s.commit()
            ok, why = safety.entries_allowed(s)
            self.assertFalse(ok)
            self.assertIn("한도", why)

    def test_live_needs_confirm_and_keys(self):
        from kimp.config import validate_settings
        with self.assertRaises(ValueError):
            validate_settings(settings(live_confirm=""))
        with self.assertRaises(ValueError):
            validate_settings(settings(upbit_access_key=""))
        validate_settings(settings(live_confirm="", live_dry_run=True))  # 드라이런은 YES 없이 됨
        self.assertEqual(settings().mode, "live")
        self.assertEqual(settings(live_dry_run=True).mode, "dry_run")
        self.assertEqual(Settings().mode, "paper")


class TelegramTest(unittest.TestCase):
    def test_handler_filters_and_commands_check_chat(self):
        from kimp.notify import TelegramCommands, TelegramLogHandler, TelegramNotifier
        sent = []
        n = TelegramNotifier("T", "42", post=lambda url, data, timeout=10: sent.append(data) or {"ok": True})
        h = TelegramLogHandler(n)
        log = logging.getLogger("kimp.test_tg")
        log.addHandler(h)
        log.setLevel(logging.INFO)
        try:
            log.info("평범한 로그")
            log.info("[진입] #1 XRP ...")
            log.warning("경고 하나")
        finally:
            log.removeHandler(h)
        import time as _t
        for _ in range(50):
            if len(sent) >= 2:
                break
            _t.sleep(0.02)
        texts = [d["text"] for d in sent]
        self.assertEqual(len(texts), 2)
        self.assertTrue(any("[진입]" in t for t in texts))
        cmds = TelegramCommands(n, {"/status": lambda: "OK"})
        self.assertEqual(cmds.handle({"message": {"chat": {"id": 42}, "text": "/status"}}), "OK")
        self.assertIsNone(cmds.handle({"message": {"chat": {"id": 7}, "text": "/status"}}))  # 남의 채팅 무시

    def test_pretty_splits_pipes_and_bolds_head(self):
        """한 줄 로그 → 머리 굵게 + 항목마다 줄바꿈 + HTML 특수문자 처리"""
        from kimp.notify import plain, pretty
        msg = ("[청산 · 익절] #3 IRYS | 순손익 -0.16 USDT (-0.82%) | 김프 진입 -0.268% → 청산 -0.605% | "
               "업비트 USDT 19.74 → 21.53 (+1.79) | 바이비트 -1.91 USDT | 펀딩 +0.00 · 비용 0.09 | 보유 5.0시간")
        out = pretty(msg, "🚨 ")
        lines = out.split("\n")
        self.assertEqual(lines[0], "🚨 <b>[청산 · 익절]</b> <b>#3 IRYS</b>")
        self.assertEqual(lines[1], "• <b>순손익 -0.16 USDT (-0.82%)</b>")   # 결과는 굵게
        self.assertEqual(lines[2], "• 김프 진입 -0.268% → 청산 -0.605%")
        self.assertEqual(len(lines), 7)
        # 송금 번호만 있는 머리 / 명령 답장(줄바꿈 항목)도 같은 모양
        t = pretty("[송금 도착] #8 업비트 → 바이비트 | 받은 금액 130.36 USDT | 걸린 시간 3분").split("\n")
        self.assertEqual(t[0], "<b>[송금 도착]</b> <b>#8</b> 업비트 → 바이비트")
        s = pretty("[현황] 실거래 · 10-24 01:40\n잔고 업비트 627.10 / 바이비트 803.29 USDT\n포지션 0 / 3").split("\n")
        self.assertEqual(s, ["<b>[현황]</b>", "• 실거래 · 10-24 01:40", "• 잔고 업비트 627.10 / 바이비트 803.29 USDT", "• 포지션 0 / 3"])
        # 긴 항목은 ' / ' 에서 한 번 더
        entry = ("[진입] #4 IRYS | 업비트 USDT 19.78개 매도(1335.0원) → 690.707개 매수(38.20원) / 바이비트 690개 숏(0.028647, 1배, "
                 "강제청산 0.057151) | 진입 김프 -0.114% (판단 -0.204%, 기준 -0.13%) → 목표 0.20% 되려면 김프 약 +0.52%")
        e = pretty(entry).split("\n")
        self.assertEqual(e[0], "<b>[진입]</b> <b>#4 IRYS</b>")
        self.assertTrue(e[2].startswith("• 바이비트 690개 숏"))
        # 태그 없는 문장과 HTML 특수문자
        self.assertEqual(pretty("a < b & c", "⚠️ "), "⚠️ a &lt; b &amp; c")
        self.assertEqual(plain(pretty("[태그] x < y")), "[태그]" + chr(10) + "• x < y")   # 번호·코인이 없으면 몸통은 다음 줄

    def test_worker_falls_back_to_plain_text_when_html_rejected(self):
        """HTML 전송이 거절되면 평문으로 한 번 더 보낸다 (알림을 잃지 않게)"""
        from kimp.notify import TelegramNotifier
        sent = []

        def post(url, data, timeout=10.0):
            sent.append(dict(data))
            if data.get("parse_mode") == "HTML":
                raise RuntimeError("400 can't parse entities")
            return {"ok": True}

        n = TelegramNotifier("T", "42", post=post)
        n.send("<b>[진입]</b> <b>#1 XRP</b>\n김프 -1%")
        self.assertTrue(n.flush(timeout=5))
        self.assertEqual([d.get("parse_mode") for d in sent], ["HTML", None])
        self.assertEqual(sent[1]["text"], "[진입] #1 XRP\n김프 -1%")

    def test_flush_sends_queued_before_exit(self):
        """
        종료 직전 flush() 가 큐에 남은 알림을 다 보낸다 (실거래에서 겪은 버그)

        보내기는 데몬 스레드가 한다 → 프로그램이 끝나면 파이썬이 즉시 죽인다.
        2026-10-23 ONE_SHOT 실거래에서 16초 만에 끝나면서 '청산' 알림이 통째로 사라졌다.
        여기서는 전송이 느린 상황을 만들어, flush 없이는 못 보내고 flush 하면 다 보내는 것을 고정한다.
        """
        import time as _t

        from kimp.notify import TelegramNotifier
        sent = []

        def slow_post(url, data, timeout=10.0):
            _t.sleep(0.3)              # 네트워크가 느린 상황
            sent.append(data["text"])
            return {"ok": True}

        n = TelegramNotifier("T", "42", post=slow_post)
        for i in range(3):
            n.send(f"알림 {i}")
        self.assertLess(len(sent), 3)  # 아직 보내는 중 — 여기서 끝내면 사라진다
        self.assertTrue(n.flush(timeout=5))
        self.assertEqual(len(sent), 3)

    def test_flush_gives_up_after_timeout(self):
        """전송이 영영 안 끝나도 종료를 막지 않는다 (False 만 돌려주고 넘어간다)"""
        import time as _t

        from kimp.notify import TelegramNotifier

        def stuck_post(url, data, timeout=10.0):
            _t.sleep(30)
            return {"ok": True}

        n = TelegramNotifier("T", "42", post=stuck_post)
        for i in range(5):
            n.send(f"안 나가는 알림 {i}")
        began = _t.monotonic()
        self.assertFalse(n.flush(timeout=0.5))
        self.assertLess(_t.monotonic() - began, 3.0)   # 오래 붙잡지 않는다


if __name__ == "__main__":
    unittest.main()


class WithdrawArgsTest(unittest.TestCase):
    """
    ccxt 출금 함수 인자 모양 검증 (실거래에서 터졌던 버그)

    2026-10-22 첫 실거래에서 송금이 이렇게 실패했다.
        upbit.withdraw() takes from 4 to 5 positional arguments but 6 were given
    ccxt 는 두 거래소 모두 withdraw(코드, 수량, 주소, params) 이고 태그를 따로 받지 않는다.
    가짜 거래소가 옛 시그니처(tag 를 4번째로 받음)였기 때문에 테스트가 이 버그를 못 잡았다.
    → 가짜도 실제와 같은 인자로 맞추고, 실제 ccxt 클래스로 인자 바인딩까지 확인한다.
    """

    def test_real_ccxt_signature(self):
        import inspect

        import ccxt
        for cls, params in ((ccxt.upbit, {"net_type": "TRX"}), (ccxt.bybit, {"network": "TRC20"})):
            sig = inspect.signature(cls.withdraw)
            sig.bind(object(), "USDT", 10.0, "주소", params)              # 우리가 쓰는 모양 → 통과해야 한다
            with self.assertRaises(TypeError):                            # 태그를 4번째로 넘기면 실패해야 한다
                sig.bind(object(), "USDT", 10.0, "주소", "태그", params)

    def test_tag_goes_into_params(self):
        """태그가 있으면 params['tag'] 로 들어간다 (트론은 태그가 없어서 보통 비어 있다)"""
        from dataclasses import replace as dc_replace

        seen = {}

        class OnlyWithdraw:
            """ccxt 와 같은 인자만 받는 가짜 (태그를 4번째로 넘기면 TypeError 가 난다)"""

            def withdraw(self, code, amount, address, params=None):
                seen.update({"code": code, "amount": amount, "address": address, "params": params or {}})
                return {"id": "w-1"}

        prices = Prices()
        s = dc_replace(settings(), bybit_deposit_address="주소", bybit_deposit_tag="메모123",
                       upbit_transfer_network="TRX")
        ex = LiveExecutor(OnlyWithdraw(), FakeBybit(prices), s, sleep=lambda _x: None)
        with make_session_factory(":memory:")() as session:
            ex.withdraw(session, True, 100.0, "TRX")
        self.assertEqual(seen["params"]["net_type"], "TRX")
        self.assertEqual(seen["params"]["tag"], "메모123")
        self.assertEqual(seen["address"], "주소")
        self.assertEqual(seen["amount"], 100.0)


class TransferDecimalsTest(unittest.TestCase):
    """
    송금 금액 소수점 자리수 (실거래에서 거절당한 버그)

    2026-10-22 첫 실거래에서 업비트가 이렇게 거절했다.
        846.6747287521739 → "출금 소수점 자리수를 초과하였습니다. 6 이하 소수점 자리수만 적용됩니다."
    2026-10-24 바이비트는 더 짧다. 73.684303 → "withdraws amount scale more than 4"
    자동 계산한 송금액은 소수점이 길게 나오므로 둘 중 짧은 4자리로 **내려서** 보낸다 (올리면 잔고를 넘을 수 있다).
    """

    def test_amount_is_floored_to_four_decimals(self):
        from kimp.broker import TRANSFER_DECIMALS
        self.assertEqual(TRANSFER_DECIMALS, 4)
        prices, up, by, broker, sf = make_live()
        up.bal["USDT"] = 1_000.0
        with sf() as session:
            broker.ensure_initial_balances(session, 0.0, 0.5)
            sent = broker.transfer(session, 846.6747287521739, 0.0, reason="테스트")
            rows = broker.transfers(session)
        self.assertAlmostEqual(sent, 846.6747, places=9)            # 4자리로 잘렸다
        self.assertAlmostEqual(rows[-1].amount_usdt, 846.6747, places=9)   # 기록도 같은 값
        self.assertEqual(len(str(round(sent, 9)).split(".")[-1]), 4)

    def test_small_amount_still_blocked_by_minimum(self):
        """자리수를 자르는 것과 최소 금액 규칙은 따로다"""
        prices, up, by, broker, sf = make_live()
        up.bal["USDT"] = 1_000.0
        with sf() as session:
            broker.ensure_initial_balances(session, 0.0, 0.5)
            self.assertEqual(broker.transfer(session, 9.9999999, 0.0, reason="테스트"), 0.0)  # 최소 10


class PolicyRejectTest(unittest.TestCase):
    """
    거래소 규정 거절은 '우리 실패' 로 세지 않는다 (2026-10-22 실거래에서 겪음)

    업비트 출금지연제: 원화 입금 후 24시간 동안 그 금액 상당의 코인을 못 보낸다.
    이걸 연속 실패로 세면 3번 만에 신규 진입이 막혀서, 풀린 뒤에도 사람이 /resume 을 눌러야 한다.
    → 규정 거절은 경고만 하고 10분 뒤 다시 시도한다.
    """

    def test_classify(self):
        from kimp.broker import is_policy_reject
        self.assertTrue(is_policy_reject("출금 가능한 수량을 초과하였습니다. 출금지연제 적용(공지 참고)에 따라"))
        self.assertTrue(is_policy_reject("일일 출금 한도를 초과하였습니다"))
        self.assertFalse(is_policy_reject("출금 소수점 자리수를 초과하였습니다"))   # 우리 버그였다
        self.assertFalse(is_policy_reject("withdraw() takes from 4 to 5 positional arguments"))
        self.assertFalse(is_policy_reject("Insufficient balance"))

    def test_policy_reject_does_not_block_entries(self):
        """규정 거절 → 연속 실패 0, 송금 기록은 FAILED, 잠시 뒤 재시도"""
        from kimp.broker import POLICY_RETRY_SEC
        prices, up, by, broker, sf = make_live()
        up.bal["USDT"] = 1_000.0
        up.fail_once["withdraw"] = "출금 가능한 수량을 초과하였습니다. 출금지연제 적용(공지 참고)에 따라"
        with sf() as session:
            broker.ensure_initial_balances(session, 0.0, 0.5)
            sent = broker.transfer(session, 500.0, 0.0, reason="테스트")
            self.assertEqual(sent, 0.0)
            self.assertEqual(broker.safety.failures, 0)          # 연속 실패로 세지 않는다
            self.assertTrue(broker.safety.entries_allowed(session)[0])   # 진입도 막히지 않는다
            from sqlalchemy import select as _select   # transfers() 는 실패 건을 빼고 주므로 직접 본다
            rows = session.scalars(_select(Transfer).order_by(Transfer.id)).all()
            self.assertEqual(rows[-1].status, TransferStatus.FAILED)
            self.assertIn("출금지연", rows[-1].note)
            # 바로 다시 찔러도 보류 중이라 요청하지 않는다
            self.assertEqual(broker.transfer(session, 500.0, 0.0, reason="테스트"), 0.0)
        self.assertGreater(broker._policy_block_until, 0)
        self.assertLessEqual(broker._policy_block_until - broker._now().timestamp(), POLICY_RETRY_SEC + 1)

    def test_real_failure_still_counts(self):
        """우리 잘못인 실패(잔고·버그)는 그대로 연속 실패로 센다"""
        prices, up, by, broker, sf = make_live()
        up.bal["USDT"] = 1_000.0
        up.fail_once["withdraw"] = "Insufficient balance"
        with sf() as session:
            broker.ensure_initial_balances(session, 0.0, 0.5)
            broker.transfer(session, 500.0, 0.0, reason="테스트")
            self.assertEqual(broker.safety.failures, 1)

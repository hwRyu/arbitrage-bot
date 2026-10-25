"""
업비트 코인 주문 지정가 FOK (UPBIT_COIN_ORDER_TYPE=fok) — 호가 단위, 설정, 페이퍼·실거래 실행기, 브로커 흐름, 엔진 재시도

왜 이 기능이 있나: 시장가는 판단 뒤 1초 사이 호가가 한 칸 비껴도 그 가격에 잡힌다 (CAP #12 2026-10-25: 140원 판단
→ 141원 체결, 한 칸 0.71%). FOK 는 판단한 가격에 전량 즉시 체결 아니면 취소라 판단보다 나쁘게는 절대 안 잡힌다.
    진입 놓침 → 숏은 아직 없으니 판 USDT 만 되삼 (ABORTED / ENTRY_MISSED)
    익절 놓침 → 포지션 그대로(OPEN), 엔진이 FOK_RETRY_SEC 뒤 다시 판단

실행: python -m unittest discover -s tests -v
"""
import logging
import os
import unittest
from unittest import mock

from kimp.broker import EntryMissed, ExitDeferred, HedgeBroker, PaperBroker
from kimp.config import load_settings
from kimp.db import ExitReason, Fill, Position, PositionStatus, make_session_factory
from kimp.engine import FOK_RETRY_SEC
from kimp.executor import PaperExecutor
from kimp.live_executor import LiveExecutor
from kimp.premium import OrderBook, Quote, upbit_krw_tick, upbit_limit_price
from kimp.safety import Safety
from test_live_broker import DEEP, FEES, FakeBybit, FakeUpbit, JumpyUpbit, Prices, settings


class TickTest(unittest.TestCase):
    def test_tick_table(self):
        """가격대별 한 칸 (실거래로 확인한 값 포함)"""
        self.assertEqual(upbit_krw_tick(0.5), 0.001)
        self.assertEqual(upbit_krw_tick(9.99), 0.01)
        self.assertEqual(upbit_krw_tick(93.5), 0.1)      # ZAMA
        self.assertEqual(upbit_krw_tick(140), 1)         # CAP (한 칸 0.71%)
        self.assertEqual(upbit_krw_tick(999), 1)
        self.assertEqual(upbit_krw_tick(1_000), 5)
        self.assertEqual(upbit_krw_tick(50_000), 10)
        self.assertEqual(upbit_krw_tick(3_000_000), 1_000)

    def test_limit_price_never_worse_than_judged(self):
        """살 때 내림, 팔 때 올림. 이미 칸에 맞는 값은 그대로"""
        self.assertEqual(upbit_limit_price(140.37, "buy"), 140)
        self.assertEqual(upbit_limit_price(140.37, "sell"), 141)
        self.assertEqual(upbit_limit_price(93.47, "sell"), 93.5)
        self.assertEqual(upbit_limit_price(93.5, "buy"), 93.5)
        self.assertEqual(upbit_limit_price(2801.0, "buy"), 2800)   # 1,000~10,000원은 5원 칸
        self.assertEqual(upbit_limit_price(2801.0, "sell"), 2805)


class ConfigTest(unittest.TestCase):
    def _load(self, env):
        with mock.patch.dict(os.environ, env, clear=True):
            return load_settings(env_file=None)

    def test_default_is_market_and_fok_is_accepted(self):
        self.assertEqual(self._load({"COINS": "XRP"}).upbit_coin_order_type, "market")
        self.assertEqual(self._load({"COINS": "XRP", "UPBIT_COIN_ORDER_TYPE": "FOK"}).upbit_coin_order_type, "fok")

    def test_unknown_value_rejected(self):
        with self.assertRaisesRegex(ValueError, "UPBIT_COIN_ORDER_TYPE"):
            self._load({"COINS": "XRP", "UPBIT_COIN_ORDER_TYPE": "ioc"})


class PaperFokTest(unittest.TestCase):
    """페이퍼 실행기: 지정가 안쪽 호가로 전량 채워질 때만 체결, 아니면 qty 0 (잔고 변화 없음)"""

    def setUp(self):
        self.sf = make_session_factory(":memory:")
        self.ex = PaperExecutor(FEES, 1.0)
        with self.sf() as s:
            self.ex.ensure_initial(s, 10_000, 0.5)
            s.commit()
        # 1,000~10,000원은 호가 한 칸이 5원 → 값을 칸에 맞춘다 (1,385 / 1,390 / 1,395)
        up = OrderBook(bids=[(1385.0, 1000.0)], asks=[(1390.0, 1000.0), (1395.0, DEEP)])
        self.quote = Quote(coin="XRP", upbit=up, bybit=OrderBook(bids=[(1.0, DEEP)], asks=[(1.0001, DEEP)]),
                           usdt=OrderBook(bids=[(1400.0, DEEP)], asks=[(1400.0, DEEP)]))

    def test_buy_fok(self):
        with self.sf() as s:
            self.ex._add(s, "upbit:KRW", 10_000_000)
            miss = self.ex.buy_coin_with_krw(s, self.quote, "XRP", 100_000, limit_price=1385.0)   # 매도호가 1390 > 지정가
            self.assertEqual(miss.qty, 0.0)
            self.assertAlmostEqual(self.ex._get(s, "upbit:KRW"), 10_000_000)                      # 아무 일 없음
            hit = self.ex.buy_coin_with_krw(s, self.quote, "XRP", 100_000, limit_price=1390.0)
            self.assertAlmostEqual(hit.qty, 100_000 / 1390.0)
            self.assertAlmostEqual(hit.avg_price, 1390.0, places=6)
            # 1390원 줄 물량(1000개)보다 많이 사려 하면 1395원 줄까지 가야 하므로 FOK 는 취소
            big = self.ex.buy_coin_with_krw(s, self.quote, "XRP", 1390.0 * 1500, limit_price=1390.0)
            self.assertEqual(big.qty, 0.0)

    def test_sell_fok(self):
        with self.sf() as s:
            self.ex._add(s, "upbit:XRP", 100.0)
            self.assertEqual(self.ex.sell_coin(s, self.quote, "XRP", 10.0, limit_price=1385.5).qty, 0.0)  # 매수호가 1385 < 지정가
            hit = self.ex.sell_coin(s, self.quote, "XRP", 10.0, limit_price=1385.0)
            self.assertEqual((hit.qty, hit.avg_price), (10.0, 1385.0))

    def test_paper_broker_round_trip_with_fok(self):
        """페이퍼 브로커 fok: 판단 호가가 곧 체결 호가라 진입·익절이 시장가와 같은 값으로 끝난다"""
        broker = PaperBroker(FEES, 0.3, coin_order_type="fok")
        with self.sf() as s:
            broker.ex.ensure_initial(s, 10_000, 0.5)
            s.commit()
            pos = broker.open_hedge(s, self.quote, 100.0)
            self.assertEqual(pos.status, PositionStatus.OPEN)
            self.assertEqual(pos.entry_upbit_price_krw, 1390.0)
            self.assertAlmostEqual(pos.bybit_qty, pos.upbit_qty, places=6)
            up2 = OrderBook(bids=[(1420.0, DEEP)], asks=[(1425.0, DEEP)])
            q2 = Quote(coin="XRP", upbit=up2, bybit=self.quote.bybit, usdt=self.quote.usdt)
            pnl = broker.close_hedge(s, pos, broker.value_exit(pos, q2), ExitReason.TAKE_PROFIT)
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertEqual(pos.exit_upbit_price_krw, 1420.0)
            self.assertGreater(pnl.net_usdt, 0)


def make_fok_live(upbit_cls=FakeUpbit):
    """실거래 흉내 + fok 브로커. 코인 호가를 5원 칸에 맞춘다 (2,800 / 2,805원) — 판단가가 곧 지정가가 되게"""
    prices = Prices()
    prices.coin_bid, prices.coin_ask = 2800.0, 2805.0
    up, by = upbit_cls(prices), FakeBybit(prices)
    ex = LiveExecutor(up, by, settings(), sleep=lambda _x: None, order_timeout_sec=0.01)
    broker = HedgeBroker(ex, FEES, 0.3, leverage=1.0, safety=Safety(3), auto_transfer=True, network="TRX",
                         coin_order_type="fok")
    return prices, up, by, broker, make_session_factory(":memory:")


class LiveFokEntryTest(unittest.TestCase):
    def test_entry_order_and_limit_params(self):
        """USDT 매도 → 코인 지정가 FOK(판단가) → 산 개수만큼 숏. 주문에 time_in_force=fok 가 붙는다"""
        prices, up, by, broker, sf = make_fok_live()
        with sf() as s:
            pos = broker.open_hedge(s, prices.quote(), 50.0)
            self.assertEqual(pos.status, PositionStatus.OPEN)
            self.assertEqual(up.calls[:2], ["sell USDT/KRW", "buy XRP/KRW limit"])
            self.assertEqual(by.calls.count("open_short"), 1)
            self.assertEqual(up.last_limit["params"]["time_in_force"], "fok")
            self.assertEqual(up.last_limit["price"], 2805.0)                 # 판단한 매도호가 그대로
            self.assertAlmostEqual(by.short, pos.upbit_qty)                   # 1:1 헷지
            self.assertAlmostEqual(up.bal["XRP"], pos.upbit_qty)
            self.assertLess(abs(up.bal["KRW"]), 5_000)                        # 자투리 원화만

    def test_entry_miss_rebuys_usdt_and_opens_no_short(self):
        """USDT 를 판 사이 코인 호가가 1% 뛰면 지정가가 안 맞아 취소 → 숏 없이 USDT 되삼 → ABORTED/ENTRY_MISSED"""
        prices, up, by, broker, sf = make_fok_live(JumpyUpbit)
        with sf() as s:
            with self.assertRaises(EntryMissed):
                broker.open_hedge(s, prices.quote(), 50.0)
            pos = s.query(Position).one()
            self.assertEqual((pos.status, pos.exit_reason), (PositionStatus.ABORTED, "ENTRY_MISSED"))
            self.assertEqual(by.calls.count("open_short"), 0)                 # 숏은 시도조차 안 함
            self.assertEqual(by.short, 0.0)
            self.assertAlmostEqual(up.bal["XRP"], 0.0)
            self.assertAlmostEqual(up.bal["USDT"], 500.0, delta=0.3)          # USDT 왕복 수수료·호가만큼만
            self.assertLess(pos.net_pnl_usdt, 0)
            self.assertGreater(pos.net_pnl_usdt, -0.3)
            sides = [f.side for f in s.query(Fill).order_by(Fill.id)]
            self.assertEqual(sides, ["sell_usdt", "buy", "trim_buy_usdt"])
            self.assertTrue(broker.entries_allowed(s)[0])                      # 놓침은 실패가 아니다


class LiveFokExitTest(unittest.TestCase):
    def _open(self):
        prices, up, by, broker, sf = make_fok_live()
        s = sf()
        pos = broker.open_hedge(s, prices.quote(), 50.0)
        return prices, up, by, broker, s, pos

    def test_take_profit_sells_with_fok_then_closes_short(self):
        prices, up, by, broker, s, pos = self._open()
        prices.coin_bid, prices.coin_ask = 2850.0, 2855.0
        val = broker.value_exit(pos, prices.quote())
        pnl = broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
        self.assertEqual(pos.status, PositionStatus.CLOSED)
        self.assertEqual(up.last_limit["side"], "sell")
        self.assertEqual(up.last_limit["price"], 2850.0)                      # 판단한 매수호가
        self.assertEqual(by.short, 0.0)
        self.assertGreater(pnl.net_usdt, 0)

    def test_take_profit_miss_keeps_position_open(self):
        """판단 뒤 매수호가가 빠지면 FOK 취소 → ExitDeferred, 포지션 OPEN 그대로, 숏·코인 그대로 → 돌아오면 청산"""
        prices, up, by, broker, s, pos = self._open()
        prices.coin_bid, prices.coin_ask = 2850.0, 2855.0
        val = broker.value_exit(pos, prices.quote())             # 2,850원에 팔 계획
        prices.coin_bid, prices.coin_ask = 2840.0, 2845.0        # 주문 나가기 전에 호가가 빠짐
        short_before, coin_before = by.short, up.bal["XRP"]
        with self.assertRaises(ExitDeferred):
            broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
        self.assertEqual(pos.status, PositionStatus.OPEN)
        self.assertIsNone(pos.exit_reason)
        self.assertEqual((by.short, up.bal["XRP"]), (short_before, coin_before))
        self.assertEqual(by.calls.count("close_short"), 0)
        miss = [f for f in s.query(Fill).order_by(Fill.id) if f.side == "sell"]
        self.assertEqual([f.qty for f in miss], [0.0])                       # 놓친 기록은 남는다
        prices.coin_bid, prices.coin_ask = 2850.0, 2855.0        # 호가가 돌아옴 → 이번엔 체결
        pnl = broker.close_hedge(s, pos, broker.value_exit(pos, prices.quote()), ExitReason.TAKE_PROFIT)
        self.assertEqual(pos.status, PositionStatus.CLOSED)
        self.assertEqual(by.short, 0.0)
        self.assertGreater(pnl.net_usdt, 0)

    def test_stop_loss_uses_market_even_in_fok_mode(self):
        """손절은 빠져나오는 게 우선 → 시장가 (지정가 주문이 안 나간다)"""
        prices, up, by, broker, s, pos = self._open()
        prices.coin_bid, prices.coin_ask = 2700.0, 2705.0
        up.last_limit = None
        broker.close_hedge(s, pos, broker.value_exit(pos, prices.quote()), ExitReason.STOP_LOSS)
        self.assertEqual(pos.status, PositionStatus.CLOSED)
        self.assertIsNone(up.last_limit)
        self.assertIn("sell XRP/KRW", up.calls)


class EngineRetryTest(unittest.TestCase):
    """엔진: 놓침 뒤 FOK_RETRY_SEC 동안은 같은 포지션·코인을 다시 시도하지 않는다 (초당 4번 판단해도 주문 도배 방지)"""

    def test_tp_miss_then_retry_after_gap(self):
        from test_engine import FakeMarket, make_engine, make_quote, make_settings
        market = FakeMarket()
        engine, sf, broker = make_engine(make_settings(), market)
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        calls = []
        real = broker.close_hedge

        def flaky(session, pos, valuation, reason, liquidated=False):
            calls.append(reason)
            if len(calls) == 1:
                pos.status, pos.exit_reason = PositionStatus.OPEN, None
                session.commit()
                raise ExitDeferred("테스트: 지정가 미체결")
            return real(session, pos, valuation, reason, liquidated=liquidated)

        broker.close_hedge = flaky
        t0 = engine._clock()
        market.quotes = {"XRP": make_quote(0.8)}
        with self.assertLogs("kimp.engine", level=logging.INFO) as logs:
            engine.step()                                     # 익절 조건 → 놓침
        self.assertTrue(any("[익절 보류]" in m and "미체결" in m for m in logs.output))
        self.assertEqual(calls, [ExitReason.TAKE_PROFIT])
        with sf() as s:
            self.assertEqual(s.query(Position).one().status, PositionStatus.OPEN)
        engine._clock = lambda: t0 + FOK_RETRY_SEC / 2
        engine.step()                                         # 아직 대기 → 주문 안 냄
        self.assertEqual(len(calls), 1)
        engine._clock = lambda: t0 + FOK_RETRY_SEC + 0.1
        engine.step()                                         # 다시 시도 → 이번엔 청산
        self.assertEqual(len(calls), 2)
        with sf() as s:
            self.assertEqual(s.query(Position).one().status, PositionStatus.CLOSED)

    def test_entry_miss_waits_then_retries(self):
        from test_engine import FakeMarket, make_engine, make_quote, make_settings
        market = FakeMarket()
        engine, sf, broker = make_engine(make_settings(), market)
        real = broker.open_hedge
        calls = []

        def flaky(session, quote, qty):
            calls.append(qty)
            if len(calls) == 1:
                raise EntryMissed("테스트: 지정가 미체결")
            return real(session, quote, qty)

        broker.open_hedge = flaky
        t0 = engine._clock()
        market.quotes = {"XRP": make_quote(-1.0)}
        with self.assertLogs("kimp.engine", level=logging.INFO) as logs:
            engine.step()
        self.assertTrue(any("[진입 놓침]" in m for m in logs.output))
        engine._clock = lambda: t0 + FOK_RETRY_SEC / 2
        engine.step()
        self.assertEqual(len(calls), 1)                       # 대기 중
        engine._clock = lambda: t0 + FOK_RETRY_SEC + 0.1
        engine.step()
        self.assertEqual(len(calls), 2)
        with sf() as s:
            self.assertEqual(s.query(Position).filter_by(status=PositionStatus.OPEN).count(), 1)


if __name__ == "__main__":
    unittest.main()

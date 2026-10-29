"""
업비트 지정가 FOK 진입 크기·지정가·안전 확인 (kimp/fok_sizing.py, docs/issues.md #3 A·B)

A. 지정가 = 우리 수량이 닿는 마지막 호가 줄 (평균가 내림이면 두 줄에 걸칠 때 늘 놓쳤다)
B. 진입 크기 ≤ 지정가 안쪽 물량 × ENTRY_MAX_DEPTH_SHARE_PERCENT
안전 확인: 전부 마지막 줄 가격에 체결돼도 진입 기준·익절 목표를 넘을 때만 주문

실행: python -m unittest discover -s tests -v
"""
import logging
import unittest

from kimp.broker import PaperBroker
from kimp.db import Decision, ExitReason, Position, PositionStatus, make_session_factory
from kimp.fok_sizing import fit_entry, max_acceptable_price
from kimp.premium import OrderBook, Quote, estimate_fill, estimate_fill_by_cost
from support_ticks import continuous_upbit_ticks
from test_engine import FEES, FakeMarket, make_engine, make_quote, make_settings
from test_live_broker import DEEP

ROUND = lambda q: float(int(q))   # noqa: E731  (바이비트 1개 단위)


def mina_quote():
    """설명 예시: 매도 88.0원 3,000개 / 88.1원 5,000개 / 88.2원 10,000개. 바이비트 × USDT = 88.15원
    → 김프 기준 0% 에서 88.1원까지는 전부 그 가격에 사도 기준 안(−0.057%), 88.2원은 밖(+0.057%)"""
    return Quote(coin="MINA",
                 upbit=OrderBook(bids=[(87.9, DEEP)], asks=[(88.0, 3000.0), (88.1, 5000.0), (88.2, 10000.0)]),
                 bybit=OrderBook(bids=[(0.06578358, DEEP)], asks=[(0.0658, DEEP)]),
                 usdt=OrderBook(bids=[(1340.0, DEEP)], asks=[(1341.0, DEEP)]))


class WorstPriceTest(unittest.TestCase):
    def test_fill_records_last_level(self):
        asks = [(88.0, 3000.0), (88.1, 5000.0)]
        self.assertEqual(estimate_fill(asks, 2000).worst_price, 88.0)       # 한 줄 안
        self.assertEqual(estimate_fill(asks, 4000).worst_price, 88.1)       # 두 줄
        self.assertEqual(estimate_fill_by_cost(asks, 88.0 * 3000).worst_price, 88.0)
        self.assertEqual(estimate_fill_by_cost(asks, 88.0 * 3000 + 1).worst_price, 88.1)

    def test_flat_quote(self):
        q = mina_quote().with_flat_upbit("asks", 88.1)
        self.assertEqual(q.upbit.best_ask, 88.1)
        self.assertEqual(estimate_fill(q.upbit.asks, 1e6).avg_price, 88.1)
        self.assertEqual(q.upbit.bids, mina_quote().upbit.bids)              # 반대쪽은 그대로


class FitEntryTest(unittest.TestCase):
    def test_max_acceptable_price(self):
        self.assertEqual(max_acceptable_price([(1, 1), (2, 1), (3, 1)], lambda p: p <= 2), 2)
        self.assertIsNone(max_acceptable_price([(5, 1)], lambda p: p <= 2))

    def test_example_shrinks_to_half_of_inside_depth(self):
        """원래 약 5,680개 → 안쪽 물량 8,000개의 50% = 4,000개 이하로 줄이고, 지정가는 88.1원"""
        fit = fit_entry(mina_quote(), 5680, 0.0, 0.0, 50.0, ROUND)
        self.assertEqual(fit.status, "ok")
        self.assertEqual(fit.max_price, 88.1)
        self.assertEqual(fit.within_qty, 8000.0)
        self.assertTrue(fit.shrunk)
        self.assertLessEqual(fit.coin_qty, 4000.0)
        self.assertGreater(fit.coin_qty, 3900.0)
        self.assertEqual(fit.limit_price, 88.1)                               # 88.0 3,000 + 88.1 나머지
        self.assertLessEqual(fit.worst_premium, 0.0)                          # 전부 88.1 이어도 기준 안
        self.assertLess(fit.premium, fit.worst_premium)                       # 평균은 더 좋다

    def test_full_share_only_keeps_inside(self):
        """비율 100% 면 줄이지 않는다 (5,680개 < 안쪽 8,000개). 지정가는 마지막 줄 88.1원"""
        fit = fit_entry(mina_quote(), 5680, 0.0, 0.0, 100.0, ROUND)
        self.assertEqual(fit.status, "ok")
        self.assertFalse(fit.shrunk)
        self.assertEqual(fit.limit_price, 88.1)

    def test_old_floor_limit_would_miss(self):
        """예전 방식(평균가 내림 = 88.0원)이면 88.0원 3,000개로는 5,680개를 못 채운다 — 그래서 늘 놓쳤다"""
        plan = mina_quote().entry_plan(5680, 0.0)
        self.assertLess(plan.upbit.avg_price, 88.1)
        self.assertGreater(plan.upbit.filled_qty, 3000.0)
        self.assertEqual(plan.upbit.worst_price, 88.1)

    def test_tighter_threshold_limits_to_first_level(self):
        """기준이 더 엄격해 88.1원은 기준 밖 → 안쪽 물량은 88.0원 3,000개 → 50% = 1,500개, 지정가 88.0원"""
        fit = fit_entry(mina_quote(), 5680, 0.0, -0.1, 50.0, ROUND)
        self.assertEqual(fit.status, "ok")
        self.assertEqual(fit.max_price, 88.0)
        self.assertLessEqual(fit.coin_qty, 1500.0)
        self.assertEqual(fit.limit_price, 88.0)

    def test_round_qty_error_is_a_reject_not_a_crash(self):
        """줄인 개수가 거래소 주문 단위보다 작아 반올림이 예외를 던져도 스캔을 멈추지 않고 depth_reject (GMT 2026-10-30 06:50)"""
        thin = Quote(coin="MINA",
                     upbit=OrderBook(bids=[(87.9, DEEP)], asks=[(88.0, 0.001), (88.2, 10000.0)]),
                     bybit=mina_quote().bybit, usdt=mina_quote().usdt)

        def strict_round(q):
            if q < 1:
                raise ValueError("amount must be greater than minimum amount precision of 1")
            return float(int(q))

        fit = fit_entry(thin, 5680, 0.0, 0.0, 50.0, strict_round)
        self.assertEqual(fit.status, "depth_reject")

    def test_average_outside_threshold(self):
        fit = fit_entry(mina_quote(), 5680, 0.0, -0.5, 50.0, ROUND)
        self.assertEqual(fit.status, "depth_fail")

    def test_book_too_thin(self):
        fit = fit_entry(mina_quote(), 5_000_000, 0.0, 5.0, 50.0, ROUND)
        self.assertEqual(fit.status, "depth_reject")


class PaperTwoLevelTest(unittest.TestCase):
    """페이퍼 브로커: 두 줄에 걸치는 주문도 지정가 FOK 로 체결된다 (예전엔 평균가 내림이라 놓침)"""

    def setUp(self):
        self.sf = make_session_factory(":memory:")
        self.broker = PaperBroker(FEES, 0.5)
        with self.sf() as s:
            self.broker.ex.ensure_initial(s, 10_000, 0.5)
            s.commit()

    def test_entry_and_take_profit_across_two_levels(self):
        q = Quote(coin="XRP",
                  upbit=OrderBook(bids=[(1380.0, DEEP)], asks=[(1385.0, 100.0), (1390.0, DEEP)]),
                  bybit=OrderBook(bids=[(1.0, DEEP)], asks=[(1.0001, DEEP)]),
                  usdt=OrderBook(bids=[(1400.0, DEEP)], asks=[(1400.0, DEEP)]))
        with self.sf() as s:
            pos = self.broker.open_hedge(s, q, 200.0)            # 약 202개 → 1385원 100개 + 1390원 나머지
            self.assertEqual(pos.status, PositionStatus.OPEN)
            self.assertGreater(pos.entry_upbit_price_krw, 1385.0)
            self.assertLess(pos.entry_upbit_price_krw, 1390.0)   # 평균은 두 줄 사이 = 판단 그대로
            q2 = Quote(coin="XRP",
                       upbit=OrderBook(bids=[(1425.0, 100.0), (1420.0, DEEP)], asks=[(1430.0, DEEP)]),
                       bybit=q.bybit, usdt=q.usdt)
            pnl = self.broker.close_hedge(s, pos, self.broker.value_exit(pos, q2), ExitReason.TAKE_PROFIT)
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertGreater(pos.exit_upbit_price_krw, 1420.0)
            self.assertLess(pos.exit_upbit_price_krw, 1425.0)
            self.assertIsNotNone(pnl)


class EngineFokSizingTest(unittest.TestCase):
    """엔진: 진입 크기 줄임(B), 익절 안전 확인(A)"""

    def setUp(self):
        ticks = continuous_upbit_ticks()   # 엔진 가짜 시세는 호가 단위 밖 가격
        ticks.start()
        self.addCleanup(ticks.stop)
        self.market = FakeMarket()

    def _thin_asks(self, premium):
        """맨 앞 매도 호가에 200개만, 다음 줄은 김프 +1% 위(기준 밖)"""
        q = make_quote(premium)
        a1 = q.upbit.asks[0][0]
        return Quote(coin="XRP", upbit=OrderBook(bids=q.upbit.bids, asks=[(a1, 200.0), (a1 * 1.01, 1e9)]),
                     bybit=q.bybit, usdt=q.usdt)

    def test_entry_size_is_cut_to_share_of_inside_depth(self):
        engine, sf, _ = make_engine(make_settings(), self.market)
        self.market.quotes = {"XRP": self._thin_asks(-1.0)}
        with self.assertLogs("kimp.engine", level=logging.INFO) as logs:
            engine.step()
        with sf() as s:
            pos = s.query(Position).one()
        self.assertEqual(pos.status, PositionStatus.OPEN)
        self.assertLessEqual(pos.upbit_qty, 100.0 + 1e-6)                  # 안쪽 200개의 50%
        self.assertLess(pos.bybit_qty, 500.0)                              # 원래 500 USDT ≈ 500개
        self.assertTrue(any("[진입 크기 줄임]" in m for m in logs.output), logs.output)

    def test_take_profit_waits_when_worst_level_misses_target(self):
        """평균 순손익은 목표를 넘지만 전부 마지막 매수 호가 줄에 팔면 못 넘음 → 안 팔고 기다림, 호가가 두꺼워지면 판다"""
        engine, sf, broker = make_engine(make_settings(), self.market)
        self.market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()

        def thin_bids(premium):
            q = make_quote(premium)
            b1 = q.upbit.bids[0][0]
            return Quote(coin="XRP", upbit=OrderBook(bids=[(b1, 250.0), (b1 * 0.99, 1e9)], asks=q.upbit.asks),
                         bybit=q.bybit, usdt=q.usdt)

        with sf() as s:
            p = s.query(Position).one()
            cands = []
            for x in range(0, 400, 2):
                pr = x / 100
                v = broker.value_exit(p, thin_bids(pr))
                worst = broker.value_exit(p, thin_bids(pr).with_flat_upbit("bids", v.plan.upbit.worst_price))
                cands.append((pr, v.pnl.net_percent, worst.pnl.net_percent))
        between = next(pr for pr, avg, worst in cands if avg >= 1.0 and worst < 1.0)
        self.market.quotes = {"XRP": thin_bids(between)}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).one().status, PositionStatus.OPEN)
            stages = [d.stage for d in s.query(Decision)]
        self.assertIn("tp_worst_fail", stages)
        self.market.quotes = {"XRP": make_quote(between)}                  # 같은 김프, 두꺼운 호가 → 판다
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).one().exit_reason, ExitReason.TAKE_PROFIT)


if __name__ == "__main__":
    unittest.main()

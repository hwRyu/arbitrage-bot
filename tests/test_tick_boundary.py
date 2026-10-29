"""
업비트 호가 단위 경계 근처 진입 금지 (ENTRY_TICK_BOUNDARY_MARGIN_PERCENT, docs/issues.md #4)

왜: 스프레드 가드는 지금 호가 간격만 본다. 99.9 / 100원처럼 아래쪽이 아직 0.1원 칸이면 통과하지만, 그 줄이 팔리면
다음 줄은 101원(+1%)이라 주문이 닿기 전에 놓친다 (CAP 2026-10-30 06:04~06:05, 47초에 4번).

실행: python -m unittest discover -s tests -v
"""
import unittest

from kimp.db import Decision, Position, PositionStatus
from kimp.premium import near_tick_boundary
from support_ticks import continuous_upbit_ticks
from test_engine import FakeMarket, make_engine, make_quote, make_settings


class NearTickBoundaryTest(unittest.TestCase):
    def test_hundred_won_edge(self):
        self.assertEqual(near_tick_boundary(99.9, 1.0), (100, 0.1, 1))
        self.assertEqual(near_tick_boundary(100, 1.0), (100, 0.1, 1))      # 딱 경계 (아래 99.9 는 0.1원 칸)
        self.assertEqual(near_tick_boundary(99.0, 1.0), (100, 0.1, 1))
        self.assertIsNone(near_tick_boundary(98.45, 1.0))                  # CAP 평소 가격 — 1.55% 아래
        self.assertIsNone(near_tick_boundary(101, 1.0))                    # 위쪽은 스프레드 가드 몫

    def test_other_edges_and_off(self):
        self.assertEqual(near_tick_boundary(995, 1.0), (1_000, 1, 5))
        self.assertEqual(near_tick_boundary(9.95, 1.0), (10, 0.01, 0.1))
        self.assertIsNone(near_tick_boundary(84.1, 1.0))                   # MINA
        self.assertIsNone(near_tick_boundary(4.5, 1.0))                    # BEAM
        self.assertIsNone(near_tick_boundary(99.9, 0.0))                   # 0 = 안 봄


class EngineTickEdgeTest(unittest.TestCase):
    def setUp(self):
        ticks = continuous_upbit_ticks()
        ticks.start()
        self.addCleanup(ticks.stop)
        self.market = FakeMarket()

    def _quote_near_100(self):
        """김프 −1% 인데 업비트 매도 호가가 약 99.5원 (바이비트 × USDT = 100.5원)"""
        return make_quote(-1.0, bybit_price=100.5 / 1400)

    def test_blocks_entry_near_edge(self):
        engine, sf, _ = make_engine(make_settings(), self.market)
        q = self._quote_near_100()
        self.assertTrue(99.0 <= q.upbit.best_ask <= 100)
        self.market.quotes = {"XRP": q}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).count(), 0)
            self.assertIn("tick_edge_block", [d.stage for d in s.query(Decision)])

    def test_off_when_margin_zero(self):
        engine, sf, _ = make_engine(make_settings(entry_tick_boundary_margin_percent=0.0), self.market)
        self.market.quotes = {"XRP": self._quote_near_100()}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).one().status, PositionStatus.OPEN)


if __name__ == "__main__":
    unittest.main()

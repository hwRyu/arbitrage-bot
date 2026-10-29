"""
직전 10초 바이비트 가격 범위 기록 (kimp/volatility.py, docs/issues.md #5) — 기록만, 매매 동작은 그대로

실행: python -m unittest discover -s tests -v
"""
import logging
import unittest

from kimp.db import Decision
from kimp.volatility import RangeTracker
from support_ticks import continuous_upbit_ticks
from test_engine import FakeMarket, make_engine, make_quote, make_settings


class RangeTrackerTest(unittest.TestCase):
    def test_range_inside_window(self):
        r = RangeTracker(10)
        r.record("CAP", 0.0, 0.1040)
        r.record("CAP", 5.0, 0.1049)
        r.record("CAP", 9.0, 0.1043)
        rng, n = r.range_percent("CAP", 9.0)
        self.assertAlmostEqual(rng, (0.1049 / 0.1040 - 1) * 100)          # 약 0.87% (CAP #16 같은 급등)
        self.assertEqual(n, 3)
        self.assertIn("10초 범위 0.865%", r.describe("CAP", 9.0))

    def test_old_points_drop_and_missing(self):
        r = RangeTracker(10)
        r.record("X", 0.0, 1.0)
        r.record("X", 20.0, 1.001)
        self.assertIsNone(r.range_percent("X", 20.0))                       # 창 안 값 1개
        r.record("X", 21.0, 1.002)
        self.assertAlmostEqual(r.range_percent("X", 21.0)[0], (1.002 / 1.001 - 1) * 100)
        self.assertIsNone(r.range_percent("NONE", 0.0))
        r.record("X", 22.0, None)                                           # 없는 가격은 무시
        self.assertIn("?", r.describe("NONE", 0.0))


class EngineRecordsRangeTest(unittest.TestCase):
    def setUp(self):
        ticks = continuous_upbit_ticks()
        ticks.start()
        self.addCleanup(ticks.stop)

    def test_entry_and_take_profit_carry_range(self):
        market = FakeMarket()
        engine, sf, _ = make_engine(make_settings(), market)
        t = [1000.0]
        engine._clock = lambda: t[0]
        market.quotes = {"XRP": make_quote(-1.0)}
        with self.assertLogs("kimp.engine", level=logging.INFO) as logs:
            engine.step()
        self.assertTrue(any("[진입] #" in m and "바이비트 10초 범위" in m for m in logs.output), logs.output)
        t[0] += 1
        market.quotes = {"XRP": make_quote(2.0)}
        with self.assertLogs("kimp.engine", level=logging.INFO) as logs:
            engine.step()
        self.assertTrue(any("[익절 판단]" in m and "바이비트 10초 범위" in m for m in logs.output), logs.output)
        with sf() as s:
            notes = {d.stage: d.note for d in s.query(Decision) if d.stage in ("entered", "tp_judge")}
        self.assertIn("바이비트 10초 범위", notes["entered"])
        self.assertIn("바이비트 10초 범위", notes["tp_judge"])


if __name__ == "__main__":
    unittest.main()

"""
지정가 FOK 놓침 기록 (kimp/fok_trace.py, docs/issues.md #3 C) — 주문 때 호가 기록과 놓친 이유 가리기

실행: python -m unittest discover -s tests -v
"""
import logging
import unittest

from kimp.broker import EntryMissed, ExitDeferred
from kimp.db import ExitReason, PositionStatus
from kimp.fok_trace import classify_miss, snapshot
from test_fok_orders import make_fok_live


class SnapshotTest(unittest.TestCase):
    def test_one_level_is_enough(self):
        """맨 앞 칸에 우리 수량이 다 들어감 → 1칸, 지정가 안쪽 물량 충분"""
        b = snapshot([(99.8, 3000.0), (99.9, 10000.0)], "buy", 99.8, 268.0)
        self.assertEqual(b.levels_used, 1)
        self.assertEqual(b.within_qty, 3000.0)
        self.assertFalse(b.spans_beyond_limit)
        self.assertIn("1칸", b.describe())

    def test_two_levels_with_floor_limit(self):
        """이슈 #3 예시: 99.8원 3,006개(약 30만 원) + 99.9원, 5,010개 사려면 두 칸 → 평균가 내림 지정가 99.8 로는 모자람"""
        b = snapshot([(99.8, 3006.0), (99.9, 10010.0)], "buy", 99.8, 5010.0)
        self.assertEqual(b.levels_used, 2)
        self.assertTrue(b.spans_beyond_limit)

    def test_sell_side_uses_bids(self):
        """매도: 지정가 이상인 매수호가만 안쪽"""
        b = snapshot([(102.0, 100.0), (101.0, 500.0)], "sell", 102.0, 190.0)
        self.assertEqual(b.within_qty, 100.0)
        self.assertTrue(b.spans_beyond_limit)


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.judged = snapshot([(99.8, 3000.0), (99.9, 10000.0)], "buy", 99.8, 268.0)

    def test_spans(self):
        judged = snapshot([(99.8, 100.0), (99.9, 10000.0)], "buy", 99.8, 268.0)
        self.assertEqual(classify_miss(judged, [(99.8, 100.0)])[0], "두 칸 걸침")

    def test_price_moved(self):
        """놓친 직후 매도 1호가가 99.9 → 지정가 99.8 밖으로"""
        self.assertEqual(classify_miss(self.judged, [(99.9, 10000.0)])[0], "호가 이동")

    def test_quantity_taken(self):
        """맨 앞은 아직 99.8 인데 물량이 100개로 줄었다 (누가 먼저 사 감)"""
        why, detail = classify_miss(self.judged, [(99.8, 100.0), (99.9, 10000.0)])
        self.assertEqual(why, "물량 부족")
        self.assertIn("3,000 → 100", detail)

    def test_refilled(self):
        self.assertEqual(classify_miss(self.judged, [(99.8, 5000.0)])[0], "다시 충분")

    def test_unknown(self):
        self.assertEqual(classify_miss(self.judged, None)[0], "알 수 없음")
        self.assertEqual(classify_miss(None, [(99.8, 1.0)])[0], "알 수 없음")


class BrokerAttachesBookTest(unittest.TestCase):
    """브로커: 지정가 주문마다 [지정가 주문] 로그, 놓치면 예외에 주문 때 호가가 붙는다"""

    def test_entry_miss_carries_book(self):
        prices, up, by, broker, sf = make_fok_live()
        judged = prices.quote()
        prices.coin_bid, prices.coin_ask = 2830.0, 2835.0        # 판단 뒤 주문이 닿기 전에 코인 호가가 1% 뜀
        with sf() as s:
            with self.assertLogs("kimp.broker", level=logging.INFO) as logs:
                with self.assertRaises(EntryMissed) as ctx:
                    broker.open_hedge(s, judged, 50.0)
        fok = ctx.exception.fok
        self.assertIsNotNone(fok)
        self.assertEqual((fok.side, fok.limit, fok.levels_used), ("buy", 2805.0, 1))
        self.assertFalse(fok.spans_beyond_limit)
        self.assertTrue(any("[지정가 주문]" in m and "미체결" in m for m in logs.output), logs.output)
        # 놓친 직후 호가(뛴 가격)와 비교하면 '호가 이동'
        self.assertEqual(classify_miss(fok, prices.quote().upbit.asks)[0], "호가 이동")

    def test_filled_entry_logs_book(self):
        prices, up, by, broker, sf = make_fok_live()
        with sf() as s:
            with self.assertLogs("kimp.broker", level=logging.INFO) as logs:
                broker.open_hedge(s, prices.quote(), 50.0)
        self.assertTrue(any("[지정가 주문]" in m and "매수 지정가 2805" in m and "| 체결" in m for m in logs.output))

    def test_tp_miss_carries_book(self):
        prices, up, by, broker, sf = make_fok_live()
        s = sf()
        pos = broker.open_hedge(s, prices.quote(), 50.0)
        prices.coin_bid, prices.coin_ask = 2850.0, 2855.0
        val = broker.value_exit(pos, prices.quote())
        prices.coin_bid, prices.coin_ask = 2840.0, 2845.0      # 주문 전에 매수호가가 빠짐
        with self.assertRaises(ExitDeferred) as ctx:
            broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)
        fok = ctx.exception.fok
        self.assertEqual((fok.side, fok.limit), ("sell", 2850.0))
        self.assertEqual(pos.status, PositionStatus.OPEN)
        self.assertEqual(classify_miss(fok, prices.quote().upbit.bids)[0], "호가 이동")
        s.close()


if __name__ == "__main__":
    unittest.main()

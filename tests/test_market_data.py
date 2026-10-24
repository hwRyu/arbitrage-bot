"""
시세 조회 실패 알림 억제 테스트 (네트워크 불필요 — 도우미 함수만 본다)

거래소 점검처럼 조회가 한참 안 되면 매 스캔(10초)마다 ERROR 가 찍히고 그게 전부 텔레그램으로 간다.
처음 한 번 알리고, 쿨다운 동안은 조용히, 복구되면 한 번 더 알리는지 확인한다.

실행: python -m unittest discover -s tests -v
"""
import logging
import queue
import threading
import time
import unittest
from unittest import mock

from kimp import market_data as md
from kimp.market_data import MarketData


class SlowUpbit:
    """호가 묶음 조회에 0.2초 걸리는 가짜 업비트"""

    def __init__(self, delay=0.2):
        self.delay = delay

    def fetch_order_books(self, symbols, depth):
        time.sleep(self.delay)
        return {s: {"bids": [[1400.0, 1e9]] if s == "USDT/KRW" else [[1386.0, 1e9]],
                    "asks": [[1401.0, 1e9]] if s == "USDT/KRW" else [[1387.0, 1e9]], "timestamp": 1}
                for s in symbols}


class SlowBybit:
    """코인 호가 하나에 0.2초 걸리는 가짜 바이비트. 같은 객체를 두 스레드가 동시에 쓰면 터뜨린다"""

    def __init__(self, delay=0.2):
        self.delay = delay
        self._busy = threading.Lock()
        self.calls = 0

    def fetch_order_book(self, symbol, depth):
        if not self._busy.acquire(blocking=False):
            raise AssertionError("같은 ccxt 객체를 두 스레드가 동시에 썼다")
        try:
            self.calls += 1
            time.sleep(self.delay)
            return {"bids": [[1.0, 1e9]], "asks": [[1.0001, 1e9]], "timestamp": 1}
        finally:
            self._busy.release()


def fake_market(coins_parallel=3):
    """네트워크 없이 MarketData 를 만든다 (load_markets 를 건너뛰고 가짜 거래소를 꽂는다)"""
    m = MarketData.__new__(MarketData)
    m.depth = 5
    m.feed = None          # __init__ 을 건너뛰므로 직접 (소켓 호가 없음 = REST 만)
    m.upbit = SlowUpbit()
    m.bybit = SlowBybit()
    m._bybit_pool = queue.Queue()
    for _ in range(coins_parallel):
        m._bybit_pool.put(SlowBybit())
    return m


class ParallelQuotesTest(unittest.TestCase):
    def test_upbit_and_bybit_are_fetched_concurrently(self):
        """업비트 1번(0.2초) + 바이비트 3번(0.2초씩) 을 순차로 하면 0.8초, 동시에 하면 0.2초 남짓"""
        m = fake_market()
        t0 = time.monotonic()
        quotes = m.fetch_quotes(["AAA", "BBB", "CCC"])
        took = time.monotonic() - t0
        self.assertEqual(set(quotes), {"AAA", "BBB", "CCC"})
        self.assertLess(took, 0.5, f"{took:.2f}초 — 동시에 받지 않았다")
        for q in quotes.values():
            self.assertIsNotNone(q.skew_ms)
            self.assertLess(q.skew_ms, 300)
            self.assertAlmostEqual(q.mid_premium, -1.0, places=2)

    def test_one_bybit_failure_drops_only_that_coin(self):
        m = fake_market()
        bad = SlowBybit()
        bad.fetch_order_book = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("바이비트 안 됨"))
        # 풀의 첫 객체를 고장 낸다 → 어느 코인 하나가 그걸 집어 간다
        first = m._bybit_pool.get()
        m._bybit_pool.put(bad)
        quotes = m.fetch_quotes(["AAA", "BBB", "CCC"])
        self.assertEqual(len(quotes), 2)

    def test_upbit_failure_returns_nothing(self):
        m = fake_market()
        m.upbit.fetch_order_books = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("업비트 안 됨"))
        self.assertEqual(m.fetch_quotes(["AAA"]), {})


class FakeFeed:
    """코인 하나만 소켓 호가가 있는 가짜 WsFeed (books 만 흉내)"""

    def __init__(self, coin):
        from kimp.premium import OrderBook
        from kimp.ws_feed import FeedBooks
        self.coin = coin
        self.fb = FeedBooks(upbit=OrderBook(bids=[(999.0, 1e9)], asks=[(1000.0, 1e9)]),
                            bybit=OrderBook(bids=[(0.7, 1e9)], asks=[(0.7001, 1e9)]),
                            usdt=OrderBook(bids=[(1400.0, 1e9)], asks=[(1401.0, 1e9)]), age_ms=80, skew_ms=12)

    def books(self, coin):
        return self.fb if coin == self.coin else None


class FeedFirstTest(unittest.TestCase):
    def test_feed_coin_skips_rest_and_others_fall_back(self):
        """소켓 호가가 있는 코인은 그걸 쓰고(REST 안 부름), 없는 코인만 REST 로 받는다"""
        m = fake_market()
        m.feed = FakeFeed("XRP")
        quotes = m.fetch_quotes(["XRP", "SOL"], fx_krw=1320.0)
        self.assertEqual(quotes["XRP"].upbit.bids[0][0], 999.0)       # 소켓 값
        self.assertEqual(quotes["XRP"].skew_ms, 0)                   # 소켓 호가는 어긋남 0 으로 본다
        self.assertEqual(quotes["SOL"].upbit.bids[0][0], 1386.0)      # REST 가짜 값
        calls = sum(b.calls for b in list(m._bybit_pool.queue))
        self.assertEqual(calls, 1)                                    # 바이비트 REST 는 SOL 한 번만

    def test_no_feed_is_plain_rest(self):
        m = fake_market()
        quotes = m.fetch_quotes(["XRP", "SOL"])
        self.assertEqual({c: q.upbit.bids[0][0] for c, q in quotes.items()}, {"XRP": 1386.0, "SOL": 1386.0})


class RepeatLogTest(unittest.TestCase):
    def setUp(self):
        md._fail_since.clear()
        md._last_told.clear()

    def test_first_failure_is_told_then_quiet_until_cooldown(self):
        """같은 실패: 처음 1번 → 쿨다운 안에는 DEBUG → 쿨다운이 지나면 다시 1번"""
        clock = [1000.0]
        with mock.patch.object(md.time, "monotonic", lambda: clock[0]):
            with self.assertLogs("kimp.market_data", level=logging.DEBUG) as logs:
                md._log_repeat("k", logging.ERROR, "업비트 호가 조회 실패")
                clock[0] += 10
                md._log_repeat("k", logging.ERROR, "업비트 호가 조회 실패")
                clock[0] += 10
                md._log_repeat("k", logging.ERROR, "업비트 호가 조회 실패")
            errors = [m for m in logs.output if m.startswith("ERROR")]
            self.assertEqual(len(errors), 1)
            self.assertEqual(len(logs.output), 3)   # 나머지 둘은 DEBUG

            clock[0] += md.REPEAT_COOLDOWN_SEC
            with self.assertLogs("kimp.market_data", level=logging.ERROR):
                md._log_repeat("k", logging.ERROR, "업비트 호가 조회 실패")

    def test_recovery_is_announced_once_with_duration(self):
        """복구되면 '얼마나 막혔었는지' 한 번 알리고, 그 뒤 성공에는 조용하다"""
        clock = [0.0]
        with mock.patch.object(md.time, "monotonic", lambda: clock[0]):
            md._log_repeat("k", logging.ERROR, "실패")
            clock[0] = 125.0
            with self.assertLogs("kimp.market_data", level=logging.WARNING) as logs:
                md._log_recovered("k", "업비트 호가 조회")
            self.assertIn("125초", logs.output[0])
            with self.assertNoLogs("kimp.market_data", level=logging.DEBUG):
                md._log_recovered("k", "업비트 호가 조회")   # 실패한 적 없으면 조용

    def test_keys_are_independent(self):
        """코인별 키가 따로 논다 (CAP 이 막혀도 ALT 첫 실패는 바로 알린다)"""
        with self.assertLogs("kimp.market_data", level=logging.WARNING) as logs:
            md._log_repeat("bybit-CAP", logging.WARNING, "CAP 실패")
            md._log_repeat("bybit-ALT", logging.WARNING, "ALT 실패")
        self.assertEqual(len([m for m in logs.output if m.startswith("WARNING")]), 2)


if __name__ == "__main__":
    unittest.main()

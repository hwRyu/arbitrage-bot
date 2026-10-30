"""
시세 수집기 테스트 (네트워크 불필요)

수집기는 '주문 없이 김프·호가만 적는' 역할이다. 봇(main.py)과 역할이 섞이지 않는지 확인한다.
"""
import os
import threading
import unittest
from dataclasses import replace

from sqlalchemy import func, select

from kimp.collect import TickDedup, pick_coins, run, run_ws, snapshot_rows, tick_row
from kimp.config import Settings
from kimp.db import PremiumSnapshot, TickPremium, make_session_factory
from kimp.market_data import TopQuote
from kimp.premium import OrderBook

TMP = "data/_test_collect.db"


def make_top(coin="AAA", fx=1_320.0):
    """업비트 99/101원, 바이비트 0.0745/0.0755, USDT 1330/1331원인 가짜 호가"""
    return TopQuote(coin=coin, upbit_bid=99.0, upbit_ask=101.0, bybit_bid=0.0745, bybit_ask=0.0755,
                    usdt_bid=1_330.0, usdt_ask=1_331.0, funding_rate=0.0001,
                    funding_interval_hours=8.0, next_funding_ms=1, fx_krw=fx)


class SnapshotRowTest(unittest.TestCase):
    def test_row_fields(self):
        """기록 한 줄에 김프 3종·호가 간격·펀딩비·테더가 다 들어간다"""
        row = snapshot_rows({"AAA": make_top()})[0]
        self.assertEqual(row.coin, "AAA")
        self.assertGreater(row.entry_premium_top, row.mid_premium)   # 진입은 불리 → 더 높다
        self.assertLess(row.exit_premium_top, row.mid_premium)       # 청산은 불리 → 더 낮다
        self.assertAlmostEqual(row.usdt_spread_percent, (1_331 / 1_330 - 1) * 100, places=6)
        self.assertAlmostEqual(row.usdt_krw, 1_330.5)
        self.assertAlmostEqual(row.funding_rate, 0.0001)
        self.assertAlmostEqual(row.funding_interval_hours, 8.0)
        self.assertIsNotNone(row.tether_premium)

    def test_no_fx_means_no_tether(self):
        """환율이 없으면 테더·사이트 김프만 비고 김프는 그대로 (표시용이라 매매 무관)"""
        row = snapshot_rows({"AAA": make_top(fx=None)})[0]
        self.assertIsNone(row.tether_premium)
        self.assertIsNone(row.site_premium)
        self.assertIsNotNone(row.mid_premium)


class RunTest(unittest.TestCase):
    """수집 루프: 지정한 DB 에 쌓고, 포지션·잔고는 건드리지 않는다"""

    class FakeMd:
        pass

    def setUp(self):
        for p in (TMP,):
            try:
                os.remove(p)
            except OSError:
                pass
        self.settings = replace(Settings(coins=["AAA"]), snapshot_db_path=TMP, fixed_usd_krw=1_320.0)

    def tearDown(self):
        try:
            os.remove(TMP)
        except OSError:  # 윈도우는 연결이 남아 있으면 못 지운다
            pass

    def test_once_writes_rows(self):
        import kimp.collect as c
        orig, c.fetch_tops = c.fetch_tops, lambda md, coins, fx_krw=None: {
            x: make_top(x, fx_krw) for x in coins}
        try:
            written = run(self.settings, ["AAA", "BBB"], 60.0, threading.Event(), once=True, md=self.FakeMd())
        finally:
            c.fetch_tops = orig
        self.assertEqual(written, 2)
        with make_session_factory(TMP)() as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(PremiumSnapshot)), 2)
            # 매매 기록 테이블은 비어 있다 (수집기는 주문을 하지 않는다)
            from kimp.db import Position
            self.assertEqual(s.scalar(select(func.count()).select_from(Position)), 0)

    def test_stop_event_ends_loop(self):
        """종료 신호를 주면 바로 멈춘다 (Ctrl+C)"""
        import kimp.collect as c
        orig, c.fetch_tops = c.fetch_tops, lambda md, coins, fx_krw=None: {x: make_top(x) for x in coins}
        stop = threading.Event()
        stop.set()
        try:
            written = run(self.settings, ["AAA"], 60.0, stop, md=self.FakeMd())
        finally:
            c.fetch_tops = orig
        self.assertEqual(written, 0)


class TickModeTest(unittest.TestCase):
    """--ws 모드: 웹소켓 호가로 바뀐 김프만 tick_premiums 에 적는다 (docs/filter.md 1단계)"""

    class FakeBooks:
        def __init__(self, up_ask):
            self.upbit = OrderBook(bids=[(99.0, 1.0)], asks=[(up_ask, 1.0)])
            self.bybit = OrderBook(bids=[(0.0745, 1.0)], asks=[(0.0755, 1.0)])
            self.usdt = OrderBook(bids=[(1_330.0, 1.0)], asks=[(1_331.0, 1.0)])
            self.age_ms, self.skew_ms = 10, 3

    class FakeFeed:
        def __init__(self, books):
            self.books_by_coin = books

        def books(self, coin):
            return self.books_by_coin.get(coin)

        def fresh_coins(self):
            return list(self.books_by_coin)

    def setUp(self):
        import tempfile
        # 윈도우는 열린 연결이 남으면 파일을 못 지워 다음 테스트에 기록이 겹친다 → 테스트마다 새 임시 폴더
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = os.path.join(self._dir.name, "ticks.db")
        self.settings = replace(Settings(coins=["AAA"]), snapshot_db_path=self.tmp)

    def tearDown(self):
        self._dir.cleanup()

    def test_row_from_books(self):
        row = tick_row("AAA", self.FakeBooks(101.0), 1_000)
        top = make_top()
        self.assertAlmostEqual(row.entry_premium_top, top.entry_premium_top, places=9)
        self.assertAlmostEqual(row.exit_premium_top, top.exit_premium_top, places=9)
        self.assertAlmostEqual(row.mid_premium, top.mid_premium, places=9)
        self.assertEqual((row.ts_ms, row.skew_ms), (1_000, 3))

    def test_dedup_writes_only_changes_and_heartbeat(self):
        d = TickDedup(heartbeat_sec=60)
        a = tick_row("AAA", self.FakeBooks(101.0), 0)
        self.assertTrue(d.should_write(a))
        self.assertFalse(d.should_write(tick_row("AAA", self.FakeBooks(101.0), 500)))        # 같은 값
        self.assertTrue(d.should_write(tick_row("AAA", self.FakeBooks(101.1), 1_000)))       # 바뀜
        self.assertFalse(d.should_write(tick_row("AAA", self.FakeBooks(101.1), 30_000)))
        self.assertTrue(d.should_write(tick_row("AAA", self.FakeBooks(101.1), 61_500)))      # 하트비트
        self.assertTrue(d.should_write(tick_row("BBB", self.FakeBooks(101.1), 61_500)))      # 코인별

    def test_run_ws_writes_changed_rows_only(self):
        feed = self.FakeFeed({"AAA": self.FakeBooks(101.0), "BBB": self.FakeBooks(102.0)})
        t = [0]

        def now_ms():
            t[0] += 500
            return t[0]

        written = run_ws(self.settings, ["AAA", "BBB", "CCC"], 0.0, threading.Event(), feed=feed, now_ms=now_ms,
                         max_rounds=3)
        self.assertEqual(written, 2)                                   # 첫 바퀴만 2줄, 그 뒤는 안 바뀜. CCC 는 호가 없음
        feed.books_by_coin["AAA"] = self.FakeBooks(101.2)
        written = run_ws(self.settings, ["AAA", "BBB"], 0.0, threading.Event(), feed=feed, now_ms=now_ms, max_rounds=1)
        self.assertEqual(written, 2)                                   # 새 루프라 기억이 없어 둘 다 적는다
        with make_session_factory(self.tmp)() as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(TickPremium)), 4)
            from kimp.db import Position
            self.assertEqual(s.scalar(select(func.count()).select_from(Position)), 0)

    def test_run_ws_records_on_update_event_between_sweeps(self):
        """호가가 바뀌면(feed.on_update) 전체 훑기를 기다리지 않고 그 코인을 바로 적는다 — 0.5초보다 짧은 틈도 남게"""
        import kimp.collect as c

        import time

        class EventFeed(self.FakeFeed):
            on_update = None
            reads = 0

            def books(self, coin):
                self.reads += 1
                return super().books(coin)

        feed = EventFeed({"AAA": self.FakeBooks(101.0), "BBB": self.FakeBooks(102.0)})
        t = [0]

        def now_ms():
            t[0] += 100
            return t[0]

        orig_flush = c.TICK_FLUSH_SEC
        c.TICK_FLUSH_SEC = 0.01
        stop = threading.Event()

        def wait_until(cond):
            for _ in range(400):
                if cond():
                    return
                time.sleep(0.005)

        def driver():
            # 시작 훑기(두 코인 읽음)가 끝난 뒤, 다음 전체 훑기(interval 10초) 전에 AAA 를 두 번 바꾼다
            wait_until(lambda: feed.on_update is not None and feed.reads >= 2)
            reads = feed.reads
            feed.books_by_coin["AAA"] = self.FakeBooks(101.3)
            feed.on_update("AAA")
            wait_until(lambda: feed.reads > reads)          # 루프가 그 변화를 읽을 때까지
            reads = feed.reads
            feed.books_by_coin["AAA"] = self.FakeBooks(101.0)
            feed.on_update("AAA")
            wait_until(lambda: feed.reads > reads)
            time.sleep(0.05)
            stop.set()

        th = threading.Thread(target=driver)
        th.start()
        try:
            written = run_ws(self.settings, ["AAA", "BBB"], 10.0, stop, feed=feed, now_ms=now_ms)
        finally:
            c.TICK_FLUSH_SEC = orig_flush
            th.join()
        with make_session_factory(self.tmp)() as s:
            rows = s.query(TickPremium).filter(TickPremium.coin == "AAA").order_by(TickPremium.ts_ms).all()
        self.assertGreaterEqual(written, 3)
        self.assertEqual(len(rows), 3)                                 # 처음 + 튐 + 되돌아옴
        self.assertGreater(rows[1].entry_premium_top, rows[0].entry_premium_top)
        self.assertAlmostEqual(rows[2].entry_premium_top, rows[0].entry_premium_top, places=9)


class PickCoinsTest(unittest.TestCase):
    class FakeMd:
        def filter_supported(self, coins):
            return [c for c in coins if c != "없는코인"]

    def test_explicit_coins_filtered(self):
        got = pick_coins(Settings(coins=["XRP"]), self.FakeMd(), "cap, sei, 없는코인", 0, False)
        self.assertEqual(got, ["CAP", "SEI"])

    def test_default_is_env_coins(self):
        got = pick_coins(Settings(coins=["XRP", "SOL"]), self.FakeMd(), None, 0, False)
        self.assertEqual(got, ["XRP", "SOL"])


class SnapshotPathsTest(unittest.TestCase):
    def test_order_and_dedup(self):
        """수집 DB → 지금 모드 DB → 페이퍼 DB 순서, 중복은 한 번만"""
        s = replace(Settings(coins=["AAA"]), snapshot_db_path="m.db", db_path="k.db")
        self.assertEqual(s.snapshot_db_paths(), ("m.db", "k.db"))   # 페이퍼 모드라 ②=③
        live = replace(s, paper_trading=False, live_dry_run=True, dry_run_db_path="d.db")
        self.assertEqual(live.snapshot_db_paths(), ("m.db", "d.db", "k.db"))


if __name__ == "__main__":
    unittest.main()

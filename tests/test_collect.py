"""
시세 수집기 테스트 (네트워크 불필요)

수집기는 '주문 없이 김프·호가만 적는' 역할이다. 봇(main.py)과 역할이 섞이지 않는지 확인한다.
"""
import os
import threading
import unittest
from dataclasses import replace

from sqlalchemy import func, select

from kimp.collect import pick_coins, run, snapshot_rows
from kimp.config import Settings
from kimp.db import PremiumSnapshot, make_session_factory
from kimp.market_data import TopQuote

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

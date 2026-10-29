"""
분석용 기록 테스트 (kimp/telemetry.py) — 가짜 시세로 엔진을 돌리고 DB 에 무엇이 남는지 본다

실행: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest

from sqlalchemy import select

sys.path.insert(0, os.path.dirname(__file__))
from test_engine import FakeMarket, make_engine, make_quote, make_settings  # noqa: E402

from kimp.db import (Decision, EngineSnapshot, Event, ExitReason, Fill, Position, PositionSnapshot, Run,
                     make_session_factory)
from kimp.telemetry import DbLogHandler, NEAR_MARGIN_PERCENT, end_run, settings_summary, start_run
from support_ticks import continuous_upbit_ticks

_TICKS = continuous_upbit_ticks()   # 가짜 시세가 호가 단위 밖이라 지정가 단위 맞춤을 끈다 (tests/support_ticks.py)


def setUpModule():
    _TICKS.start()


def tearDownModule():
    _TICKS.stop()


class DecisionTest(unittest.TestCase):
    """진입 판단 기록: 기준 근처부터 어느 단계에서 걸렸는지"""

    def setUp(self):
        self.market = FakeMarket()
        self.engine, self.sf, self.broker = make_engine(make_settings(), self.market)   # 기준 -0.5%

    def _stages(self):
        with self.sf() as s:
            return [(d.coin, d.stage) for d in s.scalars(select(Decision).order_by(Decision.id))]

    def test_far_from_threshold_leaves_nothing(self):
        """기준 근처에도 안 왔으면 기록 없음 (표가 쓸데없이 커지지 않게)"""
        self.market.quotes = {"XRP": make_quote(-0.5 + NEAR_MARGIN_PERCENT + 0.5)}
        self.engine.step()
        self.assertEqual(self._stages(), [])

    def test_near_miss_is_recorded(self):
        """기준보다 조금 높아 못 들어간 순간은 near 로 남는다"""
        self.market.quotes = {"XRP": make_quote(-0.4)}   # 기준 -0.5 보다 0.1%p 높음
        self.engine.step()
        self.assertEqual(self._stages(), [("XRP", "near")])
        with self.sf() as s:
            d = s.scalars(select(Decision)).one()
            self.assertAlmostEqual(d.threshold_percent, -0.5)
            self.assertGreater(d.premium_top, -0.5)
            self.assertIsNone(d.premium_depth)

    def test_entry_is_recorded_with_position_and_run(self):
        """진입하면 entered + 포지션 번호, 포지션에는 run_id·판단 김프가 남는다"""
        self.engine.run_id = 7
        self.market.quotes = {"XRP": make_quote(-1.0)}
        self.engine.step()
        self.assertEqual(self._stages(), [("XRP", "entered")])
        with self.sf() as s:
            d = s.scalars(select(Decision)).one()
            pos = s.query(Position).one()
            self.assertEqual(d.position_id, pos.id)
            self.assertIsNotNone(d.premium_depth)
            self.assertEqual(pos.run_id, 7)
            self.assertIsNotNone(pos.planned_entry_premium)
            self.assertAlmostEqual(pos.planned_entry_premium, pos.entry_premium, places=6)  # 페이퍼는 판단 = 실제

    def test_thin_book_is_cut_then_too_small(self):
        """맨 앞 호가 1개만 기준 안, 그 뒤는 기준 밖 → 지정가 안쪽 물량(1개)의 50% 로 줄이면 최소 주문보다 작다
        → size_reject (예전엔 원래 크기의 평균 김프로 depth_fail. 2026-10-29 부터 크기를 먼저 안쪽 물량에 맞춘다,
        docs/issues.md #3 B)"""
        from kimp.premium import OrderBook, Quote
        # 업비트 매도 호가: 맨 앞 1개만 싸고 그 뒤는 비싸다 → 500 USDT 어치 사면 평균가가 밀린다
        usdt, by = 1400.0, 1.0
        cheap = by * usdt * (1 - 0.01)     # 김프 -1% 가격
        pricey = by * usdt * (1 + 0.01)    # 김프 +1% 가격
        q = Quote(coin="XRP",
                  upbit=OrderBook(bids=[(cheap * 0.999, 1e9)], asks=[(cheap, 1.0), (pricey, 1e9)]),
                  bybit=OrderBook(bids=[(by, 1e9)], asks=[(by * 1.0001, 1e9)]),
                  usdt=OrderBook(bids=[(usdt, 1e9)], asks=[(usdt * 1.0001, 1e9)]))
        self.market.quotes = {"XRP": q}
        self.engine.step()
        self.assertEqual(self._stages(), [("XRP", "size_reject")])
        with self.sf() as s:
            d = s.scalars(select(Decision)).one()
            self.assertLess(d.premium_top, -0.5)
            self.assertIn("최소 주문", d.note)
            self.assertEqual(s.query(Position).count(), 0)

    def test_no_slot_when_two_coins_qualify_and_one_slot(self):
        """두 코인이 조건을 만족하는데 자리가 하나면 밀린 코인은 no_slot"""
        engine, sf, _ = make_engine(make_settings(coins=["XRP", "DOGE"], max_open_positions=1), self.market)
        self.market.quotes = {"XRP": make_quote(-1.0), "DOGE": make_quote(-2.0, coin="DOGE")}
        engine.step()
        with sf() as s:
            got = {(d.coin, d.stage) for d in s.scalars(select(Decision))}
        self.assertEqual(got, {("DOGE", "entered"), ("XRP", "no_slot")})   # 더 유리한 DOGE 가 들어감


class PositionTelemetryTest(unittest.TestCase):
    """보유 중 평가 기록과 최고·최저"""

    def test_extremes_and_snapshots(self):
        market = FakeMarket()
        engine, sf, _ = make_engine(make_settings(), market)
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()                                   # 진입
        market.quotes = {"XRP": make_quote(-0.3)}       # 조금 오름
        engine.step()
        market.quotes = {"XRP": make_quote(-1.5)}       # 내려감
        engine.step()
        market.quotes = {"XRP": make_quote(-0.9)}
        engine.step()
        with sf() as s:
            pos = s.query(Position).one()
            snaps = s.scalars(select(PositionSnapshot).where(PositionSnapshot.position_id == pos.id)).all()
        self.assertEqual(pos.status, "OPEN")
        self.assertEqual(len(snaps), 3)                   # 테스트 설정은 간격 0 → 평가마다 1줄
        nets = [sn.net_percent for sn in snaps]
        self.assertAlmostEqual(pos.max_net_percent, max(nets), places=9)
        self.assertAlmostEqual(pos.min_net_percent, min(nets), places=9)
        self.assertIsNotNone(pos.max_net_at)
        self.assertGreater(pos.max_net_percent, pos.min_net_percent)
        self.assertAlmostEqual(snaps[-1].funding_usdt, 0.0)
        self.assertIsNotNone(snaps[-1].mid_premium)

    def test_fills_keep_expected_price_and_done_at(self):
        """체결 기록에 예상가와 체결 확인 시각이 남는다 (페이퍼는 예상 = 실제)"""
        market = FakeMarket()
        engine, sf, broker = make_engine(make_settings(), market)
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        market.quotes = {"XRP": make_quote(0.8)}
        engine.step()                                   # 익절
        with sf() as s:
            pos = s.query(Position).one()
            fills = s.scalars(select(Fill).where(Fill.position_id == pos.id).order_by(Fill.id)).all()
        self.assertEqual(pos.exit_reason, ExitReason.TAKE_PROFIT)
        self.assertEqual({f.side for f in fills}, {"open_short", "sell_usdt", "buy", "sell", "buy_usdt", "close_short"})
        for f in fills:
            self.assertIsNotNone(f.expected_price, f.side)
            self.assertIsNotNone(f.done_at, f.side)
            self.assertAlmostEqual(f.expected_price, f.avg_price, places=6)


class EngineSnapshotTest(unittest.TestCase):
    def test_engine_snapshot_written_with_status(self):
        market = FakeMarket()
        engine, sf, _ = make_engine(make_settings(), market)
        market.quotes = {"XRP": make_quote(-0.2)}
        engine.step()
        engine.step()
        with sf() as s:
            rows = s.scalars(select(EngineSnapshot)).all()
        self.assertEqual(len(rows), 2)                    # 간격 0 → 스캔마다
        self.assertGreater(rows[0].upbit_usdt + rows[0].bybit_usdt, 9_000)
        self.assertGreaterEqual(rows[0].scan_ms, 0.0)
        self.assertEqual(rows[0].open_positions, 0)


class RunTest(unittest.TestCase):
    def test_start_and_end_run_without_secrets(self):
        sf = make_session_factory(":memory:")
        settings = make_settings(upbit_access_key="SECRET-KEY", telegram_bot_token="SECRET-TOKEN")
        run_id = start_run(sf, settings, ["XRP"])
        self.assertGreater(run_id, 0)
        end_run(sf, run_id, "one_shot")
        with sf() as s:
            run = s.get(Run, run_id)
        self.assertEqual(run.coins, "XRP")
        self.assertEqual(run.stop_reason, "one_shot")
        self.assertIsNotNone(run.ended_at)
        text = run.settings_json
        self.assertNotIn("SECRET", text)
        parsed = json.loads(text)
        self.assertEqual(parsed["target_net_profit_percent"], 1.0)
        self.assertIn("upbit_trade_fee_rate", parsed["fees"])

    def test_settings_summary_drops_secret_fields(self):
        summary = settings_summary(make_settings(bybit_api_secret="X"))
        self.assertNotIn("bybit_api_secret", summary)
        self.assertNotIn("upbit_secret_key", summary)
        self.assertIn("coins", summary)


class EventLogTest(unittest.TestCase):
    """WARNING 이상 로그가 DB 에도 남고, 예외는 traceback 까지"""

    def test_warning_and_exception_are_stored(self):
        import logging
        sf = make_session_factory(":memory:")
        handler = DbLogHandler(sf, run_id=3)
        log = logging.getLogger("kimp.test_events")
        log.addHandler(handler)
        try:
            log.info("이건 안 남는다")
            log.warning("[리밸런싱 필요] 업비트 100 / 바이비트 900")
            try:
                1 / 0
            except ZeroDivisionError:
                log.exception("스캔 중 에러 (다음 스캔에서 다시 시도)")
        finally:
            log.removeHandler(handler)
        with sf() as s:
            rows = s.scalars(select(Event).order_by(Event.id)).all()
        self.assertEqual([r.level for r in rows], ["WARNING", "ERROR"])
        self.assertIsNone(rows[0].exc_text)
        self.assertIn("ZeroDivisionError", rows[1].exc_text)
        self.assertIn("1 / 0", rows[1].exc_text)
        self.assertEqual(rows[1].run_id, 3)
        self.assertEqual(rows[0].logger, "kimp.test_events")

    def test_handler_never_raises(self):
        """DB 가 깨져도 로그 핸들러가 봇을 멈추면 안 된다"""
        import logging

        def broken():
            raise RuntimeError("DB 없음")

        handler = DbLogHandler(broken, run_id=None)
        log = logging.getLogger("kimp.test_events2")
        log.addHandler(handler)
        try:
            log.critical("아무 문제")   # 예외가 밖으로 나오면 테스트 실패
        finally:
            log.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()

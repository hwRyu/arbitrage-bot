"""
한 번만 돌리기(ONE_SHOT) 테스트 — 네트워크 불필요

첫 실거래 점검용 기능. 포지션 하나만 잡고, 청산되면 봇이 스스로 종료한다.
실행: python -m unittest discover -s tests -v
"""
import threading
import unittest
from datetime import datetime, timedelta, timezone

from kimp.db import ExitReason, PositionStatus
from kimp.engine import _as_utc
from test_engine import FEES, FakeMarket, make_quote, make_settings
from kimp.engine import Engine
from kimp.db import make_session_factory as make_sf
from kimp.paper_broker import PaperBroker


def make_one_shot_engine(market, **overrides):
    """ONE_SHOT 엔진 + 메모리 DB (시작 자본은 목표 비율대로 분배)"""
    settings = make_settings(one_shot=True, coins=["XRP", "SOL"], reentry_cooldown_sec=0, **overrides)
    sf = make_sf(":memory:")
    broker = PaperBroker(FEES, 0.3)
    with sf() as s:
        broker.ensure_initial_balances(s, settings.paper_initial_total_usdt, settings.upbit_target_share())
    return Engine(settings, market, None, broker, sf, list(settings.coins)), sf, broker


class OneShotTest(unittest.TestCase):
    def test_only_one_position_even_if_many_coins_qualify(self):
        """두 코인이 동시에 조건을 만족해도 하나만 잡는다"""
        market = FakeMarket()
        engine, sf, broker = make_one_shot_engine(market)
        market.quotes = {"XRP": make_quote(-1.0), "SOL": make_quote(-1.5)}
        engine.startup()
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 1)

    def test_no_new_entry_after_close_and_stops(self):
        """청산되면 더 진입하지 않고 '끝' 으로 표시한다 (run 루프가 멈춘다)"""
        market = FakeMarket()
        engine, sf, broker = make_one_shot_engine(market)
        engine.startup()
        market.quotes = {"XRP": make_quote(-1.0), "SOL": make_quote(-1.5)}
        engine.step()                       # 진입
        market.quotes = {"XRP": make_quote(3.0), "SOL": make_quote(-1.5)}
        engine.step()                       # 목표 달성 → 청산
        self.assertTrue(engine._one_shot_done)
        market.quotes = {"XRP": make_quote(-1.0), "SOL": make_quote(-1.5)}
        engine.step()                       # 더 돌려도 새로 잡지 않는다
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 0)

    def test_existing_position_is_adopted(self):
        """이미 열린 포지션이 있으면 그걸 끝까지 보고 종료한다 (새로 잡지 않음)"""
        market = FakeMarket()
        engine, sf, broker = make_one_shot_engine(market)
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.startup()
        engine.step()                       # 포지션 1개
        engine2, _, _ = make_one_shot_engine(market)   # 다른 엔진(= 재시작)으로 바꿔치기
        engine2.session_factory, engine2.broker = sf, broker
        engine2.startup()
        self.assertTrue(engine2._one_shot_opened)      # 이어받았다고 본다
        market.quotes = {"XRP": make_quote(-1.0), "SOL": make_quote(-2.0)}
        engine2.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 1)   # SOL 을 새로 잡지 않았다

    def test_timeout_closes_position(self):
        """ONE_SHOT_MAX_HOURS 를 넘기면 목표에 못 닿아도 정리하고 끝낸다"""
        market = FakeMarket()
        engine, sf, broker = make_one_shot_engine(market, one_shot_max_hours=1.0)
        engine.startup()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            pos = broker.open_positions(s)[0]
            pos.opened_at = datetime.now(timezone.utc) - timedelta(hours=2)   # 2시간 전에 잡은 셈
            s.commit()
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 0)
            from sqlalchemy import select
            from kimp.db import Position
            done = s.scalars(select(Position)).all()
            self.assertEqual(done[0].exit_reason, ExitReason.TIMEOUT)
            self.assertEqual(done[0].status, PositionStatus.CLOSED)
        self.assertTrue(engine._one_shot_done)

    def test_off_by_default(self):
        """평소에는 꺼져 있다 (여러 포지션을 계속 잡는다)"""
        market = FakeMarket()
        settings = make_settings()
        self.assertFalse(settings.one_shot)


class AsUtcTest(unittest.TestCase):
    """SQLite 시각 함정: 시간대가 없으면 UTC 로 붙인다 (안 하면 9시간 밀리거나 TypeError)"""

    def test_naive_becomes_utc(self):
        naive = datetime(2026, 10, 22, 8, 0, 0)
        self.assertEqual(_as_utc(naive).tzinfo, timezone.utc)

    def test_aware_untouched(self):
        aware = datetime(2026, 10, 22, 8, 0, 0, tzinfo=timezone.utc)
        self.assertIs(_as_utc(aware), aware)


if __name__ == "__main__":
    unittest.main()

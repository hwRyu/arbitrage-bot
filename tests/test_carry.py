"""펀딩비 수익 전략(kimp/carry.py) 테스트 (인터넷 필요 없음)"""
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from kimp.carry import (
    CARRY_USDT,
    CarryBroker,
    CarryEngine,
    CarryQuote,
    carry_coin_key,
    entry_ok,
    exit_due,
    funding_daily_percent,
    make_stats,
)
from kimp.config import FeeSettings, Settings
from kimp.db import CarryPosition, ExitReason, PositionStatus, make_session_factory
from kimp.premium import OrderBook

H = 3_600_000
FEES = FeeSettings(upbit_trade_fee_rate=0.0005, bybit_taker_fee_rate=0.00055, transfer_fee_usdt_per_cycle=0.0,
                   slippage_buffer_percent=0.05)


def make_settings(**kw) -> Settings:
    base = dict(coins=["XRP"], fees=FEES, carry_capital_usdt=1_000.0, carry_leverage=2.0, carry_max_positions=1,
                carry_position_size_usdt=200.0, db_path=":memory:")
    base.update(kw)
    return Settings(**base)


def book(price: float, half: float = 0.0) -> OrderBook:
    return OrderBook(bids=[(price * (1 - half), 1e9)], asks=[(price * (1 + half), 1e9)])


class FakeMarket:
    """시계 + 펀딩비 기록 + 가격을 직접 정하는 가짜 시장"""

    def __init__(self, settings):
        self.s = settings
        self.now_ms = 100 * H
        self.funding = {"XRP": []}
        self.spot = self.perp = 2.0
        self.perp_high = None

    def stats(self, now_ms, include=()):
        return {c: make_stats(h, now_ms, self.s) for c, h in self.funding.items()}

    def funding_since(self, coin, after_ms, now_ms):
        return [(t, r) for t, r in self.funding.get(coin, []) if after_ms < t <= now_ms]

    def quote(self, coin):
        return CarryQuote(coin=coin, spot=book(self.spot), perp=book(self.perp), perp_high=self.perp_high)

    def round_qty(self, coin, qty):
        return round(qty, 4)

    def min_qty(self, coin):
        return 0.0


def make_engine(settings=None):
    s = settings or make_settings()
    m = FakeMarket(s)
    sf = make_session_factory(":memory:")
    clock = lambda: m.now_ms / 1000  # noqa: E731
    broker = CarryBroker(s, now_fn=lambda: datetime.fromtimestamp(clock(), timezone.utc))
    e = CarryEngine(s, m, broker, sf, clock=clock)
    e.startup()
    return e, m, sf, broker


class StatsTest(unittest.TestCase):
    def test_daily_percent(self):
        # 72시간 동안 8시간마다 +0.01% × 9번 = 0.09% → 하루 0.03%
        hist = [(i * 8 * H, 0.0001) for i in range(1, 10)]
        self.assertAlmostEqual(funding_daily_percent(hist, 72 * H, 72), 0.03)
        # 24시간이면 마지막 3번 → 0.03% / 1일
        self.assertAlmostEqual(funding_daily_percent(hist, 72 * H, 24), 0.03)
        self.assertIsNone(funding_daily_percent(hist, 1000 * H, 24))

    def test_entry_and_exit_rules(self):
        s = make_settings()
        good = [(i * 8 * H, 0.00015) for i in range(1, 10)]  # 하루 0.045%
        self.assertTrue(entry_ok(make_stats(good, 72 * H, s), s))
        # 3일 평균은 높아도 최근 24시간이 식었으면 진입 안 함
        cooled = good[:6] + [(t, -0.0001) for t, _ in good[6:]]
        self.assertFalse(entry_ok(make_stats(cooled, 72 * H, s), s))
        self.assertTrue(exit_due(make_stats(cooled, 72 * H, s), s))
        self.assertFalse(exit_due(make_stats(good, 72 * H, s), s))


class EngineTest(unittest.TestCase):
    def _feed(self, m, rate, start_h, end_h, every=8):
        m.funding["XRP"] += [(h * H, rate) for h in range(start_h, end_h + 1, every)]

    def test_enter_collect_funding_exit(self):
        e, m, sf, broker = make_engine()
        self._feed(m, 0.0002, 4, 100)  # 8시간마다 +0.02% → 하루 0.06%
        e.step()
        with sf() as s:
            pos = s.query(CarryPosition).one()
            self.assertEqual(pos.qty, 100.0)  # 200 USDT ÷ 2.0
            self.assertAlmostEqual(pos.margin_usdt, 100.0)  # 2배
            # 1,000 − 현물 200 − 0.2 수수료 − 증거금 100 − 0.11 수수료
            self.assertAlmostEqual(broker.balance(s, CARRY_USDT), 1000 - 200 - 0.2 - 100 - 0.11)
            self.assertAlmostEqual(broker.balance(s, carry_coin_key("XRP")), 100.0)

        # 가격이 올라도 손익은 상쇄, 펀딩 3번 받음 (진입 전 정산은 안 받음)
        self._feed(m, 0.0002, 108, 124)
        m.now_ms, m.spot, m.perp = 125 * H, 2.4, 2.4
        e.step()
        with sf() as s:
            pos = s.query(CarryPosition).one()
            self.assertEqual(pos.funding_count, 3)
            self.assertAlmostEqual(pos.funding_usdt, 3 * 100 * 2.4 * 0.0002)
            self.assertEqual(pos.status, PositionStatus.OPEN)

        # 펀딩비가 음수로 바뀌면 24시간 평균이 0 아래 → 청산
        self._feed(m, -0.0003, 132, 148)
        m.now_ms = 149 * H
        e.step()
        with sf() as s:
            pos = s.query(CarryPosition).one()
            self.assertEqual(pos.exit_reason, ExitReason.FUNDING_DROP)
            fees = 0.2 + 0.11 + 100 * 2.4 * 0.001 + 100 * 2.4 * 0.00055
            self.assertAlmostEqual(pos.spot_leg_usdt + pos.perp_leg_usdt, 0.0)  # 가격 변동 상쇄
            self.assertAlmostEqual(pos.net_pnl_usdt, pos.funding_usdt - fees)
            # 잔고 = 시작 자본 + 순손익 (현물 0개 남음)
            self.assertAlmostEqual(broker.balance(s, CARRY_USDT), 1000 + pos.net_pnl_usdt)
            self.assertAlmostEqual(broker.balance(s, carry_coin_key("XRP")), 0.0)

    def test_no_entry_when_funding_low(self):
        e, m, sf, _ = make_engine()
        self._feed(m, 0.00005, 4, 100)  # 하루 0.015% < 0.03%
        e.step()
        with sf() as s:
            self.assertEqual(s.query(CarryPosition).count(), 0)

    def test_no_entry_when_perp_below_spot(self):
        e, m, sf, _ = make_engine()
        self._feed(m, 0.0002, 4, 100)
        m.perp = 1.99  # 선물이 현물보다 0.5% 쌈 → 들어가자마자 손해
        e.step()
        with sf() as s:
            self.assertEqual(s.query(CarryPosition).count(), 0)

    def test_guard_closes_before_liquidation(self):
        """강제청산(약 2.99)까지 15% 안쪽이면 양쪽을 같이 정리 → 가격 손익 상쇄"""
        e, m, sf, _ = make_engine()
        self._feed(m, 0.0002, 4, 100)
        e.step()
        m.now_ms, m.spot, m.perp = 101 * H, 2.7, 2.7  # 2.99 / 2.7 − 1 = 10.7%
        e.step()
        with sf() as s:
            pos = s.query(CarryPosition).one()
            self.assertEqual(pos.exit_reason, ExitReason.LIQ_GUARD)
            self.assertAlmostEqual(pos.spot_leg_usdt + pos.perp_leg_usdt, 0.0)

    def test_liquidation_sells_spot(self):
        e, m, sf, broker = make_engine(make_settings(carry_liq_guard_percent=0))
        self._feed(m, 0.0002, 4, 100)
        e.step()
        m.now_ms, m.spot, m.perp, m.perp_high = 101 * H, 3.0, 2.9, 3.0  # 2배 강제청산 약 2.99
        e.step()
        with sf() as s:
            pos = s.query(CarryPosition).one()
            self.assertEqual(pos.exit_reason, ExitReason.LIQUIDATION)
            self.assertAlmostEqual(pos.perp_leg_usdt, -100.0)  # 증거금 잃음
            self.assertAlmostEqual(pos.spot_leg_usdt, 100.0)  # 현물은 2.0 → 3.0
            self.assertAlmostEqual(broker.balance(s, carry_coin_key("XRP")), 0.0)

    def test_auto_size(self):
        s = replace(make_settings(), carry_position_size_usdt=None, carry_max_positions=3)
        self.assertAlmostEqual(s.carry_size_usdt(), 1000 * 0.9 / 3 / 1.5)  # 2배
        self.assertEqual(Settings().carry_leverage, 1.0)  # 기본은 1배
        self.assertAlmostEqual(Settings().carry_size_usdt(), 1000 * 0.9 / 3 / 2)


if __name__ == "__main__":
    unittest.main()

"""
시뮬레이션 테스트 (가짜 과거 데이터, 인터넷 필요 없음)

실행: python -m unittest discover -s tests -v
"""
import unittest

from kimp.config import FeeSettings, Settings
from kimp.history import (MINUTE_MS, UPBIT, HistoryStore, make_history_session_factory, missing_ranges,
                          value_at)
from kimp.simulate import (HistoricalMarket, SimClock, SimFx, apply_tick_floor, run_simulation, tick_floor_percent,
                           tick_size)

FEES = FeeSettings(upbit_trade_fee_rate=0.0005, bybit_taker_fee_rate=0.00055,
                   transfer_fee_usdt_per_cycle=1.0, slippage_buffer_percent=0.05)
FX = 1400.0
START = 1_790_000_000_000 // MINUTE_MS * MINUTE_MS  # 아무 과거 시각 (분 단위)


class FakePrecision:
    """바이비트 주문 단위 흉내: 0.1개 단위, 최소 0.1개"""

    def round_qty(self, coin, qty):
        return float(int(qty * 10) / 10)

    def min_qty(self, coin):
        return 0.1


def settings(**kw) -> Settings:
    """테스트용 설정 (진입 -0.5%, 목표 1%, 포지션 500 USDT)"""
    base = dict(coins=["XRP"], entry_premium_threshold_percent=-0.5, target_net_profit_percent=1.0,
                stop_loss_percent=-5.0, position_size_usdt=500, max_open_positions=1, fees=FEES,
                funding_refresh_sec=60, paper_initial_total_usdt=10_000,
                reentry_cooldown_sec=300)
    base.update(kw)
    return Settings(**base)


def build(premiums, bybit_price=1.0, funding=None, spread=0.0):
    """
    분마다 김프(%) 목록으로 가짜 과거 데이터를 만든다

    예) premiums = [-1.0]*60 + [1.0]*60 → 처음 1시간은 역프 1%, 다음 1시간은 김프 1%
    """
    clock = SimClock(START / 1000)
    usdt_price = FX * 1.005  # 업비트 USDT (테더 +0.5%)
    # 업비트 코인 = 바이비트 × 업비트 USDT × (1 + 코인 몫 김프)
    upbit = {"XRP": {START + i * MINUTE_MS: bybit_price * usdt_price * (1 + p / 100) for i, p in enumerate(premiums)}}
    bybit = {"XRP": {START + i * MINUTE_MS: bybit_price for i in range(len(premiums))}}
    usdt = {START + i * MINUTE_MS: usdt_price for i in range(len(premiums))}
    market = HistoricalMarket(clock, FakePrecision(), upbit, bybit, usdt, {"XRP": funding or []}, {"XRP": spread},
                              usdt_spread_percent=0.0)
    fx = SimFx(clock, [(START - 3_600_000, FX)])
    end = START + len(premiums) * MINUTE_MS
    return clock, market, fx, end


class SimulationTest(unittest.TestCase):
    """가짜 과거 데이터로 전체 시뮬레이션"""

    def test_enter_then_take_profit(self):
        """역프 1% 에서 진입 → 김프 +1% 가 되면 익절. 손익은 김프 변화 2%p - 비용"""
        clock, market, fx, end = build([-1.0] * 60 + [1.0] * 60)
        r = run_simulation(settings(), ["XRP"], market, fx, clock, START, end)
        self.assertEqual(len(r.closed), 1)
        t = r.closed[0]
        self.assertEqual(t.exit_reason, "TAKE_PROFIT")
        self.assertAlmostEqual(t.entry_premium, -1.0, places=6)
        self.assertGreater(t.net_pnl_percent, 1.0)
        self.assertLess(t.net_pnl_percent, 2.0)  # 비용이 빠져서 2% 보다 작음
        # 진입은 데이터 첫 분이 끝난 순간, 청산은 김프가 바뀐 첫 분이 끝난 순간
        self.assertEqual(int(t.opened_at.timestamp() * 1000), START + MINUTE_MS)
        self.assertEqual(int(t.closed_at.timestamp() * 1000), START + 61 * MINUTE_MS)
        self.assertEqual(r.capital_start_usdt, 10_000)

    def test_no_entry_when_premium_high(self):
        """김프가 계속 +0.5% 면 진입하지 않는다"""
        clock, market, fx, end = build([0.5] * 120)
        r = run_simulation(settings(), ["XRP"], market, fx, clock, START, end)
        self.assertEqual(r.trades, [])

    def test_open_position_at_end_is_valued(self):
        """끝까지 회복이 안 되면 'OPEN' 으로, 마지막 순간 청산했다고 치고 손익 계산"""
        clock, market, fx, end = build([-1.0] * 120)
        r = run_simulation(settings(), ["XRP"], market, fx, clock, START, end)
        [t] = r.trades
        self.assertEqual(t.exit_reason, "OPEN")
        self.assertLess(t.net_pnl_usdt, 0)  # 김프 그대로 → 비용만큼 손해

    def test_spread_makes_it_worse(self):
        """호가 간격을 크게 가정할수록 손익이 줄어든다"""
        a = run_simulation(settings(), ["XRP"], *self._args([-1.0] * 60 + [1.0] * 60, spread=0.0))
        b = run_simulation(settings(), ["XRP"], *self._args([-1.0] * 60 + [1.0] * 60, spread=0.3))
        self.assertGreater(a.total_net_usdt, b.total_net_usdt)

    def test_funding_applied_on_schedule(self):
        """보유 중에 지난 펀딩비 정산이 손익에 들어간다 (숏이라 비율 - 면 낸다)"""
        # 펀딩비 안전장치는 꺼 둔다 — 안 그러면 '내는 코인' 이라 진입이 막혀서 반영을 확인할 수 없다
        funding = [(START + 30 * MINUTE_MS, -0.001)]  # 30분 뒤 -0.1% 정산
        clock, market, fx, end = build([-1.0] * 120, funding=funding)
        no_guard = settings(entry_min_funding_8h_percent=None, max_funding_cost_percent=0)
        r = run_simulation(no_guard, ["XRP"], market, fx, clock, START, end)
        [t] = r.trades
        self.assertLess(t.funding_usdt, 0)
        self.assertAlmostEqual(t.funding_usdt, -0.001 * 500, delta=0.01)

    def test_reentry_cooldown_uses_sim_time(self):
        """익절 직후 다시 역프가 와도 재진입 대기(5분) 동안은 안 들어가고, 지나면 들어간다"""
        premiums = [-1.0] * 10 + [1.0] * 2 + [-1.0] * 20
        clock, market, fx, end = build(premiums)
        r = run_simulation(settings(), ["XRP"], market, fx, clock, START, end)
        self.assertEqual(len(r.trades), 2)
        gap_min = (r.trades[1].opened_at - r.trades[0].closed_at).total_seconds() / 60
        self.assertGreaterEqual(gap_min, 5)

    def test_tick_bounce_gives_no_fake_profit(self):
        """
        DOGE 처럼 업비트 호가 단위가 커서 종가가 109 ↔ 110원을 오가기만 하는 경우,
        호가 간격을 호가 한 칸 이상으로 잡으면 가짜 수익이 나지 않는다 (실제로 겪은 문제)
        """
        fx = 1400.0
        bybit_price = 109.5 / fx  # 바이비트는 가운데에 고정
        n = 240
        up_closes = [109.0 if i % 2 == 0 else 110.0 for i in range(n)]  # 매분 109 ↔ 110
        clock = SimClock(START / 1000)
        upbit = {"DOGE": {START + i * MINUTE_MS: up_closes[i] for i in range(n)}}
        bybit = {"DOGE": {START + i * MINUTE_MS: bybit_price for i in range(n)}}
        st = settings(coins=["DOGE"], entry_premium_threshold_percent=0.0, target_net_profit_percent=0.3,
                      reentry_cooldown_sec=0)

        def run(spread):
            usdt = {START + i * MINUTE_MS: fx for i in range(n)}
            market = HistoricalMarket(clock, FakePrecision(), upbit, bybit, usdt, {"DOGE": []}, {"DOGE": spread},
                                      usdt_spread_percent=0.0)
            return run_simulation(st, ["DOGE"], market, SimFx(clock, [(START - 3_600_000, fx)]), clock,
                                  START, START + n * MINUTE_MS)

        fake = run(0.1)  # 호가 간격을 너무 작게 가정 → 109↔110 을 이익으로 착각
        self.assertGreater(len(fake.closed), 10)
        floor = tick_floor_percent(up_closes, [bybit_price, bybit_price + 0.00001])
        self.assertAlmostEqual(floor, 1 / 109.5 * 100, delta=0.05)
        real = run(floor)  # 호가 한 칸 이상으로 잡으면 거래가 안 되거나 이익이 없다
        self.assertLessEqual(sum(t.net_pnl_usdt for t in real.closed), 0)

    def test_tick_floor_helpers(self):
        """호가 단위 찾기 + 하한 적용"""
        self.assertEqual(tick_size([109, 110, 109, 111]), 1)
        # 이상한 값 하나(1,334.5)는 무시하고 실제 호가 단위 1원을 찾는다 (실제로 겪은 문제)
        closes = [1333 + (i % 3) for i in range(1000)] + [1334.5] + [1334] * 10
        self.assertEqual(tick_size(closes), 1)
        spreads, sources = {"DOGE": 0.1, "XRP": 0.2}, {"DOGE": "기본 설정", "XRP": "실측 50건"}
        apply_tick_floor(spreads, sources, {"DOGE": 0.92, "XRP": 0.06})
        self.assertEqual(spreads, {"DOGE": 0.92, "XRP": 0.2})  # XRP 는 이미 하한보다 커서 그대로
        self.assertIn("하한", sources["DOGE"])

    @staticmethod
    def _args(premiums, spread):
        clock, market, fx, end = build(premiums, spread=spread)
        return market, fx, clock, START, end


class HistoryStoreTest(unittest.TestCase):
    """과거 데이터 저장소: 모자란 구간만 받기"""

    def test_missing_ranges(self):
        """[0,100) 중 [20,50), [60,70) 을 받았으면 남은 건 [0,20), [50,60), [70,100)"""
        self.assertEqual(missing_ranges((0, 100), [(20, 50), (60, 70)]), [(0, 20), (50, 60), (70, 100)])
        self.assertEqual(missing_ranges((0, 100), [(0, 100)]), [])
        self.assertEqual(missing_ranges((0, 100), []), [(0, 100)])

    def test_funding_no_gaps_for_hourly_coin(self):
        """1시간마다 정산하는 코인 400시간: 200시간 칸으로 나눠 받아서 빠짐없이 400개 (예전엔 끝 쪽만 받아 중간이 빔)"""
        from kimp.history import FUNDING_WINDOW_MS, HOUR_MS
        all_rows = [(h * HOUR_MS, 0.0001) for h in range(400)]

        def fetch(since):  # 거래소 흉내: since ~ since+200시간 안의 정산만 준다
            return [r for r in all_rows if since <= r[0] < since + FUNDING_WINDOW_MS]

        store = HistoryStore(make_history_session_factory(":memory:"))
        self.assertEqual(store.ensure_funding("ERA", 0, 400 * HOUR_MS, fetch), 400)
        self.assertEqual(len(store.load_funding("ERA", 0, 400 * HOUR_MS)), 400)

    def test_candles_fetched_once(self):
        """한 번 받은 구간은 다시 받지 않고, 업비트처럼 빈 분이 있어도 끝없이 다시 받지 않는다"""
        calls = []

        def fetch(since, limit):
            calls.append(since)
            # 3분마다 1개씩만 있는 캔들 (거래가 드문 코인 흉내), 한 번에 최대 5개
            rows = [[ts, 1, 1, 1, 1, 1] for ts in range(since, START + 60 * MINUTE_MS, MINUTE_MS)
                    if (ts - START) % (3 * MINUTE_MS) == 0]
            return rows[:5]

        store = HistoryStore(make_history_session_factory(":memory:"))
        n = store.ensure_candles(UPBIT, "XRP", START, START + 60 * MINUTE_MS, fetch)
        self.assertEqual(n, 20)
        first_calls = len(calls)
        self.assertEqual(store.ensure_candles(UPBIT, "XRP", START, START + 60 * MINUTE_MS, fetch), 0)
        self.assertEqual(len(calls), first_calls)  # 다시 안 받음
        self.assertEqual(len(store.load_closes(UPBIT, "XRP", START, START + 60 * MINUTE_MS)), 20)

    def test_candles_with_maintenance_gap(self):
        """
        업비트처럼 '기준 시각 이전 N개' 를 주는 방식에서 중간에 몇 시간 빈 구간(점검)이 있어도
        빈 구간을 건너뛰고 뒤쪽 캔들까지 다 받는다 (실제로 겪은 버그)
        """
        gap = range(START + 300 * MINUTE_MS, START + 600 * MINUTE_MS)  # 5시간 빈 구간
        all_ts = [START + i * MINUTE_MS for i in range(900) if START + i * MINUTE_MS not in gap]

        def fetch(since, limit):
            to = since + 200 * MINUTE_MS  # 업비트: since + 200분 '이전' 의 최근 200개
            return [[ts, 1, 1, 1, 1, 1] for ts in [t for t in all_ts if t < to][-200:]]

        store = HistoryStore(make_history_session_factory(":memory:"))
        n = store.ensure_candles(UPBIT, "XRP", START, START + 900 * MINUTE_MS, fetch)
        self.assertEqual(n, len(all_ts))  # 빈 구간 뒤의 300개까지 전부

    def test_value_at(self):
        """10:00 에 1325, 11:00 에 1326 이면 10:37 은 1325, 9:00 은 없음"""
        series = [(10, 1325.0), (11, 1326.0)]
        self.assertEqual(value_at(series, 10), 1325.0)
        self.assertEqual(value_at(series, 10.6), 1325.0)
        self.assertEqual(value_at(series, 11), 1326.0)
        self.assertIsNone(value_at(series, 9))


if __name__ == "__main__":
    unittest.main()

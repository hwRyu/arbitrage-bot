"""
목표 순이익 → 관리선(LCL/UCL) 역산 테스트 (네트워크 불필요)

kimp/levels.py 는 코인의 실제 비용을 모아 '몇 %p 올라야 목표가 남나' 를 구하고,
그 폭을 기간 안에서 왕복이 가장 많은 자리에 놓는다.

실행: python -m unittest discover -s tests -v
"""
import shutil
import tempfile
import unittest

from kimp.config import FeeSettings, Settings
from kimp.levels import (DEFAULT_USDT_SPREAD_PERCENT, MIN_POINTS, best_levels, cost_items,
                         count_round_trips, notional_for, plan, tick_floor_from_series)

HOUR_MS = 3_600_000


def series(values, usdt_spread=0.075, funding=None, start_ms=0, step_ms=60_000):
    """차트 모양(series)으로 만들기 — mid 만 바꾸고 나머지는 고정"""
    return [{"ts": start_ms + i * step_ms, "mid": v, "usdt_spread": usdt_spread, "funding": funding}
            for i, v in enumerate(values)]


def wave(low, high, cycles, per_cycle=40):
    """low → high → low 를 cycles 번 반복하는 톱니 (왕복 세기 테스트용)"""
    out = []
    half = per_cycle // 2
    for _ in range(cycles):
        out += [low + (high - low) * i / (half - 1) for i in range(half)]
        out += [high - (high - low) * i / (half - 1) for i in range(half)]
    return out


class CostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kimp_lv_")
        self.s = Settings(coins=["CAP"], snapshot_db_path=self.tmp + "/none.db",
                          db_path=self.tmp + "/none2.db", max_position_usdt=100.0,
                          sim_default_spread_percent=0.1)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_notional_priority(self):
        """포지션 금액: 코인별/공통 설정 → MAX_POSITION_USDT → 기본값"""
        self.assertEqual(notional_for(self.s, "CAP"), 100.0)                  # max_position_usdt
        fixed = Settings(coins=["CAP"], position_size_usdt=250.0, max_position_usdt=100.0)
        self.assertEqual(notional_for(fixed, "CAP"), 250.0)                   # 정해 둔 크기가 먼저

    def test_items_and_total(self):
        """비용 항목이 다 들어오고 합계가 맞다 (기록이 없으면 기본 호가 간격)"""
        items = {i.key: i for i in cost_items(self.s, "CAP", series([0.0] * 60), 100.0, 0.0)}
        self.assertEqual(set(items), {"fee", "coin_spread", "usdt_spread", "transfer", "funding", "buffer"})
        self.assertAlmostEqual(items["fee"].percent, self.s.fees.round_trip_trade_fee_percent())
        self.assertAlmostEqual(items["coin_spread"].percent, 0.1)             # 기록 없음 → 기본값
        self.assertAlmostEqual(items["usdt_spread"].percent, 0.075)           # 기록 중간값
        self.assertAlmostEqual(items["buffer"].percent, self.s.fees.slippage_buffer_percent)

    def test_usdt_spread_falls_back(self):
        """USDT 호가 간격 기록이 없으면 기본값을 쓴다"""
        rows = [{"ts": 0, "mid": 0.0, "usdt_spread": None, "funding": None}] * 60
        items = {i.key: i for i in cost_items(self.s, "CAP", rows, 100.0, 0.0)}
        self.assertAlmostEqual(items["usdt_spread"].percent, DEFAULT_USDT_SPREAD_PERCENT)

    def test_transfer_share_depends_on_size(self):
        """전송비는 금액 대비 비율이다 — 포지션이 작으면 비중이 커진다"""
        s = Settings(coins=["CAP"], fees=FeeSettings(transfer_fee_usdt_per_cycle=1.3))
        small = {i.key: i for i in cost_items(s, "CAP", series([0.0] * 60), 50.0, 0.0)}
        big = {i.key: i for i in cost_items(s, "CAP", series([0.0] * 60), 1000.0, 0.0)}
        self.assertAlmostEqual(small["transfer"].percent, 2.6)    # 1.3 / 50
        self.assertAlmostEqual(big["transfer"].percent, 0.13)     # 1.3 / 1000

    def test_funding_sign_and_hold_time(self):
        """펀딩비를 내면 필요 폭이 늘고, 받으면 줄어든다. 보유 시간에 비례한다"""
        pay = {i.key: i for i in cost_items(self.s, "CAP", series([0.0] * 60, funding=-0.04), 100.0, 8.0)}
        self.assertAlmostEqual(pay["funding"].percent, 0.04)      # 8시간 보유 → 그대로 비용
        half = {i.key: i for i in cost_items(self.s, "CAP", series([0.0] * 60, funding=-0.04), 100.0, 4.0)}
        self.assertAlmostEqual(half["funding"].percent, 0.02)     # 4시간 → 절반
        get = {i.key: i for i in cost_items(self.s, "CAP", series([0.0] * 60, funding=0.04), 100.0, 8.0)}
        self.assertAlmostEqual(get["funding"].percent, -0.04)     # 받으면 마이너스 (필요 폭이 줄어든다)


class TickFloorTest(unittest.TestCase):
    """호가 한 칸보다 좁은 호가 간격은 가정하지 않는다 (업비트 원화는 칸 단위로만 움직인다)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kimp_lv3_")
        self.s = Settings(coins=["DOGE"], snapshot_db_path=self.tmp + "/none.db",
                          db_path=self.tmp + "/none2.db", max_position_usdt=100.0,
                          sim_default_spread_percent=0.1)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _priced(self, upbit_prices, bybit_prices):
        return [{"ts": i * 60_000, "mid": 0.0, "usdt_spread": 0.075, "funding": None,
                 "upbit_krw": u, "bybit_usdt": b}
                for i, (u, b) in enumerate(zip(upbit_prices, bybit_prices))]

    def test_floor_from_prices(self):
        """업비트 106원이 1원 단위로 움직이면 하한은 약 0.94% (+ 바이비트 몫)

        한 칸은 '가격 중간값' 으로 나눈다 (가격이 오르내리므로 한쪽 끝을 쓰면 치우친다).
        """
        up = [106.0 + (i % 2) for i in range(40)]             # 106 / 107 → 한 칸 1원, 중간값 107
        by = [0.0790 + 0.00001 * (i % 2) for i in range(40)]  # 한 칸 0.00001, 중간값 0.07901
        floor = tick_floor_from_series(self._priced(up, by))
        self.assertAlmostEqual(floor, 1.0 / 107.0 * 100 + 0.00001 / 0.07901 * 100, places=3)
        self.assertAlmostEqual(floor, 0.947, places=2)        # DOGE 실측(0.95%)과 같은 수준

    def test_floor_raises_default_spread(self):
        """기본 추측값 0.1% 보다 하한이 크면 하한으로 올라가고, 출처에 그렇게 적힌다"""
        rows = self._priced([106.0 + (i % 2) for i in range(40)],
                            [0.0790 + 0.00001 * (i % 2) for i in range(40)])
        item = {i.key: i for i in cost_items(self.s, "DOGE", rows, 100.0, 0.0)}["coin_spread"]
        self.assertGreater(item.percent, 0.9)
        self.assertIn("호가 단위 하한", item.detail)

    def test_floor_does_not_lower_a_wider_spread(self):
        """하한이 가정값보다 작으면 가정값을 그대로 둔다 (하한은 내리는 장치가 아니다)"""
        rows = self._priced([5000.0 + (i % 2) for i in range(40)],   # 한 칸 1원 / 5,000원 = 0.02%
                            [3.75 + 0.0001 * (i % 2) for i in range(40)])
        item = {i.key: i for i in cost_items(self.s, "DOGE", rows, 100.0, 0.0)}["coin_spread"]
        self.assertAlmostEqual(item.percent, 0.1)                 # 기본값 유지
        self.assertNotIn("하한", item.detail)

    def test_no_prices_means_no_floor(self):
        """가격이 없으면(1분봉이 안 받아진 코인) 하한을 못 구한다 → None"""
        self.assertIsNone(tick_floor_from_series(series([0.1] * 40)))


class RoundTripTest(unittest.TestCase):
    def test_counts_completed_trips_only(self):
        """LCL 이하 진입 → UCL 이상 청산. 청산 못 한 마지막 진입은 왕복에 안 넣는다"""
        points = [(i * HOUR_MS, v) for i, v in enumerate([0.0, -1.0, 0.5, -1.0, 0.0])]
        lv = count_round_trips(points, -0.5, 0.4)
        self.assertEqual((lv.round_trips, lv.entries), (1, 2))
        self.assertTrue(lv.still_holding)
        self.assertAlmostEqual(lv.avg_hold_hours, 1.0)            # 1시간째 진입 → 2시간째 청산

    def test_no_double_entry(self):
        """이미 들고 있으면 또 진입하지 않는다 (같은 코인 포지션은 하나)"""
        points = [(i * HOUR_MS, v) for i, v in enumerate([-1.0, -1.2, -1.1, 1.0])]
        lv = count_round_trips(points, -0.5, 0.5)
        self.assertEqual((lv.round_trips, lv.entries), (1, 1))

    def test_no_trip_when_gap_too_wide(self):
        """폭이 변동폭보다 넓으면 왕복이 0 이다"""
        lv = count_round_trips([(i * HOUR_MS, v) for i, v in enumerate([-0.1, 0.1] * 20)], -0.05, 5.0)
        self.assertEqual(lv.round_trips, 0)

    def test_best_levels_picks_most_trips(self):
        """톱니 4번 + 폭 0.5 → 가장 왕복이 많은 자리를 고른다"""
        vals = wave(-1.0, 1.0, 4)
        points = [(i * 60_000, v) for i, v in enumerate(vals)]
        best, cands = best_levels(points, 0.5)
        self.assertIsNotNone(best)
        self.assertEqual(best.round_trips, 4)
        self.assertAlmostEqual(best.ucl - best.lcl, 0.5, places=3)
        self.assertTrue(all(c.round_trips > 0 for c in cands))

    def test_best_levels_needs_enough_points(self):
        """점이 모자라면 계산하지 않는다"""
        best, cands = best_levels([(i, 0.1) for i in range(MIN_POINTS - 1)], 0.5)
        self.assertIsNone(best)
        self.assertEqual(cands, [])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kimp_lv2_")
        self.s = Settings(coins=["CAP"], snapshot_db_path=self.tmp + "/none.db",
                          db_path=self.tmp + "/none2.db", max_position_usdt=100.0,
                          sim_default_spread_percent=0.1, target_net_profit_percent=1.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_required_gap_is_target_plus_costs(self):
        """필요한 폭 = 목표 + 비용 합계"""
        r = plan(self.s, "CAP", series(wave(-1.0, 1.5, 6)), target_percent=1.0, hours=24.0)
        self.assertAlmostEqual(r["required_gap_percent"], 1.0 + r["cost_percent"], places=6)
        # 수수료 0.31 + 호가 0.1 + USDT 0.075 + 여유 0.05 = 0.535
        self.assertAlmostEqual(r["cost_percent"], 0.535, places=3)
        self.assertAlmostEqual(r["required_gap_percent"], 1.535, places=3)

    def test_wider_spread_needs_more_rise(self):
        """호가 간격이 넓은 코인은 같은 목표를 내려면 더 많이 올라야 한다"""
        wide = Settings(coins=["CAP"], snapshot_db_path=self.tmp + "/n.db", db_path=self.tmp + "/n2.db",
                        max_position_usdt=100.0, sim_default_spread_percent=0.95)
        a = plan(self.s, "CAP", series(wave(-1.0, 1.5, 6)), 1.0, hours=24.0)
        b = plan(wide, "CAP", series(wave(-1.0, 1.5, 6)), 1.0, hours=24.0)
        self.assertAlmostEqual(b["required_gap_percent"] - a["required_gap_percent"], 0.85, places=3)

    def test_levels_keep_the_gap(self):
        """고른 관리선의 폭은 필요한 폭과 같다"""
        r = plan(self.s, "CAP", series(wave(-1.0, 1.5, 6)), 1.0, hours=24.0)
        lv = r["levels"]
        self.assertAlmostEqual(lv["ucl"] - lv["lcl"], r["required_gap_percent"], places=3)
        self.assertGreater(lv["round_trips"], 0)

    def test_warns_when_impossible(self):
        """변동폭이 필요한 폭보다 좁으면 왕복 0 + 경고를 준다 (숨기지 않는다)"""
        r = plan(self.s, "CAP", series(wave(-0.2, 0.2, 10)), target_percent=1.0, hours=24.0)
        self.assertEqual(r["levels"]["round_trips"], 0)
        self.assertIn("왕복이 한 번도 없었습니다", r["warning"])
        self.assertLess(r["window"]["swing"], r["required_gap_percent"])

    def test_monthly_estimate(self):
        """월 환산 거래 수와 수익 (기간 길이로 늘린 값)"""
        r = plan(self.s, "CAP", series(wave(-1.0, 1.5, 6)), 1.0, notional_usdt=200.0, hours=24.0)
        trips = r["levels"]["round_trips"]
        self.assertAlmostEqual(r["trips_per_month"], trips / 24 * 24 * 30, places=6)
        # 목표 1% × 포지션 200 USDT = 건당 2 USDT
        self.assertAlmostEqual(r["profit_per_month_usdt"], r["trips_per_month"] * 2.0, places=6)

    def test_funding_refined_with_hold_time(self):
        """펀딩비는 1차에서 나온 평균 보유 시간으로 다시 계산한다 (0 이 아니다)"""
        r = plan(self.s, "CAP", series(wave(-1.0, 1.5, 6), funding=-0.05), 1.0, hours=24.0)
        self.assertGreater(r["assumed_hold_hours"], 0)
        funding = [c for c in r["costs"] if c["key"] == "funding"][0]
        self.assertGreater(funding["percent"], 0)   # 내는 쪽이니 비용이 플러스

    def test_too_few_points(self):
        """점이 모자라면 에러 메시지를 돌려준다 (화면이 깨지지 않게)"""
        r = plan(self.s, "CAP", series([0.1] * 5), 1.0, hours=1.0)
        self.assertIn("error", r)
        self.assertIsNone(r.get("levels"))


if __name__ == "__main__":
    unittest.main()

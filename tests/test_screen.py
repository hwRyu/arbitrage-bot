"""추천 코인 순위(kimp/screen.py) 계산 테스트"""
import unittest

from kimp.screen import (
    CoinLive,
    SwingCosts,
    best_swing,
    evaluate_coin,
    funding_per_8h_percent,
    half_life_hours,
    per_month,
    premium_series,
    premium_stats,
    rank,
    run_swing,
)

BAR = 900_000  # 15분


def series(values):
    return [(i * BAR, v) for i, v in enumerate(values)]


class PremiumSeriesTest(unittest.TestCase):
    def test_coin_premium(self):
        # 업비트 3,267원 ÷ (2.50 × 1,320) − 1 = −1.0%
        out = premium_series({0: 3300.0, 1: 3267.0}, {0: 2.5, 1: 2.5}, {0: 1320.0, 1: 1320.0})
        self.assertAlmostEqual(out[0][1], 0.0)
        self.assertAlmostEqual(out[1][1], -1.0)

    def test_skips_missing_times(self):
        out = premium_series({0: 3300.0, 1: 3300.0}, {0: 2.5}, {0: 1320.0, 1: 1320.0})
        self.assertEqual([ts for ts, _ in out], [0])


class StatsTest(unittest.TestCase):
    def test_half_life_fast_vs_none(self):
        # 위아래로 번갈아 튀면 바로 돌아옴 → 짧음
        fast = half_life_hours([1, -1] * 50, BAR)
        self.assertLessEqual(fast, 0.25)
        # 계속 오르기만 하면(추세) 반감기가 아주 길거나 없음 (1시간 평균으로)
        trend = half_life_hours([i * 0.01 for i in range(200)], BAR)
        self.assertTrue(trend is None or trend > 10)

    def test_hourly_means_remove_bounce(self):
        from kimp.screen import hourly_means
        out = hourly_means([-0.5, 0.4, -0.5, 0.4, 1.0], BAR)  # 남는 1개는 버림
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0], -0.05)

    def test_sideways_and_drift(self):
        st = premium_stats([0.0] * 75 + [1.0] * 25, BAR)
        self.assertAlmostEqual(st.sideways_share, 0.75)
        self.assertAlmostEqual(st.drift, 1.0)


class SwingTest(unittest.TestCase):
    # 호가 간격(진입·청산 한 번마다) 0.1, 수수료+전송비 0.4, 목표 1.0, 손절 -5
    costs = SwingCosts(leg_spread=0.1, fixed=0.4, target=1.0, stop=-5.0)

    def test_round_trip(self):
        # -1.2 에서 진입(진입 김프 -1.1), +0.5 에서 청산(청산 김프 +0.4)
        # 순손익 = 0.4 − (−1.1) − 0.4 = +1.1 ≥ 1.0
        r = run_swing(series([0, -1.2, -0.5, 0.3, 0.5, 0]), threshold=-1.0, costs=self.costs)
        self.assertEqual(r.trades, 1)
        self.assertEqual(r.wins, 1)
        self.assertAlmostEqual(r.net_closed, 1.1)
        self.assertFalse(r.is_open_at_end)

    def test_not_enough_rise_stays_open(self):
        # +0.3 까지만 오름 → 순손익 0.2 − (−1.1) − 0.4 = +0.9 < 1.0 → 못 팔고 들고 끝남
        r = run_swing(series([-1.2, 0.3]), threshold=-1.0, costs=self.costs)
        self.assertEqual(r.trades, 0)
        self.assertTrue(r.is_open_at_end)
        self.assertAlmostEqual(r.open_net, 0.9)

    def test_entry_on_last_bar(self):
        r = run_swing(series([0, -1.2]), threshold=-1.0, costs=self.costs)
        self.assertAlmostEqual(r.open_net, -0.6)  # 호가 0.2 + 수수료 0.4

    def test_funding_counts_while_holding(self):
        # 진입 뒤 정산 1번 +0.01% (받음), 진입 전 정산은 무시
        funding = [(0, 0.01), (2 * BAR, 0.0001)]
        r = run_swing(series([0, -1.2, -1.0, 0.5]), threshold=-1.0, costs=self.costs, funding=funding)
        self.assertAlmostEqual(r.funding, 0.01)
        self.assertAlmostEqual(r.net_closed, 0.4 + 1.1 - 0.4 + 0.01)

    def test_stop_loss(self):
        r = run_swing(series([-1.2, -7.0]), threshold=-1.0, costs=self.costs)
        self.assertEqual(r.trades, 1)
        self.assertEqual(r.wins, 0)
        self.assertLess(r.net_closed, -5)

    def test_best_swing_needs_closed_trade(self):
        self.assertIsNone(best_swing(series([0.0] * 100), self.costs))
        wave = series(([-1.5] * 5 + [0.8] * 5) * 10)
        best = best_swing(wave, self.costs)
        self.assertEqual(best.trades, 10)


class WeightTest(unittest.TestCase):
    def test_cycle_weight(self):
        from kimp.screen import cycle_weight
        self.assertEqual(cycle_weight(12), 1.0)
        self.assertAlmostEqual(cycle_weight(48), 0.5)
        self.assertAlmostEqual(cycle_weight(240), 0.1)  # 30일에 3번
        self.assertEqual(cycle_weight(None), 0.0)

    def test_band_weight(self):
        from kimp.screen import band_weight
        self.assertEqual(band_weight(0.1), 1.0)
        self.assertAlmostEqual(band_weight(0.9), 0.5)
        self.assertEqual(band_weight(2.0), 0.0)

    def test_band_shift(self):
        # 앞 절반 0%, 뒤 절반 1% 로 띠가 옮겨감 → 1%p
        st = premium_stats([0.0] * 100 + [1.0] * 100, BAR)
        self.assertAlmostEqual(st.band_shift, 1.0)


class ScoreTest(unittest.TestCase):
    def test_helpers(self):
        self.assertAlmostEqual(per_month(0.7, 14), 1.5)
        # 14일 합 +0.42% → 8시간 칸 42개 → +0.01%
        self.assertAlmostEqual(funding_per_8h_percent([(0, 0.0042)], 14), 0.01)

    def _eval(self, coin, values):
        return evaluate_coin(coin, series(values), BAR, days=len(values) * BAR / 86_400_000,
                             spread_percent=0.05, usdt_spread_percent=0.05, fixed_cost_percent=0.4,
                             target=1.0, stop=-5.0, funding=[], live=CoinLive(), size_usdt=1000)

    def test_wave_coin_ranks_above_flat_coin(self):
        wave = self._eval("WAVE", ([-1.5] * 5 + [0.8] * 5) * 40)  # 4등분해도 구간마다 모양이 같게
        flat = self._eval("FLAT", [0.0, 0.05] * 150)
        self.assertGreater(wave.score, 0)
        self.assertEqual([s.coin for s in rank([flat, wave])], ["WAVE", "FLAT"])

    def test_slow_cycle_scores_lower(self):
        # 같은 모양이지만 왕복이 4배 느린 코인 → 주기 가중치가 낮아 점수가 낮다
        fast = self._eval("FAST", ([-1.5] * 5 + [0.8] * 5) * 40)
        slow = self._eval("SLOW", ([-1.5] * 20 + [0.8] * 20) * 10)
        self.assertGreater(fast.score, slow.score)
        self.assertLess(slow.weight, fast.weight)

    def test_abnormal_goes_last(self):
        wave = ([-1.5] * 5 + [0.8] * 5) * 30
        bad = self._eval("BAD", [v - 20 for v in wave])  # 김프 -20% 근처: 입출금 막힘 같은 상황
        good = self._eval("GOOD", [0.0, 0.05] * 150)
        self.assertIn("이상김프", bad.flags)
        self.assertEqual([s.coin for s in rank([bad, good])], ["GOOD", "BAD"])

    def test_trend_flag(self):
        s = self._eval("UP", [i * 0.02 for i in range(300)])
        self.assertIn("추세↑", s.flags)


if __name__ == "__main__":
    unittest.main()

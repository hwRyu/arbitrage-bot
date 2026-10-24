"""추천 코인 순위(kimp/screen.py) 계산 테스트"""
import unittest

from kimp.screen import (
    RECENT_FAIL_DISCOUNT,
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
        """같은 모양이지만 왕복이 4배 느린 코인 → 자리를 오래 차지해서 점수가 낮다"""
        fast = self._eval("FAST", ([-1.5] * 5 + [0.8] * 5) * 40)
        slow = self._eval("SLOW", ([-1.5] * 20 + [0.8] * 20) * 10)
        self.assertGreater(fast.score, slow.score)
        # 이 가짜 데이터는 둘 다 '절반은 들고 있는' 모양이라 점유율이 같다.
        # 점수 차이는 같은 자리를 쓰면서 느린 코인이 덜 번다는 데서 나온다.
        self.assertAlmostEqual(slow.occupancy, fast.occupancy, places=6)
        self.assertGreater(fast.base_score, slow.base_score)

    def test_slot_efficiency(self):
        """
        자리당 효율 = 월 수익 ÷ 자리 점유율 (하한 25%)

        자리를 80% 쓰면서 월 +6% < 자리를 25% 쓰면서 월 +4% (남는 자리에 다른 코인을 넣을 수 있다)
        """
        from kimp.screen import MIN_OCCUPANCY, slot_efficiency
        self.assertAlmostEqual(slot_efficiency(6.0, 0.8), 7.5)
        self.assertAlmostEqual(slot_efficiency(4.0, 0.25), 16.0)
        self.assertGreater(slot_efficiency(4.0, 0.25), slot_efficiency(6.0, 0.8))
        # 점유율이 하한보다 작아도 하한으로 계산 (거래 1~2건 코인 과대평가 방지)
        self.assertAlmostEqual(slot_efficiency(4.0, 0.01), slot_efficiency(4.0, MIN_OCCUPANCY))

    def test_occupancy(self):
        """자리 점유율 = 들고 있던 시간 ÷ 전체 기간"""
        s = self._eval("WAVE", ([-1.5] * 5 + [0.8] * 5) * 40)
        held = sum(s.full.holding_hours)
        self.assertAlmostEqual(s.occupancy, held / (s.days * 24), places=6)
        self.assertLessEqual(s.occupancy, 1.0)

    def test_env_snippet_respects_slot_budget(self):
        """
        추천 목록은 자리 예산(자리 수 × 0.8) 안에서만 고른다

        느린 코인을 여러 개 넣어 빠른 코인의 자리를 막는 일을 막는다.
        """
        from kimp.screen import env_snippet
        # 점유율 50% 짜리 코인 6개 → 예산 2.4 ÷ 0.5 = 4개까지만 담긴다
        waves = [self._eval(f"WAVE{i}", ([-1.5] * 5 + [0.8] * 5) * 40) for i in range(6)]
        ranked = rank(waves)
        self.assertTrue(all(s.score > 0 for s in ranked))
        out = env_snippet(ranked, n=10, max_open_positions=3)
        coins = [l for l in out.splitlines() if l.startswith("COINS=")][0]
        picked = coins.removeprefix("COINS=").split(",")
        used = sum(max(s.occupancy, 0.25) for s in ranked if s.coin in picked)
        self.assertLessEqual(used, 3 * 0.8 + 1e-9)
        self.assertIn("자리가 모자라 빠진 코인", out)

    def test_timeframe_recorded(self):
        """2단계에서 다시 본 코인을 구분하려고 어떤 봉으로 평가했는지 남긴다"""
        from kimp.screen import evaluate_all
        up = {i * 900_000: 100.0 for i in range(300)}
        by = {i * 900_000: 1.0 for i in range(300)}
        usdt = {i * 900_000: 100.0 for i in range(300)}
        scores = evaluate_all({"X": (up, by, [])}, usdt, "15m", days=3.0, resolved={"X": 0.05},
                              usdt_spread=0.05, fixed=0.4, target=1.0, stop=-5.0,
                              live={"X": CoinLive()}, size=1000, cycle_hours=24.0)
        for s in scores:
            self.assertEqual(s.timeframe, "15m")

    def test_cache_name_shares_1m_with_simulate(self):
        """1분봉은 시뮬레이션과 같은 이름으로 저장해서 서로 재사용한다 (15분봉은 따로)"""
        def src(base, timeframe):  # screen.main 안의 규칙과 같은 식
            return base if timeframe == "1m" else f"{base}@{timeframe}"
        self.assertEqual(src("upbit", "1m"), "upbit")
        self.assertEqual(src("upbit", "15m"), "upbit@15m")

    def test_abnormal_goes_last(self):
        wave = ([-1.5] * 5 + [0.8] * 5) * 30
        bad = self._eval("BAD", [v - 20 for v in wave])  # 김프 -20% 근처: 입출금 막힘 같은 상황
        good = self._eval("GOOD", [0.0, 0.05] * 150)
        self.assertIn("이상김프", bad.flags)
        self.assertEqual([s.coin for s in rank([bad, good])], ["GOOD", "BAD"])

    def test_trend_flag(self):
        s = self._eval("UP", [i * 0.02 for i in range(300)])
        self.assertIn("추세↑", s.flags)




class RecentOpportunityTest(unittest.TestCase):
    """
    최근(검증) 구간에 기회가 없으면 점수를 깎는다 — 다만 0 으로 뭉개지 않는다

    예전에는 검증 수익이 0 이면 점수도 0 이 되어, '최근에 기회가 없던' 코인이 전부 0 점으로
    뭉쳐 서로 구분이 안 됐다 (월 23% 를 자리 7% 로 버는 코인과 월 4% 를 자리 58% 로 버는 코인이 같은 점수).
    지금은 전체 점수를 RECENT_FAIL_DISCOUNT(1/4) 로 깎아서 벌점은 세고 순위는 남긴다.
    """

    WAVE = [-1.5] * 5 + [0.8] * 5            # 기회가 있는 파도 (기존 테스트와 같은 모양)
    STUCK = [-1.5] * 5 + [-1.3] * 5          # 진입은 되지만 목표에 못 닿아 청산이 안 되는 구간
    QUIET = ([-0.45] * 2 + [-0.25] * 2) * 25  # 진입 기준에 아예 안 닿는 잔잔한 구간

    def _eval(self, coin, values, recent_days=0.0, stop=-5.0):
        return evaluate_coin(coin, series(values), BAR, days=len(values) * BAR / 86_400_000,
                             spread_percent=0.05, usdt_spread_percent=0.05, fixed_cost_percent=0.4,
                             target=1.0, stop=stop, funding=[], live=CoinLive(), size_usdt=1000,
                             recent_days=recent_days)

    def test_recent_empty_is_discounted_not_zeroed(self):
        """앞은 파도, 최근은 청산이 안 되는 구간 → 점수가 0 이 아니라 전체의 1/4 로 깎인다"""
        s = self._eval("HEAD", ([-1.5] * 2 + [0.8] * 2) * 50 + self.STUCK * 10)
        self.assertTrue(s.recent_empty)
        self.assertEqual(s.test.trades, 0)
        self.assertGreater(s.monthly, 0)
        self.assertAlmostEqual(s.base_score, s.monthly * RECENT_FAIL_DISCOUNT, places=6)
        self.assertGreater(s.score, 0)                 # 0 으로 뭉개지 않는다
        self.assertIn("최근기회없음", s.flags)

    def test_discount_keeps_ranking(self):
        """둘 다 최근 기회가 없어도, 빨리 왕복해서 더 번 코인이 위로 온다 (예전엔 둘 다 0 점)"""
        fast = self._eval("FAST", ([-1.5] * 2 + [0.8] * 2) * 50 + self.QUIET)
        slow = self._eval("SLOW", ([-1.5] * 10 + [0.8] * 10) * 10 + self.QUIET)
        self.assertTrue(fast.recent_empty and slow.recent_empty)
        self.assertGreater(fast.score, 0)
        self.assertGreater(slow.score, 0)
        self.assertGreater(fast.score, slow.score)
        self.assertEqual([x.coin for x in rank([slow, fast])], ["FAST", "SLOW"])

    def test_recent_trades_means_no_discount(self):
        """최근 구간에도 거래가 있으면 깎지 않고 min(전체, 검증) 그대로"""
        s = self._eval("BOTH", self.WAVE * 40)
        self.assertFalse(s.recent_empty)
        self.assertGreater(s.test.trades, 0)
        self.assertAlmostEqual(s.base_score, min(s.monthly, s.test_monthly), places=6)
        self.assertNotIn("최근기회없음", s.flags)

    def test_json_rows_carry_test_period_trades(self):
        """웹 추천 표: 전체 거래·승 과 별도로 검증 구간 거래·승 이 나간다"""
        import json, os, tempfile
        from kimp.screen import save_json
        s = self._eval("BOTH", self.WAVE * 40)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "screen.json")
            save_json([s], 5, "", "", path)
            row = json.load(open(path, encoding="utf-8"))["rows"][0]
        self.assertEqual((row["trades"], row["wins"]), (s.full.trades, s.full.wins))
        self.assertEqual((row["test_trades"], row["test_wins"]), (s.test.trades, s.test.wins))
        self.assertGreater(row["test_trades"], 0)
        self.assertLessEqual(row["test_trades"], row["trades"])

    def test_recent_loss_is_not_rescued_by_discount(self):
        """최근에 손절이 났으면 감점으로 구제하지 않는다 (음수가 그대로 깔린다)"""
        # 진입 기준에 닿은 뒤 -4% 로 떨어져 손절 (stop -2%) → 검증 구간 손실
        s = self._eval("LOSS", self.WAVE * 20 + ([-1.5] * 5 + [-4.0] * 15) * 5, stop=-2.0)
        self.assertFalse(s.recent_empty)
        self.assertLess(s.test_monthly, 0)
        self.assertAlmostEqual(s.base_score, min(s.monthly, s.test_monthly), places=6)
        self.assertLess(s.score, 0)
        self.assertNotIn("최근기회없음", s.flags)

    def test_recent_days_moves_the_cut(self):
        """--recent-days 를 주면 검증 구간 길이가 기간이 아니라 그 일수로 정해진다"""
        values = self.WAVE * 40                        # 400봉 × 15분 ≈ 4.17일
        default = self._eval("A", values)              # 뒤 1/3 ≈ 1.39일
        short = self._eval("B", values, recent_days=1.0)
        self.assertGreater(default.test_days, short.test_days)
        self.assertAlmostEqual(short.test_days, 1.0, delta=0.05)
        self.assertGreater(short.test.trades, 0)

    def test_recent_days_ignored_when_too_long(self):
        """기간보다 긴 --recent-days 는 무시한다 (기준을 고를 앞 구간이 없어지므로)"""
        s = self._eval("C", self.WAVE * 40, recent_days=999.0)
        self.assertGreater(s.test_days, 0)
        self.assertIsNotNone(s.full)
        self.assertIsNotNone(s.test)


if __name__ == "__main__":
    unittest.main()

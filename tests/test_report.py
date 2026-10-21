"""
분석 리포트 계산 테스트 (인터넷 필요 없음)

실행: python -m unittest discover -s tests -v
"""
import unittest

from kimp.report import Row, _percentile, summarize_coin


def row(entry: float, funding: float = 0.0001, tether: float = 0.5) -> Row:
    """테스트용 기록 한 줄 (가운데 김프 = 진입 김프 - 0.05, 청산 김프 = 진입 김프 - 0.1)"""
    return Row(coin="XRP", mid_premium=entry - 0.05, entry_premium=entry, exit_premium=entry - 0.1,
               tether_premium=tether, coin_premium=entry - tether, funding_rate=funding)


class ReportTest(unittest.TestCase):
    """리포트 숫자 계산"""

    def test_percentile(self):
        """[1,2,3,4,5] 의 하위 50% = 3, 하위 25% = 2"""
        self.assertEqual(_percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertEqual(_percentile([1, 2, 3, 4, 5], 25), 2)

    def test_threshold_time_ratio_and_funding(self):
        """10건 중 진입 김프 -0.5% 이하가 3건 → 30%, 그때 펀딩비율 평균은 그 3건 평균"""
        rows = [row(0.3)] * 7 + [row(-0.6, funding=-0.0002), row(-0.8, funding=-0.0004), row(-1.2, funding=0.0)]
        s = summarize_coin("XRP", rows, thresholds=(-0.5,))
        [t] = s.thresholds
        self.assertAlmostEqual(t.time_ratio, 0.3)
        self.assertAlmostEqual(t.avg_funding_rate, (-0.0002 - 0.0004 + 0.0) / 3)

    def test_buckets(self):
        """역프 구간별로 펀딩비율 평균이 따로 계산된다"""
        rows = [row(-1.5, funding=-0.0003), row(-0.7, funding=-0.0001), row(0.2, funding=0.0001)]
        s = summarize_coin("XRP", rows)
        by_label = {b.label: b for b in s.buckets}
        self.assertEqual(by_label["-1% 미만"].count, 1)
        self.assertAlmostEqual(by_label["-1% 미만"].avg_funding_rate, -0.0003)
        self.assertAlmostEqual(by_label["-1 ~ -0.5%"].avg_funding_rate, -0.0001)
        self.assertAlmostEqual(by_label["0 ~ +0.5%"].avg_funding_rate, 0.0001)

    def test_basic_stats(self):
        """평균 김프, 호가 간격 비용, 마이너스 펀딩 비율"""
        rows = [row(0.0, funding=0.0001), row(1.0, funding=-0.0001)]
        s = summarize_coin("XRP", rows)
        self.assertAlmostEqual(s.mid_avg, 0.45)
        self.assertAlmostEqual(s.spread_cost_avg, 0.1)
        self.assertAlmostEqual(s.funding_negative_ratio, 0.5)
        self.assertAlmostEqual(s.tether_avg, 0.5)


if __name__ == "__main__":
    unittest.main()

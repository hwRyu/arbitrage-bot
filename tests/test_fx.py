"""
달러 환율 공급기 테스트 (진짜 yfinance 는 안 부르고 가짜 함수로 확인)

실행: python -m unittest discover -s tests -v
"""
import unittest

from kimp.fx import FxProvider


class FxProviderTest(unittest.TestCase):
    """환율 받아오기 / 실패 / 오래된 값"""

    def test_fixed_rate(self):
        """고정 환율을 넣으면 조회 없이 그 값"""
        fx = FxProvider(fixed_rate=1325.0, fetcher=lambda: 9999.0)
        q = fx.get()
        self.assertEqual(q.rate, 1325.0)
        self.assertTrue(q.is_fixed)
        self.assertFalse(q.is_stale)

    def test_refresh_interval(self):
        """갱신 간격 전에는 다시 조회하지 않는다"""
        now = [0.0]
        calls = []

        def fetcher():
            calls.append(now[0])
            return 1325.0 + len(calls)

        fx = FxProvider(refresh_sec=60, fetcher=fetcher, clock=lambda: now[0])
        self.assertEqual(fx.get().rate, 1326.0)
        now[0] = 30
        self.assertEqual(fx.get().rate, 1326.0)  # 30초 → 그대로
        now[0] = 61
        self.assertEqual(fx.get().rate, 1327.0)  # 61초 → 새로 받음
        self.assertEqual(len(calls), 2)

    def test_failure_keeps_last_value_then_stale(self):
        """실패하면 이전 값을 쓰고, 너무 오래되면 is_stale"""
        now = [0.0]
        ok = [True]

        def fetcher():
            if not ok[0]:
                raise RuntimeError("실패")
            return 1325.0

        fx = FxProvider(refresh_sec=10, max_stale_sec=100, fetcher=fetcher, clock=lambda: now[0])
        self.assertEqual(fx.get().rate, 1325.0)
        ok[0] = False
        now[0] = 50
        q = fx.get()
        self.assertEqual(q.rate, 1325.0)  # 실패해도 이전 값
        self.assertFalse(q.is_stale)
        now[0] = 150
        self.assertTrue(fx.get().is_stale)  # 150초 > 100초 → 오래됨

    def test_none_before_first_success(self):
        """한 번도 성공 못 하면 None"""
        def fail():
            raise RuntimeError("실패")

        self.assertIsNone(FxProvider(fetcher=fail).get())


if __name__ == "__main__":
    unittest.main()

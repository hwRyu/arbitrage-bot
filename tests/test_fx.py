"""
달러 환율 공급기 테스트 (진짜 yfinance 는 안 부르고 가짜 함수로 확인)

환율은 표시용이라 (테더 프리미엄·사이트식 김프) 못 받아도 매매는 돌아야 한다.
"조회에 실패해도 봇이 안 멈춘다" 는 쪽이 여기서 제일 중요한 성질이다.

실행: python -m unittest discover -s tests -v
"""
import unittest

from kimp.fx import FxProvider


class FxProviderTest(unittest.TestCase):
    """환율 받아오기 / 갱신 간격 / 실패"""

    def test_fixed_rate(self):
        """고정 환율을 넣으면 조회 없이 그 값"""
        fx = FxProvider(fixed_rate=1325.0, fetcher=lambda: 9999.0)
        self.assertEqual(fx.get(), 1325.0)

    def test_refresh_interval(self):
        """갱신 간격 전에는 다시 조회하지 않는다"""
        now = [0.0]
        calls = []

        def fetcher():
            calls.append(now[0])
            return 1325.0 + len(calls)

        fx = FxProvider(refresh_sec=60, fetcher=fetcher, clock=lambda: now[0])
        self.assertEqual(fx.get(), 1326.0)
        now[0] = 30
        self.assertEqual(fx.get(), 1326.0)  # 30초 → 그대로
        now[0] = 61
        self.assertEqual(fx.get(), 1327.0)  # 61초 → 새로 받음
        self.assertEqual(len(calls), 2)

    def test_failure_keeps_last_value(self):
        """실패하면 마지막으로 성공한 값을 계속 쓴다 (실패가 이어져도 덮어쓰지 않는다)"""
        now = [0.0]
        ok = [True]

        def fetcher():
            if not ok[0]:
                raise RuntimeError("yfinance 가 빈 환율 데이터를 돌려줬습니다")
            return 1325.0

        fx = FxProvider(refresh_sec=10, fetcher=fetcher, clock=lambda: now[0])
        self.assertEqual(fx.get(), 1325.0)
        ok[0] = False
        for t in (50, 150, 5000):   # 아무리 오래 실패해도 마지막 성공값 그대로
            now[0] = t
            self.assertEqual(fx.get(), 1325.0)

    def test_none_before_first_success(self):
        """한 번도 성공 못 하면 None (봇 시작 직후 실패한 경우)"""
        def fail():
            raise RuntimeError("실패")

        self.assertIsNone(FxProvider(fetcher=fail).get())

    def test_recovers_after_failure(self):
        """처음에 실패해도 다음 갱신 때 성공하면 그때부터 값이 나온다"""
        now = [0.0]
        ok = [False]

        def fetcher():
            if not ok[0]:
                raise RuntimeError("실패")
            return 1330.0

        fx = FxProvider(refresh_sec=10, fetcher=fetcher, clock=lambda: now[0])
        self.assertIsNone(fx.get())
        ok[0] = True
        now[0] = 11
        self.assertEqual(fx.get(), 1330.0)


if __name__ == "__main__":
    unittest.main()

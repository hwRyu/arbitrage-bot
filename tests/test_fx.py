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

    def test_alert_only_after_consecutive_failures(self):
        """간헐적 실패는 조용히(DEBUG), 3번 연속이면 WARNING 한 번, 복구되면 한 번"""
        import logging
        from kimp.fx import ALERT_AFTER_FAILURES
        now = [0.0]
        ok = [True]

        def fetcher():
            if not ok[0]:
                raise RuntimeError("yfinance 가 빈 환율 데이터를 돌려줬습니다")
            return 1325.0

        fx = FxProvider(refresh_sec=1, fetcher=fetcher, clock=lambda: now[0])
        fx.get()
        ok[0] = False
        with self.assertLogs("kimp.fx", level=logging.DEBUG) as logs:
            for i in range(ALERT_AFTER_FAILURES - 1):      # 한두 번 → 조용히
                now[0] += 1
                fx.get()
        self.assertFalse(any(m.startswith("WARNING") for m in logs.output))
        with self.assertLogs("kimp.fx", level=logging.WARNING) as logs:
            now[0] += 1
            fx.get()                                        # 3번째 연속 → 알림
        self.assertIn("3번 연속 실패", logs.output[0])
        with self.assertNoLogs("kimp.fx", level=logging.WARNING):
            now[0] += 1
            fx.get()                                        # 4번째 → 다시 조용히
        ok[0] = True
        with self.assertLogs("kimp.fx", level=logging.WARNING) as logs:
            now[0] += 1
            self.assertEqual(fx.get(), 1325.0)              # 복구 → 한 번 알림
        self.assertIn("복구", logs.output[0])
        self.assertEqual(fx.failures, 0)

    def test_single_failure_then_recovery_is_quiet(self):
        """한 번 실패하고 바로 되면 아무 알림도 없다"""
        import logging
        now = [0.0]
        ok = [False]

        def fetcher():
            if not ok[0]:
                raise RuntimeError("실패")
            return 1330.0

        fx = FxProvider(refresh_sec=1, fetcher=fetcher, clock=lambda: now[0])
        with self.assertNoLogs("kimp.fx", level=logging.WARNING):
            fx.get()
            ok[0] = True
            now[0] += 1
            fx.get()

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

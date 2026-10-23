"""
시세 조회 실패 알림 억제 테스트 (네트워크 불필요 — 도우미 함수만 본다)

거래소 점검처럼 조회가 한참 안 되면 매 스캔(10초)마다 ERROR 가 찍히고 그게 전부 텔레그램으로 간다.
처음 한 번 알리고, 쿨다운 동안은 조용히, 복구되면 한 번 더 알리는지 확인한다.

실행: python -m unittest discover -s tests -v
"""
import logging
import unittest
from unittest import mock

from kimp import market_data as md


class RepeatLogTest(unittest.TestCase):
    def setUp(self):
        md._fail_since.clear()
        md._last_told.clear()

    def test_first_failure_is_told_then_quiet_until_cooldown(self):
        """같은 실패: 처음 1번 → 쿨다운 안에는 DEBUG → 쿨다운이 지나면 다시 1번"""
        clock = [1000.0]
        with mock.patch.object(md.time, "monotonic", lambda: clock[0]):
            with self.assertLogs("kimp.market_data", level=logging.DEBUG) as logs:
                md._log_repeat("k", logging.ERROR, "업비트 호가 조회 실패")
                clock[0] += 10
                md._log_repeat("k", logging.ERROR, "업비트 호가 조회 실패")
                clock[0] += 10
                md._log_repeat("k", logging.ERROR, "업비트 호가 조회 실패")
            errors = [m for m in logs.output if m.startswith("ERROR")]
            self.assertEqual(len(errors), 1)
            self.assertEqual(len(logs.output), 3)   # 나머지 둘은 DEBUG

            clock[0] += md.REPEAT_COOLDOWN_SEC
            with self.assertLogs("kimp.market_data", level=logging.ERROR):
                md._log_repeat("k", logging.ERROR, "업비트 호가 조회 실패")

    def test_recovery_is_announced_once_with_duration(self):
        """복구되면 '얼마나 막혔었는지' 한 번 알리고, 그 뒤 성공에는 조용하다"""
        clock = [0.0]
        with mock.patch.object(md.time, "monotonic", lambda: clock[0]):
            md._log_repeat("k", logging.ERROR, "실패")
            clock[0] = 125.0
            with self.assertLogs("kimp.market_data", level=logging.WARNING) as logs:
                md._log_recovered("k", "업비트 호가 조회")
            self.assertIn("125초", logs.output[0])
            with self.assertNoLogs("kimp.market_data", level=logging.DEBUG):
                md._log_recovered("k", "업비트 호가 조회")   # 실패한 적 없으면 조용

    def test_keys_are_independent(self):
        """코인별 키가 따로 논다 (CAP 이 막혀도 ALT 첫 실패는 바로 알린다)"""
        with self.assertLogs("kimp.market_data", level=logging.WARNING) as logs:
            md._log_repeat("bybit-CAP", logging.WARNING, "CAP 실패")
            md._log_repeat("bybit-ALT", logging.WARNING, "ALT 실패")
        self.assertEqual(len([m for m in logs.output if m.startswith("WARNING")]), 2)


if __name__ == "__main__":
    unittest.main()

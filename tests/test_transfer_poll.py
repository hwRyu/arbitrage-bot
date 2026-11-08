"""
송금 도착 확인 주기 (kimp/broker.py _poll_transfers, 2026-11-08)

소켓 시세로 0.25초마다 스캔할 때 매 스캔 바이비트 입금 내역을 부르면 요청 제한(10006)에 걸리고, 성공·실패가 번갈아
나며 실패 알림이 매번 나갔다 (송금 #10, 2분에 183통). 송금마다 TRANSFER_POLL_SEC 에 한 번만 묻고, 실패 알림은
끝날 때까지 한 번만 나가는지 본다.

실행: python -m unittest discover -s tests -v
"""
import logging
import unittest
from datetime import datetime, timedelta, timezone

from kimp.broker import TRANSFER_POLL_SEC, HedgeBroker
from kimp.config import FeeSettings
from kimp.db import Transfer, TransferStatus, make_session_factory


class FlakyExecutor:
    """입금 조회가 성공(아직 없음)·실패(요청 제한)를 번갈아 하는 거래소 흉내"""
    is_paper = False

    def __init__(self):
        self.calls = 0
        self.arrive_at_call = None   # 이 번째 호출에서 도착으로 답한다

    def transfer_arrived(self, session, t):
        self.calls += 1
        if self.arrive_at_call is not None and self.calls >= self.arrive_at_call:
            return True
        if self.calls % 2 == 0:
            raise RuntimeError('bybit {"retCode":10006,"retMsg":"Too many visits"}')
        return None

    def refresh(self):
        pass


class TransferPollTest(unittest.TestCase):
    def setUp(self):
        self.t = datetime(2026, 11, 8, 12, 5, tzinfo=timezone.utc)
        self.ex = FlakyExecutor()
        self.broker = HedgeBroker(self.ex, FeeSettings(), 0.3, now_fn=lambda: self.t)
        self.sf = make_session_factory(":memory:")
        with self.sf() as s:
            s.add(Transfer(direction="upbit->bybit", amount_usdt=161.92, fee_usdt=0.0, network="TRX",
                           status=TransferStatus.REQUESTED, requested_at=self.t))
            s.commit()

    def _scan(self, times: int, step_sec: float):
        with self.sf() as s:
            for _ in range(times):
                self.broker._poll_transfers(s)
                self.t += timedelta(seconds=step_sec)

    def test_polls_once_per_interval_not_every_scan(self):
        """0.25초 스캔 2분(480번)에 조회는 TRANSFER_POLL_SEC 마다 한 번씩만"""
        self._scan(480, 0.25)
        expected = int(120 / TRANSFER_POLL_SEC) + 1
        self.assertLessEqual(abs(self.ex.calls - expected), 1, (self.ex.calls, expected))

    def test_failure_alert_once_until_done(self):
        """성공(아직)·실패가 번갈아도 실패 경고는 한 번만. 도착하면 키가 풀려 다음 송금에선 다시 알린다"""
        with self.assertLogs("kimp.broker", level=logging.WARNING) as cm:
            self._scan(480, 0.25)
        fails = [ln for ln in cm.output if "송금 확인 실패" in ln]
        self.assertEqual(len(fails), 1, cm.output)
        self.ex.arrive_at_call = self.ex.calls + 1
        with self.assertLogs("kimp.broker", level=logging.WARNING) as cm:
            self._scan(int(TRANSFER_POLL_SEC / 0.25) + 2, 0.25)
        self.assertTrue(any("송금 도착" in ln for ln in cm.output), cm.output)
        with self.sf() as s:
            self.assertEqual(s.get(Transfer, 1).status, TransferStatus.CONFIRMED)
        self.assertNotIn("transfer-1", self.broker._last_repeat)
        self.assertNotIn(1, self.broker._last_transfer_poll)


if __name__ == "__main__":
    unittest.main()

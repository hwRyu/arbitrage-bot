"""
펀딩 지형 기록 (kimp/funding_watch.py) — 요약 계산, 하루 1회 기록, 재시작 뒤 중복 없음

실행: python -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest

from kimp.funding_watch import DailyFundingWatch, last_record_ms, show, snapshot


class FakeBybit:
    def __init__(self, rates):
        self._rates = rates   # {coin: (rate_fraction, interval_min)}

    def fetch_funding_rates(self):
        return {f"{c}/USDT:USDT": {"fundingRate": r} for c, (r, _i) in self._rates.items()} | {"BTC/USD:BTC": {"fundingRate": 0.0001}}

    def market(self, sym):
        coin = sym.split("/")[0]
        return {"info": {"fundingInterval": str(self._rates[coin][1])}}


class SnapshotTest(unittest.TestCase):
    def test_summary(self):
        """연환산 = 1회 × (1440/주기) × 365. 8시간 0.01% → 10.95%, 4시간 0.05% → 109.5%, 음수 포함 중앙값·상위"""
        ex = FakeBybit({"AAA": (0.0001, 480), "BBB": (0.0005, 240), "CCC": (-0.0002, 480), "DDD": (0.0, 480)})
        snap = snapshot(ex, now_ms=1_000)
        self.assertEqual(snap.coins, 4)                       # 인버스(BTC/USD)는 뺀다
        self.assertEqual(snap.positive, 2)
        self.assertEqual(snap.top[0]["coin"], "BBB")
        self.assertAlmostEqual(snap.top[0]["apr"], 0.05 * 6 * 365, places=6)
        self.assertEqual(snap.over20, 1)
        self.assertIn("중앙값", snap.line())


class DailyWatchTest(unittest.TestCase):
    def test_records_once_a_day_and_survives_restart(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = os.path.join(d, "m.db")
            calls = []

            def factory():
                calls.append(1)
                return FakeBybit({"AAA": (0.0001, 480)})

            w = DailyFundingWatch(db, factory, interval_sec=3600)
            self.assertIsNotNone(w.maybe_record(now_ms=10_000_000))       # 첫 기록
            self.assertIsNone(w.maybe_record(now_ms=10_000_000 + 1800_000))  # 30분 뒤: 아직
            self.assertIsNotNone(w.maybe_record(now_ms=10_000_000 + 3600_000))  # 1시간 뒤: 다시
            self.assertEqual(len(calls), 2)
            # 재시작: 마지막 기록 시각을 DB 에서 읽어 바로 또 적지 않는다
            w2 = DailyFundingWatch(db, factory, interval_sec=3600)
            self.assertIsNone(w2.maybe_record(now_ms=10_000_000 + 3600_000 + 60_000))
            self.assertEqual(last_record_ms(db), 10_000_000 + 3600_000)
            show(db)   # 출력만, 예외 없음

    def test_fetch_failure_does_not_raise(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            def boom():
                raise ConnectionError("down")
            w = DailyFundingWatch(os.path.join(d, "m.db"), boom, interval_sec=3600)
            self.assertIsNone(w.maybe_record(now_ms=5_000_000))
            self.assertIsNone(last_record_ms(os.path.join(d, "m.db")))


if __name__ == "__main__":
    unittest.main()

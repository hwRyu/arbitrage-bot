"""
캔들 정리(prune) 테스트 — 네트워크 불필요

핵심: 캔들을 지우면 '받아본 구간' 기록도 손봐야 다음에 다시 받는다.
시뮬레이션 결과(sim_runs/sim_trades)는 절대 지우지 않는다.
"""
import unittest

from kimp.history import (Candle, FetchedRange, SimRun, SimTrade, candle_stats, make_history_session_factory,
                          prune_candles)

MIN = 60_000
DAY = 86_400_000


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.sf = make_history_session_factory(":memory:")
        with self.sf() as s:
            for source in ("upbit", "upbit@5m"):
                for coin in ("AAA", "BBB"):
                    for i in range(10):  # 0일, 1일, ... 9일 전
                        s.add(Candle(source=source, coin=coin, ts_ms=i * DAY, open=1, high=1, low=1,
                                     close=1, volume=0))
            # 받아본 구간: 0 ~ 9일
            for kind in ("candle:upbit", "candle:upbit@5m"):
                for coin in ("AAA", "BBB"):
                    s.add(FetchedRange(kind=kind, coin=coin, start_ms=0, end_ms=9 * DAY))
            s.add(SimRun(created_at="2026-10-22", from_ms=0, to_ms=DAY, coins="AAA",
                         settings_json="{}", summary_json="{}"))
            s.add(SimTrade(run_id=1, coin="AAA", opened_at="2026-10-22", entry_premium=-0.5,
                           exit_reason="TAKE_PROFIT", net_pnl_usdt=1.0, net_pnl_percent=1.0,
                           funding_usdt=0.0, cost_usdt=0.1))
            s.commit()

    def _count(self, **where):
        from sqlalchemy import func, select
        with self.sf() as s:
            q = select(func.count()).select_from(Candle)
            for k, v in where.items():
                q = q.where(getattr(Candle, k) == v)
            return s.scalar(q)

    def test_prune_by_source(self):
        """간격 끝부분('@5m')만 적어도 그 간격 전부가 지워진다"""
        out = prune_candles(self.sf, sources=["@5m"])
        self.assertEqual(out["candles"], 20)          # 5분봉 2코인 × 10개
        self.assertEqual(out["sources"], ["upbit@5m"])
        self.assertEqual(self._count(source="upbit@5m"), 0)
        self.assertEqual(self._count(source="upbit"), 20)  # 1분봉은 그대로

    def test_prune_by_days_clips_fetched_range(self):
        """오래된 쪽만 지우면 '받아본 구간' 의 시작을 잘라서 다음에 다시 받게 한다"""
        cutoff = 5 * DAY
        prune_candles(self.sf, before_ms=cutoff, sources=["upbit"])
        with self.sf() as s:
            from sqlalchemy import select
            rows = s.scalars(select(FetchedRange).where(FetchedRange.kind == "candle:upbit")).all()
            self.assertTrue(all(r.start_ms == cutoff for r in rows), [r.start_ms for r in rows])
        self.assertEqual(self._count(source="upbit"), 10)  # 5~9일치만 남음

    def test_prune_by_coin(self):
        out = prune_candles(self.sf, coins=["AAA"])
        self.assertEqual(out["candles"], 20)   # AAA 의 두 간격
        self.assertEqual(self._count(coin="AAA"), 0)
        self.assertEqual(self._count(coin="BBB"), 20)

    def test_simulation_results_untouched(self):
        """캔들을 다 지워도 시뮬레이션 결과는 남는다 (되살릴 수 없는 기록)"""
        prune_candles(self.sf)
        from sqlalchemy import func, select
        with self.sf() as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(SimRun)), 1)
            self.assertEqual(s.scalar(select(func.count()).select_from(SimTrade)), 1)

    def test_unknown_source_deletes_nothing(self):
        out = prune_candles(self.sf, sources=["@1h"])
        self.assertEqual((out["candles"], out["sources"]), (0, []))
        self.assertEqual(self._count(), 40)

    def test_stats(self):
        stats = {s["source"]: s for s in candle_stats(self.sf)}
        self.assertEqual(stats["upbit"]["rows"], 20)
        self.assertEqual(stats["upbit"]["coins"], 2)
        self.assertEqual(stats["upbit"]["from_ms"], 0)
        self.assertEqual(stats["upbit"]["to_ms"], 9 * DAY)


if __name__ == "__main__":
    unittest.main()

"""
호가 간격 정하기 테스트 (네트워크 불필요)

실행: python -m unittest discover -s tests -v
"""
import os
import unittest
from unittest import mock

from kimp.db import PremiumSnapshot, make_session_factory
from kimp.spread import (MIN_SNAPSHOTS_FOR_SPREAD, MODE_COMMON, MODE_LIVE, MODE_RECORDS, MODES,
                         SOURCE_ENV, is_rough, median_spread_depth, resolve_spreads,
                         snapshot_spreads)


class FakeBooks:
    """호가창을 정해진 순서대로 돌려주는 가짜 거래소 (조회할 때마다 다음 값)"""

    def __init__(self, upbit_spreads, bybit_spread=0.0):
        self.upbit_spreads = list(upbit_spreads)
        self.bybit_spread = bybit_spread
        self.calls = 0

    class _Ex:
        def __init__(self, outer, which):
            self.outer, self.which = outer, which
            self.id = which

        def fetch_order_books(self, symbols, limit=15):
            half = self.outer.upbit_spreads[min(self.outer.calls, len(self.outer.upbit_spreads) - 1)] / 2 / 100
            self.outer.calls += 1
            return {s: {"bids": [(100 * (1 - half), 10_000)], "asks": [(100 * (1 + half), 10_000)]} for s in symbols}

        def fetch_order_book(self, symbol, limit=50):
            half = self.outer.bybit_spread / 2 / 100
            return {"bids": [(1 * (1 - half), 10_000)], "asks": [(1 * (1 + half), 10_000)]}

    @property
    def upbit(self):
        return self._Ex(self, "upbit")

    @property
    def bybit(self):
        return self._Ex(self, "bybit")


class MedianSpreadTest(unittest.TestCase):
    def test_median_of_three_samples(self):
        """
        한 번만 재면 그 순간 값에 휘둘린다 → 3번 재서 중간값

        예) 0.11% / 0.35% / 0.33% 로 재졌으면 중간값 0.33%
        """
        md = FakeBooks([0.11, 0.35, 0.33])
        out = median_spread_depth(md, ["SEI"], usdt_krw=1_350.0, samples=3, pause_sec=0)
        self.assertAlmostEqual(out["SEI"][0], 0.33, places=6)

    def test_single_sample_still_works(self):
        md = FakeBooks([0.2])
        out = median_spread_depth(md, ["CAP"], usdt_krw=1_350.0, samples=1, pause_sec=0)
        self.assertAlmostEqual(out["CAP"][0], 0.2, places=6)


class ResolveSpreadTest(unittest.TestCase):
    """순서: 기록 평균 → 코인별 설정 → 지금 호가 중간값 → 기본값"""

    def test_priority_without_records(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            spreads, sources = resolve_spreads(["CAP", "SEI"], db_path="없는파일.db", default_percent=0.1,
                                               live={"CAP": 0.35, "SEI": None})
        self.assertAlmostEqual(spreads["CAP"], 0.35)
        self.assertIn("지금 호가", sources["CAP"])
        self.assertAlmostEqual(spreads["SEI"], 0.1)  # 라이브 값이 없으면 기본값
        self.assertEqual(sources["SEI"], "기본 설정")

    def test_env_beats_live(self):
        with mock.patch.dict(os.environ, {"CAP_SIM_SPREAD_PERCENT": "0.5"}, clear=True):
            spreads, sources = resolve_spreads(["CAP"], db_path="없는파일.db", default_percent=0.1,
                                               live={"CAP": 0.35})
        self.assertAlmostEqual(spreads["CAP"], 0.5)
        self.assertEqual(sources["CAP"], "코인별 설정")

    def test_records_beat_everything(self):
        """기록이 기준 건수 이상이면 설정·라이브보다 우선"""
        path = "data/_test_spread.db"
        os.makedirs("data", exist_ok=True)
        if os.path.exists(path):
            os.remove(path)
        sf = make_session_factory(path)
        with sf() as session:
            for _ in range(MIN_SNAPSHOTS_FOR_SPREAD):
                # 진입 0.2 − 청산 −0.1 − USDT 0.075 = 0.225%
                session.add(PremiumSnapshot(coin="CAP", mid_premium=0.0, entry_premium_top=0.2,
                                            exit_premium_top=-0.1, usdt_krw=1_350.0, usdt_spread_percent=0.075,
                                            upbit_mid_krw=100.0, bybit_mid_usdt=1.0))
            session.commit()
        try:
            got = snapshot_spreads(path, ["CAP"])
            self.assertEqual(got["CAP"][1], MIN_SNAPSHOTS_FOR_SPREAD)
            self.assertAlmostEqual(got["CAP"][0], 0.225, places=6)
            # 코인별 설정이 없으면 기록이 이긴다 (지금 호가 0.35 보다 기록 0.225 가 먼저)
            with mock.patch.dict(os.environ, {}, clear=True):
                spreads, sources = resolve_spreads(["CAP"], db_path=path, default_percent=0.1, live={"CAP": 0.35})
            self.assertAlmostEqual(spreads["CAP"], 0.225, places=6)
            self.assertIn("기록", sources["CAP"])

            # 코인별 설정이 있으면 기록보다 먼저다 (사람이 적은 값이 조용히 무시되면 안 된다)
            with mock.patch.dict(os.environ, {"CAP_SIM_SPREAD_PERCENT": "0.5"}, clear=True):
                spreads, sources = resolve_spreads(["CAP"], db_path=path, default_percent=0.1, live={"CAP": 0.35})
            self.assertAlmostEqual(spreads["CAP"], 0.5, places=6)
            self.assertEqual(sources["CAP"], SOURCE_ENV)

            # 모드를 바꿔도 코인별 설정은 그대로 (common 으로 통일해도 이 코인만 예외)
            for mode in MODES:
                with mock.patch.dict(os.environ, {"CAP_SIM_SPREAD_PERCENT": "0.5"}, clear=True):
                    sp, sr = resolve_spreads(["CAP"], db_path=path, default_percent=0.1,
                                             live={"CAP": 0.35}, mode=mode)
                self.assertAlmostEqual(sp["CAP"], 0.5, places=6, msg=mode)
                self.assertEqual(sr["CAP"], SOURCE_ENV, msg=mode)

            # 모드별로 어디서 가져오는지
            with mock.patch.dict(os.environ, {}, clear=True):
                sp, sr = resolve_spreads(["CAP"], db_path=path, default_percent=0.1,
                                         live={"CAP": 0.35}, mode=MODE_COMMON)
                self.assertAlmostEqual(sp["CAP"], 0.1)          # 공통값으로 통일
                sp, sr = resolve_spreads(["CAP"], db_path=path, default_percent=0.1,
                                         live={"CAP": 0.35}, mode=MODE_LIVE)
                self.assertAlmostEqual(sp["CAP"], 0.35)         # 기록이 있어도 지금 호가
                sp, sr = resolve_spreads(["CAP"], db_path=path, default_percent=0.1,
                                         live={"CAP": 0.35}, mode=MODE_RECORDS)
                self.assertAlmostEqual(sp["CAP"], 0.225)        # 기록만
        finally:
            try:  # 윈도우는 SQLAlchemy 연결이 남아 있으면 못 지운다 → 남아도 테스트에 지장 없음
                os.remove(path)
            except OSError:
                pass


class SpreadModeTest(unittest.TestCase):
    """호가 간격을 어디서 가져올지 (auto / records / common / live) — 기록이 없는 코인 기준"""

    def _resolve(self, mode, live=None, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=True):
            return resolve_spreads(["ZZZ"], db_path="없는파일.db", default_percent=0.1,
                                   live=live, mode=mode)

    def test_common_ignores_live(self):
        """공통값 통일: 지금 호가가 있어도 공통값을 쓴다 (호가 차이를 지우고 비교할 때)"""
        sp, sr = self._resolve(MODE_COMMON, live={"ZZZ": 0.9})
        self.assertAlmostEqual(sp["ZZZ"], 0.1)
        self.assertEqual(sr["ZZZ"], "기본 설정")

    def test_records_mode_flags_missing(self):
        """실측 기록만: 기록이 모자라면 '기록 부족' 으로 표시하고 시뮬레이션이 멈추게 한다"""
        sp, sr = self._resolve(MODE_RECORDS, live={"ZZZ": 0.9})
        self.assertAlmostEqual(sp["ZZZ"], 0.1)          # 값은 공통값으로 때우되
        self.assertIn("기록 부족", sr["ZZZ"])            # 출처는 부족하다고 알린다
        self.assertIn("0/30", sr["ZZZ"])
        self.assertTrue(is_rough(sr["ZZZ"]))            # → 시뮬레이션이 거부한다

    def test_live_mode_uses_live(self):
        sp, sr = self._resolve(MODE_LIVE, live={"ZZZ": 0.9})
        self.assertAlmostEqual(sp["ZZZ"], 0.9)

    def test_live_mode_falls_back_without_live(self):
        """지금 호가를 못 받았으면 공통값으로 (화면에서 조회 없이 불러올 때)"""
        sp, sr = self._resolve(MODE_LIVE, live=None)
        self.assertAlmostEqual(sp["ZZZ"], 0.1)

    def test_unknown_mode_falls_back_to_auto(self):
        """모르는 모드는 auto 로 본다 (화면에서 엉뚱한 값이 와도 죽지 않게)"""
        sp, sr = self._resolve("그런모드없음", live={"ZZZ": 0.9})
        self.assertAlmostEqual(sp["ZZZ"], 0.9)          # auto = 기록 없음 → 지금 호가

    def test_env_wins_in_every_mode(self):
        """코인별 설정은 모든 모드에서 1순위 (표에 '강제' 로 표시된다)"""
        for mode in MODES:
            sp, sr = self._resolve(mode, live={"ZZZ": 0.9}, env={"ZZZ_SIM_SPREAD_PERCENT": "0.42"})
            self.assertAlmostEqual(sp["ZZZ"], 0.42, msg=mode)
            self.assertEqual(sr["ZZZ"], SOURCE_ENV, msg=mode)

    def test_forced_coins_lists_and_skips_bad_values(self):
        """강제된 코인 목록. 숫자가 아닌 값은 무시한다 (경고만)"""
        from kimp.spread import forced_coins
        with mock.patch.dict(os.environ, {"AAA_SIM_SPREAD_PERCENT": "0.3",
                                          "BBB_SIM_SPREAD_PERCENT": "저렴함",
                                          "CCC_SIM_SPREAD_PERCENT": "  "}, clear=True):
            got = forced_coins(["AAA", "BBB", "CCC", "DDD"])
        self.assertEqual(got, {"AAA": 0.3})


class RoughSpreadTest(unittest.TestCase):
    """기록이 없으면 '대충 값' 으로 보고, 시뮬레이션은 멈춘다"""

    def test_is_rough(self):
        from kimp.spread import SOURCE_DEFAULT, SOURCE_ENV, SOURCE_LIVE, is_rough
        self.assertTrue(is_rough(SOURCE_LIVE))      # 지금 호가 → 실행마다 바뀜
        self.assertTrue(is_rough(SOURCE_DEFAULT))   # 기본값 0.1% → 과거와 무관
        self.assertFalse(is_rough(SOURCE_ENV))      # 사람이 정한 값
        self.assertFalse(is_rough("기록 1,440건"))   # 봇이 쌓은 실측

    def test_hint_has_command_and_coins(self):
        from kimp.spread import accumulate_hint
        text = accumulate_hint(["CAP", "SEI"])
        self.assertIn("CAP,SEI", text)
        self.assertIn("kimp.collect", text)   # 봇이 아니라 수집기로 안내한다
        self.assertIn("--rough-spread", text)

    def test_simulate_decide_spreads_marks_rough(self):
        """시뮬레이션의 호가 결정도 같은 규칙 (md 없고 기록 없으면 기본값 → 대충)"""
        from dataclasses import replace

        from kimp.config import Settings
        from kimp.simulate import decide_spreads
        from kimp.spread import is_rough
        # snapshot_db_path 까지 비켜야 한다 — 기본값 data/market.db 에 기록이 쌓이면 이 테스트가 깨진다
        settings = replace(Settings(coins=["CAP"], db_path="없는파일.db",
                                    snapshot_db_path="없는수집파일.db"), sim_default_spread_percent=0.1)
        with mock.patch.dict(os.environ, {}, clear=True):
            spreads, sources = decide_spreads(settings, ["CAP"])
        self.assertAlmostEqual(spreads["CAP"], 0.1)
        self.assertTrue(is_rough(sources["CAP"]))


if __name__ == "__main__":
    unittest.main()

"""
로컬 화면(webui) 데이터 함수 테스트 (네트워크 불필요)

서버가 아니라 '화면에 줄 데이터를 만드는 함수' 를 확인한다.
실행: python -m unittest discover -s tests -v
"""
import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from kimp.config import Settings
from kimp.db import PremiumSnapshot, make_session_factory
from kimp.webui import config_summary, premium_history, screen_latest, sim_runs, spread_summary

TMP_DB = "data/_test_webui.db"


class ConfigSummaryTest(unittest.TestCase):
    def test_no_secrets_and_has_basics(self):
        """설정 요약에 API 키 값은 들어가지 않고, 있는지 여부만 들어간다"""
        s = Settings(coins=["CAP", "SEI"], upbit_access_key="비밀", upbit_secret_key="비밀",
                     bybit_api_key="비밀", bybit_api_secret="비밀", telegram_bot_token="비밀")
        out = config_summary(s)
        text = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("비밀", text)
        self.assertTrue(out["keys_present"])
        self.assertTrue(out["telegram"])
        self.assertEqual(out["coins"], ["CAP", "SEI"])
        self.assertEqual(out["mode"], "paper")
        # 방향별 송금 수수료가 그대로 보인다 (업비트 출금 무료 / 바이비트 1.3)
        self.assertAlmostEqual(out["fees"]["transfer_to_bybit"], 0.0)
        self.assertAlmostEqual(out["fees"]["transfer_to_upbit"], 1.3)

    def test_per_coin_values(self):
        """코인별 진입 기준·목표·손절이 코인마다 나온다"""
        s = Settings(coins=["CAP"], entry_premium_threshold_percent=-0.5, target_net_profit_percent=1.0,
                     stop_loss_percent=-5.0)
        row = config_summary(s)["per_coin"][0]
        self.assertEqual((row["coin"], row["entry"], row["target"], row["stop"]), ("CAP", -0.5, 1.0, -5.0))


class SnapshotBackedTest(unittest.TestCase):
    """봇이 쌓은 기록으로 호가 간격·김프 차트 데이터를 만든다"""

    def setUp(self):
        # 윈도우는 SQLAlchemy 연결이 남아 있으면 파일을 못 지운다 → 테스트마다 다른 파일 이름을 쓴다
        self.db = TMP_DB.replace(".db", f"_{self.id().rsplit('.', 1)[-1]}.db")
        try:
            os.makedirs("data", exist_ok=True)
            os.remove(self.db)
        except OSError:
            pass
        with make_session_factory(self.db)() as session:
            for _ in range(40):  # 30건을 넘겨서 '기록' 으로 인정되게
                session.add(PremiumSnapshot(coin="CAP", mid_premium=-0.05, entry_premium_top=0.16,
                                            exit_premium_top=-0.26, usdt_krw=1_350.0, usdt_spread_percent=0.075,
                                            upbit_mid_krw=91.0, bybit_mid_usdt=0.068))
            session.commit()
        # history_db_path 를 없는 파일로 둬서 실제 data/history.db 의 1분봉이 섞이지 않게 한다
        self.settings = replace(Settings(coins=["CAP"]), db_path=self.db, history_db_path="없는파일_hist.db",
                                snapshot_db_path="없는파일_market.db")

    def tearDown(self):
        try:
            os.remove(self.db)
        except OSError:  # 연결이 남아 있으면 다음 실행에서 지워진다 (테스트에 지장 없음)
            pass

    def test_spread_summary_uses_records(self):
        """진입 김프 − 청산 김프 − USDT 호가 = 0.16 + 0.26 − 0.075 = 0.345%"""
        row = spread_summary(self.settings, ["CAP"])[0]
        self.assertAlmostEqual(row["percent"], 0.345, places=6)
        self.assertIn("기록", row["source"])
        self.assertEqual(row["records"], 40)

    def test_candles_fill_window_and_records_overlay(self):
        """
        봇 기록만 쓰면 봇을 17분 돌렸을 때 7일을 요청해도 17분치만 나온다
        → 1분봉으로 기간을 채우고, 봇 기록이 있는 분만 그 값으로 덮어쓴다
        """
        from kimp.history import BYBIT, UPBIT, UPBIT_USDT, Candle, make_history_session_factory
        hist = self.db.replace(".db", "_hist.db")
        try:
            os.remove(hist)
        except OSError:
            pass
        now = int(datetime.now(timezone.utc).timestamp() * 1000) // 60_000 * 60_000
        with make_history_session_factory(hist)() as session:
            for i in range(120):  # 2시간치 1분봉
                ts = now - i * 60_000
                session.add(Candle(source=UPBIT, coin="CAP", ts_ms=ts, open=91, high=91, low=91,
                                   close=91.0, volume=0))
                session.add(Candle(source=BYBIT, coin="CAP", ts_ms=ts, open=0.068, high=0.068, low=0.068,
                                   close=0.068, volume=0))
                session.add(Candle(source=UPBIT_USDT, coin="USDT", ts_ms=ts, open=1340, high=1340, low=1340,
                                   close=1340.0, volume=0))
            session.commit()
        settings = replace(self.settings, history_db_path=hist)
        out = premium_history(settings, "CAP", hours=2)
        try:
            os.remove(hist)
        except OSError:
            pass
        self.assertGreaterEqual(len(out["series"]), 100)       # 1분봉으로 기간이 채워졌다
        self.assertEqual(out["candles"], 120)
        self.assertEqual(out["snapshots"], 40)                 # setUp 에서 넣은 봇 기록
        self.assertIn("1분봉", out["source"])
        self.assertIn("봇 기록", out["source"])
        # 봇 기록이 있는 분에는 진입·청산 김프가 들어 있고, 나머지 분은 비어 있다
        withe = [r for r in out["series"] if r["entry"] is not None]
        self.assertTrue(0 < len(withe) <= 40)
        self.assertTrue(any(r["entry"] is None for r in out["series"]))

    def test_premium_history_from_records(self):
        """
        차트 데이터는 지표마다 칸이 있다 (화면에서 골라 그린다)

        봇 기록은 1분 칸에 맞춰 넣기 때문에, 같은 분에 들어온 기록은 한 점으로 합쳐진다.
        """
        out = premium_history(self.settings, "CAP", hours=24)
        self.assertIn("봇 기록", out["source"])
        self.assertEqual(out["snapshots"], 40)
        self.assertGreaterEqual(len(out["series"]), 1)
        row = out["series"][0]
        self.assertGreater(row["ts"], 0)
        self.assertAlmostEqual(row["mid"], -0.05)
        self.assertAlmostEqual(row["entry"], 0.16)
        self.assertAlmostEqual(row["exit"], -0.26)
        # 코인 호가 간격 = 진입 − 청산 − USDT 호가 = 0.16 + 0.26 − 0.075
        self.assertAlmostEqual(row["spread"], 0.345, places=6)
        self.assertAlmostEqual(row["usdt_krw"], 1_350.0)

    def test_premium_history_window(self):
        """요청한 시간 범위보다 오래된 기록은 빼고 준다"""
        out = premium_history(self.settings, "CAP", hours=24, now_ms=10 ** 13)  # 아주 먼 미래
        self.assertEqual(out["series"], [])


class MissingFilesTest(unittest.TestCase):
    """파일이 없을 때도 터지지 않고 빈 값을 준다"""

    def test_no_db_no_json(self):
        s = replace(Settings(coins=["CAP"]), db_path="없는파일.db", history_db_path="없는파일2.db",
                    snapshot_db_path="없는파일3.db")
        self.assertEqual(sim_runs(s), [])
        self.assertEqual(premium_history(s, "CAP")["series"], [])
        self.assertIsNone(screen_latest("없는파일3.json"))


if __name__ == "__main__":
    unittest.main()


class BuildCommandTest(unittest.TestCase):
    """화면에서 받은 값 → 터미널에 치는 명령 (허용한 값만 넘어간다)"""

    def test_screen(self):
        from kimp.webui import build_command
        cmd, env = build_command("screen", {"days": "30", "coins": "CAP,SEI", "refine_top": "20", "몰래": "rm"})
        self.assertEqual(cmd[1:], ["-m", "kimp.screen", "--days", "30", "--coins", "CAP,SEI", "--refine-top", "20"])
        self.assertEqual(env, {})

    def test_sim_env_and_flags(self):
        from kimp.webui import build_command
        cmd, env = build_command("sim", {"start": "2026-09-22", "end": "2026-10-22", "coins": "CAP",
                                         "seed": "1500", "size": "50", "rough": "1",
                                         "thresholds": "CAP=-0.55, SEI=-0.45"})
        self.assertIn("--rough-spread", cmd)
        self.assertEqual(env["PAPER_INITIAL_TOTAL_USDT"], "1500")
        self.assertEqual(env["POSITION_SIZE_USDT"], "50")
        self.assertEqual(env["COINS"], "CAP")
        self.assertEqual(env["CAP_ENTRY_PREMIUM_THRESHOLD_PERCENT"], "-0.55")
        self.assertEqual(env["SEI_ENTRY_PREMIUM_THRESHOLD_PERCENT"], "-0.45")

    def test_bot_is_always_paper(self):
        """화면에서 켜는 봇은 반드시 페이퍼 (실거래를 켤 수 없다)"""
        from kimp.webui import build_command
        cmd, env = build_command("bot", {"coins": "CAP", "scan": "20", "quiet": "1",
                                         "live": "1", "PAPER_TRADING": "false"})
        self.assertEqual(cmd[1:], ["main.py"])
        self.assertEqual(env["PAPER_TRADING"], "true")
        self.assertEqual(env["SCAN_INTERVAL_SEC"], "20")
        self.assertEqual(env["CARRY_ENABLED"], "false")
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], "")

    def test_unknown_kind(self):
        from kimp.webui import build_command
        with self.assertRaises(ValueError):
            build_command("아무거나", {})


class FundingConvertTest(unittest.TestCase):
    def test_8h_conversion(self):
        """정산 주기가 달라도 8시간 기준으로 맞춰서 비교한다"""
        from kimp.webui import _funding_8h_percent
        self.assertAlmostEqual(_funding_8h_percent(0.0001, 8), 0.01)     # 8시간마다 0.01%
        self.assertAlmostEqual(_funding_8h_percent(0.0001, 1), 0.08)     # 1시간마다 → 8배
        self.assertAlmostEqual(_funding_8h_percent(0.0001, 4), 0.02)
        self.assertIsNone(_funding_8h_percent(None, 8))


class UniverseScopeTest(unittest.TestCase):
    """전체 코인 보기: 양쪽 거래소에 다 있는 코인만, 거래대금 큰 순"""

    class FakeMd:
        """업비트·바이비트 티커를 흉내내는 가짜 (네트워크 없음)"""

        class _Ex:
            def __init__(self, tickers, markets):
                self.markets = markets
                self._t = tickers

            def fetch_tickers(self, symbols=None):
                return {s: v for s, v in self._t.items() if symbols is None or s in symbols}

        def __init__(self):
            up_markets = {f"{c}/KRW": {"base": c, "quote": "KRW", "active": True} for c in ("AAA", "BBB", "CCC")}
            by_markets = {"AAA/USDT:USDT": {}, "BBB/USDT:USDT": {}}  # CCC 는 바이비트에 없다
            self.upbit = self._Ex({"AAA/KRW": {"quoteVolume": 100e8}, "BBB/KRW": {"quoteVolume": 300e8},
                                   "CCC/KRW": {"quoteVolume": 900e8}}, up_markets)
            self.bybit = self._Ex({"AAA/USDT:USDT": {"quoteVolume": 1e6}, "BBB/USDT:USDT": {"quoteVolume": 5e6}},
                                  by_markets)

    def test_only_both_exchanges_sorted_by_volume(self):
        from kimp.webui import universe
        u = universe(self.FakeMd(), force=True)
        self.assertEqual(u["coins"], ["BBB", "AAA"])  # CCC 제외(바이비트 없음), 거래대금 큰 순
        self.assertAlmostEqual(u["volume"]["BBB"]["upbit_krw"], 300e8)
        self.assertAlmostEqual(u["volume"]["AAA"]["bybit_usdt"], 1e6)


class AutoFetchLimitTest(unittest.TestCase):
    """긴 기간은 자동으로 캔들을 받지 않는다 (DB 가 커지므로 사용자가 직접 누를 때만)"""

    class FakeMd:
        """받기를 시도하면 기록만 남기는 가짜 (실제로는 안 받음)"""

        def __init__(self):
            self.tried = 0

    def _settings(self):
        return replace(Settings(coins=["CAP"]), db_path="없는파일.db", history_db_path="없는파일2.db",
                       snapshot_db_path="없는파일3.db")

    def test_short_window_tries_fetch(self):
        import kimp.webui as w
        calls = []
        orig, w._ensure_candles = w._ensure_candles, lambda *a: calls.append(a)
        try:
            out = w.premium_history(self._settings(), "CAP", hours=24, md=self.FakeMd())
        finally:
            w._ensure_candles = orig
        self.assertEqual(len(calls), 1)            # 24시간은 자동으로 받는다
        self.assertFalse(out.get("fetch_skipped"))

    def test_long_window_skips_and_hints(self):
        import kimp.webui as w
        calls = []
        orig, w._ensure_candles = w._ensure_candles, lambda *a: calls.append(a)
        try:
            out = w.premium_history(self._settings(), "CAP", hours=720, md=self.FakeMd())
        finally:
            w._ensure_candles = orig
        self.assertEqual(calls, [])                 # 30일은 자동으로 받지 않는다
        self.assertTrue(out["fetch_skipped"])
        self.assertIn("지금 받아오기", out["hint"])

    def test_force_fetches_anyway(self):
        import kimp.webui as w
        calls = []
        orig, w._ensure_candles = w._ensure_candles, lambda *a: calls.append(a)
        try:
            w.premium_history(self._settings(), "CAP", hours=720, md=self.FakeMd(), force_fetch=True)
        finally:
            w._ensure_candles = orig
        self.assertEqual(len(calls), 1)             # 직접 누르면 받는다


class DataJobTest(unittest.TestCase):
    """데이터 탭: 캔들 받아오기 / 정리 명령"""

    def test_fetch(self):
        from kimp.webui import build_command
        cmd, env = build_command("fetch", {"coins": "CAP,SEI", "days": "30", "timeframe": "1m"})
        self.assertEqual(cmd[1:], ["-m", "kimp.history", "fetch", "--coins", "CAP,SEI",
                                   "--days", "30", "--timeframe", "1m"])
        self.assertEqual(env, {})

    def test_prune_needs_confirm(self):
        """지우기는 '정말 지웁니다' 를 체크(confirm)해야만 명령이 만들어진다"""
        from kimp.webui import build_command
        with self.assertRaises(ValueError):
            build_command("prune", {"source": "@5m"})
        cmd, _ = build_command("prune", {"source": "@5m", "days": "60", "confirm": "1"})
        self.assertEqual(cmd[1:], ["-m", "kimp.history", "prune", "--days", "60", "--source", "@5m", "--yes"])

    def test_candle_summary_without_db(self):
        from kimp.webui import candle_summary
        out = candle_summary(replace(Settings(coins=["CAP"]), history_db_path="없는파일.db"))
        self.assertEqual((out["rows"], out["sources"]), (0, []))


class AvailableKeysTest(unittest.TestCase):
    """차트에 그릴 수 있는 지표 골라내기 (없는 지표를 흐리게 보여주려고)"""

    def test_only_keys_with_values(self):
        from kimp.webui import _available_keys
        # 1분봉으로 만든 기록: 호가가 없어서 진입·청산 김프가 없다
        series = [{"ts": 1, "mid": -0.1, "entry": None, "exit": None, "spread": None,
                   "usdt_krw": 1350.0, "funding": None}]
        self.assertEqual(_available_keys(series), ["mid", "usdt_krw"])

    def test_bot_records_have_everything(self):
        from kimp.webui import _available_keys
        series = [{"ts": 1, "mid": -0.1, "entry": 0.1, "exit": -0.3, "spread": 0.3,
                   "usdt_spread": 0.07, "funding": 0.01, "tether": 0.8, "site": 0.7,
                   "usdt_krw": 1350.0, "upbit_krw": 91.0, "bybit_usdt": 0.068}]
        got = _available_keys(series)
        for k in ("mid", "entry", "exit", "spread", "funding", "tether", "site"):
            self.assertIn(k, got)

    def test_empty(self):
        from kimp.webui import _available_keys
        self.assertEqual(_available_keys([]), [])


class LivePremiumFieldsTest(unittest.TestCase):
    """지금 김프가 화면에 필요한 칸을 다 주는지 (테더·사이트식 김프 포함)"""

    class FakeMd:
        """업비트 호가창 + 바이비트 티커를 흉내내는 가짜"""

        class _Up:
            id = "upbit"
            markets = {"AAA/KRW": {"base": "AAA", "quote": "KRW", "active": True}}

            def fetch_order_books(self, symbols, limit=5):
                out = {}
                for sym in symbols:
                    if sym == "USDT/KRW":
                        out[sym] = {"bids": [(1_330.0, 1e6)], "asks": [(1_331.0, 1e6)]}
                    else:
                        out[sym] = {"bids": [(99.0, 1e6)], "asks": [(101.0, 1e6)]}
                return out

            def fetch_tickers(self, symbols=None):
                return {"AAA/KRW": {"quoteVolume": 100e8}}

        class _By:
            id = "bybit"
            markets = {"AAA/USDT:USDT": {}}

            def fetch_tickers(self, symbols=None):
                return {"AAA/USDT:USDT": {"bid": 0.0745, "ask": 0.0755, "quoteVolume": 2e6,
                                          "info": {"fundingRate": "0.0001"}}}

            def market(self, symbol):
                return {"info": {"fundingInterval": "480"}}

        def __init__(self):
            self.upbit, self.bybit = self._Up(), self._By()

    def test_fields(self):
        import kimp.webui as w
        orig_fx, w._fx_rate = w._fx_rate, lambda *a, **k: 1_320.0
        try:
            row = w.live_premium(self.FakeMd(), ["AAA"])[0]
        finally:
            w._fx_rate = orig_fx
        # 진입 김프는 가운데보다 높고(불리), 청산 김프는 낮다
        self.assertGreater(row["entry"], row["mid"])
        self.assertLess(row["exit"], row["mid"])
        # 코인 호가 간격 = 업비트(101/99-1=2.02%) + 바이비트(0.0755/0.0745-1=1.34%)
        self.assertAlmostEqual(row["coin_spread"], 2.0202 + 1.3423, places=2)
        # 테더 프리미엄 = USDT 가운데(1330.5) / 환율(1320) − 1
        self.assertAlmostEqual(row["tether_premium"], (1_330.5 / 1_320.0 - 1) * 100, places=4)
        self.assertIsNotNone(row["site_premium"])
        # 펀딩비율 0.01% (8시간 정산이라 환산 그대로)
        self.assertAlmostEqual(row["funding_8h"], 0.01, places=6)

    def test_without_fx_only_tether_site_missing(self):
        """환율을 못 받아도 김프는 그대로 나온다 (표시용이라 매매에 영향 없음)"""
        import kimp.webui as w
        orig_fx, w._fx_rate = w._fx_rate, lambda *a, **k: None
        try:
            row = w.live_premium(self.FakeMd(), ["AAA"])[0]
        finally:
            w._fx_rate = orig_fx
        self.assertIsNotNone(row["mid"])
        self.assertIsNone(row["tether_premium"])
        self.assertIsNone(row["site_premium"])

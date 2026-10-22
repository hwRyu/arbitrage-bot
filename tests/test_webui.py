"""
로컬 화면(webui) 데이터 함수 테스트 (네트워크 불필요)

서버가 아니라 '화면에 줄 데이터를 만드는 함수' 를 확인한다.
실행: python -m unittest discover -s tests -v
"""
import json
import os
import unittest
from dataclasses import replace

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
        self.settings = replace(Settings(coins=["CAP"]), db_path=self.db)

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

    def test_premium_history_from_records(self):
        """차트 데이터는 [시각, 가운데 김프, 진입 김프, 청산 김프] 네 칸"""
        out = premium_history(self.settings, "CAP", hours=24)
        self.assertEqual(out["source"], "봇 기록")
        self.assertEqual(len(out["points"]), 40)
        ts, mid, entry, exit_ = out["points"][0]
        self.assertGreater(ts, 0)
        self.assertAlmostEqual(mid, -0.05)
        self.assertAlmostEqual(entry, 0.16)
        self.assertAlmostEqual(exit_, -0.26)

    def test_premium_history_window(self):
        """요청한 시간 범위보다 오래된 기록은 빼고 준다"""
        out = premium_history(self.settings, "CAP", hours=24, now_ms=10 ** 13)  # 아주 먼 미래
        self.assertEqual(out["points"], [])


class MissingFilesTest(unittest.TestCase):
    """파일이 없을 때도 터지지 않고 빈 값을 준다"""

    def test_no_db_no_json(self):
        s = replace(Settings(coins=["CAP"]), db_path="없는파일.db", history_db_path="없는파일2.db")
        self.assertEqual(sim_runs(s), [])
        self.assertEqual(premium_history(s, "CAP")["points"], [])
        self.assertIsNone(screen_latest("없는파일3.json"))


if __name__ == "__main__":
    unittest.main()

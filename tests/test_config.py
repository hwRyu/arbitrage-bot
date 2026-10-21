"""설정 로딩 테스트 — 특히 코인별 덮어쓰기 (네트워크 불필요)

실행: python -m unittest discover -s tests -v
"""
import os
import unittest
from unittest import mock

from kimp.config import load_settings


class CoinOverrideTest(unittest.TestCase):
    """'<코인>_<항목>' 환경 변수로 코인별 값을 덮어쓰는지 검증"""

    def _load(self, env: dict):
        """주어진 환경 변수만 있는 상태에서 설정 로드 (.env 파일은 읽지 않음)"""
        with mock.patch.dict(os.environ, env, clear=True):
            return load_settings(env_file=None)

    def test_override_and_fallback(self):
        """덮어쓴 항목만 코인별 값, 나머지는 공통값"""
        s = self._load({
            "COINS": "XRP,SOL",
            "ENTRY_PREMIUM_THRESHOLD_PERCENT": "-0.5",
            "TARGET_NET_PROFIT_PERCENT": "1.0",
            "SOL_ENTRY_PREMIUM_THRESHOLD_PERCENT": "-1.2",
            "SOL_POSITION_SIZE_USDT": "300",
        })
        xrp, sol = s.params_for("XRP"), s.params_for("SOL")
        self.assertEqual(xrp.entry_premium_threshold_percent, -0.5)  # 공통값
        self.assertEqual(xrp.overridden, ())
        self.assertEqual(sol.entry_premium_threshold_percent, -1.2)  # 코인별
        self.assertEqual(sol.position_size_usdt, 300)
        self.assertEqual(sol.target_net_profit_percent, 1.0)  # 덮어쓰지 않은 항목은 공통값
        self.assertEqual(set(sol.overridden), {"entry_premium_threshold_percent", "position_size_usdt"})

    def test_unknown_coin_is_default(self):
        """COINS 에 없는 코인(예: 목록에서 뺐지만 포지션이 남은 코인)은 공통값"""
        s = self._load({"COINS": "XRP", "ENTRY_PREMIUM_THRESHOLD_PERCENT": "-0.5"})
        self.assertEqual(s.params_for("BTC").entry_premium_threshold_percent, -0.5)

    def test_warns_unused_coin_keys(self):
        """COINS 에 없는 코인의 개별 설정은 경고로 알려준다 (오타 방지)"""
        s = self._load({"COINS": "XRP", "XPR_ENTRY_PREMIUM_THRESHOLD_PERCENT": "-1"})
        self.assertEqual(len(s.config_warnings), 1)
        self.assertIn("XPR_ENTRY_PREMIUM_THRESHOLD_PERCENT", s.config_warnings[0])

    def test_invalid_coin_values_rejected(self):
        """코인별 값도 공통값과 같은 규칙으로 검증하고, 숫자가 아니면 거부"""
        with self.assertRaisesRegex(ValueError, "SOL_STOP_LOSS_PERCENT"):
            self._load({"COINS": "SOL", "SOL_STOP_LOSS_PERCENT": "3"})
        with self.assertRaisesRegex(ValueError, "SOL_POSITION_SIZE_USDT=abc"):
            self._load({"COINS": "SOL", "SOL_POSITION_SIZE_USDT": "abc"})


class CapitalSettingsTest(unittest.TestCase):
    """레버리지 / 자본 비율 / 포지션 크기 설정"""

    def _load(self, env: dict):
        with mock.patch.dict(os.environ, env, clear=True):
            return load_settings(env_file=None)

    def test_defaults(self):
        """기본: 1배, 여유분 30% → 업비트 43.5%, 포지션 크기 자동, 시작 자본 10,000 USDT"""
        s = self._load({"COINS": "XRP"})
        self.assertEqual(s.bybit_leverage, 1.0)
        self.assertAlmostEqual(s.upbit_target_share(), 1 / 2.3)
        self.assertIsNone(s.position_size_usdt)
        self.assertEqual(s.paper_initial_total_usdt, 10_000)

    def test_leverage_changes_ratio(self):
        """레버리지 3배 → 업비트 69.8% (여유분 30%), 여유분 0 이면 75%"""
        self.assertAlmostEqual(self._load({"COINS": "XRP", "BYBIT_LEVERAGE": "3"}).upbit_target_share(), 3 / 4.3)
        s = self._load({"COINS": "XRP", "BYBIT_LEVERAGE": "3", "BYBIT_MARGIN_BUFFER_PERCENT": "0"})
        self.assertAlmostEqual(s.upbit_target_share(), 0.75)

    def test_invalid_capital_settings(self):
        """레버리지 범위, 사용률 범위를 검사한다"""
        with self.assertRaisesRegex(ValueError, "BYBIT_LEVERAGE"):
            self._load({"COINS": "XRP", "BYBIT_LEVERAGE": "20"})
        with self.assertRaisesRegex(ValueError, "CAPITAL_USAGE_PERCENT"):
            self._load({"COINS": "XRP", "CAPITAL_USAGE_PERCENT": "150"})


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

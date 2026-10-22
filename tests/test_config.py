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


class UnusedKeyWarningTest(unittest.TestCase):
    """COINS 에 없는 코인 설정은 경고하되, 코인이 아닌 설정은 경고하지 않는다"""

    def _load(self, env: dict):
        with mock.patch.dict(os.environ, env, clear=True):
            return load_settings(env_file=None)

    def test_warns_for_unknown_coin(self):
        """COINS=XRP 인데 SOL_POSITION_SIZE_USDT 가 있으면 경고 (오타 방지)"""
        s = self._load({"COINS": "XRP", "SOL_POSITION_SIZE_USDT": "300"})
        self.assertTrue(any("SOL_POSITION_SIZE_USDT" in w for w in s.config_warnings))

    def test_no_warning_for_carry_settings(self):
        """CARRY_POSITION_SIZE_USDT 는 펀딩비 전략 설정이라 'CARRY 코인' 으로 착각하면 안 된다"""
        s = self._load({"COINS": "XRP", "CARRY_POSITION_SIZE_USDT": "200"})
        self.assertEqual(s.config_warnings, ())
        self.assertEqual(s.carry_position_size_usdt, 200.0)


class TransferFeeTest(unittest.TestCase):
    """송금 수수료는 방향별 (업비트 출금 무료 / 바이비트 출금 1.3)"""

    def _load(self, env: dict):
        with mock.patch.dict(os.environ, env, clear=True):
            return load_settings(env_file=None)

    def test_defaults(self):
        """기본값: 업비트 → 바이비트 0, 바이비트 → 업비트 1.3"""
        s = self._load({"COINS": "XRP"})
        self.assertEqual(s.transfer_fee_usdt(True), 0.0)
        self.assertEqual(s.transfer_fee_usdt(False), 1.3)

    def test_env_override(self):
        """.env 로 방향마다 따로 바꿀 수 있다 (거래소가 수수료를 바꿨을 때)"""
        s = self._load({"COINS": "XRP", "TRANSFER_FEE_UPBIT_TO_BYBIT_USDT": "0.2",
                        "TRANSFER_FEE_BYBIT_TO_UPBIT_USDT": "1.5"})
        self.assertEqual(s.transfer_fee_usdt(True), 0.2)
        self.assertEqual(s.transfer_fee_usdt(False), 1.5)

    def test_old_key_warns(self):
        """옛 이름(REBALANCE_TRANSFER_FEE_USDT)이 남아 있으면 경고하고 값은 쓰지 않는다"""
        s = self._load({"COINS": "XRP", "REBALANCE_TRANSFER_FEE_USDT": "1.0"})
        self.assertEqual(s.transfer_fee_usdt(True), 0.0)  # 옛 값 1.0 을 쓰지 않는다
        self.assertTrue(any("REBALANCE_TRANSFER_FEE_USDT" in w for w in s.config_warnings))


if __name__ == "__main__":
    unittest.main()

"""
코인별 바이비트 수수료 (kimp/fees.py, FeeSettings.bybit_taker_rate) — 가짜 거래소로

실행: python -m unittest discover -s tests -v
"""
import unittest

from kimp.config import FeeSettings
from kimp.fees import fetch_bybit_taker_rates, with_bybit_fee_rates
from test_engine import make_settings


class FakeBybitFees:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def fetch_trading_fee(self, symbol):
        self.calls.append(symbol)
        if symbol not in self.table:
            raise RuntimeError("symbol not found")
        return {"taker": self.table[symbol], "maker": 0.0002}


class FeeSettingsTest(unittest.TestCase):
    def test_per_coin_rate_falls_back_to_default(self):
        f = FeeSettings(upbit_trade_fee_rate=0.0005, bybit_taker_fee_rate=0.00055, bybit_taker_fee_rates={"CAP": 0.0011},
                        upbit_trade_fee_rates={"DOGE": 0.0004})
        self.assertEqual(f.bybit_taker_rate("CAP"), 0.0011)
        self.assertEqual(f.bybit_taker_rate("MINA"), 0.00055)
        self.assertEqual(f.bybit_taker_rate(None), 0.00055)
        self.assertEqual(f.upbit_trade_rate("DOGE"), 0.0004)
        self.assertEqual(f.upbit_trade_rate("CAP"), 0.0005)
        self.assertAlmostEqual(f.round_trip_trade_fee_percent("CAP"), 0.42)     # 0.2 + 2 × 0.11
        self.assertAlmostEqual(f.round_trip_trade_fee_percent("MINA"), 0.31)
        self.assertAlmostEqual(f.round_trip_trade_fee_percent("DOGE"), 0.29)    # USDT 2×0.05 + 코인 2×0.04 + 바이비트 2×0.055
        self.assertAlmostEqual(f.round_trip_trade_fee_percent(), 0.31)

    def test_env_overrides_are_read_for_any_coin_and_beat_fetched(self):
        """.env 코인별 수수료는 COINS 에 없는 코인도 받고, 거래소 조회값보다 우선한다"""
        import os
        from kimp.config import load_settings
        env = {"COINS": "CAP", "CAP_BYBIT_TAKER_FEE_RATE": "0.0009", "ZZZ_UPBIT_TRADE_FEE_RATE": "0.0004",
               "ZZZ_BYBIT_TAKER_FEE_RATE": "0.0011"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            s = load_settings(env_file=None)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(s.fees.bybit_taker_rate("CAP"), 0.0009)
        self.assertEqual(s.fees.upbit_trade_rate("ZZZ"), 0.0004)
        self.assertEqual(s.fees.bybit_taker_rate("ZZZ"), 0.0011)
        self.assertFalse(any("ZZZ_" in w for w in s.config_warnings))          # 수수료 덮어쓰기는 '무시' 경고를 내지 않는다
        by = FakeBybitFees({"CAP/USDT:USDT": 0.0011})
        s2 = with_bybit_fee_rates(s, ["CAP"], bybit=by)
        self.assertEqual(s2.fees.bybit_taker_rate("CAP"), 0.0009)               # 설정값이 조회값보다 우선
        self.assertEqual(by.calls, [])                                          # 설정된 코인은 조회조차 안 한다


class FetchTest(unittest.TestCase):
    def test_fetch_skips_failures_and_keeps_rest(self):
        by = FakeBybitFees({"CAP/USDT:USDT": 0.0011, "MINA/USDT:USDT": 0.00055})
        rates = fetch_bybit_taker_rates(by, ["CAP", "MINA", "NOPE"])
        self.assertEqual(rates, {"CAP": 0.0011, "MINA": 0.00055})
        self.assertEqual(by.calls, ["CAP/USDT:USDT", "MINA/USDT:USDT", "NOPE/USDT:USDT"])

    def test_with_rates_returns_new_settings_and_keeps_original(self):
        s = make_settings(coins=["CAP", "MINA"])
        by = FakeBybitFees({"CAP/USDT:USDT": 0.0011, "MINA/USDT:USDT": 0.00055})
        s2 = with_bybit_fee_rates(s, ["CAP", "MINA"], bybit=by)
        self.assertEqual(s2.fees.bybit_taker_rate("CAP"), 0.0011)
        self.assertEqual(s.fees.bybit_taker_rate("CAP"), s.fees.bybit_taker_fee_rate)   # 원본은 그대로 (frozen 복사)
        self.assertEqual(s2.coins, s.coins)

    def test_without_keys_settings_unchanged(self):
        s = make_settings(coins=["CAP"])
        self.assertIs(with_bybit_fee_rates(s, ["CAP"]), s)       # 키 없음 → 조회 안 함


class PaperFillUsesPerCoinFeeTest(unittest.TestCase):
    def test_paper_short_fee_uses_coin_rate(self):
        """페이퍼 체결 수수료도 코인별 요율을 쓴다 (시뮬레이션 비용이 실제와 같아지게)"""
        from dataclasses import replace
        from kimp.db import Position, make_session_factory
        from kimp.engine import Engine
        from kimp.paper_broker import PaperBroker
        from test_engine import FEES, FakeMarket, make_quote
        fees = replace(FEES, bybit_taker_fee_rates={"XRP": 0.0011}, upbit_trade_fee_rates={"XRP": 0.0004})
        settings = replace(make_settings(), fees=fees)
        market = FakeMarket()
        sf = make_session_factory(":memory:")
        broker = PaperBroker(fees, 0.3, leverage=1.0, mmr=0.005)
        engine = Engine(settings, market, None, broker, sf, ["XRP"])
        engine.startup()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            pos = s.query(Position).one()
        self.assertAlmostEqual(pos.entry_bybit_fee_usdt / (pos.bybit_qty * pos.entry_bybit_price_usdt), 0.0011, places=6)
        # 업비트 코인 매수 수수료는 코인별 0.04%, USDT 매도 수수료는 공통 0.05%
        self.assertAlmostEqual(pos.entry_upbit_fee_krw / (pos.upbit_qty * pos.entry_upbit_price_krw), 0.0004, places=6)
        self.assertAlmostEqual(pos.entry_usdt_fee_krw / (pos.usdt_sold * pos.entry_usdt_price_krw), 0.0005, places=6)

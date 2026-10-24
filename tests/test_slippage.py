"""
체결 밀림 가정 (kimp/slippage.py) — 다리별 부호, 실측 중앙값, 옵션 해석

실행: python -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest

from kimp.db import ExitReason, Fill, Position, PositionStatus, make_session_factory, utcnow
from kimp.slippage import MIN_CLOSES, leg_adverse_percent, measured_exit_slippage, resolve


def _position(s, coin):
    p = Position(coin=coin, status=PositionStatus.CLOSED, usdt_sold=20, entry_usdt_price_krw=1340, entry_usdt_fee_krw=0,
                 upbit_qty=300, entry_upbit_price_krw=90, entry_upbit_fee_krw=0, bybit_qty=300,
                 entry_bybit_price_usdt=0.067, entry_bybit_fee_usdt=0, entry_premium=-0.2, bybit_margin_usdt=20,
                 liquidation_price=0.13, closed_at=utcnow(), exit_reason=ExitReason.TAKE_PROFIT)
    s.add(p)
    s.flush()
    return p


def _close_legs(s, pos, sell=(81.3, 81.0), short=(0.0605, 0.06058), usdt=(1341.0, 1341.0)):
    """청산 세 다리 (기대가, 체결가)"""
    for side, (exp, act), ex, cur in (("sell", sell, "upbit", "KRW"), ("close_short", short, "bybit", "USDT"),
                                      ("buy_usdt", usdt, "upbit", "KRW")):
        s.add(Fill(position_id=pos.id, exchange=ex, side=side, qty=1, avg_price=act, fee=0, fee_currency=cur,
                   slippage_percent=0, expected_price=exp))


class LegSignTest(unittest.TestCase):
    def test_selling_leg_is_adverse_when_filled_lower(self):
        self.assertAlmostEqual(leg_adverse_percent("sell", 81.3, 81.0), 0.369, places=3)   # BLEND #10
        self.assertAlmostEqual(leg_adverse_percent("sell", 81.3, 81.3), 0.0)
        self.assertLess(leg_adverse_percent("sell", 81.3, 81.4), 0)                        # 유리하면 음수

    def test_buying_leg_is_adverse_when_filled_higher(self):
        self.assertAlmostEqual(leg_adverse_percent("close_short", 0.0605, 0.06058), 0.132, places=3)
        self.assertLess(leg_adverse_percent("buy_usdt", 1341, 1340), 0)


class MeasuredTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)   # 윈도우: SQLite 파일 잠금
        self.path = os.path.join(self.dir.name, "live.db")
        self.sf = make_session_factory(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_median_per_coin_and_overall(self):
        with self.sf() as s:
            for _ in range(MIN_CLOSES):                     # BLEND 3건: 0.369+0.132+0 ≈ 0.50
                _close_legs(s, _position(s, "BLEND"))
            _close_legs(s, _position(s, "CAP"), sell=(95.9, 95.9), short=(0.0718, 0.0718))   # CAP 1건: 0
            half = _position(s, "MINA")                     # 다리가 하나 빠진 건 안 센다
            s.add(Fill(position_id=half.id, exchange="upbit", side="sell", qty=1, avg_price=90, fee=0,
                       fee_currency="KRW", slippage_percent=0, expected_price=91))
            s.commit()
        m = measured_exit_slippage(self.path)
        self.assertEqual(m.counts, {"BLEND": 3, "CAP": 1})
        self.assertAlmostEqual(m.per_coin["BLEND"], 0.501, places=2)
        self.assertNotIn("CAP", m.per_coin)                 # 3건 미만 → 코인별 값 없음
        self.assertEqual(m.total, 4)
        self.assertAlmostEqual(m.overall, 0.501, places=2)  # [0.5, 0.5, 0.5, 0] 의 중앙값

    def test_resolve_chain(self):
        self.assertEqual(resolve(None, 0.05, self.path).for_coin("X"), 0.05)
        a = resolve("0.2", 0.05, self.path)
        self.assertEqual((a.for_coin("X"), a.source), (0.2, "0.2% (입력값)"))
        with self.assertRaises(ValueError):
            resolve("abc", 0.05, self.path)
        with self.assertRaises(ValueError):
            resolve("-1", 0.05, self.path)
        empty = resolve("auto", 0.05, self.path)            # 기록 없음 → 설정값
        self.assertEqual(empty.for_coin("BLEND"), 0.05)
        self.assertIn("실측 기록 없음", empty.source)
        with self.sf() as s:
            for _ in range(MIN_CLOSES):
                _close_legs(s, _position(s, "BLEND"))
            _close_legs(s, _position(s, "CAP"), sell=(95.9, 95.9), short=(0.0718, 0.0718))
            s.commit()
        auto = resolve("auto", 0.05, self.path)
        self.assertAlmostEqual(auto.for_coin("BLEND"), 0.501, places=2)   # 코인별 값
        self.assertAlmostEqual(auto.for_coin("ZAMA"), 0.501, places=2)    # 기록 없는 코인 → 전체 중앙값
        self.assertIn("BLEND 0.50(3건)", auto.source)

    def test_missing_db_is_empty(self):
        m = measured_exit_slippage(os.path.join(self.dir.name, "none", "x.db"))
        self.assertIsNone(m.overall)

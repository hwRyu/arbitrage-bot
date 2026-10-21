"""
계산 테스트: 김프 / 호가창 체결 계산 / 손익 (인터넷 필요 없음)

손익 테스트는 docs/concept.md 9번 예시(A~H)를 그대로 옮겨서,
문서의 숫자와 실제 코드 계산이 같은지 확인한다.

실행: python -m unittest discover -s tests -v
"""
import unittest

from kimp.config import FeeSettings
from kimp.pnl import EntryLegs, ExitLegs, compute_pnl, estimated_exit_premium
from kimp.premium import (OrderBook, Quote, coin_premium_percent, estimate_fill, estimate_fill_by_cost,
                          premium_percent, tether_premium_percent)

# 비용 0 설정: 원리만 볼 때 (문서 A~G)
NO_FEES = FeeSettings(upbit_trade_fee_rate=0, bybit_taker_fee_rate=0,
                      transfer_fee_usdt_per_cycle=0, slippage_buffer_percent=0)
# 실제 비슷한 비용 (문서 A′, H)
REAL_FEES = FeeSettings(upbit_trade_fee_rate=0.0005, bybit_taker_fee_rate=0.00055,
                        transfer_fee_usdt_per_cycle=1.0, slippage_buffer_percent=0)

FX = 1325.0  # 달러 환율
NOTIONAL_USDT = 1000.0  # 바이비트 숏 규모


def scenario(pb1, tether1, coin1, pb2, tether2, coin2, fx2=FX, fees=NO_FEES):
    """
    문서 예시 한 줄을 그대로 계산한다

    Args:
        pb1, pb2: 진입/청산 때 바이비트 가격 (USDT)
        tether1, tether2: 진입/청산 때 테더 프리미엄 (%)
        coin1, coin2: 진입/청산 때 코인 김프 (%)
        fx2: 청산 때 달러 환율
    Returns:
        (손익, 진입 김프, 청산 김프)
    """
    # 업비트 가격 = 바이비트 가격 × 업비트 USDT 가격 × (1 + 코인 김프)
    pu1 = pb1 * FX * (1 + tether1 / 100) * (1 + coin1 / 100)
    pu2 = pb2 * fx2 * (1 + tether2 / 100) * (1 + coin2 / 100)
    bybit_qty = NOTIONAL_USDT / pb1
    upbit_qty = NOTIONAL_USDT * FX / pu1  # 금액 헷징: 숏 금액 × 환율 원어치 매수
    entry = EntryLegs(upbit_qty=upbit_qty, bybit_qty=bybit_qty, upbit_price_krw=pu1, bybit_price_usdt=pb1,
                      fx_krw=FX, upbit_fee_krw=pu1 * upbit_qty * fees.upbit_trade_fee_rate,
                      bybit_fee_usdt=NOTIONAL_USDT * fees.bybit_taker_fee_rate)
    exit_ = ExitLegs(upbit_price_krw=pu2, bybit_price_usdt=pb2, fx_krw=fx2,
                     upbit_fee_krw=pu2 * upbit_qty * fees.upbit_trade_fee_rate,
                     bybit_fee_usdt=pb2 * bybit_qty * fees.bybit_taker_fee_rate)
    pnl = compute_pnl(entry, exit_, 0.0, fees)
    return pnl, premium_percent(pu1, pb1, FX), premium_percent(pu2, pb2, fx2)


class PremiumTest(unittest.TestCase):
    """김프 계산"""

    def test_premium_formula(self):
        """업비트 1,770원, 바이비트 1.35 USDT, 달러 1,325원 → 약 -1.05% (concept.md 3번)"""
        self.assertAlmostEqual(premium_percent(1770, 1.35, 1325), (1770 / (1.35 * 1325) - 1) * 100)
        self.assertAlmostEqual(premium_percent(1770, 1.35, 1325), -1.048, places=3)

    def test_tether_premium(self):
        """업비트 USDT 1,332원 / 달러 1,325원 → +0.53% (김프 사이트 USDT 줄과 같은 값)"""
        self.assertAlmostEqual(tether_premium_percent(1332, 1325), 0.528, places=3)

    def test_premium_includes_tether(self):
        """전체 김프 ≈ 테더 프리미엄 + 코인 김프 (곱해서 합쳐짐)"""
        usdt = FX * 1.005  # 테더 +0.5%
        upbit = 1.35 * usdt * 0.985  # 코인 -1.5%
        total = premium_percent(upbit, 1.35, FX)
        self.assertAlmostEqual(total, (1.005 * 0.985 - 1) * 100)

    def test_coin_premium_split(self):
        """전체 김프 +0.486%, 테더 +0.523% → 코인 몫 약 -0.037% (붙여준 사이트 BTC 값)"""
        self.assertAlmostEqual(coin_premium_percent(0.486, 0.523), -0.0368, places=3)
        # 다시 합치면 원래 전체 김프
        coin = coin_premium_percent(-1.0, 0.5)
        self.assertAlmostEqual(((1.005) * (1 + coin / 100) - 1) * 100, -1.0)

    def test_entry_premium_is_worse_than_mid(self):
        """진입 김프 > 가운데 김프 > 청산 김프 (호가 간격만큼 불리)"""
        q = Quote(coin="X",
                  upbit=OrderBook(bids=[(1399, 100)], asks=[(1401, 100)]),
                  bybit=OrderBook(bids=[(0.999, 100)], asks=[(1.001, 100)]),
                  fx_krw=1400)
        self.assertGreater(q.entry_premium_top, q.mid_premium)
        self.assertLess(q.exit_premium_top, q.mid_premium)

    def test_tether_premium_is_display_only(self):
        """업비트 USDT 가격이 바뀌어도 김프 값은 그대로 (달러 환율로만 계산)"""
        book_u = OrderBook(bids=[(1399, 100)], asks=[(1401, 100)])
        book_b = OrderBook(bids=[(0.999, 100)], asks=[(1.001, 100)])
        a = Quote(coin="X", upbit=book_u, bybit=book_b, fx_krw=1400, usdt_krw=1400)
        b = Quote(coin="X", upbit=book_u, bybit=book_b, fx_krw=1400, usdt_krw=1450)
        self.assertEqual(a.mid_premium, b.mid_premium)
        self.assertNotEqual(a.tether_premium, b.tether_premium)


class FillTest(unittest.TestCase):
    """호가창으로 체결 계산"""

    def test_fill_by_qty(self):
        """100원 1개 + 101원 1개 + 102원 1개 = 평균 101원, 맨 앞보다 1% 밀림"""
        fill = estimate_fill([(100, 1), (101, 1), (102, 10)], 3)
        self.assertAlmostEqual(fill.avg_price, 101)
        self.assertTrue(fill.is_complete)
        self.assertAlmostEqual(fill.slippage_percent, 1.0)

    def test_fill_by_cost(self):
        """200원어치: 100원짜리 1개 + 200원짜리 0.5개 = 1.5개, 평균 133.33원"""
        fill = estimate_fill_by_cost([(100, 1), (200, 10)], 200)
        self.assertAlmostEqual(fill.filled_qty, 1.5)
        self.assertAlmostEqual(fill.avg_price, 200 / 1.5)
        self.assertAlmostEqual(fill.cost, 200)
        self.assertTrue(fill.is_complete)

    def test_not_enough_orderbook(self):
        """호가가 모자라면 채운 만큼만 + 미완료 표시"""
        self.assertFalse(estimate_fill([(100, 1)], 2).is_complete)
        self.assertFalse(estimate_fill_by_cost([(100, 1)], 500).is_complete)

    def test_empty(self):
        """빈 호가창 / 0 은 None"""
        self.assertIsNone(estimate_fill([], 1))
        self.assertIsNone(estimate_fill([(100, 1)], 0))
        self.assertIsNone(estimate_fill_by_cost([], 100))

    def test_entry_fills_match_notional(self):
        """금액 헷징: 업비트에서 쓰는 원화 = 바이비트 숏 금액 × 달러 환율"""
        q = Quote(coin="X",
                  upbit=OrderBook(bids=[(1300, 1e6)], asks=[(1301, 1e6)]),
                  bybit=OrderBook(bids=[(1.0, 1e6)], asks=[(1.001, 1e6)]),
                  fx_krw=1325)
        up, by = q.entry_fills(1000)
        self.assertAlmostEqual(up.cost, by.cost * 1325)


class ConceptExamplesTest(unittest.TestCase):
    """docs/concept.md 9번 예시가 코드 계산과 같은지 (원 단위, 1원 오차 허용)"""

    def test_A_coin_premium_recovers(self):
        """A. 코인 김프만 회복 → +26,904원 (+2.03%)"""
        pnl, entry, exit_ = scenario(1.35, 0.5, -1.5, 1.35, 0.5, 0.5)
        self.assertAlmostEqual(entry, -1.008, places=3)
        self.assertAlmostEqual(exit_, 1.002, places=3)
        self.assertAlmostEqual(pnl.net_krw, 26_904, delta=1)
        self.assertAlmostEqual(pnl.net_percent, 2.030, places=3)

    def test_B_tether_premium_recovers(self):
        """B. 테더 프리미엄만 회복 → +20,076원. 원화는 늘고 USDT 는 그대로 = 진짜 이득"""
        pnl, _, _ = scenario(1.35, -1.0, 0.0, 1.35, 0.5, 0.0)
        self.assertAlmostEqual(pnl.net_krw, 20_076, delta=1)
        self.assertAlmostEqual(pnl.bybit_leg_usdt, 0.0)

    def test_C_both_recover_with_price_jump(self):
        """C. 둘 다 회복 + 가격 18.5% 급등 → 가격 손익은 상쇄, +31,724원"""
        pnl, _, _ = scenario(1.35, -0.5, -0.5, 1.60, 0.5, 0.5)
        self.assertAlmostEqual(pnl.upbit_leg_krw, 277_094, delta=1)
        self.assertAlmostEqual(pnl.bybit_leg_krw, -245_370, delta=1)
        self.assertAlmostEqual(pnl.net_krw, 31_724, delta=1)

    def test_D_price_crash_premium_same(self):
        """D. 가격 26% 폭락, 김프 그대로 → 0원 (금액 헷징이라 정확히 상쇄)"""
        pnl, _, _ = scenario(1.35, -0.5, -0.5, 1.00, -0.5, -0.5)
        self.assertAlmostEqual(pnl.upbit_leg_krw, -343_519, delta=1)
        self.assertAlmostEqual(pnl.net_krw, 0.0, delta=0.01)

    def test_constant_premium_any_price_is_zero(self):
        """김프와 환율이 그대로면 가격이 어디로 가도 손익 0"""
        for pb2 in (0.5, 1.0, 2.0, 5.0):
            pnl, _, _ = scenario(1.35, 0.3, -1.2, pb2, 0.3, -1.2)
            self.assertAlmostEqual(pnl.net_krw, 0.0, delta=0.01)

    def test_E_tether_falls_further(self):
        """E. 테더가 0% → -2% 로 더 떨어짐 → -26,500원 (-2.00%)"""
        pnl, _, exit_ = scenario(1.35, 0.0, -1.0, 1.35, -2.0, -1.0)
        self.assertAlmostEqual(exit_, -2.98, places=2)
        self.assertAlmostEqual(pnl.net_krw, -26_500, delta=1)

    def test_F_coin_up_tether_down_cancel(self):
        """F. 코인 김프 +1%p, 테더 -1%p → 전체 김프 그대로라 거의 0 (+134원)"""
        pnl, _, _ = scenario(1.35, 0.5, -1.5, 1.35, -0.5, -0.5)
        self.assertAlmostEqual(pnl.net_krw, 134, delta=1)

    def test_G_fx_rises(self):
        """G. 달러 환율만 1,325 → 1,345 → 김프 그대로인데 원화 기준 +20,000원"""
        pnl, entry, exit_ = scenario(1.35, 0.0, -1.0, 1.35, 0.0, -1.0, fx2=1345)
        self.assertAlmostEqual(entry, exit_)
        self.assertAlmostEqual(pnl.net_krw, 20_000, delta=1)

    def test_A_prime_with_costs(self):
        """A′. A 에 비용 포함 → 비용 4,121원, 순손익 +22,783원 (+1.72%)"""
        pnl, _, _ = scenario(1.35, 0.5, -1.5, 1.35, 0.5, 0.5, fees=REAL_FEES)
        self.assertAlmostEqual(pnl.total_cost_krw, 4_121, delta=1)
        self.assertAlmostEqual(pnl.net_krw, 22_783, delta=1)
        self.assertAlmostEqual(pnl.net_percent, 1.719, places=3)

    def test_H_small_recovery_not_enough(self):
        """H. 김프 0.4%p 회복 → 비용 빼면 +1,243원 (+0.09%), 목표 1% 에 한참 모자람"""
        pnl, _, _ = scenario(1.35, 0.0, -1.0, 1.35, 0.0, -0.6, fees=REAL_FEES)
        self.assertAlmostEqual(pnl.net_krw, 1_243, delta=1)
        self.assertLess(pnl.net_percent, 1.0)

    def test_funding_added(self):
        """펀딩비 +0.5 USDT 받으면 청산 환율로 원화 환산해서 더해진다"""
        base, _, _ = scenario(1.35, 0.0, -1.0, 1.35, 0.0, -1.0)
        pu = 1.35 * FX * 0.99
        entry = EntryLegs(upbit_qty=NOTIONAL_USDT * FX / pu, bybit_qty=NOTIONAL_USDT / 1.35, upbit_price_krw=pu,
                          bybit_price_usdt=1.35, fx_krw=FX, upbit_fee_krw=0, bybit_fee_usdt=0)
        exit_ = ExitLegs(upbit_price_krw=pu, bybit_price_usdt=1.35, fx_krw=FX, upbit_fee_krw=0, bybit_fee_usdt=0)
        pnl = compute_pnl(entry, exit_, funding_usdt=0.5, fees=NO_FEES)
        self.assertAlmostEqual(pnl.net_krw - base.net_krw, 0.5 * FX)

    def test_estimated_exit_premium(self):
        """청산 예상 김프: -1 + 1(목표) + 0.21(수수료) + 0.1(전송 1/1000) + 0.05(여유) = +0.36%"""
        fees = FeeSettings(upbit_trade_fee_rate=0.0005, bybit_taker_fee_rate=0.00055,
                           transfer_fee_usdt_per_cycle=1.0, slippage_buffer_percent=0.05)
        self.assertAlmostEqual(estimated_exit_premium(-1.0, 1.0, 1000, 0.0, fees), 0.36)


if __name__ == "__main__":
    unittest.main()

"""
계산 테스트: 김프 / 호가창 체결 계산 / 손익 (인터넷 필요 없음)

손익 테스트는 docs/concept.md 10번 예시(A~H)를 그대로 옮겨서,
문서의 숫자와 실제 코드 계산이 같은지 확인한다.

실행: python -m unittest discover -s tests -v
"""
import unittest

from kimp.config import FeeSettings
from kimp.pnl import EntryLegs, ExitLegs, compute_pnl, estimated_exit_premium, liquidation_price
from kimp.premium import (OrderBook, Quote, estimate_fill, estimate_fill_by_cost, premium_percent,
                          tether_premium_percent)

NO_FEES = FeeSettings(upbit_trade_fee_rate=0, bybit_taker_fee_rate=0, transfer_fee_usdt_per_cycle=0,
                      slippage_buffer_percent=0)
REAL_FEES = FeeSettings(upbit_trade_fee_rate=0.0005, bybit_taker_fee_rate=0.00055, transfer_fee_usdt_per_cycle=1.0,
                        slippage_buffer_percent=0)
FX = 1325.0
DEPTH = 1e12


def book(price: float, spread_percent: float = 0.0) -> OrderBook:
    """가운데 가격 price, 호가 간격 spread_percent 인 호가창 (아주 두껍게)"""
    half = spread_percent / 100 / 2
    return OrderBook(bids=[(price * (1 - half), DEPTH)], asks=[(price * (1 + half), DEPTH)])


def quote(pb: float, tether: float, coin_prem: float, fx: float = FX, usdt_spread: float = 0.0) -> Quote:
    """
    문서 예시처럼 만든 시세: 업비트 USDT = 환율 × (1 + 테더), 업비트 코인 = 바이비트 × 업비트 USDT × (1 + 코인 김프)
    """
    usdt = fx * (1 + tether / 100)
    return Quote(coin="XRP", upbit=book(pb * usdt * (1 + coin_prem / 100)), bybit=book(pb),
                 usdt=book(usdt, usdt_spread), fx_krw=fx)


def scenario(q1: Quote, q2: Quote, fees=NO_FEES, notional=1000.0):
    """q1 에서 진입(바이비트 notional USDT 숏), q2 에서 청산한 손익"""
    rate = fees.upbit_trade_fee_rate
    qb = notional / q1.bybit.best_bid
    e = q1.entry_plan(qb, rate)
    x = q2.exit_plan(e.upbit.filled_qty, qb, rate)
    entry = EntryLegs(usdt_sold=e.usdt_sold, usdt_price_krw=e.usdt.avg_price, usdt_fee_krw=e.usdt_fee_krw,
                      upbit_qty=e.upbit.filled_qty, upbit_price_krw=e.upbit.avg_price, upbit_fee_krw=e.coin_fee_krw,
                      bybit_qty=qb, bybit_price_usdt=e.bybit.avg_price,
                      bybit_fee_usdt=e.bybit.cost * fees.bybit_taker_fee_rate)
    exit_ = ExitLegs(upbit_price_krw=x.upbit.avg_price, upbit_fee_krw=x.coin_fee_krw, usdt_price_krw=x.usdt.avg_price,
                     usdt_fee_krw=x.usdt_fee_krw, usdt_bought=x.usdt_bought, bybit_price_usdt=x.bybit.avg_price,
                     bybit_fee_usdt=x.bybit.cost * fees.bybit_taker_fee_rate)
    return compute_pnl(entry, exit_, 0.0, fees), e, x


class PremiumTest(unittest.TestCase):
    """김프 계산"""

    def test_premium_formula(self):
        """업비트 1,795원, 바이비트 1.345 USDT, 업비트 USDT 1,333원 → 약 +0.12%"""
        self.assertAlmostEqual(premium_percent(1795, 1.345, 1333), 0.118, places=3)

    def test_tether_premium(self):
        """업비트 USDT 1,332원 / 달러 1,325원 → +0.53%"""
        self.assertAlmostEqual(tether_premium_percent(1332, 1325), 0.528, places=3)

    def test_bot_premium_excludes_tether_site_premium_includes(self):
        """봇 김프는 코인 몫만, 사이트식 김프는 테더 포함 (예: 테더 +0.5%, 코인 -1.5%)"""
        q = quote(1.35, 0.5, -1.5)
        self.assertAlmostEqual(q.mid_premium, -1.5)
        self.assertAlmostEqual(q.site_premium, (1.005 * 0.985 - 1) * 100)
        self.assertAlmostEqual(q.tether_premium, 0.5)

    def test_entry_premium_is_worse_than_mid(self):
        """진입 김프 > 가운데 김프 > 청산 김프 (세 군데 호가 간격만큼 불리)"""
        q = Quote(coin="X", upbit=book(1400, 0.1), bybit=book(1.0, 0.02), usdt=book(1400, 0.075))
        self.assertGreater(q.entry_premium_top, q.mid_premium)
        self.assertLess(q.exit_premium_top, q.mid_premium)
        self.assertAlmostEqual(q.usdt_spread_percent, 0.075, places=3)


class FillTest(unittest.TestCase):
    """호가창으로 체결 계산"""

    def test_fill_by_qty(self):
        """100원 1개 + 101원 1개 + 102원 1개 = 평균 101원, 맨 앞보다 1% 밀림"""
        fill = estimate_fill([(100, 1), (101, 1), (102, 10)], 3)
        self.assertAlmostEqual(fill.avg_price, 101)
        self.assertTrue(fill.is_complete)
        self.assertAlmostEqual(fill.slippage_percent, 1.0)

    def test_fill_by_cost(self):
        """200원어치: 100원짜리 1개 + 200원짜리 0.5개 = 1.5개"""
        fill = estimate_fill_by_cost([(100, 1), (200, 10)], 200)
        self.assertAlmostEqual(fill.filled_qty, 1.5)
        self.assertAlmostEqual(fill.cost, 200)

    def test_not_enough_orderbook(self):
        """호가가 모자라면 미완료"""
        self.assertFalse(estimate_fill([(100, 1)], 2).is_complete)
        self.assertFalse(estimate_fill_by_cost([(100, 1)], 500).is_complete)

    def test_entry_plan_sells_same_usdt_as_short(self):
        """금액 헷징: 업비트에서 파는 USDT 개수 = 바이비트 숏 금액, 그 원화로 코인을 산다"""
        q = quote(1.35, 0.5, -1.0)
        plan = q.entry_plan(1000 / 1.35, 0.0)
        self.assertAlmostEqual(plan.usdt_sold, 1000)
        self.assertAlmostEqual(plan.upbit.cost, 1000 * q.usdt.mid)
        self.assertAlmostEqual(plan.premium, -1.0)


class ConceptExamplesTest(unittest.TestCase):
    """docs/concept.md 10번 예시가 코드 계산과 같은지 (0.01 USDT 오차 허용)"""

    def test_A_coin_premium_recovers(self):
        """A. 코인 김프 -1.5% → +0.5% → +20.30 USDT (+2.03%)"""
        pnl, e, x = scenario(quote(1.35, 0.5, -1.5), quote(1.35, 0.5, 0.5))
        self.assertAlmostEqual(pnl.net_usdt, 20.30, delta=0.01)
        self.assertAlmostEqual(e.premium, -1.5)
        self.assertAlmostEqual(x.premium, 0.5)

    def test_B_tether_only_is_zero(self):
        """B. 테더만 -1% → +0.5% → 0 (테더 이득은 봇이 아니라 USDT 보유자 몫)"""
        pnl, _, _ = scenario(quote(1.35, -1.0, 0.0), quote(1.35, 0.5, 0.0))
        self.assertAlmostEqual(pnl.net_usdt, 0.0, delta=0.01)

    def test_C_coin_recovers_with_price_jump(self):
        """C. 코인 김프 -1 → +1, 가격 1.35 → 1.60 → +23.94 USDT"""
        pnl, _, _ = scenario(quote(1.35, 0, -1), quote(1.60, 0, 1))
        self.assertAlmostEqual(pnl.net_usdt, 23.94, delta=0.01)

    def test_D_price_crash_is_zero(self):
        """D. 가격 1.35 → 1.00, 김프 그대로 → 0"""
        pnl, _, _ = scenario(quote(1.35, 0, -1), quote(1.00, 0, -1))
        self.assertAlmostEqual(pnl.net_usdt, 0.0, delta=0.01)

    def test_E_tether_drop_is_zero(self):
        """E. 테더 0% → -2% → 0 (이전 원화 방식이면 -2%)"""
        pnl, _, _ = scenario(quote(1.35, 0, -1), quote(1.35, -2, -1))
        self.assertAlmostEqual(pnl.net_usdt, 0.0, delta=0.01)

    def test_F_fx_drop_is_about_zero(self):
        """F. 환율 -4.3% + 테더 -1.22 → +0.48 (SOL 실측과 같은 상황) → 약 0 (코인 몫 0.08 → 0.07 만큼)"""
        pnl, _, _ = scenario(quote(1.35, -1.22, 0.08), quote(1.35, 0.48, 0.07, fx=FX * 0.957))
        self.assertAlmostEqual(pnl.net_usdt, -0.10, delta=0.01)

    def test_G_coin_premium_deepens(self):
        """G. 코인 김프 -1% → -3% → -20.20 USDT"""
        pnl, _, _ = scenario(quote(1.35, 0, -1), quote(1.35, 0, -3))
        self.assertAlmostEqual(pnl.net_usdt, -20.20, delta=0.01)

    def test_constant_coin_premium_anything_else_moves(self):
        """코인 김프만 그대로면 가격·환율·테더가 어떻게 움직여도 0"""
        for pb2, t2, fx2 in ((0.5, 3, 1200), (2.0, -3, 1500), (1.35, 1, 1400)):
            pnl, _, _ = scenario(quote(1.35, 0.3, -1.2), quote(pb2, t2, -1.2, fx=fx2))
            self.assertAlmostEqual(pnl.net_usdt, 0.0, delta=1e-6)

    def test_A_prime_with_costs(self):
        """A′. A + 비용 (수수료, USDT 호가 간격 0.075%, 전송비 1 USDT) → +15.40 USDT (+1.54%)"""
        pnl, _, _ = scenario(quote(1.35, 0.5, -1.5, usdt_spread=0.075), quote(1.35, 0.5, 0.5, usdt_spread=0.075),
                             fees=REAL_FEES)
        self.assertAlmostEqual(pnl.net_usdt, 15.40, delta=0.01)

    def test_H_small_recovery_not_enough(self):
        """H. 코인 김프 -1% → -0.4% + 비용 → +1.20 USDT (+0.12%), 목표 1% 미달"""
        pnl, _, _ = scenario(quote(1.35, 0, -1, usdt_spread=0.075), quote(1.35, 0, -0.4, usdt_spread=0.075),
                             fees=REAL_FEES)
        self.assertAlmostEqual(pnl.net_usdt, 1.20, delta=0.01)
        self.assertLess(pnl.net_percent, 1.0)

    def test_net_equals_parts(self):
        """순손익 = 업비트 + 바이비트 + 펀딩 - 비용 (항목 합이 맞는지)"""
        pnl, _, _ = scenario(quote(1.35, 0.5, -1.5, usdt_spread=0.075), quote(1.4, 0.2, 0.5, usdt_spread=0.075),
                             fees=REAL_FEES)
        self.assertAlmostEqual(pnl.net_usdt, pnl.gross_usdt + pnl.funding_usdt - pnl.total_cost_usdt)

    def test_round_trip_fee(self):
        """왕복 수수료 = 업비트 4번 × 0.05% + 바이비트 2번 × 0.055% = 0.31%"""
        self.assertAlmostEqual(REAL_FEES.round_trip_trade_fee_percent(), 0.31)

    def test_estimated_exit_premium(self):
        """-1 + 1 + 0.31 + 0.1(전송 1/1000) + 0.075(USDT 호가) + 0.05(여유) = +0.535%"""
        fees = FeeSettings(0.0005, 0.00055, 1.0, 0.05)
        self.assertAlmostEqual(estimated_exit_premium(-1.0, 1.0, 1000, 0.0, fees, 0.075), 0.535)


class LiquidationTest(unittest.TestCase):
    """강제청산 가격 / 증거금 추가 계산"""

    def test_liquidation_price(self):
        """1.35 숏: 1배 → 약 2.69 (2배), 3배 → 약 1.793 (+32.8%)"""
        self.assertAlmostEqual(liquidation_price(1.35, 1, 0.005), 1.35 * 1.995)
        self.assertAlmostEqual(liquidation_price(1.35, 3, 0.005) / 1.35 - 1, 1 / 3 - 0.005)



if __name__ == "__main__":
    unittest.main()

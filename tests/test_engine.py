"""
페이퍼 트레이딩 + 엔진 흐름 테스트 (가짜 시세 사용, 인터넷 필요 없음)

실행: python -m unittest discover -s tests -v
"""
import time
import unittest
from typing import Dict, List, Optional

from kimp.config import CoinParams, FeeSettings, Settings
from kimp.db import ExitReason, Fill, FundingEvent, Position, PositionStatus, PremiumSnapshot, make_session_factory
from kimp.engine import Engine
from kimp.fx import FxProvider
from kimp.market_data import FundingInfo
from kimp.paper_broker import KRW, USDT, EntryRejected, PaperBroker, coin_key
from kimp.premium import OrderBook, Quote

FEES = FeeSettings(upbit_trade_fee_rate=0.0005, bybit_taker_fee_rate=0.00055,
                   transfer_fee_usdt_per_cycle=1.0, slippage_buffer_percent=0.05)
FX = 1400.0


def make_quote(premium: float, bybit_price: float = 1.0, fx: float = FX, depth: float = 1e6,
               coin: str = "XRP", usdt_krw: Optional[float] = None) -> Quote:
    """
    원하는 김프(%)를 가진 가짜 시세 만들기 (호가 간격은 양쪽 ±0.01% 로 아주 좁게)

    예) premium=-1.0, 바이비트 1.0, 환율 1,400 → 업비트 가운데 가격 1,386원
    """
    upbit_mid = bybit_price * fx * (1 + premium / 100)
    s = 0.0001
    return Quote(
        coin=coin,
        upbit=OrderBook(bids=[(upbit_mid * (1 - s), depth)], asks=[(upbit_mid * (1 + s), depth)]),
        bybit=OrderBook(bids=[(bybit_price * (1 - s), depth)], asks=[(bybit_price * (1 + s), depth)]),
        fx_krw=fx,
        usdt_krw=usdt_krw,
    )


class FakeMarket:
    """진짜 거래소 대신 쓰는 가짜: 테스트가 넣어둔 시세/펀딩비를 그대로 돌려준다"""

    def __init__(self):
        """시세와 펀딩비는 테스트에서 직접 넣는다"""
        self.quotes: Dict[str, Quote] = {}
        self.funding: Optional[FundingInfo] = None

    def fetch_quotes(self, coins: List[str], fx_krw: float) -> Dict[str, Quote]:
        """넣어둔 시세 중 요청한 코인만 돌려준다 (환율은 시세를 만들 때 이미 넣었음)"""
        return {c: q for c, q in self.quotes.items() if c in coins}

    def fetch_funding(self, coin: str) -> Optional[FundingInfo]:
        """넣어둔 펀딩비 정보"""
        return self.funding

    def round_qty(self, coin: str, qty: float) -> float:
        """0.1개 단위로 내림 (바이비트 XRP 와 같음)"""
        return float(int(qty * 10) / 10)

    def min_qty(self, coin: str) -> float:
        """최소 0.1개"""
        return 0.1


def make_settings(**overrides) -> Settings:
    """테스트용 설정 (메모리 DB, 진입 기준 -0.5%, 포지션 1개)"""
    base = dict(coins=["XRP"], entry_premium_threshold_percent=-0.5, target_net_profit_percent=1.0,
                stop_loss_percent=-5.0, position_size_usdt=500, max_open_positions=1,
                fees=FEES, funding_refresh_sec=0, snapshot_interval_sec=0,
                paper_initial_krw=10_000_000, paper_initial_usdt=5_000, db_path=":memory:")
    base.update(overrides)
    return Settings(**base)


def fixed_fx(rate: float = FX) -> FxProvider:
    """항상 같은 환율을 주는 공급기"""
    return FxProvider(fixed_rate=rate)


class PaperBrokerTest(unittest.TestCase):
    """페이퍼 체결과 잔고"""

    def setUp(self):
        """메모리 DB + 시작 잔고"""
        self.sf = make_session_factory(":memory:")
        self.broker = PaperBroker(FEES, max_slippage_percent=0.3)
        with self.sf() as s:
            self.broker.ensure_initial_balances(s, 10_000_000, 5_000)

    def test_round_trip_balances_match_pnl(self):
        """진입 → 청산 후 실제 잔고 변화가 계산한 손익과 같은지"""
        with self.sf() as s:
            pos = self.broker.open_hedge(s, make_quote(-1.0), 500)
            # 금액 헷징: 역프라 업비트 개수가 더 많고, 업비트 쓴 원화 = 숏 금액 × 환율
            self.assertGreater(pos.upbit_qty, pos.bybit_qty)
            self.assertAlmostEqual(pos.upbit_qty * pos.entry_upbit_price_krw,
                                   pos.bybit_qty * pos.entry_bybit_price_usdt * FX)
            self.assertAlmostEqual(self.broker.balance(s, coin_key("XRP")), pos.upbit_qty)

            val = self.broker.value_exit(pos, make_quote(+1.0))
            pnl = self.broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)

            krw_change = self.broker.balance(s, KRW) - 10_000_000
            usdt_change = self.broker.balance(s, USDT) - 5_000
            # 원화 변화 + USDT 변화 × 환율 = 손익 (전송비는 실제 잔고에서 안 빠지니 더해서 비교)
            self.assertAlmostEqual(krw_change + usdt_change * FX, pnl.net_krw + pnl.transfer_fee_krw, places=4)
            self.assertAlmostEqual(self.broker.balance(s, coin_key("XRP")), 0)
            self.assertEqual(pos.status, PositionStatus.CLOSED)
            self.assertEqual(s.query(Fill).count(), 4)

    def test_tether_rise_is_real_krw_gain(self):
        """
        보유 중 테더 프리미엄만 1% 올라 업비트 가격이 1% 오르면
        원화가 늘고 USDT 는 그대로 → 봇도 이익으로 계산해야 한다 (concept.md 4번)
        """
        with self.sf() as s:
            pos = self.broker.open_hedge(s, make_quote(-1.0), 500)
            # 바이비트 가격·달러 환율 그대로, 업비트만 1% 오름 = 김프 -1% → 약 0%
            val = self.broker.value_exit(pos, make_quote(-1.0 + 1.0 * 0.99))
            self.assertGreater(val.pnl.upbit_leg_krw, 0)
            # 바이비트 가격은 그대로라 호가 간격(±0.01%)만큼만 손해: 500개 × 0.0002 = 약 -0.1 USDT
            self.assertLess(abs(val.pnl.bybit_leg_usdt), 0.11)
            self.assertGreater(val.pnl.gross_krw, 0)

    def test_rejects_without_side_effects(self):
        """잔고가 모자라면 아무것도 바뀌면 안 된다"""
        with self.sf() as s:
            with self.assertRaises(EntryRejected):
                self.broker.open_hedge(s, make_quote(-1.0), 100_000)  # 100,000 USDT 어치 → 부족
            self.assertEqual(s.query(Position).count(), 0)
            self.assertAlmostEqual(self.broker.balance(s, KRW), 10_000_000)
            self.assertAlmostEqual(self.broker.balance(s, USDT), 5_000)

    def test_rejects_thin_orderbook(self):
        """호가가 얇으면 진입 안 함"""
        with self.sf() as s:
            with self.assertRaises(EntryRejected):
                self.broker.open_hedge(s, make_quote(-1.0, depth=10), 500)


class EngineTest(unittest.TestCase):
    """엔진: 진입 / 익절 / 손절 / 펀딩비"""

    def setUp(self):
        """메모리 DB + 고정 환율 1,400원"""
        self.market = FakeMarket()
        self.sf = make_session_factory(":memory:")
        self.engine = Engine(make_settings(), self.market, fixed_fx(), PaperBroker(FEES, 0.3), self.sf, ["XRP"])
        self.engine.startup()

    def _positions(self):
        """DB 의 모든 포지션"""
        with self.sf() as s:
            return s.query(Position).all()

    def test_no_entry_above_threshold(self):
        """김프 -0.2% 는 기준 -0.5% 보다 높으니 진입 안 함"""
        self.market.quotes = {"XRP": make_quote(-0.2)}
        self.engine.step()
        self.assertEqual(self._positions(), [])

    def test_entry_then_take_profit(self):
        """진입 → 목표 못 미치면 유지 → 목표 넘으면 청산"""
        self.market.quotes = {"XRP": make_quote(-1.0)}
        self.engine.step()
        [pos] = self._positions()
        self.assertEqual(pos.status, PositionStatus.OPEN)
        self.assertAlmostEqual(pos.bybit_qty, 500.0)  # 500 ÷ 0.9999 = 500.05 → 0.1개 단위 내림

        # -0.2%: 김프 0.8%p 올랐지만 비용(약 0.41%) 빼면 목표 1% 안 됨 → 유지
        self.market.quotes = {"XRP": make_quote(-0.2)}
        self.engine.step()
        self.assertEqual(self._positions()[0].status, PositionStatus.OPEN)

        # +0.6%: 1.6%p 올라서 비용 빼도 1% 넘음 → 청산
        self.market.quotes = {"XRP": make_quote(+0.6)}
        self.engine.step()
        [pos] = self._positions()
        self.assertEqual(pos.status, PositionStatus.CLOSED)
        self.assertEqual(pos.exit_reason, ExitReason.TAKE_PROFIT)
        self.assertGreaterEqual(pos.net_pnl_percent, 1.0)
        self.assertGreater(pos.net_pnl_krw, 0)

    def test_stop_loss_and_no_immediate_reentry(self):
        """김프 급락 → 손절. 손절 직후엔 김프가 낮아도 다시 안 들어간다"""
        self.market.quotes = {"XRP": make_quote(-1.0)}
        self.engine.step()
        self.market.quotes = {"XRP": make_quote(-7.0)}
        self.engine.step()
        self.engine.step()  # 재진입 대기 시간 중
        [pos] = self._positions()
        self.assertEqual(pos.exit_reason, ExitReason.STOP_LOSS)

    def test_entry_rules_saved_and_fixed(self):
        """진입 때 기준을 포지션에 저장하고, 나중에 설정을 바꿔도 그 기준으로 청산한다"""
        self.market.quotes = {"XRP": make_quote(-1.0)}
        self.engine.step()
        [pos] = self._positions()
        self.assertEqual((pos.entry_threshold_percent, pos.target_net_profit_percent, pos.stop_loss_percent),
                         (-0.5, 1.0, -5.0))

        # 들고 있는 중에 목표를 5% 로 바꿈 → 이 포지션은 여전히 목표 1% 로 청산돼야 한다
        self.engine.s = make_settings(target_net_profit_percent=5.0)
        self.market.quotes = {"XRP": make_quote(+0.6)}
        self.engine.step()
        [pos] = self._positions()
        self.assertEqual(pos.exit_reason, ExitReason.TAKE_PROFIT)
        self.assertLess(pos.net_pnl_percent, 5.0)

    def test_premium_split_and_funding_krw_saved(self):
        """업비트 USDT 가격이 있으면 김프를 테더 몫/코인 몫으로 나눠 저장, 펀딩비도 원화로 저장"""
        usdt = FX * 1.005  # 테더 +0.5%
        self.market.quotes = {"XRP": make_quote(-1.0, usdt_krw=usdt)}
        self.engine.step()
        [pos] = self._positions()
        self.assertAlmostEqual(pos.entry_tether_premium, 0.5, places=6)
        self.assertAlmostEqual((1 + pos.entry_tether_premium / 100) * (1 + pos.entry_coin_premium / 100),
                               1 + pos.entry_premium / 100)

        # 펀딩비 1번 받은 뒤 청산
        now_ms = int(time.time() * 1000)
        self.market.funding = FundingInfo(rate=0.0001, next_funding_ms=now_ms - 1000, mark_price=1.0,
                                          interval_hours=8.0)
        self.engine.step()
        self.engine.step()
        self.market.quotes = {"XRP": make_quote(+0.6, usdt_krw=usdt)}
        self.engine.step()
        [pos] = self._positions()
        self.assertEqual(pos.status, PositionStatus.CLOSED)
        self.assertAlmostEqual(pos.funding_krw, pos.funding_usdt * FX)
        self.assertIsNotNone(pos.exit_coin_premium)

        # 김프 기록에도 테더 몫 / 코인 몫 / 펀딩비율이 남는다
        with self.sf() as s:
            snap = s.query(PremiumSnapshot).order_by(PremiumSnapshot.id.desc()).first()
            self.assertAlmostEqual(snap.tether_premium, 0.5, places=6)
            self.assertIsNotNone(snap.coin_premium)
            self.assertEqual(snap.funding_rate, 0.0001)
            self.assertEqual(snap.funding_interval_hours, 8.0)
            self.assertEqual(snap.next_funding_ms, now_ms - 1000)

    def test_funding_applied_once(self):
        """펀딩비는 정산 한 번에 딱 한 번만 반영"""
        self.market.quotes = {"XRP": make_quote(-1.0)}
        self.engine.step()

        now_ms = int(time.time() * 1000)
        # 1) 다음 정산 정보 저장
        self.market.funding = FundingInfo(rate=0.0001, next_funding_ms=now_ms - 1000, mark_price=1.0)
        self.engine.step()
        # 2) 정산 시각이 지났으니 반영
        self.engine.step()
        # 3) 거래소가 같은 시각을 또 줘도 다시 반영하면 안 됨
        self.engine.step()
        with self.sf() as s:
            self.assertEqual(s.query(FundingEvent).count(), 1)
            pos = s.query(Position).one()
            self.assertAlmostEqual(pos.funding_usdt, 500.0 * 1.0 * 0.0001)


class FxGuardTest(unittest.TestCase):
    """달러 환율이 없거나 오래됐을 때의 동작"""

    def _engine(self, fx: FxProvider):
        """주어진 환율 공급기로 엔진 만들기"""
        market = FakeMarket()
        sf = make_session_factory(":memory:")
        engine = Engine(make_settings(), market, fx, PaperBroker(FEES, 0.3), sf, ["XRP"])
        engine.startup()
        return engine, market, sf

    def test_no_fx_no_trading(self):
        """환율을 한 번도 못 받았으면 아무것도 안 한다"""
        def fail():
            raise RuntimeError("네트워크 오류")

        engine, market, sf = self._engine(FxProvider(fetcher=fail))
        market.quotes = {"XRP": make_quote(-3.0)}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).count(), 0)

    def test_stale_fx_blocks_entry_but_allows_exit(self):
        """환율이 오래되면 새 진입은 막고, 이미 가진 포지션 청산은 계속한다"""
        now = [0.0]
        ok = [True]

        def fetcher():
            if not ok[0]:
                raise RuntimeError("조회 실패")
            return FX

        fx = FxProvider(refresh_sec=10, max_stale_sec=100, fetcher=fetcher, clock=lambda: now[0])
        engine, market, sf = self._engine(fx)

        # 환율 정상 → 진입
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()

        # 이후 조회가 계속 실패하고 200초 지남 → 오래된 값
        ok[0] = False
        now[0] = 200.0
        self.assertTrue(fx.get().is_stale)

        # 청산 조건이 되면 청산은 된다
        market.quotes = {"XRP": make_quote(+1.0)}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).filter_by(status=PositionStatus.CLOSED).count(), 1)

        # 재진입 대기 시간이 없어도, 오래된 환율이면 새 진입은 안 된다
        engine._reentry_allowed_at.clear()
        market.quotes = {"XRP": make_quote(-3.0)}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).count(), 1)


class CoinParamsEngineTest(unittest.TestCase):
    """코인별 설정(진입 기준, 포지션 규모, 목표)이 실제로 쓰이는지"""

    def setUp(self):
        """XRP 는 공통값(기준 -0.5%), SOL 은 기준 -2.0% · 규모 200 USDT · 목표 0.5%"""
        self.market = FakeMarket()
        self.sf = make_session_factory(":memory:")
        base = make_settings(coins=["XRP", "SOL"], max_open_positions=2)
        sol = CoinParams(entry_premium_threshold_percent=-2.0, target_net_profit_percent=0.5,
                         stop_loss_percent=-5.0, position_size_usdt=200,
                         overridden=("entry_premium_threshold_percent", "target_net_profit_percent",
                                     "position_size_usdt"))
        settings = Settings(**{**base.__dict__, "coin_params": {"XRP": base.default_params(), "SOL": sol}})
        self.engine = Engine(settings, self.market, fixed_fx(), PaperBroker(FEES, 0.3), self.sf, ["XRP", "SOL"])
        self.engine.startup()

    def _open(self):
        """열린 포지션을 {코인: 포지션} 으로"""
        with self.sf() as s:
            return {p.coin: p for p in s.query(Position).filter_by(status=PositionStatus.OPEN)}

    def test_per_coin_entry_threshold(self):
        """김프 -1%: XRP(기준 -0.5%) 진입, SOL(기준 -2.0%) 미진입"""
        self.market.quotes = {"XRP": make_quote(-1.0), "SOL": make_quote(-1.0, coin="SOL")}
        self.engine.step()
        self.assertEqual(set(self._open()), {"XRP"})

        # SOL 이 -2.5% 가 되면 진입, 규모는 SOL 설정(200 USDT)
        self.market.quotes = {"XRP": make_quote(-1.0), "SOL": make_quote(-2.5, coin="SOL")}
        self.engine.step()
        sol = self._open()["SOL"]
        self.assertAlmostEqual(sol.bybit_qty * sol.entry_bybit_price_usdt, 200, delta=1)

    def test_per_coin_target(self):
        """SOL 은 목표 0.5% 라서, 공통 목표 1% 에 못 미쳐도 청산된다"""
        self.market.quotes = {"SOL": make_quote(-2.5, coin="SOL")}
        self.engine.step()
        # -2.5% → -1.5%: 1%p 올랐지만 비용(수수료 0.21% + 전송비 1/200=0.5% + 호가) 빼면 약 0.27% → 유지
        self.market.quotes = {"SOL": make_quote(-1.5, coin="SOL")}
        self.engine.step()
        self.assertIn("SOL", self._open())
        # -1.2%: 1.3%p - 비용 ≈ 0.57% → SOL 목표 0.5% 넘음 → 청산
        self.market.quotes = {"SOL": make_quote(-1.2, coin="SOL")}
        self.engine.step()
        self.assertNotIn("SOL", self._open())


if __name__ == "__main__":
    unittest.main()

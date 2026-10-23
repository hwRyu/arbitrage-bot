"""
페이퍼 트레이딩 + 엔진 흐름 테스트 (가짜 시세 사용, 인터넷 필요 없음)

실행: python -m unittest discover -s tests -v
"""
import logging
import time
import unittest
from typing import Dict, List, Optional

from kimp.config import CoinParams, FeeSettings, Settings
from kimp.db import ExitReason, Fill, FundingEvent, Position, PositionStatus, PremiumSnapshot, make_session_factory
from kimp.engine import Engine
from kimp.fx import FxProvider
from kimp.market_data import FundingInfo
from kimp.paper_broker import BYBIT_USDT, UPBIT_USDT, EntryRejected, PaperBroker, coin_key
from kimp.premium import OrderBook, Quote

FEES = FeeSettings(upbit_trade_fee_rate=0.0005, bybit_taker_fee_rate=0.00055,
                   transfer_fee_usdt_per_cycle=1.0, slippage_buffer_percent=0.05)
USDT_KRW = 1400.0


def make_quote(premium: float, bybit_price: float = 1.0, usdt_krw: float = USDT_KRW, depth: float = 1e9,
               coin: str = "XRP", fx: Optional[float] = None, bybit_high: Optional[float] = None) -> Quote:
    """
    원하는 코인 몫 김프(%)를 가진 가짜 시세 (호가 간격은 세 군데 모두 ±0.01% 로 아주 좁게)

    예) premium=-1.0, 바이비트 1.0, 업비트 USDT 1,400원 → 업비트 코인 가운데 가격 1,386원
    """
    upbit_mid = bybit_price * usdt_krw * (1 + premium / 100)
    s = 0.0001

    def book(p):
        return OrderBook(bids=[(p * (1 - s), depth)], asks=[(p * (1 + s), depth)])

    return Quote(coin=coin, upbit=book(upbit_mid), bybit=book(bybit_price), usdt=book(usdt_krw), fx_krw=fx,
                 bybit_high=bybit_high)


class FakeMarket:
    """진짜 거래소 대신 쓰는 가짜: 테스트가 넣어둔 시세/펀딩비를 그대로 돌려준다"""

    def __init__(self):
        self.quotes: Dict[str, Quote] = {}
        self.funding: Optional[FundingInfo] = None
        # 주문 직전 '확인 시세' 를 다르게 주고 싶을 때. 코인 하나만 다시 받는 호출에만 쓰이므로
        # 이걸 쓰는 테스트는 엔진 코인을 둘 이상으로 만들어야 스캔 조회와 구분된다
        self.confirm_quotes: Dict[str, Quote] = {}

    def fetch_quotes(self, coins: List[str], fx_krw=None) -> Dict[str, Quote]:
        """넣어둔 시세 중 요청한 코인만. 코인 하나짜리 재조회에 confirm_quotes 가 있으면 그걸 준다"""
        if len(coins) == 1 and coins[0] in self.confirm_quotes:
            return {coins[0]: self.confirm_quotes[coins[0]]}
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
    """테스트용 설정 (진입 -0.5%, 목표 1%, 포지션 500 USDT 고정, 1개, 시작 자본 10,000 USDT)"""
    base = dict(coins=["XRP"], entry_premium_threshold_percent=-0.5, target_net_profit_percent=1.0,
                stop_loss_percent=-5.0, position_size_usdt=500, max_open_positions=1,
                fees=FEES, funding_refresh_sec=0, snapshot_interval_sec=0, paper_initial_total_usdt=10_000,
                db_path=":memory:")
    base.update(overrides)
    return Settings(**base)


def make_engine(settings: Settings, market: FakeMarket, fx=None):
    """엔진 + 메모리 DB (시작 자본은 목표 비율대로 자동 분배)"""
    sf = make_session_factory(":memory:")
    broker = PaperBroker(FEES, 0.3, leverage=settings.bybit_leverage, mmr=settings.bybit_mmr)
    engine = Engine(settings, market, fx, broker, sf, list(settings.coins))
    engine.startup()
    return engine, sf, broker


class PaperBrokerTest(unittest.TestCase):
    """페이퍼 체결과 잔고"""

    def setUp(self):
        """메모리 DB + 시작 자본 10,000 USDT 를 50:50 으로"""
        self.sf = make_session_factory(":memory:")
        self.broker = PaperBroker(FEES, max_slippage_percent=0.3)
        with self.sf() as s:
            self.broker.ensure_initial_balances(s, 10_000, 0.5)

    def test_initial_split_by_ratio(self):
        """시작 자본을 목표 비율대로 나눠 넣고, 두 번째부터는 안 넣는다"""
        with self.sf() as s:
            self.assertAlmostEqual(self.broker.balance(s, UPBIT_USDT), 5_000)
            self.assertAlmostEqual(self.broker.balance(s, BYBIT_USDT), 5_000)
            self.assertFalse(self.broker.ensure_initial_balances(s, 99, 0.5))

    def test_round_trip_balances_match_pnl(self):
        """진입 → 청산 후 실제 USDT 잔고 변화가 계산한 손익과 같은지 (전송비는 잔고에서 안 빠짐)"""
        with self.sf() as s:
            pos = self.broker.open_hedge(s, make_quote(-1.0), 500)
            self.assertAlmostEqual(pos.usdt_sold, 500 * 0.9999)  # 숏 금액만큼 USDT 를 판다
            self.assertAlmostEqual(self.broker.balance(s, coin_key("XRP")), pos.upbit_qty)

            val = self.broker.value_exit(pos, make_quote(+1.0))
            pnl = self.broker.close_hedge(s, pos, val, ExitReason.TAKE_PROFIT)

            change = self.broker.balance(s, UPBIT_USDT) + self.broker.balance(s, BYBIT_USDT) - 10_000
            self.assertLess(abs(self.broker.balance(s, "upbit:KRW")), 1e-6)  # 자투리 원화 거의 0
            self.assertAlmostEqual(change, pnl.net_usdt + pnl.transfer_fee_usdt, places=6)
            self.assertAlmostEqual(self.broker.balance(s, coin_key("XRP")), 0)
            self.assertEqual(s.query(Fill).count(), 6)  # USDT 매도·코인 매수·숏 / 코인 매도·USDT 매수·숏 닫기

    def test_usdt_price_move_does_not_matter(self):
        """업비트 USDT 가격이 3% 떨어져도(환율 하락) 코인 김프가 그대로면 손익 ≈ 0 (호가 간격만큼)"""
        with self.sf() as s:
            pos = self.broker.open_hedge(s, make_quote(-1.0), 500)
            val = self.broker.value_exit(pos, make_quote(-1.0, usdt_krw=USDT_KRW * 0.97))
            self.assertAlmostEqual(val.pnl.gross_usdt, 0.0, delta=0.5)

    def test_rejects_without_side_effects(self):
        """잔고가 모자라면 아무것도 안 바뀌고, 얼마나 더 필요한지 알려준다"""
        with self.sf() as s:
            with self.assertRaises(EntryRejected) as ctx:
                self.broker.open_hedge(s, make_quote(-1.0), 20_000)
            self.assertIn("더 필요", str(ctx.exception))
            self.assertEqual(s.query(Position).count(), 0)
            self.assertAlmostEqual(self.broker.balance(s, UPBIT_USDT), 5_000)

    def test_rejects_thin_orderbook(self):
        """호가가 얇으면 진입 안 함"""
        with self.sf() as s:
            with self.assertRaises(EntryRejected):
                self.broker.open_hedge(s, make_quote(-1.0, depth=10), 500)

    def test_leverage_margin_and_liquidation_price(self):
        """3배면 증거금은 숏 금액의 1/3, 강제청산 가격은 약 +32.8%"""
        broker = PaperBroker(FEES, 0.3, leverage=3, mmr=0.005)
        with self.sf() as s:
            pos = broker.open_hedge(s, make_quote(-1.0), 500)
            self.assertAlmostEqual(pos.bybit_margin_usdt, pos.bybit_qty * pos.entry_bybit_price_usdt / 3)
            self.assertAlmostEqual(pos.liquidation_price / pos.entry_bybit_price_usdt - 1, 1 / 3 - 0.005)


class EngineTest(unittest.TestCase):
    """엔진: 진입 / 익절 / 손절 / 펀딩비 / 진입 때 기준 고정"""

    def setUp(self):
        self.market = FakeMarket()
        self.engine, self.sf, self.broker = make_engine(make_settings(), self.market)

    def _positions(self):
        with self.sf() as s:
            return s.query(Position).all()

    def test_no_fx_still_trades(self):
        """환율 공급기가 아예 없어도(표시용) 매매한다"""
        self.market.quotes = {"XRP": make_quote(-1.0)}
        self.engine.step()
        self.assertEqual(len(self._positions()), 1)

    def test_fx_failing_from_start_still_enters(self):
        """
        봇을 켠 순간부터 환율 조회가 계속 실패해도 첫 진입을 한다

        FxProvider 는 한 번도 성공 못 하면 None 을 돌려준다. 그 None 이 끝까지 흘러가도
        막히는 데가 없어야 한다 — 환율은 테더 프리미엄·사이트식 김프를 '보여주는' 데만 쓴다.
        """
        def always_fail():
            raise RuntimeError("yfinance 가 빈 환율 데이터를 돌려줬습니다")

        fx = FxProvider(refresh_sec=0, fetcher=always_fail)
        self.assertIsNone(fx.get())                       # 받아온 값이 없는 상태로 시작
        engine, sf, _ = make_engine(make_settings(), self.market, fx=fx)
        self.market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()

        with sf() as s:
            pos = s.query(Position).one()
            snap = s.query(PremiumSnapshot).one()
        self.assertEqual(pos.status, PositionStatus.OPEN)  # 진입은 됐고
        self.assertLess(pos.entry_premium, 0)              # 진입 김프(매도호가 기준)가 제대로 찍혔고
        self.assertIsNone(pos.entry_fx_krw)                # 환율에 기대는 칸만 비어 있다
        self.assertIsNone(pos.entry_tether_premium)
        self.assertIsNone(snap.tether_premium)
        self.assertIsNone(snap.site_premium)
        self.assertAlmostEqual(snap.mid_premium, -1.0, places=6)   # 봇 김프는 멀쩡하다

    def test_entry_skipped_when_confirm_quote_is_worse(self):
        """스캔 시세는 기준 통과, 주문 직전 확인 시세는 미달 → 진입 안 함 (한순간 튄 값)"""
        from kimp.db import Decision
        from sqlalchemy import select
        engine, sf, _ = make_engine(make_settings(coins=["XRP", "DOGE"]), self.market)  # 코인 둘 → 확인 조회 구분
        self.market.quotes = {"XRP": make_quote(-1.0)}
        self.market.confirm_quotes = {"XRP": make_quote(-0.2)}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).count(), 0)
            stages = [d.stage for d in s.scalars(select(Decision))]
        self.assertEqual(stages, ["confirm_fail"])
        # 확인 시세도 통과하면 들어간다
        self.market.confirm_quotes = {"XRP": make_quote(-0.9)}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).count(), 1)

    def test_take_profit_skipped_when_confirm_quote_is_worse(self):
        """익절 조건이 스캔 시세로는 되지만 확인 시세로는 안 되면 들고 간다 (IRYS #3 사례)"""
        from kimp.db import Decision
        from sqlalchemy import select
        engine, sf, _ = make_engine(make_settings(coins=["XRP", "DOGE"]), self.market)  # 코인 둘 → 확인 조회 구분
        self.market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        self.market.quotes = {"XRP": make_quote(0.8)}        # 스캔: 목표 넘음
        self.market.confirm_quotes = {"XRP": make_quote(-0.5)}  # 1초 뒤: 도로 내려옴
        engine.step()
        with sf() as s:
            pos = s.query(Position).one()
            d = [x for x in s.scalars(select(Decision)) if x.stage == "tp_confirm_fail"]
        self.assertEqual(pos.status, PositionStatus.OPEN)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0].position_id, pos.id)
        # 확인 시세도 넘으면 청산
        self.market.confirm_quotes = {"XRP": make_quote(0.8)}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).one().exit_reason, ExitReason.TAKE_PROFIT)

    def test_no_entry_above_threshold(self):
        """김프 -0.2% 는 기준 -0.5% 보다 높으니 진입 안 함"""
        self.market.quotes = {"XRP": make_quote(-0.2)}
        self.engine.step()
        self.assertEqual(self._positions(), [])

    def test_entry_then_take_profit(self):
        """진입 → 목표 못 미치면 유지 → 넘으면 청산 (비용 약 0.55%: 수수료 0.31 + 전송 0.2 + 호가)"""
        self.market.quotes = {"XRP": make_quote(-1.0)}
        self.engine.step()
        [pos] = self._positions()
        self.assertEqual(pos.bybit_qty, 500.0)

        self.market.quotes = {"XRP": make_quote(0.2)}  # 1.2%p 올랐지만 비용 빼면 1% 안 됨
        self.engine.step()
        self.assertEqual(self._positions()[0].status, PositionStatus.OPEN)

        self.market.quotes = {"XRP": make_quote(0.8)}  # 1.8%p → 넘음
        self.engine.step()
        [pos] = self._positions()
        self.assertEqual(pos.exit_reason, ExitReason.TAKE_PROFIT)
        self.assertGreaterEqual(pos.net_pnl_percent, 1.0)
        self.assertGreater(pos.usdt_bought, pos.usdt_sold)

    def test_stop_loss_and_no_immediate_reentry(self):
        """김프 급락 → 손절, 손절 직후엔 다시 안 들어감"""
        self.market.quotes = {"XRP": make_quote(-1.0)}
        self.engine.step()
        self.market.quotes = {"XRP": make_quote(-7.0)}
        self.engine.step()
        self.engine.step()
        [pos] = self._positions()
        self.assertEqual(pos.exit_reason, ExitReason.STOP_LOSS)

    def test_entry_rules_saved_and_fixed(self):
        """진입 때 기준을 저장하고, 나중에 설정을 바꿔도 그 기준으로 청산"""
        self.market.quotes = {"XRP": make_quote(-1.0)}
        self.engine.step()
        [pos] = self._positions()
        self.assertEqual((pos.entry_threshold_percent, pos.target_net_profit_percent, pos.stop_loss_percent),
                         (-0.5, 1.0, -5.0))
        self.engine.s = make_settings(target_net_profit_percent=5.0)
        self.market.quotes = {"XRP": make_quote(0.8)}
        self.engine.step()
        self.assertEqual(self._positions()[0].exit_reason, ExitReason.TAKE_PROFIT)

    def test_funding_applied_once(self):
        """펀딩비는 정산 한 번에 딱 한 번만"""
        self.market.quotes = {"XRP": make_quote(-1.0)}
        self.engine.step()
        now_ms = int(time.time() * 1000)
        self.market.funding = FundingInfo(rate=0.0001, next_funding_ms=now_ms - 1000, mark_price=1.0)
        self.engine.step()
        self.engine.step()
        self.engine.step()
        with self.sf() as s:
            self.assertEqual(s.query(FundingEvent).count(), 1)
            self.assertAlmostEqual(s.query(Position).one().funding_usdt, 500 * 1.0 * 0.0001, delta=1e-4)

    def test_snapshot_has_bot_and_site_premium(self):
        """김프 기록에 봇 김프(코인 몫)와 사이트식 김프·테더 프리미엄이 같이 남는다"""
        self.market.quotes = {"XRP": make_quote(-0.2, fx=USDT_KRW / 1.005)}  # 테더 +0.5%
        self.engine.step()
        with self.sf() as s:
            snap = s.query(PremiumSnapshot).one()
            self.assertAlmostEqual(snap.mid_premium, -0.2, places=6)
            self.assertAlmostEqual(snap.tether_premium, 0.5, places=6)
            self.assertAlmostEqual(snap.site_premium, (1.005 * 0.998 - 1) * 100, places=6)


class LeverageAndRebalanceTest(unittest.TestCase):
    """레버리지 / 강제청산 / 리밸런싱 / 자동 포지션 크기"""

    def test_target_share(self):
        """업비트 목표 비율: 1배 43.5%, 3배 69.8% (여유분 30%)"""
        self.assertAlmostEqual(make_settings().upbit_target_share(), 1 / 2.3)
        self.assertAlmostEqual(make_settings(bybit_leverage=3).upbit_target_share(), 3 / 4.3)

    def test_liquidation_closes_both_legs(self):
        """3배: 바이비트 고가가 강제청산 가격에 닿으면 증거금을 잃고 업비트 코인도 즉시 정리"""
        market = FakeMarket()
        engine, sf, broker = make_engine(make_settings(bybit_leverage=3), market)
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            pos = s.query(Position).one()
            liq, margin = pos.liquidation_price, pos.bybit_margin_usdt
        market.quotes = {"XRP": make_quote(-1.0, bybit_price=1.30, bybit_high=liq * 1.001)}
        engine.step()
        with sf() as s:
            pos = s.query(Position).one()
            self.assertEqual(pos.exit_reason, ExitReason.LIQUIDATION)
            self.assertAlmostEqual(pos.bybit_leg_usdt, -margin)
            self.assertAlmostEqual(broker.balance(s, coin_key("XRP")), 0)

    def test_liquidation_warning(self):
        """강제청산까지 15% 안쪽이면 증거금 추가 경고"""
        market = FakeMarket()
        engine, sf, _ = make_engine(make_settings(bybit_leverage=3), market)
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with self.assertLogs("kimp.engine", level=logging.WARNING) as logs:
            market.quotes = {"XRP": make_quote(-1.0, bybit_price=1.20)}  # 강제청산(약 1.328)까지 약 10.6%
            engine.step()
        self.assertTrue(any("증거금 약" in m for m in logs.output))

    def test_rebalance_alert_at_start(self):
        """(자동 송금 끔) 시작할 때 업비트에만 USDT 가 있으면 바이비트로 옮기라고 알리기만 한다"""
        settings = make_settings(paper_auto_rebalance=False)
        sf = make_session_factory(":memory:")
        broker = PaperBroker(FEES, 0.3)
        with sf() as s:
            broker.ensure_initial_balances(s, 10_000, 1.0)  # 전부 업비트
        engine = Engine(settings, FakeMarket(), None, broker, sf, ["XRP"])
        with self.assertLogs("kimp.engine", level=logging.WARNING) as logs:
            engine.startup()
        msg = next(m for m in logs.output if "리밸런싱" in m)
        self.assertIn("업비트 → 바이비트", msg)
        self.assertIn(f"{10_000 * (1 - settings.upbit_target_share()):,.2f}", msg)

    def _engine_all_on_upbit(self, **overrides):
        """시작 자본 10,000 USDT 가 전부 업비트에 있는 엔진"""
        settings = make_settings(**overrides)
        market = FakeMarket()
        sf = make_session_factory(":memory:")
        broker = PaperBroker(FEES, 0.3)
        with sf() as s:
            broker.ensure_initial_balances(s, 10_000, 1.0)
        return Engine(settings, market, None, broker, sf, ["XRP"]), market, sf, broker

    def test_auto_transfer_at_start_once(self):
        """
        (페이퍼 자동 송금) 시작 때 비율이 깨져 있으면 한 번만 옮긴다

        업비트 → 바이비트는 업비트 USDT(TRX) 출금이 무료라 수수료 0 이다.
        예) 10,000 USDT 가 전부 업비트에 있으면 5,652.17 을 보내고 바이비트에 그대로 5,652.17 이 찍힌다.
        """
        engine, _, sf, broker = self._engine_all_on_upbit()
        engine.startup()
        import math

        from kimp.broker import TRANSFER_DECIMALS
        move = math.floor(10_000 * (1 - engine.s.upbit_target_share()) * 10 ** TRANSFER_DECIMALS)             / 10 ** TRANSFER_DECIMALS
        fee = engine.s.transfer_fee_usdt(True)
        self.assertEqual(fee, 0.0)
        with sf() as s:
            self.assertAlmostEqual(broker.balance(s, UPBIT_USDT), 10_000 - move, places=6)
            self.assertAlmostEqual(broker.balance(s, BYBIT_USDT), move - fee, places=6)
        self.assertEqual(len(engine.transfers), 1)

    def test_transfer_fee_differs_by_direction(self):
        """
        송금 수수료는 보내는 거래소가 떼니까 방향마다 다르다

        업비트 → 바이비트: 업비트 USDT(TRX) 출금 무료      → 0
        바이비트 → 업비트: 바이비트 USDT(TRC20) 출금 수수료 → 1.3
        """
        settings = make_settings()
        self.assertEqual(settings.transfer_fee_usdt(True), 0.0)
        self.assertEqual(settings.transfer_fee_usdt(False), 1.3)
        # 설정으로 바꿀 수도 있다 (거래소가 수수료를 바꿨을 때)
        changed = make_settings(transfer_fee_upbit_to_bybit_usdt=0.5, transfer_fee_bybit_to_upbit_usdt=2.0)
        self.assertEqual(changed.transfer_fee_usdt(True), 0.5)
        self.assertEqual(changed.transfer_fee_usdt(False), 2.0)

    def test_auto_transfer_back_to_upbit_charges_bybit_fee(self):
        """
        반대 방향(바이비트 → 업비트)으로 옮길 때는 바이비트 출금 수수료 1.3 을 뗀다

        예) 10,000 USDT 가 전부 바이비트에 있으면 4,347.83 을 업비트로 보내고
            업비트에는 1.3 을 뺀 4,346.53 이 들어온다.
        """
        settings = make_settings()
        sf = make_session_factory(":memory:")
        broker = PaperBroker(FEES, 0.3)
        with sf() as s:
            broker.ensure_initial_balances(s, 10_000, 0.0)  # 전부 바이비트에
        engine = Engine(settings, FakeMarket(), None, broker, sf, ["XRP"])
        engine.startup()
        import math

        from kimp.broker import TRANSFER_DECIMALS
        # 거래소가 소수점 6자리까지만 받으므로 보낼 금액을 내려서 자른다 (업비트가 실제로 거절했다)
        move = math.floor(10_000 * settings.upbit_target_share() * 10 ** TRANSFER_DECIMALS)             / 10 ** TRANSFER_DECIMALS
        with sf() as s:
            self.assertAlmostEqual(broker.balance(s, BYBIT_USDT), 10_000 - move, places=6)
            self.assertAlmostEqual(broker.balance(s, UPBIT_USDT), move - 1.3, places=6)

    def test_no_transfer_per_trade(self):
        """비율이 괜찮으면 거래를 여러 번 해도 송금하지 않는다 (거래마다 송금 X)"""
        market = FakeMarket()
        engine, sf, _ = make_engine(make_settings(reentry_cooldown_sec=0), market)
        for _ in range(3):
            market.quotes = {"XRP": make_quote(-1.0)}
            engine.step()
            market.quotes = {"XRP": make_quote(1.5)}
            engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).filter(Position.status == PositionStatus.CLOSED).count(), 3)
        self.assertEqual(engine.transfers, [])

    def test_transfer_when_entry_blocked(self):
        """진입하려는데 바이비트가 모자라면 그때 송금하고 진입한다"""
        engine, market, sf, _ = self._engine_all_on_upbit()
        # 시작 송금은 일부러 건너뛰고(비율 확인 없이) 바로 진입 시도
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).count(), 1)
        self.assertEqual(len(engine.transfers), 1)

    def test_no_transfer_when_it_cannot_help(self):
        """맞춰도 자리가 없으면(포지션이 자본에 비해 너무 큼) 송금하지 않는다 → 수수료를 버리지 않음"""
        market = FakeMarket()
        # 시작 자본 1,000 → 업비트 435. 포지션 500 은 맞춰도 못 들어감
        engine, sf, _ = make_engine(make_settings(paper_initial_total_usdt=1_000), market)
        for _ in range(5):
            market.quotes = {"XRP": make_quote(-1.0)}
            engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).count(), 0)
        self.assertEqual(engine.transfers, [])

    def test_auto_position_size(self):
        """POSITION_SIZE_USDT 가 없으면 자본 × 업비트 비율 × 90% ÷ 최대 포지션 수"""
        market = FakeMarket()
        settings = make_settings(position_size_usdt=None, max_open_positions=3)
        engine, sf, _ = make_engine(settings, market)
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            pos = s.query(Position).one()
        expected = 10_000 * settings.upbit_target_share() * 0.9 / 3
        self.assertAlmostEqual(pos.bybit_qty * pos.entry_bybit_price_usdt, expected, delta=1)


class CoinParamsEngineTest(unittest.TestCase):
    """코인별 설정이 실제로 쓰이는지"""

    def setUp(self):
        """XRP 는 공통값, SOL 은 기준 -2.0% · 크기 200 USDT · 목표 0.5%"""
        self.market = FakeMarket()
        base = make_settings(coins=["XRP", "SOL"], max_open_positions=2)
        sol = CoinParams(entry_premium_threshold_percent=-2.0, target_net_profit_percent=0.5,
                         stop_loss_percent=-5.0, position_size_usdt=200,
                         overridden=("entry_premium_threshold_percent", "target_net_profit_percent",
                                     "position_size_usdt"))
        settings = Settings(**{**base.__dict__, "coin_params": {"XRP": base.default_params(), "SOL": sol}})
        self.engine, self.sf, _ = make_engine(settings, self.market)

    def _open(self):
        with self.sf() as s:
            return {p.coin: p for p in s.query(Position).filter_by(status=PositionStatus.OPEN)}

    def test_per_coin_entry_threshold_and_size(self):
        """김프 -1%: XRP 진입, SOL 미진입 → SOL 이 -2.5% 가 되면 200 USDT 로 진입"""
        self.market.quotes = {"XRP": make_quote(-1.0), "SOL": make_quote(-1.0, coin="SOL")}
        self.engine.step()
        self.assertEqual(set(self._open()), {"XRP"})
        self.market.quotes = {"XRP": make_quote(-1.0), "SOL": make_quote(-2.5, coin="SOL")}
        self.engine.step()
        sol = self._open()["SOL"]
        self.assertAlmostEqual(sol.bybit_qty * sol.entry_bybit_price_usdt, 200, delta=1)

    def test_per_coin_target(self):
        """SOL 은 목표 0.5% (비용: 수수료 0.31 + 전송 1/200 = 0.5 + 호가 → 약 0.85%)"""
        self.market.quotes = {"SOL": make_quote(-2.5, coin="SOL")}
        self.engine.step()
        self.market.quotes = {"SOL": make_quote(-1.5, coin="SOL")}  # 1%p → 순 약 0.15% → 유지
        self.engine.step()
        self.assertIn("SOL", self._open())
        self.market.quotes = {"SOL": make_quote(-1.0, coin="SOL")}  # 1.5%p → 순 약 0.65% → 청산
        self.engine.step()
        self.assertNotIn("SOL", self._open())


if __name__ == "__main__":
    unittest.main()


class CloseRequestTest(unittest.TestCase):
    """
    텔레그램 /close — 사람이 지시한 강제 청산

    다른 스레드(텔레그램)가 포지션을 직접 건드리면 엔진과 충돌한다.
    그래서 요청만 적어두고 다음 스캔에서 엔진이 청산한다 (청산 사유 MANUAL).
    """

    def _engine_with_positions(self, coins=("XRP", "SOL")):
        market = FakeMarket()
        settings = make_settings(coins=list(coins), max_open_positions=2, reentry_cooldown_sec=0)
        sf = make_session_factory(":memory:")
        broker = PaperBroker(FEES, 0.3)
        with sf() as s:
            broker.ensure_initial_balances(s, settings.paper_initial_total_usdt, settings.upbit_target_share())
        engine = Engine(settings, market, None, broker, sf, list(coins))
        engine.startup()
        market.quotes = {c: make_quote(-1.0, coin=c) for c in coins}
        engine.step()                     # 두 코인 모두 진입
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), len(coins))
        return engine, sf, broker, market

    def test_close_all(self):
        engine, sf, broker, market = self._engine_with_positions()
        msg = engine.request_close()
        self.assertIn("전체 2개", msg)
        # 김프를 기준(-0.5%) 위로 올려 둔다. 안 그러면 청산한 그 스캔에서 바로 재진입한다
        # (/close 는 청산만 한다. 재진입까지 막으려면 /stop 과 함께 쓴다)
        market.quotes = {c: make_quote(0.0, coin=c) for c in ("XRP", "SOL")}
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 0)
            from sqlalchemy import select
            from kimp.db import Position
            for p in s.scalars(select(Position)).all():
                self.assertEqual(p.exit_reason, ExitReason.MANUAL)

    def test_close_one_coin(self):
        engine, sf, broker, market = self._engine_with_positions()
        msg = engine.request_close(["sol"])            # 소문자도 받는다
        self.assertIn("SOL", msg)
        market.quotes = {c: make_quote(0.0, coin=c) for c in ("XRP", "SOL")}   # 재진입 안 되게
        engine.step()
        with sf() as s:
            left = broker.open_positions(s)
            self.assertEqual([p.coin for p in left], ["XRP"])   # XRP 는 그대로

    def test_close_unknown_coin(self):
        engine, sf, broker, market = self._engine_with_positions()
        self.assertIn("그 코인의 포지션이 없습니다", engine.request_close(["DOGE"]))
        market.quotes = {c: make_quote(0.0, coin=c) for c in ("XRP", "SOL")}
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 2)  # 아무것도 청산하지 않는다

    def test_close_blocks_new_entries(self):
        """
        /close 는 신규 진입도 막는다 (/stop 과 같은 효과)

        김프가 여전히 기준 아래여도 다시 들어가지 않는다. 사람이 정리하라고 했는데
        같은 스캔에서 또 들어가면 뜻과 다르기 때문이다. 다시 돌리려면 /resume.
        """
        engine, sf, broker, market = self._engine_with_positions(coins=("XRP",))
        with sf() as s:
            first = broker.open_positions(s)[0].id
        engine.request_close()
        self.assertTrue(broker.safety.manual_stop)
        engine.step()                                   # 김프는 그대로 -1% (기준 아래)
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 0)   # 재진입하지 않는다
            from sqlalchemy import select
            from kimp.db import Position
            closed = [p for p in s.scalars(select(Position)).all() if p.id == first][0]
            self.assertEqual(closed.exit_reason, ExitReason.MANUAL)

    def test_resume_lets_it_trade_again(self):
        """/close 뒤 /resume 하면 다시 진입한다"""
        engine, sf, broker, market = self._engine_with_positions(coins=("XRP",))
        engine.request_close()
        engine.step()
        broker.safety.resume()                          # 텔레그램 /resume
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 1)

    def test_close_with_no_position(self):
        market = FakeMarket()
        engine, sf, _ = make_engine(make_settings(), market)
        engine.startup()
        self.assertIn("들고 있는 포지션이 없습니다", engine.request_close())


class FundingGuardTest(unittest.TestCase):
    """
    펀딩비 안전장치 (concept.md 11번 1순위)

    우리는 숏이라 펀딩비율이 − 면 낸다. 김프 회복을 며칠 기다리는 전략이라, 내는 쪽에 오래 머물면
    김프가 그대로여도 손실만 쌓인다.
      예) 100 USDT 포지션, −0.0646%/8h → 하루 −0.194 → 5일이면 목표(1%)를 전부 먹는다
    ① 진입 차단: 펀딩비율이 기준보다 나쁘면 들어가지 않는다
    ② 누적 한도: 펀딩비로 낸 돈이 포지션 금액의 N% 를 넘으면 정리한다
    """

    def _engine(self, funding_rate=None, interval_hours=8.0, **overrides):
        market = FakeMarket()
        if funding_rate is not None:
            market.funding = FundingInfo(rate=funding_rate, next_funding_ms=10 ** 14,
                                         mark_price=1.0, interval_hours=interval_hours)
        else:
            market.funding = None
        engine, sf, broker = make_engine(make_settings(**overrides), market)
        return engine, sf, broker, market

    # ---------------- ① 진입 차단 ----------------
    def test_blocks_entry_when_funding_too_negative(self):
        """−0.08%/8h (기준 −0.05%) → 진입하지 않는다"""
        engine, sf, broker, market = self._engine(funding_rate=-0.0008,
                                                  entry_min_funding_8h_percent=-0.05)
        engine.startup()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 0)

    def test_allows_entry_when_funding_ok(self):
        """+0.01%/8h → 받는 쪽이라 진입한다"""
        engine, sf, broker, market = self._engine(funding_rate=0.0001,
                                                  entry_min_funding_8h_percent=-0.05)
        engine.startup()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 1)

    def test_interval_is_converted_to_8h(self):
        """
        정산 주기가 1시간이면 8배로 환산해서 비교한다

        1시간마다 −0.01% = 8시간 기준 −0.08% → 기준 −0.05% 보다 나쁘므로 막아야 한다.
        환산을 안 하면 −0.01% 로 보여서 통과해 버린다.
        """
        engine, sf, broker, market = self._engine(funding_rate=-0.0001, interval_hours=1.0,
                                                  entry_min_funding_8h_percent=-0.05)
        engine.startup()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 0)
        self.assertAlmostEqual(engine._funding_8h_percent("XRP"), -0.08, places=6)

    def test_unknown_funding_does_not_block(self):
        """펀딩비를 못 받으면 막지 않는다 (데이터가 없다고 기회를 버리지는 않는다)"""
        engine, sf, broker, market = self._engine(funding_rate=None,
                                                  entry_min_funding_8h_percent=-0.05)
        engine.startup()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 1)

    def test_guard_off_by_none(self):
        """기준을 비우면(None) 펀딩비를 보지 않는다"""
        engine, sf, broker, market = self._engine(funding_rate=-0.01,
                                                  entry_min_funding_8h_percent=None)
        engine.startup()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 1)

    # ---------------- ② 누적 한도 ----------------
    def test_closes_when_funding_cost_exceeds_limit(self):
        """펀딩비로 포지션 금액의 1% 를 쓰면 정리한다 (목표에 못 닿았어도)"""
        engine, sf, broker, market = self._engine(funding_rate=0.0001, max_funding_cost_percent=1.0,
                                                  entry_min_funding_8h_percent=None)
        engine.startup()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            pos = broker.open_positions(s)[0]
            notional = pos.bybit_qty * pos.entry_bybit_price_usdt
            pos.funding_usdt = -notional * 0.011        # 1.1% 를 펀딩비로 냈다
            s.commit()
            self.assertAlmostEqual(engine._funding_cost_percent(pos), 1.1, places=6)
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 0)
            from sqlalchemy import select
            from kimp.db import Position
            self.assertEqual(s.scalars(select(Position)).all()[0].exit_reason, ExitReason.FUNDING_COST)

    def test_keeps_position_below_limit(self):
        """한도 아래면 그대로 들고 있는다"""
        engine, sf, broker, market = self._engine(funding_rate=0.0001, max_funding_cost_percent=1.0,
                                                  entry_min_funding_8h_percent=None)
        engine.startup()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            pos = broker.open_positions(s)[0]
            pos.funding_usdt = -pos.bybit_qty * pos.entry_bybit_price_usdt * 0.005   # 0.5%
            s.commit()
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 1)

    def test_received_funding_is_not_a_cost(self):
        """펀딩비를 받았으면(+) 비용이 0 이다"""
        engine, sf, broker, market = self._engine(funding_rate=0.0001, entry_min_funding_8h_percent=None)
        engine.startup()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            pos = broker.open_positions(s)[0]
            pos.funding_usdt = 5.0
            s.commit()
            self.assertEqual(engine._funding_cost_percent(pos), 0.0)

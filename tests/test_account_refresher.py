"""
계좌 갱신 스레드 (kimp/account_refresher.py) 와 실행기 연결, 엔진의 '계좌 오래됨' 규칙 (issues N7, 2026-10-31)

실행: python -m unittest discover -s tests -v
"""
import unittest

from kimp.account_refresher import AccountRefresher
from support_ticks import continuous_upbit_ticks

_TICKS = continuous_upbit_ticks()   # 가짜 시세가 호가 단위 밖이라 지정가 단위 맞춤을 끈다 (tests/support_ticks.py)


def setUpModule():
    _TICKS.start()


def tearDownModule():
    _TICKS.stop()

from kimp.config import Settings
from kimp.db import Position, PositionStatus, make_session_factory
from kimp.market_data import FundingCache, FundingInfo


class FakeUp:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.bal = {"USDT": {"free": 500.0, "total": 500.0}, "KRW": {"free": 50_000.0, "total": 50_000.0}}

    def fetch_balance(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("upbit down")
        return self.bal


class FakeBy:
    def __init__(self):
        self.calls = 0
        self.pos_calls = 0
        self.fail = False
        self.shorts = {"XRP": 50.0}

    def fetch_balance(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("bybit down")
        return {"USDT": {"free": 700.0, "total": 760.0}}

    def fetch_positions(self, symbols=None):
        self.pos_calls += 1
        if self.fail:
            raise ConnectionError("bybit down")
        if symbols:                                             # 주문용 객체(실행기)는 코인 하나씩 받는다
            coins = [symbols[0].split("/")[0]]
        else:                                                   # 갱신 스레드는 인자 없이 전부 한 번에
            coins = list(self.shorts)
        return [{"symbol": f"{c}/USDT:USDT", "side": "short", "contracts": self.shorts[c], "liquidationPrice": 3.0}
                for c in coins if self.shorts.get(c)]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class RefresherTest(unittest.TestCase):
    def test_cycle_fills_snapshot_and_failure_keeps_old(self):
        """한 바퀴면 두 거래소 값이 들어오고, 바이비트가 죽으면 옛 값은 남고 by_at 만 안 올라간다 (나이로 판단)"""
        up, by, clock = FakeUp(), FakeBy(), Clock()
        r = AccountRefresher(up, by, clock=clock)
        self.assertEqual(r.age(), float("inf"))
        r.watch(["XRP"])
        snap = r.cycle()
        self.assertEqual(snap.up_bal["USDT"]["free"], 500.0)
        self.assertEqual(snap.shorts["XRP"], 50.0)
        self.assertEqual(snap.liq["XRP"], 3.0)
        self.assertEqual(r.age(), 0.0)
        clock.t += 30
        by.fail = True
        snap2 = r.cycle()
        self.assertEqual(snap2.shorts["XRP"], 50.0)            # 옛 값 그대로
        self.assertIn("bybit down", snap2.by_error)
        self.assertEqual(snap2.up_at, clock.t)                 # 업비트는 새로 받음
        self.assertEqual(r.age(), 30.0)                        # 가장 오래된 쪽(바이비트) 기준
        by.fail = False
        clock.t += 1
        snap3 = r.cycle()
        self.assertEqual(snap3.by_error, "")
        self.assertEqual(r.age(), 0.0)


class ExecutorSnapshotTest(unittest.TestCase):
    """LiveExecutor.prefetch 가 스냅샷을 쓰고, 주문 뒤에는 새 바퀴가 올 때까지 그 자리에서 받는다"""

    def _ex(self):
        from kimp.live_executor import LiveExecutor
        up, by = FakeUp(), FakeBy()
        ex = LiveExecutor(up, by, Settings(coins=["XRP"]), sleep=lambda _x: None, order_timeout_sec=0.01)
        up2, by2 = FakeUp(), FakeBy()                          # 갱신 스레드는 자기 객체를 쓴다
        clock = Clock()
        ex.account = AccountRefresher(up2, by2, clock=clock)
        ex.account.watch(["XRP"])
        ex.account.cycle()
        return ex, up, by, up2, by2, clock

    def test_prefetch_uses_snapshot_without_rest(self):
        ex, up, by, up2, by2, clock = self._ex()
        ex.refresh()
        ex.prefetch(["XRP"])
        self.assertEqual((up.calls, by.calls, by.pos_calls), (0, 0, 0))   # 주문용 객체로는 REST 안 함
        self.assertEqual(ex.free(None, "upbit:USDT"), 500.0)
        self.assertEqual(ex.short_qty(None, "XRP"), 50.0)
        self.assertEqual(ex.position_liq_price(None, "XRP"), 3.0)
        self.assertEqual((up.calls, by.calls, by.pos_calls), (0, 0, 0))
        self.assertEqual(ex.account_age(), 0.0)

    def test_after_order_waits_for_fresh_cycle(self):
        """업비트 주문 뒤(dirty)엔 스냅샷이 주문 전 것이라 안 쓰고 그 자리에서 받는다. 주문 뒤 바퀴가 돌면 다시 스냅샷"""
        ex, up, by, up2, by2, clock = self._ex()
        clock.t += 1
        ex._refresh_after_order(ex.up)                         # 업비트 주문이 끝났다고 치자
        ex.refresh()
        ex.prefetch(["XRP"])
        self.assertIsNone(ex._up_bal)                          # 스냅샷(주문 전)은 안 씀
        self.assertIsNotNone(ex._by_bal)                       # 바이비트는 스냅샷 그대로
        ex.free(None, "upbit:USDT")
        self.assertEqual(up.calls, 1)                          # 그 자리에서 한 번 받음
        clock.t += 1
        ex.account.cycle()                                     # 주문 뒤에 시작한 바퀴
        ex.refresh()
        ex.prefetch(["XRP"])
        self.assertIsNotNone(ex._up_bal)                       # 이제 스냅샷을 쓴다
        self.assertEqual(up.calls, 1)

    def test_new_coin_not_in_snapshot_fetches_on_demand(self):
        ex, up, by, up2, by2, clock = self._ex()
        ex.refresh()
        ex.prefetch(["XRP", "SOL"])                            # SOL 은 아직 스냅샷에 없다
        self.assertNotIn("SOL", ex._shorts)
        self.assertEqual(ex.short_qty(None, "SOL"), 0.0)       # 그 자리에서 받음 (없으면 0)
        self.assertEqual(by.pos_calls, 1)


class BaseCoinsTest(unittest.TestCase):
    def test_base_coins_always_in_snapshot_so_reconcile_reads_cache(self):
        """운용 코인 전부를 늘 받아 두면 10분 대조가 보유하지 않은 코인의 숏도 REST 없이 읽는다 (10-31 20:23 스캔 13.4초)"""
        from kimp.live_executor import LiveExecutor
        up, by = FakeUp(), FakeBy()
        ex = LiveExecutor(up, by, Settings(coins=["XRP", "SOL"]), sleep=lambda _x: None, order_timeout_sec=0.01)
        up2, by2 = FakeUp(), FakeBy()
        ex.account = AccountRefresher(up2, by2, clock=Clock(), base_coins=["XRP", "SOL"])
        ex.account.cycle()
        self.assertEqual(set(ex.account.snapshot().shorts), {"XRP", "SOL"})
        ex.refresh()
        ex.prefetch(["XRP"])                                   # 보유는 XRP 뿐이어도
        self.assertEqual(ex.short_qty(None, "SOL"), 0.0)       # SOL 숏도 스냅샷에서
        self.assertEqual(by.pos_calls, 0)                      # 주문용 객체로 REST 없음


class FundingCacheTest(unittest.TestCase):
    def test_cache_refresh_and_fallback(self):
        clock = Clock()
        calls = []

        def fetch(coin):
            calls.append(coin)
            if coin == "BAD":
                return None
            return FundingInfo(rate=0.0001, next_funding_ms=1, mark_price=1.0, interval_hours=8.0)

        c = FundingCache(fetch, ["XRP", "BAD"], refresh_sec=60, clock=clock)
        self.assertIsNone(c.get("XRP"))
        self.assertEqual(c.refresh_once(), 1)
        self.assertEqual(c.get("XRP").rate, 0.0001)
        self.assertIsNone(c.get("BAD"))
        self.assertEqual(c.refresh_once(), 0)                 # 60초 안이면 다시 안 받는다
        clock.t += 61
        self.assertEqual(c.refresh_once(), 1)
        clock.t += 60 * 3 + 1
        self.assertIsNone(c.get("XRP"))                        # 너무 오래되면 None → 부르는 쪽이 직접 받는다


class EngineStaleRuleTest(unittest.TestCase):
    """계좌 스냅샷이 오래되면 신규 진입과 숏 사라짐 판정을 보류하고, 청산 판단은 계속한다"""

    def _setup(self, age):
        from test_engine import FakeMarket, make_engine, make_quote, make_settings
        market = FakeMarket()
        engine, sf, broker = make_engine(make_settings(), market)
        broker.account_age = lambda: age
        calls = []
        orig = broker.vanished_shorts
        broker.vanished_shorts = lambda s: (calls.append(1), orig(s))[1]
        return engine, sf, broker, market, calls, make_quote

    def test_stale_blocks_entry_and_vanished_check(self):
        engine, sf, broker, market, calls, make_quote = self._setup(age=30.0)
        market.quotes = {"XRP": make_quote(-1.0)}
        with self.assertLogs("kimp.engine", level="WARNING") as logs:
            engine.step()
        self.assertTrue(any("[계좌 조회 지연]" in l for l in logs.output))
        with sf() as s:
            self.assertEqual(s.query(Position).count(), 0)     # 진입 안 함
        self.assertEqual(calls, [])                            # 숏 사라짐 판정 안 함

    def test_fresh_enters_and_checks(self):
        engine, sf, broker, market, calls, make_quote = self._setup(age=1.0)
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).count(), 1)
        self.assertEqual(len(calls), 1)

    def test_stale_block_has_reason_and_resumes_with_notice(self):
        """보류 알림에 이유가 붙고, 스냅샷이 새것이 되면 [신규 진입 재개] 를 한 번 알린다 (메신저에 '중단' 만 남지 않게)"""
        engine, sf, broker, market, calls, make_quote = self._setup(age=30.0)
        market.quotes = {"XRP": make_quote(0.0)}
        with self.assertLogs("kimp.engine", level="WARNING") as logs:
            engine.step()
        blocked = [l for l in logs.output if "[신규 진입 중단]" in l]
        self.assertEqual(len(blocked), 1)
        self.assertIn("조회 지연", blocked[0])                    # 빈 이유가 아니다
        broker.account_age = lambda: 1.0
        with self.assertLogs("kimp.engine", level="WARNING") as logs:
            engine.step()
        self.assertEqual(sum("[신규 진입 재개]" in l for l in logs.output), 1)
        engine.step()                                           # 다음 스캔에는 다시 알리지 않는다
        # (assertLogs 없이 돌려도 예외 없음 = 재개 알림 반복 없음은 아래 handler 로 확인)
        import logging
        seen = []
        h = logging.Handler(); h.emit = lambda r: seen.append(r.getMessage())
        logging.getLogger("kimp.engine").addHandler(h)
        try:
            engine.step()
        finally:
            logging.getLogger("kimp.engine").removeHandler(h)
        self.assertFalse(any("[신규 진입 재개]" in m for m in seen))

    def test_stale_still_takes_profit(self):
        """오래된 스냅샷이어도 익절 판단은 마지막 값으로 계속한다"""
        engine, sf, broker, market, calls, make_quote = self._setup(age=1.0)
        market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        broker.account_age = lambda: 30.0
        market.quotes = {"XRP": make_quote(0.8)}
        engine.step()
        with sf() as s:
            self.assertEqual(s.query(Position).first().status, PositionStatus.CLOSED)


class OnePositionsCallTest(unittest.TestCase):
    def test_all_positions_in_one_call_and_slow_warning(self):
        """코인이 몇 개든 바이비트 포지션은 한 요청이고, 지켜보는 코인은 없으면 0 으로 채운다. 느린 바퀴는 경고 한 줄"""
        from kimp.account_refresher import AccountRefresher
        up, by, clock = FakeUp(), FakeBy(), Clock()
        by.shorts = {"XRP": 50.0, "SOL": 7.0}
        r = AccountRefresher(up, by, interval_sec=2.0, clock=clock, base_coins=["XRP", "SOL", "ADA"], slow_sec=5.0)
        snap = r.cycle()
        self.assertEqual(by.pos_calls, 1)
        self.assertEqual((snap.shorts["XRP"], snap.shorts["SOL"], snap.shorts["ADA"]), (50.0, 7.0, 0.0))
        self.assertEqual(snap.liq["XRP"], 3.0)
        self.assertIsNone(snap.liq["ADA"])
        # 느린 바퀴: 바이비트 쪽이 6초 걸린 것으로 흉내
        orig = by.fetch_positions
        def slow(symbols=None):
            clock.t += 6.0
            return orig(symbols)
        by.fetch_positions = slow
        with self.assertLogs("kimp.account_refresher", level="WARNING") as logs:
            r.cycle()
        self.assertTrue(any("[계좌 갱신 느림]" in l and "바이비트 6.0초" in l for l in logs.output))
        self.assertGreaterEqual(r.last_cycle_sec, 6.0)
        # 실패(타임아웃)한 호출도 걸린 시간이 남는다
        def timeout(symbols=None):
            clock.t += 5.5                                      # slow_sec(5.0) 를 넘겨야 경고가 난다
            raise TimeoutError("bybit timeout")
        by.fetch_positions = timeout
        clock.t += 61                                           # 느림 경고 쿨다운(60초) 지나게
        with self.assertLogs("kimp.account_refresher", level="WARNING") as logs:
            r.cycle()
        self.assertGreaterEqual(r.last_by_sec, 5.5)
        self.assertTrue(any("[계좌 갱신 느림]" in l and "바이비트 5.5초" in l for l in logs.output))


if __name__ == "__main__":
    unittest.main()

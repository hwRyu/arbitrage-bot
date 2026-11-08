"""
거래소 점검 모드 (kimp/maintenance.py, 2026-11-08) — 명령 해석·검증, 단계 전환, 한쪽 모드 엔진, 갱신 스레드·웹소켓 일시정지

실행: python -m unittest discover -s tests -v
"""
import logging
import unittest
from datetime import datetime, timedelta, timezone

from kimp import maintenance as mt
from kimp.account_refresher import AccountRefresher
from kimp.config import Settings
from kimp.db import MaintenanceWindow, Position, PositionStatus, make_session_factory
from kimp.maintenance import KST, MaintenanceManager, parse_command
from kimp.premium import OrderBook
from kimp.safety import Safety
from kimp.ws_feed import WsFeed
from support_ticks import continuous_upbit_ticks

_TICKS = continuous_upbit_ticks()


def setUpModule():
    _TICKS.start()


def tearDownModule():
    _TICKS.stop()


NOW = datetime(2026, 11, 8, 12, 0, tzinfo=KST)          # 한국 시간 11-08 12:00


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self):
        return self.t


class ParseTest(unittest.TestCase):
    def test_add_with_month_day_and_full_date(self):
        c = parse_command("bybit 11-12 02:00 11-12 04:00", NOW)
        self.assertEqual((c.action, c.exchange), ("add", "bybit"))
        self.assertEqual(c.start_at.astimezone(KST).strftime("%m-%d %H:%M"), "11-12 02:00")
        self.assertEqual((c.end_at - c.start_at).total_seconds(), 7200)
        c2 = parse_command("upbit 2026-11-12 23:30 2026-11-13 01:00", NOW)
        self.assertEqual(c2.exchange, "upbit")
        self.assertEqual(c2.end_at.astimezone(KST).strftime("%m-%d %H:%M"), "11-13 01:00")

    def test_aliases_list_and_cancel(self):
        self.assertEqual(parse_command("", NOW).action, "list")
        self.assertEqual(parse_command("off", NOW), mt.ParsedCommand("cancel", None))
        self.assertEqual(parse_command("off 바이비트", NOW).exchange, "bybit")
        self.assertEqual(parse_command("업비트 11-12 02:00 11-12 04:00", NOW).exchange, "upbit")

    def test_rejects_bad_input(self):
        """거래소 이름·항목 수·형식·순서·지난 시각·너무 긴 점검은 등록하지 않고 이유를 말한다"""
        cases = {
            "binance 11-12 02:00 11-12 04:00": "거래소는",
            "bybit 11-12 02:00 04:00": "항목",                      # 종료에 날짜가 없다
            "bybit 11/12/2026 02:00 11-12 04:00": "날짜 형식",
            "bybit 11-12 2:00pm 11-12 04:00": "시각 형식",
            "bybit 11-12 25:00 11-12 26:00": "말이 안",
            "bybit 11-12 04:00 11-12 02:00": "뒤여야",
            "bybit 11-12 04:00 11-12 04:00": "뒤여야",
            "bybit 11-01 02:00 11-01 04:00": "이미 지났",
            "bybit 11-12 02:00 11-15 04:00": "넘는 일정",
            "bybit 12-20 02:00 12-20 04:00": "넘게 남았",
        }
        for text, hint in cases.items():
            with self.assertRaises(ValueError, msg=text) as cm:
                parse_command(text, NOW)
            self.assertIn(hint, str(cm.exception), text)

    def test_year_rollover_without_year(self):
        """12월 말에 '01-02' 를 적으면 내년으로 본다"""
        late = datetime(2026, 12, 30, 12, 0, tzinfo=KST)
        c = parse_command("bybit 01-02 02:00 01-02 04:00", late)
        self.assertEqual(c.start_at.astimezone(KST).year, 2027)


class FakePauser:
    def __init__(self):
        self.paused = set()
        self.calls = []

    def pause_exchange(self, ex):
        self.paused.add(ex)
        self.calls.append(("pause", ex))

    def resume_exchange(self, ex):
        self.paused.discard(ex)
        self.calls.append(("resume", ex))


class FakeRefresher:
    """갱신 스레드 흉내: fresh 는 '응답이 온다' 를 뜻한다"""

    def __init__(self):
        self.paused = {}
        self.fresh = {"upbit": True, "bybit": True}
        self.fail = {"upbit": 0.0, "bybit": 0.0}
        self.interval_sec = 2.0

    def pause(self, ex, probe_every_sec=None):
        self.paused[ex] = probe_every_sec

    def resume(self, ex):
        self.paused.pop(ex, None)

    def fresh_within(self, ex, sec):
        return self.fresh[ex]

    def failing_for(self, ex):
        return self.fail[ex]


def make_manager(clock, pauser, refresher, confirm=None, **over):
    settings = Settings(coins=["XRP"], maint_pre_sec=1800, maint_recover_hold_sec=600, maint_auto_detect_sec=180,
                        maint_probe_sec=300, **over)
    sf = make_session_factory(":memory:")
    safety = Safety()
    recovered = []
    m = MaintenanceManager(settings, sf, clock=clock, safety=safety, pausers=[pauser], refresher=refresher,
                           confirm=confirm, on_recovered=recovered.append)
    return m, sf, safety, recovered


class ManagerTest(unittest.TestCase):
    def test_phases_pre_active_recover_hold(self):
        """예고(진입 막힘) → 점검(일시정지·한쪽 모드) → 종료(재개·복구 확인) → 안정화(진입만 막힘) → 평소"""
        t0 = NOW.timestamp()
        clock, pauser, ref = Clock(t0), FakePauser(), FakeRefresher()
        confirm_calls = []

        def confirm(ex):
            confirm_calls.append(ex)
            return True, "대조 일치"

        m, sf, safety, recovered = make_manager(clock, pauser, ref, confirm)
        reply = m.command("bybit 11-08 13:00 11-08 14:00")
        self.assertIn("등록: 바이비트 11-08 13:00~14:00", reply)
        self.assertTrue(m.update().normal)                        # 12:00 — 아직 아무것도 아님
        self.assertEqual(safety.maintenance_reason, "")

        clock.t = t0 + 40 * 60                                    # 12:40 — 예고
        st = m.update()
        self.assertTrue(st.normal)
        self.assertIn("점검 예정", safety.maintenance_reason)
        self.assertFalse(safety.entries_allowed(sf())[0])
        self.assertTrue(m.transfers_blocked())
        self.assertEqual(pauser.paused, set())

        clock.t = t0 + 61 * 60                                    # 13:01 — 점검 중
        st = m.update()
        self.assertEqual(st.down, frozenset({"bybit"}))
        self.assertIn("바이비트 점검 중", st.text)
        self.assertEqual(pauser.paused, {"bybit"})
        self.assertEqual(ref.paused, {"bybit": None})             # 사람이 적은 일정: 찔러 보지도 않는다

        clock.t = t0 + 121 * 60                                   # 14:01 — 종료 시각 → 재개 + 복구 확인 중
        st = m.update()
        self.assertEqual(pauser.paused, set())
        self.assertNotIn("bybit", ref.paused)
        self.assertEqual(st.down, frozenset({"bybit"}))           # 확인 전까지는 거래 없음
        self.assertEqual(confirm_calls, [])                       # 첫 확인은 갱신 스레드가 한 바퀴 돌 시간을 준다

        clock.t += 6
        st = m.update()
        self.assertEqual(confirm_calls, ["bybit"])
        self.assertTrue(st.normal)                                # 복구 확인 → 한쪽 모드 끝
        self.assertEqual(recovered, ["bybit"])
        self.assertIn("안정화", safety.maintenance_reason)        # 진입만 막힘
        self.assertFalse(safety.entries_allowed(sf())[0])

        clock.t += 601
        self.assertTrue(m.update().normal)
        self.assertEqual(safety.maintenance_reason, "")
        self.assertTrue(safety.entries_allowed(sf())[0])

    def test_recovery_waits_until_alive_and_reconciled(self):
        """응답이 없거나 대조가 안 맞으면 복구로 보지 않고 probe 간격마다 다시 본다"""
        t0 = NOW.timestamp()
        clock, pauser, ref = Clock(t0), FakePauser(), FakeRefresher()
        answers = [(False, "대조 불일치: XRP 숏이 보이지 않음"), (True, "대조 일치")]
        m, sf, safety, recovered = make_manager(clock, pauser, ref, lambda ex: answers.pop(0))
        m.command("upbit 11-08 12:30 11-08 13:00")
        clock.t = t0 + 31 * 60
        self.assertEqual(m.update().down, frozenset({"upbit"}))
        clock.t = t0 + 61 * 60                                    # 종료 시각
        ref.fresh["upbit"] = False                                # 아직 응답 없음
        m.update()
        clock.t += 6
        self.assertEqual(m.update().down, frozenset({"upbit"}))
        self.assertEqual(len(answers), 2)                         # 응답이 없으니 대조까지 안 간다
        clock.t += 300                                            # probe 간격 뒤
        ref.fresh["upbit"] = True
        self.assertEqual(m.update().down, frozenset({"upbit"}))   # 대조 불일치 → 계속 보류
        self.assertEqual(len(answers), 1)
        clock.t += 300
        self.assertTrue(m.update().normal)                        # 두 번째 확인에서 일치
        self.assertEqual(recovered, ["upbit"])

    def test_cancel_during_active_moves_to_recovery(self):
        """점검 중 /maint off 를 보내면 바로 재개하고 복구 확인으로 넘어간다"""
        t0 = NOW.timestamp()
        clock, pauser, ref = Clock(t0), FakePauser(), FakeRefresher()
        m, sf, safety, _ = make_manager(clock, pauser, ref, lambda ex: (True, "ok"))
        m.command("bybit 11-08 12:30 11-08 15:00")
        clock.t = t0 + 31 * 60
        self.assertEqual(m.update().down, frozenset({"bybit"}))
        reply = m.command("off bybit")
        self.assertIn("해제: 바이비트", reply)
        st = m.update()
        self.assertEqual(pauser.paused, set())
        self.assertEqual(st.down, frozenset({"bybit"}))           # 복구 확인 중
        clock.t += 6
        self.assertTrue(m.update().normal)
        self.assertEqual(m.command("off"), "해제할 점검 일정이 없습니다")

    def test_duplicate_and_status_lines(self):
        t0 = NOW.timestamp()
        m, sf, safety, _ = make_manager(Clock(t0), FakePauser(), FakeRefresher())
        self.assertIn("등록:", m.command("bybit 11-09 02:00 11-09 04:00"))
        self.assertIn("겹칩니다", m.command("bybit 11-09 03:00 11-09 05:00"))
        self.assertIn("등록:", m.command("upbit 11-09 03:00 11-09 05:00"))   # 다른 거래소는 된다
        lines = m.status_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("예정", lines[0])
        self.assertIn("형식:", m.command(""))
        self.assertIn("등록 안 됨", m.command("bybit 11-09 04:00 11-09 02:00"))

    def test_auto_detect_and_auto_end(self):
        """갱신 스레드가 3분 넘게 실패하면 자동 점검(찔러 보기 간격 설정), 살아나면 종료 시각이 적히고 복구 확인으로"""
        t0 = NOW.timestamp()
        clock, pauser, ref = Clock(t0), FakePauser(), FakeRefresher()
        m, sf, safety, recovered = make_manager(clock, pauser, ref, lambda ex: (True, "ok"))
        ref.fail["bybit"] = 100.0
        self.assertTrue(m.update().normal)
        ref.fail["bybit"] = 200.0
        ref.fresh["bybit"] = False
        with self.assertLogs("kimp.maintenance", level=logging.CRITICAL) as cm:
            st = m.update()
        self.assertIn("점검 감지", cm.output[0])
        self.assertEqual(st.down, frozenset({"bybit"}))
        self.assertEqual(ref.paused, {"bybit": 300.0})            # 자동 감지는 5분마다 한 번 찔러 본다
        with sf() as s:
            w = s.query(MaintenanceWindow).one()
            self.assertEqual((w.source, w.end_at), ("auto", None))
        clock.t += 600
        ref.fresh["bybit"] = True                                 # 찔러 본 게 성공
        st = m.update()
        self.assertEqual(pauser.paused, set())
        with sf() as s:
            self.assertIsNotNone(s.query(MaintenanceWindow).one().end_at)
        clock.t += 6
        self.assertTrue(m.update().normal)
        self.assertEqual(recovered, ["bybit"])

    def test_both_exchanges_down(self):
        t0 = NOW.timestamp()
        clock, pauser, ref = Clock(t0), FakePauser(), FakeRefresher()
        m, sf, safety, _ = make_manager(clock, pauser, ref)
        m.command("bybit 11-08 12:30 11-08 14:00")
        m.command("upbit 11-08 12:30 11-08 14:00")
        clock.t = t0 + 31 * 60
        self.assertEqual(m.update().down, frozenset({"upbit", "bybit"}))
        self.assertEqual(pauser.paused, {"upbit", "bybit"})


class RefresherPauseTest(unittest.TestCase):
    def test_paused_side_is_not_called_and_probe_interval(self):
        from test_account_refresher import FakeBy, FakeUp
        up, by, clock = FakeUp(), FakeBy(), Clock(1000.0)
        r = AccountRefresher(up, by, clock=clock)
        r.watch(["XRP"])
        r.cycle()
        self.assertEqual((up.calls, by.calls), (1, 1))
        r.pause("bybit")                                          # 사람이 적은 점검: 아예 안 부른다
        clock.t += 2
        snap = r.cycle()
        self.assertEqual((up.calls, by.calls), (2, 1))
        self.assertEqual(snap.shorts["XRP"], 50.0)                # 옛 값 그대로
        self.assertTrue(r.fresh_within("upbit", 1.0))
        self.assertFalse(r.fresh_within("bybit", 1.0))
        r.resume("bybit")
        clock.t += 2
        r.cycle()
        self.assertEqual(by.calls, 2)
        self.assertTrue(r.fresh_within("bybit", 1.0))

    def test_auto_pause_probes_quietly_and_failing_for(self):
        from test_account_refresher import FakeBy, FakeUp
        up, by, clock = FakeUp(), FakeBy(), Clock(1000.0)
        r = AccountRefresher(up, by, clock=clock)
        r.watch(["XRP"])
        r.cycle()
        by.fail = True
        clock.t += 2
        r.cycle()
        self.assertEqual(r.failing_for("bybit"), 0.0)             # 첫 실패 직후
        clock.t += 200
        r.cycle()
        self.assertAlmostEqual(r.failing_for("bybit"), 200.0)
        self.assertEqual(r.failing_for("upbit"), 0.0)
        r.pause("bybit", probe_every_sec=300)
        calls = by.calls
        for _ in range(10):                                       # 300초 안에는 한 번만 찔러 본다
            clock.t += 20
            with self.assertNoLogs("kimp.account_refresher", level=logging.WARNING):
                r.cycle()
        self.assertEqual(by.calls, calls + 1)
        clock.t += 300
        by.fail = False
        r.cycle()
        self.assertEqual(by.calls, calls + 2)
        self.assertTrue(r.fresh_within("bybit", 1.0))
        self.assertEqual(r.failing_for("bybit"), 0.0)


class WsFeedPauseTest(unittest.TestCase):
    def test_pause_hides_books_and_single_book(self):
        clock = Clock(1000.0)
        feed = WsFeed(["MINA"], max_age_ms=1500, clock=clock)

        def book(price):
            return OrderBook(bids=[(price, 10.0)], asks=[(price * 1.001, 10.0)], timestamp_ms=1)

        feed._store(("upbit", "MINA/KRW"), book(91.4))
        feed._store(("upbit", "USDT/KRW"), book(1340.0))
        feed._store(("bybit", "MINA/USDT:USDT"), book(0.0682))
        self.assertIsNotNone(feed.books("MINA"))
        feed.pause_exchange("bybit")
        self.assertIsNone(feed.books("MINA"))                     # 세 장 묶음은 못 쓴다
        self.assertIsNone(feed.book("bybit", "MINA/USDT:USDT"))
        self.assertIsNotNone(feed.book("upbit", "MINA/KRW"))      # 업비트 한 장은 쓴다 (한쪽 감시용)
        feed.resume_exchange("bybit")
        self.assertIsNone(feed.books("MINA"))                     # 다시 메시지가 와야 산다
        feed._store(("bybit", "MINA/USDT:USDT"), book(0.0683))
        self.assertIsNotNone(feed.books("MINA"))


class EngineOneSideTest(unittest.TestCase):
    """점검 중 엔진: 두 다리가 필요한 일은 안 하고, 한쪽 시세로 강제청산 거리만 본다"""

    def _engine(self):
        from test_engine import FakeMarket, make_engine, make_quote, make_settings
        settings = make_settings(bybit_leverage=2.0, liq_guard_percent=10.0, liq_warn_percent=15.0,
                                 liq_guard_topup_max_rounds=2, bybit_margin_buffer_percent=30.0)
        market = FakeMarket()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine, sf, broker = make_engine(settings, market)
        # 시계를 먼저 고정한다 — /maint 등록 검증("이미 지난 시각")이 이 시계를 쓰므로 진짜 시각이면 테스트가 날짜에 따라 깨진다
        self.clock = Clock(NOW.timestamp())
        engine._clock = self.clock
        engine.maint._clock = self.clock
        engine.step()                                             # 진입
        with sf() as s:
            pos = broker.open_positions(s)
            self.assertEqual(len(pos), 1)
        return engine, sf, broker, market, make_quote

    def test_bybit_down_blocks_take_profit_and_only_warns(self):
        engine, sf, broker, market, make_quote = self._engine()
        market.quotes = {"XRP": make_quote(+2.0)}                 # 평소라면 익절
        market.fetch_upbit_books = lambda coins: {}
        self.assertIn("등록:", engine.maint.command("bybit 11-08 12:00 11-08 13:00"))
        self.clock.t = NOW.timestamp() + 60                       # 점검 중
        engine.step()
        with sf() as s:
            self.assertEqual(broker.open_positions(s)[0].status, PositionStatus.OPEN)   # 안 팔았다

    def test_upbit_down_adds_margin_near_liquidation(self):
        engine, sf, broker, market, make_quote = self._engine()
        with sf() as s:
            pos = broker.open_positions(s)[0]
            liq, margin0 = pos.liquidation_price, pos.bybit_margin_usdt
        price = liq / 1.05                                        # 강제청산까지 5% (가드 10% 안)
        market.fetch_bybit_books = lambda coins: {
            "XRP": OrderBook(bids=[(price * 0.999, 1e9)], asks=[(price, 1e9)], timestamp_ms=1)}
        self.assertIn("등록:", engine.maint.command("upbit 11-08 12:00 11-08 13:00"))
        self.clock.t = NOW.timestamp() + 60
        with self.assertLogs("kimp.engine", level=logging.WARNING) as cm:
            engine.step()
        self.assertTrue(any("점검 중 증거금 추가" in line for line in cm.output), cm.output)
        with sf() as s:
            pos = broker.open_positions(s)[0]
            self.assertGreater(pos.bybit_margin_usdt, margin0)
            self.assertGreater(pos.liquidation_price, liq)
            self.assertEqual(pos.status, PositionStatus.OPEN)     # 정리는 안 한다 (업비트가 없다)

    def test_pre_phase_blocks_entry_but_keeps_exit(self):
        from test_engine import FakeMarket, make_engine, make_quote, make_settings
        settings = make_settings(max_open_positions=2)
        market = FakeMarket()
        market.quotes = {"XRP": make_quote(-1.0)}
        engine, sf, broker = make_engine(settings, market)
        clock = Clock(NOW.timestamp())                            # 12:00 — 예고 구간 (20분 전)
        engine._clock = clock
        engine.maint._clock = clock
        self.assertIn("등록:", engine.maint.command("bybit 11-08 12:20 11-08 13:00"))
        engine.step()
        with sf() as s:
            self.assertEqual(len(broker.open_positions(s)), 0)    # 예고라 진입 안 함
        ok, why = broker.entries_allowed(sf())
        self.assertFalse(ok)
        self.assertIn("점검 예정", why)


if __name__ == "__main__":
    unittest.main()

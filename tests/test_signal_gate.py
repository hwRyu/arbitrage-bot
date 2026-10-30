"""
신호 신뢰도 게이트 (kimp/engine.py _gate, docs/filter.md 3단계) — 기준·목표를 막 넘은 신호는 바로 믿지 않고, 되돌림 없이
유지돼 점수가 컷을 넘을 때만 주문한다. 튐(켜졌다 곧 꺼짐)은 주문이 나가지 않는다.

실행: python -m unittest discover -s tests -v
"""
import math
import unittest

from kimp.db import Decision, ExitReason, Position, PositionStatus, make_session_factory
from kimp.engine import Engine
from kimp.paper_broker import PaperBroker
from kimp.signal_filter import FilterParams, SignalFilter
from support_ticks import continuous_upbit_ticks
from test_engine import FakeMarket, make_quote, make_settings

_TICKS = continuous_upbit_ticks()
TAU, CUT = 2.5, 0.7
# 되돌림 없이 깔끔하게 유지되면 E=1 이라 P = 1 − 0.9·e^(−Δ/τ) → 컷을 넘는 데 걸리는 시간 (≈ 1.1τ)
PASS_SEC = TAU * math.log(0.9 / (1 - CUT))


def setUpModule():
    _TICKS.start()


def tearDownModule():
    _TICKS.stop()


class GateEngineTest(unittest.TestCase):
    """가짜 시계로 0.25초 간격 스캔을 흉내 낸다"""

    def setUp(self):
        self.now = [1000.0]
        self.market = FakeMarket()
        self.s = make_settings(signal_filter_enabled=True, signal_filter_tau_sec=TAU, signal_filter_cut=CUT)
        self.sf = make_session_factory(":memory:")
        self.broker = PaperBroker(self.s.fees, 0.3, leverage=self.s.bybit_leverage, mmr=self.s.bybit_mmr)
        self.engine = Engine(self.s, self.market, None, self.broker, self.sf, ["XRP"], clock=lambda: self.now[0])
        self.engine.startup()

    def _positions(self):
        with self.sf() as s:
            return s.query(Position).all()

    def _stages(self):
        with self.sf() as s:
            return [d.stage for d in s.query(Decision).order_by(Decision.id).all()]

    def _steps(self, premium: float, seconds: float, step: float = 0.25):
        """premium 시세를 유지하며 seconds 동안 step 간격으로 스캔. 포지션 상태가 바뀐 시각(초)을 돌려준다 (없으면 None)"""
        before = [(p.id, p.status) for p in self._positions()]
        t0 = self.now[0]
        while self.now[0] - t0 < seconds - 1e-9:
            self.now[0] += step
            self.market.quotes = {"XRP": make_quote(premium)}
            self.engine.step()
            if [(p.id, p.status) for p in self._positions()] != before:
                return self.now[0] - t0
        return None

    def test_entry_waits_until_score_passes_cut(self):
        """기준(-0.5%) 밖 → 안으로 들어온 순간엔 안 들어가고(점수 0.1), 유지되면 약 1.1τ 초 뒤 들어간다"""
        self._steps(-0.2, 2.0)                                  # 평소: 기준 밖
        self.assertEqual(self._positions(), [])
        changed = self._steps(-1.0, 6.0)                        # 기준 안으로 들어와 유지
        self.assertIsNotNone(changed, "유지되는 신호인데 진입하지 않았다")
        self.assertGreater(changed, 1.5)                        # 켜진 순간·1초 안에는 안 들어간다
        self.assertLess(changed, PASS_SEC + 0.6)                # 깔끔하게 유지되면 ≈1.1τ 초면 넘는다
        [pos] = self._positions()
        self.assertEqual(pos.status, PositionStatus.OPEN)
        stages = self._stages()
        self.assertIn("filter_hold", stages)                    # 기다린 기록이 남는다
        self.assertEqual(stages[-1], "entered")
        self.assertLess(stages.index("filter_hold"), stages.index("entered"))

    def test_entry_spike_never_enters(self):
        """켜졌다가 0.5초 만에 꺼지는 틈(BLEND #44 꼴) → 주문 없음. 다시 켜져도 처음부터 기다린다"""
        self._steps(-0.2, 2.0)
        self._steps(-1.0, 0.5)                                  # 틈
        self._steps(-0.2, 3.0)                                  # 곧 되돌아옴
        self.assertEqual(self._positions(), [])
        changed = self._steps(-1.0, 6.0)                        # 다시 켜짐 → 또 기다렸다가 유지되면 진입
        self.assertIsNotNone(changed)
        self.assertGreater(changed, 1.5)

    def test_entry_immediate_when_disabled(self):
        """SIGNAL_FILTER_ENABLED=false 면 예전처럼 켜지는 순간 진입"""
        s = make_settings(signal_filter_enabled=False)
        engine = Engine(s, self.market, None, PaperBroker(s.fees, 0.3), make_session_factory(":memory:"), ["XRP"],
                        clock=lambda: self.now[0])
        engine.startup()
        self.market.quotes = {"XRP": make_quote(-1.0)}
        engine.step()
        with engine.session_factory() as db:
            self.assertEqual(db.query(Position).count(), 1)

    def _enter(self):
        """게이트를 통과해 포지션 하나 잡은 상태로"""
        self._steps(-0.2, 1.0)
        self.assertIsNotNone(self._steps(-1.0, 6.0))
        [pos] = self._positions()
        self.assertEqual(pos.status, PositionStatus.OPEN)
        self._steps(-1.0, 3.0)                                  # 들어간 뒤 잠깐 평소 상태
        return pos

    def test_take_profit_waits_then_sells(self):
        """목표(1%)를 막 넘은 순간엔 팔지 않고([익절 대기] 기록), 유지되면 약 1.1τ 초 뒤 판다"""
        self._enter()
        changed = self._steps(0.8, 6.0)                         # 1.8%p 벌어짐 → 순손익 > 1% 유지
        self.assertIsNotNone(changed, "유지되는 익절 신호인데 팔지 않았다")
        self.assertGreater(changed, 1.5)
        self.assertLess(changed, PASS_SEC + 0.6)
        [pos] = self._positions()
        self.assertEqual(pos.exit_reason, ExitReason.TAKE_PROFIT)
        self.assertIn("tp_filter_hold", self._stages())
        self.assertNotIn(pos.id, self.engine._tp_filters)      # 청산 뒤 포지션 필터는 치운다

    def test_take_profit_spike_does_not_sell(self):
        """순손익이 목표를 0.5초만 넘고 되돌아오면(BLEND #36·#38·#42 꼴) 팔지 않는다"""
        self._enter()
        self._steps(0.8, 0.5)
        self._steps(-0.8, 3.0)                                  # 되돌아옴 (목표 아래)
        self.assertEqual(self._positions()[0].status, PositionStatus.OPEN)
        self.assertIn("tp_filter_hold", self._stages())
        self.assertNotIn("tp_judge", self._stages())


class SignalOnTriggerTest(unittest.TestCase):
    """필터 자체: 신호 켜짐을 급변 시작으로 보는 확장 (Z 가 작아도)"""

    P = FilterParams(tau_sec=TAU, cut=CUT, z_spike=3.0)

    def test_small_crossing_still_resets_and_recovers_with_time(self):
        """기준을 살짝(Z<3) 넘어 켜진 신호도 점수가 0.1 로 내려가고, 유지되면 ≈1.1τ 초 뒤 컷을 넘는다"""
        f = SignalFilter(self.P)
        t = 0.0
        for _ in range(40):                                    # 평소 ±0.02 흔들림, 신호 꺼짐
            f.update(t, -0.45 + (0.02 if int(t * 4) % 2 else -0.02), 0.01, signal_on=False)
            t += 0.25
        st = f.update(t, -0.52, 0.01, signal_on=True)           # 0.05 더 내려가 켜짐 (Z ≈ 1.7)
        self.assertTrue(st.spike_active)
        self.assertLess(st.score, CUT)
        passed_at = None
        for _ in range(40):
            t += 0.25
            if f.update(t, -0.52, 0.01, signal_on=True).score >= CUT:
                passed_at = t
                break
        self.assertIsNotNone(passed_at)
        self.assertAlmostEqual(passed_at - 10.0, PASS_SEC, delta=0.5)

    def test_signal_off_marks_spike_even_without_delta(self):
        """켜짐으로 시작한 급변(Δ≈0)이 신호 꺼짐으로 끝나면 Spike 확정(점수 0)"""
        f = SignalFilter(self.P)
        f.update(0.0, -0.49, 0.01, signal_on=False)
        f.update(0.25, -0.50, 0.01, signal_on=True)             # 겨우 켜짐 (d 가 min_scale 수준)
        st = f.update(0.5, -0.49, 0.01, signal_on=False)
        self.assertEqual(st.verdict, "spike")
        self.assertEqual(st.score, 0.0)
        self.assertFalse(st.spike_active)

    def test_describe_mentions_signal_age(self):
        f = SignalFilter(self.P)
        f.update(0.0, -0.2, 0.01, signal_on=False)
        f.update(1.0, -1.0, 0.01, signal_on=True)
        self.assertIn("신호 1.5초째", f.describe(2.5))
        self.assertIn("급변 뒤 판정 중", f.describe(2.5))
        f.update(3.0, -0.2, 0.01, signal_on=False)
        self.assertEqual(f.describe(3.0), "신뢰도 0.00")


if __name__ == "__main__":
    unittest.main()

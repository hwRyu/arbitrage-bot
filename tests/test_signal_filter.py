"""
신호 신뢰도 필터 (kimp/signal_filter.py, docs/filter.md) — 순간 튐은 점수가 못 오르고, 유지되는 급변은 올라오고, 완만한 변화는 처음부터 믿는다

실행: python -m unittest discover -s tests -v
"""
import unittest

from kimp.signal_filter import FilterParams, SignalFilter


def run(series, tick_pct=0.1, params=FilterParams()):
    f = SignalFilter(params)
    return [f.update(t, x, tick_pct) for t, x in series]


def quiet(level=-0.2, seconds=60, step=0.5):
    """평소: level 근처에서 ±0.01 흔들림"""
    out, t, sign = [], 0.0, 1
    while t <= seconds:
        out.append((t, level + 0.01 * sign))
        sign = -sign
        t += step
    return out


class SpikeTest(unittest.TestCase):
    def test_spike_then_revert_never_passes_cut(self):
        """BLEND #42 (2026-10-30 18:31): −0.19 에서 +0.40 으로 튀고 0.25초 뒤 −0.57 → 점수가 컷을 못 넘고 Spike 확정"""
        base = quiet(-0.19)
        t0 = base[-1][0]
        series = base + [(t0 + 0.5, 0.40), (t0 + 0.75, -0.57), (t0 + 1.25, -0.55), (t0 + 2.0, -0.50)]
        states = run(series)
        jump = states[len(base)]
        self.assertTrue(jump.spike_active)
        self.assertLess(jump.score, 0.7)                       # 튄 그 순간엔 믿지 않는다
        self.assertEqual(states[len(base) + 1].verdict, "spike")
        self.assertEqual(states[len(base) + 1].score, 0.0)
        self.assertLess(max(s.score for s in states[len(base):]), 0.7)   # 튄 뒤 2초 안에 컷을 넘는 일은 없다

    def test_level_shift_passes_after_evidence(self):
        """같은 급변이 유지되면 점수가 올라 컷을 넘는다 (몇 초 뒤인지는 증거 속도가 정한다)"""
        base = quiet(-0.19)
        t0 = base[-1][0]
        held = [(t0 + 0.5 + i * 0.5, 0.40 + (0.005 if i % 2 else -0.005)) for i in range(140)]   # 70초 유지
        states = run(base + held)
        self.assertLess(states[len(base)].score, 0.7)
        crossed = next((s for s in states[len(base):] if s.score >= 0.7), None)
        self.assertIsNotNone(crossed)
        t_cross = (base + held)[states.index(crossed)][0] - t0
        self.assertLess(t_cross, 30.0)                         # τ=8초 기준 10~15초 안
        self.assertGreater(t_cross, 2.0)
        self.assertIn("shift", [s.verdict for s in states])    # 60초 유지 → Level shift 확정

    def test_gradual_change_is_trusted_from_the_start(self):
        """몇 분에 걸쳐 0.6%p 내려오는 완만한 변화는 급변 표시가 안 붙고 점수가 높다"""
        series = [(i * 0.5, -0.2 - 0.6 * i / 240) for i in range(240)]   # 120초 동안 선형
        states = run(series, tick_pct=0.0)
        self.assertFalse(any(s.spike_active for s in states))
        self.assertGreater(min(s.score for s in states), 0.95)

    def test_tick_floor_keeps_one_tick_moves_from_counting_as_spike(self):
        """호가 한 칸(0.15%)짜리 코인: 한 칸 왕래는 급변이 아니다 (바닥 = 칸/2)"""
        series = quiet(-0.2) + [(60.5, -0.05), (61.0, -0.2), (61.5, -0.05)]
        states = run(series, tick_pct=0.15)
        self.assertFalse(any(s.spike_active for s in states[-3:]))


if __name__ == "__main__":
    unittest.main()

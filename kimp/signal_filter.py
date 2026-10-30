"""
신호 신뢰도 필터 — 급변 뒤 되돌림·지속성으로 Spike / Level shift / Noise 를 가르는 적응형 점수 (docs/filter.md)

무엇을 위해
    봇이 0.25~2초마다 보는 김프 시계열에는 두 거래소 반영 시차·호가 튐으로 생기는 '몇 초짜리 틈' 이 섞여 있다.
    순간값만 보면 그 틈이 진짜 김프 변화와 똑같이 보여 거기에 익절·진입하고, 0.6초 뒤 반대쪽 거래소가 따라오면 손해로
    끝난다 (BLEND #36·#38·#42 2026-10-30). 이 모듈은 시계열 x(t) 를 받아 **신뢰도 점수 S(t) ∈ [0,1]** 하나를 돌려준다.
    원본은 바꾸지 않는다. 엔진은 "조건 충족 그리고 S ≥ 컷" 일 때만 주문한다 (연결은 docs/filter.md 3단계).

어떻게 (명세 → 수식, docs/filter.md 2번)
    d = x(t) − x(t−1). 평소 변화폭 = 최근 W_mad 초 |d| 의 중앙값(MAD 꼴), 바닥은 호가 한 칸의 절반 (칸 코인은 d 가 대부분 0).
    Z = |d| / (1.4826 × 평소 변화폭). Z ≥ z_spike 면 '급변 시작': 급변 전 값 x0, 급변폭 Δ 를 기억하고 점수를 p_reset 로 내린다.
    급변 표시가 살아 있는 동안 매 샘플 지속 증거 E = 0.6·r + 0.2·c + 0.2·s
        r = (x − x0)/Δ 를 0~1 로 자른 것 (1 = 새 수준 그대로, 0 = 전부 반납)
        c = 최근 w_c 초 변화 부호가 Δ 와 같거나 0 인 비율 (방향 일관성)
        s = 2 − (급변 뒤 w_c 초 |d| 중앙값 / 평소 변화폭) 을 0~1 로 자른 것 (급변 뒤 흔들림이 평소 이하면 1, 평소의 2배면 0)
        c·s 는 급변 뒤 샘플로만 잰다 (2026-10-31)
    급변 표시가 없으면 E = 1 (완만한 변화는 처음부터 믿는다).
    **신호 켜짐도 급변으로 본다** (2026-10-31): update(…, signal_on=True) 가 처음 들어오는 순간(기준을 막 넘은 순간)을 급변
    시작으로 다뤄 점수를 p_reset 로 내린다. 11시간·40코인 이벤트 기록에서 '켜지자마자 행동' 은 0.6초 뒤 손해≥0.2%p 가 17%,
    1초 살아남은 신호만 행동하면 4%, 3초면 2% 였다 — 튐은 Z 가 커서가 아니라 '막 켜진 것' 이라서 거른다.
    P = (1−α)·P + α·E, α = 1 − e^(−Δt/τ)  — 샘플 간격이 불규칙해서 시간 감쇠로.
    r < r_revert 면 Spike 확정: 표시를 지우고 P = 0.   급변 뒤 w_shift 초 동안 r ≥ r_hold 면 Level shift 확정: 표시를 지우고
    x0 를 새 수준으로 (이후 완만한 변화로 취급). 급변 표시 중 또 급변이 오면 새 급변으로 다시 시작.
    S = P (1차 구현. 명세의 Stability 곱은 검증 뒤 — 이중 페널티가 확인되면 넣지 않는다).

상수 (FilterParams) 는 무차원이라 코인별 손질이 없다. 값은 수집 데이터(코인 40개)로 정한다 — 우리 코인만으로 맞추지 않는다.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional, Tuple


@dataclass(frozen=True)
class FilterParams:
    """민감도 상수 (전부 무차원·초 단위). 기본값은 자릿수 감각이고 검증(docs/filter.md 2단계)에서 정한다"""
    tau_sec: float = 8.0          # 지속 증거를 누적하는 기억 길이 (초)
    z_spike: float = 4.0          # 이 배수(평소 변화폭 대비) 이상 변하면 급변
    cut: float = 0.7              # 이 점수 이상이어야 신호를 믿는다
    p_reset: float = 0.1          # 급변 직후 점수
    r_revert: float = 0.3         # 되돌림 비율이 이 아래로 떨어지면 Spike 확정
    r_hold: float = 0.7           # 급변 뒤 w_shift 초 동안 이 이상이면 Level shift 확정
    w_mad_sec: float = 300.0      # 평소 변화폭을 재는 창
    w_c_sec: float = 10.0         # 방향 일관성·안정성을 재는 짧은 창
    w_shift_sec: float = 60.0     # Level shift 확정까지의 관찰 시간
    min_scale: float = 1e-4       # 평소 변화폭 바닥 (%p) — 0 나누기 방지


@dataclass
class FilterState:
    """update() 가 돌려주는 그 순간의 판정 (로그·분석용)"""
    score: float                  # S(t) ∈ [0,1]
    z: float                      # 이번 변화의 급변 배수
    spike_active: bool            # 급변 표시가 살아 있나 (판정 유보 중)
    r: Optional[float]            # 되돌림 비율 (급변 중일 때만)
    verdict: str                  # "" / "spike"(이번 샘플에 Spike 확정) / "shift"(Level shift 확정)


class SignalFilter:
    """
    시계열 하나(코인 하나의 김프, 포지션 하나의 순손익)에 대한 상태 기계. update(t, x, tick_pct) 를 시각순으로 부른다.

    tick_pct: 그 순간 업비트 호가 한 칸의 % (kimp.premium.upbit_krw_tick(가격)/가격×100). 평소 변화폭의 바닥(칸/2)에 쓴다
    """

    def __init__(self, params: FilterParams = FilterParams()):
        self.p = params
        self._t: Optional[float] = None
        self._x: Optional[float] = None
        self._abs_d: Deque[Tuple[float, float]] = deque()      # (t, |d|) 최근 w_mad
        self._d: Deque[Tuple[float, float]] = deque()          # (t, d) 최근 w_c
        self.score: float = 1.0                                # 급변이 없으면 처음부터 믿는다
        self._spike: Optional[Tuple[float, float, float]] = None   # (t0, x0, Δ). Δ=0 이면 되돌림을 재지 않는다
        self._signal_on = False
        self._on_since: Optional[float] = None                 # 신호가 켜진 시각 (로그용)

    # ------------------------------------------------------------------
    @staticmethod
    def _median(values) -> float:
        s = sorted(values)
        n = len(s)
        if not n:
            return 0.0
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    def _scale(self, tick_pct: float) -> float:
        """평소 변화폭 (|d| 중앙값), 바닥은 호가 한 칸의 절반"""
        base = self._median([v for _, v in self._abs_d]) if self._abs_d else 0.0
        return max(base, tick_pct / 2, self.p.min_scale)

    def update(self, t: float, x: float, tick_pct: float = 0.0, signal_on: Optional[bool] = None) -> FilterState:
        """
        샘플 하나 반영 → 판정. t 는 초 (단조 증가), x 는 % (김프 또는 순손익)

        signal_on: 이 샘플에서 매매 조건(기준 이하 / 목표 이상)이 참인가. 거짓 → 참으로 바뀌는 순간을 급변 시작으로 본다.
                   None 이면 Z 급변만 본다 (오프라인 분석용)
        """
        p = self.p
        rising = bool(signal_on) and not self._signal_on
        if signal_on is not None:
            self._signal_on = bool(signal_on)
            if rising:
                self._on_since = t
            elif not signal_on:
                self._on_since = None
        if self._t is None:
            self._t, self._x = t, x
            if rising:
                self._spike, self.score = (t, x, 0.0), p.p_reset
                return FilterState(self.score, 0.0, True, None, "")
            return FilterState(self.score, 0.0, False, None, "")
        dt = max(1e-6, t - self._t)
        d = x - self._x
        # 창 정리
        self._abs_d.append((t, abs(d)))
        while self._abs_d and t - self._abs_d[0][0] > p.w_mad_sec:
            self._abs_d.popleft()
        self._d.append((t, d))
        while self._d and t - self._d[0][0] > p.w_c_sec:
            self._d.popleft()
        scale = self._scale(tick_pct)
        z = abs(d) / (1.4826 * scale)
        verdict = ""

        r: Optional[float] = None
        if self._spike is not None:
            t0, x0, delta = self._spike
            r = (x - x0) / delta if delta else None
            if r is not None and r < p.r_revert:
                # 급변분의 대부분을 반납 → Spike 확정. (되돌아오는 움직임도 크니 '새 급변' 판정보다 먼저 본다)
                self._spike = None
                self.score = 0.0
                verdict = "spike"
                self._t, self._x = t, x
                return FilterState(self.score, z, False, r, verdict)
            if self._signal_on is False and signal_on is not None and delta == 0:
                # 켜짐으로 시작한 급변인데 신호가 꺼짐 → 반납한 것 (되돌림 비율을 못 재는 경우의 Spike 확정)
                self._spike = None
                self.score = 0.0
                self._t, self._x = t, x
                return FilterState(self.score, z, False, 0.0, "spike")

        if (z >= p.z_spike and d != 0) or rising:
            # 급변 시작: Z 가 큰 변화, 또는 신호가 막 켜짐 (급변 중 또 오면 새로 시작). 급변 전 값은 직전 샘플.
            # 켜짐으로 시작할 때 d 가 작으면(기준을 살짝 넘음) 되돌림 비율은 못 재고 시간·안정성으로만 올라간다
            self._spike = (t, self._x, d if abs(d) >= p.min_scale else 0.0)
            self.score = p.p_reset
            self._t, self._x = t, x
            return FilterState(self.score, z, True, 1.0 if d else None, "")

        if self._spike is not None:
            t0, x0, delta = self._spike
            r_clip = min(1.0, max(0.0, r)) if r is not None else 1.0
            # 방향 일관성·안정성은 급변 **뒤** 샘플로만 잰다 (급변 전 평소 흔들림이 섞이면 깔끔한 유지도 느리게 믿게 된다)
            after = [v for tt, v in self._d if tt > t0]
            same = [v for v in after if v == 0 or delta == 0 or (v > 0) == (delta > 0)]
            c = len(same) / len(after) if after else 1.0
            recent = self._median([abs(v) for v in after]) if after else 0.0
            s = min(1.0, max(0.0, 2.0 - recent / scale))       # 평소 수준 이하 흔들림 = 1, 평소의 2배 = 0
            e = 0.6 * r_clip + 0.2 * c + 0.2 * s
            if t - t0 >= p.w_shift_sec and (r is None or r >= p.r_hold):
                # 새 수준이 자리 잡음 → Level shift 확정, 이후는 완만한 변화로
                self._spike = None
                verdict = "shift"
        else:
            e = 1.0
        alpha = 1.0 - math.exp(-dt / p.tau_sec)
        self.score = (1 - alpha) * self.score + alpha * e
        self._t, self._x = t, x
        return FilterState(self.score, z, self._spike is not None, r, verdict)

    @property
    def spike_active(self) -> bool:
        return self._spike is not None

    def describe(self, t: float) -> str:
        """로그용 한 줄. 예) '신뢰도 0.42 (신호 1.3초째, 급변 뒤 판정 중)' / '신뢰도 1.00'"""
        text = f"신뢰도 {self.score:.2f}"
        extra = []
        if self._on_since is not None:
            extra.append(f"신호 {t - self._on_since:.1f}초째")
        if self._spike is not None:
            extra.append("급변 뒤 판정 중")
        return text + (f" ({', '.join(extra)})" if extra else "")

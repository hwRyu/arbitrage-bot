"""
지정가 FOK 기록 — "왜 놓쳤나" 를 나중에 가릴 수 있게 판단 때 호가와 놓친 직후 호가를 남긴다 (docs/issues.md #3 C)

업비트 코인 진입 매수·익절 매도는 판단가 지정가 FOK 다 (전량 즉시 체결 아니면 취소). 놓치면 이유가 셋 중 하나다.

    두 칸 걸침  판단 때부터 지정가 안쪽 물량이 우리 수량보다 적었다.
                (평균가를 호가 단위로 내림한 지정가라, 주문이 두 칸에 걸치면 첫 칸 물량만 쓸 수 있다 — 이슈 #3 의 구조 문제)
    호가 이동   놓친 직후 맨 앞 호가가 지정가 밖으로 나갔다 (매수면 매도 1호가 > 지정가). 김프가 한순간 튄 신호에서 흔하다.
    물량 부족   맨 앞 호가는 아직 지정가 안인데, 그 안쪽 물량이 우리 수량보다 줄었다 (누가 먼저 사 감·팔아 감).
    다시 충분   놓친 직후에는 지정가 안쪽 물량이 다시 충분하다 (그 순간만 비었다).
    알 수 없음  놓친 직후 호가를 못 받았다.

판단 때 호가(FokBook)는 브로커가 주문 직전에 만들어 [지정가 주문] 로그로 남기고 놓침 예외에 붙인다.
놓친 직후 호가는 엔진이 확인 시세를 한 번 더 받아서 classify_miss 로 비교한다 (거래소 시세 조회만, 주문 없음).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from kimp.premium import Level

TOP_LEVELS = 3   # 기록에 남기는 호가 줄 수


@dataclass(frozen=True)
class FokBook:
    """지정가 FOK 주문을 낼 때 본 업비트 코인 호가 (매수면 매도호가, 매도면 매수호가)"""
    side: str                      # 'buy' / 'sell'
    limit: float                   # 지정가
    qty: float                     # 우리 주문 수량 (코인 개수)
    within_qty: float              # 지정가 안쪽에 쌓인 물량 합 (코인 개수)
    levels_used: Optional[int]     # 우리 수량을 채우는 데 드는 호가 줄 수 (호가가 모자라면 None)
    top: Tuple[Level, ...]         # 맨 앞 몇 줄 (가격, 물량)

    @property
    def spans_beyond_limit(self) -> bool:
        """판단 때부터 지정가 안쪽 물량이 모자랐나 (= 이 지정가로는 처음부터 전량 체결이 불가능)"""
        return self.within_qty < self.qty * (1 - 1e-9)

    def describe(self) -> str:
        """한 줄 요약. 예) 매수 지정가 99.8 · 수량 268 · 안쪽 물량 3,000 (1칸) · 호가 99.8×3000 99.9×10000 100×5000"""
        what = "매수" if self.side == "buy" else "매도"
        used = f"{self.levels_used}칸" if self.levels_used else "호가 모자람"
        return (f"{what} 지정가 {self.limit:g} · 수량 {self.qty:,.4g} · 안쪽 물량 {self.within_qty:,.6g} ({used}) · "
                f"호가 {book_text(self.top)}")


def book_text(levels: Sequence[Level]) -> str:
    """호가 줄들을 '가격×물량' 으로 (물량은 유효숫자 6자리)"""
    return " ".join(f"{p:g}×{q:.6g}" for p, q in levels) or "없음"


def _inside(price: float, limit: float, side: str) -> bool:
    """이 호가가 지정가 안쪽인가 (매수: 지정가 이하, 매도: 지정가 이상)"""
    return price <= limit if side == "buy" else price >= limit


def _levels_used(levels: Sequence[Level], qty: float) -> Optional[int]:
    """qty 를 채우는 데 드는 줄 수 (모자라면 None)"""
    left = qty
    for i, (_price, size) in enumerate(levels, start=1):
        left -= size
        if left <= qty * 1e-9:
            return i
    return None


def snapshot(levels: Sequence[Level], side: str, limit: float, qty: float) -> FokBook:
    """
    주문 직전 호가로 FokBook 만들기

    Args:
        levels: 매수 주문이면 매도호가(asks), 매도 주문이면 매수호가(bids). 좋은 가격부터 (premium.OrderBook 순서)
        side: 'buy' / 'sell'
        limit: 지정가
        qty: 우리 주문 수량 (코인 개수)
    """
    within = sum(size for price, size in levels if _inside(price, limit, side))
    return FokBook(side=side, limit=limit, qty=qty, within_qty=within,
                   levels_used=_levels_used(levels, qty), top=tuple(levels[:TOP_LEVELS]))


def classify_miss(judged: Optional[FokBook], now_levels: Optional[Sequence[Level]]) -> Tuple[str, str]:
    """
    놓친 이유 가리기

    Args:
        judged: 주문 때 본 호가 (없으면 판단 근거가 없어 '알 수 없음')
        now_levels: 놓친 직후 같은 쪽 호가 (매수면 asks, 매도면 bids). 못 받았으면 None

    Returns:
        (이유, 설명). 이유는 '두 칸 걸침' / '호가 이동' / '물량 부족' / '다시 충분' / '알 수 없음'
    """
    if judged is None:
        return "알 수 없음", "주문 때 호가 기록 없음"
    if judged.spans_beyond_limit:
        return "두 칸 걸침", (f"판단 때 지정가 {judged.limit:g} 안쪽 물량 {judged.within_qty:,.6g} < "
                            f"우리 수량 {judged.qty:,.6g} (평균가 지정가라 첫 칸만 쓸 수 있음, 이슈 #3)")
    if not now_levels:
        return "알 수 없음", "놓친 직후 호가를 못 받음"
    best = now_levels[0][0]
    now = f"지금 {book_text(now_levels[:TOP_LEVELS])}"
    if not _inside(best, judged.limit, judged.side):
        return "호가 이동", f"맨 앞 호가 {best:g} 가 지정가 {judged.limit:g} 밖으로 | {now}"
    within_now = sum(size for price, size in now_levels if _inside(price, judged.limit, judged.side))
    if within_now < judged.qty * (1 - 1e-9):
        return "물량 부족", (f"지정가 안쪽 물량 {judged.within_qty:,.6g} → {within_now:,.6g} "
                         f"(우리 수량 {judged.qty:,.6g}) | {now}")
    return "다시 충분", f"놓친 직후엔 지정가 안쪽 물량 {within_now:,.6g} 로 다시 충분 (그 순간만 비었음) | {now}"

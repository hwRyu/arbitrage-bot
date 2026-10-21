"""
달러 환율(USD/KRW) 가져오기

이 값이 왜 필요한가?
------------------------------------------------------------------
업비트 코인은 '원', 바이비트 코인은 'USDT' 로 가격이 붙어 있다.
둘을 비교하려면 "1 USDT 는 몇 원짜리인가" 를 정해야 하는데,
우리 전략은 그 값을 **달러 환율**로 정한다. (1 USDT ≈ 1 달러이기 때문)

    예) 달러 환율 1,325원 → 바이비트 1.35 USDT 짜리 XRP 는 1,788.75원짜리로 본다
        업비트 XRP 가 1,770원이면 → 업비트가 약 1% 싸다 (김프 -1%)

김프 사이트들도 같은 방식으로 계산한다. (docs/concept.md 3번, 7번 참고)

동작 방식 (old 봇은 환율 조회가 자주 실패해서 스캔 전체가 멈췄다 → 그걸 막는 설계)
------------------------------------------------------------------
1) 정해진 간격(FX_REFRESH_SEC)마다 yfinance 에서 최신 환율을 받아온다.
2) 실패하면 마지막으로 성공한 값을 그대로 쓴다. (환율은 몇 분 사이에 거의 안 움직인다)
3) 마지막 성공이 너무 오래됐으면(FX_MAX_STALE_SEC) '오래된 값' 이라고 알려준다.
   → 엔진은 이때 새 진입만 멈추고, 이미 가진 포지션은 계속 관리한다.
4) FIXED_USD_KRW 를 설정하면 조회하지 않고 그 숫자를 계속 쓴다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FxQuote:
    """
    환율 한 개 값

    Attributes:
        rate: 1달러가 몇 원인지 (예: 1325.0)
        age_sec: 이 값을 받아온 지 몇 초 지났는지
        is_stale: 너무 오래된 값인지 (True 면 신규 진입 금지)
        is_fixed: 설정으로 고정한 값인지
    """

    rate: float
    age_sec: float
    is_stale: bool
    is_fixed: bool = False


def fetch_usd_krw_from_yfinance() -> float:
    """
    yfinance 에서 지금 달러 환율 한 개를 받아온다 (1분봉의 마지막 종가)

    Returns:
        1달러가 몇 원인지
    Raises:
        Exception: 네트워크 오류나 빈 응답일 때
    """
    import yfinance as yf  # 무거운 라이브러리라 실제로 쓸 때만 불러온다

    history = yf.Ticker("KRW=X").history(period="1d", interval="1m")
    if history.empty:
        raise RuntimeError("yfinance 가 빈 환율 데이터를 돌려줬습니다")
    rate = float(history["Close"].iloc[-1])
    # 말도 안 되는 값이 오면 쓰지 않는다 (예: 0, 또는 단위가 이상한 값)
    if not 500 < rate < 5000:
        raise RuntimeError(f"환율 값이 이상합니다: {rate}")
    return rate


class FxProvider:
    """
    달러 환율 공급기

    사용법:
        fx = FxProvider(refresh_sec=60, max_stale_sec=1800)
        quote = fx.get()          # 매 스캔마다 호출. 필요할 때만 실제로 새로 받아온다
        if quote is None: ...     # 아직 한 번도 못 받아옴 (봇 시작 직후 실패)
        if quote.is_stale: ...    # 너무 오래된 값 → 새 진입 하지 말 것
    """

    def __init__(
        self,
        refresh_sec: float = 60.0,
        max_stale_sec: float = 1800.0,
        fixed_rate: Optional[float] = None,
        fetcher: Callable[[], float] = fetch_usd_krw_from_yfinance,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        Args:
            refresh_sec: 몇 초마다 새로 받아올지
            max_stale_sec: 마지막 성공 후 몇 초가 지나면 '오래된 값' 으로 볼지
            fixed_rate: 값을 넣으면 조회 없이 이 환율로 고정
            fetcher: 실제로 환율을 받아오는 함수 (테스트에서 가짜 함수로 바꿔 끼움)
            clock: 현재 시각 함수 (테스트용)
        """
        self.refresh_sec = refresh_sec
        self.max_stale_sec = max_stale_sec
        self.fixed_rate = fixed_rate
        self._fetcher = fetcher
        self._clock = clock

        self._rate: Optional[float] = None  # 마지막으로 성공한 환율
        self._fetched_at: Optional[float] = None  # 그 값을 받은 시각
        self._last_try_at: Optional[float] = None  # 마지막으로 시도한 시각 (실패 포함)

    def get(self) -> Optional[FxQuote]:
        """
        지금 쓸 환율을 돌려준다

        - 고정값이 있으면 항상 그 값
        - 갱신할 때가 됐으면 새로 받아오고, 실패하면 이전 값을 그대로 쓴다
        - 한 번도 성공한 적이 없으면 None
        """
        if self.fixed_rate:
            return FxQuote(rate=self.fixed_rate, age_sec=0.0, is_stale=False, is_fixed=True)

        now = self._clock()
        if self._last_try_at is None or now - self._last_try_at >= self.refresh_sec:
            self._last_try_at = now
            try:
                rate = self._fetcher()
                if self._rate is None:
                    logger.info("달러 환율 첫 조회 성공: %.2f원", rate)
                self._rate, self._fetched_at = rate, now
            except Exception as exc:
                # 실패해도 멈추지 않는다. 이전 값이 있으면 그걸 쓴다.
                if self._rate is None:
                    logger.error("달러 환율 조회 실패 (아직 쓸 값 없음): %s", exc)
                else:
                    logger.warning("달러 환율 조회 실패, 이전 값 %.2f원 사용: %s", self._rate, exc)

        if self._rate is None or self._fetched_at is None:
            return None
        age = now - self._fetched_at
        return FxQuote(rate=self._rate, age_sec=age, is_stale=age > self.max_stale_sec)

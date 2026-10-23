"""
달러 환율(USD/KRW) 가져오기 — **표시용이다. 매매 판단에는 쓰지 않는다**

왜 매매에 안 쓰나
------------------------------------------------------------------
자본을 USDT 로 재기 때문이다. 업비트에서 USDT 를 팔아 코인을 사고, 청산할 때 코인을 팔아
USDT 를 되산다. 그래서 "1 USDT 는 몇 원인가" 의 답은 환율이 아니라 **업비트 USDT 가격**이고,
김프도 그 값으로 나눈다 (kimp/premium.py 의 premium_percent).

    봇 김프(코인 몫) = 업비트 코인 원화가 ÷ (바이비트 가격 x 업비트 USDT 원화가) − 1

업비트 USDT 가격은 팔 때와 되살 때 양쪽에 똑같이 들어가 약분되므로, 테더 프리미엄과 달러
환율이 움직여도 손익에 섞이지 않는다. 예전 설계(원화 자본 + 달러 환율 기준 김프)에서는
환율이 손익에 직접 들어왔고, 실제로 SOL 거래에서 김프로 +1.7%p 를 벌었는데 환율이 −4.3%
빠져서 원화 손익이 −3% 가 된 적이 있다 (docs/concept.md 3~4번).

그럼 어디에 쓰나 — 세 가지 '보여주는 값' 뿐이다
------------------------------------------------------------------
    테더 프리미엄  = 업비트 USDT 가격 ÷ 달러 환율 − 1
    사이트식 김프  = 달러 환율을 분모에 넣은 김프 (김프 사이트 숫자와 비슷해진다)
    로그·화면 표시

그래서 환율을 못 받아도 **매매는 그대로 돈다.** 위 세 칸만 비어서 나온다
(엔진은 fx 가 None 이어도 진입·청산을 한다 — kimp/engine.py 의 fx 인자 설명).

동작 방식 (old 봇은 환율 조회가 자주 실패해서 스캔 전체가 멈췄다 → 그걸 막는 설계)
------------------------------------------------------------------
1) 정해진 간격(FX_REFRESH_SEC)마다 yfinance 에서 최신 환율을 받아온다.
2) 실패하면 마지막으로 성공한 값을 그대로 쓴다. (환율은 몇 분 사이에 거의 안 움직인다)
   값이 얼마나 묵었는지는 따로 재지 않는다 — 표시용이라 낡아도 매매에 영향이 없다.
   yfinance 는 간헐적으로 빈 데이터를 준다 → 한두 번 실패는 조용히(DEBUG), ALERT_AFTER_FAILURES 번
   **연속** 실패해야 WARNING(텔레그램) 한 번, 그 뒤로는 ALERT_EVERY_FAILURES 번마다. 복구되면 한 번 알린다.
3) 한 번도 성공하지 못했으면(봇 시작 직후 실패) None 을 돌려준다. 이때도 매매는 그대로 돈다.
4) FIXED_USD_KRW 를 설정하면 조회하지 않고 그 숫자를 계속 쓴다.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 연속 몇 번 실패해야 알리나 (60초 간격이면 3번 = 3분). 그 뒤로는 몇 번마다 다시 알리나
ALERT_AFTER_FAILURES = 3
ALERT_EVERY_FAILURES = 30   # 30분마다


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
        fx = FxProvider(refresh_sec=60)
        rate = fx.get()           # 매 스캔마다 호출. 필요할 때만 실제로 새로 받아온다
        if rate is None: ...      # 아직 한 번도 못 받아옴 (봇 시작 직후 실패) → 표시만 비워 둔다
    """

    def __init__(
        self,
        refresh_sec: float = 60.0,
        fixed_rate: Optional[float] = None,
        fetcher: Callable[[], float] = fetch_usd_krw_from_yfinance,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        Args:
            refresh_sec: 몇 초마다 새로 받아올지
            fixed_rate: 값을 넣으면 조회 없이 이 환율로 고정
            fetcher: 실제로 환율을 받아오는 함수 (테스트에서 가짜 함수로 바꿔 끼움)
            clock: 현재 시각 함수 (테스트용)
        """
        self.refresh_sec = refresh_sec
        self.fixed_rate = fixed_rate
        self._fetcher = fetcher
        self._clock = clock

        self._rate: Optional[float] = None  # 마지막으로 성공한 환율 (실패해도 덮어쓰지 않는다)
        self._last_try_at: Optional[float] = None  # 마지막으로 시도한 시각 (실패 포함)
        self.failures = 0  # 연속 실패 횟수 (성공하면 0)

    def get(self) -> Optional[float]:
        """
        지금 쓸 환율(원)을 돌려준다

        - 고정값이 있으면 항상 그 값
        - 갱신할 때가 됐으면 새로 받아오고, 실패하면 이전 값을 그대로 쓴다
        - 한 번도 성공한 적이 없으면 None (표시만 비고 매매는 계속된다)
        """
        if self.fixed_rate:
            return self.fixed_rate

        now = self._clock()
        if self._last_try_at is None or now - self._last_try_at >= self.refresh_sec:
            self._last_try_at = now
            try:
                rate = self._fetcher()
                if self._rate is None:
                    logger.info("달러 환율 첫 조회 성공: %.2f원", rate)
                if self.failures >= ALERT_AFTER_FAILURES:
                    logger.warning("달러 환율 조회 복구 (연속 %d번 실패 뒤): %.2f원", self.failures, rate)
                self._rate = rate
                self.failures = 0
            except Exception as exc:
                # 실패해도 멈추지 않는다. 이전 값이 있으면 그걸 쓴다.
                # 간헐적 실패는 조용히, 연속으로 이어질 때만 알린다 (표시용 값이라 급하지 않다)
                self.failures += 1
                tail = "(아직 쓸 값 없음)" if self._rate is None else f"(이전 값 {self._rate:.2f}원 사용)"
                if self.failures == ALERT_AFTER_FAILURES or (
                        self.failures > ALERT_AFTER_FAILURES and self.failures % ALERT_EVERY_FAILURES == 0):
                    logger.warning("달러 환율 조회가 %d번 연속 실패 %s: %s", self.failures, tail, exc)
                else:
                    logger.debug("달러 환율 조회 실패 %d번째 %s: %s", self.failures, tail, exc)

        return self._rate

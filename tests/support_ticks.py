"""
테스트 도우미: 가짜 시세용 '연속 호가' — 업비트 지정가를 호가 단위에 맞추지 않는다

왜 필요한가
    업비트 코인 진입·익절은 판단가 지정가 FOK 다 (2026-10-29 시장가 모드 제거). 지정가는 호가 단위에 맞춰
    살 때 내림·팔 때 올림 한다 (kimp.premium.upbit_limit_price). 실제 업비트 호가는 늘 단위 위에 있어서 문제가 없지만,
    엔진·시뮬레이션 테스트의 가짜 시세는 김프를 정밀하게 맞추려고 1,386.14원 같은 '단위 밖' 가격을 쓴다.
    그 가격을 내림하면 매도호가보다 낮아져 모든 진입이 '놓침' 이 된다.

    그래서 이 테스트들에서만 단위 맞춤을 끈다 (지정가 = 판단가 그대로). FOK 흐름(지정가 주문, 놓치면 되돌리기,
    익절 보류)은 그대로 돈다. 단위 맞춤 자체는 tests/test_fok_orders.py 의 TickTest 와 실거래 흉내 테스트가 본다.
    1e-12 만큼 바깥으로 두는 건 평균가 계산의 부동소수 오차로 같은 가격이 놓침이 되지 않게 하려는 것이다.
"""
from unittest import mock


def continuous_upbit_ticks():
    """모듈 전체에 쓰는 patch (setUpModule 에서 start, tearDownModule 에서 stop)"""
    return mock.patch("kimp.broker.upbit_limit_price",
                      lambda price, side: price * (1 + 1e-12) if side == "buy" else price * (1 - 1e-12))

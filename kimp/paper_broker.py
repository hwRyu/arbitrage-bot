"""
페이퍼 트레이딩 (가짜 주문, 진짜 호가)

진입·청산·되돌리기·송금·대조 로직은 실거래와 똑같이 kimp/broker.py (HedgeBroker) 에 있다.
페이퍼는 실행기만 PaperExecutor (kimp/executor.py) 를 쓴다: 그 순간 진짜 호가창으로 체결가를 계산하고
DB 장부(paper_balances)를 바꾼다. 그래서 호가가 얇은 코인은 페이퍼에서도 손해(슬리피지)가 그대로 보인다.

이 파일은 예전 이름(PaperBroker, UPBIT_USDT ...)으로 불러 쓰던 곳을 위해 남겨둔 연결 파일이다.
"""
from kimp.broker import (  # noqa: F401
    EntryRejected,
    ExitValuation,
    HedgeBroker,
    InsufficientBalance,
    PaperBroker,
    entry_legs_of,
)
from kimp.executor import BYBIT_USDT, UPBIT_KRW, UPBIT_USDT, OrderFailed, PaperExecutor, coin_key  # noqa: F401

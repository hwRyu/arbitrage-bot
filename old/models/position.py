"""
Data classes for position and market data
These are in-memory data structures, separate from DB models
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any


@dataclass
class MarketPrice:
    """시장 가격 정보"""
    coin: str
    krw_price: float  # 업비트 가격 (KRW)
    usd_price: float  # 바이비트 가격 (USD)
    usdt_krw: float   # USDT/KRW 가격
    exchange_rate: float  # USD/KRW 환율
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def premium(self) -> float:
        """
        김프 계산 (기본 공식)
        김프 = (업비트 원화 가격 / (바이비트 USD 가격 * USD/KRW 환율)) - 1

        **중요**: USD/KRW 환율 사용 (USDT/KRW 아님!)
        """
        if self.usd_price == 0 or self.exchange_rate == 0:
            return 0.0
        theoretical_krw = self.usd_price * self.exchange_rate
        return ((self.krw_price / theoretical_krw) - 1) * 100  # %

    def to_dict(self) -> Dict[str, Any]:
        return {
            'coin': self.coin,
            'krw_price': self.krw_price,
            'usd_price': self.usd_price,
            'usdt_krw': self.usdt_krw,
            'exchange_rate': self.exchange_rate,
            'premium': self.premium,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class OrderbookDepth:
    """호가창 깊이 정보"""
    coin: str
    exchange: str
    asks_depth: float  # 매도 호가 총량
    bids_depth: float  # 매수 호가 총량
    asks_value: float  # 매도 호가 총 금액
    bids_value: float  # 매수 호가 총 금액
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Position:
    """포지션 정보"""
    coin: str
    amount: float  # 수량
    entry_price_krw: float  # 업비트 진입가
    entry_price_usd: float  # 바이비트 진입가
    leverage: int = 1
    liquidation_price: Optional[float] = None  # 청산가
    upbit_order_id: Optional[str] = None
    bybit_order_id: Optional[str] = None
    bybit_position_id: Optional[str] = None
    entry_premium: Optional[float] = None
    entry_time: datetime = field(default_factory=datetime.now)

    @property
    def position_value_krw(self) -> float:
        """포지션 가치 (KRW)"""
        return self.amount * self.entry_price_krw

    @property
    def position_value_usd(self) -> float:
        """포지션 가치 (USD)"""
        return self.amount * self.entry_price_usd

    @property
    def liquidation_distance(self) -> Optional[float]:
        """청산가까지 거리 (배수)"""
        if self.liquidation_price is None:
            return None
        return self.liquidation_price / self.entry_price_usd

    def to_dict(self) -> Dict[str, Any]:
        return {
            'coin': self.coin,
            'amount': self.amount,
            'entry_price_krw': self.entry_price_krw,
            'entry_price_usd': self.entry_price_usd,
            'leverage': self.leverage,
            'liquidation_price': self.liquidation_price,
            'liquidation_distance': self.liquidation_distance,
            'position_value_krw': self.position_value_krw,
            'entry_premium': self.entry_premium,
            'entry_time': self.entry_time.isoformat()
        }


@dataclass
class TradeResult:
    """거래 결과"""
    success: bool
    exchange: str
    side: str  # buy, sell, open_short, close_short
    coin: str
    amount: float
    price: float
    value: float
    fee: float
    order_id: Optional[str] = None
    error_message: Optional[str] = None
    slippage: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'success': self.success,
            'exchange': self.exchange,
            'side': self.side,
            'coin': self.coin,
            'amount': self.amount,
            'price': self.price,
            'value': self.value,
            'fee': self.fee,
            'order_id': self.order_id,
            'error_message': self.error_message,
            'slippage': self.slippage,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class ValidationResult:
    """진입 검증 결과"""
    is_valid: bool
    coin: str
    reasons: list[str] = field(default_factory=list)  # 실패 이유들
    liquidation_price: Optional[float] = None
    liquidation_buffer: Optional[float] = None
    orderbook_valid: bool = True
    slippage_valid: bool = True

    def add_reason(self, reason: str):
        """실패 이유 추가"""
        self.reasons.append(reason)
        self.is_valid = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'is_valid': self.is_valid,
            'coin': self.coin,
            'reasons': self.reasons,
            'liquidation_price': self.liquidation_price,
            'liquidation_buffer': self.liquidation_buffer,
            'orderbook_valid': self.orderbook_valid,
            'slippage_valid': self.slippage_valid
        }

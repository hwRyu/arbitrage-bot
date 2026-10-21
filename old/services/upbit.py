"""
업비트 거래소 서비스 래퍼
"""
import ccxt
from typing import Optional, Dict, Any
from config.settings import APIKeys, BotConfig
from utils.logger import get_logger

logger = get_logger('upbit')


class UpbitService:
    """
    업비트 거래소 래퍼
    """

    def __init__(self, paper_trading: bool = None):
        """
        업비트 서비스 초기화

        Args:
            paper_trading: 페이퍼 트레이딩 모드 (None이면 설정값 사용)
        """
        self.paper_trading = paper_trading if paper_trading is not None else BotConfig.PAPER_TRADING

        if self.paper_trading:
            logger.info("업비트 서비스 페이퍼 트레이딩 모드로 초기화")
            self.exchange = None
            # 페이퍼 트레이딩 잔고
            self.paper_balances = {
                'KRW': BotConfig.PAPER_INITIAL_KRW
            }
        else:
            logger.info("업비트 서비스 실거래 모드로 초기화")
            self.exchange = ccxt.upbit({
                'apiKey': APIKeys.UPBIT_ACCESS_KEY,
                'secret': APIKeys.UPBIT_SECRET_KEY,
                'enableRateLimit': True,
            })

    def fetch_ticker(self, coin: str) -> Optional[Dict[str, Any]]:
        """
        시세 조회

        Args:
            coin: 코인 심볼 (예: 'BTC', 'SOL')

        Returns:
            시세 데이터 또는 None
        """
        try:
            symbol = f"{coin}/KRW"
            if self.paper_trading:
                # 페이퍼 모드에서도 실제 시세 필요
                # 공개 인스턴스 사용
                exchange = ccxt.upbit({'enableRateLimit': True})
                ticker = exchange.fetch_ticker(symbol)
            else:
                ticker = self.exchange.fetch_ticker(symbol)

            logger.debug(f"업비트 {symbol} 시세: {ticker['last']:.2f} KRW")
            return ticker

        except Exception as e:
            logger.error(f"업비트 {coin} 시세 조회 실패: {e}")
            return None

    def fetch_orderbook(self, coin: str, limit: int = 10) -> Optional[Dict[str, Any]]:
        """
        호가창 조회

        Args:
            coin: 코인 심볼
            limit: 조회 레벨 수

        Returns:
            호가창 데이터 또는 None
        """
        try:
            symbol = f"{coin}/KRW"
            if self.paper_trading:
                exchange = ccxt.upbit({'enableRateLimit': True})
                orderbook = exchange.fetch_order_book(symbol, limit=limit)
            else:
                orderbook = self.exchange.fetch_order_book(symbol, limit=limit)

            return orderbook

        except Exception as e:
            logger.error(f"업비트 {coin} 호가창 조회 실패: {e}")
            return None

    def fetch_balance(self, currency: str = 'KRW') -> float:
        """
        잔고 조회

        Args:
            currency: 통화 심볼 (기본값: KRW)

        Returns:
            잔고 금액
        """
        try:
            if self.paper_trading:
                return self.paper_balances.get(currency, 0.0)

            balance = self.exchange.fetch_balance()
            return balance.get(currency, {}).get('free', 0.0)

        except Exception as e:
            logger.error(f"업비트 {currency} 잔고 조회 실패: {e}")
            return 0.0


    def create_market_order(self, coin: str, side: str, amount: float = None,
                       cost: float = None) -> Optional[Dict[str, Any]]:
        """
        시장가 주문 생성

        Args:
            coin: 코인 심볼
            side: 'buy' 또는 'sell'
            amount: 거래 수량 (매도 시 사용, 코인 단위)
            cost: 거래 금액 (매수 시 사용, KRW 단위)

        Returns:
            주문 결과 또는 None

        Note:
            - 매수: 'cost'를 KRW 단위로 지정
            - 매도: 'amount'를 코인 단위로 지정
        """
        try:
            symbol = f"{coin}/KRW"

            if self.paper_trading:
                return self._paper_create_order(coin, side, amount, cost)

            # 실거래
            if side == 'buy':
                if cost is None:
                    raise ValueError("매수 주문 시 'cost' (KRW) 필수 지정")
                # 업비트: 시장가 매수는 금액 지정 방식
                order = self.exchange.create_order(
                    symbol=symbol,
                    type='market',
                    side='buy',
                    amount=None,
                    params={'cost': cost}  # 업비트는 매수 시 금액 지정
                )
            else:  # sell
                if amount is None:
                    raise ValueError("매도 주문 시 'amount' (코인) 필수 지정")
                order = self.exchange.create_order(
                    symbol=symbol,
                    type='market',
                    side='sell',
                    amount=amount
                )

            # 주문 후 체결 정보 조회
            if order and order.get('id'):
                try:
                    import time
                    time.sleep(0.5)  # 체결 대기
                    
                    filled_order = self.exchange.fetch_order(order['id'], symbol)
                    
                    # 체결 정보로 업데이트
                    order.update({
                        'filled': filled_order.get('filled'),
                        'amount': filled_order.get('filled'),  # 실제 체결 수량
                        'price': filled_order.get('average') or filled_order.get('price'),
                        'cost': filled_order.get('cost'),
                        'fee': filled_order.get('fee')
                    })
                    
                    logger.info(
                        f"업비트 {side.upper()} 주문 체결: {order.get('id')} "
                        f"({order.get('filled')} {coin} @ {order.get('price')} KRW)"
                    )
                except Exception as fetch_error:
                    logger.warning(f"주문 정보 조회 실패 ({order.get('id')}): {fetch_error}")
            else:
                logger.info(f"업비트 {side.upper()} 주문 생성: {order.get('id') if order else 'None'}")
            
            return order

        except Exception as e:
            logger.error(f"업비트 주문 생성 실패 ({side} {coin}): {e}")
            return None
        

    def _paper_create_order(self, coin: str, side: str, amount: float = None,
                           cost: float = None) -> Dict[str, Any]:
        """
        페이퍼 트레이딩 주문 시뮬레이션

        Args:
            coin: 코인 심볼
            side: 'buy' 또는 'sell'
            amount: 코인 수량
            cost: KRW 금액

        Returns:
            시뮬레이션된 주문 결과
        """
        # 현재 시세 조회
        ticker = self.fetch_ticker(coin)
        if not ticker:
            raise Exception("페이퍼 주문을 위한 시세 조회 실패")

        price = ticker['last']

        # 수량 및 금액 계산
        if side == 'buy':
            if cost is None:
                raise ValueError("페이퍼 매수 주문 시 'cost' 필수 지정")
            actual_amount = cost / price
            actual_cost = cost
        else:  # sell
            if amount is None:
                raise ValueError("페이퍼 매도 주문 시 'amount' 필수 지정")
            actual_amount = amount
            actual_cost = amount * price

        # 잔고 확인
        if side == 'buy':
            krw_balance = self.paper_balances.get('KRW', 0)
            if krw_balance < actual_cost:
                raise Exception(f"KRW 잔고 부족: {krw_balance} < {actual_cost}")
        else:  # sell
            coin_balance = self.paper_balances.get(coin, 0)
            if coin_balance < actual_amount:
                raise Exception(f"{coin} 잔고 부족: {coin_balance} < {actual_amount}")

        # 페이퍼 거래 실행
        fee_rate = 0.0005  # 0.05%
        fee = actual_cost * fee_rate

        if side == 'buy':
            self.paper_balances['KRW'] = self.paper_balances.get('KRW', 0) - actual_cost
            self.paper_balances[coin] = self.paper_balances.get(coin, 0) + actual_amount
        else:  # sell
            self.paper_balances[coin] = self.paper_balances.get(coin, 0) - actual_amount
            self.paper_balances['KRW'] = self.paper_balances.get('KRW', 0) + (actual_cost - fee)

        logger.info(f"[페이퍼] 업비트 {side.upper()}: {actual_amount:.6f} {coin} @ {price:.2f} KRW (수수료: {fee:.2f})")

        return {
            'id': f'paper_{coin}_{side}_{hash(str(price))}',
            'symbol': f'{coin}/KRW',
            'type': 'market',
            'side': side,
            'price': price,
            'amount': actual_amount,
            'cost': actual_cost,
            'fee': {'cost': fee, 'currency': 'KRW'},
            'status': 'closed',
            'filled': actual_amount
        }

    def get_paper_balance(self, currency: str = None) -> Dict[str, float]:
        """
        페이퍼 트레이딩 잔고 조회

        Args:
            currency: 특정 통화 (None이면 전체 반환)

        Returns:
            잔고 딕셔너리
        """
        if not self.paper_trading:
            return {}

        if currency:
            return {currency: self.paper_balances.get(currency, 0.0)}

        return self.paper_balances.copy()


# 싱글톤 인스턴스
_upbit_service = None


def get_upbit_service(paper_trading: bool = None) -> UpbitService:
    """업비트 서비스 싱글톤 인스턴스 반환"""
    global _upbit_service
    if _upbit_service is None:
        _upbit_service = UpbitService(paper_trading=paper_trading)
    return _upbit_service

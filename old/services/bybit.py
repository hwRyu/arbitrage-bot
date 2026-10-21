"""
바이비트 선물 거래소 서비스 래퍼 (USDT 무기한 선물)
"""
import ccxt
from typing import Optional, Dict, Any, List
from config.settings import APIKeys, BotConfig, BybitSettings
from utils.logger import get_logger
import time
logger = get_logger('bybit')


class BybitService:
    """
    바이비트 선물 거래소 래퍼 (USDT Perpetual)
    """

    def __init__(self, paper_trading: bool = None):
        """
        바이비트 서비스 초기화

        Args:
            paper_trading: 페이퍼 트레이딩 모드 (None이면 설정값 사용)
        """
        self.paper_trading = paper_trading if paper_trading is not None else BotConfig.PAPER_TRADING

        if self.paper_trading:
            logger.info("Bybit service initialized in PAPER TRADING mode")
            self.exchange = None
            # Paper trading state
            self.paper_balances = {
                'USDT': BotConfig.PAPER_INITIAL_USDT
            }
            self.paper_positions = {}  # {coin: position_info}
        else:
            logger.info("Bybit service initialized in LIVE TRADING mode")
            self.exchange = ccxt.bybit({
                'apiKey': APIKeys.BYBIT_API_KEY,
                'secret': APIKeys.BYBIT_API_SECRET,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'linear',  # USDT perpetual
                }
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
            symbol = f"{coin}/USDT:USDT"  # Perpetual futures format
            if self.paper_trading:
                exchange = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'linear'}})
                ticker = exchange.fetch_ticker(symbol)
            else:
                ticker = self.exchange.fetch_ticker(symbol)

            logger.debug(f"Bybit ticker for {symbol}: {ticker['last']:.4f} USDT")
            return ticker

        except Exception as e:
            logger.error(f"Failed to fetch Bybit ticker for {coin}: {e}")
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
            symbol = f"{coin}/USDT:USDT"
            if self.paper_trading:
                exchange = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'linear'}})
                orderbook = exchange.fetch_order_book(symbol, limit=limit)
            else:
                orderbook = self.exchange.fetch_order_book(symbol, limit=limit)

            return orderbook

        except Exception as e:
            logger.error(f"Failed to fetch Bybit orderbook for {coin}: {e}")
            return None

    def fetch_balance(self, currency: str = 'USDT') -> float:
        """
        잔고 조회

        Args:
            currency: 통화 심볼 (기본값: USDT)

        Returns:
            잔고 금액
        """
        try:
            if self.paper_trading:
                return self.paper_balances.get(currency, 0.0)

            balance = self.exchange.fetch_balance()
            return balance.get(currency, {}).get('free', 0.0)

        except Exception as e:
            logger.error(f"Failed to fetch Bybit balance for {currency}: {e}")
            return 0.0

    def set_leverage(self, coin: str, leverage: int) -> bool:
        """
        레버리지 설정

        Args:
            coin: 코인 심볼
            leverage: 레버리지 값

        Returns:
            성공 여부
        """
        try:
            symbol = f"{coin}/USDT:USDT"

            if self.paper_trading:
                logger.info(f"[PAPER] Set leverage for {coin}: {leverage}x")
                return True

            self.exchange.set_leverage(leverage, symbol)
            logger.info(f"Set leverage for {coin}: {leverage}x")
            return True

        except Exception as e:
            logger.error(f"Failed to set leverage for {coin}: {e}")
            return False

    def set_margin_mode(self, coin: str, is_isolated: bool = True) -> bool:
        """
        마진 모드 설정 (격리 또는 교차)

        Args:
            coin: 코인 심볼
            is_isolated: True면 격리 마진, False면 교차 마진

        Returns:
            성공 여부
        """
        try:
            symbol = f"{coin}/USDT:USDT"

            if self.paper_trading:
                mode = "ISOLATED" if is_isolated else "CROSS"
                logger.info(f"[PAPER] Set margin mode for {coin}: {mode}")
                return True

            # Bybit: setMarginMode
            mode = 'isolated' if is_isolated else 'cross'
            self.exchange.set_margin_mode(mode, symbol)
            logger.info(f"Set margin mode for {coin}: {mode}")
            return True

        except Exception as e:
            logger.error(f"Failed to set margin mode for {coin}: {e}")
            return False

    def set_position_mode(self, coin: str, hedged:bool):
        """포지션 모드를 One-Way로 설정"""
        try:
            symbol = f"{coin}/USDT:USDT"
            
            # One-Way Mode로 설정
            self.exchange.set_position_mode(
                hedged=hedged,  # False = One-Way Mode
                symbol=symbol
            )
            
            logger.info(f"Set position mode to One-Way for {coin}")
            
        except Exception as e:
            logger.warning(f"Position mode setting: {e}")

    def open_short_position(self, coin: str, amount: float) -> Optional[Dict[str, Any]]:
        """
        숏 포지션 오픈 (시장가 주문)

        Args:
            coin: 코인 심볼
            amount: 코인 수량

        Returns:
            주문 결과 또는 None
        """
        try:
            symbol = f"{coin}/USDT:USDT"

            if self.paper_trading:
                return self._paper_open_position(coin, 'short', amount)

            # Set leverage and margin mode first
            self.set_leverage(coin, BybitSettings.LEVERAGE)
            self.set_margin_mode(coin, is_isolated=True)

            # Create sell order (short)
            # One-Way Mode에서는 positionIdx를 0으로 설정하거나 제거
            order = self.exchange.create_order(
                symbol=symbol,
                type='market',
                side='sell',
                amount=amount,
                params={
                    'positionIdx': BybitSettings.POSITION_IDX_SHORT,  # One-Way Mode (hedged=False일 때)
                }
            )

            # 주문 ID가 있으면 체결 정보 조회
            if order and order.get('id'):
                try:
                    time.sleep(0.5)  # 체결 대기
                    filled_order = self.exchange.fetch_order(order['id'], symbol, params={"acknowledged": True})
                    
                    # 체결 정보 업데이트
                    order.update({
                        'price': filled_order.get('average') or filled_order.get('price'),
                        'filled': filled_order.get('filled'),
                        'cost': filled_order.get('cost'),
                        'fee': filled_order.get('fee')
                    })
                except Exception as fetch_error:
                    logger.warning(f"Failed to fetch order details for {coin}: {fetch_error}")

            price = order.get('price') or order.get('average') or 'N/A'
            logger.info(f"Bybit SHORT position opened: {amount} {coin} @ {price}")
            return order

        except Exception as e:
            logger.error(f"Failed to open Bybit short position for {coin}: {e}")
            return None



    # def open_short_position(self, coin: str, amount: float) -> Optional[Dict[str, Any]]:
    #     """
    #     숏 포지션 오픈 (시장가 주문)

    #     Args:
    #         coin: 코인 심볼
    #         amount: 코인 수량

    #     Returns:
    #         주문 결과 또는 None
    #     """
    #     try:
    #         symbol = f"{coin}/USDT:USDT"

    #         if self.paper_trading:
    #             return self._paper_open_position(coin, 'short', amount)

    #         # Set leverage and margin mode first
    #         self.set_leverage(coin, BybitSettings.LEVERAGE)
    #         self.set_margin_mode(coin, is_isolated=True)

    #         # Create sell order (short)
    #         order = self.exchange.create_order(
    #             symbol=symbol,
    #             type='market',
    #             side='sell',
    #             amount=amount,
    #             params={
    #                 'positionIdx': BybitSettings.POSITION_IDX_SHORT,  # Hedge mode short
    #             }
    #         )

    #         logger.info(f"Bybit SHORT position opened: {amount} {coin} @ {order.get('price', 'N/A')}")
    #         return order

    #     except Exception as e:
    #         logger.error(f"Failed to open Bybit short position for {coin}: {e}")
    #         return None

    def close_short_position(self, coin: str, amount: float) -> Optional[Dict[str, Any]]:
        """
        숏 포지션 청산 (시장가 주문)

        Args:
            coin: 코인 심볼
            amount: 청산할 수량

        Returns:
            주문 결과 또는 None
        """
        try:
            symbol = f"{coin}/USDT:USDT"

            if self.paper_trading:
                return self._paper_close_position(coin, 'short', amount)

            # Create buy order to close short
            order = self.exchange.create_order(
                symbol=symbol,
                type='market',
                side='buy',
                amount=amount,
                params={
                    'positionIdx': BybitSettings.POSITION_IDX_SHORT,
                    'reduceOnly': True  # Close position only
                }
            )

            logger.info(f"Bybit SHORT position closed: {amount} {coin} @ {order.get('price', 'N/A')}")
            return order

        except Exception as e:
            logger.error(f"Failed to close Bybit short position for {coin}: {e}")
            return None

    def fetch_position(self, coin: str) -> Optional[Dict[str, Any]]:
        """
        현재 포지션 조회

        Args:
            coin: 코인 심볼

        Returns:
            포지션 정보 또는 None
        """
        try:
            symbol = f"{coin}/USDT:USDT"

            if self.paper_trading:
                return self.paper_positions.get(coin)

            positions = self.exchange.fetch_positions([symbol])
            for pos in positions:
                # Find short position
                if pos['side'] == 'short' and float(pos['contracts']) > 0:
                    return pos

            return None

        except Exception as e:
            logger.error(f"Failed to fetch Bybit position for {coin}: {e}")
            return None

    def fetch_funding_rate(self, coin: str) -> Optional[float]:
        """
        펀딩비율 조회

        Args:
            coin: 코인 심볼

        Returns:
            펀딩비율 (%) 또는 None
        """
        try:
            symbol = f"{coin}/USDT:USDT"

            if self.paper_trading:
                # Use real funding rate even in paper mode
                exchange = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'linear'}})
                funding = exchange.fetch_funding_rate(symbol)
            else:
                funding = self.exchange.fetch_funding_rate(symbol)

            rate = funding.get('fundingRate', 0) * 100  # Convert to %
            logger.debug(f"Bybit funding rate for {coin}: {rate:.4f}%")
            return rate

        except Exception as e:
            logger.error(f"Failed to fetch Bybit funding rate for {coin}: {e}")
            return None

    def fetch_mmr(self, coin: str) -> Optional[float]:
        """
        Fetch Maintenance Margin Rate (MMR) from Bybit API

        **중요**: 실시간 MMR을 API에서 조회
        API 엔드포인트: /v5/market/instruments-info (category=linear)

        Args:
            coin: Coin symbol (e.g., 'BTC', 'SOL')

        Returns:
            MMR (Maintenance Margin Rate) as decimal (e.g., 0.01 = 1%)
            Returns None if API call fails
        """
        try:
            symbol = f"{coin}USDT"  # Bybit V5 API format (no slash)

            if self.paper_trading:
                # Use real API even in paper mode to get accurate MMR
                exchange = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'linear'}})
            else:
                exchange = self.exchange

            # Call Bybit V5 API
            response = exchange.public_get_v5_market_instruments_info({
                'category': 'linear',
                'symbol': symbol
            })

            if response and 'result' in response and 'list' in response['result']:
                instruments = response['result']['list']
                if instruments and len(instruments) > 0:
                    instrument = instruments[0]
                    # MMR is in 'maintMarginRate' field (as string)
                    mmr_str = instrument.get('maintMarginRate', None)
                    if mmr_str:
                        mmr = float(mmr_str)
                        logger.debug(f"Bybit MMR for {coin}: {mmr:.4f} ({mmr*100:.2f}%)")
                        return mmr

            logger.warning(f"MMR not found in API response for {coin}")
            return None

        except Exception as e:
            logger.error(f"Failed to fetch Bybit MMR for {coin}: {e}")
            return None

    def get_mmr_with_fallback(self, coin: str) -> float:
        """
        Get MMR with fallback to config

        먼저 API에서 조회 시도, 실패 시 config의 폴백값 사용

        Args:
            coin: Coin symbol

        Returns:
            MMR (소수점, 예: 0.01 = 1%)
        """
        # Try API first
        mmr = self.fetch_mmr(coin)
        if mmr is not None:
            return mmr

        # Fallback to config
        from config.settings import RiskParams
        fallback_mmr = RiskParams.get_mmr(coin)
        logger.warning(f"Using fallback MMR for {coin}: {fallback_mmr:.4f} ({fallback_mmr*100:.2f}%)")
        return fallback_mmr

    def _paper_open_position(self, coin: str, side: str, amount: float) -> Dict[str, Any]:
        """
        페이퍼 트레이딩: 포지션 오픈

        Args:
            coin: 코인 심볼
            side: 'long' 또는 'short'
            amount: 포지션 크기

        Returns:
            시뮬레이션된 주문 결과
        """
        ticker = self.fetch_ticker(coin)
        if not ticker:
            raise Exception("Failed to fetch ticker for paper position")

        price = ticker['last']
        position_value = amount * price

        # Check balance
        usdt_balance = self.paper_balances.get('USDT', 0)
        required_margin = position_value / BybitSettings.LEVERAGE

        if usdt_balance < required_margin:
            raise Exception(f"Insufficient USDT margin: {usdt_balance} < {required_margin}")

        # Lock margin
        self.paper_balances['USDT'] -= required_margin

        # Calculate liquidation price (simplified)
        # For short: liq_price = entry_price * (2 - MMR) when leverage=1
        from config.settings import RiskParams
        mmr = RiskParams.get_mmr(coin)
        if side == 'short':
            liq_price = price * (2 - mmr)
        else:
            liq_price = price * mmr

        # Store position
        fee_rate = 0.00075  # Taker fee
        fee = position_value * fee_rate

        self.paper_positions[coin] = {
            'symbol': f'{coin}/USDT:USDT',
            'side': side,
            'contracts': amount,
            'contractSize': 1,
            'entryPrice': price,
            'liquidationPrice': liq_price,
            'leverage': BybitSettings.LEVERAGE,
            'initialMargin': required_margin,
            'unrealizedPnl': 0,
            'percentage': 0
        }

        logger.info(f"[PAPER] Bybit {side.upper()} position opened: {amount} {coin} @ {price:.4f} USDT (liq: {liq_price:.4f})")

        return {
            'id': f'paper_{coin}_{side}_{hash(str(price))}',
            'symbol': f'{coin}/USDT:USDT',
            'type': 'market',
            'side': 'sell' if side == 'short' else 'buy',
            'price': price,
            'amount': amount,
            'cost': position_value,
            'fee': {'cost': fee, 'currency': 'USDT'},
            'status': 'closed',
            'filled': amount
        }

    def _paper_close_position(self, coin: str, side: str, amount: float) -> Dict[str, Any]:
        """
        페이퍼 트레이딩: 포지션 청산

        Args:
            coin: 코인 심볼
            side: 'long' 또는 'short'
            amount: 청산할 수량

        Returns:
            시뮬레이션된 주문 결과
        """
        position = self.paper_positions.get(coin)
        if not position:
            raise Exception(f"No {side} position found for {coin}")

        if position['contracts'] < amount:
            raise Exception(f"Insufficient position size: {position['contracts']} < {amount}")

        ticker = self.fetch_ticker(coin)
        if not ticker:
            raise Exception("Failed to fetch ticker for paper position close")

        exit_price = ticker['last']
        entry_price = position['entryPrice']

        # Calculate PnL
        if side == 'short':
            pnl = (entry_price - exit_price) * amount
        else:
            pnl = (exit_price - entry_price) * amount

        # Fee
        fee_rate = 0.00075
        fee = exit_price * amount * fee_rate

        # Release margin and add PnL
        released_margin = position['initialMargin'] * (amount / position['contracts'])
        self.paper_balances['USDT'] += released_margin + pnl - fee

        # Update or remove position
        if amount >= position['contracts']:
            del self.paper_positions[coin]
            logger.info(f"[PAPER] Bybit {side.upper()} position fully closed: {amount} {coin} @ {exit_price:.4f} USDT (PnL: {pnl:.2f} USDT)")
        else:
            position['contracts'] -= amount
            position['initialMargin'] -= released_margin
            logger.info(f"[PAPER] Bybit {side.upper()} position partially closed: {amount} {coin} @ {exit_price:.4f} USDT (PnL: {pnl:.2f} USDT)")

        return {
            'id': f'paper_close_{coin}_{side}_{hash(str(exit_price))}',
            'symbol': f'{coin}/USDT:USDT',
            'type': 'market',
            'side': 'buy' if side == 'short' else 'sell',
            'price': exit_price,
            'amount': amount,
            'cost': exit_price * amount,
            'fee': {'cost': fee, 'currency': 'USDT'},
            'status': 'closed',
            'filled': amount,
            'info': {'pnl': pnl}
        }

    def get_paper_balance(self, currency: str = None) -> Dict[str, float]:
        """페이퍼 트레이딩 잔고 조회"""
        if not self.paper_trading:
            return {}

        if currency:
            return {currency: self.paper_balances.get(currency, 0.0)}

        return self.paper_balances.copy()

    def get_paper_positions(self) -> Dict[str, Dict]:
        """페이퍼 트레이딩 포지션 조회"""
        if not self.paper_trading:
            return {}

        return self.paper_positions.copy()


# Singleton instance
_bybit_service = None


def get_bybit_service(paper_trading: bool = None) -> BybitService:
    """바이비트 서비스 싱글톤 인스턴스 반환"""
    global _bybit_service
    if _bybit_service is None:
        _bybit_service = BybitService(paper_trading=paper_trading)
    return _bybit_service

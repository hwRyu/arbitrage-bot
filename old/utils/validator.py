"""
Validation logic for entry conditions
"""
from typing import Optional, Dict, Any
from models.position import ValidationResult, OrderbookDepth, MarketPrice
from config.settings import RiskParams, BotConfig
from utils.calculator import (
    calculate_liquidation_price,
    calculate_liquidation_distance,
    is_liquidation_safe,
    calculate_position_size
)
from utils.logger import get_logger

logger = get_logger('validator')


def validate_entry(coin: str, market_price: MarketPrice,
                   upbit_orderbook: Dict[str, Any],
                   bybit_orderbook: Dict[str, Any]) -> ValidationResult:
    """
    진입 조건 검증

    Args:
        coin: 코인 심볼
        market_price: 시장 가격 정보
        upbit_orderbook: 업비트 호가창
        bybit_orderbook: 바이비트 호가창

    Returns:
        ValidationResult
    """
    result = ValidationResult(is_valid=True, coin=coin)

    # 1. 청산가 안전성 체크
    if not _validate_liquidation_safety(coin, market_price.usd_price, result):
        return result

    # 2. 호가창 깊이 체크
    if not _validate_orderbook_depth(
        coin, market_price, upbit_orderbook, bybit_orderbook, result
    ):
        return result

    # 3. 슬리피지 예상 체크
    if not _validate_expected_slippage(
        coin, market_price, upbit_orderbook, bybit_orderbook, result
    ):
        return result

    logger.info(f"Entry validation passed for {coin}")
    return result


def _validate_liquidation_safety(coin: str, entry_price: float,
                                 result: ValidationResult) -> bool:
    """
    청산가 안전성 검증

    Args:
        coin: 코인 심볼
        entry_price: 진입가 (USD)
        result: ValidationResult (수정됨)

    Returns:
        검증 통과 여부
    """
    leverage = 1  # 레버리지 1배 고정
    liq_price = calculate_liquidation_price(entry_price, leverage, 'short', coin)
    liq_distance = calculate_liquidation_distance(entry_price, liq_price)

    result.liquidation_price = liq_price
    result.liquidation_buffer = liq_distance

    if not is_liquidation_safe(entry_price, leverage, coin):
        result.add_reason(
            f"Liquidation price too close: {liq_price:.2f} "
            f"(distance: {liq_distance:.2f}x < {RiskParams.LIQUIDATION_BUFFER}x required)"
        )
        logger.warning(f"Liquidation safety check failed for {coin}: {liq_distance:.2f}x")
        return False

    logger.debug(f"Liquidation safety OK for {coin}: {liq_distance:.2f}x")
    return True


def _validate_orderbook_depth(coin: str, market_price: MarketPrice,
                              upbit_orderbook: Dict[str, Any],
                              bybit_orderbook: Dict[str, Any],
                              result: ValidationResult) -> bool:
    """
    호가창 깊이 검증

    Args:
        coin: 코인 심볼
        market_price: 시장 가격
        upbit_orderbook: 업비트 호가창
        bybit_orderbook: 바이비트 호가창
        result: ValidationResult (수정됨)

    Returns:
        검증 통과 여부
    """
    # 포지션 크기 계산
    position_size = calculate_position_size(
        BotConfig.MAX_POSITION_KRW,
        BotConfig.MAX_POSITION_USDT,
        market_price.krw_price,
        market_price.usd_price,
        market_price.usdt_krw
    )

    # 최소 호가 깊이 = 포지션 크기 * 배수
    min_depth = position_size * RiskParams.MIN_ORDERBOOK_DEPTH_MULTIPLIER

    # 업비트 asks 깊이 (매수 시 asks를 소진)
    upbit_asks = upbit_orderbook.get('asks', [])
    upbit_depth = sum([level[1] for level in upbit_asks[:10]])  # 상위 10개 레벨

    if upbit_depth < min_depth:
        result.orderbook_valid = False
        result.add_reason(
            f"Upbit orderbook too thin: {upbit_depth:.4f} < {min_depth:.4f} {coin}"
        )
        logger.warning(f"Upbit orderbook depth insufficient for {coin}: {upbit_depth:.4f}")
        return False

    # 바이비트 asks 깊이 (숏 진입 시 asks를 소진)
    bybit_asks = bybit_orderbook.get('asks', [])
    bybit_depth = sum([level[1] for level in bybit_asks[:10]])

    if bybit_depth < min_depth:
        result.orderbook_valid = False
        result.add_reason(
            f"Bybit orderbook too thin: {bybit_depth:.4f} < {min_depth:.4f} {coin}"
        )
        logger.warning(f"Bybit orderbook depth insufficient for {coin}: {bybit_depth:.4f}")
        return False

    logger.debug(f"Orderbook depth OK for {coin}: Upbit={upbit_depth:.4f}, Bybit={bybit_depth:.4f}")
    return True


def _validate_expected_slippage(coin: str, market_price: MarketPrice,
                                upbit_orderbook: Dict[str, Any],
                                bybit_orderbook: Dict[str, Any],
                                result: ValidationResult) -> bool:
    """
    예상 슬리피지 검증

    Args:
        coin: 코인 심볼
        market_price: 시장 가격
        upbit_orderbook: 업비트 호가창
        bybit_orderbook: 바이비트 호가창
        result: ValidationResult (수정됨)

    Returns:
        검증 통과 여부
    """
    # 포지션 크기 계산
    position_size = calculate_position_size(
        BotConfig.MAX_POSITION_KRW,
        BotConfig.MAX_POSITION_USDT,
        market_price.krw_price,
        market_price.usd_price,
        market_price.usdt_krw
    )

    # 예상 체결가 계산 (VWAP)
    upbit_expected_price = _calculate_vwap(upbit_orderbook.get('asks', []), position_size)
    bybit_expected_price = _calculate_vwap(bybit_orderbook.get('asks', []), position_size)

    if upbit_expected_price is None or bybit_expected_price is None:
        result.slippage_valid = False
        result.add_reason(f"Cannot calculate expected price for {coin}")
        return False

    # 슬리피지 계산
    upbit_slippage = abs((upbit_expected_price - market_price.krw_price) / market_price.krw_price) * 100
    bybit_slippage = abs((bybit_expected_price - market_price.usd_price) / market_price.usd_price) * 100

    # 최대 슬리피지 체크
    max_slippage = RiskParams.MAX_SLIPPAGE_PERCENT

    if upbit_slippage > max_slippage:
        result.slippage_valid = False
        result.add_reason(
            f"Upbit slippage too high: {upbit_slippage:.2f}% > {max_slippage:.2f}%"
        )
        logger.warning(f"Upbit slippage too high for {coin}: {upbit_slippage:.2f}%")
        return False

    if bybit_slippage > max_slippage:
        result.slippage_valid = False
        result.add_reason(
            f"Bybit slippage too high: {bybit_slippage:.2f}% > {max_slippage:.2f}%"
        )
        logger.warning(f"Bybit slippage too high for {coin}: {bybit_slippage:.2f}%")
        return False

    logger.debug(f"Slippage OK for {coin}: Upbit={upbit_slippage:.2f}%, Bybit={bybit_slippage:.2f}%")
    return True


def _calculate_vwap(orderbook_levels: list, target_amount: float) -> Optional[float]:
    """
    VWAP (Volume Weighted Average Price) 계산

    Args:
        orderbook_levels: 호가 레벨 리스트 [[price, amount], ...]
        target_amount: 목표 거래량

    Returns:
        VWAP 가격 또는 None (불가능한 경우)
    """
    if not orderbook_levels:
        return None

    total_value = 0.0
    total_amount = 0.0

    for price, amount in orderbook_levels:
        if total_amount >= target_amount:
            break

        # 이 레벨에서 소진할 수량
        take_amount = min(amount, target_amount - total_amount)

        total_value += price * take_amount
        total_amount += take_amount

    if total_amount == 0:
        return None

    # 목표 수량을 채우지 못한 경우
    if total_amount < target_amount:
        logger.warning(f"Cannot fill target amount: {total_amount} < {target_amount}")
        # 그래도 계산 가능한 VWAP 반환
        return total_value / total_amount

    vwap = total_value / total_amount
    return vwap


def validate_balance(upbit_balance_krw: float, bybit_balance_usdt: float,
                    required_krw: float, required_usdt: float) -> tuple[bool, str]:
    """
    잔고 검증

    Args:
        upbit_balance_krw: 업비트 KRW 잔고
        bybit_balance_usdt: 바이비트 USDT 잔고
        required_krw: 필요한 KRW
        required_usdt: 필요한 USDT

    Returns:
        (검증 통과 여부, 실패 이유)
    """
    if upbit_balance_krw < required_krw:
        return False, f"Insufficient KRW balance: {upbit_balance_krw:.0f} < {required_krw:.0f}"

    if bybit_balance_usdt < required_usdt:
        return False, f"Insufficient USDT balance: {bybit_balance_usdt:.2f} < {required_usdt:.2f}"

    return True, ""


def check_liquidation_risk(current_price: float, liquidation_price: float) -> tuple[bool, str]:
    """
    청산 위험도 체크

    Args:
        current_price: 현재 가격
        liquidation_price: 청산가

    Returns:
        (위험 여부, 위험 수준)
    """
    distance = calculate_liquidation_distance(current_price, liquidation_price)

    if distance <= RiskParams.LIQUIDATION_REDUCE_DISTANCE:
        return True, "CRITICAL"  # 즉시 포지션 축소 필요
    elif distance <= RiskParams.LIQUIDATION_WARNING_DISTANCE:
        return True, "WARNING"  # 경고
    else:
        return False, "SAFE"

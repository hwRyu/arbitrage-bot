"""
Calculation utilities for premium, liquidation price, profit, etc.
"""
from typing import Tuple
from config.settings import Fees, RiskParams


def calculate_premium(krw_price: float, usd_price: float, exchange_rate: float) -> float:
    """
    김프 계산 (기본 공식)

    김프 = (업비트 원화 가격 / (바이비트 USD 가격 * USD/KRW 환율)) - 1

    **중요**: USD/KRW 환율은 한국수출입은행 API에서 조회한 공식 환율 사용
    USDT/KRW 가격이 아님! (사용자가 수동으로 USDT 관리)

    Args:
        krw_price: 업비트 가격 (KRW)
        usd_price: 바이비트 가격 (USD)
        exchange_rate: USD/KRW 환율 (한국수출입은행)

    Returns:
        김프 (%)
    """
    if usd_price == 0 or exchange_rate == 0:
        return 0.0

    theoretical_krw = usd_price * exchange_rate
    premium = ((krw_price / theoretical_krw) - 1) * 100

    return premium


def calculate_liquidation_price(entry_price: float, leverage: int,
                                side: str, coin: str) -> float:
    """
    청산가 계산 (Isolated 모드 기준)

    Bybit Isolated Margin 청산가:
    - Short: liq_price = entry_price * (2 - MMR) when leverage=1
    - Long: liq_price = entry_price * MMR when leverage=1

    For leverage > 1:
    - Short: liq_price = entry_price * (1 + (1 - MMR) / leverage)
    - Long: liq_price = entry_price * (1 - (1 - MMR) / leverage)

    Args:
        entry_price: 진입가
        leverage: 레버리지
        side: 'long' or 'short'
        coin: 코인 심볼 (MMR 조회용)

    Returns:
        청산가
    """
    mmr = RiskParams.get_mmr(coin)

    if side == 'short':
        if leverage == 1:
            liq_price = entry_price * (2 - mmr)
        else:
            liq_price = entry_price * (1 + (1 - mmr) / leverage)
    else:  # long
        if leverage == 1:
            liq_price = entry_price * mmr
        else:
            liq_price = entry_price * (1 - (1 - mmr) / leverage)

    return liq_price


def calculate_liquidation_distance(current_price: float, liquidation_price: float) -> float:
    """
    청산가까지 거리 (배수)

    Args:
        current_price: 현재 가격
        liquidation_price: 청산가

    Returns:
        청산가까지 거리 (배수, e.g., 2.0 = 청산가의 2배)
    """
    if liquidation_price == 0:
        return float('inf')

    return liquidation_price / current_price


def is_liquidation_safe(entry_price: float, leverage: int, coin: str) -> bool:
    """
    청산가 안전 체크

    Args:
        entry_price: 진입가
        leverage: 레버리지
        coin: 코인 심볼

    Returns:
        안전 여부 (청산가 >= 진입가 * LIQUIDATION_BUFFER)
    """
    liq_price = calculate_liquidation_price(entry_price, leverage, 'short', coin)
    required_liq = entry_price * RiskParams.LIQUIDATION_BUFFER

    return liq_price >= required_liq


def calculate_net_profit_krw(entry_value_krw: float, exit_value_krw: float,
                              funding_fee_krw: float = 0.0) -> Tuple[float, float]:
    """
    순수익 계산 (원화 기준)

    Args:
        entry_value_krw: 진입 금액 (KRW)
        exit_value_krw: 청산 금액 (KRW)
        funding_fee_krw: 펀딩비 수익 (KRW)

    Returns:
        (순수익 KRW, 수익률 %)
    """
    # 수수료 계산
    entry_fee = entry_value_krw * (Fees.UPBIT_TRADE_FEE + Fees.BYBIT_TAKER_FEE)
    exit_fee = exit_value_krw * (Fees.UPBIT_TRADE_FEE + Fees.BYBIT_TAKER_FEE)

    # 김프 수익 (현물 매도 - 현물 매수)
    kimp_profit = exit_value_krw - entry_value_krw

    # 총 순수익 = 김프 수익 + 펀딩비 - 수수료
    net_profit = kimp_profit + funding_fee_krw - entry_fee - exit_fee

    # 수익률
    profit_percent = (net_profit / entry_value_krw) * 100 if entry_value_krw > 0 else 0

    return net_profit, profit_percent


def calculate_required_exit_premium(entry_premium: float,
                                   target_profit_percent: float) -> float:
    """
    목표 수익률을 달성하기 위한 필요 청산 김프 계산 (역전 전략)

    필요 청산 김프 = 진입 김프 + 목표 수익 + 왕복 수수료 + 슬리피지

    **전략**: 낮은 김프에서 진입 → 높은 김프에서 청산

    Args:
        entry_premium: 진입 시 김프 (%)
        target_profit_percent: 목표 수익률 (%)

    Returns:
        목표 수익을 위한 필요 청산 김프 (%)

    Example:
        진입 김프: 0.0%
        목표 수익: 1.0%
        왕복 수수료: 0.25%
        슬리피지: 0.1%
        → 필요 청산 김프 = 0.0% + 1.0% + 0.25% + 0.1% = 1.35%
    """
    total_fee_percent = Fees.total_round_trip_fee_percent()
    slippage_buffer = Fees.SLIPPAGE_BUFFER * 100

    required_exit_premium = entry_premium + target_profit_percent + total_fee_percent + slippage_buffer

    return required_exit_premium


def calculate_position_size(max_position_krw: float, max_position_usdt: float,
                           krw_price: float, usd_price: float,
                           usdt_krw: float = None) -> float:
    """
    포지션 크기 계산 (동일한 수량으로 헷지)

    **레버리지 1배 고정**이므로 포지션 크기 = 증거금 크기

    Args:
        max_position_krw: 최대 투자금액 (KRW)
        max_position_usdt: 최대 투자금액 (USDT)
        krw_price: 업비트 가격 (KRW)
        usd_price: 바이비트 가격 (USD)
        usdt_krw: USDT/KRW 가격 (사용 안 함, 호환성 유지용)

    Returns:
        코인 수량
    """
    # KRW로 살 수 있는 수량
    amount_from_krw = max_position_krw / krw_price

    # USDT로 숏 잡을 수 있는 수량 (레버리지 1배이므로 직접 계산)
    amount_from_usdt = max_position_usdt / usd_price

    # 둘 중 작은 값 (헷지 가능한 최대 수량)
    max_amount = min(amount_from_krw, amount_from_usdt)

    return max_amount


def calculate_slippage(expected_price: float, actual_price: float) -> float:
    """
    슬리피지 계산

    Args:
        expected_price: 예상 가격
        actual_price: 실제 체결 가격

    Returns:
        슬리피지 (%)
    """
    if expected_price == 0:
        return 0.0

    slippage = ((actual_price - expected_price) / expected_price) * 100

    return abs(slippage)


def calculate_funding_fee(position_value_usdt: float, funding_rate: float,
                         exchange_rate: float) -> Tuple[float, float]:
    """
    펀딩비 계산

    Args:
        position_value_usdt: 포지션 가치 (USDT)
        funding_rate: 펀딩비율 (%)
        exchange_rate: USD/KRW 환율 (한국수출입은행)

    Returns:
        (펀딩비 USDT, 펀딩비 KRW)
    """
    funding_fee_usdt = position_value_usdt * (funding_rate / 100)
    funding_fee_krw = funding_fee_usdt * exchange_rate

    return funding_fee_usdt, funding_fee_krw


def calculate_target_premium(target_profit_percent: float,
                            current_premium: float,
                            entry_premium: float) -> float:
    """
    목표 청산 김프 계산 (역전 전략)

    목표 청산 김프 = 진입 김프 + 목표 수익 + 청산 수수료 + 슬리피지 버퍼

    **전략 개념**:
    - 김프가 낮을 때 진입 (예: -0.5%, 0%)
    - 김프가 높아질 때까지 대기
    - 김프가 목표값에 도달하면 청산

    Args:
        target_profit_percent: 목표 수익률 (%)
        current_premium: 현재 김프 (%) - 사용되지 않음 (호환성 유지)
        entry_premium: 진입 시 김프 (%)

    Returns:
        목표 청산 김프 (%)

    Example:
        진입 김프: -0.5%
        목표 수익: 1.0%
        청산 수수료: 0.125%
        슬리피지 버퍼: 0.1%
        → 목표 청산 김프 = -0.5% + 1.0% + 0.125% + 0.1% = 0.725%
    """
    exit_fee_percent = Fees.total_exit_fee_percent()  # 청산 시 수수료
    slippage_buffer = Fees.SLIPPAGE_BUFFER * 100  # 슬리피지 버퍼 (%)

    target_premium = entry_premium + target_profit_percent + exit_fee_percent + slippage_buffer

    return target_premium


def format_currency(amount: float, currency: str = 'KRW') -> str:
    """
    통화 포맷팅

    Args:
        amount: 금액
        currency: 통화 (KRW, USDT, USD)

    Returns:
        포맷팅된 문자열
    """
    if currency == 'KRW':
        return f"{amount:,.0f} KRW"
    elif currency in ['USDT', 'USD']:
        return f"{amount:,.2f} {currency}"
    else:
        return f"{amount:,.4f} {currency}"


def format_premium(premium: float) -> str:
    """
    김프 포맷팅

    Args:
        premium: 김프 (%)

    Returns:
        포맷팅된 문자열 (+/- 포함)
    """
    sign = '+' if premium >= 0 else ''
    return f"{sign}{premium:.2f}%"


def format_percentage(value: float) -> str:
    """
    퍼센트 포맷팅

    Args:
        value: 값 (%)

    Returns:
        포맷팅된 문자열 (+/- 포함)
    """
    sign = '+' if value >= 0 else ''
    return f"{sign}{value:.2f}%"

"""
로깅 설정 및 유틸리티

로거 설정, 거래 로그, 상태 전이 로그 등을 관리합니다.
"""
import os
import logging
import sys
from logging.handlers import RotatingFileHandler
from datetime import datetime
from config.settings import LogConfig


def setup_logger(name: str = 'kimp_bot') -> logging.Logger:
    """
    파일 및 콘솔 핸들러를 가진 로거 설정

    로그를 콘솔과 파일에 동시에 출력하도록 설정합니다.
    파일 로그는 회전(rotation) 방식으로 관리됩니다.

    Args:
        name: 로거 이름

    Returns:
        설정된 로거 인스턴스
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, LogConfig.LOG_LEVEL))

    # 중복 핸들러 방지
    if logger.handlers:
        return logger

    # 로그 포맷 설정
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 콘솔 핸들러 (표준 출력)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, LogConfig.LOG_LEVEL))
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 파일 핸들러 (회전 로그)
    if LogConfig.LOG_TO_FILE:
        # 로그 디렉토리 생성 (없으면)
        os.makedirs(LogConfig.LOG_DIR, exist_ok=True)

        log_file_path = os.path.join(LogConfig.LOG_DIR, LogConfig.LOG_FILE)
        file_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=LogConfig.MAX_LOG_SIZE_MB * 1024 * 1024,
            backupCount=LogConfig.BACKUP_COUNT,
            encoding='utf-8'
        )
        file_handler.setLevel(getattr(logging, LogConfig.LOG_LEVEL))
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def log_trade(logger: logging.Logger, exchange: str, side: str, coin: str,
              amount: float, price: float, success: bool, order_id: str = None):
    """
    거래 실행 로그 기록

    Args:
        logger: 로거 인스턴스
        exchange: 거래소 이름 (upbit, bybit)
        side: 거래 방향 (buy, sell, open_short, close_short)
        coin: 코인 심볼
        amount: 거래 수량
        price: 거래 가격
        success: 거래 성공 여부
        order_id: 주문 ID (선택사항)
    """
    status = "성공" if success else "실패"
    msg = f"[거래 {status}] {exchange} {side.upper()} {amount} {coin} @ {price}"
    if order_id:
        msg += f" (주문 ID: {order_id})"

    if success:
        logger.info(msg)
    else:
        logger.error(msg)


def log_premium(logger: logging.Logger, coin: str, premium: float,
                krw_price: float, usd_price: float, exchange_rate:float):
    """
    김프 정보 로그 기록

    Args:
        logger: 로거 인스턴스
        coin: 코인 심볼
        premium: 김프 (%)
        krw_price: 업비트 가격 (KRW)
        usd_price: 바이비트 가격 (USD)
        exchange_rate: USD/KRW 환율
    """
    logger.info(f"[김프] {coin}: {premium:+.3f}% (KRW: {krw_price:,.0f}, USD: {usd_price:.3f}, 환율: {exchange_rate:.3f})")


def log_state_transition(logger: logging.Logger, strategy_id: int, coin: str,
                         old_state: str, new_state: str):
    """
    상태 머신 전이 로그 기록

    Args:
        logger: 로거 인스턴스
        strategy_id: 전략 ID
        coin: 코인 심볼
        old_state: 이전 상태
        new_state: 새로운 상태
    """
    logger.info(f"[상태] 전략 #{strategy_id} ({coin}): {old_state} → {new_state}")


def log_pnl(logger: logging.Logger, strategy_id: int, coin: str,
            pnl_krw: float, pnl_percent: float, exit_reason: str):
    """
    손익 로그 기록

    Args:
        logger: 로거 인스턴스
        strategy_id: 전략 ID
        coin: 코인 심볼
        pnl_krw: 손익 (KRW)
        pnl_percent: 손익률 (%)
        exit_reason: 청산 이유
    """
    sign = "+" if pnl_krw >= 0 else ""
    logger.info(
        f"[손익] 전략 #{strategy_id} ({coin}): {sign}{pnl_krw:,.0f} KRW "
        f"({sign}{pnl_percent:.2f}%) - {exit_reason}"
    )


def log_liquidation_warning(logger: logging.Logger, strategy_id: int, coin: str,
                           current_price: float, liquidation_price: float, distance: float):
    """
    청산 위험 경고 로그 기록

    Args:
        logger: 로거 인스턴스
        strategy_id: 전략 ID
        coin: 코인 심볼
        current_price: 현재 가격
        liquidation_price: 청산가
        distance: 청산가까지 거리 (배수)
    """
    logger.warning(
        f"[청산 위험] 전략 #{strategy_id} ({coin}): "
        f"현재가 {current_price:.2f} 청산가 {liquidation_price:.2f} 접근 중 "
        f"(거리: {distance:.2f}배)"
    )


def log_error(logger: logging.Logger, message: str, exception: Exception = None):
    """
    에러 로그 기록 (예외 정보 포함 가능)

    Args:
        logger: 로거 인스턴스
        message: 에러 메시지
        exception: 예외 객체 (선택사항)
    """
    if exception:
        logger.error(f"{message}: {str(exception)}", exc_info=True)
    else:
        logger.error(message)


# 기본 로거 생성
logger = setup_logger()


def get_logger(name: str = None) -> logging.Logger:
    """
    로거 인스턴스 조회

    Args:
        name: 로거 이름 (선택사항)

    Returns:
        로거 인스턴스
    """
    if name:
        return logging.getLogger(f'kimp_bot.{name}')
    return logger

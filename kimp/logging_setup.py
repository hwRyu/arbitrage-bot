"""
로깅 설정

콘솔 + 회전 파일(logs/bot.log, 10MB × 5개) 동시 출력.
로그 형식: 시각 [레벨] 모듈 - 메시지
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """
    루트 로거 설정 (프로그램 시작 시 1회 호출)

    Args:
        level: 로그 레벨 (DEBUG / INFO / WARNING / ERROR)
        log_dir: 로그 파일 디렉터리 (없으면 생성)
    """
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s", "%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()  # 중복 호출 시 핸들러가 쌓이지 않도록

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "bot.log"), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 라이브러리 내부 로그는 너무 많아서 경고 이상만 남긴다
    for noisy in ("ccxt", "urllib3", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

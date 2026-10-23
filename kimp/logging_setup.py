"""
로깅 설정

콘솔 + 날짜별 파일(logs/bot.log, 자정마다 bot.log.YYYY-MM-DD 로 넘기고 90일 보관) 동시 출력.
로그 형식: 시각(밀리초) [레벨] 모듈 - 메시지

크기 회전(10MB × 5)에서 날짜 회전으로 바꾼 이유: 나중에 "그날 무슨 일이 있었나" 를 되짚으려면
날짜로 잘려 있어야 하고, 크기 회전은 오래된 날을 소리 없이 지웠다. 밀리초는 주문 세 개 사이의
간격(1~3초)과 스캔 소요를 재기 위해서다.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """
    루트 로거 설정 (프로그램 시작 시 1회 호출)

    Args:
        level: 로그 레벨 (DEBUG / INFO / WARNING / ERROR)
        log_dir: 로그 파일 디렉터리 (없으면 생성)
    """
    formatter = logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s - %(message)s",
                                  "%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()  # 중복 호출 시 핸들러가 쌓이지 않도록

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    os.makedirs(log_dir, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        os.path.join(log_dir, "bot.log"), when="midnight", backupCount=90, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 라이브러리 내부 로그는 너무 많아서 경고 이상만 남긴다
    for noisy in ("ccxt", "urllib3", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

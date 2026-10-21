"""
봇 실행 진입점

    python main.py

하는 일: 설정 읽기 → 거래소/환율/DB 준비 → 엔진 실행
전략 기준: docs/concept.md

끄기: Ctrl+C. 하던 스캔을 마치고 안전하게 끝난다.
열린 포지션은 DB 에 남아서 다음에 켜면 이어서 관리한다.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading

from kimp.config import load_settings
from kimp.db import make_session_factory
from kimp.engine import Engine
from kimp.fx import FxProvider
from kimp.logging_setup import setup_logging
from kimp.market_data import MarketData
from kimp.paper_broker import PaperBroker

logger = logging.getLogger("main")


def main() -> int:
    """설정 읽기 → 준비 → 엔진 실행. 돌려주는 값은 종료 코드 (0 = 정상)"""
    try:
        settings = load_settings()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.log_dir)

    # 지금 버전은 페이퍼 트레이딩(가짜 주문)만 된다. 실주문 코드가 아직 없어서 실거래 설정은 막는다.
    if not settings.paper_trading:
        logger.error("PAPER_TRADING=false 는 아직 지원하지 않습니다 (실주문 기능 미구현)")
        return 2

    f = settings.fees
    share = settings.upbit_target_share()
    size_text = (f"{settings.position_size_usdt:,.0f} USDT 고정" if settings.position_size_usdt
                 else f"자동 (업비트 자본 × {settings.capital_usage_percent:.0f}% ÷ {settings.max_open_positions})")
    logger.info("=" * 70)
    logger.info("김프 헷지 봇 시작 (페이퍼 트레이딩) — 업비트·바이비트 모두 USDT 자본, "
                "김프는 업비트 USDT 가격 기준(코인 몫), 손익은 USDT 기준")
    logger.info("공통값: 진입 김프 ≤ %+.2f%% | 목표 순이익 %.2f%% | 손절 %.2f%% | 포지션 크기 %s | 최대 %d개",
                settings.entry_premium_threshold_percent, settings.target_net_profit_percent,
                settings.stop_loss_percent, size_text, settings.max_open_positions)
    logger.info("바이비트 레버리지 %.0f배 → 목표 자본 비율 업비트 %.1f : 바이비트 %.1f (여유분 %.0f%%) | "
                "강제청산 경고 %.0f%% 이내 | 리밸런싱 알림 %.0f%%p 이탈",
                settings.bybit_leverage, share * 100, (1 - share) * 100, settings.bybit_margin_buffer_percent,
                settings.liquidation_warn_percent, settings.rebalance_threshold_percent)
    logger.info("비용: 업비트 %.3f%% (코인·USDT 매매 각각) · 바이비트 %.3f%% (왕복 %.2f%%) · 전송 %.2f USDT/사이클",
                f.upbit_trade_fee_rate * 100, f.bybit_taker_fee_rate * 100,
                f.round_trip_trade_fee_percent(), f.transfer_fee_usdt_per_cycle)
    if settings.fixed_usd_krw:
        logger.info("달러 환율(표시용): %.2f원으로 고정", settings.fixed_usd_krw)
    else:
        logger.info("달러 환율(표시용): yfinance 에서 %.0f초마다. 못 받아도 매매에는 영향 없음", settings.fx_refresh_sec)
    for warning in settings.config_warnings:
        logger.warning(warning)

    market = MarketData(depth=settings.orderbook_depth)
    coins = market.filter_supported(settings.coins)
    if not coins:
        logger.error("운용 가능한 코인이 없습니다 (COINS 확인)")
        return 2

    # 코인별로 실제로 쓰는 값 보여주기. 코인별로 따로 정한 값에는 * 표시
    logger.info("코인별 적용값 (* = 코인별 설정):")
    for coin in coins:
        p = settings.params_for(coin)

        def mark(name: str) -> str:
            return "*" if name in p.overridden else " "

        size = f"{p.position_size_usdt:7.0f} USDT" if p.position_size_usdt else "   자동     "
        logger.info("  %-6s 진입 ≤ %+6.2f%%%s | 목표 %5.2f%%%s | 손절 %6.2f%%%s | 포지션 %s%s", coin,
                    p.entry_premium_threshold_percent, mark("entry_premium_threshold_percent"),
                    p.target_net_profit_percent, mark("target_net_profit_percent"),
                    p.stop_loss_percent, mark("stop_loss_percent"), size, mark("position_size_usdt"))
    logger.info("=" * 70)

    # 달러 환율은 표시용. 못 받아도 매매는 한다
    fx = FxProvider(settings.fx_refresh_sec, fixed_rate=settings.fixed_usd_krw)
    fx.get()

    engine = Engine(
        settings=settings,
        market=market,
        fx=fx,
        broker=PaperBroker(settings.fees, settings.max_slippage_percent,
                           leverage=settings.bybit_leverage, mmr=settings.bybit_mmr),
        session_factory=make_session_factory(settings.db_path),
        coins=coins,
    )

    # Ctrl+C 를 누르면 '끝내라' 표시만 해두고, 엔진이 하던 스캔을 마친 뒤 멈춘다
    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        """종료 신호를 받으면 '끝내라' 표시만 한다 (실제로 멈추는 건 엔진 루프)"""
        logger.info("종료 신호(%s) 수신, 현재 스캔을 마치고 종료합니다", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    engine.run(stop_event)
    return 0


if __name__ == "__main__":
    sys.exit(main())

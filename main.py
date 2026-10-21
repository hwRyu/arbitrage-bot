"""
봇 실행 진입점

    python3 main.py

하는 일: 설정 읽기 → 거래소/환율/DB 준비 → 엔진 실행
  - 김프 헷지 엔진 (메인 스레드). 전략 기준: docs/concept.md
  - 펀딩비 수익 엔진 (따로 스레드, CARRY_ENABLED=true 일 때, 항상 페이퍼). 전략 기준: docs/carry.md

모드 (docs/live.md)
  - 페이퍼   PAPER_TRADING=true (기본)         가짜 잔고, 가짜 주문, DB data/kimp.db
  - 드라이런 PAPER_TRADING=false, LIVE_DRY_RUN=true (기본)   실제 잔고로 시작, 주문은 안 냄, data/dryrun.db
  - 실거래   PAPER_TRADING=false, LIVE_DRY_RUN=false, LIVE_CONFIRM=YES   실제 주문, data/live.db
  세 모드 모두 엔진·브로커(진입 순서, 되돌리기, 손익, 송금, 대조, 안전장치)는 같은 코드다. 실행기만 다르다.

끄기: Ctrl+C. 하던 스캔을 마치고 안전하게 끝난다.
열린 포지션은 DB 에 남아서 다음에 켜면 이어서 관리한다.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
from datetime import datetime

from kimp.broker import HedgeBroker, PaperBroker, broker_kwargs
from kimp.carry import CarryBroker, CarryEngine, LiveCarryMarket
from kimp.config import Settings, load_settings
from kimp.db import make_session_factory
from kimp.engine import Engine
from kimp.executor import BYBIT_USDT, UPBIT_USDT
from kimp.fx import FxProvider
from kimp.logging_setup import setup_logging
from kimp.market_data import MarketData
from kimp.safety import KST, Safety

logger = logging.getLogger("main")

MODE_LABEL = {"paper": "페이퍼", "dry_run": "드라이런", "live": "실거래"}


def make_exchanges(settings: Settings):
    """키가 들어간 ccxt 업비트·바이비트 (주문·잔고·출금용)"""
    import ccxt

    upbit = ccxt.upbit({"apiKey": settings.upbit_access_key, "secret": settings.upbit_secret_key,
                        "enableRateLimit": True})
    bybit = ccxt.bybit({"apiKey": settings.bybit_api_key, "secret": settings.bybit_api_secret,
                        "enableRateLimit": True, "options": {"defaultType": "swap"}})
    upbit.load_markets()
    bybit.load_markets()
    return upbit, bybit


def make_broker(settings: Settings, coins, safety: Safety) -> HedgeBroker:
    """모드에 맞는 실행기를 끼운 브로커 (로직은 모두 같은 HedgeBroker)"""
    kw = broker_kwargs(settings, safety)
    common = dict(leverage=settings.bybit_leverage, mmr=settings.bybit_mmr)
    if settings.mode == "paper":
        return PaperBroker(settings.fees, settings.max_slippage_percent,
                           upbit_min_order_krw=settings.upbit_min_order_krw, **common, **kw)

    from kimp.live_executor import DryRunExecutor, LiveExecutor

    upbit, bybit = make_exchanges(settings)
    live = LiveExecutor(upbit, bybit, settings)
    if settings.mode == "dry_run":
        ex = DryRunExecutor(settings.fees, settings.bybit_leverage, live)  # 최소 주문은 실제 거래소 값
    else:
        live.setup(coins, settings.bybit_leverage)  # 바이비트 단방향·격리·레버리지 맞추기
        ex = live
    return HedgeBroker(ex, settings.fees, settings.max_slippage_percent, **common,
                       auto_transfer=settings.auto_transfer_enabled, **kw)


def status_text(settings: Settings, broker: HedgeBroker, session_factory) -> str:
    """/status 답장과 하루 요약에 쓰는 현황"""
    with session_factory() as session:
        broker.ex.refresh()
        ok, why = broker.entries_allowed(session)
        lines = [f"[{MODE_LABEL[settings.mode]}] {datetime.now(KST):%m-%d %H:%M}",
                 f"잔고: 업비트 USDT {broker.balance(session, UPBIT_USDT):,.2f} / 바이비트 USDT "
                 f"{broker.balance(session, BYBIT_USDT):,.2f} (남은 것, 포지션 제외)",
                 f"오늘 확정 손익: {broker.safety.daily_loss_usdt(session):+,.2f} USDT",
                 "신규 진입: " + ("가능" if ok else f"중단 — {why}")]
        positions = broker.open_positions(session)
        lines.append(f"포지션 {len(positions)}/{settings.max_open_positions}")
        for p in positions:
            lines.append(f"  #{p.id} {p.coin} 진입 김프 {p.entry_premium:+.2f}% → 지금 순손익 {p.last_net_percent or 0:+.2f}%")
        for p in broker.unfinished_positions(session):
            lines.append(f"  ⚠️ #{p.id} {p.coin} {p.status}")
        for t in broker.pending_transfers(session):
            lines.append(f"  송금 중 #{t.id} {t.direction} {t.amount_usdt:,.2f} USDT")
    return "\n".join(lines)


def start_telegram(settings: Settings, broker: HedgeBroker, session_factory, stop_event: threading.Event):
    """텔레그램 알림(로그 → 텔레그램) + 명령(/status /stop /resume) + 매일 9시 요약"""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        if settings.mode != "paper":
            logger.warning("텔레그램 설정이 없어서 알림을 못 보냅니다 (실거래에서는 꼭 설정하세요)")
        return
    from kimp.notify import TelegramCommands, TelegramLogHandler, TelegramNotifier

    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id,
                                prefix=f"[{MODE_LABEL[settings.mode]}] ")
    logging.getLogger().addHandler(TelegramLogHandler(notifier))

    def stop() -> str:
        broker.safety.manual_stop = True
        return "신규 진입을 멈췄습니다 (가진 포지션은 계속 관리). 재개: /resume"

    def resume() -> str:
        broker.safety.resume()
        return "재개했습니다 (수동 정지·연속 실패 횟수 풀림). 다른 이유로 막혀 있으면 /status 에 나옵니다"

    TelegramCommands(notifier, {"/status": lambda: status_text(settings, broker, session_factory),
                                "/stop": stop, "/resume": resume,
                                "/help": lambda: "/status 현황, /stop 신규 진입 중단, /resume 재개"}).start(stop_event)

    def daily() -> None:
        sent_for = None
        while not stop_event.wait(30):
            now = datetime.now(KST)
            if now.hour == 9 and sent_for != now.date():
                sent_for = now.date()
                try:
                    logger.info("[하루 요약]\n%s", status_text(settings, broker, session_factory))
                except Exception:
                    logger.exception("하루 요약 실패")

    threading.Thread(target=daily, name="daily-summary", daemon=True).start()
    notifier.send(f"봇 시작 ({MODE_LABEL[settings.mode]}, 코인 {', '.join(settings.coins)})")


def main() -> int:
    """설정 읽기 → 준비 → 엔진 실행. 돌려주는 값은 종료 코드 (0 = 정상)"""
    try:
        settings = load_settings()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.log_dir)

    f = settings.fees
    share = settings.upbit_target_share()
    size_text = (f"{settings.position_size_usdt:,.0f} USDT 고정" if settings.position_size_usdt
                 else f"자동 (업비트 자본 × {settings.capital_usage_percent:.0f}% ÷ {settings.max_open_positions})")
    if settings.max_position_usdt:
        size_text += f", 최대 {settings.max_position_usdt:,.0f} USDT"
    logger.info("=" * 70)
    logger.info("김프 헷지 봇 시작 [%s] — 업비트·바이비트 모두 USDT 자본, 김프는 업비트 USDT 가격 기준(코인 몫), "
                "손익은 USDT 기준 (DB %s)", MODE_LABEL[settings.mode], settings.active_db_path)
    if settings.mode == "live":
        logger.warning("실거래 모드: 실제 주문이 나갑니다. 포지션 최대 %s USDT, 하루 손실 한도 %s USDT, 자동 송금 %s",
                       f"{settings.max_position_usdt:,.0f}", f"{settings.daily_loss_limit_usdt:,.0f}",
                       "켜짐" if settings.auto_transfer_enabled else "꺼짐(알림만)")
    elif settings.mode == "dry_run":
        logger.info("드라이런 모드: 실제 잔고·호가로 돌지만 주문은 내지 않습니다 ('[드라이런] 실제였다면 …' 로그 확인)")
    logger.info("공통값: 진입 김프 ≤ %+.2f%% | 목표 순이익 %.2f%% | 손절 %.2f%% | 포지션 크기 %s | 최대 %d개",
                settings.entry_premium_threshold_percent, settings.target_net_profit_percent,
                settings.stop_loss_percent, size_text, settings.max_open_positions)
    logger.info("바이비트 레버리지 %.0f배 → 목표 자본 비율 업비트 %.1f : 바이비트 %.1f (여유분 %.0f%%) | "
                "강제청산 경고 %.0f%% 이내 | 리밸런싱 %.0f%%p 이탈",
                settings.bybit_leverage, share * 100, (1 - share) * 100, settings.bybit_margin_buffer_percent,
                settings.liquidation_warn_percent, settings.rebalance_threshold_percent)
    logger.info("비용: 업비트 %.3f%% (코인·USDT 매매 각각) · 바이비트 %.3f%% (왕복 %.2f%%) · 송금 한 번 %.2f USDT",
                f.upbit_trade_fee_rate * 100, f.bybit_taker_fee_rate * 100,
                f.round_trip_trade_fee_percent(), settings.rebalance_transfer_fee_usdt)
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

    safety = Safety.from_settings(settings)
    try:
        broker = make_broker(settings, coins, safety)
    except Exception:
        logger.exception("거래소 연결 실패 (키·IP 허용 확인: python3 -m kimp.live_check)")
        return 2
    session_factory = make_session_factory(settings.active_db_path)
    engine = Engine(settings=settings, market=market, fx=fx, broker=broker,
                    session_factory=session_factory, coins=coins)

    # Ctrl+C 를 누르면 '끝내라' 표시만 해두고, 엔진이 하던 스캔을 마친 뒤 멈춘다
    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        """종료 신호를 받으면 '끝내라' 표시만 한다 (실제로 멈추는 건 엔진 루프)"""
        logger.info("종료 신호(%s) 수신, 현재 스캔을 마치고 종료합니다", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    start_telegram(settings, broker, session_factory, stop_event)

    # 펀딩비 수익 전략: 항상 페이퍼. 김프 전략과 자본·시세 객체·DB 연결을 따로 써서 다른 스레드로 돌린다
    carry_thread = None
    if settings.carry_enabled:
        try:
            carry = CarryEngine(settings, LiveCarryMarket(settings), CarryBroker(settings),
                                make_session_factory(settings.db_path))
            carry_thread = threading.Thread(target=carry.run, args=(stop_event,), name="carry", daemon=True)
            carry_thread.start()
        except Exception:
            logger.exception("펀딩비 전략 시작 실패 (김프 전략은 계속)")

    engine.run(stop_event)
    if carry_thread:
        carry_thread.join(timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())

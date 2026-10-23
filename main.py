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
from datetime import datetime, timezone

from kimp.broker import HedgeBroker, PaperBroker, broker_kwargs
from kimp.carry import CarryBroker, CarryEngine, LiveCarryMarket
from sqlalchemy import select

from kimp.config import Settings, load_settings
from kimp.db import make_session_factory
from kimp.engine import Engine, _as_utc
from kimp.executor import BYBIT_USDT, UPBIT_USDT
from kimp.fx import FxProvider
from kimp.telemetry import end_run, install_db_log_handler, start_run
from kimp.logging_setup import setup_logging
from kimp.market_data import MarketData
from kimp.safety import KST, Safety

LF = chr(10)

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
        from kimp.broker import dir_label
        positions = broker.open_positions(session)
        lines = [f"[현황] {MODE_LABEL[settings.mode]} · {datetime.now(KST):%m-%d %H:%M}",
                 f"잔고 업비트 {broker.balance(session, UPBIT_USDT):,.2f} / 바이비트 "
                 f"{broker.balance(session, BYBIT_USDT):,.2f} USDT (포지션 제외)",
                 f"오늘 확정 손익 {broker.safety.daily_loss_usdt(session):+,.2f} USDT",
                 "신규 진입 " + ("가능" if ok else f"중단 — {why}"),
                 f"포지션 {len(positions)} / {settings.max_open_positions}"]
        for p in positions:
            # 남은 거리: 순손익과 청산 김프는 1:1 로 움직이므로 '목표 − 지금 순손익' 이 곧 필요한 상승폭(%p)
            net = p.last_net_percent or 0.0
            target = p.target_net_profit_percent or 0.0
            stop = p.stop_loss_percent or 0.0
            held_h = (datetime.now(timezone.utc) - _as_utc(p.opened_at)).total_seconds() / 3600
            limit = f" / {settings.one_shot_max_hours:g}시간" if (settings.one_shot and settings.one_shot_max_hours) else ""
            lines.append(
                f"#{p.id} {p.coin} 진입 {p.entry_premium:+.2f}% · 순손익 {net:+.2f}% → 목표 {target:+.2f}% "
                f"({target - net:+.2f}%p 더) · 손절 {stop:.0f}% 까지 {net - stop:+.2f}%p · "
                f"보유 {held_h:.1f}시간{limit} · 펀딩 {p.funding_usdt:+.3f} USDT")
        for p in broker.unfinished_positions(session):
            lines.append(f"⚠️ #{p.id} {p.coin} {p.status} — 다음 스캔에 이어서 처리")
        for t in broker.pending_transfers(session):
            lines.append(f"송금 중 #{t.id} {dir_label(t.direction)} {t.amount_usdt:,.2f} USDT")
    return "\n".join(lines)


HELP_TEXT = (
    "[명령어]" + LF +
    "/status   잔고·포지션·진입 가능 여부" + LF +
    "/premium  지금 김프 (가운데·진입·청산, 기준까지 얼마 남았나)" + LF +
    "/close    강제 청산 + 신규 진입 중단 (예: /close 또는 /close CAP,SEI → 재개는 /resume)" + LF +
    "/stop     신규 진입 중단 (청산·관리는 계속)" + LF +
    "/resume   재개" + LF +
    "/collect  시세 수집 현황 (/collect start 40 · /collect stop)" + LF +
    "/help     이 목록"
)


def premium_text(settings: Settings, market) -> str:
    """
    /premium 답장: 지금 김프를 코인별로 한 줄씩

    진입 김프가 기준보다 얼마나 높은지(얼마나 더 떨어져야 하는지)까지 보여준다.
    """
    from kimp.market_data import fetch_tops

    if market is None:
        return "시세 조회를 쓸 수 없습니다"
    tops = fetch_tops(market, list(settings.coins))
    if not tops:
        return "시세를 못 받았습니다"
    lines = [f"[김프] {datetime.now(KST):%m-%d %H:%M} · USDT {next(iter(tops.values())).usdt_mid:,.1f}원"]
    for coin in settings.coins:
        t = tops.get(coin)
        if t is None:
            lines.append(f"{coin} 시세 없음")
            continue
        threshold = settings.params_for(coin).entry_premium_threshold_percent
        need = t.entry_premium_top - threshold  # + 면 이만큼 더 떨어져야 진입
        lines.append(
            f"{coin} 진입 {t.entry_premium_top:+.3f}% (기준 {threshold:+.2f}%, {need:+.2f}%p 남음) · "
            f"청산 {t.exit_premium_top:+.3f}% · 가운데 {t.mid_premium:+.3f}% · "
            f"왕복 호가 {t.entry_premium_top - t.exit_premium_top:.2f}% · 펀딩 {t.funding_8h_percent() or 0:+.4f}%/8h")
    return LF.join(lines)


# /collect 로 띄운 수집기 프로세스 (한 번에 하나만)
_COLLECTOR: dict = {"proc": None, "args": ""}


def collect_command(settings: Settings, args: str) -> str:
    """
    /collect            수집 현황 (코인별 기록 줄 수)
    /collect start 40   거래대금 상위 40개 수집 시작 (숫자를 안 적으면 .env COINS)
    /collect stop       수집 중지
    """
    import subprocess
    import sys

    from kimp.webui import recorded_coins

    action, _, rest = args.partition(" ")
    action = action.strip().lower()
    proc = _COLLECTOR["proc"]
    running = proc is not None and proc.poll() is None

    if action == "stop":
        if not running:
            return "수집기가 돌고 있지 않습니다 (터미널에서 직접 띄운 것은 여기서 못 끕니다)"
        proc.terminate()
        _COLLECTOR["proc"] = None
        return "수집기를 멈췄습니다"

    if action == "start":
        if running:
            return f"이미 돌고 있습니다 ({_COLLECTOR['args']}). 먼저 /collect stop"
        top = (rest or action and "").strip()
        cmd = [sys.executable, "-m", "kimp.collect", "--interval", "60"]
        if top.isdigit():
            cmd += ["--top", top]
        _COLLECTOR["proc"] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _COLLECTOR["args"] = " ".join(cmd[2:])
        return f"수집기를 켰습니다: {_COLLECTOR['args']}"

    # 현황
    rows = recorded_coins(settings)
    ready = sum(1 for r in rows if r["count"] >= 30)
    lines = [f"[수집] {'돌고 있음' if running else '안 돌고 있음 (이 봇이 띄운 것 기준)'}",
             f"기록 있는 코인 {len(rows)}개 · 30줄 넘은 코인 {ready}개 ({settings.snapshot_db_path})"]
    for r in rows[:12]:
        lines.append(f"  {r['coin']} {r['count']:,}줄" + ("" if r["count"] >= 30 else " (30줄 필요)"))
    if len(rows) > 12:
        lines.append(f"  … 그 외 {len(rows) - 12}개")
    lines.append("/collect start 40 · /collect stop")
    return LF.join(lines)


def start_telegram(settings: Settings, broker: HedgeBroker, session_factory, stop_event: threading.Event,
                   engine=None, market=None):
    """텔레그램 알림(로그 → 텔레그램) + 명령 + 매일 9시 요약 (명령 목록은 HELP_TEXT)"""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        if settings.mode != "paper":
            logger.warning("텔레그램 설정이 없어서 알림을 못 보냅니다 (실거래에서는 꼭 설정하세요)")
        return None
    from kimp.notify import TelegramCommands, TelegramLogHandler, TelegramNotifier

    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id,
                                prefix=f"[{MODE_LABEL[settings.mode]}] ")
    logging.getLogger().addHandler(TelegramLogHandler(notifier))

    # ---- 텔레그램 명령 핸들러. 돌려주는 글자가 그대로 답장이 된다 ----
    # 여기서 하는 일은 '깃발을 세우는 것' 뿐이다. 실제 동작은 다음 스캔에서 엔진이 한다 —
    # 텔레그램 스레드가 매매 로직을 직접 건드리면 두 스레드가 같은 DB 를 동시에 고치게 된다.
    def stop() -> str:
        broker.safety.manual_stop = True
        return "신규 진입을 멈췄습니다 (가진 포지션은 계속 관리). 재개: /resume"

    def resume() -> str:
        broker.safety.resume()
        return "재개했습니다 (수동 정지·연속 실패 횟수 풀림). 다른 이유로 막혀 있으면 /status 에 나옵니다"

    TelegramCommands(notifier, {
        "/status": lambda: status_text(settings, broker, session_factory),
        "/stop": stop,
        "/resume": resume,
        "/premium": lambda: premium_text(settings, market),
        "/close": lambda args: (engine.request_close(args.split(",") if args else None) if engine
                                else "엔진이 아직 준비되지 않았습니다"),
        "/collect": lambda args: collect_command(settings, args),
        "/help": lambda: HELP_TEXT,
    }).start(stop_event)

    def daily() -> None:
        """
        매일 한국 시간 9시에 하루 요약 보내기 (데몬 스레드)

        sent_for 로 '어느 날짜 분을 보냈나' 를 기억한다. 30초마다 깨어나므로 9시대에
        여러 번 걸리는데, 날짜가 같으면 다시 보내지 않는다.
        """
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
    from kimp.notify import pretty
    notifier.send(pretty(f"[봇 시작] {MODE_LABEL[settings.mode]} | 코인 {', '.join(settings.coins)}"))
    return notifier   # 종료할 때 큐를 비우려고 (notifier.flush)


def print_one_shot_report(settings: Settings, session_factory) -> None:
    """
    한 번만 돌리기(ONE_SHOT)가 끝났을 때 '제대로 돌았나' 점검표를 찍어준다

    처음 실제 주문을 내보는 자리라서, 어디를 확인해야 하는지까지 같이 알려준다.
    (거래소 앱의 체결 내역과 숫자가 맞는지 사람이 봐야 한다)
    """
    from kimp.db import Fill, FundingEvent, Position, Transfer

    line = "=" * 78
    with session_factory() as session:
        pos = session.scalars(select(Position).order_by(Position.id.desc()).limit(1)).first()
        if pos is None:
            print(f"{LF}{line}{LF}[한 번만 돌리기] 진입한 포지션이 없습니다 "
                  f"(김프가 기준까지 내려오지 않았거나, 안전장치에 막혔습니다){LF}{line}")
            return
        fills = session.scalars(select(Fill).where(Fill.position_id == pos.id).order_by(Fill.id)).all()
        fundings = session.scalars(select(FundingEvent).where(FundingEvent.position_id == pos.id)).all()
        transfers = session.scalars(select(Transfer).order_by(Transfer.id)).all()

    from kimp.engine import _as_utc
    held = ((_as_utc(pos.closed_at).timestamp() - _as_utc(pos.opened_at).timestamp()) / 3600
            if pos.closed_at else 0.0)
    ok = pos.status == "CLOSED"
    print()
    print(line)
    print(f"[한 번만 돌리기 결과] 모드 {settings.mode} · 포지션 #{pos.id} {pos.coin} · 상태 {pos.status}"
          f"{' ✅' if ok else ' ⚠️ 확인 필요'}")
    print(line)
    print(f"  진입   {pos.opened_at:%Y-%m-%d %H:%M:%S} UTC  김프 {pos.entry_premium:+.3f}% "
          f"(기준 {pos.entry_threshold_percent:+.2f}%)")
    print(f"         업비트 USDT {pos.usdt_sold:.2f}개 매도 @{pos.entry_usdt_price_krw:,.2f}원 "
          f"→ {pos.coin} {pos.upbit_qty:.6g}개 매수 @{pos.entry_upbit_price_krw:,.4f}원")
    print(f"         바이비트 숏 {pos.bybit_qty:g}개 @{pos.entry_bybit_price_usdt:.6f} "
          f"({pos.leverage:g}배, 증거금 {pos.bybit_margin_usdt:.2f}, 강제청산 {pos.liquidation_price:.6f})")
    if pos.closed_at:
        print(f"  청산   {pos.closed_at:%Y-%m-%d %H:%M:%S} UTC  김프 {pos.exit_premium:+.3f}% "
              f"· 사유 {pos.exit_reason} · 보유 {held:.2f}시간")
        print(f"         {pos.coin} {pos.upbit_qty:.6g}개 매도 @{pos.exit_upbit_price_krw:,.4f}원 "
              f"→ USDT {pos.usdt_bought:.2f}개 매수 @{pos.exit_usdt_price_krw:,.2f}원")
        print(f"  손익   업비트 {pos.upbit_leg_usdt:+.3f} + 바이비트 {pos.bybit_leg_usdt:+.3f} "
              f"+ 펀딩 {pos.funding_usdt:+.3f} − 비용 {pos.total_cost_usdt:.3f} "
              f"= 순손익 {pos.net_pnl_usdt:+.3f} USDT ({pos.net_pnl_percent:+.3f}%)")
    else:
        print("  청산   아직 안 됨")
    print(f"  체결   {len(fills)}건 (진입 3 + 청산 3 이 정상. 부분 체결이 있으면 더 많다)")
    for f in fills:
        state = f.state or "DONE"
        print(f"         {f.exchange:7s} {f.side:16s} {f.qty:>14.6g} @ {f.avg_price:>14.6g} "
              f"수수료 {f.fee:>10.4f} {f.fee_currency}  밀림 {f.slippage_percent:+.3f}%  {state}")
    if fundings:
        print(f"  펀딩비 {len(fundings)}번, 합계 {sum(f.amount_usdt for f in fundings):+.4f} USDT")
    if transfers:
        print(f"  송금   {len(transfers)}번")
        for t in transfers:
            print(f"         {t.direction} {t.amount_usdt:.2f} USDT (수수료 {t.fee_usdt:g}, {t.network}) {t.status}")
    print()
    print("  확인할 것")
    print(f"    1) 거래소 앱의 체결 내역과 위 '체결' 의 개수·평균가·수수료가 같은지")
    print(f"    2) 양쪽 잔고가 예상대로인지 (업비트 USDT, 바이비트 USDT·포지션 0)")
    print(f"    3) 자세한 기록: {settings.active_db_path} 의 positions · fills 테이블 "
          f"(python -m kimp.webui 의 '페이퍼 봇' 탭에서도 보입니다)")
    print(f"    4) 로그 파일: {settings.log_dir}/")
    if pos.status == "ERROR":
        print("    ⚠️ 상태가 ERROR 입니다 → 거래소에서 직접 정리하고 DB 상태를 고친 뒤 다시 켜세요 (docs/live.md 6번)")
    elif pos.status == "ABORTED":
        print("    ⚠️ 진입 중 실패해서 되돌렸습니다 → 양쪽에 남은 것이 없는지 확인하세요")
    print(line)


def main() -> int:
    """
    설정 읽기 → 준비 → 엔진 실행. 돌려주는 값은 종료 코드 (0 = 정상)

    준비 순서 (앞 단계가 실패하면 뒤로 가지 않는다)
        1) .env 읽기·검증 — 실거래인데 LIVE_CONFIRM 이 YES 가 아니면 여기서 멈춘다
        2) 모드에 맞는 DB 열기 (페이퍼 kimp.db / 드라이런 dryrun.db / 실거래 live.db)
        3) 거래소 연결 + 실행기 만들기 (페이퍼는 키 없이도 된다)
        4) 바이비트 설정 맞추기 (단방향·격리·레버리지)
        5) 텔레그램 알림·명령 시작
        6) 엔진 루프 — Ctrl+C 로 멈추면 들고 있는 포지션은 **그대로 둔다** (다시 켜면 이어서 관리)
    """
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
        logger.warning("[실거래 시작] 실제 주문이 나갑니다 | 포지션 최대 %s USDT | 하루 손실 한도 %s USDT | 자동 송금 %s",
                       f"{settings.max_position_usdt:,.0f}", f"{settings.daily_loss_limit_usdt:,.0f}",
                       "켜짐" if settings.auto_transfer_enabled else "꺼짐 (알림만)")
    elif settings.mode == "dry_run":
        logger.info("드라이런 모드: 실제 잔고·호가로 돌지만 주문은 내지 않습니다 ('[드라이런] 실제였다면 …' 로그 확인)")
    logger.info("공통값: 진입 김프 ≤ %+.2f%% | 목표 순이익 %.2f%% | 손절 %.2f%% | 포지션 크기 %s | 최대 %d개",
                settings.entry_premium_threshold_percent, settings.target_net_profit_percent,
                settings.stop_loss_percent, size_text, settings.max_open_positions)
    logger.info("바이비트 레버리지 %.0f배 → 목표 자본 비율 업비트 %.1f : 바이비트 %.1f "
                "(여유분 %.0f%% — 수수료·펀딩비·추가 포지션용, 강제청산을 막아주지는 않음) | "
                "강제청산 경고 %.0f%% 이내 | 리밸런싱 %.0f%%p 이탈",
                settings.bybit_leverage, share * 100, (1 - share) * 100, settings.bybit_margin_buffer_percent,
                settings.liquidation_warn_percent, settings.rebalance_threshold_percent)
    logger.info("비용: 업비트 %.3f%% (코인·USDT 매매 각각) · 바이비트 %.3f%% (왕복 %.2f%%) · "
                "송금 한 번 업비트→바이비트 %.2f / 바이비트→업비트 %.2f USDT",
                f.upbit_trade_fee_rate * 100, f.bybit_taker_fee_rate * 100,
                f.round_trip_trade_fee_percent(), settings.transfer_fee_usdt(True),
                settings.transfer_fee_usdt(False))
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
            """코인별로 따로 적어 둔 값에 * 를 붙인다 (공통값과 구분해서 보여주려고)"""
            return "*" if name in p.overridden else " "

        size = f"{p.position_size_usdt:7.0f} USDT" if p.position_size_usdt else "   자동     "
        logger.info("  %-6s 진입 ≤ %+6.2f%%%s | 목표 %5.2f%%%s | 손절 %6.2f%%%s | 포지션 %s%s", coin,
                    p.entry_premium_threshold_percent, mark("entry_premium_threshold_percent"),
                    p.target_net_profit_percent, mark("target_net_profit_percent"),
                    p.stop_loss_percent, mark("stop_loss_percent"), size, mark("position_size_usdt"))
    logger.info("=" * 70)

    # 달러 환율은 표시용(테더 프리미엄·사이트식 김프). 못 받아도 매매는 한다.
    # 여기서 한 번 불러 두는 건 첫 스캔 전에 값을 채워 두려는 것 — 실패해도 그냥 넘어간다
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
    # 분석용: 이번 실행의 모드·코인·설정·코드 버전을 남기고, 새 포지션이 이 번호를 가리키게 한다.
    # 그리고 여기서부터 WARNING 이상 로그(예외 traceback 포함)는 DB events 에도 쌓인다
    engine.run_id = start_run(session_factory, settings, coins)
    install_db_log_handler(session_factory, engine.run_id)

    # Ctrl+C 를 누르면 '끝내라' 표시만 해두고, 엔진이 하던 스캔을 마친 뒤 멈춘다
    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        """종료 신호를 받으면 '끝내라' 표시만 한다 (실제로 멈추는 건 엔진 루프)"""
        logger.info("종료 신호(%s) 수신, 현재 스캔을 마치고 종료합니다", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    notifier = start_telegram(settings, broker, session_factory, stop_event, engine=engine, market=market)

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
    end_run(session_factory, engine.run_id, "one_shot" if engine._one_shot_done else "stopped")
    if carry_thread:
        carry_thread.join(timeout=30)
    if settings.one_shot:
        print_one_shot_report(settings, session_factory)
    # 보내는 스레드는 데몬이라 여기서 끝내면 큐에 남은 알림이 사라진다 (청산 알림이 가장 중요하다)
    if notifier is not None:
        notifier.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

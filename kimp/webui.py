"""
작은 로컬 화면 (브라우저로 보는 편의 도구)

    python -m kimp.webui          # http://127.0.0.1:8765 열기

왜 로컬 서버인가: 브라우저만으로는 `.env` 설정, `data/*.db` 기록, 거래소 API 를 읽을 수 없다.
그래서 파이썬 표준 라이브러리(http.server)로 작은 서버를 띄우고, 화면은 web/index.html 한 장이다.
외부 프레임워크를 쓰지 않는다 (CDN 도 안 쓴다 → 인터넷이 끊겨도 화면은 뜬다).

보여주는 것
    설정        지금 .env 로 봇이 쓰는 값 (코인별 진입 기준, 목표, 비용, 레버리지, 모드)
    김프 차트   코인별 봇 김프(코인 몫). 봇 기록(premium_snapshots)이 있으면 그걸,
                없으면 과거 1분봉(data/history.db)으로 계산해서 그린다
    지금 김프   거래소 호가창을 그 자리에서 조회한 진입/청산 김프 (새로고침마다)
    추천 코인   python -m kimp.screen 이 남긴 data/screen_latest.json
    데이터      시세 수집 실행·기록 현황, 초 단위 김프 시계열(수집기 --ws, tick_premiums) 차트
    시뮬레이션  python -m kimp.simulate 가 history DB 에 저장한 실행 기록과 거래 내역
    대시보드    기간을 정해서 본 성과 (누적 손익·일별·코인별·거래 이력). 집계는 kimp/dashboard.py
    관리선      목표 순이익에 맞는 진입·청산 김프 (비용 역산 + 기회 많은 자리). 계산은 kimp/levels.py

주의: 127.0.0.1 에만 바인딩한다 (다른 PC 에서 접근 불가). API 키는 화면에 보내지 않는다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from kimp.config import Settings, load_settings
from kimp.dashboard import dashboard
from kimp.levels import plan as level_plan
from kimp.db import PaperBalance, Position, PositionStatus, PremiumSnapshot, TickPremium, make_session_factory
from kimp.history import BYBIT, UPBIT, UPBIT_USDT, HistoryStore, SimRun, SimTrade, make_history_session_factory
from kimp.spread import (MODE_AUTO, MODE_LABELS, MODES, forced_coins, resolve_spreads,
                         snapshot_spreads_multi)

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SCREEN_JSON = "data/screen_latest.json"
MINUTE_MS = 60_000
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


# ======================================================================
# 화면에 줄 데이터 만들기 (서버와 분리해서 테스트하기 쉽게)
# ======================================================================
def config_summary(settings: Settings) -> Dict[str, Any]:
    """지금 설정 요약. API 키는 넣지 않는다 (있는지 여부만)"""
    f = settings.fees
    share = settings.upbit_target_share()
    return {
        "mode": settings.mode,
        "coins": list(settings.coins),
        "per_coin": [
            {
                "coin": c,
                "entry": settings.params_for(c).entry_premium_threshold_percent,
                "target": settings.params_for(c).target_net_profit_percent,
                "stop": settings.params_for(c).stop_loss_percent,
                "size": settings.params_for(c).position_size_usdt,
            }
            for c in settings.coins
        ],
        "target": settings.target_net_profit_percent,
        "stop": settings.stop_loss_percent,
        "position_size_usdt": settings.position_size_usdt,
        "max_open_positions": settings.max_open_positions,
        "max_position_usdt": settings.max_position_usdt,
        "leverage": settings.bybit_leverage,
        "capital_split": {"upbit": round(share * 100, 1), "bybit": round((1 - share) * 100, 1)},
        "fees": {
            "upbit_percent": f.upbit_trade_fee_rate * 100,
            "bybit_percent": f.bybit_taker_fee_rate * 100,
            "round_trip_percent": f.round_trip_trade_fee_percent(),
            "transfer_to_bybit": settings.transfer_fee_usdt(True),
            "transfer_to_upbit": settings.transfer_fee_usdt(False),
        },
        "keys_present": bool(settings.upbit_access_key and settings.bybit_api_key),
        "telegram": bool(settings.telegram_bot_token),
        "db": {"paper": settings.db_path, "history": settings.history_db_path,
               "paper_mb": _file_mb(settings.db_path), "history_mb": _file_mb(settings.history_db_path)},
        "warnings": list(settings.config_warnings),
    }


def _file_mb(path: str) -> Optional[float]:
    """파일 크기 (MB). 없으면 None — 과거 캔들 DB 가 얼마나 커졌는지 화면에서 보려고"""
    try:
        return round(os.path.getsize(path) / 1_048_576, 1)
    except OSError:
        return None


def spread_summary(settings: Settings, coins: Sequence[str],
                   mode: str = MODE_AUTO) -> Dict[str, Any]:
    """
    코인별 호가 간격과 그 출처 (어느 모드로 보면 어떤 값이 쓰이는지)

    코인별 설정(<코인>_SIM_SPREAD_PERCENT)으로 고정된 코인은 forced 로 알려준다 —
    모드를 바꿔도 그 코인은 안 바뀌므로 화면에 명시해야 한다.
    """
    paths = settings.snapshot_db_paths()
    records = snapshot_spreads_multi(paths, coins)
    forced = forced_coins(coins)
    spreads, sources = resolve_spreads(coins, paths, settings.sim_default_spread_percent, mode=mode)
    return {
        "mode": mode,
        "modes": [{"value": m, "label": MODE_LABELS[m]} for m in MODES],
        "common_percent": settings.sim_default_spread_percent,
        "forced": forced,
        "rows": [
            {
                "coin": c,
                "percent": spreads.get(c),
                "source": sources.get(c, "-"),
                "records": records.get(c, (None, 0))[1],
                "records_percent": records.get(c, (None, 0))[0],
                "forced": c in forced,
            }
            for c in coins
        ],
    }


def recorded_coins(settings: Settings) -> List[Dict[str, Any]]:
    """
    김프·호가 기록이 있는 코인과 건수 — 차트에서 '진입·청산 김프를 볼 수 있는 코인'

    수집기 DB(data/market.db)와 봇 DB 를 모두 본다 (수집기가 주로 쌓고, 봇도 돌면서 남긴다).
    """
    total: Dict[str, int] = {}
    for path in settings.snapshot_db_paths():
        if not os.path.exists(path):
            continue
        with make_session_factory(path)() as session:
            for coin, n in session.execute(
                    select(PremiumSnapshot.coin, func.count()).group_by(PremiumSnapshot.coin)).all():
                total[coin] = total.get(coin, 0) + n
    return [{"coin": c, "count": n} for c, n in sorted(total.items(), key=lambda kv: -kv[1])]


def _unique_paths(paths) -> List[str]:
    """같은 파일이 두 번 들어오면(모드 DB 와 수집 DB 가 같은 경로) 한 번만 (기록이 두 배로 세지지 않게)"""
    seen, out = set(), []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p)) if p != ":memory:" else p
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def tick_summary(settings: Settings) -> Dict[str, Any]:
    """초 단위 김프 시계열(tick_premiums) 현황: 코인별 건수·기간, 전체 건수, DB 크기 (데이터 탭)"""
    coins: Dict[str, Dict[str, Any]] = {}
    total = 0
    size_mb = 0.0
    for path in _unique_paths(settings.snapshot_db_paths()):
        if not os.path.exists(path):
            continue
        with make_session_factory(path)() as session:
            rows = session.execute(
                select(TickPremium.coin, func.count(), func.min(TickPremium.ts_ms), func.max(TickPremium.ts_ms))
                .group_by(TickPremium.coin)).all()
        if rows:
            size_mb += os.path.getsize(path) / 1e6
        for coin, n, lo, hi in rows:
            c = coins.setdefault(coin, {"coin": coin, "count": 0, "from_ms": lo, "to_ms": hi})
            c["count"] += n
            c["from_ms"], c["to_ms"] = min(c["from_ms"], lo), max(c["to_ms"], hi)
            total += n
    out = sorted(coins.values(), key=lambda c: -c["count"])
    return {"rows": total, "coins": out, "size_mb": round(size_mb, 1),
            "to_ms": max((c["to_ms"] for c in out), default=None)}


def tick_history(settings: Settings, coin: str, minutes: float = 30.0, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """
    코인 하나의 초 단위 김프 시계열 (데이터 탭 차트). 값이 바뀐 점만 있으므로 선으로 이으면 계단이 된다

    series: [{ts, entry, exit, mid, skew}] 시각순. 기록이 없으면 빈 목록
    """
    import time as _time

    now_ms = now_ms if now_ms is not None else int(_time.time() * 1000)
    start_ms = now_ms - int(minutes * 60_000)
    points: List[Dict[str, Any]] = []
    for path in _unique_paths(settings.snapshot_db_paths()):
        if not os.path.exists(path):
            continue
        with make_session_factory(path)() as session:
            for r in session.execute(
                    select(TickPremium).where(TickPremium.coin == coin.upper(), TickPremium.ts_ms >= start_ms,
                                              TickPremium.ts_ms <= now_ms).order_by(TickPremium.ts_ms)).scalars():
                points.append({"ts": r.ts_ms, "entry": r.entry_premium_top, "exit": r.exit_premium_top,
                               "mid": r.mid_premium, "skew": r.skew_ms})
    points.sort(key=lambda p: p["ts"])
    return {"coin": coin.upper(), "minutes": minutes, "series": points, "count": len(points)}


def premium_history(settings: Settings, coin: str, hours: float = 24.0, now_ms: Optional[int] = None,
                    md=None, force_fetch: bool = False) -> Dict[str, Any]:
    """
    코인 하나의 기록 (차트용). 두 가지를 **섞어서** 돌려준다.

        과거 1분봉 (data/history.db)  → 기간 전체의 가운데 김프·가격·펀딩비
        봇 기록 (premium_snapshots)   → 봇이 돌던 구간의 진입·청산 김프, 호가 간격, 테더 프리미엄

    봇 기록만 쓰면 봇을 17분 돌렸을 때 7일을 요청해도 17분치만 나온다. 그래서 1분봉으로 기간을 채우고,
    봇 기록이 있는 분은 그 값으로 덮어쓴다 (그 분에는 호가 정보까지 들어간다).

    series 의 각 칸 (없으면 null)
        mid     가운데 가격 기준 봇 김프 (%)          ← 보통 시세
        entry   맨 앞 호가로 지금 진입하면 나오는 김프 (%)   ← 봇 기록이 있는 분만
        exit    맨 앞 호가로 지금 청산하면 나오는 김프 (%)   ← 봇 기록이 있는 분만
        spread  코인 호가 간격 (%) = entry − exit − usdt_spread
        usdt_spread  업비트 USDT/KRW 호가 간격 (%)
        funding 바이비트 펀딩비율 (%, 8시간 기준 환산). + 면 숏인 우리가 받는다
        tether  테더 프리미엄 (%) / site 사이트식 김프 (%)
        usdt_krw 업비트 USDT 가격 (원) / upbit_krw · bybit_usdt 각 거래소 코인 가격
    """
    import time as _time

    now_ms = now_ms if now_ms is not None else int(_time.time() * 1000)
    start_ms = now_ms - int(hours * 3_600_000)

    # 1) 과거 1분봉으로 기간 전체를 채운다
    store = HistoryStore(make_history_session_factory(settings.history_db_path))
    skipped = False
    if md is not None:
        if hours <= AUTO_FETCH_MAX_HOURS or force_fetch:
            _ensure_candles(store, md, coin, start_ms, now_ms)
        else:
            skipped = True  # 긴 기간은 사용자가 직접 누를 때만 받는다 (DB 가 커지므로)
    upbit = store.load_closes(UPBIT, coin, start_ms, now_ms)
    bybit = store.load_closes(BYBIT, coin, start_ms, now_ms)
    usdt = store.load_closes(UPBIT_USDT, "USDT", start_ms, now_ms)
    funding = dict(store.load_funding(coin, start_ms, now_ms))
    rows: Dict[int, Dict[str, Any]] = {}
    for ts in sorted(upbit.keys() & bybit.keys() & usdt.keys()):
        b, u = bybit[ts], usdt[ts]
        if b <= 0 or u <= 0:
            continue
        rows[ts] = {"ts": ts, "mid": (upbit[ts] / (b * u) - 1) * 100, "entry": None, "exit": None,
                    "spread": None, "usdt_spread": None,
                    "funding": (funding[ts] * 100) if ts in funding else None,
                    "tether": None, "site": None,
                    "usdt_krw": u, "upbit_krw": upbit[ts], "bybit_usdt": b}
    candle_count = len(rows)

    # 2) 봇 기록이 있는 분은 그 값으로 덮어쓴다 (호가 정보가 들어 있어서 더 정확하다)
    snap_count = 0
    records = []
    for path in settings.snapshot_db_paths():
        if not os.path.exists(path):
            continue
        with make_session_factory(path)() as session:
            records += session.execute(
                select(PremiumSnapshot).where(PremiumSnapshot.coin == coin)
                .order_by(PremiumSnapshot.created_at)
            ).scalars().all()
    if records:
        for r in records:
            if r.created_at is None:
                continue
            ts = int(r.created_at.replace(tzinfo=r.created_at.tzinfo or _utc()).timestamp() * 1000)
            if ts < start_ms or ts > now_ms:
                continue
            minute = ts // MINUTE_MS * MINUTE_MS  # 1분봉과 같은 칸에 맞춘다
            spread = None
            if r.entry_premium_top is not None and r.exit_premium_top is not None:
                spread = r.entry_premium_top - r.exit_premium_top - (r.usdt_spread_percent or 0.0)
            rows[minute] = {
                "ts": minute, "mid": r.mid_premium, "entry": r.entry_premium_top, "exit": r.exit_premium_top,
                "spread": spread, "usdt_spread": r.usdt_spread_percent,
                "funding": _funding_8h_percent(r.funding_rate, r.funding_interval_hours),
                "tether": r.tether_premium, "site": r.site_premium,
                "usdt_krw": r.usdt_krw, "upbit_krw": r.upbit_mid_krw, "bybit_usdt": r.bybit_mid_usdt,
            }
            snap_count += 1

    series = [rows[k] for k in sorted(rows)]
    if snap_count and candle_count:
        source = f"1분봉 {candle_count:,}개 + 봇 기록 {snap_count:,}개"
    elif snap_count:
        source = f"봇 기록 {snap_count:,}개"
    else:
        source = f"1분봉 {candle_count:,}개" if candle_count else "없음"
    out: Dict[str, Any] = {"coin": coin, "source": source, "series": series,
                           "available": _available_keys(series), "recorded": recorded_coins(settings),
                           "snapshots": snap_count, "candles": candle_count}
    if skipped and candle_count < hours * 60 * 0.9:
        out["fetch_skipped"] = True
        out["hint"] = (f"{hours / 24:.0f}일치 1분봉이 아직 없습니다 (코인당 약 {hours * 60 * 2 / 1000:.0f}천 개). "
                       f"'지금 받아오기' 를 누르면 받아서 저장합니다 (시뮬레이션에서도 재사용됩니다).")
    return out


def _available_keys(series: Sequence[Dict[str, Any]]) -> List[str]:
    """
    이 기록에 값이 들어 있는 지표만 골라준다 (화면에서 없는 지표를 흐리게 보여주려고)

    1분봉으로 만든 기록에는 호가가 없다 → 진입·청산 김프, 호가 간격, 테더 프리미엄이 없다.
    그걸 알려주지 않으면 "토글은 켜져 있는데 아무것도 안 그려진다" 로 보인다.
    """
    keys = ("mid", "entry", "exit", "spread", "usdt_spread", "funding", "tether", "site",
            "usdt_krw", "upbit_krw", "bybit_usdt")
    return [k for k in keys if any(r.get(k) is not None for r in series)]


def _ensure_candles(store: HistoryStore, md, coin: str, start_ms: int, now_ms: int) -> None:
    """
    과거 1분봉이 없으면 그 자리에서 받아온다 (화면에서 아무 코인이나 차트로 보려고)

    24시간이면 1,440개라 업비트 8번·바이비트 2번 요청으로 금방이다.
    30일(43,200개)이면 업비트 216번이라 20초쯤 걸린다. 받은 캔들은 data/history.db 에 남아
    시뮬레이션에서도 그대로 쓰인다.
    """
    from kimp.history import ccxt_candle_fetcher, ccxt_funding_fetcher
    from kimp.market_data import USDT_KRW_SYMBOL, bybit_symbol, upbit_symbol

    try:
        store.ensure_candles(UPBIT_USDT, "USDT", start_ms, now_ms,
                             ccxt_candle_fetcher(md.upbit, USDT_KRW_SYMBOL))
        store.ensure_candles(UPBIT, coin, start_ms, now_ms,
                             ccxt_candle_fetcher(md.upbit, upbit_symbol(coin)))
        store.ensure_candles(BYBIT, coin, start_ms, now_ms,
                             ccxt_candle_fetcher(md.bybit, bybit_symbol(coin)))
        store.ensure_funding(coin, start_ms, now_ms, ccxt_funding_fetcher(md.bybit, bybit_symbol(coin)))
    except Exception as exc:  # 받기 실패해도 있는 것만 그린다
        logger.warning("%s 캔들 받기 실패: %s", coin, exc)


def _funding_8h_percent(rate: Optional[float], interval_hours: Optional[float]) -> Optional[float]:
    """
    펀딩비율을 8시간 기준 %로 바꾸기

    코인마다 정산 주기가 1·4·8시간으로 달라서 그대로 비교하면 안 된다.
      예) 1시간마다 0.0025% → 8시간 기준 0.02%
    """
    if rate is None:
        return None
    hours = interval_hours or 8.0
    return rate * 100 * (8.0 / hours if hours else 1.0)


def _utc():
    """UTC 시간대 (SQLite 가 시간대 없이 저장하므로 읽을 때 붙여 준다)"""
    from datetime import timezone
    return timezone.utc


# 전체 코인을 볼 때 쓰는 값들
UNIVERSE_TTL_SEC = 600.0  # 코인 목록·거래량은 10분 캐시 (매번 받을 필요 없다)

# 차트에서 기록이 없는 코인의 1분봉을 '자동으로' 받아줄 최대 기간 (시간).
# 24시간이면 코인당 1분봉 2,880개(약 100KB)라 2~3초면 끝난다.
# 30일은 코인당 약 17만 개(6MB)라, 여러 코인을 눌러보면 DB 가 금방 커진다 → 사용자가 직접 누를 때만 받는다.
AUTO_FETCH_MAX_HOURS = 24.0
_UNIVERSE: Dict[str, Any] = {"at": 0.0, "coins": [], "volume": {}}


def universe(md, force: bool = False) -> Dict[str, Any]:
    """
    업비트 원화 마켓과 바이비트 USDT 무기한에 **둘 다 있는** 코인 목록 + 24시간 거래대금

    김프 헷지는 양쪽에서 동시에 거래해야 하므로 한쪽에만 있는 코인은 쓸 수 없다.
    목록과 거래량은 10분 동안 캐시한다 (티커 조회 2번).
    """
    import time as _time

    from kimp.market_data import bybit_symbol, upbit_symbol
    from kimp.screen import list_universe

    if not force and _UNIVERSE["coins"] and _time.time() - _UNIVERSE["at"] < UNIVERSE_TTL_SEC:
        return _UNIVERSE
    coins = list_universe(md)
    up_t = md.upbit.fetch_tickers([upbit_symbol(c) for c in coins])
    by_t = md.bybit.fetch_tickers()
    volume = {}
    for c in coins:
        up = up_t.get(upbit_symbol(c)) or {}
        by = by_t.get(bybit_symbol(c)) or {}
        volume[c] = {"upbit_krw": float(up.get("quoteVolume") or 0),
                     "bybit_usdt": float(by.get("quoteVolume") or 0)}
    coins.sort(key=lambda c: volume[c]["upbit_krw"], reverse=True)  # 거래대금 큰 순
    _UNIVERSE.update({"at": _time.time(), "coins": coins, "volume": volume})
    return _UNIVERSE


def live_premium(md, coins: Sequence[str]) -> List[Dict[str, Any]]:
    """
    지금 김프 (여러 코인을 한 번에)

    요청을 적게 쓰려고 이렇게 받는다.
        업비트  호가창을 50개씩 묶어서 (코인 150개면 3번)
        바이비트 티커 1번 — 맨 앞 호가·펀딩비율이 다 들어 있다 (913개가 1초)
    그래서 코인이 많아도 몇 초면 끝난다. 호가창을 코인마다 따로 받으면 150번이 된다.
    """
    from kimp.market_data import USDT_KRW_SYMBOL, bybit_symbol, upbit_symbol

    coins = list(coins)
    books: Dict[str, Any] = {}
    symbols = [upbit_symbol(c) for c in coins] + [USDT_KRW_SYMBOL]
    for i in range(0, len(symbols), 50):
        books.update(md.upbit.fetch_order_books(symbols[i:i + 50], 5))
    usdt = books.get(USDT_KRW_SYMBOL) or {}
    usdt_bid = (usdt.get("bids") or [[None]])[0][0]
    usdt_ask = (usdt.get("asks") or [[None]])[0][0]
    if not usdt_bid or not usdt_ask:
        return [{"coin": c, "error": "업비트 USDT 호가 없음"} for c in coins]
    usdt_mid = (usdt_bid + usdt_ask) / 2
    tickers = md.bybit.fetch_tickers()
    vol = universe(md)["volume"]
    fx = _fx_rate()  # 달러 환율 (표시용: 테더 프리미엄·사이트식 김프). 없으면 그 두 칸만 빈다
    tether = (usdt_mid / fx - 1) * 100 if fx else None

    out: List[Dict[str, Any]] = []
    for c in coins:
        up = books.get(upbit_symbol(c)) or {}
        by = tickers.get(bybit_symbol(c)) or {}
        up_bid = (up.get("bids") or [[None]])[0][0]
        up_ask = (up.get("asks") or [[None]])[0][0]
        by_bid, by_ask = by.get("bid"), by.get("ask")
        if not (up_bid and up_ask and by_bid and by_ask):
            out.append({"coin": c, "error": "호가 없음"})
            continue
        up_mid, by_mid = (up_bid + up_ask) / 2, (by_bid + by_ask) / 2
        prem = lambda u, b, t: (u / (b * t) - 1) * 100  # noqa: E731 (김프 공식: 2번 문서)
        info = by.get("info") or {}
        rate = info.get("fundingRate")
        interval = _bybit_funding_interval_hours(md, c)
        out.append({
            "coin": c,
            "mid": prem(up_mid, by_mid, usdt_mid),
            "entry": prem(up_ask, by_bid, usdt_bid),   # 살 때(ask) / 숏 칠 때(bid) / USDT 팔 때(bid)
            "exit": prem(up_bid, by_ask, usdt_ask),    # 팔 때(bid) / 숏 닫을 때(ask) / USDT 살 때(ask)
            "coin_spread": (up_ask / up_bid - 1) * 100 + (by_ask / by_bid - 1) * 100,
            "usdt_spread": (usdt_ask / usdt_bid - 1) * 100,
            "upbit_krw": up_mid, "bybit_usdt": by_mid, "usdt_krw": usdt_mid,
            "fx_krw": fx, "tether_premium": tether,
            # 사이트식 김프 = 달러 환율 기준. 대략 '봇 김프 + 테더 프리미엄'
            "site_premium": prem(up_mid, by_mid, fx) if fx else None,
            "funding_8h": _funding_8h_percent(float(rate) if rate not in (None, "") else None, interval),
            "upbit_krw_24h": (vol.get(c) or {}).get("upbit_krw"),
            "bybit_usdt_24h": (vol.get(c) or {}).get("bybit_usdt"),
        })
    return out


# 달러 환율은 표시용이라 10분 캐시로 충분하다 (김프 계산에는 쓰지 않는다)
_FX: Dict[str, Any] = {"at": 0.0, "rate": None}


def _fx_rate(ttl_sec: float = 600.0) -> Optional[float]:
    """달러 환율 (표시용). 실패하면 None — 테더 프리미엄·사이트식 김프 칸만 비워진다"""
    import time as _time

    if _FX["rate"] and _time.time() - _FX["at"] < ttl_sec:
        return _FX["rate"]
    try:
        from kimp.fx import fetch_usd_krw_from_yfinance
        _FX.update({"at": _time.time(), "rate": fetch_usd_krw_from_yfinance()})
    except Exception as exc:
        logger.warning("달러 환율 조회 실패 (표시용이라 매매에는 영향 없음): %s", exc)
    return _FX["rate"]


def _bybit_funding_interval_hours(md, coin: str) -> float:
    """바이비트 펀딩 정산 주기 (시간). 마켓 정보의 fundingInterval 은 분 단위다 (480분 = 8시간)"""
    from kimp.market_data import bybit_symbol

    try:
        info = (md.bybit.market(bybit_symbol(coin)) or {}).get("info") or {}
        minutes = float(info.get("fundingInterval") or 480)
        return minutes / 60 if minutes else 8.0
    except Exception:
        return 8.0


def screen_latest(path: str = SCREEN_JSON) -> Optional[Dict[str, Any]]:
    """스크리너가 마지막에 남긴 결과 (python -m kimp.screen 이 저장한다)"""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fp:
        return json.load(fp)


def sim_runs(settings: Settings, limit: int = 20) -> List[Dict[str, Any]]:
    """시뮬레이션 실행 목록 (최근 것부터)"""
    if not os.path.exists(settings.history_db_path):
        return []
    with make_history_session_factory(settings.history_db_path)() as session:
        rows = session.scalars(select(SimRun).order_by(SimRun.id.desc()).limit(limit)).all()
        return [
            {
                "id": r.id, "created_at": r.created_at, "from_ms": r.from_ms, "to_ms": r.to_ms,
                "coins": r.coins.split(","),
                "settings": json.loads(r.settings_json or "{}"),
                "summary": json.loads(r.summary_json or "{}"),
            }
            for r in rows
        ]


def sim_trades(settings: Settings, run_id: int) -> List[Dict[str, Any]]:
    """한 실행의 거래 내역"""
    if not os.path.exists(settings.history_db_path):
        return []
    with make_history_session_factory(settings.history_db_path)() as session:
        rows = session.scalars(select(SimTrade).where(SimTrade.run_id == run_id).order_by(SimTrade.id)).all()
        return [
            {
                "coin": t.coin, "opened_at": t.opened_at, "closed_at": t.closed_at,
                "entry_premium": t.entry_premium, "exit_premium": t.exit_premium,
                "exit_reason": t.exit_reason, "net_pnl_usdt": t.net_pnl_usdt,
                "net_pnl_percent": t.net_pnl_percent, "funding_usdt": t.funding_usdt, "cost_usdt": t.cost_usdt,
            }
            for t in rows
        ]


def candle_summary(settings: Settings) -> Dict[str, Any]:
    """과거 캔들 현황 (간격별 개수·코인 수·기간·대략 크기)"""
    from kimp.history import candle_stats, make_history_session_factory as _hsf

    path = settings.history_db_path
    size_mb = _file_mb(path) or 0.0
    if not os.path.exists(path):
        return {"db": path, "size_mb": None, "rows": 0, "sources": []}
    stats = candle_stats(_hsf(path))
    total = sum(x["rows"] for x in stats) or 1
    for x in stats:
        x["size_mb"] = round(x["rows"] / total * size_mb, 1)  # 전체 크기를 행 수로 나눠 어림한 값
    return {"db": path, "size_mb": size_mb, "rows": sum(x["rows"] for x in stats), "sources": stats}


def bot_state(settings: Settings) -> Dict[str, Any]:
    """
    봇 DB 에서 지금 상태 읽기 (잔고 · 열린 포지션 · 최근 닫은 거래)

    페이퍼 모드면 data/kimp.db, 드라이런·실거래면 각자 DB 를 본다 (섞이지 않는다).
    봇이 돌고 있지 않아도 마지막 상태가 그대로 보인다.
    """
    path = settings.active_db_path
    out: Dict[str, Any] = {"db": path, "mode": settings.mode, "exists": os.path.exists(path),
                           "balances": [], "positions": [], "recent": [], "snapshots": 0}
    if not out["exists"]:
        return out
    with make_session_factory(path)() as session:
        for row in session.scalars(select(PaperBalance).order_by(PaperBalance.asset)).all():
            out["balances"].append({"asset": row.asset, "amount": row.amount})
        open_states = (PositionStatus.OPENING, PositionStatus.OPEN, PositionStatus.CLOSING,
                       PositionStatus.ERROR)
        for p in session.scalars(select(Position).where(Position.status.in_(open_states))
                                 .order_by(Position.id)).all():
            out["positions"].append({
                "id": p.id, "coin": p.coin, "status": p.status, "opened_at": str(p.opened_at),
                "entry_premium": p.entry_premium, "upbit_qty": p.upbit_qty, "bybit_qty": p.bybit_qty,
                "usdt_sold": p.usdt_sold, "margin": p.bybit_margin_usdt,
                "liquidation_price": p.liquidation_price, "funding_usdt": p.funding_usdt,
                "target": p.target_net_profit_percent, "stop": p.stop_loss_percent,
            })
        for p in session.scalars(select(Position).where(Position.status == PositionStatus.CLOSED)
                                 .order_by(Position.id.desc()).limit(20)).all():
            out["recent"].append({
                "id": p.id, "coin": p.coin, "opened_at": str(p.opened_at), "closed_at": str(p.closed_at),
                "entry_premium": p.entry_premium, "exit_premium": p.exit_premium,
                "exit_reason": p.exit_reason, "net_pnl_usdt": p.net_pnl_usdt,
                "net_pnl_percent": p.net_pnl_percent,
            })
        out["snapshots"] = session.scalar(select(func.count()).select_from(PremiumSnapshot)) or 0
        out["snapshot_coins"] = [
            {"coin": c, "count": n, "last": str(last)}
            for c, n, last in session.execute(
                select(PremiumSnapshot.coin, func.count(), func.max(PremiumSnapshot.created_at))
                .group_by(PremiumSnapshot.coin).order_by(PremiumSnapshot.coin)).all()
        ]
    return out


# ======================================================================
# 화면에서 돌리기 (스크리너 / 시뮬레이션)
# ======================================================================
# 왜 따로 프로세스로 돌리나: 스크리너는 몇 분 걸리고 거래소를 많이 부른다.
# 서버 안에서 돌리면 그동안 화면이 멈추고, 중간에 취소할 수도 없다.
# 그래서 터미널에서 치는 것과 '똑같은 명령' 을 자식 프로세스로 돌리고 출력만 받아 보여준다.
JOBS: Dict[int, Dict[str, Any]] = {}
_JOB_SEQ = [0]
JOB_LOG_LINES = 400  # 로그는 마지막 400줄만 들고 있는다

# 화면에서 받을 수 있는 값만 허용한다 (아무 설정이나 넘기지 못하게)
SCREEN_PARAMS = {"days": "--days", "top": "--top", "coins": "--coins", "timeframe": "--timeframe",
                 "refine_top": "--refine-top", "refine_timeframe": "--refine-timeframe",
                 "target": "--target", "recent_days": "--recent-days",
                 "entry_threshold": "--entry-threshold", "spread_source": "--spread-source",
                 "exec_slippage": "--exec-slippage"}
# 손절은 스크리너에 옵션이 없고 설정에서 읽는다 → 환경 변수로 넘긴다 (비우면 .env 값)
SCREEN_ENV = {"stop": "STOP_LOSS_PERCENT"}
SIM_PARAMS = {"start": "--from", "end": "--to", "coins": "--coins",
              "spread_source": "--spread-source", "exec_slippage": "--exec-slippage"}
SIM_ENV = {"seed": "PAPER_INITIAL_TOTAL_USDT", "size": "POSITION_SIZE_USDT",
           "max_positions": "MAX_OPEN_POSITIONS", "target": "TARGET_NET_PROFIT_PERCENT",
           "stop": "STOP_LOSS_PERCENT"}
# 페이퍼 봇(main.py)은 옵션이 없고 설정(환경 변수)으로만 조절한다.
# 화면에서 켜는 봇은 '호가 기록을 쌓는 용도' 라 스캔을 느리게 하고 펀딩비 전략은 끈다 (요청 제한 여유).
BOT_ENV = {"coins": "COINS", "scan": "SCAN_INTERVAL_SEC", "seed": "PAPER_INITIAL_TOTAL_USDT",
           "size": "POSITION_SIZE_USDT", "max_positions": "MAX_OPEN_POSITIONS",
           "target": "TARGET_NET_PROFIT_PERCENT", "one_shot_max_hours": "ONE_SHOT_MAX_HOURS"}


def build_command(kind: str, params: Dict[str, str]) -> Tuple[List[str], Dict[str, str]]:
    """
    화면에서 받은 값으로 '터미널에 치는 명령' 과 환경 변수를 만든다

    예) kind=screen, {"days": "30", "coins": "CAP,SEI"}
        → ([python, -m, kimp.screen, --days, 30, --coins, CAP,SEI], {})
        kind=sim, {"start": "2026-09-22", "seed": "1500", "rough": "1"}
        → ([python, -m, kimp.simulate, --from, 2026-09-22, --rough-spread], {"PAPER_INITIAL_TOTAL_USDT": "1500"})
    """
    import sys

    if kind == "screen":
        cmd = [sys.executable, "-m", "kimp.screen"]
        allowed, env_map = SCREEN_PARAMS, SCREEN_ENV
    elif kind == "sim":
        cmd = [sys.executable, "-m", "kimp.simulate"]
        allowed, env_map = SIM_PARAMS, SIM_ENV
    elif kind == "fetch":
        cmd = [sys.executable, "-m", "kimp.history", "fetch"]
        allowed, env_map = {"coins": "--coins", "days": "--days", "timeframe": "--timeframe"}, {}
    elif kind == "prune":
        # 지우는 작업이라 화면에서 '정말 지웁니다' 를 체크했을 때만 (confirm=1) 받는다
        cmd = [sys.executable, "-m", "kimp.history", "prune"]
        allowed, env_map = {"days": "--days", "source": "--source", "coins": "--coins"}, {}
    elif kind == "collect":
        # 시세 수집기: 주문·포지션 없이 김프·호가만 적는다 (봇과 별개). all / ws 는 값 없는 스위치
        cmd = [sys.executable, "-m", "kimp.collect"]
        allowed, env_map = {"coins": "--coins", "top": "--top", "interval": "--interval",
                            "hours": "--hours"}, {}
        for key, flag in (("all", "--all"), ("ws", "--ws")):
            if (params.get(key) or "").strip() in ("1", "true", "on"):
                cmd.append(flag)
    elif kind == "bot":
        # 페이퍼 모드로만 켠다 (화면에서 실거래를 켤 수 있으면 사고가 난다)
        cmd = [sys.executable, "main.py"]
        allowed, env_map = {}, BOT_ENV
    else:
        raise ValueError(f"모르는 작업: {kind}")
    for key, flag in allowed.items():
        value = (params.get(key) or "").strip()
        if value:
            cmd += [flag, value]
    env: Dict[str, str] = {}
    for key, name in env_map.items():
        value = (params.get(key) or "").strip()
        if value:
            env[name] = value
    if kind == "collect" and params.get("all"):
        cmd.append("--all")
    if kind == "prune":
        if not params.get("confirm"):
            raise ValueError("지우기는 '정말 지웁니다' 를 체크해야 합니다")
        cmd.append("--yes")
    if kind == "bot":
        env["PAPER_TRADING"] = "true"  # 화면에서는 실거래를 켤 수 없다
        if params.get("one_shot"):
            env["ONE_SHOT"] = "true"   # 포지션 하나만 잡고 청산되면 스스로 종료
        env.setdefault("SCAN_INTERVAL_SEC", "20")
        env["CARRY_ENABLED"] = "true" if params.get("carry") else "false"
        if params.get("quiet"):
            env["TELEGRAM_BOT_TOKEN"] = ""  # 텔레그램 알림 끄기
    if kind == "sim":
        if params.get("rough"):
            cmd.append("--rough-spread")
        if params.get("verbose"):
            cmd.append("--verbose")
        if params.get("no_save"):
            cmd.append("--no-save")
        coins = (params.get("coins") or "").strip()
        if coins:
            env["COINS"] = coins  # 시뮬레이션은 .env 의 COINS 도 함께 맞춰야 코인별 설정이 적용된다
    # 코인별 진입 기준 (CAP=-0.55 처럼 받아서 CAP_ENTRY_PREMIUM_THRESHOLD_PERCENT 로)
    for item in (params.get("thresholds") or "").split(","):
        if "=" in item:
            coin, value = item.split("=", 1)
            coin, value = coin.strip().upper(), value.strip()
            if coin.isalnum() and value:
                env[f"{coin}_ENTRY_PREMIUM_THRESHOLD_PERCENT"] = value
    return cmd, env


def start_job(kind: str, params: Dict[str, str]) -> Dict[str, Any]:
    """자식 프로세스로 돌리고 출력을 모은다 (같은 종류가 이미 돌고 있으면 거절)"""
    import subprocess
    import threading
    import time as _time

    for job in JOBS.values():
        if job["kind"] == kind and job["status"] == "running":
            return {"error": f"이미 돌고 있습니다 (작업 #{job['id']}). 끝나거나 멈춘 뒤에 다시 하세요"}
    cmd, extra_env = build_command(kind, params)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **extra_env}
    _JOB_SEQ[0] += 1
    job_id = _JOB_SEQ[0]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    job = {"id": job_id, "kind": kind, "cmd": " ".join(cmd[2:]), "env": extra_env, "status": "running",
           "started": _time.time(), "log": [], "proc": proc, "returncode": None}
    JOBS[job_id] = job

    def reader():
        """자식 프로세스 출력을 읽어 로그에 쌓는다 (따로 스레드)

        진행률 표시는 캐리지리턴으로 한 줄을 덮어쓴다 → 마지막 조각만 남긴다.
        """
        for line in proc.stdout:
            text = line.rstrip().split('\r')[-1]
            if text:
                job["log"].append(text)
                del job["log"][:-JOB_LOG_LINES]
        proc.wait()
        job["returncode"] = proc.returncode
        job["status"] = "done" if proc.returncode == 0 else "failed"

    threading.Thread(target=reader, daemon=True).start()
    return job_view(job)


def stop_job(job_id: int) -> Dict[str, Any]:
    """돌고 있는 작업 멈추기"""
    job = JOBS.get(job_id)
    if not job:
        return {"error": "없는 작업"}
    if job["status"] == "running":
        job["proc"].terminate()
        job["status"] = "stopped"
    return job_view(job)


def job_view(job: Dict[str, Any]) -> Dict[str, Any]:
    """화면에 줄 모양 (프로세스 객체는 빼고)"""
    return {k: v for k, v in job.items() if k != "proc"}


# ======================================================================
# 서버
# ======================================================================
class Handler(BaseHTTPRequestHandler):
    settings: Settings
    market = None  # 거래소 조회는 처음 쓸 때 한 번만 만든다 (마켓 정보 받기가 몇 초 걸린다)

    def _market(self):
        """거래소 조회 객체 (처음 쓸 때 한 번만 만든다)

        MarketData 를 만들 때 마켓 정보를 받아오느라 몇 초 걸린다. 화면을 띄울 때마다
        만들면 느려서, 클래스 변수에 한 번만 만들어 두고 모든 요청이 같이 쓴다.
        """
        if Handler.market is None:
            from kimp.market_data import MarketData
            Handler.market = MarketData(depth=self.settings.orderbook_depth)
        return Handler.market

    def log_message(self, fmt, *args):
        """요청마다 찍히는 기본 접근 로그를 끈다 (브라우저가 초마다 부르면 터미널이 안 보인다)"""
        pass

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        """응답 보내기. Cache-Control: no-store 로 브라우저가 옛 값을 보여주지 않게 한다"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        """JSON 응답 (ensure_ascii=False — 한글을 그대로 보낸다)"""
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802 (http.server 규칙)
        """
        주소별 처리 (이 화면은 GET 만 쓴다)

        마지막에 모든 예외를 받아 **500 + 메시지로 돌려준다.** 그러지 않으면 조회 한 번
        실패에 서버가 응답을 끊어서 화면 전체가 '불러오지 못했습니다' 가 된다.
        실행 명령(/api/run)도 GET 이다 — 혼자 쓰는 로컬 도구라 127.0.0.1 에만 열고,
        받을 수 있는 값을 정해진 목록으로 제한했다 (build_command).
        """
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as fp:
                    self._send(fp.read(), "text/html; charset=utf-8")
            elif url.path == "/api/config":
                self._json(config_summary(self.settings))
            elif url.path == "/api/spreads":
                coins = self._coins(query)
                mode = (query.get("mode") or [MODE_AUTO])[0]
                self._json(spread_summary(self.settings, coins, mode))
            elif url.path == "/api/premium":
                coin = (query.get("coin") or [self.settings.coins[0]])[0].upper()
                hours = float((query.get("hours") or ["24"])[0])
                # fetch=1 이면 기록이 없는 코인도 캔들을 받아서 그린다 (거래소 조회)
                flag = (query.get("fetch") or ["1"])[0]
                md = self._market() if flag not in ("0", "") else None
                self._json(premium_history(self.settings, coin, hours, md=md, force_fetch=(flag == "force")))
            elif url.path == "/api/levels":
                # 목표 순이익에 맞는 관리선 (차트와 같은 데이터로 계산 — 캔들을 새로 받지는 않는다)
                coin = (query.get("coin") or [self.settings.coins[0]])[0].upper()
                hours = float((query.get("hours") or ["24"])[0])
                target = float((query.get("target") or ["1"])[0])
                notional = (query.get("notional") or [""])[0].strip()
                slippage = (query.get("slippage") or [""])[0].strip()   # 숫자 | auto | 빈칸(설정값)
                from kimp.slippage import resolve as resolve_slippage
                slip = resolve_slippage(slippage or None, self.settings.fees.slippage_buffer_percent,
                                        self.settings.live_db_path)
                data = premium_history(self.settings, coin, hours, md=None)
                self._json(level_plan(self.settings, coin, data["series"], target,
                                      notional_usdt=float(notional) if notional else None, hours=hours,
                                      exec_slippage=slip))
            elif url.path == "/api/universe":
                u = universe(self._market())
                self._json({"coins": u["coins"], "volume": u["volume"], "my": list(self.settings.coins),
                            "recorded": recorded_coins(self.settings)})
            elif url.path == "/api/live":
                self._json(live_premium(self._market(), self._coins(query)))
            elif url.path == "/api/screen":
                self._json(screen_latest() or {"error": "아직 없습니다. python -m kimp.screen 을 한 번 돌리세요"})
            elif url.path == "/api/dashboard":
                # 기간 성과 (source: live / dry_run / paper / sim:3, start·end 는 YYYY-MM-DD)
                self._json(dashboard(self.settings,
                                     (query.get("source") or [""])[0],
                                     (query.get("start") or [""])[0],
                                     (query.get("end") or [""])[0]))
            elif url.path == "/api/sims":
                self._json(sim_runs(self.settings))
            elif url.path == "/api/candles":
                self._json(candle_summary(self.settings))
            elif url.path == "/api/ticks":
                # 초 단위 김프 시계열: coin 이 있으면 그 코인의 최근 minutes 분, 없으면 현황
                coin = (query.get("coin") or [""])[0].strip()
                if coin:
                    self._json(tick_history(self.settings, coin, float((query.get("minutes") or ["30"])[0])))
                else:
                    self._json(tick_summary(self.settings))
            elif url.path == "/api/bot":
                self._json(bot_state(self.settings))
            elif url.path == "/api/jobs":
                self._json([job_view(j) for j in sorted(JOBS.values(), key=lambda x: -x["id"])][:10])
            elif url.path == "/api/job":
                job = JOBS.get(int((query.get("id") or ["0"])[0]))
                self._json(job_view(job) if job else {"error": "없는 작업"})
            elif url.path == "/api/run":
                kind = (query.get("kind") or [""])[0]
                self._json(start_job(kind, {k: v[0] for k, v in query.items()}))
            elif url.path == "/api/stop":
                self._json(stop_job(int((query.get("id") or ["0"])[0])))
            elif url.path == "/api/sim":
                self._json(sim_trades(self.settings, int((query.get("id") or ["0"])[0])))
            else:
                self._json({"error": "없는 주소"}, 404)
        except Exception as exc:  # 화면이 죽지 않게 메시지로 돌려준다
            logger.exception("요청 처리 실패: %s", self.path)
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _coins(self, query) -> List[str]:
        """
        볼 코인 정하기

            (없음)        .env 의 COINS
            coins=all     양쪽 거래소에 다 있는 코인 전부 (거래대금 큰 순)
            top=40        거래대금 상위 40개
            coins=CAP,SEI 적은 것만
        """
        raw = (query.get("coins") or [""])[0].strip()
        top = int((query.get("top") or ["0"])[0] or 0)
        if raw.lower() == "all" or top:
            coins = universe(self._market())["coins"]
            return coins[:top] if top else coins
        picked = [c.strip().upper() for c in raw.split(",") if c.strip()]
        return picked or list(self.settings.coins)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    화면 서버 띄우기 (python -m kimp.webui)

    127.0.0.1 에만 연다 — 다른 PC 에서는 접근할 수 없다. 혼자 쓰는 도구라 로그인이 없고,
    실행 명령(/api/run)까지 열려 있으므로 **외부에 열면 안 된다.**
    봇을 돌리는 중에 같이 띄워도 된다 (DB 를 읽기만 한다).
    """
    parser = argparse.ArgumentParser(description="작은 로컬 화면 (설정·김프 차트·추천 코인·시뮬레이션 결과)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"포트 (기본 {DEFAULT_PORT})")
    parser.add_argument("--no-open", action="store_true", help="브라우저를 자동으로 열지 않음")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s - %(message)s")
    Handler.settings = load_settings()
    url = f"http://{HOST}:{args.port}"
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"화면 열기: {url}   (Ctrl+C 로 종료)")
    print(f"  모드 {Handler.settings.mode} · 코인 {', '.join(Handler.settings.coins)}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

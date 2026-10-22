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
    시뮬레이션  python -m kimp.simulate 가 history DB 에 저장한 실행 기록과 거래 내역

주의: 127.0.0.1 에만 바인딩한다 (다른 PC 에서 접근 불가). API 키는 화면에 보내지 않는다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from kimp.config import Settings, load_settings
from kimp.db import PremiumSnapshot, make_session_factory
from kimp.history import BYBIT, UPBIT, UPBIT_USDT, HistoryStore, SimRun, SimTrade, make_history_session_factory
from kimp.spread import resolve_spreads, snapshot_spreads

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
        "db": {"paper": settings.db_path, "history": settings.history_db_path},
        "warnings": list(settings.config_warnings),
    }


def spread_summary(settings: Settings, coins: Sequence[str]) -> List[Dict[str, Any]]:
    """코인별 호가 간격과 그 출처 (봇 기록이 쌓였는지 한눈에 보기)"""
    records = snapshot_spreads(settings.db_path, coins)
    spreads, sources = resolve_spreads(coins, settings.db_path, settings.sim_default_spread_percent)
    return [
        {
            "coin": c,
            "percent": spreads.get(c),
            "source": sources.get(c, "-"),
            "records": records.get(c, (None, 0))[1],
        }
        for c in coins
    ]


def premium_history(settings: Settings, coin: str, hours: float = 24.0,
                    now_ms: Optional[int] = None) -> Dict[str, Any]:
    """
    코인 하나의 봇 김프 기록 (차트용)

    ① 봇이 쌓은 premium_snapshots 가 있으면 그걸 쓴다 (진입/청산 김프까지 있어서 제일 좋다)
    ② 없으면 과거 1분봉(history DB)으로 계산한다 (가운데 가격 기준만)

    Returns:
        {"coin": "CAP", "source": "봇 기록"|"1분봉", "points": [[시각ms, 김프%, 진입김프|None, 청산김프|None], ...]}
    """
    import time as _time

    now_ms = now_ms if now_ms is not None else int(_time.time() * 1000)
    start_ms = now_ms - int(hours * 3_600_000)
    points: List[List[Optional[float]]] = []
    if os.path.exists(settings.db_path):
        with make_session_factory(settings.db_path)() as session:
            rows = session.execute(
                select(PremiumSnapshot.created_at, PremiumSnapshot.mid_premium,
                       PremiumSnapshot.entry_premium_top, PremiumSnapshot.exit_premium_top)
                .where(PremiumSnapshot.coin == coin).order_by(PremiumSnapshot.created_at)
            ).all()
        for created, mid, entry, exit_ in rows:
            ts = int(created.replace(tzinfo=created.tzinfo or _utc()).timestamp() * 1000) if created else None
            if ts is None or ts < start_ms:
                continue
            points.append([ts, mid, entry, exit_])
    if points:
        return {"coin": coin, "source": "봇 기록", "points": points}

    store = HistoryStore(make_history_session_factory(settings.history_db_path))
    upbit = store.load_closes(UPBIT, coin, start_ms, now_ms)
    bybit = store.load_closes(BYBIT, coin, start_ms, now_ms)
    usdt = store.load_closes(UPBIT_USDT, "USDT", start_ms, now_ms)
    for ts in sorted(upbit.keys() & bybit.keys() & usdt.keys()):
        b, u = bybit[ts], usdt[ts]
        if b > 0 and u > 0:
            points.append([ts, (upbit[ts] / (b * u) - 1) * 100, None, None])
    return {"coin": coin, "source": "1분봉" if points else "없음", "points": points}


def _utc():
    from datetime import timezone
    return timezone.utc


def live_premium(md, coins: Sequence[str]) -> List[Dict[str, Any]]:
    """지금 호가창으로 계산한 김프 (진입/청산/가운데) + 호가 간격·펀딩비율"""
    quotes = md.fetch_quotes(list(coins))
    out = []
    for c in coins:
        q = quotes.get(c)
        if not q:
            out.append({"coin": c, "error": "시세 없음"})
            continue
        funding = md.fetch_funding(c)
        coin_spread = None
        if q.entry_premium_top is not None and q.exit_premium_top is not None:
            coin_spread = q.entry_premium_top - q.exit_premium_top - (q.usdt_spread_percent or 0.0)
        out.append({
            "coin": c,
            "mid": q.mid_premium,
            "entry": q.entry_premium_top,
            "exit": q.exit_premium_top,
            "usdt_spread": q.usdt_spread_percent,
            "coin_spread": coin_spread,
            "upbit_krw": q.upbit.mid,
            "bybit_usdt": q.bybit.mid,
            "usdt_krw": q.usdt_krw,
            "tether_premium": q.tether_premium,
            "site_premium": q.site_premium,
            "funding_8h": (funding.rate * 100) if funding else None,
        })
    return out


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


# ======================================================================
# 서버
# ======================================================================
class Handler(BaseHTTPRequestHandler):
    settings: Settings
    market = None  # 거래소 조회는 처음 쓸 때 한 번만 만든다 (마켓 정보 받기가 몇 초 걸린다)

    def _market(self):
        if Handler.market is None:
            from kimp.market_data import MarketData
            Handler.market = MarketData(depth=self.settings.orderbook_depth)
        return Handler.market

    def log_message(self, fmt, *args):  # 요청마다 찍히는 기본 로그는 끈다
        pass

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802 (http.server 규칙)
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
                self._json(spread_summary(self.settings, coins))
            elif url.path == "/api/premium":
                coin = (query.get("coin") or [self.settings.coins[0]])[0].upper()
                hours = float((query.get("hours") or ["24"])[0])
                self._json(premium_history(self.settings, coin, hours))
            elif url.path == "/api/live":
                self._json(live_premium(self._market(), self._coins(query)))
            elif url.path == "/api/screen":
                self._json(screen_latest() or {"error": "아직 없습니다. python -m kimp.screen 을 한 번 돌리세요"})
            elif url.path == "/api/sims":
                self._json(sim_runs(self.settings))
            elif url.path == "/api/sim":
                self._json(sim_trades(self.settings, int((query.get("id") or ["0"])[0])))
            else:
                self._json({"error": "없는 주소"}, 404)
        except Exception as exc:  # 화면이 죽지 않게 메시지로 돌려준다
            logger.exception("요청 처리 실패: %s", self.path)
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _coins(self, query) -> List[str]:
        raw = (query.get("coins") or [""])[0]
        return [c.strip().upper() for c in raw.split(",") if c.strip()] or list(self.settings.coins)


def main(argv: Optional[Sequence[str]] = None) -> int:
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

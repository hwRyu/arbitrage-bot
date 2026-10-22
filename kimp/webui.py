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
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from kimp.config import Settings, load_settings
from kimp.db import PaperBalance, Position, PositionStatus, PremiumSnapshot, make_session_factory
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
    코인 하나의 기록 (차트용). 봇이 남긴 지표를 다 담아서 화면에서 골라 그릴 수 있게 한다.

    ① 봇 기록(premium_snapshots)이 있으면 그걸 쓴다 (지표가 다 있다)
    ② 없으면 과거 1분봉(history DB)으로 김프만 계산한다 (가운데 가격 기준)

    series 의 각 칸 (없으면 null)
        mid     가운데 가격 기준 봇 김프 (%)          ← 보통 시세
        entry   맨 앞 호가로 지금 진입하면 나오는 김프 (%)
        exit    맨 앞 호가로 지금 청산하면 나오는 김프 (%)
        spread  코인 호가 간격 (%) = entry − exit − usdt_spread
        usdt_spread  업비트 USDT/KRW 호가 간격 (%)
        funding 바이비트 펀딩비율 (%, 8시간 기준으로 환산). + 면 숏인 우리가 받는다
        tether  테더 프리미엄 (%)
        site    김프 사이트식 김프 (%, 달러 환율 기준)
        usdt_krw 업비트 USDT 가격 (원)
        upbit_krw / bybit_usdt  각 거래소 코인 가격
    """
    import time as _time

    now_ms = now_ms if now_ms is not None else int(_time.time() * 1000)
    start_ms = now_ms - int(hours * 3_600_000)
    series: List[Dict[str, Any]] = []
    if os.path.exists(settings.db_path):
        with make_session_factory(settings.db_path)() as session:
            rows = session.execute(
                select(PremiumSnapshot).where(PremiumSnapshot.coin == coin)
                .order_by(PremiumSnapshot.created_at)
            ).scalars().all()
            for r in rows:
                created = r.created_at
                if created is None:
                    continue
                ts = int(created.replace(tzinfo=created.tzinfo or _utc()).timestamp() * 1000)
                if ts < start_ms:
                    continue
                usdt_spread = r.usdt_spread_percent
                spread = None
                if r.entry_premium_top is not None and r.exit_premium_top is not None:
                    spread = r.entry_premium_top - r.exit_premium_top - (usdt_spread or 0.0)
                series.append({
                    "ts": ts, "mid": r.mid_premium, "entry": r.entry_premium_top, "exit": r.exit_premium_top,
                    "spread": spread, "usdt_spread": usdt_spread,
                    "funding": _funding_8h_percent(r.funding_rate, r.funding_interval_hours),
                    "tether": r.tether_premium, "site": r.site_premium,
                    "usdt_krw": r.usdt_krw, "upbit_krw": r.upbit_mid_krw, "bybit_usdt": r.bybit_mid_usdt,
                })
    if series:
        return {"coin": coin, "source": "봇 기록", "series": series}

    store = HistoryStore(make_history_session_factory(settings.history_db_path))
    upbit = store.load_closes(UPBIT, coin, start_ms, now_ms)
    bybit = store.load_closes(BYBIT, coin, start_ms, now_ms)
    usdt = store.load_closes(UPBIT_USDT, "USDT", start_ms, now_ms)
    funding = dict(store.load_funding(coin, start_ms, now_ms))
    for ts in sorted(upbit.keys() & bybit.keys() & usdt.keys()):
        b, u = bybit[ts], usdt[ts]
        if b > 0 and u > 0:
            series.append({
                "ts": ts, "mid": (upbit[ts] / (b * u) - 1) * 100, "entry": None, "exit": None,
                "spread": None, "usdt_spread": None,
                "funding": (funding[ts] * 100) if ts in funding else None,
                "tether": None, "site": None,
                "usdt_krw": u, "upbit_krw": upbit[ts], "bybit_usdt": b,
            })
    return {"coin": coin, "source": "1분봉" if series else "없음", "series": series}


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
                 "refine_top": "--refine-top", "refine_timeframe": "--refine-timeframe", "target": "--target"}
SIM_PARAMS = {"start": "--from", "end": "--to", "coins": "--coins"}
SIM_ENV = {"seed": "PAPER_INITIAL_TOTAL_USDT", "size": "POSITION_SIZE_USDT",
           "max_positions": "MAX_OPEN_POSITIONS", "target": "TARGET_NET_PROFIT_PERCENT"}
# 페이퍼 봇(main.py)은 옵션이 없고 설정(환경 변수)으로만 조절한다.
# 화면에서 켜는 봇은 '호가 기록을 쌓는 용도' 라 스캔을 느리게 하고 펀딩비 전략은 끈다 (요청 제한 여유).
BOT_ENV = {"coins": "COINS", "scan": "SCAN_INTERVAL_SEC", "seed": "PAPER_INITIAL_TOTAL_USDT",
           "size": "POSITION_SIZE_USDT", "max_positions": "MAX_OPEN_POSITIONS",
           "target": "TARGET_NET_PROFIT_PERCENT"}


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
        allowed, env_map = SCREEN_PARAMS, {}
    elif kind == "sim":
        cmd = [sys.executable, "-m", "kimp.simulate"]
        allowed, env_map = SIM_PARAMS, SIM_ENV
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
    if kind == "bot":
        env["PAPER_TRADING"] = "true"  # 화면에서는 실거래를 켤 수 없다
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
        # 진행률 표시는 캐리지리턴으로 한 줄을 덮어쓴다 → 마지막 조각만 남긴다
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

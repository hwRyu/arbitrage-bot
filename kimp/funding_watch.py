"""
펀딩 지형 기록 — 바이비트 USDT 무기한 전 코인의 펀딩비율을 하루 한 번 요약해 남긴다 (2026-11-01)

왜 있나
    펀딩비 캐리 전략(docs/carry.md)은 기본 펀딩 0.01%/8h(연 11%)에서는 자본 대비 연 5~8% 라 보류했다. 강세장에서 펀딩이
    전반적으로 0.03~0.1%/8h 로 오르면 다시 꺼낼 가치가 생긴다. 그 '지형 변화' 를 놓치지 않으려고 중앙값·상위 코인을 매일 적어 둔다.
    (2026-11-01 기준: 822개 중 플러스 411개, 중앙값 연 0.2%, 연 20% 이상 24개 — 전부 소형주)

어떻게
    snapshot(ex) 가 ccxt fetch_funding_rates() 한 번으로 요약(dict)을 만들고, record() 가 market.db 의 funding_landscape 에 한 줄
    적는다. 수집기(kimp.collect --ws)가 하루 한 번 부른다. 수동: python -m kimp.funding_watch [--show]
    연환산 = 1회 비율 × (1440 ÷ 정산 주기 분) × 365. 숏이 받는 쪽이 +.
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from kimp.db import FundingLandscape, make_session_factory

logger = logging.getLogger(__name__)

TOP_N = 20
RECORD_INTERVAL_SEC = 24 * 3600.0


@dataclass
class FundingSnapshot:
    ts_ms: int
    coins: int
    positive: int
    median_apr: float          # 연환산 중앙값 (%)
    p90_apr: float             # 상위 10% 경계 (%)
    over20: int                # 연 20% 이상 코인 수
    top: List[Dict[str, Any]]  # [{coin, apr, rate, interval_min}] 상위 TOP_N

    def line(self) -> str:
        head = ", ".join(f"{t['coin']} {t['apr']:.0f}%" for t in self.top[:5])
        return (f"펀딩 지형: {self.coins}개 중 플러스 {self.positive}개 · 연환산 중앙값 {self.median_apr:+.1f}% · "
                f"상위10% 경계 {self.p90_apr:.1f}% · 연 20% 이상 {self.over20}개 | 상위: {head}")


def snapshot(ex, now_ms: Optional[int] = None) -> FundingSnapshot:
    """바이비트 ccxt 객체(마켓 로드됨)로 요약 하나. 실패는 예외로 올린다 (부르는 쪽이 넘긴다)"""
    rates = ex.fetch_funding_rates()
    rows = []
    for sym, r in rates.items():
        if not sym.endswith("/USDT:USDT") or r.get("fundingRate") is None:
            continue
        info = (ex.market(sym).get("info") or {})
        interval = int(info.get("fundingInterval") or 480)
        rate = float(r["fundingRate"]) * 100
        apr = rate * (1440 / interval) * 365
        rows.append({"coin": sym.split("/")[0], "apr": apr, "rate": rate, "interval_min": interval})
    if not rows:
        raise RuntimeError("펀딩비 응답이 비었습니다")
    aprs = sorted(x["apr"] for x in rows)
    rows.sort(key=lambda x: -x["apr"])
    return FundingSnapshot(
        ts_ms=now_ms or int(time.time() * 1000), coins=len(rows), positive=sum(1 for a in aprs if a > 0),
        median_apr=statistics.median(aprs), p90_apr=aprs[int(len(aprs) * 0.9)], over20=sum(1 for a in aprs if a >= 20),
        top=rows[:TOP_N],
    )


def record(db_path: str, snap: FundingSnapshot) -> None:
    sf = make_session_factory(db_path)
    with sf() as s:
        s.add(FundingLandscape(ts_ms=snap.ts_ms, coins=snap.coins, positive=snap.positive, median_apr=snap.median_apr,
                               p90_apr=snap.p90_apr, over20=snap.over20, top_json=json.dumps(snap.top, ensure_ascii=False)))
        s.commit()


def last_record_ms(db_path: str) -> Optional[int]:
    sf = make_session_factory(db_path)
    with sf() as s:
        return s.scalar(select(FundingLandscape.ts_ms).order_by(FundingLandscape.ts_ms.desc()).limit(1))


class DailyFundingWatch:
    """수집기 루프에서 매 상태 주기마다 due() 를 물어 하루 한 번만 기록한다 (마지막 기록은 DB 에서 읽어 재시작해도 중복 없음)"""

    def __init__(self, db_path: str, ex_factory, interval_sec: float = RECORD_INTERVAL_SEC):
        self.db_path = db_path
        self._ex_factory = ex_factory          # 호출 때 바이비트 ccxt 객체를 만든다 (수집기 소켓 객체와 분리)
        self.interval_sec = interval_sec
        self._last_ms: Optional[int] = None

    def maybe_record(self, now_ms: Optional[int] = None) -> Optional[FundingSnapshot]:
        now_ms = now_ms or int(time.time() * 1000)
        if self._last_ms is None:
            self._last_ms = last_record_ms(self.db_path) or 0
        if now_ms - self._last_ms < self.interval_sec * 1000:
            return None
        try:
            snap = snapshot(self._ex_factory(), now_ms)
            record(self.db_path, snap)
        except Exception as exc:   # noqa: BLE001 — 기록 실패로 수집기를 멈추지 않는다
            logger.warning("[펀딩 지형] 기록 실패 (다음 상태 주기에 다시): %s", exc)
            return None
        self._last_ms = now_ms
        logger.info("[펀딩 지형] %s", snap.line())
        return snap


def show(db_path: str, limit: int = 30) -> None:
    sf = make_session_factory(db_path)
    with sf() as s:
        rows = list(s.scalars(select(FundingLandscape).order_by(FundingLandscape.ts_ms.desc()).limit(limit)))
    if not rows:
        print("기록 없음")
        return
    print(f"{'시각(UTC)':16} {'코인':>5} {'플러스':>5} {'중앙값%':>8} {'상위10%':>8} {'≥20%':>5}  상위 5")
    for r in reversed(rows):
        top = json.loads(r.top_json)[:5]
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r.ts_ms / 1000))
        print(f"{when:16} {r.coins:5d} {r.positive:5d} {r.median_apr:8.1f} {r.p90_apr:8.1f} {r.over20:5d}  "
              + ", ".join(f"{t['coin']} {t['apr']:.0f}%" for t in top))


def main(argv=None) -> int:
    from kimp.config import load_settings
    parser = argparse.ArgumentParser(description="바이비트 펀딩 지형 기록 / 조회")
    parser.add_argument("--show", action="store_true", help="기록된 추이 보기 (기본: 지금 한 번 받아 기록)")
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.show:
        show(settings.snapshot_db_path)
        return 0
    import ccxt
    ex = ccxt.bybit({"enableRateLimit": True, "timeout": 10_000, "options": {"defaultType": "swap"}})
    ex.load_markets()
    snap = snapshot(ex)
    record(settings.snapshot_db_path, snap)
    print(snap.line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

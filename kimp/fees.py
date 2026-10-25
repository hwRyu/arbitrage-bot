"""
코인별 바이비트 수수료율 — 거래소에서 받아 설정(FeeSettings)에 채운다

왜: 바이비트는 일부 소형 코인 무기한에 **2배 수수료**를 매긴다. 2026-10-25 계정 조회:
    CAP taker 0.110% / MINA·BTC 0.055%.  실거래 체결 20건 중 18건이 0.11% 였다 (IRYS·CAP·BLEND).
    설정 하나(BYBIT_TAKER_FEE_RATE)로는 맞출 수 없고, 낮게 잡으면 익절 판단이 거래당 그 차이만큼 낙관적이 된다
    (CAP #11: 판단 +0.354% → 확정 -0.05%, 그중 0.06%p 가 이 수수료 차이).

어디서 쓰나
    main(실거래·페이퍼)·screen·simulate 가 코인 목록이 정해진 직후 with_bybit_fee_rates() 로 설정을 바꿔 쓴다.
    그 뒤 FeeSettings.bybit_taker_rate(coin) / round_trip_trade_fee_percent(coin) 이 코인별 값을 돌려준다.
    키가 없거나 조회가 실패한 코인은 BYBIT_TAKER_FEE_RATE 그대로.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Dict, Iterable, Optional

from kimp.config import Settings
from kimp.market_data import bybit_symbol

logger = logging.getLogger(__name__)


def fetch_bybit_taker_rates(bybit, coins: Iterable[str]) -> Dict[str, float]:
    """
    코인별 taker 수수료율 (소수, 예: 0.0011). 조회가 실패한 코인은 빠진다

    Args:
        bybit: 인증된 ccxt 바이비트 객체 (fee-rate 조회는 비공개 API)
    """
    rates: Dict[str, float] = {}
    failed = []
    for coin in coins:
        try:
            info = bybit.fetch_trading_fee(bybit_symbol(coin))
            taker = info.get("taker")
            if taker is not None:
                rates[coin] = float(taker)
        except Exception as exc:  # 네트워크·권한·상장 안 된 코인 등 — 그 코인만 기본값으로
            failed.append(f"{coin}({type(exc).__name__})")
    if failed:
        logger.warning("바이비트 수수료 조회 실패 → 기본값 사용: %s", ", ".join(failed))
    return rates


def with_bybit_fee_rates(settings: Settings, coins: Iterable[str], bybit=None) -> Settings:
    """
    코인별 바이비트 수수료를 받아 넣은 새 Settings (frozen 이라 복사본). 키가 없으면 그대로 돌려준다

    Args:
        bybit: 이미 만든 인증 ccxt 객체가 있으면 재사용 (없으면 설정의 키로 만든다)
    """
    coins = list(coins)
    if not coins:
        return settings
    if bybit is None:
        if not (settings.bybit_api_key and settings.bybit_api_secret):
            logger.info("바이비트 API 키가 없어 코인별 수수료를 못 받습니다 → BYBIT_TAKER_FEE_RATE %.3f%% 로 계산",
                        settings.fees.bybit_taker_fee_rate * 100)
            return settings
        import ccxt
        bybit = ccxt.bybit({"apiKey": settings.bybit_api_key, "secret": settings.bybit_api_secret,
                            "enableRateLimit": True, "options": {"defaultType": "swap"}})
        bybit.load_markets()
    fetched = fetch_bybit_taker_rates(bybit, [c for c in coins if c not in settings.fees.bybit_taker_fee_rates])
    manual = dict(settings.fees.bybit_taker_fee_rates)          # .env 코인별 값은 조회값보다 우선
    rates = {**fetched, **manual}
    if not rates:
        return settings
    default = settings.fees.bybit_taker_fee_rate
    text = " · ".join(f"{c} {r * 100:.3f}%{'(설정)' if c in manual else ''}{' ★' if r > default * 1.5 else ''}"
                      for c, r in sorted(rates.items()) if c in coins)
    logger.info("바이비트 수수료 (코인별 taker): %s%s", text,
                "   (★ = 기본의 2배 — 바이비트가 소형 코인에 더 매기는 것)" if "★" in text else "")
    return replace(settings, fees=replace(settings.fees, bybit_taker_fee_rates=rates))

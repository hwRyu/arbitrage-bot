"""
실거래 전 점검 (읽기만 함 — 주문·출금은 절대 안 냄)

실행:
    python3 -m kimp.live_check

확인하는 것 (항목마다 OK / 확인 필요 / 실패)
------------------------------------------------------------------
1) 설정: 모드, 키 4개, LIVE_CONFIRM, 안전 한도
2) 업비트: 키로 잔고 조회 (IP 허용 확인), 키 만료일, USDT 출금 허용 주소에 바이비트 입금 주소가 있는지
3) 바이비트: 키로 잔고 조회, 키 권한(선물 거래·출금), 통합계좌인지, 코인별 포지션 모드·마진 모드·레버리지
4) 송금 네트워크: 양쪽이 지원하는 USDT 네트워크 목록, 설정한 네트워크로 양쪽 입금 주소 조회
   (같은 체인도 이름이 다르다: 트론 = 업비트 TRX / 바이비트 TRC20)
5) 포지션 크기가 업비트 최소 주문(5,000원)보다 큰지
6) 텔레그램 테스트 메시지
"""
from __future__ import annotations

import logging
import sys
from typing import List, Tuple

from kimp.config import load_settings
from kimp.market_data import bybit_symbol

# 점검 결과 세 가지. 실패가 하나라도 있으면 main() 이 1 을 돌려준다 (실거래를 켜지 말라는 뜻)
#   OK    그대로 두면 된다
#   WARN  실거래에 꼭 필요하진 않거나, 조회가 안 돼서 확인하지 못했다
#   FAIL  이대로 실거래를 켜면 주문·송금이 막힌다
# 같은 항목이 자동 송금(AUTO_TRANSFER_ENABLED)을 쓸 때만 FAIL 이 되는 경우가 많다 —
# 송금을 안 쓰면 출금 주소·권한이 없어도 매매 자체는 돈다.
OK, WARN, FAIL = "OK", "확인 필요", "실패"


def _recent_krw_deposit_hold(upbit):
    """
    24시간 안에 들어온 원화 입금이 있으면 (금액, 남은 시간) 을 돌려준다

    업비트 출금지연제: 원화를 입금하면 그 금액 상당의 코인을 24시간 동안 다른 곳으로 못 보낸다
    (사고팔기는 된다). 그 시간에는 자동 송금이 거절되므로 미리 알려준다.
    """
    from datetime import datetime, timedelta, timezone

    try:
        deposits = upbit.private_get_deposits({"currency": "KRW", "limit": 5})
    except Exception:
        return None
    now = datetime.now(timezone.utc)
    for d in deposits:
        when = d.get("done_at") or d.get("created_at")
        if not when:
            continue
        left = (datetime.fromisoformat(when.replace("Z", "+00:00")) + timedelta(hours=24)) - now
        if left.total_seconds() > 0:
            return float(d.get("amount") or 0), left.total_seconds() / 3600
    return None


def _short(exc: Exception) -> str:
    """오류를 한 줄로 줄이기 (거래소 오류는 여러 줄에 길게 와서 표가 깨진다)"""
    return str(exc).replace("\n", " ")[:160]


def run_checks(settings, upbit=None, bybit=None) -> List[Tuple[str, str, str]]:
    """
    점검을 전부 돌려서 결과 줄들을 돌려준다 (읽기만 — 주문·출금은 한 번도 안 낸다)

    한 항목이 실패해도 **멈추지 않고 끝까지 간다.** 한 번 돌려서 고칠 것을 다 보여주는 게
    목적이기 때문이다 (하나씩 고치고 다시 돌리면 거래소 조회만 반복된다).
    예외는 두 곳뿐이다 — 키가 아예 없거나 거래소 연결이 안 되면 그 뒤를 볼 수 없어 바로 끝낸다.

    Args:
        settings: 점검할 설정 (load_settings() 결과)
        upbit, bybit: ccxt 객체. 안 주면 main.make_exchanges 로 만든다 (테스트에서 가짜를 넣으려고 받는다)
    Returns:
        [(상태, 항목 이름, 설명)] — 화면 출력 순서 그대로
    """
    rows: List[Tuple[str, str, str]] = []

    def add(status, item, detail=""):
        """점검 결과 한 줄 추가 (항목 이름은 그대로 화면에 찍힌다)"""
        rows.append((status, item, detail))

    # 1) 설정 — 거래소를 부르기 전에 .env 만 보고 알 수 있는 것들
    add(OK if not settings.paper_trading else WARN, "모드",
        {"paper": "페이퍼 (PAPER_TRADING=true) — 실거래하려면 false", "dry_run": "드라이런 (주문 안 냄)",
         "live": "실거래 (실제 주문)"}[settings.mode])
    keys = [settings.upbit_access_key, settings.upbit_secret_key, settings.bybit_api_key, settings.bybit_api_secret]
    add(OK if all(keys) else FAIL, "API 키 4개", f"{sum(bool(k) for k in keys)}/4 개 있음")
    if settings.mode == "live":
        add(OK if settings.live_confirm == "YES" else FAIL, "LIVE_CONFIRM", "YES 여야 실제 주문")
    add(OK, "안전 한도", f"포지션 최대 {settings.max_position_usdt or '-'} USDT, 하루 손실 한도 "
        f"{settings.daily_loss_limit_usdt or '-'} USDT, 연속 실패 {settings.max_consecutive_failures}번")
    if not all(keys):
        return rows  # 키가 없으면 아래 조회가 전부 실패한다 → 그 전에 끝낸다

    if upbit is None or bybit is None:
        # main 에서 만드는 이유: 요청 제한·타임아웃 설정이 실거래와 같아야 한다
        from main import make_exchanges
        try:
            upbit, bybit = make_exchanges(settings)
        except Exception as exc:
            add(FAIL, "거래소 연결", _short(exc))
            return rows
    up_net, by_net = settings.upbit_transfer_network, settings.bybit_transfer_network

    # 2) 업비트 — 잔고 조회가 되면 키와 IP 허용이 둘 다 맞다는 뜻이다
    try:
        bal = upbit.fetch_balance()
        add(OK, "업비트 잔고 조회 (키·IP)", f"USDT {float((bal.get('USDT') or {}).get('free') or 0):,.2f}, "
            f"원화 {float((bal.get('KRW') or {}).get('free') or 0):,.0f}")
    except Exception as exc:
        hint = " → 업비트 Open API 관리에서 이 서버 IP 를 허용하세요" if "ip" in str(exc).lower() else ""
        add(FAIL, "업비트 잔고 조회 (키·IP)", _short(exc) + hint)
    try:
        keys_info = upbit.private_get_api_keys()
        exp = ", ".join(str(k.get("expire_at", "?"))[:10] for k in keys_info)
        add(OK, "업비트 키 만료일", exp or "-")
    except Exception as exc:
        add(WARN, "업비트 키 만료일", _short(exc))

    # 4) 송금 네트워크
    # 같은 체인인데 이름이 다르다 (트론 = 업비트 'TRX' / 바이비트 'TRC20').
    # 이름을 잘못 적으면 다른 체인으로 보내서 **돈이 사라진다.** 그래서 양쪽 목록을 받아 대조한다.
    by_nets, up_nets = set(), set()
    try:
        cur = (bybit.fetch_currencies() or {}).get("USDT") or {}
        by_nets = {str(n).upper() for n, info in (cur.get("networks") or {}).items()
                   if (info or {}).get("deposit") is not False}
    except Exception as exc:
        add(WARN, "바이비트 USDT 네트워크", _short(exc))
    try:
        for w in upbit.private_get_status_wallet() or []:
            if w.get("currency") == "USDT" and w.get("wallet_state") in ("working", "withdraw_only", "deposit_only"):
                up_nets.add(str(w.get("net_type")).upper())
    except Exception as exc:
        add(WARN, "업비트 USDT 네트워크", _short(exc))
    bad = FAIL if settings.auto_transfer_enabled else WARN
    if by_nets:
        add(OK if by_net in by_nets else bad, f"바이비트 송금 네트워크 BYBIT_TRANSFER_NETWORK={by_net}",
            ("지원함" if by_net in by_nets else "지원 안 함") + f" (바이비트 목록: {', '.join(sorted(by_nets))})")
    if up_nets:
        add(OK if up_net in up_nets else bad, f"업비트 송금 네트워크 UPBIT_TRANSFER_NETWORK={up_net}",
            ("지원함" if up_net in up_nets else "지원 안 함") + f" (업비트 목록: {', '.join(sorted(up_nets))})")
    add(OK, "송금 네트워크 짝", f"업비트 {up_net} ↔ 바이비트 {by_net} — 같은 체인인지 꼭 확인 (다르면 돈을 잃음)")

    # 입금 주소: 설정에 적어 뒀으면 그 값, 없으면 거래소에서 조회한다.
    # 업비트는 '출금 허용 주소' 에 미리 등록해 둔 주소로만 보낼 수 있어서, 등록 여부까지 확인한다.
    bybit_addr = settings.bybit_deposit_address
    try:
        if not bybit_addr:
            bybit_addr = bybit.fetch_deposit_address("USDT", {"network": by_net})["address"]
        add(OK, f"바이비트 USDT 입금 주소 ({by_net})", bybit_addr)
    except Exception as exc:
        add(FAIL if settings.auto_transfer_enabled else WARN, f"바이비트 USDT 입금 주소 ({by_net})", _short(exc))
    try:
        upbit_addr = settings.upbit_deposit_address or upbit.fetch_deposit_address(
            "USDT", {"network": up_net})["address"]
        add(OK, f"업비트 USDT 입금 주소 ({up_net})",
            f"{upbit_addr} → 바이비트 출금 화이트리스트에 등록 (자동 송금 쓸 때)")
    except Exception as exc:
        add(FAIL if settings.auto_transfer_enabled else WARN, f"업비트 USDT 입금 주소 ({up_net})", _short(exc))
    try:
        allowed = upbit.private_get_withdraws_coin_addresses()
        usdt = [a for a in allowed if a.get("currency") == "USDT"]
        found = any(a.get("withdraw_address") == bybit_addr for a in usdt)
        status = OK if found else (FAIL if settings.auto_transfer_enabled else WARN)
        add(status, "업비트 출금 허용 주소에 바이비트 주소",
            "등록됨" if found else f"없음 (USDT 허용 주소 {len(usdt)}개) → 업비트 Open API 관리 > 출금 허용 주소에 등록")
    except Exception as exc:
        add(WARN, "업비트 출금 허용 주소", _short(exc))

    # 지금 실제로 출금이 되는 상태인가 (출금지연제·한도)
    # 2026-10-22: 원화 200만원을 입금한 날 846 USDT 송금이 거절됐다.
    #   "출금지연제 적용에 따라 24시간 이내 입금하신 원화 입금액 상당의 코인은 출금 가능 수량에서 제외"
    # 모르고 실거래를 켜면 송금만 반복 실패한다 → 켜기 전에 알려준다.
    try:
        limit = (upbit.private_get_withdraws_chance(
            {"currency": "USDT", "net_type": up_net}).get("withdraw_limit") or {})
        hold = _recent_krw_deposit_hold(upbit)
        detail = (f"출금 가능 수량 {float(limit.get('remaining_daily') or 0):,.4f} USDT · "
                  f"1회 한도 {float(limit.get('onetime') or 0):,.0f} · 출금 가능 {limit.get('can_withdraw')}")
        if hold:
            amount, hours_left = hold
            detail += (f" | 오늘 원화 {amount:,.0f}원 입금 → 그 금액 상당은 약 {hours_left:.1f}시간 뒤 "
                       f"출금 가능 (업비트 출금지연제)")
        status = OK if (limit.get("can_withdraw") and not hold) else (
            FAIL if settings.auto_transfer_enabled else WARN)
        add(status, "업비트 출금 가능 상태", detail)
    except Exception as exc:
        add(WARN, "업비트 출금 가능 상태", _short(exc))

    # 3) 바이비트 — 키 권한(선물 거래·출금), 통합계좌 여부, 코인별 마진·레버리지
    try:
        bal = bybit.fetch_balance()
        add(OK, "바이비트 잔고 조회 (키·IP)", f"USDT {float((bal.get('USDT') or {}).get('free') or 0):,.2f}")
    except Exception as exc:
        add(FAIL, "바이비트 잔고 조회 (키·IP)", _short(exc))
    try:
        info = bybit.private_get_v5_user_query_api().get("result", {})
        perms = info.get("permissions", {})
        trade = bool(perms.get("ContractTrade") or perms.get("Derivatives"))
        wd = "Withdraw" in (perms.get("Wallet") or [])
        add(OK if trade else FAIL, "바이비트 키 권한: 선물 거래", str(perms.get("ContractTrade") or perms.get("Derivatives")))
        add(OK if wd or not settings.auto_transfer_enabled else FAIL, "바이비트 키 권한: 출금",
            "있음" if wd else "없음 (자동 송금 쓰려면 필요)")
        add(OK if int(info.get("uta") or 0) else WARN, "바이비트 통합계좌(UTA)",
            "예" if int(info.get("uta") or 0) else "아님 — 통합계좌 기준으로 만들었음")
        ips = info.get("ips") or []
        add(OK if ips and ips != ["*"] else WARN, "바이비트 키 IP 제한", ", ".join(ips) or "없음 (IP 제한 권장)")
    except Exception as exc:
        add(WARN, "바이비트 키 권한", _short(exc))
    for coin in settings.coins:
        try:
            ps = bybit.fetch_positions([bybit_symbol(coin)])
            p = ps[0] if ps else {}
            mode = p.get("marginMode")
            lev = p.get("leverage")
            good = mode in (None, "isolated") and (lev is None or float(lev) == float(settings.bybit_leverage))
            add(OK if good else WARN, f"바이비트 {coin} 마진·레버리지",
                f"{mode or '포지션 없음'}, {lev}배 → 실거래 시작 때 격리·{settings.bybit_leverage:g}배로 자동 설정")
        except Exception as exc:
            add(WARN, f"바이비트 {coin} 포지션 정보", _short(exc))

    # 5) 최소 주문 금액 vs 포지션 크기
    # 포지션이 최소 주문의 2배는 돼야 한다. 청산할 때 자투리가 남으면 그 자투리를 못 팔아서
    # 한쪽만 남는 사고가 난다 (× 1400 은 원화 환산용 어림값).
    size = settings.max_position_usdt or settings.position_size_usdt
    for coin in list(settings.coins) + ["USDT"]:
        try:
            mk = (upbit.private_get_orders_chance({"market": f"KRW-{coin}"}) or {}).get("market") or {}
            min_krw = max(float((mk.get(side) or {}).get("min_total") or 0) for side in ("bid", "ask"))
            ok = not size or size * 1400 > min_krw * 2
            add(OK if ok else WARN, f"업비트 {coin} 최소 주문", f"{min_krw:,.0f}원"
                + (f" (포지션 {size:,.0f} USDT ≈ {size * 1400:,.0f}원)" if size else ""))
        except Exception as exc:
            add(WARN, f"업비트 {coin} 최소 주문", f"조회 실패 → 설정값 {settings.upbit_min_order_krw:,.0f}원 사용 ({_short(exc)})")
    for coin in settings.coins:
        try:
            m = bybit.market(bybit_symbol(coin))
            lot = (m.get("info") or {}).get("lotSizeFilter") or {}
            add(OK, f"바이비트 {coin} 최소 주문", f"{lot.get('minOrderQty')}개, {lot.get('minNotionalValue')} USDT, "
                f"시장가 최대 {lot.get('maxMktOrderQty')}개")
        except Exception as exc:
            add(WARN, f"바이비트 {coin} 최소 주문", _short(exc))

    # 6) 텔레그램 — 실제로 한 통 보내 본다. 실거래 중 유일한 알림 수단이라 여기서 확인해야 한다
    if settings.telegram_bot_token and settings.telegram_chat_id:
        from kimp.notify import TelegramNotifier
        ok = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id).send_now("live_check 테스트 메시지")
        add(OK if ok else FAIL, "텔레그램", "테스트 메시지 보냄" if ok else "보내기 실패 (토큰·chat_id 확인)")
    else:
        add(WARN, "텔레그램", "설정 없음 (실거래에서는 꼭)")
    return rows


def main() -> int:
    """
    명령줄 실행 (python -m kimp.live_check)

    Returns:
        0 전부 통과 / 1 실패 항목 있음 / 2 설정 자체가 잘못됨
        실거래를 켜기 전에 0 이 나오는지 확인하는 용도다.
    """
    logging.basicConfig(level=logging.WARNING)
    try:
        settings = load_settings()
    except ValueError as exc:
        print(exc)
        return 2
    rows = run_checks(settings)
    width = max(len(r[1]) for r in rows)
    print("\n실거래 점검 (읽기만 함)\n")
    for status, item, detail in rows:
        mark = {"OK": "✅", "확인 필요": "⚠️ ", "실패": "❌"}[status]
        print(f"{mark} {item:<{width}}  {detail}")
    fails = sum(1 for r in rows if r[0] == FAIL)
    print(f"\n실패 {fails}개, 확인 필요 {sum(1 for r in rows if r[0] == WARN)}개")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

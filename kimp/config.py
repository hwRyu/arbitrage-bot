"""
봇 설정 로딩

모든 설정은 .env 파일(또는 환경 변수)에서 읽어 불변(frozen) dataclass 로 만든다.
old 버전은 클래스 속성 + DB 프록시 바꿔치기 방식이라 추적이 어려웠으므로,
여기서는 `load_settings()` 한 번으로 Settings 객체를 만들고 필요한 곳에 주입한다.

전략의 기준은 docs/concept.md 이다.

단위 규칙 (코드 전체 공통)
- 수수료율(rate)  : 소수 (0.0005 = 0.05%)
- 퍼센트(percent) : 퍼센트 값 그대로 (1.0 = 1%)
- 금액            : 변수명 끝에 통화를 붙인다 (_krw, _usdt). 손익과 자본은 USDT 기준
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Dict, List, Mapping, Optional, Tuple

from dotenv import load_dotenv


def _env_str(key: str, default: str) -> str:
    """환경 변수 문자열 조회 (앞뒤 공백 제거)"""
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float) -> float:
    """환경 변수 실수 조회. 값이 비어 있으면 기본값 사용"""
    raw = os.getenv(key, "").strip()
    return float(raw) if raw else default


def _env_int(key: str, default: int) -> int:
    """환경 변수 정수 조회. 값이 비어 있으면 기본값 사용"""
    raw = os.getenv(key, "").strip()
    return int(raw) if raw else default


def _env_optional_float(key: str) -> Optional[float]:
    """환경 변수 실수 조회. 비어 있으면 None (값을 안 넣으면 '자동' 인 설정에 씀)"""
    raw = os.getenv(key, "").strip()
    return float(raw) if raw else None


def _env_bool(key: str, default: bool) -> bool:
    """환경 변수 불리언 조회 ('true', '1', 'yes' 를 참으로 간주)"""
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "y")


def _env_list(key: str, default: str) -> List[str]:
    """쉼표로 구분된 코인 목록 조회. 공백 제거 + 대문자화 + 중복 제거(순서 유지)"""
    raw = os.getenv(key, default)
    seen: List[str] = []
    for item in raw.split(","):
        coin = item.strip().upper()
        if coin and coin not in seen:
            seen.append(coin)
    return seen


@dataclass(frozen=True)
class FeeSettings:
    """
    손익 계산에 들어가는 비용

    봇의 진입/청산 판단은 "이 비용을 모두 뺀 순이익" 기준이다.
    거래소 등급, 이벤트, 출금 네트워크에 따라 실제 값이 다르니 본인 계정 기준으로 확인해서 넣을 것.
    """

    # 업비트 원화 마켓 수수료율 (코인 매매와 USDT/KRW 매매 둘 다, 한 번 거래할 때마다)
    # 업비트 기본 0.05%. 살 때는 주문 금액과 별도로 원화에서 더 빠지고, 팔 때는 받는 원화에서 빠진다.
    upbit_trade_fee_rate: float = 0.0005

    # 바이비트 USDT 무기한 Taker 수수료율 (시장가 주문). 숏 열 때 1번, 닫을 때 1번. 기본 0.055%
    # 바이비트는 **코인마다** 다르다 — 일부 소형 코인 무기한은 2배(0.11%): 2026-10-25 조회 CAP 0.11% / MINA·BTC 0.055%.
    # 그래서 시작할 때 코인별 요율을 거래소에서 받아 아래 bybit_taker_fee_rates 에 채운다 (kimp/fees.py).
    # 이 값은 조회가 안 된 코인의 기본값. 판단 순손익은 이 요율로 닫기 수수료를 추정하므로 낮게 잡으면 낙관적이 된다
    bybit_taker_fee_rate: float = 0.00055

    # (선택) 사이클(진입 → 청산 1번)마다 손익에서 미리 떼어두는 전송비 (USDT). 기본 0
    # 송금은 매 거래마다 하는 게 아니라 '잔고가 모자라거나 비율이 많이 깨졌을 때만' 한다.
    # 그 실제 송금 비용은 TRANSFER_FEE_UPBIT_TO_BYBIT_USDT / TRANSFER_FEE_BYBIT_TO_UPBIT_USDT 로
    # 송금할 때 한 번씩 뗀다.
    # 그래도 거래 손익에 미리 나눠서 반영하고 싶을 때만 쓴다. 예) 다섯 사이클에 한 번 송금 → 0.2
    transfer_fee_usdt_per_cycle: float = 0.0

    # 체결 밀림 가정 (%) — 추천·시뮬레이션·김프 차트 관리선·'필요 김프' 표시가 거래당 비용으로 쓰는 값.
    # 청산 때 판단한 호가보다 불리하게 체결되는 몫 (2026-10-24 실거래: 청산 다리 0.12~1.58%, 진입은 거의 0).
    # 실거래 익절 판단은 이 값을 쓰지 않고 유동 여유(TP_EXEC_BUFFER, 그 순간 호가판)로 계산한다.
    # 세 도구는 --exec-slippage 숫자|auto 로 실행마다 바꿀 수 있다 (auto = 실거래 체결 기록 중앙값, kimp/slippage.py)
    slippage_buffer_percent: float = 0.05

    # ---- 코인별 요율 (맨 뒤: 위 항목들은 위치 인자로 만드는 코드가 있어 순서를 지킨다) ----
    # 업비트: .env <코인>_UPBIT_TRADE_FEE_RATE. 코인 매매에만 쓰고 USDT/KRW 매매는 공통값
    upbit_trade_fee_rates: Mapping[str, float] = field(default_factory=dict, compare=False, hash=False)
    # 바이비트 taker: .env <코인>_BYBIT_TAKER_FEE_RATE > 거래소 조회값(kimp/fees.py) > 기본값. 비교·해시에서는 뺀다
    bybit_taker_fee_rates: Mapping[str, float] = field(default_factory=dict, compare=False, hash=False)

    def upbit_trade_rate(self, coin: Optional[str] = None) -> float:
        """이 코인의 업비트 요율 (코인별 설정, 없으면 공통값). USDT/KRW 매매는 coin 없이 불러 공통값"""
        if coin:
            return self.upbit_trade_fee_rates.get(coin, self.upbit_trade_fee_rate)
        return self.upbit_trade_fee_rate

    def bybit_taker_rate(self, coin: Optional[str] = None) -> float:
        """이 코인의 바이비트 taker 요율 (코인별 설정 또는 거래소 조회값, 없으면 기본값)"""
        if coin:
            return self.bybit_taker_fee_rates.get(coin, self.bybit_taker_fee_rate)
        return self.bybit_taker_fee_rate

    def round_trip_trade_fee_percent(self, coin: Optional[str] = None) -> float:
        """
        들어갔다 나오는 한 바퀴의 거래 수수료 합계 (%). coin 을 주면 그 코인의 바이비트 요율로

        업비트 USDT 매도·매수 2번(공통 요율) + 코인 매수·매도 2번(코인별 요율) + 바이비트 숏 열기·닫기 2번(코인별 요율)
        → 4 × 0.05% + 2 × 0.055% = 0.31%  (바이비트 0.11% 코인이면 0.42%)
        """
        return (2 * self.upbit_trade_fee_rate + 2 * self.upbit_trade_rate(coin) + 2 * self.bybit_taker_rate(coin)) * 100


# 코인별로 덮어쓸 수 있는 설정 항목: (CoinParams 필드 이름, .env 키 뒷부분)
# .env 에 '<코인>_<뒷부분>' 으로 넣으면 그 코인만 그 값을 쓴다. 예) XRP_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.3
COIN_OVERRIDE_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("entry_premium_threshold_percent", "ENTRY_PREMIUM_THRESHOLD_PERCENT"),
    ("target_net_profit_percent", "TARGET_NET_PROFIT_PERCENT"),
    ("stop_loss_percent", "STOP_LOSS_PERCENT"),
    ("position_size_usdt", "POSITION_SIZE_USDT"),
)


@dataclass(frozen=True)
class CoinParams:
    """
    코인 하나에 실제로 쓰는 진입/청산/크기 설정

    공통값에서 시작해서, .env 에 코인별 값이 있으면 그 값으로 바꾼다.
    각 항목 뜻은 Settings 의 같은 이름 항목 설명을 볼 것.

    Attributes:
        position_size_usdt: None 이면 자동 크기 (자본 × 사용률 ÷ 최대 포지션 수)
        overridden: 코인별 값으로 바꾼 항목 이름들 (시작 로그 표시용)
    """

    entry_premium_threshold_percent: float
    target_net_profit_percent: float
    stop_loss_percent: float
    position_size_usdt: Optional[float]
    overridden: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    """
    봇 전체 설정

    진입 기준 / 목표 / 손절 / 포지션 크기는 여기 값이 '공통값' 이고,
    코인에 실제로 쓰는 값은 반드시 `params_for(coin)` 으로 꺼내 쓴다. (코인별 덮어쓰기 반영)
    """

    # ------------------------------------------------------------------
    # 실행 모드
    # ------------------------------------------------------------------
    # true = 페이퍼(가짜 주문, 가짜 잔고). false = 실거래 계열 (아래 LIVE_* 확인, docs/live.md)
    paper_trading: bool = True

    # ------------------------------------------------------------------
    # 실거래 (PAPER_TRADING=false 일 때만, docs/live.md)
    # ------------------------------------------------------------------
    # 거래소 API 키. 로그에는 절대 안 찍는다
    upbit_access_key: str = ""
    upbit_secret_key: str = ""
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    # 실수로 실거래가 켜지는 것 방지: 정확히 YES 라고 적어야 실제 주문을 낸다
    live_confirm: str = ""
    # true = 실제 키·잔고·호가로 돌지만 주문은 안 냄 (페이퍼 장부로 '이 주문을 냈을 것' 만 기록). 첫 단계
    live_dry_run: bool = True
    # 포지션 하나 최대 금액 (USDT). 자동 크기나 POSITION_SIZE_USDT 보다 크면 이 값으로 줄인다
    #   모든 모드에 똑같이 적용. 비우면 제한 없음, 단 실거래·드라이런에서 비우면 50
    max_position_usdt: Optional[float] = None
    # 하루(한국 시간 0시 기준) 확정 손실이 이 USDT 를 넘으면 그날은 신규 진입 중단
    #   모든 모드에 똑같이 적용. 비우면 제한 없음, 단 실거래·드라이런에서 비우면 10
    daily_loss_limit_usdt: Optional[float] = None
    # 주문 실패가 연속 이 횟수면 신규 진입 중단 (가진 포지션은 계속 관리)
    max_consecutive_failures: int = 3
    # 이 파일이 있으면 신규 진입 중단 (비상정지). 지우면 재개. 텔레그램 /stop, /resume 도 같음
    stop_file: str = "data/STOP"
    # 거래소 실제 잔고·포지션과 DB 를 비교하는 간격 (초). 다르면 신규 진입 중단 + 긴급 알림
    reconcile_interval_sec: float = 600.0
    # 실거래 / 드라이런 DB (페이퍼 DB_PATH 와 따로)
    live_db_path: str = "data/live.db"
    dry_run_db_path: str = "data/dryrun.db"

    # ------------------------------------------------------------------
    # 자동 송금 (실거래, docs/live.md). 기본 꺼짐 = 알림만
    # ------------------------------------------------------------------
    auto_transfer_enabled: bool = False
    # 보낼 네트워크. 같은 체인도 거래소마다 이름이 다르다 → 따로 적는다 (live_check 가 양쪽 목록을 보여줌)
    #   예) 트론: 업비트 'TRX' / 바이비트 'TRC20',  이더리움: 업비트 'ETH' / 바이비트 'ERC20'
    upbit_transfer_network: str = "TRX"
    bybit_transfer_network: str = "TRC20"
    # 이보다 적게는 안 보냄 (USDT). 수수료만 버리지 않게
    transfer_min_usdt: float = 10.0
    # 하루에 보낼 수 있는 최대 (USDT, 양방향 합)
    transfer_max_usdt_per_day: float = 1_000.0
    # 송금 요청 뒤 이 시간(분) 안에 입금이 안 보이면 긴급 알림
    transfer_alert_minutes: float = 60.0
    # 받는 주소 (비우면 거래소에서 내 입금 주소를 조회). 업비트·바이비트 출금 허용 주소에 미리 등록해야 함
    bybit_deposit_address: str = ""
    bybit_deposit_tag: str = ""
    upbit_deposit_address: str = ""
    upbit_deposit_tag: str = ""

    # ------------------------------------------------------------------
    # 텔레그램 알림 (비우면 알림 없음)
    # ------------------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ------------------------------------------------------------------
    # 대상 코인
    # ------------------------------------------------------------------
    # 업비트 원화 마켓과 바이비트 USDT 무기한에 둘 다 있는 코인만 쓴다. 한쪽에만 있으면 경고 후 뺀다.
    coins: List[str] = field(default_factory=lambda: ["XRP", "SOL", "DOGE"])

    # ------------------------------------------------------------------
    # 진입 / 청산 (공통값 — 코인별 덮어쓰기 가능)
    # ------------------------------------------------------------------
    # 진입 기준 김프 (%)
    # 김프 = 업비트 USDT 가격 기준 '코인 몫' 김프 (테더 프리미엄과 환율이 빠진 값, docs/concept.md 3번)
    # 실제로 체결될 가격으로 계산한 진입 김프가 이 값 이하일 때 진입한다.
    # 예) -0.5 → 업비트 코인이 USDT 기준으로 바이비트보다 0.5% 이상 쌀 때 진입
    # 주의: 김프 사이트 숫자(테더 포함)와 다르다. 대략 사이트 김프 ≈ 이 값 + 테더 프리미엄
    entry_premium_threshold_percent: float = -0.5

    # 목표 순이익률 (%). 지금 청산하면 모든 비용을 빼고 이만큼 남을 때 청산한다.
    target_net_profit_percent: float = 1.0

    # ★ 익절 실행 여유 (kimp/exec_buffer.py): 익절 조건을 '순손익 ≥ 목표 + 여유' 로. 고정값 없이 그 순간 호가판으로만 계산
    #   여유 = 업비트 항 + 바이비트 항. 거래소 항 = "우리 앞에 우리만 한 주문이 (배수−1)개 먼저 체결된 뒤" 우리 평균가가
    #   지금 평균가보다 얼마나 나쁜가 (%). 배수 2 = 앞에 하나, 1 = 아무도 없음(여유 0). 호가 한 칸·상수 같은 고정값 없음
    #   BLEND #10 2026-10-24: 판단 +0.3% 익절 → 0.7초 사이 매수벽 3칸 빠져 −0.25% 체결. 끄면 여유 0 = 예전 동작
    tp_exec_buffer_enabled: bool = True
    tp_stress_qty_multiplier: float = 2.0

    # 손절 순손익률 (%, 음수). 이만큼 손해면 정리한다. 0 이면 손절 안 함.
    stop_loss_percent: float = -5.0

    # ------------------------------------------------------------------
    # 포지션 크기
    # ------------------------------------------------------------------
    # 포지션 하나의 크기 (바이비트 숏 금액, USDT). 업비트에서는 같은 개수의 USDT 를 팔아 코인을 산다.
    # 비워두면 자동: 업비트 쪽 자본 × CAPITAL_USAGE_PERCENT ÷ MAX_OPEN_POSITIONS (자본이 늘면 같이 커짐)
    position_size_usdt: Optional[float] = None

    # 자동 크기일 때 업비트 쪽 자본 중 몇 % 를 포지션에 쓸지. 나머지는 수수료와 오차 여유분.
    capital_usage_percent: float = 90.0

    # 동시에 들고 있을 수 있는 포지션 수 (코인당 최대 1개)
    max_open_positions: int = 3

    # 청산 후 같은 코인을 다시 진입하기 전 기다리는 시간 (초)
    reentry_cooldown_sec: float = 300.0

    # 맨 앞 호가보다 평균 체결가가 이 % 넘게 밀리면 진입하지 않는다 (호가가 얇은 코인 거르기)
    max_slippage_percent: float = 0.3

    # ★ 업비트 코인 주문 방식 (USDT 사고팔기·바이비트 숏은 늘 시장가)
    #   market = 시장가. 판단 뒤 1초 사이 호가가 한 칸 비껴도 그 가격에 잡힌다
    #            (CAP #12 2026-10-25: 140원 판단 → 141원 체결, 한 칸 0.71% = 목표 0.2% 의 3.5배)
    #   fok    = 판단한 가격에 지정가, '전량 즉시 체결 아니면 취소' (Fill or Kill). 판단가보다 나쁘게는 절대 안 잡힌다.
    #            진입 놓침 → 판 USDT 를 되삼 (비용은 USDT 왕복 수수료 약 0.1%), 익절 놓침 → 포지션 그대로, 잠시 뒤 다시
    #            손절·강제청산 가드·수동 청산·되돌리기는 fok 여도 시장가 (빠져나오는 게 우선)
    upbit_coin_order_type: str = "market"

    # ------------------------------------------------------------------
    # 바이비트 레버리지 / 자본 비율 / 리밸런싱 (docs/concept.md 7번)
    # ------------------------------------------------------------------
    # 바이비트 숏 레버리지 (배). 증거금 = 숏 금액 ÷ 레버리지
    # 1배: 강제청산은 가격이 약 2배가 돼야. 3배: 약 +33% 오르면 강제청산
    bybit_leverage: float = 1.0

    # 바이비트에 증거금 말고 더 둘 여유분 (증거금의 %).
    # **강제청산을 막아주는 돈이 아니다** — 격리 마진이라 계좌에 남은 USDT 는 그 포지션의
    # 증거금이 되지 않고, 봇도 증거금을 자동으로 넣지 않는다 (경고만 보낸다).
    # 실제 쓰임: 바이비트 수수료·펀딩비 지출 / 여러 포지션 동시 운용 / 경고가 왔을 때
    # 사람이 손으로 증거금을 넣을 준비금.
    bybit_margin_buffer_percent: float = 30.0

    # 바이비트 유지증거금률 (소수). 강제청산 가격 계산에 쓴다. 코인/포지션 크기마다 다를 수 있음
    bybit_mmr: float = 0.005

    # 강제청산 가격까지 남은 상승폭이 이 % 아래면 '증거금 추가' 긴급 경고 (사람에게 알림만)
    liq_warn_percent: float = 15.0

    # ★ 강제청산 가드 — 남은 상승폭이 이 % 안으로 오면 (= 청산가의 90% 지점) 봇이 스스로 판단한다.
    #   거래소가 주는 강제청산가 기준 (봇 계산값은 거래소보다 늦게 잡힌다: IRYS 12.8% 경고 때 실제는 6.3%).
    #     목표까지 ≤ liq_guard_near_target_percent (%p)  → 증거금 추가 (목표 1% 면 절반 넘게 와 있으면 들고 간다)
    #     순손익 ≤ liq_guard_hold_below_percent (%)       → 증거금 추가 (역프가 깊다 = 지금 정리하면 손실 확정.
    #                                                      급등 때 역프는 수 분 만에 돌아온다 — IRYS 2026-10-24)
    #     그 외                                            → 정리 (사유 LIQ_GUARD). 1배 헷지라 청산당하는 것과
    #                                                      돈 차이는 유지증거금 수준이지만, 시점을 우리가 고른다
    #   재원과 한도: 자본 비율을 정할 때 바이비트에 증거금 외로 더 두기로 한 '여유분'(bybit_margin_buffer_percent)
    #   = 전체 자본 × 여유분% ÷ (레버리지 + 1 + 여유분%), 1배 30% 면 자본의 13%. 모든 포지션이 넣은 합이 이걸 넘지
    #   못한다 → 아무리 넣어도 자리 몫(다음 진입 돈)은 그대로다. 포지션 크기와는 무관하다.
    #   한 번 넣는 양 = 그 포지션의 원래 증거금 × liq_guard_topup_unit_ratio (여유분에 남은 만큼만),
    #   포지션당 최대 liq_guard_topup_max_rounds 회. 1배·배수 1.0 이면 1회에 청산가 2배 → 3배, 2회면 4배.
    #   배수 0.5 면 1회에 2배 → 2.5배, 2회면 3배 — 여유분을 더 많은 포지션에 나눠 쓸 수 있다.
    #   여유분을 다 썼으면 정리 (회수 0 이면 추가 없이 정리만).
    #   왜 교차 마진이 아닌가: 격리는 코인 하나의 손실 상한이 증거금이다. IRYS 가 하루에 21배 갈 때 교차였다면
    #   20 USDT 포지션이 계좌 400 USDT 를 끌어다 쓰며 다른 포지션까지 위험해진다. 헷지에서 청산은 거의 본전이다.
    liq_guard_percent: float = 10.0
    liq_guard_near_target_percent: float = 0.5
    liq_guard_hold_below_percent: float = -2.0
    liq_guard_topup_max_rounds: int = 2
    liq_guard_topup_unit_ratio: float = 1.0

    # 양쪽 자본 비율이 목표에서 이 %p 넘게 벗어나면 리밸런싱 알림 (청산 직후 확인)
    rebalance_threshold_percent: float = 10.0

    # 진입 뒤 양쪽 금액(업비트 코인 값 vs 바이비트 숏 금액) 차이가 이 %보다 크면 큰 쪽을 줄여서 맞춘다
    #   예) 숏 100 USDT, 코인 80 USDT 어치만 체결 → 숏 20 USDT 만큼 닫기 (비용 약 0.02 USDT)
    #   1% 이하 차이는 그대로 둔다 (50 USDT 포지션이면 0.5 USDT. 가격 10% 움직여도 0.05 USDT)
    hedge_tolerance_percent: float = 1.0

    # 업비트 원화 마켓 최소 주문 금액 (원). 실거래·드라이런은 시작할 때 업비트에 마켓별 값을 물어보고
    # (주문 가능 정보 API), 못 받으면 이 값을 쓴다. 페이퍼·시뮬레이션은 이 값.
    # 바이비트 최소 주문(개수·금액)은 거래소 마켓 정보에서 읽는다 (예: XRP 0.1개, 5 USDT)
    upbit_min_order_krw: float = 5_000.0

    # ★ 펀딩비 안전장치 (concept.md 11번 1순위)
    # 우리는 바이비트 숏이라 펀딩비율이 +면 받고 −면 낸다. 김프 회복을 며칠 기다리는 전략이라
    # 내는 쪽에 오래 머물면 김프가 그대로여도 손실만 쌓인다.
    #   예) 100 USDT 포지션, −0.0646%/8h → 하루 −0.194 USDT → 5일이면 목표(1%)를 전부 먹는다
    #
    # ① 진입 차단: 8시간 기준 펀딩비율이 이 값보다 나쁘면 진입하지 않는다 (None 이면 보지 않음)
    #    −0.05 = "8시간에 0.05% 넘게 내야 하면 들어가지 않는다"
    entry_min_funding_8h_percent: Optional[float] = -0.05
    # ② 누적 한도: 이 포지션에서 펀딩비로 낸 금액이 포지션 금액의 이 % 를 넘으면 정리한다 (0 = 무제한)
    #
    # 왜 0.3 인가: 펀딩비는 순손익에 들어가므로, 낸 만큼 **필요한 청산 김프가 1:1 로 올라간다.**
    #   진입 -0.55%, 목표 1% 일 때 필요 청산 김프
    #     펀딩 0      → +0.885%
    #     펀딩 -0.3%  → +1.185%
    #     펀딩 -1.0%  → +1.885%
    #   CAP·LIT 처럼 김프가 ±0.7% 범위에서 움직이는 코인은 펀딩비가 0.3%만 쌓여도 목표 도달이
    #   사실상 불가능해진다. 더 기다리면 자리만 묶이고 손실이 커진다 (악순환).
    #   그래서 '목표만큼(1%)' 이 아니라 '필요 김프가 범위를 벗어나는 지점(0.3%)' 으로 잡는다.
    max_funding_cost_percent: float = 0.3

    # ★ 호가 간격 안전장치: 진입 직전 '지금 왕복 호가' 가 이 % 를 넘으면 들어가지 않는다 (0 = 보지 않음)
    #   왕복 호가 = 맨 앞 호가로 본 진입 김프 − 청산 김프 (업비트 코인 + 바이비트 + USDT/KRW 호가 간격의 합).
    #   이게 목표 순이익보다 크면 그 거래는 애초에 이익이 날 수 없다.
    #   왜 필요한가: 코인 가격이 호가 단위 경계(10·100·1,000원)를 넘으면 한 칸이 갑자기 10배 넓어진다.
    #   IRYS 2026-10-24: 34원(한 칸 0.03%) 때 골랐는데 100원을 넘자 한 칸 1원(≈1%) → 0.2% 목표로 다섯 번
    #   들어가 −4.05 USDT. 스크리너는 그때그때 가격을 모르므로 엔진이 봐야 한다.
    entry_max_spread_percent: float = 0.8

    # ★ 한 번만 돌리기 (첫 실거래 점검용)
    # true 면 포지션을 **딱 하나만** 잡고, 그 포지션이 청산되면 봇이 스스로 종료한다.
    # 처음 실제 주문을 내볼 때 "한 바퀴가 제대로 도는지"만 확인하려고 쓴다.
    #   진입 → 보유 → 청산 → 요약 출력 → 종료
    # 이미 열린 포지션이 있으면 그걸 끝까지 관리하고 종료한다 (새로 잡지 않는다).
    one_shot: bool = False
    # 한 번만 돌리기에서 이 시간을 넘겨도 청산 조건에 닿지 않으면 시장가로 정리하고 끝낸다.
    # 0 이면 무제한(목표에 닿을 때까지 기다림). 예) 6 = 6시간 뒤에도 안 되면 정리
    one_shot_max_hours: float = 0.0

    # 리밸런싱 송금 한 번에 드는 출금 수수료 (USDT). 송금할 때만 한 번 든다 (거래마다 X)
    # 보내는 거래소가 떼기 때문에 방향마다 다르다. (2026-10-22 확인: 실제 송금 + 바이비트 API 조회)
    #   업비트 → 바이비트: 업비트 USDT(TRX) 출금 무료      → 0
    #   바이비트 → 업비트: 바이비트 USDT(TRC20) 출금 수수료 → 1.3
    # 거래소가 언제든 바꿀 수 있다. `python -m kimp.live_check` 가 지금 값과 설정을 비교해서 알려준다.
    transfer_fee_upbit_to_bybit_usdt: float = 0.0
    transfer_fee_bybit_to_upbit_usdt: float = 1.3

    # 페이퍼/시뮬레이션: 리밸런싱이 필요할 때 '사용자가 송금했다' 고 치고 자동으로 USDT 를 옮긴다
    # (페이퍼 잔고는 사용자가 실제로 보낼 수 없으니까). 옮길 때마다 위 수수료를 뗀다.
    #   송금하는 때: ① 청산 후 비율이 목표에서 REBALANCE_THRESHOLD_PERCENT 넘게 벗어났을 때
    #               ② 진입하려는데 한쪽 USDT 가 모자랄 때
    paper_auto_rebalance: bool = True

    # ------------------------------------------------------------------
    # 비용
    # ------------------------------------------------------------------
    fees: FeeSettings = field(default_factory=FeeSettings)

    # ------------------------------------------------------------------
    # 주기
    # ------------------------------------------------------------------
    # 스캔 간격 (초). REST 시세면 이 간격마다 시세를 받아 판단한다 (바이비트는 코인 수만큼 요청이 나가니 코인이
    # 많으면 늘릴 것). 소켓 시세(QUOTE_SOURCE=ws)면 판단은 호가가 바뀔 때마다 하고, 이 간격은 '잔고·포지션을
    # 거래소에서 새로 받는 주기' 겸 '호가가 안 바뀌어도 한 번은 도는 최대 대기' 가 된다
    scan_interval_sec: float = 10.0

    # 김프 기록 저장 간격 (초). 코인마다 이 간격에 1건
    snapshot_interval_sec: float = 60.0

    # 펀딩비 정보 갱신 간격 (초). 보유 포지션 코인만
    funding_refresh_sec: float = 60.0

    # 호가창 몇 줄까지 받을지 (REST)
    orderbook_depth: int = 30

    # 시세를 어떻게 받나 (kimp/ws_feed.py)
    #   rest = 스캔마다 요청해서 받는다 (기본). 한 번에 ~190 ms, 10초에 한 번
    #   ws   = 웹소켓으로 밀어 받는다 (바이비트 초당 ~8회, 업비트 1~2회, 70~80 ms 뒤 도착). 판단 순간 손에 최신 판이
    #          있어 요청 시간이 0 이고, SCAN_INTERVAL_SEC 를 줄이면 기회를 1초 아래로 본다. 소켓이 끊기거나(예외)
    #          그 거래소에서 quote_ws_max_age_ms 동안 아무것도 안 오면 그 코인들은 그 스캔만 REST 로 → 소켓이 죽어도
    #          봇은 선다. 주문은 늘 REST
    quote_source: str = "rest"
    quote_ws_max_age_ms: int = 5000

    # ------------------------------------------------------------------
    # 달러 환율 (표시용. 사이트식 김프와 테더 프리미엄 보여주기, kimp/fx.py)
    # ------------------------------------------------------------------
    fx_refresh_sec: float = 60.0
    # 값을 넣으면 조회 없이 이 값으로 고정 (예: 1325)
    fixed_usd_krw: Optional[float] = None

    # ------------------------------------------------------------------
    # 페이퍼 트레이딩 시작 자본 (처음 실행할 때 1번만. 목표 비율대로 업비트/바이비트에 나눠 넣음)
    # ------------------------------------------------------------------
    paper_initial_total_usdt: float = 10_000.0

    # ------------------------------------------------------------------
    # 저장소 / 로그
    # ------------------------------------------------------------------
    db_path: str = "data/kimp.db"
    log_level: str = "INFO"
    log_dir: str = "logs"

    # ------------------------------------------------------------------
    # 시뮬레이션 (python3 -m kimp.simulate)
    # ------------------------------------------------------------------
    # 과거 캔들 / 환율 / 펀딩비 이력과 시뮬레이션 결과 DB (실시간 봇 DB 와 따로)
    history_db_path: str = "data/history.db"

    # 시세 수집기(python -m kimp.collect)가 김프·호가 기록을 쌓는 DB.
    # 매매 기록(kimp.db / live.db)과 **따로** 둔다 — 수집은 전략과 무관한 '시장 데이터' 이기 때문이다.
    # 스크리너·시뮬레이션은 이 파일을 먼저 보고, 없으면 봇 DB의 기록을 본다.
    snapshot_db_path: str = "data/market.db"

    # 과거엔 호가창이 없어서 코인 호가 간격(업비트 코인 + 바이비트, %)을 가정한다.
    # 실시간 김프 기록에 측정값이 있으면 먼저 쓰고, 없으면 이 값. 호가 한 칸보다는 좁게 잡지 않는다.
    # 코인별로는 .env 에 <코인>_SIM_SPREAD_PERCENT
    sim_default_spread_percent: float = 0.1

    # ------------------------------------------------------------------
    # 펀딩비 수익 전략 (kimp/carry.py, 김프 전략과 따로 돈다. 기준: docs/carry.md)
    #   바이비트 현물 매수 + 바이비트 무기한 같은 개수 숏 → 가격 변동은 상쇄, 펀딩비(+)를 숏이 받는다
    # ------------------------------------------------------------------
    carry_enabled: bool = False  # 김프 전략과 별개로 주문을 낸다 → 켤 때만 명시적으로 켠다
    # 대상 코인. 비워두면 자동: 바이비트 현물+무기한 둘 다 있고 거래대금이 충분한 코인 중 펀딩비 높은 순
    carry_coins: List[str] = field(default_factory=list)
    # 이 전략용 페이퍼 자본 (USDT, 처음 1번). 김프 전략 자본과 섞이지 않는다. 전부 바이비트에 있다고 본다
    carry_capital_usdt: float = 1_000.0
    # 숏 레버리지 (기본 1배, 김프 전략 BYBIT_LEVERAGE 와 같은 기본값). 포지션 금액 N 마다 현물 N + 증거금 N÷레버리지
    #   1배 → 2N (강제청산 가격 약 2배) / 2배 → 1.5N (약 +49.5%) / 3배 → 1.33N (약 +32.8%)
    #   레버리지를 올리면 같은 자본으로 더 크게 잡지만 강제청산이 가까워진다
    carry_leverage: float = 1.0
    carry_max_positions: int = 3
    # 포지션 하나 금액 (USDT). 비우면 자동 = 자본 × 사용률 ÷ 최대 개수 ÷ (1 + 1/레버리지)
    #   예) 1배: 1,000 × 90% ÷ 3 ÷ 2 = 150 USDT / 2배: ÷ 1.5 = 200 USDT
    carry_position_size_usdt: Optional[float] = None
    # 진입: 최근 72시간 평균과 최근 24시간 평균이 둘 다 '하루 0.03%' 이상 + 지금 펀딩비율이 + 일 때
    carry_entry_min_daily_percent: float = 0.03
    carry_entry_lookback_hours: float = 72.0
    # 청산: 최근 24시간 평균이 '하루 0%' 아래로 떨어지면 (= 펀딩비를 내기 시작)
    carry_exit_below_daily_percent: float = 0.0
    carry_exit_lookback_hours: float = 24.0
    # 진입 때 선물 매수1호가 ÷ 현물 매도1호가 − 1 이 이 값(%)보다 낮으면 진입 안 함 (들어가자마자 손해가 큼)
    carry_min_entry_basis_percent: float = -0.1
    # 바이비트 현물 수수료율 (Taker 0.1%). 선물은 BYBIT_TAKER_FEE_RATE
    carry_spot_fee_rate: float = 0.001
    # 강제청산 미리 피하기: 강제청산 가격까지 남은 상승폭이 이 %보다 작으면 양쪽을 같이 정리 (0 = 안 함)
    #   현물과 숏을 같은 순간에 정리하면 가격 손익은 상쇄되고 수수료만 든다.
    #   강제청산까지 가면 숏 증거금을 통째로 잃고, 현물은 그 뒤 가격에 팔게 된다.
    #   예) 2배 → 강제청산 약 +49.5%. 15% 면 가격이 약 +30% 올랐을 때 정리
    carry_liq_guard_percent: float = 15.0
    # 청산한 코인은 이 시간 동안 다시 안 들어감 (나왔다 바로 들어가며 수수료만 내는 것 방지)
    carry_reentry_cooldown_hours: float = 24.0
    # 자동 선택 때 바이비트 무기한 24시간 거래대금 최소 (USDT)
    carry_min_volume_usdt: float = 5_000_000.0
    # 자동 선택 때 펀딩비 기록을 받아볼 후보 수 (지금 펀딩비율 높은 순)
    carry_universe_max: int = 40
    carry_scan_interval_sec: float = 60.0
    # 펀딩비 기록 / 후보 새로 고침 주기 (초). 펀딩비는 1~8시간마다 정산이라 자주 볼 필요 없음
    carry_refresh_sec: float = 600.0

    # ------------------------------------------------------------------
    # 코인별 설정 (load_settings 가 채운다)
    # ------------------------------------------------------------------
    coin_params: Dict[str, CoinParams] = field(default_factory=dict)
    # 설정 읽다가 발견한 경고 (예: COINS 에 없는 코인의 개별 설정)
    config_warnings: Tuple[str, ...] = ()

    def default_params(self) -> CoinParams:
        """공통값으로만 만든 CoinParams"""
        return CoinParams(
            entry_premium_threshold_percent=self.entry_premium_threshold_percent,
            target_net_profit_percent=self.target_net_profit_percent,
            stop_loss_percent=self.stop_loss_percent,
            position_size_usdt=self.position_size_usdt,
        )

    def params_for(self, coin: str) -> CoinParams:
        """코인에 실제로 쓸 설정 (코인별 값이 있으면 그 값, 없으면 공통값)"""
        return self.coin_params.get(coin) or self.default_params()

    def transfer_fee_usdt(self, to_bybit: bool) -> float:
        """
        송금 한 번에 드는 출금 수수료 (USDT). 보내는 거래소가 뗀다.

        예) to_bybit=True  (업비트 → 바이비트): 0 (업비트 USDT TRX 출금 무료)
            to_bybit=False (바이비트 → 업비트): 1.3 (바이비트 TRC20 출금)
        """
        return self.transfer_fee_upbit_to_bybit_usdt if to_bybit else self.transfer_fee_bybit_to_upbit_usdt

    def upbit_target_share(self) -> float:
        """
        전체 자본 중 업비트에 둘 목표 비율 (0~1)

        업비트 비율 = 레버리지 ÷ (레버리지 + 1 + 여유분)
        예) 1배, 여유분 30% → 1 ÷ 2.3 = 43.5% (바이비트 56.5%)
            3배, 여유분 30% → 3 ÷ 4.3 = 69.8% (바이비트 30.2%)

        이유: 숏 금액 N 마다 업비트에 코인 N 어치, 바이비트에 증거금 N÷레버리지 가 필요하다.
        1배면 양쪽에 N 씩이라 반반에 가깝고, 레버리지를 올리면 바이비트에 묶이는 돈이 줄어
        업비트 쪽 비율이 커진다.

        여유분은 그냥 두면 **강제청산을 막아 주지 않는다.** 격리 마진이라 계좌에 남은 USDT 는 그 포지션의
        증거금이 되지 않는다. 바이비트 수수료·펀딩비가 나가는 곳이고, 강제청산 가드(liq_guard_percent)가
        증거금을 더 넣을 때 쓰는 돈이다 — 가드는 이 여유분 안에서만 넣는다.
        """
        lev = self.bybit_leverage
        return lev / (lev + 1 + self.bybit_margin_buffer_percent / 100)

    @property
    def transfer_network_label(self) -> str:
        """기록용 네트워크 이름. 예) 'TRX/TRC20' (업비트/바이비트)"""
        return f"{self.upbit_transfer_network}/{self.bybit_transfer_network}"

    @property
    def mode(self) -> str:
        """실행 모드: 'paper' (가짜) / 'dry_run' (실제 잔고·호가, 주문 안 냄) / 'live' (실제 주문)"""
        if self.paper_trading:
            return "paper"
        return "dry_run" if self.live_dry_run else "live"

    def snapshot_db_paths(self) -> Tuple[str, ...]:
        """
        김프·호가 기록을 찾을 DB 순서 (앞쪽이 우선)

        ① 수집기 DB (data/market.db) — 수집 전용 명령이 쌓는 곳
        ② 지금 모드의 봇 DB — 봇도 돌면서 기록을 남기므로 보조로 본다
        ③ 페이퍼 DB — 모드를 바꿔도 전에 페이퍼로 쌓아둔 기록은 그대로 쓸 수 있다

        시세 기록은 전략과 무관한 '시장 데이터' 라서 모드가 달라도 같이 쓴다. (매매 기록은 섞지 않는다)
        """
        paths = [self.snapshot_db_path, self.active_db_path, self.db_path]
        return tuple(dict.fromkeys(paths))  # 중복 제거, 순서 유지

    @property
    def active_db_path(self) -> str:
        """김프 전략이 쓰는 DB (모드마다 따로라 기록이 섞이지 않는다)"""
        return {"paper": self.db_path, "dry_run": self.dry_run_db_path, "live": self.live_db_path}[self.mode]

    def carry_size_usdt(self) -> float:
        """펀딩비 전략 포지션 하나 금액 (설정값, 없으면 자동). 예) 1배: 1,000 × 90% ÷ 3 ÷ 2 = 150"""
        if self.carry_position_size_usdt:
            return self.carry_position_size_usdt
        return (self.carry_capital_usdt * self.capital_usage_percent / 100 / self.carry_max_positions
                / (1 + 1 / self.carry_leverage))


LIVE_DEFAULT_MAX_POSITION_USDT = 50.0
LIVE_DEFAULT_DAILY_LOSS_LIMIT_USDT = 10.0


def load_settings(env_file: str | None = ".env") -> Settings:
    """
    .env + 환경 변수에서 설정을 읽어 Settings 를 만든다

    Args:
        env_file: 읽을 .env 경로. None 이면 .env 는 안 읽고 환경 변수만 본다
    Raises:
        ValueError: 설정값이 이상할 때 (모든 오류를 한 번에 모아서 알려줌)
    """
    if env_file:
        load_dotenv(env_file)

    d = Settings()
    fd = FeeSettings()
    fees = FeeSettings(
        upbit_trade_fee_rate=_env_float("UPBIT_TRADE_FEE_RATE", fd.upbit_trade_fee_rate),
        bybit_taker_fee_rate=_env_float("BYBIT_TAKER_FEE_RATE", fd.bybit_taker_fee_rate),
        upbit_trade_fee_rates=_coin_fee_overrides("UPBIT_TRADE_FEE_RATE"),
        bybit_taker_fee_rates=_coin_fee_overrides("BYBIT_TAKER_FEE_RATE"),
        transfer_fee_usdt_per_cycle=_env_float("TRANSFER_FEE_USDT_PER_CYCLE", fd.transfer_fee_usdt_per_cycle),
        slippage_buffer_percent=_env_float("SLIPPAGE_BUFFER_PERCENT", fd.slippage_buffer_percent),
    )
    settings = Settings(
        paper_trading=_env_bool("PAPER_TRADING", d.paper_trading),
        upbit_access_key=_env_str("UPBIT_ACCESS_KEY", ""),
        upbit_secret_key=_env_str("UPBIT_SECRET_KEY", ""),
        bybit_api_key=_env_str("BYBIT_API_KEY", ""),
        bybit_api_secret=_env_str("BYBIT_API_SECRET", ""),
        live_confirm=_env_str("LIVE_CONFIRM", ""),
        live_dry_run=_env_bool("LIVE_DRY_RUN", d.live_dry_run),
        max_position_usdt=_env_optional_float("MAX_POSITION_USDT"),
        daily_loss_limit_usdt=_env_optional_float("DAILY_LOSS_LIMIT_USDT"),
        max_consecutive_failures=_env_int("MAX_CONSECUTIVE_FAILURES", d.max_consecutive_failures),
        stop_file=_env_str("STOP_FILE", d.stop_file),
        reconcile_interval_sec=_env_float("RECONCILE_INTERVAL_SEC", d.reconcile_interval_sec),
        live_db_path=_env_str("LIVE_DB_PATH", d.live_db_path),
        dry_run_db_path=_env_str("DRY_RUN_DB_PATH", d.dry_run_db_path),
        auto_transfer_enabled=_env_bool("AUTO_TRANSFER_ENABLED", d.auto_transfer_enabled),
        upbit_transfer_network=_env_str("UPBIT_TRANSFER_NETWORK", d.upbit_transfer_network).upper(),
        bybit_transfer_network=_env_str("BYBIT_TRANSFER_NETWORK", d.bybit_transfer_network).upper(),
        transfer_min_usdt=_env_float("TRANSFER_MIN_USDT", d.transfer_min_usdt),
        transfer_max_usdt_per_day=_env_float("TRANSFER_MAX_USDT_PER_DAY", d.transfer_max_usdt_per_day),
        transfer_alert_minutes=_env_float("TRANSFER_ALERT_MINUTES", d.transfer_alert_minutes),
        bybit_deposit_address=_env_str("BYBIT_DEPOSIT_ADDRESS", ""),
        bybit_deposit_tag=_env_str("BYBIT_DEPOSIT_TAG", ""),
        upbit_deposit_address=_env_str("UPBIT_DEPOSIT_ADDRESS", ""),
        upbit_deposit_tag=_env_str("UPBIT_DEPOSIT_TAG", ""),
        telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=_env_str("TELEGRAM_CHAT_ID", ""),
        coins=_env_list("COINS", ",".join(d.coins)),
        entry_premium_threshold_percent=_env_float("ENTRY_PREMIUM_THRESHOLD_PERCENT", d.entry_premium_threshold_percent),
        target_net_profit_percent=_env_float("TARGET_NET_PROFIT_PERCENT", d.target_net_profit_percent),
        stop_loss_percent=_env_float("STOP_LOSS_PERCENT", d.stop_loss_percent),
        tp_exec_buffer_enabled=_env_bool("TP_EXEC_BUFFER_ENABLED", d.tp_exec_buffer_enabled),
        tp_stress_qty_multiplier=_env_float("TP_STRESS_QTY_MULTIPLIER", d.tp_stress_qty_multiplier),
        position_size_usdt=_env_optional_float("POSITION_SIZE_USDT"),
        capital_usage_percent=_env_float("CAPITAL_USAGE_PERCENT", d.capital_usage_percent),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", d.max_open_positions),
        reentry_cooldown_sec=_env_float("REENTRY_COOLDOWN_SEC", d.reentry_cooldown_sec),
        max_slippage_percent=_env_float("MAX_SLIPPAGE_PERCENT", d.max_slippage_percent),
        upbit_coin_order_type=_env_str("UPBIT_COIN_ORDER_TYPE", d.upbit_coin_order_type).strip().lower(),
        bybit_leverage=_env_float("BYBIT_LEVERAGE", d.bybit_leverage),
        bybit_margin_buffer_percent=_env_float("BYBIT_MARGIN_BUFFER_PERCENT", d.bybit_margin_buffer_percent),
        bybit_mmr=_env_float("BYBIT_MMR", d.bybit_mmr),
        liq_warn_percent=_env_float("LIQ_WARN_PERCENT", d.liq_warn_percent),
        liq_guard_percent=_env_float("LIQ_GUARD_PERCENT", d.liq_guard_percent),
        liq_guard_near_target_percent=_env_float("LIQ_GUARD_NEAR_TARGET_PERCENT", d.liq_guard_near_target_percent),
        liq_guard_hold_below_percent=_env_float("LIQ_GUARD_HOLD_BELOW_PERCENT", d.liq_guard_hold_below_percent),
        liq_guard_topup_max_rounds=_env_int("LIQ_GUARD_TOPUP_MAX_ROUNDS", d.liq_guard_topup_max_rounds),
        liq_guard_topup_unit_ratio=_env_float("LIQ_GUARD_TOPUP_UNIT_RATIO", d.liq_guard_topup_unit_ratio),
        rebalance_threshold_percent=_env_float("REBALANCE_THRESHOLD_PERCENT", d.rebalance_threshold_percent),
        hedge_tolerance_percent=_env_float("HEDGE_TOLERANCE_PERCENT", d.hedge_tolerance_percent),
        upbit_min_order_krw=_env_float("UPBIT_MIN_ORDER_KRW", d.upbit_min_order_krw),
        entry_min_funding_8h_percent=(_env_optional_float("ENTRY_MIN_FUNDING_8H_PERCENT")
                                      if os.getenv("ENTRY_MIN_FUNDING_8H_PERCENT") is not None
                                      else d.entry_min_funding_8h_percent),
        max_funding_cost_percent=_env_float("MAX_FUNDING_COST_PERCENT", d.max_funding_cost_percent),
        entry_max_spread_percent=_env_float("ENTRY_MAX_SPREAD_PERCENT", d.entry_max_spread_percent),
        one_shot=_env_bool("ONE_SHOT", d.one_shot),
        one_shot_max_hours=_env_float("ONE_SHOT_MAX_HOURS", d.one_shot_max_hours),
        transfer_fee_upbit_to_bybit_usdt=_env_float("TRANSFER_FEE_UPBIT_TO_BYBIT_USDT",
                                                    d.transfer_fee_upbit_to_bybit_usdt),
        transfer_fee_bybit_to_upbit_usdt=_env_float("TRANSFER_FEE_BYBIT_TO_UPBIT_USDT",
                                                    d.transfer_fee_bybit_to_upbit_usdt),
        paper_auto_rebalance=_env_bool("PAPER_AUTO_REBALANCE", d.paper_auto_rebalance),
        fees=fees,
        scan_interval_sec=_env_float("SCAN_INTERVAL_SEC", d.scan_interval_sec),
        snapshot_interval_sec=_env_float("SNAPSHOT_INTERVAL_SEC", d.snapshot_interval_sec),
        funding_refresh_sec=_env_float("FUNDING_REFRESH_SEC", d.funding_refresh_sec),
        orderbook_depth=_env_int("ORDERBOOK_DEPTH", d.orderbook_depth),
        quote_source=_env_str("QUOTE_SOURCE", d.quote_source).strip().lower(),
        quote_ws_max_age_ms=_env_int("QUOTE_WS_MAX_AGE_MS", d.quote_ws_max_age_ms),
        fx_refresh_sec=_env_float("FX_REFRESH_SEC", d.fx_refresh_sec),
        fixed_usd_krw=_env_optional_float("FIXED_USD_KRW"),
        paper_initial_total_usdt=_env_float("PAPER_INITIAL_TOTAL_USDT", d.paper_initial_total_usdt),
        db_path=_env_str("DB_PATH", d.db_path),
        log_level=_env_str("LOG_LEVEL", d.log_level).upper(),
        log_dir=_env_str("LOG_DIR", d.log_dir),
        history_db_path=_env_str("HISTORY_DB_PATH", d.history_db_path),
        snapshot_db_path=_env_str("SNAPSHOT_DB_PATH", d.snapshot_db_path),
        sim_default_spread_percent=_env_float("SIM_DEFAULT_SPREAD_PERCENT", d.sim_default_spread_percent),
        carry_enabled=_env_bool("CARRY_ENABLED", d.carry_enabled),
        carry_coins=_env_list("CARRY_COINS", ""),
        carry_capital_usdt=_env_float("CARRY_CAPITAL_USDT", d.carry_capital_usdt),
        carry_leverage=_env_float("CARRY_LEVERAGE", d.carry_leverage),
        carry_max_positions=_env_int("CARRY_MAX_POSITIONS", d.carry_max_positions),
        carry_position_size_usdt=_env_optional_float("CARRY_POSITION_SIZE_USDT"),
        carry_entry_min_daily_percent=_env_float("CARRY_ENTRY_MIN_DAILY_PERCENT", d.carry_entry_min_daily_percent),
        carry_entry_lookback_hours=_env_float("CARRY_ENTRY_LOOKBACK_HOURS", d.carry_entry_lookback_hours),
        carry_exit_below_daily_percent=_env_float("CARRY_EXIT_BELOW_DAILY_PERCENT", d.carry_exit_below_daily_percent),
        carry_exit_lookback_hours=_env_float("CARRY_EXIT_LOOKBACK_HOURS", d.carry_exit_lookback_hours),
        carry_min_entry_basis_percent=_env_float("CARRY_MIN_ENTRY_BASIS_PERCENT", d.carry_min_entry_basis_percent),
        carry_spot_fee_rate=_env_float("CARRY_SPOT_FEE_RATE", d.carry_spot_fee_rate),
        carry_liq_guard_percent=_env_float("CARRY_LIQ_GUARD_PERCENT", d.carry_liq_guard_percent),
        carry_reentry_cooldown_hours=_env_float("CARRY_REENTRY_COOLDOWN_HOURS", d.carry_reentry_cooldown_hours),
        carry_min_volume_usdt=_env_float("CARRY_MIN_VOLUME_USDT", d.carry_min_volume_usdt),
        carry_universe_max=_env_int("CARRY_UNIVERSE_MAX", d.carry_universe_max),
        carry_scan_interval_sec=_env_float("CARRY_SCAN_INTERVAL_SEC", d.carry_scan_interval_sec),
        carry_refresh_sec=_env_float("CARRY_REFRESH_SEC", d.carry_refresh_sec),
    )
    # 실거래·드라이런에서 안전 한도를 비워두면 기본값 (페이퍼와 같은 로직, 값만 정해 줌)
    if not settings.paper_trading:
        settings = replace(settings,
                           max_position_usdt=settings.max_position_usdt or LIVE_DEFAULT_MAX_POSITION_USDT,
                           daily_loss_limit_usdt=settings.daily_loss_limit_usdt or LIVE_DEFAULT_DAILY_LOSS_LIMIT_USDT)
    # 공통값이 정해진 뒤에 코인별 덮어쓰기 적용 (frozen 이라 replace 로 새로 만든다)
    settings = replace(settings, coin_params=_load_coin_params(settings),
                       config_warnings=tuple(_find_unused_coin_keys(settings.coins)) + tuple(_find_old_keys()))
    validate_settings(settings)
    return settings


def _load_coin_params(settings: Settings) -> Dict[str, CoinParams]:
    """COINS 의 코인마다 '<코인>_<항목>' 환경 변수를 읽어 CoinParams 를 만든다 (없는 항목은 공통값)"""
    result: Dict[str, CoinParams] = {}
    base = settings.default_params()
    for coin in settings.coins:
        values = {}
        overridden: List[str] = []
        for field_name, suffix in COIN_OVERRIDE_FIELDS:
            key = f"{coin}_{suffix}"
            raw = os.getenv(key, "").strip()
            if raw:
                try:
                    values[field_name] = float(raw)
                except ValueError:
                    raise ValueError(f"설정 오류: {key}={raw} 는 숫자여야 합니다") from None
                overridden.append(field_name)
        result[coin] = replace(base, **values, overridden=tuple(overridden))
    return result


# 이름이 바뀐 설정: {옛 이름: 새 이름 안내}
OLD_KEYS = {
    "REBALANCE_TRANSFER_FEE_USDT":
        "송금 수수료가 방향별로 나뉘었습니다 → TRANSFER_FEE_UPBIT_TO_BYBIT_USDT(기본 0) / "
        "TRANSFER_FEE_BYBIT_TO_UPBIT_USDT(기본 1.3). 업비트 USDT(TRX) 출금은 무료이고 "
        "바이비트 USDT(TRC20) 출금만 수수료가 있어서 하나로 두면 한쪽이 틀립니다. 이 줄은 지워 주세요",
}


def _find_old_keys() -> List[str]:
    """옛 설정 이름이 .env 에 남아 있으면 경고한다 (값은 쓰지 않는다. 실행은 막지 않음)"""
    return [f"{key} 는 이제 쓰이지 않습니다: {hint}" for key, hint in OLD_KEYS.items() if os.getenv(key, "").strip()]


# 코인 이름이 아니라서 '코인별 설정' 으로 착각하면 안 되는 앞머리
# 예) CARRY_POSITION_SIZE_USDT 는 펀딩비 전략 설정이지 'CARRY 코인' 설정이 아니다
NOT_COIN_PREFIXES = ("CARRY",)


def _coin_fee_overrides(suffix: str) -> Dict[str, float]:
    """
    .env 의 코인별 수수료 덮어쓰기: <코인>_UPBIT_TRADE_FEE_RATE / <코인>_BYBIT_TAKER_FEE_RATE → {코인: 요율}

    다른 코인별 설정과 달리 COINS 에 없는 코인도 받는다 — 추천(screen)·시뮬레이션이 그 코인을 평가할 때 쓰기 때문.
    (CARRY_ 같은 전략 접두어는 코인이 아니다)
    """
    pattern = re.compile(rf"^([A-Z0-9]+)_{suffix}$")
    out: Dict[str, float] = {}
    for key, value in os.environ.items():
        match = pattern.match(key)
        if match and match.group(1) not in NOT_COIN_PREFIXES and value.strip():
            out[match.group(1)] = float(value)
    return out


def _find_unused_coin_keys(coins: List[str]) -> List[str]:
    """COINS 에 없는 코인의 개별 설정 키를 찾아 경고로 돌려준다 (오타 방지. 실행은 막지 않음)"""
    suffixes = "|".join(suffix for _, suffix in COIN_OVERRIDE_FIELDS)
    pattern = re.compile(rf"^([A-Z0-9]+)_({suffixes})$")
    warnings: List[str] = []
    for key in sorted(os.environ):
        match = pattern.match(key)
        if match and match.group(1) not in coins and match.group(1) not in NOT_COIN_PREFIXES:
            warnings.append(f"{key} 는 COINS 에 없는 코인({match.group(1)})의 설정이라 무시됩니다")
    return warnings


def validate_settings(s: Settings) -> None:
    """설정값 검사. 이상한 값이 있으면 전부 모아서 한 번에 알려준다"""
    errors: List[str] = []
    if not s.coins:
        errors.append("COINS 가 비어 있습니다")
    if s.max_open_positions < 1:
        errors.append("MAX_OPEN_POSITIONS 는 1 이상이어야 합니다")

    # 공통값과 코인별 값을 같은 규칙으로 검사 (키 이름은 .env 에 쓴 이름으로)
    targets = [("", s.default_params())] + [(f"{c}_", p) for c, p in s.coin_params.items()]
    for prefix, p in targets:
        if p.position_size_usdt is not None and p.position_size_usdt <= 0:
            errors.append(f"{prefix}POSITION_SIZE_USDT 는 0 보다 커야 합니다 (자동으로 하려면 비워두기)")
        if p.target_net_profit_percent <= 0:
            errors.append(f"{prefix}TARGET_NET_PROFIT_PERCENT 는 0 보다 커야 합니다")
        if p.stop_loss_percent > 0:
            errors.append(f"{prefix}STOP_LOSS_PERCENT 는 0 이하(음수)여야 합니다 (0 = 사용 안 함)")

    if not 0 < s.capital_usage_percent <= 100:
        errors.append("CAPITAL_USAGE_PERCENT 는 0 초과 100 이하여야 합니다")
    if not 1 <= s.bybit_leverage <= 10:
        errors.append("BYBIT_LEVERAGE 는 1~10 사이여야 합니다")
    if s.bybit_margin_buffer_percent < 0:
        errors.append("BYBIT_MARGIN_BUFFER_PERCENT 는 0 이상이어야 합니다")
    if not 0 <= s.bybit_mmr < 0.1:
        errors.append("BYBIT_MMR 는 소수 비율이어야 합니다 (예: 0.005 = 0.5%)")
    if s.liq_warn_percent <= 0:
        errors.append("LIQ_WARN_PERCENT 는 0 보다 커야 합니다")
    if not 0 < s.liq_guard_percent <= s.liq_warn_percent:
        errors.append("LIQ_GUARD_PERCENT 는 0 보다 크고 LIQ_WARN_PERCENT 이하여야 합니다 (경고가 먼저, 가드가 뒤)")
    if s.liq_guard_topup_max_rounds < 0:
        errors.append("LIQ_GUARD_TOPUP_MAX_ROUNDS 는 0 이상이어야 합니다 (0 = 증거금 추가 안 함)")
    if s.liq_guard_topup_unit_ratio <= 0:
        errors.append("LIQ_GUARD_TOPUP_UNIT_RATIO 는 0 보다 커야 합니다 (원래 증거금 대비 배수, 예: 1 / 0.5)")
    if s.transfer_fee_upbit_to_bybit_usdt < 0 or s.transfer_fee_bybit_to_upbit_usdt < 0:
        errors.append("TRANSFER_FEE_UPBIT_TO_BYBIT_USDT / TRANSFER_FEE_BYBIT_TO_UPBIT_USDT 는 0 이상이어야 합니다")
    if s.rebalance_threshold_percent <= 0:
        errors.append("REBALANCE_THRESHOLD_PERCENT 는 0 보다 커야 합니다")
    if s.reentry_cooldown_sec < 0:
        errors.append("REENTRY_COOLDOWN_SEC 는 0 이상이어야 합니다")
    if s.max_slippage_percent < 0:
        errors.append("MAX_SLIPPAGE_PERCENT 는 0 이상이어야 합니다")
    if s.upbit_coin_order_type not in ("market", "fok"):
        errors.append("UPBIT_COIN_ORDER_TYPE 는 market(시장가) 또는 fok(판단가 지정가, 전량 즉시 체결 아니면 취소) 여야 합니다")
    if s.entry_max_spread_percent < 0:
        errors.append("ENTRY_MAX_SPREAD_PERCENT 는 0 이상이어야 합니다 (0 = 보지 않음)")
    if s.tp_stress_qty_multiplier < 1:
        errors.append("TP_STRESS_QTY_MULTIPLIER 는 1 이상이어야 합니다 (1 = 앞에 아무도 없음 = 여유 0)")
    if s.scan_interval_sec < 1:
        errors.append("SCAN_INTERVAL_SEC 는 1 이상이어야 합니다 (거래소 요청 제한)")
    if s.orderbook_depth < 5:
        errors.append("ORDERBOOK_DEPTH 는 5 이상이어야 합니다")
    if s.quote_source not in ("rest", "ws"):
        errors.append("QUOTE_SOURCE 는 rest 또는 ws 여야 합니다")
    if s.quote_ws_max_age_ms < 200:
        errors.append("QUOTE_WS_MAX_AGE_MS 는 200 이상이어야 합니다 (소켓 호가 도착 지연이 70~80 ms)")
    if s.sim_default_spread_percent < 0:
        errors.append("SIM_DEFAULT_SPREAD_PERCENT 는 0 이상이어야 합니다")
    if s.fx_refresh_sec < 5:
        errors.append("FX_REFRESH_SEC 는 5 이상이어야 합니다")
    if s.fixed_usd_krw is not None and not 500 < s.fixed_usd_krw < 5000:
        errors.append(f"FIXED_USD_KRW={s.fixed_usd_krw} 가 이상합니다 (예: 1325)")
    if s.paper_initial_total_usdt <= 0:
        errors.append("PAPER_INITIAL_TOTAL_USDT 는 0 보다 커야 합니다")

    f = s.fees
    for coin, rate in {**{f"{c}_UPBIT_TRADE_FEE_RATE": r for c, r in f.upbit_trade_fee_rates.items()},
                       **{f"{c}_BYBIT_TAKER_FEE_RATE": r for c, r in f.bybit_taker_fee_rates.items()}}.items():
        if not 0 <= rate < 0.01:
            errors.append(f"{coin} 는 0 이상 1% 미만의 소수여야 합니다 (예: 0.0011 = 0.11%)")
    for name, value in (("UPBIT_TRADE_FEE_RATE", f.upbit_trade_fee_rate), ("BYBIT_TAKER_FEE_RATE", f.bybit_taker_fee_rate)):
        # 수수료율은 소수로. 0.05 처럼 % 로 잘못 넣는 실수를 잡는다
        if not 0 <= value < 0.01:
            errors.append(f"{name}={value} 는 소수 비율이어야 합니다 (예: 0.0005 = 0.05%)")
    if f.transfer_fee_usdt_per_cycle < 0:
        errors.append("TRANSFER_FEE_USDT_PER_CYCLE 는 0 이상이어야 합니다")

    # 실거래
    if not s.paper_trading:
        if not (s.upbit_access_key and s.upbit_secret_key and s.bybit_api_key and s.bybit_api_secret):
            errors.append("실거래(PAPER_TRADING=false)에는 UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY, "
                          "BYBIT_API_KEY, BYBIT_API_SECRET 가 모두 필요합니다")
        if not s.live_dry_run and s.live_confirm != "YES":
            errors.append("실제 주문(LIVE_DRY_RUN=false)을 내려면 LIVE_CONFIRM=YES 를 적어야 합니다 (실수 방지)")
    if s.max_position_usdt is not None and s.max_position_usdt <= 0:
        errors.append("MAX_POSITION_USDT 는 0보다 커야 합니다")
    if s.daily_loss_limit_usdt is not None and s.daily_loss_limit_usdt <= 0:
        errors.append("DAILY_LOSS_LIMIT_USDT 는 0보다 커야 합니다")
    if s.max_funding_cost_percent < 0:
        errors.append("MAX_FUNDING_COST_PERCENT 는 0 이상이어야 합니다 (0 = 무제한)")
    if s.one_shot_max_hours < 0:
        errors.append("ONE_SHOT_MAX_HOURS 는 0 이상이어야 합니다 (0 = 무제한)")
    if s.transfer_min_usdt <= max(s.transfer_fee_upbit_to_bybit_usdt, s.transfer_fee_bybit_to_upbit_usdt):
        errors.append("TRANSFER_MIN_USDT 는 송금 수수료(TRANSFER_FEE_*)보다 커야 합니다")

    # 펀딩비 전략
    if s.carry_capital_usdt <= 0:
        errors.append("CARRY_CAPITAL_USDT 는 0보다 커야 합니다")
    if s.carry_leverage < 1:
        errors.append("CARRY_LEVERAGE 는 1 이상이어야 합니다")
    if s.carry_max_positions < 1:
        errors.append("CARRY_MAX_POSITIONS 는 1 이상이어야 합니다")
    if s.carry_exit_below_daily_percent >= s.carry_entry_min_daily_percent:
        errors.append("CARRY_EXIT_BELOW_DAILY_PERCENT 는 CARRY_ENTRY_MIN_DAILY_PERCENT 보다 작아야 합니다 "
                      "(같으면 들어가자마자 나왔다 다시 들어가기를 반복)")
    if not 0 <= s.carry_spot_fee_rate < 0.01:
        errors.append(f"CARRY_SPOT_FEE_RATE={s.carry_spot_fee_rate} 는 소수 비율이어야 합니다 (예: 0.001 = 0.1%)")

    if errors:
        raise ValueError("설정 오류:\n" + "\n".join(f"  - {e}" for e in errors))

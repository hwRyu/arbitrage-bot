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
from typing import Dict, List, Optional, Tuple

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

    # 바이비트 USDT 무기한 Taker 수수료율 (시장가 주문). 숏 열 때 1번, 닫을 때 1번
    bybit_taker_fee_rate: float = 0.00055

    # (선택) 사이클(진입 → 청산 1번)마다 손익에서 미리 떼어두는 전송비 (USDT). 기본 0
    # 송금은 매 거래마다 하는 게 아니라 '잔고가 모자라거나 비율이 많이 깨졌을 때만' 한다.
    # 그 실제 송금 비용은 REBALANCE_TRANSFER_FEE_USDT 로 송금할 때 한 번씩 뗀다.
    # 그래도 거래 손익에 미리 나눠서 반영하고 싶을 때만 쓴다. 예) 다섯 사이클에 한 번 송금 → 0.2
    transfer_fee_usdt_per_cycle: float = 0.0

    # "김프가 대략 몇 % 가 되면 청산되나" 를 로그에 보여줄 때만 쓰는 여유분 (%)
    # 실제 판단은 호가창으로 계산한 실제 체결가로 한다.
    slippage_buffer_percent: float = 0.05

    def round_trip_trade_fee_percent(self) -> float:
        """
        들어갔다 나오는 한 바퀴의 거래 수수료 합계 (%)

        업비트 USDT 매도 + 코인 매수 + 코인 매도 + USDT 매수 = 업비트 4번
        바이비트 숏 열기 + 닫기 = 바이비트 2번
        → 4 × 0.05% + 2 × 0.055% = 0.31%
        """
        return (4 * self.upbit_trade_fee_rate + 2 * self.bybit_taker_fee_rate) * 100


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
    # 보낼 네트워크 (업비트 net_type / 바이비트 chain). 예) TRX (수수료 싸고 빠름). 양쪽이 지원해야 함
    transfer_network: str = "TRX"
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

    # ------------------------------------------------------------------
    # 바이비트 레버리지 / 자본 비율 / 리밸런싱 (docs/concept.md 7번)
    # ------------------------------------------------------------------
    # 바이비트 숏 레버리지 (배). 증거금 = 숏 금액 ÷ 레버리지
    # 1배: 강제청산은 가격이 약 2배가 돼야. 3배: 약 +33% 오르면 강제청산
    bybit_leverage: float = 1.0

    # 바이비트에 증거금 말고 더 둘 여유분 (증거금의 %). 가격이 올라 숏이 손해 볼 때 버티는 돈
    bybit_margin_buffer_percent: float = 30.0

    # 바이비트 유지증거금률 (소수). 강제청산 가격 계산에 쓴다. 코인/포지션 크기마다 다를 수 있음
    bybit_mmr: float = 0.005

    # 강제청산 가격까지 남은 상승폭이 이 % 아래면 '증거금 추가' 긴급 경고
    liquidation_warn_percent: float = 15.0

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

    # 리밸런싱 송금 한 번에 드는 출금 수수료 (USDT). 송금할 때만 한 번 든다 (거래마다 X)
    rebalance_transfer_fee_usdt: float = 1.0

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
    # 시세 보는 간격 (초). 바이비트는 코인 수만큼 요청이 나가니 코인이 많으면 늘릴 것
    scan_interval_sec: float = 3.0

    # 김프 기록 저장 간격 (초). 코인마다 이 간격에 1건
    snapshot_interval_sec: float = 60.0

    # 펀딩비 정보 갱신 간격 (초). 보유 포지션 코인만
    funding_refresh_sec: float = 60.0

    # 호가창 몇 줄까지 받을지
    orderbook_depth: int = 30

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

    # 과거엔 호가창이 없어서 코인 호가 간격(업비트 코인 + 바이비트, %)을 가정한다.
    # 실시간 김프 기록에 측정값이 있으면 먼저 쓰고, 없으면 이 값. 호가 한 칸보다는 좁게 잡지 않는다.
    # 코인별로는 .env 에 <코인>_SIM_SPREAD_PERCENT
    sim_default_spread_percent: float = 0.1

    # ------------------------------------------------------------------
    # 펀딩비 수익 전략 (kimp/carry.py, 김프 전략과 따로 돈다. 기준: docs/carry.md)
    #   바이비트 현물 매수 + 바이비트 무기한 같은 개수 숏 → 가격 변동은 상쇄, 펀딩비(+)를 숏이 받는다
    # ------------------------------------------------------------------
    carry_enabled: bool = True
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

    def upbit_target_share(self) -> float:
        """
        전체 자본 중 업비트에 둘 목표 비율 (0~1)

        업비트 비율 = 레버리지 ÷ (레버리지 + 1 + 여유분)
        예) 1배, 여유분 30% → 1 ÷ 2.3 = 43.5% (바이비트 56.5%)
            3배, 여유분 30% → 3 ÷ 4.3 = 69.8% (바이비트 30.2%)
        이유: 숏 금액 N 마다 업비트에 N, 바이비트에 증거금 N÷레버리지 + 여유분이 필요하다.
        """
        lev = self.bybit_leverage
        return lev / (lev + 1 + self.bybit_margin_buffer_percent / 100)

    @property
    def mode(self) -> str:
        """실행 모드: 'paper' (가짜) / 'dry_run' (실제 잔고·호가, 주문 안 냄) / 'live' (실제 주문)"""
        if self.paper_trading:
            return "paper"
        return "dry_run" if self.live_dry_run else "live"

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
        transfer_network=_env_str("TRANSFER_NETWORK", d.transfer_network).upper(),
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
        position_size_usdt=_env_optional_float("POSITION_SIZE_USDT"),
        capital_usage_percent=_env_float("CAPITAL_USAGE_PERCENT", d.capital_usage_percent),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", d.max_open_positions),
        reentry_cooldown_sec=_env_float("REENTRY_COOLDOWN_SEC", d.reentry_cooldown_sec),
        max_slippage_percent=_env_float("MAX_SLIPPAGE_PERCENT", d.max_slippage_percent),
        bybit_leverage=_env_float("BYBIT_LEVERAGE", d.bybit_leverage),
        bybit_margin_buffer_percent=_env_float("BYBIT_MARGIN_BUFFER_PERCENT", d.bybit_margin_buffer_percent),
        bybit_mmr=_env_float("BYBIT_MMR", d.bybit_mmr),
        liquidation_warn_percent=_env_float("LIQUIDATION_WARN_PERCENT", d.liquidation_warn_percent),
        rebalance_threshold_percent=_env_float("REBALANCE_THRESHOLD_PERCENT", d.rebalance_threshold_percent),
        hedge_tolerance_percent=_env_float("HEDGE_TOLERANCE_PERCENT", d.hedge_tolerance_percent),
        upbit_min_order_krw=_env_float("UPBIT_MIN_ORDER_KRW", d.upbit_min_order_krw),
        rebalance_transfer_fee_usdt=_env_float("REBALANCE_TRANSFER_FEE_USDT", d.rebalance_transfer_fee_usdt),
        paper_auto_rebalance=_env_bool("PAPER_AUTO_REBALANCE", d.paper_auto_rebalance),
        fees=fees,
        scan_interval_sec=_env_float("SCAN_INTERVAL_SEC", d.scan_interval_sec),
        snapshot_interval_sec=_env_float("SNAPSHOT_INTERVAL_SEC", d.snapshot_interval_sec),
        funding_refresh_sec=_env_float("FUNDING_REFRESH_SEC", d.funding_refresh_sec),
        orderbook_depth=_env_int("ORDERBOOK_DEPTH", d.orderbook_depth),
        fx_refresh_sec=_env_float("FX_REFRESH_SEC", d.fx_refresh_sec),
        fixed_usd_krw=_env_optional_float("FIXED_USD_KRW"),
        paper_initial_total_usdt=_env_float("PAPER_INITIAL_TOTAL_USDT", d.paper_initial_total_usdt),
        db_path=_env_str("DB_PATH", d.db_path),
        log_level=_env_str("LOG_LEVEL", d.log_level).upper(),
        log_dir=_env_str("LOG_DIR", d.log_dir),
        history_db_path=_env_str("HISTORY_DB_PATH", d.history_db_path),
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
                       config_warnings=tuple(_find_unused_coin_keys(settings.coins)))
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


def _find_unused_coin_keys(coins: List[str]) -> List[str]:
    """COINS 에 없는 코인의 개별 설정 키를 찾아 경고로 돌려준다 (오타 방지. 실행은 막지 않음)"""
    suffixes = "|".join(suffix for _, suffix in COIN_OVERRIDE_FIELDS)
    pattern = re.compile(rf"^([A-Z0-9]+)_({suffixes})$")
    warnings: List[str] = []
    for key in sorted(os.environ):
        match = pattern.match(key)
        if match and match.group(1) not in coins:
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
    if s.liquidation_warn_percent <= 0:
        errors.append("LIQUIDATION_WARN_PERCENT 는 0 보다 커야 합니다")
    if s.rebalance_transfer_fee_usdt < 0:
        errors.append("REBALANCE_TRANSFER_FEE_USDT 는 0 이상이어야 합니다")
    if s.rebalance_threshold_percent <= 0:
        errors.append("REBALANCE_THRESHOLD_PERCENT 는 0 보다 커야 합니다")
    if s.reentry_cooldown_sec < 0:
        errors.append("REENTRY_COOLDOWN_SEC 는 0 이상이어야 합니다")
    if s.max_slippage_percent < 0:
        errors.append("MAX_SLIPPAGE_PERCENT 는 0 이상이어야 합니다")
    if s.scan_interval_sec < 1:
        errors.append("SCAN_INTERVAL_SEC 는 1 이상이어야 합니다 (거래소 요청 제한)")
    if s.orderbook_depth < 5:
        errors.append("ORDERBOOK_DEPTH 는 5 이상이어야 합니다")
    if s.sim_default_spread_percent < 0:
        errors.append("SIM_DEFAULT_SPREAD_PERCENT 는 0 이상이어야 합니다")
    if s.fx_refresh_sec < 5:
        errors.append("FX_REFRESH_SEC 는 5 이상이어야 합니다")
    if s.fixed_usd_krw is not None and not 500 < s.fixed_usd_krw < 5000:
        errors.append(f"FIXED_USD_KRW={s.fixed_usd_krw} 가 이상합니다 (예: 1325)")
    if s.paper_initial_total_usdt <= 0:
        errors.append("PAPER_INITIAL_TOTAL_USDT 는 0 보다 커야 합니다")

    f = s.fees
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
    if s.transfer_min_usdt <= s.rebalance_transfer_fee_usdt:
        errors.append("TRANSFER_MIN_USDT 는 REBALANCE_TRANSFER_FEE_USDT(송금 수수료)보다 커야 합니다")

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

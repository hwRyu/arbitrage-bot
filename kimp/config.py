"""
봇 설정 로딩

모든 설정은 .env 파일(또는 환경 변수)에서 읽어 불변(frozen) dataclass 로 만든다.
old 버전은 클래스 속성 + DB 프록시 바꿔치기 방식이라 추적이 어려웠으므로,
여기서는 `load_settings()` 한 번으로 Settings 객체를 만들고 필요한 곳에 주입한다.

단위 규칙 (코드 전체 공통)
- 수수료율(rate)  : 소수 (0.0005 = 0.05%)
- 퍼센트(percent) : 퍼센트 값 그대로 (1.0 = 1%)
- 금액            : 변수명 끝에 통화를 붙인다 (_krw, _usdt)
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
    손익 계산에 들어가는 모든 비용

    봇의 진입/청산 판단은 "이 비용을 모두 뺀 순이익" 기준으로 이뤄진다.
    거래소 등급(VIP), 이벤트, 사용하는 출금 네트워크에 따라 실제 값이 달라지므로
    반드시 본인 계정 기준으로 확인 후 .env 에 맞춰 넣을 것.
    """

    # 업비트 KRW 마켓 현물 거래 수수료율 (매수/매도 각각 부과)
    # 업비트 기본 0.05%. 매수 시에는 주문 금액과 별도로 KRW 에서 추가 차감된다.
    upbit_trade_fee_rate: float = 0.0005

    # 바이비트 USDT 무기한 선물 Taker 수수료율 (시장가 = 호가를 긁는 주문)
    # 진입(숏 오픈) 1회 + 청산(숏 종료) 1회 부과. 계정 등급에 따라 다름 → 확인 필요.
    bybit_taker_fee_rate: float = 0.00055

    # 사이클(진입 → 청산 1회)당 배정하는 전송 비용 (USDT)
    # 봇은 USDT 를 직접 옮기지 않지만, 사용자가 업비트 ↔ 바이비트로 USDT(또는 코인)를
    # 옮길 때 드는 출금 수수료를 사이클 손익에 미리 반영하기 위한 값.
    # 예) USDT 출금 수수료가 1 USDT 이고 매 사이클마다 한 번 옮긴다면 1.0
    #     여러 사이클에 한 번 옮긴다면 (출금 수수료 / 사이클 수) 로 나눠서 입력
    #     전송을 전혀 안 한다면 0
    transfer_fee_usdt_per_cycle: float = 1.0

    # 목표 청산 김프 "추정치"를 계산할 때만 쓰는 슬리피지 여유분 (%)
    # 실제 진입/청산 판단은 호가창을 직접 긁어 계산한 체결가(VWAP)로 하므로
    # 이 값은 로그에 "대략 김프가 몇 % 가 되면 청산되는지" 보여주는 용도다.
    slippage_buffer_percent: float = 0.05

    def round_trip_trade_fee_percent(self) -> float:
        """
        왕복 거래 수수료 합계 (%)

        업비트 매수 + 업비트 매도 + 바이비트 숏 오픈 + 바이비트 숏 종료
        = 2 × (업비트 수수료율 + 바이비트 수수료율)
        포지션 크기 대비 비율이므로 금액과 무관하게 일정하다.
        """
        return 2 * (self.upbit_trade_fee_rate + self.bybit_taker_fee_rate) * 100


# 코인별로 덮어쓸 수 있는 설정 항목: (CoinParams 필드명, .env 키 접미사)
# .env 에 '<코인>_<접미사>' 로 넣으면 그 코인만 해당 값을 쓴다.
# 예) XRP_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.3
COIN_OVERRIDE_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("entry_premium_threshold_percent", "ENTRY_PREMIUM_THRESHOLD_PERCENT"),
    ("target_net_profit_percent", "TARGET_NET_PROFIT_PERCENT"),
    ("stop_loss_percent", "STOP_LOSS_PERCENT"),
    ("position_size_usdt", "POSITION_SIZE_USDT"),
)


@dataclass(frozen=True)
class CoinParams:
    """
    코인 한 개에 실제로 적용되는 진입/청산/규모 설정

    공통값(Settings 의 같은 이름 필드)에서 시작해서, .env 에 코인별 값이 있으면 그 값으로 덮어쓴다.
    각 항목의 의미는 Settings 의 같은 이름 필드 주석을 참고.

    Attributes:
        overridden: 코인별 값으로 덮어쓴 필드 이름 목록 (시작 로그 표시용)
    """

    entry_premium_threshold_percent: float
    target_net_profit_percent: float
    stop_loss_percent: float
    position_size_usdt: float
    overridden: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    """
    봇 전체 설정

    진입 기준 / 목표 순이익 / 손절 / 포지션 규모는 여기 값이 '공통 기본값'이고,
    실제로 코인에 적용되는 값은 반드시 `params_for(coin)` 으로 꺼내 써야 한다.
    (코인별 덮어쓰기가 반영된 값)
    """

    # ------------------------------------------------------------------
    # 실행 모드
    # ------------------------------------------------------------------
    # 현재 버전은 페이퍼 트레이딩(모의 체결)만 지원한다.
    # 실주문 코드는 다음 단계에서 추가 예정이며, false 로 두면 실행을 거부한다.
    paper_trading: bool = True

    # ------------------------------------------------------------------
    # 대상 코인
    # ------------------------------------------------------------------
    # 사용자가 직접 추린 모니터링 코인 목록.
    # 업비트 KRW 마켓과 바이비트 USDT 무기한 선물에 모두 상장된 코인만 유효하며,
    # 한쪽에라도 없으면 시작 시 경고 후 목록에서 제외한다.
    coins: List[str] = field(default_factory=lambda: ["XRP", "SOL", "DOGE"])

    # ------------------------------------------------------------------
    # 진입 / 청산 조건 (공통 기본값 — 코인별 덮어쓰기 가능, COIN_OVERRIDE_FIELDS 참고)
    # ------------------------------------------------------------------
    # 진입 기준 김프 (%)
    # 김프는 달러 환율 기준(테더 프리미엄 포함)이라 김프 사이트 숫자와 같은 기준이다.
    # 실제로 체결될 가격(업비트에서 살 가격, 바이비트에서 숏 칠 가격)으로 계산한
    # "진입 김프"가 이 값 이하일 때 진입한다.
    # 예) -0.5 → 업비트가 바이비트보다 0.5% 이상 쌀 때(역프 0.5% 이상) 진입
    entry_premium_threshold_percent: float = 0.0

    # 목표 순이익률 (%)
    # 지금 청산한다고 가정했을 때의 순이익(모든 수수료·전송비·펀딩비 반영)이
    # 포지션 규모 대비 이 값 이상이면 청산한다.
    target_net_profit_percent: float = 1.0

    # 손절 순손익률 (%)
    # 순손익률이 이 값 이하로 떨어지면 강제 청산한다. (음수로 입력)
    # 헷지 포지션이라 가격 방향 손실은 없지만, 김프가 진입 시보다 더 떨어지면
    # 그만큼 평가 손실이 생긴다. 0 이면 손절 비활성화.
    stop_loss_percent: float = -5.0

    # ------------------------------------------------------------------
    # 포지션 크기 / 리스크
    # ------------------------------------------------------------------
    # 코인 1개당 포지션 규모 (USDT 기준, 공통 기본값 — 코인별 덮어쓰기 가능)
    # 바이비트 숏 명목가 기준이며, 업비트에는 같은 금액(USDT 환산)만큼 매수한다. (금액 헷징)
    position_size_usdt: float = 500.0

    # 동시에 보유할 수 있는 최대 포지션 수 (코인당 최대 1개)
    max_open_positions: int = 3

    # 청산 후 같은 코인 재진입 대기 시간 (초)
    # 손절 직후에도 김프가 여전히 기준 이하라 바로 다시 들어가는 것을 막는다.
    # (메모리에만 보관하므로 봇을 재시작하면 초기화된다)
    reentry_cooldown_sec: float = 300.0

    # 허용 최대 슬리피지 (%)
    # 포지션 수량만큼 호가창을 긁었을 때 평균 체결가가 최우선 호가 대비
    # 이 값보다 불리하면 진입하지 않는다. (호가가 얇은 코인 걸러내기)
    max_slippage_percent: float = 0.3

    # ------------------------------------------------------------------
    # 비용
    # ------------------------------------------------------------------
    fees: FeeSettings = field(default_factory=FeeSettings)

    # ------------------------------------------------------------------
    # 주기
    # ------------------------------------------------------------------
    # 시세 스캔 주기 (초). 매 주기마다 전 코인의 호가창을 조회한다.
    # 바이비트는 코인 수만큼 요청이 나가므로 코인이 많으면 늘릴 것.
    scan_interval_sec: float = 3.0

    # 김프 스냅샷 DB 저장 주기 (초)
    # 매 스캔을 다 저장하면 DB 가 빠르게 커지므로 코인별로 이 주기마다 1건만 저장.
    snapshot_interval_sec: float = 60.0

    # 펀딩비 정보 갱신 주기 (초). 보유 포지션이 있는 코인만 조회한다.
    funding_refresh_sec: float = 60.0

    # 호가창 조회 깊이 (레벨 수)
    orderbook_depth: int = 30

    # ------------------------------------------------------------------
    # 달러 환율 (김프 계산 기준, kimp/fx.py)
    # ------------------------------------------------------------------
    # 몇 초마다 환율을 새로 받아올지
    fx_refresh_sec: float = 60.0

    # 마지막으로 환율을 받은 지 이 시간(초)이 넘으면 '오래된 값' 으로 보고 새 진입을 멈춘다.
    # (보유 포지션 청산 판단은 마지막 값으로 계속한다)
    fx_max_stale_sec: float = 1800.0

    # 값을 넣으면 환율을 조회하지 않고 이 값으로 고정한다. (예: 1325)
    # 테스트할 때나 환율 조회가 계속 실패할 때 쓴다. 비워두면 실시간 조회.
    fixed_usd_krw: Optional[float] = None

    # ------------------------------------------------------------------
    # 페이퍼 트레이딩 초기 자본 (최초 실행 시 1회만 적용, 이후 DB 잔고 유지)
    # ------------------------------------------------------------------
    paper_initial_krw: float = 10_000_000.0
    paper_initial_usdt: float = 5_000.0

    # ------------------------------------------------------------------
    # 저장소 / 로그
    # ------------------------------------------------------------------
    db_path: str = "data/kimp.db"
    log_level: str = "INFO"
    log_dir: str = "logs"

    # ------------------------------------------------------------------
    # 코인별 설정 (load_settings 가 채운다)
    # ------------------------------------------------------------------
    # {코인: 덮어쓰기까지 반영된 CoinParams}. COINS 에 있는 코인만 들어 있다.
    coin_params: Dict[str, CoinParams] = field(default_factory=dict)

    # 설정 로딩 중 발견한 경고 (예: COINS 에 없는 코인의 개별 설정). 로깅 준비 후 main 이 출력한다.
    config_warnings: Tuple[str, ...] = ()

    def default_params(self) -> CoinParams:
        """공통값으로만 만든 CoinParams (코인별 덮어쓰기 없음)"""
        return CoinParams(
            entry_premium_threshold_percent=self.entry_premium_threshold_percent,
            target_net_profit_percent=self.target_net_profit_percent,
            stop_loss_percent=self.stop_loss_percent,
            position_size_usdt=self.position_size_usdt,
        )

    def params_for(self, coin: str) -> CoinParams:
        """
        코인에 실제로 적용할 설정

        코인별 설정이 있으면 그 값을, 없으면 공통값을 돌려준다.
        COINS 에서 빠졌지만 보유 포지션이 남아 있는 코인도 공통값으로 계속 관리된다.
        """
        return self.coin_params.get(coin) or self.default_params()


def load_settings(env_file: str | None = ".env") -> Settings:
    """
    .env + 환경 변수에서 설정을 읽어 Settings 생성

    Args:
        env_file: 읽을 .env 경로. None 이면 .env 를 읽지 않고 환경 변수만 사용.

    Returns:
        Settings (검증 완료)

    Raises:
        ValueError: 설정값이 올바르지 않을 때 (모든 오류를 한 번에 모아서 보고)
    """
    if env_file:
        load_dotenv(env_file)

    defaults = Settings()
    fee_defaults = FeeSettings()

    fees = FeeSettings(
        upbit_trade_fee_rate=_env_float("UPBIT_TRADE_FEE_RATE", fee_defaults.upbit_trade_fee_rate),
        bybit_taker_fee_rate=_env_float("BYBIT_TAKER_FEE_RATE", fee_defaults.bybit_taker_fee_rate),
        transfer_fee_usdt_per_cycle=_env_float(
            "TRANSFER_FEE_USDT_PER_CYCLE", fee_defaults.transfer_fee_usdt_per_cycle
        ),
        slippage_buffer_percent=_env_float("SLIPPAGE_BUFFER_PERCENT", fee_defaults.slippage_buffer_percent),
    )

    settings = Settings(
        paper_trading=_env_bool("PAPER_TRADING", defaults.paper_trading),
        coins=_env_list("COINS", ",".join(defaults.coins)),
        entry_premium_threshold_percent=_env_float(
            "ENTRY_PREMIUM_THRESHOLD_PERCENT", defaults.entry_premium_threshold_percent
        ),
        target_net_profit_percent=_env_float("TARGET_NET_PROFIT_PERCENT", defaults.target_net_profit_percent),
        stop_loss_percent=_env_float("STOP_LOSS_PERCENT", defaults.stop_loss_percent),
        position_size_usdt=_env_float("POSITION_SIZE_USDT", defaults.position_size_usdt),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", defaults.max_open_positions),
        max_slippage_percent=_env_float("MAX_SLIPPAGE_PERCENT", defaults.max_slippage_percent),
        reentry_cooldown_sec=_env_float("REENTRY_COOLDOWN_SEC", defaults.reentry_cooldown_sec),
        fees=fees,
        scan_interval_sec=_env_float("SCAN_INTERVAL_SEC", defaults.scan_interval_sec),
        snapshot_interval_sec=_env_float("SNAPSHOT_INTERVAL_SEC", defaults.snapshot_interval_sec),
        funding_refresh_sec=_env_float("FUNDING_REFRESH_SEC", defaults.funding_refresh_sec),
        orderbook_depth=_env_int("ORDERBOOK_DEPTH", defaults.orderbook_depth),
        fx_refresh_sec=_env_float("FX_REFRESH_SEC", defaults.fx_refresh_sec),
        fx_max_stale_sec=_env_float("FX_MAX_STALE_SEC", defaults.fx_max_stale_sec),
        fixed_usd_krw=_env_float("FIXED_USD_KRW", 0.0) or None,
        paper_initial_krw=_env_float("PAPER_INITIAL_KRW", defaults.paper_initial_krw),
        paper_initial_usdt=_env_float("PAPER_INITIAL_USDT", defaults.paper_initial_usdt),
        db_path=_env_str("DB_PATH", defaults.db_path),
        log_level=_env_str("LOG_LEVEL", defaults.log_level).upper(),
        log_dir=_env_str("LOG_DIR", defaults.log_dir),
    )

    # 공통값이 확정된 뒤에 코인별 덮어쓰기를 적용한다 (frozen 이라 replace 로 새 객체 생성)
    settings = replace(
        settings,
        coin_params=_load_coin_params(settings),
        config_warnings=tuple(_find_unused_coin_keys(settings.coins)),
    )

    validate_settings(settings)
    return settings


def _load_coin_params(settings: Settings) -> Dict[str, CoinParams]:
    """
    COINS 의 각 코인에 대해 '<코인>_<항목>' 환경 변수를 읽어 CoinParams 생성

    값이 없거나 비어 있는 항목은 공통값을 그대로 쓴다.
    """
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
    """
    COINS 에 없는 코인에 대한 개별 설정 키를 찾아 경고 메시지로 반환

    오타(예: XPR_...)나 목록에서 뺀 코인의 설정이 남아 있는 경우를 알려주기 위함.
    값은 무시될 뿐 오류는 아니므로 실행을 막지는 않는다.
    """
    suffixes = "|".join(suffix for _, suffix in COIN_OVERRIDE_FIELDS)
    pattern = re.compile(rf"^([A-Z0-9]+)_({suffixes})$")
    warnings: List[str] = []
    for key in sorted(os.environ):
        match = pattern.match(key)
        if match and match.group(1) not in coins:
            warnings.append(f"{key} 는 COINS 에 없는 코인({match.group(1)})의 설정이라 무시됩니다")
    return warnings


def validate_settings(s: Settings) -> None:
    """
    설정값 검증

    잘못된 값으로 봇이 도는 것을 막기 위해 시작 시점에 모든 오류를 모아서 한 번에 보고한다.
    """
    errors: List[str] = []

    if not s.coins:
        errors.append("COINS 가 비어 있습니다")
    if s.max_open_positions < 1:
        errors.append("MAX_OPEN_POSITIONS 는 1 이상이어야 합니다")

    # 공통값과 코인별 값을 같은 규칙으로 검사한다. 키 이름은 사용자가 .env 에 쓴 이름으로 보여준다.
    targets = [("", s.default_params())] + [(f"{c}_", p) for c, p in s.coin_params.items()]
    for prefix, p in targets:
        if p.position_size_usdt <= 0:
            errors.append(f"{prefix}POSITION_SIZE_USDT 는 0 보다 커야 합니다")
        if p.target_net_profit_percent <= 0:
            errors.append(f"{prefix}TARGET_NET_PROFIT_PERCENT 는 0 보다 커야 합니다")
        if p.stop_loss_percent > 0:
            errors.append(f"{prefix}STOP_LOSS_PERCENT 는 0 이하(음수)여야 합니다 (0 = 비활성화)")
    if s.reentry_cooldown_sec < 0:
        errors.append("REENTRY_COOLDOWN_SEC 는 0 이상이어야 합니다")
    if s.max_slippage_percent < 0:
        errors.append("MAX_SLIPPAGE_PERCENT 는 0 이상이어야 합니다")
    if s.scan_interval_sec < 1:
        errors.append("SCAN_INTERVAL_SEC 는 1 이상이어야 합니다 (거래소 요청 제한)")
    if s.orderbook_depth < 5:
        errors.append("ORDERBOOK_DEPTH 는 5 이상이어야 합니다")
    if s.fx_refresh_sec < 5:
        errors.append("FX_REFRESH_SEC 는 5 이상이어야 합니다")
    if s.fx_max_stale_sec < s.fx_refresh_sec:
        errors.append("FX_MAX_STALE_SEC 는 FX_REFRESH_SEC 이상이어야 합니다")
    if s.fixed_usd_krw is not None and not 500 < s.fixed_usd_krw < 5000:
        errors.append(f"FIXED_USD_KRW={s.fixed_usd_krw} 가 이상합니다 (예: 1325)")

    f = s.fees
    for name, value in (
        ("UPBIT_TRADE_FEE_RATE", f.upbit_trade_fee_rate),
        ("BYBIT_TAKER_FEE_RATE", f.bybit_taker_fee_rate),
    ):
        # 수수료율은 소수로 입력해야 한다. 0.05 처럼 퍼센트로 잘못 넣는 실수를 잡는다.
        if not 0 <= value < 0.01:
            errors.append(f"{name}={value} 는 소수 비율이어야 합니다 (예: 0.0005 = 0.05%)")
    if f.transfer_fee_usdt_per_cycle < 0:
        errors.append("TRANSFER_FEE_USDT_PER_CYCLE 는 0 이상이어야 합니다")

    if errors:
        raise ValueError("설정 오류:\n" + "\n".join(f"  - {e}" for e in errors))

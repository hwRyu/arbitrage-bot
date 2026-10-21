"""
김치 프리미엄 차익거래 봇 설정 및 상수 정의

이 파일은 봇의 모든 설정값과 상수를 관리합니다.
환경 변수(.env 파일)에서 값을 불러오며, 기본값을 제공합니다.

주요 구성:
- API 키: 거래소 및 환율 API 인증 정보
- 봇 설정: 타겟 코인, 수익률 목표, 포지션 크기 등
- 수수료: 거래소 수수료 및 비용 정보
- 리스크 관리: 청산 안전마진, 호가창 깊이, 슬리피지 제한 등
- 바이비트 설정: 레버리지, 마진 모드 등
- 스캐너 설정: 스캔 주기, 최대 동시 전략 수 등
- 로깅 설정: 로그 레벨, 파일 저장 등
- 데이터베이스: DB 경로 설정
- 상태 머신: 전략 실행 상태 정의
"""
import os
from typing import List
from dotenv import load_dotenv

# 환경 변수 로드 (.env 파일)
load_dotenv()


# ============================================================================
# API 키
# ============================================================================
class APIKeys:
    """
    거래소 및 외부 서비스 API 키

    환경 변수에서 로드되며, 실거래 시 반드시 설정해야 합니다.
    페이퍼 트레이딩 모드에서는 필수가 아닙니다.

    보안 주의사항:
    - API 키는 절대 Git에 커밋하지 마세요
    - .env 파일은 .gitignore에 포함되어 있어야 합니다
    - 가능하면 읽기 전용 또는 최소 권한만 부여하세요
    """

    # 업비트 API 키 (국내 거래소)
    # 발급: 업비트 웹사이트 → 고객센터 → Open API 안내
    # 필요 권한: 자산 조회, 주문 조회, 주문하기
    UPBIT_ACCESS_KEY = os.getenv('UPBIT_ACCESS_KEY', '')
    UPBIT_SECRET_KEY = os.getenv('UPBIT_SECRET_KEY', '')

    # 바이비트 API 키 (해외 거래소)
    # 발급: Bybit 웹사이트 → API Management
    # 필요 권한: Read-Write (거래), Position (포지션 조회)
    BYBIT_API_KEY = os.getenv('BYBIT_API_KEY', '')
    BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET', '')

    # 환율 API: yfinance 사용 (API 키 불필요)
    # yfinance는 Yahoo Finance에서 무료로 USD/KRW 환율 제공
    # 24/7 실시간 환율, rate limit 없음


# ============================================================================
# 봇 기본 설정
# ============================================================================
class BotConfig:
    """
    봇 전반적인 설정

    거래 대상, 수익률 목표, 포지션 크기, 모의 거래 등을 설정합니다.
    모든 값은 .env 파일에서 변경 가능하며, 기본값이 제공됩니다.
    """

    # 타겟 코인 목록
    # 쉼표로 구분된 코인 심볼 리스트
    # 추천: SOL, XRP, DOGE, ADA (김프 변동이 크고 거래량이 많은 코인)
    # 비추천: BTC (김프 반영 느림), ETH (김프 작음)
    TARGET_COINS: List[str] = os.getenv('TARGET_COINS', 'SOL,XRP,DOGE,ADA').split(',')

    # 목표 수익률 (%)
    # 수수료 제외 전 목표 수익률
    # 예: 1.0% 설정 시, 수수료 0.25% 제외하면 실제 수익은 약 0.75%
    # 높게 설정: 진입 기회 감소, 안정성 증가
    # 낮게 설정: 진입 기회 증가, 리스크 증가
    TARGET_PROFIT_PERCENT = float(os.getenv('TARGET_PROFIT_PERCENT', '1.0'))

    # 최대 진입 김프 (%) - 역전 전략
    # 이 값 이하의 김프일 때만 진입 (낮은 김프에서 진입)
    # 기본값: 0.0% (김프가 0% 이하일 때 진입)
    # 예시:
    # - 0.0%: 김프가 0% 이하 (해외가 같거나 비쌀 때) 진입
    # - -0.5%: 김프가 -0.5% 이하 (해외가 최소 0.5% 더 비쌀 때) 진입
    # - 0.5%: 김프가 0.5% 이하 (약간 높은 김프에서도) 진입
    MIN_ENTRY_PREMIUM = float(os.getenv('MIN_ENTRY_PREMIUM', '0.0'))

    # 최대 포지션 크기 (코인당)
    # 업비트에서 현물 매수할 최대 금액 (원화)
    # 기본값: 1,000,000원 (100만원)
    # 주의: USDT 포지션과 완전 헷지를 위해 두 값 중 작은 쪽으로 실제 크기 결정
    MAX_POSITION_KRW = float(os.getenv('MAX_POSITION_KRW', '1000000'))

    # 바이비트에서 숏 포지션 잡을 최대 금액 (USDT)
    # 기본값: 1,000 USDT
    # 주의: KRW 포지션과 완전 헷지를 위해 두 값 중 작은 쪽으로 실제 크기 결정
    MAX_POSITION_USDT = float(os.getenv('MAX_POSITION_USDT', '1000'))

    # USDT 차익거래 임계값 (향후 구현)
    # USDT 프리미엄이 이 값(%) 이하일 때 업비트에서 USDT 매수
    # 예: 0.5% 이하일 때 싸게 사서 바이비트로 전송
    USDT_PREMIUM_BUY_THRESHOLD = float(os.getenv('USDT_PREMIUM_BUY_THRESHOLD', '0.5'))

    # USDT 프리미엄이 이 값(%) 이상일 때 업비트에서 USDT 매도
    # 예: 0.5% 이상일 때 바이비트에서 받아 업비트에서 비싸게 매도
    USDT_PREMIUM_SELL_THRESHOLD = float(os.getenv('USDT_PREMIUM_SELL_THRESHOLD', '0.5'))

    # 페이퍼 트레이딩 (모의 거래) 활성화 여부
    # true: 실제 API 호출 없이 시뮬레이션 (안전한 테스트)
    # false: 실제 돈으로 거래 (주의!)
    # 중요: 실거래 전 반드시 페이퍼 트레이딩으로 충분히 테스트하세요!
    PAPER_TRADING = os.getenv('PAPER_TRADING', 'true').lower() == 'true'

    # 페이퍼 트레이딩 초기 자본 (원화)
    # 모의 거래 시작 시 가상으로 보유할 KRW 금액
    # 기본값: 10,000,000원 (1천만원)
    PAPER_INITIAL_KRW = float(os.getenv('PAPER_INITIAL_KRW', '10000000'))

    # 페이퍼 트레이딩 초기 자본 (USDT)
    # 모의 거래 시작 시 가상으로 보유할 USDT 금액
    # 기본값: 10,000 USDT
    PAPER_INITIAL_USDT = float(os.getenv('PAPER_INITIAL_USDT', '10000'))

    # 고정 환율 적용
    # exchange_rate.py에서 해당 값이 None이 아니라면 해당 값을 환율로 사용한다.
    FIXED_EXCHANGE=os.getenv('FIXED_EXCHANGE', None)


# ============================================================================
# 거래 수수료 및 비용
# ============================================================================
class Fees:
    """
    거래소 수수료 및 관련 비용

    차익거래 수익률 계산에 필수적인 모든 수수료와 비용을 정의합니다.
    실제 거래소 수수료율을 정확히 반영해야 정확한 수익 계산이 가능합니다.
    """

    # 업비트 거래 수수료 (현물)
    # 매수/매도 모두 동일하게 0.05% 적용
    # VIP 등급에 따라 할인율이 다를 수 있으니 실제 등급 확인 필요
    UPBIT_TRADE_FEE = 0.0005  # 0.05%

    # 바이비트 거래 수수료 (선물)
    # Maker: 지정가 주문으로 유동성 공급 시 리베이트 (-0.025%)
    # Taker: 시장가 주문으로 즉시 체결 시 수수료 (0.075%)
    # 차익거래는 빠른 진입이 중요하므로 주로 Taker 수수료 발생
    BYBIT_MAKER_FEE = -0.00025  # -0.025% (리베이트)
    BYBIT_TAKER_FEE = 0.00075   # 0.075%

    # 출금 수수료
    # USDT를 TRON 네트워크(TRC20)로 출금 시 수수료
    # 업비트 → 바이비트 전송 또는 반대 방향 전송 시 발생
    USDT_WITHDRAWAL_TRC20 = 1.0  # 1 USDT 고정

    # 평균 펀딩비 (변동, 추정치)
    # 바이비트 무기한 선물의 8시간마다 발생하는 펀딩비
    # 롱 우세 시 숏 포지션이 펀딩비 수취 (추가 수익)
    # 숏 우세 시 숏 포지션이 펀딩비 지불 (추가 비용)
    # 평균 0.01%로 추정 (실제는 시장 상황에 따라 크게 변동)
    BYBIT_AVG_FUNDING_FEE = 0.0001  # 0.01% per 8시간 (대략적 추정치)

    # 슬리피지 버퍼
    # 시장가 주문 시 예상 가격과 실제 체결 가격의 차이
    # 호가창이 얇거나 포지션이 클수록 슬리피지 증가
    # 기본값: 0.1% (보수적 추정)
    SLIPPAGE_BUFFER = 0.001  # 0.1%

    @classmethod
    def total_entry_fee_percent(cls) -> float:
        """
        진입 시 총 수수료 (%)

        포지션 진입 시 발생하는 모든 수수료의 합계:
        - 업비트 현물 매수: 0.05%
        - 바이비트 선물 숏 진입: 0.075%
        = 총 0.125%

        Returns:
            float: 진입 시 총 수수료 (퍼센트)
        """
        return (cls.UPBIT_TRADE_FEE + cls.BYBIT_TAKER_FEE) * 100

    @classmethod
    def total_exit_fee_percent(cls) -> float:
        """
        청산 시 총 수수료 (%)

        포지션 청산 시 발생하는 모든 수수료의 합계:
        - 업비트 현물 매도: 0.05%
        - 바이비트 선물 숏 청산: 0.075%
        = 총 0.125%

        Returns:
            float: 청산 시 총 수수료 (퍼센트)
        """
        return (cls.UPBIT_TRADE_FEE + cls.BYBIT_TAKER_FEE) * 100

    @classmethod
    def total_round_trip_fee_percent(cls) -> float:
        """
        왕복 총 수수료 (%)

        진입 + 청산의 전체 사이클 수수료:
        - 진입: 0.125%
        - 청산: 0.125%
        = 총 0.25%

        이 값은 목표 수익률 계산 시 필수적으로 고려되어야 합니다.
        예: 1% 목표 수익을 원한다면, 실제로는 1.25% 이상의 김프가 필요합니다.

        Returns:
            float: 왕복 총 수수료 (퍼센트)
        """
        return cls.total_entry_fee_percent() + cls.total_exit_fee_percent()


# ============================================================================
# 리스크 관리 파라미터
# ============================================================================
class RiskParams:
    """
    리스크 관리 파라미터

    차익거래의 안정성을 보장하기 위한 모든 안전장치를 정의합니다.
    청산 위험, 슬리피지, 호가창 깊이 등을 체크하여 안전한 진입만 허용합니다.

    주의사항:
    - 너무 보수적으로 설정하면 진입 기회가 줄어듭니다
    - 너무 공격적으로 설정하면 청산 위험이 증가합니다
    - 기본값은 안전성을 우선으로 설정되어 있습니다
    """

    # 청산가 안전마진 (배수)
    # 청산가가 진입가의 N배 이상 멀리 떨어져 있어야 진입 허용
    # 1.7배: 가격이 약 70% 상승해야 청산 (레버리지 1배 기준 실제로는 ~2배)
    # 보수적: 2.0 (진입 기회 감소, 안정성 극대화)
    # 공격적: 1.5 (진입 기회 증가, 리스크 증가 - 비추천)
    LIQUIDATION_BUFFER = float(os.getenv('LIQUIDATION_BUFFER', '1.7'))

    # 최소 호가창 깊이 배수
    # 호가창에 포지션 크기의 N배 이상 물량이 있어야 진입 허용
    # 10배: 포지션 10개를 동시에 잡을 수 있는 유동성 필요
    # 목적: 슬리피지 최소화 및 긴급 청산 시 유동성 확보
    # 높게 설정: 진입 기회 감소, 슬리피지 감소
    # 낮게 설정: 진입 기회 증가, 슬리피지 증가
    MIN_ORDERBOOK_DEPTH_MULTIPLIER = float(os.getenv('MIN_ORDERBOOK_DEPTH_MULTIPLIER', '10'))

    # 최대 허용 슬리피지 (%)
    # 호가창 분석으로 계산한 예상 슬리피지가 이 값을 초과하면 진입 거부
    # 0.3%: 포지션 크기가 크거나 호가가 얇으면 진입 불가
    # 슬리피지가 크면 실제 수익이 예상보다 크게 감소할 수 있음
    MAX_SLIPPAGE_PERCENT = float(os.getenv('SLIPPAGE_BUFFER_PERCENT', '0.3'))

    # 손절 기준 (%)
    # 미실현 손실이 이 값 이하로 떨어지면 자동 손절
    # -5.0%: 5% 손실 시 강제 청산
    # 주의: 헷지 전략이므로 이론적으로 큰 손실은 발생하지 않음
    # 이 기능은 예상치 못한 긴급 상황(청산, 시스템 오류 등)을 위한 최후의 안전장치
    STOP_LOSS_PERCENT = float(os.getenv('STOP_LOSS_PERCENT', '-5.0'))

    # 청산 경고 거리 (배수)
    # 현재가가 청산가의 N배 이하로 근접하면 경고 로그 출력
    # 1.3배: 청산가까지 30% 이내 근접 시 경고
    # 아직 위험하지는 않지만 모니터링 강화 필요
    LIQUIDATION_WARNING_DISTANCE = float(os.getenv('LIQUIDATION_WARNING_DISTANCE', '1.3')) 

    # 청산 위험 거리 (배수)
    # 현재가가 청산가의 N배 이하로 근접하면 포지션 50% 축소
    # 1.2배: 청산가까지 20% 이내 근접 시 자동으로 반 청산
    # 완전 청산 전에 일부를 먼저 정리하여 리스크 감소
    LIQUIDATION_REDUCE_DISTANCE = float(os.getenv('LIQUIDATION_REDUCE_DISTANCE', '1.2')) 

    # 유지 증거금률 (MMR, Maintenance Margin Rate)
    # 바이비트에서 포지션 유지에 필요한 최소 증거금 비율
    # 증거금이 이 비율 이하로 떨어지면 강제 청산
    #
    # **중요**: MMR은 Bybit API에서 동적으로 조회하여 사용
    # API 엔드포인트: /v5/market/instruments-info (category=linear)
    # 각 코인의 'maintMarginRate' 필드 값 사용
    #
    # 아래 값들은 폴백(fallback)용 참고값으로만 사용
    # 실제 운영 시에는 API에서 조회한 실시간 MMR 사용
    MMR_DEFAULT = 0.005  # 0.5% (기본값, API 조회 실패 시)
    MMR_BY_COIN = {
        'BTC': 0.005,   # 0.5% (비트코인)
        'ETH': 0.005,   # 0.5% (이더리움)
        'SOL': 0.01,    # 1.0% (솔라나)
        'XRP': 0.01,    # 1.0% (리플)
        'DOGE': 0.02,   # 2.0% (도지코인)
        'ADA': 0.01,    # 1.0% (에이다)
    }

    @classmethod
    def get_mmr(cls, coin: str) -> float:
        """
        코인별 유지 증거금률(MMR) 반환 (폴백용)

        **중요**: 실제 운영 시에는 Bybit API에서 동적으로 조회하여 사용
        이 함수는 API 조회 실패 시 폴백으로만 사용

        Args:
            coin: 코인 심볼 (예: 'SOL', 'XRP')

        Returns:
            float: 해당 코인의 MMR (소수점, 예: 0.01 = 1%)
                   정의되지 않은 코인은 기본값(0.5%) 반환
        """
        return cls.MMR_BY_COIN.get(coin.upper(), cls.MMR_DEFAULT)


# ============================================================================
# 바이비트 선물 거래 설정
# ============================================================================
class BybitSettings:
    """
    바이비트 선물 거래 설정

    레버리지, 마진 모드, 포지션 인덱스 등 바이비트 특유의 설정을 관리합니다.

    중요사항:
    - 레버리지는 1배(무레버리지) 권장 (청산 위험 최소화)
    - 마진 모드는 ISOLATED(격리) 권장 (포지션별 독립 증거금)
    """

    # 레버리지 (배수)
    # **고정값: 1배 (무레버리지)**
    # 본 전략은 레버리지 1배만 사용하도록 설계됨
    # 절대 변경하지 마세요! 레버리지를 높이면 청산 위험 급증
    # 환경 변수로 변경 가능하지만, 1배 이외의 값은 비권장
    LEVERAGE = int(os.getenv('BYBIT_LEVERAGE', '1'))

    # 레버리지 검증
    if LEVERAGE != 1:
        import warnings
        warnings.warn(
            f"경고: 레버리지가 {LEVERAGE}배로 설정되어 있습니다. "
            f"본 전략은 1배 레버리지 전용으로 설계되었습니다. "
            f"청산 위험이 크게 증가할 수 있습니다!",
            UserWarning
        )

    # 마진 모드
    # ISOLATED (격리 마진): 각 포지션이 독립적인 증거금 사용 (권장)
    #   - 장점: 한 포지션 청산되어도 다른 포지션 영향 없음
    #   - 단점: 각 포지션마다 별도 증거금 필요
    # CROSS (교차 마진): 전체 잔고를 증거금으로 사용 (비추천)
    #   - 장점: 증거금 효율적 활용
    #   - 단점: 한 포지션 문제로 전체 잔고 손실 위험
    MARGIN_MODE = os.getenv('BYBIT_MARGIN_MODE', 'ISOLATED')

    # 포지션 모드 인덱스 (바이비트 API 전용)
    # 바이비트는 포지션을 인덱스로 관리:
    # 0 = One-Way Mode (단방향 모드)
    # 1 = Hedge Mode Long (헷지 모드 롱)
    # 2 = Hedge Mode Short (헷지 모드 숏)
    # 이 봇은 항상 숏 포지션만 사용하므로 2번 고정
    POSITION_IDX_SHORT = 0


# ============================================================================
# 스캐너 설정
# ============================================================================
class ScannerConfig:
    """
    시장 스캐너 설정

    Layer 1 스캐너의 동작을 제어합니다.
    스캔 주기, 최소 진입 김프, 최대 동시 실행 전략 수 등을 정의합니다.
    """

    # 시장 스캔 주기 (초)
    # N초마다 모든 타겟 코인의 가격을 조회하여 김프 계산
    # 10초: 기본값, API Rate Limit를 고려한 안전한 주기
    # 주의: 너무 짧게 설정하면 거래소 API Rate Limit 초과 가능
    SCAN_INTERVAL = int(os.getenv('SCAN_INTERVAL', '10'))

    # 최대 동시 실행 전략 수
    # 동시에 활성화될 수 있는 최대 코인 전략 개수
    # 3개: 리스크 분산 및 자본 효율의 균형
    # 주의:
    # - 너무 많으면: 자본 분산으로 개별 포지션이 작아짐, 관리 복잡
    # - 너무 적으면: 기회 놓침, 자본 효율 저하
    MAX_CONCURRENT_STRATEGIES = int(os.getenv('MAX_CONCURRENT_STRATEGIES', '3'))

    # 전략 모니터링 주기 (초) - Layer 2
    # 포지션 진입 후 N초마다 가격 확인 및 상태 체크
    # 2초: 빠른 김프 변화 감지 (기존 10초에서 개선)
    #
    # 변경 이유:
    # - 김프는 시장 상황에 따라 빠르게 변할 수 있음 (급등락 시 1-5%/분)
    # - 목표 수익 도달 시점을 놓치지 않기 위해 짧은 인터벌 필요
    # - 손절 및 청산 위험 감지 지연 최소화
    #
    # API 레이트 리미트 영향:
    # - 전략 1개당: 2회(upbit+bybit) / 2초 = 1회/초
    # - 전략 3개: 3회/초 (업비트 한도 10회/초, 바이비트 50회/초 대비 안전)
    #
    # 주의: 1초 미만으로 설정 시 레이트 리미트 위험
    MONITORING_INTERVAL = int(os.getenv('MONITORING_INTERVAL', '2'))


# ============================================================================
# OHLC Data Collection Settings
# ============================================================================
class OHLCConfig:
    """
    OHLC 데이터 수집 설정

    김치 프리미엄의 일봉(OHLC) 데이터를 수집하여 추후 분석 및 백테스팅에 활용합니다.
    """

    # OHLC 데이터 수집 활성화 여부
    # true: OHLC 수집기 실행 (독립 스레드)
    # false: OHLC 수집 비활성화
    ENABLED = os.getenv('OHLC_ENABLED', 'true').lower() == 'true'

    # OHLC 수집 대상 코인
    # None이면 BotConfig.TARGET_COINS 사용
    # 쉼표로 구분된 리스트로 별도 지정 가능
    TARGET_COINS_OVERRIDE = os.getenv('OHLC_TARGET_COINS', None)

    @classmethod
    def get_target_coins(cls) -> List[str]:
        """OHLC 수집 대상 코인 목록 반환"""
        if cls.TARGET_COINS_OVERRIDE:
            return cls.TARGET_COINS_OVERRIDE.split(',')
        return BotConfig.TARGET_COINS

    # OHLC 타임프레임 (쉼표로 구분)
    # 기본값: '1d' (일봉)
    # 향후 확장: '1h,4h,1d' (1시간봉, 4시간봉, 일봉)
    TIMEFRAMES = os.getenv('OHLC_TIMEFRAMES', '1d').split(',')

    # OHLC 업데이트 주기 (초)
    # market_scans 테이블에서 데이터를 읽어 OHLC 캔들을 업데이트하는 주기
    # 60초: 1분마다 캔들 업데이트 (권장)
    # 주의: 너무 짧으면 DB 부하 증가, 너무 길면 데이터 지연
    UPDATE_INTERVAL = int(os.getenv('OHLC_UPDATE_INTERVAL', '60'))

    # OHLC 데이터 보관 기간 (일)
    # 이 기간보다 오래된 데이터는 자동 삭제 (0 = 무제한)
    # 기본값: 365일 (1년)
    RETENTION_DAYS = int(os.getenv('OHLC_RETENTION_DAYS', '365'))

    # KST 타임존 오프셋 (시간)
    # KST = UTC+9
    KST_OFFSET_HOURS = 9


# ============================================================================
# 로깅 설정
# ============================================================================
class LogConfig:
    """
    로깅 설정

    봇의 모든 동작을 기록하는 로깅 시스템 설정입니다.
    디버깅, 모니터링, 감사를 위해 로그가 필수적입니다.
    """

    # 로그 레벨
    # DEBUG: 모든 상세 로그 출력 (개발 및 디버깅용)
    # INFO: 일반 정보 로그 출력 (운영 환경 권장)
    # WARNING: 경고 이상만 출력 (문제 발생 시만)
    # ERROR: 에러만 출력 (최소 로그)
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()

    # 파일 로그 활성화 여부
    # true: logs/bot.log 파일에 로그 저장 (권장)
    # false: 콘솔에만 출력
    # 주의: 운영 환경에서는 반드시 파일 로그 활성화 필요 (추후 분석용)
    LOG_TO_FILE = os.getenv('LOG_TO_FILE', 'true').lower() == 'true'

    # 로그 디렉토리
    # 로그 파일이 저장될 디렉토리 경로
    LOG_DIR = 'logs'

    # 로그 파일명
    # 메인 로그 파일 이름
    LOG_FILE = 'bot.log'

    # 로그 파일 최대 크기 (MB)
    # 로그 파일이 이 크기를 초과하면 자동으로 롤오버
    # 10MB: 일반적으로 수천~수만 줄의 로그 저장 가능
    MAX_LOG_SIZE_MB = 10

    # 로그 파일 백업 개수
    # 롤오버 시 유지할 백업 파일 개수
    # 5개: 총 50MB (10MB × 5개)의 로그 히스토리 유지
    BACKUP_COUNT = 5


# ============================================================================
# 데이터베이스 설정
# ============================================================================
class DBConfig:
    """
    데이터베이스 설정

    전략 상태, 거래 내역, 펀딩비 등 모든 데이터를 저장하는 DB 설정입니다.
    SQLite를 기본으로 사용하며, 필요 시 MySQL로 전환 가능합니다.
    """

    # 데이터베이스 파일 경로
    # SQLite 파일 경로 (상대 경로 또는 절대 경로)
    # 기본값: 'kimp_bot.db' (프로젝트 루트에 생성)
    # 예: '/var/data/kimp_bot.db' (절대 경로)
    DB_PATH = os.getenv('DB_PATH', 'kimp_bot.db')


# ============================================================================
# 상태 머신 상태 정의
# ============================================================================
class States:
    """
    전략 실행 상태 정의

    각 전략 인스턴스는 정의된 상태를 따라 진행됩니다.
    상태 전이는 엄격하게 제어되며, 허용되지 않은 전이는 에러를 발생시킵니다.

    정상 흐름:
    INIT → CHECK_CAPITAL → VALIDATE_ENTRY → ENTERING → HEDGED → MONITORING
    → PROFITABLE → EXITING → SETTLEMENT → EXITED

    비정상 흐름 (위험 상황):
    MONITORING → REDUCE_POSITION (포지션 축소)
    MONITORING → STOP_LOSS (손절)
    MONITORING → LIQUIDATION_RISK (청산 위험)
    Any → ERROR (예상치 못한 에러)
    """

    # 초기화 상태
    # 전략 인스턴스가 생성되고 초기 설정을 하는 상태
    INIT = "INIT"

    # 자본 확인 상태
    # 업비트(KRW)와 바이비트(USDT) 잔고를 확인하는 상태
    CHECK_CAPITAL = "CHECK_CAPITAL"

    # 기회 스캔 상태 (사용 안 함 - Layer 1에서 처리)
    # Layer 2 전략은 이미 기회를 발견한 후 생성되므로 이 상태는 건너뜀
    # SCAN_OPPORTUNITY = "SCAN_OPPORTUNITY"  # 제거: 핸들러 미구현, 실제 흐름에서 미사용

    # 진입 검증 상태
    # 청산 안전성, 호가창 깊이, 슬리피지 등을 검증하는 상태
    VALIDATE_ENTRY = "VALIDATE_ENTRY"

    # 진입 중 상태
    # 업비트 매수 + 바이비트 숏 포지션을 동시에 진입하는 상태
    ENTERING = "ENTERING"

    # 헷지 완료 상태
    # 양쪽 포지션이 모두 체결되어 헷지가 완성된 상태
    HEDGED = "HEDGED"

    # 모니터링 상태
    # 김프가 수렴하여 목표 수익에 도달할 때까지 대기하는 상태
    # 10초마다 가격 체크 및 청산 위험 감시
    MONITORING = "MONITORING"

    # 수익 달성 상태
    # 목표 수익에 도달하여 청산 준비를 하는 상태
    PROFITABLE = "PROFITABLE"

    # 포지션 축소 상태
    # 청산가에 근접하여 포지션의 50%를 먼저 정리하는 상태
    REDUCE_POSITION = "REDUCE_POSITION"

    # 손절 상태
    # 예상치 못한 큰 손실(-5% 이하)이 발생하여 강제 청산하는 상태
    STOP_LOSS = "STOP_LOSS"

    # 청산 위험 상태
    # 청산가에 매우 근접하여 긴급 대응이 필요한 상태
    LIQUIDATION_RISK = "LIQUIDATION_RISK"

    # 청산 중 상태
    # 업비트 매도 + 바이비트 숏 청산을 진행하는 상태
    EXITING = "EXITING"

    # 정산 상태
    # 거래가 완료되고 최종 손익을 계산하는 상태
    SETTLEMENT = "SETTLEMENT"

    # 종료 상태
    # 전략이 성공적으로 완료된 상태 (터미널 상태)
    EXITED = "EXITED"

    # 에러 상태
    # 예상치 못한 에러가 발생한 상태 (터미널 상태)
    ERROR = "ERROR"


# ============================================================================
# 설정 검증
# ============================================================================
def validate_config():
    """
    봇 시작 시 설정 검증

    환경 변수와 설정값이 올바른지 확인합니다.
    필수 값이 누락되었거나 잘못된 값이 있으면 에러를 발생시킵니다.

    검증 항목:
    - API 키 (실거래 시 필수)
    - 타겟 코인 목록
    - 포지션 크기 제한

    Raises:
        ValueError: 설정 오류가 있을 경우

    Returns:
        bool: 검증 성공 시 True
    """
    errors = []

    # API 키 확인 (실거래 시에만)
    if not BotConfig.PAPER_TRADING:
        # 업비트 API 키
        if not APIKeys.UPBIT_ACCESS_KEY or not APIKeys.UPBIT_SECRET_KEY:
            errors.append("업비트 API 키가 설정되지 않았습니다 (UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY)")

        # 바이비트 API 키
        if not APIKeys.BYBIT_API_KEY or not APIKeys.BYBIT_API_SECRET:
            errors.append("바이비트 API 키가 설정되지 않았습니다 (BYBIT_API_KEY, BYBIT_API_SECRET)")

    # 환율 API: yfinance 사용 (API 키 불필요, 검증 생략)

    # 타겟 코인 확인
    if not BotConfig.TARGET_COINS:
        errors.append("타겟 코인이 지정되지 않았습니다 (TARGET_COINS)")

    # 포지션 크기 제한 확인
    if BotConfig.MAX_POSITION_KRW <= 0:
        errors.append("MAX_POSITION_KRW는 양수여야 합니다")
    if BotConfig.MAX_POSITION_USDT <= 0:
        errors.append("MAX_POSITION_USDT는 양수여야 합니다")

    # 에러가 있으면 예외 발생
    if errors:
        error_msg = "설정 오류가 발견되었습니다:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(error_msg)

    return True


# ============================================================================
# EXPORTS
# ============================================================================
__all__ = [
    'APIKeys',
    'BotConfig',
    'Fees',
    'RiskParams',
    'BybitSettings',
    'ScannerConfig',
    'OHLCConfig',
    'LogConfig',
    'DBConfig',
    'States',
    'validate_config'
]


# ============================================================================
# 데이터베이스 기반 설정 (선택적)
# ============================================================================

# CONFIG_SOURCE 환경 변수 확인
_CONFIG_SOURCE = os.getenv('CONFIG_SOURCE', 'env').lower()

if _CONFIG_SOURCE == 'db':
    # DB 기반 설정 활성화
    try:
        # Save original classes before replacement
        _BotConfig_original = BotConfig
        _RiskParams_original = RiskParams
        _ScannerConfig_original = ScannerConfig

        from config.config_manager import (
            BotConfig as _BotConfigDB,
            RiskParams as _RiskParamsDB,
            ScannerConfig as _ScannerConfigDB,
        )

        # Replace classes with DB-backed proxies
        # 이렇게 하면 기존 import 구문이 proxy 클래스를 가져옴
        BotConfig = _BotConfigDB
        RiskParams = _RiskParamsDB
        ScannerConfig = _ScannerConfigDB

        print("✓ DB 기반 설정 활성화됨")

    except Exception as e:
        # DB 로드 실패 시 .env로 폴백
        import traceback
        print(f"경고: DB 설정 로드 실패, .env로 폴백: {e}")
        traceback.print_exc()
        # Keep original classes (already defined above)

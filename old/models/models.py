"""
Database models for KIMP arbitrage bot
"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey, Boolean, UniqueConstraint, Index
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from models.database import Base


class Strategy(Base):
    """
    전략 실행 기록 (메인 테이블)
    하나의 전략 = 한 코인에 대한 진입부터 청산까지의 전체 사이클
    """
    __tablename__ = 'strategies'

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin = Column(String(20), nullable=False, index=True)
    state = Column(String(50), nullable=False, index=True)  # 상태머신 상태

    # 타임스탬프
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
    exited_at = Column(DateTime, nullable=True)

    # 진입 정보
    entry_premium = Column(Float, nullable=True)  # 진입 시 김프 (%)
    entry_price_krw = Column(Float, nullable=True)  # 업비트 진입가 (KRW)
    entry_price_usd = Column(Float, nullable=True)  # 바이비트 진입가 (USD)
    entry_amount = Column(Float, nullable=True)  # 진입 수량
    entry_value_krw = Column(Float, nullable=True)  # 진입 금액 (KRW 기준)
    exchange_rate = Column(Float, nullable=True)  # 진입 시 환율
    usdt_krw = Column(Float, nullable=True)  # 진입 시 USDT/KRW 가격

    # 포지션 정보
    upbit_order_id = Column(String(100), nullable=True)  # 업비트 주문 ID
    bybit_order_id = Column(String(100), nullable=True)  # 바이비트 주문 ID
    bybit_position_id = Column(String(100), nullable=True)  # 바이비트 포지션 ID
    leverage = Column(Integer, default=1, nullable=False)  # 레버리지
    liquidation_price = Column(Float, nullable=True)  # 청산가
    initial_margin = Column(Float, nullable=True)  # 초기 증거금

    # 수익 정보
    target_premium = Column(Float, nullable=True)  # 목표 김프 (%)
    current_premium = Column(Float, nullable=True)  # 현재 김프 (%)
    funding_fee_earned = Column(Float, default=0.0, nullable=False)  # 누적 펀딩비 수익
    total_fees_paid = Column(Float, default=0.0, nullable=False)  # 총 지불 수수료

    # 청산 정보
    exit_premium = Column(Float, nullable=True)  # 청산 시 김프 (%)
    exit_price_krw = Column(Float, nullable=True)  # 업비트 청산가
    exit_price_usd = Column(Float, nullable=True)  # 바이비트 청산가
    exit_value_krw = Column(Float, nullable=True)  # 청산 금액 (KRW 기준)
    realized_pnl_krw = Column(Float, nullable=True)  # 실현 손익 (KRW)
    realized_pnl_percent = Column(Float, nullable=True)  # 실현 손익률 (%)

    # 종료 이유
    exit_reason = Column(String(100), nullable=True)  # PROFITABLE, STOP_LOSS, LIQUIDATION_RISK, etc.

    # 페이퍼 트레이딩 여부
    is_paper_trading = Column(Boolean, default=False, nullable=False)

    # 관계: 이 전략에 속한 모든 거래
    trades = relationship("Trade", back_populates="strategy", cascade="all, delete-orphan")
    funding_fees = relationship("FundingFee", back_populates="strategy", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Strategy(id={self.id}, coin={self.coin}, state={self.state}, pnl={self.realized_pnl_krw})>"


class Trade(Base):
    """
    개별 거래 로그
    각 매수/매도/진입/청산 거래를 기록
    """
    __tablename__ = 'trades'

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(Integer, ForeignKey('strategies.id'), nullable=True, index=True)

    # 거래 정보
    exchange = Column(String(20), nullable=False)  # upbit, bybit
    side = Column(String(20), nullable=False)  # buy, sell, open_short, close_short
    coin = Column(String(20), nullable=False)
    amount = Column(Float, nullable=False)  # 거래 수량
    price = Column(Float, nullable=False)  # 거래 가격
    value = Column(Float, nullable=False)  # 거래 금액 (가격 * 수량)
    fee = Column(Float, nullable=False)  # 수수료
    fee_currency = Column(String(10), nullable=False)  # 수수료 통화 (KRW, USDT)

    # 주문 정보
    order_id = Column(String(100), nullable=True)  # 거래소 주문 ID
    order_type = Column(String(20), nullable=True)  # market, limit

    # 슬리피지 정보
    expected_price = Column(Float, nullable=True)  # 예상 가격
    slippage = Column(Float, nullable=True)  # 슬리피지 (%)

    # 타임스탬프
    executed_at = Column(DateTime, default=func.now(), nullable=False)

    # 페이퍼 트레이딩 여부
    is_paper_trading = Column(Boolean, default=False, nullable=False)

    # 관계
    strategy = relationship("Strategy", back_populates="trades")

    def __repr__(self):
        return f"<Trade(id={self.id}, exchange={self.exchange}, side={self.side}, coin={self.coin}, amount={self.amount})>"


class FundingFee(Base):
    """
    펀딩비 수익 기록
    바이비트 선물에서 8시간마다 발생하는 펀딩비 기록
    """
    __tablename__ = 'funding_fees'

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(Integer, ForeignKey('strategies.id'), nullable=False, index=True)

    # 펀딩비 정보
    coin = Column(String(20), nullable=False)
    funding_rate = Column(Float, nullable=False)  # 펀딩비율 (%)
    position_value = Column(Float, nullable=False)  # 포지션 가치 (USDT)
    funding_fee = Column(Float, nullable=False)  # 받은/지불한 펀딩비 (USDT, + = 수익, - = 비용)
    funding_fee_krw = Column(Float, nullable=False)  # 펀딩비 (KRW 환산)

    # 타임스탬프
    collected_at = Column(DateTime, default=func.now(), nullable=False)

    # 관계
    strategy = relationship("Strategy", back_populates="funding_fees")

    def __repr__(self):
        return f"<FundingFee(id={self.id}, coin={self.coin}, fee={self.funding_fee} USDT)>"


class MarketScan(Base):
    """
    시장 스캔 로그 (선택적)
    메인 스캐너가 시장을 스캔한 결과를 주기적으로 기록
    추후 데이터 분석 및 전략 개선에 활용
    """
    __tablename__ = 'market_scans'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 스캔 정보
    coin = Column(String(20), nullable=False, index=True)
    premium = Column(Float, nullable=False)  # 김프 (%)

    # 가격 정보
    krw_price = Column(Float, nullable=False)  # 업비트 가격 (KRW)
    usd_price = Column(Float, nullable=False)  # 바이비트 가격 (USD)
    usdt_krw = Column(Float, nullable=False)  # USDT/KRW 가격
    exchange_rate = Column(Float, nullable=False)  # USD/KRW 환율

    # 진입 가능 여부
    is_opportunity = Column(Boolean, default=False, nullable=False)  # 진입 기회 여부
    opportunity_reason = Column(String(200), nullable=True)  # 진입 불가 이유

    # 호가창 정보
    orderbook_depth_krw = Column(Float, nullable=True)  # 업비트 호가 깊이
    orderbook_depth_usd = Column(Float, nullable=True)  # 바이비트 호가 깊이

    # 타임스탬프
    scanned_at = Column(DateTime, default=func.now(), nullable=False, index=True)

    def __repr__(self):
        return f"<MarketScan(coin={self.coin}, premium={self.premium}%, at={self.scanned_at})>"


class BotLog(Base):
    """
    봇 운영 로그
    에러, 경고, 중요 이벤트 등을 DB에 기록
    파일 로그와 별도로 DB에도 저장하여 쿼리 가능하게 함
    """
    __tablename__ = 'bot_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 로그 정보
    level = Column(String(20), nullable=False, index=True)  # INFO, WARNING, ERROR, CRITICAL
    message = Column(Text, nullable=False)
    module = Column(String(100), nullable=True)  # 로그 발생 모듈

    # 관련 정보
    strategy_id = Column(Integer, nullable=True, index=True)  # 관련 전략 ID
    coin = Column(String(20), nullable=True, index=True)

    # 에러 상세 (에러일 경우)
    error_type = Column(String(100), nullable=True)
    error_trace = Column(Text, nullable=True)

    # 타임스탬프
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)

    def __repr__(self):
        return f"<BotLog(level={self.level}, message={self.message[:50]})>"


class PremiumOHLC(Base):
    """
    프리미엄 OHLC 데이터 (일봉, 추후 시간봉 확장 가능)

    김치프리미엄의 시간대별 OHLC 데이터를 저장하여 추후 분석 및 백테스팅에 활용
    """
    __tablename__ = 'premium_ohlc'

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False, default='1d')  # '1h', '4h', '1d'
    date_kst = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD in KST

    # Premium OHLC (%)
    premium_open = Column(Float, nullable=True)
    premium_high = Column(Float, nullable=True)
    premium_low = Column(Float, nullable=True)
    premium_close = Column(Float, nullable=True)

    # KRW Price OHLC (Upbit)
    krw_open = Column(Float, nullable=True)
    krw_high = Column(Float, nullable=True)
    krw_low = Column(Float, nullable=True)
    krw_close = Column(Float, nullable=True)

    # USD Price OHLC (Bybit)
    usd_open = Column(Float, nullable=True)
    usd_high = Column(Float, nullable=True)
    usd_low = Column(Float, nullable=True)
    usd_close = Column(Float, nullable=True)

    # Exchange Rate OHLC (USD/KRW)
    exchange_rate_open = Column(Float, nullable=True)
    exchange_rate_high = Column(Float, nullable=True)
    exchange_rate_low = Column(Float, nullable=True)
    exchange_rate_close = Column(Float, nullable=True)

    # Metadata
    scan_count = Column(Integer, default=0, nullable=False)  # Number of scans used to build candle
    is_finalized = Column(Boolean, default=False, nullable=False)  # True when day is complete
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    # Composite unique constraint
    __table_args__ = (
        UniqueConstraint('coin', 'timeframe', 'date_kst', name='uq_coin_timeframe_date'),
        Index('idx_premium_ohlc_coin_date', 'coin', 'date_kst'),
        Index('idx_premium_ohlc_timeframe', 'timeframe', 'date_kst'),
        Index('idx_premium_ohlc_finalized', 'is_finalized', 'date_kst'),
    )

    def __repr__(self):
        return f"<PremiumOHLC(coin={self.coin}, date={self.date_kst}, premium_close={self.premium_close}%)>"


class ConfigSetting(Base):
    """
    봇 설정 저장소 (DB 기반 설정)

    CONFIG_SOURCE=db일 때 .env 대신 이 테이블에서 설정 로드
    """
    __tablename__ = 'config_settings'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 설정 정보
    config_class = Column(String(50), nullable=False, index=True)  # 'BotConfig', 'RiskParams' 등
    key = Column(String(100), nullable=False, index=True)  # 'TARGET_COINS', 'MAX_POSITION_KRW' 등
    value = Column(Text, nullable=False)  # JSON-encoded value
    value_type = Column(String(20), nullable=False)  # 'str', 'int', 'float', 'bool', 'list'

    # 메타데이터
    description = Column(Text, nullable=True)  # 설정 설명 (한글)
    default_value = Column(Text, nullable=True)  # 기본값 (참고용)

    # 버전 관리
    version = Column(Integer, default=1, nullable=False)
    updated_by = Column(String(50), default='system', nullable=False)  # 'system', 'admin', 'auto'

    # 타임스탬프
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    # 유니크 제약: (config_class, key) 조합은 유일
    __table_args__ = (
        UniqueConstraint('config_class', 'key', name='uq_config_class_key'),
        Index('idx_config_class_key', 'config_class', 'key'),
    )

    def __repr__(self):
        return f"<ConfigSetting(class={self.config_class}, key={self.key}, value={self.value})>"

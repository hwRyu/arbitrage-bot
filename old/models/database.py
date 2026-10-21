"""
Database configuration and session management
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session
from dotenv import load_dotenv

load_dotenv()

# Database path from env or default
DB_PATH = os.getenv('DB_PATH', 'kimp_bot.db')
DATABASE_URL = f'sqlite:///{DB_PATH}'

# Create engine
# check_same_thread=False is needed for SQLite with multi-threading
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False  # Set to True for SQL debugging
)

# Session factory
SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))

# Base class for models
Base = declarative_base()


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize database - create all tables"""
    from models.models import Strategy, Trade, MarketScan, FundingFee, BotLog, PremiumOHLC, ConfigSetting
    Base.metadata.create_all(bind=engine)
    print(f"Database initialized at: {DB_PATH}")

    # Auto-seed if CONFIG_SOURCE=db and DB is empty
    if os.getenv('CONFIG_SOURCE', 'env').lower() == 'db':
        seed_db_config_from_env()


def close_db():
    """Close database connection"""
    SessionLocal.remove()


def seed_db_config_from_env():
    """
    .env 설정을 DB로 마이그레이션

    CONFIG_SOURCE=db로 전환 시 초기 DB 설정 생성
    """
    from models.models import ConfigSetting
    import json

    # Import original classes to get .env values
    # Note: We must import BEFORE config_manager modifies them
    import sys
    if 'config.settings' in sys.modules:
        # Already imported - get from module
        settings_module = sys.modules['config.settings']
        _BotConfig = getattr(settings_module, 'BotConfig')
        _RiskParams = getattr(settings_module, 'RiskParams')
        _ScannerConfig = getattr(settings_module, 'ScannerConfig')
    else:
        # Not imported yet - safe to import
        from config.settings import BotConfig as _BotConfig, RiskParams as _RiskParams, ScannerConfig as _ScannerConfig

    db = SessionLocal()
    try:
        # Check if already seeded
        count = db.query(ConfigSetting).count()
        if count > 0:
            print(f"DB 설정 이미 존재 ({count}개), 시드 생략")
            return

        settings_to_seed = [
            # BotConfig
            ('BotConfig', 'TARGET_COINS', _BotConfig.TARGET_COINS, 'list', '타겟 코인 목록'),
            ('BotConfig', 'TARGET_PROFIT_PERCENT', _BotConfig.TARGET_PROFIT_PERCENT, 'float', '목표 수익률 (%)'),
            ('BotConfig', 'MIN_ENTRY_PREMIUM', _BotConfig.MIN_ENTRY_PREMIUM, 'float', '최대 진입 김프 (%)'),
            ('BotConfig', 'MAX_POSITION_KRW', _BotConfig.MAX_POSITION_KRW, 'float', '최대 포지션 (KRW)'),
            ('BotConfig', 'MAX_POSITION_USDT', _BotConfig.MAX_POSITION_USDT, 'float', '최대 포지션 (USDT)'),

            # RiskParams
            ('RiskParams', 'LIQUIDATION_BUFFER', _RiskParams.LIQUIDATION_BUFFER, 'float', '청산가 안전마진'),
            ('RiskParams', 'MIN_ORDERBOOK_DEPTH_MULTIPLIER', _RiskParams.MIN_ORDERBOOK_DEPTH_MULTIPLIER, 'float', '최소 호가창 깊이 배수'),
            ('RiskParams', 'STOP_LOSS_PERCENT', _RiskParams.STOP_LOSS_PERCENT, 'float', '손절 기준 (%)'),

            # ScannerConfig
            ('ScannerConfig', 'SCAN_INTERVAL', _ScannerConfig.SCAN_INTERVAL, 'int', '스캔 주기 (초)'),
            ('ScannerConfig', 'MAX_CONCURRENT_STRATEGIES', _ScannerConfig.MAX_CONCURRENT_STRATEGIES, 'int', '최대 동시 전략 수'),
            ('ScannerConfig', 'MONITORING_INTERVAL', _ScannerConfig.MONITORING_INTERVAL, 'int', '모니터링 주기 (초)'),
        ]

        for config_class, key, value, value_type, description in settings_to_seed:
            # Serialize value
            if value_type == 'list':
                value_str = json.dumps(value)
            else:
                value_str = str(value)

            setting = ConfigSetting(
                config_class=config_class,
                key=key,
                value=value_str,
                value_type=value_type,
                description=description,
                default_value=value_str,
                updated_by='seed'
            )
            db.add(setting)

        db.commit()
        print(f"{len(settings_to_seed)}개 설정을 DB로 마이그레이션 완료")

    except Exception as e:
        db.rollback()
        print(f"DB 설정 시드 실패: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()

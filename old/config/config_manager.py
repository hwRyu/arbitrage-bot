"""
설정 관리자 - DB 기반 동적 설정 로딩

CONFIG_SOURCE 환경 변수에 따라 .env 또는 DB에서 설정 로드
기존 코드 수정 없이 투명하게 동작
"""

import os
import json
import time
import threading
from typing import Any, Dict, Optional


class ConfigManager:
    """
    설정 관리자 싱글톤

    CONFIG_SOURCE에 따라 .env 또는 DB에서 설정 로드
    실시간 변경 감지 (5초 주기)
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._config_source = os.getenv('CONFIG_SOURCE', 'env').lower()
        self._db_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_timestamp = None
        self._last_check_time = 0
        self._check_interval = 5  # 5초마다 DB 변경 감지

        # Simple logging (utils.logger may not be available yet during import)
        print(f"ConfigManager 초기화: CONFIG_SOURCE={self._config_source}")

        if self._config_source == 'db':
            self._load_db_config()

        self._initialized = True

    def _load_db_config(self):
        """DB에서 설정 로드"""
        from models.database import SessionLocal
        from models.models import ConfigSetting
        from sqlalchemy.orm import Session

        try:
            db: Session = SessionLocal()
            settings = db.query(ConfigSetting).all()

            if not settings:
                print("경고: DB 설정이 비어있습니다. .env 값으로 폴백합니다.")
                return

            # Build cache: {config_class: {key: value}}
            self._db_cache.clear()
            for setting in settings:
                if setting.config_class not in self._db_cache:
                    self._db_cache[setting.config_class] = {}

                # Deserialize value based on type
                value = self._deserialize_value(setting.value, setting.value_type)
                self._db_cache[setting.config_class][setting.key] = value

            self._cache_timestamp = time.time()
            print(f"DB 설정 로드 완료: {len(settings)}개 설정")

        except Exception as e:
            print(f"에러: DB 설정 로드 실패: {e}, .env로 폴백")
            self._config_source = 'env'
        finally:
            db.close()

    def get_config_value(self, config_class_name: str, key: str, default: Any) -> Any:
        """
        설정 값 조회

        Args:
            config_class_name: 설정 클래스 이름 ('BotConfig', 'RiskParams' 등)
            key: 설정 키 ('TARGET_COINS', 'MAX_POSITION_KRW' 등)
            default: 기본값 (.env에서 로드된 값)

        Returns:
            설정 값 (DB 또는 .env)
        """
        if self._config_source == 'env':
            return default

        # Check if we need to reload (every 5 seconds)
        current_time = time.time()
        if current_time - self._last_check_time >= self._check_interval:
            self._load_db_config()
            self._last_check_time = current_time

        # Get from DB cache
        if config_class_name in self._db_cache:
            if key in self._db_cache[config_class_name]:
                return self._db_cache[config_class_name][key]

        # Fallback to .env default
        # print(f"디버그: DB에 {config_class_name}.{key} 없음, .env 값 사용: {default}")
        return default

    def _deserialize_value(self, value: str, value_type: str) -> Any:
        """값 역직렬화"""
        try:
            if value_type == 'str':
                return value
            elif value_type == 'int':
                return int(value)
            elif value_type == 'float':
                return float(value)
            elif value_type == 'bool':
                return value.lower() in ('true', '1', 'yes')
            elif value_type == 'list':
                return json.loads(value)
            else:
                print(f"경고: 알 수 없는 value_type: {value_type}, 문자열로 반환")
                return value
        except Exception as e:
            print(f"에러: 값 역직렬화 실패: {value} (type={value_type}), 에러: {e}")
            return value

    def reload_config(self):
        """설정 강제 리로드 (수동 호출용)"""
        if self._config_source == 'db':
            self._load_db_config()
            print("DB 설정 수동 리로드 완료")


# Singleton instance (module-level)
_config_manager = ConfigManager()


# ============================================================================
# Proxy Classes
# ============================================================================

# Import original config classes (before potential modification)
# We need to be careful about import order to avoid circular dependencies
import sys
if 'config.settings' in sys.modules:
    # settings.py already loaded - get classes from module
    settings_module = sys.modules['config.settings']
    _BotConfig = getattr(settings_module, 'BotConfig', None)
    _RiskParams = getattr(settings_module, 'RiskParams', None)
    _ScannerConfig = getattr(settings_module, 'ScannerConfig', None)
else:
    # Not loaded yet - this should be the normal case
    # settings.py will import this module at the end
    _BotConfig = None
    _RiskParams = None
    _ScannerConfig = None


class BotConfig:
    """
    BotConfig 프록시 - DB 또는 .env에서 동적 로드

    기존 코드에서 BotConfig.TARGET_COINS 같은 접근이 투명하게 동작
    """

    @classmethod
    def __getattribute__(cls, name: str) -> Any:
        # Import original class if not already done
        global _BotConfig
        if _BotConfig is None:
            from config import settings as settings_module
            # Get the ORIGINAL class before our proxy replacement
            # Look in module's __dict__ to bypass any descriptors
            for key, value in settings_module.__dict__.items():
                if key == 'BotConfig' and hasattr(value, 'TARGET_COINS'):
                    _BotConfig = value
                    break

        # Handle special attributes
        if name in ('__class__', '__dict__', '__module__', '__doc__'):
            return object.__getattribute__(cls, name)

        # Get original value from _BotConfig
        try:
            if _BotConfig is not None:
                original_value = getattr(_BotConfig, name)
            else:
                raise AttributeError(f"BotConfig not initialized")
        except AttributeError:
            raise AttributeError(f"'BotConfig' has no attribute '{name}'")

        # If it's a method, private attribute, or dunder, return as-is
        if name.startswith('_') or callable(original_value):
            return original_value

        # Intercept and check DB
        return _config_manager.get_config_value('BotConfig', name, original_value)


class RiskParams:
    """RiskParams 프록시"""

    @classmethod
    def __getattribute__(cls, name: str) -> Any:
        global _RiskParams
        if _RiskParams is None:
            from config import settings as settings_module
            for key, value in settings_module.__dict__.items():
                if key == 'RiskParams' and hasattr(value, 'LIQUIDATION_BUFFER'):
                    _RiskParams = value
                    break

        if name in ('__class__', '__dict__', '__module__', '__doc__'):
            return object.__getattribute__(cls, name)

        try:
            if _RiskParams is not None:
                original_value = getattr(_RiskParams, name)
            else:
                raise AttributeError(f"RiskParams not initialized")
        except AttributeError:
            raise AttributeError(f"'RiskParams' has no attribute '{name}'")

        if name.startswith('_') or callable(original_value):
            return original_value

        return _config_manager.get_config_value('RiskParams', name, original_value)


class ScannerConfig:
    """ScannerConfig 프록시"""

    @classmethod
    def __getattribute__(cls, name: str) -> Any:
        global _ScannerConfig
        if _ScannerConfig is None:
            from config import settings as settings_module
            for key, value in settings_module.__dict__.items():
                if key == 'ScannerConfig' and hasattr(value, 'SCAN_INTERVAL'):
                    _ScannerConfig = value
                    break

        if name in ('__class__', '__dict__', '__module__', '__doc__'):
            return object.__getattribute__(cls, name)

        try:
            if _ScannerConfig is not None:
                original_value = getattr(_ScannerConfig, name)
            else:
                raise AttributeError(f"ScannerConfig not initialized")
        except AttributeError:
            raise AttributeError(f"'ScannerConfig' has no attribute '{name}'")

        if name.startswith('_') or callable(original_value):
            return original_value

        return _config_manager.get_config_value('ScannerConfig', name, original_value)

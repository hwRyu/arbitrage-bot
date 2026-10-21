"""
전략 실행을 위한 상태 머신

전략의 생명주기 동안 상태 전이를 관리하고 검증합니다.
"""
from enum import Enum
from typing import Optional, Dict, Any, Callable
from config.settings import States
from utils.logger import get_logger

logger = get_logger('state_machine')


class StateTransitionError(Exception):
    """유효하지 않은 상태 전이 에러"""
    pass


class StateMachine:
    """
    전략 상태 머신

    전략 실행 중 발생하는 모든 상태 전이를 관리하고 검증합니다.
    허용되지 않은 전이는 StateTransitionError를 발생시켜 무결성을 보장합니다.
    """

    # 허용된 상태 전이 (from_state → [to_states])
    ALLOWED_TRANSITIONS: Dict[str, list[str]] = {
        States.INIT: [States.CHECK_CAPITAL, States.ERROR],
        # States.CHECK_CAPITAL: [States.SCAN_OPPORTUNITY, States.ERROR],
        States.CHECK_CAPITAL: [States.VALIDATE_ENTRY, States.ERROR],
        # States.SCAN_OPPORTUNITY: [States.VALIDATE_ENTRY, States.ERROR],
        States.VALIDATE_ENTRY: [
            States.ENTERING,
            # States.SCAN_OPPORTUNITY,  # 검증 실패 시 다시 스캔
            States.ERROR
        ],
        States.ENTERING: [
            States.HEDGED,
            States.ERROR  # 진입 실패
        ],
        States.HEDGED: [
            States.MONITORING,
            States.ERROR
        ],
        States.MONITORING: [
            States.PROFITABLE,
            States.REDUCE_POSITION,
            States.STOP_LOSS,
            States.LIQUIDATION_RISK,
            States.EXITING,  # 직접 청산 (긴급)
            States.ERROR
        ],
        States.PROFITABLE: [
            States.EXITING,
            States.ERROR
        ],
        States.REDUCE_POSITION: [
            States.MONITORING,  # 포지션 축소 후 계속 모니터링
            States.EXITING,     # 전체 청산
            States.ERROR
        ],
        States.STOP_LOSS: [
            States.EXITING,
            States.ERROR
        ],
        States.LIQUIDATION_RISK: [
            States.REDUCE_POSITION,
            States.EXITING,
            States.ERROR
        ],
        States.EXITING: [
            States.SETTLEMENT,
            States.ERROR
        ],
        States.SETTLEMENT: [
            States.EXITED,
            States.ERROR
        ],
        States.EXITED: [],  # 종료 상태
        States.ERROR: [
            States.EXITING,  # 에러 상태에서 청산 시도 가능
            States.EXITED    # 또는 바로 종료
        ]
    }

    def __init__(self, initial_state: str = States.INIT):
        """
        상태 머신 초기화

        Args:
            initial_state: 초기 상태 (기본값: States.INIT)
        """
        self.current_state = initial_state
        self.previous_state: Optional[str] = None
        self.transition_history: list[tuple[str, str]] = []  # (from, to) pairs

    def can_transition(self, to_state: str) -> bool:
        """
        상태 전이 가능 여부 확인

        Args:
            to_state: 목표 상태

        Returns:
            전이 가능하면 True, 불가능하면 False
        """
        allowed = self.ALLOWED_TRANSITIONS.get(self.current_state, [])
        return to_state in allowed

    def transition(self, to_state: str, force: bool = False) -> bool:
        """
        새로운 상태로 전이

        현재 상태에서 목표 상태로의 전이가 허용되는지 검증한 후,
        전이를 실행하고 이력을 기록합니다.

        Args:
            to_state: 목표 상태
            force: 허용되지 않은 전이를 강제로 실행 (주의하여 사용)

        Returns:
            전이 성공 시 True

        Raises:
            StateTransitionError: 허용되지 않은 전이를 시도할 경우
        """
        if not force and not self.can_transition(to_state):
            error_msg = f"유효하지 않은 상태 전이: {self.current_state} -> {to_state}"
            logger.error(error_msg)
            raise StateTransitionError(error_msg)

        # 전이 이력 기록
        self.transition_history.append((self.current_state, to_state))
        self.previous_state = self.current_state
        self.current_state = to_state

        logger.debug(f"상태 전이: {self.previous_state} -> {self.current_state}")
        return True

    def is_terminal_state(self) -> bool:
        """
        현재 상태가 종료 상태인지 확인

        종료 상태는 더 이상 전이가 발생하지 않는 최종 상태입니다.

        Returns:
            종료 상태이면 True
        """
        return self.current_state in [States.EXITED]

    def is_error_state(self) -> bool:
        """
        현재 상태가 에러 상태인지 확인

        Returns:
            에러 상태이면 True
        """
        return self.current_state == States.ERROR

    def is_active_trading_state(self) -> bool:
        """
        현재 상태가 활성 포지션을 보유한 상태인지 확인

        활성 거래 상태는 업비트 현물 + 바이비트 숏 포지션이
        동시에 열려있는 상태를 의미합니다.

        Returns:
            활성 포지션이 있으면 True
        """
        return self.current_state in [
            States.HEDGED,
            States.MONITORING,
            States.PROFITABLE,
            States.REDUCE_POSITION,
            States.STOP_LOSS,
            States.LIQUIDATION_RISK
        ]

    def reset(self):
        """상태 머신을 초기 상태로 리셋"""
        self.transition_history.append((self.current_state, States.INIT))
        self.previous_state = self.current_state
        self.current_state = States.INIT
        logger.debug("상태 머신 초기화 (INIT)")

    def get_state_info(self) -> Dict[str, Any]:
        """
        현재 상태 정보 조회

        상태 머신의 현재 상태와 관련 메타데이터를 반환합니다.

        Returns:
            상태 정보 딕셔너리
        """
        return {
            'current_state': self.current_state,
            'previous_state': self.previous_state,
            'is_terminal': self.is_terminal_state(),
            'is_error': self.is_error_state(),
            'is_active_trading': self.is_active_trading_state(),
            'transition_count': len(self.transition_history)
        }


class StateHandler:
    """
    상태별 핸들러를 등록하고 실행하는 클래스

    각 상태에 대응하는 핸들러 함수를 등록하고 실행합니다.
    """

    def __init__(self):
        """상태 핸들러 초기화"""
        self.handlers: Dict[str, Callable] = {}

    def register(self, state: str, handler: Callable):
        """
        특정 상태에 대한 핸들러 등록

        Args:
            state: 상태 이름
            handler: 핸들러 함수 (전략 인스턴스를 인자로 받아야 함)
        """
        self.handlers[state] = handler
        logger.debug(f"상태 핸들러 등록: {state}")

    def handle(self, state: str, *args, **kwargs) -> Any:
        """
        특정 상태에 대한 핸들러 실행

        Args:
            state: 상태 이름
            *args, **kwargs: 핸들러에 전달할 인자

        Returns:
            핸들러 실행 결과

        Raises:
            ValueError: 해당 상태에 대한 핸들러가 등록되지 않은 경우
        """
        handler = self.handlers.get(state)
        if handler is None:
            raise ValueError(f"상태 핸들러 없음: {state}")

        logger.debug(f"상태 핸들러 실행: {state}")
        return handler(*args, **kwargs)

    def has_handler(self, state: str) -> bool:
        """
        특정 상태에 대한 핸들러 존재 여부 확인

        Args:
            state: 상태 이름

        Returns:
            핸들러가 등록되어 있으면 True
        """
        return state in self.handlers

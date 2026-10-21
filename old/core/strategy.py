"""
Layer 2: 전략 인스턴스

개별 코인에 대한 독립적인 전략 실행을 담당합니다.
각 전략 인스턴스는 하나의 코인에 대해 진입부터 청산까지의 전체 생명주기를 관리합니다.

주요 역할:
- 상태 머신 기반의 전략 실행 (INIT → ... → EXITED)
- 자본 확인 및 진입 검증
- 업비트 현물 매수 + 바이비트 선물 숏 동시 진입
- 실시간 모니터링 (가격 추적, 청산 위험 감시)
- 목표 수익 도달 시 자동 청산
- 모든 거래 내역 및 상태 DB 기록

상태 흐름:
정상: INIT → CHECK_CAPITAL → VALIDATE_ENTRY → ENTERING → HEDGED
      → MONITORING → PROFITABLE → EXITING → SETTLEMENT → EXITED
위험: MONITORING → REDUCE_POSITION/LIQUIDATION_RISK/STOP_LOSS → EXITING
"""
import time
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session

from config.settings import States, BotConfig, BybitSettings, RiskParams, ScannerConfig
from models.database import SessionLocal
from models.models import Strategy as StrategyModel, Trade, FundingFee, BotLog
from models.position import MarketPrice, Position
from core.state_machine import StateMachine
from services.upbit import get_upbit_service
from services.bybit import get_bybit_service
from services.exchange_rate import get_exchange_rate_service
from utils.calculator import (
    calculate_liquidation_price,
    calculate_position_size,
    calculate_net_profit_krw,
    calculate_target_premium
)
from utils.validator import validate_entry, validate_balance, check_liquidation_risk
from utils.logger import get_logger, log_state_transition, log_trade, log_pnl, log_liquidation_warning

logger = get_logger('strategy')


class StrategyInstance:
    """
    전략 인스턴스 (Layer 2)

    한 코인에 대한 하나의 완전한 차익거래 전략을 실행합니다.
    상태 머신 패턴을 사용하여 각 단계를 순차적으로 진행하며,
    모든 상태와 거래 내역을 데이터베이스에 저장합니다.

    특징:
    - 독립 실행: 각 코인별로 독립적인 스레드에서 실행
    - 상태 복구: 봇 재시작 시 DB에서 이전 상태 복구 가능
    - 헷지 전략: 업비트 롱 + 바이비트 숏으로 완전 헷지
    - 리스크 관리: 청산 위험 실시간 모니터링
    - 자동 대응: 청산 위험 시 포지션 축소, 손절 등 자동 처리

    Attributes:
        coin: 코인 심볼 (예: 'SOL', 'XRP')
        market_price: 현재 시장 가격 정보
        strategy_id: DB에 저장된 전략 ID
        position: 현재 포지션 정보 (진입 후)
        state_machine: 상태 관리 객체
    """

    def __init__(self, coin: str, market_price: MarketPrice, strategy_id: Optional[int] = None):
        """
        전략 인스턴스 초기화

        새로운 전략을 시작하거나 기존 전략을 복구합니다.
        strategy_id가 제공되면 DB에서 기존 전략을 로드하고,
        없으면 새로운 전략 레코드를 생성합니다.

        Args:
            coin: 코인 심볼 (예: 'SOL', 'XRP', 'DOGE')
            market_price: 초기 시장 가격 정보 (김프, 환율, USDT/KRW 포함)
            strategy_id: 기존 전략 ID (복구 시), None이면 신규 전략 생성

        Raises:
            ValueError: strategy_id가 제공되었으나 DB에 해당 전략이 없는 경우
            Exception: DB 작업 실패 시
        """
        self.coin = coin
        self.market_price = market_price
        self.strategy_id = strategy_id

        # 거래소 서비스 (업비트, 바이비트)
        self.upbit = get_upbit_service()
        self.bybit = get_bybit_service()

        # 상태 머신 (전략 실행 상태 관리)
        self.state_machine = StateMachine()

        # 포지션 정보 (진입 후 생성)
        self.position: Optional[Position] = None
        # 수집된 펀딩비 (USDT)
        self.funding_fees_collected = 0.0

        # 데이터베이스 세션
        self.db: Optional[Session] = None

        # 기존 전략 복구 또는 신규 생성
        if strategy_id:
            self._load_from_db()
        else:
            self._create_strategy_record()

        logger.info(f"[{self.coin}] 전략 인스턴스 생성 완료 (ID: {self.strategy_id})")

    def _create_strategy_record(self):
        """
        데이터베이스에 새 전략 레코드 생성

        신규 전략 시작 시 호출됩니다.
        초기 상태(INIT)와 진입 시점의 김프, 환율 등을 DB에 저장합니다.

        Raises:
            Exception: DB 작업 실패 시
        """
        try:
            self.db = SessionLocal()

            # 전략 레코드 생성
            strategy = StrategyModel(
                coin=self.coin,
                state=States.INIT,
                entry_premium=self.market_price.premium,
                exchange_rate=self.market_price.exchange_rate,
                usdt_krw=self.market_price.usdt_krw,
                is_paper_trading=BotConfig.PAPER_TRADING
            )

            self.db.add(strategy)
            self.db.commit()
            self.db.refresh(strategy)

            self.strategy_id = strategy.id

            logger.info(f"[{self.coin}] 전략 레코드 생성 완료 (ID: {self.strategy_id})")

        except Exception as e:
            logger.error(f"[{self.coin}] 전략 레코드 생성 실패: {e}", exc_info=True)
            if self.db:
                self.db.rollback()
            raise

    def _load_from_db(self):
        """
        데이터베이스에서 기존 전략 로드 (복구)

        봇 재시작 시 호출되어 이전에 실행 중이던 전략을 복구합니다.
        상태, 포지션 정보, 펀딩비 등을 DB에서 불러와 실행을 이어갑니다.

        Raises:
            ValueError: 해당 strategy_id가 DB에 존재하지 않는 경우
            Exception: DB 작업 실패 시
        """
        try:
            self.db = SessionLocal()
            strategy = self.db.query(StrategyModel).filter_by(id=self.strategy_id).first()

            if not strategy:
                raise ValueError(f"전략 {self.strategy_id}를 데이터베이스에서 찾을 수 없습니다")

            # 상태 복원
            self.state_machine.current_state = strategy.state

            # 포지션 복원 (진입 완료된 전략의 경우)
            if strategy.entry_amount and strategy.entry_price_krw and strategy.entry_price_usd:
                self.position = Position(
                    coin=self.coin,
                    amount=strategy.entry_amount,
                    entry_price_krw=strategy.entry_price_krw,
                    entry_price_usd=strategy.entry_price_usd,
                    leverage=strategy.leverage,
                    liquidation_price=strategy.liquidation_price,
                    upbit_order_id=strategy.upbit_order_id,
                    bybit_order_id=strategy.bybit_order_id,
                    entry_premium=strategy.entry_premium
                )

            # 펀딩비 복원
            self.funding_fees_collected = strategy.funding_fee_earned

            logger.info(f"[{self.coin}] 전략 {self.strategy_id} 복구 완료 (상태: {strategy.state})")

        except Exception as e:
            logger.error(f"[{self.coin}] 전략 복구 실패: {e}", exc_info=True)
            raise

    def _update_state(self, new_state: str):
        """
        상태 업데이트 (상태 머신 + 데이터베이스)

        상태 머신에서 상태 전이를 수행하고, DB에도 동기화합니다.
        허용되지 않은 상태 전이는 StateTransitionError를 발생시킵니다.

        Args:
            new_state: 새로운 상태 (States 클래스의 상수)

        Raises:
            StateTransitionError: 허용되지 않은 상태 전이 시
        """
        old_state = self.state_machine.current_state
        self.state_machine.transition(new_state)

        # 데이터베이스에 상태 업데이트
        try:
            strategy = self.db.query(StrategyModel).filter_by(id=self.strategy_id).first()
            strategy.state = new_state
            strategy.updated_at = datetime.now()
            self.db.commit()

            log_state_transition(logger, self.strategy_id, self.coin, old_state, new_state)

        except Exception as e:
            logger.error(f"[{self.coin}] DB 상태 업데이트 실패: {e}", exc_info=True)
            self.db.rollback()

    def run(self) -> bool:
        """
        전략 메인 루프 실행

        현재 상태에 따라 적절한 핸들러를 호출합니다.
        각 핸들러는 자신의 작업을 수행하고 다음 상태로 전이합니다.

        반환값:
            True: 전략이 계속 실행되어야 함 (다음 사이클에서 다시 호출)
            False: 전략 종료 (EXITED, ERROR 등 터미널 상태)

        Raises:
            Exception: 예상치 못한 에러 발생 시 ERROR 상태로 전이
        """
        try:
            # 현재 상태에 따라 적절한 핸들러 실행
            if self.state_machine.current_state == States.INIT:
                return self._handle_init()
            elif self.state_machine.current_state == States.CHECK_CAPITAL:
                return self._handle_check_capital()
            elif self.state_machine.current_state == States.VALIDATE_ENTRY:
                return self._handle_validate_entry()
            elif self.state_machine.current_state == States.ENTERING:
                return self._handle_entering()
            elif self.state_machine.current_state == States.HEDGED:
                return self._handle_hedged()
            elif self.state_machine.current_state == States.MONITORING:
                return self._handle_monitoring()
            elif self.state_machine.current_state == States.PROFITABLE:
                return self._handle_profitable()
            elif self.state_machine.current_state == States.REDUCE_POSITION:
                return self._handle_reduce_position()
            elif self.state_machine.current_state == States.LIQUIDATION_RISK:
                return self._handle_liquidation_risk()
            elif self.state_machine.current_state == States.STOP_LOSS:
                return self._handle_stop_loss()
            elif self.state_machine.current_state == States.EXITING:
                return self._handle_exiting()
            elif self.state_machine.current_state == States.SETTLEMENT:
                return self._handle_settlement()
            elif self.state_machine.current_state == States.EXITED:
                return False  # 전략 종료
            elif self.state_machine.current_state == States.ERROR:
                return self._handle_error()
            else:
                logger.error(f"[{self.coin}] 알 수 없는 상태: {self.state_machine.current_state}")
                return False

        except Exception as e:
            logger.error(f"[{self.coin}] 전략 {self.strategy_id} 실행 중 에러 발생: {e}", exc_info=True)
            self._update_state(States.ERROR)
            return False

    def _handle_init(self) -> bool:
        """
        INIT 상태 처리

        전략 초기화 단계입니다.
        바로 자본 확인 단계로 넘어갑니다.
        """
        logger.info(f"[{self.coin}] 전략 {self.strategy_id} 초기화 중...")
        self._update_state(States.CHECK_CAPITAL)
        return True

    def _handle_check_capital(self) -> bool:
        """
        CHECK_CAPITAL 상태 처리

        진입에 필요한 자본이 충분한지 확인합니다.
        - 업비트: KRW 잔고 확인
        - 바이비트: USDT 잔고 확인 (레버리지 고려)

        실패 시 ERROR 상태로 전이합니다.
        """
        logger.info(f"[{self.coin}] 전략 {self.strategy_id} 자본 확인 중...")

        # 1단계: 필요 자본 계산
        # 양 거래소의 자본 한도(MAX_POSITION)와 현재 가격을 고려하여
        # 실제로 진입 가능한 포지션 크기를 계산
        position_size = calculate_position_size(
            BotConfig.MAX_POSITION_KRW,      # 업비트 최대 투입 금액 (KRW)
            BotConfig.MAX_POSITION_USDT,     # 바이비트 최대 투입 금액 (USDT)
            self.market_price.krw_price,     # 현재 업비트 가격
            self.market_price.usd_price,     # 현재 바이비트 가격
            self.market_price.usdt_krw       # USDT/KRW 가격
        )

        # 필요한 KRW: 현물 매수에 필요한 금액
        required_krw = position_size * self.market_price.krw_price

        # 필요한 USDT: 숏 포지션 증거금 (레버리지 1배이므로 포지션 크기와 동일)
        required_usdt = position_size * self.market_price.usd_price / BybitSettings.LEVERAGE

        # 2단계: 실제 잔고 조회
        upbit_balance = self.upbit.fetch_balance('KRW')
        bybit_balance = self.bybit.fetch_balance('USDT')

        # 3단계: 잔고 충분성 검증
        is_valid, reason = validate_balance(upbit_balance, bybit_balance, required_krw, required_usdt)

        if not is_valid:
            logger.error(f"전략 {self.strategy_id}: {reason}")
            self._update_state(States.ERROR)
            return False

        logger.info(f"전략 {self.strategy_id} 자본 확인 완료 (KRW: {upbit_balance:.0f}, USDT: {bybit_balance:.2f})")
        self._update_state(States.VALIDATE_ENTRY)
        return True

    def _handle_validate_entry(self) -> bool:
        """
        VALIDATE_ENTRY 상태 처리 - 진입 조건 검증

        실제 포지션 진입 전 마지막 안전 체크를 수행합니다:
        1. 호가창 깊이: 큰 주문에도 슬리피지가 적은지 확인
        2. 청산가 안전성: 레버리지 1배에서도 충분히 안전한지 확인
        3. 슬리피지 추정: 예상 체결가와 현재가의 차이 계산

        실패 시 ERROR 상태로 전이합니다.
        """
        logger.info(f"전략 {self.strategy_id} 진입 검증 중...")

        # 1단계: 호가창 조회
        # 두 거래소의 호가창을 조회하여 실제 주문 가능 여부 확인
        upbit_orderbook = self.upbit.fetch_orderbook(self.coin)
        bybit_orderbook = self.bybit.fetch_orderbook(self.coin)

        if not upbit_orderbook or not bybit_orderbook:
            logger.error(f"전략 {self.strategy_id} 호가창 조회 실패")
            self._update_state(States.ERROR)
            return False

        # 2단계: 진입 조건 종합 검증
        # validate_entry()는 다음을 체크합니다:
        # - 청산가가 진입가의 1.7배 이상인지 (LIQUIDATION_BUFFER)
        # - 호가 깊이가 포지션 크기의 10배 이상인지 (MIN_ORDERBOOK_DEPTH_MULTIPLIER)
        # - 예상 슬리피지가 0.5% 미만인지 (MAX_SLIPPAGE_PERCENT)
        validation = validate_entry(self.coin, self.market_price, upbit_orderbook, bybit_orderbook)

        if not validation.is_valid:
            logger.warning(f"전략 {self.strategy_id} 진입 검증 실패: {', '.join(validation.reasons)}")
            self._update_state(States.ERROR)
            return False

        logger.info(f"전략 {self.strategy_id} 진입 검증 통과")
        self._update_state(States.ENTERING)
        return True

    def _handle_entering(self) -> bool:
        """
        ENTERING 상태 처리 - 포지션 진입 실행

        역전 전략의 핵심 진입 로직:
        1. 바이비트 숏 포지션 오픈 (해외 선물 매도)
        2. 업비트 현물 매수 (국내 현물 매수)
        3. 완전 헷지: 동일 수량으로 양 거래소 포지션 생성
        4. 청산가 계산 및 저장

        전략 설명:
        - 진입 시점: 김프가 낮거나 음수 (해외가 더 비쌈)
        - 헷지 구조: 업비트 롱 + 바이비트 숏
        - 수익 발생: 김프 상승 시 (국내 가격이 상대적으로 올라갈 때)
        """
        logger.info(f"[{self.coin}] 전략 {self.strategy_id} 포지션 진입 중...")

        # 1단계: 포지션 크기 계산
        # 양 거래소의 자본 한도와 현재 가격을 고려하여 최적 포지션 크기 결정
        # - MAX_POSITION_KRW: 업비트 최대 투입 금액 (예: 1,000,000 KRW)
        # - MAX_POSITION_USDT: 바이비트 최대 투입 금액 (예: 1,000 USDT)
        # - 결과: 두 거래소 모두 진입 가능한 최대 코인 수량
        position_size = calculate_position_size(
            BotConfig.MAX_POSITION_KRW,
            BotConfig.MAX_POSITION_USDT,
            self.market_price.krw_price,
            self.market_price.usd_price,
            self.market_price.usdt_krw
        )

        try:
            # 2단계: 바이비트 숏 포지션 오픈 (먼저 실행)
            #
            # 바이비트를 먼저 실행하는 이유:
            # - 선물 주문이 현물보다 빠름
            # - 만약 업비트 먼저 매수 후 바이비트 실패 시, 일방향 노출 위험
            # - 바이비트 실패 시 업비트 진입 취소하면 되므로 안전
            #
            # 숏 포지션 의미:
            # - "해외 가격이 하락할 것" 또는 "국내 가격이 상대적으로 상승할 것"에 베팅
            # - 김프 상승(국내 가격 상대적 상승) 시 수익
            self.bybit.set_position_mode(self.coin, False)
            bybit_order = self.bybit.open_short_position(self.coin, position_size)
            if not bybit_order:
                raise Exception("바이비트 숏 포지션 오픈 실패")
            
            # 거래 로그 DB 기록
            self._log_trade('bybit', 'open_short', bybit_order)

            # 3단계: 업비트 현물 매수
            #
            # 주의: 업비트는 시장가 매수 시 '금액'(cost)을 지정
            # - cost = 수량 × 가격 (KRW)
            # - 예: 0.5 SOL × 200,000 KRW = 100,000 KRW
            upbit_cost = position_size * self.market_price.krw_price
            upbit_order = self.upbit.create_market_order(self.coin, 'buy', cost=upbit_cost)
            if not upbit_order:
                # 업비트 매수 실패 시 롤백: 바이비트 포지션 즉시 청산
                # 이렇게 하지 않으면 바이비트 숏만 남아 방향성 리스크 발생
                self.bybit.close_short_position(self.coin, position_size)
                raise Exception("업비트 매수 실패, 바이비트 포지션 롤백 완료")

            # 거래 로그 DB 기록
            self._log_trade('upbit', 'buy', upbit_order)

            # 4단계: 청산가 계산
            #
            # 청산가 공식 (Isolated Margin, 1배 레버리지 숏):
            # liq_price = entry_price × (2 - MMR)
            #
            # 예시: SOL 100 USD 진입, MMR 1% (0.01)
            # liq_price = 100 × (2 - 0.01) = 199 USD
            # → 가격이 거의 2배가 되어야 청산 (매우 안전)
            #
            # 레버리지 1배의 장점:
            # - 청산 위험 극히 낮음
            # - 김프 변동에만 집중, 가격 방향성 리스크 최소화
            liq_price = calculate_liquidation_price(
                self.market_price.usd_price,
                BybitSettings.LEVERAGE,
                'short',
                self.coin
            )

            # 5단계: 포지션 객체 생성
            # 진입 완료된 포지션 정보를 메모리에 저장
            self.position = Position(
                coin=self.coin,
                amount=position_size,
                entry_price_krw=self.market_price.krw_price,
                entry_price_usd=self.market_price.usd_price,
                leverage=BybitSettings.LEVERAGE,
                liquidation_price=liq_price,
                upbit_order_id=upbit_order.get('id'),
                bybit_order_id=bybit_order.get('id'),
                entry_premium=self.market_price.premium
            )

            # 6단계: DB에 진입 정보 저장
            # 봇 재시작 시 복구를 위해 모든 진입 정보 기록
            strategy = self.db.query(StrategyModel).filter_by(id=self.strategy_id).first()
            strategy.entry_price_krw = self.market_price.krw_price
            strategy.entry_price_usd = self.market_price.usd_price
            strategy.entry_amount = position_size
            strategy.entry_value_krw = upbit_cost
            strategy.leverage = BybitSettings.LEVERAGE
            strategy.liquidation_price = liq_price
            strategy.upbit_order_id = upbit_order.get('id')
            strategy.bybit_order_id = bybit_order.get('id')

            # 7단계: 목표 청산 김프 계산 (역전 전략 핵심)
            #
            # 목표 김프 = 진입 김프 + 목표 수익 + 수수료 + 슬리피지
            #
            # 예시:
            # - 진입 김프: -1% (해외가 더 비쌈)
            # - 목표 수익: 1% (설정값)
            # - 수수료: 0.25% (업비트 0.05% + 바이비트 0.075% × 2)
            # - 슬리피지: 0.1% (예상치)
            # → 목표 김프 = -1% + 1% + 0.25% + 0.1% = 0.35%
            #
            # 의미: 현재 김프가 0.35% 이상으로 상승하면 청산하여 수익 실현
            entry_premium = self.market_price.premium
            strategy.target_premium = calculate_target_premium(
                BotConfig.TARGET_PROFIT_PERCENT,
                self.market_price.premium,  # current (not used)
                entry_premium  # entry premium
            )
            self.db.commit()

            logger.info(f"[{self.coin}] 전략 {self.strategy_id} 포지션 진입 완료 (목표 이율 : {BotConfig.TARGET_PROFIT_PERCENT}, 목표 김프 : {strategy.target_premium}%, 진입 김프 : {entry_premium}%)")
            self._update_state(States.HEDGED)
            return True

        except Exception as e:
            logger.error(f"[{self.coin}] 전략 {self.strategy_id} 포지션 진입 실패: {e}")
            self._update_state(States.ERROR)
            return False

    def _handle_hedged(self) -> bool:
        """
        HEDGED 상태 처리

        양방향 헷지 포지션이 완성된 상태입니다.
        바로 MONITORING 상태로 전환하여 실시간 모니터링을 시작합니다.
        """
        logger.info(f"전략 {self.strategy_id} 헷지 완료, 모니터링 시작")
        self._update_state(States.MONITORING)
        return True

    def _handle_monitoring(self) -> bool:
        """
        MONITORING 상태 처리 - 핵심 모니터링 로직

        진입 완료 후 지속적으로 가격을 추적하며:
        1. 현재 김프 계산 및 DB 업데이트
        2. 청산 위험 감지 (가격이 청산가에 근접)
        3. 목표 수익 도달 체크 (역전 전략: 김프 상승으로 수익 실현)
        4. 손절 조건 체크 (설정된 손실 한도 초과)
        """
        logger.debug(f"[{self.coin}] 전략 {self.strategy_id} 모니터링 중...")

        # 1단계: 현재 시세 및 환율 조회
        # - 업비트 KRW 가격
        # - 바이비트 USD 가격
        # - USD/KRW 환율 (yfinance, 1시간 캐시)
        upbit_ticker = self.upbit.fetch_ticker(self.coin)
        bybit_ticker = self.bybit.fetch_ticker(self.coin)

        if not upbit_ticker or not bybit_ticker:
            logger.error(f"[{self.coin}] 전략 {self.strategy_id} 가격 조회 실패")
            return True  # 일시적 에러, 다음 사이클에서 재시도

        # 환율 업데이트 (정확한 김프 계산을 위해 필수)
        #
        # 김프 공식: premium = (업비트_KRW / (바이비트_USD × 환율)) - 1
        # 환율이 변동하면 김프도 변동하므로, 최신 환율을 반영해야 정확한 수익 계산 가능
        #
        # 예시:
        # - 진입 시 환율 1,300원, 현재 환율 1,350원
        # - 코인 가격은 동일해도 김프는 약 3.8% 하락
        # - 따라서 환율 변동을 실시간 추적해야 함
        exchange_rate_service = get_exchange_rate_service()
        current_exchange_rate = exchange_rate_service.get_usd_krw_rate()

        if not current_exchange_rate:
            # yfinance API 이용 불가 시간대이거나 네트워크 에러
            # → 캐시된 환율 사용 (서비스 내부에서 1시간 캐싱)
            logger.warning(f"[{self.coin}] 전략 {self.strategy_id} 환율 조회 실패, 캐시된 환율 사용")
            current_exchange_rate = self.market_price.exchange_rate

        current_krw = upbit_ticker['last']
        current_usd = bybit_ticker['last']

        # 2단계: 마켓프라이스 객체 업데이트 (김프 자동 재계산)
        # MarketPrice 객체는 krw_price, usd_price, exchange_rate 중 하나라도
        # 업데이트되면 premium을 자동으로 재계산합니다.
        # 따라서 세 값을 모두 업데이트하면 최신 김프가 계산됩니다.
        self.market_price.krw_price = current_krw
        self.market_price.usd_price = current_usd
        self.market_price.exchange_rate = current_exchange_rate
        current_premium = self.market_price.premium

        # 3단계: 현재 김프를 DB에 기록 (실시간 추적용)
        strategy = self.db.query(StrategyModel).filter_by(id=self.strategy_id).first()
        strategy.current_premium = current_premium
        self.db.commit()

        # 4단계: 청산 위험 체크
        # 숏 포지션이므로 가격이 올라가면 청산 위험 증가
        # 현재가와 청산가의 거리를 계산하여 위험 수준 판단
        is_risk, risk_level = check_liquidation_risk(current_usd, self.position.liquidation_price)
        if is_risk:
            if risk_level == "CRITICAL":
                # 긴급 상황: 청산가까지 20% 미만 남음
                # → LIQUIDATION_RISK 상태로 전환하여 즉시 포지션 50% 축소
                log_liquidation_warning(
                    logger, self.strategy_id, self.coin,
                    current_usd, self.position.liquidation_price,
                    self.position.liquidation_distance
                )
                self._update_state(States.LIQUIDATION_RISK)
                return True
            elif risk_level == "WARNING":
                # 경고 상황: 청산가까지 30% 미만 남음
                # → 로그만 남기고 모니터링 계속
                log_liquidation_warning(
                    logger, self.strategy_id, self.coin,
                    current_usd, self.position.liquidation_price,
                    self.position.liquidation_distance
                )

        # 5단계: 목표 수익 도달 체크 (역전 전략의 청산 조건)
        #
        # 역전 전략 청산 로직:
        # - 진입: 김프가 낮을 때 (예: -1%, 해외가 더 비쌈)
        # - 청산: 김프가 상승하여 목표에 도달 (예: +1%, 국내가 더 비쌈)
        #
        # 목표 김프 = 진입 김프 + 목표 수익 + 수수료 + 슬리피지
        # 예시: -1% + 1% + 0.25% + 0.1% = 0.35%
        target_premium = strategy.target_premium or 0
        if current_premium >= target_premium:
            logger.info(f"[{self.coin}] 전략 {self.strategy_id} 목표 도달 (현재 김프: {current_premium:.2f}%, 목표: {target_premium:.2f}%)")
            self._update_state(States.PROFITABLE)
            return True

        # 6단계: 손절 조건 체크
        #
        # 완전 헷지 전략의 손익 계산 원리:
        # - 실제 손익 = (현재 김프 - 진입 김프) × 포지션 가치
        # - 업비트 롱 + 바이비트 숏의 조합이므로 김프 변화가 곧 손익
        #
        # 역전 전략에서 손실 발생 케이스:
        # - 진입 시: 김프 -0.5% (해외가 더 비쌈)
        # - 현재: 김프 -1.5% (김프가 더 낮아짐)
        # - 손실: -1.5% - (-0.5%) = -1.0%
        #
        # 왜 손실이 발생하는가?
        # - 업비트 현물 가격은 그대로, 바이비트 가격이 상승
        # - 또는 업비트 가격은 하락, 바이비트는 그대로
        # - 결과적으로 국내외 가격 차이가 더 벌어짐 (역전 방향으로)
        entry_premium = self.position.entry_premium
        premium_change = current_premium - entry_premium

        # 손절 기준: STOP_LOSS_PERCENT (예: -5.0%)
        # 김프가 진입 대비 설정값 이하로 하락하면 강제 청산
        # 레버리지 1배이므로 -5% 손실은 전체 포지션의 5% 손실을 의미
        if premium_change <= RiskParams.STOP_LOSS_PERCENT:
            logger.warning(
                f"[{self.coin}] 전략 {self.strategy_id} 손절 조건 도달 "
                f"(진입 김프: {entry_premium:.2f}%, 현재 김프: {current_premium:.2f}%, 변화: {premium_change:.2f}%)"
            )
            self._update_state(States.STOP_LOSS)
            return True

        # 7단계: 모든 조건 정상 → 계속 모니터링
        # 현재 상태 로그 출력 (김프 변화, 목표까지 거리)
        entry_premium = self.position.entry_premium
        premium_change = current_premium - entry_premium
        distance_to_target = target_premium - current_premium

        logger.info(
            f"[{self.coin}] 전략 {self.strategy_id} 모니터링 - "
            f"현재 김프: {current_premium:.2f}% | "
            f"진입 대비: {premium_change:+.2f}% | "
            f"목표까지: {distance_to_target:.2f}%"
        )

        # 설정된 인터벌마다 체크 (기본 2초)
        # 빠른 김프 변화 감지를 위해 10초에서 2초로 단축
        time.sleep(ScannerConfig.MONITORING_INTERVAL)
        return True

    def _handle_profitable(self) -> bool:
        """
        PROFITABLE 상태 처리

        목표 수익에 도달하여 정상 청산을 시작합니다.
        EXITING 상태로 전환하여 양쪽 포지션을 동시에 청산합니다.
        """
        logger.info(f"전략 {self.strategy_id} 목표 수익 도달, 청산 시작...")
        self._update_state(States.EXITING)
        return True

    def _handle_reduce_position(self) -> bool:
        """
        REDUCE_POSITION 상태 처리

        청산 위험이 감지되어 포지션을 50% 축소합니다.
        양쪽 거래소에서 동시에 절반을 청산하여 리스크를 낮춥니다.
        """
        logger.warning(f"전략 {self.strategy_id} 포지션 축소 중...")

        # Reduce by 50%
        reduce_amount = self.position.amount * 0.5

        try:
            # Close partial Bybit position
            bybit_order = self.bybit.close_short_position(self.coin, reduce_amount)
            if bybit_order:
                self._log_trade('bybit', 'close_short', bybit_order)

            # Sell partial Upbit position
            upbit_order = self.upbit.create_market_order(self.coin, 'sell', amount=reduce_amount)
            if upbit_order:
                self._log_trade('upbit', 'sell', upbit_order)

            # 포지션 정보 업데이트
            self.position.amount -= reduce_amount

            logger.info(f"전략 {self.strategy_id} 포지션 50% 축소 완료")
            self._update_state(States.MONITORING)
            return True

        except Exception as e:
            logger.error(f"전략 {self.strategy_id} 포지션 축소 실패: {e}")
            return True  # 계속 재시도

    def _handle_liquidation_risk(self) -> bool:
        """
        LIQUIDATION_RISK 상태 처리

        청산가에 너무 근접하여 긴급 상황입니다.
        REDUCE_POSITION 상태로 전환하여 포지션을 50% 축소합니다.
        """
        logger.critical(f"전략 {self.strategy_id} 청산 위험, 포지션 축소")
        self._update_state(States.REDUCE_POSITION)
        return True

    def _handle_stop_loss(self) -> bool:
        """
        STOP_LOSS 상태 처리

        손절 기준에 도달하여 손실을 제한하기 위해 청산합니다.
        EXITING 상태로 전환하여 전체 포지션을 청산합니다.
        """
        logger.warning(f"전략 {self.strategy_id} 손절, 청산 시작")
        self._update_state(States.EXITING)
        return True

    def _handle_exiting(self) -> bool:
        """
        EXITING 상태 처리 - 포지션 청산

        양쪽 거래소에서 동시에 포지션을 청산합니다:
        1. 바이비트 숏 포지션 청산 (reduceOnly=True)
        2. 업비트 현물 매도
        """
        logger.info(f"전략 {self.strategy_id} 포지션 청산 중...")

        try:
            # Close Bybit short
            bybit_order = self.bybit.close_short_position(self.coin, self.position.amount)
            if bybit_order:
                self._log_trade('bybit', 'close_short', bybit_order)

            # 업비트 현물 매도
            upbit_order = self.upbit.create_market_order(self.coin, 'sell', amount=self.position.amount)
            if upbit_order:
                self._log_trade('upbit', 'sell', upbit_order)

            logger.info(f"전략 {self.strategy_id} 포지션 청산 완료")
            self._update_state(States.SETTLEMENT)
            return True

        except Exception as e:
            logger.error(f"전략 {self.strategy_id} 포지션 청산 실패: {e}")
            return True  # 계속 재시도

    def _handle_settlement(self) -> bool:
        """
        SETTLEMENT 상태 처리 - 최종 정산

        포지션 청산 후 최종 손익을 계산하고 DB에 기록합니다:
        1. 최종 가격 조회
        2. 실현 손익 계산 (진입가 대비 청산가)
        3. 펀딩비 수익 포함
        4. DB 업데이트 후 EXITED 상태로 전환
        """
        logger.info(f"전략 {self.strategy_id} 정산 중...")

        # Fetch final prices
        upbit_ticker = self.upbit.fetch_ticker(self.coin)
        bybit_ticker = self.bybit.fetch_ticker(self.coin)

        exit_krw = upbit_ticker['last'] if upbit_ticker else self.market_price.krw_price
        exit_usd = bybit_ticker['last'] if bybit_ticker else self.market_price.usd_price

        # Calculate final PnL
        entry_value = self.position.position_value_krw
        exit_value = self.position.amount * exit_krw
        funding_fee_krw = self.funding_fees_collected

        net_pnl_krw, net_pnl_percent = calculate_net_profit_krw(entry_value, exit_value, funding_fee_krw)

        # Update DB
        strategy = self.db.query(StrategyModel).filter_by(id=self.strategy_id).first()
        strategy.exit_price_krw = exit_krw
        strategy.exit_price_usd = exit_usd
        strategy.exit_value_krw = exit_value
        strategy.exit_premium = self.market_price.premium
        strategy.realized_pnl_krw = net_pnl_krw
        strategy.realized_pnl_percent = net_pnl_percent
        strategy.exited_at = datetime.now()
        strategy.exit_reason = self.state_machine.previous_state or 'UNKNOWN'
        self.db.commit()

        # Log PnL
        log_pnl(logger, self.strategy_id, self.coin, net_pnl_krw, net_pnl_percent, strategy.exit_reason)

        self._update_state(States.EXITED)
        return False  # Terminate

    def _handle_error(self) -> bool:
        """
        ERROR 상태 처리

        예상치 못한 에러가 발생한 상태입니다.
        포지션이 있으면 청산을 시도하고, 없으면 바로 종료합니다.
        """
        logger.error(f"전략 {self.strategy_id} 에러 상태")

        # Try to exit if position exists
        if self.position and self.state_machine.can_transition(States.EXITING):
            self._update_state(States.EXITING)
            return True

        # Otherwise terminate
        self._update_state(States.EXITED)
        return False

    def _log_trade(self, exchange: str, side: str, order: dict):
        """거래를 데이터베이스에 기록"""
        try:
            trade = Trade(
                strategy_id=self.strategy_id,
                exchange=exchange,
                side=side,
                coin=self.coin,
                amount=order.get('filled', order.get('amount', 0)),
                price=order.get('price', 0),
                value=order.get('cost', 0),
                fee=order.get('fee', {}).get('cost', 0),
                fee_currency=order.get('fee', {}).get('currency', 'UNKNOWN'),
                order_id=order.get('id'),
                order_type=order.get('type', 'market'),
                is_paper_trading=BotConfig.PAPER_TRADING
            )

            self.db.add(trade)
            self.db.commit()

            log_trade(logger, exchange, side, self.coin, trade.amount, trade.price, True, trade.order_id)

        except Exception as e:
            logger.error(f"거래 로그 기록 실패: {e}")
            self.db.rollback()

    def cleanup(self):
        """리소스 정리"""
        if self.db:
            self.db.close()
        logger.info(f"전략 {self.strategy_id} 정리 완료")

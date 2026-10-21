"""
KIMP 차익거래 봇 메인 진입점
"""
import sys
import time
import signal
from typing import Dict, List
from threading import Thread, Event
from sqlalchemy.orm import Session

from config.settings import validate_config, BotConfig, ScannerConfig, OHLCConfig, States
from models.database import init_db, SessionLocal, close_db
from models.models import Strategy as StrategyModel
from core.scanner import MarketScanner
from core.strategy import StrategyInstance
from core.ohlc_collector import get_ohlc_collector
from utils.logger import get_logger

logger = get_logger('main')

# Global shutdown event
shutdown_event = Event()


class BotManager:
    """
    봇 메인 관리자

    역할:
    - 메인 스캐너 실행
    - 전략 인스턴스 생성 및 관리
    - 재시작 시 복구
    """

    def __init__(self):
        """봇 매니저 초기화"""
        self.scanner = MarketScanner()
        self.active_strategies: Dict[str, StrategyInstance] = {}
        self.strategy_threads: Dict[str, Thread] = {}

        # OHLC collector (if enabled)
        self.ohlc_collector = None
        self.ohlc_thread = None

        if OHLCConfig.ENABLED:
            self.ohlc_collector = get_ohlc_collector()
            logger.info("OHLC 수집기 활성화됨")

        logger.info("봇 매니저 초기화 완료")

    def recover_active_strategies(self):
        """데이터베이스에서 활성 전략 복구"""
        logger.info("데이터베이스에서 활성 전략 복구 중...")

        try:
            db: Session = SessionLocal()

            # Find strategies that are not in terminal state
            active_states = [
                States.INIT,
                States.CHECK_CAPITAL,
                # States.SCAN_OPPORTUNITY,  # 제거됨: 핸들러 미구현, 실제 흐름에서 사용되지 않음
                States.VALIDATE_ENTRY,
                States.ENTERING,
                States.HEDGED,
                States.MONITORING,
                States.PROFITABLE,
                States.REDUCE_POSITION,
                States.STOP_LOSS,
                States.LIQUIDATION_RISK,
                States.EXITING,
                States.SETTLEMENT
            ]

            active_strategies = db.query(StrategyModel).filter(
                StrategyModel.state.in_(active_states)
            ).all()

            logger.info(f"{len(active_strategies)}개의 활성 전략 발견")

            for strategy_record in active_strategies:
                try:
                    # 코인의 최신 시장가 조회
                    market_price = self.scanner.get_latest_price(strategy_record.coin)
                    if not market_price:
                        logger.warning(f"{strategy_record.coin} 시장가 조회 불가, 복구 건너뜀")
                        continue

                    # 전략 인스턴스 생성
                    strategy = StrategyInstance(
                        coin=strategy_record.coin,
                        market_price=market_price,
                        strategy_id=strategy_record.id
                    )

                    # 전략 스레드 시작
                    self._start_strategy_thread(strategy_record.coin, strategy)

                    logger.info(f"전략 {strategy_record.id} 복구 완료 ({strategy_record.coin})")

                except Exception as e:
                    logger.error(f"전략 {strategy_record.id} 복구 실패: {e}")
                    continue

        except Exception as e:
            logger.error(f"전략 복구 중 오류: {e}")
        finally:
            db.close()

    def on_market_scan(self, market_prices: dict, opportunities: list):
        """
        시장 스캔 콜백

        Args:
            market_prices: 코인 -> MarketPrice 딕셔너리
            opportunities: 진입 기회가 있는 코인 목록
        """
        # 새로운 전략을 시작할 수 있는지 확인
        current_count = len(self.active_strategies)
        max_count = ScannerConfig.MAX_CONCURRENT_STRATEGIES

        if current_count >= max_count:
            logger.debug(f"최대 동시 전략 수 도달 ({current_count}/{max_count})")
            return

        # 기회가 있는 코인에 대해 전략 시작
        for coin in opportunities:
            if coin in self.active_strategies:
                continue  # 이미 활성 전략 있음

            if current_count >= max_count:
                break

            # 전략 생성 및 시작
            market_price = market_prices[coin]
            strategy = StrategyInstance(coin, market_price)

            self._start_strategy_thread(coin, strategy)

            current_count += 1
            logger.info(f"{coin} 전략 시작 ({current_count}/{max_count} 전략 실행 중)")

    def _start_strategy_thread(self, coin: str, strategy: StrategyInstance):
        """
        별도 스레드에서 전략 실행

        Args:
            coin: 코인 심볼
            strategy: 전략 인스턴스
        """
        def run_strategy():
            logger.info(f"{coin} 전략 스레드 시작됨")
            try:
                while not shutdown_event.is_set():
                    should_continue = strategy.run()
                    if not should_continue:
                        logger.info(f"{coin} 전략 종료됨")
                        break

                    time.sleep(1)  # 반복 사이 짧은 대기

            except Exception as e:
                logger.error(f"{coin} 전략 스레드 오류: {e}", exc_info=True)
            finally:
                strategy.cleanup()
                # 활성 전략 목록에서 제거
                if coin in self.active_strategies:
                    del self.active_strategies[coin]
                if coin in self.strategy_threads:
                    del self.strategy_threads[coin]
                logger.info(f"{coin} 전략 스레드 완료")

        # Start thread
        thread = Thread(target=run_strategy, name=f"Strategy-{coin}", daemon=True)
        thread.start()

        self.active_strategies[coin] = strategy
        self.strategy_threads[coin] = thread

    def run(self):
        """봇 메인 루프 실행"""
        logger.info("봇 메인 루프 시작...")

        try:
            # 활성 전략 복구
            self.recover_active_strategies()

            # Start OHLC collector thread (if enabled)
            if self.ohlc_collector:
                def run_ohlc():
                    logger.info("OHLC 수집기 스레드 시작")
                    try:
                        self.ohlc_collector.run_continuous()
                    except Exception as e:
                        logger.error(f"OHLC 수집기 오류: {e}", exc_info=True)
                    finally:
                        logger.info("OHLC 수집기 스레드 종료")

                self.ohlc_thread = Thread(target=run_ohlc, name="OHLC-Collector", daemon=True)
                self.ohlc_thread.start()
                logger.info("OHLC 수집기 스레드 시작됨")

            # 지속적인 스캐너 실행
            while not shutdown_event.is_set():
                try:
                    # 시장 스캔
                    market_prices = self.scanner.scan_market()

                    if market_prices:
                        # 활성 전략 코인 목록 조회
                        active_coins = list(self.active_strategies.keys())

                        # 진입 기회 탐색
                        opportunities = self.scanner.find_opportunities(market_prices, active_coins)

                        # DB에 저장
                        self.scanner.save_scan_to_db(market_prices, opportunities)

                        # 새로운 전략 생성
                        self.on_market_scan(market_prices, opportunities)

                    # 다음 스캔까지 대기
                    for _ in range(ScannerConfig.SCAN_INTERVAL):
                        if shutdown_event.is_set():
                            break
                        time.sleep(1)

                except Exception as e:
                    logger.error(f"메인 루프 오류: {e}", exc_info=True)
                    time.sleep(5)  # 재시도 전 대기

        except KeyboardInterrupt:
            logger.info("사용자에 의해 봇 중지됨")
        finally:
            self.shutdown()

    def shutdown(self):
        """봇 정상 종료"""
        logger.info("봇 종료 중...")

        # 종료 신호 전달
        shutdown_event.set()

        # OHLC 수집기 스레드 종료 대기
        if self.ohlc_thread:
            logger.info("OHLC 수집기 스레드 종료 대기 중...")
            self.ohlc_thread.join(timeout=10)
            if self.ohlc_thread.is_alive():
                logger.warning("OHLC 수집기 스레드 시간 내 종료 실패")

        # 전략 스레드 종료 대기
        for coin, thread in list(self.strategy_threads.items()):
            logger.info(f"전략 스레드 {coin} 종료 대기 중...")
            thread.join(timeout=10)
            if thread.is_alive():
                logger.warning(f"전략 스레드 {coin} 시간 내 종료 실패")

        # 데이터베이스 종료
        close_db()

        logger.info("봇 종료 완료")


def signal_handler(signum, frame):
    """종료 시그널 처리"""
    logger.info(f"시그널 {signum} 수신, 종료 시작...")
    shutdown_event.set()


def main():
    """메인 진입점"""
    print("=" * 60)
    print("KIMP Arbitrage Bot")
    print("=" * 60)
    print()

    try:
        # 설정 검증
        logger.info("설정 검증 중...")
        validate_config()
        logger.info("설정 검증 완료")

        # Show mode
        mode = "PAPER TRADING" if BotConfig.PAPER_TRADING else "LIVE TRADING"
        print(f"Mode: {mode}")
        print(f"Target coins: {', '.join(BotConfig.TARGET_COINS)}")
        print(f"Target profit: {BotConfig.TARGET_PROFIT_PERCENT}%")
        print(f"Max position: {BotConfig.MAX_POSITION_KRW:,.0f} KRW / {BotConfig.MAX_POSITION_USDT:,.0f} USDT")
        print()

        if not BotConfig.PAPER_TRADING:
            response = input("WARNING: You are in LIVE TRADING mode. Continue? (yes/no): ")
            if response.lower() != 'yes':
                print("Exiting...")
                return

        # 데이터베이스 초기화
        logger.info("데이터베이스 초기화 중...")
        init_db()
        logger.info("데이터베이스 초기화 완료")

        # 시그널 핸들러 설정
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # 봇 매니저 생성 및 실행
        bot = BotManager()
        bot.run()

    except KeyboardInterrupt:
        logger.info("사용자에 의해 봇 중단됨")
    except Exception as e:
        logger.error(f"치명적 오류: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

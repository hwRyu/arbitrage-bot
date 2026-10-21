# 기존 프로젝트(old/) 분석 — 재작성 베이스 검토

## Context
`old/`의 KIMP 차익거래 봇을 베이스로 개선된 새 버전을 만들 예정(컨셉 일부 변경 예정).
이번 단계는 **분석만** 수행. 코드 변경 없음. 새 컨셉이 정해지면 이 분석을 근거로 설계 계획을 세운다.

## 1. 개요
- Python 3.10, ccxt(Upbit 현물 + Bybit USDT 무기한), SQLAlchemy+SQLite, yfinance 환율. 코드 ~4.3k줄, 문서(PROJECT_SPEC 2.6k줄) 별도.
- 전략(“역전 전략”): 김프 ≤ `MIN_ENTRY_PREMIUM`(기본 0%)이면 업비트 현물 매수 + 바이비트 1x 숏 → 김프가 `진입김프 + 목표수익 + 청산수수료 + 슬리피지`에 도달하면 청산.
- 김프 = 업비트KRW / (바이비트USDT가 × USD/KRW) − 1 (USDT/KRW 아님, 테더 프리미엄은 사용자가 수동 관리)
- 실거래 이력 있음 (2026-01-24~26 로그에 LIVE 모드로 여러 번 기동, XRP 주문 체결/실패 기록). 현재 DB의 `strategies`/`trades`는 비어 있고 `market_scans` 9,460건, `premium_ohlc` 2건만 남음.

## 2. 구조
| 레이어 | 파일 | 역할 |
|---|---|---|
| 진입점 | `main.py` (BotManager) | 스캐너 루프, 코인별 전략 스레드 생성, 재시작 복구, 시그널 종료 |
| L1 스캐너 | `core/scanner.py` | 10초마다 전 코인 시세·김프 계산, 기회 판단, `market_scans` 저장 |
| L2 전략 | `core/strategy.py` | 코인당 1 스레드, 상태머신 핸들러(if/elif 디스패치) |
| 상태머신 | `core/state_machine.py` | 허용 전이 테이블 (`StateHandler` 클래스는 미사용) |
| 수집기 | `core/ohlc_collector.py` | market_scans → 일봉 김프 OHLC 집계 |
| 서비스 | `services/upbit.py`, `bybit.py`, `exchange_rate.py` | ccxt 래퍼 + 페이퍼 시뮬레이션 내장, 싱글톤 |
| 설정 | `config/settings.py`, `config_manager.py` | .env 클래스 상수 + `CONFIG_SOURCE=db`시 클래스를 프록시로 바꿔치기 |
| 유틸 | `utils/calculator.py`, `validator.py`, `logger.py` | 김프/청산가/PnL 계산, 진입 검증 |

상태 흐름: `INIT → CHECK_CAPITAL → VALIDATE_ENTRY → ENTERING → HEDGED → MONITORING → PROFITABLE → EXITING → SETTLEMENT → EXITED` (+ STOP_LOSS / LIQUIDATION_RISK → REDUCE_POSITION, ERROR)

## 3. 살릴 만한 것
- 2-레이어(스캐너 / 포지션 라이프사이클) 분리 개념, DB에 상태 영속화 후 재시작 복구 아이디어
- 바이비트 먼저 → 업비트 실패 시 바이비트 롤백하는 진입 순서
- 체결 후 `fetch_order`로 실제 체결가/수량/수수료 보정 (실거래 경로)
- 호가창 VWAP 슬리피지 추정, OHLC 집계, 페이퍼 트레이딩 모드라는 발상

## 4. 치명적 버그 (실자금 영향)
1. **ERROR 상태 포지션 방치** — `run()` 예외 시 `ERROR`로 전이 후 `False` 반환 → 스레드 종료라 `_handle_error()`(청산 시도)가 실행되지 않음. 게다가 복구 대상 상태 목록에 `ERROR`가 없어 재시작해도 안 잡힘 → 헷지 포지션이 고아가 됨. (`core/strategy.py` run/_handle_error, `main.py` recover)
2. **청산 시 실패를 무시** — `_handle_exiting`은 바이비트/업비트 주문이 `None`(실패)이어도 그대로 SETTLEMENT로 넘어감 → 한쪽만 청산된 채 EXITED.
3. **업비트 매도 수량 불일치** — 매수는 `cost`로 하고 매도는 계산상 `position_size`로 함. 수수료·체결 차이로 실보유량 < 매도량 → 로그에 `insufficient_funds_ask` 실제 발생. 체결 수량을 Position에 반영하지 않음.
4. **ENTERING 상태 복구 시 중복 진입** — 주문 도중 크래시 후 재시작하면 거래소 실제 포지션 확인 없이 다시 주문.
5. **PnL 계산 오류** — `_handle_settlement`가 업비트 현물 손익만 계산하고 바이비트 숏 손익을 빼먹음(헷지인데 가격 하락 시 손실로 기록). 펀딩비는 USDT 값을 KRW로 그대로 더함. 실제 체결가가 아닌 ticker `last` 사용.
6. **펀딩비 미수집** — `FundingFee` 테이블/`funding_fees_collected` 필드는 있지만 기록하는 코드가 없음.
7. **REDUCE_POSITION이 DB에 반영 안 됨** — 메모리 `position.amount`만 줄이고 `entry_amount` 미갱신 → 재시작 시 원래 수량으로 청산 시도.

## 5. 로직/설계 문제
- **김프 정의와 실손익 괴리**: USD/KRW(yfinance, 1시간 캐시) 기준 김프는 환율만 움직여도 변하는데 포지션 실손익(KRW 현물 + USDT 숏)은 그만큼 안 변함 → 실제 이익 없이 목표 도달/손절 트리거 가능. 1시간 캐시라 모니터링 2초 주기와도 안 맞음.
- **검증 로직 오류**: 숏 진입인데 바이비트 호가 깊이/VWAP을 `asks`로 계산(매도는 `bids` 소진). 1x 청산가 체크(≈1.99배 ≥ 1.7배)는 항상 통과하는 무의미한 검사. 숏 1x에서 청산 위험 대응으로 "50% 축소"는 현물 쪽과 마진 이동 없이는 효과가 제한적.
- **진입 검증 실패 = ERROR**: 일시적 호가 부족도 ERROR → 전략 레코드가 계속 쌓임(기회 있는 동안 매 스캔마다 새 레코드).
- **수수료 상수 하드코딩**: Bybit taker 0.075%(현재 일반 등급은 더 낮을 수 있음, 확인 필요), 페이퍼 서비스 내부에도 별도 하드코딩.
- **주문 수량 정밀도 미처리**: `amount_to_precision`/최소 주문 단위 처리 없음 (로그의 retCode 10001 계열 실패와 연관 가능).
- **환율 실패 시 전체 스캔 중단**: 로그 ERROR 5,972건 중 약 5,600건이 환율 조회 실패.

## 6. 구조/코드 품질
- 스레드 + 공유 dict(`active_strategies`)를 락 없이 여러 스레드에서 수정. ccxt 인스턴스 싱글톤을 스레드 간 공유.
- SQLAlchemy `scoped_session`인데 전략 인스턴스가 세션을 장기 보유 → 로그에 `This transaction is closed` 발생.
- 페이퍼 모드는 호출마다 `ccxt.bybit()/upbit()` 새로 생성(매번 load_markets) → 느리고 429 유발. 페이퍼 잔고가 메모리에만 있어 재시작 시 초기화.
- 페이퍼/실거래 분기가 서비스 메서드마다 `if self.paper_trading`으로 섞여 있음 → 거래소 인터페이스 + 시뮬레이터 구현 분리 필요.
- 설정 DB 프록시가 모듈 import 시점에 클래스 바꿔치기 → 이해·테스트 어려움. `BybitSettings.POSITION_IDX_SHORT=0`인데 주석/문서는 2(Hedge) 라고 하는 등 문서-코드 불일치 다수.
- REST 폴링만 사용(웹소켓 없음), 알림(텔레그램 등) 없음, 테스트 0개, 미사용 코드(`StateHandler`, `run_continuous_scan`, `calculate_required_exit_premium`, `aiohttp`/`pandas` 의존성).
- `scanned_at`은 로컬시간(`datetime.now()`)으로 저장하는데 OHLC 수집기는 UTC 경계로 조회 → 일봉 날짜 경계가 9시간 어긋남.

## 7. 재작성 시 우선 고려 사항 (컨셉 확정 전 공통)
1. 거래소 어댑터 인터페이스 + 페이퍼 시뮬레이터 분리, 실제 체결값 기반 포지션 관리
2. 시작 시/주기적으로 **거래소 실제 잔고·포지션과 DB 상태 대조(reconciliation)**
3. 진입/청산을 양쪽 체결 확인까지 포함한 원자적 단계로, 실패 시 재시도·알림
4. 김프 기준 환율 재정의(USDT/KRW 또는 손익과 일치하는 지표), 실시간 데이터는 웹소켓
5. 스레드 대신 asyncio 단일 이벤트 루프 고려, 순수 계산 로직은 단위 테스트
6. 텔레그램 등 알림, 수수료·정밀도는 거래소 API에서 조회

## 다음 단계
- 새 컨셉 확정 후 이 문서를 근거로 신규 구조/구현 계획 작성

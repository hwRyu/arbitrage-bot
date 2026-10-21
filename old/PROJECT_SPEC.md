# 김치 프리미엄 차익거래 봇 프로젝트 기획서

> **프로젝트명**: KIMP (Kimchi Premium) Arbitrage Bot
> **버전**: 1.2.0
> **작성일**: 2025-01-23
> **최종 수정**: 2025-01-26
> **언어**: Python 3.10+
---

## 📋 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [핵심 전략](#2-핵심-전략)
3. [시스템 아키텍처](#3-시스템-아키텍처)
4. [상태 머신 설계](#4-상태-머신-설계)
5. [데이터베이스 스키마](#5-데이터베이스-스키마)
6. [수수료 및 비용](#6-수수료-및-비용)
7. [수식 및 계산법](#7-수식-및-계산법)
8. [리스크 관리](#8-리스크-관리)
9. [설정값 가이드](#9-설정값-가이드)
10. [API 연동](#10-api-연동)
11. [운영 가이드](#11-운영-가이드)
12. [개발 가이드](#12-개발-가이드)
13. [향후 개선 사항](#13-향후-개선-사항)

---

## 1. 프로젝트 개요

### 1.1 목적
비트코인 및 알트코인의 한국 시장 프리미엄(김치 프리미엄)을 활용한 **무위험 차익거래 자동화 봇** 개발

### 1.2 핵심 컨셉
- **헷지 전략**: 국내 거래소(업비트) 현물 매수 + 해외 거래소(바이비트) 선물 숏 포지션
- **USDT 환헷징**: 테더 프리미엄을 분리하여 정확한 코인 프리미엄만 계산
- **펀딩비 수익**: 롱 우세 시 숏 포지션으로 8시간마다 펀딩비 추가 수익
- **자동화**: 기회 탐색 → 진입 → 모니터링 → 청산까지 완전 자동화

### 1.3 기대 효과
- **목표 수익률**: 거래당 1% (설정 가능)
- **무위험**: 양방향 헷지로 가격 변동 위험 제거
- **추가 수익**: 펀딩비 수익 (연 3-10%)
- **자동 운영**: 24/7 자동 모니터링 및 실행

### 1.4 기술 스택
- **언어**: Python 3.10+
- **거래소 API**: ccxt (통합 라이브러리)
- **데이터베이스**: SQLite (개발/테스트), MySQL (프로덕션 선택)
- **환율 API**: yfinance (Yahoo Finance) - 실시간 USD/KRW 환율 조회
- **비동기**: 멀티스레딩 (코인별 독립 실행)

---

## 2. 핵심 전략

### 2.1 전략 개요

#### 기본 원리
```
김치 프리미엄 역전 차익거래:
1. 김프가 낮거나 마이너스일 때 (해외가 더 비쌀 때):
   - 업비트에서 현물 매수 (KRW)
   - 바이비트에서 동일 수량 숏 포지션 진입 (USDT)
2. 김프가 상승하여 목표 수익에 도달할 때까지 대기
3. 양쪽 동시 청산하여 수익 실현
```

**전략 방향**:
- 기존 전략: 김프 높을 때 진입 → 김프 낮아질 때 청산
- **현재 전략**: 김프 낮거나 마이너스일 때 진입 → 김프 높아질 때 청산

#### 수익 구조
```
수익 = (청산시 김프 - 진입시 김프) + 펀딩비 수익 - 거래 수수료 - 슬리피지
```

**핵심**: 김프 차이만큼 수익 발생 (진입 시 낮은 김프, 청산 시 높은 김프)

### 2.2 진입 조건

#### 필수 조건
1. **김프 충족**: 현재 김프 ≤ MIN_ENTRY_PREMIUM (기본값: 0%, 설정 가능)
2. **청산가 안전**: 청산가 ≥ 진입가 × 1.7배 (레버리지 1배 고정)
3. **호가창 깊이**: 각 거래소 호가 ≥ 포지션 크기 × 10배
4. **슬리피지**: 예상 슬리피지 < 0.5%
5. **자본 확보**: KRW 잔고 및 USDT 증거금 충분

#### 김프 계산 (기본 공식)
```
김프(%) = (업비트 KRW 가격 / (바이비트 USD 가격 × USD/KRW 환율)) - 1
```

**중요**:
- USD/KRW 환율은 yfinance(Yahoo Finance)에서 조회한 실시간 환율 사용
- USDT 프리미엄은 고려하지 않음 (사용자가 수동으로 USDT 관리)
- 사용자는 USDT 프리미엄 0.5% 이하일 때 국내에서 USDT 매수 후 해외로 전송
- 청산 시 USDT 프리미엄 0.5% 이상일 때 해외에서 국내로 전송하여 매도
- 따라서 봇은 거래소 간 송금을 고려할 필요 없음

#### 진입 시점 예시
```
- SOL 업비트: 200,000원
- SOL 바이비트: 150 USD
- USD/KRW: 1,340원
- 김프 = (200,000 / (150 × 1,340)) - 1 = -0.50% (마이너스 김프)

→ MIN_ENTRY_PREMIUM이 0%라면 진입 조건 충족
→ 해외가 더 비싼 상태에서 진입
```

### 2.3 포지션 관리

#### 포지션 크기 결정
```python
# KRW로 살 수 있는 수량
amount_from_krw = MAX_POSITION_KRW / 업비트_가격

# USDT로 숏 잡을 수 있는 수량
amount_from_usdt = MAX_POSITION_USDT / 바이비트_가격

# 둘 중 작은 값 선택 (완전 헷지)
position_size = min(amount_from_krw, amount_from_usdt)
```

#### 레버리지
- **기본**: 1배 (무레버리지)
- **이유**: 청산 위험 최소화, 안전한 운영

#### 청산가 계산 (1배 레버리지 숏)
```
청산가 = 진입가 × (2 - MMR)

여기서 MMR(Maintenance Margin Rate):
- BTC, ETH: 0.5%
- 기타 알트: 1.0%
```

**예시**: SOL을 100 USD에 진입 (MMR 1%)
```
청산가 = 100 × (2 - 0.01) = 199 USD
```
→ 가격이 2배 가까이 상승해야 청산 (매우 안전)

### 2.4 청산 조건

#### 정상 청산 (목표 도달)
```
순수익(%) ≥ 목표 수익률

순수익 = (현재 김프 - 진입 김프) - 왕복 수수료 - 예상 슬리피지

여기서:
- 왕복 수수료 = 0.25% (진입 0.125% + 청산 0.125%)
- 예상 슬리피지 = 진입 시 계산된 값 (기본 0.1% 버퍼)
```

**예시 1** (마이너스 김프에서 진입):
- 진입 김프: -0.5%
- 현재 김프: 1.2%
- 목표 수익률: 1.0%
- 왕복 수수료: 0.25%
- 슬리피지: 0.1%
- **순수익** = (1.2% - (-0.5%)) - 0.25% - 0.1% = 1.35%
- 1.35% ≥ 1.0% → **청산 조건 충족**

**예시 2** (김프 0%에서 진입):
- 진입 김프: 0.0%
- 현재 김프: 1.5%
- 목표 수익률: 1.0%
- 왕복 수수료: 0.25%
- 슬리피지: 0.1%
- **순수익** = (1.5% - 0.0%) - 0.25% - 0.1% = 1.15%
- 1.15% ≥ 1.0% → **청산 조건 충족**

#### 긴급 청산
1. **손절**: 미실현 손실 ≤ -5%
2. **청산 위험**: 현재가 / 청산가 < 1.2배
3. **포지션 축소**: 현재가 / 청산가 < 1.3배 (50% 축소)

**참고**: 레버리지 1배 고정으로 청산 위험은 극히 낮음

### 2.5 횡보 시 전략

#### 펀딩비 수집 (현재 구현)
- 바이비트 선물은 8시간마다 펀딩비 정산
- 롱 우세 시: 숏 포지션 보유자가 펀딩비 수취
- 평균 펀딩비: 0.01% (8시간) = 연 10.95%

#### 향후 추가 가능 전략
1. **USDT 차익**: 테더 프리미엄 변동 활용
2. **부분 청산**: 일부 수익 실현 후 재진입
3. **스테이킹**: 현물 스테이킹 (단, 즉시 청산 불가 주의)

---

## 3. 시스템 아키텍처

### 3.1 전체 구조

```
┌─────────────────────────────────────────┐
│          Main Process (main.py)         │
│  - 봇 초기화                               │
│  - 활성 전략 복구                          │
│  - 스캐너 및 전략 관리                      │
└─────────────┬───────────────────────────┘
              │
      ┌───────┴────────┬───────────┐
      │                │           │
      ▼                ▼           ▼
┌───────────┐    ┌──────────────┐ ┌──────────────┐
│ Layer 1   │    │  Layer 2     │ │  Layer 3     │
│ Scanner   │───▶│  Strategy    │ │  OHLC        │
│           │    │  Instances   │ │  Collector   │
└─────┬─────┘    └──────┬───────┘ └──────┬───────┘
      │                 │                │
      ▼                 ▼                │
┌─────────────────────────────┐          │
│      External Services      │          │
│  - Upbit API                │          │
│  - Bybit API                │          │
│  - 환율 API                  │          │
└─────────────────────────────┘          │
      │                                  │
      ▼                                  ▼
┌─────────────────────────────┐
│      Database (SQLite)      │
│  - strategies               │
│  - trades                   │
│  - funding_fees             │
│  - market_scans             │◀─ (읽기)
│  - bot_logs                 │
│  - premium_ohlc             │◀─ (쓰기)
└─────────────────────────────┘
```

### 3.2 Layer 1: Market Scanner

**역할**: 시장을 지속적으로 스캔하여 기회 탐색

**동작**:
1. 10초마다 모든 타겟 코인 가격 조회 (스캐너)
2. 김프 계산
3. 진입 기회 판단
4. DB에 스캔 결과 저장

**참고**: 포지션 진입 후 모니터링은 2초마다 수행 (빠른 김프 변화 감지)
5. 기회 발견 시 Layer 2 전략 인스턴스 생성

**특징**:
- 상태 없음 (stateless)
- 빠르고 가벼움
- 여러 코인 동시 스캔

### 3.3 Layer 2: Strategy Instance

**역할**: 한 코인에 대한 전략을 담당 (진입 → 청산)

**동작**:
1. 전략 생성 (INIT)
2. 자본 확인 (CHECK_CAPITAL)
3. 진입 검증 (VALIDATE_ENTRY)
4. 포지션 진입 (ENTERING)
5. 실시간 모니터링 (MONITORING)
6. 목표 도달 시 청산 (EXITING)
7. 정산 및 종료 (SETTLEMENT → EXITED)

**특징**:
- 코인당 1개만 존재
- 독립적인 스레드로 실행
- DB에 상태 저장 (재시작 복구 가능)
- 오래 살아있을 수 있음 (수 시간 ~ 수일)

### 3.4 Layer 3: OHLC Collector

**역할**: 김치 프리미엄 OHLC 데이터 수집 및 저장

**동작**:
1. 60초마다 market_scans 테이블에서 데이터 읽기
2. 일별 OHLC 캔들 생성/업데이트
   - Premium OHLC (시가/고가/저가/종가)
   - KRW Price OHLC (업비트)
   - USD Price OHLC (바이비트)
   - Exchange Rate OHLC (USD/KRW)
3. 날짜 경계(KST 자정)에서 전날 캔들 확정
4. 보관 기간 초과 데이터 자동 정리

**특징**:
- **완전 독립 실행**: 데몬 스레드로 실행, 메인 프로세스와 완전 격리
- **독립 DB 세션**: 자체 SessionLocal() 사용, 락 경합 없음
- **예외 격리**: 에러 발생 시에도 메인 트레이딩에 영향 없음
- **읽기 전용**: market_scans 테이블 읽기만 수행, 기존 데이터 수정 안 함
- **재시작 복구**: 봇 재시작 시 자동으로 기존 캔들 이어서 업데이트
- **비활성화 가능**: OHLC_ENABLED=false로 언제든 완전 비활성화
- **백테스팅 지원**: 수집된 데이터는 향후 백테스팅 엔진에서 활용

**프로세스 격리 보장**:
- 메인 스캐너: 10초마다 실행
- OHLC 수집기: 60초마다 실행 (충돌 없음)
- CPU 사용량: < 1%
- 메모리 사용: ~1MB
- 네트워크: API 호출 없음 (DB 읽기만)

### 3.5 디렉토리 구조

```
kimp/
├── config/                 # 설정
│   ├── __init__.py
│   └── settings.py        # 모든 상수 및 환경변수
│
├── core/                   # 핵심 로직
│   ├── __init__.py
│   ├── scanner.py         # Layer 1: 시장 스캐너
│   ├── state_machine.py   # 상태 머신
│   ├── strategy.py        # Layer 2: 전략 인스턴스
│   └── ohlc_collector.py  # Layer 3: OHLC 데이터 수집기
│
├── services/               # 외부 서비스 연동
│   ├── __init__.py
│   ├── upbit.py           # 업비트 API 래퍼
│   ├── bybit.py           # 바이비트 API 래퍼
│   └── exchange_rate.py   # 환율 API
│
├── models/                 # 데이터 모델
│   ├── __init__.py
│   ├── database.py        # DB 설정 및 세션
│   ├── models.py          # SQLAlchemy 모델
│   └── position.py        # 데이터 클래스
│
├── utils/                  # 유틸리티
│   ├── __init__.py
│   ├── calculator.py      # 계산 함수
│   ├── logger.py          # 로깅 설정
│   ├── validator.py       # 검증 로직
│   └── ohlc_utils.py      # OHLC 데이터 조회 헬퍼
│
├── scripts/                # 유틸리티 스크립트
│   └── check_ohlc_gaps.py # OHLC 데이터 갭 진단 도구
│
├── tests/                  # 테스트 (향후 추가)
├── logs/                   # 로그 파일
├── main.py                 # 진입점
├── .env                    # 환경변수 (gitignore)
├── .env.example            # 환경변수 예시
├── requirements.txt        # 패키지 목록
├── README.md               # 사용자 가이드
└── PROJECT_SPEC.md         # 본 기획서
```

---

## 4. 상태 머신 설계

### 4.1 상태 정의

```python
States = {
    'INIT': '초기화',
    'CHECK_CAPITAL': '자본 확인',
    # 'SCAN_OPPORTUNITY': '기회 탐색',  # 제거됨: 실제 구현에서 미사용
    'VALIDATE_ENTRY': '진입 검증',
    'ENTERING': '포지션 진입 중',
    'HEDGED': '헷지 완료',
    'MONITORING': '실시간 모니터링',
    'PROFITABLE': '목표 수익 도달',
    'REDUCE_POSITION': '포지션 축소',
    'STOP_LOSS': '손절',
    'LIQUIDATION_RISK': '청산 위험',
    'EXITING': '청산 중',
    'SETTLEMENT': '정산',
    'EXITED': '종료',
    'ERROR': '에러'
}
```

### 4.2 상태 전이 다이어그램

```
INIT
  ↓
CHECK_CAPITAL
  ↓
VALIDATE_ENTRY ←──────┐
  ↓                   │
  ├─ (검증 실패) ─────┘
  ↓ (검증 성공)
ENTERING
  ↓
HEDGED
  ↓
MONITORING ←─────────────┐
  ↓                      │
  ├─ (목표 도달) → PROFITABLE
  ├─ (청산 위험) → LIQUIDATION_RISK → REDUCE_POSITION ─┘
  ├─ (손절) → STOP_LOSS
  ↓
EXITING
  ↓
SETTLEMENT
  ↓
EXITED
```

### 4.3 상태별 설명

#### INIT (초기화)
- 전략 인스턴스 생성
- DB에 레코드 생성
- 다음: CHECK_CAPITAL

#### CHECK_CAPITAL (자본 확인)
- 업비트 KRW 잔고 확인
- 바이비트 USDT 잔고 확인
- 필요 자본 계산 및 비교
- 부족 시: ERROR
- 충분 시: VALIDATE_ENTRY

#### VALIDATE_ENTRY (진입 검증)
- 청산가 안전성 체크
- 호가창 깊이 확인
- 예상 슬리피지 계산
- 검증 실패 시: ERROR
- 검증 성공 시: ENTERING

#### ENTERING (포지션 진입)
- 바이비트 숏 포지션 진입
- 업비트 현물 매수
- 한쪽 실패 시 롤백
- 성공 시: HEDGED

#### HEDGED (헷지 완료)
- 포지션 정보 DB 저장
- 다음: MONITORING

#### MONITORING (실시간 모니터링)
- **2초마다** 가격 및 환율 확인 (빠른 김프 변화 감지)
  - 업비트 KRW 가격 조회
  - 바이비트 USD 가격 조회
  - USD/KRW 환율 업데이트 (yfinance, 1시간 캐시)
- 김프 계산 (최신 환율 반영)
- 청산가 거리 체크
- 미실현 손익 계산
- 조건에 따라:
  - PROFITABLE (목표 도달)
  - LIQUIDATION_RISK (청산 위험)
  - STOP_LOSS (손절)

#### PROFITABLE (목표 수익 도달)
- 목표 김프 도달
- 다음: EXITING

#### LIQUIDATION_RISK (청산 위험)
- 청산가 너무 근접
- 다음: REDUCE_POSITION

#### REDUCE_POSITION (포지션 축소)
- 50% 포지션 정리
- 다음: MONITORING (계속 모니터링)

#### STOP_LOSS (손절)
- 미실현 손실 -5% 이상
- 다음: EXITING

#### EXITING (청산)
- 바이비트 숏 포지션 청산
- 업비트 현물 매도
- 성공 시: SETTLEMENT

#### SETTLEMENT (정산)
- 최종 손익 계산
- DB 업데이트
- 다음: EXITED

#### EXITED (종료)
- 전략 종료
- 스레드 종료

#### ERROR (에러)
- 에러 발생
- 포지션 있으면 EXITING 시도
- 없으면 EXITED

### 4.4 상태 전이 규칙

**허용된 전이 (StateMachine.ALLOWED_TRANSITIONS)**:
```python
{
    'INIT': ['CHECK_CAPITAL', 'ERROR'],
    'CHECK_CAPITAL': ['VALIDATE_ENTRY', 'ERROR'],
    'VALIDATE_ENTRY': ['ENTERING', 'ERROR'],
    'ENTERING': ['HEDGED', 'ERROR'],
    'HEDGED': ['MONITORING', 'ERROR'],
    'MONITORING': [
        'PROFITABLE',
        'REDUCE_POSITION',
        'STOP_LOSS',
        'LIQUIDATION_RISK',
        'EXITING',
        'ERROR'
    ],
    'PROFITABLE': ['EXITING', 'ERROR'],
    'REDUCE_POSITION': ['MONITORING', 'EXITING', 'ERROR'],
    'STOP_LOSS': ['EXITING', 'ERROR'],
    'LIQUIDATION_RISK': ['REDUCE_POSITION', 'EXITING', 'ERROR'],
    'EXITING': ['SETTLEMENT', 'ERROR'],
    'SETTLEMENT': ['EXITED', 'ERROR'],
    'EXITED': [],
    'ERROR': ['EXITING', 'EXITED']
}
```

---

## 5. 데이터베이스 스키마

### 5.1 strategies (전략 실행 기록)

**목적**: 하나의 전략 = 한 코인의 진입부터 청산까지 전체 사이클

| 컬럼명 | 타입 | 설명 |
|--------|------|------|
| id | INTEGER PK | 전략 ID |
| coin | VARCHAR(20) | 코인 심볼 (SOL, XRP 등) |
| state | VARCHAR(50) | 현재 상태 |
| created_at | DATETIME | 생성 시각 |
| updated_at | DATETIME | 수정 시각 |
| exited_at | DATETIME | 종료 시각 |
| **진입 정보** | | |
| entry_premium | FLOAT | 진입 시 김프 (%) |
| entry_price_krw | FLOAT | 업비트 진입가 (KRW) |
| entry_price_usd | FLOAT | 바이비트 진입가 (USD) |
| entry_amount | FLOAT | 진입 수량 |
| entry_value_krw | FLOAT | 진입 금액 (KRW) |
| exchange_rate | FLOAT | 진입 시 환율 |
| usdt_krw | FLOAT | 진입 시 USDT/KRW |
| **포지션 정보** | | |
| upbit_order_id | VARCHAR(100) | 업비트 주문 ID |
| bybit_order_id | VARCHAR(100) | 바이비트 주문 ID |
| bybit_position_id | VARCHAR(100) | 바이비트 포지션 ID |
| leverage | INTEGER | 레버리지 |
| liquidation_price | FLOAT | 청산가 (USD) |
| initial_margin | FLOAT | 초기 증거금 (USDT) |
| **수익 정보** | | |
| target_premium | FLOAT | 목표 청산 김프 (%) |
| current_premium | FLOAT | 현재 김프 (%) |
| funding_fee_earned | FLOAT | 누적 펀딩비 (KRW) |
| total_fees_paid | FLOAT | 총 수수료 (KRW) |
| **청산 정보** | | |
| exit_premium | FLOAT | 청산 시 김프 (%) |
| exit_price_krw | FLOAT | 업비트 청산가 |
| exit_price_usd | FLOAT | 바이비트 청산가 |
| exit_value_krw | FLOAT | 청산 금액 (KRW) |
| realized_pnl_krw | FLOAT | 실현 손익 (KRW) |
| realized_pnl_percent | FLOAT | 실현 손익률 (%) |
| exit_reason | VARCHAR(100) | 종료 이유 |
| **기타** | | |
| is_paper_trading | BOOLEAN | 페이퍼 트레이딩 여부 |

### 5.2 trades (거래 로그)

**목적**: 각 매수/매도 거래를 기록

| 컬럼명 | 타입 | 설명 |
|--------|------|------|
| id | INTEGER PK | 거래 ID |
| strategy_id | INTEGER FK | 전략 ID |
| exchange | VARCHAR(20) | 거래소 (upbit, bybit) |
| side | VARCHAR(20) | 방향 (buy, sell, open_short, close_short) |
| coin | VARCHAR(20) | 코인 심볼 |
| amount | FLOAT | 거래 수량 |
| price | FLOAT | 거래 가격 |
| value | FLOAT | 거래 금액 |
| fee | FLOAT | 수수료 |
| fee_currency | VARCHAR(10) | 수수료 통화 |
| order_id | VARCHAR(100) | 주문 ID |
| order_type | VARCHAR(20) | 주문 타입 (market, limit) |
| expected_price | FLOAT | 예상 가격 |
| slippage | FLOAT | 슬리피지 (%) |
| executed_at | DATETIME | 체결 시각 |
| is_paper_trading | BOOLEAN | 페이퍼 트레이딩 여부 |

### 5.3 funding_fees (펀딩비 기록)

**목적**: 8시간마다 발생하는 펀딩비 수익 기록

| 컬럼명 | 타입 | 설명 |
|--------|------|------|
| id | INTEGER PK | ID |
| strategy_id | INTEGER FK | 전략 ID |
| coin | VARCHAR(20) | 코인 심볼 |
| funding_rate | FLOAT | 펀딩비율 (%) |
| position_value | FLOAT | 포지션 가치 (USDT) |
| funding_fee | FLOAT | 받은 펀딩비 (USDT) |
| funding_fee_krw | FLOAT | 받은 펀딩비 (KRW) |
| collected_at | DATETIME | 수취 시각 |

### 5.4 market_scans (시장 스캔 로그)

**목적**: 스캐너의 시장 스캔 결과를 주기적으로 기록 (데이터 분석용)

| 컬럼명 | 타입 | 설명 |
|--------|------|------|
| id | INTEGER PK | ID |
| coin | VARCHAR(20) | 코인 심볼 |
| premium | FLOAT | 김프 (%) |
| krw_price | FLOAT | 업비트 가격 (KRW) |
| usd_price | FLOAT | 바이비트 가격 (USD) |
| usdt_krw | FLOAT | USDT/KRW 가격 |
| exchange_rate | FLOAT | USD/KRW 환율 |
| is_opportunity | BOOLEAN | 진입 기회 여부 |
| opportunity_reason | VARCHAR(200) | 진입 불가 이유 |
| orderbook_depth_krw | FLOAT | 업비트 호가 깊이 |
| orderbook_depth_usd | FLOAT | 바이비트 호가 깊이 |
| scanned_at | DATETIME | 스캔 시각 |

### 5.5 bot_logs (봇 운영 로그)

**목적**: 에러, 경고, 중요 이벤트를 DB에 기록

| 컬럼명 | 타입 | 설명 |
|--------|------|------|
| id | INTEGER PK | ID |
| level | VARCHAR(20) | 로그 레벨 (INFO, WARNING, ERROR) |
| message | TEXT | 로그 메시지 |
| module | VARCHAR(100) | 발생 모듈 |
| strategy_id | INTEGER | 관련 전략 ID |
| coin | VARCHAR(20) | 관련 코인 |
| error_type | VARCHAR(100) | 에러 타입 |
| error_trace | TEXT | 에러 스택 트레이스 |
| created_at | DATETIME | 발생 시각 |

### 5.6 premium_ohlc (프리미엄 OHLC 데이터)

**목적**: 김치 프리미엄의 일봉 OHLC 데이터 저장 (분석 및 백테스팅용)

| 컬럼명 | 타입 | 설명 |
|--------|------|------|
| id | INTEGER PK | ID |
| coin | VARCHAR(20) | 코인 심볼 (인덱스) |
| timeframe | VARCHAR(10) | 타임프레임 ('1d', 향후 '4h', '1h') |
| date_kst | VARCHAR(10) | KST 날짜 (YYYY-MM-DD) (인덱스) |
| **Premium OHLC** | | |
| premium_open | FLOAT | 프리미엄 시가 (%) |
| premium_high | FLOAT | 프리미엄 고가 (%) |
| premium_low | FLOAT | 프리미엄 저가 (%) |
| premium_close | FLOAT | 프리미엄 종가 (%) |
| **KRW Price OHLC** | | |
| krw_open | FLOAT | 업비트 시가 (KRW) |
| krw_high | FLOAT | 업비트 고가 (KRW) |
| krw_low | FLOAT | 업비트 저가 (KRW) |
| krw_close | FLOAT | 업비트 종가 (KRW) |
| **USD Price OHLC** | | |
| usd_open | FLOAT | 바이비트 시가 (USD) |
| usd_high | FLOAT | 바이비트 고가 (USD) |
| usd_low | FLOAT | 바이비트 저가 (USD) |
| usd_close | FLOAT | 바이비트 종가 (USD) |
| **Exchange Rate OHLC** | | |
| exchange_rate_open | FLOAT | 환율 시가 (USD/KRW) |
| exchange_rate_high | FLOAT | 환율 고가 (USD/KRW) |
| exchange_rate_low | FLOAT | 환율 저가 (USD/KRW) |
| exchange_rate_close | FLOAT | 환율 종가 (USD/KRW) |
| **Metadata** | | |
| scan_count | INTEGER | 캔들 생성에 사용된 스캔 수 |
| is_finalized | BOOLEAN | 캔들 확정 여부 (날짜 완료 시 true) |
| created_at | DATETIME | 생성 시각 (UTC) |
| updated_at | DATETIME | 수정 시각 (UTC) |

**제약조건**:
- UNIQUE(coin, timeframe, date_kst): 중복 캔들 방지
- INDEX(coin, date_kst): 코인별 날짜 조회 최적화
- INDEX(timeframe, date_kst): 타임프레임별 조회 최적화
- INDEX(is_finalized, date_kst): 미확정 캔들 조회 최적화

**데이터 수집 방식**:
- market_scans 테이블에서 해당 날짜(KST 기준)의 모든 스캔 데이터 읽기
- Open: 첫 스캔 값
- High: 최댓값
- Low: 최솟값
- Close: 마지막 스캔 값
- scan_count: 스캔 횟수 (데이터 품질 지표)

**타임존 처리**:
- 저장: UTC 타임스탬프 (created_at, updated_at)
- 날짜 경계: KST 기준 (date_kst)
- KST 00:00-23:59 = UTC (전날) 15:00 - (당일) 14:59

### 5.7 config_settings (설정 저장소)

**목적**: DB 기반 설정 관리 (CONFIG_SOURCE=db일 때)

| 컬럼명 | 타입 | 설명 |
|--------|------|------|
| id | INTEGER PK | ID |
| **설정 정보** | | |
| config_class | VARCHAR(50) | 설정 클래스 ('BotConfig', 'RiskParams', 'ScannerConfig') |
| key | VARCHAR(100) | 설정 키 ('TARGET_COINS', 'MAX_POSITION_KRW' 등) |
| value | TEXT | 설정 값 (JSON 인코딩) |
| value_type | VARCHAR(20) | 타입 ('str', 'int', 'float', 'bool', 'list') |
| **메타데이터** | | |
| description | TEXT | 설정 설명 (한글) |
| default_value | TEXT | 기본값 (참고용) |
| **버전 관리** | | |
| version | INTEGER | 버전 (업데이트 시 +1) |
| updated_by | VARCHAR(50) | 업데이트 주체 ('system', 'admin', 'auto') |
| **타임스탬프** | | |
| created_at | DATETIME | 생성 시각 |
| updated_at | DATETIME | 수정 시각 |

**제약조건**:
- UNIQUE(config_class, key): 중복 설정 방지
- INDEX(config_class, key): 조회 최적화

**지원하는 설정 (11개)**:

| config_class | key | value_type | 설명 |
|--------------|-----|------------|------|
| BotConfig | TARGET_COINS | list | 타겟 코인 목록 |
| BotConfig | TARGET_PROFIT_PERCENT | float | 목표 수익률 (%) |
| BotConfig | MIN_ENTRY_PREMIUM | float | 최대 진입 김프 (%) |
| BotConfig | MAX_POSITION_KRW | float | 최대 포지션 (KRW) |
| BotConfig | MAX_POSITION_USDT | float | 최대 포지션 (USDT) |
| RiskParams | LIQUIDATION_BUFFER | float | 청산가 안전마진 (배수) |
| RiskParams | MIN_ORDERBOOK_DEPTH_MULTIPLIER | float | 최소 호가창 깊이 배수 |
| RiskParams | STOP_LOSS_PERCENT | float | 손절 기준 (%) |
| ScannerConfig | SCAN_INTERVAL | int | 스캔 주기 (초) |
| ScannerConfig | MAX_CONCURRENT_STRATEGIES | int | 최대 동시 전략 수 |
| ScannerConfig | MONITORING_INTERVAL | int | 모니터링 주기 (초) |

**참고**: API 키는 보안상 DB에 저장하지 않으며 항상 .env에서 로드됨

---

## 6. 수수료 및 비용

### 6.1 거래 수수료

#### 업비트 (현물)
- **거래 수수료**: 0.05%
- **입출금 수수료**:
  - KRW 입출금: 무료
  - USDT 출금: 없음 (TRC20 전송은 바이비트로)

#### 바이비트 (선물)
- **Maker 수수료**: -0.025% (리베이트)
- **Taker 수수료**: 0.075%
- **펀딩비**: 8시간마다 (보통 0.01%, 변동)
- **USDT 출금 (TRC20)**: 1 USDT

### 6.2 총 수수료 계산

#### 진입 시 (Entry)
```
진입 수수료 = 업비트 매수(0.05%) + 바이비트 숏 진입(0.075%)
           = 0.125%
```

#### 청산 시 (Exit)
```
청산 수수료 = 업비트 매도(0.05%) + 바이비트 숏 청산(0.075%)
           = 0.125%
```

#### 왕복 (Round-trip)
```
총 수수료 = 진입 + 청산 = 0.25%
```

### 6.3 최대 진입 김프 (역전 전략)

**목표 수익 1%를 얻기 위한 최대 진입 김프**:
```
최대 진입 김프 = MIN_ENTRY_PREMIUM (기본값: 0%, 설정 가능)
```

**전략 개념**:
- 기존: 김프가 높을 때(≥ 1.25%) 진입 → 김프 낮아질 때 청산
- **현재**: 김프가 낮을 때(≤ 0%) 진입 → 김프 높아질 때 청산
- MIN_ENTRY_PREMIUM을 0% 미만(예: -0.5%)으로 설정하면 더 낮은 김프에서만 진입
- MIN_ENTRY_PREMIUM을 0% 초과(예: 0.5%)로 설정하면 약간 높은 김프에서도 진입

**수익 계산**:
```
필요한 청산 김프 = 진입 김프 + 목표 수익 + 왕복 수수료 + 슬리피지

예시 (진입 김프 0%, 목표 1%):
필요한 청산 김프 = 0% + 1.0% + 0.25% + 0.1% = 1.35%
```

### 6.4 슬리피지

- **버퍼**: 0.1%
- **최대 허용**: 0.5%
- **예상 슬리피지 계산**: VWAP 방식으로 호가창 분석

### 6.5 USDT 전송 비용

- **TRC20 네트워크**: 1 USDT (고정)
- **전송 시기**: USDT 프리미엄 0.5% 이하일 때 구매 후 전송

---

## 7. 수식 및 계산법

### 7.1 김프 계산

#### 김프 계산 공식 (본 전략에서 사용) ⭐
```
김프(%) = (업비트 KRW 가격 / (바이비트 USD 가격 × USD/KRW 환율)) - 1
```

**사용하는 환율**:
- USD/KRW: yfinance(Yahoo Finance)에서 조회한 실시간 환율
- USDT/KRW는 사용하지 않음

**USDT 프리미엄을 고려하지 않는 이유**:
1. 사용자가 수동으로 USDT 관리
   - 진입 전: USDT 프리미엄 0.5% 이하일 때 국내에서 USDT 매수 → 해외로 전송
   - 청산 후: USDT 프리미엄 0.5% 이상일 때 해외에서 국내로 전송 → USDT 매도
2. 봇은 거래소 간 USDT 송금을 수행하지 않음
3. 공식 USD/KRW 환율을 사용하여 순수한 코인 가격 차이만 추적

**예시 1** (마이너스 김프):
```
- 업비트 SOL/KRW: 200,000원
- 바이비트 SOL/USD: 150 USD
- USD/KRW 환율: 1,340원

김프 = (200,000 / (150 × 1,340)) - 1
     = (200,000 / 201,000) - 1
     = -0.50%
```
→ 해외가 더 비싼 상태 (진입 조건 충족 가능)

**예시 2** (플러스 김프):
```
- 업비트 SOL/KRW: 210,000원
- 바이비트 SOL/USD: 150 USD
- USD/KRW 환율: 1,340원

김프 = (210,000 / (150 × 1,340)) - 1
     = (210,000 / 201,000) - 1
     = +4.48%
```
→ 국내가 더 비싼 상태 (청산 조건 검토)

### 7.2 청산가 계산

#### Bybit Isolated Margin 청산가 (숏)

**본 전략은 레버리지 1배 고정**:
```
청산가 = 진입가 × (2 - MMR)
```

**레버리지 N배 공식 (참고용)**:
```
청산가 = 진입가 × (1 + (1 - MMR) / N)
```

여기서:
- **MMR** (Maintenance Margin Rate): 유지 증거금률
- **중요**: MMR은 고정값이 아닌 Bybit API에서 코인별로 조회하여 사용
- API 엔드포인트: `/v5/market/instruments-info` (category=linear)
- 각 코인의 `maintMarginRate` 값 사용

**예시 코인별 MMR** (참고, 실제는 API 조회):
- BTC: 0.5% (0.005)
- ETH: 0.5% (0.005)
- SOL: 1.0% (0.01)
- XRP: 1.0% (0.01)
- DOGE: 2.0% (0.02)

**계산 예시** (SOL 100 USD 진입, 레버리지 1배, API에서 조회한 MMR 1%):
```
청산가 = 100 × (2 - 0.01) = 199 USD
```
→ 가격이 약 2배 상승해야 청산 (레버리지 1배로 매우 안전)

### 7.3 청산가 거리

```
청산가 거리 = 청산가 / 현재가
```

- **거리 ≥ 1.7**: 안전 (진입 가능)
- **거리 < 1.3**: 경고
- **거리 < 1.2**: 위험 (포지션 축소)

### 7.4 포지션 크기 계산

```python
# KRW로 살 수 있는 수량
amount_from_krw = MAX_POSITION_KRW / 업비트_가격

# USDT로 숏 잡을 수 있는 수량 (레버리지 1배 고정)
# 레버리지 1배이므로 포지션 크기 = 증거금 크기
amount_from_usdt = MAX_POSITION_USDT / 바이비트_가격

# 둘 중 작은 값 (완전 헷지)
position_size = min(amount_from_krw, amount_from_usdt)
```

### 7.5 수익 계산 (역전 전략)

#### 김프 차익 수익 (메인 수익원)
```
김프_차익_수익 = (청산_시_김프 - 진입_시_김프) × 포지션_가치
```

**계산 예시**:
```
- 진입 시: 김프 -0.5%, 포지션 200만원 (10 SOL @ 200,000원)
- 청산 시: 김프 +1.5%, 포지션 210만원 (10 SOL @ 210,000원)
- 김프 차익 = (1.5% - (-0.5%)) = 2.0%
- 김프_차익_수익 = 200만원 × 2.0% = 40,000원
```

#### 세부 손익 분해

**업비트 현물 손익**:
```
현물_손익_KRW = (청산_시_가격 - 진입_시_가격) × 수량
예: (210,000 - 200,000) × 10 = 100,000원
```

**바이비트 숏 손익**:
```
숏_손익_USD = (진입_시_가격 - 청산_시_가격) × 수량
숏_손익_KRW = 숏_손익_USD × USD/KRW_환율

예:
- 진입 시 바이비트: 150 USD, 청산 시: 157 USD
- 숏_손익_USD = (150 - 157) × 10 = -70 USD
- USD/KRW = 1,340원
- 숏_손익_KRW = -70 × 1,340 = -93,800원
```

**펀딩비 수익** (추가 수익):
```
펀딩비_수익_KRW = 포지션_보유_기간_동안_누적된_펀딩비
```

#### 총 수익
```
총_수익_KRW = 현물_손익_KRW + 숏_손익_KRW + 펀딩비_KRW - 수수료_KRW - 슬리피지_KRW

예:
= 100,000 + (-93,800) + 5,000 - 5,000 - 2,000
= 4,200원
```

**핵심 개념**:
- 완전 헷지이므로 가격 변동 위험 제거
- 김프 차이만큼 수익 발생 (진입 시 낮은 김프, 청산 시 높은 김프)
- 환율 변동이 있어도 양쪽 포지션이 상쇄하여 안전
- 펀딩비는 보너스 수익

### 7.6 펀딩비 계산

```
펀딩비_USDT = 포지션_가치_USDT × 펀딩비율

펀딩비_KRW = 펀딩비_USDT × USDT/KRW
```

**예시**:
```
- 포지션: 10 SOL @ 150 USD = 1,500 USDT
- 펀딩비율: 0.01% (롱 우세)
- USDT/KRW: 1,320원

펀딩비_USDT = 1,500 × 0.0001 = 0.15 USDT
펀딩비_KRW = 0.15 × 1,320 = 198원 (8시간당)
```

### 7.7 VWAP (Volume Weighted Average Price)

호가창에서 예상 체결가 계산:

```python
def calculate_vwap(orderbook_levels, target_amount):
    total_value = 0
    total_amount = 0

    for price, amount in orderbook_levels:
        take_amount = min(amount, target_amount - total_amount)
        total_value += price × take_amount
        total_amount += take_amount

        if total_amount >= target_amount:
            break

    return total_value / total_amount
```

### 7.8 슬리피지 계산

```
슬리피지(%) = |실제_체결가 - 예상_가격| / 예상_가격 × 100
```

---

## 8. 리스크 관리

### 8.1 리스크 파라미터

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| LIQUIDATION_BUFFER | 1.7 | 청산가 안전마진 (배수) |
| MIN_ORDERBOOK_DEPTH_MULTIPLIER | 10 | 최소 호가 깊이 (포지션 대비) |
| MAX_SLIPPAGE_PERCENT | 0.5 | 최대 허용 슬리피지 (%) |
| STOP_LOSS_PERCENT | -5.0 | 손절 기준 (%) |
| LIQUIDATION_WARNING_DISTANCE | 1.3 | 청산 경고 거리 |
| LIQUIDATION_REDUCE_DISTANCE | 1.2 | 포지션 축소 거리 |

### 8.2 진입 전 검증

#### 1. 청산가 안전성
```python
def is_liquidation_safe(entry_price, leverage, coin):
    liq_price = calculate_liquidation_price(entry_price, leverage, 'short', coin)
    required_liq = entry_price × LIQUIDATION_BUFFER
    return liq_price >= required_liq
```

#### 2. 호가창 깊이
```python
def validate_orderbook_depth(coin, position_size):
    min_depth = position_size × MIN_ORDERBOOK_DEPTH_MULTIPLIER

    upbit_depth = sum(asks[:10])  # 상위 10개 레벨
    bybit_depth = sum(asks[:10])

    return upbit_depth >= min_depth and bybit_depth >= min_depth
```

#### 3. 예상 슬리피지
```python
def validate_slippage(coin, position_size, market_price):
    expected_price = calculate_vwap(orderbook, position_size)
    slippage = abs(expected_price - market_price) / market_price × 100

    return slippage < MAX_SLIPPAGE_PERCENT
```

### 8.3 실시간 모니터링

#### 청산 위험 체크 (2초마다 - 빠른 감지)
```python
distance = liquidation_price / current_price

if distance <= 1.2:
    # CRITICAL: 즉시 포지션 50% 축소
    reduce_position(50%)
elif distance <= 1.3:
    # WARNING: 경고 로그
    log_warning("청산가 근접")
```

#### 손절 체크
```python
unrealized_pnl_percent = (current_value - entry_value) / entry_value × 100

if unrealized_pnl_percent <= -5.0:
    # 손절 실행
    exit_all_positions()
```

### 8.4 롤백 처리

진입 시 한쪽 실패 → 즉시 롤백:

```python
try:
    # 1. 바이비트 숏 진입
    bybit_order = bybit.open_short_position(coin, amount)

    # 2. 업비트 매수
    upbit_order = upbit.create_market_order(coin, 'buy', cost=krw_cost)

except Exception as e:
    # 롤백: 체결된 포지션 즉시 청산
    if bybit_order:
        bybit.close_short_position(coin, amount)
    raise
```

### 8.5 최대 동시 전략

```python
MAX_CONCURRENT_STRATEGIES = 3
```

- 리스크 분산
- 자본 효율
- 모니터링 가능 범위

---

## 9. 설정값 가이드

### 9.1 환경 변수 (.env)

#### API 키
```env
# 업비트
UPBIT_ACCESS_KEY=your_access_key
UPBIT_SECRET_KEY=your_secret_key

# 바이비트
BYBIT_API_KEY=your_api_key
BYBIT_API_SECRET=your_api_secret

# 환율 (yfinance 사용, API 키 불필요)
# USD/KRW 환율은 yfinance로 자동 조회
```

#### 봇 설정
```env
# 타겟 코인 (쉼표 구분)
TARGET_COINS=SOL,XRP,DOGE,ADA

# 목표 수익률 (%)
TARGET_PROFIT_PERCENT=1.0

# 최대 포지션 크기
MAX_POSITION_KRW=1000000      # 100만원
MAX_POSITION_USDT=1000         # 1000 USDT
```

#### USDT 차익 거래 임계값
```env
USDT_PREMIUM_BUY_THRESHOLD=0.5    # 0.5% 이하에서 매수
USDT_PREMIUM_SELL_THRESHOLD=0.5   # 0.5% 이상에서 매도
```

#### 리스크 관리
```env
LIQUIDATION_BUFFER=1.7                    # 청산가 1.7배 안전마진
MIN_ORDERBOOK_DEPTH_MULTIPLIER=10        # 호가 깊이 10배
```

#### 바이비트 설정
```env
BYBIT_LEVERAGE=1              # 레버리지 (1배 권장)
BYBIT_MARGIN_MODE=ISOLATED    # Isolated 모드
```

#### 페이퍼 트레이딩
```env
PAPER_TRADING=true            # true = 모의거래, false = 실거래
PAPER_INITIAL_KRW=10000000    # 초기 가상 자본 (원화)
PAPER_INITIAL_USDT=10000      # 초기 가상 자본 (USDT)
```

#### 로깅
```env
LOG_LEVEL=INFO                # DEBUG, INFO, WARNING, ERROR
LOG_TO_FILE=true              # 파일 로깅 활성화
```

#### 데이터베이스
```env
DB_PATH=kimp_bot.db           # SQLite 파일 경로
```

#### OHLC 데이터 수집
```env
# OHLC 데이터 수집 활성화 여부
OHLC_ENABLED=true             # true = 수집, false = 비활성화

# OHLC 수집 대상 코인 (선택 사항)
# 설정하지 않으면 TARGET_COINS 사용
# OHLC_TARGET_COINS=SOL,XRP,DOGE,ADA

# OHLC 타임프레임
OHLC_TIMEFRAMES=1d            # 기본값: 1d (일봉)

# OHLC 업데이트 주기 (초)
OHLC_UPDATE_INTERVAL=60       # 60초 권장 (1분마다 업데이트)

# OHLC 데이터 보관 기간 (일)
OHLC_RETENTION_DAYS=365       # 365일 (1년), 0 = 무제한
```

**OHLC 설정 가이드**:
- `OHLC_ENABLED=true`: 백그라운드에서 자동으로 OHLC 데이터 수집
- `OHLC_ENABLED=false`: OHLC 수집 완전 비활성화 (메인 프로세스 영향 없음)
- `OHLC_UPDATE_INTERVAL`: 60초 권장 (너무 짧으면 DB 부하 증가)
- `OHLC_RETENTION_DAYS`: 365일 권장 (백테스팅용 1년 데이터)

### 9.2 설정값 조정 가이드

#### 목표 수익률 조정
```
TARGET_PROFIT_PERCENT = 1.0   # 기본값
```

- **높게 (1.5% 이상)**: 진입 기회 감소, 안정성 증가
- **낮게 (0.5%)**: 진입 기회 증가, 리스크 증가

**권장**: 1.0% (수수료 0.25% 제외 시 순수익 0.75%)

#### 최대 포지션 크기
```
MAX_POSITION_KRW = 1000000    # 100만원
MAX_POSITION_USDT = 1000      # 1000 USDT
```

- **초보**: 10만원 / 100 USDT
- **중급**: 100만원 / 1000 USDT
- **고급**: 1000만원 / 10000 USDT

#### 청산가 안전마진
```
LIQUIDATION_BUFFER = 1.7
```

- **보수적**: 2.0 (진입 기회 감소)
- **공격적**: 1.5 (리스크 증가)

#### 모니터링 인터벌 (신규)
```python
# config/settings.py - StrategyConfig 클래스
MONITORING_INTERVAL = 2  # 초 단위
```

- **스캐너 (Layer 1)**: 10초마다 전체 코인 스캔 (ScannerConfig.SCAN_INTERVAL)
- **전략 모니터링 (Layer 2)**: **2초마다** 포지션 모니터링 (권장)
  - 목표 수익 도달 빠른 감지
  - 손절 타이밍 최적화
  - 청산 위험 즉시 대응

**API 레이트 리미트 영향 분석**:
- 전략 1개: 2회/2초 = 1회/초 (upbit + bybit)
- 전략 3개: 3회/초
- 업비트 한도: 10회/초 → ✅ 안전
- 바이비트 한도: 50회/초 → ✅ 안전

**참고**: 1초 미만으로 설정 시 레이트 리미트 위험

**권장**: 1.7

#### 타겟 코인 선정
```
TARGET_COINS = SOL,XRP,DOGE,ADA
```

**추천 코인**:
- **SOL**: 김프 변동 크고 거래량 많음
- **XRP**: 안정적, 스프레드 작음
- **DOGE**: 변동성 큼, 김프 기회 많음
- **ADA**: 중간 정도

**비추천**:
- **BTC**: 김프 반영 느림
- **ETH**: 김프 작음

### 9.3 데이터베이스 기반 설정 관리

봇 설정은 두 가지 방식으로 관리할 수 있습니다: **.env 파일** (기본값) 또는 **데이터베이스** (실시간 반영).

#### CONFIG_SOURCE 환경 변수

```env
# .env 파일
CONFIG_SOURCE=env  # 기본값: .env 파일에서 설정 로드
# CONFIG_SOURCE=db   # 데이터베이스에서 설정 로드 (실시간 반영)
```

#### 방식 1: .env 파일 (기본값)

**특징**:
- 전통적인 방식
- 설정 변경 시 봇 재시작 필요
- 간단하고 직관적

**사용법**:
```bash
# .env 파일 수정
TARGET_PROFIT_PERCENT=1.5
MAX_POSITION_KRW=2000000

# 봇 재시작 필요
python main.py
```

#### 방식 2: 데이터베이스 (실시간 반영)

**특징**:
- 설정 변경 시 봇 재시작 **불필요**
- 5초 이내 자동 반영
- SQL로 직접 설정 변경 가능
- 버전 관리 (version 컬럼)

**활성화 방법**:

```bash
# 1. .env 파일에서 CONFIG_SOURCE 변경
CONFIG_SOURCE=db

# 2. 봇 재시작 (최초 1회만)
python main.py
# → "11개 설정을 DB로 마이그레이션 완료" 메시지 확인

# 3. 이후 SQL로 설정 변경
sqlite3 kimp_bot.db "UPDATE config_settings SET value='1.5' WHERE key='TARGET_PROFIT_PERCENT';"
# → 5초 이내 자동 반영 (재시작 불필요)
```

**DB 설정 조회**:

```bash
# 모든 설정 보기
sqlite3 kimp_bot.db "SELECT config_class, key, value, description FROM config_settings;"

# BotConfig 설정만 보기
sqlite3 kimp_bot.db "SELECT key, value, description FROM config_settings WHERE config_class='BotConfig';"

# 최근 수정된 설정 보기
sqlite3 kimp_bot.db "SELECT config_class, key, value, version, updated_at FROM config_settings WHERE version > 1 ORDER BY updated_at DESC;"
```

**주요 설정 변경 예시**:

```bash
# 목표 수익률 변경
sqlite3 kimp_bot.db "UPDATE config_settings SET value='2.0', version=version+1 WHERE key='TARGET_PROFIT_PERCENT';"

# 타겟 코인 변경 (JSON 형식)
sqlite3 kimp_bot.db "UPDATE config_settings SET value='[\"BTC\",\"ETH\",\"SOL\"]', version=version+1 WHERE key='TARGET_COINS';"

# 최대 포지션 변경
sqlite3 kimp_bot.db "UPDATE config_settings SET value='5000000', version=version+1 WHERE key='MAX_POSITION_KRW';"

# 스캔 주기 변경
sqlite3 kimp_bot.db "UPDATE config_settings SET value='5', version=version+1 WHERE key='SCAN_INTERVAL';"
```

**지원하는 설정 (11개)**:

| 설정 클래스 | 설정 키 | 타입 | 설명 |
|------------|--------|------|------|
| BotConfig | TARGET_COINS | list | 타겟 코인 목록 |
| BotConfig | TARGET_PROFIT_PERCENT | float | 목표 수익률 (%) |
| BotConfig | MIN_ENTRY_PREMIUM | float | 최대 진입 김프 (%) |
| BotConfig | MAX_POSITION_KRW | float | 최대 포지션 (KRW) |
| BotConfig | MAX_POSITION_USDT | float | 최대 포지션 (USDT) |
| RiskParams | LIQUIDATION_BUFFER | float | 청산가 안전마진 |
| RiskParams | MIN_ORDERBOOK_DEPTH_MULTIPLIER | float | 최소 호가창 깊이 배수 |
| RiskParams | STOP_LOSS_PERCENT | float | 손절 기준 (%) |
| ScannerConfig | SCAN_INTERVAL | int | 스캔 주기 (초) |
| ScannerConfig | MAX_CONCURRENT_STRATEGIES | int | 최대 동시 전략 수 |
| ScannerConfig | MONITORING_INTERVAL | int | 모니터링 주기 (초) |

**주의사항**:

1. **API 키는 DB에 저장되지 않음**: 보안상 API 키는 항상 .env 파일에서만 로드됩니다
2. **실행 중인 전략은 진입 시 설정값 유지**: 새로운 전략부터 변경된 설정이 적용됩니다
3. **DB 모드 → ENV 모드 되돌리기**: `CONFIG_SOURCE=env`로 변경 후 재시작 (DB 설정은 유지됨)

**동작 원리**:

- ConfigManager 싱글톤이 5초마다 DB를 자동으로 체크
- 변경사항 발견 시 메모리 캐시 업데이트
- 기존 코드 (`BotConfig.TARGET_COINS` 등)는 수정 없이 투명하게 동작 (Proxy 패턴)

---

## 10. API 연동

### 10.1 업비트 API

#### 인증
```python
import ccxt

upbit = ccxt.upbit({
    'apiKey': UPBIT_ACCESS_KEY,
    'secret': UPBIT_SECRET_KEY,
    'enableRateLimit': True,
})
```

#### 주요 메서드
```python
# 시세 조회
ticker = upbit.fetch_ticker('SOL/KRW')

# 호가창 조회
orderbook = upbit.fetch_order_book('SOL/KRW', limit=10)

# 잔고 조회
balance = upbit.fetch_balance()

# 시장가 매수 (금액 지정)
order = upbit.create_order(
    symbol='SOL/KRW',
    type='market',
    side='buy',
    amount=None,
    params={'cost': 100000}  # 10만원 어치
)

# 시장가 매도 (수량 지정)
order = upbit.create_order(
    symbol='SOL/KRW',
    type='market',
    side='sell',
    amount=10.5
)
```

#### Rate Limit
- **공개 API**: 600회/분
- **인증 API**: 300회/분
- ccxt의 `enableRateLimit=True` 사용 권장

### 10.2 바이비트 API

#### 인증
```python
bybit = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'linear',  # USDT 무기한 선물
    }
})
```

#### 주요 메서드
```python
# 시세 조회
ticker = bybit.fetch_ticker('SOL/USDT:USDT')

# 호가창 조회
orderbook = bybit.fetch_order_book('SOL/USDT:USDT', limit=10)

# 잔고 조회
balance = bybit.fetch_balance()

# 레버리지 설정
bybit.set_leverage(1, 'SOL/USDT:USDT')

# 마진 모드 설정 (Isolated)
bybit.set_margin_mode('isolated', 'SOL/USDT:USDT')

# 숏 포지션 진입 (Isolated, Hedge Mode)
order = bybit.create_order(
    symbol='SOL/USDT:USDT',
    type='market',
    side='sell',
    amount=10.5,
    params={
        'positionIdx': 2,  # Hedge Mode Short
    }
)

# 숏 포지션 청산
order = bybit.create_order(
    symbol='SOL/USDT:USDT',
    type='market',
    side='buy',
    amount=10.5,
    params={
        'positionIdx': 2,
        'reduceOnly': True
    }
)

# 포지션 조회
positions = bybit.fetch_positions(['SOL/USDT:USDT'])

# 펀딩비율 조회
funding = bybit.fetch_funding_rate('SOL/USDT:USDT')
```

#### Rate Limit
- **공개 API**: 120회/분
- **인증 API**: 100회/분

### 10.3 환율 API (yfinance)

#### 라이브러리
```python
import yfinance as yf
```

#### USD/KRW 환율 조회
```python
# KRW=X 티커를 통해 USD/KRW 환율 조회
ticker = yf.Ticker("KRW=X")
data = ticker.history(period="1d")
rate = data['Close'].iloc[-1]  # 최신 종가
```

#### 응답 예시
```python
# ticker.history() 반환값
            Open       High       Low        Close      Volume
Date
2025-01-23  1350.25   1352.10   1348.50    1351.75    0
```

#### 장점
- **API 키 불필요**: 무료로 사용 가능
- **24/7 운영**: 주말/공휴일에도 최신 환율 제공
- **신뢰성**: Yahoo Finance의 안정적인 데이터
- **Rate Limit 없음**: 한국수출입은행 API의 rate limit 문제 해결

#### 주의사항
- **캐싱 필수**: 매 요청마다 조회하지 않고 1시간 단위로 캐싱
- **폴백 로직**: 네트워크 오류 시 캐시된 환율 사용
- **티커 심볼**: `"KRW=X"` 또는 `"USDKRW=X"` 사용 가능

---

## 11. 운영 가이드

### 11.1 초기 설정

#### 1. 환경 준비
```bash
# Python 설치 (3.10 이상)
python --version

# 프로젝트 클론
git clone <repository>
cd kimp

# 가상환경 생성
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate

# 패키지 설치
pip install -r requirements.txt
```

#### 2. 환경 변수 설정
```bash
# .env.example을 .env로 복사
copy .env.example .env

# .env 파일 수정 (API 키 입력)
notepad .env
```

#### 3. DB 초기화
```bash
# 자동으로 생성됨 (첫 실행 시)
python main.py
```

### 11.2 페이퍼 트레이딩 테스트

#### 설정
```env
PAPER_TRADING=true
PAPER_INITIAL_KRW=10000000
PAPER_INITIAL_USDT=10000
```

#### 실행
```bash
python main.py
```

#### 확인 사항
1. 시장 스캔이 정상 작동하는가?
2. 김프 계산이 올바른가?
3. 진입 검증 로직이 작동하는가?
4. 가상 거래가 정상적으로 실행되는가?
5. DB에 기록이 저장되는가?

### 11.3 실거래 시작

#### 준비 사항
1. API 키 발급 및 권한 확인
2. 충분한 자본 준비
3. 소액 테스트 완료

#### 설정 변경
```env
PAPER_TRADING=false
MAX_POSITION_KRW=100000    # 소액으로 시작
MAX_POSITION_USDT=100
```

#### 실행
```bash
python main.py
```

#### 초기 모니터링 (필수)
```bash
# 로그 실시간 확인
tail -f logs/bot.log

# 다른 터미널에서 DB 확인
sqlite3 kimp_bot.db
SELECT * FROM strategies WHERE state != 'EXITED';
```

### 11.4 로그 확인

#### 로그 파일
```
logs/bot.log
```

#### 주요 로그
```
[INFO] 시장 스캐너 초기화 완료. 타겟 코인: SOL, XRP, DOGE
[INFO] [김프] SOL: -0.50% (KRW: 200000, USD: 150.00)
[INFO] 진입 기회 발견: SOL, 김프 -0.50%
[INFO] [SOL] 전략 인스턴스 생성 완료 (ID: 1)
[INFO] [SOL] 전략 1 자본 확인 중...
[INFO] 전략 1 자본 확인 완료 (KRW: 5000000, USDT: 5000.00)
[INFO] 전략 1 진입 검증 중...
[INFO] 전략 1 진입 검증 통과
[INFO] [SOL] 전략 1 포지션 진입 중...
[INFO] [SOL] 전략 1 포지션 진입 완료 (목표 이율 : 1.0, 목표 김프 : 0.85%, 진입 김프 : -0.50%)
[INFO] [거래 성공] upbit BUY 10.5 SOL @ 200000
[INFO] [거래 성공] bybit OPEN_SHORT 10.5 SOL @ 150.00
[INFO] 전략 1 헷지 완료, 모니터링 시작
[INFO] [SOL] 전략 1 모니터링 - 현재 김프: 0.50% | 진입 대비: +1.00% | 목표까지: 0.35%
[INFO] [SOL] 전략 1 목표 도달 (현재 김프: 0.90%, 목표: 0.85%)
[INFO] 전략 1 목표 수익 도달, 청산 시작...
[INFO] 전략 1 포지션 청산 중...
[INFO] 전략 1 포지션 청산 완료
[INFO] 전략 1 정산 중...
[INFO] [손익] 전략 1 (SOL): +15,000 KRW (+0.75%)
```

### 11.5 DB 조회

#### SQLite 접속
```bash
sqlite3 kimp_bot.db
```

#### 유용한 쿼리

**활성 전략 조회**
```sql
SELECT id, coin, state, entry_premium, current_premium,
       created_at, updated_at
FROM strategies
WHERE state != 'EXITED'
ORDER BY created_at DESC;
```

**수익 통계**
```sql
SELECT
    COUNT(*) as 총_전략_수,
    SUM(CASE WHEN realized_pnl_krw > 0 THEN 1 ELSE 0 END) as 수익_전략_수,
    SUM(CASE WHEN realized_pnl_krw < 0 THEN 1 ELSE 0 END) as 손실_전략_수,
    SUM(realized_pnl_krw) as 총_손익,
    AVG(realized_pnl_percent) as 평균_수익률,
    MAX(realized_pnl_krw) as 최대_수익,
    MIN(realized_pnl_krw) as 최대_손실
FROM strategies
WHERE state = 'EXITED';
```

**최근 거래 내역**
```sql
SELECT
    t.executed_at,
    t.exchange,
    t.side,
    t.coin,
    t.amount,
    t.price,
    t.value,
    t.fee
FROM trades t
ORDER BY t.executed_at DESC
LIMIT 20;
```

**코인별 성과**
```sql
SELECT
    coin,
    COUNT(*) as 전략_수,
    AVG(realized_pnl_percent) as 평균_수익률,
    SUM(realized_pnl_krw) as 총_손익
FROM strategies
WHERE state = 'EXITED'
GROUP BY coin
ORDER BY 총_손익 DESC;
```

**최근 시장 스캔 (김프 순)**
```sql
SELECT
    coin,
    premium,
    krw_price,
    usd_price,
    is_opportunity,
    scanned_at
FROM market_scans
WHERE scanned_at > datetime('now', '-1 hour')
ORDER BY premium DESC
LIMIT 10;
```

**OHLC 데이터 조회**
```sql
-- 오늘 OHLC 캔들 확인
SELECT
    coin,
    premium_open,
    premium_high,
    premium_low,
    premium_close,
    scan_count,
    updated_at
FROM premium_ohlc
WHERE date_kst = date('now')
ORDER BY coin;

-- 최근 7일 데이터 완성도 확인
SELECT
    coin,
    COUNT(*) as days,
    AVG(scan_count) as avg_scans_per_day,
    MIN(scan_count) as min_scans,
    MAX(scan_count) as max_scans
FROM premium_ohlc
WHERE date_kst >= date('now', '-7 days')
GROUP BY coin
ORDER BY coin;

-- 특정 코인의 최근 30일 프리미엄 추이
SELECT
    date_kst,
    premium_open,
    premium_high,
    premium_low,
    premium_close,
    scan_count
FROM premium_ohlc
WHERE coin = 'SOL'
  AND date_kst >= date('now', '-30 days')
ORDER BY date_kst DESC;

-- 미확정 캔들 (오늘 진행 중인 캔들)
SELECT
    coin,
    date_kst,
    premium_close,
    scan_count,
    updated_at
FROM premium_ohlc
WHERE is_finalized = 0
ORDER BY updated_at DESC;
```

**설정 관리 (config_settings)**
```sql
-- 모든 설정 조회
SELECT
    config_class,
    key,
    value,
    value_type,
    description,
    version,
    updated_at
FROM config_settings
ORDER BY config_class, key;

-- BotConfig 설정만 조회
SELECT
    key,
    value,
    description,
    updated_at
FROM config_settings
WHERE config_class = 'BotConfig'
ORDER BY key;

-- 최근 수정된 설정 조회 (버전 > 1)
SELECT
    config_class,
    key,
    value,
    version,
    updated_by,
    updated_at
FROM config_settings
WHERE version > 1
ORDER BY updated_at DESC;

-- 특정 설정값 조회
SELECT value
FROM config_settings
WHERE config_class = 'BotConfig'
  AND key = 'TARGET_PROFIT_PERCENT';
```

**설정 변경 (UPDATE)**
```sql
-- 목표 수익률 변경
UPDATE config_settings
SET value = '1.5',
    version = version + 1,
    updated_by = 'admin'
WHERE config_class = 'BotConfig'
  AND key = 'TARGET_PROFIT_PERCENT';

-- 타겟 코인 변경 (JSON 형식)
UPDATE config_settings
SET value = '["BTC","ETH","SOL"]',
    version = version + 1,
    updated_by = 'admin'
WHERE config_class = 'BotConfig'
  AND key = 'TARGET_COINS';

-- 최대 포지션 변경
UPDATE config_settings
SET value = '5000000',
    version = version + 1
WHERE config_class = 'BotConfig'
  AND key = 'MAX_POSITION_KRW';
```

**참고**: CONFIG_SOURCE=db 모드에서만 유효하며, 변경 후 5초 이내 자동 반영됨

### 11.6 재시작 및 복구

#### 봇 중단
```bash
# Ctrl + C
# 또는
kill <PID>
```

#### 재시작
```bash
python main.py
```

- 자동으로 DB에서 활성 전략 복구
- 모니터링 계속 진행

### 11.7 OHLC 데이터 수집 사용 가이드

#### 개요

OHLC 데이터 수집 기능은 김치 프리미엄의 시계열 데이터를 자동으로 수집하여 향후 분석 및 백테스팅에 활용할 수 있도록 합니다.

**핵심 특징**:
- 메인 트레이딩 프로세스와 완전 독립 실행
- 백그라운드 데몬 스레드로 자동 수집
- 봇 재시작 시 자동 복구 및 이어서 업데이트
- 언제든 비활성화 가능 (OHLC_ENABLED=false)

#### 활성화 및 설정

**.env 파일 설정**:
```env
# OHLC 수집 활성화
OHLC_ENABLED=true

# 수집 대상 코인 (생략 시 TARGET_COINS 사용)
# OHLC_TARGET_COINS=SOL,XRP,DOGE,ADA

# 타임프레임 (현재 1d만 지원)
OHLC_TIMEFRAMES=1d

# 업데이트 주기 (60초 권장)
OHLC_UPDATE_INTERVAL=60

# 보관 기간 (365일 = 1년)
OHLC_RETENTION_DAYS=365
```

#### 테스트 방법

**1. 봇 시작 및 OHLC 수집 확인**

```bash
# 봇 실행
python main.py

# 로그 확인 (별도 터미널)
tail -f logs/bot.log | grep ohlc_collector

# 예상 로그:
# [INFO] OHLC 수집기 활성화됨
# [INFO] OHLC Collector initialized: coins=['SOL', 'XRP', 'DOGE'], timeframes=['1d']
# [INFO] OHLC 수집기 스레드 시작
# [INFO] [SOL] Updated 2025-01-25 candle: premium=-0.50%
```

**2. 데이터 수집 확인**

2-3분 후 DB에서 확인:

```bash
sqlite3 kimp_bot.db
```

```sql
-- 오늘 수집된 캔들 확인
SELECT coin, premium_close, scan_count, updated_at
FROM premium_ohlc
WHERE date_kst = date('now')
ORDER BY coin;
```

예상 결과:
```
coin  premium_close  scan_count  updated_at
----  -------------  ----------  --------------------
DOGE  -0.23          15          2025-01-25 12:35:42
SOL   -0.50          15          2025-01-25 12:35:42
XRP   0.12           15          2025-01-25 12:35:42
```

scan_count가 시간이 지날수록 증가하면 정상 작동 중입니다.

**3. 데이터 갭 확인 (진단 스크립트)**

```bash
# 누락된 날짜 및 데이터 품질 확인
python scripts/check_ohlc_gaps.py
```

예상 출력:
```
============================================================
OHLC Data Gap Checker
============================================================

Checking period: 2025-01-01 to 2025-01-25
Target coins: SOL, XRP, DOGE

[SOL]
----------------------------------------
No missing dates
Data quality: 1420.5 scans/candle
Avg premium: -0.35%

[XRP]
----------------------------------------
Missing dates: 2
  - 2025-01-10
  - 2025-01-11
Data quality: 1398.2 scans/candle
Avg premium: 0.12%

============================================================
Total missing dates across all coins: 2
============================================================
```

#### 재시작 및 복구 테스트

**봇 재시작 시 OHLC 자동 복구 확인**:

1. 봇 실행 중 Ctrl+C로 중단
2. 다시 `python main.py` 실행
3. 로그 확인:
   ```
   [INFO] OHLC 수집기 스레드 시작
   [INFO] [SOL] Updated 2025-01-25 candle: premium=-0.48%
   ```
4. DB 확인:
   ```sql
   SELECT coin, scan_count, updated_at
   FROM premium_ohlc
   WHERE date_kst = date('now');
   ```
   scan_count가 이어서 증가하면 정상입니다.

#### 데이터 활용 예시

**1. 최근 30일 프리미엄 추이 분석**

```sql
SELECT
    date_kst,
    premium_high,
    premium_low,
    premium_close,
    scan_count
FROM premium_ohlc
WHERE coin = 'SOL'
  AND date_kst >= date('now', '-30 days')
ORDER BY date_kst DESC;
```

**2. 코인별 평균 프리미엄 비교**

```sql
SELECT
    coin,
    COUNT(*) as days,
    AVG(premium_close) as avg_premium,
    MAX(premium_high) as max_premium,
    MIN(premium_low) as min_premium
FROM premium_ohlc
WHERE date_kst >= date('now', '-30 days')
GROUP BY coin
ORDER BY avg_premium DESC;
```

**3. 데이터 품질 확인 (스캔 수 기준)**

```sql
SELECT
    coin,
    date_kst,
    scan_count,
    CASE
        WHEN scan_count >= 1400 THEN 'GOOD'
        WHEN scan_count >= 1000 THEN 'FAIR'
        ELSE 'POOR'
    END as quality
FROM premium_ohlc
WHERE date_kst >= date('now', '-7 days')
ORDER BY date_kst DESC, coin;
```

**참고**: 하루 = 1440분이므로 1분마다 1회 스캔 시 scan_count는 약 1440이 정상입니다.

#### 데이터 내보내기 (향후 백테스팅용)

```bash
# CSV로 내보내기
sqlite3 kimp_bot.db -header -csv \
  "SELECT * FROM premium_ohlc WHERE coin='SOL' ORDER BY date_kst" \
  > sol_ohlc_data.csv
```

#### 비활성화

OHLC 수집이 필요 없다면:

```env
# .env 파일에서
OHLC_ENABLED=false
```

봇 재시작 시 OHLC 스레드가 시작되지 않습니다.

#### 주의사항

1. **메인 프로세스 영향 없음**: OHLC 수집기는 완전 독립 스레드로 실행되며, 에러가 발생해도 메인 트레이딩에 영향 없음
2. **DB 용량**: 코인 3개 기준, 1년 데이터 = 약 3,000 레코드 (수 MB 수준)
3. **scan_count 감소**: 봇이 중단된 기간의 데이터는 수집되지 않으므로 해당 날짜의 scan_count가 낮을 수 있음
4. **타임존**: 모든 날짜는 KST 기준이며, UTC로 저장됨

### 11.8 트러블슈팅

#### 환율 API 에러
```
ERROR: Failed to fetch exchange rate
```

**해결**:
- 인터넷 연결 확인
- yfinance 패키지 설치 확인: `pip install yfinance`
- 캐시된 환율이 있으면 자동으로 사용됨
- 티커 심볼 확인 ("KRW=X" 또는 "USDKRW=X")

#### 거래소 API 에러
```
ERROR: Failed to open Bybit short position
```

**해결**:
- API 키 권한 확인 (거래, 조회)
- Rate limit 확인
- 잔고 확인

#### 청산 위험
```
WARNING: Liquidation price too close
```

**해결**:
- 자동으로 포지션 축소
- 레버리지 낮추기 (1배 권장)
- 포지션 크기 줄이기

---

## 12. 개발 가이드

### 12.1 코드 구조

#### Layer 1: Scanner
```python
# core/scanner.py

class MarketScanner:
    """시장 스캐너"""

    def scan_market(self) -> Dict[str, MarketPrice]:
        """모든 코인 스캔"""
        pass

    def find_opportunities(self, market_prices, active_strategies) -> List[str]:
        """진입 기회 탐색"""
        pass
```

#### Layer 2: Strategy
```python
# core/strategy.py

class StrategyInstance:
    """전략 인스턴스"""

    def __init__(self, coin, market_price, strategy_id=None):
        """초기화 또는 복구"""
        pass

    def run(self) -> bool:
        """메인 루프 (상태별 핸들러 실행)"""
        pass

    def _handle_monitoring(self) -> bool:
        """모니터링 상태 처리"""
        pass
```

#### Layer 3: OHLC Collector
```python
# core/ohlc_collector.py

class OHLCCollector:
    """OHLC 데이터 수집기"""

    def __init__(self):
        """초기화"""
        self.target_coins = OHLCConfig.get_target_coins()
        self.timeframes = OHLCConfig.TIMEFRAMES
        self.update_interval = OHLCConfig.UPDATE_INTERVAL

    def aggregate_candle(self, db: Session, coin: str, date_kst: str) -> Optional[Dict]:
        """
        market_scans 테이블에서 OHLC 데이터 집계

        Args:
            db: DB 세션
            coin: 코인 심볼
            date_kst: KST 날짜 (YYYY-MM-DD)

        Returns:
            OHLC 데이터 딕셔너리 또는 None
        """
        pass

    def update_candle(self, db: Session, coin: str, timeframe: str, date_kst: str):
        """캔들 생성 또는 업데이트"""
        pass

    def finalize_candle(self, db: Session, coin: str, timeframe: str, date_kst: str):
        """캔들 확정 (날짜 완료 시)"""
        pass

    def collect_cycle(self):
        """단일 수집 사이클 (오늘 업데이트, 어제 확정)"""
        pass

    def run_continuous(self):
        """연속 수집 루프 (60초마다)"""
        pass

# utils/ohlc_utils.py

def get_ohlc_data(coin: str, timeframe: str = '1d',
                  start_date: str = None, end_date: str = None,
                  limit: int = None) -> List[PremiumOHLC]:
    """OHLC 데이터 조회"""
    pass

def find_missing_dates(coin: str, timeframe: str = '1d',
                       start_date: str = None, end_date: str = None) -> List[str]:
    """누락된 날짜 찾기"""
    pass

def get_ohlc_summary(coin: str, timeframe: str = '1d', days: int = 30) -> Dict:
    """OHLC 통계 요약"""
    pass
```

### 12.2 새로운 상태 추가

#### 1. States 정의
```python
# config/settings.py
class States:
    NEW_STATE = "NEW_STATE"
```

#### 2. 전이 규칙 추가
```python
# core/state_machine.py
ALLOWED_TRANSITIONS = {
    'MONITORING': ['PROFITABLE', 'NEW_STATE', ...],
    'NEW_STATE': ['EXITING', 'ERROR']
}
```

#### 3. 핸들러 구현
```python
# core/strategy.py
def _handle_new_state(self) -> bool:
    """NEW_STATE 처리"""
    # 로직 구현
    self._update_state(States.NEXT_STATE)
    return True
```

#### 4. run() 메서드에 추가
```python
def run(self) -> bool:
    if self.state_machine.current_state == States.NEW_STATE:
        return self._handle_new_state()
```

### 12.3 새로운 거래소 추가

#### 1. 서비스 클래스 생성
```python
# services/new_exchange.py

class NewExchangeService:
    def __init__(self, paper_trading=None):
        pass

    def fetch_ticker(self, coin):
        pass

    def create_market_order(self, coin, side, amount):
        pass
```

#### 2. 설정 추가
```python
# config/settings.py
class APIKeys:
    NEW_EXCHANGE_API_KEY = os.getenv('NEW_EXCHANGE_API_KEY', '')
```

#### 3. 전략에서 사용
```python
# core/strategy.py
from services.new_exchange import get_new_exchange_service

self.new_exchange = get_new_exchange_service()
```

### 12.4 페이퍼 트레이딩 구현 패턴

```python
def create_market_order(self, coin, side, amount):
    if self.paper_trading:
        return self._paper_create_order(coin, side, amount)

    # 실거래 로직
    return self.exchange.create_order(...)

def _paper_create_order(self, coin, side, amount):
    # 실제 시세 조회
    ticker = self.fetch_ticker(coin)
    price = ticker['last']

    # 가상 잔고 업데이트
    if side == 'buy':
        self.paper_balances['KRW'] -= amount * price
        self.paper_balances[coin] += amount

    # 가상 주문 결과 반환
    return {
        'id': f'paper_{coin}_{side}_{hash(price)}',
        'price': price,
        'amount': amount,
        ...
    }
```

### 12.5 테스트 작성 (향후)

```python
# tests/test_calculator.py

import pytest
from utils.calculator import calculate_premium

def test_premium_calculation():
    krw_price = 200000
    usd_price = 150
    usdt_krw = 1320

    premium = calculate_premium(krw_price, usd_price, usdt_krw)

    assert abs(premium - 1.01) < 0.01
```

### 12.6 코드 스타일 가이드

#### 12.6.1 로그 메시지 및 주석 언어
- **모든 로그 메시지**: 한국어 사용
- **모든 주석 및 Docstring**: 한국어 사용
- **변수명/함수명**: 영어 사용 (PEP 8 준수)

**예시**:
```python
# 좋은 예
def calculate_premium(krw_price: float, usd_price: float) -> float:
    """
    김치 프리미엄 계산

    Args:
        krw_price: 업비트 원화 가격
        usd_price: 바이비트 달러 가격

    Returns:
        김프 (%)
    """
    # 이론적 원화 가격 계산
    theoretical_krw = usd_price * exchange_rate

    logging.info(f"김프 계산 완료: {premium:.2f}%")
    return premium

# 나쁜 예 (영어 로그 및 주석)
def calculate_premium(krw_price: float, usd_price: float) -> float:
    """Calculate kimchi premium"""  # 영어 docstring (X)

    # Calculate theoretical KRW price  # 영어 주석 (X)
    theoretical_krw = usd_price * exchange_rate

    logging.info(f"Premium calculated: {premium:.2f}%")  # 영어 로그 (X)
    return premium
```

#### 12.6.2 주석 작성 원칙

1. **복잡한 계산식**: 인라인 주석으로 단계별 설명
   ```python
   # 1. 이론적 원화 가격 계산
   # = 해외 가격을 환율로 환산한 가격
   # 예: 바이비트 100 USD × 환율 1,300 = 130,000 KRW
   theoretical_krw = usd_price * exchange_rate

   # 2. 김프 계산
   # = (실제 국내 가격 / 이론적 가격) - 1
   # 예: (135,000 / 130,000) - 1 = 0.0385 = 3.85% 김프
   premium = ((krw_price / theoretical_krw) - 1) * 100
   ```

2. **상태 전환**: 전환 이유와 조건 명시
   ```python
   # 목표 수익 도달 시 PROFITABLE 상태로 전환
   # 조건: 현재 김프 >= 진입 김프 + 목표 수익 + 수수료
   if current_premium >= self.target_exit_premium:
       logging.info(f"[{self.coin}] 목표 수익 도달!")
       self._update_state(States.PROFITABLE)
   ```

3. **리스크 체크**: 임계값과 대응 방안 설명
   ```python
   # 청산가 거리 체크
   # 청산가까지 20% 미만 남았다면 위험!
   # → LIQUIDATION_RISK 상태로 전환하여 포지션 50% 축소
   if liquidation_distance < 0.20:
       logging.warning(f"[{self.coin}] 청산가 위험! 거리: {liquidation_distance*100:.1f}%")
       self._update_state(States.LIQUIDATION_RISK)
   ```

4. **Docstring**: Args, Returns, Raises 포함
   ```python
   def open_short_position(self, coin: str, amount: float) -> dict:
       """
       바이비트에서 숏 포지션 오픈

       positionIdx=2 (Hedge Mode)를 사용하여 숏 포지션을 진입합니다.
       ISOLATED 마진 모드, 1배 레버리지 고정입니다.

       Args:
           coin: 코인 심볼 (예: 'SOL', 'BTC')
           amount: 진입 수량 (코인 단위)

       Returns:
           주문 결과 딕셔너리 (price, amount, orderId 포함)

       Raises:
           Exception: API 호출 실패 또는 잔고 부족 시

       Note:
           페이퍼 트레이딩 모드에서는 가상 포지션을 생성합니다.
       """
       pass
   ```

#### 12.6.3 로그 레벨 가이드

**INFO**: 정상 동작 흐름
- 초기화 완료
- 전략 시작/종료
- 상태 전환
- 주문 생성 성공
- 데이터 조회 성공

```python
logging.info("봇 매니저 초기화 완료")
logging.info(f"[{coin}] 전략 시작")
logging.info(f"[{coin}] 상태 전환: MONITORING → PROFITABLE")
logging.info(f"업비트 BUY 주문 생성: {order_id}")
```

**WARNING**: 주의 필요 (정상 동작 가능하지만 모니터링 필요)
- 폴백 값 사용
- 청산가 접근
- 재시도 중
- 캐시 사용

```python
logging.warning(f"{coin} 폴백 MMR 사용: {mmr}")
logging.warning(f"[{coin}] 청산가 위험! 거리: {distance:.1f}%")
logging.warning("캐시된 환율 사용 (API 이용 불가 시간대)")
logging.warning(f"API 재시도 중 ({retry}/3)")
```

**ERROR**: 오류 발생 (정상 동작 불가, 개입 필요)
- API 호출 실패
- 주문 생성 실패
- 데이터 조회 실패
- 복구 불가능한 상태

```python
logging.error(f"업비트 {coin} 시세 조회 실패: {e}")
logging.error(f"바이비트 숏 포지션 오픈 실패: {e}")
logging.error(f"전략 {id} 복구 실패: {e}")
logging.error(f"치명적 오류: {e}")
```

#### 12.6.4 금융 용어 한글 표기 가이드

코드 내에서 일관된 한글 표기를 사용합니다:

| 영어 | 한글 |
|------|------|
| Kimchi Premium | 김치 프리미엄 / 김프 |
| Liquidation Price | 청산가 |
| MMR (Maintenance Margin Rate) | 유지증거금률 / MMR |
| Leverage | 레버리지 |
| Funding Rate/Fee | 펀딩비율 / 펀딩비 |
| Slippage | 슬리피지 |
| Orderbook | 호가창 |
| Position | 포지션 |
| Short Position | 숏 포지션 |
| Long Position | 롱 포지션 |
| Hedge | 헷지 |
| Arbitrage | 차익거래 |
| Entry | 진입 |
| Exit | 청산 / 청산 |
| Paper Trading | 페이퍼 트레이딩 |
| Live Trading | 실거래 |

**예시**:
```python
logging.info(f"김프 계산: {premium:.2f}%")  # ✓
logging.info(f"Kimchi premium: {premium:.2f}%")  # ✗

logging.warning(f"청산가 위험!")  # ✓
logging.warning(f"Liquidation price risk!")  # ✗
```

---

## 13. 향후 개선 사항

### 13.1 우선순위 높음

#### 1. USDT 환헷징 자동화
**현재**: USDT 프리미엄 분리 계산만 구현
**개선**: USDT 0.5% 이하 자동 매수 → 바이비트 전송

```python
# 추가 구현 필요
class USDTArbitrageStrategy:
    def check_usdt_premium(self):
        """USDT 프리미엄 체크"""
        pass

    def buy_and_transfer_usdt(self):
        """USDT 매수 및 전송"""
        pass
```

#### 2. 펀딩비 자동 수집 및 기록
**현재**: 수동 계산
**개선**: 8시간마다 자동으로 펀딩비 조회 및 DB 기록

```python
# core/strategy.py
def collect_funding_fee(self):
    """펀딩비 수집"""
    funding_rate = self.bybit.fetch_funding_rate(self.coin)
    # DB에 기록
```

#### 3. 텔레그램 알림
**추가 기능**:
- 전략 진입 알림
- 목표 수익 도달 알림
- 청산 위험 알림
- 에러 알림

```python
# services/telegram.py
class TelegramNotifier:
    def send_message(self, message):
        pass
```

### 13.2 우선순위 중간

#### 4. 웹 대시보드
- 실시간 모니터링
- 수익 차트
- 활성 전략 현황
- 수동 개입 가능

**기술 스택**: Flask/FastAPI + React

#### 5. 백테스팅 엔진
- 과거 데이터로 전략 검증
- 최적 파라미터 탐색
- 수익률 시뮬레이션

```python
# tests/backtest.py
class Backtester:
    def run(self, start_date, end_date, params):
        pass
```

#### 6. 다중 거래소 지원
- 국내: 빗썸, 코인원
- 해외: 바이낸스, OKX

### 13.3 우선순위 낮음

#### 7. 머신러닝 모델
- 김프 예측
- 진입 타이밍 최적화
- 청산 타이밍 최적화

#### 8. 고급 전략
- 부분 익절/손절
- 동적 포지션 조절
- 멀티 코인 포트폴리오

#### 9. 클라우드 배포
- AWS/GCP 자동 배포
- Docker 컨테이너화
- CI/CD 파이프라인

---

## 14. 부록

### 14.1 용어 정리

| 용어 | 설명 |
|------|------|
| 김프 (김치 프리미엄) | 한국 시장의 코인 가격이 해외보다 높은 프리미엄 |
| 헷지 (Hedge) | 양방향 포지션으로 가격 변동 위험 제거 |
| 숏 (Short) | 하락 베팅, 선물 매도 포지션 |
| 롱 (Long) | 상승 베팅, 선물 매수 포지션 |
| 청산가 (Liquidation Price) | 손실로 인해 포지션이 강제 청산되는 가격 |
| 펀딩비 (Funding Fee) | 선물 시장에서 롱-숏 간 균형을 위한 수수료 |
| Isolated Margin | 격리 마진, 포지션별 독립적인 증거금 |
| Cross Margin | 교차 마진, 전체 잔고를 증거금으로 사용 |
| VWAP | Volume Weighted Average Price, 거래량 가중 평균가 |
| Slippage | 주문 시 예상가와 실제 체결가의 차이 |

### 14.2 참고 자료

#### API 문서
- [Upbit API](https://docs.upbit.com/)
- [Bybit API](https://bybit-exchange.github.io/docs/v5/intro)
- [CCXT 문서](https://docs.ccxt.com/)
- [yfinance 문서](https://pypi.org/project/yfinance/)

#### 관련 개념
- [김치 프리미엄 설명](https://www.investopedia.com/terms/k/kimchi-premium.asp)
- [펀딩비 설명](https://www.bybit.com/en-US/help-center/bybitHC_Article?id=360039261334)
- [Isolated vs Cross Margin](https://www.bybit.com/en-US/help-center/bybitHC_Article?id=360039749613)

---

## 15. 버전 이력

| 버전 | 날짜 | 변경 사항 |
|------|------|-----------|
| 1.0.0 | 2025-01-23 | 초기 버전 완성 |
| 1.1.0 | 2025-01-25 | OHLC 데이터 수집 기능 추가<br>- Layer 3: OHLC Collector 구현<br>- PremiumOHLC 테이블 추가<br>- 일별 김프 OHLC 자동 수집<br>- OHLC 데이터 조회 유틸리티<br>- OHLC 갭 진단 도구<br>- 백테스팅 인프라 구축 |
| 1.2.0 | 2025-01-26 | 데이터베이스 기반 설정 관리 시스템 추가<br>- ConfigSetting 테이블 추가 (7번째 DB 테이블)<br>- ConfigManager 싱글톤 구현 (5초 자동 리로드)<br>- Proxy 패턴으로 투명한 설정 로드<br>- CONFIG_SOURCE 환경 변수 (env/db 선택)<br>- 11개 핵심 설정 DB 관리 지원<br>- .env → DB 자동 마이그레이션<br>- 재시작 없는 실시간 설정 변경 (5초 반영)<br>- API 키 보안 유지 (.env만 저장) |

---

## 16. 라이선스

MIT License

---

**작성자**: Claude (Anthropic)
**프로젝트**: KIMP Arbitrage Bot
**최종 수정**: 2025-01-26

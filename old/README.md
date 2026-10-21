# KIMP Arbitrage Bot (김치 프리미엄 차익거래 봇)

비트코인 및 알트코인의 한국 프리미엄(김치 프리미엄)을 활용한 자동 차익거래 봇입니다.

## 전략 개요

1. **헷지 전략**: 국내 거래소(업비트) 현물 매수 + 해외 거래소(바이비트) 숏 포지션
2. **USDT 환헷징**: 테더 프리미엄 0.5% 이하에서 매수, 0.5% 이상에서 매도
3. **펀딩비 수익**: 롱 우세 시 숏 포지션으로 펀딩비 수익 추가
4. **청산가 관리**: 진입가 대비 1.7배 이상 안전마진 확보

## 특징

- **2-Layer 아키텍처**: 메인 스캐너(Layer 1)와 독립 전략 인스턴스(Layer 2) 분리
- **상태 머신**: DB 기반 상태 관리로 재시작 시 복구 가능
- **페이퍼 트레이딩**: 실거래 전 시뮬레이션 모드
- **리스크 관리**: 청산가 실시간 모니터링 및 자동 포지션 축소
- **로그 분석**: 모든 거래 및 수익 DB 기록

## 프로젝트 구조

```
kimp/
├── config/          # 설정 및 상수
├── core/            # 메인 로직 (스캐너, 전략, 상태머신)
├── services/        # 거래소 및 외부 API 래퍼
├── models/          # DB 모델
├── utils/           # 계산 및 검증 유틸리티
├── tests/           # 테스트
├── logs/            # 로그 파일
├── main.py          # 진입점
└── requirements.txt
```

## 설치

### 1. Python 환경 설정

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 환경 변수 설정

```bash
cp .env.example .env
# .env 파일을 열어 API 키 입력
```

필수 API 키:
- **업비트**: Access Key, Secret Key
- **바이비트**: API Key, API Secret
- **한국수출입은행**: 환율 API 키

### 3. 설정 조정

[.env](.env) 파일에서 다음 항목 조정:
- `TARGET_COINS`: 거래할 코인 (쉼표 구분)
- `TARGET_PROFIT_PERCENT`: 목표 수익률 (%)
- `MAX_POSITION_KRW`: 코인당 최대 투자 금액 (원화)
- `PAPER_TRADING`: 페이퍼 트레이딩 모드 (true/false)

## 사용법

### 페이퍼 트레이딩 (권장)

```bash
# .env에서 PAPER_TRADING=true 설정 후
python main.py
```

### 실거래

```bash
# .env에서 PAPER_TRADING=false 설정 후
python main.py
```

### 로그 확인

```bash
# 실시간 로그
tail -f logs/bot.log

# DB 조회 (예시)
sqlite3 kimp_bot.db "SELECT * FROM strategies ORDER BY created_at DESC LIMIT 10;"
```

## 설정 관리

봇 설정은 두 가지 방식으로 관리할 수 있습니다:

### 방식 1: .env 파일 (기본값)

전통적인 방식으로 `.env` 파일을 직접 수정합니다.

```bash
# .env 파일 수정
TARGET_PROFIT_PERCENT=1.5
MAX_POSITION_KRW=2000000

# 봇 재시작 필요
python main.py
```

### 방식 2: 데이터베이스 (실시간 반영)

데이터베이스에서 설정을 관리하면 봇 재시작 없이 5초 내 자동 반영됩니다.

**활성화 방법:**

```bash
# 1. .env 파일에서 CONFIG_SOURCE 변경
CONFIG_SOURCE=db

# 2. 봇 재시작 (최초 1회만)
python main.py
# → ".env 설정을 DB로 마이그레이션 완료" 메시지 확인

# 3. 이후 SQL로 설정 변경
sqlite3 kimp_bot.db "UPDATE config_settings SET value='1.5' WHERE key='TARGET_PROFIT_PERCENT';"
# → 5초 이내 자동 반영 (재시작 불필요)
```

**DB 설정 조회:**

```bash
# 모든 설정 보기
sqlite3 kimp_bot.db "SELECT config_class, key, value, description FROM config_settings;"

# BotConfig 설정만 보기
sqlite3 kimp_bot.db "SELECT key, value FROM config_settings WHERE config_class='BotConfig';"
```

**주요 설정 변경 예시:**

```bash
# 목표 수익률 변경
sqlite3 kimp_bot.db "UPDATE config_settings SET value='2.0' WHERE key='TARGET_PROFIT_PERCENT';"

# 타겟 코인 변경 (JSON 형식)
sqlite3 kimp_bot.db "UPDATE config_settings SET value='[\"BTC\",\"ETH\",\"SOL\"]' WHERE key='TARGET_COINS';"

# 최대 포지션 변경
sqlite3 kimp_bot.db "UPDATE config_settings SET value='5000000' WHERE key='MAX_POSITION_KRW';"
```

**참고:**
- API 키는 보안상 데이터베이스에 저장되지 않으며, 항상 `.env` 파일에서 로드됩니다
- DB 모드는 11개 설정만 지원: TARGET_COINS, TARGET_PROFIT_PERCENT, MIN_ENTRY_PREMIUM, MAX_POSITION_KRW, MAX_POSITION_USDT, LIQUIDATION_BUFFER, MIN_ORDERBOOK_DEPTH_MULTIPLIER, STOP_LOSS_PERCENT, SCAN_INTERVAL, MAX_CONCURRENT_STRATEGIES, MONITORING_INTERVAL

## 상태 흐름

```
INIT → CHECK_CAPITAL → VALIDATE_ENTRY
  → ENTERING → HEDGED → MONITORING
  → (PROFITABLE / STOP_LOSS / LIQUIDATION_RISK)
  → EXITING → SETTLEMENT → EXITED
```

## 수수료 및 비용

- 업비트 현물: 0.05%
- 바이비트 선물: Maker -0.025%, Taker 0.075%
- USDT 출금 (TRC20): 1 USDT
- 슬리피지 버퍼: 0.1%

## 주의사항

⚠️ **리스크 고지**:
- 암호화폐 거래는 고위험 투자입니다
- 청산 리스크, 시장 급변동 리스크가 존재합니다
- 반드시 페이퍼 트레이딩으로 충분히 테스트 후 소액으로 시작하세요
- 투자 손실에 대한 책임은 사용자 본인에게 있습니다

## 라이선스

MIT License

## 문의

이슈 또는 개선 제안은 GitHub Issues를 이용해주세요.

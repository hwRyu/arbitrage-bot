# arbitrage-bot

업비트 원화 마켓 + 바이비트 USDT 무기한 김프 헷지 봇 (현재: 페이퍼 트레이딩 전용)

> **전략의 기준은 [docs/concept.md](docs/concept.md)** 입니다. 다른 문서나 코드와 다르면 그 문서가 맞습니다.

## 전략 한눈에
- **출발 상태:** 업비트에 원화, 바이비트에 USDT (USDT는 테더 역프일 때 직접 사서 옮겨둠)
- **진입:** 고른 코인(`COINS`) 중 김프가 기준 이하로 낮으면, 바이비트에서 숏 + 같은 금액만큼 업비트에서 매수 (금액 헷징, 레버리지 1배)
- **청산:** 김프가 올라서 수수료·전송비·펀딩비를 다 빼고도 목표 수익이 나면 양쪽 정리
- **코인 가격 방향은 상관없음:** 양쪽 금액이 같아서 김프가 그대로면 가격이 얼마나 움직여도 손익 0

## 김프 계산 (김프 사이트와 같은 방식)
```
김프(%) = (업비트 원화 가격 ÷ (바이비트 USDT 가격 × 달러 환율) − 1) × 100
```
- 달러 환율 기준이라 **테더 프리미엄이 포함**됩니다. 원화와 USDT를 각자 거래소에 그대로 두는 전제에서는 테더 프리미엄 변화도 실제 손익이기 때문입니다.
- 진입은 업비트에서 살 가격과 바이비트에서 숏 칠 가격, 청산은 그 반대 가격으로 판단합니다. 둘 다 호가창을 정한 수량만큼 긁은 평균가입니다.
- 손익은 원화 기준입니다. 바이비트 USDT 손익을 달러 환율로 바꿔서 합칩니다.
- 진입·청산 예시 8가지는 [docs/concept.md 9번](docs/concept.md#9-예시로-보는-손익-구조)에 있습니다.

## 코인별 설정
진입 기준, 목표 순이익, 손절, 포지션 규모는 공통값이 기본이고, 코인별로 덮어쓸 수 있습니다.
```bash
ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.5        # 공통
SOL_ENTRY_PREMIUM_THRESHOLD_PERCENT=-1.0    # SOL 만 -1.0%
SOL_TARGET_NET_PROFIT_PERCENT=1.5
```

## 실행
```bash
pip install -r requirements.txt
cp .env.example .env      # 코인, 진입 기준, 비용 설정
python main.py            # Ctrl+C 로 종료 (열린 포지션은 DB 에 남아 다음 실행 때 이어서 관리)
```

## 김프 기록 분석
봇이 돌면서 코인마다 1분에 한 번 김프, 테더 몫·코인 몫, 펀딩비율을 기록합니다.
```bash
python -m kimp.report             # 최근 7일
python -m kimp.report --hours 24  # 최근 24시간
```
코인별 김프 수준, 진입 기준별 시간 비율, 김프 구간별 평균 펀딩비율을 보여줍니다. 진입 기준을 정하고 펀딩비 대응을 설계할 때 씁니다([향후 과제](docs/concept.md#10-향후-과제)).

## 테스트
```bash
python -m unittest discover -s tests -v
```

## 구조
| 파일 | 역할 |
|---|---|
| `kimp/config.py` | `.env` 설정 읽기와 검사 (비용, 코인별 설정, 환율 설정) |
| `kimp/fx.py` | 달러 환율 가져오기 (실패해도 멈추지 않음) |
| `kimp/premium.py` | 김프 계산, 호가창으로 체결가 계산 |
| `kimp/pnl.py` | 비용 포함 손익 계산 (원화 기준) |
| `kimp/market_data.py` | 업비트·바이비트 시세 (ccxt) |
| `kimp/paper_broker.py` | 가짜 주문 체결, 페이퍼 잔고, 펀딩비 |
| `kimp/engine.py` | 스캔 → 가진 포지션 관리 → 새 진입 |
| `kimp/db.py` | SQLite 테이블 |
| `kimp/report.py` | 김프 기록 분석 리포트 |

- 파일별 자세한 역할: [docs/architecture.md](docs/architecture.md)
- 이전 버전 분석: [docs/old-analysis.md](docs/old-analysis.md)

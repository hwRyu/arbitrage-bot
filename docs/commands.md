# 명령어와 실행 방법

> 이 맥에는 `python` 이 없고 `python3` 만 있다. 모든 명령은 `python3` 로 실행한다.
> (`python` 으로 치고 싶으면 `~/.zshrc` 에 `alias python=python3`)

## 0. 처음 한 번
```bash
cd ~/Desktop/Individual/Repository/arbitrage-bot
pip3 install -r requirements.txt
cp .env.example .env        # 코인, 기준값, 자본, 수수료를 여기서 고친다 (없으면 기본값)
python3 -m unittest discover -s tests   # 테스트 (인터넷 필요 없음)
```

## 1. 한눈에
| 하고 싶은 것 | 명령 | 걸리는 시간 |
|---|---|---|
| 봇 실행 (김프 + 펀딩비, 페이퍼) | `python3 main.py` | Ctrl+C 로 끔 |
| 김프 전략 코인 추천 순위 | `python3 -m kimp.screen` | 처음 2~3분, 다음부터 수십 초 |
| 김프 전략 과거 시뮬레이션 | `python3 -m kimp.simulate --from 2026-09-22` | 코인당 처음 1~2분 |
| 펀딩비 전략 코인 순위 | `python3 -m kimp.carry_backtest screen` | 처음 약 2분 |
| 펀딩비 전략 과거 시뮬레이션 | `python3 -m kimp.carry_backtest run --from 2026-09-22` | 처음 1~2분 |
| 봇이 쌓은 김프 기록 분석 | `python3 -m kimp.report` | 몇 초 |

날짜는 모두 **한국 시간**이다. `--to` 를 안 적으면 지금까지.
과거 데이터는 `data/history.db` 에 저장돼서 두 번째부터는 모자란 구간만 받는다.

## 2. 봇 실행 — `python3 main.py`
- 김프 헷지 엔진(3초마다)과 펀딩비 엔진(60초마다, `CARRY_ENABLED=true`)이 같이 돈다. **지금은 페이퍼(가짜 주문)만** 된다.
- 끄기: Ctrl+C. 열린 포지션과 잔고는 `data/kimp.db` 에 남아서 다시 켜면 이어서 관리한다.
- 로그: 화면 + `logs/` 폴더
- 처음부터 다시 하고 싶으면 봇을 끄고 `data/kimp.db` 를 지운다 (페이퍼 잔고·포지션 초기화).

## 3. 김프 전략 도구
### 코인 추천 순위 — `python3 -m kimp.screen`
| 옵션 | 기본 | 뜻 |
|---|---|---|
| `--days` | 30 | 최근 며칠을 볼지 |
| `--top` | 20 | 몇 위까지 |
| `--target` | `.env` | 목표 순이익 % (바꿔서 비교할 때) |
| `--coins` | 전체 | `XRP,SOL` 처럼 정한 코인끼리만 |
| `--cycle-hours` | 24 | 반복 주기가 이 시간 이하면 가중치 1 |
| `--size-usdt` | 자동 | 포지션 크기 (물량 확인용) |
| `--min-upbit-krw` / `--min-bybit-usdt` | 10억원 / 1M | 24시간 거래대금 최소 |
| `--max-coins` | 80 | 거래대금 상위 몇 개까지 |
| `--end` | 지금 | 이 날짜까지의 데이터로 고르기 (예: `--end 2026-09-22` 로 고르고 9/22~10/22 를 시뮬레이션해서 검증) |

끝에 `.env` 에 붙여넣을 `COINS=...` 와 코인별 진입 기준이 나온다. `이상김프` 표시 코인은 쓰지 말 것.

### 과거 시뮬레이션 — `python3 -m kimp.simulate`
```bash
python3 -m kimp.simulate --from 2026-09-22                       # 9/22 0시부터 지금까지
python3 -m kimp.simulate --from 2026-09-22 --to 2026-10-15
python3 -m kimp.simulate --from 2026-09-22 --coins MEW,CAP --verbose   # 거래 하나하나 보기
python3 -m kimp.simulate --from 2026-09-22 --no-save             # 결과를 DB 에 안 남김
```

### 김프 기록 분석 — `python3 -m kimp.report`
```bash
python3 -m kimp.report              # 최근 7일
python3 -m kimp.report --hours 24
```
봇을 돌리면서 쌓인 기록이 있어야 나온다.

## 4. 펀딩비 전략 도구 (자세한 설명: [carry.md](carry.md))
```bash
python3 -m kimp.carry_backtest screen                      # 최근 30일 코인 순위
python3 -m kimp.carry_backtest screen --days 60 --top 30
python3 -m kimp.carry_backtest run --from 2026-09-22       # .env 설정 그대로 과거 시뮬레이션
python3 -m kimp.carry_backtest run --from 2026-09-22 --coins COMP,SUI --verbose
```

## 5. 설정을 잠깐만 바꿔서 돌리기
`.env` 를 고치지 않고 명령 앞에 붙이면 그 실행에만 적용된다.
```bash
# 시드 250 USDT, 포지션 50 USDT 고정, MEW·CAP 만, MEW 진입 기준 -0.10%
PAPER_INITIAL_TOTAL_USDT=250 POSITION_SIZE_USDT=50 MEW_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.10 \
  python3 -m kimp.simulate --from 2026-09-22 --coins MEW,CAP

# 펀딩비 전략 진입 기준 0.05%/일, 2배
CARRY_ENTRY_MIN_DAILY_PERCENT=0.05 CARRY_LEVERAGE=2 python3 -m kimp.carry_backtest run --from 2026-09-22
```

## 6. 자주 쓰는 설정 (전체는 `.env.example`)
| 설정 | 기본 | 뜻 |
|---|---|---|
| `COINS` | XRP,SOL,DOGE | 김프 전략 코인 |
| `ENTRY_PREMIUM_THRESHOLD_PERCENT` | −0.5 | 진입 김프 기준 (코인별: `<코인>_ENTRY_PREMIUM_THRESHOLD_PERCENT`) |
| `TARGET_NET_PROFIT_PERCENT` | 1.0 | 목표 순이익 % |
| `STOP_LOSS_PERCENT` | −5.0 | 손절 % |
| `PAPER_INITIAL_TOTAL_USDT` | 10,000 | 김프 전략 페이퍼 시작 자본 |
| `POSITION_SIZE_USDT` | 자동 | 포지션 하나 크기 |
| `BYBIT_LEVERAGE` | 1 | 김프 전략 숏 레버리지 (자본 비율 자동) |
| `LIQUIDATION_WARN_PERCENT` | 15 | 강제청산까지 남은 상승폭이 이보다 작으면 경고 (두 전략 공통) |
| `REBALANCE_THRESHOLD_PERCENT` | 10 | 양쪽 비율이 이만큼 깨지면 리밸런싱 |
| `REBALANCE_TRANSFER_FEE_USDT` | 1.0 | 송금 한 번 수수료 (송금할 때만) |
| `PAPER_AUTO_REBALANCE` | true | 페이퍼·시뮬레이션에서 필요할 때 자동 송금 흉내 |
| `CARRY_ENABLED` | true | 펀딩비 전략 같이 돌리기 |
| `CARRY_CAPITAL_USDT` | 1,000 | 펀딩비 전략 자본 (따로) |
| `CARRY_LEVERAGE` | 1 | 펀딩비 전략 숏 레버리지 |
| `CARRY_LIQ_GUARD_PERCENT` | 15 | 펀딩비 전략: 강제청산까지 이보다 가까우면 양쪽 미리 정리 |

## 7. 송금(리밸런싱)은 누가 하나
- **지금은 실제 송금을 봇이 하지 않는다.** 실주문 기능 자체가 아직 없어서(페이퍼만) "필요하면 알림 → 사용자가 직접 송금" 이다.
- **실거래 단계에서 자동 송금으로 바꿀 계획이다.** 송금 시점은 지금 페이퍼와 같고, 출금 API 권한·출금 주소 등록·트래블룰 확인이 먼저 필요하다 (concept.md 11번).
- 페이퍼·시뮬레이션에서는 페이퍼 잔고를 사람이 옮길 수 없으니, 필요한 순간(청산 후 비율이 10%p 넘게 깨짐 / 진입하려는데 한쪽이 모자람)에 **사용자가 보냈다고 치고** 자동으로 옮기고 수수료 1번을 뗀다.
- 펀딩비 전략은 바이비트 안에서 끝나서 송금이 필요 없다.

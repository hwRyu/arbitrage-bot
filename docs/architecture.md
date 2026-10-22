# 코드 구조와 파일별 역할

- 지금 버전: 페이퍼 트레이딩(가짜 주문, 진짜 시세) 전용. 실주문은 아직 없음.
- 전략의 기준: [concept.md](concept.md). 이 문서는 "코드가 어디서 무엇을 하는지" 만 설명한다.
- 실행 방법: [README](../README.md)

## 1. 전체 흐름

```
main.py
 ├─ load_settings()      .env 읽기 + 검사
 ├─ MarketData           거래소 마켓 정보 1번 받기, 양쪽에 다 있는 코인만 남기기
 ├─ FxProvider           달러 환율 (표시용. 못 받아도 매매에 영향 없음)
 ├─ PaperBroker          가짜 체결 + 잔고 (레버리지, 유지증거금률)
 ├─ make_session_factory DB 파일 열기
 └─ Engine.run()         Ctrl+C 전까지 SCAN_INTERVAL_SEC 마다 step() 반복
      └─ startup()       페이퍼 시작 자본을 목표 비율대로 분배, 자본 비율 확인 (리밸런싱 알림)

Engine.step()  (스캔 한 번)
 1. MarketData.fetch_quotes(코인, 환율)  업비트 코인 + 업비트 USDT/KRW + 바이비트 호가를 같은 순간으로 → Quote
 2. 가진 포지션마다
      _update_funding()                  지난 펀딩비 정산 반영, 다음 정산 정보 받기
      _check_liquidation()               강제청산 가격 도달 → 업비트도 즉시 정리 / 가까우면 증거금 추가 경고
      _manage_position()                 "지금 청산하면?" → 목표 넘으면 익절, 손절선 아래면 손절
 3. 청산이 있었으면 _check_rebalance()    양쪽 비율이 목표에서 10%p 넘게 벗어나면 옮길 USDT 알림
 4. _try_entries()                       김프 ≤ 기준인 코인 진입 (크기: 설정값 또는 자동)
 5. _save_snapshots() / _log_status()    김프 기록, 현황 한 줄 로그
```

파일끼리 부르는 방향: `engine → paper_broker / market_data → pnl / premium → config`.
`premium.py`, `pnl.py`, `fx.py` 는 인터넷이나 DB 없이 계산만 해서 테스트하기 쉽다.

## 2. 파일별 역할

### `main.py` — 시작점
- 설정 읽기 → 준비 → 엔진 실행. `PAPER_TRADING=false` 는 거부 (실주문 코드 없음)
- 시작할 때 보여주는 것: 공통 설정, 레버리지와 목표 자본 비율, 비용, 코인별 적용값(따로 정한 값에 `*`), 설정 경고

### `kimp/config.py` — 설정
- `.env` 를 읽어 `Settings` 하나로 만든다 (`load_settings()`)
- `FeeSettings`: 업비트 수수료율(코인·USDT 매매 공통), 바이비트 수수료율, 사이클당 전송비(선택, 기본 0), 표시용 여유분. 송금 수수료는 방향별 `Settings.transfer_fee_usdt(to_bybit)` (업비트 출금 무료 0 / 바이비트 출금 1.3), 페이퍼 자동 송금은 `paper_auto_rebalance`
  - `round_trip_trade_fee_percent()`: 업비트 4번 + 바이비트 2번 = 0.31%
- **코인별 덮어쓰기:** 진입 기준, 목표, 손절, 포지션 크기. `params_for(coin)` 으로 꺼내 쓴다
- **레버리지 / 자본:** `bybit_leverage`, `bybit_margin_buffer_percent`, `bybit_mmr`, `liquidation_warn_percent`, `rebalance_threshold_percent`
  - `upbit_target_share()`: 업비트 목표 비율 = 레버리지 ÷ (레버리지 + 1 + 여유분)
- **포지션 크기:** `position_size_usdt` (None 이면 자동), `capital_usage_percent`
- `validate_settings()`: 이상한 값을 한 번에 모아서 알려준다

### `kimp/premium.py` — 김프와 체결 계산
- `premium_percent(업비트 원, 바이비트 USDT, 원/USDT)`: 김프(%). 봇은 세 번째에 업비트 USDT 가격을 넣는다
- `tether_premium_percent()`: 테더 프리미엄 (표시용)
- `estimate_fill()` (개수 지정) / `estimate_fill_by_cost()` (금액 지정): 호가창을 긁은 평균가
- `Quote`: 코인 하나의 한 순간 시세 (업비트 코인, 바이비트, **업비트 USDT/KRW**, 달러 환율, 시뮬레이션용 바이비트 고가)
  - `mid_premium`: 봇 김프 (코인 몫, 가운데 가격)
  - `entry_premium_top` / `exit_premium_top`: 맨 앞 호가로 계산한 진입·청산 김프
  - `site_premium` / `tether_premium`: 사이트식 김프, 테더 프리미엄 (표시용)
  - `entry_plan(바이비트 개수, 수수료율)` → `EntryPlan`: 바이비트 숏 → 그 금액만큼 USDT 매도 → 받은 원화로 코인 매수
  - `exit_plan(업비트 개수, 바이비트 개수, 수수료율)` → `ExitPlan`: 코인 매도 → 받은 원화로 USDT 매수, 숏 닫기
  - `entry_premium_for()`: **진입 판단 기준값** (실제 체결가로 계산한 진입 김프)

### `kimp/pnl.py` — 손익 (USDT)
- `EntryLegs` / `ExitLegs`: 판·되산 USDT 개수와 가격, 코인 개수와 가격, 바이비트 값, 수수료
- `compute_pnl()` → `PnLBreakdown`
  - 업비트 = 되산 USDT − 판 USDT (수수료·호가 간격 포함 실제 결과). 보여주기용으로 수수료를 따로 뺀다
  - 바이비트 = 숏 손익 − 수수료 (강제청산이면 −증거금)
  - 순손익 = 업비트 + 바이비트 + 펀딩비 − 전송비
- `liquidation_price()`: 숏 강제청산 가격 ≈ 진입가 × (1 + 1/레버리지 − 유지증거금률)
- `margin_to_add_usdt()`: 강제청산까지 거리를 되돌리려면 넣어야 할 증거금
- `estimated_exit_premium()`: "김프가 대략 몇 % 가 되면 목표인지" (로그용)

### `kimp/market_data.py` — 거래소 시세 (ccxt)
- `fetch_quotes(coins, fx_krw)`: 업비트 코인 + USDT/KRW 호가를 요청 1번에, 바이비트는 코인마다. **USDT/KRW 호가가 없으면 그 스캔은 건너뛴다** (김프 계산 기준이라서)
- `filter_supported()`, `round_qty()`, `min_qty()`, `fetch_funding()`

### 모드 구조: 페이퍼·드라이런·실거래는 같은 로직 (자세히: [live.md](live.md))
엔진 → 브로커(공통) → 실행기(모드마다 다름). 실행기는 주문 '한 동작' 씩만 한다.

### `kimp/broker.py` — 공통 브로커 `HedgeBroker`
- `open_hedge()`: 계산 확인(호가·밀림·잔고) → `OPENING` 저장 → ① 바이비트 숏 ② 업비트 USDT 매도 ③ 받은 원화로 코인 매수. 단계마다 `Fill` 저장
  - 실패하면 `_unwind_entry()`: 끝난 단계를 거꾸로 되돌림 → `ABORTED`, 되돌리기도 실패하면 `ERROR`
- `value_exit()` / `value_liquidation()`: 지금 청산하면 얼마 (판단용)
- `close_hedge()` → `_continue_close()`: `CLOSING` → 코인 매도 → USDT 매수 → 숏 닫기. 실패하면 다음 스캔에 남은 단계만. 손익은 실제 체결값으로
- `sync()`: 매 스캔 — 송금 도착 확인, `OPENING`/`CLOSING` 이어서, 주기적으로 `reconcile()` (DB ↔ 실제 숏·코인 개수)
- `transfer()` / `_poll_transfers()`: 송금 요청 → 도착 확인 (`transfers` 테이블). 송금 중엔 또 안 보냄, 하루 한도·최소 금액
- `capital_split()`: 양쪽 자본 (송금 중인 돈은 받는 쪽으로)
- `PaperBroker`: `HedgeBroker` + `PaperExecutor` / `broker_kwargs(settings)`: 실시간 봇과 시뮬레이션이 같은 옵션을 쓰게

### `kimp/executor.py` — 페이퍼 실행기 `PaperExecutor`
- 동작: `open_short`, `sell_usdt`, `buy_coin_with_krw`, `sell_coin`, `buy_usdt_with_krw`, `close_short`, `withdraw`, `transfer_arrived`, `free`, `coin_total`, `short_qty`
- 그 순간 호가로 체결 계산, 장부 `paper_balances` (`upbit:USDT`, `upbit:<코인>`, `upbit:KRW`, `bybit:USDT`, `bybit:SHORT:<코인>`)
- `fail_next`: 테스트에서 특정 동작을 한 번 실패시키기

### `kimp/live_executor.py` — 실거래 / 드라이런 실행기
- `LiveExecutor`: ccxt 로 실제 주문 → 끝날 때까지 조회 → 실제 체결 개수·평균가·수수료. 시작 때 `setup()` 으로 바이비트 단방향·격리·레버리지. 출금·입금 확인
- `DryRunExecutor`: 실제 잔고로 장부 시작 + 페이퍼처럼 계산 + "[드라이런] 실제였다면 …" 로그. 주문 안 냄

### `kimp/safety.py` — 안전장치 `Safety`
- `entries_allowed()`: 비상정지 파일, 텔레그램 /stop, 연속 실패, `ERROR` 포지션, 대조 불일치, 하루 손실 한도 → 하나라도 있으면 신규 진입 중단

### `kimp/notify.py` — 텔레그램
- `TelegramLogHandler`: 경고 이상 + 진입·청산·송금 로그를 텔레그램으로 / `TelegramCommands`: `/status` `/stop` `/resume`

### `kimp/live_check.py` — 실거래 전 점검 (읽기만)

### `kimp/paper_broker.py` — 예전 이름 호환 (`PaperBroker`, `UPBIT_USDT` …)

### `kimp/engine.py` — 전략 엔진
- `step()`: 스캔 한 번 (위 흐름 참고)
- `_check_liquidation()`: 강제청산 감시와 처리
- `_manage_position()` / `_exit_rules()`: 목표·손절은 진입 때 포지션에 저장한 값
- `_check_rebalance()`: 목표 비율에서 많이 벗어났으면 옮길 USDT 를 알림 (시작할 때, 청산 직후)
- `_position_size_usdt()`: 포지션 크기 (설정값 또는 자동)
- `_try_entries()` / `_plan_bybit_qty()`: 진입
- `_update_funding()`: 펀딩비
- `_log_status()`: USDT 가격, 테더 프리미엄, 코인별 봇 김프(사이트식 김프)/기준, 자본 비율
- `clock`: 시각 함수 (평소 진짜 시계, 시뮬레이션은 가짜 시계)

### `kimp/fx.py` — 달러 환율 (표시용)
- yfinance `KRW=X`. 실패하면 마지막 값. 못 받아도 매매에는 영향 없음. `FIXED_USD_KRW` 로 고정 가능

### `kimp/db.py` — DB 테이블 (SQLite)
| 테이블 | 내용 |
|---|---|
| `positions` | 포지션 1개 = 진입~청산. 판/되산 USDT, 코인·숏 개수와 가격, 수수료, 김프(봇·사이트식·테더), 레버리지·증거금·강제청산 가격, 진입 때 기준, 펀딩비, 확정 손익(USDT) |
| `fills` | 체결 한 건씩 (`sell_usdt`, `buy`, `open_short`, `sell`, `buy_usdt`, `close_short`, `liquidated`) |
| `funding_events` | 펀딩비 받거나 낸 기록 |
| `premium_snapshots` | 코인마다 1분에 1건: 봇 김프, 진입/청산 김프, USDT 가격과 호가 간격, 달러 환율, 테더 프리미엄, 사이트식 김프, 펀딩비율·다음 정산·주기 |
| `paper_balances` | 페이퍼 잔고 |

- 시간은 모두 UTC. 구조가 바뀌면 예전 DB 파일과 맞지 않으니 DB 파일을 지우고 새로 시작 (아직 운영 데이터 없음)

### `kimp/report.py` — 김프 기록 분석 리포트
- `python3 -m kimp.report [--hours 24] [--db 경로]`
- 코인별: 봇 김프 평균·최저·최고 (참고로 사이트식 김프·테더), 진입 김프 분포, 호가 간격 비용, 펀딩비율, 기준별 시간 비율, **김프 구간별 평균 펀딩비율**

### `kimp/history.py` — 과거 데이터 (시뮬레이션용)
- 별도 DB `data/history.db`: `candles`(업비트 코인·업비트 USDT·바이비트 1분봉, 추천 코인 순위용 15분봉은 출처 이름에 `@15m`), `fx_hourly`, `funding_history`, `fetched_ranges`, `sim_runs`, `sim_trades`
- 모자란 구간만 받고, 거래소 점검 같은 빈 구간은 건너뛰고 계속 받는다. 환율은 주말 대비 7일 앞부터

### `kimp/simulate.py` — 시뮬레이션
- `python3 -m kimp.simulate --from 날짜 [--to 날짜] [--coins ...] [--verbose] [--no-save]`
- **실시간 봇과 같은 `Engine`, `PaperBroker`** 를 쓴다. `HistoricalMarket`(1분봉 종가 → 호가창, 바이비트 고가 → 강제청산 확인)와 `SimClock`(가짜 시계)만 다르다
- 호가 간격: 코인은 `decide_spreads()`(실측 → 코인별 설정 → 기본값) + `apply_tick_floor()`(호가 한 칸 하한), USDT/KRW 는 호가 한 칸
- 결과: 확정 손익과 평가 손익을 나눠서, USDT 로 (원화 참고값 포함)

### `kimp/screen.py` — 추천 코인 순위
- `python3 -m kimp.screen [--days 30] [--top 20] [--target 0.5] [--coins ...] [--timeframe 15m] [--cycle-hours 24]`
- 양쪽 거래소에 다 있는 코인 전체 → 24시간 거래대금으로 거르기 → 15분봉으로 봇 김프 기록 만들기
- 코인마다: 김프 통계(`premium_stats`: 중간값, 범위, 횡보 비율, 추세, 반감기), 지금 호가 간격·물량, 평균 펀딩비율
- `run_swing()`: 진입 기준 하나로 '한 번에 포지션 1개' 백테스트 (펀딩비 포함, 진입·청산마다 호가 간격 한 번씩 손해)
- `best_swing()`: 기준 후보(진입 김프 하위 1~50% 지점) 중 가장 좋은 것. 기본 점수 = min(전체 월수익, 앞 2/3 로 고른 기준의 뒤 1/3 월수익), 점수 = 기본 점수 × `cycle_weight()`(반복 주기) × `band_weight()`(띠 이동). 이상김프(±5%)는 맨 아래
- 끝에 `.env` 에 붙여넣을 `COINS=` 와 코인별 진입 기준을 출력

### `kimp/carry.py` — 펀딩비 수익 전략 (자세히: [carry.md](carry.md))
- `funding_daily_percent()`, `make_stats()`: 펀딩비 기록 → 72시간·24시간 하루 평균 %
- `entry_ok()` / `exit_due()`: 진입·청산 규칙
- `CarryBroker`: 페이퍼 체결과 잔고 (`carry:USDT`, `carry:<코인>`), 펀딩비 반영, 강제청산
- `CarryEngine`: 펀딩비 반영 → 강제청산·안전장치·청산 → 진입. `main.py` 가 따로 스레드로 돌린다
- `LiveCarryMarket`: 바이비트 실시간 (전용 ccxt 객체, 후보 자동 선택, 펀딩비 기록 캐시)
- DB 테이블: `carry_positions`, `carry_funding` (같은 `data/kimp.db`)

### `kimp/carry_backtest.py` — 펀딩비 전략 순위 / 시뮬레이션
- `HistoricalCarryMarket`: 1시간봉(바이비트 현물 `bybit_spot@1h`, 선물 `bybit@1h`) + 펀딩비 기록 → 같은 `CarryEngine` 으로 돌림
- `screen`: 코인마다 1,000 USDT 하나로 돌린 결과 순위 (점수 = min(전체, 뒤 절반) 자본 대비 월 수익)
- `run`: `.env` 설정 그대로 포트폴리오 시뮬레이션

### `tests/` — 테스트 (인터넷 필요 없음)
| 파일 | 확인하는 것 |
|---|---|
| `test_calc.py` | 김프 공식, 체결 계산, **concept.md 예시 A~H 숫자와 코드 계산이 같은지**, 강제청산 가격 |
| `test_engine.py` | 왕복 후 잔고 = 계산 손익, USDT 가격이 움직여도 손익 0, 진입/익절/손절, 진입 때 기준 고정, 펀딩비, 강제청산과 경고, 시작 리밸런싱 알림, 자동 포지션 크기, 코인별 설정 |
| `test_simulate.py` | 시뮬레이션 흐름, 호가 단위 하한(DOGE 가짜 수익 방지), 모자란 구간만 받기, 점검 빈 구간 |
| `test_fx.py` | 고정 환율, 갱신 간격, 실패 시 이전 값 |
| `test_config.py` | 코인별 덮어쓰기, 레버리지 → 자본 비율, 잘못된 값 거부 |
| `test_report.py` | 리포트 숫자 계산 |
| `test_live_broker.py` | 가짜 ccxt 업비트·바이비트로 실거래 실행기: 실제 체결값 손익, 되돌리기, ERROR, 청산 이어서, 재시작 복구, 대조 불일치, 강제청산 처리, 송금 요청→도착·한도·지연 알림, 드라이런 주문 0건, 안전장치, 설정, 텔레그램 |
| `test_carry.py` | 펀딩비 평균, 진입·청산 규칙, 왕복 잔고 = 손익, 가격 변동 상쇄, 강제청산 안전장치, 강제청산, 자동 크기 |
| `test_screen.py` | 추천 코인 순위: 김프 기록, 반감기, 스윙 백테스트(익절·손절·펀딩비·끝까지 보유), 순위 |

실행: `python3 -m unittest discover -s tests -v`

## 3. 단위 규칙
- 수수료율: 소수 (`0.0005` = 0.05%) / 퍼센트: 숫자 그대로 (`1.0` = 1%)
- 금액 변수 이름 끝에 단위: `_krw`(원), `_usdt`. **손익과 자본은 USDT**
- 개수: 금액 헷징이라 업비트 코인 개수와 바이비트 숏 개수가 다르다 (`upbit_qty`, `bybit_qty`)

# 코드 구조와 파일별 역할

- 지금 버전: 페이퍼 트레이딩(가짜 주문, 진짜 시세) 전용. 실주문은 아직 없음.
- 전략의 기준: [concept.md](concept.md). 이 문서는 "코드가 어디서 무엇을 하는지" 만 설명한다.
- 실행 방법: [README](../README.md)

## 1. 전체 흐름

```
main.py
 ├─ load_settings()      .env 읽기 + 검사
 ├─ MarketData           거래소 마켓 정보 1번 받기, 양쪽에 다 있는 코인만 남기기
 ├─ FxProvider           달러 환율 공급기 (처음에 1번 받아봄)
 ├─ PaperBroker          가짜 체결 + 잔고
 ├─ make_session_factory DB 파일 열기
 └─ Engine.run()         Ctrl+C 전까지 SCAN_INTERVAL_SEC 마다 step() 반복

Engine.step()  (스캔 한 번)
 1. FxProvider.get()                  달러 환율
      없음 → 이번 스캔 쉼 / 오래됨 → 새 진입만 막음
 2. MarketData.fetch_quotes(코인, 환율) 모든 코인 호가를 같은 순간으로 → Quote
 3. 가진 포지션마다
      _update_funding()               펀딩비 정산 시각이 지났으면 1번 반영
      _manage_position()              "지금 청산하면?" 계산 → 목표 넘으면 익절, 손절선 아래면 손절
 4. _try_entries()                    김프 ≤ 기준인 코인 진입 (기준보다 더 낮은 코인부터)
 5. _save_snapshots() / _log_status() 김프 기록, 현황 한 줄 로그
```

파일끼리 부르는 방향: `engine → paper_broker / market_data / fx → pnl / premium → config`.
`premium.py`, `pnl.py`, `fx.py` 는 인터넷이나 DB 없이 계산만 해서 테스트하기 쉽다.

## 2. 파일별 역할

### `main.py` — 시작점
- 설정을 읽고 준비한 다음 엔진을 돌린다.
- `PAPER_TRADING=false` 면 거부한다. (실주문 코드가 없어서)
- 시작할 때 보여주는 것: 공통 설정, 비용, 환율 설정, 코인별 적용값(따로 정한 값에 `*`), 설정 경고.
- Ctrl+C 를 누르면 하던 스캔을 마치고 끝난다.

### `kimp/config.py` — 설정
- `.env` 를 읽어서 `Settings` 하나로 만든다. (`load_settings()`)
- `FeeSettings`: 손익에 들어가는 비용 (업비트 수수료율, 바이비트 수수료율, 사이클당 전송비, 표시용 슬리피지 여유)
- **코인별 덮어쓰기:** 진입 기준, 목표, 손절, 포지션 규모는 공통값이 기본이다. `.env` 에 `<코인>_<항목>` (예: `SOL_ENTRY_PREMIUM_THRESHOLD_PERCENT=-1.0`) 을 넣으면 그 코인만 바뀐다.
  - `Settings.params_for(coin)`: 그 코인에 실제로 쓸 값. 엔진은 이것만 쓴다.
  - `COINS` 에 없는 코인의 설정 키가 있으면 경고한다. (오타 방지)
- **달러 환율 설정:** `FX_REFRESH_SEC`(갱신 간격), `FX_MAX_STALE_SEC`(이보다 오래되면 새 진입 중지), `FIXED_USD_KRW`(고정값)
- `validate_settings()`: 잘못된 값을 시작할 때 한 번에 모아서 알려준다. 수수료를 % 로 잘못 넣는 실수(0.05 vs 0.0005)도 잡는다.
- 모든 설정 설명은 [.env.example](../.env.example) 에 있다.

### `kimp/fx.py` — 달러 환율
- `FxProvider.get()` → `FxQuote(rate, age_sec, is_stale)`
- yfinance `KRW=X` 에서 `FX_REFRESH_SEC` 마다 받아온다.
- 실패하면 마지막 성공 값을 쓴다. 너무 오래되면 `is_stale=True` 로 알려준다.
- 한 번도 못 받았으면 `None` 이다. 이때 엔진은 그 스캔을 쉰다.
- `FIXED_USD_KRW` 가 있으면 조회 없이 그 값을 쓴다.

### `kimp/premium.py` — 김프와 체결 계산
- `premium_percent(업비트 원, 바이비트 USDT, 달러 환율)`: 김프(%)
- `tether_premium_percent(업비트 USDT 가격, 달러 환율)`: 테더 프리미엄 (표시용)
- `coin_premium_percent(전체 김프, 테더 몫)`: 테더 몫을 뺀 코인 몫 김프 (분석용). `Quote.split_premium()` 으로 한 번에 나눈다
- `estimate_fill()`: **개수를 정해서** 시장가 주문할 때 평균 체결가 (바이비트 숏 열기/닫기, 업비트 팔기)
- `estimate_fill_by_cost()`: **금액을 정해서** 시장가로 살 때 체결 개수 (업비트 사기)
- `Quote`: 코인 하나의 한 순간 시세 (업비트 호가, 바이비트 호가, 달러 환율, 업비트 USDT 가격)
  - `mid_premium`: 가운데 가격 김프 (사이트 숫자와 비슷)
  - `entry_premium_top` / `exit_premium_top`: 맨 앞 호가로 계산한 진입·청산 김프
  - `entry_fills(bybit_qty)`: 바이비트 숏 개수만큼 bids 를 긁고, 그 금액 × 달러 환율 원어치만큼 업비트 asks 를 긁는다 (금액 헷징)
  - `exit_fills(upbit_qty, bybit_qty)`: 가진 개수만큼 청산할 때
  - `entry_premium_for(bybit_qty)`: **진입 판단 기준값**. 실제 체결가로 계산한 진입 김프

### `kimp/pnl.py` — 손익 (원화 기준)
- `EntryLegs` / `ExitLegs`: 진입·청산 값 (양쪽 개수, 가격, 달러 환율, 수수료)
- `compute_pnl()` → `PnLBreakdown`
  - 업비트 손익(원) + 바이비트 손익(USDT × 청산 때 환율) + 펀딩비(원 환산) − 비용 = 순손익(원)
  - 수익률 분모: 바이비트 숏 금액 × 진입 때 환율
- `estimated_exit_premium()`: "김프가 대략 몇 % 가 되면 목표 수익인지" 추정 (로그용)

### `kimp/market_data.py` — 거래소 시세 (ccxt)
- `MarketData`: 거래소 연결과 마켓 정보를 처음에 한 번만 준비한다.
  - `filter_supported()`: 양쪽에 다 있는 코인만 남긴다.
  - `fetch_quotes(coins, fx_krw)`: 업비트는 모든 코인과 USDT 호가를 요청 1번에, 바이비트는 코인마다 받아서 `Quote` 를 만든다. 업비트 USDT 가격이 없어도 진행한다. (표시용이라서)
  - `round_qty()` / `min_qty()`: 바이비트 주문 단위로 내림, 최소 주문 개수
  - `fetch_funding()`: 펀딩비율, 다음 정산 시각, 기준 가격
- API 키가 필요 없는 공개 시세만 쓴다.

### `kimp/paper_broker.py` — 가짜 체결과 잔고
- `open_hedge()`: 바이비트 숏 + 같은 금액만큼 업비트 매수. 양쪽 다 되는지(호가, 잔고, 밀림)를 먼저 계산하고, 둘 다 될 때만 한 번에 저장한다. 안 되면 `EntryRejected` 를 내고 아무것도 바꾸지 않는다.
- `value_exit()`: "지금 청산하면 얼마?" (아무것도 안 바꿈)
- `close_hedge()`: 그 계산대로 양쪽 청산하고 손익을 저장한다.
- `apply_funding()`: 펀딩비 1번 반영 (숏이라 비율이 + 면 받음)
- 잔고 이름: `upbit:KRW`, `upbit:<코인>`, `bybit:USDT`
- 전송비는 실제 잔고에서는 안 빠지고 손익 계산에만 들어간다.

### `kimp/engine.py` — 전략 엔진
- `step()`: 스캔 한 번 (위 흐름 참고)
- `_manage_position()`: 순손익률 ≥ 목표면 익절, ≤ 손절선이면 손절. 목표와 손절은 **진입 때 포지션에 저장한 값**을 쓴다(`_exit_rules`). 청산 후 재진입 대기 시작
- `_split_text()`: 청산 로그에 "김프 변화 = 테더 몫 + 코인 몫" 을 보여준다
- `_update_funding()`: 지난 정산을 딱 1번만 반영 (이미 반영한 정산 시각과 비교)
- `_try_entries()` / `_plan_bybit_qty()`: 바이비트 숏 개수 = 포지션 규모 ÷ 바이비트 가격 → 주문 단위로 내림. 업비트 최소 주문 금액(5,000원) 확인
- `_log_status()`: 달러 환율, 업비트 USDT 가격, 테더 프리미엄, 코인별 "지금 김프/기준" 또는 "보유 순손익/목표"
- 루프 하나에서 모든 코인을 차례로 처리한다. (old 봇의 스레드 꼬임 문제 없음)

### `kimp/db.py` — DB 테이블 (SQLite)
| 테이블 | 내용 |
|---|---|
| `positions` | 포지션 1개 = 진입~청산 1사이클. 양쪽 개수, 진입·청산 가격, 진입·청산 환율, 수수료, 김프(테더 몫·코인 몫 포함), **진입 때 정한 기준(진입 기준·목표·손절)**, 펀딩비(USDT·원), 확정 손익(원) |
| `fills` | 체결 한 건씩 (평균가, 수수료, 밀린 정도) |
| `funding_events` | 펀딩비 받거나 낸 기록 |
| `premium_snapshots` | 봇이 돌면서 코인마다 1분에 1번 저장하는 김프 기록 (김프, 테더 몫, 코인 몫, 달러 환율, 업비트 USDT 가격, 바이비트 펀딩비율·다음 정산 시각·정산 주기) |
| `paper_balances` | 페이퍼 잔고 |

- 시간은 모두 UTC 로 저장한다.
- 테이블 구조가 바뀌면 예전 DB 파일과 맞지 않는다. 아직 운영 데이터가 없으니 DB 파일을 지우고 새로 시작하면 된다.

### `kimp/report.py` — 김프 기록 분석 리포트
- 실행: `python -m kimp.report` (최근 7일), `--hours 24`, `--db 경로`
- 코인마다 보여주는 것:
  - 김프 평균, 최저, 최고와 테더 몫·코인 몫
  - 진입 김프 분포(하위 5% / 25% / 중간값)
  - 호가 간격 비용
  - 펀딩비율 평균과 마이너스 비율
  - 기준별로 "진입 김프가 기준 이하였던 시간 비율"과 그때의 펀딩비율
  - **김프 구간별 평균 펀딩비율**
- 계산 부분(`summarize_coin`)과 출력 부분(`format_summary`)이 나뉘어 있어서 계산 부분을 테스트할 수 있다.

### `kimp/logging_setup.py` — 로그
- 화면 + `logs/bot.log` (10MB × 5개 돌려쓰기)

### `tests/` — 테스트 (인터넷 필요 없음)
| 파일 | 확인하는 것 |
|---|---|
| `test_calc.py` | 김프 공식, 테더 프리미엄, 체결 계산, **concept.md 예시 A~H 숫자와 코드 계산이 같은지** |
| `test_engine.py` | 왕복 후 잔고 = 계산 손익, 테더 상승 = 원화 이득, 실패 시 아무것도 안 바뀜, 진입/익절/손절/재진입 대기, 펀딩비 1번만, 환율 없음/오래됨 처리, 코인별 설정 |
| `test_fx.py` | 고정 환율, 갱신 간격, 실패 시 이전 값, 오래된 값 표시 |
| `test_config.py` | 코인별 덮어쓰기, 오타 경고, 잘못된 값 거부 |
| `test_report.py` | 리포트 숫자 계산 (분위수, 기준별 시간 비율, 김프 구간별 펀딩비율) |

실행: `python -m unittest discover -s tests -v`

## 3. 단위 규칙
- 수수료율: 소수 (`0.0005` = 0.05%)
- 퍼센트: 숫자 그대로 (`1.0` = 1%)
- 금액 변수 이름 끝에 단위를 붙인다: `_krw`(원), `_usdt`
- 개수: 금액 헷징이라 양쪽 개수가 다르다. 포지션에 `upbit_qty`, `bybit_qty` 를 따로 저장한다.

# 데이터 파일과 DB 구조

> 어떤 파일에 무엇이 들어 있고, 각 테이블·컬럼이 무슨 뜻인지 정리한 문서.
> **지워도 되는 것과 지우면 안 되는 것**을 먼저 확인할 것 (아래 0번).

모든 DB 는 **SQLite 파일 한 개**다. 서버를 따로 띄우지 않는다. 테이블은 프로그램이 시작할 때 없으면 만들고,
예전 DB 에 새 컬럼이 생기면 자동으로 덧붙인다 (`kimp/db.py` 의 `add_missing_columns`).

---

## 0. 파일 한눈에

| 파일 | 무엇이 들어 있나 | 누가 쓰나 | 지워도 되나 |
|---|---|---|---|
| `data/kimp.db` | **페이퍼 매매 기록** + 김프 기록(호가 포함) | `main.py` (페이퍼) | ❌ 매매 이력. 처음부터 다시 할 때만 |
| `data/dryrun.db` | 드라이런 매매 기록 | `main.py` (`PAPER_TRADING=false`, `LIVE_DRY_RUN=true`) | ❌ 같은 이유 |
| `data/live.db` | **실거래 매매 기록** | `main.py` (실주문) | ❌ **절대** |
| `data/market.db` | **시세 기록** (김프·호가·펀딩비). `SNAPSHOT_DB_PATH` | `python -m kimp.collect` (수집기) | ⚠️ 지우면 실측 호가가 사라져 시뮬레이션이 추측값으로 돌아간다 (다시 모으는 데 시간이 든다) |
| `data/history.db` | 과거 캔들 **캐시** + **시뮬레이션 결과** | 스크리너 · 시뮬레이션 · 로컬 화면 | ⚠️ 캔들만 지워도 됨 (다시 받음). `sim_*` 는 되살릴 수 없음 |
| `data/screen_latest.json` | 스크리너 마지막 결과 (화면이 읽음) | `kimp.screen` → `kimp.webui` | ✅ 다시 돌리면 생김 |
| `data/STOP` | 이 파일이 있으면 신규 진입 중단 | 사람이 만들고 지움 | ✅ |
| `logs/` | 실행 로그 (`bot.log`, 자정마다 `bot.log.YYYY-MM-DD` 로 넘기고 90일 보관) | `main.py` | ✅ |

- `data/` 와 `logs/` 는 `.gitignore` 에 있어서 git 에 올라가지 않는다.
- **모드마다 DB 가 따로다.** 페이퍼로 연습한 기록이 실거래 손익에 섞이지 않는다 (`Settings.active_db_path`).
- 세 모드 파일의 **테이블 구조는 완전히 같다.** 아래 1번 설명이 세 파일에 그대로 적용된다.

### 지금 크기 확인
```bash
ls -la data/                     # 맥 / Git Bash
python -m kimp.webui             # 화면 '설정' 탭에 파일 크기가 나온다
```

---

### 시세 기록은 왜 따로 두나
`premium_snapshots`(김프·호가 기록)는 **전략과 무관한 시장 데이터**다. 그래서 수집 전용 명령
(`python -m kimp.collect`)이 `data/market.db` 에 쌓고, 매매 기록(`kimp.db`·`live.db`)과 섞지 않는다.
봇도 돌면서 자기 DB 에 같은 기록을 남기므로, 읽을 때는 **`market.db` → 지금 모드 DB → 페이퍼 DB** 순서로 찾는다
(`Settings.snapshot_db_paths()`). 모드를 바꿔도 전에 모아둔 시세 기록은 그대로 쓸 수 있다.

## 1. 매매 기록 DB (`kimp.db` / `dryrun.db` / `live.db`)

| 테이블 | 한 줄 설명 | 언제 쌓이나 |
|---|---|---|
| `positions` | 헷지 포지션 하나 = 진입 한 번 | 진입할 때 1줄, 청산하며 채워짐 |
| `fills` | 체결 한 건 (주문 한 동작) | 포지션당 보통 6줄 (진입 3 + 청산 3) |
| `funding_events` | 펀딩비 정산 한 번 | 보유 중 8시간마다 (코인에 따라 1·4시간) |
| `premium_snapshots` | 김프 기록 (호가 포함) | **코인마다 1분에 1줄** — 가장 빨리 쌓인다 |
| `transfers` | 거래소 간 USDT 송금 한 건 | 리밸런싱할 때만 |
| `paper_balances` | 페이퍼 가짜 잔고 | 페이퍼·시뮬레이션만 (실거래는 거래소가 진짜) |
| `carry_positions` / `carry_funding` | 펀딩비 수익 전략 (따로 도는 전략) | `CARRY_ENABLED=true` 일 때 |
| `runs` | 봇을 켠 기록 (모드·코인·설정·git 커밋) | 켤 때 1줄, 끌 때 채움 |
| `decisions` | **진입 판단** — 기준 근처에 왔을 때 어느 단계에서 걸렸나 | 기준 +0.3%p 안에 들어온 스캔마다 |
| `position_snapshots` | **보유 중 평가** — 순손익·청산 김프·펀딩·강제청산 거리 | 포지션마다 1분에 1줄 |
| `engine_snapshots` | 양쪽 자본과 **스캔 소요 시간** | 1분에 1줄 |

### 1-1. `positions` — 포지션 하나
업비트에서 USDT 를 팔아 코인을 사고, 바이비트에 같은 금액 숏을 친 **한 세트**. 손익은 모두 USDT.

**기본**

| 컬럼 | 뜻 |
|---|---|
| `id` | 번호. 로그에 `#12` 처럼 나온다 |
| `coin` | 코인 (예: `CAP`) |
| `status` | `OPENING` → `OPEN` → `CLOSING` → `CLOSED` / `ABORTED`(되돌림) / `ERROR`(사람이 정리해야 함) |
| `is_paper` | 페이퍼 여부 |
| `opened_at` / `closed_at` | 진입·청산 시각 (UTC) |

**진입할 때 (실제 체결값)**

| 컬럼 | 뜻 | 예 |
|---|---|---|
| `usdt_sold` | 업비트에서 판 USDT 개수 (= 숏 금액) | 50.0 |
| `entry_usdt_price_krw` | USDT 판 평균가 (원) | 1,332.5 |
| `entry_usdt_fee_krw` | 그 매도 수수료 (원) | 33 |
| `upbit_qty` | 산 코인 개수 | 728.4 |
| `entry_upbit_price_krw` | 코인 평균 매수가 (원) | 91.4 |
| `entry_upbit_fee_krw` | 코인 매수 수수료 (원) | 33 |
| `bybit_qty` | 숏 개수 | 730 |
| `entry_bybit_price_usdt` | 평균 숏 가격 | 0.0685 |
| `entry_bybit_fee_usdt` | 숏 수수료 | 0.0275 |
| `entry_premium` | **진입 김프 (코인 몫, %)** — 실제 체결가 기준 | −0.55 |
| `entry_tether_premium` · `entry_site_premium` · `entry_fx_krw` | 참고용 (손익 계산에는 안 씀) | +0.84 / +0.29 / 1,321 |

**레버리지·증거금**

| 컬럼 | 뜻 |
|---|---|
| `leverage` | 그때 쓴 레버리지 (`BYBIT_LEVERAGE`) |
| `bybit_margin_usdt` | 이 포지션에 묶인 증거금 = 숏 금액 ÷ 레버리지 |
| `liquidation_price` | 숏 강제청산 가격. 1배면 진입가의 약 2배 |

**진입할 때 고정한 기준** (들고 있는 중 `.env` 를 바꿔도 이 값으로 청산한다)

| 컬럼 | 뜻 |
|---|---|
| `entry_threshold_percent` | 그때 쓴 진입 기준 |
| `target_net_profit_percent` | 목표 순이익 (기본 1%) |
| `stop_loss_percent` | 손절 (기본 −5%) |

**들고 있는 동안 갱신되는 값**

| 컬럼 | 뜻 |
|---|---|
| `funding_usdt` | 펀딩비 합계 (+ 받음 / − 냄) |
| `next_funding_ms` · `pending_funding_rate` | 다음 정산 시각과 예고 비율 |
| `last_net_percent` | 마지막으로 계산한 평가 순손익률 (%) |
| `last_exit_premium` | 마지막으로 계산한 청산 김프 |
| `last_liq_distance_percent` | 강제청산까지 남은 상승폭 (%). 15% 미만이면 긴급 경고 |

**분석용** (매매 판단에는 안 쓴다, `kimp/telemetry.py`)

| 컬럼 | 뜻 |
|---|---|
| `planned_entry_premium` | **판단 때 김프** (호가 소진 계산). `entry_premium − planned_entry_premium` = 주문 세 개 사이에 밀린 폭 |
| `max_net_percent` · `max_net_at` | 보유 중 순손익 **최고**와 그 시각 — "목표에 얼마나 가까웠다가 놓쳤나" |
| `min_net_percent` · `min_net_at` | 보유 중 순손익 **최저**와 그 시각 — 손절선에 얼마나 다가갔나 |
| `run_id` | 어느 실행(`runs.id`)에서 잡았나 → 그때 설정·코드 버전 |
| `margin_added_usdt` | 강제청산 가드가 더 넣은 증거금 누적 (USDT). `bybit_margin_usdt` 에 이미 포함되어 있다 |

**청산할 때 / 확정 손익**

| 컬럼 | 뜻 |
|---|---|
| `exit_upbit_price_krw` · `exit_upbit_fee_krw` | 코인 판 평균가·수수료 |
| `exit_usdt_price_krw` · `exit_usdt_fee_krw` · `usdt_bought` | USDT 되산 평균가·수수료·**되산 개수** |
| `exit_bybit_price_usdt` · `exit_bybit_fee_usdt` | 숏 닫은 평균가·수수료 |
| `exit_premium` | 청산 김프 (코인 몫, %) |
| `exit_reason` | `TAKE_PROFIT`(목표 달성) / `STOP_LOSS`(손절) / `LIQUIDATION`(강제청산) / `LIQ_GUARD`(강제청산 회피 — 청산가 90% 지점에서 목표도 멀고 손실도 작아 미리 정리) / `TIMEOUT`(시간 초과) / `MANUAL`(수동) / `FUNDING_COST`(펀딩비 한도) |
| `upbit_leg_usdt` | 업비트 몫 손익 = `usdt_bought − usdt_sold` |
| `bybit_leg_usdt` | 바이비트 몫 손익 = 숏 개수 × (진입가 − 청산가) |
| `gross_pnl_usdt` | 비용 빼기 전 손익 |
| `total_cost_usdt` | 수수료 + 전송비 합 |
| `net_pnl_usdt` | **최종 순손익 (USDT)** |
| `net_pnl_percent` | 순손익률 = 순손익 ÷ 숏 진입 금액 |

### 1-2. `fills` — 체결 한 건
주문을 내기 **전에** `SENT` 로 먼저 적는다. 응답이 끊기거나 프로그램이 꺼져도 추적번호로 거래소에 조회해서
결과를 채울 수 있다 (레그 리스크 대비, `docs/live.md` 2번).

| 컬럼 | 뜻 |
|---|---|
| `position_id` | 어느 포지션의 체결인지 |
| `exchange` | `upbit` / `bybit` |
| `side` | `sell_usdt`(USDT 매도) · `buy`(코인 매수) · `sell`(코인 매도) · `buy_usdt`(USDT 매수) · `open_short` · `close_short` · `liquidated` · `trim_*`(크기 맞추기) · `unwind_*`(되돌리기) |
| `qty` · `avg_price` · `cost` | 개수 · 평균가 · 금액 (업비트는 원, 바이비트는 USDT) |
| `fee` · `fee_currency` | 수수료와 통화 (`KRW` / `USDT`) |
| `slippage_percent` | 맨 앞 호가보다 얼마나 밀렸나 (%) |
| `order_id` | 거래소 주문 번호 (페이퍼는 `paper-…`) |
| `client_id` | **우리가 붙인 추적번호** (바이비트 `orderLinkId`, 업비트 `identifier`) |
| `state` | `SENT`(결과 모름) / `DONE`(체결 확인) / `VOID`(주문 안 들어감) |
| `created_at` · `done_at` | 주문을 **보낸** 시각 · 체결을 **확인한** 시각 (차이 = 주문 한 동작 소요) |
| `expected_price` | 판단 때 호가 소진 계산으로 **예상한 평균가**. `avg_price` 와 비교하면 체결 계산이 얼마나 맞는지 나온다 |

```sql
-- 예상 체결가 vs 실제 (양수 = 예상보다 불리). 진입 세 단계 사이 소요 시간도 같이
SELECT position_id, side, expected_price, avg_price,
       ROUND((avg_price / expected_price - 1) * 100, 4) AS 차이_pct,
       ROUND((julianday(done_at) - julianday(created_at)) * 86400, 2) AS 소요_초
FROM fills WHERE expected_price IS NOT NULL AND state = 'DONE' ORDER BY id;
```

### 1-3. `premium_snapshots` — 김프·호가 기록 (가장 중요한 분석 자료)
**주로 수집기(`python -m kimp.collect`)가 `data/market.db` 에 쌓는다.** 봇도 돌면서 자기 DB 에 남긴다.
코인마다 `--interval`(기본 60초)에 1줄. **과거 호가창은 거래소가 주지 않으므로, 이 기록이 유일한 호가 간격 실측값이다.**
스크리너·시뮬레이션이 30줄 이상 쌓이면 자동으로 이 값을 쓴다 (`kimp/spread.py`).

| 컬럼 | 뜻 |
|---|---|
| `coin` · `created_at` | 코인 · 기록 시각 (UTC) |
| `mid_premium` | **봇 김프 (코인 몫)** — 가운데 가격 기준. "보통 시세" |
| `entry_premium_top` | 맨 앞 호가로 **지금 진입하면** 나오는 김프 |
| `exit_premium_top` | 맨 앞 호가로 **지금 청산하면** 나오는 김프 |
| `usdt_spread_percent` | 업비트 USDT/KRW 호가 간격 (%) |
| `usdt_krw` | 업비트 USDT 가운데 가격 (원) |
| `upbit_mid_krw` · `bybit_mid_usdt` | 각 거래소 코인 가운데 가격 |
| `fx_krw` · `tether_premium` · `site_premium` | 달러 환율 · 테더 프리미엄 · 사이트식 김프 (표시용) |
| `funding_rate` · `next_funding_ms` · `funding_interval_hours` | 다음 펀딩비율 · 정산 시각 · 정산 주기(1·4·8시간) |

```sql
-- 코인 호가 간격 실측 평균 (스크리너·시뮬레이션이 쓰는 식)
SELECT coin, COUNT(*),
       AVG(entry_premium_top - exit_premium_top - COALESCE(usdt_spread_percent, 0)) AS 호가간격
FROM premium_snapshots GROUP BY coin;
```

### 1-4. `funding_events` — 펀딩비 정산

| 컬럼 | 뜻 |
|---|---|
| `position_id` | 어느 포지션 |
| `rate` | 펀딩비율 (소수. `0.0001` = 0.01%) |
| `mark_price` | 정산 때 가격 |
| `amount_usdt` | 실제 금액 (+ 받음 / − 냄) |
| `funding_time_ms` | 정산 시각 (UTC ms) |

### 1-5. `transfers` — 거래소 간 송금

| 컬럼 | 뜻 |
|---|---|
| `direction` | `upbit->bybit` 또는 `bybit->upbit` |
| `amount_usdt` · `fee_usdt` | 보낸 양 · 출금 수수료 (받는 양 ≈ 보낸 양 − 수수료) |
| `network` | 체인 (트론이면 업비트 `TRX` / 바이비트 `TRC20`) |
| `status` | `REQUESTED`(출금 요청) → `CONFIRMED`(입금 확인) / `FAILED` |
| `withdraw_id` · `reason` · `note` | 거래소 출금 번호 · 사유(시작/청산 후/진입 막힘) · 메모 |
| `requested_at` · `confirmed_at` · `alerted` | 요청·확인 시각 · 늦어서 긴급 알림을 보냈는지 |

### 1-6. `paper_balances` — 페이퍼 가짜 잔고
`asset` 이름은 `<거래소>:<자산>` 꼴이다.

| asset | 뜻 |
|---|---|
| `upbit:USDT` | 업비트 USDT |
| `upbit:KRW` | 매매 중 생기는 자투리 원화 (거의 0) |
| `upbit:<코인>` | 업비트에 들고 있는 코인 개수 |
| `bybit:USDT` | 바이비트 남은 USDT (**증거금 제외**) |
| `carry:USDT` · `carry:<코인>` | 펀딩비 전략 잔고 (김프 전략과 따로) |

### 1-8. `decisions` — 진입 판단 기록 (왜 안 들어갔나)
맨 앞 호가 진입 김프가 **기준 + 0.3%p 안**에 들어온 스캔마다 1줄. 기준을 못 넘긴 스캔은 안 남는다 (표가 커지기만 한다).

| 컬럼 | 뜻 |
|---|---|
| `coin` · `created_at` | 코인 · 판단 시각 |
| `stage` | 어디서 걸렸나 (아래) |
| `premium_top` · `threshold_percent` | 맨 앞 호가 진입 김프 · 그때 기준 |
| `premium_depth` | 수량만큼 호가를 소진한 평균가 김프 (계산한 단계부터) |
| `size_usdt` · `bybit_qty` · `funding_8h_percent` | 그때 정한 크기 · 숏 개수 · 펀딩비(8h) |
| `position_id` · `note` | 진입했으면 포지션 번호 · 거절 사유 |

| `stage` | 뜻 |
|---|---|
| `near` | 기준 근처까지 왔지만 못 넘음 — **아깝게 놓친 순간** |
| `spread_block` | 김프는 됐는데 **지금 왕복 호가**가 `ENTRY_MAX_SPREAD_PERCENT` 를 넘어 보류 (`premium_depth` = 왕복 호가 %). 가격이 호가 단위 경계를 넘은 코인 |
| `funding_block` | 김프는 됐는데 펀딩비가 불리해서 보류 |
| `size_reject` | 주문 크기가 거래소 최소 미만 |
| `depth_reject` | 수량만큼 호가가 없음 |
| `depth_fail` | 맨 앞 호가는 통과, 수량 소진 평균가로는 못 넘음 — **호가가 얇아서 밀린 경우** |
| `no_slot` | 조건은 됐지만 자리(최대 포지션 수·한 번만 돌리기)가 없어 다른 코인에 밀림 |
| `balance_block` | 한쪽 USDT 부족 |
| `rejected` | 주문 단계 거절·되돌림 (`note`) |
| `confirm_fail` | 조건은 됐는데 **주문 직전 확인 시세**에서는 기준을 못 넘음 — 한순간 튄 값 (`premium_depth` = 확인 시세 김프) |
| `entered` | 진입함 |
| `tp_confirm_fail` | 익절 조건이 됐는데 확인 시세에서는 목표 미달 (`premium_top` = 처음 순손익, `premium_depth` = 확인 순손익, `position_id`) |

```sql
-- 코인별로 어디서 막히는지
SELECT coin, stage, COUNT(*) FROM decisions GROUP BY coin, stage ORDER BY coin, 3 DESC;
-- 호가 밀림 때문에 못 들어간 평균 폭 (맨 앞 호가 → 수량 소진)
SELECT coin, ROUND(AVG(premium_depth - premium_top), 3) FROM decisions WHERE stage = 'depth_fail' GROUP BY coin;
```

### 1-9. `position_snapshots` — 보유 중 평가 (목표에 얼마나 가까웠나)
들고 있는 포지션마다 `SNAPSHOT_INTERVAL_SEC`(기본 60초)에 1줄. "지금 청산하면" 을 그대로 적는다.

| 컬럼 | 뜻 |
|---|---|
| `position_id` · `created_at` | 포지션 · 시각 |
| `net_percent` · `net_usdt` | 지금 청산하면 순손익 |
| `exit_premium` | 지금 청산 김프 (수량 소진 평균가) |
| `mid_premium` · `upbit_mid_krw` · `bybit_mid_usdt` · `usdt_krw` | 그때 시세 |
| `funding_usdt` · `liq_distance_percent` | 그때까지 펀딩비 · 강제청산까지 거리 |

```sql
-- 포지션별: 최고점이 목표에 얼마나 못 미쳤나 (TIMEOUT 으로 끝난 것들 분석)
SELECT p.id, p.coin, p.exit_reason, p.target_net_profit_percent AS 목표,
       ROUND(p.max_net_percent, 3) AS 최고, p.max_net_at, ROUND(p.net_pnl_percent, 3) AS 결과
FROM positions p WHERE p.status = 'CLOSED' ORDER BY p.id;
```

### 1-10. `engine_snapshots` — 자본 곡선과 스캔 소요
1분에 1줄. `upbit_usdt + bybit_usdt` 가 **포지션과 무관한 총자본 곡선**이다 (손익 기록과 대조할 때 쓴다).
`scan_ms` 가 평소(1~3초)보다 길게 이어지는 구간 = 거래소 응답이 느렸거나 통신 장애.
`quote_skew_ms` = 업비트 호가와 바이비트 호가를 받은 시각 차이(코인 중 최대). 동시에 받아서 평소 수십 ms 이고,
이게 크면 그 스캔의 김프에 '시차' 가 섞였을 수 있다.

### 1-11. `runs` — 실행 기록
켤 때 `mode` · `coins` · `git_commit` · `settings_json`(비밀값 뺀 설정 전부) 을 적고, 끌 때 `ended_at` · `stop_reason`
(`one_shot` / `stopped`) 을 채운다. 포지션의 `run_id` 로 "그 거래 때 설정이 뭐였나" 를 찾는다.

### 1-12. `events` — 문제 기록 (WARNING 이상 로그 전부)
텔레그램으로 가는 것과 같은 기준(WARNING 이상)으로 DB 에도 남긴다. `logger.exception` 으로 찍힌 예외는
**traceback 전체**가 `exc_text` 에 들어간다. 로그 파일을 열지 않고도 "언제·어디서·몇 번" 을 센다.

| 컬럼 | 뜻 |
|---|---|
| `level` · `logger` | `WARNING` / `ERROR` / `CRITICAL` · 어느 모듈 (`kimp.broker`, `kimp.engine` …) |
| `message` | 로그 한 줄 (1,000자까지) |
| `exc_text` | 예외였으면 traceback |
| `run_id` · `created_at` | 어느 실행 · 시각 |

```sql
-- 종류별 횟수 (메시지 앞 30자로 묶기)
SELECT level, SUBSTR(message, 1, 30) AS 종류, COUNT(*) FROM events GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 30;
-- 예외만
SELECT created_at, logger, message, exc_text FROM events WHERE exc_text IS NOT NULL ORDER BY id DESC;
-- 포지션 번호가 들어간 메시지로 그 포지션의 사건 모으기
SELECT created_at, level, message FROM events WHERE message LIKE '%#12 %' ORDER BY id;
```

### 1-7. `carry_positions` / `carry_funding` — 펀딩비 수익 전략
바이비트 현물 매수 + 같은 개수 숏 → 가격 변동은 상쇄되고 펀딩비(+)를 숏이 받는다 (`docs/carry.md`).
주요 컬럼: `qty`(현물=숏 개수), `entry_spot_price` / `entry_perp_price`, `entry_basis_percent`(선물가 ÷ 현물가 − 1),
`entry_daily_funding_percent`(진입 때 하루 평균 펀딩), `margin_usdt`, `liquidation_price`,
`funding_usdt` / `funding_count`, `spot_leg_usdt` / `perp_leg_usdt`, `net_pnl_usdt` / `net_pnl_percent`.

---

## 2. 과거 데이터 DB (`history.db`)

**두 가지가 섞여 있다.** 캔들은 캐시(다시 받을 수 있음)이고, 시뮬레이션 결과는 기록(되살릴 수 없음)이다.

| 테이블 | 한 줄 설명 | 성격 |
|---|---|---|
| `candles` | 과거 봉 (분·5분·15분) | 캐시 — 지워도 다시 받음 |
| `funding_history` | 바이비트 펀딩비 정산 이력 | 캐시 (받기가 느려서 두는 게 좋다) |
| `fx_hourly` | 달러 환율 1시간봉 | 캐시 |
| `fetched_ranges` | "이 구간은 받아봤다" 기록 | 캐시와 짝 — 같이 지워야 함 |
| `sim_runs` | 시뮬레이션 실행 한 번 | **기록** |
| `sim_trades` | 시뮬레이션 안의 거래 한 건 | **기록** |

### 2-1. `candles`

| 컬럼 | 뜻 |
|---|---|
| `source` | 어디 봉인지. `upbit`(업비트 코인/KRW) · `bybit`(무기한) · `upbit_usdt`(USDT/KRW). **1분봉은 이름 그대로, 다른 간격은 `upbit@15m` 처럼 붙는다** |
| `coin` | 코인 (`upbit_usdt` 는 `USDT`) |
| `ts_ms` | 봉 시작 시각 (UTC ms) |
| `open` · `high` · `low` · `close` · `volume` | 시가·고가·저가·종가·거래량 |

- 1분봉 이름을 간격 없이 쓰는 이유: **스크리너 2단계와 시뮬레이션이 같은 캔들을 공유**하려고.
- 강제청산 판정은 **고가(`high`)** 로 한다 (종가만 보면 잠깐 치솟은 순간을 놓친다).
- 크기 감각: 캔들 1개 ≈ 94바이트. **코인 1개 30일 1분봉(업비트+바이비트) ≈ 7.7MB.**

### 2-2. `fetched_ranges` (왜 필요한가)
업비트는 **거래가 없던 분에는 캔들을 안 준다.** 그래서 "비어 있으면 다시 받기" 로 만들면 영원히 다시 받는다.
받아본 구간을 적어 두고 그 구간은 건너뛴다.

| 컬럼 | 뜻 |
|---|---|
| `kind` | `candle:upbit` · `candle:bybit` · `candle:upbit@15m` · `fx` · `funding` |
| `coin` · `start_ms` · `end_ms` | 코인 · 받아본 구간 |

### 2-3. `sim_runs` / `sim_trades` — 시뮬레이션 결과
> 이 두 테이블은 화면 **대시보드 탭**에서 봇 기록(`positions`)과 **같은 모양으로** 볼 수 있다.
> 실거래를 아직 안 했을 때도 성과 화면을 그대로 써 보려는 것이다 (`kimp/dashboard.py`).


| `sim_runs` 컬럼 | 뜻 |
|---|---|
| `id` | 실행 번호 (로컬 화면 '시뮬레이션' 탭에 나오는 번호) |
| `created_at` | 돌린 시각 (UTC ISO) |
| `from_ms` · `to_ms` | 시뮬레이션 기간 |
| `coins` | 쉼표로 이은 코인 목록 |
| `settings_json` | **그때 쓴 설정 전부** (진입 기준·목표·비용·레버리지·호가 간격과 그 출처) |
| `summary_json` | 결과 요약: `total_net_usdt` · `realized_usdt`(확정) · `unrealized_usdt`(평가) · `return_percent` · `capital_start_usdt` · `closed_trades` · `open_positions` · `minutes_simulated` |

| `sim_trades` 컬럼 | 뜻 |
|---|---|
| `run_id` | 어느 실행 |
| `coin` · `opened_at` · `closed_at` | 코인 · 진입 · 청산 (끝까지 들고 있었으면 `closed_at` 이 비어 있음) |
| `entry_premium` · `exit_premium` | 진입·청산 김프 (%) |
| `exit_reason` | `TAKE_PROFIT` / `STOP_LOSS` / `LIQUIDATION` / `OPEN`(끝까지 보유) |
| `net_pnl_usdt` · `net_pnl_percent` | 순손익과 손익률 |
| `funding_usdt` · `cost_usdt` | 펀딩비 · 비용 |

`--no-save` 로 돌리면 **저장하지 않는다** (화면 목록에도 안 나온다).

---

## 3. 자주 하는 조회

```bash
# 쌓인 김프 기록 요약 (봇이 돌고 있어야 생긴다)
python -m kimp.report

# 브라우저로 보기 (설정 탭에 DB 크기, 페이퍼 봇 탭에 코인별 기록 건수)
python -m kimp.webui
```

**아래 SQL 을 직접 치지 않아도 되는 것들**은 화면 **대시보드 탭**에 이미 있다 (`python -m kimp.webui`).
기록(`live.db` / `dryrun.db` / `kimp.db` 또는 시뮬레이션 결과)과 기간을 고르면 `positions` · `transfers` ·
`sim_trades` 를 읽어 순손익·승률·누적 곡선·코인별·거래 이력으로 정리해 준다. 집계 코드는 `kimp/dashboard.py`.

```sql
-- 닫은 거래 손익 (페이퍼)
SELECT coin, COUNT(*) AS 건수, ROUND(SUM(net_pnl_usdt), 2) AS 손익,
       ROUND(AVG(net_pnl_percent), 2) AS 평균률
FROM positions WHERE status = 'CLOSED' GROUP BY coin;

-- 지금 들고 있는 포지션
SELECT id, coin, status, entry_premium, last_net_percent, last_liq_distance_percent FROM positions
WHERE status IN ('OPENING', 'OPEN', 'CLOSING', 'ERROR');

-- 간격별 캔들 크기 (history.db)
SELECT source, COUNT(*) AS 개수, COUNT(DISTINCT coin) AS 코인수 FROM candles GROUP BY source ORDER BY 2 DESC;
```

---

## 4. 유지보수

### 커지는 건 캔들뿐이다
매매 기록 쪽은 다 합쳐도 1MB 미만이다. `premium_snapshots` 가 코인 6개로 한 달 돌면 약 26만 줄(≈30MB)로
가장 빨리 늘지만, 호가 간격 실측값이라 **지우면 시뮬레이션 정확도가 떨어진다.**

| 동작 | `history.db` 증가량 |
|---|---|
| 스크리너 1회 (80코인 15분봉, 첫 실행) | 약 40MB |
| 스크리너 2단계 (상위 20개 1분봉) | 약 150MB |
| 시뮬레이션 (코인 4개 30일) | 약 31MB |
| 차트에서 코인 하나 24시간 보기 | 약 0.3MB |

같은 구간은 다시 받지 않으므로, 매일 돌리면 하루치(코인당 약 0.26MB)만 늘어난다.
폭증하는 건 **새 코인·새 간격을 처음 받을 때**뿐이다.

### 미리 받아두기
```bash
python -m kimp.history fetch --coins CAP,SEI --days 30              # 1분봉 30일치
python -m kimp.history fetch --coins CAP --days 7 --timeframe 15m
```
시뮬레이션·차트는 필요한 구간을 그때그때 받는다. 미리 받아두면 그다음부터 바로 그려진다.
로컬 화면 '데이터' 탭에서도 할 수 있다.

### 정리 명령 (직접 SQL 쓰지 말 것)
```bash
python -m kimp.history info                      # 간격별 개수·크기·기간
python -m kimp.history prune --source @5m        # 미리보기
python -m kimp.history prune --source @5m --yes  # 5분봉 전부 지우고 VACUUM
python -m kimp.history prune --days 60 --yes     # 60일보다 오래된 캔들
```
이 명령은 **캔들과 '받아본 구간' 기록을 함께** 손본다. SQL 로 캔들만 지우면 `fetched_ranges` 에
"이미 받아봤다" 가 남아서 **다시 받지 않는다.** (구간이 걸쳐 있으면 시작 시각만 잘라낸다)
시뮬레이션 결과는 건드리지 않는다.
- `sim_runs` / `sim_trades` 는 남겨 둘 것 (**다시 만들 수 없다**).
- `data/history.db` 를 **파일째 지우면 시뮬레이션 결과도 사라진다.**
- `kimp.db` / `dryrun.db` / `live.db` 는 매매 이력이다. 페이퍼를 처음부터 다시 하고 싶을 때만 `kimp.db` 를 지운다.

### 백업
`data/*.db` 를 복사하면 끝이다 (봇을 끈 상태에서). 그중 꼭 챙길 것은 **`live.db`(실거래 이력)** 과
**`kimp.db` 의 `premium_snapshots`(호가 실측)** 이다. 캔들은 다시 받을 수 있다.

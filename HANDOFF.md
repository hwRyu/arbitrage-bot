# HANDOFF — 다음 작업자(Claude)용 인수인계

> 2026-10-22 기준. PC 를 옮겨서 다른 Claude 가 바로 이어서 작업하기 위한 문서.
> **먼저 이 문서 → `docs/concept.md` → `docs/live.md` 순서로 읽을 것.** 전략의 기준은 항상 `docs/concept.md`.

## 0. 새 PC 에서 바로 시작하기
**지금 쓰는 PC: 윈도우 11, Python 3.14.2 (`python`·`python3`·`py` 다 됨). 가상환경 `.venv/` 를 쓴다.**
```powershell
git clone https://github.com/hwRyu/arbitrage-bot.git; cd arbitrage-bot
python -m venv .venv                      # 처음 한 번
.\.venv\Scripts\Activate.ps1              # 터미널 열 때마다 (Git Bash: source .venv/Scripts/activate)
pip install -r requirements.txt
python -m unittest discover -s tests      # 134개 통과해야 정상 (인터넷 필요 없음)
```
- 터미널별 접속 명령·PowerShell 에서 설정 넣는 법(`$env:`)은 `docs/commands.md` 0번과 5번.
- **윈도우 venv 에는 `python3.exe` 가 없다.** 켜고도 `python3` 를 치면 전역 파이썬이 돌아가니 `python` 으로 쓸 것.
- 맥에서는 `python3 -m venv .venv` → `source .venv/bin/activate`.
- **`.env` 는 git 에 없다** (`.gitignore`). 키가 들어 있으니 사용자가 직접 안전하게 옮기거나, `cp .env.example .env` 로 새로 만들고 키를 다시 넣어야 한다. 절대 커밋하지 말 것.
- **`data/` 도 git 에 없다.** `data/history.db`(과거 캔들·펀딩비 캐시, 약 256MB)는 없어도 되고, 시뮬레이션·스크리너를 돌리면 모자란 구간만 다시 받는다 (처음엔 몇 분). `data/kimp.db` 등 봇 DB 는 아직 운영 기록이 없어서 옮길 필요 없음.
- 실거래 점검: `python -m kimp.live_check` (읽기만 함). 명령 전체는 `docs/commands.md`.

## 1. 사용자 / 일하는 방식 (꼭 지킬 것)
- 사용자는 한국어로 대화한다. 답도 한국어로, 쉬운 말로. 반말 섞인 짧은 질문이 많다 ("~냐?", "~해").
- **코드 주석은 아주 쉬운 한국어 + 숫자 예시.** 문서에는 규칙마다 숫자로 된 예시(진입 → 청산 표 등).
- **전략 개념을 바꾸기 전에 구체적으로 되물어 확인한다.** 예) "업비트 XRP/KRW 에서 사고 바이비트 XRP 무기한 숏, 맞나요?" — 과거에 'USDT 기준' 을 잘못 해석해서 크게 되돌린 적이 있다.
- **페이퍼·드라이런·실거래·시뮬레이션은 같은 로직이어야 한다** (사용자 원칙). 매매 로직은 전부 `kimp/broker.py`(공통 브로커)에 넣고, 실행기(`kimp/executor.py`, `kimp/live_executor.py`)는 주문 한 동작씩만. 설정 '값' 은 모드별로 달라도 되지만 '로직' 은 같아야 한다.
- 구조를 바꿀 때는 **바꾸기 전 커밋과 시뮬레이션 결과를 줄 단위로 비교**해서 같음을 확인한다 (아래 6번 방법).
- **git: 사용자가 커밋하라고 할 때만, `main` 에 바로 커밋하고 `git push origin main`.** 브랜치·PR 만들지 말 것. 커밋 메시지 끝에 `Co-Authored-By` 줄.
- 숫자로 솔직하게 보고한다 (수익이 안 나면 안 난다고). 사용자는 "왜?" 를 자주 묻는다 → 원인을 숫자 예시로.
- zsh 주의: `env $VAR` 처럼 변수 하나에 여러 설정을 넣으면 쪼개지지 않는다 → 설정은 명령 앞에 직접 나열.

## 2. 프로젝트 한 줄
업비트 원화 마켓과 바이비트 USDT 무기한 사이의 **김프 헷지 봇** (+ 따로 도는 **바이비트 펀딩비 수익 전략**, 페이퍼만).

### 김프 헷지 전략 (docs/concept.md 가 기준)
- 자본: 업비트·바이비트 **둘 다 USDT** 로 들고 시작 (사용자가 테더 역프 때 USDT 를 사 둠).
- 진입: 바이비트 숏 → 업비트 USDT/KRW 매도 → 받은 원화로 코인 매수 (**금액 헷징**: USDT 판 개수 = 숏 금액).
- 청산: 코인 매도 → 원화로 USDT 매수 → 숏 닫기. 손익은 **USDT 기준**.
- 김프 = 업비트 코인 원화가 ÷ (바이비트 가격 × 업비트 USDT 원화가) − 1 → **코인 몫 김프** (테더 프리미엄·환율 제외. 달러 환율은 표시용).
- 진입: 실제 체결가로 계산한 진입 김프 ≤ 기준 (코인별 덮어쓰기 `<COIN>_ENTRY_PREMIUM_THRESHOLD_PERCENT`).
- 청산: 비용(수수료·펀딩비) 뺀 순이익 ≥ 목표(기본 1%), 손절 −5%. 목표·손절은 진입 때 값으로 고정.
- 레버리지 `BYBIT_LEVERAGE`(기본 1) → 자본 비율 자동 (업비트 = L/(L+1+여유 30%), 1배면 43.5:56.5).
- **1배 숏도 강제청산 있음** (가격 약 2배에서). 경고 `LIQ_WARN_PERCENT` 15%.
- 리밸런싱(송금)은 **필요할 때만**: 청산 후 비율 10%p 넘게 깨짐 / 진입하려는데 한쪽 부족. 거래마다 송금하지 않는다 (사용자가 강하게 지적했던 부분). 송금 한 번 수수료 `REBALANCE_TRANSFER_FEE_USDT` 1.

### 펀딩비 전략 (docs/carry.md)
바이비트 현물 + 같은 개수 숏, 펀딩비(+) 수취. 자본 `CARRY_CAPITAL_USDT` 따로. 2026-09~10 시뮬레이션은 본전 근처 (펀딩이 낮은 시기). 페이퍼로만 `main.py` 와 같이 돈다.

## 3. 코드 지도
| 파일 | 역할 |
|---|---|
| `main.py` | 시작점. 모드별 실행기 선택(`make_broker`), 텔레그램, 펀딩비 스레드 |
| `kimp/config.py` | 모든 설정 (`.env`) + 검사. `Settings.mode` = paper / dry_run / live |
| `kimp/engine.py` | 김프 엔진: 스캔 → `broker.sync` → 포지션 관리 → 리밸런싱 → 진입 (모드 모름) |
| `kimp/broker.py` | **공통 브로커 `HedgeBroker`**: 진입 3단계, 크기 맞추기, 되돌리기, 청산 남은 양 이어서, 추적번호 복구, 대조, 송금 |
| `kimp/executor.py` | 페이퍼 실행기 `PaperExecutor` (호가로 체결 계산, 장부 `paper_balances`) |
| `kimp/live_executor.py` | 실거래 `LiveExecutor`(ccxt 주문·조회·취소·출금) / 드라이런 `DryRunExecutor` |
| `kimp/safety.py` | 신규 진입 막기 (STOP 파일, /stop, 연속 실패, ERROR, 대조 불일치, 하루 손실 한도) |
| `kimp/notify.py` | 텔레그램 알림(로그 → 텔레그램) + `/status` `/stop` `/resume` |
| `kimp/live_check.py` | 실거래 전 점검 (읽기만) |
| `kimp/premium.py` / `kimp/pnl.py` | 김프·체결 계산 / 손익 |
| `kimp/simulate.py` | 과거 1분봉 시뮬레이션 (실시간과 같은 엔진·브로커) |
| `kimp/screen.py` | 김프 전략 추천 코인 순위 (`--end` 로 과거 시점 기준) |
| `kimp/carry.py`, `kimp/carry_backtest.py` | 펀딩비 전략 / 순위·시뮬레이션 |
| `kimp/history.py` | 과거 데이터 캐시 (`data/history.db`) |
| `kimp/db.py` | 테이블. 예전 DB 에 새 칸 자동 추가(`add_missing_columns`) |
| `tests/` | `test_live_broker.py`(가짜 ccxt 거래소로 실거래 경로), `test_engine.py`, `test_calc.py` 등 |

자세한 구조: `docs/architecture.md`. 실거래 흐름·안전장치·레그 리스크 표: `docs/live.md`.

## 4. 지금 상태
- 최근 커밋: `081fccf` (송금 네트워크 거래소별), `d290c17` (실거래 구조 + 레그 리스크). 작업 트리 깨끗.
- 실거래 코드는 다 있지만 **실제 키로 주문해 본 적은 없다.** 가짜 ccxt 거래소 테스트로만 검증.
- 사용자 `.env`: 페이퍼 모드, 키 4개 입력됨, 텔레그램 없음, 코인 기본값(XRP,SOL,DOGE).
- `live_check` 마지막 결과 (사용자 맥, 모바일 연결):
  - ❌ **업비트 IP**: 요청마다 IP 가 바뀜 (118.235.25.205 → .243 → .64 → .196). 등록이 소용없음 → 고정 IP 필요
  - ✅ 바이비트 키 동작, 통합계좌, 선물 주문 권한. ⚠️ IP 제한 없음(`*`, 사용자가 IP 오류 때문에 풀어 둠), 출금 권한 없음, 잔고 0 USDT
  - ✅ 바이비트 USDT 입금 네트워크에 TRC20 있음, 입금 주소 조회됨
  - 업비트 쪽(네트워크 목록, 입금 주소, 출금 허용 주소, 최소 주문)은 IP 해결 후 확인 필요
- 사용자가 실행 환경(서버)을 아직 못 정함.

## 5. 다음 할 일 (우선순위)
1. **고정 IP 확보**: 집 와이파이(같은 IP 유지되는지 `curl -s https://api.ipify.org` 여러 번)나 클라우드 서버(서울 리전 권장, `docs/deploy.md`). 그 IP 를 업비트·바이비트 키에 등록, 바이비트 IP 제한 다시 걸기
2. `live_check` 다시 → 업비트 USDT 네트워크 목록에 `TRX` 있는지, 최소 주문 금액 조회, 출금 허용 주소
3. 텔레그램 봇 토큰·chat_id 설정
4. 코인 설정: 사용자에게 제안해 둔 값 (최근 30일 스크리너)
   ```
   COINS=CAP,ERA,MEW,MEGA
   CAP_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.20
   ERA_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.05
   MEW_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.10
   MEGA_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.15
   ```
   (적용 여부는 사용자에게 물어볼 것. 매달 `python -m kimp.screen` 으로 다시 고르는 게 원칙)
5. **드라이런** (`PAPER_TRADING=false`) 하루 이상 → 로그·텔레그램 확인
6. 바이비트에 USDT 입금 → **소액 실거래** (`LIVE_DRY_RUN=false`, `LIVE_CONFIRM=YES`, `MAX_POSITION_USDT=20~50`, `MAX_OPEN_POSITIONS=1`) → 체결 내역을 거래소 앱과 비교
7. 자동 송금은 소액 수동 송금으로 트래블룰·네트워크 확인 뒤에만 `AUTO_TRANSFER_ENABLED=true` (출금 허용 주소 등록, 바이비트 출금 권한 필요)

### 실제 키로만 확인 가능한 것 (아직 미확인)
- 바이비트 `orderLinkId` 주문 조회(`private_get_v5_order_realtime`/`history`), 업비트 `identifier` 조회(`private_get_order`), 업비트 `orders/chance`(최소 주문), `status/wallet`(네트워크)
- 바이비트 격리 마진·레버리지 설정 응답, 계좌 이동(unified → funding)·출금, 업비트 출금 `net_type`
- 업비트 ↔ 바이비트 트래블룰 (2차 자료상 '계정주 확인 연동', 공식 확인 필요)

### 남은 과제 (docs/concept.md 11번)
- 펀딩비를 전략에 더 반영 (지금은 진입 기준에 안 씀). 실거래 펀딩비는 예고 비율로 추정 기록 중 (실제 정산 내역으로 바꾸면 좋음)
- 펀딩비 전략: 교차 마진(현물 담보) 계산, 변동성 큰 코인 빼기, 6개월 이상 시뮬레이션
- **운영 중 코인 재선정 프로세스 (사용자와 논의 먼저)**: 봇을 계속 돌리는 입장에서 `COINS` 와 코인별 진입 기준을
  언제·누가·어떤 기준으로 바꿀지 정해져 있지 않다. 재선정 주기, 사람 승인 여부, 코인을 넣고 빼는 기준,
  들고 있는 포지션 처리, 과최적화 방지, 교체 기록을 어디 남길지 → concept.md 11번 3순위에 질문 목록 정리됨

## 6. 검증 방법 (구조를 바꿀 때)
```bash
SP=/tmp/handoff_check && rm -rf $SP && mkdir -p $SP && git worktree add -q $SP/old HEAD
ROOT=$PWD
for d in $SP/old $ROOT; do (cd $d && PAPER_INITIAL_TOTAL_USDT=250 POSITION_SIZE_USDT=50 \
  CAP_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.20 ERA_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.05 \
  MEW_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.10 MEGA_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.15 COINS=CAP,ERA,MEW,MEGA \
  HISTORY_DB_PATH=$ROOT/data/history.db python -m kimp.simulate --from 2026-09-22 --to 2026-10-22 \
  --coins CAP,ERA,MEW,MEGA --no-save --verbose 2>&1 | tr '\r' '\n' \
  | grep -E "^■ 총|^  확정|^  평가|^  리밸런싱|^  (CAP|ERA|MEW|MEGA) +2026" > $SP/$(basename $d).txt); done
git worktree remove --force $SP/old; diff $SP/old.txt $SP/$(basename $ROOT).txt && echo 같음
```
이 시나리오의 현재 결과: **+12.25 USDT (+4.9%), 닫은 거래 26건 모두 이익, 송금 2번**.

## 7. 지금까지 확인한 숫자 (판단 근거)
- 주요 코인(XRP·SOL·DOGE)은 코인 몫 김프가 거의 안 움직여 한 달에 거래 0~2건 → 기회는 거래량 작은 코인에 있다.
- **앞 30일로 고르고 다음 30일 시뮬레이션** (시드 250, 포지션 50, 목표 1%):
  8/22~9/22 +9.70 USDT (+3.9%), 9/22~10/22 +9.48 USDT (+3.8%), 비교 주요 코인 ±0.2
- 호가 간격을 0.3%/0.5% 로 넓혀도 +2.6% / +1.7% (9/22~10/22).
- LSK 같은 '이상김프'(입출금 막힘 의심, ±5% 넘음) 코인은 손절만 반복 → 스크리너가 맨 아래로 보냄.
- 펀딩비 전략: 이번 달 BTC·ETH 하루 0.01% 수준, 1배 자본 대비 월 0.3% 정도가 한계. 진입 기준 0.07% 가 0.03% 보다 나았지만 표본이 작음.

## 8. 과거에 고친 함정 (같은 실수 방지)
- 바이비트 펀딩비 기록은 **200시간 칸으로 나눠** 받아야 한다 (그냥 '언제부터 200개' 면 1·4시간 정산 코인 중간이 빔). `kimp/history.py` `FUNDING_WINDOW_MS`.
- 업비트 1분봉은 점검 구간에서 끊긴다 → 200분 건너뛰기. 환율은 주말 대비 7일 앞부터.
- SQLite 시각은 시간대 없이 읽힌다 → UTC 로 붙여 쓰기 (9시간 밀림 사고).
- 호가 한 칸이 큰 코인(DOGE)은 종가 튐 때문에 가짜 수익 → 호가 단위 하한, 스크리너는 진입·청산마다 호가 간격 한 번씩 비용.
- 업비트 최소 주문 금액은 ccxt 가 안 준다 → `orders/chance` 조회, 못 받으면 `UPBIT_MIN_ORDER_KRW`. 바이비트는 마켓 정보 `minNotionalValue`.
- 같은 체인도 거래소마다 이름이 다르다: 트론 = 업비트 `TRX` / 바이비트 `TRC20`.
- 부분 체결 뒤 **남은 원화를 USDT 로 되돌리지 않으면** 손익 기록과 실제 잔고가 어긋난다 (`_return_leftover_krw`).
- ERROR 상태 포지션은 봇이 청산하지 않는다 → 크기 맞추기 실패 같은 경우는 OPEN 으로 두고 알림.

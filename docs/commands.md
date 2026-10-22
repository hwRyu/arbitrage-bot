# 명령어와 실행 방법

> **아래 모든 명령은 가상환경(venv)을 켠 상태로 실행한다.** 켜는 법은 0-2 번.
> 가상환경을 켜면 윈도우·맥 모두 `python` 으로 쓸 수 있다.
> (맥에서 가상환경을 안 켜면 `python` 이 없고 `python3` 만 있다 → 명령마다 `python3` 로 바꿔 쓸 것)

## 0-1. 처음 한 번 (PC 마다 한 번씩)
가상환경 = 이 프로젝트만 쓰는 파이썬 패키지 폴더(`.venv/`). 전역에 깔면 다른 프로젝트가 패키지 버전을
바꿀 때 봇이 조용히 깨진다. `.venv/` 는 `.gitignore` 에 있어서 git 에 올라가지 않는다.

**윈도우 (PowerShell)**
```powershell
cd C:\Users\<사용자>\Desktop\Repositories\arbitrage-bot
python -m venv .venv                      # .venv\ 폴더 만들기 (한 번만)
.\.venv\Scripts\Activate.ps1              # 가상환경 켜기
pip install -r requirements.txt           # 패키지 설치 (ccxt, yfinance 등)
Copy-Item .env.example .env               # 코인, 기준값, 자본, 수수료를 여기서 고친다
python -m unittest discover -s tests      # 테스트 134개 (인터넷 필요 없음)
```
- `Activate.ps1` 이 "스크립트 실행이 차단되었습니다" 로 막히면 한 번만:
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

**맥 / 리눅스 (zsh, bash)**
```bash
cd ~/Desktop/Individual/Repository/arbitrage-bot
python3 -m venv .venv                     # 한 번만
source .venv/bin/activate                 # 가상환경 켜기
pip install -r requirements.txt
cp .env.example .env
python -m unittest discover -s tests
```

## 0-2. 그다음부터 (터미널 열 때마다)
가상환경은 **터미널을 새로 열면 꺼져 있다.** 매번 켜야 한다.

| 터미널 | 켜기 | 끄기 |
|---|---|---|
| 윈도우 PowerShell | `.\.venv\Scripts\Activate.ps1` | `deactivate` |
| 윈도우 cmd | `.venv\Scripts\activate.bat` | `deactivate` |
| 윈도우 Git Bash | `source .venv/Scripts/activate` | `deactivate` |
| 맥 / 리눅스 | `source .venv/bin/activate` | `deactivate` |

- 켜졌으면 프롬프트 앞에 `(.venv)` 가 붙는다. 확인: `python -c "import ccxt; print(ccxt.__version__)"` → 버전이 나오면 정상
- `ModuleNotFoundError: No module named 'ccxt'` 가 나오면 **가상환경을 안 켠 것**이다 (또는 0-1 설치를 안 한 것).
- 켜지 않고 경로를 직접 적어도 된다 (스크립트나 작업 스케줄러에서 쓰기 좋다):
  - 윈도우: `.\.venv\Scripts\python.exe -m kimp.simulate --from 2026-09-22`
  - 맥: `./.venv/bin/python -m kimp.simulate --from 2026-09-22`
- 지금 깔린 버전을 그대로 보존하려면: `pip freeze > requirements.lock`
  (서버로 옮겨 똑같이 재현할 때 `pip install -r requirements.lock`)

### 윈도우에서 한글이 깨질 때
로그의 한글이 `���` 처럼 보이면 출력 인코딩 때문이다. 터미널에서 한 번 설정하면 그 터미널에서 계속 적용된다.
```powershell
$env:PYTHONUTF8 = "1"
python main.py
```

## 1. 한눈에
| 하고 싶은 것 | 명령 | 걸리는 시간 |
|---|---|---|
| 봇 실행 (김프 + 펀딩비, 페이퍼) | `python main.py` | Ctrl+C 로 끔 |
| 실거래 전 점검 (읽기만) | `python -m kimp.live_check` | 몇 초 |
| 드라이런 / 실거래 | `PAPER_TRADING=false python main.py` ([live.md](live.md)) | |
| 김프 전략 코인 추천 순위 | `python -m kimp.screen` | 처음 2~3분, 다음부터 수십 초 |
| 김프 전략 과거 시뮬레이션 | `python -m kimp.simulate --from 2026-09-22` | 코인당 처음 1~2분 |
| 펀딩비 전략 코인 순위 | `python -m kimp.carry_backtest screen` | 처음 약 2분 |
| 펀딩비 전략 과거 시뮬레이션 | `python -m kimp.carry_backtest run --from 2026-09-22` | 처음 1~2분 |
| 봇이 쌓은 김프 기록 분석 | `python -m kimp.report` | 몇 초 |
| **브라우저로 보기 (설정·김프 차트·추천·시뮬 결과)** | `python -m kimp.webui` | 바로 |
| 과거 데이터 상태 보기 / 정리 | `python -m kimp.history info` / `prune` | 몇 초 |

> 가상환경을 켠 상태 기준이다 (0-2 번). 맥에서 안 켰으면 `python` → `python3`.
> 윈도우 venv 에는 `python3.exe` 가 없어서, 켜고도 `python3` 를 치면 **전역 파이썬**이 돌아간다 → 꼭 `python`.

날짜는 모두 **한국 시간**이다. `--to` 를 안 적으면 지금까지.
과거 데이터는 `data/history.db` 에 저장돼서 두 번째부터는 모자란 구간만 받는다.

## 2. 봇 실행 — `python main.py`
- 김프 헷지 엔진(3초마다)과 펀딩비 엔진(60초마다, `CARRY_ENABLED=true`)이 같이 돈다. **지금은 페이퍼(가짜 주문)만** 된다.
- 끄기: Ctrl+C. 열린 포지션과 잔고는 `data/kimp.db` 에 남아서 다시 켜면 이어서 관리한다.
- 로그: 화면 + `logs/` 폴더
- 처음부터 다시 하고 싶으면 봇을 끄고 `data/kimp.db` 를 지운다 (페이퍼 잔고·포지션 초기화).

## 3. 김프 전략 도구
### 코인 추천 순위 — `python -m kimp.screen`
| 옵션 | 기본 | 뜻 |
|---|---|---|
| `--days` | 30 | 최근 며칠을 볼지 |
| `--top` | 20 | 몇 위까지 |
| `--target` | `.env` | 목표 순이익 % (바꿔서 비교할 때) |
| `--coins` | 전체 | `XRP,SOL` 처럼 정한 코인끼리만 |
| `--refine-top` | 20 | **2단계**: 1단계 상위 몇 개를 짧은 봉으로 다시 평가할지 (`0` 이면 2단계 생략) |
| `--refine-timeframe` | `1m` | 2단계에서 쓸 캔들 간격 (시뮬레이션과 같은 기준) |
| `--cycle-hours` | 24 | 반복 주기가 이 시간 이하면 가중치 1 |
| `--size-usdt` | 자동 | 포지션 크기 (물량 확인용) |
| `--min-upbit-krw` / `--min-bybit-usdt` | 10억원 / 1M | 24시간 거래대금 최소 |
| `--max-coins` | 80 | 거래대금 상위 몇 개까지 |
| `--end` | 지금 | 이 날짜까지의 데이터로 고르기 (예: `--end 2026-09-22` 로 고르고 9/22~10/22 를 시뮬레이션해서 검증) |

끝에 `.env` 에 붙여넣을 `COINS=...` 와 코인별 진입 기준이 나온다. `이상김프` 표시 코인은 쓰지 말 것.

**2단계로 돈다** (기본값)
```
1단계  코인 80개를 15분봉으로 평가 → 상위 20개 추림
2단계  그 20개만 1분봉으로 다시 평가 (시뮬레이션과 같은 기준)
```
15분봉은 짧게 벌어졌다 메워진 기회를 놓쳐서, 15분봉 순위와 시뮬레이션 결과가 어긋난다
(CAP: 15분봉 9건 vs 1분봉 21건). 표의 `봉` 칸에 어느 봉으로 계산한 점수인지 찍힌다.
2단계에서 받은 1분봉은 시뮬레이션에서도 그대로 쓰인다 (`data/history.db` 공유).

**점수는 '자리당 효율'이다**
```
점수 = min(전체 월수익, 검증 월수익) ÷ 자리 점유율 × 띠 가중치
자리 점유율 = 포지션을 들고 있던 시간 ÷ 전체 기간 (하한 25%)
```
동시에 들 수 있는 포지션이 `MAX_OPEN_POSITIONS`(3)개뿐이라, 오래 들고 있는 코인은 다른 코인의 진입을 막는다.
예) 느린 코인 6개로 돌리면 +12.30 USDT, 빠른 코인 2개만 쓰면 +92.79 USDT (CAP 거래가 7건 → 80건)
추천 `COINS=` 목록도 자리 예산(자리 수 × 0.8) 안에서만 고르고, 자리가 모자라 빠진 코인을 알려준다.

**호가 간격은 이 순서로 정한다** (스크리너·시뮬레이션 공통, `kimp/spread.py`)
```
① 봇이 쌓은 기록(premium_snapshots) 평균 → ② 코인별 설정 <코인>_SIM_SPREAD_PERCENT
→ ③ 지금 호가창 3번 재서 중간값 → ④ 기본값 SIM_DEFAULT_SPREAD_PERCENT (0.1%)
```
표 위에 어느 값을 썼는지 찍힌다. 한 번만 재면 그 순간 호가에 순위가 휘둘린다 (같은 데이터로 14분 뒤
다시 돌렸더니 CAP 검증 월수익이 +13.94% → +4.90% 로 바뀜). ①이 가장 정확하니 아래 '호가 기록 쌓기' 를 먼저 하면 좋다.

### 호가 기록 쌓기 (시뮬레이션 정확도 올리기)
과거 호가창은 거래소가 주지 않는다. 그래서 **봇을 돌려서 직접 쌓는다.** 페이퍼 모드로 켜두면
코인마다 1분에 1건씩 `premium_snapshots` 에 기록되고, 그다음부터 스크리너·시뮬레이션이 자동으로 그 값을 쓴다
(설정을 바꿀 것은 없다).

```powershell
# 윈도우 PowerShell — 후보 코인을 다 넣는다 (기록은 포지션과 무관하게 코인별로 쌓인다)
$env:PYTHONUTF8="1"
$env:COINS="CAP,SEI,LIT,GRVT,MEW,WAVES"
$env:SCAN_INTERVAL_SEC="20"      # 기록은 1분에 1건이라 3초마다 볼 필요가 없다 (요청 제한 여유)
$env:CARRY_ENABLED="false"       # 펀딩비 전략까지 돌릴 필요 없음
.\.venv\Scripts\python.exe main.py
```
```bash
# 맥 / Git Bash
COINS=CAP,SEI,LIT,GRVT,MEW,WAVES SCAN_INTERVAL_SEC=20 CARRY_ENABLED=false python main.py
```
- 하루 돌리면 코인당 약 1,440건. 30건(30분)만 넘어도 '기록' 으로 인정된다.
- 쌓인 값 확인: `python -m kimp.report` (또는 `data/kimp.db` 의 `premium_snapshots`)
- 봇을 두 개 돌리면 업비트 요청 제한(IP 기준 초당 10회)을 넘을 수 있다 → 기록용은 `SCAN_INTERVAL_SEC` 를 늘릴 것.

### 과거 시뮬레이션 — `python -m kimp.simulate`
> **호가 간격 기록이 없으면 시뮬레이션이 멈춥니다.** 믿을 수 없는 숫자를 내놓지 않기 위해서입니다.
> 같은 기간을 돌렸는데 실행마다 CAP 호가가 0.125% / 0.266% 로 잡혀서 거래 115건 / 76건,
> 손익 +63 / +42 USDT 로 갈린 적이 있습니다. 먼저 아래 '호가 기록 쌓기' 를 하거나,
> 대충이라도 보려면 `--rough-spread` 를 붙이세요.
```bash
python -m kimp.simulate --from 2026-09-22                       # 9/22 0시부터 지금까지
python -m kimp.simulate --from 2026-09-22 --to 2026-10-15
python -m kimp.simulate --from 2026-09-22 --coins MEW,CAP --verbose   # 거래 하나하나 보기
python -m kimp.simulate --from 2026-09-22 --no-save             # 결과를 DB 에 안 남김
python -m kimp.simulate --from 2026-09-22 --rough-spread        # 호가 기록이 없어도 '지금 호가' 로 강행
```

### 김프 기록 분석 — `python -m kimp.report`
```bash
python -m kimp.report              # 최근 7일
python -m kimp.report --hours 24
```
봇을 돌리면서 쌓인 기록이 있어야 나온다.

### 브라우저로 보기 — `python -m kimp.webui`
```bash
python -m kimp.webui                 # http://127.0.0.1:8765 (브라우저가 자동으로 열림)
python -m kimp.webui --port 9000     # 포트 바꾸기
python -m kimp.webui --no-open       # 브라우저 자동 열기 끄기
```
표준 라이브러리로 만든 작은 로컬 서버다. 외부 프레임워크·CDN 을 쓰지 않아서 인터넷이 끊겨도 화면은 뜬다.
`127.0.0.1` 에만 열려서 다른 PC 에서는 접근할 수 없고, **API 키는 화면에 보내지 않는다** (있는지 여부만).

| 탭 | 내용 |
|---|---|
| 지금 김프 | 거래소 호가창을 그 자리에서 조회한 진입·청산 김프, 호가 간격, 펀딩비율, 테더 프리미엄. '10초마다 새로고침' 체크 가능 |
| 김프 차트 | 코인·기간을 고르고 **지표를 눌러서 켜고 끈다**. 진입 기준선이 점선으로 같이 나온다 |
| 추천 코인 | **화면에서 스크리너를 돌릴 수 있다** (기간·상위·2단계·코인 입력 → 돌리기). 결과는 표와 `.env` 붙여넣기 조각으로 |
| 시뮬레이션 | **화면에서 시뮬레이션을 돌릴 수 있다** (기간·코인·시드·포지션·동시 보유·목표·코인별 진입 기준·`--rough-spread`). 실행 목록에서 한 줄을 누르면 거래 내역이 펼쳐진다 |
| 페이퍼 봇 | **페이퍼 봇을 켜고 끈다** (코인·스캔 간격·텔레그램 끄기·펀딩비 전략). 잔고·열린 포지션·최근 거래와 **호가 기록이 코인별로 몇 건 쌓였는지**(30건 넘으면 실측 사용) 표시 |
| 설정 | 지금 `.env` 로 봇이 쓰는 값 (모드·레버리지·자본 비율·코인별 기준·수수료). 설정 경고도 여기 표시 |

**차트에서 켜고 끌 수 있는 지표** (봇 기록이 있을 때)
| 왼쪽 축 (%) | 오른쪽 축 (원, 점선) |
|---|---|
| 가운데 김프 · 진입 김프 · 청산 김프 · 코인 호가 간격 · USDT 호가 간격 · 펀딩비율(8시간 환산) · 테더 프리미엄 · 사이트식 김프 | USDT 가격 · 업비트 코인 가격 |

**화면에서 돌리기**
- 터미널에 치는 명령과 **똑같은 명령**을 자식 프로세스로 돌리고 출력만 받아서 아래 '실행 로그' 에 보여준다
  (서버 안에서 돌리면 화면이 멈추고 중간에 취소할 수 없다). '멈추기' 로 중단할 수 있다.
- 같은 종류는 한 번에 하나만 돌아간다. 스크리너가 끝나면 추천 표가, 시뮬레이션이 끝나면 실행 목록이 자동으로 새로 불린다.
- **화면에서 켜는 봇은 항상 페이퍼다** (`PAPER_TRADING=true` 를 강제). 실거래는 터미널에서만 켤 수 있다.
- 받을 수 있는 값은 정해진 목록(기간·코인·시드 등)뿐이라 아무 설정이나 넘어가지 않는다.

- 차트 데이터는 **봇 기록(`premium_snapshots`)이 있으면 그걸**, 없으면 과거 1분봉으로 계산한다 (화면에 출처가 표시된다).
- 봇을 돌리는 중에도 같이 띄워도 된다 (DB 를 읽기만 한다). 단 거래소 조회가 늘어나니 '10초마다 새로고침' 은 필요할 때만 켤 것.

### 과거 데이터 정리 — `python -m kimp.history`
받아둔 캔들은 `data/history.db` 에 계속 쌓인다 (캔들 1개 약 94바이트, **코인 1개 30일 1분봉이 약 7.7MB**).
한 번 써보고 안 쓰는 간격(예: 5분봉)이 쌓이면 이 명령으로 지운다.

```bash
python -m kimp.history info                      # 간격별 캔들 개수·크기·기간
python -m kimp.history prune --source @5m        # 무엇을 지울지 미리보기 (안 지움)
python -m kimp.history prune --source @5m --yes  # 5분봉 전부 지우고 파일 크기 줄이기
python -m kimp.history prune --days 60 --yes      # 60일보다 오래된 캔들
python -m kimp.history prune --coins DOOD,ALT --yes   # 특정 코인만
```
| 옵션 | 뜻 |
|---|---|
| `--days` | 이 일수보다 오래된 캔들만 (안 적으면 전부) |
| `--source` | `@5m`(5분봉 전부) · `upbit@15m` · `bybit` 처럼. 끝부분만 적어도 된다 |
| `--coins` | 코인 지정 |
| `--yes` | 실제로 지움 (없으면 미리보기만) |
| `--no-vacuum` | 지운 뒤 파일 크기 줄이기를 건너뜀 |

- **시뮬레이션 결과(`sim_runs`·`sim_trades`)는 지우지 않는다.** 캔들은 다시 받을 수 있지만 그 기록은 되살릴 수 없다.
- 캔들과 함께 '받아본 구간' 기록도 손봐서, 지운 구간은 다음에 다시 받는다.
- 매매 기록 DB(`kimp.db`·`live.db`)는 건드리지 않는다. 자세한 구조는 [data.md](data.md).

예) 5분봉을 지웠을 때: `지운 캔들 1,278,986개` → 파일 **307MB → 183MB**

## 4. 펀딩비 전략 도구 (자세한 설명: [carry.md](carry.md))
```bash
python -m kimp.carry_backtest screen                      # 최근 30일 코인 순위
python -m kimp.carry_backtest screen --days 60 --top 30
python -m kimp.carry_backtest run --from 2026-09-22       # .env 설정 그대로 과거 시뮬레이션
python -m kimp.carry_backtest run --from 2026-09-22 --coins COMP,SUI --verbose
```

## 5. 설정을 잠깐만 바꿔서 돌리기
`.env` 를 고치지 않고 그 실행에만 설정을 적용할 수 있다. **적는 방법이 터미널마다 다르다.**

**맥 / 리눅스 / Git Bash** — 명령 앞에 붙인다
```bash
# 시드 250 USDT, 포지션 50 USDT 고정, MEW·CAP 만, MEW 진입 기준 -0.10%
PAPER_INITIAL_TOTAL_USDT=250 POSITION_SIZE_USDT=50 MEW_ENTRY_PREMIUM_THRESHOLD_PERCENT=-0.10 \
  python -m kimp.simulate --from 2026-09-22 --coins MEW,CAP

# 펀딩비 전략 진입 기준 0.05%/일, 2배
CARRY_ENTRY_MIN_DAILY_PERCENT=0.05 CARRY_LEVERAGE=2 python -m kimp.carry_backtest run --from 2026-09-22
```

**윈도우 PowerShell** — 앞에 붙이는 방식이 없다. `$env:` 로 **먼저 설정**하고 그다음 줄에서 실행한다
```powershell
$env:PAPER_INITIAL_TOTAL_USDT = "250"
$env:POSITION_SIZE_USDT = "50"
$env:MEW_ENTRY_PREMIUM_THRESHOLD_PERCENT = "-0.10"
python -m kimp.simulate --from 2026-09-22 --coins MEW,CAP
```
- `$env:` 로 넣은 값은 **그 터미널이 닫힐 때까지 계속 남는다.** 다음 실행에 섞이지 않게 쓰고 나면 지운다:
  `Remove-Item Env:PAPER_INITIAL_TOTAL_USDT, Env:POSITION_SIZE_USDT, Env:MEW_ENTRY_PREMIUM_THRESHOLD_PERCENT`
- 매번 지우기 번거로우면 **Git Bash** 를 쓰는 게 편하다 (위 bash 방식 그대로 되고, 끝나면 설정도 안 남는다).
  예) `PAPER_TRADING=false ./.venv/Scripts/python.exe main.py`
- `PAPER_TRADING=false python main.py` 를 PowerShell 에 그대로 치면 **에러**가 난다 (bash 문법).

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
| `TRANSFER_FEE_UPBIT_TO_BYBIT_USDT` | 0 | 업비트 → 바이비트 송금 수수료 (업비트 USDT(TRX) 출금 무료) |
| `TRANSFER_FEE_BYBIT_TO_UPBIT_USDT` | 1.3 | 바이비트 → 업비트 송금 수수료 (바이비트 TRC20 출금) |
| `PAPER_AUTO_REBALANCE` | true | 페이퍼·시뮬레이션에서 필요할 때 자동 송금 흉내 |
| `CARRY_ENABLED` | true | 펀딩비 전략 같이 돌리기 |
| `CARRY_CAPITAL_USDT` | 1,000 | 펀딩비 전략 자본 (따로) |
| `CARRY_LEVERAGE` | 1 | 펀딩비 전략 숏 레버리지 |
| `CARRY_LIQ_GUARD_PERCENT` | 15 | 펀딩비 전략: 강제청산까지 이보다 가까우면 양쪽 미리 정리 |

## 7. 실거래 (자세히: [live.md](live.md))
**맥 / 리눅스 / Git Bash**
```bash
python -m kimp.live_check                                               # ❌ 없을 때까지
PAPER_TRADING=false python main.py                                      # 드라이런 (주문 안 냄)
PAPER_TRADING=false LIVE_DRY_RUN=false LIVE_CONFIRM=YES python main.py  # 실제 주문
touch data/STOP                                                         # 비상정지 (지우면 재개)
```

**윈도우 PowerShell**
```powershell
python -m kimp.live_check                     # ❌ 없을 때까지

$env:PAPER_TRADING = "false"                  # 드라이런 (주문 안 냄)
python main.py

$env:LIVE_DRY_RUN = "false"                   # 여기서부터 실제 주문이 나간다
$env:LIVE_CONFIRM = "YES"
python main.py

New-Item -ItemType File data\STOP             # 비상정지 (지우면 재개)
Remove-Item data\STOP                         # 재개
```
- 위 `$env:` 값은 그 터미널에 **계속 남는다.** 페이퍼로 돌아가려면 `$env:PAPER_TRADING = "true"` 로 바꾸거나 터미널을 새로 열 것.
  실수로 실거래가 켜진 채 `main.py` 를 또 켜는 사고를 막으려면, 실거래용 설정은 `$env:` 가 아니라 **`.env` 에 적어 두는 게 안전하다.**
- 비상정지는 파일만 만들면 되니 탐색기에서 `data` 폴더에 `STOP` 파일을 만들어도 같다.

텔레그램: `/status`, `/stop`, `/resume`

## 8. 송금(리밸런싱)은 누가 하나
- 페이퍼·시뮬레이션: 필요한 순간(청산 후 비율이 10%p 넘게 깨짐 / 진입하려는데 한쪽이 모자람) **사용자가 보냈다고 치고** 바로 옮기고 수수료 1번 (`PAPER_AUTO_REBALANCE`)
- 실거래: `AUTO_TRANSFER_ENABLED=true` 면 봇이 출금하고 도착을 확인한다. false(기본)면 알림만 하고 직접 보낸다 ([live.md](live.md) 4번)
- 펀딩비 전략은 바이비트 안에서 끝나서 송금이 필요 없다.

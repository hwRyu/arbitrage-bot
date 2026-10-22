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
| `--recent-days` | 0 (뒤 1/3) | **검증에 쓸 최근 기간(일)**. `--days 30 --recent-days 7` → 앞 23일로 기준 고르고 최근 7일로 검증. `--days` 를 바꿔도 검증 길이가 안 흔들린다 |
| `--target` | `.env` | 목표 순이익 % (바꿔서 비교할 때) |
| `--coins` | 전체 | `XRP,SOL` 처럼 정한 코인끼리만 |
| `--refine-top` | 20 | **2단계**: 1단계 상위 몇 개를 짧은 봉으로 다시 평가할지 (`0` 이면 2단계 생략) |
| `--refine-timeframe` | `1m` | 2단계에서 쓸 캔들 간격 (시뮬레이션과 같은 기준) |
| `--cycle-hours` | 24 | 반복 주기가 이 시간 이하면 가중치 1 |
| `--size-usdt` | 자동 | 포지션 크기 (물량 확인용) |
| `--min-upbit-krw` / `--min-bybit-usdt` | 10억원 / 1M | 24시간 거래대금 최소 |
| `--max-coins` | 80 | 거래대금 상위 몇 개까지 |
| `--json-path` | `data/screen_latest.json` | 결과를 저장할 JSON 경로 (로컬 화면이 이 파일을 읽는다) |
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
점수 = 기본 점수 ÷ 자리 점유율 × 띠 가중치
기본 점수 = min(전체 월수익, 검증 월수익)
           └ 단, 검증 구간에 완결된 거래가 아예 없으면 전체 월수익 × 1/4  (최근기회없음)
자리 점유율 = 포지션을 들고 있던 시간 ÷ 전체 기간 (하한 25%)
```
동시에 들 수 있는 포지션이 `MAX_OPEN_POSITIONS`(3)개뿐이라, 오래 들고 있는 코인은 다른 코인의 진입을 막는다.
예) 느린 코인 6개로 돌리면 +12.30 USDT, 빠른 코인 2개만 쓰면 +92.79 USDT (CAP 거래가 7건 → 80건)
추천 `COINS=` 목록도 자리 예산(자리 수 × 0.8) 안에서만 고르고, 자리가 모자라 빠진 코인을 알려준다.

**최근에 기회가 없으면 점수를 깎는다** (`최근기회없음`)
`검증/월` 은 **최근 구간**(기본 뒤 1/3, `--recent-days` 로 고정 가능) 성적이다. 여기서 완결된 거래가
하나도 없으면 "최근에는 목표를 낼 수 있는 진입·청산 지점이 없었다" 는 뜻이므로 전체 점수를 **1/4 로 깎는다.**
0 으로 만들지 않는 이유: 0 으로 뭉개면 최근 기회가 없던 코인이 전부 0 점이 되어 서로 구분이 안 된다
(월 23% 를 자리 7% 로 버는 코인과 월 4% 를 자리 58% 로 버는 코인이 같은 0 점). 벌점은 세고 순위는 남긴다.
검증 구간에 거래가 있었는데 **손실**이면 감점이 아니라 음수가 그대로 깔린다 (기회가 없던 것보다 나쁘다).

`--days` 를 짧게 주면 검증 구간도 같이 짧아져서(7일 → 2.3일) 거래 1~2건으로 0 이 되기 쉽다.
**`--days 30 --recent-days 7`** 처럼 쓰는 것이 안정적이다.

**호가 간격(틱)은 점수에 어떻게 들어가나**
가중치로 곱하지 않는다. 백테스트가 **매 거래 손익에서 직접 뺀다** — 진입할 때 호가만큼 불리하게 잡고,
청산할 때 또 한 번 뺀다. 그래서 호가가 넓은 코인은 수익 자체가 줄어 점수가 내려간다 (가중치를 또 곱하면
같은 비용을 두 번 세는 것이 된다). `호가` 열은 **실시간 호가와 호가 한 칸 중 큰 값**이고, 0.3% 를 넘으면
`호가넓음` 경고가 붙는다. 업비트 원화는 정해진 칸 단위로만 움직여서, 가격이 싼 코인은 호가 간격에
구조적 하한이 생긴다 (DOGE 106원·칸 1원 → 최소 0.94%, ALT 10.6원·칸 0.1원 → 0.94%).

**호가 간격을 어디서 가져올지 고를 수 있다** — `--spread-source` (화면에도 콤보로 있다)

| 모드 | 무엇을 쓰나 | 언제 |
|---|---|---|
| `auto` (기본) | 기록(30건↑) → 지금 호가 → 공통값 | 평소 |
| `records` | **실측 기록만**. 모자란 코인은 `기록 부족 (9/30건)` 으로 표시 → 시뮬레이션이 멈춘다 | 믿을 수 있는 숫자만 보고 싶을 때 |
| `common` | 모든 코인을 `SIM_DEFAULT_SPREAD_PERCENT` 로 **통일** | 호가 차이를 지우고 '김프 움직임' 만 코인끼리 비교할 때 (실제 수익 추정에는 쓰지 말 것) |
| `live` | 지금 호가창을 여러 번 재서 중간값 | 참고용 (순간값에 휘둘린다) |

**`<코인>_SIM_SPREAD_PERCENT` 를 적어 두면 그 코인은 모드와 무관하게 그 값으로 간다.**
표·화면에 `코인별 설정 (강제)` 로 표시된다. 전에는 기록이 30건을 넘으면 이 설정이 조용히 무시돼서
"호가가 더 나빠지면?" 을 시험할 수 없었다 — 사람이 적은 값이 먼저다.

호가 간격은 실거래·페이퍼와 무관하다. 그쪽은 항상 **그 순간 진짜 호가창 30단**을 주문 수량만큼
소진해서 가중 평균 체결가를 낸다 (`estimate_fill`). 이 설정은 **과거 호가창이 없는 백테스트 전용**이다.

**호가 간격은 이 순서로 정한다** (코인별 설정이 없을 때, `kimp/spread.py`)
```
① 봇이 쌓은 기록(premium_snapshots) 평균 → ② 코인별 설정 <코인>_SIM_SPREAD_PERCENT
→ ③ 지금 호가창 3번 재서 중간값 → ④ 기본값 SIM_DEFAULT_SPREAD_PERCENT (0.1%)
```
표 위에 어느 값을 썼는지 찍힌다. 한 번만 재면 그 순간 호가에 순위가 휘둘린다 (같은 데이터로 14분 뒤
다시 돌렸더니 CAP 검증 월수익이 +13.94% → +4.90% 로 바뀜). ①이 가장 정확하니 아래 '호가 기록 쌓기' 를 먼저 하면 좋다.

### 시세 수집 — `python -m kimp.collect`
**봇과 역할이 다르다.** 수집기는 김프·호가만 적는다 (주문·포지션·잔고 없음).

| 명령 | 역할 | 주문 | DB |
|---|---|---|---|
| `python main.py` | 전략 실행 (진입·청산·손익) | 모드에 따라 | `kimp.db` / `dryrun.db` / `live.db` |
| `python -m kimp.collect` | **시세 기록만** | **없음** | `data/market.db` (`SNAPSHOT_DB_PATH`) |

```bash
python -m kimp.collect                   # .env 의 COINS
python -m kimp.collect --coins CAP,SEI   # 코인 지정
python -m kimp.collect --top 40          # 업비트 거래대금 상위 40개
python -m kimp.collect --all             # 양쪽 거래소에 다 있는 코인 전부 (약 240개)
python -m kimp.collect --interval 30     # 기록 주기 (초, 기본 60)
python -m kimp.collect --once            # 한 번만 기록하고 종료 (확인용)
python -m kimp.collect --hours 24        # 24시간 돌고 종료
```

**왜 필요한가:** 과거 호가창은 거래소가 주지 않는다. "그 순간 호가로 진입/청산하면 김프가 얼마였나" 는
직접 적어 둬야 알 수 있다. 이 기록이 **30줄**을 넘으면 스크리너·시뮬레이션이 추측값(기본 0.1%) 대신
실측 호가 간격을 쓴다. 전략을 돌릴 필요가 없으니 수집은 따로 돌린다.
- 코인을 수백 개 넣어도 안전하다 (진입을 안 한다). 20개가 2초, 240개가 2초대.
- 봇과 같이 돌려도 된다 (DB 가 다르다). 업비트 요청 제한이 걱정되면 `--interval` 을 늘린다.
- 기록을 찾는 순서: `data/market.db` → 지금 모드 봇 DB → 페이퍼 DB (시장 데이터라 모드가 달라도 같이 쓴다)
- 쌓인 양 보기: `python -m kimp.webui` 의 **데이터 탭** (코인별 기록 줄 수와 '✔ 실측 사용' 표시), 또는 `python -m kimp.report`
- 로컬 화면 데이터 탭에서 **수집 시작** 버튼으로도 돌릴 수 있다.

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
| **대시보드** | **기간을 정해서 본 성과**. 기록(실거래·드라이런·페이퍼 DB 또는 시뮬레이션 결과)과 시작·종료 날짜를 고르면 요약 카드·누적 손익 곡선·일별 손익·코인별·사유별·거래 이력·송금 이력이 나온다 |
| 지금 김프 | 거래소 호가창을 그 자리에서 조회한 진입·청산 김프, 호가 간격, 펀딩비율, 테더 프리미엄. '10초마다 새로고침' 체크 가능 |
| 김프 차트 | 코인·기간을 고르고 **지표를 눌러서 켜고 끈다**. 진입 기준선이 점선으로 같이 나온다. **관리선(UCL/LCL)** 과 **목표 순이익으로 맞추기** 가 여기 있다 |
| 추천 코인 | **화면에서 스크리너를 돌릴 수 있다** (기간·검증일·상위·2단계·코인·**목표 순이익**·**손절** 입력 → 돌리기). 목표·손절은 `.env` 값이 기본으로 채워지고, 고치면 그 값으로 돈다 (`.env` 를 건드리지 않는다). 결과는 표와 `.env` 붙여넣기 조각으로. `검증/월` 이 최근 구간 성적이고, 최근에 기회가 없던 코인은 점수가 1/4 로 깎인다 (`최근기회없음`) |
| 시뮬레이션 | **화면에서 시뮬레이션을 돌릴 수 있다** (기간·코인·시드·포지션·동시 보유·목표·코인별 진입 기준·`--rough-spread`). 실행 목록에서 한 줄을 누르면 거래 내역이 펼쳐진다 |
| 데이터 | 과거 캔들 현황(간격별 개수·크기·기간)을 보고, **미리 받아오기**와 **정리하기**를 화면에서 한다 |
| 페이퍼 봇 | **페이퍼 봇을 켜고 끈다** (코인·스캔 간격·텔레그램 끄기·펀딩비 전략·**한 번만 돌리기**). 잔고·열린 포지션·최근 거래와 **호가 기록이 코인별로 몇 건 쌓였는지**(30건 넘으면 실측 사용) 표시 |
| 설정 | 지금 `.env` 로 봇이 쓰는 값 (모드·레버리지·자본 비율·코인별 기준·수수료). 설정 경고도 여기 표시 |

**대시보드 (성과 보기)**
- 위쪽에서 **기록**과 **시작·종료 날짜**를 고른다 (빠른 기간: 7일 · 30일 · 90일 · 전체).
  날짜를 비우면 그 기록 전체가 들어가는 기간으로 맞춰 준다.
- **기간은 청산 시각 기준**이다 (손익이 확정된 날에 잡는다). 진입만 하고 들고 있는 것은 '열려 있는 포지션' 에 따로 나온다.
- 실거래·페이퍼 기록이 비어 있으면 **가장 최근 시뮬레이션 결과**를 자동으로 띄운다. 같은 화면 모양으로 비교할 수 있다.
- 요약 카드: 순손익 / 거래 수 / 승률 / 평균 손익률 / 건당 평균 / 거래 규모 대비 / **김프 몫 · 펀딩비 · 비용** /
  평균 보유 / 최대 낙폭 / 월 환산. 카드 제목에 마우스를 올리면 공식이 나온다.
- 차트 두 개: **누적 순손익**(점 하나 = 거래 한 건, 올리면 코인·손익) · **일별 손익**(막대, 한국 시간 날짜).
- 집계는 `kimp/dashboard.py` 가 한다 (HTTP 와 분리 → 터미널·테스트에서도 쓸 수 있다).

**차트에서 켜고 끌 수 있는 지표** (봇 기록이 있을 때)
| 왼쪽 축 (%) | 오른쪽 축 (원, 점선) |
|---|---|
| 가운데 김프 · 진입 김프 · 청산 김프 · 코인 호가 간격 · USDT 호가 간격 · 펀딩비율(8시간 환산) · 테더 프리미엄 · 사이트식 김프 | USDT 가격 · 업비트 코인 가격 |

**관리선 (UCL / CL / LCL)**
- 켜 둔 % 지표 중 맨 위 것의 **평균(CL)과 평균 ± kσ** 를 가로선으로 그린다 (±1σ/±2σ/±3σ).
- **직접 입력** 을 고르면 UCL·LCL 을 손으로 적는다. 한쪽만 적어도 된다 (LCL 만 → 진입선 하나만).
  적은 값은 브라우저에 남아서 다시 열어도 그대로다. `±2σ 값 넣기` 로 자동값을 가져와 고칠 수 있다.
- 김프는 추세가 생기면 평균 자체가 움직인다 → 기간을 길게 잡으면 띠가 넓어져 쓸모가 없다 (6~24시간이 쓸 만하다).

**목표 순이익으로 관리선 맞추기** (`kimp/levels.py`)
"순이익 1% 를 남기려면 김프가 몇 %p 올라야 하나" 를 **그 코인의 실제 수치로** 역산해서 UCL/LCL 을 정해 준다.

| 나가는 돈 | 어디서 오나 |
|---|---|
| 왕복 거래 수수료 | 업비트 0.05%×4 + 바이비트 0.055%×2 = **0.31%** |
| 코인 호가 간격 | 그 코인 실측 (기록 30줄 이상) → 없으면 설정값 → 기본 0.1%. `kimp/spread.py` 와 같은 출처 |
| USDT 호가 간격 | 기록 중간값 (없으면 0.075%) |
| 전송비 | `TRANSFER_FEE_USDT_PER_CYCLE` ÷ 포지션 금액 — 금액이 작을수록 비중이 커진다 |
| 펀딩비 | 8시간 비율 × 예상 보유 시간 ÷ 8. **받는 쪽이면 마이너스**(필요 폭이 줄어든다) |
| 여유 | `SLIPPAGE_BUFFER_PERCENT` (기본 0.05%) |

필요한 폭 = 목표 + 비용 합계. 폭이 정해지면 **그 폭을 유지한 채 LCL 자리를 아래에서 위로 훑어서**
'LCL 에서 진입 → UCL 에서 청산' 이 그 기간에 가장 많이 **완결된** 자리를 고른다 (왕복 많은 순 →
평균 보유 짧은 순 → LCL 낮은 순). 거래가 일어나려면 차트가 두 선을 다 지나야 하므로 폭만이 아니라 자리도 중요하다.
펀딩비는 보유 시간에 걸리고 보유 시간은 관리선에 걸리므로 한 번 더 계산한다 (1차 평균 보유 시간으로 재계산).

결과에는 비용 항목별 수치와 출처, 기간 변동폭, 왕복 횟수, 월 환산 거래 수·수익, 다른 후보 자리가 같이 나온다.
**변동폭이 필요한 폭보다 좁으면 "이 기간엔 목표를 낼 수 있는 왕복이 없었다" 고 그대로 알려 준다** (숨기지 않는다).
이 계산은 차트에 있는 데이터만 쓴다 — 기간에 비해 점이 적으면 `지금 받아오기` 로 캔들을 먼저 받을 것.

**화면에서 돌리기**
- 터미널에 치는 명령과 **똑같은 명령**을 자식 프로세스로 돌리고 출력만 받아서 아래 '실행 로그' 에 보여준다
  (서버 안에서 돌리면 화면이 멈추고 중간에 취소할 수 없다). '멈추기' 로 중단할 수 있다.
- 같은 종류는 한 번에 하나만 돌아간다. 스크리너가 끝나면 추천 표가, 시뮬레이션이 끝나면 실행 목록이 자동으로 새로 불린다.
- **화면에서 켜는 봇은 항상 페이퍼다** (`PAPER_TRADING=true` 를 강제). 실거래는 터미널에서만 켤 수 있다.
- 받을 수 있는 값은 정해진 목록(기간·코인·시드 등)뿐이라 아무 설정이나 넘어가지 않는다.
- **목표 순이익·손절은 화면에서 바꿔 비교할 수 있다** (추천 코인·시뮬레이션 둘 다). 칸을 비우면 `.env` 값으로 돈다.
  목표가 순위를 가장 크게 바꾼다 — 실측 예) CAP 30일, 목표 1% → 거래 9건·평균 보유 57시간·점수 7.17,
  목표 0.5% → 거래 33건·평균 보유 10시간·점수 29.86. 목표를 낮추면 한 건당 덜 벌지만 훨씬 자주 돌아서
  자리당 효율이 올라간다. 목표·손절을 몇 개 값으로 돌려 보고 정하는 것이 맞다.

- **표 헤더에 마우스를 올리면 용어 설명이 뜬다** (뜻·공식·예시). '지금 김프' 탭 맨 위의 '용어 설명' 을 펼치면
  김프가 왜 세 개인지(가운데·진입·청산), 호가 간격과 필요한 상승폭이 정리돼 있다.
- 차트 데이터는 **봇 기록(`premium_snapshots`)이 있으면 그걸**, 없으면 과거 1분봉으로 계산한다 (화면에 출처가 표시된다).
- 봇을 돌리는 중에도 같이 띄워도 된다 (DB 를 읽기만 한다). 단 거래소 조회가 늘어나니 '10초마다 새로고침' 은 필요할 때만 켤 것.

### 과거 데이터 정리 — `python -m kimp.history`
받아둔 캔들은 `data/history.db` 에 계속 쌓인다 (캔들 1개 약 94바이트, **코인 1개 30일 1분봉이 약 7.7MB**).
한 번 써보고 안 쓰는 간격(예: 5분봉)이 쌓이면 이 명령으로 지운다.

```bash
python -m kimp.history info                                  # 간격별 캔들 개수·크기·기간
python -m kimp.history fetch --coins CAP,SEI --days 30        # 미리 받아두기 (시뮬레이션·차트가 빨라짐)
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

`fetch` 옵션: `--coins`(필수) · `--days`(기본 30) · `--timeframe`(`1m`/`5m`/`15m`, 기본 1분봉) · `--no-funding`

**이 둘은 로컬 화면 '데이터' 탭에서도 할 수 있다** (`python -m kimp.webui`): 간격별 현황을 보고,
코인·기간을 넣어 받아오거나, 간격·기간·코인을 골라 지운다. 지우기는 '정말 지웁니다' 를 체크해야 눌린다.

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

| 옵션 | 기본 | 뜻 | 어디에 |
|---|---|---|---|
| `--days` | 30 | 최근 며칠을 볼지 | screen |
| `--top` | 20 | 몇 위까지 | screen |
| `--coins` | 전체 | 정한 코인끼리만 | screen · run |
| `--min-perp-usdt` | 5,000,000 | 무기한(선물) 24시간 거래대금 최소 (USDT). 얇은 코인을 후보에서 뺀다 | screen · run |
| `--min-spot-usdt` | 500,000 | 현물 24시간 거래대금 최소 (USDT). 현물을 못 사면 전략이 안 된다 | screen · run |
| `--from` / `--to` | — / 지금 | 시뮬레이션 기간 (한국 시간). `--from` 은 필수 | run |
| `--verbose` | 꺼짐 | 진입·청산 로그 하나하나 보기 | run |

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
| `ENTRY_MIN_FUNDING_8H_PERCENT` | -0.05 | 8시간 기준 펀딩비율이 이보다 나쁘면 진입 안 함 (비우면 끔) |
| `MAX_FUNDING_COST_PERCENT` | 0.3 | 펀딩비로 낸 돈이 포지션 금액의 이 %를 넘으면 정리 (0 = 무제한). 펀딩비만큼 필요 청산 김프가 올라가서, 0.3%면 이미 도달이 어려워진다 |
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

**한 번만 돌려보기 (첫 실거래 점검, 기본은 꺼짐)** — 포지션 1개만 잡고 청산되면 스스로 종료하며 점검 요약을 찍는다
```powershell
$env:ONE_SHOT="true"; $env:ONE_SHOT_MAX_HOURS="6"   # 6시간 안에 목표에 못 닿으면 정리하고 끝
python main.py
```
```bash
ONE_SHOT=true ONE_SHOT_MAX_HOURS=6 python main.py   # 맥 / Git Bash
```
페이퍼에서도 쓸 수 있다 (한 바퀴만 보고 싶을 때). 화면 '페이퍼 봇' 탭에도 체크박스가 있다.

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

텔레그램: `/status` `/premium` `/close` `/stop` `/resume` `/collect` `/help` → 자세한 설명은 [live.md](live.md) 6번

## 8. 송금(리밸런싱)은 누가 하나
- 페이퍼·시뮬레이션: 필요한 순간(청산 후 비율이 10%p 넘게 깨짐 / 진입하려는데 한쪽이 모자람) **사용자가 보냈다고 치고** 바로 옮기고 수수료 1번 (`PAPER_AUTO_REBALANCE`)
- 실거래: `AUTO_TRANSFER_ENABLED=true` 면 봇이 출금하고 도착을 확인한다. false(기본)면 알림만 하고 직접 보낸다 ([live.md](live.md) 4번)
- 펀딩비 전략은 바이비트 안에서 끝나서 송금이 필요 없다.

"""
웹소켓 호가 (QUOTE_SOURCE=ws) — 업비트·바이비트 호가창을 거래소가 밀어 주는 대로 받아 들고만 있는다

왜 필요한가
    REST 는 스캔마다 호가를 "요청해서" 받는다: 한 번에 190 ms 가 들고, 10초에 한 번만 본다. 그래서 김프가
    2~3초 튀었다 꺼지는 순간은 못 보고, 판단에 쓴 호가판은 체결 순간 기준 0.4초 전 것이다.
    소켓은 호가가 바뀔 때마다 거래소가 보내 준다 (실측: 바이비트 초당 ~8회, 업비트 초당 1~2회, 거래소 시각
    대비 70~80 ms 뒤 도착). 판단 순간 손에 최신 판이 있으니 요청 시간이 0 이 되고, 스캔 주기를 줄이면
    기회를 10초 단위가 아니라 1초 아래로 볼 수 있다.

이 모듈이 하는 일 / 안 하는 일
    - 한다: 심볼마다 최신 호가창(OrderBook)과 받은 시각을 들고 있는다. books(coin) 으로 세 장(코인·바이비트·USDT)을
      준다 — 셋 다 있고 "신선" 할 때만. 아니면 None.
      신선 = 그 거래소 연결이 살아 있다 + 그 심볼 호가가 SYMBOL_SILENT_MAX_SEC 안에 온 적 있다.
      연결이 죽은 건 두 가지로 안다: ① 소켓이 예외를 던지면 즉시 (그 거래소 전체를 죽음으로 표시, 다음 메시지가 오면
      산 것), ② 예외 없이 max_age_ms 동안 아무것도 안 오면 (조용히 매달린 연결). 심볼 하나가 한동안 조용한 건 "호가가
      안 바뀜" 이지 묵은 게 아니다 — 거래소는 바뀔 때만 보낸다 (실측: 조용한 코인은 수 초씩 안 온다). 거래소 전체가
      1~2초 조용한 것도 한가한 장에선 생기므로 ② 는 몇 초 단위로 둔다 (기본 5초).
    - 안 한다: 김프 계산, Quote 조립, 매매 판단. 그건 MarketData.fetch_quotes 가 한다 — 여기 books() 를 먼저 보고
      없거나 묵은 코인만 REST 로 받는다. 그래서 엔진·브로커는 시세가 어디서 왔는지 모른다.
    - 주문은 그대로 REST (live_executor). 소켓 주문은 쓰지 않는다.

동작
    데몬 스레드 하나에서 asyncio 루프를 돌리고, 심볼마다 ccxt.pro watch_order_book 을 끝없이 기다린다.
    연결이 끊기면 ccxt 가 다시 붙고, 예외가 나면 잠깐 쉬었다 다시 시도한다 (로그는 묶어서, 복구도 한 번만).
    그동안 그 심볼은 묵은 호가가 되어 books() 가 None → MarketData 가 REST 로 받는다. 즉 소켓이 죽어도 봇은 선다.

한계
    업비트 소켓은 호가 15줄, 바이비트 50줄 (REST 는 ORDERBOOK_DEPTH=30). 15줄로 우리 수량을 못 채우면 평가가
    "호가 모자람" 으로 그 스캔을 건너뛴다 — 포지션이 아주 크면 REST 가 낫다.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from kimp.market_data import USDT_KRW_SYMBOL, _log_recovered, _log_repeat, bybit_symbol, upbit_symbol
from kimp.premium import OrderBook

logger = logging.getLogger(__name__)

Key = Tuple[str, str]   # (거래소 이름, 심볼). 예) ("upbit", "MINA/KRW"), ("bybit", "MINA/USDT:USDT")

RETRY_BACKOFF_MAX_SEC = 10.0   # 소켓 예외 뒤 다시 붙기까지 최대 대기 (1초에서 두 배씩)
SYMBOL_SILENT_MAX_SEC = 60.0   # 심볼 하나가 이보다 오래 아무것도 안 오면 (연결은 살아 있어도) 의심 → REST 로
# 시작 뒤 이 시간 안의 끊김은 조용히 다시 붙는다 (경고 안 함). 업비트는 ccxt 가 심볼을 하나 더 구독할 때마다 연결을
# 닫고 새로 여는데(원격 종료 코드 1000), 코인 6개면 시작 3초 동안 그게 열두 번쯤 찍힌다 — 실거래면 전부 텔레그램으로
# 간다. 이 시간이 지나도 끊기면 진짜 문제라 경고한다. (업비트 묶음 구독은 ccxt 가 아직 지원하지 않는다)
WARMUP_SEC = 10.0


@dataclass(frozen=True)
class FeedBooks:
    """books(coin) 이 돌려주는 세 장. age_ms 는 셋 중 가장 오래된 것, skew_ms 는 업비트·바이비트 받은 시각 차"""
    upbit: OrderBook
    bybit: OrderBook
    usdt: OrderBook
    age_ms: int
    skew_ms: int


class WsFeed:
    """
    사용법:
        feed = WsFeed(["MINA", "CAP"], max_age_ms=1500)
        feed.start()                  # 스레드 시작 (바로 돌아온다)
        feed.wait_ready(timeout=5)    # 첫 호가가 들어올 때까지 잠깐 기다림 (선택)
        fb = feed.books("MINA")       # None 이면 아직 없거나 묵었다 → REST 로
        feed.stop()
    """

    def __init__(self, coins: Iterable[str], max_age_ms: int = 1500, clock: Callable[[], float] = time.monotonic):
        self.coins = list(coins)
        self.max_age_ms = max_age_ms
        self._clock = clock
        self._books: Dict[Key, Tuple[OrderBook, float]] = {}   # key → (호가창, 받은 시각 monotonic)
        self._updates: Dict[Key, int] = {}                     # key → 받은 횟수 (상태 표시용)
        self._last_any: Dict[str, float] = {}                  # 거래소 이름 → 그 거래소에서 마지막으로 뭔가 온 시각
        self._lock = threading.Lock()
        self._changed = threading.Event()   # 호가가 하나라도 새로 오면 켜진다 (엔진이 기다렸다 깨는 신호)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None

    # ------------------------------------------------------------------
    # 읽기 (메인 스레드)
    # ------------------------------------------------------------------
    def books(self, coin: str) -> Optional[FeedBooks]:
        """
        코인 호가 세 장. 하나라도 없거나 신선하지 않으면 None (→ 부르는 쪽이 REST 로 간다)

        신선: 그 거래소에서 max_age_ms 안에 뭔가 왔고(연결 생존), 그 심볼도 SYMBOL_SILENT_MAX_SEC 안에 온 적 있다.
        age_ms 는 세 장 중 가장 오래된 것 (정보용 — 조용한 코인은 커도 유효하다).
        """
        now = self._clock()
        keys = (("upbit", upbit_symbol(coin)), ("bybit", bybit_symbol(coin)), ("upbit", USDT_KRW_SYMBOL))
        with self._lock:
            got = [self._books.get(k) for k in keys]
            alive = {ex: (now - t) * 1000 <= self.max_age_ms for ex, t in self._last_any.items()}
        if any(g is None for g in got):
            return None
        for (ex, _), (_, t) in zip(keys, got):
            if not alive.get(ex) or now - t > SYMBOL_SILENT_MAX_SEC:
                return None
        (up, t_up), (by, t_by), (us, t_us) = got
        return FeedBooks(upbit=up, bybit=by, usdt=us, age_ms=int(round((now - min(t_up, t_by, t_us)) * 1000)),
                         skew_ms=int(round(abs(t_up - t_by) * 1000)))

    def wait_change(self, timeout: float) -> bool:
        """
        호가가 새로 올 때까지 기다린다 (최대 timeout 초). 왔으면 True, 시간이 다 됐으면 False

        엔진이 "소켓이 바뀌면 즉시 판단" 하려고 쓴다. 기다리는 동안 온 변경은 다음 호출에서 바로 True 로 나온다
        (신호를 비우는 건 여기뿐).
        """
        changed = self._changed.wait(timeout)
        self._changed.clear()
        return changed

    def fresh_coins(self) -> List[str]:
        """지금 소켓 호가로 바로 쓸 수 있는 코인"""
        return [c for c in self.coins if self.books(c) is not None]

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """모든 코인의 호가가 한 번씩 들어올 때까지 기다린다 (최대 timeout 초). 다 들어왔으면 True"""
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if len(self.fresh_coins()) == len(self.coins):
                return True
            time.sleep(0.1)
        return len(self.fresh_coins()) == len(self.coins)

    def status_text(self) -> str:
        """/status·시작 로그용 한 줄. 예) 웹소켓 신선 4/5 (ZAMA 묵음) · 업데이트 업비트 1.3/s 바이비트 7.9/s"""
        fresh = set(self.fresh_coins())
        stale = [c for c in self.coins if c not in fresh]
        elapsed = max(1e-9, self._clock() - (self._started_at or self._clock()))
        with self._lock:
            up = sum(n for (ex, _), n in self._updates.items() if ex == "upbit")
            by = sum(n for (ex, _), n in self._updates.items() if ex == "bybit")
        note = f" ({', '.join(stale)} 끊김·묵음 → REST)" if stale else ""
        return (f"웹소켓 신선 {len(fresh)}/{len(self.coins)}{note} · 업데이트 업비트 {up / elapsed:.1f}/s "
                f"바이비트 {by / elapsed:.1f}/s")

    # ------------------------------------------------------------------
    # 켜고 끄기
    # ------------------------------------------------------------------
    def start(self) -> None:
        """소켓 스레드 시작. 두 번 불러도 한 번만 뜬다"""
        if self._thread is not None:
            return
        self._started_at = self._clock()
        self._thread = threading.Thread(target=self._thread_main, name="ws-feed", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """소켓 닫고 스레드가 끝날 때까지 기다린다 (최대 timeout 초)"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # 내부: 받기 (소켓 스레드)
    # ------------------------------------------------------------------
    def _mark_dead(self, exchange: str) -> None:
        """소켓이 예외를 던졌다 → 그 거래소 호가는 다음 메시지가 올 때까지 안 쓴다 (books() 가 None)"""
        with self._lock:
            self._last_any[exchange] = float("-inf")

    def _store(self, key: Key, book: OrderBook) -> None:
        """호가창 한 장 갱신 (테스트에서는 소켓 없이 이걸로 넣는다)"""
        now = self._clock()
        with self._lock:
            self._books[key] = (book, now)
            self._updates[key] = self._updates.get(key, 0) + 1
            self._last_any[key[0]] = now
        self._changed.set()

    def _subscriptions(self) -> List[Tuple[str, Key, str]]:
        """(거래소 이름, key, 심볼) 목록: 코인마다 업비트·바이비트 + 업비트 USDT/KRW 한 번"""
        subs: List[Tuple[str, Key, str]] = []
        for coin in self.coins:
            subs.append(("upbit", ("upbit", upbit_symbol(coin)), upbit_symbol(coin)))
            subs.append(("bybit", ("bybit", bybit_symbol(coin)), bybit_symbol(coin)))
        subs.append(("upbit", ("upbit", USDT_KRW_SYMBOL), USDT_KRW_SYMBOL))
        return subs

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except Exception:
            logger.exception("[웹소켓 호가] 스레드가 예외로 끝났습니다 — 이후 시세는 REST 로만 받습니다")
        finally:
            loop.close()

    async def _run(self) -> None:
        """거래소 소켓 객체를 만들고 심볼마다 받기 작업을 띄운 뒤, 멈추라는 신호까지 기다린다"""
        import ccxt.pro as ccxtpro   # 무거운 모듈이라 쓸 때만 (REST 만 쓰는 실행에서는 안 올린다)

        exchanges = {"upbit": ccxtpro.upbit(),
                     "bybit": ccxtpro.bybit({"options": {"defaultType": "swap"}})}
        tasks = [asyncio.create_task(self._watch(exchanges[name], key, symbol), name=f"ws-{symbol}")
                 for name, key, symbol in self._subscriptions()]
        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.2)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for ex in exchanges.values():
                try:
                    await ex.close()
                except Exception:
                    pass

    async def _watch(self, ex, key: Key, symbol: str) -> None:
        """심볼 하나를 끝없이 받는다. 예외가 나면 묶어서 알리고 잠깐 쉬었다 다시 (그동안은 REST 가 받는다)"""
        backoff = 1.0
        log_key = f"ws-{key[0]}-{symbol}"
        while not self._stop.is_set():
            try:
                raw = await ex.watch_order_book(symbol)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._mark_dead(key[0])
                if self._clock() - (self._started_at or 0.0) < WARMUP_SEC:
                    logger.debug("[웹소켓 호가] 시작 중 재접속 %s %s: %s", key[0], symbol, exc)
                    await asyncio.sleep(0.5)
                    continue
                _log_repeat(log_key, logging.WARNING,
                            f"[웹소켓 호가 끊김] {key[0]} {symbol} | {exc} | 다시 붙는 중 (그동안 REST 로 받습니다)")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RETRY_BACKOFF_MAX_SEC)
                continue
            backoff = 1.0
            _log_recovered(log_key, f"웹소켓 호가 {key[0]} {symbol}")
            self._store(key, OrderBook(
                bids=[(float(p), float(q)) for p, q, *_ in raw.get("bids", [])],
                asks=[(float(p), float(q)) for p, q, *_ in raw.get("asks", [])],
                timestamp_ms=raw.get("timestamp"),
            ))

"""
웹소켓 호가(kimp/ws_feed.py) — 소켓 없이 '신선한가' 판단과 상태 글자만 본다

실행: python -m unittest discover -s tests -v
"""
import unittest

from kimp.premium import OrderBook
from kimp.ws_feed import WsFeed


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def book(price):
    return OrderBook(bids=[(price, 10.0)], asks=[(price * 1.001, 10.0)], timestamp_ms=1)


class WsFeedTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.feed = WsFeed(["MINA", "CAP"], max_age_ms=1500, clock=self.clock)

    def test_books_needs_all_three_and_fresh(self):
        """코인·바이비트·USDT 세 장이 다 있고 max_age 안쪽일 때만 준다. 하나라도 없거나 묵으면 None"""
        self.assertIsNone(self.feed.books("MINA"))
        self.feed._store(("upbit", "MINA/KRW"), book(91.4))
        self.feed._store(("upbit", "USDT/KRW"), book(1340.0))
        self.assertIsNone(self.feed.books("MINA"))                 # 바이비트가 아직 없다
        self.clock.t += 0.3
        self.feed._store(("bybit", "MINA/USDT:USDT"), book(0.0682))
        fb = self.feed.books("MINA")
        self.assertIsNotNone(fb)
        self.assertEqual(fb.upbit.bids[0][0], 91.4)
        self.assertEqual(fb.age_ms, 300)                           # 가장 오래된 장(업비트) 기준
        self.assertEqual(fb.skew_ms, 300)                          # 업비트 ↔ 바이비트 받은 시각 차
        self.assertEqual(self.feed.fresh_coins(), ["MINA"])        # CAP 은 없다
        self.clock.t += 1.3                                        # 업비트는 1.6초, 바이비트는 1.3초 동안 아무것도 안 옴
        self.assertIsNone(self.feed.books("MINA"))                 # 업비트 연결 의심(1600 > 1500) → None
        self.feed._store(("upbit", "USDT/KRW"), book(1340.0))      # 업비트에서 뭔가 옴 → 업비트 살아 있음
        fb = self.feed.books("MINA")
        self.assertIsNotNone(fb)                                   # 바이비트는 1.3초 전 → 아직 살아 있다고 본다
        self.assertEqual(fb.age_ms, 1600)                          # 업비트 MINA 장은 1.6초 전 것이지만 (조용했을 뿐) 유효
        self.clock.t += 0.3                                        # 바이비트 1.6초 → 연결 의심 → None
        self.assertIsNone(self.feed.books("MINA"))
        self.feed._store(("bybit", "CAP/USDT:USDT"), book(0.07))   # 바이비트에서 다른 코인이 와도 연결은 산 것
        self.assertIsNotNone(self.feed.books("MINA"))

    def test_socket_error_marks_exchange_dead_until_next_message(self):
        """소켓 예외 → 그 거래소 호가는 바로 안 쓴다 (묵을 때까지 기다리지 않는다). 다음 메시지가 오면 다시 산다"""
        for key, p in ((("upbit", "MINA/KRW"), 91.4), (("bybit", "MINA/USDT:USDT"), 0.0682), (("upbit", "USDT/KRW"), 1340.0)):
            self.feed._store(key, book(p))
        self.assertIsNotNone(self.feed.books("MINA"))
        self.feed._mark_dead("bybit")
        self.assertIsNone(self.feed.books("MINA"))
        self.feed._store(("bybit", "CAP/USDT:USDT"), book(0.07))   # 바이비트에서 뭔가 다시 옴
        self.assertIsNotNone(self.feed.books("MINA"))

    def test_quiet_symbol_stays_valid_while_exchange_is_alive(self):
        """코인 호가가 한참 안 바뀌어도(거래소는 바뀔 때만 보낸다) 그 거래소에서 다른 게 계속 오면 유효하다"""
        self.feed._store(("upbit", "MINA/KRW"), book(91.4))
        self.feed._store(("bybit", "MINA/USDT:USDT"), book(0.0682))
        for _ in range(20):                                        # 10초 동안 USDT/KRW 와 다른 코인만 온다
            self.clock.t += 0.5
            self.feed._store(("upbit", "USDT/KRW"), book(1340.0))
            self.feed._store(("bybit", "CAP/USDT:USDT"), book(0.07))
        fb = self.feed.books("MINA")
        self.assertIsNotNone(fb)
        self.assertEqual(fb.age_ms, 10_000)
        self.clock.t += 55                                         # 65초째 조용 → 심볼 자체를 의심
        self.feed._store(("upbit", "USDT/KRW"), book(1340.0))
        self.feed._store(("bybit", "CAP/USDT:USDT"), book(0.07))
        self.assertIsNone(self.feed.books("MINA"))

    def test_status_text_names_stale_coins(self):
        self.feed._started_at = self.clock.t
        for key, p in ((("upbit", "MINA/KRW"), 91.4), (("bybit", "MINA/USDT:USDT"), 0.0682), (("upbit", "USDT/KRW"), 1340.0)):
            self.feed._store(key, book(p))
        self.clock.t += 2.0
        for key, p in ((("upbit", "MINA/KRW"), 91.4), (("bybit", "MINA/USDT:USDT"), 0.0682), (("upbit", "USDT/KRW"), 1340.0)):
            self.feed._store(key, book(p))
        text = self.feed.status_text()
        self.assertIn("신선 1/2", text)
        self.assertIn("CAP 끊김·묵음 → REST", text)
        self.assertIn("업데이트 업비트 2.0/s 바이비트 1.0/s", text)   # 2초 동안 업비트 4회, 바이비트 2회

    def test_subscriptions_cover_each_coin_and_usdt_once(self):
        subs = self.feed._subscriptions()
        self.assertEqual([s[2] for s in subs], ["MINA/KRW", "MINA/USDT:USDT", "CAP/KRW", "CAP/USDT:USDT", "USDT/KRW"])

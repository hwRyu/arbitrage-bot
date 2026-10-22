"""
성과 대시보드 집계 테스트 (네트워크 불필요)

kimp/dashboard.py 는 DB 에서 읽은 거래를 기간으로 잘라 요약·차트·표 데이터를 만든다.
여기서는 임시 폴더에 DB 를 만들어 넣고 숫자가 맞는지 확인한다.

실행: python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from kimp.config import Settings
from kimp.dashboard import (KST, Trade, by_coin, by_reason, daily, dashboard, default_source,
                            equity_curve, in_range, list_sources, load_open, load_trades, parse_day,
                            summarize)
from kimp.db import Position, PositionStatus, Transfer, TransferStatus, make_session_factory
from kimp.history import SimRun, SimTrade, make_history_session_factory

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


def ms(text: str) -> int:
    """'2026-10-01 09:30' (한국 시간) → epoch 밀리초"""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp() * 1000)


def utc(ms_value: int) -> datetime:
    """epoch 밀리초 → 시간대 없는 UTC (SQLite 가 저장하는 모양)"""
    return datetime.fromtimestamp(ms_value / 1000, timezone.utc).replace(tzinfo=None)


def trade(coin="CAP", closed="2026-10-01 12:00", hold_hours=2.0, net=0.5, percent=1.0,
          funding=0.0, cost=0.15, notional=50.0, reason=PositionStatus.CLOSED, trade_id=1) -> Trade:
    """테스트용 거래 한 건"""
    closed_ms = ms(closed)
    return Trade(id=trade_id, coin=coin, opened_ms=closed_ms - int(hold_hours * HOUR_MS),
                 closed_ms=closed_ms, entry_premium=-0.7, exit_premium=0.8,
                 exit_reason="TAKE_PROFIT" if reason == PositionStatus.CLOSED else reason,
                 net_usdt=net, net_percent=percent, funding_usdt=funding, cost_usdt=cost,
                 notional_usdt=notional)


def add_position(session, coin="CAP", status=PositionStatus.CLOSED, opened_ms=None, closed_ms=None,
                 net=0.5, percent=1.0, funding=0.0, cost=0.15, notional=50.0, reason="TAKE_PROFIT"):
    """positions 테이블에 한 줄 (필수 칸은 적당한 값으로 채운다)"""
    session.add(Position(
        coin=coin, status=status, is_paper=True,
        opened_at=utc(opened_ms or ms("2026-10-01 10:00")),
        closed_at=utc(closed_ms) if closed_ms else None,
        usdt_sold=notional, entry_usdt_price_krw=1337.0, entry_usdt_fee_krw=33.0,
        upbit_qty=100.0, entry_upbit_price_krw=668.0, entry_upbit_fee_krw=33.0,
        bybit_qty=100.0, entry_bybit_price_usdt=0.5, entry_bybit_fee_usdt=0.03,
        entry_premium=-0.7, leverage=1.0, bybit_margin_usdt=notional, liquidation_price=1.0,
        target_net_profit_percent=1.0, stop_loss_percent=-5.0, funding_usdt=funding,
        last_net_percent=0.4, last_exit_premium=-0.3,
        exit_premium=0.8, exit_reason=reason if closed_ms else None,
        total_cost_usdt=cost if closed_ms else None,
        net_pnl_usdt=net if closed_ms else None, net_pnl_percent=percent if closed_ms else None,
    ))


class TimeHelpersTest(unittest.TestCase):
    def test_parse_day_is_kst(self):
        """날짜 칸은 한국 시간이다. 시작은 00:00, 끝은 그 날 23:59:59 까지 포함한다"""
        start = parse_day("2026-10-01")
        end = parse_day("2026-10-01", end_of_day=True)
        self.assertEqual(start, ms("2026-10-01 00:00"))
        self.assertEqual(end, ms("2026-10-02 00:00") - 1)
        self.assertEqual(end - start, DAY_MS - 1)

    def test_parse_day_bad_input(self):
        """빈 값이나 엉뚱한 글자는 None (기간을 '안 정했다' 로 본다)"""
        self.assertIsNone(parse_day(""))
        self.assertIsNone(parse_day("어제"))
        self.assertIsNone(parse_day("2026-13-99"))

    def test_trade_derived_values(self):
        """보유 시간과 김프 몫은 계산해서 나온다"""
        t = trade(hold_hours=3.0, net=0.5, funding=-0.1, cost=0.2)
        self.assertAlmostEqual(t.hold_hours, 3.0)
        # 순손익 = 김프 몫 + 펀딩비 − 비용  →  김프 몫 = 0.5 − (−0.1) + 0.2 = 0.8
        self.assertAlmostEqual(t.gross_usdt, 0.8)


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self.trades = [
            trade(trade_id=1, coin="CAP", closed="2026-10-01 12:00", net=1.0, percent=2.0, cost=0.1),
            trade(trade_id=2, coin="CAP", closed="2026-10-02 12:00", net=-0.4, percent=-0.8, cost=0.2,
                  reason="STOP_LOSS"),
            trade(trade_id=3, coin="SEI", closed="2026-10-03 12:00", net=0.6, percent=1.2, cost=0.1,
                  funding=-0.05),
        ]
        self.start, self.end = parse_day("2026-10-01"), parse_day("2026-10-10", end_of_day=True)

    def test_totals(self):
        """합계·승률·평균이 맞다"""
        s = summarize(self.trades, self.start, self.end)
        self.assertEqual(s["trades"], 3)
        self.assertAlmostEqual(s["net_usdt"], 1.2)
        self.assertEqual((s["wins"], s["losses"]), (2, 1))
        self.assertAlmostEqual(s["win_rate"], 200 / 3)
        self.assertAlmostEqual(s["avg_net_usdt"], 0.4)
        self.assertAlmostEqual(s["avg_net_percent"], (2.0 - 0.8 + 1.2) / 3)
        self.assertAlmostEqual(s["funding_usdt"], -0.05)
        self.assertAlmostEqual(s["cost_usdt"], 0.4)
        # 김프 몫 = 순손익 − 펀딩비 + 비용
        self.assertAlmostEqual(s["gross_usdt"], 1.2 + 0.05 + 0.4)

    def test_turnover_and_month(self):
        """거래 규모 대비 수익률과 월 환산"""
        s = summarize(self.trades, self.start, self.end)
        self.assertAlmostEqual(s["turnover_usdt"], 150.0)
        self.assertAlmostEqual(s["return_on_turnover"], 1.2 / 150 * 100)
        self.assertAlmostEqual(s["days"], 10.0, places=2)
        self.assertAlmostEqual(s["net_per_month"], 1.2 / 10 * 30, places=4)

    def test_drawdown(self):
        """최대 낙폭: +1.0 까지 올랐다가 0.6 으로 밀렸으니 −0.4"""
        s = summarize(self.trades, self.start, self.end)
        self.assertAlmostEqual(s["max_drawdown_usdt"], -0.4)

    def test_best_and_worst(self):
        s = summarize(self.trades, self.start, self.end)
        self.assertEqual(s["best"]["id"], 1)
        self.assertEqual(s["worst"]["id"], 2)

    def test_empty_is_zero_not_error(self):
        """거래가 없으면 0 으로 채운다 (화면이 깨지지 않게)"""
        s = summarize([], self.start, self.end)
        self.assertEqual(s["trades"], 0)
        self.assertEqual(s["net_usdt"], 0.0)
        self.assertIsNone(s["win_rate"])
        self.assertIsNone(s["best"])


class GroupingTest(unittest.TestCase):
    def setUp(self):
        self.trades = [
            trade(trade_id=1, coin="CAP", closed="2026-10-01 12:00", net=1.0, percent=2.0),
            trade(trade_id=2, coin="CAP", closed="2026-10-01 20:00", net=-0.4, percent=-0.8,
                  reason="STOP_LOSS"),
            trade(trade_id=3, coin="SEI", closed="2026-10-02 12:00", net=0.6, percent=1.2),
        ]

    def test_equity_curve_accumulates(self):
        """누적 곡선은 청산 시각 순서로 더해 나간다"""
        curve = equity_curve(self.trades)
        self.assertEqual([round(p["cum_usdt"], 2) for p in curve], [1.0, 0.6, 1.2])

    def test_daily_buckets_by_kst(self):
        """날짜는 한국 시간으로 묶는다 (같은 날 두 건은 한 칸)"""
        rows = daily(self.trades)
        self.assertEqual([r["date"] for r in rows], ["2026-10-01", "2026-10-02"])
        self.assertAlmostEqual(rows[0]["net_usdt"], 0.6)
        self.assertEqual((rows[0]["trades"], rows[0]["wins"]), (2, 1))

    def test_daily_uses_kst_not_utc(self):
        """한국 시간 0시 30분에 닫은 거래는 그 날에 들어간다 (UTC 로 보면 전날이 된다)"""
        rows = daily([trade(closed="2026-10-05 00:30")])
        self.assertEqual(rows[0]["date"], "2026-10-05")

    def test_by_coin_sorted_by_profit(self):
        rows = by_coin(self.trades)
        self.assertEqual([r["coin"] for r in rows], ["CAP", "SEI"])
        cap = rows[0]
        self.assertEqual((cap["trades"], cap["wins"]), (2, 1))
        self.assertAlmostEqual(cap["net_usdt"], 0.6)
        self.assertAlmostEqual(cap["win_rate"], 50.0)
        self.assertAlmostEqual(cap["best_usdt"], 1.0)
        self.assertAlmostEqual(cap["worst_usdt"], -0.4)

    def test_by_reason(self):
        rows = by_reason(self.trades)
        self.assertEqual(rows[0]["reason"], "TAKE_PROFIT")
        self.assertEqual(rows[0]["trades"], 2)
        self.assertEqual(rows[1]["reason"], "STOP_LOSS")

    def test_in_range_uses_close_time(self):
        """기간은 청산 시각 기준 — 진입이 기간보다 앞서도 청산이 안에 있으면 들어간다"""
        picked = in_range(self.trades, parse_day("2026-10-02"), parse_day("2026-10-02", end_of_day=True))
        self.assertEqual([t.id for t in picked], [3])


class DbSourceTest(unittest.TestCase):
    """실제 DB 파일에서 읽기 (봇 positions / 시뮬레이션 sim_trades)"""

    def setUp(self):
        # 윈도우에서는 SQLAlchemy 가 DB 파일을 잡고 있어 TemporaryDirectory 정리가 실패한다 → 직접 지운다
        d = self.tmp = tempfile.mkdtemp(prefix="kimp_dash_")
        self.settings = Settings(
            db_path=os.path.join(d, "kimp.db"),
            dry_run_db_path=os.path.join(d, "dryrun.db"),
            live_db_path=os.path.join(d, "live.db"),
            history_db_path=os.path.join(d, "history.db"),
            snapshot_db_path=os.path.join(d, "market.db"),
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fill_paper(self):
        with make_session_factory(self.settings.db_path)() as s:
            add_position(s, coin="CAP", closed_ms=ms("2026-10-01 12:00"), net=1.0, percent=2.0)
            add_position(s, coin="SEI", closed_ms=ms("2026-10-03 12:00"), net=-0.5, percent=-1.0,
                         reason="STOP_LOSS")
            add_position(s, coin="MEW", status=PositionStatus.OPEN)  # 아직 열려 있음
            s.add(Transfer(direction="upbit->bybit", amount_usdt=846.0, fee_usdt=0.0, network="TRX",
                           status=TransferStatus.FAILED, reason="시작", note="출금지연제",
                           requested_at=utc(ms("2026-10-02 09:00"))))
            s.commit()

    def _fill_sim(self):
        with make_history_session_factory(self.settings.history_db_path)() as s:
            s.add(SimRun(id=1, created_at="2026-10-22T00:00:00+00:00",
                         from_ms=ms("2026-10-01 00:00"), to_ms=ms("2026-10-05 00:00"),
                         coins="CAP", settings_json="{}",
                         summary_json=json.dumps({"total_net_usdt": 1.5, "closed_trades": 2})))
            s.add(SimTrade(run_id=1, coin="CAP", opened_at="2026-10-01T01:00:00+00:00",
                           closed_at="2026-10-01T03:00:00+00:00", entry_premium=-0.7, exit_premium=0.8,
                           exit_reason="TAKE_PROFIT", net_pnl_usdt=1.0, net_pnl_percent=2.0,
                           funding_usdt=0.0, cost_usdt=0.15))
            s.add(SimTrade(run_id=1, coin="CAP", opened_at="2026-10-02T01:00:00+00:00",
                           closed_at=None, entry_premium=-0.6, exit_premium=None,  # 끝까지 보유
                           exit_reason="OPEN", net_pnl_usdt=0.5, net_pnl_percent=1.0,
                           funding_usdt=0.0, cost_usdt=0.15))
            s.commit()

    def test_load_only_closed_positions(self):
        """끝난 거래만 거래 목록에 들어간다. 열린 포지션은 따로 읽는다"""
        self._fill_paper()
        trades, meta = load_trades(self.settings, "paper")
        self.assertEqual(sorted(t.coin for t in trades), ["CAP", "SEI"])
        self.assertTrue(meta["exists"])
        self.assertEqual(meta["kind"], "bot")
        [open_pos] = load_open(self.settings, "paper")
        self.assertEqual((open_pos["coin"], open_pos["status"]), ("MEW", PositionStatus.OPEN))

    def test_missing_db_is_empty_not_error(self):
        """DB 파일이 없어도 빈 결과를 준다 (실거래를 아직 안 한 상태)"""
        trades, meta = load_trades(self.settings, "live")
        self.assertEqual(trades, [])
        self.assertFalse(meta["exists"])

    def test_unknown_source(self):
        with self.assertRaises(ValueError):
            load_trades(self.settings, "그런거없음")

    def test_load_sim_trades_skips_still_open(self):
        """시뮬레이션에서 기간 끝까지 들고 있던 거래는 끝난 거래가 아니다"""
        self._fill_sim()
        trades, meta = load_trades(self.settings, "sim:1")
        self.assertEqual(len(trades), 1)
        self.assertEqual(meta["run_id"], 1)
        # 포지션 금액은 손익 ÷ 손익률 로 거꾸로 구한다 (1.0 / 2.0 × 100 = 50)
        self.assertAlmostEqual(trades[0].notional_usdt, 50.0)

    def test_list_sources_counts_and_active(self):
        """드롭다운 목록: 모드 DB 는 거래 수, 지금 모드에는 표시가 붙는다"""
        self._fill_paper()
        self._fill_sim()
        rows = {r["value"]: r for r in list_sources(self.settings)}
        self.assertEqual(rows["paper"]["trades"], 2)
        self.assertEqual(rows["live"]["trades"], 0)
        self.assertTrue(rows["paper"]["active"])  # Settings 기본은 페이퍼 모드
        self.assertIn("sim:1", rows)
        self.assertIn("CAP", rows["sim:1"]["label"])

    def test_default_source_falls_back_to_sim(self):
        """지금 모드에 거래가 없으면 가장 최근 시뮬레이션을 기본으로 본다"""
        self._fill_sim()
        self.assertEqual(default_source(self.settings), "sim:1")
        self._fill_paper()
        self.assertEqual(default_source(self.settings), "paper")

    def test_dashboard_end_to_end(self):
        """대시보드 한 덩어리: 기간을 정하면 그 안의 거래만, 송금 이력도 함께"""
        self._fill_paper()
        d = dashboard(self.settings, "paper", "2026-10-01", "2026-10-02")
        self.assertEqual(d["summary"]["trades"], 1)  # 10-03 거래는 기간 밖
        self.assertAlmostEqual(d["summary"]["net_usdt"], 1.0)
        self.assertEqual(len(d["trades"]), 1)
        self.assertEqual(len(d["open"]), 1)
        self.assertEqual([t["status"] for t in d["transfers"]], [TransferStatus.FAILED])
        self.assertEqual(d["range"]["start"], "2026-10-01")
        self.assertEqual(d["range"]["end"], "2026-10-02")

    def test_dashboard_auto_range_covers_all(self):
        """날짜를 비우면 기록 전체가 들어가는 기간으로 맞춘다"""
        self._fill_paper()
        d = dashboard(self.settings, "paper")
        self.assertEqual(d["summary"]["trades"], 2)
        self.assertEqual((d["range"]["start"], d["range"]["end"]), ("2026-10-01", "2026-10-03"))

    def test_dashboard_swaps_reversed_dates(self):
        """시작과 끝을 거꾸로 적어도 바꿔서 본다"""
        self._fill_paper()
        d = dashboard(self.settings, "paper", "2026-10-05", "2026-10-01")
        self.assertEqual(d["summary"]["trades"], 2)

    def test_dashboard_no_records_at_all(self):
        """기록이 하나도 없으면 최근 30일을 보여준다 (빈 화면이어도 깨지지 않게)"""
        d = dashboard(self.settings, "live", now_ms=ms("2026-10-31 12:00"))
        self.assertEqual(d["summary"]["trades"], 0)
        self.assertEqual(d["range"]["start"], "2026-10-01")
        self.assertEqual(d["equity"], [])
        self.assertEqual(d["daily"], [])

    def test_sim_source_has_no_open_or_transfers(self):
        """시뮬레이션에는 열린 포지션·송금이 없다 (봇 DB 를 섞어 보지 않는다)"""
        self._fill_paper()
        self._fill_sim()
        d = dashboard(self.settings, "sim:1")
        self.assertEqual(d["open"], [])
        self.assertEqual(d["transfers"], [])


if __name__ == "__main__":
    unittest.main()

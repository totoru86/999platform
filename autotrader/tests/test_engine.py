"""엔진 통합 테스트: 합성 1분봉 하루를 실제 엔진 코드로 재생."""
import json
from datetime import datetime, timedelta

import pytest

from trader.backtest import run_backtest
from trader.broker import SimBroker
from trader.engine import Engine
from trader.feed import ReplayFeed
from trader.journal import Journal
from trader.market import KST
from trader.models import OrderStatus
from trader.notifier import NullNotifier
from trader import watchlist as wl


def _run(cfg, day_dir, data_dir=None, watchlist_loader=None, broker=None):
    feed = ReplayFeed(day_dir)
    broker = broker or SimBroker(cash=10_000_000, costs=cfg.costs.model(), slippage_ticks=1)
    j = Journal(":memory:")
    eng = Engine(cfg, feed, broker, j, NullNotifier(), "backtest", data_dir=data_dir,
                 watchlist_loader=watchlist_loader, persist=data_dir is not None)
    for now in feed.minutes():
        eng.step(now)
        if eng.done:
            break
    return eng, j, broker


def test_full_day_take_profit_and_stop_loss(cfg, synthetic_day):
    eng, j, broker = _run(cfg, synthetic_day)
    trades = {t["code"]: t for t in j.trades()}
    assert set(trades) == {"111111", "222222"}

    a = trades["111111"]
    assert a["exit_reason"] == "take_profit"
    assert a["entry_price"] == 10460 and a["qty"] == 191        # 1회 주문한도 200만원
    assert a["exit_price"] == 10770 and a["net_pnl"] > 0
    assert a["entry_time"].startswith("2026-11-27T09:21")

    b = trades["222222"]
    assert b["exit_reason"] == "stop_loss"
    assert b["entry_price"] == 20950 and b["exit_price"] == 20550 and b["net_pnl"] < 0

    assert eng.done and not eng.positions and not broker.positions
    daily = j.daily_rows()
    assert len(daily) == 1 and daily[0]["trades"] == 2
    assert daily[0]["realized_pnl"] == pytest.approx(a["net_pnl"] + b["net_pnl"], abs=2)
    # 손실 거래도 계획한 손실 범위(계좌 0.5%) 안
    assert b["net_pnl"] > -50_000


def test_claude_avoid_list_blocks_symbol(cfg, synthetic_day, tmp_path):
    def loader(d):
        return wl.validate({"date": d.isoformat(), "market_view": "neutral",
                            "candidates": [], "avoid": [{"code": "111111", "reason": "재탕 뉴스"}]}, d)
    _, j, _ = _run(cfg, synthetic_day, watchlist_loader=loader)
    assert {t["code"] for t in j.trades()} == {"222222"}


def test_claude_only_mode_and_tags(cfg, synthetic_day):
    cfg.universe.claude_watchlist = "only"

    def loader(d):
        return wl.validate({"date": d.isoformat(), "market_view": "neutral",
                            "candidates": [{"code": "111111", "score": 90, "news_freshness": "new"}]}, d)
    _, j, _ = _run(cfg, synthetic_day, watchlist_loader=loader)
    trades = j.trades()
    assert [t["code"] for t in trades] == ["111111"]
    assert trades[0]["tags"]["claude_listed"] is True
    assert trades[0]["tags"]["news_freshness"] == "new"


def test_risk_off_view_halves_position(cfg, synthetic_day):
    def loader(d):
        return wl.validate({"date": d.isoformat(), "market_view": "risk_off", "candidates": []}, d)
    _, j, _ = _run(cfg, synthetic_day, watchlist_loader=loader)
    a = next(t for t in j.trades() if t["code"] == "111111")
    assert a["qty"] == 95          # 191 → 절반 (200만원 × 0.5 / 10470)


def test_max_open_positions_one(cfg, synthetic_day):
    cfg.risk.max_open_positions = 1
    _, j, _ = _run(cfg, synthetic_day)
    assert [t["code"] for t in j.trades()] == ["111111"]      # A 보유 중이라 B 진입 불가


def test_stop_file_flattens(cfg, synthetic_day, tmp_path):
    feed = ReplayFeed(synthetic_day)
    broker = SimBroker(cash=10_000_000, costs=cfg.costs.model())
    j = Journal(":memory:")
    eng = Engine(cfg, feed, broker, j, NullNotifier(), "backtest", data_dir=tmp_path, persist=True)
    for now in feed.minutes():
        if now.strftime("%H%M") == "0923":
            (tmp_path / "STOP").write_text("x")
        eng.step(now)
        if eng.done:
            break
    trades = j.trades()
    assert [t["exit_reason"] for t in trades] == ["risk_flatten"]   # A 만 보유 중 → 즉시 청산, B 진입 없음
    assert not broker.positions


def test_state_persist_and_restore(cfg, synthetic_day, tmp_path):
    feed = ReplayFeed(synthetic_day)
    broker = SimBroker(cash=10_000_000, costs=cfg.costs.model())
    j = Journal(":memory:")
    eng = Engine(cfg, feed, broker, j, NullNotifier(), "backtest", data_dir=tmp_path, persist=True)
    stop_at = None
    for now in feed.minutes():
        eng.step(now)
        if now.strftime("%H%M") == "0923":
            stop_at = now
            break
    st = json.loads((tmp_path / "state" / "backtest.json").read_text(encoding="utf-8"))
    assert [p["code"] for p in st["positions"]] == ["111111"]
    # 재시작: 같은 날 새 엔진이 상태를 복구해서 이어서 관리
    eng2 = Engine(cfg, feed, broker, j, NullNotifier(), "backtest", data_dir=tmp_path, persist=True)
    t = stop_at + timedelta(minutes=1)
    while not eng2.done and t.strftime("%H%M") <= "1530":
        eng2.step(t)
        t += timedelta(minutes=1)
    codes = [tr["code"] for tr in j.trades()]
    assert codes.count("111111") == 1                     # 중복 매수 없이 한 번만 청산
    assert eng2.entries_by_symbol["111111"] == 1


class SlowBroker(SimBroker):
    """실제 KIS처럼 매수 주문이 즉시 체결되지 않는 브로커."""
    def buy_limit(self, code, qty, price, ref_price):
        ack = self._ack()
        self.orders[ack.order_no] = OrderStatus(ack.order_no, code, "buy", qty, 0, 0, qty, False)
        return ack


def test_unfilled_buy_is_cancelled_after_timeout(cfg, synthetic_day):
    cfg.strategy.entry_timeout_seconds = 30
    broker = SlowBroker(cash=10_000_000, costs=cfg.costs.model())
    eng, j, _ = _run(cfg, synthetic_day, broker=broker)
    assert j.trades() == []
    assert not eng.positions and not eng.pending
    assert all(o.cancelled for o in broker.orders.values())


def test_backtest_runner(cfg, synthetic_day):
    rep, j = run_backtest(cfg, [synthetic_day], capital=10_000_000)
    assert rep["overall"]["trades"] == 2
    assert rep["overall"]["win_rate_pct"] == 50.0
    assert rep["by"]["exit_reason"]["take_profit"]["trades"] == 1
    assert rep["days"]["count"] == 1


def test_watchlist_written_after_engine_start_is_picked_up(cfg, synthetic_day):
    """엔진을 먼저 켜고 Claude 분석이 나중에 끝나도 다음 재선정 때 반영 (회피 종목 차단)."""
    state = {"ready": False}

    def loader(d):
        if not state["ready"]:
            return None, ["아직 없음"]
        return wl.validate({"date": d.isoformat(), "market_view": "neutral", "candidates": [],
                            "avoid": [{"code": "111111", "reason": "선반영"}]}, d)
    feed = ReplayFeed(synthetic_day)
    eng = Engine(cfg, feed, SimBroker(cash=10_000_000, costs=cfg.costs.model()), Journal(":memory:"),
                 NullNotifier(), "backtest", watchlist_loader=loader, persist=False)
    for now in feed.minutes():
        if now.strftime("%H%M") == "0910":
            state["ready"] = True            # 09:10 에 watchlist 생성
        eng.step(now)
        if eng.done:
            break
    assert eng.watchlist is not None
    assert {t.code for t in eng.closed} == {"222222"}

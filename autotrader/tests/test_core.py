from datetime import date, datetime

import pytest

from trader.config import TradingConfig, load_trading_config, PROJECT_ROOT
from trader.market import KST, CostModel, add_ticks, round_to_tick, tick_size
from trader.models import Bar, Quote
from trader.risk import RiskManager
from trader.strategy import OrbVwapStrategy, Position, opening_range
from trader import watchlist as wl


# ───────────── 호가단위 / 비용 ─────────────
@pytest.mark.parametrize("price,tick", [(1999, 1), (2000, 5), (4995, 5), (5000, 10), (19990, 10),
                                        (20000, 50), (49950, 50), (50000, 100), (199900, 100),
                                        (200000, 500), (500000, 1000)])
def test_tick_size(price, tick):
    assert tick_size(price) == tick


def test_add_ticks_crosses_band_boundaries():
    assert add_ticks(4995, 1) == 5000
    assert add_ticks(5000, 1) == 5010
    assert add_ticks(5000, -1) == 4995
    assert add_ticks(19990, 2) == 20050
    assert add_ticks(10450, 2) == 10470


def test_round_to_tick():
    assert round_to_tick(10293.25, "down") == 10290
    assert round_to_tick(10773.8, "up") == 10780
    assert round_to_tick(20586.5, "down") == 20550


def test_cost_model_round_trip():
    c = CostModel(0.015, 0.20)
    net, costs = c.net_pnl(10000, 10100, 100)
    assert costs == pytest.approx(10000 * 100 * 0.00015 + 10100 * 100 * 0.00215)
    assert net == pytest.approx(10000 - costs)
    assert c.round_trip_pct() == pytest.approx(0.23)


def test_example_config_loads_and_validates():
    cfg = load_trading_config(PROJECT_ROOT / "config.example.yaml")
    assert cfg.validate() == []
    assert cfg.risk.capital_krw == 5_000_000


def test_config_validation_catches_bad_values():
    cfg = TradingConfig()
    cfg.schedule.force_exit = "15:25"
    cfg.strategy.take_profit_pct = 0.3
    errs = cfg.validate()
    assert any("force_exit" in e for e in errs)
    assert any("take_profit_pct" in e for e in errs)


# ───────────── 리스크 ─────────────
def _risk(cfg=None, equity=10_000_000):
    cfg = cfg or TradingConfig()
    return RiskManager(cfg.risk, cfg.costs.model(), equity)


def test_position_size_takes_tightest_limit():
    r = _risk()
    qty, why = r.position_size(entry=10000, stop=9850, cash_available=10_000_000)
    # 손실한도 50,000/150=333, 비중 2.5M/10k=250, 주문한도 2M/10k=200 → 200
    assert (qty, why) == (200, "1회 주문금액한도")
    qty, why = r.position_size(entry=10000, stop=9850, cash_available=500_000)
    assert why == "가용현금" and qty == 48       # 500,000×0.98 ÷ (10,000×1.00015)


def test_risk_off_halves_size_and_zero_halts():
    cfg = TradingConfig()
    r = _risk(cfg)
    r.apply_market_view("risk_off")
    qty, _ = r.position_size(10000, 9850, 10_000_000)
    assert qty == 100
    cfg.risk.risk_off_size_multiplier = 0
    r2 = _risk(cfg)
    r2.apply_market_view("risk_off")
    assert r2.state.halted


def test_daily_loss_limit_halts_and_flattens():
    r = _risk()
    r.check_drawdown(-50_000)
    assert not r.state.halted
    r.check_drawdown(-100_000)
    assert r.state.halted and r.state.flatten_requested


def test_consecutive_losses_and_profit_target():
    r = _risk()
    r.on_trade_closed(-10_000)
    assert not r.state.halted
    r.on_trade_closed(-10_000)
    assert "연속 2패" in r.state.halted
    r2 = _risk()
    r2.on_trade_closed(100_000)
    assert "목표수익" in r2.state.halted


def test_can_open_limits():
    r = _risk()
    assert r.can_open(0, 0, 1)[0]
    assert not r.can_open(2, 0, 1)[0]
    assert not r.can_open(0, 1, 1)[0]
    r.state.entries_today = 4
    assert not r.can_open(0, 0, 1)[0]


# ───────────── 전략 ─────────────
def _bars(rows):
    return [Bar("20261127", t, o, h, l, c, v) for t, o, h, l, c, v in rows]


def _quote(price, vwap, upper=13000):
    return Quote(code="111111", price=price, acc_volume=1000, acc_turnover=int(vwap * 1000), upper_limit=upper)


def _or_rows():
    rows = []
    for i in range(15):
        rows.append((f"09{i:02d}00", 10300, 10400 if i == 5 else 10350, 10200 if i == 7 else 10250, 10300, 100))
    return rows


def test_opening_range():
    o = opening_range(_bars(_or_rows()), "091500")
    assert (o.high, o.low) == (10400, 10200)
    assert o.range_pct == pytest.approx(1.96, abs=0.01)


def test_entry_signal_on_fresh_breakout_with_volume():
    s = OrbVwapStrategy(TradingConfig().strategy, CostModel())
    rows = _or_rows() + [("091500", 10300, 10380, 10300, 10350, 100), ("091600", 10350, 10460, 10340, 10450, 300)]
    bars = _bars(rows)
    orng = opening_range(bars, "091500")
    sig, why = s.check_entry("111111", bars, _quote(10450, 10300), orng, "091500")
    assert sig and why == ""
    assert sig.stop == 10290 and sig.target == 10770


@pytest.mark.parametrize("mutate,reason", [
    (lambda r: r.__setitem__(-1, ("091600", 10350, 10460, 10340, 10450, 120)), "거래량"),
    (lambda r: r.__setitem__(-2, ("091500", 10300, 10450, 10300, 10420, 100)), "이미 돌파"),
    (lambda r: r.__setitem__(-1, ("091600", 10350, 10400, 10340, 10390, 300)), "돌파 전"),
])
def test_entry_rejections(mutate, reason):
    s = OrbVwapStrategy(TradingConfig().strategy, CostModel())
    rows = _or_rows() + [("091500", 10300, 10380, 10300, 10350, 100), ("091600", 10350, 10460, 10340, 10450, 300)]
    mutate(rows)
    bars = _bars(rows)
    sig, why = s.check_entry("111111", bars, _quote(10450, 10300), opening_range(bars, "091500"), "091500")
    assert sig is None and reason in why


def test_entry_rejected_below_vwap_and_near_upper_limit():
    s = OrbVwapStrategy(TradingConfig().strategy, CostModel())
    rows = _or_rows() + [("091500", 10300, 10380, 10300, 10350, 100), ("091600", 10350, 10460, 10340, 10450, 300)]
    bars = _bars(rows)
    orng = opening_range(bars, "091500")
    assert "VWAP" in s.check_entry("x", bars, _quote(10450, 10500), orng, "091500")[1]
    assert "상한가" in s.check_entry("x", bars, _quote(10450, 10300, upper=11000), orng, "091500")[1]


def _pos():
    return Position("111111", "A", 100, 10000, "2026-11-27T09:20:00+09:00", stop=9850, target=10300,
                    initial_stop=9850, peak=10000)


def test_exit_stop_and_target_priority():
    s = OrbVwapStrategy(TradingConfig().strategy, CostModel())
    assert s.check_exit(_pos(), 9840, 10310, 10000) == ("stop_loss", 9850)     # 같은 봉이면 손절 우선
    assert s.check_exit(_pos(), 9900, 10310, 10300) == ("take_profit", 10300)
    assert s.check_exit(_pos(), 9900, 10100, 10050) is None


def test_trailing_stop_locks_in_profit():
    s = OrbVwapStrategy(TradingConfig().strategy, CostModel())
    p = _pos()
    assert s.check_exit(p, 10150, 10200, 10200) is None      # +2% → 추적 시작
    assert p.trailing and p.stop >= 10098                     # 고점 10200 × 0.99
    res = s.check_exit(p, 10090, 10120, 10095)
    assert res and res[0] == "trail_stop" and res[1] > p.entry_price


def test_vwap_break_exit():
    s = OrbVwapStrategy(TradingConfig().strategy, CostModel())
    assert s.check_exit(_pos(), 9950, 10050, 9980, vwap=10000, last_bar_close=9980) == ("vwap_break", 9980)


# ───────────── watchlist ─────────────
def test_watchlist_validation():
    today = date(2026, 11, 27)
    good = dict(wl.EXAMPLE, date="2026-11-27")
    w, errs = wl.validate(good, today)
    assert errs == [] and w.codes == ["000660"] and "123456" in w.avoid
    _, errs = wl.validate(dict(good, date="2026-11-26"), today)
    assert any("오늘" in e for e in errs)
    bad = dict(good, candidates=[{"code": "abc"}], market_view="yolo")
    _, errs = wl.validate(bad, today)
    assert len(errs) == 2


def test_watchlist_rehash_penalty():
    w = wl.Watchlist("2026-11-27", candidates=[
        wl.WatchItem("111111", score=80, news_freshness="new"),
        wl.WatchItem("222222", score=80, news_freshness="rehash", priced_in=True)])
    assert w.score("111111") == 80
    assert w.score("222222") == 45
    assert w.score("999999") is None

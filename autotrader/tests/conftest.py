import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.config import TradingConfig  # noqa: E402


@pytest.fixture
def cfg():
    c = TradingConfig()
    c.risk.capital_krw = 10_000_000
    return c


def _write_stock(day_dir: Path, code: str, bars: list[tuple]):
    with open(day_dir / f"{code}.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow(b)


def _minutes(start_h, start_m, n):
    out = []
    h, m = start_h, start_m
    for _ in range(n):
        out.append(f"{h:02d}{m:02d}00")
        m += 1
        if m == 60:
            h, m = h + 1, 0
    return out


def make_synthetic_day(root: Path, day: str = "20261127") -> Path:
    """종목 A(111111): 09:20 돌파 → 익절, 종목 B(222222): 09:25 돌파 → 손절, 종목 C(333333): 돌파 없음."""
    d = root / day
    d.mkdir(parents=True)
    times = _minutes(9, 0, 391)   # 09:00 ~ 15:30

    # ── A: 전일종가 10,000 / OR 10,200~10,400
    a = []
    for i, t in enumerate(times):
        if i < 15:     # 09:00~09:14 OR
            a.append((t, 10300, 10400 if i == 5 else 10350, 10200 if i == 7 else 10250, 10300, 120_000))
        elif i < 20:   # 09:15~09:19 레인지 내
            a.append((t, 10300, 10380, 10300, 10350, 120_000))
        elif i == 20:  # 09:20 돌파봉 (거래량 급증)
            a.append((t, 10350, 10460, 10340, 10450, 300_000))
        elif i <= 26:  # 09:21~09:26 상승
            o = 10450 + (i - 21) * 60
            a.append((t, o, o + 70, o - 10, o + 60, 150_000))
        else:
            a.append((t, 10800, 10810, 10790, 10800, 50_000))
    _write_stock(d, "111111", a)

    # ── B: 전일종가 20,000 / OR 20,400~20,800
    b = []
    for i, t in enumerate(times):
        if i < 15:
            b.append((t, 20600, 20800 if i == 3 else 20700, 20400 if i == 9 else 20500, 20600, 100_000))
        elif i < 25:
            b.append((t, 20650, 20750, 20600, 20700, 100_000))
        elif i == 25:  # 09:25 돌파
            b.append((t, 20700, 20950, 20690, 20900, 300_000))
        elif i == 26:
            b.append((t, 20900, 20950, 20780, 20800, 150_000))
        elif i == 27:  # 09:27 급락 → 손절
            b.append((t, 20800, 20800, 20500, 20550, 200_000))
        else:
            b.append((t, 20500, 20550, 20450, 20500, 50_000))
    _write_stock(d, "222222", b)

    # ── C: 돌파 없음
    c = [(t, 50000, 50100, 49900, 50000, 40_000) for t in times]
    _write_stock(d, "333333", c)

    meta = {
        "111111": {"name": "에이전자", "prev_close": 10000, "market_cap_eok": 5000, "w52_high": 11000},
        "222222": {"name": "비바이오", "prev_close": 20000, "market_cap_eok": 8000, "w52_high": 25000},
        "333333": {"name": "씨화학", "prev_close": 48000, "market_cap_eok": 20000, "w52_high": 60000},
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return d


@pytest.fixture
def synthetic_day(tmp_path):
    return make_synthetic_day(tmp_path / "bars")

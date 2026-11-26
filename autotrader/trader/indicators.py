"""일봉 기술지표 요약 (Claude 기술분석 에이전트가 읽는 용도)."""
from __future__ import annotations

from .models import DailyBar


def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr_pct(bars: list[DailyBar], n: int = 14) -> float | None:
    if len(bars) <= n:
        return None
    trs = []
    for prev, b in zip(bars[-n - 1:-1], bars[-n:]):
        trs.append(max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close)))
    return sum(trs) / n / bars[-1].close * 100 if bars[-1].close else None


def summarize_daily(code: str, bars: list[DailyBar]) -> dict:
    if not bars:
        return {"code": code, "error": "일봉 데이터 없음"}
    closes = [float(b.close) for b in bars]
    last = bars[-1]
    hi = max(b.high for b in bars)
    hi_bar = max(bars, key=lambda b: b.high)

    def ret(n):
        return round((closes[-1] / closes[-1 - n] - 1) * 100, 2) if len(closes) > n else None

    ma5, ma20, ma60 = sma(closes, 5), sma(closes, 20), sma(closes, 60)
    r = rsi(closes)
    a = atr_pct(bars)
    avg_turnover_20 = sum(b.turnover for b in bars[-20:]) / min(20, len(bars))
    return {
        "code": code,
        "date": last.date,
        "close": last.close,
        "period_days": len(bars),
        "period_high": hi,
        "period_high_date": hi_bar.date,
        "pct_from_period_high": round((last.close / hi - 1) * 100, 2),
        "is_new_period_high": last.high >= hi,
        "ma5": round(ma5) if ma5 else None,
        "ma20": round(ma20) if ma20 else None,
        "ma60": round(ma60) if ma60 else None,
        "trend": ("정배열" if ma5 and ma20 and ma60 and ma5 > ma20 > ma60 else
                  "역배열" if ma5 and ma20 and ma60 and ma5 < ma20 < ma60 else "혼조"),
        "rsi14": round(r, 1) if r is not None else None,
        "atr14_pct": round(a, 2) if a is not None else None,
        "ret_1d": ret(1), "ret_5d": ret(5), "ret_20d": ret(20),
        "consecutive_up_days": _streak(closes),
        "avg_turnover_20d_eok": round(avg_turnover_20 / 1e8, 1),
        "last_turnover_eok": round(last.turnover / 1e8, 1),
    }


def _streak(closes: list[float]) -> int:
    n = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            n += 1
        else:
            break
    return n

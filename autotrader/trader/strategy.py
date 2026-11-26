"""ORB(오프닝 레인지 돌파) + VWAP 전략 — 순수 로직 (API 호출 없음).

진입: 09:00~OR종료 고가를 1분봉 '종가'로 돌파(직전 봉은 돌파 전) + 종가 > VWAP + 거래량 급증
손절: OR 저가와 -max_stop_pct% 중 진입가에 더 가까운 값
익절: +take_profit_pct%
추적손절: +trail_activate_pct% 도달 후 고점 대비 -trail_pct%, 손절선은 최소 본전(+비용) 이상
VWAP 이탈: 완성된 1분봉 종가가 VWAP 아래면 청산
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import StrategyConfig
from .market import CostModel, round_to_tick
from .models import Bar, Quote


@dataclass
class OpeningRange:
    high: int
    low: int

    @property
    def range_pct(self) -> float:
        return (self.high - self.low) / self.low * 100 if self.low else 0.0


@dataclass
class EntrySignal:
    code: str
    price: int          # 신호 가격 (돌파봉 종가 또는 현재가)
    stop: int
    target: int
    reason: str
    tags: dict = field(default_factory=dict)


@dataclass
class Position:
    code: str
    name: str
    qty: int
    entry_price: float
    entry_time: str
    stop: int
    target: int
    initial_stop: int
    peak: float = 0.0
    trailing: bool = False
    state: str = "open"             # entering | open | exiting | closed
    tags: dict = field(default_factory=dict)
    exit_reason: str = ""
    exit_attempts: int = 0
    last_price: float = 0.0

    def unrealized(self, price: float) -> float:
        return (price - self.entry_price) * self.qty


def opening_range(bars: list[Bar], or_end: str, start: str = "090000") -> OpeningRange | None:
    """or_end='091500' → 09:00 이상 09:15 미만 봉의 고가/저가."""
    rng = [b for b in bars if start <= b.time < or_end and b.high > 0]
    if len(rng) < 3:
        return None
    return OpeningRange(max(b.high for b in rng), min(b.low for b in rng if b.low > 0))


class OrbVwapStrategy:
    def __init__(self, cfg: StrategyConfig, costs: CostModel):
        self.cfg = cfg
        self.costs = costs

    def check_entry(self, code: str, bars: list[Bar], quote: Quote, orng: OpeningRange | None,
                    or_end: str) -> tuple[EntrySignal | None, str]:
        """완성된 1분봉(bars, 오름차순)과 현재 시세로 진입 신호 판단. (신호, 탈락사유)"""
        c = self.cfg
        if orng is None:
            return None, "오프닝레인지 없음"
        if orng.range_pct > c.max_or_range_pct:
            return None, f"OR폭 {orng.range_pct:.1f}% > {c.max_or_range_pct}%"
        post = [b for b in bars if b.time >= or_end]
        if len(post) < 1 or len(bars) < 2:
            return None, "OR 이후 완성봉 없음"
        last, prev = bars[-1], bars[-2]
        level = orng.high * (1 + c.breakout_buffer_pct / 100)
        if not (last.close > level):
            return None, "돌파 전"
        if prev.close > level:
            return None, "이미 돌파 진행 중(신규 돌파 아님)"
        vwap = quote.vwap
        if last.close <= vwap:
            return None, f"VWAP({vwap:,.0f}) 아래"
        lookback = [b.volume for b in bars[-1 - c.volume_lookback_bars:-1]]
        avg_vol = sum(lookback) / len(lookback) if lookback else 0
        if avg_vol > 0 and last.volume < avg_vol * c.volume_surge_ratio:
            return None, f"거래량 부족 ({last.volume:,} < 평균 {avg_vol:,.0f}×{c.volume_surge_ratio})"
        if quote.upper_limit and last.close >= quote.upper_limit * (1 - c.upper_limit_buffer_pct / 100):
            return None, "상한가 근접"
        if quote.halted or quote.vi:
            return None, "거래정지/VI"

        entry = max(last.close, quote.price)
        stop = max(orng.low, entry * (1 - c.max_stop_pct / 100))
        stop = round_to_tick(stop, "down")
        if stop >= entry:
            return None, "손절가 계산 오류"
        target = round_to_tick(entry * (1 + c.take_profit_pct / 100), "up")
        tags = {
            "or_range_pct": round(orng.range_pct, 2),
            "vol_ratio": round(last.volume / avg_vol, 2) if avg_vol else None,
            "vwap_gap_pct": round((last.close / vwap - 1) * 100, 2) if vwap else None,
        }
        return EntrySignal(code, int(entry), stop, target, "ORB+VWAP 돌파", tags), ""

    def check_exit(self, pos: Position, low: float, high: float, last: float,
                   vwap: float | None = None, last_bar_close: float | None = None) -> tuple[str, float] | None:
        """청산 판단. 실시간은 low=high=last=현재가, 백테스트는 해당 1분봉 저가/고가/종가.
        반환: (사유, 가정 체결가) 또는 None. 손절이 익절보다 우선(보수적)."""
        c = self.cfg
        # 1) 기존 손절선 먼저 확인 — 한 봉 안에서 고가/저가 순서를 모르므로 저가가 먼저 왔다고 가정(보수적)
        if low <= pos.stop:
            reason = "trail_stop" if pos.trailing else "stop_loss"
            return reason, min(pos.stop, high)       # 갭하락 시 봉 고가 이하로 체결 가정
        # 2) 익절
        if high >= pos.target:
            return "take_profit", pos.target
        # 3) 고점 갱신 → 추적손절선 상향 (다음 틱/봉부터 적용)
        pos.peak = max(pos.peak or pos.entry_price, high)
        if not pos.trailing and pos.peak >= pos.entry_price * (1 + c.trail_activate_pct / 100):
            pos.trailing = True
        if pos.trailing:
            breakeven = pos.entry_price * (1 + self.costs.round_trip_pct() / 100)
            trail_stop = pos.peak * (1 - c.trail_pct / 100)
            pos.stop = max(pos.stop, round_to_tick(max(breakeven, trail_stop), "up"))
        # 4) VWAP 이탈 (완성봉 종가 기준)
        if c.vwap_exit and vwap and last_bar_close is not None and last_bar_close < vwap:
            return "vwap_break", last
        return None


def entry_time_bucket(ts: datetime | str) -> str:
    hhmm = ts.strftime("%H%M") if isinstance(ts, datetime) else str(ts)[:4]
    h = int(hhmm[:2])
    return f"{h:02d}시"

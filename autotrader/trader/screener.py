"""종목 선정: 거래대금 순위 + 숫자 기준 필터 + Claude watchlist 반영."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .config import UniverseConfig
from .models import Quote
from .watchlist import Watchlist

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    code: str
    name: str
    rank: int | None
    quote: Quote | None
    passed: bool
    reasons: list[str] = field(default_factory=list)
    tags: dict = field(default_factory=dict)

    def summary(self) -> dict:
        q = self.quote
        return {
            "code": self.code, "name": self.name, "rank": self.rank, "passed": self.passed,
            "reasons": self.reasons, "tags": self.tags,
            "price": q.price if q else None,
            "change_pct": q.change_pct if q else None,
            "turnover_eok": round(q.turnover_eok, 1) if q else None,
            "market_cap_eok": q.market_cap_eok if q else None,
            "program_net_qty": q.program_net_qty if q else None,
            "foreign_net_qty": q.foreign_net_qty if q else None,
            "w52_high": q.w52_high if q else None,
            "vwap": round(q.vwap) if q else None,
        }


def min_turnover_eok(schedule: dict, now: datetime) -> float:
    """시각별 최소 거래대금: now 이전 마지막 구간 값 (첫 구간 전이면 첫 값)."""
    hhmm = now.strftime("%H:%M")
    items = sorted((str(k), float(v)) for k, v in schedule.items())
    if not items:
        return 0.0
    value = items[0][1]
    for k, v in items:
        if hhmm >= k:
            value = v
    return value


class Screener:
    def __init__(self, cfg: UniverseConfig, feed):
        self.cfg = cfg
        self.feed = feed

    def evaluate(self, code: str, name: str, rank: int | None, now: datetime,
                 watchlist: Watchlist | None) -> Candidate:
        c = self.cfg
        cand = Candidate(code, name, rank, None, False)
        try:
            q = self.feed.quote(code, now)
        except Exception as e:      # noqa: BLE001 — 한 종목 실패가 전체를 막지 않도록
            cand.reasons.append(f"시세조회 실패: {e}")
            return cand
        cand.quote = q
        q.name = q.name or name
        r = cand.reasons
        if q.halted:
            r.append("거래정지")
        if q.warning:
            r.append("투자경고/주의/관리/단기과열")
        if not c.min_price <= q.price <= c.max_price:
            r.append(f"가격 {q.price:,}원 범위밖")
        if q.market_cap_eok and q.market_cap_eok < c.min_market_cap_eok:
            r.append(f"시총 {q.market_cap_eok:,.0f}억 < {c.min_market_cap_eok:,.0f}억")
        need = min_turnover_eok(c.min_turnover_schedule, now)
        if q.turnover_eok < need:
            r.append(f"거래대금 {q.turnover_eok:,.0f}억 < {need:,.0f}억")
        if not c.min_change_pct <= q.change_pct <= c.max_change_pct:
            r.append(f"등락률 {q.change_pct:+.2f}% 범위밖")
        if c.require_program_net_buy and q.program_net_qty <= 0:
            r.append("프로그램 순매도")
        if c.require_foreign_net_buy and q.foreign_net_qty <= 0:
            r.append("외국인 순매도")
        near = bool(q.w52_high) and q.price >= q.w52_high * (1 - c.near_high_pct / 100)
        cand.tags["near_52w_high"] = near
        cand.tags["new_52w_high"] = bool(q.w52_high) and q.high >= q.w52_high
        if c.require_near_high and not near:
            r.append("52주 신고가 근접 아님")
        if watchlist:
            if code in watchlist.avoid:
                r.append(f"Claude 회피: {watchlist.avoid[code]}")
            s = watchlist.score(code)
            cand.tags["claude_listed"] = s is not None
            cand.tags["claude_score"] = s
            if c.claude_watchlist == "only" and s is None:
                r.append("Claude 추천 아님 (only 모드)")
        else:
            cand.tags["claude_listed"] = False
            cand.tags["claude_score"] = None
        cand.passed = not r
        return cand

    def scan(self, now: datetime, watchlist: Watchlist | None) -> list[Candidate]:
        """전체 평가 결과 (통과·탈락 모두). 통과 종목이 앞, 우선순위 순."""
        c = self.cfg
        wl = watchlist if c.claude_watchlist != "off" else None
        try:
            ranks = self.feed.turnover_rank(now)
        except Exception as e:      # noqa: BLE001
            log.warning("거래대금 순위 조회 실패: %s — watchlist 종목만 평가", e)
            ranks = []
        targets: dict[str, tuple[str, int | None]] = {}
        for item in ranks[: c.rank_top_n]:
            targets[item.code] = (item.name, item.rank)
        rank_of = {r.code: r.rank for r in ranks}
        if wl:
            for w in wl.candidates:
                targets.setdefault(w.code, (w.name, rank_of.get(w.code)))
        results = [self.evaluate(code, name, rank, now, wl) for code, (name, rank) in targets.items()]

        def priority(cand: Candidate):
            score = cand.tags.get("claude_score")
            return (not cand.passed, -(score if score is not None else -1), cand.rank or 999)

        results.sort(key=priority)
        return results

    def select(self, now: datetime, watchlist: Watchlist | None) -> list[Candidate]:
        return [c for c in self.scan(now, watchlist) if c.passed]

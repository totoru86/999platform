"""시세 데이터 공급: 실시간(KIS) / 리플레이(수집한 분봉 CSV, 백테스트용)."""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .kis_client import KisApiError, KisClient
from .market import KST, add_ticks, hhmmss, round_to_tick
from .models import Bar, Quote, RankItem

log = logging.getLogger(__name__)


def _minute_floor(now: datetime) -> str:
    return now.strftime("%H%M") + "00"


class LiveFeed:
    """KIS REST 폴링. 분봉은 종목별 캐시 후 최근 30개씩 갱신."""

    def __init__(self, client: KisClient, market: str = "J", exclude_flags: str = "1111111101"):
        self.client = client
        self.market = market
        self.exclude_flags = exclude_flags
        self._bars: dict[str, dict[str, Bar]] = {}
        self._bars_fetched_minute: dict[str, str] = {}
        self._quotes: dict[str, Quote] = {}

    def turnover_rank(self, now: datetime) -> list[RankItem]:
        return self.client.get_turnover_rank(self.market, self.exclude_flags)

    def quote(self, code: str, now: datetime) -> Quote:
        q = self.client.get_quote(code, "J")
        self._quotes[code] = q
        return q

    def completed_bars(self, code: str, now: datetime) -> list[Bar]:
        """현재 진행 중인 봉을 제외한 당일 완성 1분봉 (오름차순). 1분에 한 번만 API 호출."""
        minute = _minute_floor(now)
        today = now.strftime("%Y%m%d")
        cache = self._bars.setdefault(code, {})
        if self._bars_fetched_minute.get(code) != minute:
            if not cache:
                bars = self.client.get_minute_bars_since(code, "090000", hhmmss(now))
            else:
                bars = self.client.get_minute_bars(code, hhmmss(now))
            for b in bars:
                if b.date == today:
                    cache[b.key] = b
            self._bars_fetched_minute[code] = minute
        return sorted((b for b in cache.values() if b.time < minute), key=lambda b: b.key)

    def exit_probe(self, code: str, now: datetime) -> tuple[float, float, float, Quote]:
        q = self.quote(code, now)
        return q.price, q.price, q.price, q

    def market_open_today(self, now: datetime) -> bool:
        """휴장일 판별: 삼성전자 당일 분봉 존재 여부."""
        try:
            bars = self.client.get_minute_bars("005930", hhmmss(now))
        except KisApiError:
            return False
        return any(b.date == now.strftime("%Y%m%d") for b in bars)


class ReplayFeed:
    """collect 명령으로 저장한 분봉을 시간순으로 재생 (백테스트).
    한계: 장중 외국인/프로그램 수급은 재현 불가, 수집 종목군이 '장 마감 기준 거래대금 상위'라 선택편향 존재."""

    def __init__(self, day_dir: Path):
        self.day = day_dir.name                       # YYYYMMDD
        meta_path = day_dir / "meta.json"
        self.meta: dict = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        self.bars: dict[str, list[Bar]] = {}
        for f in sorted(day_dir.glob("*.csv")):
            with open(f, encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.bars[f.stem] = [Bar(self.day, r["time"], int(r["open"]), int(r["high"]), int(r["low"]),
                                     int(r["close"]), int(r["volume"])) for r in rows]

    def _upto(self, code: str, now: datetime) -> list[Bar]:
        minute = _minute_floor(now)
        return [b for b in self.bars.get(code, []) if b.time < minute]

    def completed_bars(self, code: str, now: datetime) -> list[Bar]:
        return self._upto(code, now)

    def quote(self, code: str, now: datetime) -> Quote:
        bars = self._upto(code, now)
        m = self.meta.get(code, {})
        prev_close = m.get("prev_close") or (bars[0].open if bars else 0)
        if not bars:
            return Quote(code=code, price=prev_close, name=m.get("name", ""), ts=now)
        last = bars[-1]
        vol = sum(b.volume for b in bars)
        turnover = int(sum((b.high + b.low + b.close) / 3 * b.volume for b in bars))
        upper = round_to_tick(prev_close * 1.3, "down") if prev_close else 0
        return Quote(
            code=code, price=last.close, open=bars[0].open, high=max(b.high for b in bars),
            low=min(b.low for b in bars), change_pct=(last.close / prev_close - 1) * 100 if prev_close else 0,
            acc_volume=vol, acc_turnover=turnover, upper_limit=upper,
            market_cap_eok=m.get("market_cap_eok", 0), w52_high=m.get("w52_high", 0),
            program_net_qty=1, foreign_net_qty=1,     # 재현 불가 → 필터 통과로 간주
            name=m.get("name", ""), ts=now)

    def turnover_rank(self, now: datetime) -> list[RankItem]:
        items = []
        for code in self.bars:
            q = self.quote(code, now)
            items.append(RankItem(0, code, q.name, q.price, q.change_pct, q.acc_volume, q.acc_turnover))
        items.sort(key=lambda r: r.acc_turnover, reverse=True)
        for i, r in enumerate(items, 1):
            r.rank = i
        return items

    def exit_probe(self, code: str, now: datetime) -> tuple[float, float, float, Quote]:
        """직전 완성봉의 저가/고가/종가로 손절·익절 터치 여부 판정."""
        bars = self._upto(code, now)
        q = self.quote(code, now)
        if not bars:
            return q.price, q.price, q.price, q
        b = bars[-1]
        return b.low, b.high, b.close, q

    def market_open_today(self, now: datetime) -> bool:
        return bool(self.bars)

    def minutes(self, start: str = "0900", end: str = "1530"):
        """리플레이 시계: 해당일 09:00~15:30 매 분."""
        t = datetime.strptime(self.day + start, "%Y%m%d%H%M").replace(tzinfo=KST)
        stop = datetime.strptime(self.day + end, "%Y%m%d%H%M").replace(tzinfo=KST)
        while t <= stop:
            yield t
            t += timedelta(minutes=1)


def sim_fill_price(ref_price: float, side: str, slippage_ticks: int) -> int:
    p = int(ref_price)
    return add_ticks(p, slippage_ticks if side == "buy" else -slippage_ticks)

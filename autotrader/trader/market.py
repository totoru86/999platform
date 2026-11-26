"""한국 주식시장 공통 규칙: 한국시간, 호가단위, 거래비용."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

# 한국은 서머타임이 없으므로 고정 오프셋 사용 (Windows 에서 tzdata 불필요)
KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    return datetime.now(KST)


def parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


def hhmmss(t: datetime | time) -> str:
    return t.strftime("%H%M%S")


def tick_size(price: float) -> int:
    """KRX 호가가격단위 (2023-01-25 개편, 코스피·코스닥 공통)."""
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def round_to_tick(price: float, direction: str = "down") -> int:
    """가격을 호가단위에 맞춤. direction: down | up | nearest"""
    tick = tick_size(price)
    if direction == "up":
        result = math.ceil(price / tick) * tick
    elif direction == "nearest":
        result = round(price / tick) * tick
    else:
        result = math.floor(price / tick) * tick
    # 경계값에서 호가단위가 바뀌는 경우 한 번 더 보정
    if result % tick_size(result) != 0:
        result = round_to_tick(result, direction)
    return int(result)


def add_ticks(price: int, n: int) -> int:
    """호가 n칸 이동 (n<0 이면 아래로). 구간 경계에서 호가단위 변화를 반영."""
    p = round_to_tick(price, "nearest")
    step = 1 if n > 0 else -1
    for _ in range(abs(n)):
        if step > 0:
            p += tick_size(p)
        else:
            p -= tick_size(p - 1)
    return max(p, 1)


@dataclass(frozen=True)
class CostModel:
    commission_pct: float = 0.015   # 매수·매도 각각
    sell_tax_pct: float = 0.20      # 매도 시

    def buy_cost(self, amount: float) -> float:
        return amount * self.commission_pct / 100

    def sell_cost(self, amount: float) -> float:
        return amount * (self.commission_pct + self.sell_tax_pct) / 100

    def round_trip_pct(self) -> float:
        return self.commission_pct * 2 + self.sell_tax_pct

    def net_pnl(self, entry_price: float, exit_price: float, qty: int) -> tuple[float, float]:
        """(순손익, 총비용) 반환."""
        buy_amt = entry_price * qty
        sell_amt = exit_price * qty
        costs = self.buy_cost(buy_amt) + self.sell_cost(sell_amt)
        return sell_amt - buy_amt - costs, costs

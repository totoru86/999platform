"""주문 실행 계층: KIS(모의/실전) 또는 시뮬레이션(드라이런·백테스트)."""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field

from .feed import sim_fill_price
from .kis_client import KisClient
from .market import CostModel
from .models import AccountSummary, Holding, OrderAck, OrderStatus

log = logging.getLogger(__name__)


class KisBroker:
    """실제 KIS 계좌로 주문. 모의투자/실전은 KisClient 자격증명으로 결정."""

    def __init__(self, client: KisClient):
        self.client = client
        self.name = "KIS-모의투자" if client.creds.is_paper else "KIS-실전"

    def account(self) -> AccountSummary:
        return self.client.get_balance()

    def buyable_cash(self, code: str, price: int) -> int:
        amt, _ = self.client.get_buyable(code, price)
        return amt

    def buy_limit(self, code: str, qty: int, price: int, ref_price: float) -> OrderAck:
        return self.client.place_order("buy", code, qty, price)

    def sell_market(self, code: str, qty: int, ref_price: float) -> OrderAck:
        return self.client.place_order("sell", code, qty, None)

    def cancel(self, ack: OrderAck) -> None:
        self.client.cancel_order(ack)

    def order_status(self, ack: OrderAck) -> OrderStatus | None:
        target = ack.order_no.lstrip("0")
        for o in self.client.get_today_orders(ack.order_no):
            if o.order_no.lstrip("0") == target:
                return o
        return None


@dataclass
class _SimPos:
    qty: int
    avg: float
    name: str = ""


@dataclass
class SimBroker:
    """즉시 체결 가정 시뮬레이터. 슬리피지·수수료·세금 반영."""
    cash: float
    costs: CostModel
    slippage_ticks: int = 1
    name: str = "시뮬레이션"
    positions: dict[str, _SimPos] = field(default_factory=dict)
    orders: dict[str, OrderStatus] = field(default_factory=dict)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))

    def account(self) -> AccountSummary:
        holdings = [Holding(c, p.name, p.qty, p.qty, p.avg, int(p.avg), 0) for c, p in self.positions.items()]
        equity = self.cash + sum(p.qty * p.avg for p in self.positions.values())
        return AccountSummary(int(self.cash), int(self.cash), int(equity), int(equity), holdings)

    def buyable_cash(self, code: str, price: int) -> int:
        return int(self.cash)

    def _ack(self) -> OrderAck:
        n = f"{next(self._seq):010d}"
        return OrderAck(n, "SIM", "")

    def buy_limit(self, code: str, qty: int, price: int, ref_price: float) -> OrderAck:
        ack = self._ack()
        fill = sim_fill_price(ref_price, "buy", self.slippage_ticks)
        if fill > price:          # 지정가보다 비싸면 미체결
            self.orders[ack.order_no] = OrderStatus(ack.order_no, code, "buy", qty, 0, 0, qty, False)
            return ack
        cost = fill * qty + self.costs.buy_cost(fill * qty)
        if cost > self.cash:
            raise RuntimeError("시뮬레이션 현금 부족")
        self.cash -= cost
        p = self.positions.get(code)
        if p:
            p.avg = (p.avg * p.qty + fill * qty) / (p.qty + qty)
            p.qty += qty
        else:
            self.positions[code] = _SimPos(qty, fill)
        self.orders[ack.order_no] = OrderStatus(ack.order_no, code, "buy", qty, qty, fill, 0, False)
        return ack

    def sell_market(self, code: str, qty: int, ref_price: float) -> OrderAck:
        ack = self._ack()
        p = self.positions.get(code)
        if not p or p.qty < qty:
            raise RuntimeError(f"시뮬레이션 보유수량 부족 {code}")
        fill = sim_fill_price(ref_price, "sell", self.slippage_ticks)
        amt = fill * qty
        self.cash += amt - self.costs.sell_cost(amt)
        p.qty -= qty
        if p.qty == 0:
            del self.positions[code]
        self.orders[ack.order_no] = OrderStatus(ack.order_no, code, "sell", qty, qty, fill, 0, False)
        return ack

    def cancel(self, ack: OrderAck) -> None:
        o = self.orders.get(ack.order_no)
        if o and o.remaining_qty:
            o.cancelled = True

    def order_status(self, ack: OrderAck) -> OrderStatus | None:
        return self.orders.get(ack.order_no)

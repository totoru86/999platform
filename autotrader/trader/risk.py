"""하드 리스크 관리. 어떤 신호(Claude 포함)도 이 한도를 넘을 수 없습니다."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import RiskConfig
from .market import CostModel


@dataclass
class RiskState:
    start_equity: float
    realized_pnl: float = 0.0
    entries_today: int = 0
    consecutive_losses: int = 0
    wins: int = 0
    losses: int = 0
    halted: str = ""            # 신규 진입 중단 사유 (빈 문자열이면 정상)
    flatten_requested: bool = False
    size_multiplier: float = 1.0
    api_errors: int = 0


class RiskManager:
    def __init__(self, cfg: RiskConfig, costs: CostModel, start_equity: float):
        if start_equity <= 0:
            raise ValueError("시작 자산이 0 이하입니다 — 계좌 잔고를 확인하세요")
        self.cfg = cfg
        self.costs = costs
        self.state = RiskState(start_equity=start_equity)

    # ── Claude watchlist 의 시장 판단 반영 (축소만 가능) ──
    def apply_market_view(self, view: str) -> None:
        if view == "risk_off":
            self.state.size_multiplier = max(0.0, min(1.0, self.cfg.risk_off_size_multiplier))
            if self.state.size_multiplier == 0:
                self.halt("Claude 시장판단 risk_off (매매 중지 설정)")
        else:
            self.state.size_multiplier = 1.0

    def halt(self, reason: str, flatten: bool = False) -> None:
        if not self.state.halted:
            self.state.halted = reason
        if flatten:
            self.state.flatten_requested = True

    def can_open(self, open_positions: int, entries_for_symbol: int, max_per_symbol: int) -> tuple[bool, str]:
        s, c = self.state, self.cfg
        if s.halted:
            return False, f"진입중단: {s.halted}"
        if open_positions >= c.max_open_positions:
            return False, f"동시보유 한도 {c.max_open_positions}"
        if s.entries_today >= c.max_trades_per_day:
            return False, f"일 매매횟수 한도 {c.max_trades_per_day}"
        if entries_for_symbol >= max_per_symbol:
            return False, "종목당 진입 한도"
        return True, ""

    def position_size(self, entry: float, stop: float, cash_available: float) -> tuple[int, str]:
        """손실 한도 기준 수량. (수량, 제한사유)"""
        s, c = self.state, self.cfg
        per_share_risk = entry - stop
        if per_share_risk <= 0 or entry <= 0:
            return 0, "손절가가 진입가 이상"
        eq = s.start_equity
        mult = s.size_multiplier
        limits = {
            "1회 손실한도": math.floor(eq * c.risk_per_trade_pct / 100 * mult / per_share_risk),
            "종목 비중한도": math.floor(eq * c.max_position_pct / 100 * mult / entry),
            "1회 주문금액한도": math.floor(c.max_order_krw * mult / entry),
            "가용현금": math.floor(cash_available * 0.98 / (entry * (1 + self.costs.commission_pct / 100))),
        }
        reason = min(limits, key=limits.get)
        qty = max(0, limits[reason])
        return qty, reason

    def on_entry(self) -> None:
        self.state.entries_today += 1

    def on_trade_closed(self, net_pnl: float) -> None:
        s, c = self.state, self.cfg
        s.realized_pnl += net_pnl
        if net_pnl > 0:
            s.wins += 1
            s.consecutive_losses = 0
        else:
            s.losses += 1
            s.consecutive_losses += 1
        if s.consecutive_losses >= c.max_consecutive_losses:
            self.halt(f"연속 {s.consecutive_losses}패")
        if s.realized_pnl >= s.start_equity * c.daily_profit_target_pct / 100:
            self.halt(f"일 목표수익 달성 (+{s.realized_pnl:,.0f}원) — 오늘은 여기까지")

    def check_drawdown(self, unrealized_pnl: float) -> None:
        s, c = self.state, self.cfg
        total = s.realized_pnl + unrealized_pnl
        if total <= -s.start_equity * c.max_daily_loss_pct / 100:
            self.halt(f"일 손실한도 도달 ({total:,.0f}원)", flatten=c.flatten_on_daily_loss)

    def on_api_error(self) -> None:
        self.state.api_errors += 1
        if self.state.api_errors >= self.cfg.max_api_errors:
            self.halt(f"API 연속 오류 {self.state.api_errors}회")

    def on_api_ok(self) -> None:
        self.state.api_errors = 0

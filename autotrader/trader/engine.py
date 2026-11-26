"""매매 엔진: 시간대별 상태 관리 → 종목선정 → 진입 → 청산 → 일일 마감.

실시간(paper/live/dry_run)과 백테스트가 같은 Engine.step(now) 코드를 사용합니다.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import TradingConfig
from .journal import Journal, TradeRecord
from .kis_client import KisApiError
from .market import add_ticks, now_kst, round_to_tick
from .models import OrderAck
from .notifier import Notifier
from .risk import RiskManager
from .screener import Candidate, Screener
from .strategy import OpeningRange, OrbVwapStrategy, Position, opening_range
from .watchlist import Watchlist

log = logging.getLogger(__name__)


@dataclass
class PendingOrder:
    ack: OrderAck
    side: str               # buy | sell
    code: str
    qty: int
    price: float            # 지정가(매수) 또는 기준가(매도)
    placed_at: str          # ISO
    cancel_requested: bool = False


def _hhmm(now: datetime) -> str:
    return now.strftime("%H:%M")


class Engine:
    def __init__(self, cfg: TradingConfig, feed, broker, journal: Journal, notifier: Notifier,
                 run_mode: str, data_dir: Path | None = None, watchlist_loader=None,
                 persist: bool = True):
        self.cfg = cfg
        self.feed = feed
        self.broker = broker
        self.journal = journal
        self.notify = notifier
        self.run_mode = run_mode
        self.data_dir = data_dir
        self.watchlist_loader = watchlist_loader
        self.persist = persist and data_dir is not None
        self.costs = cfg.costs.model()
        self.strategy = OrbVwapStrategy(cfg.strategy, self.costs)
        self.screener = Screener(cfg.universe, feed)
        self.or_end = cfg.schedule.opening_range_end.replace(":", "") + "00"

        self.day: str | None = None
        self.risk: RiskManager | None = None
        self.watchlist: Watchlist | None = None
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, PendingOrder] = {}
        self.entries_by_symbol: Counter = Counter()
        self.candidates: list[Candidate] = []
        self.or_cache: dict[str, OpeningRange | None] = {}
        self.last_scan: datetime | None = None
        self.last_entry_bar: dict[str, str] = {}
        self.last_exit_bar: dict[str, str] = {}
        self.closed: list[TradeRecord] = []
        self._wl_sig: tuple | None = None
        self.done = False

    # ───────────────────────── 경로/상태 저장 ─────────────────────────
    @property
    def stop_file(self) -> Path | None:
        return self.data_dir / "STOP" if self.data_dir else None

    @property
    def state_file(self) -> Path | None:
        return self.data_dir / "state" / f"{self.run_mode}.json" if self.data_dir else None

    def save_state(self) -> None:
        if not self.persist or not self.risk:
            return
        st = {
            "day": self.day,
            "risk": asdict(self.risk.state),
            "entries_by_symbol": dict(self.entries_by_symbol),
            "positions": [asdict(p) for p in self.positions.values()],
            "pending": [{**asdict(o), "ack": asdict(o.ack)} for o in self.pending.values()],
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    def _restore_state(self) -> bool:
        if not self.persist or not self.state_file.exists():
            return False
        try:
            st = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if st.get("day") != self.day:
            return False
        for k, v in st["risk"].items():
            setattr(self.risk.state, k, v)
        self.entries_by_symbol = Counter(st.get("entries_by_symbol", {}))
        for p in st.get("positions", []):
            pos = Position(**p)
            if pos.state == "exiting":
                pos.state = "open"          # 미확인 매도는 재확인 후 재시도
            self.positions[pos.code] = pos
        for o in st.get("pending", []):
            ack = OrderAck(**o.pop("ack"))
            self.pending[ack.order_no] = PendingOrder(ack=ack, **o)
        self._event("info", "restore", f"오늘 상태 복구: 포지션 {len(self.positions)}, 미체결 {len(self.pending)}")
        return True

    def _event(self, level: str, kind: str, message: str, data: dict | None = None) -> None:
        {"warn": log.warning, "error": log.error}.get(level, log.info)(message)
        self.journal.event(self.run_mode, level, kind, message, data)

    # ───────────────────────── 하루 시작 ─────────────────────────
    def start_day(self, now: datetime) -> None:
        self.day = now.strftime("%Y-%m-%d")
        acct = self.broker.account()
        equity = acct.equity
        cap = self.cfg.risk.capital_krw
        base = min(cap, equity) if cap and equity else (cap or equity)
        self.risk = RiskManager(self.cfg.risk, self.costs, base)
        restored = self._restore_state()
        if restored:
            self._reconcile_with_account(acct)
        self._reload_watchlist(now, announce_missing=True)
        self.notify.send(f"[{self.run_mode}] 자동매매 시작 {self.day} | 기준자금 {base:,.0f}원"
                         f" | 브로커 {getattr(self.broker, 'name', '')}")
        self.save_state()

    def _reload_watchlist(self, now: datetime, announce_missing: bool = False) -> None:
        """Claude watchlist 를 (재)로딩. 장중에 Claude 가 파일을 갱신해도 다음 종목 재선정 때 반영."""
        if not self.watchlist_loader:
            return
        w, msgs = self.watchlist_loader(now.date())
        if w is None:
            if announce_missing or self.watchlist is not None:
                for m in msgs:
                    self._event("warn", "watchlist", m)
            if self.watchlist is not None:
                self._event("warn", "watchlist", "새 watchlist 가 유효하지 않아 이전 것을 유지합니다")
            return
        sig = (w.market_view, tuple(w.codes), tuple(sorted(w.avoid)),
               tuple(w.score(c) for c in w.codes))
        if sig == self._wl_sig:
            return
        self._wl_sig = sig
        self.watchlist = w
        self.risk.apply_market_view(w.market_view)
        self._event("info", "watchlist", f"Claude watchlist 적용: 추천 {len(w.candidates)} / 회피 "
                    f"{len(w.avoid)} / 시장판단 {w.market_view} (사이즈 배수 {self.risk.state.size_multiplier})")

    def _reconcile_with_account(self, acct) -> None:
        held = {h.code: h.qty for h in acct.holdings}
        for code, pos in list(self.positions.items()):
            if pos.state == "entering":
                continue
            have = held.get(code, 0)
            if have < pos.qty:
                self._event("warn", "reconcile", f"{code} 계좌 보유 {have}주 < 엔진 기록 {pos.qty}주 → 보정")
                pos.qty = have
                if have == 0:
                    del self.positions[code]

    # ───────────────────────── 메인 스텝 ─────────────────────────
    def step(self, now: datetime) -> None:
        if self.done:
            return
        if self.day != now.strftime("%Y-%m-%d"):
            self.start_day(now)
        hhmm = _hhmm(now)
        if hhmm < "09:00":
            return
        risk = self.risk
        if self.stop_file and self.stop_file.exists() and not risk.state.flatten_requested:
            risk.halt("수동 정지 (STOP 파일)", flatten=True)
            self._event("warn", "halt", "STOP 파일 감지 — 신규 진입 중단 및 전량 청산")
            self.notify.send("STOP 파일 감지 — 신규 진입 중단, 보유 종목 청산")

        try:
            self._process_pending(now)
            self._manage_positions(now)
            force = hhmm >= self.cfg.schedule.force_exit
            if force or risk.state.flatten_requested:
                self._flatten_all(now, "force_exit" if force else "risk_flatten")
            if force:
                if not self.positions and not self.pending:
                    self.end_day(now)
                elif hhmm >= "15:29":
                    self._event("error", "eod", f"장 마감 임박인데 미정리 포지션 {list(self.positions)} — 수동 확인 필요")
                    self.notify.send(f"⚠️ 미정리 포지션 {list(self.positions)} — HTS/MTS 에서 직접 확인하세요")
                    self.end_day(now)
                return
            self._maybe_scan(now)
            if self._entry_window(now) and not risk.state.halted:
                self._scan_entries(now)
            risk.on_api_ok()
        except KisApiError as e:
            risk.on_api_error()
            self._event("error", "api", f"API 오류: {e}")
        finally:
            self.save_state()

    def _entry_window(self, now: datetime) -> bool:
        s = self.cfg.schedule
        hhmm = _hhmm(now)
        if not s.entry_start <= hhmm < s.entry_end:
            return False
        return not any(a <= hhmm < b for a, b in s.no_entry_windows)

    # ───────────────────────── 종목 선정 ─────────────────────────
    def _maybe_scan(self, now: datetime) -> None:
        if _hhmm(now) < self.cfg.schedule.opening_range_end:
            return
        refresh = self.cfg.universe.refresh_minutes * 60
        if self.last_scan and (now - self.last_scan).total_seconds() < refresh:
            return
        self.last_scan = now
        self._reload_watchlist(now)
        self.candidates = self.screener.select(now, self.watchlist)
        names = ", ".join(f"{c.name or c.code}" for c in self.candidates[:8])
        self._event("info", "scan", f"{_hhmm(now)} 후보 {len(self.candidates)}종목: {names}",
                    {"candidates": [c.summary() for c in self.candidates]})

    # ───────────────────────── 진입 ─────────────────────────
    def _active_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.state in ("entering", "open", "exiting"))

    def _scan_entries(self, now: datetime) -> None:
        st = self.cfg.strategy
        for cand in self.candidates:
            code = cand.code
            if code in self.positions:
                continue
            ok, why = self.risk.can_open(self._active_count(), self.entries_by_symbol[code],
                                         st.max_entries_per_symbol)
            if not ok:
                if "종목당" in why:
                    continue
                return
            bars = self.feed.completed_bars(code, now)
            if len(bars) < 2 or bars[-1].key == self.last_entry_bar.get(code):
                continue
            self.last_entry_bar[code] = bars[-1].key
            orng = self.or_cache.get(code)
            if orng is None:
                orng = opening_range(bars, self.or_end)
                self.or_cache[code] = orng
            if orng is None:
                continue
            level = orng.high * (1 + st.breakout_buffer_pct / 100)
            if not (bars[-1].close > level >= bars[-2].close):
                continue                        # 돌파 봉이 아니면 시세 재조회 생략 (API 절약)
            quote = self.feed.quote(code, now)
            sig, reason = self.strategy.check_entry(code, bars, quote, orng, self.or_end)
            if not sig:
                self._event("info", "skip", f"{cand.name or code} 돌파했지만 진입 보류: {reason}")
                continue
            self._enter(cand, sig, quote, now)

    def _enter(self, cand: Candidate, sig, quote, now: datetime) -> None:
        st = self.cfg.strategy
        code = cand.code
        limit = add_ticks(quote.price, st.entry_slippage_ticks)
        if quote.upper_limit:
            limit = min(limit, quote.upper_limit)
        try:
            cash = self.broker.buyable_cash(code, limit)
        except KisApiError as e:
            self._event("error", "api", f"매수가능금액 조회 실패 {code}: {e}")
            return
        qty, limiter = self.risk.position_size(limit, sig.stop, cash)
        if qty < 1:
            self._event("info", "skip", f"{cand.name or code} 수량 0 (제한: {limiter})")
            return
        try:
            ack = self.broker.buy_limit(code, qty, limit, quote.price)
        except KisApiError as e:
            self.risk.on_api_error()
            self._event("error", "order", f"매수 주문 실패 {code}: {e}")
            return
        self.entries_by_symbol[code] += 1
        wl_item = next((w for w in (self.watchlist.candidates if self.watchlist else []) if w.code == code), None)
        tags = {**cand.tags, **sig.tags, "rank": cand.rank,
                "news_freshness": wl_item.news_freshness if wl_item else None,
                "turnover_eok": round(quote.turnover_eok), "change_pct": quote.change_pct,
                "size_limiter": limiter}
        self.positions[code] = Position(
            code=code, name=cand.name or quote.name, qty=0, entry_price=limit,
            entry_time=now.isoformat(timespec="seconds"), stop=sig.stop, target=sig.target,
            initial_stop=sig.stop, peak=limit, state="entering", tags=tags, last_price=quote.price)
        self.pending[ack.order_no] = PendingOrder(ack, "buy", code, qty, limit, now.isoformat())
        self._event("info", "order", f"매수 주문 {cand.name or code} {qty}주 @{limit:,} "
                    f"(손절 {sig.stop:,} / 익절 {sig.target:,}, 제한요인 {limiter})", {"order_no": ack.order_no})

    # ───────────────────────── 주문 체결 확인 ─────────────────────────
    def _process_pending(self, now: datetime) -> None:
        for oid, po in list(self.pending.items()):
            status = self.broker.order_status(po.ack)
            age = (now - datetime.fromisoformat(po.placed_at)).total_seconds()
            pos = self.positions.get(po.code)
            if po.side == "buy":
                if pos is None:
                    del self.pending[oid]
                    continue
                if status and status.filled_qty > 0:
                    pos.qty = status.filled_qty
                    pos.entry_price = status.avg_price or po.price
                done = bool(status and status.done)
                if not done and age >= self.cfg.strategy.entry_timeout_seconds and not po.cancel_requested:
                    try:
                        self.broker.cancel(po.ack)
                    except KisApiError as e:
                        self._event("warn", "order", f"매수 취소 실패(이미 체결 가능) {po.code}: {e}")
                    po.cancel_requested = True
                    continue
                if done:
                    del self.pending[oid]
                    if pos.qty > 0:
                        self._on_entry_filled(pos)
                    else:
                        self._event("info", "order", f"{pos.name} 미체결 → 취소")
                        del self.positions[po.code]
            else:  # sell
                if status and status.done:
                    del self.pending[oid]
                    filled = status.filled_qty
                    if pos and filled > 0:
                        self._close_trade(pos, filled, status.avg_price or po.price, now)
                    if pos and pos.qty > 0:
                        pos.state = "open"          # 잔량 재매도
                elif age > 60 and not po.cancel_requested:
                    self._event("warn", "order", f"{po.code} 매도 60초 미체결 — 취소 후 재주문")
                    try:
                        self.broker.cancel(po.ack)
                    except KisApiError:
                        pass
                    po.cancel_requested = True

    def _on_entry_filled(self, pos: Position) -> None:
        st = self.cfg.strategy
        fill = pos.entry_price
        # 실제 체결가 기준으로 손절폭이 max_stop_pct 를 넘지 않게 보정
        pos.stop = max(pos.initial_stop, round_to_tick(fill * (1 - st.max_stop_pct / 100), "down"))
        pos.initial_stop = pos.stop
        pos.target = round_to_tick(fill * (1 + st.take_profit_pct / 100), "up")
        pos.peak = fill
        pos.state = "open"
        self.risk.on_entry()
        msg = (f"🟢 매수체결 {pos.name}({pos.code}) {pos.qty}주 @{fill:,.0f} | 손절 {pos.stop:,} "
               f"익절 {pos.target:,}")
        self._event("info", "fill", msg, pos.tags)
        self.notify.send(msg)

    # ───────────────────────── 보유 관리/청산 ─────────────────────────
    def _manage_positions(self, now: datetime) -> None:
        unrealized = 0.0
        for pos in list(self.positions.values()):
            if pos.state != "open" or pos.qty <= 0:
                unrealized += pos.unrealized(pos.last_price) if pos.qty else 0
                continue
            low, high, last, quote = self.feed.exit_probe(pos.code, now)
            pos.last_price = last
            bars = self.feed.completed_bars(pos.code, now)
            last_bar_close = None
            if bars and bars[-1].key != self.last_exit_bar.get(pos.code):
                self.last_exit_bar[pos.code] = bars[-1].key
                # VWAP 이탈은 '진입 이후에 완성된 봉' 종가로만 판단
                entry_key = datetime.fromisoformat(pos.entry_time).strftime("%Y%m%d%H%M00")
                if bars[-1].key >= entry_key:
                    last_bar_close = bars[-1].close
            res = self.strategy.check_exit(pos, low, high, last, quote.vwap, last_bar_close)
            if res:
                self._exit(pos, res[0], res[1], now)
            unrealized += pos.unrealized(last)
        self.risk.check_drawdown(unrealized)

    def _exit(self, pos: Position, reason: str, price_hint: float, now: datetime) -> None:
        if pos.state != "open" or pos.qty <= 0:
            return
        try:
            ack = self.broker.sell_market(pos.code, pos.qty, price_hint)
        except (KisApiError, RuntimeError) as e:
            pos.exit_attempts += 1
            self.risk.on_api_error()
            self._event("error", "order", f"매도 주문 실패 {pos.code} ({pos.exit_attempts}회): {e}")
            if pos.exit_attempts >= 3:
                self.notify.send(f"⚠️ {pos.name} 매도 3회 실패 — 직접 확인 필요: {e}")
            return
        pos.state = "exiting"
        pos.exit_reason = reason
        self.pending[ack.order_no] = PendingOrder(ack, "sell", pos.code, pos.qty, price_hint, now.isoformat())
        self._event("info", "order", f"매도 주문 {pos.name} {pos.qty}주 ({reason}, 기준가 {price_hint:,.0f})")

    def _flatten_all(self, now: datetime, reason: str) -> None:
        for po in self.pending.values():
            if po.side == "buy" and not po.cancel_requested:
                try:
                    self.broker.cancel(po.ack)
                except KisApiError:
                    pass
                po.cancel_requested = True
        for pos in list(self.positions.values()):
            if pos.state == "open" and pos.qty > 0:
                price = pos.last_price or pos.entry_price
                self._exit(pos, reason, price, now)

    def _close_trade(self, pos: Position, qty: int, exit_price: float, now: datetime) -> None:
        net, costs = self.costs.net_pnl(pos.entry_price, exit_price, qty)
        gross = (exit_price - pos.entry_price) * qty
        rec = TradeRecord(
            run_mode=self.run_mode, day=self.day, code=pos.code, name=pos.name,
            entry_time=pos.entry_time, entry_price=pos.entry_price, qty=qty,
            exit_time=now.isoformat(timespec="seconds"), exit_price=exit_price,
            exit_reason=pos.exit_reason or "unknown", gross_pnl=round(gross), costs=round(costs),
            net_pnl=round(net), ret_pct=round(net / (pos.entry_price * qty) * 100, 3), tags=pos.tags)
        self.journal.add_trade(rec)
        self.closed.append(rec)
        self.risk.on_trade_closed(net)
        pos.qty -= qty
        if pos.qty <= 0:
            pos.state = "closed"
            del self.positions[pos.code]
        icon = "🔵" if net > 0 else "🔴"
        msg = (f"{icon} 매도체결 {pos.name} {qty}주 @{exit_price:,.0f} ({rec.exit_reason}) "
               f"순손익 {net:+,.0f}원 ({rec.ret_pct:+.2f}%) | 오늘 누적 {self.risk.state.realized_pnl:+,.0f}원")
        self._event("info", "close", msg)
        self.notify.send(msg)
        if self.risk.state.halted:
            self.notify.send(f"⏸ 신규 진입 중단: {self.risk.state.halted}")

    # ───────────────────────── 하루 마감 ─────────────────────────
    def end_day(self, now: datetime) -> None:
        s = self.risk.state
        self.journal.save_daily(self.day, self.run_mode, s.start_equity, s.realized_pnl,
                                s.wins + s.losses, s.wins, s.losses, s.halted)
        pct = s.realized_pnl / s.start_equity * 100 if s.start_equity else 0
        msg = (f"📊 {self.day} 마감 [{self.run_mode}] 순손익 {s.realized_pnl:+,.0f}원 ({pct:+.2f}%) | "
               f"{s.wins}승 {s.losses}패" + (f" | 중단사유: {s.halted}" if s.halted else ""))
        self._event("info", "eod", msg)
        self.notify.send(msg)
        self.done = True


# ───────────────────────── 실행 루프 ─────────────────────────
def run_realtime(engine: Engine, poll_seconds: float, end_hhmm: str = "15:35") -> None:
    """장 시작 전부터 대기 → 장중 폴링 → 마감 후 종료."""
    while True:
        now = now_kst()
        if now.weekday() >= 5:
            log.info("주말 — 종료")
            return
        if _hhmm(now) >= end_hhmm:
            log.info("장 종료 시각 이후 — 종료")
            return
        if _hhmm(now) < "08:59":
            log.info("장 시작 대기 중 (%s)", _hhmm(now))
            time.sleep(min(60, max(1, poll_seconds)))
            continue
        break
    now = now_kst()
    if _hhmm(now) >= "09:01" and not engine.feed.market_open_today(now):
        log.info("오늘 거래 데이터가 없습니다 — 휴장일로 판단하고 종료")
        return
    checked_open = _hhmm(now) >= "09:01"
    while not engine.done:
        now = now_kst()
        if _hhmm(now) >= end_hhmm:
            if not engine.done and engine.risk:
                engine.end_day(now)
            break
        if not checked_open and _hhmm(now) >= "09:01":
            checked_open = True
            if not engine.feed.market_open_today(now):
                log.info("휴장일로 판단 — 종료")
                return
        try:
            engine.step(now)
        except Exception as e:  # noqa: BLE001 — 루프는 죽지 않게, 단 연속 오류는 리스크가 중단시킴
            log.exception("엔진 스텝 오류: %s", e)
            if engine.risk:
                engine.risk.on_api_error()
        time.sleep(poll_seconds)

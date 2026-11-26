"""분봉 수집(collect) + 리플레이 백테스트.

collect : 장 마감 후 당일 거래대금 상위 종목의 1분봉을 data/bars/YYYYMMDD/ 에 저장
backtest: 저장된 날짜들을 실전과 '같은 엔진 코드'로 재생해 성과 측정

주의(백테스트 한계)
- 수집 종목이 '장 마감 기준 거래대금 상위' → 장중엔 몰랐던 정보로 종목군을 고른 선택편향
- 장중 외국인/프로그램 수급은 재현 불가 → 해당 필터는 통과로 처리
- 체결은 1분봉 종가 + 슬리피지 가정. 실제 호가 공백·VI·부분체결은 반영 안 됨
→ 백테스트 수익은 '상한선'으로 보고, 반드시 모의투자로 검증하세요.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import date, datetime
from pathlib import Path

from .broker import SimBroker
from .config import TradingConfig
from .engine import Engine
from .feed import ReplayFeed
from .journal import Journal, build_report
from .kis_client import KisApiError, KisClient
from .market import now_kst
from .notifier import NullNotifier
from . import watchlist as wl

log = logging.getLogger(__name__)


def collect_day(client: KisClient, out_root: Path, top: int = 30, exclude_flags: str = "1111111101") -> Path:
    now = now_kst()
    day = now.strftime("%Y%m%d")
    out = out_root / day
    out.mkdir(parents=True, exist_ok=True)
    ranks = client.get_turnover_rank("J", exclude_flags)[:top]
    meta = {}
    for r in ranks:
        try:
            bars = client.get_minute_bars_since(r.code, "090000", "153100")
            q = client.get_quote(r.code)
        except KisApiError as e:
            log.warning("%s 수집 실패: %s", r.code, e)
            continue
        with open(out / f"{r.code}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "open", "high", "low", "close", "volume"])
            for b in bars:
                w.writerow([b.time, b.open, b.high, b.low, b.close, b.volume])
        meta[r.code] = {"name": r.name, "rank_eod": r.rank, "prev_close": q.base_price,
                        "market_cap_eok": q.market_cap_eok, "w52_high": q.w52_high,
                        "turnover_eok": round(q.turnover_eok, 1)}
        log.info("수집 %s %s (%d봉)", r.code, r.name, len(bars))
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def list_days(bars_root: Path, start: str | None = None, end: str | None = None) -> list[Path]:
    days = sorted(p for p in bars_root.glob("[0-9]" * 8) if p.is_dir())
    return [d for d in days if (not start or d.name >= start) and (not end or d.name <= end)]


def run_backtest(cfg: TradingConfig, days: list[Path], capital: float, data_dir: Path | None = None,
                 journal: Journal | None = None) -> tuple[dict, Journal]:
    journal = journal or Journal(":memory:")
    for day_dir in days:
        feed = ReplayFeed(day_dir)
        if not feed.bars:
            continue
        broker = SimBroker(cash=capital, costs=cfg.costs.model(), slippage_ticks=cfg.costs.slippage_ticks)
        loader = None
        if data_dir is not None:
            def loader(d: date, _dd=data_dir):
                return wl.load(_dd, d)
        engine = Engine(cfg, feed, broker, journal, NullNotifier(), "backtest",
                        data_dir=None, watchlist_loader=loader, persist=False)
        last = None
        for now in feed.minutes():
            engine.step(now)
            last = now
            if engine.done:
                break
        if not engine.done and engine.risk and last is not None:
            engine.end_day(last)
        log.info("백테스트 %s: 매매 %d건, 손익 %+,.0f원", day_dir.name, len(engine.closed),
                 engine.risk.state.realized_pnl if engine.risk else 0)
    rep = build_report(journal.trades("backtest"), journal.daily_rows("backtest"))
    return rep, journal


def parse_day(s: str) -> str:
    return datetime.strptime(s.replace("-", ""), "%Y%m%d").strftime("%Y%m%d")

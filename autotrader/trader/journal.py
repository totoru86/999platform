"""매매일지(SQLite) + 승률 통계 자동 집계."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_mode TEXT, day TEXT, code TEXT, name TEXT,
    entry_time TEXT, entry_price REAL, qty INTEGER,
    exit_time TEXT, exit_price REAL, exit_reason TEXT,
    gross_pnl REAL, costs REAL, net_pnl REAL, ret_pct REAL,
    tags TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, run_mode TEXT, level TEXT, kind TEXT, message TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS daily (
    day TEXT, run_mode TEXT, start_equity REAL, realized_pnl REAL, trades INTEGER,
    wins INTEGER, losses INTEGER, halted TEXT, PRIMARY KEY (day, run_mode)
);
"""


@dataclass
class TradeRecord:
    run_mode: str
    day: str
    code: str
    name: str
    entry_time: str
    entry_price: float
    qty: int
    exit_time: str
    exit_price: float
    exit_reason: str
    gross_pnl: float
    costs: float
    net_pnl: float
    ret_pct: float
    tags: dict


class Journal:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add_trade(self, t: TradeRecord) -> None:
        self.db.execute(
            "INSERT INTO trades (run_mode, day, code, name, entry_time, entry_price, qty, exit_time,"
            " exit_price, exit_reason, gross_pnl, costs, net_pnl, ret_pct, tags)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.run_mode, t.day, t.code, t.name, t.entry_time, t.entry_price, t.qty, t.exit_time,
             t.exit_price, t.exit_reason, t.gross_pnl, t.costs, t.net_pnl, t.ret_pct,
             json.dumps(t.tags, ensure_ascii=False)))
        self.db.commit()

    def event(self, run_mode: str, level: str, kind: str, message: str, data: dict | None = None) -> None:
        self.db.execute("INSERT INTO events (ts, run_mode, level, kind, message, data) VALUES (?,?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), run_mode, level, kind, message,
                         json.dumps(data or {}, ensure_ascii=False, default=str)))
        self.db.commit()

    def save_daily(self, day: str, run_mode: str, start_equity: float, realized: float, trades: int,
                   wins: int, losses: int, halted: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?)",
                        (day, run_mode, start_equity, realized, trades, wins, losses, halted))
        self.db.commit()

    def trades(self, run_mode: str | None = None, since: str | None = None, until: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM trades WHERE 1=1", []
        if run_mode:
            q += " AND run_mode=?"
            args.append(run_mode)
        if since:
            q += " AND day>=?"
            args.append(since)
        if until:
            q += " AND day<=?"
            args.append(until)
        rows = [dict(r) for r in self.db.execute(q + " ORDER BY id", args)]
        for r in rows:
            r["tags"] = json.loads(r["tags"] or "{}")
        return rows

    def daily_rows(self, run_mode: str | None = None, since: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM daily WHERE 1=1", []
        if run_mode:
            q += " AND run_mode=?"
            args.append(run_mode)
        if since:
            q += " AND day>=?"
            args.append(since)
        return [dict(r) for r in self.db.execute(q + " ORDER BY day", args)]


def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0}
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": n,
        "win_rate_pct": round(len(wins) / n * 100, 1),
        "net_pnl": round(sum(t["net_pnl"] for t in trades)),
        "avg_win": round(gross_win / len(wins)) if wins else 0,
        "avg_loss": round(-gross_loss / len(losses)) if losses else 0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy_per_trade": round(sum(t["net_pnl"] for t in trades) / n),
        "avg_ret_pct": round(sum(t["ret_pct"] for t in trades) / n, 3),
        "total_costs": round(sum(t["costs"] for t in trades)),
    }


def build_report(trades: list[dict], daily: list[dict] | None = None) -> dict:
    """전체 통계 + 조건별(태그별) 승률 비교."""
    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for t in trades:
        tags = t.get("tags") or {}
        groups["exit_reason"][t["exit_reason"]].append(t)
        groups["entry_hour"][str(t["entry_time"])[11:13] + "시"].append(t)   # ISO 시각 기준
        groups["claude_listed"][str(bool(tags.get("claude_listed")))].append(t)
        groups["near_52w_high"][str(bool(tags.get("near_52w_high")))].append(t)
        rank = tags.get("rank")
        bucket = "순위없음" if rank is None else ("1~3위" if rank <= 3 else "4~10위" if rank <= 10 else "11위~")
        groups["turnover_rank"][bucket].append(t)
        fresh = tags.get("news_freshness") or "unknown"
        groups["news_freshness"][fresh].append(t)
    by = {g: {k: _stats(v) for k, v in sorted(d.items())} for g, d in groups.items()}
    days = daily or []
    green = [d for d in days if d["realized_pnl"] > 0]
    return {
        "overall": _stats(trades),
        "days": {
            "count": len(days),
            "green_days": len(green),
            "green_day_pct": round(len(green) / len(days) * 100, 1) if days else None,
            "worst_day": min((d["realized_pnl"] for d in days), default=None),
            "best_day": max((d["realized_pnl"] for d in days), default=None),
        },
        "by": by,
    }


def format_report(rep: dict) -> str:
    o = rep["overall"]
    lines = ["═══ 매매 성과 리포트 ═══"]
    if not o.get("trades"):
        return "\n".join(lines + ["기록된 매매가 없습니다."])
    lines += [
        f"매매 {o['trades']}건 | 승률 {o['win_rate_pct']}% | 순손익 {o['net_pnl']:,}원 | 비용 {o['total_costs']:,}원",
        f"평균수익 {o['avg_win']:,}원 / 평균손실 {o['avg_loss']:,}원 | PF {o['profit_factor']} | 1회 기대값 {o['expectancy_per_trade']:,}원",
    ]
    d = rep["days"]
    if d["count"]:
        lines.append(f"거래일 {d['count']}일 중 수익일 {d['green_days']}일 ({d['green_day_pct']}%) | "
                     f"최고 {d['best_day']:,.0f}원 / 최악 {d['worst_day']:,.0f}원")
    names = {"exit_reason": "청산 사유", "entry_hour": "진입 시간대", "claude_listed": "Claude 추천 여부",
             "near_52w_high": "52주 신고가 근접", "turnover_rank": "거래대금 순위", "news_freshness": "뉴스 신선도"}
    for g, title in names.items():
        rows = rep["by"].get(g) or {}
        if not rows:
            continue
        lines.append(f"\n[{title}]")
        for k, s in rows.items():
            lines.append(f"  {k:<10} {s['trades']:>3}건  승률 {s['win_rate_pct']:>5}%  순손익 {s['net_pnl']:>10,}원")
    if o["trades"] < 30:
        lines.append(f"\n※ 표본 {o['trades']}건 — 30건 미만은 통계적으로 의미가 약합니다. 파라미터 변경은 신중히.")
    return "\n".join(lines)

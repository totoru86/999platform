"""명령줄 진입점:  python -m trader <명령>

조회(읽기 전용, Claude Code 가 장전 분석에 사용):
  check                  설정·접속·잔고 점검
  balance                계좌 잔고
  quote CODE [CODE..]    현재가/거래대금/수급 (JSON)
  index                  코스피·코스닥 지수
  rank [--top 30]        거래대금 순위 (JSON)
  screen                 현재 시각 기준 종목 필터 결과 (통과/탈락 사유, JSON)
  daily CODE [--days N]  일봉 기술지표 요약 (JSON)
  news CODE [--limit 30] 종목 뉴스·공시 제목 + 시각 (재탕 여부 판단용, JSON)
  watchlist validate|show|example

매매:
  run                    모의투자 자동매매 (KIS_MODE=paper)
  run --dry-run          주문 없이 실시간 시뮬레이션
  run --live             실전 매매 (KIS_MODE=live + AUTOTRADER_LIVE_CONFIRM 필요)
  stop / resume          비상정지 파일 생성/삭제 (실행 중 엔진이 신규진입 중단+전량청산)

분석:
  report [--days 30] [--mode paper] [--json]
  collect [--top 30]     장 마감 후 당일 분봉 수집 (백테스트용)
  backtest [--from YYYYMMDD] [--to YYYYMMDD] [--capital 5000000]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import watchlist as wl
from .backtest import collect_day, list_days, parse_day, run_backtest
from .broker import KisBroker, SimBroker
from .config import (LIVE_CONFIRM_PHRASE, as_dict, data_dir, load_secrets, load_trading_config)
from .engine import Engine, run_realtime
from .feed import LiveFeed
from .indicators import summarize_daily
from .journal import Journal, build_report, format_report
from .kis_client import KisApiError, KisClient
from .market import now_kst
from .notifier import Notifier
from .screener import Screener

log = logging.getLogger("trader")


def setup_logging(verbose: bool = False) -> None:
    logs = data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s | %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr),
                                       logging.FileHandler(logs / f"{now_kst():%Y%m%d}.log", encoding="utf-8")]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, handlers=handlers)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


class Ctx:
    def __init__(self, args):
        self.args = args
        self.cfg = load_trading_config(getattr(args, "config", None))
        self.secrets = load_secrets()
        self._trade = self._data = None

    def _client(self, creds) -> KisClient:
        a = self.cfg.api
        return KisClient(creds, data_dir() / "tokens",
                         rate_per_sec=a.rate_per_sec_paper if creds.is_paper else a.rate_per_sec_real,
                         timeout=a.timeout_seconds)

    def trade_client(self) -> KisClient:
        if self._trade is None:
            c = self.secrets.trade
            if c is None:
                sys.exit(f"❌ {self.secrets.mode} 계좌 앱키가 .env 에 없습니다 (.env.example 참고)")
            if c.problems():
                sys.exit("❌ " + " / ".join(c.problems()))
            self._trade = self._client(c)
        return self._trade

    def data_client(self) -> KisClient:
        if self._data is None:
            c = self.secrets.data
            if c is None:
                sys.exit("❌ 시세 조회용 앱키가 없습니다 (.env 의 KIS_PAPER_* 또는 KIS_REAL_*)")
            if self.secrets.trade is not None and c == self.secrets.trade:
                self._data = self.trade_client()
            else:
                self._data = self._client(c)
        return self._data


# ───────────────────────── 조회 명령 ─────────────────────────
def cmd_check(ctx: Ctx) -> int:
    ok = True
    errs = ctx.cfg.validate()
    print(f"설정 파일 검사: {'OK' if not errs else '문제 있음'}")
    for e in errs:
        print("  -", e)
        ok = False
    s = ctx.secrets
    print(f"KIS_MODE = {s.mode}")
    print(f"  주문 계좌: {s.trade.masked() if s.trade else '없음'}")
    print(f"  시세 조회: {s.data.masked() if s.data else '없음'} 서버")
    if s.mode == "live":
        print(f"  실전 확인문구: {'설정됨' if s.live_confirmed else '미설정 (run --live 불가)'}")
    print(f"  텔레그램 알림: {'설정됨' if s.telegram_token and s.telegram_chat_id else '미설정'}")
    try:
        acct = ctx.trade_client().get_balance()
        print(f"잔고 조회 OK — 예수금 {acct.cash:,}원 / 총평가 {acct.total_eval:,}원 / 보유 {len(acct.holdings)}종목")
    except KisApiError as e:
        print(f"❌ 잔고 조회 실패: {e}")
        ok = False
    try:
        q = ctx.data_client().get_quote("005930")
        print(f"시세 조회 OK — 삼성전자 {q.price:,}원 ({q.change_pct:+.2f}%), 거래대금 {q.turnover_eok:,.0f}억")
    except KisApiError as e:
        print(f"❌ 시세 조회 실패: {e}")
        ok = False
    try:
        ranks = ctx.data_client().get_turnover_rank("J", ctx.cfg.universe.exclude_flags)
        print(f"거래대금 순위 OK — 1위 {ranks[0].name if ranks else '-'} (총 {len(ranks)}종목)")
    except KisApiError as e:
        print(f"⚠️ 거래대금 순위 조회 실패: {e}\n   → 모의투자 서버는 순위 조회가 제한될 수 있습니다. "
              f".env 에 KIS_REAL_* 앱키를 넣으면 시세는 실전 서버에서 조회합니다.")
    print("\n결과:", "✅ 준비 완료" if ok else "❌ 위 문제를 해결하세요")
    return 0 if ok else 1


def cmd_balance(ctx: Ctx) -> int:
    a = ctx.trade_client().get_balance()
    out({"cash": a.cash, "d2_cash": a.d2_cash, "total_eval": a.total_eval, "net_asset": a.net_asset,
         "holdings": [h.__dict__ for h in a.holdings]})
    return 0


def cmd_quote(ctx: Ctx) -> int:
    c = ctx.data_client()
    res = []
    for code in ctx.args.codes:
        q = c.get_quote(code)
        d = {k: v for k, v in q.__dict__.items() if k != "ts"}
        d["vwap"] = round(q.vwap)
        d["turnover_eok"] = round(q.turnover_eok, 1)
        res.append(d)
    out(res)
    return 0


def cmd_index(ctx: Ctx) -> int:
    c = ctx.data_client()
    out([{"name": n, **c.get_index(code)} for n, code in (("코스피", "0001"), ("코스닥", "1001"),
                                                            ("코스피200", "2001"))])
    return 0


def cmd_rank(ctx: Ctx) -> int:
    ranks = ctx.data_client().get_turnover_rank(ctx.args.market or ctx.cfg.universe.rank_market,
                                               ctx.cfg.universe.exclude_flags)
    out([{**r.__dict__, "turnover_eok": round(r.turnover_eok, 1)} for r in ranks[: ctx.args.top]])
    return 0


def cmd_screen(ctx: Ctx) -> int:
    feed = LiveFeed(ctx.data_client(), ctx.cfg.universe.rank_market, ctx.cfg.universe.exclude_flags)
    today = now_kst().date()
    w, msgs = wl.load(data_dir(), today)
    res = Screener(ctx.cfg.universe, feed).scan(now_kst(), w)
    out({"time": now_kst().isoformat(timespec="seconds"), "watchlist": msgs or "적용",
         "passed": [c.summary() for c in res if c.passed],
         "rejected": [c.summary() for c in res if not c.passed]})
    return 0


def cmd_daily(ctx: Ctx) -> int:
    end = now_kst().date()
    c = ctx.data_client()
    bars = []
    cursor_end = end
    need = ctx.args.days
    while len(bars) < need:          # 1회 최대 100봉 → 필요시 이어붙임
        start = cursor_end - timedelta(days=140)
        chunk = c.get_daily_bars(ctx.args.code, start.strftime("%Y%m%d"), cursor_end.strftime("%Y%m%d"))
        chunk = [b for b in chunk if b.date not in {x.date for x in bars}]
        if not chunk:
            break
        bars = chunk + bars
        cursor_end = datetime.strptime(chunk[0].date, "%Y%m%d").date() - timedelta(days=1)
    out(summarize_daily(ctx.args.code, bars[-need:]))
    return 0


def cmd_news(ctx: Ctx) -> int:
    out(ctx.data_client().get_news_titles(ctx.args.code)[: ctx.args.limit])
    return 0


def cmd_watchlist(ctx: Ctx) -> int:
    action = ctx.args.action
    today = now_kst().date()
    if action == "example":
        ex = dict(wl.EXAMPLE, date=today.isoformat())
        out(ex)
        return 0
    path = Path(ctx.args.file) if ctx.args.file else wl.path_for(data_dir(), today)
    if not path.exists():
        print(f"파일 없음: {path}")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    w, errs = wl.validate(data, today)
    if errs:
        print("❌ watchlist 검증 실패:")
        for e in errs:
            print("  -", e)
        return 1
    if action == "show":
        out(as_dict(w))
    print(f"✅ {path} 유효 — 추천 {len(w.candidates)} / 회피 {len(w.avoid)} / 시장판단 {w.market_view}")
    return 0


# ───────────────────────── 매매 ─────────────────────────
def cmd_run(ctx: Ctx) -> int:
    args, cfg, s = ctx.args, ctx.cfg, ctx.secrets
    errs = cfg.validate()
    if errs:
        print("❌ 설정 오류:\n  - " + "\n  - ".join(errs))
        return 1
    dd = data_dir()
    if (dd / "STOP").exists():
        print(f"⚠️ 비상정지 파일이 있습니다 ({dd / 'STOP'}). 'python -m trader resume' 후 실행하세요.")
        return 1
    notifier = Notifier(s.telegram_token, s.telegram_chat_id)

    if args.dry_run:
        run_mode = "dry_run"
        data = ctx.data_client()
        capital = cfg.risk.capital_krw or 10_000_000
        broker = SimBroker(cash=capital, costs=cfg.costs.model(), slippage_ticks=cfg.costs.slippage_ticks)
        notifier.prefix = "[드라이런] "
    elif args.live:
        if s.mode != "live":
            print("❌ --live 는 .env 에 KIS_MODE=live 일 때만 가능합니다.")
            return 1
        if not s.live_confirmed:
            print(f"❌ 실전 매매 확인문구가 없습니다. .env 에 AUTOTRADER_LIVE_CONFIRM={LIVE_CONFIRM_PHRASE} 를 넣으세요.")
            return 1
        run_mode = "live"
        broker = KisBroker(ctx.trade_client())
        data = ctx.data_client()
        notifier.prefix = "[실전] "
        print("=" * 60)
        print("⚠️  실전 매매 모드: 실제 돈으로 주문이 나갑니다.")
        print(f"   1회 주문 최대 {cfg.risk.max_order_krw:,}원 / 일 손실한도 -{cfg.risk.max_daily_loss_pct}%"
              f" / 운용자금 {cfg.risk.capital_krw:,}원")
        print("   10초 안에 Ctrl+C 를 누르면 취소됩니다.")
        print("=" * 60)
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            print("취소됨")
            return 1
    else:
        if s.mode == "live":
            print("❌ KIS_MODE=live 입니다. 실전 실행은 'run --live', 모의투자는 KIS_MODE=paper 로 바꾸세요.")
            return 1
        run_mode = "paper"
        broker = KisBroker(ctx.trade_client())
        data = ctx.data_client()
        notifier.prefix = "[모의] "

    feed = LiveFeed(data, cfg.universe.rank_market, cfg.universe.exclude_flags)
    journal = Journal(dd / "journal.db")
    engine = Engine(cfg, feed, broker, journal, notifier, run_mode, data_dir=dd,
                    watchlist_loader=lambda d: wl.load(dd, d))
    log.info("자동매매 시작: mode=%s broker=%s", run_mode, broker.name)
    try:
        run_realtime(engine, cfg.schedule.poll_seconds)
    except KeyboardInterrupt:
        log.warning("사용자 중단 (Ctrl+C). 보유 포지션은 그대로 남아 있습니다 — HTS/MTS 또는 재실행으로 관리하세요.")
        notifier.send("사용자가 엔진을 중단했습니다. 보유 포지션을 확인하세요.")
        engine.save_state()
    return 0


def cmd_stop(ctx: Ctx) -> int:
    p = data_dir() / "STOP"
    p.write_text(f"stopped at {now_kst().isoformat()}\n", encoding="utf-8")
    print(f"🛑 비상정지 파일 생성: {p}\n   실행 중인 엔진은 다음 주기에 신규 진입을 멈추고 보유 종목을 시장가 청산합니다.")
    return 0


def cmd_resume(ctx: Ctx) -> int:
    p = data_dir() / "STOP"
    if p.exists():
        p.unlink()
    print("▶️ 비상정지 해제. 다음 실행부터 정상 동작합니다.")
    return 0


# ───────────────────────── 분석 ─────────────────────────
def cmd_report(ctx: Ctx) -> int:
    j = Journal(data_dir() / "journal.db")
    since = (now_kst().date() - timedelta(days=ctx.args.days)).isoformat()
    rep = build_report(j.trades(ctx.args.mode, since), j.daily_rows(ctx.args.mode, since))
    if ctx.args.json:
        out(rep)
    else:
        print(f"(기간: {since} ~, 모드: {ctx.args.mode or '전체'})")
        print(format_report(rep))
    return 0


def cmd_collect(ctx: Ctx) -> int:
    path = collect_day(ctx.data_client(), data_dir() / "bars", ctx.args.top, ctx.cfg.universe.exclude_flags)
    print(f"✅ 분봉 저장: {path}")
    return 0


def cmd_backtest(ctx: Ctx) -> int:
    a = ctx.args
    days = list_days(data_dir() / "bars", parse_day(a.date_from) if a.date_from else None,
                     parse_day(a.date_to) if a.date_to else None)
    if not days:
        print("백테스트할 데이터가 없습니다. 장 마감 후 'python -m trader collect' 로 매일 분봉을 모으세요.")
        return 1
    db = data_dir() / "backtest.db"
    if db.exists():
        db.unlink()
    rep, _ = run_backtest(ctx.cfg, days, a.capital, data_dir(), Journal(db))
    if a.json:
        out(rep)
    else:
        print(f"백테스트 {days[0].name} ~ {days[-1].name} ({len(days)}일), 일별 자금 {a.capital:,.0f}원")
        print(format_report(rep))
        print("\n※ 백테스트는 선택편향·체결가정 때문에 실제보다 좋게 나오는 경향이 있습니다. 모의투자로 검증하세요.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m trader", description="KIS 자동매매 (Claude 연동)",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--config", help="설정 파일 경로 (기본 config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("balance")
    q = sub.add_parser("quote")
    q.add_argument("codes", nargs="+")
    sub.add_parser("index")
    r = sub.add_parser("rank")
    r.add_argument("--top", type=int, default=30)
    r.add_argument("--market", choices=["J", "NX", "UN"])
    sub.add_parser("screen")
    d = sub.add_parser("daily")
    d.add_argument("code")
    d.add_argument("--days", type=int, default=250)
    n = sub.add_parser("news")
    n.add_argument("code")
    n.add_argument("--limit", type=int, default=30)
    w = sub.add_parser("watchlist")
    w.add_argument("action", choices=["validate", "show", "example"])
    w.add_argument("file", nargs="?")
    run = sub.add_parser("run")
    g = run.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    sub.add_parser("stop")
    sub.add_parser("resume")
    rep = sub.add_parser("report")
    rep.add_argument("--days", type=int, default=30)
    rep.add_argument("--mode", choices=["paper", "live", "dry_run"])
    rep.add_argument("--json", action="store_true")
    c = sub.add_parser("collect")
    c.add_argument("--top", type=int, default=30)
    b = sub.add_parser("backtest")
    b.add_argument("--from", dest="date_from")
    b.add_argument("--to", dest="date_to")
    b.add_argument("--capital", type=float, default=5_000_000)
    b.add_argument("--json", action="store_true")
    return p


COMMANDS = {
    "check": cmd_check, "balance": cmd_balance, "quote": cmd_quote, "index": cmd_index,
    "rank": cmd_rank, "screen": cmd_screen, "daily": cmd_daily, "news": cmd_news, "watchlist": cmd_watchlist,
    "run": cmd_run, "stop": cmd_stop, "resume": cmd_resume, "report": cmd_report,
    "collect": cmd_collect, "backtest": cmd_backtest,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    ctx = Ctx(args)
    try:
        return COMMANDS[args.cmd](ctx)
    except KisApiError as e:
        print(f"❌ KIS API 오류: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())

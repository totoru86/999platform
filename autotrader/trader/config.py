"""설정 로딩: config.yaml(전략·리스크) + .env(비밀정보)."""
from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from .market import CostModel, parse_hhmm

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIVE_CONFIRM_PHRASE = "I_ACCEPT_THE_RISK"


# ───────────────────────── 전략·리스크 설정 ─────────────────────────
@dataclass
class ScheduleConfig:
    opening_range_end: str = "09:15"
    entry_start: str = "09:15"
    entry_end: str = "14:30"
    no_entry_windows: list = field(default_factory=lambda: [["11:30", "13:00"]])
    force_exit: str = "15:15"
    poll_seconds: float = 5


@dataclass
class UniverseConfig:
    rank_top_n: int = 10
    rank_market: str = "J"
    exclude_flags: str = "1111111101"
    min_price: int = 2000
    max_price: int = 1_000_000
    min_market_cap_eok: float = 3000
    min_turnover_schedule: dict = field(default_factory=lambda: {
        "09:15": 150, "09:30": 250, "10:30": 400, "12:00": 600, "14:00": 800})
    min_change_pct: float = 2.0
    max_change_pct: float = 20.0
    require_program_net_buy: bool = True
    require_foreign_net_buy: bool = False
    near_high_pct: float = 5.0
    require_near_high: bool = False
    refresh_minutes: float = 5
    claude_watchlist: str = "boost"   # off | boost | only


@dataclass
class StrategyConfig:
    breakout_buffer_pct: float = 0.1
    max_or_range_pct: float = 6.0
    volume_surge_ratio: float = 1.5
    volume_lookback_bars: int = 10
    max_stop_pct: float = 1.5
    take_profit_pct: float = 3.0
    trail_activate_pct: float = 1.5
    trail_pct: float = 1.0
    vwap_exit: bool = True
    upper_limit_buffer_pct: float = 7.0
    entry_slippage_ticks: int = 2
    entry_timeout_seconds: float = 20
    max_entries_per_symbol: int = 1


@dataclass
class RiskConfig:
    capital_krw: int = 0                 # 자동매매에 쓸 자금 (0 = 계좌 총평가금액 전체)
    risk_per_trade_pct: float = 0.5
    max_position_pct: float = 25
    max_order_krw: int = 2_000_000
    max_open_positions: int = 2
    max_trades_per_day: int = 4
    max_consecutive_losses: int = 2
    max_daily_loss_pct: float = 1.0
    flatten_on_daily_loss: bool = True
    daily_profit_target_pct: float = 1.0
    risk_off_size_multiplier: float = 0.5
    max_api_errors: int = 10


@dataclass
class CostsConfig:
    commission_pct: float = 0.015
    sell_tax_pct: float = 0.20
    slippage_ticks: int = 1

    def model(self) -> CostModel:
        return CostModel(self.commission_pct, self.sell_tax_pct)


@dataclass
class ApiConfig:
    rate_per_sec_real: float = 10
    rate_per_sec_paper: float = 2
    timeout_seconds: float = 10


@dataclass
class TradingConfig:
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostsConfig = field(default_factory=CostsConfig)
    api: ApiConfig = field(default_factory=ApiConfig)

    def validate(self) -> list[str]:
        """설정값 상식 검사. 문제 목록 반환 (빈 리스트면 OK)."""
        errs = []
        s, r, u, st = self.schedule, self.risk, self.universe, self.strategy
        for name in ("opening_range_end", "entry_start", "entry_end", "force_exit"):
            try:
                parse_hhmm(getattr(s, name))
            except Exception:
                errs.append(f"schedule.{name} 형식 오류 (HH:MM)")
        if not errs:
            if not (parse_hhmm(s.entry_start) < parse_hhmm(s.entry_end) < parse_hhmm(s.force_exit)):
                errs.append("schedule: entry_start < entry_end < force_exit 순서여야 합니다")
            if parse_hhmm(s.force_exit) > parse_hhmm("15:19"):
                errs.append("schedule.force_exit 는 15:19 이전이어야 합니다 (15:20부터 동시호가)")
        if not 0 < r.risk_per_trade_pct <= 2:
            errs.append("risk.risk_per_trade_pct 는 0~2% 사이 권장")
        if not 0 < r.max_position_pct <= 100:
            errs.append("risk.max_position_pct 는 0~100")
        if r.max_order_krw <= 0:
            errs.append("risk.max_order_krw 는 양수여야 합니다")
        if not 0 < r.max_daily_loss_pct <= 5:
            errs.append("risk.max_daily_loss_pct 는 0~5% 사이 권장")
        if not 0 <= r.risk_off_size_multiplier <= 1:
            errs.append("risk.risk_off_size_multiplier 는 0~1")
        if u.claude_watchlist not in ("off", "boost", "only"):
            errs.append("universe.claude_watchlist 는 off|boost|only")
        if len(u.exclude_flags) != 10 or set(u.exclude_flags) - {"0", "1"}:
            errs.append("universe.exclude_flags 는 0/1 10자리")
        if u.rank_market not in ("J", "NX", "UN"):
            errs.append("universe.rank_market 는 J|NX|UN")
        if st.max_stop_pct <= 0 or st.take_profit_pct <= 0:
            errs.append("strategy 손절/익절 % 는 양수")
        rt = self.costs.commission_pct * 2 + self.costs.sell_tax_pct
        if st.take_profit_pct <= rt * 2:
            errs.append(f"strategy.take_profit_pct({st.take_profit_pct}%)가 왕복비용({rt:.2f}%)에 비해 너무 작습니다")
        return errs


def _build(cls, data: dict | None, path: str = ""):
    """dict → dataclass (알 수 없는 키는 경고)."""
    obj = cls()
    if not data:
        return obj
    known = {f.name: f for f in fields(cls)}
    for key, value in data.items():
        if key not in known:
            warnings.warn(f"알 수 없는 설정 키 무시: {path}{key}")
            continue
        default = getattr(obj, key)
        if is_dataclass(default):
            setattr(obj, key, _build(type(default), value, f"{path}{key}."))
        else:
            setattr(obj, key, value)
    return obj


def load_trading_config(path: str | Path | None = None) -> TradingConfig:
    if path is None:
        path = PROJECT_ROOT / "config.yaml"
        if not path.exists():
            path = PROJECT_ROOT / "config.example.yaml"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return _build(TradingConfig, data)


# ───────────────────────── 비밀정보(.env) ─────────────────────────
def load_dotenv(path: str | Path | None = None) -> None:
    """의존성 없는 간단한 .env 로더. 이미 설정된 환경변수는 덮어쓰지 않음."""
    path = Path(path) if path else PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if " #" in value:                       # 줄 끝 주석 제거
            value = value.split(" #", 1)[0].strip()
        value = value.strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class KisCredentials:
    env: str            # "real" | "paper"
    app_key: str
    app_secret: str
    account_no: str     # 8자리
    product_code: str   # 2자리

    @property
    def is_paper(self) -> bool:
        return self.env == "paper"

    def masked(self) -> str:
        acct = self.account_no[:2] + "****" + self.account_no[-2:] if len(self.account_no) >= 4 else "****"
        return f"{self.env} 계좌 {acct}-{self.product_code}"

    def problems(self) -> list[str]:
        p = []
        if not self.app_key or not self.app_secret:
            p.append(f"[{self.env}] 앱키/시크릿이 비어 있습니다")
        if not (self.account_no.isdigit() and len(self.account_no) == 8):
            p.append(f"[{self.env}] 계좌번호는 앞 8자리 숫자여야 합니다")
        if not (self.product_code.isdigit() and len(self.product_code) == 2):
            p.append(f"[{self.env}] 계좌상품코드는 2자리 숫자 (종합계좌 01)")
        return p


def _creds(prefix: str, env: str) -> KisCredentials | None:
    key = os.environ.get(f"KIS_{prefix}_APP_KEY", "").strip()
    if not key:
        return None
    return KisCredentials(
        env=env,
        app_key=key,
        app_secret=os.environ.get(f"KIS_{prefix}_APP_SECRET", "").strip(),
        account_no=os.environ.get(f"KIS_{prefix}_ACCOUNT_NO", "").strip().replace("-", "")[:8],
        product_code=os.environ.get(f"KIS_{prefix}_ACCOUNT_PRODUCT", "01").strip() or "01",
    )


@dataclass
class Secrets:
    mode: str                                   # paper | live
    trade: KisCredentials | None                # 주문 계좌
    data: KisCredentials | None                 # 시세 조회용 (실전 키가 있으면 실전 서버)
    live_confirmed: bool
    telegram_token: str
    telegram_chat_id: str


def load_secrets() -> Secrets:
    load_dotenv()
    mode = os.environ.get("KIS_MODE", "paper").strip().lower()
    if mode not in ("paper", "live"):
        raise ValueError("KIS_MODE 는 paper 또는 live 여야 합니다")
    paper = _creds("PAPER", "paper")
    real = _creds("REAL", "real")
    trade = real if mode == "live" else paper
    data = real or trade
    return Secrets(
        mode=mode,
        trade=trade,
        data=data,
        live_confirmed=os.environ.get("AUTOTRADER_LIVE_CONFIRM", "").strip() == LIVE_CONFIRM_PHRASE,
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
    )


def data_dir() -> Path:
    d = Path(os.environ.get("AUTOTRADER_DATA_DIR", PROJECT_ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def as_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: as_dict(getattr(obj, f.name)) for f in fields(obj)}
    return obj

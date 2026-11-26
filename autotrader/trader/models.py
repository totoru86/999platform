"""데이터 모델."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


def to_int(v, default: int = 0) -> int:
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return default


def to_float(v, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


@dataclass
class Quote:
    code: str
    price: int
    open: int = 0
    high: int = 0
    low: int = 0
    change_pct: float = 0.0
    acc_volume: int = 0
    acc_turnover: int = 0           # 누적 거래대금 (원)
    upper_limit: int = 0            # 상한가
    lower_limit: int = 0
    market_cap_eok: float = 0.0     # 시가총액 (억원)
    w52_high: int = 0
    foreign_net_qty: int = 0        # 외국인 순매수 수량 (장중은 추정)
    program_net_qty: int = 0        # 프로그램 순매수 수량
    halted: bool = False            # 거래정지/임시정지
    warning: bool = False           # 투자주의/경고/위험, 관리, 단기과열, 정리매매 등
    vi: bool = False                # VI 발동 중
    base_price: int = 0             # 기준가 (보통 전일 종가)
    name: str = ""
    ts: datetime | None = None

    @property
    def vwap(self) -> float:
        """당일 VWAP = 누적거래대금 / 누적거래량 (정확값)."""
        return self.acc_turnover / self.acc_volume if self.acc_volume > 0 else float(self.price)

    @property
    def turnover_eok(self) -> float:
        return self.acc_turnover / 1e8

    @classmethod
    def from_kis(cls, code: str, o: dict, ts: datetime | None = None) -> "Quote":
        warn = (
            o.get("mrkt_warn_cls_code", "00") not in ("00", "", None)
            or o.get("invt_caful_yn") == "Y"
            or o.get("short_over_yn") == "Y"
            or o.get("sltr_yn") == "Y"
            or o.get("mang_issu_cls_code") == "Y"
            # 종목상태: 51 관리, 52 투자위험, 53 투자경고, 54 투자주의, 59 단기과열
            or o.get("iscd_stat_cls_code") in ("51", "52", "53", "54", "59")
        )
        return cls(
            code=code,
            price=to_int(o.get("stck_prpr")),
            open=to_int(o.get("stck_oprc")),
            high=to_int(o.get("stck_hgpr")),
            low=to_int(o.get("stck_lwpr")),
            change_pct=to_float(o.get("prdy_ctrt")),
            acc_volume=to_int(o.get("acml_vol")),
            acc_turnover=to_int(o.get("acml_tr_pbmn")),
            upper_limit=to_int(o.get("stck_mxpr")),
            lower_limit=to_int(o.get("stck_llam")),
            market_cap_eok=to_float(o.get("hts_avls")),
            w52_high=to_int(o.get("w52_hgpr")),
            foreign_net_qty=to_int(o.get("frgn_ntby_qty")),
            program_net_qty=to_int(o.get("pgtr_ntby_qty")),
            halted=o.get("temp_stop_yn") == "Y" or o.get("iscd_stat_cls_code") in ("58",),
            warning=warn,
            vi=o.get("vi_cls_code", "N") not in ("N", "", None),
            base_price=to_int(o.get("stck_sdpr")),
            ts=ts,
        )


@dataclass
class Bar:
    """1분봉. time 은 'HHMMSS' (봉 시작 시각)."""
    date: str
    time: str
    open: int
    high: int
    low: int
    close: int
    volume: int

    @property
    def key(self) -> str:
        return self.date + self.time

    @classmethod
    def from_kis(cls, o: dict) -> "Bar":
        return cls(
            date=o.get("stck_bsop_date", ""),
            time=o.get("stck_cntg_hour", ""),
            open=to_int(o.get("stck_oprc")),
            high=to_int(o.get("stck_hgpr")),
            low=to_int(o.get("stck_lwpr")),
            close=to_int(o.get("stck_prpr")),
            volume=to_int(o.get("cntg_vol")),
        )


@dataclass
class DailyBar:
    date: str
    open: int
    high: int
    low: int
    close: int
    volume: int
    turnover: int


@dataclass
class RankItem:
    rank: int
    code: str
    name: str
    price: int
    change_pct: float
    acc_volume: int
    acc_turnover: int

    @property
    def turnover_eok(self) -> float:
        return self.acc_turnover / 1e8


@dataclass
class Holding:
    code: str
    name: str
    qty: int
    sellable_qty: int
    avg_price: float
    price: int
    pnl: int


@dataclass
class AccountSummary:
    cash: int               # 예수금총금액
    d2_cash: int            # D+2 예수금 (가수도정산금액)
    total_eval: int         # 총평가금액
    net_asset: int          # 순자산
    holdings: list[Holding] = field(default_factory=list)

    @property
    def equity(self) -> int:
        return self.total_eval or self.net_asset or self.cash


@dataclass
class OrderAck:
    order_no: str           # ODNO
    org_no: str             # KRX_FWDG_ORD_ORGNO
    time: str


@dataclass
class OrderStatus:
    order_no: str
    code: str
    side: str               # buy | sell
    qty: int
    filled_qty: int
    avg_price: float
    remaining_qty: int
    cancelled: bool
    time: str = ""

    @property
    def done(self) -> bool:
        return self.cancelled or self.remaining_qty <= 0 or self.filled_qty >= self.qty

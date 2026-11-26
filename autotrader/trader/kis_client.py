"""한국투자증권 KIS Open API REST 클라이언트 (국내주식).

공식 샘플(https://github.com/koreainvestment/open-trading-api, examples_llm/domestic_stock)의
엔드포인트·TR ID·파라미터를 기준으로 작성했습니다.

안전 원칙
- 주문(POST)은 네트워크 오류 시 자동 재시도하지 않습니다 (중복 주문 방지). 호출자가 체결조회로 확인.
- 조회(GET)는 초당 호출 초과(EGW00201)·일시 오류 시 백오프 재시도.
- 토큰은 파일에 캐시해 재사용 (발급은 1분당 1회 제한, 유효기간 약 24시간).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .config import KisCredentials
from .market import KST, hhmmss, now_kst
from .models import (AccountSummary, Bar, DailyBar, Holding, OrderAck, OrderStatus, Quote,
                     RankItem, to_float, to_int)

log = logging.getLogger(__name__)

REAL_URL = "https://openapi.koreainvestment.com:9443"
PAPER_URL = "https://openapivts.koreainvestment.com:29443"

# 주문/계좌 TR (실전). 모의투자는 첫 글자 T → V
TR_BUY = "TTTC0012U"
TR_SELL = "TTTC0011U"
TR_CANCEL = "TTTC0013U"
TR_BALANCE = "TTTC8434R"
TR_BUYABLE = "TTTC8908R"
TR_DAILY_ORDERS = "TTTC0081R"
# 시세 TR (실전/모의 동일)
TR_PRICE = "FHKST01010100"
TR_MINUTE = "FHKST03010200"
TR_DAILY = "FHKST03010100"
TR_VOLUME_RANK = "FHPST01710000"
TR_INDEX = "FHPUP02100000"
TR_NEWS = "FHKST01011800"

RETRYABLE_CODES = {"EGW00201"}                  # 초당 거래건수 초과
TOKEN_EXPIRED_CODES = {"EGW00123", "EGW00121"}  # 기간 만료 / 유효하지 않은 토큰
TOKEN_RATE_CODE = "EGW00133"                    # 토큰 발급 1분당 1회


class KisApiError(Exception):
    def __init__(self, message: str, msg_cd: str = "", status: int = 0, tr_id: str = ""):
        super().__init__(f"[{msg_cd or status}] {message} (tr_id={tr_id})")
        self.msg_cd = msg_cd
        self.status = status
        self.tr_id = tr_id


class RateLimiter:
    def __init__(self, per_sec: float):
        self.interval = 1.0 / max(per_sec, 0.1)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


class KisClient:
    def __init__(self, creds: KisCredentials, token_dir: Path, rate_per_sec: float = 2,
                 timeout: float = 10, session: requests.Session | None = None):
        self.creds = creds
        self.base_url = PAPER_URL if creds.is_paper else REAL_URL
        self.token_path = Path(token_dir) / f"kis_token_{creds.env}.json"
        self.timeout = timeout
        self.session = session or requests.Session()
        self.limiter = RateLimiter(rate_per_sec)
        self._token: str | None = None
        self._token_exp: datetime | None = None

    # ─────────────── 인증 ───────────────
    def _load_cached_token(self) -> bool:
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
            if data.get("app_key_tail") != self.creds.app_key[-6:]:
                return False
            exp = datetime.fromisoformat(data["expires_at"])
            if exp - now_kst() > timedelta(minutes=10):
                self._token, self._token_exp = data["access_token"], exp
                return True
        except (OSError, ValueError, KeyError):
            pass
        return False

    def _issue_token(self) -> None:
        url = f"{self.base_url}/oauth2/tokenP"
        body = {"grant_type": "client_credentials",
                "appkey": self.creds.app_key, "appsecret": self.creds.app_secret}
        for attempt in range(2):
            res = self.session.post(url, json=body, timeout=self.timeout)
            data = _safe_json(res)
            if res.status_code == 200 and data.get("access_token"):
                exp_str = data.get("access_token_token_expired")
                if exp_str:
                    exp = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
                else:
                    exp = now_kst() + timedelta(seconds=to_int(data.get("expires_in"), 86400))
                self._token, self._token_exp = data["access_token"], exp
                self.token_path.parent.mkdir(parents=True, exist_ok=True)
                self.token_path.write_text(json.dumps({
                    "access_token": self._token, "expires_at": exp.isoformat(),
                    "app_key_tail": self.creds.app_key[-6:]}), encoding="utf-8")
                try:
                    os.chmod(self.token_path, 0o600)
                except OSError:
                    pass
                log.info("KIS 접근토큰 발급 완료 (%s, 만료 %s)", self.creds.env, exp)
                return
            if data.get("error_code") == TOKEN_RATE_CODE and attempt == 0:
                log.warning("토큰 발급 1분 제한 — 61초 후 재시도")
                time.sleep(61)
                continue
            raise KisApiError(data.get("error_description") or res.text[:200],
                              data.get("error_code", ""), res.status_code, "tokenP")

    def token(self) -> str:
        if self._token and self._token_exp and self._token_exp - now_kst() > timedelta(minutes=10):
            return self._token
        if not self._load_cached_token():
            self._issue_token()
        return self._token  # type: ignore[return-value]

    # ─────────────── 공통 요청 ───────────────
    def _tr(self, real_tr: str) -> str:
        """주문/계좌 TR 은 모의투자일 때 V 로 시작."""
        return "V" + real_tr[1:] if self.creds.is_paper and real_tr[0] in "TJC" else real_tr

    def request(self, method: str, path: str, tr_id: str, params: dict | None = None,
                body: dict | None = None, tr_cont: str = "") -> tuple[dict, dict]:
        is_order = method == "POST"
        refreshed = False
        for attempt in range(4):
            self.limiter.wait()
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.token()}",
                "appkey": self.creds.app_key,
                "appsecret": self.creds.app_secret,
                "tr_id": tr_id,
                "tr_cont": tr_cont,
                "custtype": "P",
            }
            url = self.base_url + path
            try:
                if is_order:
                    res = self.session.post(url, headers=headers, data=json.dumps(body or {}),
                                            timeout=self.timeout)
                else:
                    res = self.session.get(url, headers=headers, params=params or {},
                                           timeout=self.timeout)
            except requests.RequestException as e:
                if is_order:
                    # 주문은 재시도하지 않는다: 실제 접수됐을 수 있으므로 체결조회로 확인해야 함
                    raise KisApiError(f"주문 요청 네트워크 오류(접수 여부 불명): {e}", "NETWORK", 0, tr_id)
                log.warning("네트워크 오류 %s — 재시도 %d", e, attempt + 1)
                time.sleep(1 + attempt)
                continue

            data = _safe_json(res)
            msg_cd = data.get("msg_cd", "")
            if res.status_code == 200 and data.get("rt_cd") == "0":
                return data, dict(res.headers)
            if msg_cd in TOKEN_EXPIRED_CODES and not refreshed:
                log.info("토큰 만료 — 재발급")
                self._token = None
                try:
                    self.token_path.unlink()
                except OSError:
                    pass
                refreshed = True
                continue
            if msg_cd in RETRYABLE_CODES and attempt < 3:
                time.sleep(0.5 * (attempt + 1))
                continue
            if not is_order and res.status_code >= 500 and not msg_cd and attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise KisApiError(data.get("msg1") or res.text[:200], msg_cd, res.status_code, tr_id)
        raise KisApiError("재시도 횟수 초과", "RETRY", 0, tr_id)

    # ─────────────── 시세 ───────────────
    def get_quote(self, code: str, market: str = "J") -> Quote:
        data, _ = self.request("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", TR_PRICE,
                               {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code})
        return Quote.from_kis(code, data.get("output") or {}, now_kst())

    def get_index(self, index_code: str) -> dict:
        """업종지수 현재가. 0001=코스피, 1001=코스닥, 2001=코스피200."""
        data, _ = self.request("GET", "/uapi/domestic-stock/v1/quotations/inquire-index-price",
                               TR_INDEX, {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": index_code})
        o = data.get("output") or {}
        return {
            "index_code": index_code,
            "price": to_float(o.get("bstp_nmix_prpr")),
            "change_pct": to_float(o.get("bstp_nmix_prdy_ctrt")),
            "acc_turnover_eok": round(to_int(o.get("acml_tr_pbmn")) / 100, 1),  # 백만원 → 억원
        }

    def get_minute_bars(self, code: str, until: str | None = None, market: str = "J") -> list[Bar]:
        """당일 1분봉 최대 30개 (until=HHMMSS 이전). 시간 오름차순."""
        until = until or hhmmss(now_kst())
        data, _ = self.request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice", TR_MINUTE,
            {"FID_ETC_CLS_CODE": "", "FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code,
             "FID_INPUT_HOUR_1": until, "FID_PW_DATA_INCU_YN": "Y"})
        bars = [Bar.from_kis(o) for o in (data.get("output2") or []) if o.get("stck_cntg_hour")]
        bars = [b for b in bars if b.volume > 0 or b.close > 0]
        return sorted(bars, key=lambda b: b.key)

    def get_minute_bars_since(self, code: str, since: str = "090000", until: str | None = None,
                              market: str = "J", max_pages: int = 15) -> list[Bar]:
        """since~until 구간 당일 1분봉 전부 (30개씩 역순 페이징)."""
        today = now_kst().strftime("%Y%m%d")
        cursor = until or hhmmss(now_kst())
        result: dict[str, Bar] = {}
        for _ in range(max_pages):
            bars = [b for b in self.get_minute_bars(code, cursor, market) if b.date == today]
            new = [b for b in bars if b.key not in result]
            for b in new:
                result[b.key] = b
            if not new:
                break
            earliest = min(b.time for b in new)
            if earliest <= since:
                break
            t = datetime.strptime(earliest, "%H%M%S") - timedelta(minutes=1)
            cursor = t.strftime("%H%M%S")
        return sorted((b for b in result.values() if b.time >= since), key=lambda b: b.key)

    def get_daily_bars(self, code: str, start: str, end: str, market: str = "J") -> list[DailyBar]:
        """일봉 (최대 100개/호출, 수정주가). 날짜 오름차순."""
        data, _ = self.request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", TR_DAILY,
            {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": start,
             "FID_INPUT_DATE_2": end, "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
        bars = []
        for o in data.get("output2") or []:
            if not o.get("stck_bsop_date"):
                continue
            bars.append(DailyBar(o["stck_bsop_date"], to_int(o.get("stck_oprc")),
                                 to_int(o.get("stck_hgpr")), to_int(o.get("stck_lwpr")),
                                 to_int(o.get("stck_clpr")), to_int(o.get("acml_vol")),
                                 to_int(o.get("acml_tr_pbmn"))))
        return sorted(bars, key=lambda b: b.date)

    def get_turnover_rank(self, market: str = "J", exclude_flags: str = "1111111101",
                          price_min: int = 0, price_max: int = 0) -> list[RankItem]:
        """거래대금 순위 (최대 30종목). 모의투자 서버에서는 제한될 수 있음."""
        data, _ = self.request(
            "GET", "/uapi/domestic-stock/v1/quotations/volume-rank", TR_VOLUME_RANK,
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20171",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "1",          # 보통주
             "FID_BLNG_CLS_CODE": "3",                                     # 거래금액순
             "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": exclude_flags,
             "FID_INPUT_PRICE_1": str(price_min) if price_min else "",
             "FID_INPUT_PRICE_2": str(price_max) if price_max else "",
             "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""})
        items = []
        for o in data.get("output") or []:
            items.append(RankItem(
                rank=to_int(o.get("data_rank")), code=o.get("mksc_shrn_iscd", ""),
                name=o.get("hts_kor_isnm", ""), price=to_int(o.get("stck_prpr")),
                change_pct=to_float(o.get("prdy_ctrt")), acc_volume=to_int(o.get("acml_vol")),
                acc_turnover=to_int(o.get("acml_tr_pbmn"))))
        return sorted(items, key=lambda r: r.rank)

    def get_news_titles(self, code: str = "", date: str = "", hour: str = "") -> list[dict]:
        """종합 시황/공시 제목 (최신순). date=YYYYMMDD, hour=HHMMSS 이전 기사. 실전 서버 전용일 수 있음."""
        data, _ = self.request(
            "GET", "/uapi/domestic-stock/v1/quotations/news-title", TR_NEWS,
            {"FID_NEWS_OFER_ENTP_CODE": "", "FID_COND_MRKT_CLS_CODE": "", "FID_INPUT_ISCD": code,
             "FID_TITL_CNTT": "", "FID_INPUT_DATE_1": date, "FID_INPUT_HOUR_1": hour,
             "FID_RANK_SORT_CLS_CODE": "", "FID_INPUT_SRNO": ""})
        rows = data.get("output") or data.get("output1") or []
        if isinstance(rows, dict):
            rows = [rows]
        return [{"date": r.get("data_dt", ""), "time": r.get("data_tm", ""),
                 "title": (r.get("hts_pbnt_titl_cntt") or "").strip(), "source": r.get("dorg", ""),
                 "codes": [r.get(f"iscd{i}") for i in range(1, 6) if r.get(f"iscd{i}")]}
                for r in rows if r.get("hts_pbnt_titl_cntt")]

    # ─────────────── 계좌 ───────────────
    def _acct(self) -> dict:
        return {"CANO": self.creds.account_no, "ACNT_PRDT_CD": self.creds.product_code}

    def get_balance(self) -> AccountSummary:
        holdings: list[Holding] = []
        summary: dict = {}
        fk = nk = ""
        tr_cont = ""
        for _ in range(10):
            data, headers = self.request(
                "GET", "/uapi/domestic-stock/v1/trading/inquire-balance", self._tr(TR_BALANCE),
                {**self._acct(), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                 "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
                 "PRCS_DVSN": "00", "CTX_AREA_FK100": fk, "CTX_AREA_NK100": nk}, tr_cont=tr_cont)
            for o in data.get("output1") or []:
                qty = to_int(o.get("hldg_qty"))
                if qty <= 0:
                    continue
                holdings.append(Holding(
                    code=o.get("pdno", ""), name=o.get("prdt_name", ""), qty=qty,
                    sellable_qty=to_int(o.get("ord_psbl_qty")),
                    avg_price=to_float(o.get("pchs_avg_pric")), price=to_int(o.get("prpr")),
                    pnl=to_int(o.get("evlu_pfls_amt"))))
            out2 = data.get("output2") or [{}]
            summary = out2[0] if isinstance(out2, list) and out2 else (out2 or {})
            cont = _header(headers, "tr_cont")
            if cont in ("M", "F"):
                fk, nk, tr_cont = data.get("ctx_area_fk100", ""), data.get("ctx_area_nk100", ""), "N"
                continue
            break
        return AccountSummary(
            cash=to_int(summary.get("dnca_tot_amt")),
            d2_cash=to_int(summary.get("prvs_rcdl_excc_amt")),
            total_eval=to_int(summary.get("tot_evlu_amt")),
            net_asset=to_int(summary.get("nass_amt")),
            holdings=holdings)

    def get_buyable(self, code: str, price: int) -> tuple[int, int]:
        """(미수없는 매수가능금액, 매수가능수량)."""
        data, _ = self.request(
            "GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order", self._tr(TR_BUYABLE),
            {**self._acct(), "PDNO": code, "ORD_UNPR": str(price), "ORD_DVSN": "00",
             "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
        o = data.get("output") or {}
        return to_int(o.get("nrcvb_buy_amt")), to_int(o.get("nrcvb_buy_qty"))

    def place_order(self, side: str, code: str, qty: int, price: int | None = None) -> OrderAck:
        """현금 주문. price=None 이면 시장가. side: buy | sell"""
        if qty <= 0:
            raise ValueError("주문 수량은 1 이상")
        tr = TR_BUY if side == "buy" else TR_SELL
        body = {**self._acct(), "PDNO": code,
                "ORD_DVSN": "01" if price is None else "00",      # 01 시장가, 00 지정가
                "ORD_QTY": str(int(qty)), "ORD_UNPR": "0" if price is None else str(int(price)),
                "EXCG_ID_DVSN_CD": "KRX"}
        if side == "sell":
            body["SLL_TYPE"] = "01"                                 # 일반매도
        data, _ = self.request("POST", "/uapi/domestic-stock/v1/trading/order-cash",
                               self._tr(tr), body=body)
        o = data.get("output") or {}
        return OrderAck(o.get("ODNO", ""), o.get("KRX_FWDG_ORD_ORGNO", ""), o.get("ORD_TMD", ""))

    def cancel_order(self, ack: OrderAck) -> None:
        """잔량 전부 취소."""
        body = {**self._acct(), "KRX_FWDG_ORD_ORGNO": ack.org_no, "ORGN_ODNO": ack.order_no,
                "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": "KRX"}
        self.request("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl",
                     self._tr(TR_CANCEL), body=body)

    def get_today_orders(self, order_no: str = "") -> list[OrderStatus]:
        today = now_kst().strftime("%Y%m%d")
        results: list[OrderStatus] = []
        fk = nk = ""
        tr_cont = ""
        for _ in range(10):
            data, headers = self.request(
                "GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", self._tr(TR_DAILY_ORDERS),
                {**self._acct(), "INQR_STRT_DT": today, "INQR_END_DT": today,
                 "SLL_BUY_DVSN_CD": "00", "PDNO": "", "CCLD_DVSN": "00", "INQR_DVSN": "00",
                 "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "", "ODNO": order_no, "INQR_DVSN_1": "",
                 "CTX_AREA_FK100": fk, "CTX_AREA_NK100": nk, "EXCG_ID_DVSN_CD": "KRX"},
                tr_cont=tr_cont)
            for o in data.get("output1") or []:
                qty = to_int(o.get("ord_qty"))
                filled = to_int(o.get("tot_ccld_qty"))
                results.append(OrderStatus(
                    order_no=o.get("odno", ""), code=o.get("pdno", ""),
                    side="sell" if o.get("sll_buy_dvsn_cd") == "01" else "buy",
                    qty=qty, filled_qty=filled, avg_price=to_float(o.get("avg_prvs")),
                    remaining_qty=to_int(o.get("rmn_qty"), max(qty - filled, 0)),
                    cancelled=o.get("cncl_yn") == "Y", time=o.get("ord_tmd", "")))
            if _header(headers, "tr_cont") in ("M", "F"):
                fk, nk, tr_cont = data.get("ctx_area_fk100", ""), data.get("ctx_area_nk100", ""), "N"
                continue
            break
        return results


def _safe_json(res: requests.Response) -> dict:
    try:
        data = res.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _header(headers: dict, name: str) -> str:
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return ""

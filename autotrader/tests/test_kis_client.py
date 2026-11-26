"""KIS 클라이언트: HTTP 를 가짜 세션으로 대체해 프로토콜 동작 검증."""
from datetime import timedelta

import pytest
import requests

from trader.config import KisCredentials
from trader.kis_client import PAPER_URL, REAL_URL, KisApiError, KisClient
from trader.market import now_kst


class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.calls = []
        self.queue = {}         # path -> list of FakeResp | Exception

    def add(self, path, *responses):
        self.queue.setdefault(path, []).extend(responses)

    def _pop(self, method, url, **kw):
        path = url.split(":9443")[-1].split(":29443")[-1]
        self.calls.append((method, path, kw))
        items = self.queue.get(path)
        if not items:
            raise AssertionError(f"예상치 못한 호출 {method} {path}")
        r = items.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, **kw):
        return self._pop("GET", url, **kw)

    def post(self, url, **kw):
        return self._pop("POST", url, **kw)


def token_resp():
    exp = (now_kst() + timedelta(hours=23)).strftime("%Y-%m-%d %H:%M:%S")
    return FakeResp(200, {"access_token": "TKN", "access_token_token_expired": exp})


OK = {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리"}


def make(tmp_path, env="paper"):
    creds = KisCredentials(env, "APPKEY123456", "SECRET", "12345678", "01")
    s = FakeSession()
    return KisClient(creds, tmp_path, rate_per_sec=1000, session=s), s


def test_paper_uses_virtual_domain_and_tr(tmp_path):
    c, s = make(tmp_path, "paper")
    assert c.base_url == PAPER_URL
    s.add("/oauth2/tokenP", token_resp())
    s.add("/uapi/domestic-stock/v1/trading/inquire-balance",
          FakeResp(200, {**OK, "output1": [{"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "10",
                                             "ord_psbl_qty": "10", "pchs_avg_pric": "70000.0", "prpr": "71000",
                                             "evlu_pfls_amt": "10000"}],
                         "output2": [{"dnca_tot_amt": "1000000", "tot_evlu_amt": "1710000",
                                      "nass_amt": "1710000", "prvs_rcdl_excc_amt": "1000000"}]}))
    a = c.get_balance()
    _, _, kw = s.calls[-1]
    assert kw["headers"]["tr_id"] == "VTTC8434R"
    assert kw["headers"]["authorization"] == "Bearer TKN"
    assert a.holdings[0].qty == 10 and a.total_eval == 1_710_000


def test_real_tr_ids(tmp_path):
    c, s = make(tmp_path, "real")
    assert c.base_url == REAL_URL
    s.add("/oauth2/tokenP", token_resp())
    s.add("/uapi/domestic-stock/v1/trading/order-cash", FakeResp(200, {**OK, "output": {
        "ODNO": "0000123", "KRX_FWDG_ORD_ORGNO": "91252", "ORD_TMD": "093000"}}))
    ack = c.place_order("buy", "005930", 3, 70000)
    _, _, kw = s.calls[-1]
    assert kw["headers"]["tr_id"] == "TTTC0012U"
    import json
    body = json.loads(kw["data"])
    assert body["ORD_DVSN"] == "00" and body["ORD_QTY"] == "3" and body["ORD_UNPR"] == "70000"
    assert ack.order_no == "0000123"


def test_market_sell_body(tmp_path):
    c, s = make(tmp_path, "paper")
    s.add("/oauth2/tokenP", token_resp())
    s.add("/uapi/domestic-stock/v1/trading/order-cash", FakeResp(200, {**OK, "output": {"ODNO": "1"}}))
    c.place_order("sell", "005930", 5, None)
    import json
    _, _, kw = s.calls[-1]
    body = json.loads(kw["data"])
    assert kw["headers"]["tr_id"] == "VTTC0011U"
    assert body["ORD_DVSN"] == "01" and body["ORD_UNPR"] == "0" and body["SLL_TYPE"] == "01"


def test_token_is_cached_on_disk(tmp_path):
    c, s = make(tmp_path)
    s.add("/oauth2/tokenP", token_resp())
    assert c.token() == "TKN"
    c2, s2 = make(tmp_path)          # 새 프로세스 가정
    assert c2.token() == "TKN"
    assert s2.calls == []            # 재발급 안 함 (1분 1회 제한 회피)


def test_order_network_error_is_not_retried(tmp_path):
    c, s = make(tmp_path)
    s.add("/oauth2/tokenP", token_resp())
    s.add("/uapi/domestic-stock/v1/trading/order-cash", requests.ConnectionError("boom"),
          FakeResp(200, {**OK, "output": {"ODNO": "2"}}))
    with pytest.raises(KisApiError) as e:
        c.place_order("buy", "005930", 1, 1000)
    assert e.value.msg_cd == "NETWORK"
    assert sum(1 for m, p, _ in s.calls if p.endswith("order-cash")) == 1   # 중복 주문 위험 → 1회만


def test_rate_limit_error_is_retried_for_queries(tmp_path):
    c, s = make(tmp_path)
    s.add("/oauth2/tokenP", token_resp())
    s.add("/uapi/domestic-stock/v1/quotations/inquire-price",
          FakeResp(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}),
          FakeResp(200, {**OK, "output": {"stck_prpr": "71000", "acml_vol": "100", "acml_tr_pbmn": "7000000",
                                          "pgtr_ntby_qty": "5", "hts_avls": "4200000", "stck_sdpr": "70000"}}))
    q = c.get_quote("005930")
    assert q.price == 71000 and q.vwap == 70000 and q.program_net_qty == 5 and q.base_price == 70000


def test_expired_token_is_reissued(tmp_path):
    c, s = make(tmp_path)
    s.add("/oauth2/tokenP", token_resp(), token_resp())
    s.add("/uapi/domestic-stock/v1/quotations/inquire-price",
          FakeResp(500, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}),
          FakeResp(200, {**OK, "output": {"stck_prpr": "100"}}))
    assert c.get_quote("005930").price == 100
    assert sum(1 for _, p, _ in s.calls if p == "/oauth2/tokenP") == 2


def test_business_error_raises(tmp_path):
    c, s = make(tmp_path)
    s.add("/oauth2/tokenP", token_resp())
    s.add("/uapi/domestic-stock/v1/trading/order-cash",
          FakeResp(200, {"rt_cd": "1", "msg_cd": "APBK0986", "msg1": "장운영시간이 아닙니다"}))
    with pytest.raises(KisApiError, match="장운영시간"):
        c.place_order("buy", "005930", 1, 1000)


def test_balance_pagination(tmp_path):
    c, s = make(tmp_path)
    s.add("/oauth2/tokenP", token_resp())
    row = {"pdno": "000001", "hldg_qty": "1", "ord_psbl_qty": "1", "pchs_avg_pric": "1", "prpr": "1"}
    s.add("/uapi/domestic-stock/v1/trading/inquire-balance",
          FakeResp(200, {**OK, "output1": [row], "output2": [{}], "ctx_area_fk100": "FK", "ctx_area_nk100": "NK"},
                   {"tr_cont": "M"}),
          FakeResp(200, {**OK, "output1": [dict(row, pdno="000002")], "output2": [{"tot_evlu_amt": "2"}]},
                   {"tr_cont": "D"}))
    a = c.get_balance()
    assert [h.code for h in a.holdings] == ["000001", "000002"]
    _, _, kw = s.calls[-1]
    assert kw["headers"]["tr_cont"] == "N" and kw["params"]["CTX_AREA_NK100"] == "NK"


def test_minute_bars_paging_since_open(tmp_path, monkeypatch):
    from datetime import datetime
    import trader.kis_client as kc
    fixed = datetime(2026, 11, 27, 10, 0, tzinfo=kc.KST)
    monkeypatch.setattr(kc, "now_kst", lambda: fixed)
    c, s = make(tmp_path)
    s.add("/oauth2/tokenP", token_resp())

    def page(end_h, end_m):
        rows = []
        h, m = end_h, end_m
        for _ in range(30):
            if (h, m) < (9, 0):
                break
            rows.append({"stck_bsop_date": "20261127", "stck_cntg_hour": f"{h:02d}{m:02d}00",
                         "stck_prpr": "100", "stck_oprc": "100", "stck_hgpr": "101", "stck_lwpr": "99",
                         "cntg_vol": "10"})
            m -= 1
            if m < 0:
                h, m = h - 1, 59
        return FakeResp(200, {**OK, "output1": {}, "output2": rows})

    s.add("/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
          page(10, 0), page(9, 30), page(9, 0))
    bars = c.get_minute_bars_since("005930", "090000")
    assert bars[0].time == "090000" and bars[-1].time == "100000" and len(bars) == 61

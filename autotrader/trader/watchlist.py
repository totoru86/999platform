"""Claude 장전 분석 결과(watchlist) 로딩·검증.

Claude Code 가 /premarket 명령으로 data/watchlist/YYYY-MM-DD.json 을 작성하면
엔진이 읽어서 종목 우선순위·회피 종목·시장판단(사이즈 축소)에 반영합니다.
Claude 는 '더 보수적으로'만 만들 수 있고, 리스크 한도를 올릴 수는 없습니다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

CODE_RE = re.compile(r"^[0-9A-Z]{6}$")
VIEWS = {"risk_on", "neutral", "risk_off"}
FRESHNESS = {"new", "follow_up", "rehash", "none"}

EXAMPLE = {
    "date": "2026-11-27",
    "market_view": "neutral",
    "summary": "미 증시 혼조, 반도체 강세 / 2차전지 약세. 거래대금 상위는 반도체·조선 중심.",
    "candidates": [
        {"code": "000660", "name": "SK하이닉스", "score": 80, "theme": "반도체",
         "news_freshness": "new", "priced_in": False,
         "reason": "오늘 새벽 첫 보도된 HBM 공급계약 — 신선한 재료, 프로그램 순매수 유입"},
    ],
    "avoid": [
        {"code": "123456", "reason": "어제와 같은 재탕 뉴스, 이미 3일 연속 상승으로 선반영"},
    ],
}


@dataclass
class WatchItem:
    code: str
    name: str = ""
    score: float = 50
    theme: str = ""
    news_freshness: str = "none"
    priced_in: bool = False
    reason: str = ""


@dataclass
class Watchlist:
    date: str
    market_view: str = "neutral"
    summary: str = ""
    candidates: list[WatchItem] = field(default_factory=list)
    avoid: dict[str, str] = field(default_factory=dict)

    def score(self, code: str) -> float | None:
        for c in self.candidates:
            if c.code == code:
                # 재탕 뉴스·선반영은 점수 감점 (블로그: 처음 나온 뉴스가 임팩트가 강하다)
                penalty = (20 if c.news_freshness == "rehash" else 0) + (15 if c.priced_in else 0)
                return max(0.0, c.score - penalty)
        return None

    @property
    def codes(self) -> list[str]:
        return [c.code for c in self.candidates]


def validate(data: dict, today: date | None = None) -> tuple[Watchlist | None, list[str]]:
    errs: list[str] = []
    if not isinstance(data, dict):
        return None, ["최상위는 JSON 객체여야 합니다"]
    d = str(data.get("date", ""))
    if today and d != today.isoformat():
        errs.append(f"date({d})가 오늘({today.isoformat()})이 아닙니다 — 지난 분석은 사용하지 않습니다")
    view = data.get("market_view", "neutral")
    if view not in VIEWS:
        errs.append(f"market_view 는 {sorted(VIEWS)} 중 하나")
    items = []
    for i, c in enumerate(data.get("candidates") or []):
        code = str(c.get("code", "")).strip()
        if not CODE_RE.match(code):
            errs.append(f"candidates[{i}].code '{code}' 는 6자리 종목코드여야 합니다")
            continue
        fr = c.get("news_freshness", "none")
        if fr not in FRESHNESS:
            errs.append(f"candidates[{i}].news_freshness 는 {sorted(FRESHNESS)} 중 하나")
            fr = "none"
        try:
            score = float(c.get("score", 50))
        except (TypeError, ValueError):
            errs.append(f"candidates[{i}].score 는 숫자")
            score = 50
        if not 0 <= score <= 100:
            errs.append(f"candidates[{i}].score 는 0~100")
        items.append(WatchItem(code=code, name=str(c.get("name", "")), score=min(max(score, 0), 100),
                               theme=str(c.get("theme", "")), news_freshness=fr,
                               priced_in=bool(c.get("priced_in", False)), reason=str(c.get("reason", ""))))
    avoid = {}
    for i, a in enumerate(data.get("avoid") or []):
        code = str(a.get("code", "")).strip()
        if not CODE_RE.match(code):
            errs.append(f"avoid[{i}].code '{code}' 는 6자리 종목코드여야 합니다")
            continue
        avoid[code] = str(a.get("reason", ""))
    if errs:
        return None, errs
    return Watchlist(date=d, market_view=view, summary=str(data.get("summary", "")),
                     candidates=items, avoid=avoid), []


def path_for(data_dir: Path, day: date) -> Path:
    return data_dir / "watchlist" / f"{day.isoformat()}.json"


def load(data_dir: Path, day: date) -> tuple[Watchlist | None, list[str]]:
    p = path_for(data_dir, day)
    if not p.exists():
        return None, [f"{p} 없음 (Claude /premarket 미실행) — 숫자 기준만으로 매매합니다"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, [f"{p} 읽기 실패: {e}"]
    return validate(data, day)

---
description: 장 전 분석 실행 → 오늘의 watchlist 작성 (08:00~09:00 권장)
---
지금 한국시간 기준으로 오늘의 장 전 분석을 시작한다. 추가 지시: $ARGUMENTS

1. `python -m trader check` 로 접속 상태만 확인 (실패하면 원인 보고 후 중단).
2. 다음 세 담당을 **병렬로** 실행한다:
   - `market-analyst`: 시장 분위기와 market_view
   - `stock-screener`: 거래대금·수급·숫자 기준 후보 5~12개
   - (스크리너 결과가 나오면) `news-analyst` 와 `technical-analyst` 에 후보 코드 목록을 넘겨 병렬 분석
3. `trade-strategist` 가 결과를 종합해 `data/watchlist/<오늘 YYYY-MM-DD>.json` 을 작성하고
   `python -m trader watchlist validate` 로 검증한다.
4. 최종 보고 (한국어, 간결하게):
   - 오늘의 market_view 와 근거 3줄
   - 후보 표: 코드 | 종목 | 점수 | 뉴스신선도 | 선반영 | 한줄근거
   - 회피 종목과 사유
   - "엔진 실행: `python -m trader run` (모의투자)" 안내

규칙: 주문은 내지 않는다. `.env` 는 읽지 않는다. 확인 못한 정보는 "확인 불가".

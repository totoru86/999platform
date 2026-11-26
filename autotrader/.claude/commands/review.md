---
description: 장 마감 후 성과 리뷰 (승률 통계, 조건별 비교, 개선 제안 1개)
---
`performance-reviewer` 담당으로 오늘과 최근 30일 매매를 리뷰한다. 추가 지시: $ARGUMENTS

1. `python -m trader report --days 1` 로 오늘 결과, `python -m trader report --days 30 --json` 으로 누적 통계
2. 오늘 watchlist(`data/watchlist/<오늘>.json`)의 추천 종목이 실제로 어떻게 움직였는지 `python -m trader quote` 로 확인
   (추천했지만 엔진이 진입 안 한 종목 → 왜 안 했는지 로그에서 확인)
3. 보고: 요약 → 조건별 승률 표 → 개선 제안 1개 (config 키, 현재값→제안값, 근거)
4. config.yaml 은 사용자가 승인할 때만 수정한다. 리스크 한도 완화는 제안하지 않는다.
5. 마지막에 백테스트용 분봉 수집을 안내: `python -m trader collect` (15:40 이후)

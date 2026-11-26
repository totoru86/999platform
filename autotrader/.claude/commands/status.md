---
description: 장중 현재 상태 점검 (보유 종목, 오늘 손익, 엔진 상태)
---
읽기 전용으로 현재 상태를 점검한다.

1. `data/state/` 의 오늘 상태 파일(paper.json / live.json / dry_run.json 중 최신)을 읽어 보유 포지션·손절가·익절가·오늘 실현손익·중단 사유 요약
2. 보유 종목은 `python -m trader quote <코드>` 로 현재가 확인 → 평가손익 계산
3. 오늘 로그(`data/logs/<YYYYMMDD>.log`)의 마지막 30줄에서 오류·경고 요약
4. `data/STOP` 파일 존재 여부 (비상정지 상태인지)

보고는 표 하나 + 3줄 요약. 사용자가 비상정지를 원하면 `python -m trader stop` 을 안내(실행은 사용자 확인 후).

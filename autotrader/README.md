# KIS 자동매매 + Claude Code 장전분석

한국투자증권 **KIS Open API**로 국내주식 당일매매(데이트레이딩)를 자동화하고,
**Claude Code**가 장 전에 시장·뉴스·차트를 분석해 "오늘 볼 종목 / 피할 종목"을 정해주는 시스템입니다.

```
 08:20  Claude Code  /premarket ──► 시장분석 · 종목스크리닝 · 뉴스신선도 · 기술분석 (에이전트 병렬)
                                   └► data/watchlist/오늘.json  (추천·회피·시장판단)
 08:50  python -m trader run ─────► 엔진이 watchlist + 실시간 숫자 기준으로 매매
          09:00~09:15  오프닝 레인지(OR) 형성 — 진입 없음
          09:15~14:30  OR 고가 돌파 + VWAP 위 + 거래량 급증 → 매수 (11:30~13:00 진입 금지)
                       손절 -1.5% / 익절 +3% / 추적손절 / VWAP 이탈 청산
          15:15        남은 종목 전량 청산 (오버나잇 없음)
 15:40  python -m trader collect ─► 분봉 저장 (백테스트용)
        Claude Code  /review ─────► 조건별 승률 통계 + 개선 제안 1개
```

**역할 분담이 핵심입니다.** Claude는 *분석*만 하고, *주문*은 하드 리스크 한도가 걸린 Python 엔진만 냅니다.
Claude의 판단은 엔진을 **더 보수적으로만** 바꿀 수 있습니다 (회피 종목 제외, 우선순위, `risk_off` 시 비중 절반).

---

## ⚠️ 먼저 읽어주세요 — 현실적인 기대치

- **왕복 거래비용이 약 0.23%** 입니다 (수수료 0.015%×2 + 매도 세금 0.20%). 한 번 매매할 때마다 이만큼 불리하게 시작합니다.
- 개인 단타의 대부분은 장기적으로 손실이 납니다. "하루 조금씩"이 **매일** 되지는 않습니다. 목표는 *손실일을 작게, 수익일을 조금 크게* 만드는 것이고, 그게 맞는지는 데이터로만 확인됩니다.
- 이 전략(ORB+VWAP)은 출발점일 뿐, 수익이 검증된 전략이 아닙니다. **모의투자에서 최소 30~50건 매매해서 기대값이 비용 차감 후 플러스인지 확인**하기 전에는 실전에 쓰지 마세요.
- 투자 판단과 손익의 책임은 본인에게 있습니다.

---

## 1. 준비물

| 항목 | 내용 |
|---|---|
| 한국투자증권 계좌 | 비대면 개설 가능. **자동매매 전용 계좌를 따로** 쓰는 걸 권장 (엔진은 자기가 산 종목만 팔지만, 섞이면 헷갈립니다) |
| KIS Open API 신청 | [KIS Developers](https://apiportal.koreainvestment.com) → API 신청 → **실전 앱키**와 **모의투자 앱키**를 각각 발급 (모의투자 계좌도 신청) |
| Python 3.10 이상 | Windows는 python.org 설치 시 "Add to PATH" 체크 |
| Claude Code | 장 전 분석/리뷰용. 이 폴더에서 `claude` 실행 |
| (선택) 텔레그램 봇 | @BotFather 로 봇 생성 → 토큰, 본인 chat_id |

> 실행은 **본인 PC(또는 항상 켜둔 미니PC/클라우드 서버)** 에서 하세요. 장중 6시간 동안 켜져 있어야 합니다.

## 2. 설치

```bash
git clone https://github.com/totoru86/999platform.git
```

```bash
cd 999platform/autotrader
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

```bash
cp config.example.yaml config.yaml
```

(Windows 명령 프롬프트에서는 `cp` 대신 `copy`)

`.env` 에 앱키·계좌번호를 넣습니다. **처음엔 `KIS_MODE=paper` 그대로 두세요.**
실전 앱키(`KIS_REAL_*`)도 넣어두면 시세·거래대금순위는 실전 서버에서 조회합니다 (조회는 주문이 아니라 안전하고, 모의투자 서버는 호출 한도가 낮습니다).

`config.yaml` 에서 최소한 이것만 확인:
- `risk.capital_krw` — 자동매매에 쓸 돈 (모의투자도 실전과 같은 금액으로 연습하세요)
- `costs.commission_pct` — 본인 계좌 수수료율

## 3. 점검

```bash
python -m trader check
```

설정 검사 → 잔고 조회 → 삼성전자 시세 → 거래대금 순위까지 확인합니다. `✅ 준비 완료` 가 나오면 됩니다.
오류가 나면 Claude Code에 이렇게 물어보세요 (블로그 팁 그대로):

```
python -m trader check 에서 이런 오류가 납니다: [오류 메시지]
공식 레포 https://github.com/koreainvestment/open-trading-api 의 examples_llm/domestic_stock 를 참고해서 원인을 찾고 고쳐줘.
```

## 4. 하루 루틴

| 시각 | 할 일 | 명령 |
|---|---|---|
| 08:20 | 장 전 분석 (Claude) | 이 폴더에서 `claude` 실행 → `/premarket` |
| 08:50 | 엔진 시작 (모의투자) | `python -m trader run` |
| 장중 | 텔레그램 알림 확인. 상태 보기 | Claude에서 `/status` |
| 비상시 | 신규진입 중단 + 전량 청산 | `python -m trader stop` (해제: `resume`) |
| 15:40 | 분봉 수집 (백테스트 데이터) | `python -m trader collect` |
| 15:45 | 성과 리뷰 (Claude) | `/review` |

엔진은 장 시작 전에 켜두면 09:00까지 대기하고, 15:35에 스스로 종료합니다. 주말·휴장일엔 바로 종료합니다.
엔진이 장중에 꺼졌다 다시 켜져도 `data/state/` 에서 오늘 포지션을 복구해 이어서 관리합니다.

### 자동 실행 (선택)

**Windows 작업 스케줄러** — 평일(월~금) 트리거로 3개 작업 등록, "시작 위치"는 `autotrader` 폴더:

| 시각 | 프로그램 | 인수 |
|---|---|---|
| 08:20 | `claude` | `-p "/premarket"` |
| 08:50 | `python` | `-m trader run` |
| 15:40 | `python` | `-m trader collect` |

**macOS / Linux (cron)** — `crontab -e`:

```bash
20 8 * * 1-5 cd ~/999platform/autotrader && claude -p "/premarket" >> data/logs/premarket.log 2>&1
50 8 * * 1-5 cd ~/999platform/autotrader && python -m trader run >> data/logs/engine.log 2>&1
40 15 * * 1-5 cd ~/999platform/autotrader && python -m trader collect >> data/logs/collect.log 2>&1
```

`claude -p` 로 돌릴 때 필요한 조회 명령·웹검색·watchlist 쓰기는 `.claude/settings.json` 에 미리 허용돼 있고,
`.env` 읽기와 `run --live` 는 차단돼 있습니다.

## 5. 매매 기준 (config.yaml)

블로그의 "숫자로 말하기" 원칙을 그대로 설정값으로 만들었습니다.

**종목 선정** (`universe`)
- 당일 거래대금 순위 **10위 이내** + Claude 추천 종목
- 누적 거래대금: 09:15 150억 → 09:30 250억 → 10:30 400억 → 12:00 600억 → 14:00 800억 이상 (마감 기준 1,000억을 시간대별로 환산)
- 시총 3,000억 이상, 주가 2,000원 이상 (동전주 제외), 등락률 +2%~+20%
- **프로그램 순매수 > 0** 필수, 외국인 순매수는 선택 (장중 집계는 추정치라 기본 off)
- 투자경고/주의/관리/거래정지/ETF/ETN/우선주/스팩 제외, 52주 신고가 근접 태그
- 5분마다 재선정 (테마 순환 대응)

**진입/청산** (`strategy`)
- 09:00~09:15 고가(OR)를 **1분봉 종가로 새로 돌파** + 종가 > VWAP + 거래량 ≥ 직전 10봉 평균 × 1.5
- 상한가 7% 이내 추격 금지, 종목당 하루 1회 (재진입·물타기 없음)
- 손절: OR 저가와 -1.5% 중 가까운 쪽 / 익절 +3% / +1.5% 도달 후 고점 대비 -1% 추적손절(최소 본전) / VWAP 이탈

**리스크 한도** (`risk`, Claude도 완화 불가)

| 한도 | 기본값 | 의미 |
|---|---|---|
| `risk_per_trade_pct` | 0.5% | 1회 손절 시 최대 손실 = 운용자금의 0.5% |
| `max_order_krw` | 200만원 | 1회 주문 최대 금액 |
| `max_open_positions` | 2 | 동시 보유 종목 수 |
| `max_trades_per_day` | 4 | 하루 최대 매매 |
| `max_consecutive_losses` | 2 | 2연패 → 그날 종료 |
| `max_daily_loss_pct` | 1.0% | 일 손실 -1% → 진입 중단 + 전량 청산 |
| `daily_profit_target_pct` | 1.0% | 일 수익 +1% → 그날은 더 진입 안 함 ("조금씩 벌기") |

## 6. 실전 전환 단계

1. **드라이런 (1주)** — `python -m trader run --dry-run`: 실시간 시세로 판단하되 주문 없이 가상 체결. 로직·알림·로그 확인용
2. **모의투자 (4~8주)** — `python -m trader run`: KIS 모의계좌로 실제 주문
3. **판정** — `python -m trader report --days 60 --mode paper`
   - 매매 30건 이상, **1회 기대값(비용 차감) > 0**, Profit Factor > 1.2, 최악의 날이 일 손실한도 안쪽
   - 하나라도 못 미치면 실전 금지 → `/review` 로 원인 분석, 파라미터는 **한 번에 하나만** 바꾸고 다시 모의투자
4. **실전 소액** — `.env`: `KIS_MODE=live`, `AUTOTRADER_LIVE_CONFIRM=I_ACCEPT_THE_RISK` /
   `config.yaml`: `capital_krw` 100~300만원, `max_order_krw` 50만원 → `python -m trader run --live`
5. 실전에서도 한 달 이상 모의투자와 비슷한 성과가 나오면 그때 조금씩 증액

## 7. 명령어

```
python -m trader check | balance | index | rank | screen | quote CODE.. | daily CODE | news CODE
python -m trader run [--dry-run | --live]
python -m trader stop | resume
python -m trader report [--days 30] [--mode paper] [--json]
python -m trader collect [--top 30]
python -m trader backtest [--from 20261201] [--to 20261231] [--capital 5000000]
python -m trader watchlist validate | show | example
python -m pytest -q        # 테스트
```

## 8. 안전장치

- 기본 모드는 모의투자. 실전은 `.env` 의 `KIS_MODE=live` + 확인문구 + `--live` 플래그 + 10초 카운트다운 **4중 확인**
- 주문 요청이 네트워크 오류면 **재시도하지 않음** (중복 주문 방지) → 체결조회로 확인
- 매수 지정가는 현재가+2호가까지만, 20초 미체결 시 자동 취소
- 엔진은 **자기가 산 종목만** 매도 (계좌의 다른 보유 종목은 건드리지 않음)
- 비상정지 파일(`data/STOP`), API 연속 오류 10회 시 중단, 15:15 강제 청산
- 접근토큰은 파일 캐시 후 재사용 (발급 1분 1회 제한 대응), 토큰 만료 시 자동 재발급
- 매매일지(SQLite)에 모든 체결·이벤트 기록 → 조건별 승률 자동 집계

## 9. 한계

- **REST 폴링(5초)** 방식이라 초단타에는 부적합합니다. 손절도 5초 단위로 확인되므로 급락 시 손절가보다 불리하게 체결될 수 있습니다.
- **백테스트는 낙관적**입니다: 수집 종목이 '장 마감 기준 거래대금 상위'라 선택편향이 있고, 장중 수급은 재현되지 않으며, 체결은 1분봉 종가+슬리피지로 가정합니다.
- 모의투자 체결은 실전보다 후합니다 (호가 공백·부분체결이 덜함).
- 증권거래세율·수수료는 바뀔 수 있으니 `costs` 값을 본인 계좌 기준으로 확인하세요.
- KIS API 사양이 바뀌면 `trader/kis_client.py` 상단의 TR ID·엔드포인트를 공식 레포 기준으로 업데이트해야 합니다.

## 10. 파일 구조

```
autotrader/
├─ CLAUDE.md              Claude Code 작업 규칙 (주문 금지, .env 금지, 리스크 완화 금지)
├─ .claude/
│  ├─ agents/             시장분석·종목스크리닝·뉴스분석·기술분석·매매전략·성과리뷰 에이전트
│  ├─ commands/           /premarket  /review  /status
│  └─ settings.json       허용/차단 권한
├─ config.example.yaml    전략·리스크 설정 템플릿
├─ .env.example           앱키·계좌 템플릿
├─ trader/
│  ├─ kis_client.py       KIS REST (토큰·시세·순위·뉴스·잔고·주문·체결조회)
│  ├─ screener.py         종목 선정 필터
│  ├─ strategy.py         ORB+VWAP 진입/청산 (순수 로직)
│  ├─ risk.py             하드 리스크 한도
│  ├─ engine.py           시간대별 매매 엔진 (실시간·백테스트 공용)
│  ├─ broker.py / feed.py KIS 주문·시세 / 시뮬레이션·리플레이
│  ├─ watchlist.py        Claude 분석 결과 검증
│  ├─ journal.py          매매일지 + 승률 통계
│  └─ backtest.py         분봉 수집 + 백테스트
└─ tests/                 52개 테스트 (합성 장중 데이터로 엔진 전체 재생 포함)
```

참고: [KIS Developers](https://apiportal.koreainvestment.com/about-devcenter) ·
[KIS Open API 공식 GitHub](https://github.com/koreainvestment/open-trading-api)
